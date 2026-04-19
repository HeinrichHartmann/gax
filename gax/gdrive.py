"""Google Drive file and folder operations for gax.

Resource module — follows the draft.py reference pattern.

Handles upload/download of arbitrary files to/from Google Drive.
Uses a sidecar .gax.md tracking file alongside the actual file.

Module structure
================

  File format        — create/read sidecar tracking files
  Drive API helpers  — download, upload, update, permissions, folder listing
  File(Resource)     — single file resource (clone/pull/push)
  Folder             — folder collection manager (checkout/pull)

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

  Folder checkout: creates a .drive.gax.md.d/ directory with per-file
  sidecars. Google Workspace files (Docs, Sheets, Forms) are cloned
  via their native gax resource instead of binary download.
  See ADR 028 for design details.
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
# Folder API helpers — list folder contents, extract folder ID.
# =============================================================================

WORKSPACE_MIME_TYPES = {
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.form": "form",
    "application/vnd.google-apps.presentation": "slides",
}

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def extract_folder_id(url_or_id: str) -> str:
    """Extract folder ID from Google Drive folder URL or return as-is."""
    patterns = [
        r"/folders/([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    raise ValueError(f"Cannot extract folder ID from: {url_or_id}")


def get_folder_metadata(folder_id: str, *, service=None) -> dict:
    """Get folder name and metadata. Returns dict with id, name."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("drive", "v3", credentials=creds)
    return service.files().get(fileId=folder_id, fields="id,name").execute()


def list_folder(folder_id: str, *, recursive: bool = False, service=None) -> list[dict]:
    """List files in a Drive folder.

    Returns flat list of dicts, each with:
      id, name, mimeType, size, path (relative to root folder), is_folder

    Handles pagination. With recursive=True, traverses subfolders.
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("drive", "v3", credentials=creds)

    def _list_one(parent_id: str, prefix: str) -> list[dict]:
        items = []
        page_token = None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{parent_id}' in parents and trashed=false",
                    fields="nextPageToken,files(id,name,mimeType,size)",
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                rel_path = f"{prefix}{f['name']}" if prefix else f["name"]
                is_folder = f["mimeType"] == FOLDER_MIME_TYPE
                items.append(
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "mimeType": f["mimeType"],
                        "size": int(f.get("size", 0)),
                        "path": rel_path,
                        "is_folder": is_folder,
                    }
                )
                if is_folder and recursive:
                    items.extend(_list_one(f["id"], f"{rel_path}/"))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    return _list_one(folder_id, "")


# =============================================================================
# File(Resource) — single file resource.
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
            metadata = update_file(file_id, path, public=public if public else None)
        else:
            logger.info(f"Uploading: {path.name}")
            metadata = upload_file(path, public=public)

        create_tracking_file(path, metadata)
        logger.info(f"View: {metadata.get('webViewLink', '')}")


def _safe_name(name: str) -> str:
    """Sanitize a file/folder name for use as a local path component."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", name)
    return re.sub(r"\s+", "_", safe)


# =============================================================================
# Folder — collection manager for Drive folders (checkout/pull).
# =============================================================================


