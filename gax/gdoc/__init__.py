"""Google Docs sync for gax.

Re-exports from doc.py. CLI commands live in cli.py.
"""

from .doc import (  # noqa: F401
    DocSection,
    Comment,
    CommentReply,
    format_section,
    format_multipart,
    parse_multipart,
    extract_doc_id,
    pull_doc,
    pull_single_tab,
    get_tabs_list,
    create_tab_with_content,
    update_tab_content,
    _add_comments_to_sections,
    Tab,
    Doc,
)

from . import native_md  # noqa: F401
