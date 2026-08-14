"""Google Sheets resource module for gax.

Resource module — follows the draft.py reference pattern.

Two resource classes that share this module:

  SheetTab(Resource)  — single tab, single file (.sheet.gax.md / .tab.sheet.gax.md)
  Sheet(Resource)     — whole spreadsheet, folder (.sheet.gax.md.d/)

Module structure
================

  Multipart helpers    — clone_all, pull_all (legacy multipart format)
  Single-tab helpers   — pull_single_tab, push_single_tab
  Folder push          — TabChange, PushPlan, create_push_plan, apply_push_plan
  Helpers              — _extract_spreadsheet_id, _safe_filename
  SheetTab(Resource)   — single-tab resource (clone/pull/push)
  Sheet(Resource)      — whole-spreadsheet resource (clone/pull/diff/push + tab_list)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).

  SheetTab handles .tab.sheet.gax.md files (frontmatter format).
  Sheet handles .sheet.gax.md.d/ folders with .gax.yaml metadata.

  The multipart format (.sheet.gax.md with multiple tabs in one file) is
  legacy. pull_all() handles it; new code should use folders.

  Sheet.diff() uses the plan/apply workflow (PushPlan).
  Sheet.push() applies the plan unconditionally.

  GSheetClient (client.py) and SheetConfig/frontmatter (frontmatter.py)
  are kept as separate files — they are stable, self-contained abstractions.
"""

import difflib
import logging
import re
import time
from pathlib import Path
from typing import NamedTuple, Optional

import gspread.exceptions
import pandas as pd
import yaml

from ..resource import Resource
from ..formats import get_format
from ..gaxfile import Section, format_multipart, parse_multipart
from ..syncstate import write_sync_header
from ..ui import operation
from .client import GSheetClient, _tlog
from .frontmatter import SheetConfig, parse_file, parse_content, write_file, format_content

logger = logging.getLogger(__name__)


# =============================================================================
# Multipart helpers (legacy — for .sheet.gax.md with all tabs in one file)
# =============================================================================


def clone_all(
    spreadsheet_id: str,
    url: str,
    fmt: str = "csv",
    client: GSheetClient | None = None,
) -> tuple[str, list[Section]]:
    """Clone all tabs from a spreadsheet.

    Returns:
        Tuple of (title, list of Section objects)
    """
    if client is None:
        client = GSheetClient()

    formatter = get_format(fmt)
    info = client.get_spreadsheet_info(spreadsheet_id)
    title = info["title"]
    sections = []

    for idx, tab_info in enumerate(info["tabs"], start=1):
        tab_name = tab_info["title"]
        df = client.read(spreadsheet_id, tab_name)
        data = formatter.write(df)

        from ..formats import get_content_type

        section = Section(
            headers={
                "type": "gax/sheet",
                "title": title,
                "source": url,
                "section": idx,
                "tab": tab_name,
                "content-type": get_content_type(fmt),
            },
            content=data,
        )
        sections.append(section)

    return title, sections


def pull_all(
    file_path: Path,
    client: GSheetClient | None = None,
) -> int:
    """Pull all tabs from a multipart sheet file.

    Returns number of total rows pulled across all tabs.
    """
    if client is None:
        client = GSheetClient()

    content = file_path.read_text(encoding="utf-8")
    sections = parse_multipart(content)

    if not sections:
        raise ValueError(f"No sections found in {file_path}")

    first = sections[0]
    source = first.headers.get("source", "")

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", source)
    if not match:
        raise ValueError(f"Could not extract spreadsheet ID from source: {source}")
    spreadsheet_id = match.group(1)

    # Re-fetch the remote tab list — the authoritative set of tabs.
    # Local sections for deleted remote tabs are dropped; new remote
    # tabs are added.
    info = client.get_spreadsheet_info(spreadsheet_id)
    title = info["title"]
    remote_tabs = [t["title"] for t in info["tabs"]]
    local_by_tab: dict[str, Section] = {}
    for s in sections:
        tab = s.headers.get("tab")
        if not tab:
            raise ValueError(f"Section missing 'tab' header in {file_path}")
        local_by_tab[tab] = s

    for gone in sorted(set(local_by_tab) - set(remote_tabs)):
        logger.warning(f"Removing (no matching remote tab): {gone}")

    default_fmt = first.headers.get("format", "csv")
    all_data = client.read_all(spreadsheet_id, remote_tabs)

    total_rows = 0
    updated_sections = []

    with operation("Pulling tabs", total=len(remote_tabs)) as op:
        for idx, tab_name in enumerate(remote_tabs, start=1):
            logger.info(f"Pulling tab: {tab_name}")
            df = all_data[tab_name]

            local = local_by_tab.get(tab_name)
            if local is not None:
                headers = dict(local.headers)
                fmt = headers.get("format", default_fmt)
            else:
                from ..formats import get_content_type

                fmt = default_fmt
                headers = {
                    "type": "gax/sheet",
                    "title": title,
                    "source": source,
                    "tab": tab_name,
                    "content-type": get_content_type(fmt),
                }
            headers["section"] = idx
            headers = write_sync_header(headers)

            formatter = get_format(fmt)
            data = formatter.write(df)

            updated_sections.append(Section(headers=headers, content=data))
            total_rows += len(df)
            op.advance()

    output = format_multipart(updated_sections)
    file_path.write_text(output, encoding="utf-8")

    return total_rows


