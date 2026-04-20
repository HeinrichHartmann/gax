"""Google Forms sync for gax.

Implements clone/pull/plan/apply for Google Forms definitions (ADR 014).

Module structure
================

  FormHeader           — dataclass for .form.gax.md frontmatter
  File format          — parse/format .form.gax.md files
  Google Forms helpers — API wrappers and diff/patch logic
  Form(Resource)       — resource class (the public interface for cli.py)

Design decisions
================

Separation of concerns:
  This module contains ONLY business logic. No Click, no sys.exit(), no
  UI imports. The CLI layer (cli.py) owns all command definitions, argument
  parsing, confirmation prompts, and user-facing output.

Communication conventions:
  - logging.info()  — status messages (picked up by the spinner)
  - ValueError      — user-fixable errors (cli.py catches and formats)
  - Return values   — results for cli.py to format (e.g. Path from clone)

The plan/apply workflow maps onto the Resource interface:
  - diff()  computes and returns a human-readable change summary (or None)
  - push()  computes and applies changes via batchUpdate
  No intermediate plan file — the CLI plan command just prints diff().
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from googleapiclient.discovery import build

from ..gaxfile import GaxFile, format_single
from ..auth import get_authenticated_credentials
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data class
# =============================================================================


@dataclass
class FormHeader:
    """Frontmatter of a .form.gax.md file."""

    id: str = ""
    title: str = ""
    source: str = ""
    synced: str = ""
    content_type: str = "text/markdown"


# =============================================================================
# File format — parse/format .form.gax.md files.
# =============================================================================


def parse_form_file(file_path: Path) -> tuple[FormHeader, str]:
    """Parse a .form.gax.md file into header and body.

    Returns:
        Tuple of (FormHeader, body_content)
    """
    gf = GaxFile.from_path(file_path, multipart=False)
    h = gf.headers

    header = FormHeader(
        id=h.get("id", ""),
        title=h.get("title", ""),
        source=h.get("source", ""),
        synced=h.get("synced", ""),
        content_type=h.get("content-type", "text/markdown"),
    )

    return header, gf.body.strip()


def parse_form_body(body: str) -> dict:
    """Parse the YAML body of a .form.gax.md file.

    Only works with content-type: application/yaml files.
    Returns the parsed body dict.
    """
    return yaml.safe_load(body) or {}


def format_form_file(header: FormHeader, body_str: str) -> str:
    """Format a form header and body as .form.gax.md content."""
    h: dict[str, Any] = {"type": "gax/form"}

    if header.id:
        h["id"] = header.id
    if header.title:
        h["title"] = header.title
    if header.source:
        h["source"] = header.source
    if header.synced:
        h["synced"] = header.synced

    h["content-type"] = header.content_type

    return format_single(h, body_str)


# =============================================================================
# Google Forms API helpers
# =============================================================================


def extract_form_id(url: str) -> str:
    """Extract form ID from Google Forms URL or return as-is.

    Supports:
    - https://docs.google.com/forms/d/{FORM_ID}/edit
    - https://docs.google.com/forms/d/{FORM_ID}/viewform
    - Raw form ID
    """
    match = re.search(r"/forms/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Cannot extract form ID from: {url}")


def get_form(form_id: str, *, service=None) -> dict:
    """Fetch form definition from Google Forms API."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("forms", "v1", credentials=creds)

    return service.forms().get(formId=form_id).execute()


def form_to_yaml(form: dict, source_url: str) -> str:
    """Convert Forms API response to YAML body (round-trip safe)."""
    info = form.get("info", {})
    title = info.get("title", "Untitled Form")
    document_title = info.get("documentTitle", title)
    description = info.get("description", "")

    info_body: dict[str, Any] = {}
    if "title" in info:
        info_body["title"] = title
    if description:
        info_body["description"] = description

    body: dict[str, Any] = {
        "documentTitle": document_title,
    }
    if info_body:
        body["info"] = info_body

    settings = form.get("settings", {})
    if settings:
        body["settings"] = settings

    items = form.get("items", [])
    if items:
        body["items"] = items

    linked_sheet = form.get("linkedSheetId")
    if linked_sheet:
        body["linkedSheetId"] = linked_sheet

    responder_uri = form.get("responderUri")
    if responder_uri:
        body["responderUri"] = responder_uri

    return yaml.dump(
        body, default_flow_style=False, allow_unicode=True, sort_keys=False
    )


