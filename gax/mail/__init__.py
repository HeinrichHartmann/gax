"""Gmail sync for gax.

Package structure:
  shared   -- dataclasses, format helpers, Gmail API helpers
  thread   -- Thread(Resource) class
  mailbox  -- Mailbox class
  draft    -- Draft(Resource) class
"""

from .thread import Thread as Thread  # noqa: F401
from .mailbox import Mailbox as Mailbox  # noqa: F401
from .draft import Draft as Draft  # noqa: F401
