"""Gmail filter management for gax.

Resource module -- follows the draft.py reference pattern.

Declarative filter management: clone filters to YAML, edit locally,
diff to preview changes, push to apply. See ADR 011.

Module structure
================

  FilterHeader         -- dataclass for file frontmatter
  File format          -- parse/format filter files
  API helpers          -- criteria/action conversion (inverse pairs)
  Comparison helpers   -- diff logic for desired vs current state
  Filter(Resource)     -- resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to filters:

  Gmail applies ALL matching filters simultaneously, not sequentially.
  Filter order has no significance -- there is no "stop processing" feature.
  Conflicting actions from multiple filters may neutralize each other.

  Filters are matched by criteria hash (MD5 of normalized JSON).
  Updates are implemented as delete+recreate since the Gmail API has
  no patch endpoint for filters.

  Label auto-creation: when a filter references a label that doesn't
  exist, push() creates it automatically (including parent labels
  for nested paths like "Projects/Active").
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data class
# =============================================================================


@dataclass
class FilterHeader:
    """Frontmatter of a filters file."""

    pulled: str = ""


# =============================================================================
# File format -- parse/format filter files.
# =============================================================================


def parse_filters_file(path: Path) -> tuple[FilterHeader, list[dict]]:
    """Parse a filters file into header and filter list."""
    content = path.read_text(encoding="utf-8")

    # Skip comment lines at start
    lines = content.split("\n")
    while lines and lines[0].startswith("#"):
        lines = lines[1:]
    content = "\n".join(lines)

    header = FilterHeader()

    if content.startswith("---\n"):
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            header_data = yaml.safe_load(parts[1]) or {}
            header.pulled = header_data.get("pulled", "")
            filters = yaml.safe_load(parts[2]) or []
            return header, filters

    # Old format: single YAML doc with filters key
    doc = yaml.safe_load(content)
    filters = doc.get("filters", []) if doc else []
    return header, filters


def format_filters_file(header: FilterHeader, filters: list[dict]) -> str:
    """Format header and filter list as file content."""
    file_header = {
        "type": "gax/filters",
        "content-type": "application/yaml",
        "pulled": header.pulled,
    }

    parts = [
        "---\n",
        yaml.dump(
            file_header, default_flow_style=False, allow_unicode=True, sort_keys=False
        ),
        "---\n",
        yaml.dump(
            filters, default_flow_style=False, allow_unicode=True, sort_keys=False
        ),
    ]
    return "".join(parts)


# =============================================================================
# API helpers -- criteria/action conversion (inverse pairs).
# =============================================================================

CRITERIA_KEYS = [
    "from",
    "to",
    "subject",
    "query",
    "negatedQuery",
    "hasAttachment",
    "excludeChats",
    "size",
    "sizeComparison",
]


def get_service():
    """Get authenticated Gmail API service."""
    creds = get_authenticated_credentials()
    return build("gmail", "v1", credentials=creds)


def fetch_filters(*, service=None) -> list[dict]:
    """Fetch all filters from Gmail."""
    service = service or get_service()
    result = service.users().settings().filters().list(userId="me").execute()
    return result.get("filter", [])


def fetch_label_maps(*, service=None) -> tuple[dict, dict]:
    """Fetch label ID<->name mappings. Returns (id_to_name, name_to_id)."""
    service = service or get_service()
    labels_result = service.users().labels().list(userId="me").execute()
    labels = labels_result.get("labels", [])
    id_to_name = {lbl["id"]: lbl["name"] for lbl in labels}
    name_to_id = {lbl["name"]: lbl["id"] for lbl in labels}
    return id_to_name, name_to_id


# --- Criteria: inverse pair ---


def api_to_criteria(api_criteria: dict) -> dict:
    """Convert Gmail API criteria to local format."""
    return {k: api_criteria[k] for k in CRITERIA_KEYS if k in api_criteria}


def criteria_to_api(local_criteria: dict) -> dict:
    """Convert local criteria to Gmail API format."""
    return {k: local_criteria[k] for k in CRITERIA_KEYS if k in local_criteria}


# --- Action: inverse pair ---


def api_to_action(api_action: dict, label_id_to_name: dict) -> dict:
    """Convert Gmail API action to local format."""
    result = {}

    if api_action.get("addLabelIds"):
        labels = []
        for lid in api_action["addLabelIds"]:
            name = label_id_to_name.get(lid, lid)
            if name not in ("INBOX", "TRASH", "SPAM", "STARRED", "IMPORTANT", "UNREAD"):
                labels.append(name)
            elif name == "STARRED":
                result["star"] = True
            elif name == "IMPORTANT":
                result["important"] = True
            elif name == "TRASH":
                result["trash"] = True
        if labels:
            result["label"] = labels[0] if len(labels) == 1 else labels

    if api_action.get("removeLabelIds"):
        for lid in api_action["removeLabelIds"]:
            name = label_id_to_name.get(lid, lid)
            if name == "INBOX":
                result["archive"] = True
            elif name == "UNREAD":
                result["markRead"] = True
            elif name == "IMPORTANT":
                result["neverImportant"] = True
            elif name == "SPAM":
                result["neverSpam"] = True
            else:
                result["removeLabel"] = name

    if api_action.get("forward"):
        result["forward"] = api_action["forward"]

    return result


def action_to_api(local_action: dict, label_name_to_id: dict, service=None) -> dict:
    """Convert local action to Gmail API format.

    If a referenced label doesn't exist and service is provided,
    creates it automatically.
    """
    result = {"addLabelIds": [], "removeLabelIds": []}

    if local_action.get("label"):
        labels = local_action["label"]
        if isinstance(labels, str):
            labels = [labels]
        for label_name in labels:
            label_id = get_or_create_label(service, label_name, label_name_to_id)
            result["addLabelIds"].append(label_id)

    if local_action.get("removeLabel"):
        label_name = local_action["removeLabel"]
        if label_name in label_name_to_id:
            result["removeLabelIds"].append(label_name_to_id[label_name])

    if local_action.get("archive"):
        result["removeLabelIds"].append("INBOX")
    if local_action.get("markRead"):
        result["removeLabelIds"].append("UNREAD")
    if local_action.get("star"):
        result["addLabelIds"].append("STARRED")
    if local_action.get("important"):
        result["addLabelIds"].append("IMPORTANT")
    if local_action.get("neverImportant"):
        result["removeLabelIds"].append("IMPORTANT")
    if local_action.get("trash"):
        result["addLabelIds"].append("TRASH")
    if local_action.get("neverSpam"):
        result["removeLabelIds"].append("SPAM")

    if local_action.get("forward"):
        result["forward"] = local_action["forward"]

    if local_action.get("category"):
        cat = local_action["category"].upper()
        if not cat.startswith("CATEGORY_"):
            cat = f"CATEGORY_{cat}"
        result["addLabelIds"].append(cat)

    if not result["addLabelIds"]:
        del result["addLabelIds"]
    if not result["removeLabelIds"]:
        del result["removeLabelIds"]

    return result


def get_or_create_label(service, label_name: str, label_name_to_id: dict) -> str:
    """Get label ID, creating the label if it doesn't exist."""
    if label_name in label_name_to_id:
        return label_name_to_id[label_name]

    if service is None:
        raise ValueError(f"Label '{label_name}' does not exist")

    # Create parent labels first for nested labels
    if "/" in label_name:
        parts = label_name.split("/")
        for i in range(len(parts) - 1):
            parent = "/".join(parts[: i + 1])
            if parent not in label_name_to_id:
                result = (
                    service.users()
                    .labels()
                    .create(
                        userId="me",
                        body={"name": parent, "labelListVisibility": "labelShow"},
                    )
                    .execute()
                )
                label_name_to_id[parent] = result["id"]
                logger.info(f"Created label: {parent}")

    result = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow"},
        )
        .execute()
    )
    label_name_to_id[label_name] = result["id"]
    logger.info(f"Created label: {label_name}")
    return result["id"]


