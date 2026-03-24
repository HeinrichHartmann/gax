"""Google Forms sync for gax.

Implements clone/pull commands for Google Forms definitions (ADR 014).
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials


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
    """Fetch form definition from Google Forms API.

    Returns the full form resource including items (questions).
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("forms", "v1", credentials=creds)

    return service.forms().get(formId=form_id).execute()


def form_to_yaml(form: dict, source_url: str) -> str:
    """Convert Forms API response to YAML .form.gax format.

    Returns YAML frontmatter + YAML body for faithful round-trip.
    """
    form_id = form.get("formId", "")
    info = form.get("info", {})
    title = info.get("title", "Untitled Form")
    document_title = info.get("documentTitle", title)
    description = info.get("description", "")

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build header
    header = {
        "type": "gax/form",
        "id": form_id,
        "title": document_title,
        "source": source_url,
        "synced": time_str,
        "content-type": "application/yaml",
    }

    # Build body - the full form structure for faithful round-trip
    body = {
        "documentTitle": document_title,
        "info": {
            "title": title,
        },
    }

    if description:
        body["info"]["description"] = description

    # Settings
    settings = form.get("settings", {})
    if settings:
        body["settings"] = settings

    # Items (questions, page breaks, etc.)
    items = form.get("items", [])
    if items:
        body["items"] = items

    # Linked sheet ID if present
    linked_sheet = form.get("linkedSheetId")
    if linked_sheet:
        body["linkedSheetId"] = linked_sheet

    # Response URL
    responder_uri = form.get("responderUri")
    if responder_uri:
        body["responderUri"] = responder_uri

    # Format output
    header_yaml = yaml.dump(header, default_flow_style=False, allow_unicode=True, sort_keys=False)
    body_yaml = yaml.dump(body, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return f"---\n{header_yaml}---\n{body_yaml}"


def form_to_markdown(form: dict, source_url: str) -> str:
    """Convert Forms API response to readable markdown format.

    This is view-only (not round-trip safe for push).
    """
    form_id = form.get("formId", "")
    info = form.get("info", {})
    title = info.get("title", "Untitled Form")
    document_title = info.get("documentTitle", title)
    description = info.get("description", "")

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build header
    header = {
        "type": "gax/form",
        "id": form_id,
        "title": document_title,
        "source": source_url,
        "synced": time_str,
        "content-type": "text/markdown",
    }

    header_yaml = yaml.dump(header, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Build markdown body
    lines = []
    lines.append(f"# {title}")
    lines.append("")

    if description:
        lines.append(description)
        lines.append("")

    # Settings summary
    settings = form.get("settings", {})
    quiz_settings = settings.get("quizSettings", {})
    if quiz_settings.get("isQuiz"):
        lines.append("_This is a quiz._")
        lines.append("")

    # Items
    items = form.get("items", [])
    question_num = 0

    for item in items:
        item_title = item.get("title", "")
        item_desc = item.get("description", "")

        # Page break
        if "pageBreakItem" in item:
            lines.append("---")
            lines.append("")
            if item_title:
                lines.append(f"### {item_title}")
                lines.append("")
            continue

        # Section header
        if "textItem" in item:
            if item_title:
                lines.append(f"### {item_title}")
                if item_desc:
                    lines.append("")
                    lines.append(item_desc)
                lines.append("")
            continue

        # Image item
        if "imageItem" in item:
            image = item.get("imageItem", {}).get("image", {})
            source_uri = image.get("sourceUri", "")
            alt_text = image.get("altText", "Image")
            if source_uri:
                lines.append(f"![{alt_text}]({source_uri})")
            else:
                lines.append(f"_[Image: {alt_text}]_")
            lines.append("")
            continue

        # Video item
        if "videoItem" in item:
            video = item.get("videoItem", {}).get("video", {})
            youtube_uri = video.get("youtubeUri", "")
            if youtube_uri:
                lines.append(f"[Video: {youtube_uri}]")
            else:
                lines.append("_[Video]_")
            lines.append("")
            continue

        # Question item
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

            # Format based on question type
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
                    else:  # RADIO
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
                # Grid/matrix question - part of questionGroupItem
                pass

            continue

        # Question group (grid/matrix)
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

            # Column headers
            col_values = [c.get("value", "") for c in columns]
            lines.append("| | " + " | ".join(col_values) + " |")
            lines.append("|---" + "|---" * len(col_values) + "|")

            # Rows
            questions = group.get("questions", [])
            for q in questions:
                row_q = q.get("rowQuestion", {})
                row_title = row_q.get("title", "")
                cells = " | ".join(["( )" for _ in col_values])
                lines.append(f"| {row_title} | {cells} |")

            lines.append("")
            continue

    body = "\n".join(lines)
    return f"---\n{header_yaml}---\n{body}"


def parse_form_file(file_path: Path) -> dict:
    """Parse a .form.gax file and return header dict.

    Returns dict with: id, title, source, content-type, etc.
    """
    content = file_path.read_text(encoding="utf-8")

    if not content.startswith("---"):
        raise ValueError("Invalid .form.gax file: missing YAML header")

    # Split header from body
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid .form.gax file: malformed header")

    header_yaml = parts[1].strip()
    header = yaml.safe_load(header_yaml)

    return header


def parse_form_file_full(file_path: Path) -> tuple[dict, dict]:
    """Parse a .form.gax file and return (header, body) dicts.

    The body contains the form structure (items, settings, etc.).
    """
    content = file_path.read_text(encoding="utf-8")

    if not content.startswith("---"):
        raise ValueError("Invalid .form.gax file: missing YAML header")

    # Split header from body
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid .form.gax file: malformed header")

    header_yaml = parts[1].strip()
    body_content = parts[2].strip()

    header = yaml.safe_load(header_yaml)

    # Body is YAML for yaml format, otherwise not parseable
    if header.get("content-type") != "application/yaml":
        raise ValueError("Plan/apply only works with YAML format files (use --format yaml)")

    body = yaml.safe_load(body_content)
    return header, body


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
    """Check if two form items are equivalent (ignoring IDs).

    Does a deep comparison of the entire item structure after stripping
    itemId and questionId fields.
    """
    stripped1 = _strip_ids(item1)
    stripped2 = _strip_ids(item2)
    return stripped1 == stripped2


def _generate_create_request(item: dict, index: int) -> dict:
    """Generate a createItem request for the Forms API."""
    # Build the item without local-only fields
    api_item = {}

    if "title" in item:
        api_item["title"] = item["title"]
    if "description" in item:
        api_item["description"] = item["description"]

    # Handle question items
    if "questionItem" in item:
        question = item["questionItem"].get("question", {})
        # Remove questionId - API will assign one
        api_question = {k: v for k, v in question.items() if k != "questionId"}
        api_item["questionItem"] = {"question": api_question}

    # Handle other item types - these are marker objects, title goes on item level
    if "pageBreakItem" in item:
        # pageBreakItem can have a title inside, but API wants it at item level
        pb = item["pageBreakItem"]
        if isinstance(pb, dict) and "title" in pb:
            api_item["title"] = pb["title"]
        api_item["pageBreakItem"] = {}  # Empty object, title is at item level
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
    api_item = {"itemId": item["itemId"]}

    if "title" in item:
        api_item["title"] = item["title"]
    if "description" in item:
        api_item["description"] = item["description"]

    # Handle question items
    if "questionItem" in item:
        api_item["questionItem"] = item["questionItem"]

    # Build update mask
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


# =============================================================================
# CLI commands
# =============================================================================


@click.group()
def form():
    """Google Forms operations"""
    pass


@form.command("clone")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.form.gax)",
)
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Content format: md (readable, default) or yaml (round-trip safe)",
)
def clone(url: str, output: Optional[Path], fmt: str):
    """Clone a Google Form to a local .form.gax file.

    By default, creates a human-readable markdown representation.
    Use --format yaml for faithful round-trip representation (required for push).
    """
    try:
        form_id = extract_form_id(url)
        source_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        click.echo(f"Fetching: {form_id}")
        form_data = get_form(form_id)

        # Get title for filename
        info = form_data.get("info", {})
        doc_title = info.get("documentTitle", info.get("title", "Untitled"))

        # Format content
        if fmt == "yaml":
            content = form_to_yaml(form_data, source_url)
        else:
            content = form_to_markdown(form_data, source_url)

        # Determine output path
        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", doc_title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.form.gax")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")

        items = form_data.get("items", [])
        questions = sum(1 for i in items if "questionItem" in i or "questionGroupItem" in i)

        click.echo(f"Created: {file_path}")
        click.echo(f"Title: {doc_title}")
        click.echo(f"Questions: {questions}")
        if fmt == "md":
            click.echo("Note: Use --format yaml for round-trip safe format")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@form.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def pull(file: Path):
    """Pull latest form definition from Google Forms."""
    try:
        header = parse_form_file(file)

        form_id = header.get("id")
        if not form_id:
            # Try to extract from source URL
            source = header.get("source", "")
            if source:
                form_id = extract_form_id(source)
            else:
                click.echo("Error: No form ID found in file", err=True)
                sys.exit(1)

        source_url = header.get("source", f"https://docs.google.com/forms/d/{form_id}/edit")
        content_type = header.get("content-type", "text/markdown")

        click.echo(f"Pulling: {form_id}")
        form_data = get_form(form_id)

        # Format based on original content-type
        if content_type == "application/yaml":
            content = form_to_yaml(form_data, source_url)
        else:
            content = form_to_markdown(form_data, source_url)

        file.write_text(content, encoding="utf-8")

        items = form_data.get("items", [])
        questions = sum(1 for i in items if "questionItem" in i or "questionGroupItem" in i)

        click.echo(f"Updated: {file}")
        click.echo(f"Questions: {questions}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@form.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", default="form.plan.yaml", help="Output plan file")
