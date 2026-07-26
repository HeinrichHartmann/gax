"""CLI surface consistency tests.

Validates that the gax CLI provides uniform access to diff, pull, push
across all resource types — both at the top-level meta-commands (gax pull,
gax push, gax diff) and at the per-resource level (gax doc pull, etc.).

These tests inspect Click command objects, they never call APIs.
"""

import click
import pytest

from gax.cli import main as cli
from gax.resource import Resource

# Import CLI to trigger all registrations
import gax.cli  # noqa: F401


# =============================================================================
# Helpers
# =============================================================================


def get_command(path: list[str]) -> click.BaseCommand | None:
    """Navigate to a command in the CLI tree."""
    cmd = cli
    for part in path:
        if isinstance(cmd, click.Group):
            cmd = cmd.commands.get(part)
            if cmd is None:
                return None
        else:
            return None
    return cmd


def walk_commands(cmd, path=""):
    """Yield (path, cmd) for all leaf commands in the CLI tree."""
    if isinstance(cmd, click.Group):
        for name, subcmd in cmd.commands.items():
            yield from walk_commands(subcmd, f"{path}/{name}" if path else name)
    else:
        yield path, cmd


def find_commands_named(name: str) -> list[tuple[str, click.BaseCommand]]:
    """Find all leaf commands with a given name anywhere in the tree."""
    return [(p, c) for p, c in walk_commands(cli) if c.name == name]


def has_option(cmd, name: str, *expected_opts, is_flag: bool = False) -> bool:
    """Check if command has an option with expected flags."""
    for param in cmd.params:
        if param.name == name and isinstance(param, click.Option):
            if expected_opts and set(param.opts) != set(expected_opts):
                return False
            if is_flag and not param.is_flag:
                return False
            return True
    return False


# =============================================================================
# Top-level meta-commands
# =============================================================================


class TestTopLevelCommands:
    """The global CLI must expose pull, push, and diff."""

    def test_has_pull_command(self):
        cmd = get_command(["pull"])
        assert cmd is not None, "Missing top-level 'gax pull' command"

    def test_has_push_command(self):
        cmd = get_command(["push"])
        assert cmd is not None, "Missing top-level 'gax push' command"

    def test_has_diff_command(self):
        cmd = get_command(["diff"])
        assert cmd is not None, (
            "Missing top-level 'gax diff' meta-command — "
            "diff should be dispatched via Resource.from_file() like pull/push"
        )

    def test_pull_accepts_multiple_files(self):
        cmd = get_command(["pull"])
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert any(a.nargs == -1 for a in args), "pull should accept multiple files"

    def test_push_accepts_multiple_files(self):
        cmd = get_command(["push"])
        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        assert any(a.nargs == -1 for a in args), "push should accept multiple files"

    def test_pull_has_yes_flag(self):
        cmd = get_command(["pull"])
        assert has_option(cmd, "yes", "-y", "--yes", is_flag=True)

    def test_push_has_yes_flag(self):
        cmd = get_command(["push"])
        assert has_option(cmd, "yes", "-y", "--yes", is_flag=True)


# =============================================================================
# Resource class diff/pull/push method coverage
# =============================================================================


# All registered Resource subclasses that represent a concrete, pullable resource.
# Excludes abstract base and internal-only classes.
def _concrete_resources() -> list[type]:
    return [cls for cls in Resource._subclasses if hasattr(cls, "name")]


def _resource_names() -> list[str]:
    return [cls.name for cls in _concrete_resources()]


# Resources that have push semantics (i.e. you can edit locally and push back).
# Readonly resources (Thread, Cal, Mailbox browse) are excluded.
PUSHABLE_RESOURCES = {
    "doc-tab", "doc",
    "sheet-tab", "sheet",
    "slides", "presentation",
    "draft",
    "event",
    "task",
    "form",
    "contacts",
    "label",
    "filter",
    "file",
}

# Resources that should have diff (all pushable ones, plus anything with pull)
DIFFABLE_RESOURCES = PUSHABLE_RESOURCES | {
    "cal",          # cal list diff (shows added/removed events)
    "task-list",    # task list diff
    "thread",       # thread diff (new messages)
    "mailbox",      # mailbox diff (new threads)
}


