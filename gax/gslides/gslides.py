"""Google Slides operations for gax.

Resource module — follows the gdoc/doc.py reference pattern.

Module structure
================

  API helpers         — fetch presentation, extract text from slides
  Slide(Resource)     — single slide resource (pull/push/diff)
  Presentation        — collection manager (checkout/pull/push/diff)

Design decisions
================

Same conventions as gdoc/doc.py (see its docstring for full rationale).
Additional notes specific to slides:

  Two serialization formats: markdown (read-only, human/LLM friendly)
  and JSON (read-write, full fidelity). Push is JSON-only — markdown
  checkouts refuse push with a warning.

  See ADR 031 for design details.
"""

import json
import logging
import re
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path

import yaml
from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials
from ..gaxfile import Section, format_section, parse_multipart
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# API helpers
# =============================================================================


def extract_presentation_id(url_or_id: str) -> str:
    """Extract presentation ID from Google Slides URL or return as-is."""
    patterns = [
        r"/presentation/d/([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    raise ValueError(f"Cannot extract presentation ID from: {url_or_id}")


def _get_presentation(presentation_id: str, *, service=None) -> dict:
    """Fetch full presentation JSON from Slides API."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("slides", "v1", credentials=creds)
    return service.presentations().get(presentationId=presentation_id).execute()


def _safe_filename(name: str) -> str:
    """Sanitize a name for use as a local filename."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", name)
    return re.sub(r"\s+", "_", safe)


def _extract_text_from_elements(text_elements: list[dict]) -> str:
    """Extract plain text from Slides textElements array."""
    parts = []
    for elem in text_elements:
        run = elem.get("textRun")
        if run:
            parts.append(run.get("content", ""))
    return "".join(parts).rstrip("\n")


def _get_placeholder_type(shape: dict) -> str:
    """Get placeholder type from a shape, or empty string."""
    return shape.get("placeholder", {}).get("type", "")


def _extract_slide_markdown(slide: dict) -> str:
    """Extract markdown text from a slide's page elements.

    Maps placeholder types to markdown:
      TITLE/CENTERED_TITLE → # heading
      SUBTITLE              → ## heading
      BODY/OTHER            → paragraphs
    """
    title_text = ""
    subtitle_text = ""
    body_parts = []

    for element in slide.get("pageElements", []):
        shape = element.get("shape")
        if not shape:
            continue
        text_content = shape.get("text")
        if not text_content:
            continue

        placeholder = _get_placeholder_type(shape)
        text = _extract_text_from_elements(text_content.get("textElements", []))
        if not text.strip():
            continue

        if placeholder in ("TITLE", "CENTERED_TITLE"):
            title_text = text.strip()
        elif placeholder == "SUBTITLE":
            subtitle_text = text.strip()
        else:
            body_parts.append(text)

    lines = []
    if title_text:
        lines.append(f"# {title_text}")
        lines.append("")
    if subtitle_text:
        lines.append(f"## {subtitle_text}")
        lines.append("")
    for part in body_parts:
        lines.append(part)
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n" if lines else ""


def _extract_speaker_notes(slide: dict) -> str:
    """Extract speaker notes text from a slide."""
    notes_page = slide.get("slideProperties", {}).get("notesPage", {})
    for element in notes_page.get("pageElements", []):
        shape = element.get("shape")
        if not shape:
            continue
        placeholder = _get_placeholder_type(shape)
        if placeholder != "BODY":
            continue
        text_content = shape.get("text")
        if not text_content:
            continue
        text = _extract_text_from_elements(text_content.get("textElements", []))
        if text.strip():
            return text.strip()
    return ""


def _get_slide_title(slide: dict) -> str:
    """Extract title from a slide for use in filenames."""
    for element in slide.get("pageElements", []):
        shape = element.get("shape")
        if not shape:
            continue
        placeholder = _get_placeholder_type(shape)
        if placeholder in ("TITLE", "CENTERED_TITLE"):
            text_content = shape.get("text")
            if text_content:
                text = _extract_text_from_elements(text_content.get("textElements", []))
                if text.strip():
                    return text.strip()
    return "Untitled"


def _get_slide_layout(slide: dict) -> str:
    """Get the layout name of a slide."""
    return slide.get("slideProperties", {}).get("layoutObjectId", "")


def _slide_to_content(slide: dict, fmt: str) -> str:
    """Convert a slide to its body content (markdown or JSON)."""
    if fmt == "json":
        return json.dumps(slide, indent=2, ensure_ascii=False) + "\n"

    # Markdown format
    md = _extract_slide_markdown(slide)
    notes = _extract_speaker_notes(slide)
    if notes:
        if md and not md.endswith("\n"):
            md += "\n"
        md += f"\n```notes\n{notes}\n```\n"
    return md


def _slide_headers(
    presentation_title: str,
    source_url: str,
    slide: dict,
    slide_index: int,
    fmt: str,
) -> dict:
    """Build multipart headers for a slide file."""
    headers = {
        "type": "gax/slides",
        "title": presentation_title,
        "source": source_url,
        "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slide_index": slide_index,
        "slide_id": slide.get("objectId", ""),
        "layout": _get_slide_layout(slide),
    }
    if fmt == "json":
        headers["format"] = "json"
    return headers