def plan(file: Path, output: str):
    """Generate a plan from edited form file.

    Compares local YAML file against the remote form and generates
    a plan showing what would be created, updated, or deleted.
    """
    try:
        header, local_body = parse_form_file_full(file)

        form_id = header.get("id")
        if not form_id:
            source = header.get("source", "")
            if source:
                form_id = extract_form_id(source)
            else:
                click.echo("Error: No form ID found in file", err=True)
                sys.exit(1)

        # Fetch current form from API
        click.echo(f"Fetching remote: {form_id}")
        remote_form = get_form(form_id)

        local_items = local_body.get("items", [])
        remote_items = remote_form.get("items", [])

        # Build maps by itemId
        remote_by_id = {}
        for idx, item in enumerate(remote_items):
            item_id = item.get("itemId")
            if item_id:
                remote_by_id[item_id] = {"item": item, "index": idx}

        # Compute changes
        plan_data = {
            "type": "gax/form-plan",
            "form_id": form_id,
            "source": str(file),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "create": [],
            "update": [],
            "delete": [],
            "move": [],
        }

        # Check for info changes (title, description)
        local_info = local_body.get("info", {})
        remote_info = remote_form.get("info", {})
        if local_info != remote_info:
            plan_data["update_info"] = local_info

        # Check for settings changes
        local_settings = local_body.get("settings", {})
        remote_settings = remote_form.get("settings", {})
        if local_settings != remote_settings:
            plan_data["update_settings"] = local_settings

        # Track which remote items we've seen
        seen_remote_ids = set()

        # Process local items in order
        for local_idx, local_item in enumerate(local_items):
            item_id = local_item.get("itemId")

            if not item_id:
                # New item - needs to be created
                plan_data["create"].append({
                    "index": local_idx,
                    "title": local_item.get("title", "(untitled)"),
                    "item": local_item,
                })
            else:
                seen_remote_ids.add(item_id)
                if item_id in remote_by_id:
                    remote_item = remote_by_id[item_id]["item"]
                    # Check if item type changed (e.g., pageBreakItem -> textItem)
                    item_types = ["questionItem", "pageBreakItem", "textItem", "imageItem", "videoItem"]
                    local_type = next((t for t in item_types if t in local_item), None)
                    remote_type = next((t for t in item_types if t in remote_item), None)

                    if local_type != remote_type:
                        # Type changed - need delete + create (can't update item type)
                        plan_data["delete"].append({
                            "itemId": item_id,
                            "index": remote_by_id[item_id]["index"],
                            "title": remote_item.get("title", "(untitled)"),
                        })
                        # Create without itemId so it gets a new one
                        new_item = {k: v for k, v in local_item.items() if k != "itemId"}
                        plan_data["create"].append({
                            "index": local_idx,
                            "title": local_item.get("title", "(untitled)"),
                            "item": new_item,
                        })
                    elif not _items_equal(local_item, remote_item):
                        # Same type but content changed - update
                        plan_data["update"].append({
                            "index": local_idx,
                            "itemId": item_id,
                            "title": local_item.get("title", "(untitled)"),
                            "item": local_item,
                        })

                    # Check if position changed (needs move)
                    remote_idx = remote_by_id[item_id]["index"]
                    if local_idx != remote_idx:
                        plan_data["move"].append({
                            "itemId": item_id,
                            "from_index": remote_idx,
                            "to_index": local_idx,
                            "title": local_item.get("title", "(untitled)"),
                        })
                else:
                    # Item has ID but not in remote - treat as create
                    click.echo(f"Warning: Item {item_id} not found remotely, will create new", err=True)
                    plan_data["create"].append({
                        "index": local_idx,
                        "title": local_item.get("title", "(untitled)"),
                        "item": local_item,
                    })

        # Check for deletes - remote items not in local
        for item_id, remote_data in remote_by_id.items():
            if item_id not in seen_remote_ids:
                plan_data["delete"].append({
                    "itemId": item_id,
                    "index": remote_data["index"],
                    "title": remote_data["item"].get("title", "(untitled)"),
                })

        # Remove empty lists for cleaner output
        plan_data = {k: v for k, v in plan_data.items()
                     if v or k in ("type", "form_id", "source", "generated")}

        # Show summary
        has_changes = any(k in plan_data for k in ("create", "update", "delete", "move", "update_info", "update_settings"))
        if not has_changes:
            click.echo("No changes to apply.")
            return

        click.echo("\nPlan:")
        if "create" in plan_data:
            click.echo(f"  Create: {len(plan_data['create'])}")
            for item in plan_data["create"]:
                click.echo(f"    + [{item['index']}] {item['title']}")
        if "update" in plan_data:
            click.echo(f"  Update: {len(plan_data['update'])}")
            for item in plan_data["update"]:
                click.echo(f"    ~ [{item['index']}] {item['title']}")
        if "delete" in plan_data:
            click.echo(f"  Delete: {len(plan_data['delete'])}")
            for item in plan_data["delete"]:
                click.echo(f"    - [{item['index']}] {item['title']}")
        if "move" in plan_data:
            click.echo(f"  Move: {len(plan_data['move'])}")
            for item in plan_data["move"]:
                click.echo(f"    > [{item['from_index']}] -> [{item['to_index']}] {item['title']}")
        if "update_info" in plan_data:
            click.echo("  Update info: yes")
        if "update_settings" in plan_data:
            click.echo("  Update settings: yes")

        # Write plan
        with open(output, "w") as f:
            yaml.dump(plan_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        click.echo(f"\nWrote plan to {output}")
        click.echo(f"Review and run: gax form apply {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@form.command("apply")
@click.argument("plan_file", type=click.Path(exists=True, path_type=Path))
def apply(plan_file: Path):
    """Apply form changes from a plan file.

    Executes the changes specified in the plan file using
    the Forms API batchUpdate.
    """
    try:
        # Load plan
        with open(plan_file) as f:
            plan_data = yaml.safe_load(f)

        if plan_data.get("type") != "gax/form-plan":
            click.echo("Error: Not a form plan file", err=True)
            sys.exit(1)

        form_id = plan_data.get("form_id")
        if not form_id:
            click.echo("Error: No form_id in plan", err=True)
            sys.exit(1)

        to_create = plan_data.get("create", [])
        to_update = plan_data.get("update", [])
        to_delete = plan_data.get("delete", [])
        to_move = plan_data.get("move", [])
        update_info = plan_data.get("update_info")
        update_settings = plan_data.get("update_settings")

        if not to_create and not to_update and not to_delete and not to_move and not update_info and not update_settings:
            click.echo("No changes in plan.")
            return

        click.echo("Applying:")
        if to_create:
            click.echo(f"  Create: {len(to_create)}")
        if to_update:
            click.echo(f"  Update: {len(to_update)}")
        if to_delete:
            click.echo(f"  Delete: {len(to_delete)}")
        if to_move:
            click.echo(f"  Move: {len(to_move)}")
        if update_info:
            click.echo("  Update info: yes")
        if update_settings:
            click.echo("  Update settings: yes")

        if not click.confirm("Apply these changes?"):
            click.echo("Aborted.")
            return

        # Build batchUpdate requests
        # Order: info/settings first, then item updates, then deletes (reverse order), then creates
        # This avoids index shifting issues
        requests = []

        # First, fetch current form to get real indices for updates
        creds = get_authenticated_credentials()
        service = build("forms", "v1", credentials=creds)
        current_form = service.forms().get(formId=form_id).execute()
        current_items = current_form.get("items", [])

        # Update form info (title, description)
        if update_info:
            requests.append({
                "updateFormInfo": {
                    "info": update_info,
                    "updateMask": ",".join(update_info.keys())
                }
            })
            click.echo("  ~ Updating form info")

        # Update form settings
        if update_settings:
            requests.append({
                "updateSettings": {
                    "settings": update_settings,
                    "updateMask": ",".join(update_settings.keys())
                }
            })
            click.echo("  ~ Updating form settings")

        # Build itemId -> current index map
        id_to_index = {}
        for idx, item in enumerate(current_items):
            if item.get("itemId"):
                id_to_index[item["itemId"]] = idx

        # Updates - use current remote index, not local index
        for item in to_update:
            item_id = item.get("itemId")
            if item_id and item_id in id_to_index:
                current_idx = id_to_index[item_id]
                requests.append(_generate_update_request(item["item"], current_idx))
                click.echo(f"  ~ Updating: {item['title']}")
            else:
                click.echo(f"  ! Skipping update (item not found): {item['title']}", err=True)

        # Deletes - process in reverse index order to maintain correct indices
        for item in sorted(to_delete, key=lambda x: x["index"], reverse=True):
            requests.append(_generate_delete_request(item["index"]))
            click.echo(f"  - Deleting: {item['title']}")

        # Creates - process in order (lowest index first)
        # The target index is the FINAL position we want in the local YAML.
        # When we insert at position N, items at N and after shift down.
        # Processing in order from low to high means each insert happens at its
        # final position because previous inserts have already shifted items correctly.
        for item in sorted(to_create, key=lambda x: x["index"]):
            target_idx = item["index"]
            requests.append(_generate_create_request(item["item"], target_idx))
            click.echo(f"  + Creating: {item['title']} at index {target_idx}")

        # Execute non-move requests first
        if requests:
            click.echo(f"\nExecuting {len(requests)} request(s)...")
            result = service.forms().batchUpdate(
                formId=form_id,
                body={"requests": requests}
            ).execute()
            replies = result.get("replies", [])
            created_count = sum(1 for r in replies if "createItem" in r)
            click.echo(f"Done: {len(replies)} operations completed")
            if created_count:
                click.echo(f"  Created {created_count} new item(s)")

        # Handle moves separately - need to process one at a time due to index shifting
        if to_move:
            click.echo(f"\nProcessing {len(to_move)} move(s)...")
            # Sort moves by target index (lowest first) so each item ends at correct position
            for move in sorted(to_move, key=lambda x: x["to_index"]):
                # Re-fetch current form state to get accurate indices
                current_form = service.forms().get(formId=form_id).execute()
                current_items = current_form.get("items", [])

                # Find current position of this item
                item_id = move["itemId"]
                current_idx = None
                for idx, item in enumerate(current_items):
                    if item.get("itemId") == item_id:
                        current_idx = idx
                        break

                if current_idx is None:
                    click.echo(f"  ! Item {item_id} not found, skipping move", err=True)
                    continue

                target_idx = move["to_index"]
                if current_idx == target_idx:
                    continue  # Already in position

                # Execute move
                move_request = {
                    "moveItem": {
                        "originalLocation": {"index": current_idx},
                        "newLocation": {"index": target_idx}
                    }
                }
                service.forms().batchUpdate(
                    formId=form_id,
                    body={"requests": [move_request]}
                ).execute()
                click.echo(f"  > Moved: {move['title']} [{current_idx}] -> [{target_idx}]")

            click.echo(f"Done: {len(to_move)} move(s) completed")

        if not requests and not to_move:
            click.echo("No API requests to execute.")
            return

        # Suggest pulling to sync local file
        source_file = plan_data.get("source")
        if source_file:
            click.echo(f"\nRun 'gax form pull {source_file}' to sync local file with new IDs")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