def form_to_markdown(form: dict, source_url: str) -> str:
    """Convert Forms API response to readable markdown body (view-only)."""
    info = form.get("info", {})
    title = info.get("title", "Untitled Form")
    description = info.get("description", "")

    lines = []
    lines.append(f"# {title}")
    lines.append("")

    if description:
        lines.append(description)
        lines.append("")

    settings = form.get("settings", {})
    quiz_settings = settings.get("quizSettings", {})
    if quiz_settings.get("isQuiz"):
        lines.append("_This is a quiz._")
        lines.append("")

    items = form.get("items", [])
    question_num = 0

    for item in items:
        item_title = item.get("title", "")
        item_desc = item.get("description", "")

        if "pageBreakItem" in item:
            lines.append("---")
            lines.append("")
            if item_title:
                lines.append(f"### {item_title}")
                lines.append("")
            logger.info(f"Processing page break: {item_title or '(untitled)'}")
            continue

        if "textItem" in item:
            if item_title:
                lines.append(f"### {item_title}")
                if item_desc:
                    lines.append("")
                    lines.append(item_desc)
                lines.append("")
            logger.info(f"Processing text: {item_title or '(untitled)'}")
            continue

        if "imageItem" in item:
            image = item.get("imageItem", {}).get("image", {})
            source_uri = image.get("sourceUri", "")
            alt_text = image.get("altText", "Image")
            if source_uri:
                lines.append(f"![{alt_text}]({source_uri})")
            else:
                lines.append(f"_[Image: {alt_text}]_")
            lines.append("")
            logger.info(f"Processing image: {alt_text}")
            continue

        if "videoItem" in item:
            video = item.get("videoItem", {}).get("video", {})
            youtube_uri = video.get("youtubeUri", "")
            if youtube_uri:
                lines.append(f"[Video: {youtube_uri}]")
            else:
                lines.append("_[Video]_")
            lines.append("")
            logger.info("Processing video item")
            continue

        if "questionItem" in item:
            question_num += 1
            question = item.get("questionItem", {}).get("question", {})
            required = question.get("required", False)
            required_marker = " *" if required else ""

            lines.append(f"## {question_num}. {item_title}{required_marker}")
            lines.append("")

            if item_desc:
                lines.append(f"_{item_desc}_")
                lines.append("")

            if "textQuestion" in question:
                text_q = question["textQuestion"]
                if text_q.get("paragraph"):
                    lines.append("_Long answer text_")
                else:
                    lines.append("_Short answer text_")
                lines.append("")

            elif "scaleQuestion" in question:
                scale = question["scaleQuestion"]
                low = scale.get("low", 1)
                high = scale.get("high", 5)
                low_label = scale.get("lowLabel", "")
                high_label = scale.get("highLabel", "")
                low_str = f"{low}" + (f" ({low_label})" if low_label else "")
                high_str = f"{high}" + (f" ({high_label})" if high_label else "")
                lines.append(f"Scale: {low_str} - {high_str}")
                lines.append("")

            elif "choiceQuestion" in question:
                choice = question["choiceQuestion"]
                q_type = choice.get("type", "RADIO")
                options = choice.get("options", [])
                shuffle = choice.get("shuffle", False)

                for opt in options:
                    value = opt.get("value", "")
                    is_other = opt.get("isOther", False)
                    if is_other:
                        value = "Other..."
                    if q_type == "CHECKBOX":
                        lines.append(f"- [ ] {value}")
                    elif q_type == "DROP_DOWN":
                        lines.append(f"  - {value}")
                    else:
                        lines.append(f"- ( ) {value}")

                if shuffle:
                    lines.append("")
                    lines.append("_(Options shuffled)_")
                lines.append("")

            elif "dateQuestion" in question:
                date_q = question["dateQuestion"]
                include_time = date_q.get("includeTime", False)
                include_year = date_q.get("includeYear", True)
                if include_time:
                    lines.append("_Date and time_")
                elif not include_year:
                    lines.append("_Date (month/day only)_")
                else:
                    lines.append("_Date_")
                lines.append("")

            elif "timeQuestion" in question:
                time_q = question["timeQuestion"]
                duration = time_q.get("duration", False)
                if duration:
                    lines.append("_Duration_")
                else:
                    lines.append("_Time_")
                lines.append("")

            elif "fileUploadQuestion" in question:
                file_q = question["fileUploadQuestion"]
                max_files = file_q.get("maxFiles", 1)
                types = file_q.get("types", [])
                type_str = ", ".join(types) if types else "any"
                lines.append(f"_File upload ({type_str}, max {max_files} files)_")
                lines.append("")

            elif "rowQuestion" in question:
                pass

            logger.info(f"Processing question {question_num}: {item_title}")
            continue

        if "questionGroupItem" in item:
            question_num += 1
            group = item.get("questionGroupItem", {})
            grid = group.get("grid", {})
            columns = grid.get("columns", {}).get("options", [])

            lines.append(f"## {question_num}. {item_title}")
            lines.append("")

            if item_desc:
                lines.append(f"_{item_desc}_")
                lines.append("")

            col_values = [c.get("value", "") for c in columns]
            lines.append("| | " + " | ".join(col_values) + " |")
            lines.append("|---" + "|---" * len(col_values) + "|")

            questions = group.get("questions", [])
            for q in questions:
                row_q = q.get("rowQuestion", {})
                row_title = row_q.get("title", "")
                cells = " | ".join(["( )" for _ in col_values])
                lines.append(f"| {row_title} | {cells} |")

            lines.append("")
            logger.info(f"Processing grid question {question_num}: {item_title}")
            continue

    return "\n".join(lines)


