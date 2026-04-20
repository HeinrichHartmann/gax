"""Unit tests for Google Drive folder workspace cloning helpers."""

from unittest.mock import Mock, patch

from gax.gdrive import Folder


class TestFolderWorkspaceClone:
    def test_clone_workspace_form_uses_form_resource_constructor(self, tmp_path):
        """Workspace Forms must use the stateful Form.from_url(...).clone(...) path."""
        folder = Folder()
        item = {
            "id": "form-123",
            "name": "Quarterly Survey",
            "mimeType": "application/vnd.google-apps.form",
            "path": "Quarterly Survey",
        }

        form_resource = Mock()

        with patch("gax.form.Form.from_url", return_value=form_resource) as from_url:
            folder._clone_workspace_file(item, tmp_path)

        expected_url = "https://docs.google.com/forms/d/form-123/edit"
        expected_output = tmp_path / "Quarterly_Survey.form.gax.md"

        from_url.assert_called_once_with(expected_url)
        form_resource.clone.assert_called_once_with(output=expected_output)
