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
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import pandas as pd
import yaml

from ..resource import Resource
from ..formats import get_format
from ..multipart import Section, format_multipart, parse_multipart
from ..ui import operation
from .client import GSheetClient
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

    total_rows = 0
    updated_sections = []

    with operation("Pulling tabs", total=len(sections)) as op:
        for section in sections:
            tab_name = section.headers.get("tab")
            fmt = section.headers.get("format", "csv")

            if not tab_name:
                raise ValueError(f"Section missing 'tab' header in {file_path}")

            logger.info(f"Pulling tab: {tab_name}")
            df = client.read(spreadsheet_id, tab_name)
            formatter = get_format(fmt)
            data = formatter.write(df)

            updated_section = Section(
                headers=section.headers,
                content=data,
            )
            updated_sections.append(updated_section)
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
    file_path: Path, client: GSheetClient | None = None, with_formulas: bool = False
) -> int:
    """Push data from a single-tab file to Google Sheets.

    Returns number of rows pushed.
    """
    if client is None:
        client = GSheetClient()

    config, data = parse_file(file_path)
    fmt = get_format(config.format)
    df = fmt.read(data)

    rows = client.write(
        config.spreadsheet_id, config.tab, df, with_formulas=with_formulas
    )
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


def _compare_dataframes(
    local_df: pd.DataFrame, remote_df: pd.DataFrame
) -> tuple[int, int]:
    """Compare two dataframes and return (added_lines, removed_lines)."""
    local_lines = [",".join(str(c) for c in local_df.columns)]
    remote_lines = [",".join(str(c) for c in remote_df.columns)]

    for _, row in local_df.iterrows():
        local_lines.append(",".join(str(v) for v in row.values))
    for _, row in remote_df.iterrows():
        remote_lines.append(",".join(str(v) for v in row.values))

    diff = list(difflib.unified_diff(remote_lines, local_lines, lineterm=""))
    added = sum(
        1 for line in diff if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff if line.startswith("-") and not line.startswith("---")
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
        except Exception:
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
        except Exception:
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
    with_formulas: bool = False,
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
                    with_formulas=with_formulas,
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

    def pull(self, **kw) -> None:
        """Refresh a single-tab file from remote."""
        logger.info(f"Pulling: {self.path.name}")
        pull_single_tab(self.path)

    def push(self, **kw) -> None:
        """Push a single-tab file to remote.

        Keyword args:
            with_formulas: interpret formulas (default: False)
        """
        with_formulas = kw.get("with_formulas", False)
        logger.info(f"Pushing: {self.path.name}")
        push_single_tab(self.path, with_formulas=with_formulas)


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

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs into a folder.

        Keyword args:
            fmt: output format (default: "md")
        """
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

        metadata = {
            "type": "gax/sheet-checkout",
            "spreadsheet_id": spreadsheet_id,
            "url": self.url,
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

        created = 0
        skipped = 0

        with operation("Checking out tabs", total=len(tabs)) as op:
            for tab_info in tabs:
                tab_name = tab_info["title"]
                file_path = folder / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"

                if file_path.exists():
                    skipped += 1
                    op.advance()
                    continue

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
                file_path.write_text(content, encoding="utf-8")
                created += 1

                op.advance()

        logger.info(f"Checked out: {created}, Skipped: {skipped}")
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

        metadata["checked_out"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        metadata["title"] = info["title"]
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        with operation("Pulling tabs", total=len(info["tabs"])) as op:
            for tab_info in info["tabs"]:
                tab_name = tab_info["title"]
                file_path = self.path / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"

                logger.info(f"Pulling tab: {tab_name}")
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
                file_path.write_text(content, encoding="utf-8")

                op.advance()

    def diff(self, **kw) -> str | None:
        """Preview changes between local folder and remote.

        Returns a human-readable summary, or None if no changes.
        """
        plan = create_push_plan(self.path)
        if not plan.has_changes:
            return None
        return plan.format_summary()

    def push(self, **kw) -> None:
        """Push all changed tabs in a checkout folder.

        Keyword args:
            with_formulas: interpret formulas (default: False)
        """
        with_formulas = kw.get("with_formulas", False)
        plan = create_push_plan(self.path)
        if plan.has_changes:
            apply_push_plan(plan, with_formulas=with_formulas)

    def tab_list(self, out) -> None:
        """Write tab listing to file descriptor."""
        spreadsheet_id = _extract_spreadsheet_id(self.url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        out.write(f"# {info['title']}\n")
        out.write("index\tid\ttitle\n")
        for t in info["tabs"]:
            out.write(f"{t['index']}\t{t['id']}\t{t['title']}\n")
