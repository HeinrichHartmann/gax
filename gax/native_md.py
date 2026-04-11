"""Native Google Docs Markdown import/export via Drive API.

Uses the Drive API's native text/markdown support for high-quality
roundtrip conversion of Google Docs to/from Markdown.
"""

import base64
import re

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from .auth import get_authenticated_credentials
from .store import store_blob


def extract_images_to_store(markdown: str) -> str:
    """Extract base64 images from markdown and store them as blobs.

    Replaces data:image/... URLs with file:// URLs pointing to
    ~/.gax/store/blob/*.

    Args:
        markdown: Markdown content potentially containing base64 images

    Returns:
        Markdown with base64 images replaced by file:// URLs
    """
    # Pattern to match data URLs in markdown images: ![alt](data:image/...)
    # Also matches standalone data URLs
    pattern = r'data:image/([a-zA-Z0-9+]+);base64,([A-Za-z0-9+/=]+)'

    def replace_image(match: re.Match) -> str:
        image_type = match.group(1)
        b64_data = match.group(2)

        try:
            image_data = base64.b64decode(b64_data)
        except Exception:
            # If decoding fails, leave the original
            return match.group(0)

        # Determine mime type and extension
        mime_type = f"image/{image_type}"
        ext = image_type.lower()
        if ext == "jpeg":
            ext = "jpg"

        # Generate a name based on content hash prefix
        from .store import compute_hash
        content_hash = compute_hash(image_data)
        name = f"image-{content_hash[7:15]}.{ext}"  # sha256-XXXXXXXX -> XXXXXXXX

        # Store the blob
        file_url = store_blob(
            data=image_data,
            original_name=name,
            mime_type=mime_type,
        )

        return file_url

    return re.sub(pattern, replace_image, markdown)


def inline_images_from_store(markdown: str) -> str:
    """Convert file:// URLs back to base64 data URLs for push.

    Replaces file:// URLs pointing to ~/.gax/store/blob/* with
    base64 data URLs that Google Docs can accept.

    Args:
        markdown: Markdown content with file:// image URLs

    Returns:
        Markdown with file:// URLs replaced by base64 data URLs
    """
    from pathlib import Path
    from .store import get_metadata

    # Pattern to match file:// URLs to our blob store
    pattern = r'file://([^\s\)>"]+)'

    def replace_url(match: re.Match) -> str:
        file_path = match.group(1)
        path = Path(file_path)

        if not path.exists():
            # File not found, leave as-is
            return match.group(0)

        # Check if it's in our blob store
        if ".gax/store/blob/" not in file_path:
            # Not our blob, leave as-is
            return match.group(0)

        try:
            data = path.read_bytes()
            b64_data = base64.b64encode(data).decode("ascii")

            # Get mime type from metadata or guess from content
            content_hash = path.name
            meta = get_metadata(content_hash)
            if meta and "mime_type" in meta:
                mime_type = meta["mime_type"]
            else:
                # Guess from file magic
                if data[:8] == b'\x89PNG\r\n\x1a\n':
                    mime_type = "image/png"
                elif data[:2] == b'\xff\xd8':
                    mime_type = "image/jpeg"
                elif data[:6] in (b'GIF87a', b'GIF89a'):
                    mime_type = "image/gif"
                else:
                    mime_type = "application/octet-stream"

            return f"data:{mime_type};base64,{b64_data}"
        except Exception:
            return match.group(0)

    return re.sub(pattern, replace_url, markdown)


def export_doc_markdown(
    document_id: str,
    *,
    drive_service=None,
    extract_images: bool = True,
    num_retries: int = 0,
) -> str:
    """Export a Google Doc to Markdown using native Drive API.

    Args:
        document_id: Google Docs document ID
        drive_service: Optional Drive API service for testing
        extract_images: If True, extract base64 images to blob store
        num_retries: Retries with exponential backoff on 429/5xx

    Returns:
        Markdown content as string
    """
    if drive_service is None:
        creds = get_authenticated_credentials()
        drive_service = build("drive", "v3", credentials=creds)

    result = drive_service.files().export(
        fileId=document_id,
        mimeType="text/markdown"
    ).execute(num_retries=num_retries)

    markdown = result.decode("utf-8")

    if extract_images:
        markdown = extract_images_to_store(markdown)

    # Normalize: Drive API exports bullet lists as "* item", standardize to "- item"
    markdown = re.sub(r'^\* ', '- ', markdown, flags=re.MULTILINE)

    # Normalize: Remove trailing whitespace from all lines
    markdown = re.sub(r'[ \t]+$', '', markdown, flags=re.MULTILINE)

    # Normalize: Unescape Drive API over-escaped characters.
    # Google's markdown export backslash-escapes -, >, #, ~, ` even in
    # contexts where they're not special (e.g. "# nodes", "~equal", `=`).
    markdown = re.sub(r'\\([->#~`])', r'\1', markdown)

    # Normalize: Ensure trailing newline
    if not markdown.endswith('\n'):
        markdown += '\n'

    return markdown