# =============================================================================
# Single-tab helpers
# =============================================================================


def pull_single_tab(file_path: Path, client: GSheetClient | None = None) -> int:
    """Pull data from Google Sheets to a single-tab file.

    Returns number of rows pulled.
    """
    if client is None:
        client = GSheetClient()

    config, _ = parse_file(file_path)
    df = client.read(config.spreadsheet_id, config.tab, config.range)

    fmt = get_format(config.format)
    data = fmt.write(df)

    write_file(file_path, config, data)
    return len(df)


def push_single_tab(
    file_path: Path, client: GSheetClient | None = None, values: bool = False
) -> int:
    """Push data from a single-tab file to Google Sheets.

    Returns number of rows pushed.
    """
    if client is None:
        client = GSheetClient()

    config, data = parse_file(file_path)
    fmt = get_format(config.format)
    df = fmt.read(data)

    rows = client.write(config.spreadsheet_id, config.tab, df, values=values)
    return rows


# =============================================================================
# Folder push — plan/apply workflow
# =============================================================================


class TabChange(NamedTuple):
    """Represents changes to a single tab."""

    tab_name: str
    file_path: Path
    local_rows: int
    remote_rows: int
    added_lines: int
    removed_lines: int
    is_new: bool = False
    is_deleted: bool = False


class PushPlan(NamedTuple):
    """Plan for pushing folder changes to Google Sheets."""

    folder_path: Path
    spreadsheet_id: str
    url: str
    changes: list[TabChange]

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    def format_summary(self) -> str:
        """Format a human-readable summary of the plan."""
        if not self.has_changes:
            return "No changes to push"

        lines = [f"Changes to push to {self.folder_path.name}:"]
        lines.append("-" * 60)

        for change in self.changes:
            if change.is_new:
                lines.append(
                    f"  + {change.tab_name} (new tab, {change.local_rows} rows)"
                )
            elif change.is_deleted:
                lines.append(
                    f"  - {change.tab_name} (deleted, {change.remote_rows} rows)"
                )
            else:
                lines.append(
                    f"  M {change.tab_name} "
                    f"(+{change.added_lines}/-{change.removed_lines} lines, "
                    f"{change.local_rows} rows)"
                )

        lines.append("-" * 60)
        lines.append(f"Total: {len(self.changes)} tab(s) changed")
        return "\n".join(lines)


def _df_to_csv_lines(df: pd.DataFrame) -> list[str]:
    """Serialize a DataFrame to a list of CSV lines (header + rows)."""
    lines = [",".join(str(c) for c in df.columns)]
    for _, row in df.iterrows():
        lines.append(",".join(str(v) for v in row.values))
    return lines


def _unified_diff_csv(
    remote_df: pd.DataFrame,
    local_df: pd.DataFrame,
    fromfile: str = "remote",
    tofile: str = "local",
) -> str:
    """Return a unified diff of two DataFrames as CSV text.

    Convention: remote is 'a' (---), local is 'b' (+++), so the diff
    reads as "what changed locally relative to remote".
    """
    remote_lines = _df_to_csv_lines(remote_df)
    local_lines = _df_to_csv_lines(local_df)
    return "\n".join(
        difflib.unified_diff(
            remote_lines, local_lines, fromfile=fromfile, tofile=tofile, lineterm=""
        )
    )


def _compare_dataframes(
    local_df: pd.DataFrame, remote_df: pd.DataFrame
) -> tuple[int, int]:
    """Compare two dataframes and return (added_lines, removed_lines)."""
    diff_text = _unified_diff_csv(remote_df, local_df)
    diff_lines = diff_text.splitlines()
    added = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    return (added, removed)


