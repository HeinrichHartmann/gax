"""Dispatch helpers for gax CLI.

Route file/folder operations to the correct resource class based on
file type detection. These functions are called by the unified CLI
commands (pull, push, diff) and will shrink as each resource takes
ownership of its own dispatch.
"""

import click
from pathlib import Path

from .multipart import parse_multipart
from .gsheet import pull_all
from .gsheet.frontmatter import parse_content
from .label import Label
from .filter import Filter
from .gcal import Cal, Event
from .gtask import TaskList as TaskListResource, Task as TaskSingleResource
from .form import Form
from .draft import Draft
from .contacts import Contacts
from .mail import Thread, Mailbox
from .gdrive import File


def _detect_file_type(file_path: Path) -> str | None:
    """Detect .gax.md file type from YAML header or extension.

    Supports:
    - Multipart format (---/---/---) with type in first section header
    - Simple YAML with type field (e.g., .gax.yaml files)

    Returns type string (e.g., 'gax/doc') or None if unknown.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Try to parse as multipart to get type from header
    if content.startswith("---"):
        sections = parse_multipart(content)
        if sections:
            file_type = sections[0].headers.get("type")
            if file_type:
                return file_type

            # Infer from header fields
            headers = sections[0].headers
            if "thread_id" in headers:
                return "gax/mail"
            if "draft_id" in headers:
                return "gax/draft"
            if "spreadsheet_id" in headers or "tab" in headers:
                return "gax/sheet"
            if "document_id" in headers or "source" in headers:
                # Check source URL pattern
                source = headers.get("source", "")
                if "docs.google.com/document" in source:
                    return "gax/doc"
                if "docs.google.com/spreadsheets" in source:
                    return "gax/sheet"

        # Try frontmatter-style for single-tab sheets
        try:
            config, _ = parse_content(content)
            if config.spreadsheet_id:
                return "gax/sheet-tab"
        except Exception:
            pass

        # Check for relabel/label/filter files (YAML-only format)
        for line in content.split("\n"):
            if line.startswith("type:"):
                file_type = line.split(":", 1)[1].strip()
                return file_type
            if line.startswith("query:"):
                return "gax/list"
    else:
        # For simple YAML without leading ---, still check for type field
        for line in content.split("\n")[:20]:  # Check first 20 lines
            if line.startswith("type:"):
                file_type = line.split(":", 1)[1].strip()
                return file_type

    # Fallback to extension
    name = file_path.name.lower()
    if name.endswith(".doc.gax.md") or name.endswith(".tab.gax.md"):
        return "gax/doc"
    if name.endswith(".sheet.gax.md"):
        return "gax/sheet"
    if name.endswith(".mail.gax.md"):
        return "gax/mail"
    if name.endswith(".draft.gax.md"):
        return "gax/draft"
    if name.endswith(".cal.gax.md"):
        return "gax/cal"
    if name.endswith(".task.gax.yaml"):
        return "gax/task"
    if name.endswith(".tasks.gax.md") or name.endswith(".tasks.gax.yaml"):
        return "gax/task-list"
    if name.endswith(".form.gax.md"):
        return "gax/form"
    if ".contacts." in name or name.endswith(".contacts.gax.md"):
        return "gax/contacts"
    # Mailbox/list files often don't have specific extension, just .gax.md
    if name.endswith(".gax.md") or name.endswith(".mailbox.gax.md"):
        # Could be a mailbox file - check for query: field as last resort
        try:
            if "query:" in content:
                return "gax/list"
        except Exception:
            pass

    return None


def _pull_folder(
    folder_path: Path, verbose: bool = False, yes: bool = False
) -> tuple[bool, str]:
    """Pull a .gax.d folder. Returns (success, message).

    Performs a checkout to a scratch directory, shows diff, and asks for confirmation.
    """
    import shutil
    import yaml
    from filecmp import dircmp

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

    # Create scratch directory in .gax/
    scratch_base = Path(".gax")
    scratch_base.mkdir(exist_ok=True)

    # Use folder name for scratch dir
    scratch_name = f"{folder_path.name}.tmp"
    scratch_path = scratch_base / scratch_name

    # Remove scratch dir if it exists
    if scratch_path.exists():
        shutil.rmtree(scratch_path)

    try:
        # Perform checkout to scratch directory
        if checkout_type == "gax/sheet-checkout":
            url = metadata.get("url")
            if not url:
                return False, "No URL in .gax.yaml"
            fmt = metadata.get("format", "md")

            from .gsheet import Sheet

            Sheet().clone(url, output=scratch_path, fmt=fmt)

        elif checkout_type == "gax/doc-checkout":
            url = metadata.get("url")
            if not url:
                return False, "No URL in .gax.yaml"

            from .gdoc import Doc

            Doc().clone(url, output=scratch_path)

        elif checkout_type == "gax/drive-checkout":
            # Drive folders pull in-place (no scratch dir diffing for binary files)
            if scratch_path.exists():
                shutil.rmtree(scratch_path)
            from .gdrive import Folder

            Folder().pull(folder_path)
            return True, "updated"

        elif checkout_type == "gax/task-checkout":
            # Task checkouts pull in-place
            if scratch_path.exists():
                shutil.rmtree(scratch_path)
            for task_file in sorted(folder_path.glob("*.task.gax.yaml")):
                try:
                    TaskSingleResource().pull(task_file)
                except ValueError:
                    pass
            return True, "updated"

        else:
            return False, f"Unsupported checkout type: {checkout_type}"

        # Show diff
        click.echo(f"\nChanges for {folder_path}/:")
        click.echo("-" * 60)

        def filter_timestamps(lines: list[str]) -> list[str]:
            """Remove timestamp lines from YAML headers."""
            import re

            filtered = []
            for line in lines:
                # Skip lines that are just timestamps
                if re.match(
                    r"^\s*(pulled|checked_out|time):\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*$",
                    line,
                ):
                    continue
                filtered.append(line)
            return filtered

        def count_diff_lines(file1: Path, file2: Path) -> tuple[int, int] | None:
            """Count added/removed lines between two files, excluding timestamps.

            Returns (added, removed) or None if files are identical after filtering.
            """
            import difflib

            try:
                content1 = file1.read_text(encoding="utf-8").splitlines(keepends=True)
                content2 = file2.read_text(encoding="utf-8").splitlines(keepends=True)

                # Filter timestamps
                filtered1 = filter_timestamps(content1)
                filtered2 = filter_timestamps(content2)

                # Check if identical after filtering
                if filtered1 == filtered2:
                    return None

                # Count changes
                diff = list(difflib.unified_diff(filtered1, filtered2, lineterm=""))
                added = sum(
                    1
                    for line in diff
                    if line.startswith("+") and not line.startswith("+++")
                )
                removed = sum(
                    1
                    for line in diff
                    if line.startswith("-") and not line.startswith("---")
                )

                return (added, removed)
            except Exception:
                # If we can't read/diff, treat as changed
                return (1, 1)

        real_changes = 0

        def show_diff(dcmp: dircmp, prefix: str = ""):
            nonlocal real_changes

            # Files only in scratch (new files)
            for name in dcmp.left_only:
                if not name.startswith("."):
                    click.echo(f"  + {prefix}{name}")
                    real_changes += 1

            # Files only in current (deleted files)
            for name in dcmp.right_only:
                if not name.startswith("."):
                    click.echo(f"  - {prefix}{name}")
                    real_changes += 1

            # Modified files - check if really changed beyond timestamps
            for name in dcmp.diff_files:
                if not name.startswith("."):
                    scratch_file = Path(dcmp.left) / name
                    current_file = Path(dcmp.right) / name

                    diff_stats = count_diff_lines(scratch_file, current_file)
                    if diff_stats is not None:
                        added, removed = diff_stats
                        click.echo(f"  M {prefix}{name} (+{added}/-{removed} lines)")
                        real_changes += 1
                    # else: only timestamps changed, don't show or count

            # Recurse into subdirectories
            for sub_dcmp in dcmp.subdirs.values():
                show_diff(sub_dcmp, prefix + sub_dcmp.left + "/")

        dcmp = dircmp(str(scratch_path), str(folder_path))
        show_diff(dcmp)

        if real_changes == 0:
            click.echo("  (no changes)")
            shutil.rmtree(scratch_path)
            return True, "up to date"

        click.echo("-" * 60)

        # Prompt for confirmation
        if not yes and not click.confirm(f"\nApply these changes to {folder_path}?"):
            shutil.rmtree(scratch_path)
            return False, "cancelled"

        # Apply changes by syncing scratch to folder
        # Delete files that are in folder but not in scratch
        for name in dcmp.right_only:
            if not name.startswith("."):
                (folder_path / name).unlink()

        # Copy new and modified files from scratch to folder
        for name in dcmp.left_only + dcmp.diff_files:
            if not name.startswith("."):
                shutil.copy2(scratch_path / name, folder_path / name)

        # Copy metadata file
        shutil.copy2(scratch_path / ".gax.yaml", folder_path / ".gax.yaml")

        # Clean up scratch
        shutil.rmtree(scratch_path)

        return True, f"{real_changes} changes applied"

    except Exception as e:
        # Clean up scratch on error
        if scratch_path.exists():
            shutil.rmtree(scratch_path)
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
    file_type = _detect_file_type(file_path)

    if not file_type:
        return False, f"Unknown file type for {file_path}"

    try:
        if file_type == "gax/sheet-tab":
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

                SheetTab().push(file_path, with_formulas=with_formulas)
                return True, f"pushed {row_count} rows"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/doc":
            try:
                from .gdoc import Tab

                t = Tab()
                diff_text = t.diff(file_path)
                if diff_text is None:
                    return True, "no changes"
                if not yes:
                    click.echo(diff_text)
                    if not click.confirm("Push these changes?"):
                        return False, "cancelled"
                t.push(file_path)
                return True, "pushed"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/draft":
            try:
                d = Draft()
                diff_text = d.diff(file_path)
                if diff_text is None:
                    return True, "no changes"
                if not yes:
                    click.echo(diff_text)
                    if not click.confirm("Push these changes?"):
                        return False, "cancelled"
                d.push(file_path)
                return True, "pushed"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/cal":
            try:
                e = Event()
                diff_text = e.diff(file_path)
                if diff_text is None:
                    return True, "no changes"
                if not yes:
                    click.echo(diff_text)
                    if not click.confirm("Push these changes?"):
                        return False, "cancelled"
                link = e.push(file_path)
                return True, f"pushed {link}"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/task":
            try:
                t = TaskSingleResource()
                diff_text = t.diff(file_path)
                if diff_text is None:
                    return True, "no changes"
                if not yes:
                    click.echo(diff_text)
                    if not click.confirm("Push these changes?"):
                        return False, "cancelled"
                t.push(file_path)
                return True, "pushed"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/file":
            # This is a tracking file, find the actual file
            from .gdrive import read_tracking_file

            tracking_data = read_tracking_file(file_path)
            file_id = tracking_data.get("file_id")

            if not file_id:
                return False, "No file_id in tracking file"

            # Find the actual file (tracking file without .gax.md suffix)
            # e.g. report.pdf.gax.md -> report.pdf
            name = file_path.name
            if not name.endswith(".gax.md"):
                return False, f"Cannot find actual file for {file_path}"
            actual_file = file_path.parent / name[:-7]
            if not actual_file.exists():
                return False, f"Cannot find actual file for {file_path}"

            if not yes:
                click.echo(f"Update Drive file: {tracking_data.get('name')}")
                click.echo(f"From local file: {actual_file}")
                if not click.confirm("Proceed?"):
                    return False, "cancelled"

            File().push(actual_file)
            return True, "pushed to Drive"

        elif file_type == "gax/sheet":
            return (
                False,
                "Multipart sheet push not supported. Use 'gax push <folder>.sheet.gax.md.d' or 'gax sheet tab push' for individual tabs.",
            )

        else:
            return False, f"Push not supported for type: {file_type}"

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
        if checkout_type == "gax/sheet-checkout":
            from .gsheet import Sheet

            s = Sheet()
            diff_text = s.diff(folder_path)
            if diff_text is None:
                return True, "no changes"
            if not yes:
                click.echo("\n" + diff_text)
                if not click.confirm("\nPush these changes?"):
                    return False, "cancelled"
            s.push(folder_path, with_formulas=with_formulas)
            return True, "pushed"

        elif checkout_type == "gax/doc-checkout":
            return (
                False,
                "Doc folder push not yet supported. Use 'gax doc tab push' for individual tabs.",
            )

        else:
            return False, f"Push not supported for checkout type: {checkout_type}"

    except Exception as e:
        return False, str(e)


def _pull_file(file_path: Path, verbose: bool = False) -> tuple[bool, str]:
    """Pull a single .gax.md file. Returns (success, message)."""
    file_type = _detect_file_type(file_path)

    if not file_type:
        return False, f"Unknown file type for {file_path}"

    try:
        # Handle labels and filters first (YAML-only, not multipart)
        if file_type == "gax/labels":
            try:
                Label().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        if file_type == "gax/filters":
            try:
                Filter().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)
        if file_type == "gax/doc":
            try:
                from .gdoc import Tab

                Tab().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/sheet":
            try:
                rows = pull_all(file_path)
                return True, f"{rows} rows"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/sheet-tab":
            try:
                from .gsheet import SheetTab

                SheetTab().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/mail":
            try:
                Thread().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/draft":
            try:
                Draft().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/list":
            try:
                Mailbox().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/cal":
            try:
                Event().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/cal-list":
            try:
                Cal().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/task":
            try:
                TaskSingleResource().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/task-list":
            try:
                TaskListResource().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/form":
            try:
                Form().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/contacts":
            try:
                Contacts().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        else:
            return False, f"Unsupported type: {file_type}"

    except Exception as e:
        return False, str(e)
