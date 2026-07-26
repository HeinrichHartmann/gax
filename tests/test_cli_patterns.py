"""
CLI Pattern Tests

Validates that gax commands follow consistent design patterns:
1. Readonly pattern: clone + pull
2. Writable pattern: push with -y/--yes flag (uses diff+confirm)
3. Legacy writable: plan + apply (being phased out)
4. Checkout pattern: checkout/fetch for multipart resources
"""

import click
import pytest

from gax.cli import main as cli, _collect_recursive


# =============================================================================
# Helper Functions
# =============================================================================


def get_command(path: list[str]):
    """Navigate to command in CLI tree."""
    cmd = cli
    for part in path:
        if isinstance(cmd, click.Group):
            cmd = cmd.commands.get(part)
            if cmd is None:
                return None
        else:
            return None
    return cmd


def has_option(cmd, name: str, *expected_opts, is_flag: bool = False):
    """Check if command has an option with expected flags."""
    for param in cmd.params:
        if param.name == name and isinstance(param, click.Option):
            if expected_opts and set(param.opts) != set(expected_opts):
                return False
            if is_flag and not param.is_flag:
                return False
            return True
    return False


def has_argument(cmd, name: str):
    """Check if command has a positional argument."""
    return any(p.name == name and isinstance(p, click.Argument) for p in cmd.params)


# =============================================================================
# Resource Classifications
# =============================================================================

# Resources that follow clone + pull pattern
READONLY_RESOURCES = [
    "mailbox",
    "mail",
    "contacts",
    "draft",
    "mail-label",
    "mail-filter",
    "cal",
    "sheet",
]

# Resources that follow push pattern (diff + confirm + push)
PUSH_RESOURCES = [
    "draft",
    "contacts",
]

# Resources that follow legacy plan + apply pattern
WRITABLE_RESOURCES = [
    "mailbox",
    "mail-label",
    "form",
]

# Resources that can be split (have checkout or fetch)
CHECKOUT_RESOURCES = {
    "cal": "checkout",
    "mailbox": "fetch",
    "sheet": "checkout",
    "doc": "checkout",
}


# =============================================================================
# Pattern 1: Readonly (clone + pull)
# =============================================================================


class TestReadonlyPattern:
    """Resources must have clone and pull commands with standard options."""

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_has_clone_command(self, resource):
        """All readonly resources must have clone."""
        cmd = get_command([resource])
        assert cmd is not None, f"Resource '{resource}' not found"
        assert isinstance(cmd, click.Group), f"'{resource}' should be a group"
        assert "clone" in cmd.commands, f"'{resource}' missing 'clone' subcommand"

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_clone_has_output_option(self, resource):
        """Clone must have -o/--output option."""
        cmd = get_command([resource])
        clone = cmd.commands["clone"]
        assert has_option(clone, "output", "-o", "--output"), (
            f"'{resource} clone' missing -o/--output option"
        )

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_has_pull_command(self, resource):
        """All readonly resources must have pull."""
        cmd = get_command([resource])
        assert cmd is not None
        assert isinstance(cmd, click.Group)
        assert "pull" in cmd.commands, f"'{resource}' missing 'pull' subcommand"

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_pull_has_file_argument(self, resource):
        """Pull must accept a file argument."""
        cmd = get_command([resource])
        pull = cmd.commands["pull"]
        # Pull should have at least one argument (file)
        args = [p for p in pull.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} pull' missing file argument"


# =============================================================================
# Pattern 2a: Push (diff + confirm + push)
# =============================================================================


class TestPushPattern:
    """Resources with push must have push command with -y/--yes flag."""

    @pytest.mark.parametrize("resource", PUSH_RESOURCES)
    def test_has_push_command(self, resource):
        """Push resources must have push subcommand."""
        cmd = get_command([resource])
        assert cmd is not None, f"Resource '{resource}' not found"
        assert isinstance(cmd, click.Group)
        assert "push" in cmd.commands, f"'{resource}' missing 'push' subcommand"

    @pytest.mark.parametrize("resource", PUSH_RESOURCES)
    def test_push_has_file_argument(self, resource):
        """Push must accept a file argument."""
        cmd = get_command([resource])
        push = cmd.commands["push"]
        args = [p for p in push.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} push' missing file argument"

    @pytest.mark.parametrize("resource", PUSH_RESOURCES)
    def test_push_has_yes_flag(self, resource):
        """Push must have -y/--yes flag to skip confirmation."""
        cmd = get_command([resource])
        push = cmd.commands["push"]
        assert has_option(push, "yes", "-y", "--yes", is_flag=True), (
            f"'{resource} push' missing -y/--yes flag"
        )


# =============================================================================
# Pattern 2b: Writable (plan + apply) — legacy pattern
# =============================================================================


