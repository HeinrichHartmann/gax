"""Image blob helpers and Drive API markdown import for Google Docs."""

import base64
import re

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from ..auth import get_authenticated_credentials
from ..store import store_blob


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
    pattern = r"data:image/([a-zA-Z0-9+]+);base64,([A-Za-z0-9+/=]+)"

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
        from ..store import compute_hash

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
    from ..store import get_metadata

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
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    mime_type = "image/png"
                elif data[:2] == b"\xff\xd8":
                    mime_type = "image/jpeg"
                elif data[:6] in (b"GIF87a", b"GIF89a"):
                    mime_type = "image/gif"
                else:
                    mime_type = "application/octet-stream"

            return f"data:{mime_type};base64,{b64_data}"
        except Exception:
            return match.group(0)

    return re.sub(pattern, replace_url, markdown)


def create_doc_from_markdown(
    name: str, markdown: str, *, parent_folder_id: str | None = None, drive_service=None
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

    media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype="text/markdown")

    result = (
        drive_service.files()
        .create(
            body=metadata,
            media_body=media,
        )
        .execute()
    )

    return result["id"]
