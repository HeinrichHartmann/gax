"""Pytest configuration for Tree IR prototype integration tests."""

import os
import uuid

import pytest
from googleapiclient.discovery import build

from gax.auth import get_authenticated_credentials, is_authenticated


E2E_PREFIX = "gaxe2e_tree"


@pytest.fixture(scope="session")
def docs_service():
    """Authenticated Google Docs API service."""
    if not is_authenticated():
        pytest.skip("Not authenticated. Run 'gax auth login' first.")
    creds = get_authenticated_credentials()
    return build("docs", "v1", credentials=creds)


@pytest.fixture(scope="session")
def drive_service():
    """Authenticated Google Drive API service."""
    creds = get_authenticated_credentials()
    return build("drive", "v3", credentials=creds)


@pytest.fixture(scope="session")
def test_doc_id():
    """Get test doc ID from environment."""
    doc_id = os.environ.get("GAX_TEST_DOC")
    if not doc_id:
        pytest.skip("GAX_TEST_DOC not set")
    return doc_id


@pytest.fixture
def scratch_doc(docs_service, drive_service):
    """Create a scratch Google Doc for testing, clean up after.

    Returns (doc_id, doc) tuple.
    """
    uid = uuid.uuid4().hex[:8]
    title = f"{E2E_PREFIX}_{uid}"

    # Create doc via Drive API
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    file = drive_service.files().create(body=file_metadata, fields="id").execute()
    doc_id = file["id"]

    yield doc_id

    # Cleanup: delete the doc
    try:
        drive_service.files().delete(fileId=doc_id).execute()
    except Exception as e:
        print(f"Warning: Could not delete scratch doc {doc_id}: {e}")


