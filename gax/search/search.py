"""Search functions across Google Workspace resources."""

import json

from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials


DRIVE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink"


def search_drive(query: str, limit: int = 50) -> list[dict]:
    """Search Google Drive files. Returns list of file metadata dicts."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    results = []
    page_token = None
    while len(results) < limit:
        resp = (
            service.files()
            .list(
                q=query,
                fields=f"nextPageToken,files({DRIVE_FIELDS})",
                pageSize=min(limit - len(results), 100),
                pageToken=page_token,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results[:limit]


def format_table(results: list[dict]) -> str:
    """Format results as a plain text table."""
    if not results:
        return "(no results)"
    lines = []
    for f in results:
        name = f.get("name", "")
        mime = f.get("mimeType", "").replace("application/vnd.google-apps.", "")
        modified = f.get("modifiedTime", "")[:10]
        url = f.get("webViewLink", f"https://drive.google.com/file/d/{f['id']}/view")
        lines.append(f"{modified}  {mime:<20s}  {name:<40s}  {url}")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    return json.dumps(results, indent=2)


def format_jsonl(results: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in results)
