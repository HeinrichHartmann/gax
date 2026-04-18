"""Google Drive file operations for gax.

Resource module — follows the draft.py reference pattern.

Handles upload/download of arbitrary files to/from Google Drive.
Uses a sidecar .gax.md tracking file alongside the actual file.

Module structure
================

  File format        — create/read sidecar tracking files
  Drive API helpers  — download, upload, update, permissions
  File(Resource)     — resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to file:

  Sidecar tracking: unlike other resources that use a single .gax.md file,
  the file resource tracks an arbitrary file (binary or text) with a separate
  .gax.md YAML sidecar. The Resource methods take the actual file path;
  the class manages the sidecar internally.

  No diff: binary files can't be meaningfully diffed. diff() raises
  NotImplementedError (inherited from base class).
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .auth import get_authenticated_credentials
from .resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# File format — create/read sidecar tracking files.
# =============================================================================


def create_tracking_file(file_path: Path, metadata: dict) -> Path:
    """Create a .gax.md tracking file for a downloaded file.

    Returns path to the tracking file.
    """
    tracking_path = file_path.with_suffix(file_path.suffix + ".gax.md")

    tracking_data = {
        "type": "gax/file",
        "file_id": metadata["id"],
        "name": metadata["name"],
        "mime_type": metadata.get("mimeType", ""),
        "source": metadata.get(
            "webViewLink", f"https://drive.google.com/file/d/{metadata['id']}/view"
        ),
        "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size": int(metadata.get("size", 0)),
    }

    if metadata.get("webContentLink"):
        tracking_data["download"] = metadata["webContentLink"]

    with open(tracking_path, "w") as f:
        yaml.dump(
            tracking_data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    return tracking_path


def read_tracking_file(tracking_path: Path) -> dict:
    """Read a .gax.md tracking file."""
    with open(tracking_path, "r") as f:
        return yaml.safe_load(f)


# =============================================================================
# Drive API helpers — download, upload, update, permissions.
# =============================================================================


def extract_file_id(url_or_id: str) -> str:
    """Extract file ID from Google Drive URL or return as-is if already an ID."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]+)$",
    ]

    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)

    raise ValueError(f"Cannot extract file ID from: {url_or_id}")


def download_file(file_id: str, output_path: Path) -> dict:
    """Download a file from Google Drive. Returns metadata dict."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    file_metadata = (
        service.files()
        .get(fileId=file_id, fields="id,name,mimeType,size,webViewLink,webContentLink")
        .execute()
    )

    request = service.files().get_media(fileId=file_id)

    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return file_metadata


def upload_file(
    file_path: Path,
    name: str | None = None,
    parent_folder_id: str | None = None,
    public: bool = False,
) -> dict:
    """Upload a file to Google Drive. Returns file metadata dict."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    file_name = name or file_path.name

    file_metadata = {"name": file_name}
    if parent_folder_id:
        file_metadata["parents"] = [parent_folder_id]

    media = MediaFileUpload(str(file_path), resumable=True)
    file = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id,name,mimeType,size,webViewLink,webContentLink",
        )
        .execute()
    )

    if public:
        set_public(file["id"], True)
        file = (
            service.files()
            .get(
                fileId=file["id"],
                fields="id,name,mimeType,size,webViewLink,webContentLink",
            )
            .execute()
        )

    return file


def update_file(file_id: str, file_path: Path, public: bool | None = None) -> dict:
    """Update an existing file on Google Drive. Returns updated metadata dict."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    media = MediaFileUpload(str(file_path), resumable=True)
    file = (
        service.files()
        .update(
            fileId=file_id,
            media_body=media,
            fields="id,name,mimeType,size,webViewLink,webContentLink",
        )
        .execute()
    )

    if public is not None:
        set_public(file_id, public)
        file = (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size,webViewLink,webContentLink",
            )
            .execute()
        )

    return file


def set_public(file_id: str, public: bool = True):
    """Make a file public or private."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    if public:
        permission = {"type": "anyone", "role": "reader"}
        service.permissions().create(fileId=file_id, body=permission).execute()
    else:
        permissions = service.permissions().list(fileId=file_id).execute()
        for perm in permissions.get("permissions", []):
            if perm.get("type") == "anyone":
                logger.info(f"Removing permission: {perm['id']}")
                service.permissions().delete(
                    fileId=file_id, permissionId=perm["id"]
                ).execute()


# =============================================================================
# Resource class — the public interface for cli.py.
# =============================================================================


class File(Resource):
    """Google Drive file resource."""

    name = "file"

    def _tracking_path(self, path: Path) -> Path:
        """Compute sidecar tracking file path for an actual file."""
        return path.with_suffix(path.suffix + ".gax.md")

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone a file from Google Drive. Returns path created."""
        file_id = extract_file_id(url)
        logger.info(f"Fetching file: {file_id}")

        creds = get_authenticated_credentials()
        service = build("drive", "v3", credentials=creds)
        metadata = (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size,webViewLink,webContentLink",
            )
            .execute()
        )

        file_path = output or Path(metadata["name"])
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        logger.info(f"Downloading: {file_path}")
        metadata = download_file(file_id, file_path)
        create_tracking_file(file_path, metadata)

        logger.info(f"Size: {metadata.get('size', 'unknown')} bytes")
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Pull latest version from Google Drive."""
        tracking_path = self._tracking_path(path)
        if not tracking_path.exists():
            raise ValueError(
                f"No tracking file found: {tracking_path}\n"
                "Use 'gax file clone' to download a tracked file."
            )

        tracking_data = read_tracking_file(tracking_path)
        file_id = tracking_data.get("file_id")
        if not file_id:
            raise ValueError("No file_id in tracking file")

        logger.info("Downloading latest version")
        metadata = download_file(file_id, path)
        create_tracking_file(path, metadata)
        logger.info(f"Size: {metadata.get('size', 'unknown')} bytes")

    def push(self, path: Path, *, public: bool = False, **kw) -> None:
        """Push local file to Google Drive. Unconditional."""
        tracking_path = self._tracking_path(path)

        if tracking_path.exists():
            tracking_data = read_tracking_file(tracking_path)
            file_id = tracking_data.get("file_id")
            if not file_id:
                raise ValueError("No file_id in tracking file")

            logger.info(f"Updating Drive file: {file_id}")
            metadata = update_file(
                file_id, path, public=public if public else None
            )
        else:
            logger.info(f"Uploading: {path.name}")
            metadata = upload_file(path, public=public)

        create_tracking_file(path, metadata)
        logger.info(f"View: {metadata.get('webViewLink', '')}")
