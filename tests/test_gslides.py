"""Tests for Google Slides support.

Uses mock service objects to test without hitting real Google APIs.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gax.gslides import Presentation, Slide
from gax.gslides.gslides import (
    _extract_slide_markdown,
    _extract_speaker_notes,
    _get_slide_title,
    _slide_to_content,
    extract_presentation_id,
)


# =============================================================================
# Fixtures
# =============================================================================

SAMPLE_SLIDE = {
    "objectId": "slide_001",
    "pageElements": [
        {
            "objectId": "title_001",
            "shape": {
                "placeholder": {"type": "TITLE"},
                "text": {
                    "textElements": [
                        {"textRun": {"content": "Welcome\n"}},
                    ]
                },
            },
        },
        {
            "objectId": "subtitle_001",
            "shape": {
                "placeholder": {"type": "SUBTITLE"},
                "text": {
                    "textElements": [
                        {"textRun": {"content": "A presentation\n"}},
                    ]
                },
            },
        },
        {
            "objectId": "body_001",
            "shape": {
                "placeholder": {"type": "BODY"},
                "text": {
                    "textElements": [
                        {"textRun": {"content": "First point\n"}},
                        {"textRun": {"content": "Second point\n"}},
                    ]
                },
            },
        },
    ],
    "slideProperties": {
        "layoutObjectId": "TITLE_AND_BODY",
        "notesPage": {
            "pageElements": [
                {
                    "shape": {
                        "placeholder": {"type": "BODY"},
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Speaker note here\n"}},
                            ]
                        },
                    }
                }
            ]
        },
    },
}

SAMPLE_SLIDE_EMPTY = {
    "objectId": "slide_002",
    "pageElements": [],
    "slideProperties": {"layoutObjectId": "BLANK"},
}

SAMPLE_PRESENTATION = {
    "presentationId": "pres_abc123",
    "title": "Test Presentation",
    "slides": [SAMPLE_SLIDE, SAMPLE_SLIDE_EMPTY],
}


# =============================================================================
# extract_presentation_id
# =============================================================================


class TestExtractPresentationId:
    def test_full_url(self):
        url = "https://docs.google.com/presentation/d/abc123xyz/edit"
        assert extract_presentation_id(url) == "abc123xyz"

    def test_url_with_slide(self):
        url = "https://docs.google.com/presentation/d/abc123xyz/edit#slide=id.g123"
        assert extract_presentation_id(url) == "abc123xyz"

    def test_bare_id(self):
        assert extract_presentation_id("abc123xyz") == "abc123xyz"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot extract"):
            extract_presentation_id("https://example.com/not-a-slide")


# =============================================================================
# Text extraction
# =============================================================================


class TestExtractSlideMarkdown:
    def test_title_subtitle_body(self):
        md = _extract_slide_markdown(SAMPLE_SLIDE)
        assert "# Welcome" in md
        assert "## A presentation" in md
        assert "First point" in md
        assert "Second point" in md

    def test_empty_slide(self):
        md = _extract_slide_markdown(SAMPLE_SLIDE_EMPTY)
        assert md == ""

    def test_title_only(self):
        slide = {
            "objectId": "s1",
            "pageElements": [
                {
                    "shape": {
                        "placeholder": {"type": "CENTERED_TITLE"},
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Big Title\n"}},
                            ]
                        },
                    }
                }
            ],
        }
        md = _extract_slide_markdown(slide)
        assert "# Big Title" in md


class TestExtractSpeakerNotes:
    def test_with_notes(self):
        notes = _extract_speaker_notes(SAMPLE_SLIDE)
        assert notes == "Speaker note here"

    def test_without_notes(self):
        notes = _extract_speaker_notes(SAMPLE_SLIDE_EMPTY)
        assert notes == ""


class TestGetSlideTitle:
    def test_with_title(self):
        assert _get_slide_title(SAMPLE_SLIDE) == "Welcome"

    def test_without_title(self):
        assert _get_slide_title(SAMPLE_SLIDE_EMPTY) == "Untitled"


class TestSlideToContent:
    def test_markdown_format(self):
        content = _slide_to_content(SAMPLE_SLIDE, "md")
        assert "# Welcome" in content
        assert "```notes" in content
        assert "Speaker note here" in content

    def test_json_format(self):
        content = _slide_to_content(SAMPLE_SLIDE, "json")
        data = json.loads(content)
        assert data["objectId"] == "slide_001"

    def test_markdown_no_notes(self):
        content = _slide_to_content(SAMPLE_SLIDE_EMPTY, "md")
        assert "```notes" not in content


# =============================================================================
# Presentation.clone (checkout)
# =============================================================================


class TestPresentationClone:
    @patch("gax.gslides.gslides._get_presentation")
    def test_checkout_creates_directory(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        result = Presentation(url="pres_abc123").clone(output=output)

        assert result == output
        assert output.exists()
        assert (output / ".gax.yaml").exists()

    @patch("gax.gslides.gslides._get_presentation")
    def test_checkout_writes_gax_yaml(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output)

        meta = yaml.safe_load((output / ".gax.yaml").read_text())
        assert meta["type"] == "gax/slides-checkout"
        assert meta["presentation_id"] == "pres_abc123"
        assert meta["title"] == "Test Presentation"
        assert meta["format"] == "md"

    @patch("gax.gslides.gslides._get_presentation")
    def test_checkout_creates_slide_files(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output)

        slide_files = sorted(output.glob("*.slides.gax.md"))
        assert len(slide_files) == 2
        assert slide_files[0].name.startswith("00_")
        assert slide_files[1].name.startswith("01_")

    @patch("gax.gslides.gslides._get_presentation")
    def test_checkout_json_format(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output, fmt="json")

        meta = yaml.safe_load((output / ".gax.yaml").read_text())
        assert meta["format"] == "json"

        # First slide should contain JSON
        slide_file = sorted(output.glob("*.slides.gax.md"))[0]
        content = slide_file.read_text()
        assert '"objectId"' in content

    @patch("gax.gslides.gslides._get_presentation")
    def test_checkout_markdown_has_content(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output)

        slide_file = sorted(output.glob("*.slides.gax.md"))[0]
        content = slide_file.read_text()
        assert "type: gax/slides" in content
        assert "# Welcome" in content


# =============================================================================
# Presentation.pull
# =============================================================================


class TestPresentationPull:
    @patch("gax.gslides.gslides._get_presentation")
    def test_pull_updates_files(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        # First checkout
        Presentation(url="pres_abc123").clone(output=output)

        # Modify presentation title
        modified = {**SAMPLE_PRESENTATION, "title": "Updated Title"}
        mock_get.return_value = modified

        # Pull
        Presentation(path=output).pull()

        meta = yaml.safe_load((output / ".gax.yaml").read_text())
        assert meta["title"] == "Updated Title"

    @patch("gax.gslides.gslides._get_presentation")
    def test_pull_removes_stale_files(self, mock_get, tmp_path):
        """Pull should remove slide files for slides that no longer exist remotely."""
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        # Checkout with 2 slides
        Presentation(url="pres_abc123").clone(output=output)
        assert len(list(output.glob("*.slides.gax.md"))) == 2

        # Remote now has only one slide
        one_slide_pres = {**SAMPLE_PRESENTATION, "slides": [SAMPLE_SLIDE]}
        mock_get.return_value = one_slide_pres

        Presentation(path=output).pull()

        slide_files = list(output.glob("*.slides.gax.md"))
        assert len(slide_files) == 1

    @patch("gax.gslides.gslides._get_presentation")
    def test_pull_handles_reordered_slides(self, mock_get, tmp_path):
        """Pull should update filenames when slides are reordered."""
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        # Checkout: slide_001 at index 0, slide_002 at index 1
        Presentation(url="pres_abc123").clone(output=output)
        files_before = sorted(f.name for f in output.glob("*.slides.gax.md"))
        assert files_before[0].startswith("00_")
        assert files_before[1].startswith("01_")

        # Reverse the slide order
        reversed_pres = {
            **SAMPLE_PRESENTATION,
            "slides": [SAMPLE_SLIDE_EMPTY, SAMPLE_SLIDE],
        }
        mock_get.return_value = reversed_pres

        Presentation(path=output).pull()

        files_after = sorted(f.name for f in output.glob("*.slides.gax.md"))
        assert len(files_after) == 2
        # Old files with wrong index should be gone
        assert files_after[0].startswith("00_")
        assert files_after[1].startswith("01_")


# =============================================================================
# Slide.push — format check
# =============================================================================


class TestSlidePush:
    @patch("gax.gslides.gslides._get_presentation")
    def test_push_markdown_raises(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output)

        slide_file = sorted(output.glob("*.slides.gax.md"))[0]

        with pytest.raises(ValueError, match="not supported for markdown"):
            Slide(path=slide_file).push()

    @patch("gax.gslides.gslides._get_presentation")
    def test_push_json_calls_api(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output, fmt="json")

        slide_file = sorted(output.glob("*.slides.gax.md"))[0]

        with (
            patch("gax.gslides.gslides.get_authenticated_credentials"),
            patch("gax.gslides.gslides.build") as mock_build,
        ):
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            Slide(path=slide_file).push()

            mock_service.presentations().batchUpdate.assert_called_once()


# =============================================================================
# Presentation.push — format gate
# =============================================================================


class TestPresentationPush:
    @patch("gax.gslides.gslides._get_presentation")
    def test_push_markdown_checkout_raises(self, mock_get, tmp_path):
        mock_get.return_value = SAMPLE_PRESENTATION
        output = tmp_path / "test.slides.gax.md.d"

        Presentation(url="pres_abc123").clone(output=output)

        with pytest.raises(ValueError, match="not supported for markdown"):
            Presentation(path=output).push()
