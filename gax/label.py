"""Gmail label management commands.

Declarative label management following ADR 010:
- pull: Export current labels to YAML
- push: Apply label changes (create, rename, delete, visibility)
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from .auth import get_authenticated_credentials
from googleapiclient.discovery import build


# Visibility mappings
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

# System labels that cannot be modified
SYSTEM_LABELS = {
    "INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT",
    "SENT", "DRAFT", "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


@click.group()
def label():
    """Gmail label management (declarative)."""
    pass


@label.command("list")
def label_list():
    """List Gmail labels (TSV output)."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        result = service.users().labels().list(userId="me").execute()
        labels_list = result.get("labels", [])

        # Print header
        click.echo("id\tname\ttype")

        # Sort: system labels first, then user labels alphabetically
        system_labels = [lbl for lbl in labels_list if lbl.get("type") == "system"]
        user_labels = [lbl for lbl in labels_list if lbl.get("type") == "user"]

        system_labels.sort(key=lambda x: x.get("name", ""))
        user_labels.sort(key=lambda x: x.get("name", ""))

        for lbl in system_labels + user_labels:
            label_id = lbl.get("id", "")
            name = lbl.get("name", "")
            label_type = lbl.get("type", "")
            click.echo(f"{label_id}\t{name}\t{label_type}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@label.command("pull")
@click.option("-o", "--output", default="labels.yaml", help="Output file")
@click.option("--all", "include_all", is_flag=True, help="Include system labels (read-only)")
def label_pull(output: str, include_all: bool):
    """Export labels to YAML file.

    Creates a state file with all user labels and their settings.
    Edit this file and use 'push' to apply changes.

    \b
    Example:
        gax label pull
        gax label pull -o my-labels.yaml
        gax label pull --all  # include system labels
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        result = service.users().labels().list(userId="me").execute()
        labels = result.get("labels", [])

        # Build label list
        label_list = []
        for lbl in sorted(labels, key=lambda x: x["name"]):
            label_type = lbl.get("type", "user")

            # Skip system labels unless --all
            if label_type == "system" and not include_all:
                continue

            entry = {"name": lbl["name"]}

            # Add visibility settings if not default
            llv = lbl.get("labelListVisibility")
            if llv and llv != "labelShow":
                entry["visible"] = LABEL_LIST_VISIBILITY_REV.get(llv, llv)

            mlv = lbl.get("messageListVisibility")
            if mlv and mlv != "show":
                entry["show_in_list"] = mlv

            # Add color if present
            color = lbl.get("color")
            if color:
                entry["color"] = {
                    "text": color.get("textColor", "#000000"),
                    "bg": color.get("backgroundColor", "#ffffff"),
                }

            # Mark system labels as read-only
            if label_type == "system":
                entry["system"] = True

            label_list.append(entry)

        # Build output document
        doc = {
            "type": "gax/labels",
            "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "labels": label_list,
        }

        # Write YAML
        path = Path(output)
        with open(path, "w") as f:
            f.write("# Gmail Labels\n")
            f.write(f"# Pulled: {doc['pulled']}\n")
            f.write("#\n")
            f.write("# Visibility options:\n")
            f.write("#   visible: show | hide | unread\n")
            f.write("#   show_in_list: show | hide\n")
            f.write("#\n")
            f.write("# To rename: add 'rename_from: OldName'\n")
            f.write("# To delete: remove label from list and use --delete flag\n")
            f.write("#\n")
            yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        click.echo(f"Wrote {len(label_list)} labels to {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@label.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option("-o", "--output", default="labels.plan.yaml", help="Output plan file")
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions in plan")
def label_plan(file: str, output: str, allow_delete: bool):
    """Generate plan from edited labels file.

    Compares file with current Gmail state and generates a plan:
    - Create: Labels in file but not in Gmail
    - Rename: Labels with 'rename_from' field
    - Update: Changed visibility/color settings
    - Delete: Labels in Gmail but not in file (requires --delete)

    \b
    Example:
        gax label plan labels.yaml
        gax label plan labels.yaml --delete
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Load desired state from file
        with open(file) as f:
            doc = yaml.safe_load(f)

        desired_labels = doc.get("labels", [])

        # Get current state from Gmail
        result = service.users().labels().list(userId="me").execute()
        current_labels = {lbl["name"]: lbl for lbl in result.get("labels", [])}

        # Build desired state map
        desired_map = {}
        rename_map = {}  # old_name -> new_name
        for lbl in desired_labels:
            name = lbl["name"]
            desired_map[name] = lbl
            if "rename_from" in lbl:
                rename_map[lbl["rename_from"]] = name

        # Compute changes
        plan = {
            "type": "gax/labels-plan",
            "source": file,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "create": [],
            "rename": [],
            "update": [],
            "delete": [],
        }

        # Check for creates and updates
        for name, desired in desired_map.items():
            if desired.get("system"):
                continue  # Skip system labels

            if "rename_from" in desired:
                old_name = desired["rename_from"]
                if old_name in current_labels:
                    plan["rename"].append({
                        "from": old_name,
                        "to": name,
                        "id": current_labels[old_name]["id"],
                        **_extract_settings(desired),
                    })
                elif name not in current_labels:
                    plan["create"].append({"name": name, **_extract_settings(desired)})
            elif name not in current_labels:
                plan["create"].append({"name": name, **_extract_settings(desired)})
            else:
                current = current_labels[name]
                if _needs_update(current, desired):
                    plan["update"].append({
                        "name": name,
                        "id": current["id"],
                        **_extract_settings(desired),
                    })

        # Check for deletes
        if allow_delete:
            for name, current in current_labels.items():
                if current.get("type") == "system":
                    continue
                if name not in desired_map and name not in rename_map:
                    plan["delete"].append({"name": name, "id": current["id"]})

        # Remove empty lists
        plan = {k: v for k, v in plan.items() if v or k in ("type", "source", "generated")}

        # Show summary
        has_changes = any(k in plan for k in ("create", "rename", "update", "delete"))
        if not has_changes:
            click.echo("No changes to apply.")
            return

        click.echo("Plan:")
        if "create" in plan:
            click.echo(f"  Create: {len(plan['create'])}")
            for item in plan["create"]:
                click.echo(f"    + {item['name']}")
        if "rename" in plan:
            click.echo(f"  Rename: {len(plan['rename'])}")
            for item in plan["rename"]:
                click.echo(f"    {item['from']} -> {item['to']}")
        if "update" in plan:
            click.echo(f"  Update: {len(plan['update'])}")
            for item in plan["update"]:
                click.echo(f"    ~ {item['name']}")
        if "delete" in plan:
            click.echo(f"  Delete: {len(plan['delete'])}")
            for item in plan["delete"]:
                click.echo(f"    - {item['name']}")

        # Check for potential deletes not included
        if not allow_delete:
            potential_deletes = []
            for name, current in current_labels.items():
                if current.get("type") == "system":
                    continue
                if name not in desired_map and name not in rename_map:
                    potential_deletes.append(name)
            if potential_deletes:
                click.echo(f"  (Skipped {len(potential_deletes)} deletions, use --delete)")

        # Write plan
        with open(output, "w") as f:
            yaml.dump(plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        click.echo(f"Wrote plan to {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@label.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
def label_apply(plan_file: str):
    """Apply label changes from plan file.

    \b
    Example:
        gax label apply labels.plan.yaml
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Load plan
        with open(plan_file) as f:
            plan = yaml.safe_load(f)

        if plan.get("type") != "gax/labels-plan":
            click.echo("Error: Not a labels plan file", err=True)
            sys.exit(1)

        # Get current labels for parent creation
        result = service.users().labels().list(userId="me").execute()
        current_labels = {lbl["name"]: lbl for lbl in result.get("labels", [])}

        # Show summary
        to_create = plan.get("create", [])
        to_rename = plan.get("rename", [])
        to_update = plan.get("update", [])
        to_delete = plan.get("delete", [])

        if not to_create and not to_rename and not to_update and not to_delete:
            click.echo("No changes in plan.")
            return

        click.echo("Applying:")
        if to_create:
            click.echo(f"  Create: {len(to_create)}")
        if to_rename:
            click.echo(f"  Rename: {len(to_rename)}")
        if to_update:
            click.echo(f"  Update: {len(to_update)}")
        if to_delete:
            click.echo(f"  Delete: {len(to_delete)}")

        # Execute changes
        # 1. Create (parents first for nesting)
        created = set()
        for item in sorted(to_create, key=lambda x: x["name"].count("/")):
            _create_label_with_parents(service, item["name"], item, current_labels, created)

        # 2. Rename
        for item in to_rename:
            body = {"name": item["to"]}
            _apply_settings(body, item)
            service.users().labels().patch(userId="me", id=item["id"], body=body).execute()
            click.echo(f"Renamed: {item['from']} -> {item['to']}")

        # 3. Update
        for item in to_update:
            body = {}
            _apply_settings(body, item)
            service.users().labels().patch(userId="me", id=item["id"], body=body).execute()
            click.echo(f"Updated: {item['name']}")

        # 4. Delete
        for item in to_delete:
            service.users().labels().delete(userId="me", id=item["id"]).execute()
            click.echo(f"Deleted: {item['name']}")

        click.echo("Done.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _extract_settings(desired: dict) -> dict:
    """Extract visibility/color settings for plan."""
    settings = {}
    if "visible" in desired:
        settings["visible"] = desired["visible"]
    if "show_in_list" in desired:
        settings["show_in_list"] = desired["show_in_list"]
    if "color" in desired:
        settings["color"] = desired["color"]
    return settings


def _needs_update(current: dict, desired: dict) -> bool:
    """Check if label needs updating."""
    # Check visibility
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

    # Check color
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


def _apply_settings(body: dict, desired: dict):
    """Apply visibility and color settings to API body."""
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


def _create_label_with_parents(
    service, name: str, desired: dict, current_labels: dict, created: set
):
    """Create label, ensuring parent labels exist first."""
    if name in created or name in current_labels:
        return

    # Create parent labels first if nested
    if "/" in name:
        parts = name.split("/")
        for i in range(len(parts) - 1):
            parent = "/".join(parts[: i + 1])
            if parent not in current_labels and parent not in created:
                body = {"name": parent, "labelListVisibility": "labelShow"}
                result = service.users().labels().create(userId="me", body=body).execute()
                current_labels[parent] = result
                created.add(parent)
                click.echo(f"Created: {parent} (parent)")

    # Create the label
    body = {"name": name}
    _apply_settings(body, desired)
    result = service.users().labels().create(userId="me", body=body).execute()
    current_labels[name] = result
    created.add(name)
    click.echo(f"Created: {name}")
