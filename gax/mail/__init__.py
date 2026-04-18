"""Gmail sync for gax.

Package structure:
  _legacy  -- original monolith (being split into shared/thread/mailbox)
"""

from ._legacy import *  # noqa: F401,F403
from ._legacy import (  # noqa: F401 — explicit re-exports for cli.py
    thread as thread,
    mailbox as mailbox,
    pull_thread as pull_thread,
    format_multipart as format_multipart,
    format_section as format_section,
    extract_thread_id as extract_thread_id,
    _mail_section_to_multipart as _mail_section_to_multipart,
    _parse_gax_header as _parse_gax_header,
    _relabel_fetch_threads as _relabel_fetch_threads,
    _write_gax_file as _write_gax_file,
)
