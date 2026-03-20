"""Content-addressable storage for attachments.

Stores binary files in ~/.gax/store/ with deduplication via SHA-256 hashing.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default store location
STORE_DIR = Path.home() / ".gax" / "store"


def get_store_dir() -> Path:
    """Get or create store directory."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / "blob").mkdir(exist_ok=True)
    (STORE_DIR / "meta").mkdir(exist_ok=True)
    (STORE_DIR / "ref").mkdir(exist_ok=True)
    return STORE_DIR


def compute_hash(data: bytes) -> str:
    """Compute SHA-256 hash of data."""
    return f"sha256-{hashlib.sha256(data).hexdigest()}"


def store_blob(
    data: bytes,
    original_name: str,
    mime_type: str = "application/octet-stream",
    source_message_id: Optional[str] = None,
) -> str:
    """
    Store binary data in CAS.

    Returns file:// URL to the stored blob.
    """
    store_dir = get_store_dir()
    content_hash = compute_hash(data)

    blob_path = store_dir / "blob" / content_hash
    meta_path = store_dir / "meta" / f"{content_hash}.json"

    # Write blob if not already present (deduplication)
    if not blob_path.exists():
        blob_path.write_bytes(data)

    # Write/update metadata
    metadata = {
        "hash": content_hash,
        "size": len(data),
        "mime_type": mime_type,
        "original_name": original_name,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    if source_message_id:
        metadata["source_message_id"] = source_message_id

    meta_path.write_text(json.dumps(metadata, indent=2))

    # Create/update named reference symlink
    ref_path = store_dir / "ref" / original_name
    if ref_path.is_symlink():
        ref_path.unlink()
    elif ref_path.exists():
        # Non-symlink file exists, add hash suffix to avoid collision
        ref_path = store_dir / "ref" / f"{original_name}.{content_hash[:8]}"

    try:
        ref_path.symlink_to(f"../blob/{content_hash}")
    except OSError:
        pass  # Symlink creation may fail on some systems

    return f"file://{blob_path}"


def get_blob(content_hash: str) -> Optional[bytes]:
    """Retrieve blob by hash."""
    blob_path = get_store_dir() / "blob" / content_hash
    if blob_path.exists():
        return blob_path.read_bytes()
    return None


def get_metadata(content_hash: str) -> Optional[dict]:
    """Retrieve metadata by hash."""
    meta_path = get_store_dir() / "meta" / f"{content_hash}.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return None
