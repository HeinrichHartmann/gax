"""Dispatch helpers for gax CLI."""

import click
from pathlib import Path

from .gdrive import File


def _pull_folder(
    folder_path: Path, verbose: bool = False, yes: bool = False
) -> tuple[bool, str]:
    """Pull a .gax.d folder. Returns (success, message)."""
    from .resource import Resource

    try:
        r = Resource.from_file(folder_path)
        r.pull()
        return True, "updated"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _push_file(
    file_path: Path, yes: bool = False, with_formulas: bool = False
) -> tuple[bool, str]:
    """Push a single .gax.md file. Returns (success, message).

    Args:
        file_path: Path to the .gax.md file
        yes: Skip confirmation prompts
        with_formulas: For sheets, interpret formulas

    Returns:
        Tuple of (success, message)
    """
    from .resource import Resource

    try:
        # Special case: sheet-tab has custom confirmation with row count
        if file_path.name.endswith(".tab.sheet.gax.md"):
            try:
                from .gsheet import SheetTab
                from .gsheet.frontmatter import parse_file
                from .formats import get_format as get_fmt

                config, data = parse_file(file_path)
                fmt = get_fmt(config.format)
                df = fmt.read(data)
                row_count = len(df)

                if not yes:
                    click.echo(
                        f"Push {row_count} rows from {file_path} to {config.tab}?"
                    )
                    if not click.confirm("Proceed?"):
                        return False, "cancelled"

                SheetTab(path=file_path).push(with_formulas=with_formulas)
                return True, f"pushed {row_count} rows"
            except ValueError as e:
                return False, str(e)

        # Special case: Drive file tracking indirection
        if file_path.name.endswith(".gax.md"):
            from .gdrive import read_tracking_file

            try:
                tracking_data = read_tracking_file(file_path)
                if tracking_data.get("type") == "gax/file" or "file_id" in tracking_data:
                    name = file_path.name
                    actual_file = file_path.parent / name[:-7]
                    if not actual_file.exists():
                        return False, f"Cannot find actual file for {file_path}"

                    if not yes:
                        click.echo(f"Update Drive file: {tracking_data.get('name')}")
                        click.echo(f"From local file: {actual_file}")
                        if not click.confirm("Proceed?"):
                            return False, "cancelled"

                    File(path=actual_file).push()
                    return True, "pushed to Drive"
            except Exception:
                pass

        # Generic dispatch: diff → confirm → push
        try:
            r = Resource.from_file(file_path)
        except ValueError:
            return False, f"Unsupported file: {file_path}"

        try:
            diff_text = r.diff()
        except NotImplementedError:
            return False, f"Push not supported for: {file_path}"

        if diff_text is None:
            return True, "no changes"
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                return False, "cancelled"
        r.push()
        return True, "pushed"

    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _push_folder(
    folder_path: Path, yes: bool = False, with_formulas: bool = False
) -> tuple[bool, str]:
    """Push a .gax.d folder. Returns (success, message).

    Args:
        folder_path: Path to the .gax.d folder
        yes: Skip confirmation prompts
        with_formulas: For sheets, interpret formulas

    Returns:
        Tuple of (success, message)
    """
    import yaml

    # Read .gax.yaml metadata
    metadata_path = folder_path / ".gax.yaml"
    if not metadata_path.exists():
        return False, "No .gax.yaml metadata file found"

    try:
        with open(metadata_path, "r") as f:
            metadata = yaml.safe_load(f)
    except Exception as e:
        return False, f"Failed to read .gax.yaml: {e}"

    checkout_type = metadata.get("type")
    if not checkout_type:
        return False, "No type in .gax.yaml"

    try:
        from .resource import Resource

        try:
            r = Resource.from_file(folder_path)
        except ValueError:
            return False, f"Push not supported for checkout type: {checkout_type}"

        try:
            diff_text = r.diff()
        except NotImplementedError:
            return False, f"Push not supported for checkout type: {checkout_type}"

        if diff_text is None:
            return True, "no changes"
        if not yes:
            click.echo("\n" + diff_text)
            if not click.confirm("\nPush these changes?"):
                return False, "cancelled"
        r.push(with_formulas=with_formulas)
        return True, "pushed"

    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _pull_file(file_path: Path, verbose: bool = False) -> tuple[bool, str]:
    """Pull a single .gax.md file. Returns (success, message)."""
    from .resource import Resource

    # Special case: single contact (not a Resource)
    if file_path.name.endswith(".contact.gax.yaml"):
        try:
            from .contacts import yaml_to_contact, contact_to_yaml

            c = yaml_to_contact(file_path.read_text(encoding="utf-8"))
            rn = c.get("resourceName", "")
            if not rn:
                return False, "Contact has no resourceName"
            from .contacts import fetch_contacts, api_to_contact

            raw, groups = fetch_contacts()
            for raw_c in raw:
                if raw_c.get("resourceName") == rn:
                    updated = api_to_contact(raw_c, groups)
                    file_path.write_text(contact_to_yaml(updated), encoding="utf-8")
                    return True, "updated"
            return False, f"Contact {rn} not found remotely"
        except ValueError as e:
            return False, str(e)

    # Generic dispatch via Resource.from_file
    try:
        r = Resource.from_file(file_path)
        r.pull()
        return True, "updated"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
