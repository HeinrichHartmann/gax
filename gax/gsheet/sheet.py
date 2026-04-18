"""Google Sheets resource module for gax.

Resource module — follows the draft.py reference pattern.

Two resource classes that share this module:

  SheetTab(Resource)  — single tab, single file (.sheet.gax.md / .tab.sheet.gax.md)
  Sheet(Resource)     — whole spreadsheet, folder (.sheet.gax.md.d/)

Module structure
================

  Helpers              — _extract_spreadsheet_id, _safe_filename
  SheetTab(Resource)   — single-tab resource (clone/pull/push)
  Sheet(Resource)      — whole-spreadsheet resource (clone/pull/diff/push + tab_list)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).

  SheetTab handles .tab.sheet.gax.md files (frontmatter format).
  Sheet handles .sheet.gax.md.d/ folders with .gax.yaml metadata.

  The multipart format (.sheet.gax.md with multiple tabs in one file) is
  legacy. pull_all() in clone.py handles it; new code should use folders.

  Sheet.diff() uses the plan/apply workflow from folder_push.py.
  Sheet.push() applies the plan unconditionally.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..resource import Resource
from ..formats import get_format
from ..ui import operation
from .client import GSheetClient
from .frontmatter import SheetConfig, format_content

logger = logging.getLogger(__name__)


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
    """A single Google Sheets tab (.tab.sheet.gax.md file)."""

    name = "sheet-tab"

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone a single tab to a .sheet.gax.md file.

        Keyword args:
            tab_name: specific tab to clone (default: first tab)
            fmt: output format (default: "md")
        """
        tab_name = kw.get("tab_name")
        fmt = kw.get("fmt", "md")

        spreadsheet_id = _extract_spreadsheet_id(url)
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
            url=url,
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

    def pull(self, path: Path, **kw) -> None:
        """Refresh a single-tab file from remote."""
        from .pull import pull

        logger.info(f"Pulling: {path.name}")
        pull(path)

    def push(self, path: Path, **kw) -> None:
        """Push a single-tab file to remote.

        Keyword args:
            with_formulas: interpret formulas (default: False)
        """
        from .push import push

        with_formulas = kw.get("with_formulas", False)
        logger.info(f"Pushing: {path.name}")
        push(path, with_formulas=with_formulas)


# =============================================================================
# Sheet(Resource) — whole spreadsheet, folder
# =============================================================================


class Sheet(Resource):
    """A Google Spreadsheet (.sheet.gax.md.d/ folder)."""

    name = "sheet"

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs into a folder.

        Keyword args:
            fmt: output format (default: "md")
        """
        fmt = kw.get("fmt", "md")

        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        title = info["title"]
        tabs = info["tabs"]

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(title)}.sheet.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata
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
                    url=url,
                )

                content = format_content(config, data)
                file_path.write_text(content, encoding="utf-8")
                created += 1

                op.advance()

        logger.info(f"Checked out: {created}, Skipped: {skipped}")
        return folder

    def pull(self, path: Path, **kw) -> None:
        """Pull all tabs in a checkout folder."""
        metadata_path = path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        spreadsheet_id = metadata.get("spreadsheet_id")
        url = metadata.get("url")
        fmt = metadata.get("format", "md")
        if not spreadsheet_id or not url:
            raise ValueError("No spreadsheet_id or url in .gax.yaml")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        # Update metadata
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

        # Update each tab file
        with operation("Pulling tabs", total=len(info["tabs"])) as op:
            for tab_info in info["tabs"]:
                tab_name = tab_info["title"]
                file_path = path / f"{_safe_filename(tab_name)}.tab.sheet.gax.md"

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

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local folder and remote.

        Returns a human-readable summary, or None if no changes.
        Uses the plan/apply workflow from folder_push.py.
        """
        from .folder_push import create_push_plan

        plan = create_push_plan(path)
        if not plan.has_changes:
            return None
        return plan.format_summary()

    def push(self, path: Path, **kw) -> None:
        """Push all changed tabs in a checkout folder.

        Keyword args:
            with_formulas: interpret formulas (default: False)
        """
        from .folder_push import create_push_plan, apply_push_plan

        with_formulas = kw.get("with_formulas", False)
        plan = create_push_plan(path)
        if plan.has_changes:
            apply_push_plan(plan, with_formulas=with_formulas)

    # Non-standard operations

    def tab_list(self, url: str, out) -> None:
        """Write tab listing to file descriptor."""
        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        out.write(f"# {info['title']}\n")
        out.write("index\tid\ttitle\n")
        for t in info["tabs"]:
            out.write(f"{t['index']}\t{t['id']}\t{t['title']}\n")