def populate_rich_doc(docs_service, doc_id: str) -> dict:
    """Populate a scratch doc with rich formatting for testing.

    Creates:
    - A heading
    - A paragraph with mixed runs (plain + bold + colored + link)
    - A centered small-font paragraph
    - A bulleted list
    - A table with a styled cell
    - An emoji-containing paragraph (UTF-16 stress)

    Returns the document JSON after population.
    """
    requests = []

    # Build content from bottom to top (inserting at index 1 each time)
    # Actually, let's insert all text first, then style it.

    # Text content (inserted at index 1, grows the document)
    text_blocks = [
        "Test Heading\n",
        "This is plain text with bold word and colored link here see more.\n",
        "Confidential — internal only\n",
        "Revenue increased significantly\n",
        "Costs remained flat\n",
        "Market share grew\n",
        "Extra data here\n",  # placeholder for table (we'll replace)
        "Emoji test: 🎉 party 🚀 rocket 🏳️\u200d🌈 flag 𝕳𝖊𝖑𝖑𝖔\n",
    ]

    full_text = "".join(text_blocks)
    requests.append({
        "insertText": {
            "text": full_text,
            "location": {"index": 1},
        }
    })

    # Now apply styles. Calculate offsets:
    idx = 1  # start after the implicit newline at index 0

    # Block 1: "Test Heading\n" → heading
    h_start = idx
    h_end = idx + len("Test Heading")
    requests.append({
        "updateParagraphStyle": {
            "range": {"startIndex": h_start, "endIndex": h_end + 1},
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
            "fields": "namedStyleType",
        }
    })
    idx += len("Test Heading\n")

    # Block 2: "This is plain text with bold word and colored link here see more.\n"
    p2_start = idx
    # Make "bold word" bold
    bold_offset = "This is plain text with ".count("")  # 24 chars
    bold_start = p2_start + len("This is plain text with ")
    bold_end = bold_start + len("bold word")
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": bold_start, "endIndex": bold_end},
            "textStyle": {"bold": True},
            "fields": "bold",
        }
    })
    # Make "colored" red
    colored_start = p2_start + len("This is plain text with bold word and ")
    colored_end = colored_start + len("colored")
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": colored_start, "endIndex": colored_end},
            "textStyle": {
                "foregroundColor": {"color": {"rgbColor": {"red": 0.8, "green": 0.0, "blue": 0.0}}}
            },
            "fields": "foregroundColor",
        }
    })
    # Make "link here" a link
    link_start = p2_start + len("This is plain text with bold word and colored ")
    link_end = link_start + len("link here")
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": link_start, "endIndex": link_end},
            "textStyle": {"link": {"url": "https://example.com"}},
            "fields": "link",
        }
    })
    idx += len("This is plain text with bold word and colored link here see more.\n")

    # Block 3: "Confidential — internal only\n" → centered, small font
    p3_start = idx
    p3_end = idx + len("Confidential — internal only")
    requests.append({
        "updateParagraphStyle": {
            "range": {"startIndex": p3_start, "endIndex": p3_end + 1},
            "paragraphStyle": {"alignment": "CENTER"},
            "fields": "alignment",
        }
    })
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": p3_start, "endIndex": p3_end},
            "textStyle": {"fontSize": {"magnitude": 9, "unit": "PT"}},
            "fields": "fontSize",
        }
    })
    idx += len("Confidential — internal only\n")

    # Blocks 4-6: list items
    list_items_text = [
        "Revenue increased significantly\n",
        "Costs remained flat\n",
        "Market share grew\n",
    ]
    list_start = idx
    list_end = idx
    for item in list_items_text:
        list_end += len(item)
    requests.append({
        "createParagraphBullets": {
            "range": {"startIndex": list_start, "endIndex": list_end},
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
        }
    })
    idx = list_end

    # Block 7: "Extra data here\n" → will be replaced with a table later
    table_placeholder_start = idx
    table_placeholder_end = idx + len("Extra data here\n")
    idx = table_placeholder_end

    # Block 8: emoji paragraph (no special styling needed, UTF-16 stress test)
    # idx already past it

    # Apply all requests so far
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()

    # Now delete the placeholder and insert a table
    # Re-fetch to get current state
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

    # Find the placeholder paragraph
    tab = doc.get("tabs", [{}])[0]
    body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])

    # Find "Extra data here" paragraph
    table_insert_idx = None
    table_delete_start = None
    table_delete_end = None
    for elem in body_content:
        if "paragraph" in elem:
            para_text = ""
            for el in elem["paragraph"].get("elements", []):
                tr = el.get("textRun")
                if tr:
                    para_text += tr["content"]
            if "Extra data here" in para_text:
                table_delete_start = elem["startIndex"]
                table_delete_end = elem["endIndex"]
                table_insert_idx = elem["startIndex"]
                break

    if table_insert_idx is not None:
        table_requests = []
        # Delete placeholder
        table_requests.append({
            "deleteContentRange": {
                "range": {"startIndex": table_delete_start, "endIndex": table_delete_end}
            }
        })
        # Insert table
        table_requests.append({
            "insertTable": {
                "rows": 2,
                "columns": 2,
                "location": {"index": table_insert_idx},
            }
        })
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": table_requests},
        ).execute()

        # Re-fetch and populate table cells
        doc = docs_service.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        tab = doc.get("tabs", [{}])[0]
        body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])

        # Find the table
        for elem in body_content:
            if "table" in elem:
                table_data = elem["table"]
                rows = table_data.get("tableRows", [])
                cell_texts = [
                    ["Region", "Revenue"],
                    ["EMEA", "4.2M"],
                ]
                cell_requests = []
                for ri, row in enumerate(rows):
                    cells = row.get("tableCells", [])
                    for ci, cell in enumerate(cells):
                        content = cell.get("content", [])
                        if content:
                            para = content[0]
                            if "paragraph" in para:
                                cell_start = para["paragraph"].get("elements", [{}])[0].get("startIndex")
                                if cell_start is not None and ri < len(cell_texts) and ci < len(cell_texts[ri]):
                                    cell_requests.append({
                                        "insertText": {
                                            "text": cell_texts[ri][ci],
                                            "location": {"index": cell_start},
                                        }
                                    })
                # Apply cell text in reverse order
                cell_requests.reverse()
                if cell_requests:
                    docs_service.documents().batchUpdate(
                        documentId=doc_id,
                        body={"requests": cell_requests},
                    ).execute()

                # Style "4.2M" as bold in cell [1][1]
                doc = docs_service.documents().get(
                    documentId=doc_id, includeTabsContent=True
                ).execute()
                tab = doc.get("tabs", [{}])[0]
                body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
                for elem2 in body_content:
                    if "table" in elem2:
                        rows2 = elem2["table"].get("tableRows", [])
                        if len(rows2) > 1:
                            cells2 = rows2[1].get("tableCells", [])
                            if len(cells2) > 1:
                                content2 = cells2[1].get("content", [])
                                if content2 and "paragraph" in content2[0]:
                                    elems = content2[0]["paragraph"].get("elements", [])
                                    if elems:
                                        tr = elems[0].get("textRun")
                                        if tr:
                                            s = elems[0].get("startIndex")
                                            e = s + len("4.2M") if s else None
                                            if s and e:
                                                docs_service.documents().batchUpdate(
                                                    documentId=doc_id,
                                                    body={"requests": [{
                                                        "updateTextStyle": {
                                                            "range": {"startIndex": s, "endIndex": e},
                                                            "textStyle": {"bold": True},
                                                            "fields": "bold",
                                                        }
                                                    }]},
                                                ).execute()
                break

    # Final fetch
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    return doc
