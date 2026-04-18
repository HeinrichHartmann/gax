"""Documentation annotations for CLI commands.

Usage:
    from . import docs as doc

    @doc.section("resource")
    @doc.maturity("unstable")
    @click.group()
    def contacts():
        '''Google Contacts operations.'''
"""


def section(name: str):
    """Assign a command to a man page section (main, resource, utility)."""

    def decorator(cmd):
        cmd.doc_section = name
        return cmd

    return decorator


def maturity(level: str):
    """Mark command maturity level (e.g. 'unstable').

    Prepends [level] to the command help text.
    """

    def decorator(cmd):
        cmd.doc_maturity = level
        if cmd.help:
            cmd.help = f"[{level}] " + cmd.help
        return cmd

    return decorator