def create_push_plan(
    folder_path: Path, client: Optional[GSheetClient] = None
) -> PushPlan:
    """Create a plan for pushing folder changes to Google Sheets."""
    if client is None:
        client = GSheetClient()

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

    tab_files = sorted(folder_path.glob("*.tab.sheet.gax.md"))
    if not tab_files:
        raise ValueError(f"No .tab.sheet.gax.md files found in {folder_path}")

    info = client.get_spreadsheet_info(spreadsheet_id)
    remote_tabs = {tab["title"] for tab in info["tabs"]}

    changes = []
    local_tabs = set()

    for tab_file in tab_files:
        config, data = parse_file(tab_file)
        fmt = get_format(config.format)
        local_df = fmt.read(data)
        local_tabs.add(config.tab)

        try:
            remote_df = client.read(spreadsheet_id, config.tab)
        except gspread.exceptions.WorksheetNotFound:
            changes.append(
                TabChange(
                    tab_name=config.tab,
                    file_path=tab_file,
                    local_rows=len(local_df),
                    remote_rows=0,
                    added_lines=len(local_df) + 1,
                    removed_lines=0,
                    is_new=True,
                )
            )
            continue

        added, removed = _compare_dataframes(local_df, remote_df)

        if added > 0 or removed > 0:
            changes.append(
                TabChange(
                    tab_name=config.tab,
                    file_path=tab_file,
                    local_rows=len(local_df),
                    remote_rows=len(remote_df),
                    added_lines=added,
                    removed_lines=removed,
                )
            )

    deleted_tabs = remote_tabs - local_tabs
    for tab_name in sorted(deleted_tabs):
        try:
            remote_df = client.read(spreadsheet_id, tab_name)
            remote_rows = len(remote_df)
        except gspread.exceptions.WorksheetNotFound:
            remote_rows = 0

        changes.append(
            TabChange(
                tab_name=tab_name,
                file_path=Path(""),
                local_rows=0,
                remote_rows=remote_rows,
                added_lines=0,
                removed_lines=remote_rows + 1,
                is_deleted=True,
            )
        )

    return PushPlan(
        folder_path=folder_path, spreadsheet_id=spreadsheet_id, url=url, changes=changes
    )


def apply_push_plan(
    plan: PushPlan,
    client: Optional[GSheetClient] = None,
    values: bool = False,
) -> int:
    """Apply a push plan. Returns total number of rows pushed."""
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
                config, data = parse_file(change.file_path)
                fmt = get_format(config.format)
                local_df = fmt.read(data)

                action = "Creating" if change.is_new else "Updating"
                logger.info(f"{action}: {change.tab_name}")

                rows = client.write(
                    config.spreadsheet_id,
                    config.tab,
                    local_df,
                    values=values,
                    create_if_missing=change.is_new,
                )
                total_rows += rows

            op.advance()

    return total_rows


# =============================================================================
# Helpers
# =============================================================================


def _extract_spreadsheet_id(url: str) -> str:
    """Extract spreadsheet ID from Google Sheets URL or return as-is."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Could not parse spreadsheet ID from: {url}")


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", name)
    return re.sub(r"\s+", "_", safe)


# =============================================================================
# SheetTab(Resource) — single tab, single file
# =============================================================================


class SheetTab(Resource):
    """A single Google Sheets tab (.tab.sheet.gax.md file).

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "sheet-tab"
    URL_PATTERN = r"docs\.google\.com/spreadsheets/d/"
    FILE_EXTENSIONS = (".sheet.gax.md",)
    SCOPES = ("spreadsheets",)

    @classmethod
    def from_file(cls, path: Path) -> "SheetTab":
        """Construct from a .sheet.gax.md file."""
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            raise ValueError(f"Cannot read: {path}")
        if content.startswith("---"):
            try:
                config, _ = parse_content(content)
                if config.spreadsheet_id and config.tab:
                    return cls(path=path)
            except Exception:
                pass
        raise ValueError(f"Not a sheet-tab file: {path}")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone a single tab to a .sheet.gax.md file.

        Keyword args:
            tab_name: specific tab to clone (default: first tab)
            fmt: output format (default: "md")
        """
        tab_name = kw.get("tab_name")
        fmt = kw.get("fmt", "md")

        spreadsheet_id = _extract_spreadsheet_id(self.url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)
        title = info["title"]

        if tab_name is None:
            tab_name = info["tabs"][0]["title"]

        logger.info(f"Fetching tab: {tab_name}")
        df = client.read(spreadsheet_id, tab_name)

        formatter = get_format(fmt)
        data = formatter.write(df)

        config = SheetConfig(
            spreadsheet_id=spreadsheet_id,
            tab=tab_name,
            format=fmt,
            url=self.url,
        )

        content = format_content(config, data)

        if output:
            file_path = output
        else:
            safe = _safe_filename(tab_name if kw.get("tab_name") else title)
            file_path = Path(f"{safe}.sheet.gax.md")

        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        return file_path

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs from this spreadsheet URL into a folder.

        Delegates to Sheet.checkout() — tab URLs and spreadsheet URLs are
        interchangeable for checkout purposes.
        """
        return Sheet(url=self.url).checkout(output=output, **kw)

    def get(self, **kw) -> str:
        """Fetch current remote content for this tab."""
        config, _ = parse_file(self.path)
        client = GSheetClient()
        df = client.read(config.spreadsheet_id, config.tab, config.range)
        fmt = get_format(config.format)
        return fmt.write(df)

    def pull(self, **kw) -> None:
        """Refresh a single-tab file from remote."""
        logger.info(f"Pulling: {self.path.name}")
        pull_single_tab(self.path)

    def diff(self, **kw) -> str | None:
        """Show unified diff between local file and remote tab."""
        config, data = parse_file(self.path)
        fmt = get_format(config.format)
        local_df = fmt.read(data)

        client = GSheetClient()
        remote_df = client.read(config.spreadsheet_id, config.tab, config.range)

        diff_text = _unified_diff_csv(
            remote_df,
            local_df,
            fromfile=f"remote/{config.tab}",
            tofile=str(self.path),
        )
        return diff_text or None

    def push(self, **kw) -> None:
        """Push a single-tab file to remote.

        Keyword args:
            values: write as literal strings, no formula interpretation
        """
        values = kw.get("values", False)
        logger.info(f"Pushing: {self.path.name}")
        push_single_tab(self.path, values=values)