# =============================================================================
# Comparison helpers -- diff logic for desired vs current filters.
# =============================================================================


def criteria_hash(criteria: dict) -> str:
    """Generate hash from filter criteria for matching."""
    normalized = json.dumps(criteria, sort_keys=True)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def generate_filter_name(criteria: dict) -> str:
    """Generate human-readable name from criteria."""
    parts = []
    if criteria.get("from"):
        parts.append(f"from:{criteria['from']}")
    if criteria.get("to"):
        parts.append(f"to:{criteria['to']}")
    if criteria.get("subject"):
        parts.append(f"subject:{criteria['subject']}")
    if criteria.get("query"):
        parts.append(criteria["query"][:30])
    if criteria.get("hasAttachment"):
        parts.append("has:attachment")
    return " ".join(parts) if parts else "filter"


def compute_changes(
    desired_filters: list[dict],
    current_filters: list[dict],
    label_id_to_name: dict,
) -> dict:
    """Compute changes between desired (local) and current (remote) filters.

    Returns dict with keys: create, update, delete (each a list).
    """
    desired_by_hash = {}
    for f in desired_filters:
        h = criteria_hash(f.get("criteria", {}))
        desired_by_hash[h] = f

    current_by_hash = {}
    for f in current_filters:
        criteria = api_to_criteria(f.get("criteria", {}))
        h = criteria_hash(criteria)
        current_by_hash[h] = {
            "id": f["id"],
            "criteria": criteria,
            "api_filter": f,
        }

    creates = []
    updates = []
    deletes = []

    for h, desired in desired_by_hash.items():
        if h not in current_by_hash:
            creates.append(
                {
                    "name": desired.get("name", ""),
                    "criteria": desired.get("criteria", {}),
                    "action": desired.get("action", {}),
                }
            )
        else:
            current = current_by_hash[h]
            current_action = api_to_action(
                current["api_filter"].get("action", {}), label_id_to_name
            )
            desired_action = desired.get("action", {})
            if current_action != desired_action:
                updates.append(
                    {
                        "id": current["id"],
                        "name": desired.get("name", ""),
                        "criteria": desired.get("criteria", {}),
                        "action": desired_action,
                    }
                )

    for h, current in current_by_hash.items():
        if h not in desired_by_hash:
            deletes.append(
                {
                    "id": current["id"],
                    "criteria": current["criteria"],
                }
            )

    return {"create": creates, "update": updates, "delete": deletes}


