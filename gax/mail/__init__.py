"""Gmail sync for gax.

Package structure:
  shared   -- dataclasses, format helpers, Gmail API helpers
  thread   -- Thread(Resource) class
  mailbox  -- Mailbox class
"""

from .shared import (  # noqa: F401 — public API
    Attachment as Attachment,
    Message as Message,
    MailSection as MailSection,
    format_section as format_section,
    format_multipart as format_multipart,
    extract_thread_id as extract_thread_id,
    pull_thread as pull_thread,
)
from .thread import Thread as Thread  # noqa: F401
from .mailbox import Mailbox as Mailbox  # noqa: F401
