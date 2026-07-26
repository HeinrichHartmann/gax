#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Basic checks for wiki/ (counterpart to wiki/schema.md).

Assertions against parsed YAML frontmatter of every page, plus:
relative links resolve, every page is listed in index.md.

Usage: direnv exec . scripts/wikilint.py
"""

import re
import sys
from datetime import date
from pathlib import Path

import yaml

WIKI = Path(__file__).resolve().parent.parent / "wiki"
CATEGORIES = ["product", "solution", "technical", "implementation"]
LINK_RE = re.compile(r"\[[^\]]*\]\((?!\w+:)([^)#\s]+)(?:#[^)]*)?\)")

errors = []


def check(page: Path, cond: bool, msg: str):
    if not cond:
        errors.append(f"{page.relative_to(WIKI.parent)}: {msg}")


def frontmatter(page: Path, text: str) -> dict:
    parts = text.split("---\n", 2)
    if len(parts) < 3 or parts[0]:
        check(page, False, "missing YAML frontmatter")
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        check(page, False, f"unparseable frontmatter: {e}")
        return {}


pages = sorted(p for c in CATEGORIES for p in (WIKI / c).glob("*.md"))
index = (WIKI / "index.md").read_text()

for page in pages:
    text = page.read_text()
    fm = frontmatter(page, text)
    if fm:
        check(page, isinstance(fm.get("title"), str), "title: missing or not a string")
        check(page, isinstance(fm.get("description"), str), "description: missing or not a string")
        check(page, fm.get("status") in ("current", "draft", "stale"), f"status: {fm.get('status')!r} not current/draft/stale")
        check(page, isinstance(fm.get("updated"), date), f"updated: {fm.get('updated')!r} not a YYYY-MM-DD date")
        check(page, bool(fm.get("sources")) and isinstance(fm.get("sources"), list), "sources: missing or empty list")
    check(page, page.name in index, "not listed in index.md")
    for target in LINK_RE.findall(text):
        check(page, (page.parent / target).exists(), f"broken link: {target}")

for target in LINK_RE.findall(index):
    check(WIKI / "index.md", (WIKI / target).exists(), f"broken link: {target}")

print("\n".join(errors))
print(f"wikilint: {len(pages)} pages, {len(errors)} error(s)")
sys.exit(1 if errors else 0)