# =============================================================================
# Diff/patch helpers — compute changes between local and remote forms.
# =============================================================================


def _strip_ids(obj):
    """Recursively strip ID fields from an object for comparison."""
    if isinstance(obj, dict):
        return {
            k: _strip_ids(v)
            for k, v in obj.items()
            if k not in ("itemId", "questionId")
        }
    elif isinstance(obj, list):
        return [_strip_ids(item) for item in obj]
    else:
        return obj


def _items_equal(item1: dict, item2: dict) -> bool:
    """Check if two form items are equivalent (ignoring IDs)."""
    return _strip_ids(item1) == _strip_ids(item2)


def _generate_create_request(item: dict, index: int) -> dict:
    """Generate a createItem request for the Forms API."""
    api_item: dict[str, Any] = {}

    if "title" in item:
        api_item["title"] = item["title"]
    if "description" in item:
        api_item["description"] = item["description"]

    if "questionItem" in item:
        question = item["questionItem"].get("question", {})
        api_question = {k: v for k, v in question.items() if k != "questionId"}
        api_item["questionItem"] = {"question": api_question}

    if "pageBreakItem" in item:
        pb = item["pageBreakItem"]
        if isinstance(pb, dict) and "title" in pb:
            api_item["title"] = pb["title"]
        api_item["pageBreakItem"] = {}
    if "textItem" in item:
        api_item["textItem"] = item["textItem"]
    if "imageItem" in item:
        api_item["imageItem"] = item["imageItem"]
    if "videoItem" in item:
        api_item["videoItem"] = item["videoItem"]

    return {
        "createItem": {
            "item": api_item,
            "location": {"index": index},
        }
    }


def _generate_update_request(item: dict, index: int) -> dict:
    """Generate an updateItem request for the Forms API."""
    api_item: dict[str, Any] = {"itemId": item["itemId"]}

    if "title" in item:
        api_item["title"] = item["title"]
    if "description" in item:
        api_item["description"] = item["description"]

    if "questionItem" in item:
        api_item["questionItem"] = item["questionItem"]

    mask_parts = []
    if "title" in item:
        mask_parts.append("title")
    if "description" in item:
        mask_parts.append("description")
    if "questionItem" in item:
        mask_parts.append("questionItem")

    return {
        "updateItem": {
            "item": api_item,
            "location": {"index": index},
            "updateMask": ",".join(mask_parts),
        }
    }


def _generate_delete_request(index: int) -> dict:
    """Generate a deleteItem request for the Forms API."""
    return {
        "deleteItem": {
            "location": {"index": index},
        }
    }


def _resolve_form_id(header: FormHeader) -> str:
    """Extract form ID from header, trying id field then source URL."""
    if header.id:
        return header.id
    if header.source:
        return extract_form_id(header.source)
    raise ValueError("No form ID found in file")