class TestResourceDiffMethod:
    """Every resource with push or meaningful pull must implement diff()."""

    @pytest.mark.parametrize("cls", _concrete_resources(), ids=lambda c: c.name)
    def test_diff_not_base_notimplemented(self, cls):
        """Resources in DIFFABLE_RESOURCES must override diff()."""
        if cls.name not in DIFFABLE_RESOURCES:
            pytest.skip(f"{cls.name} not expected to have diff")
        assert cls.diff is not Resource.diff, (
            f"{cls.__name__} (name={cls.name}) does not override diff() — "
            f"base Resource.diff raises NotImplementedError"
        )

    @pytest.mark.parametrize("cls", _concrete_resources(), ids=lambda c: c.name)
    def test_pushable_has_push(self, cls):
        """Pushable resources must override push()."""
        if cls.name not in PUSHABLE_RESOURCES:
            pytest.skip(f"{cls.name} not expected to have push")
        assert cls.push is not Resource.push, (
            f"{cls.__name__} has no push() override"
        )

    @pytest.mark.parametrize("cls", _concrete_resources(), ids=lambda c: c.name)
    def test_all_have_pull(self, cls):
        """Every concrete resource must override pull()."""
        assert cls.pull is not Resource.pull, (
            f"{cls.__name__} (name={cls.name}) has no pull() override"
        )


# =============================================================================
# Per-resource CLI: pull/push/diff subcommands
# =============================================================================


# Map: CLI group name -> resource class names it covers
# Groups that are top-level Click groups in the CLI
RESOURCE_GROUPS = {
    "doc":         {"commands": {"clone", "checkout", "pull", "push"}},
    "sheet":       {"commands": {"clone", "checkout", "pull", "push"}},
    "slides":      {"commands": {"checkout", "pull", "push"}},
    "drive":       {"commands": {"clone", "checkout", "pull", "push"}},
    "draft":       {"commands": {"clone", "pull", "push"}},
    "contacts":    {"commands": {"clone", "checkout", "pull", "push"}},
    "cal":         {"commands": {"clone", "checkout", "pull"}},
    "task":        {"commands": {"clone", "checkout", "pull", "push"}},
    "form":        {"commands": {"clone", "pull"}},
    "mail":        {"commands": {"clone", "pull"}},
    "mail-label":  {"commands": {"clone", "pull"}},
    "mail-filter": {"commands": {"clone", "pull"}},
    "mailbox":     {"commands": {"clone", "pull"}},
}

# Groups that must have a pull subcommand
GROUPS_WITH_PULL = list(RESOURCE_GROUPS.keys())

# Groups that must have a push subcommand (writable resources)
GROUPS_WITH_PUSH = [
    g for g, spec in RESOURCE_GROUPS.items() if "push" in spec["commands"]
]


class TestResourceGroupPull:
    """Every resource group must have a pull subcommand with -y/--yes."""

    @pytest.mark.parametrize("group", GROUPS_WITH_PULL)
    def test_has_pull(self, group):
        cmd = get_command([group])
        assert cmd is not None, f"CLI group '{group}' not found"
        assert isinstance(cmd, click.Group), f"'{group}' should be a Click group"
        # pull can be directly on the group or in a subgroup
        all_cmds = set(cmd.commands.keys())
        # Also check nested groups (e.g. "doc tab pull")
        has_pull = "pull" in all_cmds
        if not has_pull:
            for sub_name, sub_cmd in cmd.commands.items():
                if isinstance(sub_cmd, click.Group) and "pull" in sub_cmd.commands:
                    has_pull = True
                    break
        assert has_pull, f"'{group}' has no 'pull' subcommand"

    @pytest.mark.parametrize("group", GROUPS_WITH_PULL)
    def test_pull_has_yes_flag(self, group):
        cmd = get_command([group])
        pull = cmd.commands.get("pull")
        if pull is None:
            pytest.skip(f"{group} pull is in a subgroup")
        assert has_option(pull, "yes", "-y", "--yes", is_flag=True), (
            f"'{group} pull' missing -y/--yes flag"
        )


class TestResourceGroupPush:
    """Writable resource groups must have push with -y/--yes."""

    @pytest.mark.parametrize("group", GROUPS_WITH_PUSH)
    def test_has_push(self, group):
        cmd = get_command([group])
        assert cmd is not None, f"CLI group '{group}' not found"
        all_cmds = set(cmd.commands.keys())
        has_push = "push" in all_cmds
        if not has_push:
            for sub_name, sub_cmd in cmd.commands.items():
                if isinstance(sub_cmd, click.Group) and "push" in sub_cmd.commands:
                    has_push = True
                    break
        assert has_push, f"'{group}' has no 'push' subcommand"

    @pytest.mark.parametrize("group", GROUPS_WITH_PUSH)
    def test_push_has_yes_flag(self, group):
        cmd = get_command([group])
        push = cmd.commands.get("push")
        if push is None:
            # Check subgroups
            for sub_name, sub_cmd in cmd.commands.items():
                if isinstance(sub_cmd, click.Group) and "push" in sub_cmd.commands:
                    push = sub_cmd.commands["push"]
                    break
        if push is None:
            pytest.skip(f"{group} push not found directly")
        assert has_option(push, "yes", "-y", "--yes", is_flag=True), (
            f"'{group} push' missing -y/--yes flag"
        )