class Folder:
    """Google Drive folder — checkout/pull a folder tree.

    Not a Resource subclass (collection manager, like Mailbox and Sheet).
    Dispatches to native gax resources for Google Workspace files.
    """

    name = "folder"

    def checkout(
        self,
        url: str,
        output: Path | None = None,
        *,
        recursive: bool = False,
    ) -> Path:
        """Checkout a Drive folder to a local directory. Returns path created.

        Downloads all files. Google Workspace files (Docs, Sheets, Forms)
        are cloned via their native gax resource.
        """
        folder_id = extract_folder_id(url)
        meta = get_folder_metadata(folder_id)
        title = meta["name"]

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_name(title)}.drive.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata
        metadata = {
            "type": "gax/drive-checkout",
            "folder_id": folder_id,
            "url": f"https://drive.google.com/drive/folders/{folder_id}",
            "title": title,
            "recursive": recursive,
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

        # List and download files
        items = list_folder(folder_id, recursive=recursive)

        cloned = 0
        skipped = 0

        for item in items:
            if item["is_folder"]:
                # Create local subdirectory
                (folder / item["path"]).mkdir(parents=True, exist_ok=True)
                continue

            local_path = folder / item["path"]
            mime = item["mimeType"]

            # Skip if already exists
            if local_path.exists() or (
                mime in WORKSPACE_MIME_TYPES and _workspace_file_exists(folder, item)
            ):
                skipped += 1
                continue

            # Ensure parent dir exists
            local_path.parent.mkdir(parents=True, exist_ok=True)

            logger.info(f"Cloning: {item['path']}")

            if mime in WORKSPACE_MIME_TYPES:
                self._clone_workspace_file(item, folder)
            else:
                download_file(item["id"], local_path)
                create_tracking_file(
                    local_path,
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "mimeType": mime,
                        "size": item["size"],
                    },
                )

            cloned += 1

        logger.info(f"Cloned: {cloned}, Skipped: {skipped}")
        return folder

    def pull(self, path: Path) -> None:
        """Pull latest files for a checkout folder.

        Re-lists the remote folder and downloads new/updated files.
        Existing files are refreshed via their sidecar.
        """
        metadata_path = path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {path}")

        meta = yaml.safe_load(metadata_path.read_text())
        folder_id = meta.get("folder_id")
        if not folder_id:
            raise ValueError("No folder_id in .gax.yaml")

        recursive = meta.get("recursive", False)
        remote_items = list_folder(folder_id, recursive=recursive)

        # Build set of remote file IDs (non-folders)
        remote_by_path = {
            item["path"]: item for item in remote_items if not item["is_folder"]
        }

        # Pull existing tracked files
        updated = 0
        for sidecar in path.rglob("*.gax.md"):
            actual = sidecar.parent / sidecar.name[:-7]  # strip .gax.md
            if actual.exists():
                try:
                    File().pull(actual)
                    updated += 1
                except Exception as e:
                    logger.warning(f"{actual}: {e}")

        # Download new remote files
        new_files = 0
        for rel_path, item in remote_by_path.items():
            local_path = path / rel_path
            mime = item["mimeType"]

            if local_path.exists():
                continue
            if mime in WORKSPACE_MIME_TYPES and _workspace_file_exists(path, item):
                continue

            local_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"New file: {rel_path}")

            if mime in WORKSPACE_MIME_TYPES:
                self._clone_workspace_file(item, path)
            else:
                download_file(item["id"], local_path)
                create_tracking_file(
                    local_path,
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "mimeType": mime,
                        "size": item["size"],
                    },
                )

            new_files += 1

        # Ensure subfolders exist
        for item in remote_items:
            if item["is_folder"]:
                (path / item["path"]).mkdir(parents=True, exist_ok=True)

        # Update metadata timestamp
        meta["checked_out"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(metadata_path, "w") as f:
            yaml.dump(
                meta,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Report remotely deleted files
        local_sidecars = set()
        for sidecar in path.rglob("*.gax.md"):
            rel = sidecar.parent / sidecar.name[:-7]  # strip .gax.md
            try:
                local_sidecars.add(str(rel.relative_to(path)))
            except ValueError:
                continue
        remote_paths = set(remote_by_path.keys())
        deleted = local_sidecars - remote_paths
        for d in sorted(deleted):
            logger.info(f"Deleted remotely: {d}")

        logger.info(
            f"Updated: {updated}, New: {new_files}, Deleted remotely: {len(deleted)}"
        )

    def _clone_workspace_file(self, item: dict, folder: Path) -> None:
        """Clone a Google Workspace file using its native gax resource."""
        mime = item["mimeType"]
        resource_type = WORKSPACE_MIME_TYPES[mime]
        file_id = item["id"]
        parent = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else ""
        target_dir = folder / parent if parent else folder

        url = f"https://docs.google.com/{_workspace_url_path(resource_type)}/d/{file_id}/edit"
        safe_name = _safe_name(item["name"])

        if resource_type == "doc":
            from .gdoc.doc import Tab

            output = target_dir / f"{safe_name}.doc.gax.md"
            if not output.exists():
                Tab().clone(url, output=output)
        elif resource_type == "sheet":
            from .gsheet.sheet import SheetTab

            output = target_dir / f"{safe_name}.sheet.gax.md"
            if not output.exists():
                SheetTab().clone(url, output=output)
        elif resource_type == "form":
            from .form import Form

            output = target_dir / f"{safe_name}.form.gax.md"
            if not output.exists():
                Form().clone(url, output=output)
        elif resource_type == "slides":
            from .gslides import Presentation

            output = target_dir / f"{safe_name}.slides.gax.md.d"
            if not output.exists():
                Presentation().clone(url, output=output)


def _workspace_url_path(resource_type: str) -> str:
    """Map resource type to Google URL path segment."""
    return {
        "doc": "document",
        "sheet": "spreadsheets",
        "form": "forms",
        "slides": "presentation",
    }[resource_type]


def _workspace_file_exists(folder: Path, item: dict) -> bool:
    """Check if a Workspace file was already cloned as a .gax.md file."""
    mime = item["mimeType"]
    resource_type = WORKSPACE_MIME_TYPES.get(mime, "")
    safe_name = _safe_name(item["name"])
    parent = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else ""
    target_dir = folder / parent if parent else folder

    ext_map = {
        "doc": ".doc.gax.md",
        "sheet": ".sheet.gax.md",
        "form": ".form.gax.md",
        "slides": ".slides.gax.md.d",
    }
    ext = ext_map.get(resource_type, "")
    return (target_dir / f"{safe_name}{ext}").exists() if ext else False
