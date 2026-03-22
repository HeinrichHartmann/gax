"""Gmail filter management commands.

Declarative filter management following ADR 011:
- list: List filters (TSV)
- pull: Export filters to YAML
- plan: Generate plan from edited file
- apply: Execute filter changes
"""

import hashlib
import json
import sys
from datetime import datetime, timezone

import click
import yaml

from .auth import get_authenticated_credentials
from googleapiclient.discovery import build


@click.group("filter")
def filter_group():
    """Gmail filter management (declarative)."""
    pass


def _criteria_hash(criteria: dict) -> str:
    """Generate hash from filter criteria for matching."""
    # Normalize and sort keys for consistent hashing
    normalized = json.dumps(criteria, sort_keys=True)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def _generate_filter_name(criteria: dict) -> str:
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


def _api_to_yaml_criteria(api_criteria: dict) -> dict:
    """Convert Gmail API criteria to our YAML format."""
    result = {}
    mapping = {
        "from": "from",
        "to": "to",
        "subject": "subject",
        "query": "query",
        "negatedQuery": "negatedQuery",
        "hasAttachment": "hasAttachment",
        "excludeChats": "excludeChats",
        "size": "size",
        "sizeComparison": "sizeComparison",
    }
    for api_key, yaml_key in mapping.items():
        if api_key in api_criteria:
            result[yaml_key] = api_criteria[api_key]
    return result


def _yaml_to_api_criteria(yaml_criteria: dict) -> dict:
    """Convert our YAML format to Gmail API criteria."""
    result = {}
    mapping = {
        "from": "from",
        "to": "to",
        "subject": "subject",
        "query": "query",
        "negatedQuery": "negatedQuery",
        "hasAttachment": "hasAttachment",
        "excludeChats": "excludeChats",
        "size": "size",
        "sizeComparison": "sizeComparison",
    }
    for yaml_key, api_key in mapping.items():
        if yaml_key in yaml_criteria:
            result[api_key] = yaml_criteria[yaml_key]
    return result


def _api_to_yaml_action(api_action: dict, label_id_to_name: dict) -> dict:
    """Convert Gmail API action to our YAML format."""
    result = {}

    # Label actions - convert IDs to names
    if api_action.get("addLabelIds"):
        labels = []
        for lid in api_action["addLabelIds"]:
            name = label_id_to_name.get(lid, lid)
            # Skip system labels that are handled by other flags
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


def _yaml_to_api_action(yaml_action: dict, label_name_to_id: dict, service) -> dict:
    """Convert our YAML format to Gmail API action."""
    result = {"addLabelIds": [], "removeLabelIds": []}

    # Handle label - may need to create
    if yaml_action.get("label"):
        labels = yaml_action["label"]
        if isinstance(labels, str):
            labels = [labels]
        for label_name in labels:
            label_id = _get_or_create_label(service, label_name, label_name_to_id)
            result["addLabelIds"].append(label_id)

    if yaml_action.get("removeLabel"):
        label_name = yaml_action["removeLabel"]
        if label_name in label_name_to_id:
            result["removeLabelIds"].append(label_name_to_id[label_name])

    # Boolean flags
    if yaml_action.get("archive"):
        result["removeLabelIds"].append("INBOX")
    if yaml_action.get("markRead"):
        result["removeLabelIds"].append("UNREAD")
    if yaml_action.get("star"):
        result["addLabelIds"].append("STARRED")
    if yaml_action.get("important"):
        result["addLabelIds"].append("IMPORTANT")
    if yaml_action.get("neverImportant"):
        result["removeLabelIds"].append("IMPORTANT")
    if yaml_action.get("trash"):
        result["addLabelIds"].append("TRASH")
    if yaml_action.get("neverSpam"):
        result["removeLabelIds"].append("SPAM")

    # Forward
    if yaml_action.get("forward"):
        result["forward"] = yaml_action["forward"]

    # Category
    if yaml_action.get("category"):
        cat = yaml_action["category"].upper()
        if not cat.startswith("CATEGORY_"):
            cat = f"CATEGORY_{cat}"
        result["addLabelIds"].append(cat)

    # Clean up empty lists
    if not result["addLabelIds"]:
        del result["addLabelIds"]
    if not result["removeLabelIds"]:
        del result["removeLabelIds"]

    return result


