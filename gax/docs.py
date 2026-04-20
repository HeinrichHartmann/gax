"""Documentation annotations and manual rendering for gax CLI.

Decorators for annotating commands with section/maturity metadata,
plus plain text and Markdown manual page generation from Click metadata.

Usage:
    from . import docs

    @docs.section("resource")
    @docs.maturity("unstable")
    @click.group()
    def contacts():
        '''Google Contacts operations.'''
"""

import click


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


# =============================================================================
# Manual page rendering
# =============================================================================


def _collect_commands(
    cmd: click.Command, prefix: str = "", override_name: str | None = None
) -> list[tuple[str, str, list, list]]:
    """Collect all commands as (full_name, help, arguments, options) tuples."""
    results = []
    cmd_name = override_name if override_name else cmd.name
    name = f"{prefix} {cmd_name}".strip() if prefix else cmd_name

    if isinstance(cmd, click.Group):
        for subcmd_name in sorted(cmd.list_commands(None)):
            subcmd = cmd.get_command(None, subcmd_name)
            if subcmd:
                results.extend(_collect_commands(subcmd, name, subcmd_name))
    else:
        help_text = (cmd.help or "").split("\n")[0]
        arguments = []
        options = []
        for param in cmd.params:
            if isinstance(param, click.Argument):
                arg_name = param.name.upper()
                if param.default is not None:
                    arguments.append(f"[{arg_name}]")
                elif param.required:
                    arguments.append(arg_name)
                else:
                    arguments.append(f"[{arg_name}]")
            elif isinstance(param, click.Option) and param.help:
                opts = ", ".join(param.opts)
                options.append((opts, param.help))
        results.append((name, help_text, arguments, options))

    return results


def format_man_plain(
    sections: list[tuple[str, dict[str, tuple[str | None, list]]]],
) -> str:
    """Format manual as plain text."""
    lines = ["GAX(1)", "", "NAME", "    gax - Google Access CLI", ""]
    lines.append("COMMANDS")

    for section_title, groups in sections:
        lines.append(f"\n  {section_title}:")
        for group_name, (maturity_level, commands) in groups.items():
            label = f"{group_name} [{maturity_level}]" if maturity_level else group_name
            lines.append(f"\n    {label}:")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                if args_str:
                    lines.append(f"      gax {full_name} {args_str}")
                else:
                    lines.append(f"      gax {full_name}")
                if help_text:
                    lines.append(f"          {help_text}")
                for opt, opt_help in options:
                    lines.append(f"          {opt}: {opt_help}")

    lines.extend(_file_section_plain())
    return "\n".join(lines)


def format_man_md(
    sections: list[tuple[str, dict[str, tuple[str | None, list]]]],
) -> str:
    """Format manual as Markdown (suitable for pandoc conversion to man page)."""
    lines = [
        "---",
        "title: GAX",
        "section: 1",
        "header: User Manual",
        "footer: gax",
        "---",
        "",
        "# NAME",
        "",
        "gax - Google Access CLI",
        "",
        "# SYNOPSIS",
        "",
        "**gax** *command* [*options*] [*args*]",
        "",
        "# DESCRIPTION",
        "",
        "Sync Google Workspace (Sheets, Docs, Gmail, Calendar) to local files "
        "that are human-readable, machine-readable, and git-friendly.",
        "",
        "# COMMANDS",
    ]

    for section_title, groups in sections:
        lines.append("")
        lines.append(f"## {section_title}")

        for group_name, (maturity_level, commands) in groups.items():
            lines.append("")
            label = f"{group_name} [{maturity_level}]" if maturity_level else group_name
            lines.append(f"### {label}")
            lines.append("")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                cmd = f"**gax {full_name}**"
                if args_str:
                    cmd += f" *{args_str}*"
                lines.append(cmd)
                if help_text:
                    lines.append(f":   {help_text}")
                for opt, opt_help in options:
                    lines.append(f"    **{opt}**: {opt_help}")
                lines.append("")

    lines.extend(_file_section_md())
    return "\n".join(lines)


def _file_section_plain() -> list[str]:
    return [
        "",
        "FILES",
        "    .sheet.gax.md         Spreadsheet data",
        "    .doc.gax.md           Document",
        "    .tab.gax.md           Single document tab",
        "    .mail.gax.md          Email thread",
        "    .draft.gax.md         Email draft",
        "    .cal.gax.md           Calendar event",
        "    .form.gax.md          Google Form definition",
        "    .gax.md               Mail list (TSV with YAML header)",
        "    .label.mail.gax.md    Gmail labels state",
        "    .filter.mail.gax.md   Gmail filters state",
        "",
        "    ~/.config/gax/credentials.json    OAuth credentials",
        "    ~/.config/gax/token.json          Access token",
        "",
        "SEE ALSO",
        "    gax <command> --help",
    ]


def _file_section_md() -> list[str]:
    return [
        "# FILES",
        "",
        "| Extension | Description |",
        "|-----------|-------------|",
        "| .sheet.gax.md | Spreadsheet data |",
        "| .doc.gax.md | Document |",
        "| .tab.gax.md | Single document tab |",
        "| .mail.gax.md | Email thread |",
        "| .draft.gax.md | Email draft |",
        "| .cal.gax.md | Calendar event |",
        "| .form.gax.md | Google Form definition |",
        "| .gax.md | Mail list (TSV with YAML header) |",
        "| .label.mail.gax.md | Gmail labels state |",
        "| .filter.mail.gax.md | Gmail filters state |",
        "",
        "| Path | Description |",
        "|------|-------------|",
        "| ~/.config/gax/credentials.json | OAuth credentials |",
        "| ~/.config/gax/token.json | Access token |",
        "",
        "# SEE ALSO",
        "",
        "**gax** *command* **--help**",
    ]