def _format_slide_file(
    presentation_title: str,
    source_url: str,
    slide: dict,
    slide_index: int,
    fmt: str,
) -> str:
    """Format a slide as a complete multipart .slides.gax.md file."""
    headers = _slide_headers(presentation_title, source_url, slide, slide_index, fmt)
    content = _slide_to_content(slide, fmt)
    return format_section(headers, content)


def _parse_slide_file(path: Path) -> Section:
    """Parse a .slides.gax.md file into a Section."""
    text = path.read_text(encoding="utf-8")
    sections = parse_multipart(text)
    if not sections:
        raise ValueError(f"Cannot parse slide file: {path}")
    return sections[0]


# =============================================================================
# Slide(Resource) — single slide file.
# =============================================================================


class Slide(Resource):
    """Single Google Slide — pull/push/diff a .slides.gax.md file.

    Not used standalone (no clone). Created by Presentation.clone().
    Constructed via from_file(path). No from_url (slides are not
    individually URL-addressable).
    """

    name = "slides"
    FILE_TYPE = "gax/slides"
    FILE_EXTENSIONS = (".slides.gax.md",)
    SCOPES = ("presentations",)

    def pull(self, **kw) -> None:
        """Refresh a single slide file from remote."""
        section = _parse_slide_file(self.path)
        headers = section.headers

        source = headers.get("source", "")
        presentation_id = extract_presentation_id(source)
        slide_id = headers.get("slide_id", "")
        fmt = headers.get("format", "md")

        if not slide_id:
            raise ValueError(f"No slide_id in {self.path}")

        pres = _get_presentation(presentation_id)
        for i, slide in enumerate(pres.get("slides", [])):
            if slide.get("objectId") == slide_id:
                content = _format_slide_file(
                    pres.get("title", ""),
                    source,
                    slide,
                    i,
                    fmt,
                )
                self.path.write_text(content, encoding="utf-8")
                logger.info(f"Updated: {self.path.name}")
                return

        raise ValueError(f"Slide {slide_id} not found in presentation")

    def push(self, **kw) -> None:
        """Push a single slide to remote. JSON format only."""
        section = _parse_slide_file(self.path)
        headers = section.headers
        fmt = headers.get("format", "md")

        if fmt != "json":
            raise ValueError(
                "Push is not supported for markdown format.\n"
                "Re-checkout with --format json to enable push:\n"
                "  gax slides checkout <url> --format json"
            )

        source = headers.get("source", "")
        presentation_id = extract_presentation_id(source)
        slide_id = headers.get("slide_id", "")

        if not slide_id:
            raise ValueError(f"No slide_id in {self.path}")

        # Parse local JSON
        local_slide = json.loads(section.content)

        logger.warning(
            "Push replaces text content only — formatting (bold, italic, "
            "font, color, links) will be lost on modified shapes."
        )

        # Build batchUpdate requests to replace text in all shapes
        requests = []
        for element in local_slide.get("pageElements", []):
            shape = element.get("shape")
            if not shape or not shape.get("text"):
                continue
            object_id = element.get("objectId")
            if not object_id:
                continue

            # Delete all text, then insert new text
            text_elements = shape["text"].get("textElements", [])
            full_text = ""
            for te in text_elements:
                run = te.get("textRun")
                if run:
                    full_text += run.get("content", "")

            requests.append(
                {
                    "deleteText": {
                        "objectId": object_id,
                        "textRange": {"type": "ALL"},
                    }
                }
            )
            if full_text.strip():
                requests.append(
                    {
                        "insertText": {
                            "objectId": object_id,
                            "text": full_text,
                            "insertionIndex": 0,
                        }
                    }
                )

        if requests:
            creds = get_authenticated_credentials()
            service = build("slides", "v1", credentials=creds)
            service.presentations().batchUpdate(
                presentationId=presentation_id,
                body={"requests": requests},
            ).execute()
            logger.info(f"Pushed: {self.path.name}")
        else:
            logger.info(f"No text changes to push: {self.path.name}")

    def diff(self, **kw) -> str | None:
        """Diff a single slide file against remote."""
        section = _parse_slide_file(self.path)
        headers = section.headers
        local_content = section.content

        source = headers.get("source", "")
        presentation_id = extract_presentation_id(source)
        slide_id = headers.get("slide_id", "")
        fmt = headers.get("format", "md")

        if not slide_id:
            raise ValueError(f"No slide_id in {self.path}")

        pres = _get_presentation(presentation_id)
        for i, slide in enumerate(pres.get("slides", [])):
            if slide.get("objectId") == slide_id:
                remote_content = _slide_to_content(slide, fmt)
                if local_content.strip() == remote_content.strip():
                    return None
                diff_lines = unified_diff(
                    remote_content.splitlines(keepends=True),
                    local_content.splitlines(keepends=True),
                    fromfile=f"{self.path.name} (remote)",
                    tofile=f"{self.path.name} (local)",
                )
                return "".join(diff_lines)

        raise ValueError(f"Slide {slide_id} not found in presentation")


