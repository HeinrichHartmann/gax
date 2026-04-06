"""Push folder changes to Google Sheets with plan/apply workflow"""

import logging
from pathlib import Path
from typing import NamedTuple, Optional
import yaml
import pandas as pd
from ..frontmatter import parse_file
from ..formats import get_format
from ..ui import operation
from .client import GSheetClient

logger = logging.getLogger(__name__)


class TabChange(NamedTuple):
    """Represents changes to a single tab"""
    tab_name: str
    file_path: Path
    local_rows: int
    remote_rows: int
    added_lines: int
    removed_lines: int
    is_new: bool = False
    is_deleted: bool = False


class PushPlan(NamedTuple):
    """Plan for pushing folder changes to Google Sheets"""
    folder_path: Path
    spreadsheet_id: str
    url: str
    changes: list[TabChange]

    @property
    def has_changes(self) -> bool:
        """Check if there are any real changes"""
        return len(self.changes) > 0

    def format_summary(self) -> str:
        """Format a human-readable summary of the plan"""
        if not self.has_changes:
            return "No changes to push"

        lines = [f"Changes to push to {self.folder_path.name}:"]
        lines.append("-" * 60)

        for change in self.changes:
            if change.is_new:
                lines.append(f"  + {change.tab_name} (new tab, {change.local_rows} rows)")
            elif change.is_deleted:
                lines.append(f"  - {change.tab_name} (deleted, {change.remote_rows} rows)")
            else:
                lines.append(
                    f"  M {change.tab_name} "
                    f"(+{change.added_lines}/-{change.removed_lines} lines, "
                    f"{change.local_rows} rows)"
                )

        lines.append("-" * 60)
        lines.append(f"Total: {len(self.changes)} tab(s) changed")
        return "\n".join(lines)


def _compare_dataframes(local_df: pd.DataFrame, remote_df: pd.DataFrame) -> tuple[int, int]:
    """Compare two dataframes and return (added_lines, removed_lines)"""
    import difflib

    # Convert dataframes to string representation for diffing
    local_lines = []
    remote_lines = []

    # Add headers
    local_lines.append(",".join(str(c) for c in local_df.columns))
    remote_lines.append(",".join(str(c) for c in remote_df.columns))

    # Add data rows
    for _, row in local_df.iterrows():
        local_lines.append(",".join(str(v) for v in row.values))

    for _, row in remote_df.iterrows():
        remote_lines.append(",".join(str(v) for v in row.values))

    # Compute diff
    diff = list(difflib.unified_diff(remote_lines, local_lines, lineterm=''))
    added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
    removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))

    return (added, removed)


