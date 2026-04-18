"""Resource type abstraction for gax.

One base class defines the standard operations every resource supports:

  clone(url)   — fetch remote → local file (or directory)
  pull(path)   — refresh local from remote
  diff(path)   — preview changes (returns string or None)
  push(path)   — push local to remote (unconditional)

Subclasses add custom operations as plain methods (e.g. list, show).
No need to declare these in the base class — they are resource-specific.
Whether the resource is a single file, a directory, or a collection
serialized into one file is an implementation detail, not an interface
concern.

## Conventions

Status messages: use `logging.getLogger(__name__)`.
    logger.info("Fetching tab: Revenue")   # shown in spinner during operation()
    logger.debug("Skipping empty row")     # silent unless verbose

Errors: standard Python exceptions.
    ValueError          — bad input the user can fix (wrong URL, missing field)
    NotImplementedError — operation not available on this resource
    RuntimeError        — internal bug, should not happen

The CLI layer catches and formats these — resource code should
not call sys.exit() or print errors directly.

See draft.py for a reference implementation with design rationale.
"""

from pathlib import Path


class Resource:
    """Base class for all gax resources.

    Standard ops: clone, pull, diff, push.
    Subclasses override the operations they support.
    Unimplemented operations raise NotImplementedError.
    """

    name: str

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Fetch remote → local file/directory. Returns path created."""
        raise NotImplementedError(f"{self.name} does not support clone")

    def pull(self, path: Path, **kw) -> None:
        """Refresh local from remote."""
        raise NotImplementedError(f"{self.name} does not support pull")

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local and remote.

        Returns a human-readable diff string, or None if no changes.
        Used by cli.py to display changes before push.
        """
        raise NotImplementedError(f"{self.name} does not support diff")

    def push(self, path: Path, **kw) -> None:
        """Push local to remote. Unconditional — caller handles confirmation."""
        raise NotImplementedError(f"{self.name} does not support push")
