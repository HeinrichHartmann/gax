"""Gmail sync for gax.

Package structure:
  shared   -- dataclasses, format helpers, Gmail API helpers
  thread   -- Thread(Resource) class
  mailbox  -- Mailbox class
  _legacy  -- CLI commands (being moved to cli.py)
"""

from .shared import (  # noqa: F401 — public API
    Attachment as Attachment,
    Message as Message,
    MailSection as MailSection,
    format_section as format_section,
    format_multipart as format_multipart,
    extract_thread_id as extract_thread_id,
    pull_thread as pull_thread,
    _mail_section_to_multipart as _mail_section_to_multipart,
    _get_header as _get_header,
)
from .thread import Thread as Thread  # noqa: F401
from .mailbox import (  # noqa: F401
    Mailbox as Mailbox,
    _parse_gax_header as _parse_gax_header,
    _relabel_fetch_threads as _relabel_fetch_threads,
    _write_gax_file as _write_gax_file,
)
from ._legacy import *  # noqa: F401,F403
from ._legacy import (  # noqa: F401 — CLI groups for cli.py registration
    thread as thread,
    mailbox as mailbox,
)