def format_diff_summary(changes: dict) -> str:
    """Format a human-readable diff summary."""
    creates = changes["create"]
    updates = changes["update"]
    deletes = changes["delete"]

    if not creates and not updates and not deletes:
        return ""

    lines = []

    if creates:
        lines.append(f"  Create: {len(creates)}")
        for item in creates:
            lines.append(f"    + {item.get('name', 'filter')}")

    if updates:
        lines.append(f"  Update: {len(updates)} (delete+recreate)")
        for item in updates:
            lines.append(f"    ~ {item.get('name', 'filter')}")

    if deletes:
        lines.append(f"  Delete: {len(deletes)}")
        for item in deletes:
            name = generate_filter_name(item.get("criteria", {}))
            lines.append(f"    - {name}")

    return "\n".join(lines)


# =============================================================================
# Resource class -- the public interface for cli.py.
# =============================================================================


class Filter(Resource):
    """Gmail filter resource.

    Constructed via from_file(path) or directly with Filter(path=...).
    Account-level resource (no URL dispatch).
    """

    name = "filter"
    FILE_TYPE = "gax/filters"

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone Gmail filters to a local file."""
        service = get_service()
        filters = self._fetch_normalized(service=service)

        header = FilterHeader(
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_filters_file(header, filters)

        file_path = output or Path("filters.mail.gax.md")
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Filters: {len(filters)}")
        return file_path

    def pull(self, **kw) -> None:
        """Pull latest filters from Gmail."""
        service = get_service()
        filters = self._fetch_normalized(service=service)

        header = FilterHeader(
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_filters_file(header, filters)
        self.path.write_text(content, encoding="utf-8")
        logger.info(f"Filters: {len(filters)}")

    def list(self, out, **kw) -> None:
        """List Gmail filters as TSV to file descriptor."""
        service = get_service()
        id_to_name, _ = fetch_label_maps(service=service)
        api_filters = fetch_filters(service=service)

        out.write("id\tfrom\tto\tsubject\tquery\tlabels\tactions\n")

        for f in api_filters:
            fid = f.get("id", "")
            criteria = f.get("criteria", {})
            action = f.get("action", {})

            from_addr = criteria.get("from", "")
            to_addr = criteria.get("to", "")
            subject = criteria.get("subject", "")
            query = criteria.get("query", "")

            labels = []
            for lid in action.get("addLabelIds", []):
                name = id_to_name.get(lid, lid)
                if name not in ("STARRED", "IMPORTANT", "TRASH"):
                    labels.append(name)
            labels_str = ",".join(labels)

            actions = []
            if "INBOX" in action.get("removeLabelIds", []):
                actions.append("archive")
            if "UNREAD" in action.get("removeLabelIds", []):
                actions.append("read")
            if "STARRED" in action.get("addLabelIds", []):
                actions.append("star")
            if "TRASH" in action.get("addLabelIds", []):
                actions.append("trash")
            if action.get("forward"):
                actions.append(f"fwd:{action['forward']}")
            actions_str = ",".join(actions)

            out.write(
                f"{fid}\t{from_addr}\t{to_addr}\t{subject}\t{query}"
                f"\t{labels_str}\t{actions_str}\n"
            )

    def diff(self, **kw) -> str | None:
        """Preview changes between local filters file and Gmail."""
        _, desired_filters = parse_filters_file(self.path)

        service = get_service()
        current_filters = fetch_filters(service=service)
        id_to_name, _ = fetch_label_maps(service=service)

        changes = compute_changes(desired_filters, current_filters, id_to_name)
        summary = format_diff_summary(changes)
        return summary or None

    def push(self, **kw) -> None:
        """Push local filter changes to Gmail. Unconditional."""
        _, desired_filters = parse_filters_file(self.path)

        service = get_service()
        current_filters = fetch_filters(service=service)
        id_to_name, name_to_id = fetch_label_maps(service=service)

        changes = compute_changes(desired_filters, current_filters, id_to_name)

        creates = changes["create"]
        updates = changes["update"]
        deletes = changes["delete"]

        if not creates and not updates and not deletes:
            logger.info("No changes to apply")
            return

        # 1. Delete (including updates -- delete first, recreate later)
        for item in deletes + updates:
            name = item.get("name") or generate_filter_name(item.get("criteria", {}))
            logger.info(f"Deleting: {name}")
            service.users().settings().filters().delete(
                userId="me", id=item["id"]
            ).execute()

        # 2. Create (including recreate for updates)
        for item in creates + updates:
            name = item.get("name") or generate_filter_name(item.get("criteria", {}))
            action_type = "Creating" if item in creates else "Recreating"
            logger.info(f"{action_type}: {name}")
            body = {
                "criteria": criteria_to_api(item.get("criteria", {})),
                "action": action_to_api(item.get("action", {}), name_to_id, service),
            }
            service.users().settings().filters().create(
                userId="me", body=body
            ).execute()

        logger.info(
            f"Applied: {len(creates)} created, {len(updates)} updated, "
            f"{len(deletes)} deleted"
        )

    def _fetch_normalized(self, *, service=None) -> list[dict]:
        """Fetch filters and normalize to local format."""
        service = service or get_service()
        id_to_name, _ = fetch_label_maps(service=service)
        api_filters = fetch_filters(service=service)

        filters = []
        for f in api_filters:
            criteria = api_to_criteria(f.get("criteria", {}))
            action = api_to_action(f.get("action", {}), id_to_name)
            entry = {
                "name": generate_filter_name(criteria),
                "criteria": criteria,
                "action": action,
            }
            filters.append(entry)
        return filters
