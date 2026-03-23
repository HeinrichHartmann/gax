# Improvement Recommendations

Ranked improvements across multiple dimensions, prioritized by impact and effort.

## Priority Matrix

| Priority | Impact | Effort | Items |
|----------|--------|--------|-------|
| P0 | High | Low | Quick wins, do now |
| P1 | High | Medium | Next sprint |
| P2 | Medium | Medium | Planned work |
| P3 | Low | Any | Nice to have |

---

## 1. Code Organization

### P0: Split mail.py (High Impact, Medium Effort)

**Current:** `mail.py` is 1739 lines handling threads, lists, relabeling, and TSV parsing.

**Proposed:**
```
gax/mail/
├── __init__.py      # Public API: from .thread import pull_thread, etc.
├── thread.py        # Thread clone/pull (~400 lines)
├── list.py          # List operations, TSV state (~500 lines)
├── relabel.py       # Plan/apply for labels (~300 lines)
├── section.py       # MailSection dataclass, conversions (~200 lines)
└── abbrev.py        # System label abbreviations (~100 lines)
```

**Benefits:**
- Easier navigation
- Focused test files per module
- Parallel development possible

**Effort:** 2-3 hours (mostly mechanical moves)

---

### P1: Standardize Service Client Pattern (Medium Impact, Medium Effort)

**Current:** Inconsistent patterns across modules:
```python
# Pattern A: Inline (mail.py, gdoc.py, label.py, filter.py)
creds = get_authenticated_credentials()
service = build("gmail", "v1", credentials=creds)

# Pattern B: Client class (gsheet/client.py)
class GSheetClient:
    def __init__(self, gc=None):
        self._gc = gc
```

**Proposed:** Service factory with lazy initialization:
```python
# gax/services.py
class Services:
    _gmail = None
    _docs = None
    _drive = None
    _calendar = None

    @classmethod
    def gmail(cls, service=None):
        if service:
            return service  # For testing
        if cls._gmail is None:
            creds = get_authenticated_credentials()
            cls._gmail = build("gmail", "v1", credentials=creds)
        return cls._gmail

# Usage in modules
def pull_thread(thread_id: str, *, service=None):
    service = Services.gmail(service)
    ...
```

**Benefits:**
- Consistent testability
- Single credential fetch per service
- Clear injection point for mocks

**Effort:** 4-6 hours

---

### P2: Extract Shared Utilities (Low Impact, Low Effort)

**Current:** Duplicated code across modules:
- URL/ID extraction (sheets, docs, mail, calendar)
- TSV parsing
- Safe filename generation
- Timestamp formatting

**Proposed:**
```python
# gax/utils.py
def extract_id(url: str, pattern: str) -> str: ...
def safe_filename(title: str) -> str: ...
def iso_timestamp() -> str: ...

# gax/formats/tsv.py (move from mail.py)
def parse_tsv_line(line: str) -> list[str]: ...
```

**Effort:** 2 hours

---

## 2. Testing

### P0: Add Label/Filter Plan Tests (High Impact, Low Effort)

**Current:** No unit tests for `label.py` or `filter.py` plan/diff logic.

**Proposed:** Test the diff computation:
```python
# tests/test_label.py
class TestLabelPlan:
    def test_detect_create(self):
        current = {"Inbox": {...}}
        desired = [{"name": "Inbox"}, {"name": "NewLabel"}]
        plan = compute_plan(current, desired)
        assert len(plan["create"]) == 1
        assert plan["create"][0]["name"] == "NewLabel"

    def test_detect_rename(self):
        current = {"OldName": {"id": "123"}}
        desired = [{"name": "NewName", "rename_from": "OldName"}]
        plan = compute_plan(current, desired)
        assert plan["rename"][0]["from"] == "OldName"
        assert plan["rename"][0]["to"] == "NewName"
```

**Effort:** 2-3 hours

---

### P1: Add Calendar/Draft Tests (Medium Impact, Medium Effort)

**Current:** `gcal.py` (817 lines) and `draft.py` (679 lines) have zero test coverage.

**Proposed:**
```
tests/
├── test_gcal.py     # Event parsing, format round-trips
└── test_draft.py    # Draft creation, MIME building
```

**Effort:** 4-6 hours

---

### P1: Add CLI Tests with CliRunner (Medium Impact, Medium Effort)

**Current:** No tests for Click command parsing or output formatting.