class TestWritablePattern:
    """Writable resources must have plan and apply commands."""

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_has_plan_command(self, resource):
        """All writable resources must have plan."""
        cmd = get_command([resource])
        assert cmd is not None, f"Resource '{resource}' not found"
        assert isinstance(cmd, click.Group), f"'{resource}' should be a group"
        assert "plan" in cmd.commands, f"'{resource}' missing 'plan' subcommand"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_plan_has_file_argument(self, resource):
        """Plan must accept a file argument."""
        cmd = get_command([resource])
        plan = cmd.commands["plan"]
        args = [p for p in plan.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} plan' missing file argument"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_plan_has_output_option(self, resource):
        """Plan should have -o/--output option for plan file."""
        cmd = get_command([resource])
        plan = cmd.commands["plan"]
        assert has_option(plan, "output", "-o", "--output"), (
            f"'{resource} plan' missing -o/--output option"
        )

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_has_apply_command(self, resource):
        """All writable resources must have apply."""
        cmd = get_command([resource])
        assert cmd is not None
        assert isinstance(cmd, click.Group)
        assert "apply" in cmd.commands, f"'{resource}' missing 'apply' subcommand"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_apply_has_plan_file_argument(self, resource):
        """Apply must accept a plan_file argument."""
        cmd = get_command([resource])
        apply_cmd = cmd.commands["apply"]
        args = [p for p in apply_cmd.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} apply' missing plan_file argument"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_apply_has_yes_flag(self, resource):
        """Apply must have -y/--yes flag to skip confirmation for automation."""
        cmd = get_command([resource])
        apply_cmd = cmd.commands["apply"]
        # Check that there's a 'yes' parameter with correct flags
        assert has_option(apply_cmd, "yes", "-y", "--yes", is_flag=True), (
            f"'{resource} apply' missing -y/--yes flag"
        )


# =============================================================================
# Pattern 3: Checkout (folder output for multipart resources)
# =============================================================================


class TestCheckoutPattern:
    """Multipart resources must have checkout or fetch command."""

    @pytest.mark.parametrize("resource,command_name", CHECKOUT_RESOURCES.items())
    def test_has_checkout_command(self, resource, command_name):
        """Multipart resources must have checkout/fetch."""
        cmd = get_command([resource])
        assert cmd is not None, f"Resource '{resource}' not found"
        assert isinstance(cmd, click.Group), f"'{resource}' should be a group"
        assert command_name in cmd.commands, (
            f"'{resource}' missing '{command_name}' subcommand"
        )

    @pytest.mark.parametrize("resource,command_name", CHECKOUT_RESOURCES.items())
    def test_checkout_has_output_option(self, resource, command_name):
        """Checkout/fetch must have -o/--output option."""
        cmd = get_command([resource])
        checkout = cmd.commands[command_name]
        assert has_option(checkout, "output", "-o", "--output"), (
            f"'{resource} {command_name}' missing -o/--output option"
        )


# =============================================================================
# Cross-Pattern Consistency Tests
# =============================================================================