def _compute_changes(local_body: dict, remote_form: dict) -> dict:
    """Compute changes between local YAML body and remote form.

    Returns a dict with keys: create, update, delete, move,
    update_info, update_settings — each populated only if changes exist.
    """
    local_items = local_body.get("items", [])
    remote_items = remote_form.get("items", [])

    remote_by_id: dict[str, dict] = {}
    for idx, item in enumerate(remote_items):
        item_id = item.get("itemId")
        if item_id:
            remote_by_id[item_id] = {"item": item, "index": idx}

    changes: dict[str, Any] = {
        "create": [],
        "update": [],
        "delete": [],
        "move": [],
    }

    # Check for info changes.
    # The local YAML stores info without documentTitle (it's at top level),
    # so strip documentTitle from remote info before comparing.
    local_info = local_body.get("info", {})
    remote_info = {
        k: v for k, v in remote_form.get("info", {}).items() if k != "documentTitle"
    }
    if local_info != remote_info:
        changes["update_info"] = local_info

    # Check for settings changes
    local_settings = local_body.get("settings", {})
    remote_settings = remote_form.get("settings", {})
    if local_settings != remote_settings:
        changes["update_settings"] = local_settings

    seen_remote_ids: set[str] = set()
    item_types = [
        "questionItem",
        "pageBreakItem",
        "textItem",
        "imageItem",
        "videoItem",
    ]

    for local_idx, local_item in enumerate(local_items):
        item_id = local_item.get("itemId")
        item_title = local_item.get("title", "(untitled)")

        if not item_id:
            logger.info(f"New item: {item_title}")
            changes["create"].append(
                {"index": local_idx, "title": item_title, "item": local_item}
            )
        else:
            seen_remote_ids.add(item_id)
            if item_id in remote_by_id:
                remote_item = remote_by_id[item_id]["item"]
                local_type = next((t for t in item_types if t in local_item), None)
                remote_type = next((t for t in item_types if t in remote_item), None)

                if local_type != remote_type:
                    logger.info(f"Type changed: {item_title}")
                    changes["delete"].append(
                        {
                            "itemId": item_id,
                            "index": remote_by_id[item_id]["index"],
                            "title": remote_item.get("title", "(untitled)"),
                        }
                    )
                    new_item = {k: v for k, v in local_item.items() if k != "itemId"}
                    changes["create"].append(
                        {"index": local_idx, "title": item_title, "item": new_item}
                    )
                elif not _items_equal(local_item, remote_item):
                    logger.info(f"Item changed: {item_title}")
                    changes["update"].append(
                        {
                            "index": local_idx,
                            "itemId": item_id,
                            "title": item_title,
                            "item": local_item,
                        }
                    )

                remote_idx = remote_by_id[item_id]["index"]
                if local_idx != remote_idx:
                    logger.info(f"Item moved: {item_title}")
                    changes["move"].append(
                        {
                            "itemId": item_id,
                            "from_index": remote_idx,
                            "to_index": local_idx,
                            "title": item_title,
                        }
                    )
            else:
                logger.warning(f"Item {item_id} not found remotely, will create new")
                changes["create"].append(
                    {"index": local_idx, "title": item_title, "item": local_item}
                )

    # Remote items not in local -> deletes
    for item_id, remote_data in remote_by_id.items():
        if item_id not in seen_remote_ids:
            logger.info(
                f"Item to delete: {remote_data['item'].get('title', '(untitled)')}"
            )
            changes["delete"].append(
                {
                    "itemId": item_id,
                    "index": remote_data["index"],
                    "title": remote_data["item"].get("title", "(untitled)"),
                }
            )

    # Remove empty lists for cleaner output
    return {k: v for k, v in changes.items() if v}