# =============================================================================
# Sheet(Resource) — whole spreadsheet, folder
# =============================================================================


class Sheet(Resource):
    """A Google Spreadsheet (.sheet.gax.md.d/ folder).

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "sheet"
    URL_PATTERN = r"docs\.google\.com/spreadsheets/d/"
    CHECKOUT_TYPE = "gax/sheet-checkout"
    HAS_GENERIC_DISPATCH = False
    SCOPES = ("spreadsheets",)

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs into a folder.

        Keyword args:
            fmt: output format (default: "md")
        """
        t_total = time.perf_counter()
        fmt = kw.get("fmt", "md")

        spreadsheet_id = _extract_spreadsheet_id(self.url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        title = info["title"]
        tabs = info["tabs"]

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(title)}.sheet.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        metadata = write_sync_header(
            {
                "type": "gax/sheet-checkout",
                "spreadsheet_id": spreadsheet_id,
                "url": self.url,
                "title": title,
                "format": fmt,
            }
        )
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Determine which tabs need fetching
        tabs_to_fetch = []
        for tab_info in tabs:
            tab_name = tab_info["title"]
            file_path = folder / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"
            if not file_path.exists():
                tabs_to_fetch.append(tab_name)

        # Fetch all needed tabs in parallel
        if tabs_to_fetch:
            all_data = client.read_all(spreadsheet_id, tabs_to_fetch)
        else:
            all_data = {}

        created = 0
        skipped = 0

        with operation("Checking out tabs", total=len(tabs)) as op:
            for tab_info in tabs:
                tab_name = tab_info["title"]
                file_path = folder / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"

                if tab_name not in all_data:
                    skipped += 1
                    op.advance()
                    continue

                logger.info(f"Writing tab: {tab_name}")
                df = all_data[tab_name]

                formatter = get_format(fmt)
                data = formatter.write(df)

                config = SheetConfig(
                    spreadsheet_id=spreadsheet_id,
                    tab=tab_name,
                    format=fmt,
                    url=self.url,
                )

                content = format_content(config, data)
                file_path.write_text(content, encoding="utf-8")
                created += 1

                op.advance()

        logger.info(f"Checked out: {created}, Skipped: {skipped}")
        _tlog(f"clone total: {time.perf_counter() - t_total:.3f}s")
        return folder

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs into a folder."""
        return self.clone(output=output, **kw)

    def pull(self, **kw) -> None:
        """Pull all tabs in a checkout folder."""
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        spreadsheet_id = metadata.get("spreadsheet_id")
        url = metadata.get("url")
        fmt = metadata.get("format", "md")
        if not spreadsheet_id or not url:
            raise ValueError("No spreadsheet_id or url in .gax.yaml")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        metadata = write_sync_header(metadata)
        metadata["title"] = info["title"]
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Fetch all tabs in parallel
        tab_names = [t["title"] for t in info["tabs"]]
        all_data = client.read_all(spreadsheet_id, tab_names)

        # Track which files belong to remote tabs
        remote_tab_files = set()

        with operation("Pulling tabs", total=len(info["tabs"])) as op:
            for tab_info in info["tabs"]:
                tab_name = tab_info["title"]
                file_path = self.path / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"
                remote_tab_files.add(file_path.name)

                logger.info(f"Writing tab: {tab_name}")
                df = all_data[tab_name]

                formatter = get_format(fmt)
                data = formatter.write(df)

                config = SheetConfig(
                    spreadsheet_id=spreadsheet_id,
                    tab=tab_name,
                    format=fmt,
                    url=url,
                )

                content = format_content(config, data)
                file_path.write_text(content, encoding="utf-8")

                op.advance()

        # Clean up local files that no longer have a matching remote tab
        for stale in sorted(self.path.iterdir()):
            if stale.name == ".gax.yaml":
                continue
            if stale.name in remote_tab_files:
                continue
            logger.warning(f"Removing (no matching remote tab): {stale.name}")
            stale.unlink()

    def diff(self, **kw) -> str | None:
        """Show unified diff between local folder and remote spreadsheet.

        Direction-neutral: tabs present only locally or only remotely are
        listed as headers; common tabs show a unified CSV diff.
        """
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        spreadsheet_id = metadata.get("spreadsheet_id")
        if not spreadsheet_id:
            raise ValueError("No spreadsheet_id in .gax.yaml")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)
        remote_tab_names = {t["title"] for t in info["tabs"]}

        # Read local tab files
        tab_files = sorted(self.path.glob("*.tab.sheet.gax.md"))
        local_tabs: dict[str, tuple[pd.DataFrame, Path]] = {}
        for tab_file in tab_files:
            config, data = parse_file(tab_file)
            fmt = get_format(config.format)
            local_tabs[config.tab] = (fmt.read(data), tab_file)

        # Fetch remote data for tabs that exist both locally and remotely
        common = sorted(set(local_tabs) & remote_tab_names)
        remote_data = client.read_all(spreadsheet_id, common) if common else {}

        sections: list[str] = []

        # Unified diffs for common tabs
        for tab_name in common:
            local_df, tab_file = local_tabs[tab_name]
            diff_text = _unified_diff_csv(
                remote_data[tab_name],
                local_df,
                fromfile=f"remote/{tab_name}",
                tofile=str(tab_file),
            )
            if diff_text:
                sections.append(diff_text)

        # Remote-only tabs (would be added on pull)
        for tab_name in sorted(remote_tab_names - set(local_tabs)):
            sections.append(f"--- remote/{tab_name}\n+++ (not present locally)")

        # Local-only tabs (would be created on push)
        for tab_name in sorted(set(local_tabs) - remote_tab_names):
            local_df, tab_file = local_tabs[tab_name]
            sections.append(f"--- (not present remotely)\n+++ {tab_file}")

        return "\n\n".join(sections) or None

    def get(self, **kw) -> str:
        """Fetch all remote tabs and return formatted content."""
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        spreadsheet_id = metadata.get("spreadsheet_id")
        fmt_name = metadata.get("format", "md")
        if not spreadsheet_id:
            raise ValueError("No spreadsheet_id in .gax.yaml")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)
        tab_names = [t["title"] for t in info["tabs"]]

        tab_filter = kw.get("tab")
        if tab_filter:
            if tab_filter not in tab_names:
                raise ValueError(
                    f"Tab '{tab_filter}' not found. Available: {', '.join(tab_names)}"
                )
            tab_names = [tab_filter]

        all_data = client.read_all(spreadsheet_id, tab_names)
        formatter = get_format(fmt_name)

        parts = []
        for tab_name in tab_names:
            df = all_data[tab_name]
            if len(tab_names) > 1:
                parts.append(f"# {tab_name}\n")
            parts.append(formatter.write(df))

        return "\n".join(parts)

    def push(self, **kw) -> None:
        """Push all changed tabs in a checkout folder.

        Keyword args:
            values: write as literal strings, no formula interpretation
        """
        values = kw.get("values", False)
        plan = create_push_plan(self.path)
        if plan.has_changes:
            apply_push_plan(plan, values=values)

    def tab_list(self, out) -> None:
        """Write tab listing to file descriptor."""
        spreadsheet_id = _extract_spreadsheet_id(self.url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        out.write(f"# {info['title']}\n")
        out.write("index\tid\ttitle\n")
        for t in info["tabs"]:
            out.write(f"{t['index']}\t{t['id']}\t{t['title']}\n")


Resource.register(SheetTab)
Resource.register(Sheet)
