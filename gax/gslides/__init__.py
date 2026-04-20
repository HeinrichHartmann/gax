"""Google Slides operations for gax.

Re-exports from gslides.py.
"""

from .gslides import (  # noqa: F401
    extract_presentation_id,
    _extract_slide_markdown,
    _extract_speaker_notes,
    _get_slide_title,
    _slide_to_content,
    _safe_filename,
    _get_presentation,
    _get_placeholder_type,
    _extract_text_from_elements,
    _get_slide_layout,
    _slide_headers,
    _format_slide_file,
    _parse_slide_file,
    Slide,
    Presentation,
)
