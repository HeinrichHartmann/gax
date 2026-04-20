"""Gmail label management for gax.

Resource module — follows the draft.py reference pattern.

Declarative label management: clone labels to YAML, edit locally,
diff to preview changes, push to apply. See ADR 010.

Module structure
================

  LabelHeader          — dataclass for file frontmatter
  File format          — parse/format label files
  API helpers          — fetch labels, visibility/color mappings
  Comparison helpers   — diff logic for desired vs current state
  Label(Resource)      — resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to labels:

  Deletions are opt-in: diff() and push() accept allow_delete=False
  by default. Removing a label from the file does NOT delete it unless
  explicitly requested. This prevents accidental mass deletion.

  Nested labels (e.g. "Projects/Active") require parent creation.
  push() handles this automatically by sorting creates by depth.

  Rename is supported via 'rename_from' field in the YAML. This is
  detected during diff and executed as a patch on the existing label.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from googleapiclient.discovery import build

from ..gaxfile import parse as gaxfile_parse, format_single
from ..auth import get_authenticated_credentials
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

LABEL_LIST_VISIBILITY = {
    "show": "labelShow",
    "hide": "labelHide",
    "unread": "labelShowIfUnread",
}
LABEL_LIST_VISIBILITY_REV = {v: k for k, v in LABEL_LIST_VISIBILITY.items()}

MESSAGE_LIST_VISIBILITY = {
    "show": "show",
    "hide": "hide",
}

SYSTEM_LABELS = {
    "INBOX",
    "SPAM",
    "TRASH",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "SENT",
    "DRAFT",
    "CHAT",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}


# =============================================================================
# Data class
# =============================================================================


@dataclass
class LabelHeader:
    """Frontmatter of a labels file."""

    pulled: str = ""


# =============================================================================
# File format — parse/format label files.
# =============================================================================


def parse_labels_file(path: Path) -> tuple[LabelHeader, list[dict]]:
    """Parse a labels file into header and label list."""
    content = path.read_text(encoding="utf-8")

    try:
        header_data, body = gaxfile_parse(content)
    except ValueError:
        # Old format: single YAML doc with labels key
        doc = yaml.safe_load(content)
        labels = doc.get("labels", []) if doc else []
        return LabelHeader(), labels

    header = LabelHeader(pulled=header_data.get("pulled", ""))
    labels = yaml.safe_load(body) or []
    return header, labels


def format_labels_file(header: LabelHeader, labels: list[dict]) -> str:
    """Format header and label list as file content."""
    file_header = {
        "type": "gax/labels",
        "content-type": "application/yaml",
        "pulled": header.pulled,
    }
    body = yaml.dump(
        labels, default_flow_style=False, allow_unicode=True, sort_keys=False
    )

    comments = (
        "# Gmail Labels\n"
        "# Visibility: visible (show|hide|unread), show_in_list (show|hide)\n"
        "# Rename: add 'rename_from: OldName'\n"
        "# Delete: remove from list, use --delete flag\n"
    )
    return comments + format_single(file_header, body)


# =============================================================================
# API helpers — fetch labels, visibility/color mappings.
# =============================================================================


def get_service():
    """Get authenticated Gmail API service."""
    creds = get_authenticated_credentials()
    return build("gmail", "v1", credentials=creds)


def fetch_labels(*, service=None) -> list[dict]:
    """Fetch all labels from Gmail."""
    service = service or get_service()
    result = service.users().labels().list(userId="me").execute()
    return result.get("labels", [])


def api_to_label(api_label: dict) -> dict:
    """Normalize API label to local format."""
    entry = {"name": api_label["name"]}

    llv = api_label.get("labelListVisibility")
    if llv and llv != "labelShow":
        entry["visible"] = LABEL_LIST_VISIBILITY_REV.get(llv, llv)

    mlv = api_label.get("messageListVisibility")
    if mlv and mlv != "show":
        entry["show_in_list"] = mlv

    color = api_label.get("color")
    if color:
        entry["color"] = {
            "text": color.get("textColor", "#000000"),
            "bg": color.get("backgroundColor", "#ffffff"),
        }

    if api_label.get("type") == "system":
        entry["system"] = True

    return entry


def label_to_api_body(desired: dict) -> dict:
    """Build API body from local label settings."""
    body = {}

    if "visible" in desired:
        body["labelListVisibility"] = LABEL_LIST_VISIBILITY.get(
            desired["visible"], "labelShow"
        )

    if "show_in_list" in desired:
        body["messageListVisibility"] = desired["show_in_list"]

    if "color" in desired:
        body["color"] = {
            "textColor": desired["color"].get("text", "#000000"),
            "backgroundColor": desired["color"].get("bg", "#ffffff"),
        }

    return body


# =============================================================================
# Comparison helpers — diff logic for desired vs current labels.
# =============================================================================


def needs_update(current: dict, desired: dict) -> bool:
    """Check if a label needs updating (visibility or color changed)."""
    desired_llv = desired.get("visible", "show")
    current_llv = LABEL_LIST_VISIBILITY_REV.get(
        current.get("labelListVisibility", "labelShow"), "show"
    )
    if desired_llv != current_llv:
        return True

    desired_mlv = desired.get("show_in_list", "show")
    current_mlv = current.get("messageListVisibility", "show")
    if desired_mlv != current_mlv:
        return True

    desired_color = desired.get("color")
    current_color = current.get("color")
    if desired_color and not current_color:
        return True
    if desired_color and current_color:
        if desired_color.get("text") != current_color.get("textColor"):
            return True
        if desired_color.get("bg") != current_color.get("backgroundColor"):
            return True

    return False


def compute_changes(
    desired_labels: list[dict],
    current_labels: dict[str, dict],
    allow_delete: bool = False,
) -> dict:
    """Compute changes between desired (local) and current (remote) labels.

    Returns dict with keys: create, rename, update, delete (each a list).
    """
    desired_map = {}
    rename_map = {}
    for lbl in desired_labels:
        name = lbl["name"]
        desired_map[name] = lbl
        if "rename_from" in lbl:
            rename_map[lbl["rename_from"]] = name

    creates = []
    renames = []
    updates = []
    deletes = []

    for name, desired in desired_map.items():
        if desired.get("system"):
            continue

        if "rename_from" in desired:
            old_name = desired["rename_from"]
            if old_name in current_labels:
                renames.append(
                    {
                        "from": old_name,
                        "to": name,
                        "id": current_labels[old_name]["id"],
                        "settings": desired,
                    }
                )
            elif name not in current_labels:
                creates.append({"name": name, "settings": desired})
        elif name not in current_labels:
            creates.append({"name": name, "settings": desired})
        else:
            current = current_labels[name]
            if needs_update(current, desired):
                updates.append(
                    {
                        "name": name,
                        "id": current["id"],
                        "settings": desired,
                    }
                )

    if allow_delete:
        for name, current in current_labels.items():
            if current.get("type") == "system":
                continue
            if name not in desired_map and name not in rename_map:
                deletes.append({"name": name, "id": current["id"]})

    return {
        "create": creates,
        "rename": renames,
        "update": updates,
        "delete": deletes,
    }


def format_diff_summary(changes: dict, skipped_deletes: int = 0) -> str:
    """Format a human-readable diff summary."""
    creates = changes["create"]
    renames = changes["rename"]
    updates = changes["update"]
    deletes = changes["delete"]

    if not creates and not renames and not updates and not deletes:
        return ""

    lines = []

    if creates:
        lines.append(f"  Create: {len(creates)}")
        for item in creates:
            lines.append(f"    + {item['name']}")

    if renames:
        lines.append(f"  Rename: {len(renames)}")
        for item in renames:
            lines.append(f"    {item['from']} -> {item['to']}")

    if updates:
        lines.append(f"  Update: {len(updates)}")
        for item in updates:
            lines.append(f"    ~ {item['name']}")

    if deletes:
        lines.append(f"  Delete: {len(deletes)}")
        for item in deletes:
            lines.append(f"    - {item['name']}")

    if skipped_deletes > 0:
        lines.append(f"  (Skipped {skipped_deletes} deletions, use --delete)")

    return "\n".join(lines)


# =============================================================================
# Resource class — the public interface for cli.py.
# =============================================================================


class Label(Resource):
    """Gmail label resource.

    Constructed via from_file(path) or directly with Label(path=...).
    Account-level resource (no URL dispatch).
    """

    name = "label"
    FILE_TYPE = "gax/labels"

    def clone(
        self,
        output: Path | None = None,
        *,
        include_all: bool = False,
        **kw,
    ) -> Path:
        """Clone Gmail labels to a local file."""
        labels = self._fetch_normalized(include_all=include_all)

        header = LabelHeader(
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_labels_file(header, labels)

        file_path = output or Path("labels.mail.gax.md")
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Labels: {len(labels)}")
        return file_path

    def pull(self, *, include_all: bool = False, **kw) -> None:
        """Pull latest labels from Gmail."""
        labels = self._fetch_normalized(include_all=include_all)

        header = LabelHeader(
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_labels_file(header, labels)
        self.path.write_text(content, encoding="utf-8")
        logger.info(f"Labels: {len(labels)}")

    def list(self, out, **kw) -> None:
        """List Gmail labels as TSV to file descriptor."""
        api_labels = fetch_labels()

        system = [lbl for lbl in api_labels if lbl.get("type") == "system"]
        user = [lbl for lbl in api_labels if lbl.get("type") == "user"]
        system.sort(key=lambda x: x.get("name", ""))
        user.sort(key=lambda x: x.get("name", ""))

        out.write("id\tname\ttype\n")
        for lbl in system + user:
            out.write(
                f"{lbl.get('id', '')}\t{lbl.get('name', '')}\t{lbl.get('type', '')}\n"
            )

    def diff(self, *, allow_delete: bool = False, **kw) -> str | None:
        """Preview changes between local labels file and Gmail."""
        _, desired_labels = parse_labels_file(self.path)

        api_labels = fetch_labels()
        current_map = {lbl["name"]: lbl for lbl in api_labels}

        changes = compute_changes(
            desired_labels, current_map, allow_delete=allow_delete
        )

        # Count skipped deletions
        skipped = 0
        if not allow_delete:
            desired_names = {lbl["name"] for lbl in desired_labels}
            rename_sources = {
                lbl.get("rename_from") for lbl in desired_labels if "rename_from" in lbl
            }
            for name, current in current_map.items():
                if current.get("type") == "system":
                    continue
                if name not in desired_names and name not in rename_sources:
                    skipped += 1

        summary = format_diff_summary(changes, skipped_deletes=skipped)
        return summary or None

    def push(self, *, allow_delete: bool = False, **kw) -> None:
        """Push local label changes to Gmail. Unconditional."""
        _, desired_labels = parse_labels_file(self.path)

        service = get_service()
        api_labels = fetch_labels(service=service)
        current_map = {lbl["name"]: lbl for lbl in api_labels}

        changes = compute_changes(
            desired_labels, current_map, allow_delete=allow_delete
        )

        creates = changes["create"]
        renames = changes["rename"]
        updates = changes["update"]
        deletes = changes["delete"]

        if not creates and not renames and not updates and not deletes:
            logger.info("No changes to apply")
            return

        # 1. Create (parents first for nesting)
        created = set()
        for item in sorted(creates, key=lambda x: x["name"].count("/")):
            self._create_with_parents(
                service, item["name"], item["settings"], current_map, created
            )

        # 2. Rename
        for item in renames:
            logger.info(f"Renaming: {item['from']} -> {item['to']}")
            body = {"name": item["to"]}
            body.update(label_to_api_body(item["settings"]))
            service.users().labels().patch(
                userId="me", id=item["id"], body=body
            ).execute()

        # 3. Update
        for item in updates:
            logger.info(f"Updating: {item['name']}")
            body = label_to_api_body(item["settings"])
            service.users().labels().patch(
                userId="me", id=item["id"], body=body
            ).execute()

        # 4. Delete
        for item in deletes:
            logger.info(f"Deleting: {item['name']}")
            service.users().labels().delete(userId="me", id=item["id"]).execute()

        logger.info(
            f"Applied: {len(creates)} created, {len(renames)} renamed, "
            f"{len(updates)} updated, {len(deletes)} deleted"
        )

    def _fetch_normalized(self, *, include_all: bool = False) -> list[dict]:
        """Fetch labels and normalize to local format."""
        api_labels = fetch_labels()
        labels = []
        for lbl in sorted(api_labels, key=lambda x: x["name"]):
            if lbl.get("type") == "system" and not include_all:
                continue
            labels.append(api_to_label(lbl))
        return labels

    def _create_with_parents(
        self, service, name: str, settings: dict, current_labels: dict, created: set
    ):
        """Create label, ensuring parent labels exist first."""
        if name in created or name in current_labels:
            return

        if "/" in name:
            parts = name.split("/")
            for i in range(len(parts) - 1):
                parent = "/".join(parts[: i + 1])
                if parent not in current_labels and parent not in created:
                    body = {"name": parent, "labelListVisibility": "labelShow"}
                    result = (
                        service.users()
                        .labels()
                        .create(userId="me", body=body)
                        .execute()
                    )
                    current_labels[parent] = result
                    created.add(parent)
                    logger.info(f"Created parent: {parent}")

        body = {"name": name}
        body.update(label_to_api_body(settings))
        result = service.users().labels().create(userId="me", body=body).execute()
        current_labels[name] = result
        created.add(name)
        logger.info(f"Created: {name}")


Resource.register(Label)
