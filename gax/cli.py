"""CLI interface for gax.

Policy: All Click command definitions and CLI UX logic live here.

Resource modules (draft.py, gcal.py, etc.) contain pure business logic
and must not import Click or call sys.exit(). They communicate via:

  - logging.info() / logging.debug()  — status messages (shown in spinner)
  - ValueError                        — user-fixable errors
  - Return values                     — results for cli.py to format

Confirmation prompts (--yes, diff display) are handled here in cli.py
using ResourceItem.diff() to preview changes before calling push/pull.

Output conventions for resource methods:
  - No output (most ops): return None, cli.py prints success()
  - Structured result (path, ID): return it, cli.py formats
  - Tabular/streaming (list, diff): accept a file descriptor, write to it
"""

import glob
import re
import sys
import click
from datetime import datetime, timezone
from pathlib import Path

from .gsheet import pull as gsheet_pull, push as gsheet_push, pull_all
from .gsheet.client import GSheetClient
from .multipart import Section, format_section, format_multipart, parse_multipart
from .gsheet.frontmatter import SheetConfig, format_content, parse_content
from .formats import get_format
from . import auth
from . import docs
from .gdoc import doc
from .mail import thread as mail_group, mailbox
from .label import label as mail_label
from .filter import filter_group as mail_filter
from .gcal import cal_cli
from .form import form
from .draft import draft
from .contacts import contacts
from .gdrive import file as gdrive_file


@click.group()
@click.version_option()
def main():
    """gax - Google Access CLI"""
    from . import ui

    ui.setup_logging()


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