# =============================================================================
# Diff subcommands
# =============================================================================

# Groups where a diff subcommand should exist — at minimum all pushable groups,
# since diff is needed to preview before push.
GROUPS_WANTING_DIFF = [
    "doc", "sheet", "draft", "contacts", "task",
    "cal", "drive", "slides", "form",
    "mail-label", "mail-filter",
]


class TestResourceGroupDiff:
    """Writable resource groups should have a diff subcommand."""

    @pytest.mark.parametrize("group", GROUPS_WANTING_DIFF)
    def test_has_diff(self, group):
        """Each writable group should expose 'diff' as a CLI subcommand.

        Currently most resources only call diff() internally from push.
        This test documents the expectation that diff becomes a first-class
        subcommand for all writable resources.
        """
        cmd = get_command([group])
        assert cmd is not None, f"CLI group '{group}' not found"
        all_cmds = set(cmd.commands.keys())
        # Check direct and one level of nesting
        has_diff = "diff" in all_cmds
        if not has_diff:
            for sub_name, sub_cmd in cmd.commands.items():
                if isinstance(sub_cmd, click.Group) and "diff" in sub_cmd.commands:
                    has_diff = True
                    break
        assert has_diff, (
            f"'{group}' has no 'diff' subcommand — "
            f"available: {sorted(all_cmds)}"
        )


# =============================================================================
# Push confirmation consistency
# =============================================================================


class TestPushConfirmationConsistency:
    """All push/apply commands must have -y/--yes for automation."""

    def test_all_push_commands_have_yes(self):
        """Every 'push' command in the CLI tree must have -y/--yes."""
        violations = []
        for path, cmd in find_commands_named("push"):
            if not has_option(cmd, "yes", "-y", "--yes", is_flag=True):
                violations.append(f"{path}: push missing -y/--yes flag")
        assert not violations, "\n".join(violations)

    def test_all_apply_commands_have_yes(self):
        """Every 'apply' command in the CLI tree must have -y/--yes."""
        violations = []
        for path, cmd in find_commands_named("apply"):
            if not has_option(cmd, "yes", "-y", "--yes", is_flag=True):
                violations.append(f"{path}: apply missing -y/--yes flag")
        assert not violations, "\n".join(violations)

    def test_all_pull_commands_have_yes(self):
        """Every 'pull' command in the CLI tree must have -y/--yes."""
        violations = []
        for path, cmd in find_commands_named("pull"):
            if not has_option(cmd, "yes", "-y", "--yes", is_flag=True):
                violations.append(f"{path}: pull missing -y/--yes flag")
        assert not violations, "\n".join(violations)


# =============================================================================
# Resource.from_file dispatch coverage for diff/pull/push
# =============================================================================


# File extensions that should be dispatchable by `gax pull/push/diff <file>`
DISPATCHABLE_EXTENSIONS = [
    (".doc.gax.md",    "gax/doc",       "Tab"),
    (".tab.gax.md",    "gax/doc",       "Tab"),
    (".tab.sheet.gax.md", "gax/sheet",  "SheetTab"),
    (".draft.gax.md",  "gax/draft",     "Draft"),
    (".mail.gax.md",   "gax/mail",      "Thread"),
    (".cal.gax.md",    "gax/cal",       "Event"),
    (".form.gax.md",   "gax/form",      "Form"),
    (".slides.gax.md", "gax/slides",    "Slide"),
]

# Checkout folders that should be dispatchable
DISPATCHABLE_CHECKOUTS = [
    ("gax/doc-checkout",       "Doc"),
    ("gax/sheet-checkout",     "Sheet"),
    ("gax/slides-checkout",    "Presentation"),
    ("gax/task-checkout",      "TaskList"),
    ("gax/contacts-checkout",  "Contacts"),
]


