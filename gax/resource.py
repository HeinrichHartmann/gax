"""Resource type abstraction for gax.

One base class defines the standard operations every resource supports:

  clone()    — fetch remote → local file (or directory)
  pull()     — refresh local from remote
  diff()     — preview changes (returns string or None)
  push()     — push local to remote (unconditional)

Resources are constructed from a URL or file path:

  Resource.from_url(url)   — dispatch: tries each subclass, returns first match
  Resource.from_file(path) — dispatch: tries each subclass, returns first match
  Tab.from_url(url)        — construct a specific resource (or raise ValueError)

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

    Construction: from_url(url) or from_file(path).
    Operations: clone, pull, diff, push — all use instance state.
    Unimplemented operations raise NotImplementedError.

    Subclasses are auto-registered via __init_subclass__ and
    discovered by Resource.from_url() / Resource.from_file().
    """

    name: str

    _subclasses: list[type["Resource"]] = []

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        Resource._subclasses.append(cls)

    @classmethod
    def from_url(cls, url: str) -> "Resource":
        """Construct a resource from a URL.

        When called on Resource (base class): dispatches across all
        subclasses, returning the first that can handle the URL.

        When called on a subclass: validates and constructs that
        specific resource, or raises ValueError.
        """
        if cls is Resource:
            for sub in Resource._subclasses:
                try:
                    return sub.from_url(url)
                except ValueError:
                    continue
            raise ValueError(
                f"Unrecognized URL: {url}\n"
                "Supported: Google Docs/Sheets/Forms/Slides, Gmail, Calendar"
            )
        raise ValueError(f"{cls.__name__} does not support from_url")

    @classmethod
    def from_file(cls, path: Path) -> "Resource":
        """Construct a resource from a local file or directory.

        When called on Resource (base class): dispatches across all
        subclasses, returning the first that can handle the file.

        When called on a subclass: validates and constructs that
        specific resource, or raises ValueError.
        """
        if cls is Resource:
            for sub in Resource._subclasses:
                try:
                    return sub.from_file(path)
                except (ValueError, OSError):
                    continue
            raise ValueError(f"Unknown file type: {path}")
        raise ValueError(f"{cls.__name__} does not support from_file")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Fetch remote → local file/directory. Returns path created."""
        raise NotImplementedError(f"{self.name} does not support clone")

    def pull(self, **kw) -> None:
        """Refresh local from remote."""
        raise NotImplementedError(f"{self.name} does not support pull")

    def diff(self, **kw) -> str | None:
        """Preview changes between local and remote.

        Returns a human-readable diff string, or None if no changes.
        Used by cli.py to display changes before push.
        """
        raise NotImplementedError(f"{self.name} does not support diff")

    def push(self, **kw) -> None:
        """Push local to remote. Unconditional — caller handles confirmation."""
        raise NotImplementedError(f"{self.name} does not support push")