def create_push_plan(folder_path: Path, client: Optional[GSheetClient] = None) -> PushPlan:
    """Create a plan for pushing folder changes to Google Sheets.

    Analyzes local tab files and compares them with remote Google Sheets data
    to determine what changes would be pushed.

    Args:
        folder_path: Path to .sheet.gax.d folder
        client: Optional GSheetClient instance

    Returns:
        PushPlan with list of changes

    Raises:
        ValueError: If folder is invalid or missing metadata
    """
    if client is None:
        client = GSheetClient()

    # Read metadata
    metadata_path = folder_path / ".gax.yaml"
    if not metadata_path.exists():
        raise ValueError(f"No .gax.yaml metadata file found in {folder_path}")

    with open(metadata_path, "r") as f:
        metadata = yaml.safe_load(f)

    checkout_type = metadata.get("type")
    if checkout_type != "gax/sheet-checkout":
        raise ValueError(f"Unsupported checkout type: {checkout_type}")

    spreadsheet_id = metadata.get("spreadsheet_id")
    url = metadata.get("url")
    if not spreadsheet_id or not url:
        raise ValueError("Missing spreadsheet_id or url in .gax.yaml")

    # Find all tab files
    tab_files = sorted(folder_path.glob("*.tab.sheet.gax"))
    if not tab_files:
        raise ValueError(f"No .tab.sheet.gax files found in {folder_path}")

    # Get remote tabs to detect deletions
    info = client.get_spreadsheet_info(spreadsheet_id)
    remote_tabs = {tab['title'] for tab in info['tabs']}

    changes = []
    local_tabs = set()

    for tab_file in tab_files:
        # Parse local file
        config, data = parse_file(tab_file)
        fmt = get_format(config.format)
        local_df = fmt.read(data)
        local_tabs.add(config.tab)

        # Read remote data
        try:
            remote_df = client.read(spreadsheet_id, config.tab)
        except Exception:
            # Tab doesn't exist remotely (new tab)
            changes.append(TabChange(
                tab_name=config.tab,
                file_path=tab_file,
                local_rows=len(local_df),
                remote_rows=0,
                added_lines=len(local_df) + 1,  # +1 for header
                removed_lines=0,
                is_new=True
            ))
            continue

        # Compare dataframes
        added, removed = _compare_dataframes(local_df, remote_df)

        # Only include if there are actual changes
        if added > 0 or removed > 0:
            changes.append(TabChange(
                tab_name=config.tab,
                file_path=tab_file,
                local_rows=len(local_df),
                remote_rows=len(remote_df),
                added_lines=added,
                removed_lines=removed
            ))

    # Detect deleted tabs (exist remotely but not locally)
    deleted_tabs = remote_tabs - local_tabs
    for tab_name in sorted(deleted_tabs):
        try:
            remote_df = client.read(spreadsheet_id, tab_name)
            remote_rows = len(remote_df)
        except Exception:
            remote_rows = 0

        changes.append(TabChange(
            tab_name=tab_name,
            file_path=Path(""),  # No local file
            local_rows=0,
            remote_rows=remote_rows,
            added_lines=0,
            removed_lines=remote_rows + 1,  # +1 for header
            is_deleted=True
        ))

    return PushPlan(
        folder_path=folder_path,
        spreadsheet_id=spreadsheet_id,
        url=url,
        changes=changes
    )


def apply_push_plan(
    plan: PushPlan,
    client: Optional[GSheetClient] = None,
    with_formulas: bool = False,
) -> int:
    """Apply a push plan by pushing all changed tabs to Google Sheets.

    Args:
        plan: PushPlan to apply
        client: Optional GSheetClient instance
        with_formulas: Whether to interpret formulas

    Returns:
        Total number of rows pushed
    """
    if client is None:
        client = GSheetClient()

    total_rows = 0

    if not plan.has_changes:
        return total_rows

    with operation("Pushing changes...", total=len(plan.changes)) as op:
        for change in plan.changes:
            if change.is_deleted:
                logger.info(f"Deleting: {change.tab_name}")
                client.delete_worksheet(plan.spreadsheet_id, change.tab_name)
            else:
                # Parse local file and push
                config, data = parse_file(change.file_path)
                fmt = get_format(config.format)
                local_df = fmt.read(data)

                action = "Creating" if change.is_new else "Updating"
                logger.info(f"{action}: {change.tab_name}")

                # Push to Google Sheets (create tab if it's new)
                rows = client.write(
                    config.spreadsheet_id,
                    config.tab,
                    local_df,
                    with_formulas=with_formulas,
                    create_if_missing=change.is_new
                )
                total_rows += rows

            op.advance()

    return total_rows


def push_folder(
    folder_path: Path,
    client: Optional[GSheetClient] = None,
    with_formulas: bool = False,
    auto_approve: bool = False
) -> tuple:
    """Push all tabs in a folder to Google Sheets with confirmation.

    This is a convenience function that combines create_push_plan and apply_push_plan.

    Args:
        folder_path: Path to .sheet.gax.d folder
        client: Optional GSheetClient instance
        with_formulas: Whether to interpret formulas
        auto_approve: Skip confirmation prompt

    Returns:
        (success, message) tuple
    """
    import click

    try:
        # Create plan
        plan = create_push_plan(folder_path, client)

        if not plan.has_changes:
            return True, "No changes to push"

        # Show plan
        click.echo("\n" + plan.format_summary())

        # Confirm
        if not auto_approve:
            if not click.confirm("\nPush these changes?"):
                return False, "Cancelled"

        # Apply plan
        total_rows = apply_push_plan(plan, client, with_formulas)

        return True, f"Pushed {len(plan.changes)} tab(s), {total_rows} rows total"

    except Exception as e:
        return False, str(e)