class TestCrossCuttingConsistency:
    """Test consistency across all patterns."""

    def test_all_output_options_use_same_flags(self):
        """All output options should use -o/--output consistently."""
        output_violations = []

        def check_command(cmd, path=""):
            for param in cmd.params:
                if param.name == "output" and isinstance(param, click.Option):
                    if set(param.opts) != {"-o", "--output"}:
                        output_violations.append(
                            f"{path}: has {param.opts}, expected {{'-o', '--output'}}"
                        )

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not output_violations, (
            "Output option flag inconsistencies:\n" + "\n".join(output_violations)
        )

    def test_all_commands_have_help_text(self):
        """All commands and subcommands must have help text."""
        missing_help = []

        def check_command(cmd, path=""):
            if cmd.help is None:
                missing_help.append(path or "root")

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not missing_help, "Commands missing help text:\n" + "\n".join(
            missing_help
        )

    def test_pull_flags_forwardable_by_unified_pull(self):
        """All extra flags on resource pull commands must be forwardable
        through 'gax pull' (unified_pull).

        unified_pull uses _split_flags_and_files which converts --flag-name
        to flag_name=True and passes it as **kwargs.  This only works if:
          1. The option is a boolean flag (is_flag=True)
          2. There's no Click parameter rename (e.g. --all -> include_all)
             because _split_flags_and_files doesn't know about renames

        If this test fails, either:
          - Fix the option to not use a rename, OR
          - Add the flag as an explicit option on unified_pull
        """
        # Options that unified_pull handles itself (not forwarded)
        HANDLED_BY_UNIFIED = {"yes", "files"}

        violations = []

        def check_pull_cmd(cmd, path: str):
            for param in cmd.params:
                if not isinstance(param, click.Option):
                    continue
                if param.name in HANDLED_BY_UNIFIED:
                    continue

                # (1) Must be a boolean flag
                if not param.is_flag:
                    violations.append(
                        f"{path}: --{param.name} is not a boolean flag "
                        f"(cannot be forwarded by _split_flags_and_files)"
                    )
                    continue

                # (2) The CLI flag name must produce the same kwarg name
                # as _split_flags_and_files would generate
                long_opts = [o for o in param.opts if o.startswith("--")]
                if not long_opts:
                    continue
                cli_flag = max(long_opts, key=len)  # e.g. "--with-comments"
                forwarded_name = cli_flag.lstrip("-").replace("-", "_")
                if forwarded_name != param.name:
                    violations.append(
                        f"{path}: {cli_flag} maps to Click param "
                        f"'{param.name}', but _split_flags_and_files "
                        f"produces '{forwarded_name}'"
                    )

        def walk(cmd, path=""):
            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    walk(subcmd, f"{path}/{name}" if path else name)
            elif cmd.name == "pull":
                check_pull_cmd(cmd, path)

        walk(cli)

        assert not violations, (
            "Pull flags not forwardable by unified_pull:\n"
            + "\n".join(violations)
        )

    def test_all_format_options_use_same_flags(self):
        """All format options should use -f/--format consistently."""
        format_violations = []

        def check_command(cmd, path=""):
            for param in cmd.params:
                if param.name == "fmt" and isinstance(param, click.Option):
                    # Should have both -f and --format
                    if set(param.opts) != {"-f", "--format"}:
                        format_violations.append(
                            f"{path}: has {param.opts}, expected {{'-f', '--format'}}"
                        )

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not format_violations, (
            "Format option flag inconsistencies:\n" + "\n".join(format_violations)
        )

    def test_pull_commands_have_yes_flag(self):
        """All pull commands must have -y/--yes flag to skip confirmation."""
        violations = []

        def check_command(cmd, path=""):
            if cmd.name == "pull" and not isinstance(cmd, click.Group):
                yes_param = next((p for p in cmd.params if p.name == "yes"), None)
                if yes_param is None:
                    violations.append(f"{path}: pull command missing -y/--yes flag")
                elif set(getattr(yes_param, "opts", [])) != {"-y", "--yes"}:
                    violations.append(
                        f"{path}: yes flag should be -y/--yes, got {yes_param.opts}"
                    )
                elif not getattr(yes_param, "is_flag", False):
                    violations.append(f"{path}: yes option should be a boolean flag")

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not violations, (
            "Pull commands missing -y/--yes flag:\n" + "\n".join(violations)
        )

    def test_apply_commands_have_yes_flag(self):
        """Apply commands must have -y/--yes flag to skip confirmation for automation."""
        apply_violations = []

        def check_command(cmd, path=""):
            # Check if this is an apply command
            if cmd.name == "apply" and not isinstance(cmd, click.Group):
                # Check for yes parameter with correct flags
                yes_param = next((p for p in cmd.params if p.name == "yes"), None)
                if yes_param is None:
                    apply_violations.append(
                        f"{path}: apply command missing -y/--yes flag"
                    )
                elif not isinstance(yes_param, click.Option):
                    apply_violations.append(
                        f"{path}: 'yes' should be an option, not {type(yes_param)}"
                    )
                elif set(yes_param.opts) != {"-y", "--yes"}:
                    apply_violations.append(
                        f"{path}: yes flag should be -y/--yes, got {yes_param.opts}"
                    )
                elif not yes_param.is_flag:
                    apply_violations.append(
                        f"{path}: yes option should be a boolean flag"
                    )

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not apply_violations, (
            "Apply commands missing or incorrect -y/--yes flag:\n"
            + "\n".join(apply_violations)
        )


# =============================================================================
# _collect_recursive tree-walking logic
# =============================================================================


class TestCollectRecursive:
    """Unit tests for the _collect_recursive tree-walking helper (no API calls)."""

    def test_collects_gax_md_files(self, tmp_path):
        (tmp_path / "a.doc.gax.md").write_text("x")
        (tmp_path / "b.sheet.gax.md").write_text("x")
        (tmp_path / "ignore.txt").write_text("x")
        result = {p.name for p in _collect_recursive(tmp_path)}
        assert result == {"a.doc.gax.md", "b.sheet.gax.md"}

    def test_collects_gax_md_d_folders_as_units(self, tmp_path):
        folder = tmp_path / "checkout.doc.gax.md.d"
        folder.mkdir()
        (folder / "page1.md").write_text("x")
        result = [p for p in _collect_recursive(tmp_path)]
        assert len(result) == 1
        assert result[0].name == "checkout.doc.gax.md.d"

    def test_does_not_descend_into_gax_md_d(self, tmp_path):
        folder = tmp_path / "checkout.doc.gax.md.d"
        folder.mkdir()
        # A .gax.md file inside the checkout folder — should NOT be collected
        (folder / "nested.gax.md").write_text("x")
        result = _collect_recursive(tmp_path)
        names = {p.name for p in result}
        assert "nested.gax.md" not in names
        assert "checkout.doc.gax.md.d" in names

    def test_collects_from_nested_subdirectories(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "deep.doc.gax.md").write_text("x")
        (tmp_path / "top.doc.gax.md").write_text("x")
        result = {p.name for p in _collect_recursive(tmp_path)}
        assert result == {"deep.doc.gax.md", "top.doc.gax.md"}

    def test_empty_tree_returns_empty(self, tmp_path):
        assert _collect_recursive(tmp_path) == []
