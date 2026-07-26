"""Tests for the revision guard (gax-5ck).

Covers:
  - rev_guard() in syncstate.py: changed / unchanged / no-rev cases
  - confirm_and_push() in ui.py: warning + prompt when rev differs, silent
    when revs match, bypassed when resource has no fetch_rev.
"""

import io
import sys

import click
import yaml
from click.testing import CliRunner
from unittest.mock import MagicMock

from gax.syncstate import SyncState, rev_guard, write_sync_header


# ---------------------------------------------------------------------------
# Unit tests for rev_guard()
# ---------------------------------------------------------------------------


class TestRevGuard:
    def test_rev_changed_returns_true(self):
        state = SyncState(time=None, rev="old-rev")
        changed, remote = rev_guard(state, lambda: "new-rev")
        assert changed is True
        assert remote == "new-rev"

    def test_rev_unchanged_returns_false(self):
        state = SyncState(time=None, rev="same-rev")
        changed, remote = rev_guard(state, lambda: "same-rev")
        assert changed is False
        assert remote == "same-rev"

    def test_no_rev_skips_check(self):
        """When state.rev is empty, guard never calls fetch_rev."""
        called = []

        def fetch():
            called.append(True)
            return "whatever"

        state = SyncState(time=None, rev="")
        changed, remote = rev_guard(state, fetch)
        assert changed is False
        assert remote == ""
        assert not called, "fetch_rev should not be called when state.rev is empty"

    def test_fetch_rev_called_once(self):
        calls = []

        def fetch():
            calls.append(1)
            return "v2"

        state = SyncState(time=None, rev="v1")
        rev_guard(state, fetch)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Integration tests for confirm_and_push() with revision guard
# ---------------------------------------------------------------------------


def _make_file_with_rev(tmp_path, rev: str):
    """Write a gax file with sync.rev=rev to tmp_path and return path."""
    headers = write_sync_header({"type": "gax/test"}, rev=rev)
    content = "---\n" + yaml.dump(headers) + "---\n# body\n"
    p = tmp_path / "test.gax.md"
    p.write_text(content)
    return p


@click.command()
@click.pass_context
def _wrap_confirm_and_push(ctx):
    from gax.ui import confirm_and_push

    obj = ctx.obj
    confirm_and_push(obj["resource"], yes=obj["yes"])


class TestConfirmAndPushRevGuard:
    def test_rev_mismatch_prints_warning_and_prompts(self, tmp_path):
        """When remote rev differs from stored rev, user gets a prompt."""
        path = _make_file_with_rev(tmp_path, rev="rev-1")

        resource = MagicMock()
        resource.path = path
        resource.diff.return_value = "some diff"
        resource.fetch_rev.return_value = "rev-2"

        runner = CliRunner()
        result = runner.invoke(
            _wrap_confirm_and_push,
            obj={"resource": resource, "yes": False},
            input="n\n",
            catch_exceptions=False,
        )

        resource.push.assert_not_called()
        assert "remote changed since last pull" in result.output

    def test_rev_match_no_prompt(self, tmp_path):
        """When remote rev matches stored rev, no guard prompt is shown."""
        from gax.ui import confirm_and_push

        path = _make_file_with_rev(tmp_path, rev="rev-1")

        resource = MagicMock()
        resource.path = path
        resource.diff.return_value = None  # no changes
        resource.fetch_rev.return_value = "rev-1"  # same

        confirm_and_push(resource, yes=True)
        resource.push.assert_not_called()  # diff=None → no push

    def test_no_fetch_rev_skips_guard(self, tmp_path):
        """Resources without fetch_rev are not guarded."""
        from gax.ui import confirm_and_push

        path = _make_file_with_rev(tmp_path, rev="rev-1")

        resource = MagicMock(spec=["path", "diff", "push"])
        resource.path = path
        resource.diff.return_value = None

        # Should not raise even though rev-1 in header but no fetch_rev
        confirm_and_push(resource, yes=True)
        resource.push.assert_not_called()

    def test_yes_flag_proceeds_despite_mismatch(self, tmp_path):
        """With yes=True and mismatched rev, push still runs (warning is printed)."""
        from gax.ui import confirm_and_push

        path = _make_file_with_rev(tmp_path, rev="rev-1")

        resource = MagicMock()
        resource.path = path
        resource.diff.return_value = "some diff"
        resource.fetch_rev.return_value = "rev-2"

        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            confirm_and_push(resource, yes=True)
        finally:
            sys.stderr = old_stderr

        resource.push.assert_called_once()