def _format_changes_summary(changes: dict) -> str | None:
    """Format computed changes as a human-readable summary.

    Returns summary string, or None if no changes.
    """
    has_changes = any(
        k in changes
        for k in (
            "create",
            "update",
            "delete",
            "move",
            "update_info",
            "update_settings",
        )
    )
    if not has_changes:
        return None

    lines = ["Plan:"]

    if "create" in changes:
        lines.append(f"  Create: {len(changes['create'])}")
        for item in changes["create"]:
            lines.append(f"    + [{item['index']}] {item['title']}")
    if "update" in changes:
        lines.append(f"  Update: {len(changes['update'])}")
        for item in changes["update"]:
            lines.append(f"    ~ [{item['index']}] {item['title']}")
    if "delete" in changes:
        lines.append(f"  Delete: {len(changes['delete'])}")
        for item in changes["delete"]:
            lines.append(f"    - [{item['index']}] {item['title']}")
    if "move" in changes:
        lines.append(f"  Move: {len(changes['move'])}")
        for item in changes["move"]:
            lines.append(
                f"    > [{item['from_index']}] -> [{item['to_index']}] {item['title']}"
            )
    if "update_info" in changes:
        lines.append("  Update info: yes")
    if "update_settings" in changes:
        lines.append("  Update settings: yes")

    return "\n".join(lines)


