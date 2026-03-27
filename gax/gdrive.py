"""Google Drive file operations for gax.

Handles upload/download of arbitrary files to/from Google Drive.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .auth import get_authenticated_credentials


def extract_file_id(url_or_id: str) -> str:
    """Extract file ID from Google Drive URL or return as-is if already an ID."""
    # Match various Drive URL formats
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'^([a-zA-Z0-9_-]+)$',  # Just an ID
    ]

    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)

    raise ValueError(f"Cannot extract file ID from: {url_or_id}")


def download_file(file_id: str, output_path: Path) -> dict:
    """Download a file from Google Drive.

    Returns metadata dict with file info.
    """
    creds = get_authenticated_credentials()
    service = build('drive', 'v3', credentials=creds)

    # Get file metadata
    file_metadata = service.files().get(
        fileId=file_id,
        fields='id,name,mimeType,size,webViewLink,webContentLink'
    ).execute()

    # Download file content
    request = service.files().get_media(fileId=file_id)

    with open(output_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return file_metadata


def upload_file(
    file_path: Path,
    name: Optional[str] = None,
    parent_folder_id: Optional[str] = None,
    public: bool = False
) -> dict:
    """Upload a file to Google Drive.

    Args:
        file_path: Local file to upload
        name: Name for the file in Drive (defaults to filename)
        parent_folder_id: ID of parent folder (defaults to root)
        public: Make file publicly accessible

    Returns:
        File metadata dict
    """
    creds = get_authenticated_credentials()
    service = build('drive', 'v3', credentials=creds)

    file_name = name or file_path.name

    # Prepare metadata
    file_metadata = {'name': file_name}
    if parent_folder_id:
        file_metadata['parents'] = [parent_folder_id]

    # Upload file
    media = MediaFileUpload(str(file_path), resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id,name,mimeType,size,webViewLink,webContentLink'
    ).execute()

    # Set public permissions if requested
    if public:
        set_public(file['id'], True)
        # Re-fetch to get webContentLink after making public
        file = service.files().get(
            fileId=file['id'],
            fields='id,name,mimeType,size,webViewLink,webContentLink'
        ).execute()

    return file


def update_file(file_id: str, file_path: Path, public: Optional[bool] = None) -> dict:
    """Update an existing file on Google Drive.

    Args:
        file_id: Drive file ID to update
        file_path: Local file with new content
        public: If set, update public sharing status

    Returns:
        Updated file metadata dict
    """
    creds = get_authenticated_credentials()
    service = build('drive', 'v3', credentials=creds)

    # Update file content
    media = MediaFileUpload(str(file_path), resumable=True)
    file = service.files().update(
        fileId=file_id,
        media_body=media,
        fields='id,name,mimeType,size,webViewLink,webContentLink'
    ).execute()

    # Update sharing if specified
    if public is not None:
        set_public(file_id, public)
        # Re-fetch to get updated webContentLink
        file = service.files().get(
            fileId=file_id,
            fields='id,name,mimeType,size,webViewLink,webContentLink'
        ).execute()

    return file


def set_public(file_id: str, public: bool = True):
    """Make a file public or private.

    Args:
        file_id: Drive file ID
        public: True to make public, False to make private
    """
    creds = get_authenticated_credentials()
    service = build('drive', 'v3', credentials=creds)

    if public:
        # Add public reader permission
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        service.permissions().create(
            fileId=file_id,
            body=permission
        ).execute()
    else:
        # Remove public permissions
        permissions = service.permissions().list(fileId=file_id).execute()
        for perm in permissions.get('permissions', []):
            if perm.get('type') == 'anyone':
                service.permissions().delete(
                    fileId=file_id,
                    permissionId=perm['id']
                ).execute()


def create_tracking_file(file_path: Path, metadata: dict) -> Path:
    """Create a .gax tracking file for a downloaded file.

    Args:
        file_path: Path to the downloaded file
        metadata: File metadata from Drive API

    Returns:
        Path to the tracking file
    """
    tracking_path = file_path.with_suffix(file_path.suffix + '.gax')

    tracking_data = {
        'type': 'gax/file',
        'file_id': metadata['id'],
        'name': metadata['name'],
        'mime_type': metadata.get('mimeType', ''),
        'source': metadata.get('webViewLink', f"https://drive.google.com/file/d/{metadata['id']}/view"),
        'pulled': datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        'size': int(metadata.get('size', 0)),
    }

    # Add download link if available (for public files)
    if metadata.get('webContentLink'):
        tracking_data['download'] = metadata['webContentLink']

    with open(tracking_path, 'w') as f:
        yaml.dump(tracking_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return tracking_path


def read_tracking_file(tracking_path: Path) -> dict:
    """Read a .gax tracking file.

    Returns:
        Tracking data dict
    """
    with open(tracking_path, 'r') as f:
        return yaml.safe_load(f)


# =============================================================================
# CLI commands
# =============================================================================

@click.group()
def file():
    """Google Drive file operations"""
    pass


@file.command()
@click.argument('url_or_id')
@click.option('-o', '--output', type=click.Path(path_type=Path), help='Output file path')
def clone(url_or_id: str, output: Optional[Path]):
    """Clone a file from Google Drive.

    Downloads the file and creates a tracking .gax file.
    """
    try:
        file_id = extract_file_id(url_or_id)

        click.echo(f"Fetching file: {file_id}")

        # Get metadata first to determine filename
        creds = get_authenticated_credentials()
        service = build('drive', 'v3', credentials=creds)
        metadata = service.files().get(
            fileId=file_id,
            fields='id,name,mimeType,size,webViewLink,webContentLink'
        ).execute()

        # Determine output path
        if output:
            file_path = output
        else:
            file_path = Path(metadata['name'])

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        # Download file
        click.echo(f"Downloading to: {file_path}")
        metadata = download_file(file_id, file_path)

        # Create tracking file
        tracking_path = create_tracking_file(file_path, metadata)

        click.echo(f"Created: {file_path}")
        click.echo(f"Tracking: {tracking_path}")
        click.echo(f"Size: {metadata.get('size', 'unknown')} bytes")
        if metadata.get('webContentLink'):
            click.echo(f"Public download: {metadata.get('webContentLink')}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@file.command()
@click.argument('file_path', type=click.Path(exists=True, path_type=Path))
def pull(file_path: Path):
    """Pull latest version of a file from Google Drive.

    Requires a .gax tracking file.
    """
    try:
        # Find tracking file
        tracking_path = file_path.with_suffix(file_path.suffix + '.gax')
        if not tracking_path.exists():
            click.echo(f"Error: No tracking file found: {tracking_path}", err=True)
            click.echo("Use 'gax file clone' to download a tracked file.", err=True)
            sys.exit(1)

        # Read tracking data
        tracking_data = read_tracking_file(tracking_path)
        file_id = tracking_data.get('file_id')

        if not file_id:
            click.echo("Error: No file_id in tracking file", err=True)
            sys.exit(1)

        click.echo(f"Pulling: {file_path}")

        # Download updated file
        metadata = download_file(file_id, file_path)

        # Update tracking file
        create_tracking_file(file_path, metadata)

        click.echo(f"Updated: {file_path}")
        click.echo(f"Size: {metadata.get('size', 'unknown')} bytes")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@file.command()
@click.argument('file_path', type=click.Path(exists=True, path_type=Path))
@click.option('--public', is_flag=True, help='Make file publicly accessible')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation')
def push(file_path: Path, public: bool, yes: bool):
    """Push local file to Google Drive.

    If file has a .gax tracking file, updates existing file.
    Otherwise, uploads as a new file.
    """
    try:
        # Check for tracking file
        tracking_path = file_path.with_suffix(file_path.suffix + '.gax')

        if tracking_path.exists():
            # Update existing file
            tracking_data = read_tracking_file(tracking_path)
            file_id = tracking_data.get('file_id')

            if not file_id:
                click.echo("Error: No file_id in tracking file", err=True)
                sys.exit(1)

            # Show what will be updated
            click.echo(f"Will update Drive file: {tracking_data.get('name')}")
            click.echo(f"File ID: {file_id}")
            click.echo(f"Local file: {file_path}")
            if public:
                click.echo("Will make publicly accessible")

            if not yes:
                if not click.confirm("\nPush these changes?"):
                    click.echo("Aborted.")
                    return

            click.echo("Pushing changes...")
            metadata = update_file(file_id, file_path, public=public if public else None)

            # Update tracking file
            create_tracking_file(file_path, metadata)

            click.echo(f"Updated: {metadata['name']}")
            click.echo(f"View: {metadata.get('webViewLink')}")
            if metadata.get('webContentLink'):
                click.echo(f"Download: {metadata.get('webContentLink')}")

        else:
            # Upload new file
            click.echo(f"Will upload new file: {file_path.name}")
            if public:
                click.echo("Will make publicly accessible")

            if not yes:
                if not click.confirm("\nUpload this file?"):
                    click.echo("Aborted.")
                    return

            click.echo("Uploading...")
            metadata = upload_file(file_path, public=public)

            # Create tracking file
            tracking_path = create_tracking_file(file_path, metadata)

            click.echo(f"Uploaded: {metadata['name']}")
            click.echo(f"Tracking: {tracking_path}")
            click.echo(f"View: {metadata.get('webViewLink')}")
            if metadata.get('webContentLink'):
                click.echo(f"Download: {metadata.get('webContentLink')}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