def _pull_folder(folder_path: Path, verbose: bool = False, yes: bool = False) -> tuple[bool, str]:
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

            # Import sheet checkout logic
            spreadsheet_id = metadata.get("spreadsheet_id")
            if not spreadsheet_id:
                return False, "No spreadsheet_id in .gax.yaml"

            # Run checkout to scratch dir
            from .gsheet.client import GSheetClient

            client = GSheetClient()
            info = client.get_spreadsheet_info(spreadsheet_id)
            tabs = info["tabs"]

            scratch_path.mkdir(parents=True, exist_ok=True)

            # Write metadata
            new_metadata = {
                "type": "gax/sheet-checkout",
                "spreadsheet_id": spreadsheet_id,
                "url": url,
                "title": info["title"],
                "format": fmt,
                "checked_out": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
            with open(scratch_path / ".gax.yaml", "w") as f:
                yaml.dump(
                    new_metadata,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

            # Create tab files
            for tab_info in tabs:
                tab_name = tab_info["title"]
                safe_tab_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
                safe_tab_name = re.sub(r"\s+", "_", safe_tab_name)
                file_name = f"{safe_tab_name}.tab.sheet.gax.md"

                df = client.read(spreadsheet_id, tab_name)
                formatter = get_format(fmt)
                data = formatter.write(df)

                config = SheetConfig(
                    spreadsheet_id=spreadsheet_id,
                    tab=tab_name,
                    format=fmt,
                    url=url,
                )
                content = format_content(config, data)
                (scratch_path / file_name).write_text(content, encoding="utf-8")

        elif checkout_type == "gax/doc-checkout":
            url = metadata.get("url")
            document_id = metadata.get("document_id")
            if not url or not document_id:
                return False, "No URL or document_id in .gax.yaml"

            # Run checkout to scratch dir
            from .gdoc import pull_doc, format_section, compute_tab_paths

            sections = pull_doc(document_id, url)

            scratch_path.mkdir(parents=True, exist_ok=True)

            # Compute nested tab paths
            tab_paths = compute_tab_paths(sections, scratch_path)

            # Write metadata with tab tree
            tab_tree = []
            for section, fpath in zip(sections, tab_paths):
                if section.section_type == "comments":
                    continue
                tab_tree.append({
                    "id": section.tab_id,
                    "title": section.section_title,
                    "path": str(fpath.relative_to(scratch_path)),
                    "depth": section.tab_depth,
                })

            new_metadata = {
                "type": "gax/doc-checkout",
                "document_id": document_id,
                "url": url,
                "title": sections[0].title if sections else metadata.get("title", ""),
                "checked_out": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "tabs": tab_tree,
            }
            with open(scratch_path / ".gax.yaml", "w") as f:
                yaml.dump(
                    new_metadata,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

            # Create tab files at computed paths
            for section, file_path in zip(sections, tab_paths):
                if section.section_type == "comments":
                    continue
                file_path.parent.mkdir(parents=True, exist_ok=True)
                content = format_section(section)
                file_path.write_text(content, encoding="utf-8")
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
            # Push single sheet tab
            from .gsheet.frontmatter import parse_file
            from .formats import get_format as get_fmt

            config, data = parse_file(file_path)
            fmt = get_fmt(config.format)
            df = fmt.read(data)
            row_count = len(df)

            if not yes:
                click.echo(f"Push {row_count} rows from {file_path} to {config.tab}?")
                if not click.confirm("Proceed?"):
                    return False, "cancelled"

            rows = gsheet_push(file_path, with_formulas=with_formulas)
            return True, f"pushed {rows} rows"

        elif file_type == "gax/doc":
            # Check if it's a single tab file
            content = file_path.read_text(encoding="utf-8")
            sections = parse_multipart(content)
            if not sections:
                return False, "No sections found"

            # Single tab push
            if len(sections) == 1:
                from .gdoc import extract_doc_id, pull_single_tab, update_tab_content
                from .gdoc import native_md
                import difflib

                local_section = sections[0]
                source_url = local_section.headers.get("source", "")
                tab_name = local_section.headers.get(
                    "tab", local_section.headers.get("section_title", "")
                )

                if not source_url:
                    return False, "No source URL found"

                document_id = extract_doc_id(source_url)

                # Get remote content for diff
                remote_section = pull_single_tab(document_id, tab_name, source_url)

                local_lines = local_section.content.splitlines(keepends=True)
                remote_lines = remote_section.content.splitlines(keepends=True)

                diff = list(
                    difflib.unified_diff(
                        remote_lines,
                        local_lines,
                        fromfile="remote",
                        tofile="local",
                        lineterm="",
                    )
                )

                if not diff:
                    return True, "no changes"

                # Check for unsupported features before confirming
                from .gdoc.md2docs import parse_markdown, check_unsupported

                push_warnings = check_unsupported(parse_markdown(local_section.content))

                if not yes:
                    click.echo("Changes to push:")
                    click.echo("-" * 40)
                    for line in diff:
                        click.echo(line.rstrip("\n"))
                    click.echo("-" * 40)
                    if push_warnings:
                        for w in push_warnings:
                            click.echo(f"  Warning: {w.feature}: {w.detail}")
                    if not click.confirm("Push these changes?"):
                        return False, "cancelled"

                content_to_push = native_md.inline_images_from_store(
                    local_section.content
                )
                update_tab_content(document_id, tab_name, content_to_push)
                return True, "pushed"
            else:
                return (
                    False,
                    "Multipart doc push not supported. Use 'gax doc tab push' for individual tabs.",
                )

        elif file_type == "gax/draft":
            from .draft import Draft as DraftResource

            try:
                DraftResource().push(file_path, yes=yes)
                return True, "pushed"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/cal":
            from .gcal import yaml_to_event, create_event, update_event, event_to_yaml

            content = file_path.read_text(encoding="utf-8")
            local_event = yaml_to_event(content)

            if local_event.id:
                # Update existing event
                if not yes:
                    click.echo(f"Update event '{local_event.title}'?")
                    if not click.confirm("Proceed?"):
                        return False, "cancelled"

                result = update_event(local_event)
                return True, f"updated {result.get('htmlLink', '')}"
            else:
                # Create new event
                if not yes:
                    click.echo(f"Create new event '{local_event.title}'?")
                    if not click.confirm("Proceed?"):
                        return False, "cancelled"

                result = create_event(local_event)

                # Update local file with new ID
                local_event.id = result["id"]
                local_event.source = (
                    f"https://calendar.google.com/calendar/event?eid={result['id']}"
                )
                local_event.synced = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )

                new_content = event_to_yaml(local_event)
                file_path.write_text(new_content, encoding="utf-8")

                return True, f"created {result.get('htmlLink', '')}"

        elif file_type == "gax/file":
            # This is a tracking file, find the actual file
            from .gdrive import read_tracking_file, update_file, create_tracking_file

            tracking_data = read_tracking_file(file_path)
            file_id = tracking_data.get("file_id")

            if not file_id:
                return False, "No file_id in tracking file"

            # Find the actual file (tracking file without .gax.md suffix)
            actual_file = file_path.with_suffix("")
            if not actual_file.exists():
                # Try removing the .gax.md to find base file
                name = file_path.name
                if name.endswith(".gax.md"):
                    base_name = name[:-7]  # Remove .gax.md
                    actual_file = file_path.parent / base_name
                    if not actual_file.exists():
                        return False, f"Cannot find actual file for {file_path}"

            if not yes:
                click.echo(f"Update Drive file: {tracking_data.get('name')}")
                click.echo(f"From local file: {actual_file}")
                if not click.confirm("Proceed?"):
                    return False, "cancelled"

            metadata = update_file(file_id, actual_file)
            create_tracking_file(actual_file, metadata)

            return True, f"pushed to {metadata.get('webViewLink', file_id)}"

        elif file_type == "gax/sheet":
            return (
                False,
                "Multipart sheet push not supported. Use 'gax push <folder>.sheet.gax.md.d' or 'gax sheet tab push' for individual tabs.",
            )

        else:
            return False, f"Push not supported for type: {file_type}"

    except Exception as e:
        return False, str(e)


def _push_doc_folder(
    folder_path: Path, metadata: dict, yes: bool = False
) -> tuple[bool, str]:
    """Push a doc checkout folder. Each .tab.gax.md is pushed individually.

    Returns (success, message).
    """
    from .gdoc import update_tab_content
    from .gdoc import native_md
    import difflib

    document_id = metadata.get("document_id")
    url = metadata.get("url")
    if not document_id or not url:
        return False, "No document_id or url in .gax.yaml"

    # Find all .tab.gax.md files recursively
    tab_files = sorted(folder_path.rglob("*.tab.gax.md"))
    if not tab_files:
        return False, "No .tab.gax.md files found"

    # Parse each tab file and show diffs
    tabs_to_push = []
    for tab_file in tab_files:
        content = tab_file.read_text(encoding="utf-8")
        sections = parse_multipart(content)
        if not sections:
            continue

        local_section = sections[0]
        tab_name = local_section.headers.get(
            "tab", local_section.headers.get("section_title", "")
        )
        if not tab_name:
            continue

        # Get remote content for diff
        try:
            remote_md = native_md.export_tab_markdown(document_id, tab_name)
        except ValueError:
            # Tab doesn't exist remotely yet — new tab
            remote_md = ""

        local_lines = local_section.content.splitlines(keepends=True)
        remote_lines = remote_md.splitlines(keepends=True)

        diff = list(
            difflib.unified_diff(
                remote_lines, local_lines, fromfile="remote", tofile="local"
            )
        )

        if diff:
            tabs_to_push.append((tab_file, tab_name, local_section.content, diff))

    if not tabs_to_push:
        return True, "no changes"

    if not yes:
        for tab_file, tab_name, _content, diff in tabs_to_push:
            rel = tab_file.relative_to(folder_path)
            click.echo(f"\n{rel} ({tab_name}):")
            click.echo("-" * 40)
            for line in diff:
                click.echo(line.rstrip("\n"))
        click.echo("-" * 40)
        if not click.confirm(f"\nPush {len(tabs_to_push)} tab(s)?"):
            return False, "cancelled"

    pushed = 0
    for _tab_file, tab_name, content, _diff in tabs_to_push:
        content_to_push = native_md.inline_images_from_store(content)
        update_tab_content(document_id, tab_name, content_to_push)
        pushed += 1

    return True, f"pushed {pushed} tab(s)"


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
            from .gsheet.folder_push import push_folder

            success_result, message = push_folder(
                folder_path, with_formulas=with_formulas, auto_approve=yes
            )
            return success_result, message

        elif checkout_type == "gax/doc-checkout":
            return _push_doc_folder(folder_path, metadata, yes=yes)

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
            from .label import label_pull_to_file

            count = label_pull_to_file(file_path)
            return True, f"{count} labels"

        if file_type == "gax/filters":
            from .filter import filter_pull_to_file

            count = filter_pull_to_file(file_path)
            return True, f"{count} filters"
        if file_type == "gax/doc":
            from .gdoc import pull_doc, extract_doc_id
            from .gdoc import format_multipart as doc_format_multipart

            content = file_path.read_text(encoding="utf-8")
            sections = parse_multipart(content)
            if not sections:
                return False, "No sections found"
            source_url = sections[0].headers.get("source", "")
            if not source_url:
                return False, "No source URL found"
            document_id = extract_doc_id(source_url)
            new_sections = pull_doc(document_id, source_url)
            new_content = doc_format_multipart(new_sections)
            file_path.write_text(new_content, encoding="utf-8")
            return True, f"{len(new_sections)} tabs"

        elif file_type == "gax/sheet":
            rows = pull_all(file_path)
            return True, f"{rows} rows"

        elif file_type == "gax/sheet-tab":
            rows = gsheet_pull(file_path)
            return True, f"{rows} rows"

        elif file_type == "gax/mail":
            from .mail import pull_thread, _mail_section_to_multipart

            content = file_path.read_text(encoding="utf-8")
            sections = parse_multipart(content)
            if not sections:
                return False, "No sections found"
            thread_id = sections[0].headers.get("thread_id", "")
            if not thread_id:
                return False, "No thread_id found"
            new_sections = pull_thread(thread_id)
            new_content = format_multipart(
                [_mail_section_to_multipart(s) for s in new_sections]
            )
            file_path.write_text(new_content, encoding="utf-8")
            return True, f"{len(new_sections)} messages"

        elif file_type == "gax/draft":
            from .draft import Draft as DraftResource

            try:
                DraftResource().pull(file_path)
                return True, "updated"
            except ValueError as e:
                return False, str(e)

        elif file_type == "gax/list":
            from .mail import _parse_gax_header, _relabel_fetch_threads, _write_gax_file
            from .auth import get_authenticated_credentials
            from googleapiclient.discovery import build

            header = _parse_gax_header(file_path)
            if not header["query"]:
                return False, "No query found"
            creds = get_authenticated_credentials()
            service = build("gmail", "v1", credentials=creds)
            labels_result = service.users().labels().list(userId="me").execute()
            label_id_to_name = {
                lbl["id"]: lbl["name"] for lbl in labels_result.get("labels", [])
            }
            thread_data = _relabel_fetch_threads(
                service, header["query"], header["limit"], label_id_to_name
            )
            _write_gax_file(file_path, header["query"], header["limit"], thread_data)
            return True, f"{len(thread_data)} threads"

        elif file_type == "gax/cal":
            from .gcal import yaml_to_event, api_event_to_dataclass, event_to_yaml
            from .auth import get_authenticated_credentials
            from googleapiclient.discovery import build

            content = file_path.read_text(encoding="utf-8")
            local_event = yaml_to_event(content)
            if not local_event.id:
                return False, "No event ID found"
            creds = get_authenticated_credentials()
            service = build("calendar", "v3", credentials=creds)
            api_event = (
                service.events()
                .get(calendarId=local_event.calendar, eventId=local_event.id)
                .execute()
            )
            updated_event = api_event_to_dataclass(api_event, local_event.calendar, "")
            new_content = event_to_yaml(updated_event)
            file_path.write_text(new_content, encoding="utf-8")
            return True, "updated"

        elif file_type == "gax/cal-list":
            from .gcal import _parse_cal_list_file, _clone_events_to_file
            import yaml as _yaml

            time_min, time_max, calendar, verbose = _parse_cal_list_file(file_path)
            _header = _yaml.safe_load(file_path.read_text().split("---", 2)[1])
            count = _clone_events_to_file(
                file_path,
                time_min=time_min,
                time_max=time_max,
                calendar=calendar,
                verbose=verbose,
                days=_header.get("days"),
                date_from=str(_header["from"]) if "from" in _header else None,
                date_to=str(_header["to"]) if "to" in _header else None,
            )
            return True, f"{count} events"

        elif file_type == "gax/form":
            from .form import (
                parse_form_file,
                get_form,
                form_to_yaml,
                form_to_markdown,
                extract_form_id,
            )

            header = parse_form_file(file_path)
            form_id = header.get("id")
            if not form_id:
                source = header.get("source", "")
                if source:
                    form_id = extract_form_id(source)
                else:
                    return False, "No form ID found"
            source_url = header.get(
                "source", f"https://docs.google.com/forms/d/{form_id}/edit"
            )
            content_type = header.get("content-type", "text/markdown")
            form_data = get_form(form_id)
            if content_type == "application/yaml":
                content = form_to_yaml(form_data, source_url)
            else:
                content = form_to_markdown(form_data, source_url)
            file_path.write_text(content, encoding="utf-8")
            items = form_data.get("items", [])
            questions = sum(
                1 for i in items if "questionItem" in i or "questionGroupItem" in i
            )
            return True, f"{questions} questions"

        elif file_type == "gax/contacts":
            from .contacts import parse_contacts_file, list_contacts
            from .contacts import contacts_to_jsonl, contacts_to_markdown, format_header

            header = parse_contacts_file(file_path)
            fmt = header.get("format", "md")
            all_contacts, groups = list_contacts()
            if fmt == "jsonl":
                body = contacts_to_jsonl(all_contacts, groups)
            else:
                body = contacts_to_markdown(all_contacts, groups)
            new_header = format_header(fmt, len(all_contacts))
            content = f"{new_header}\n{body}\n"
            file_path.write_text(content, encoding="utf-8")
            return True, f"{len(all_contacts)} contacts"

        else:
            return False, f"Unsupported type: {file_type}"

    except Exception as e:
        return False, str(e)


@docs.section("main")
@main.command("pull")
@click.argument("files", nargs=-1, required=True)
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
def unified_pull(files: tuple[str, ...], verbose: bool, yes: bool):
    """Pull/update .gax.md file(s) or .gax.md.d folder(s) from their sources.

    Automatically detects file type from YAML header and calls
    the appropriate pull command. For .gax.md.d folders, performs
    a checkout to a scratch directory, shows diff, and prompts
    for confirmation.

    \b
    Examples:
        gax pull file.doc.gax.md           # Pull a single doc
        gax pull *.gax.md                   # Pull all .gax.md files
        gax pull inbox.gax.md notes.doc.gax.md # Pull multiple files
        gax pull folder.doc.gax.md.d/       # Pull a checkout folder
    """
    # Expand globs and '.'
    all_paths: list[Path] = []
    for pattern in files:
        if pattern == ".":
            # Current directory - find all .gax.md files and .gax.md.d folders
            all_paths.extend(Path(".").glob("*.gax.md"))
            all_paths.extend(Path(".").glob("*.gax.md.d"))
        elif "*" in pattern or "?" in pattern:
            # Glob pattern
            all_paths.extend(Path(p) for p in glob.glob(pattern))
        else:
            all_paths.append(Path(pattern))

    if not all_paths:
        click.echo("No .gax.md files or .gax.md.d folders found.", err=True)
        sys.exit(1)

    import logging
    from .ui import operation, success as ui_success, error as ui_error

    logger = logging.getLogger(__name__)

    results = []  # (path, ok, message)

    with operation("Pulling", total=len(all_paths)) as op:
        for path in all_paths:
            if not path.exists():
                results.append((path, False, "not found"))
                op.advance()
                continue

            # Check if it's a folder
            if path.is_dir():
                if not path.name.endswith(".gax.md.d"):
                    results.append((path, False, "not a .gax.md.d folder"))
                    op.advance()
                    continue

                logger.info(f"Pulling {path}/")
                ok, message = _pull_folder(path, verbose, yes=yes)
                results.append((path, ok, message))
            else:
                # Check if this is a file with a .gax.md tracking file (Drive file)
                if not path.name.endswith(".gax.md"):
                    tracking_path = path.with_suffix(path.suffix + ".gax.md")
                    if tracking_path.exists():
                        from .gdrive import (
                            download_file,
                            read_tracking_file,
                            create_tracking_file,
                        )

                        try:
                            tracking_data = read_tracking_file(tracking_path)
                            file_id = tracking_data.get("file_id")

                            if file_id:
                                logger.info(f"Pulling Drive file {path}")
                                metadata = download_file(file_id, path)
                                create_tracking_file(path, metadata)
                                results.append((path, True, "updated"))
                                op.advance()
                                continue
                        except Exception as e:
                            results.append((path, False, str(e)))
                            op.advance()
                            continue

                # Pull regular .gax.md file
                file_type = _detect_file_type(path)
                type_str = f"({file_type})" if file_type else "(unknown)"
                logger.info(f"Pulling {path} {type_str}")

                ok, message = _pull_file(path, verbose)
                results.append((path, ok, message))

            op.advance()

    # Print results after spinner is done
    success_count = 0
    fail_count = 0
    for path, ok, message in results:
        if ok:
            if message != "cancelled":
                ui_success(f"{path}: {message}")
            success_count += 1
        else:
            if message != "cancelled":
                ui_error(f"{path}: {message}")
            fail_count += 1

    if len(all_paths) > 1:
        summary = f"Done: {success_count}/{len(all_paths)} updated"
        if fail_count:
            ui_error(summary)
        else:
            ui_success(summary)

    if fail_count:
        sys.exit(1)


@docs.section("main")
@main.command("push")
@click.argument("files", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--with-formulas", is_flag=True, help="Interpret formulas (sheets only)")
def unified_push(files: tuple[str, ...], yes: bool, with_formulas: bool):
    """Push local .gax.md file(s) or .gax.md.d folder(s) to their sources.

    Automatically detects file type from YAML header and calls
    the appropriate push command. Shows diff/confirmation unless -y is passed.

    \b
    Supported types:
        .sheet.gax.md       Single sheet tab
        .sheet.gax.md.d/    Sheet checkout folder
        .tab.gax.md         Single doc tab
        .draft.gax.md       Gmail draft
        .cal.gax.md         Calendar event
        <file>.gax.md       Drive file tracking

    \b
    Examples:
        gax push file.sheet.gax.md          # Push a single sheet tab
        gax push *.draft.gax.md             # Push all drafts
        gax push Budget.sheet.gax.md.d/     # Push a checkout folder
        gax push event.cal.gax.md -y        # Push without confirmation
    """
    # Expand globs
    all_paths: list[Path] = []
    for pattern in files:
        if "*" in pattern or "?" in pattern:
            all_paths.extend(Path(p) for p in glob.glob(pattern))
        else:
            all_paths.append(Path(pattern))

    if not all_paths:
        click.echo("No .gax.md files or .gax.md.d folders found.", err=True)
        sys.exit(1)

    success_count = 0
    for path in all_paths:
        if not path.exists():
            click.echo(f"Error: {path} not found", err=True)
            continue

        # Check if it's a folder
        if path.is_dir():
            if not path.name.endswith(".gax.md.d"):
                click.echo(
                    f"Skipping directory: {path} (not a .gax.md.d folder)", err=True
                )
                continue

            # Push folder
            result, message = _push_folder(path, yes=yes, with_formulas=with_formulas)

            if result:
                if message != "cancelled":
                    click.echo(f"Pushed {path}: {message}")
                success_count += 1
            else:
                if message != "cancelled":
                    click.echo(f"Error: {path}: {message}", err=True)
        else:
            # Check if this is a non-.gax.md file with a .gax.md tracking file (Drive file)
            if not path.name.endswith(".gax.md"):
                tracking_path = path.with_suffix(path.suffix + ".gax.md")
                if tracking_path.exists():
                    # This is a tracked Drive file - push using the tracking file
                    from .gdrive import (
                        read_tracking_file,
                        update_file,
                        create_tracking_file,
                    )

                    try:
                        tracking_data = read_tracking_file(tracking_path)
                        file_id = tracking_data.get("file_id")

                        if file_id:
                            if not yes:
                                click.echo(
                                    f"Update Drive file: {tracking_data.get('name')}"
                                )
                                click.echo(f"From local file: {path}")
                                if not click.confirm("Proceed?"):
                                    click.echo("Cancelled.")
                                    continue

                            metadata = update_file(file_id, path)
                            create_tracking_file(path, metadata)

                            click.echo(f"Pushed {path} to Drive")
                            success_count += 1
                            continue
                    except Exception as e:
                        click.echo(f"Error pushing Drive file {path}: {e}", err=True)
                        continue

            # Push regular .gax.md file
            file_type = _detect_file_type(path)
            type_str = f"({file_type})" if file_type else "(unknown)"

            click.echo(f"Pushing {path} {type_str}...")

            result, message = _push_file(path, yes=yes, with_formulas=with_formulas)

            if result:
                if message != "cancelled":
                    click.echo(f"  {message}")
                success_count += 1
            else:
                if message != "cancelled":
                    click.echo(f"Error: {path}: {message}", err=True)

    if len(all_paths) > 1:
        click.echo(f"Done: {success_count}/{len(all_paths)} pushed")


@docs.section("main")
@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output file")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Output format (for forms)",
)
@click.pass_context
def clone(ctx, url: str, output: Path | None, fmt: str):
    """Clone a Google resource from URL.

    Supports Google Docs, Sheets, Forms, Gmail, and Calendar events.
    """
    # Google Docs
    if re.search(r"docs\.google\.com/document/d/", url):
        ctx.invoke(doc.commands["clone"], url=url, output=output)

    # Google Sheets
    elif re.search(r"docs\.google\.com/spreadsheets/d/", url):
        ctx.invoke(sheet_clone, url=url, output=output)

    # Google Forms
    elif re.search(r"docs\.google\.com/forms/d/", url):
        ctx.invoke(form.commands["clone"], url=url, output=output, fmt=fmt)

    # Gmail drafts (must come before general mail pattern)
    elif re.search(r"mail\.google\.com/mail/[^#]*#drafts/", url):
        ctx.invoke(draft.commands["clone"], draft_id_or_url=url, output=output)

    # Gmail threads
    elif re.search(r"mail\.google\.com/mail/", url):
        ctx.invoke(mail_group.commands["clone"], thread_id_or_url=url, output=output)

    # Calendar events
    elif re.search(r"calendar\.google\.com/calendar/", url):
        ctx.invoke(
            cal_cli.commands["event"].commands["clone"],
            id_or_url=url,
            output_path=output,
        )

    else:
        click.echo(f"Unrecognized URL: {url}", err=True)
        click.echo("Supported: Google Docs/Sheets/Forms, Gmail, Calendar", err=True)
        sys.exit(1)


@docs.section("main")
@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output folder")
@click.option("-f", "--format", "fmt", default="md", help="Output format (for sheets)")
@click.pass_context
def checkout(ctx, url: str, output: Path | None, fmt: str):
    """Checkout a Google resource from URL into a folder of individual files.

    Supports Google Docs, Sheets, and Calendar.

    \b
    Examples:
        gax checkout <docs-url>
        gax checkout <sheets-url> -f csv
        gax checkout <calendar-url> -o Week/
    """
    # Google Docs
    if re.search(r"docs\.google\.com/document/d/", url):
        kwargs = {"url": url}
        if output:
            kwargs["output"] = output
        ctx.invoke(doc.commands["checkout"], **kwargs)

    # Google Sheets
    elif re.search(r"docs\.google\.com/spreadsheets/d/", url):
        kwargs = {"url": url, "fmt": fmt}
        if output:
            kwargs["output"] = output
        ctx.invoke(sheet_checkout, **kwargs)

    # Calendar
    elif re.search(r"calendar\.google\.com/calendar/", url):
        kwargs = {}
        if output:
            kwargs["output"] = output
        ctx.invoke(cal_cli.commands["checkout"], **kwargs)

    else:
        click.echo(f"Unrecognized URL: {url}", err=True)
        click.echo("Supported: Google Docs, Sheets, Calendar", err=True)
        sys.exit(1)


def _collect_commands(
    cmd: click.Command, prefix: str = "", override_name: str | None = None
) -> list[tuple[str, str, list, list]]:
    """Collect all commands as (full_name, help, arguments, options) tuples."""
    results = []
    # Use override_name if provided (for renamed commands), otherwise use cmd.name
    cmd_name = override_name if override_name else cmd.name
    name = f"{prefix} {cmd_name}".strip() if prefix else cmd_name

    if isinstance(cmd, click.Group):
        for subcmd_name in sorted(cmd.list_commands(None)):
            subcmd = cmd.get_command(None, subcmd_name)
            if subcmd:
                # Pass subcmd_name as override to preserve registered names
                results.extend(_collect_commands(subcmd, name, subcmd_name))
    else:
        # Get first line of help only
        help_text = (cmd.help or "").split("\n")[0]
        arguments = []
        options = []
        for param in cmd.params:
            if isinstance(param, click.Argument):
                arg_name = param.name.upper()
                # Show default if present
                if param.default is not None:
                    arguments.append(f"[{arg_name}]")
                elif param.required:
                    arguments.append(arg_name)
                else:
                    arguments.append(f"[{arg_name}]")
            elif isinstance(param, click.Option) and param.help:
                opts = ", ".join(param.opts)
                options.append((opts, param.help))
        results.append((name, help_text, arguments, options))

    return results


@main.command()
@click.option("--md", is_flag=True, help="Output as Markdown (for pandoc)")
@click.pass_context
def man(ctx, md: bool):
    """Print the complete manual (auto-generated from commands)."""
    root = ctx.find_root().command

    # Collect commands and group by doc_section attribute
    _section_order = {"main": 0, "resource": 1, "utility": 2}
    _section_titles = {"main": "Main", "resource": "Resources", "utility": "Utility"}

    buckets: dict[str, dict[str, tuple[str | None, list]]] = {}
    for cmd_name in root.list_commands(ctx):
        if cmd_name == "man":
            continue
        cmd = root.get_command(ctx, cmd_name)
        if not cmd:
            continue
        commands = _collect_commands(cmd, override_name=cmd_name)
        if not commands:
            continue

        section_key = getattr(cmd, "doc_section", "resource")
        maturity = getattr(cmd, "doc_maturity", None)
        buckets.setdefault(section_key, {})[cmd_name] = (maturity, commands)

    sections: list[tuple[str, dict[str, tuple[str | None, list]]]] = []
    for key in sorted(buckets, key=lambda k: _section_order.get(k, 99)):
        title = _section_titles.get(key, key.title())
        sections.append((title, buckets[key]))

    if md:
        click.echo(_format_man_md(sections))
    else:
        click.echo(_format_man_plain(sections))


def _format_man_plain(sections: list[tuple[str, dict[str, tuple[str | None, list]]]]) -> str:
    """Format manual as plain text."""
    lines = ["GAX(1)", "", "NAME", "    gax - Google Access CLI", ""]
    lines.append("COMMANDS")

    for section_title, groups in sections:
        lines.append(f"\n  {section_title}:")
        for group_name, (maturity, commands) in groups.items():
            label = f"{group_name} [{maturity}]" if maturity else group_name
            lines.append(f"\n    {label}:")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                if args_str:
                    lines.append(f"      gax {full_name} {args_str}")
                else:
                    lines.append(f"      gax {full_name}")
                if help_text:
                    lines.append(f"          {help_text}")
                for opt, opt_help in options:
                    lines.append(f"          {opt}: {opt_help}")

    lines.extend(_file_section_plain())
    return "\n".join(lines)


def _format_man_md(sections: list[tuple[str, dict[str, tuple[str | None, list]]]]) -> str:
    """Format manual as Markdown (suitable for pandoc conversion to man page)."""
    lines = [
        "---",
        'title: GAX',
        'section: 1',
        'header: User Manual',
        'footer: gax',
        "---",
        "",
        "# NAME",
        "",
        "gax - Google Access CLI",
        "",
        "# SYNOPSIS",
        "",
        "**gax** *command* [*options*] [*args*]",
        "",
        "# DESCRIPTION",
        "",
        "Sync Google Workspace (Sheets, Docs, Gmail, Calendar) to local files "
        "that are human-readable, machine-readable, and git-friendly.",
        "",
        "# COMMANDS",
    ]

    for section_title, groups in sections:
        lines.append("")
        lines.append(f"## {section_title}")

        for group_name, (maturity, commands) in groups.items():
            lines.append("")
            label = f"{group_name} [{maturity}]" if maturity else group_name
            lines.append(f"### {label}")
            lines.append("")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                cmd = f"**gax {full_name}**"
                if args_str:
                    cmd += f" *{args_str}*"
                lines.append(cmd)
                if help_text:
                    lines.append(f":   {help_text}")
                for opt, opt_help in options:
                    lines.append(f"    **{opt}**: {opt_help}")
                lines.append("")

    lines.extend(_file_section_md())
    return "\n".join(lines)


def _file_section_plain() -> list[str]:
    return [
        "",
        "FILES",
        "    .sheet.gax.md         Spreadsheet data",
        "    .doc.gax.md           Document",
        "    .tab.gax.md           Single document tab",
        "    .mail.gax.md          Email thread",
        "    .draft.gax.md         Email draft",
        "    .cal.gax.md           Calendar event",
        "    .form.gax.md          Google Form definition",
        "    .gax.md               Mail list (TSV with YAML header)",
        "    .label.mail.gax.md    Gmail labels state",
        "    .filter.mail.gax.md   Gmail filters state",
        "",
        "    ~/.config/gax/credentials.json    OAuth credentials",
        "    ~/.config/gax/token.json          Access token",
        "",
        "SEE ALSO",
        "    gax <command> --help",
    ]


def _file_section_md() -> list[str]:
    return [
        "# FILES",
        "",
        "| Extension | Description |",
        "|-----------|-------------|",
        "| .sheet.gax.md | Spreadsheet data |",
        "| .doc.gax.md | Document |",
        "| .tab.gax.md | Single document tab |",
        "| .mail.gax.md | Email thread |",
        "| .draft.gax.md | Email draft |",
        "| .cal.gax.md | Calendar event |",
        "| .form.gax.md | Google Form definition |",
        "| .gax.md | Mail list (TSV with YAML header) |",
        "| .label.mail.gax.md | Gmail labels state |",
        "| .filter.mail.gax.md | Gmail filters state |",
        "",
        "| Path | Description |",
        "|------|-------------|",
        "| ~/.config/gax/credentials.json | OAuth credentials |",
        "| ~/.config/gax/token.json | Access token |",
        "",
        "# SEE ALSO",
        "",
        "**gax** *command* **--help**",
    ]


# --- Auth commands ---


@docs.section("utility")
@main.group()
def auth_cmd():
    """Authentication management"""
    pass


# Rename to 'auth' for CLI
main.add_command(auth_cmd, name="auth")


@auth_cmd.command()
def login():
    """Authenticate with Google (opens browser)."""
    try:
        if not auth.credentials_exist():
            click.echo(f"OAuth credentials not found at {auth.CREDENTIALS_FILE}")
            click.echo("")
            click.echo(
                "Please download OAuth client credentials from Google Cloud Console:"
            )
            click.echo("  1. Go to https://console.cloud.google.com/apis/credentials")
            click.echo("  2. Create OAuth 2.0 Client ID (Desktop app)")
            click.echo(f"  3. Download JSON and save to: {auth.CREDENTIALS_FILE}")
            sys.exit(1)

        click.echo("Opening browser for authentication...")
        auth.login()
        click.echo("Authenticated successfully!")
        click.echo(f"Token saved to: {auth.TOKEN_FILE}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@auth_cmd.command()
def status():
    """Show authentication status."""
    status = auth.get_status()

    click.echo(f"config_dir\t{status['config_dir']}")
    click.echo(f"credentials_path\t{status['credentials_path']}")
    click.echo(f"credentials_exists\t{status['credentials_exists']}")
    click.echo(f"token_path\t{status['token_path']}")
    click.echo(f"token_exists\t{status['token_exists']}")
    click.echo(f"authenticated\t{status['authenticated']}")


@auth_cmd.command()
def logout():
    """Remove stored authentication token."""
    if auth.logout():
        click.echo("Logged out successfully.")
    else:
        click.echo("No token to remove.")


# --- GSheet commands ---


def _extract_spreadsheet_id(url: str) -> str:
    """Extract spreadsheet ID from Google Sheets URL."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    # Maybe it's already an ID
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Could not parse spreadsheet ID from: {url}")


@docs.section("resource")
@main.group()
def sheet():
    """Google Sheets operations"""
    pass


@sheet.command("clone")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.sheet.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
@click.option(
    "-q", "--quiet",
    is_flag=True,
    help="Suppress multi-tab status message",
)
def sheet_clone(url: str, output: Path | None, fmt: str, quiet: bool):
    """Clone first tab from a spreadsheet to a .sheet.gax.md file.

    For all tabs, use 'gax sheet checkout'.
    """
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        click.echo(f"Fetching spreadsheet: {spreadsheet_id}")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)
        title = info["title"]
        all_tabs = info["tabs"]
        first_tab = all_tabs[0]

        # Fetch only the first tab
        formatter = get_format(fmt)
        df = client.read(spreadsheet_id, first_tab["title"])
        data = formatter.write(df)

        from .formats import get_content_type
        section = Section(
            headers={
                "type": "gax/sheet",
                "title": title,
                "source": url,
                "tab": first_tab["title"],
                "content-type": get_content_type(fmt),
            },
            content=data,
        )

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.sheet.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        content = format_section(section.headers, section.content)
        file_path.write_text(content, encoding="utf-8")

        rows = len(data.strip().split("\n")) - 1
        click.echo(f"Created: {file_path}")
        click.echo(f"Rows: {rows}")

        if not quiet and len(all_tabs) > 1:
            click.echo(
                f'  Tab "{first_tab["title"]}" cloned (1 of {len(all_tabs)} tabs).\n'
                f"  For all tabs: gax sheet checkout {url}"
            )

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def sheet_pull(file: Path):
    """Pull latest data for all tabs in a multipart file or checkout folder."""
    from .ui import success as ui_success

    try:
        if file.is_dir():
            ok, message = _pull_folder(file)
            if ok:
                ui_success(message)
            else:
                click.echo(f"Error: {message}", err=True)
                sys.exit(1)
        else:
            rows = pull_all(file)
            ui_success(f"Pulled {rows} rows to {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("checkout")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: <title>.sheet.gax.md.d)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
def sheet_checkout(url: str, output: Path | None, fmt: str):
    """Checkout all tabs to individual files in a folder.

    Creates a folder with individual .tab.sheet.gax.md files for each tab.
    Incremental: skips existing files.

    \b
    Examples:
        gax sheet checkout <url>
        gax sheet checkout <url> -o MyBudget/
        gax sheet checkout <url> -f csv
    """
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        title = info["title"]
        tabs = info["tabs"]

        # Determine output folder
        if output:
            folder = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            folder = Path(f"{safe_name}.sheet.gax.md.d")

        # Create folder
        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata file
        import yaml

        metadata = {
            "type": "gax/sheet-checkout",
            "spreadsheet_id": spreadsheet_id,
            "url": url,
            "title": title,
            "format": fmt,
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        import logging
        from .ui import operation, success as ui_success

        _logger = logging.getLogger(__name__)

        click.echo(f"Checking out {len(tabs)} tabs to {folder}/")

        created = 0
        skipped = 0

        with operation("Checking out tabs", total=len(tabs)) as op:
            for tab_info in tabs:
                tab_name = tab_info["title"]

                # Generate filename
                safe_tab_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
                safe_tab_name = re.sub(r"\s+", "_", safe_tab_name)
                file_path = folder / f"{safe_tab_name}.tab.sheet.gax.md"

                # Skip if exists
                if file_path.exists():
                    skipped += 1
                    op.advance()
                    continue

                try:
                    _logger.info(f"Fetching tab: {tab_name}")
                    # Read tab data
                    df = client.read(spreadsheet_id, tab_name)

                    # Format data
                    formatter = get_format(fmt)
                    data = formatter.write(df)

                    # Create config
                    config = SheetConfig(
                        spreadsheet_id=spreadsheet_id,
                        tab=tab_name,
                        format=fmt,
                        url=url,
                    )

                    # Write file
                    content = format_content(config, data)
                    file_path.write_text(content, encoding="utf-8")

                    created += 1

                except Exception as e:
                    click.echo(f"  Error with tab '{tab_name}': {e}", err=True)

                op.advance()

        ui_success(f"Checked out: {created}, Skipped: {skipped} (already present)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("push")
@click.argument("folder", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def sheet_push(folder: Path, with_formulas: bool, yes: bool):
    """Push all tabs in a checkout folder to Google Sheets.

    Shows a diff preview of changes and prompts for confirmation before pushing.

    \b
    Examples:
        gax sheet push Budget.sheet.gax.md.d
        gax sheet push Budget.sheet.gax.md.d -y
        gax sheet push Budget.sheet.gax.md.d --with-formulas
    """
    from .gsheet.folder_push import push_folder

    try:
        success, message = push_folder(
            folder, with_formulas=with_formulas, auto_approve=yes
        )
        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("plan")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
def sheet_plan(folder):
    """Show what changes would be pushed to Google Sheets.

    Similar to 'terraform plan' - previews changes without applying them.
    If no folder is specified, looks for a .sheet.gax.md.d folder in the current directory.

    \b
    Examples:
        gax sheet plan
        gax sheet plan Budget.sheet.gax.md.d
    """
    from .gsheet.folder_push import create_push_plan

    try:
        # If no folder specified, find .sheet.gax.md.d in current directory
        if folder is None:
            candidates = list(Path.cwd().glob("*.sheet.gax.md.d"))
            if len(candidates) == 0:
                click.echo(
                    "Error: No .sheet.gax.md.d folder found in current directory",
                    err=True,
                )
                sys.exit(1)
            elif len(candidates) > 1:
                click.echo(
                    "Error: Multiple .sheet.gax.md.d folders found. Please specify one:",
                    err=True,
                )
                for c in candidates:
                    click.echo(f"  {c.name}")
                sys.exit(1)
            folder = candidates[0]

        # Create and display plan
        plan = create_push_plan(folder)
        click.echo("\n" + plan.format_summary())

        if plan.has_changes:
            click.echo(
                "\nRun 'gax sheet apply' to push these changes, or 'gax sheet push <folder>' with confirmation."
            )

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("apply")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def sheet_apply(folder, with_formulas: bool, yes: bool):
    """Apply planned changes by pushing to Google Sheets.

    Similar to 'terraform apply' - shows plan and applies changes with confirmation.
    If no folder is specified, looks for a .sheet.gax.md.d folder in the current directory.

    \b
    Examples:
        gax sheet apply
        gax sheet apply Budget.sheet.gax.md.d
        gax sheet apply Budget.sheet.gax.md.d --with-formulas
    """
    from .gsheet.folder_push import create_push_plan, apply_push_plan

    try:
        # If no folder specified, find .sheet.gax.md.d in current directory
        if folder is None:
            candidates = list(Path.cwd().glob("*.sheet.gax.md.d"))
            if len(candidates) == 0:
                click.echo(
                    "Error: No .sheet.gax.md.d folder found in current directory",
                    err=True,
                )
                sys.exit(1)
            elif len(candidates) > 1:
                click.echo(
                    "Error: Multiple .sheet.gax.md.d folders found. Please specify one:",
                    err=True,
                )
                for c in candidates:
                    click.echo(f"  {c.name}")
                sys.exit(1)
            folder = candidates[0]

        # Create and display plan
        plan = create_push_plan(folder)
        click.echo("\n" + plan.format_summary())

        if not plan.has_changes:
            click.echo("Nothing to apply.")
            return

        # Confirm
        if not yes and not click.confirm("\nApply these changes?"):
            click.echo("Cancelled.")
            return

        # Apply
        total_rows = apply_push_plan(plan, with_formulas=with_formulas)
        click.echo(f"\nPushed {len(plan.changes)} tab(s), {total_rows} rows total")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.group()
def tab():
    """Single tab operations"""
    pass


@tab.command("list")
@click.argument("url")
def tab_list(url: str):
    """List tabs in a spreadsheet (TSV output)."""
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        click.echo(f"# {info['title']}")
        click.echo("index\tid\ttitle")
        for t in info["tabs"]:
            click.echo(f"{t['index']}\t{t['id']}\t{t['title']}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.sheet.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
def tab_clone(url: str, tab_name: str, output: Path | None, fmt: str):
    """Clone a single tab to a .sheet.gax.md file."""
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        click.echo(f"Fetching: {tab_name}")

        client = GSheetClient()
        df = client.read(spreadsheet_id, tab_name)

        formatter = get_format(fmt)
        data = formatter.write(df)

        config = SheetConfig(
            spreadsheet_id=spreadsheet_id,
            tab=tab_name,
            format=fmt,
            url=url,
        )

        content = format_content(config, data)

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.sheet.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        click.echo(f"Created: {file_path}")
        click.echo(f"Rows: {len(df)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_pull(file: Path):
    """Pull latest data for a single tab."""
    try:
        rows = gsheet_pull(file)
        click.echo(f"Pulled {rows} rows to {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def tab_push(file: Path, with_formulas: bool, yes: bool):
    """Push local data to a single tab."""
    try:
        # Preview: count rows in local file
        from .gsheet.frontmatter import parse_file
        from .formats import get_format

        config, data = parse_file(file)
        fmt = get_format(config.format)
        df = fmt.read(data)
        row_count = len(df)

        click.echo(f"Push {row_count} rows from {file} to {config.tab}?")
        if not yes and not click.confirm("Proceed?"):
            click.echo("Aborted.")
            return

        rows = gsheet_push(file, with_formulas=with_formulas)
        click.echo(f"Pushed {rows} rows")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# Register doc, mail, cal, form, file, and contacts command groups
main.add_command(doc)
main.add_command(mail_group, name="mail")  # Flattened from mail.thread (ADR 020)
main.add_command(mailbox)  # Flattened from mail.list (ADR 020)
main.add_command(mail_label, name="mail-label")  # Flattened from mail.label (ADR 020)
main.add_command(
    mail_filter, name="mail-filter"
)  # Flattened from mail.filter (ADR 020)
main.add_command(cal_cli, name="cal")
main.add_command(form)
main.add_command(draft)  # Flattened from mail.draft (ADR 020)
main.add_command(contacts)
main.add_command(gdrive_file, name="file")


REPO = "HeinrichHartmann/gax"
ISSUES_URL = f"https://github.com/{REPO}/issues"


@docs.section("utility")
@main.command()
@click.argument("title", required=False)
@click.option("--body", "-b", help="Issue description")
@click.option(
    "--type",
    "issue_type",
    type=click.Choice(["bug", "feature"]),
    default="bug",
    show_default=True,
    help="Issue type (sets the GitHub label)",
)
def issue(title: str | None, body: str | None, issue_type: str):
    """File a GitHub issue for gax (opens via gh CLI).

    \b
    Examples:
        gax issue
        gax issue "Push swallows newlines"
        gax issue "Attachment support" --type feature
    """
    import shutil
    import subprocess

    if not shutil.which("gh"):
        click.echo("Error: 'gh' (GitHub CLI) is not installed.", err=True)
        click.echo(f"\nPlease file issues at: {ISSUES_URL}/new", err=True)
        click.echo("\nOr install gh: https://cli.github.com/", err=True)
        sys.exit(1)

    cmd = ["gh", "issue", "create", "--repo", REPO, "--label", issue_type]
    if title:
        cmd += ["--title", title]
    if body:
        cmd += ["--body", body]

    sys.exit(subprocess.call(cmd))


def _get_installed_sha() -> str | None:
    """Return the git commit SHA of the currently installed gax uv tool, or None."""
    import glob
    import json

    pattern = (
        f"{Path.home()}/.local/share/uv/tools/gax"
        "/lib/python*/site-packages/gax-*.dist-info/direct_url.json"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    try:
        data = json.loads(Path(matches[0]).read_text())
        return data.get("vcs_info", {}).get("commit_id")
    except Exception:
        return None


def _fetch_commits_since(sha: str, verbose: bool) -> list[str] | None:
    """Use gh CLI to fetch commits on main since sha. Returns formatted lines, or None."""
    import shutil
    import subprocess

    if not shutil.which("gh"):
        return None

    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{REPO}/commits?sha=main&per_page=100",
                "--jq",
                ".[] | .sha + \" \" + (.commit.message | split(\"\\n\")[0])",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None

        lines = []
        for line in result.stdout.strip().splitlines():
            commit_sha, _, message = line.partition(" ")
            if commit_sha.startswith(sha[:7]) or sha.startswith(commit_sha[:7]):
                break
            if verbose:
                lines.append(f"  {commit_sha[:7]}  {message}")
            else:
                lines.append(f"  {commit_sha[:7]}  {message}")
        return lines if lines else []
    except Exception:
        return None


@docs.section("utility")
@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Show full commit messages")
@click.option("-q", "--quiet", is_flag=True, help="Skip changelog after upgrade")
def upgrade(verbose: bool, quiet: bool):
    """Upgrade gax to the latest version from GitHub (uv tool install path).

    After upgrading, shows commits merged since your previous install.
    Requires ``gh`` CLI for the changelog (skipped silently if absent).
    Press Ctrl+C during changelog fetch to skip it.
    """
    import shutil
    import subprocess
    from .ui import operation

    if not shutil.which("uv"):
        click.echo("Error: 'uv' is not installed.", err=True)
        click.echo(
            "Install it: https://docs.astral.sh/uv/getting-started/installation/",
            err=True,
        )
        sys.exit(1)

    old_sha = _get_installed_sha()

    git_url = f"git+https://github.com/{REPO}.git"
    cmd = ["uv", "tool", "install", "--reinstall", git_url]
    click.echo(f"Running: {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(rc)

    if quiet or not shutil.which("gh"):
        return

    if not old_sha:
        click.echo("\nCould not determine previous version; skipping changelog.")
        return

    click.echo("\nFetching changelog... (Ctrl+C to skip)")
    try:
        with operation("Fetching commits from GitHub"):
            commits = _fetch_commits_since(old_sha, verbose)
    except KeyboardInterrupt:
        click.echo("\nChangelog skipped.")
        return

    if commits is None:
        click.echo("(gh CLI unavailable or request failed — skipping changelog)")
    elif not commits:
        click.echo("Already up to date.")
    else:
        click.echo(f"\nChanges since last upgrade ({old_sha[:7]}):")
        for line in commits:
            click.echo(line)


if __name__ == "__main__":
    main()
