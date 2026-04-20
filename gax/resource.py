"""Resource type abstraction for gax.

One base class defines the standard operations every resource supports:

  clone()    — fetch remote → local file (or directory)
  checkout() — fetch remote → local folder/collection representation
  pull()     — refresh local from remote
  diff()     — preview changes (returns string or None)
  push()     — push local to remote (unconditional)

Resources are constructed from a URL or file path:

  Resource.from_url(url)   — dispatch: tries each subclass, returns first match
  Resource.from_file(path) — dispatch: tries each subclass, returns first match
  Tab.from_url(url)        — construct a specific resource (or raise ValueError)

Subclasses declare dispatch metadata as class attributes:

  URL_PATTERN     — regex for URL matching (from_url)
  FILE_TYPE       — YAML header type string (from_file)
  FILE_EXTENSIONS — filename suffixes (from_file)
  CHECKOUT_TYPE   — type in .gax.yaml for checkout directories (from_file)

Override from_url/from_file only for non-standard matching logic.

See draft.py for a reference implementation with design rationale.
"""

import re
from pathlib import Path

from . import gaxfile


def _read_file_type(path: Path) -> str | None:
    """Read the type field from a gax file's YAML header."""
    return gaxfile.read_type(path)


def _read_checkout_url(gax_yaml_path: Path) -> str:
    """Read the url field from a .gax.yaml checkout metadata file."""
    try:
        content = gax_yaml_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in content.split("\n"):
        if line.startswith("url:"):
            return line.split(":", 1)[1].strip()
    return ""


class Resource:
    """Base class for all gax resources.

    Construction: from_url(url) or from_file(path).
    Operations: clone, checkout, pull, diff, push — all use instance state.
    Unimplemented operations raise NotImplementedError.

    Subclasses are auto-registered via __init_subclass__ and
    discovered by Resource.from_url() / Resource.from_file().

    Dispatch metadata (set on subclasses):
        URL_PATTERN     — regex string for URL matching
        FILE_TYPE       — e.g. "gax/doc", matched against YAML type field
        FILE_EXTENSIONS — e.g. (".doc.gax.md", ".tab.gax.md")
        CHECKOUT_TYPE   — e.g. "gax/doc-checkout", matched in .gax.yaml
    """

    name: str

    URL_PATTERN: str | None = None
    FILE_TYPE: str | None = None
    FILE_EXTENSIONS: tuple[str, ...] = ()
    CHECKOUT_TYPE: str | None = None
    HAS_GENERIC_DISPATCH: bool = True

    _subclasses: list[type["Resource"]] = []

    def __init__(self, *, url: str = "", path: Path | None = None):
        self.url = url
        self.path = path or Path()

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        Resource._subclasses.append(cls)

    @classmethod
    def from_url(cls, url: str) -> "Resource":
        """Construct a resource from a URL.

        When called on Resource (base class): dispatches across all
        subclasses, returning the first that can handle the URL.

        When called on Resource (base class), only real URLs participate in
        generic dispatch. Raw IDs must go through explicit subclass
        constructors such as Draft.from_id(id).

        When called on a subclass: matches URL_PATTERN and constructs, or
        raises ValueError. Override for custom URL matching.
        """
        if cls is Resource:
            if "://" not in url:
                raise ValueError(
                    f"Resource.from_url requires a URL, got: {url!r}. "
                    "Use an explicit resource class for raw IDs."
                )
            for sub in Resource._subclasses:
                if not sub.HAS_GENERIC_DISPATCH:
                    continue
                try:
                    return sub.from_url(url)
                except ValueError:
                    continue
            raise ValueError(
                f"Unrecognized URL: {url}\n"
                "Supported: Google Docs/Sheets/Forms/Slides, Gmail, Calendar"
            )
        if cls.URL_PATTERN and re.search(cls.URL_PATTERN, url):
            return cls(url=url)
        raise ValueError(f"{cls.__name__} does not handle URL: {url}")

    @classmethod
    def from_id(cls, id_value: str) -> "Resource":
        """Construct a resource from an explicit resource-specific ID."""
        raise ValueError(f"{cls.__name__} does not support ID-based construction")

    @classmethod
    def from_url_or_id(cls, value: str) -> "Resource":
        """Try from_url first, fall back to from_id."""
        try:
            return cls.from_url(value)
        except ValueError:
            return cls.from_id(value)

    @classmethod
    def from_file(cls, path: Path) -> "Resource":
        """Construct a resource from a local file or directory.

        When called on Resource (base class): dispatches across all
        subclasses, returning the first that can handle the file.

        When called on a subclass: checks checkout metadata, FILE_TYPE
        against YAML header, and FILE_EXTENSIONS against filename.
        Override for custom matching.
        """
        if cls is Resource:
            for sub in Resource._subclasses:
                try:
                    return sub.from_file(path)
                except (ValueError, OSError):
                    continue
            raise ValueError(f"Unknown file type: {path}")

        if path.is_dir():
            if cls.CHECKOUT_TYPE:
                gax_yaml = path / ".gax.yaml"
                checkout_type = _read_file_type(gax_yaml)
                if checkout_type == cls.CHECKOUT_TYPE:
                    url = _read_checkout_url(gax_yaml)
                    return cls(path=path, url=url)
            raise ValueError(f"{cls.__name__} does not handle file: {path}")

        name = path.name.lower()
        file_type = _read_file_type(path) if cls.FILE_TYPE else None

        # Check file extension
        if cls.FILE_EXTENSIONS:
            for ext in cls.FILE_EXTENSIONS:
                if name.endswith(ext):
                    if cls.FILE_TYPE and file_type not in (None, cls.FILE_TYPE):
                        break
                    return cls(path=path)

        # Check YAML type field
        if cls.FILE_TYPE:
            if file_type == cls.FILE_TYPE:
                return cls(path=path)

        raise ValueError(f"{cls.__name__} does not handle file: {path}")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Fetch remote → local file/directory. Returns path created."""
        raise NotImplementedError(f"{self.name} does not support clone")

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Fetch remote → local checkout/folder representation."""
        raise NotImplementedError(f"{self.name} does not support checkout")

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