def create_doc_from_markdown(
    name: str,
    markdown: str,
    *,
    parent_folder_id: str | None = None,
    drive_service=None
) -> str:
    """Create a new Google Doc from Markdown content.

    Args:
        name: Name for the new document
        markdown: Markdown content
        parent_folder_id: Optional folder ID to create doc in
        drive_service: Optional Drive API service for testing

    Returns:
        New document ID
    """
    if drive_service is None:
        creds = get_authenticated_credentials()
        drive_service = build("drive", "v3", credentials=creds)

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
    }
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    media = MediaInMemoryUpload(
        markdown.encode("utf-8"),
        mimetype="text/markdown"
    )

    result = drive_service.files().create(
        body=metadata,
        media_body=media,
    ).execute()

    return result["id"]


def split_doc_by_tabs(
    markdown: str,
    tab_titles: list[str]
) -> dict[str, str]:
    """Split exported markdown by tab titles.

    The native Drive API export concatenates all tabs. Each tab starts
    with its title as an H1 header. This function splits the content
    back into individual tabs.

    Args:
        markdown: Full markdown export from Drive API
        tab_titles: List of tab titles in order

    Returns:
        Dict mapping tab title to markdown content
    """
    result = {}
    lines = markdown.split("\n")

    current_tab = None
    current_lines = []

    def _normalize_header(text: str) -> str:
        """Normalize Drive API header: strip bold markers and unescape markdown."""
        text = text.strip()
        if text.startswith("**") and text.endswith("**"):
            text = text[2:-2]
        # Unescape markdown special characters
        text = re.sub(r'\\(.)', r'\1', text)
        return text

    for line in lines:
        # Check if this line is a tab header
        if line.startswith("# "):
            header_text = _normalize_header(line[2:])
            if header_text in tab_titles:
                # Save previous tab
                if current_tab is not None:
                    result[current_tab] = "\n".join(current_lines).strip()
                # Start new tab
                current_tab = header_text
                current_lines = []
                continue

        if current_tab is not None:
            current_lines.append(line)

    # Save last tab
    if current_tab is not None:
        result[current_tab] = "\n".join(current_lines).strip()

    # Fallback: single-tab doc with no H1 title header — use full content
    if not result and len(tab_titles) == 1:
        result[tab_titles[0]] = markdown.strip()

    return result


def get_doc_tabs(document_id: str, *, docs_service=None, num_retries: int = 0) -> list[dict]:
    """Get list of tabs in a document.

    Args:
        document_id: Google Docs document ID
        docs_service: Optional Docs API service for testing
        num_retries: Retries with exponential backoff on 429/5xx

    Returns:
        List of {id, title, index} dicts
    """
    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    doc = docs_service.documents().get(
        documentId=document_id,
        includeTabsContent=True
    ).execute(num_retries=num_retries)

    tabs = []
    for i, tab in enumerate(doc.get("tabs", [])):
        props = tab.get("tabProperties", {})
        tabs.append({
            "id": props.get("tabId", ""),
            "title": props.get("title", f"Tab {i}"),
            "index": i,
        })

    return tabs


def export_tab_markdown(
    document_id: str,
    tab_title: str,
    *,
    drive_service=None,
    docs_service=None,
    num_retries: int = 0,
) -> str:
    """Export a single tab to Markdown.

    Exports the full document and extracts the specified tab.

    Args:
        document_id: Google Docs document ID
        tab_title: Title of the tab to export
        drive_service: Optional Drive API service
        docs_service: Optional Docs API service
        num_retries: Retries with exponential backoff on 429/5xx

    Returns:
        Markdown content for the specified tab
    """
    # Get tab titles
    tabs = get_doc_tabs(document_id, docs_service=docs_service,
                        num_retries=num_retries)
    tab_titles = [t["title"] for t in tabs]

    if tab_title not in tab_titles:
        raise ValueError(f"Tab not found: {tab_title}")

    # Export full doc
    full_md = export_doc_markdown(document_id, drive_service=drive_service,
                                  num_retries=num_retries)

    # Split by tabs
    tab_contents = split_doc_by_tabs(full_md, tab_titles)

    if tab_title not in tab_contents:
        # Tab might be empty or have different header structure
        return ""

    return tab_contents[tab_title]
