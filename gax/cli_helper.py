"""Dispatch helpers for gax CLI.

All dispatch goes through Resource.from_url / Resource.from_file.
These thin wrappers add CLI concerns (confirmation, error formatting).
"""

import click
from pathlib import Path


def _pull_folder(
    folder_path: Path, verbose: bool = False, yes: bool = False
) -> tuple[bool, str]:
    """Pull a .gax.d folder. Returns (success, message)."""
    from .resource import Resource

    try:
        r = Resource.from_file(folder_path)
        r.pull()
        return True, "updated"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _push_file(
    file_path: Path, yes: bool = False, with_formulas: bool = False
) -> tuple[bool, str]:
    """Push a single .gax.md file. Returns (success, message)."""
    from .resource import Resource

    try:
        r = Resource.from_file(file_path)
    except ValueError:
        return False, f"Unsupported file: {file_path}"

    try:
        diff_text = r.diff()
    except NotImplementedError:
        return False, f"Push not supported for: {file_path}"

    if diff_text is None:
        return True, "no changes"
    if not yes:
        click.echo(diff_text)
        if not click.confirm("Push these changes?"):
            return False, "cancelled"

    try:
        r.push(with_formulas=with_formulas)
        return True, "pushed"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _push_folder(
    folder_path: Path, yes: bool = False, with_formulas: bool = False
) -> tuple[bool, str]:
    """Push a .gax.d folder. Returns (success, message)."""
    from .resource import Resource

    try:
        r = Resource.from_file(folder_path)
    except ValueError:
        return False, f"Unsupported folder: {folder_path}"

    try:
        diff_text = r.diff()
    except NotImplementedError:
        return False, f"Push not supported for: {folder_path}"

    if diff_text is None:
        return True, "no changes"
    if not yes:
        click.echo("\n" + diff_text)
        if not click.confirm("\nPush these changes?"):
            return False, "cancelled"

    try:
        r.push(with_formulas=with_formulas)
        return True, "pushed"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _pull_file(file_path: Path, verbose: bool = False) -> tuple[bool, str]:
    """Pull a single .gax.md file. Returns (success, message)."""
    from .resource import Resource

    try:
        r = Resource.from_file(file_path)
        r.pull()
        return True, "updated"
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
