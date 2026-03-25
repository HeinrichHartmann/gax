# ADR 016: Resource Abstraction

## Status

**DRAFT - NOT IMPLEMENTED**

This ADR captures a design discussion. The abstraction has not been built yet.

## Context

The gax CLI has grown to support multiple Google resource types:
- Google Docs, Sheets, Forms
- Gmail threads, drafts
- Calendar events
- Gmail filters, labels (account-level state)

Each resource type implements similar operations (clone, pull, plan, apply) with copy-pasted patterns. This leads to:

1. **Inconsistent behavior**: Some `apply` commands prompt for confirmation, others don't
2. **Code duplication**: Each resource re-implements clone/pull/push workflows
3. **Hard to add features**: Adding `gax show <url>` requires touching every module
4. **No unified interface**: The `gax clone` dispatcher uses string matching and ctx.invoke

### Current Pattern (repeated per resource)

```python
# In form.py, doc.py, mail.py, etc.
def clone(url, output):
    id = extract_form_id(url)           # varies
    data = get_form(id)                  # varies
    content = form_to_markdown(data)     # varies
    if output.exists(): error()          # duplicated
    output.write_text(content)           # duplicated

def pull(file):
    header = parse_file(file)            # duplicated
    id = extract_id(header["source"])    # varies
    data = fetch(id)                     # varies
    content = format(data)               # varies
    file.write_text(content)             # duplicated

def apply(plan_file):
    plan = read_plan(plan_file)          # varies
    show_plan(plan)                      # varies
    # INCONSISTENT: some confirm, some don't
    execute(plan)                        # varies
```

## Decision

### HTTP Handler Analogy

Model resources like HTTP handlers:
- **Request**: URL or parsed file header (the "input")
- **Response**: List of sections (the "output")
- **Framework**: Routing, serialization, middleware (confirmation flows)

The resource doesn't know about file I/O. It receives parsed input and returns structured output. The framework handles serialization to multipart format.

### Section: The Common Currency

All resources return `list[Section]` - even single-section resources. This unifies the serialization layer.

```python
@dataclass
class Section:
    """One section of a gax document."""
    headers: dict[str, Any]   # YAML header fields
    body: str                 # Content (markdown, yaml, etc.)
```

A form returns one section. A doc returns multiple sections (one per tab). The framework serializes both the same way.

### Resource Protocol

```python
from typing import Protocol, Any
from dataclasses import dataclass

@dataclass
class Plan:
    """Changes to apply to a resource."""
    resource_id: str
    changes: dict[str, Any]   # Resource-specific change structure

    def is_empty(self) -> bool:
        return not any(self.changes.values())

class Resource(Protocol):
    """Protocol for gax resources."""

    # Identity
    name: str              # "form", "doc", "mail"
    type_id: str           # "gax/form", "gax/doc"
    url_pattern: str       # regex for URL matching
    file_suffix: str       # ".form.gax", ".doc.gax"

    def extract_id(self, url: str) -> str:
        """Extract resource ID from URL."""
        ...

    def fetch(self, id: str) -> list[Section]:
        """Fetch resource from API, return as sections."""
        ...

    def default_filename(self, sections: list[Section]) -> str:
        """Generate default filename from fetched data."""
        ...

class MutableResource(Resource, Protocol):
    """Resource that supports modifications."""

    def diff(self, local: list[Section], remote: list[Section]) -> Plan:
        """Compare local vs remote, return changes."""
        ...

    def apply(self, plan: Plan) -> None:
        """Apply changes to remote."""
        ...

    def format_plan(self, plan: Plan) -> str:
        """Format plan for display."""
        ...
```

### Framework: Unified Workflows

The framework provides the CLI commands. Resources just plug in.

```python
# gax/framework.py

def show(url: str, resource: Resource) -> None:
    """Fetch and display (no file created)."""
    id = resource.extract_id(url)
    sections = resource.fetch(id)
    click.echo(serialize_multipart(sections))

def clone(url: str, resource: Resource, output: Path | None) -> Path:
    """Clone to local file."""
    id = resource.extract_id(url)
    sections = resource.fetch(id)

    path = output or Path(resource.default_filename(sections))
    if path.exists():
        raise FileExistsError(f"File exists: {path}")

    path.write_text(serialize_multipart(sections))
    return path

def pull(file: Path, resource: Resource) -> None:
    """Update local file from remote."""
    sections = parse_multipart(file.read_text())
    header = sections[0].headers

    id = header.get("id") or resource.extract_id(header["source"])
    new_sections = resource.fetch(id)

    file.write_text(serialize_multipart(new_sections))

def push(file: Path, resource: MutableResource) -> None:
    """Plan, confirm, and apply changes."""
    local = parse_multipart(file.read_text())
    header = local[0].headers

    id = header.get("id") or resource.extract_id(header["source"])
    remote = resource.fetch(id)

    plan = resource.diff(local, remote)

    if plan.is_empty():
        click.echo("No changes.")
        return

    # Display plan
    click.echo(resource.format_plan(plan))

    # ALWAYS confirm - enforced by framework
    if not click.confirm("Apply these changes?"):
        click.echo("Aborted.")
        return

    resource.apply(plan)
    click.echo("Applied.")
```

### Resource Registry