def _get_or_create_label(service, label_name: str, label_name_to_id: dict) -> str:
    """Get label ID, creating the label if it doesn't exist."""
    if label_name in label_name_to_id:
        return label_name_to_id[label_name]

    # Create parent labels first for nested labels
    if "/" in label_name:
        parts = label_name.split("/")
        for i in range(len(parts) - 1):
            parent = "/".join(parts[:i + 1])
            if parent not in label_name_to_id:
                result = service.users().labels().create(
                    userId="me",
                    body={"name": parent, "labelListVisibility": "labelShow"}
                ).execute()
                label_name_to_id[parent] = result["id"]
                click.echo(f"Created label: {parent}")

    # Create the label
    result = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow"}
    ).execute()
    label_name_to_id[label_name] = result["id"]
    click.echo(f"Created label: {label_name}")
    return result["id"]


@filter_group.command("list")
def filter_list():
    """List Gmail filters (TSV output)."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Get label mappings
        labels_result = service.users().labels().list(userId="me").execute()
        label_id_to_name = {lbl["id"]: lbl["name"] for lbl in labels_result.get("labels", [])}

        # Get filters
        result = service.users().settings().filters().list(userId="me").execute()
        filters = result.get("filter", [])

        click.echo("id\tfrom\tto\tsubject\tquery\tlabels\tactions")

        for f in filters:
            fid = f.get("id", "")
            criteria = f.get("criteria", {})
            action = f.get("action", {})

            from_addr = criteria.get("from", "")
            to_addr = criteria.get("to", "")
            subject = criteria.get("subject", "")
            query = criteria.get("query", "")

            # Labels being added
            labels = []
            for lid in action.get("addLabelIds", []):
                name = label_id_to_name.get(lid, lid)
                if name not in ("STARRED", "IMPORTANT", "TRASH"):
                    labels.append(name)
            labels_str = ",".join(labels)

            # Action summary
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

            click.echo(f"{fid}\t{from_addr}\t{to_addr}\t{subject}\t{query}\t{labels_str}\t{actions_str}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@filter_group.command("pull")
@click.option("-o", "--output", default="filters.yaml", help="Output file")
def filter_pull(output: str):
    """Export filters to YAML file."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Get label mappings
        labels_result = service.users().labels().list(userId="me").execute()
        label_id_to_name = {lbl["id"]: lbl["name"] for lbl in labels_result.get("labels", [])}

        # Get filters
        result = service.users().settings().filters().list(userId="me").execute()
        api_filters = result.get("filter", [])

        # Convert to YAML format
        filters = []
        for f in api_filters:
            criteria = _api_to_yaml_criteria(f.get("criteria", {}))
            action = _api_to_yaml_action(f.get("action", {}), label_id_to_name)

            entry = {
                "name": _generate_filter_name(criteria),
                "criteria": criteria,
                "action": action,
            }
            filters.append(entry)

        doc = {
            "type": "gax/filters",
            "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "filters": filters,
        }

        with open(output, "w") as f:
            yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        click.echo(f"Wrote {len(filters)} filters to {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@filter_group.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option("-o", "--output", default="filters.plan.yaml", help="Output plan file")
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions in plan")
def filter_plan(file: str, output: str, allow_delete: bool):
    """Generate plan from edited filters file."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Load desired state
        with open(file) as f:
            doc = yaml.safe_load(f)

        desired_filters = doc.get("filters", [])

        # Get current state
        result = service.users().settings().filters().list(userId="me").execute()
        current_filters = result.get("filter", [])

        # Build maps by criteria hash
        desired_by_hash = {}
        for f in desired_filters:
            h = _criteria_hash(f.get("criteria", {}))
            desired_by_hash[h] = f

        current_by_hash = {}
        for f in current_filters:
            criteria = _api_to_yaml_criteria(f.get("criteria", {}))
            h = _criteria_hash(criteria)
            current_by_hash[h] = {
                "id": f["id"],
                "criteria": criteria,
                "api_filter": f,
            }

        # Compute changes
        plan = {
            "type": "gax/filters-plan",
            "source": file,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "create": [],
            "update": [],
            "delete": [],
        }

        # Get label mappings for action comparison
        labels_result = service.users().labels().list(userId="me").execute()
        label_id_to_name = {lbl["id"]: lbl["name"] for lbl in labels_result.get("labels", [])}

        # Check for creates and updates
        for h, desired in desired_by_hash.items():
            if h not in current_by_hash:
                # New filter
                plan["create"].append({
                    "name": desired.get("name", ""),
                    "criteria": desired.get("criteria", {}),
                    "action": desired.get("action", {}),
                })
            else:
                # Check if action changed
                current = current_by_hash[h]
                current_action = _api_to_yaml_action(
                    current["api_filter"].get("action", {}),
                    label_id_to_name
                )
                desired_action = desired.get("action", {})

                if current_action != desired_action:
                    plan["update"].append({
                        "id": current["id"],
                        "name": desired.get("name", ""),
                        "criteria": desired.get("criteria", {}),
                        "action": desired_action,
                    })

        # Check for deletes
        if allow_delete:
            for h, current in current_by_hash.items():
                if h not in desired_by_hash:
                    plan["delete"].append({
                        "id": current["id"],
                        "criteria": current["criteria"],
                    })

        # Remove empty lists
        plan = {k: v for k, v in plan.items() if v or k in ("type", "source", "generated")}

        # Show summary
        has_changes = any(k in plan for k in ("create", "update", "delete"))
        if not has_changes:
            click.echo("No changes to apply.")
            return

        click.echo("Plan:")
        if "create" in plan:
            click.echo(f"  Create: {len(plan['create'])}")
            for item in plan["create"]:
                click.echo(f"    + {item.get('name', 'filter')}")
        if "update" in plan:
            click.echo(f"  Update: {len(plan['update'])} (delete+recreate)")
            for item in plan["update"]:
                click.echo(f"    ~ {item.get('name', 'filter')}")
        if "delete" in plan:
            click.echo(f"  Delete: {len(plan['delete'])}")
            for item in plan["delete"]:
                name = _generate_filter_name(item.get("criteria", {}))
                click.echo(f"    - {name}")

        # Show potential deletes not included
        if not allow_delete:
            potential = sum(1 for h in current_by_hash if h not in desired_by_hash)
            if potential:
                click.echo(f"  (Skipped {potential} deletions, use --delete)")

        # Write plan
        with open(output, "w") as f:
            yaml.dump(plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        click.echo(f"Wrote plan to {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@filter_group.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
def filter_apply(plan_file: str):
    """Apply filter changes from plan file."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Load plan
        with open(plan_file) as f:
            plan = yaml.safe_load(f)

        if plan.get("type") != "gax/filters-plan":
            click.echo("Error: Not a filters plan file", err=True)
            sys.exit(1)

        to_create = plan.get("create", [])
        to_update = plan.get("update", [])
        to_delete = plan.get("delete", [])

        if not to_create and not to_update and not to_delete:
            click.echo("No changes in plan.")
            return

        click.echo("Applying:")
        if to_create:
            click.echo(f"  Create: {len(to_create)}")
        if to_update:
            click.echo(f"  Update: {len(to_update)}")
        if to_delete:
            click.echo(f"  Delete: {len(to_delete)}")

        # Get label mappings
        labels_result = service.users().labels().list(userId="me").execute()
        label_name_to_id = {lbl["name"]: lbl["id"] for lbl in labels_result.get("labels", [])}

        # 1. Delete (including updates - delete first, recreate later)
        for item in to_delete + to_update:
            service.users().settings().filters().delete(
                userId="me", id=item["id"]
            ).execute()
            name = item.get("name") or _generate_filter_name(item.get("criteria", {}))
            if item in to_delete:
                click.echo(f"Deleted: {name}")
            else:
                click.echo(f"Deleted (for update): {name}")

        # 2. Create (including recreate for updates)
        for item in to_create + to_update:
            body = {
                "criteria": _yaml_to_api_criteria(item.get("criteria", {})),
                "action": _yaml_to_api_action(item.get("action", {}), label_name_to_id, service),
            }
            service.users().settings().filters().create(
                userId="me", body=body
            ).execute()
            name = item.get("name") or _generate_filter_name(item.get("criteria", {}))
            if item in to_create:
                click.echo(f"Created: {name}")
            else:
                click.echo(f"Recreated: {name}")

        click.echo("Done.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
