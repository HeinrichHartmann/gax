"""
CLI Pattern Tests

Validates that gax commands follow consistent design patterns:
1. Readonly pattern: clone + pull
2. Writable pattern: plan + apply
3. Checkout pattern: checkout/fetch for multipart resources
"""

import click
import pytest

from gax.cli import main as cli


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
    return any(
        p.name == name and isinstance(p, click.Argument)
        for p in cmd.params
    )


# =============================================================================
# Resource Classifications
# =============================================================================

# Resources that follow clone + pull pattern
READONLY_RESOURCES = [
    'mailbox',
    'mail',
    'contacts',
    'draft',
    'mail-label',
    'mail-filter',
    'cal',
    'sheet',
]

# Resources that follow plan + apply pattern
WRITABLE_RESOURCES = [
    'mailbox',
    'contacts',
    'mail-label',
    'mail-filter',
    'form',
]

# Resources that can be split (have checkout or fetch)
CHECKOUT_RESOURCES = {
    'cal': 'checkout',
    'mailbox': 'fetch',
    'sheet': 'checkout',
    'doc': 'checkout',
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
        assert 'clone' in cmd.commands, f"'{resource}' missing 'clone' subcommand"

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_clone_has_output_option(self, resource):
        """Clone must have -o/--output option."""
        cmd = get_command([resource])
        clone = cmd.commands['clone']
        assert has_option(clone, 'output', '-o', '--output'), \
            f"'{resource} clone' missing -o/--output option"

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_has_pull_command(self, resource):
        """All readonly resources must have pull."""
        cmd = get_command([resource])
        assert cmd is not None
        assert isinstance(cmd, click.Group)
        assert 'pull' in cmd.commands, f"'{resource}' missing 'pull' subcommand"

    @pytest.mark.parametrize("resource", READONLY_RESOURCES)
    def test_pull_has_file_argument(self, resource):
        """Pull must accept a file argument."""
        cmd = get_command([resource])
        pull = cmd.commands['pull']
        # Pull should have at least one argument (file)
        args = [p for p in pull.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} pull' missing file argument"


# =============================================================================
# Pattern 2: Writable (plan + apply)
# =============================================================================

class TestWritablePattern:
    """Writable resources must have plan and apply commands."""

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_has_plan_command(self, resource):
        """All writable resources must have plan."""
        cmd = get_command([resource])
        assert cmd is not None, f"Resource '{resource}' not found"
        assert isinstance(cmd, click.Group), f"'{resource}' should be a group"
        assert 'plan' in cmd.commands, f"'{resource}' missing 'plan' subcommand"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_plan_has_file_argument(self, resource):
        """Plan must accept a file argument."""
        cmd = get_command([resource])
        plan = cmd.commands['plan']
        args = [p for p in plan.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} plan' missing file argument"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_plan_has_output_option(self, resource):
        """Plan should have -o/--output option for plan file."""
        cmd = get_command([resource])
        plan = cmd.commands['plan']
        assert has_option(plan, 'output', '-o', '--output'), \
            f"'{resource} plan' missing -o/--output option"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_has_apply_command(self, resource):
        """All writable resources must have apply."""
        cmd = get_command([resource])
        assert cmd is not None
        assert isinstance(cmd, click.Group)
        assert 'apply' in cmd.commands, f"'{resource}' missing 'apply' subcommand"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_apply_has_plan_file_argument(self, resource):
        """Apply must accept a plan_file argument."""
        cmd = get_command([resource])
        apply_cmd = cmd.commands['apply']
        args = [p for p in apply_cmd.params if isinstance(p, click.Argument)]
        assert len(args) > 0, f"'{resource} apply' missing plan_file argument"

    @pytest.mark.parametrize("resource", WRITABLE_RESOURCES)
    def test_apply_has_yes_flag(self, resource):
        """Apply must have -y/--yes flag to skip confirmation for automation."""
        cmd = get_command([resource])
        apply_cmd = cmd.commands['apply']
        # Check that there's a 'yes' parameter with correct flags
        assert has_option(apply_cmd, 'yes', '-y', '--yes', is_flag=True), \
            f"'{resource} apply' missing -y/--yes flag"


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
        assert command_name in cmd.commands, \
            f"'{resource}' missing '{command_name}' subcommand"

    @pytest.mark.parametrize("resource,command_name", CHECKOUT_RESOURCES.items())
    def test_checkout_has_output_option(self, resource, command_name):
        """Checkout/fetch must have -o/--output option."""
        cmd = get_command([resource])
        checkout = cmd.commands[command_name]
        assert has_option(checkout, 'output', '-o', '--output'), \
            f"'{resource} {command_name}' missing -o/--output option"


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
                if param.name == 'output' and isinstance(param, click.Option):
                    if set(param.opts) != {'-o', '--output'}:
                        output_violations.append(
                            f"{path}: has {param.opts}, expected {{'-o', '--output'}}"
                        )

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not output_violations, \
            "Output option flag inconsistencies:\n" + "\n".join(output_violations)

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

        assert not missing_help, \
            "Commands missing help text:\n" + "\n".join(missing_help)

    def test_all_format_options_use_same_flags(self):
        """All format options should use -f/--format consistently."""
        format_violations = []

        def check_command(cmd, path=""):
            for param in cmd.params:
                if param.name == 'fmt' and isinstance(param, click.Option):
                    # Should have both -f and --format
                    if set(param.opts) != {'-f', '--format'}:
                        format_violations.append(
                            f"{path}: has {param.opts}, expected {{'-f', '--format'}}"
                        )

            if isinstance(cmd, click.Group):
                for name, subcmd in cmd.commands.items():
                    check_command(subcmd, f"{path}/{name}" if path else name)

        check_command(cli)

        assert not format_violations, \
            "Format option flag inconsistencies:\n" + "\n".join(format_violations)

    def test_apply_commands_have_yes_flag(self):
        """Apply commands must have -y/--yes flag to skip confirmation for automation."""
        apply_violations = []

        def check_command(cmd, path=""):
            # Check if this is an apply command
            if cmd.name == 'apply' and not isinstance(cmd, click.Group):
                # Check for yes parameter with correct flags
                yes_param = next((p for p in cmd.params if p.name == 'yes'), None)
                if yes_param is None:
                    apply_violations.append(
                        f"{path}: apply command missing -y/--yes flag"
                    )
                elif not isinstance(yes_param, click.Option):
                    apply_violations.append(
                        f"{path}: 'yes' should be an option, not {type(yes_param)}"
                    )
                elif set(yes_param.opts) != {'-y', '--yes'}:
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

        assert not apply_violations, \
            "Apply commands missing or incorrect -y/--yes flag:\n" + "\n".join(apply_violations)