class TestFromFileDispatch:
    """Resource.from_file must correctly dispatch files for diff/pull/push."""

    @pytest.mark.parametrize(
        "ext,file_type,expected_class",
        DISPATCHABLE_EXTENSIONS,
        ids=[e[0] for e in DISPATCHABLE_EXTENSIONS],
    )
    def test_file_dispatches(self, tmp_path, ext, file_type, expected_class):
        f = tmp_path / f"test{ext}"
        if file_type == "gax/sheet":
            # SheetTab.from_file needs spreadsheet_id + tab in the header
            f.write_text(
                "---\nspreadsheet_id: abc123\ntab: Sheet1\n---\ncontent\n"
            )
        else:
            f.write_text(f"---\ntype: {file_type}\n---\ncontent\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == expected_class
        assert r.path == f

    @pytest.mark.parametrize(
        "checkout_type,expected_class",
        DISPATCHABLE_CHECKOUTS,
        ids=[c[0] for c in DISPATCHABLE_CHECKOUTS],
    )
    def test_folder_dispatches(self, tmp_path, checkout_type, expected_class):
        folder = tmp_path / "test.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text(
            f"type: {checkout_type}\nurl: https://example.com\n"
        )
        r = Resource.from_file(folder)
        assert r.__class__.__name__ == expected_class
        assert r.path == folder

    @pytest.mark.parametrize(
        "checkout_type,expected_class",
        DISPATCHABLE_CHECKOUTS,
        ids=[c[0] for c in DISPATCHABLE_CHECKOUTS],
    )
    def test_folder_resource_has_diff(self, tmp_path, checkout_type, expected_class):
        """Checkout folder resources must implement diff()."""
        folder = tmp_path / "test.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text(
            f"type: {checkout_type}\nurl: https://example.com\n"
        )
        r = Resource.from_file(folder)
        assert type(r).diff is not Resource.diff, (
            f"{expected_class} (checkout folder) does not implement diff()"
        )

    @pytest.mark.parametrize(
        "checkout_type,expected_class",
        DISPATCHABLE_CHECKOUTS,
        ids=[c[0] for c in DISPATCHABLE_CHECKOUTS],
    )
    def test_folder_resource_has_pull(self, tmp_path, checkout_type, expected_class):
        """Checkout folder resources must implement pull()."""
        folder = tmp_path / "test.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text(
            f"type: {checkout_type}\nurl: https://example.com\n"
        )
        r = Resource.from_file(folder)
        assert type(r).pull is not Resource.pull, (
            f"{expected_class} (checkout folder) does not implement pull()"
        )


# =============================================================================
# Confirm-and-* flow consistency
# =============================================================================


class TestConfirmFlowSymmetry:
    """confirm_and_pull and confirm_and_push should have symmetric behavior."""

    def test_confirm_and_pull_exists(self):
        from gax.ui import confirm_and_pull
        assert callable(confirm_and_pull)

    def test_confirm_and_push_exists(self):
        from gax.ui import confirm_and_push
        assert callable(confirm_and_push)

    def test_confirm_and_pull_accepts_yes_kwarg(self):
        """confirm_and_pull must accept yes= keyword."""
        import inspect
        from gax.ui import confirm_and_pull
        sig = inspect.signature(confirm_and_pull)
        assert "yes" in sig.parameters

    def test_confirm_and_push_accepts_yes_kwarg(self):
        """confirm_and_push must accept yes= keyword."""
        import inspect
        from gax.ui import confirm_and_push
        sig = inspect.signature(confirm_and_push)
        assert "yes" in sig.parameters

    def test_pull_always_computes_diff(self):
        """confirm_and_pull should compute diff even when yes=True.

        Currently it skips diff entirely when yes=True (ui.py:232-234),
        which means -y silently clobbers without knowing if remote changed.
        This test documents the expected behavior: diff is always computed
        (for safety logging/reporting), only the interactive prompt is skipped.
        """
        import inspect
        from gax.ui import confirm_and_pull
        source = inspect.getsource(confirm_and_pull)
        # The diff call should NOT be inside an "if not yes" block.
        # This is a structural assertion: diff() must be called unconditionally.
        lines = source.split("\n")
        diff_line = None
        for i, line in enumerate(lines):
            if "resource.diff(" in line or ".diff(" in line:
                diff_line = i
                break
        assert diff_line is not None, "confirm_and_pull doesn't call diff() at all"

        # Check that diff is not guarded by "if not yes"
        # Look at the preceding non-blank line
        for j in range(diff_line - 1, -1, -1):
            stripped = lines[j].strip()
            if stripped:
                assert "not yes" not in stripped, (
                    "confirm_and_pull gates diff() behind 'if not yes' — "
                    "diff should always be computed for safety, "
                    "only the prompt should be skipped with -y"
                )
                break