# =============================================================================
# Presentation — collection manager for slide checkout folders.
# =============================================================================


class Presentation(Resource):
    """Google Slides presentation — checkout/pull/push a slide deck.

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "presentation"
    URL_PATTERN = r"docs\.google\.com/presentation/d/"
    CHECKOUT_TYPE = "gax/slides-checkout"
    SCOPES = ("presentations",)

    def clone(
        self,
        output: Path | None = None,
        *,
        fmt: str = "md",
        **kw,
    ) -> Path:
        """Checkout a presentation to a local directory. Returns path created."""
        presentation_id = extract_presentation_id(self.url)
        source_url = f"https://docs.google.com/presentation/d/{presentation_id}/edit"

        logger.info(f"Fetching: {presentation_id}")
        pres = _get_presentation(presentation_id)
        title = pres.get("title", "Untitled")
        slides = pres.get("slides", [])

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(title)}.slides.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata
        metadata = {
            "type": "gax/slides-checkout",
            "presentation_id": presentation_id,
            "url": source_url,
            "title": title,
            "format": fmt,
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with open(folder / ".gax.yaml", "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Write per-slide files
        for i, slide in enumerate(slides):
            slide_title = _get_slide_title(slide)
            filename = f"{i:02d}_{_safe_filename(slide_title)}.slides.gax.md"
            file_path = folder / filename

            content = _format_slide_file(title, source_url, slide, i, fmt)
            file_path.write_text(content, encoding="utf-8")
            logger.info(f"Created: {filename}")

        logger.info(f"Checked out {len(slides)} slides to {folder}")
        return folder

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Checkout a presentation to a folder."""
        return self.clone(output=output, **kw)

    def pull(self, **kw) -> None:
        """Pull all slides in a checkout folder."""
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        presentation_id = metadata.get("presentation_id")
        url = metadata.get("url")
        fmt = metadata.get("format", "md")
        if not presentation_id or not url:
            raise ValueError("No presentation_id or url in .gax.yaml")

        logger.info(f"Pulling: {presentation_id}")
        pres = _get_presentation(presentation_id)
        title = pres.get("title", "Untitled")
        slides = pres.get("slides", [])

        # Update metadata
        metadata["checked_out"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        metadata["title"] = title
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Write slide files, tracking which files we wrote
        written_files: set[str] = set()
        for i, slide in enumerate(slides):
            slide_title = _get_slide_title(slide)
            filename = f"{i:02d}_{_safe_filename(slide_title)}.slides.gax.md"
            file_path = self.path / filename
            written_files.add(filename)

            content = _format_slide_file(title, url, slide, i, fmt)
            file_path.write_text(content, encoding="utf-8")
            logger.info(f"Updated: {filename}")

        # Remove stale slide files (deleted or reordered slides)
        for existing in self.path.glob("*.slides.gax.md"):
            if existing.name not in written_files:
                existing.unlink()
                logger.info(f"Removed: {existing.name}")

    def diff(self, **kw) -> str | None:
        """Diff all slides in a checkout folder against remote."""
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        presentation_id = metadata.get("presentation_id")
        url = metadata.get("url")
        fmt = metadata.get("format", "md")
        if not presentation_id or not url:
            raise ValueError("No presentation_id or url in .gax.yaml")

        pres = _get_presentation(presentation_id)
        slides = pres.get("slides", [])

        all_diffs = []
        slide_by_id = {s.get("objectId"): s for s in slides}

        for slide_file in sorted(self.path.glob("*.slides.gax.md")):
            section = _parse_slide_file(slide_file)
            slide_id = section.headers.get("slide_id", "")
            local_content = section.content

            remote_slide = slide_by_id.get(slide_id)
            if not remote_slide:
                all_diffs.append(f"--- {slide_file.name}: slide deleted remotely\n")
                continue

            remote_content = _slide_to_content(remote_slide, fmt)
            if local_content.strip() != remote_content.strip():
                diff_lines = unified_diff(
                    remote_content.splitlines(keepends=True),
                    local_content.splitlines(keepends=True),
                    fromfile=f"{slide_file.name} (remote)",
                    tofile=f"{slide_file.name} (local)",
                )
                all_diffs.append("".join(diff_lines))

        return "\n".join(all_diffs) if all_diffs else None

    def push(self, **kw) -> None:
        """Push all slides. JSON format only."""
        metadata_path = self.path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {self.path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        fmt = metadata.get("format", "md")
        if fmt != "json":
            raise ValueError(
                "Push is not supported for markdown format.\n"
                "Re-checkout with --format json to enable push:\n"
                "  gax slides checkout <url> --format json"
            )

        for slide_file in sorted(self.path.glob("*.slides.gax.md")):
            Slide.from_file(slide_file).push()


Resource.register(Slide)
Resource.register(Presentation)