Simple dict mapping URL patterns to resources:

```python
# gax/registry.py

RESOURCES: dict[str, Resource] = {}

def register(resource: Resource) -> None:
    RESOURCES[resource.url_pattern] = resource

def match(url: str) -> Resource | None:
    for pattern, resource in RESOURCES.items():
        if re.search(pattern, url):
            return resource
    return None
```

### CLI Commands

Unified commands that dispatch to framework:

```python
# gax/cli.py

@main.command()
@click.argument("url")
def show(url: str):
    """Fetch and display a Google resource."""
    resource = registry.match(url)
    if not resource:
        click.echo(f"Unrecognized URL: {url}", err=True)
        sys.exit(1)
    framework.show(url, resource)

@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path))
def clone(url: str, output: Path | None):
    """Clone a Google resource from URL."""
    resource = registry.match(url)
    if not resource:
        click.echo(f"Unrecognized URL: {url}", err=True)
        sys.exit(1)
    path = framework.clone(url, resource, output)
    click.echo(f"Created: {path}")
```

### Example: FormResource

```python
# gax/resources/form.py

class FormResource:
    name = "form"
    type_id = "gax/form"
    url_pattern = r"docs\.google\.com/forms/d/"
    file_suffix = ".form.gax"

    def extract_id(self, url: str) -> str:
        match = re.search(r"/forms/d/([a-zA-Z0-9-_]+)", url)
        return match.group(1) if match else url

    def fetch(self, id: str) -> list[Section]:
        service = get_forms_service()
        data = service.forms().get(formId=id).execute()

        source_url = f"https://docs.google.com/forms/d/{id}/edit"
        content = form_to_yaml(data, source_url)

        return [Section(
            headers={
                "type": "gax/form",
                "id": id,
                "source": source_url,
                "title": data["info"]["title"],
            },
            body=content,
        )]

    def default_filename(self, sections: list[Section]) -> str:
        title = sections[0].headers.get("title", "form")
        safe = re.sub(r'[<>:"/\\|?*\s]+', "_", title)
        return f"{safe}.form.gax"

    def diff(self, local: list[Section], remote: list[Section]) -> Plan:
        # Extract items from YAML body, compare, return plan
        local_items = yaml.safe_load(local[0].body).get("items", [])
        remote_data = self.fetch(...)  # or parse remote sections
        return compute_form_plan(local_items, remote_data)

    def apply(self, plan: Plan) -> None:
        service = get_forms_service()
        requests = build_batch_requests(plan.changes)
        service.forms().batchUpdate(formId=plan.resource_id, body={"requests": requests}).execute()

    def format_plan(self, plan: Plan) -> str:
        lines = ["Plan:"]
        for op, items in plan.changes.items():
            if items:
                lines.append(f"  {op}: {len(items)}")
        return "\n".join(lines)

# Register
registry.register(FormResource())
```

## What's Excluded

### Account-Level State

Filters, labels, and bulk operations (mail list, cal list) don't fit this pattern:
- Not URL-addressable
- Clone without URL (account state)
- Different workflows

These remain as separate modules with their own commands.

### Format Options

The `--format md|yaml` option is resource-specific. Resources that support multiple formats handle this internally in `fetch()`, possibly accepting a format parameter.

## Migration Strategy

### Phase 1: Define Protocol

Create `gax/resource.py` with Section, Plan, Resource, MutableResource. No implementation changes yet.

### Phase 2: Implement FormResource

Form is the most complete resource (has plan/apply). Extract existing code into FormResource class. Prove the abstraction works.

### Phase 3: Wire Framework

Implement `gax/framework.py` with show/clone/pull/push. Wire up CLI commands for Form only.

### Phase 4: Migrate Others

One resource at a time:
- DocResource (multi-section)
- SheetResource (multi-section)
- MailResource (multi-section)
- DraftResource
- CalEventResource

Keep existing commands working during migration. Remove old code only when new implementation is proven.

### Phase 5: Unify CLI

Once all resources migrated:
- `gax show <url>` works for everything
- `gax clone <url>` uses registry
- `gax pull <file>` detects type from header
- `gax push <file>` detects type, always confirms

## Consequences

### Positive

- **Consistency enforced**: Confirmation flows, error handling in one place
- **Easy to add resources**: Implement protocol, register, done
- **`gax show` for free**: Once framework exists, show works for all resources
- **Testable**: Framework and resources can be tested independently
- **Single dispatch mechanism**: No more string matching in clone()

### Negative

- **Upfront refactoring**: Migrating existing resources takes time
- **Another abstraction layer**: More indirection in the code
- **Some things won't fit**: Account-level state remains separate

### Neutral

- **Same total code**: Not less code, just better organized
- **Learning curve**: Contributors need to understand the protocol

## Alternatives Considered

### 1. Keep Current Approach

Continue with per-resource implementations, fix inconsistencies manually.

**Rejected**: Whack-a-mole. Each new feature requires touching all modules.

### 2. Code Generation

Generate clone/pull/push from resource definitions.

**Rejected**: Over-engineering. Protocol + base class is simpler.

### 3. Mixin Classes

Use mixins for shared behavior.

**Rejected**: Mixins are harder to reason about than composition.

## References

- ADR 012: Unified Pull Command
- ADR 015: Unified Clone Command