**Proposed:**
```python
from click.testing import CliRunner
from gax.cli import main

def test_pull_detects_file_type(tmp_path):
    # Create a .sheet.gax file
    file = tmp_path / "test.sheet.gax"
    file.write_text("---\ntype: gax/sheet\n---\ndata")

    runner = CliRunner()
    result = runner.invoke(main, ["pull", str(file)])

    assert result.exit_code == 0
    assert "Pulling sheet" in result.output
```

**Benefits:**
- Catch argument parsing regressions
- Test help text
- Verify error messages

**Effort:** 4 hours

---

### P2: Add Error Handling Tests (Medium Impact, Medium Effort)

**Current:** No tests for error paths (malformed files, API errors, auth failures).

**Proposed:**
```python
def test_malformed_yaml_header():
    content = "---\ninvalid: yaml: here\n---\nbody"
    with pytest.raises(ParseError):
        parse_multipart(content)

def test_api_rate_limit(mock_service):
    mock_service.side_effect = HttpError(resp={'status': 429}, content=b'')
    with pytest.raises(RateLimitError):
        pull_thread("id", service=mock_service)
```

**Effort:** 3-4 hours

---

## 3. Reliability

### P0: Add Retry with Exponential Backoff (High Impact, Low Effort)

**Current:** No retry logic. API rate limits cause immediate failures.

**Proposed:**
```python
# gax/retry.py
import time
from functools import wraps

def with_retry(max_attempts=3, base_delay=1.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except HttpError as e:
                    if e.resp.status == 429 and attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        time.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator

# Usage
@with_retry(max_attempts=3)
def pull_thread(thread_id, service=None):
    ...
```

**Effort:** 1-2 hours

---

### P1: Add Input Validation (Medium Impact, Low Effort)

**Current:** Minimal validation of user input. Invalid URLs/IDs cause cryptic API errors.

**Proposed:**
```python
# gax/validation.py
import re

def validate_doc_id(doc_id: str) -> str:
    if not re.fullmatch(r'[a-zA-Z0-9_-]{20,50}', doc_id):
        raise ValueError(f"Invalid document ID format: {doc_id}")
    return doc_id

def validate_email(email: str) -> str:
    if '@' not in email:
        raise ValueError(f"Invalid email format: {email}")
    return email
```

**Effort:** 2 hours

---

### P2: Add Checkpointing for Bulk Operations (Medium Impact, High Effort)

**Current:** Bulk operations (label apply, filter apply, list apply) have no resume mechanism.

**Proposed:**
```python
# gax/checkpoint.py
class Checkpoint:
    def __init__(self, operation_id: str):
        self.path = Path(f"~/.gax/checkpoints/{operation_id}.json").expanduser()

    def save(self, completed: list, pending: list):
        self.path.write_text(json.dumps({
            "completed": completed,
            "pending": pending,
            "timestamp": iso_timestamp(),
        }))

    def load(self) -> tuple[list, list]:
        if not self.path.exists():
            return [], []
        data = json.loads(self.path.read_text())
        return data["completed"], data["pending"]

    def clear(self):
        self.path.unlink(missing_ok=True)
```

**Effort:** 6-8 hours

---

## 4. Developer Experience

### P0: Add --dry-run to All Mutating Commands (High Impact, Low Effort)

**Current:** Only some commands show what would happen without executing.

**Proposed:** Add `--dry-run` flag consistently:
```python
@click.option("--dry-run", is_flag=True, help="Show what would be done without executing")
def push(file: Path, dry_run: bool):
    changes = compute_changes(file)
    if dry_run:
        click.echo("Would push:")
        for change in changes:
            click.echo(f"  {change}")
        return
    # Actually push...
```

**Commands needing --dry-run:**
- `gax doc tab push`
- `gax sheet tab push`
- `gax mail draft push`
- `gax cal event push`
- `gax cal event delete`

**Effort:** 2 hours

---

### P1: Add --verbose/-v Flag (Medium Impact, Low Effort)

**Current:** Limited visibility into what's happening during operations.

**Proposed:**
```python
# gax/cli.py
@click.option("-v", "--verbose", count=True, help="Increase verbosity (-v, -vv)")
@click.pass_context
def main(ctx, verbose):
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

# In commands
if ctx.obj.get("verbose", 0) >= 1:
    click.echo(f"Fetching document {doc_id}...")
if ctx.obj.get("verbose", 0) >= 2:
    click.echo(f"API response: {response}")
```

**Effort:** 2-3 hours

---

### P1: Improve Error Messages (Medium Impact, Medium Effort)

**Current:** Generic error messages:
```
Error: 404
Error: Invalid argument
```

