"""Resource type abstractions for gax.

Two base classes define the standard operations:

- ResourceItem: a single resource on disk (clone, pull, push)
- ResourceGroup: a collection that checks out to a directory
  (checkout, pull, push, plan, apply)

Subclasses add custom operations as plain methods.

## Conventions

Status messages: use `logging.getLogger(__name__)`.
    logger.info("Fetching tab: Revenue")   # shown in spinner during operation()
    logger.debug("Skipping empty row")     # silent unless verbose

Output for the user: use `gax.ui`.
    ui.success("Created: report.draft.gax.md")
    ui.warning("Tab has unsupported features")

Errors: we use the following standard Python exceptions to flag failures:
    ValueError          — bad input the user can fix (wrong URL, missing field)
    NotImplementedError — operation not available on this resource
    RuntimeError        — internal bug, should not happen

The CLI layer catches and formats these — resource code should
not call sys.exit() or print errors directly.
"""

from pathlib import Path


class ResourceItem:
    """A single resource on disk (tab, event, draft, file, ...).

    Standard ops: clone, pull, push.
    Subclasses override the operations they support.
    Unimplemented operations raise NotImplementedError.
    """

    name: str

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Fetch remote item → local file. Returns path created."""
        raise NotImplementedError(f"{self.name} does not support clone")

    def pull(self, path: Path, **kw) -> None:
        """Refresh local file from remote."""
        raise NotImplementedError(f"{self.name} does not support pull")

    def push(self, path: Path, yes: bool = False, **kw) -> None:
        """Push local file to remote."""
        raise NotImplementedError(f"{self.name} does not support push")


class ResourceGroup:
    """A collection that checks out to a directory (sheet, doc, cal, ...).

    Standard ops: checkout, pull, push, plan, apply.
    Has an entry type (ResourceItem) for individual items.
    Subclasses override the operations they support.
    Unimplemented operations raise NotImplementedError.
    """

    name: str
    entry: ResourceItem

    def checkout(self, url: str, output: Path | None = None, **kw) -> Path:
        """Fetch remote collection → local directory. Returns path created."""
        raise NotImplementedError(f"{self.name} does not support checkout")

    def pull(self, path: Path, **kw) -> None:
        """Refresh local directory from remote."""
        raise NotImplementedError(f"{self.name} does not support pull")

    def push(self, path: Path, yes: bool = False, **kw) -> None:
        """Push local directory to remote."""
        raise NotImplementedError(f"{self.name} does not support push")

    def plan(self, path: Path, **kw) -> Path | None:
        """Preview changes. Returns plan file path, or None."""
        raise NotImplementedError(f"{self.name} does not support plan")

    def apply(self, plan_path: Path, yes: bool = False, **kw) -> None:
        """Apply a plan."""
        raise NotImplementedError(f"{self.name} does not support apply")
