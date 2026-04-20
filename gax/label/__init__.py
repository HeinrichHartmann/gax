"""Gmail label management for gax.

Re-exports from label.py.
"""

from .label import (  # noqa: F401
    LABEL_LIST_VISIBILITY,
    LABEL_LIST_VISIBILITY_REV,
    MESSAGE_LIST_VISIBILITY,
    SYSTEM_LABELS,
    LabelHeader,
    parse_labels_file,
    format_labels_file,
    get_service,
    fetch_labels,
    api_to_label,
    label_to_api_body,
    needs_update,
    compute_changes,
    format_diff_summary,
    Label,
)