**Proposed:** Contextual, actionable messages:
```
Error: Document not found (ID: abc123)
  - Check that the URL is correct
  - Ensure you have access to this document
  - Try: gax auth status

Error: Invalid spreadsheet URL
  Expected: https://docs.google.com/spreadsheets/d/<ID>/...
  Got: https://example.com/sheet
```

**Effort:** 4 hours

---

### P2: Add Shell Completions (Low Impact, Medium Effort)

**Current:** No tab completion for commands or arguments.

**Proposed:** Use Click's built-in completion:
```python
# gax/cli.py
@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell):
    """Generate shell completion script."""
    import click.shell_completion as sc
    click.echo(sc.get_completion_class(shell)(main, {}, "gax").source())
```

**Effort:** 2 hours

---

## 5. Performance

### P2: Lazy Import Heavy Dependencies (Low Impact, Low Effort)

**Current:** All imports at module load time. `import pandas` adds ~200ms to startup.

**Proposed:** Defer heavy imports:
```python
# Before
import pandas as pd

def read_sheet():
    return pd.DataFrame(data)

# After
def read_sheet():
    import pandas as pd
    return pd.DataFrame(data)
```

**Modules to optimize:**
- `pandas` in formats/csv.py, gsheet/client.py
- `googleapiclient` in service modules

**Effort:** 1 hour

---

### P3: Parallel API Calls for Multi-Tab Operations (Low Impact, High Effort)

**Current:** Tabs fetched sequentially in clone_all/pull_all.

**Proposed:**
```python
import concurrent.futures

def clone_all_parallel(spreadsheet_id, tabs, client):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(client.read, spreadsheet_id, tab): tab
            for tab in tabs
        }
        results = {}
        for future in concurrent.futures.as_completed(futures):
            tab = futures[future]
            results[tab] = future.result()
    return results
```

**Note:** Google APIs have rate limits; parallelism helps but has diminishing returns.

**Effort:** 4-6 hours

---

## 6. Security

### P1: Validate File Paths (Medium Impact, Low Effort)

**Current:** No protection against path traversal in output filenames.

**Proposed:**
```python
def safe_output_path(base_dir: Path, filename: str) -> Path:
    """Ensure output stays within base directory."""
    safe_name = re.sub(r'[<>:"/\\|?*]', "-", filename)
    safe_name = safe_name.lstrip(".")  # No hidden files
    output = (base_dir / safe_name).resolve()

    if not str(output).startswith(str(base_dir.resolve())):
        raise ValueError(f"Path traversal detected: {filename}")

    return output
```

**Effort:** 1 hour

---

### P2: Credential Storage Review (Medium Impact, Low Effort)

**Current:** Token stored in `~/.config/gax/token.json` with default permissions.

**Proposed:**
```python
def save_token(token_path: Path, token_data: dict):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token_data))
    token_path.chmod(0o600)  # User read/write only
```

**Effort:** 30 minutes

---

## Summary by Priority

### P0 (Do Now)
| Improvement | Category | Effort |
|-------------|----------|--------|
| Split mail.py | Organization | 2-3h |
| Add retry with backoff | Reliability | 1-2h |
| Add label/filter plan tests | Testing | 2-3h |
| Add --dry-run everywhere | DX | 2h |

### P1 (Next Sprint)
| Improvement | Category | Effort |
|-------------|----------|--------|
| Standardize service clients | Organization | 4-6h |
| Add calendar/draft tests | Testing | 4-6h |
| Add CLI tests | Testing | 4h |
| Add input validation | Reliability | 2h |
| Add --verbose flag | DX | 2-3h |
| Improve error messages | DX | 4h |
| Validate file paths | Security | 1h |

### P2 (Planned)
| Improvement | Category | Effort |
|-------------|----------|--------|
| Extract shared utilities | Organization | 2h |
| Add error handling tests | Testing | 3-4h |
| Add checkpointing | Reliability | 6-8h |
| Add shell completions | DX | 2h |
| Lazy import dependencies | Performance | 1h |
| Credential storage review | Security | 30m |

### P3 (Nice to Have)
| Improvement | Category | Effort |
|-------------|----------|--------|
| Parallel API calls | Performance | 4-6h |

---

## Effort Totals

| Priority | Total Effort |
|----------|--------------|
| P0 | 7-10 hours |
| P1 | 21-28 hours |
| P2 | 14-18 hours |
| P3 | 4-6 hours |

**Recommendation:** Complete all P0 items (~1 day), then work through P1 over 1-2 weeks.