def _apply_changes(form_id: str, changes: dict, *, service=None) -> None:
    """Apply computed changes to a Google Form via batchUpdate."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("forms", "v1", credentials=creds)

    to_create = changes.get("create", [])
    to_update = changes.get("update", [])
    to_delete = changes.get("delete", [])
    to_move = changes.get("move", [])
    update_info = changes.get("update_info")
    update_settings = changes.get("update_settings")

    requests: list[dict] = []

    # Fetch current form for accurate indices
    current_form = service.forms().get(formId=form_id).execute()
    current_items = current_form.get("items", [])

    # Update form info
    if update_info:
        requests.append(
            {
                "updateFormInfo": {
                    "info": update_info,
                    "updateMask": ",".join(update_info.keys()),
                }
            }
        )
        logger.info("Updating form info")

    # Update form settings
    if update_settings:
        requests.append(
            {
                "updateSettings": {
                    "settings": update_settings,
                    "updateMask": ",".join(update_settings.keys()),
                }
            }
        )
        logger.info("Updating form settings")

    # Build itemId -> current index map
    id_to_index: dict[str, int] = {}
    for idx, item in enumerate(current_items):
        if item.get("itemId"):
            id_to_index[item["itemId"]] = idx

    # Updates
    for item in to_update:
        item_id = item.get("itemId")
        if item_id and item_id in id_to_index:
            current_idx = id_to_index[item_id]
            requests.append(_generate_update_request(item["item"], current_idx))
            logger.info(f"Updating: {item['title']}")
        else:
            logger.warning(f"Skipping update (item not found): {item['title']}")

    # Deletes — reverse index order to maintain correct indices
    sorted_deletes = sorted(to_delete, key=lambda x: x["index"], reverse=True)
    for item in sorted_deletes:
        requests.append(_generate_delete_request(item["index"]))
        logger.info(f"Deleting: {item['title']}")

    # Creates — lowest index first
    sorted_creates = sorted(to_create, key=lambda x: x["index"])
    for item in sorted_creates:
        target_idx = item["index"]
        requests.append(_generate_create_request(item["item"], target_idx))
        logger.info(f"Creating: {item['title']} at index {target_idx}")

    # Execute non-move requests
    if requests:
        logger.info(f"Executing {len(requests)} request(s)")
        result = (
            service.forms()
            .batchUpdate(formId=form_id, body={"requests": requests})
            .execute()
        )
        replies = result.get("replies", [])
        created_count = sum(1 for r in replies if "createItem" in r)
        logger.info(f"Done: {len(replies)} operations completed")
        if created_count:
            logger.info(f"Created {created_count} new item(s)")

    # Handle moves separately — one at a time due to index shifting
    if to_move:
        sorted_moves = sorted(to_move, key=lambda x: x["to_index"])
        for move in sorted_moves:
            current_form = service.forms().get(formId=form_id).execute()
            current_items = current_form.get("items", [])

            item_id = move["itemId"]
            current_idx = None
            for idx, item in enumerate(current_items):
                if item.get("itemId") == item_id:
                    current_idx = idx
                    break

            if current_idx is None:
                logger.warning(f"Item {item_id} not found, skipping move")
                continue

            target_idx = move["to_index"]
            if current_idx == target_idx:
                continue

            move_request = {
                "moveItem": {
                    "originalLocation": {"index": current_idx},
                    "newLocation": {"index": target_idx},
                }
            }
            service.forms().batchUpdate(
                formId=form_id, body={"requests": [move_request]}
            ).execute()
            logger.info(f"Moved: {move['title']} [{current_idx}] -> [{target_idx}]")


# =============================================================================
# Resource class — the public interface for cli.py.
# =============================================================================


class Form(Resource):
    """Google Forms resource.

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "form"
    URL_PATTERN = r"docs\.google\.com/forms/d/"
    FILE_TYPE = "gax/form"
    FILE_EXTENSIONS = (".form.gax.md",)
    SCOPES = ("forms.body",)

    def _output_path(self, title: str, output: Path | None) -> Path:
        if output:
            return output
        safe = re.sub(r'[<>:"/\\|?*]', "-", title or "Untitled")
        safe = re.sub(r"\s+", "_", safe)
        return Path(f"{safe}.form.gax.md")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone a Google Form to a local .form.gax.md file."""
        fmt = kw.get("fmt", kw.get("format", "md"))

        form_id = extract_form_id(self.url)
        source_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        logger.info(f"Fetching: {form_id}")
        form_data = get_form(form_id)

        info = form_data.get("info", {})
        doc_title = info.get("documentTitle", info.get("title", "Untitled"))

        if fmt == "yaml":
            body_str = form_to_yaml(form_data, source_url)
            content_type = "application/yaml"
        else:
            body_str = form_to_markdown(form_data, source_url)
            content_type = "text/markdown"

        header = FormHeader(
            id=form_id,
            title=doc_title,
            source=source_url,
            synced=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            content_type=content_type,
        )

        file_path = self._output_path(doc_title, output)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        content = format_form_file(header, body_str)
        file_path.write_text(content, encoding="utf-8")

        items = form_data.get("items", [])
        questions = sum(
            1 for i in items if "questionItem" in i or "questionGroupItem" in i
        )
        logger.info(f"Title: {doc_title}, Questions: {questions}")
        return file_path

    def pull(self, **kw) -> None:
        """Pull latest form definition from Google Forms."""
        header, _ = parse_form_file(self.path)
        form_id = _resolve_form_id(header)
        source_url = header.source or f"https://docs.google.com/forms/d/{form_id}/edit"

        logger.info(f"Pulling: {form_id}")
        form_data = get_form(form_id)

        info = form_data.get("info", {})
        doc_title = info.get("documentTitle", info.get("title", "Untitled"))

        if header.content_type == "application/yaml":
            body_str = form_to_yaml(form_data, source_url)
        else:
            body_str = form_to_markdown(form_data, source_url)

        new_header = FormHeader(
            id=form_id,
            title=doc_title,
            source=source_url,
            synced=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            content_type=header.content_type,
        )

        content = format_form_file(new_header, body_str)
        self.path.write_text(content, encoding="utf-8")

        items = form_data.get("items", [])
        questions = sum(
            1 for i in items if "questionItem" in i or "questionGroupItem" in i
        )
        logger.info(f"Questions: {questions}")

    def diff(self, **kw) -> str | None:
        """Compare local YAML form to remote. Returns plan summary or None.

        Only works with content-type: application/yaml files.
        """
        header, body_content = parse_form_file(self.path)

        if header.content_type != "application/yaml":
            raise ValueError(
                "Plan/apply only works with YAML format files (use --format yaml)"
            )

        form_id = _resolve_form_id(header)
        local_body = parse_form_body(body_content)

        logger.info(f"Fetching remote: {form_id}")
        remote_form = get_form(form_id)

        changes = _compute_changes(local_body, remote_form)
        return _format_changes_summary(changes)

    def push(self, **kw) -> None:
        """Apply form changes to Google Forms via batchUpdate.

        Only works with content-type: application/yaml files.
        """
        header, body_content = parse_form_file(self.path)

        if header.content_type != "application/yaml":
            raise ValueError(
                "Plan/apply only works with YAML format files (use --format yaml)"
            )

        form_id = _resolve_form_id(header)
        local_body = parse_form_body(body_content)

        logger.info(f"Fetching remote: {form_id}")
        remote_form = get_form(form_id)

        changes = _compute_changes(local_body, remote_form)

        if not any(
            k in changes
            for k in (
                "create",
                "update",
                "delete",
                "move",
                "update_info",
                "update_settings",
            )
        ):
            logger.info("No changes to apply")
            return

        _apply_changes(form_id, changes)

        # Suggest pulling to sync local file with new IDs
        logger.info(f"Run 'gax form pull {self.path}' to sync local file with new IDs")


Resource.register(Form)
