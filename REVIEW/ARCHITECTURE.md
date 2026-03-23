# Architecture Review: gax

A top-down review of the actual implementation.

## Overview

**~6,900 lines of Python** across 24 source files implementing a CLI for syncing Google Workspace to local files.

```
gax/
├── cli.py          (593)   # Entry point, unified pull, sheet commands
├── auth.py         (169)   # OAuth2 flow
├── frontmatter.py   (73)   # Simple YAML+body parsing (for sheets)
├── multipart.py    (192)   # Multi-section format (ADR 002)
├── mail.py        (1739)   # Gmail: threads, list, relabel
├── gdoc.py         (931)   # Google Docs
├── gcal.py         (817)   # Calendar
├── draft.py        (679)   # Mail drafts
├── filter.py       (564)   # Gmail filters (IaC)
├── label.py        (498)   # Gmail labels (IaC)
├── native_md.py    (313)   # Drive API markdown export
├── md2docs.py      (222)   # Markdown → Docs API
├── store.py         (94)   # Content-addressable blob store
├── gsheet/
│   ├── client.py    (75)   # gspread wrapper
│   ├── clone.py            # Multi-tab clone
│   ├── pull.py             # Single/multi tab pull
│   └── push.py             # Push to sheet
└── formats/
    ├── __init__.py  (24)   # Format registry
    ├── base.py             # Protocol
    ├── csv.py              # CSV/TSV/PSV
    ├── json.py             # JSON/JSONL
    └── markdown.py         # Markdown tables
```

## Component Map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CLI LAYER (cli.py)                              │
│                                                                              │
│  main ─┬─ auth (login, logout, status)                                       │
│        ├─ sheet (clone, pull) ─── tab (list, clone, pull, push)             │
│        ├─ doc (clone, pull) ───── tab (list, clone, pull, push, import)     │
│        ├─ mail ─┬─ thread (clone, pull, reply)                              │
│        │        ├─ draft (new, clone, list, pull, push)                     │
│        │        ├─ list (clone, pull, plan, apply, checkout)                │
│        │        ├─ label (list, clone, pull, plan, apply)                   │
│        │        └─ filter (list, clone, pull, plan, apply)                  │
│        ├─ cal (list, calendars) ─ event (clone, new, pull, push, delete)    │
│        ├─ pull (unified dispatcher)                                          │
│        └─ man                                                                │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           SERVICE MODULES                                    │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   gdoc.py   │  │  gsheet/*   │  │   mail.py   │  │   gcal.py   │         │
│  │             │  │             │  │             │  │             │         │
│  │ pull_doc()  │  │ GSheetClient│  │ pull_thread │  │ Event       │         │
│  │ DocSection  │  │ clone_all() │  │ MailSection │  │ dataclass   │         │
│  │ Comments    │  │ pull/push   │  │ Attachment  │  │ yaml format │         │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘         │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                          │
│  │  draft.py   │  │  label.py   │  │  filter.py  │                          │
│  │             │  │             │  │             │                          │
│  │ DraftConfig │  │ plan/apply  │  │ plan/apply  │                          │
│  │ push/pull   │  │ IaC pattern │  │ IaC pattern │                          │
│  └─────────────┘  └─────────────┘  └─────────────┘                          │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           FOUNDATION LAYER                                   │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   auth.py   │  │ multipart.py│  │  formats/*  │  │  store.py   │         │
│  │             │  │             │  │             │  │             │         │
│  │ OAuth flow  │  │ Section     │  │ Format      │  │ CAS blobs   │         │
│  │ token mgmt  │  │ parse/format│  │ registry    │  │ for attach  │         │
│  │ credentials │  │ content-len │  │ DataFrame   │  │             │         │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘         │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐                                           │
│  │ native_md.py│  │  md2docs.py │                                           │
│  │             │  │             │                                           │
│  │ Drive export│  │ MD → Docs   │                                           │
│  │ tab split   │  │ API requests│                                           │
│  └─────────────┘  └─────────────┘                                           │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL DEPENDENCIES                                 │
│                                                                              │
│  click          gspread        google-api-python-client    pandas           │
│  (CLI)          (Sheets)       (Docs, Gmail, Calendar)     (DataFrames)     │
│                                                                              │
│  google-auth-oauthlib          pyyaml                                       │
│  (OAuth2)                      (YAML)                                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Key Design Observations

### 1. CLI Structure: Mixed Approaches

The CLI uses two patterns for command groups:

**Pattern A: Separate module with `@click.group()` decorator** (gdoc, mail, gcal)
```python
# In gdoc.py
@click.group()
def doc():
    """Google Docs operations"""
    pass

# In cli.py
main.add_command(doc)
```

**Pattern B: Commands defined in cli.py** (sheet, auth)
```python
# In cli.py
@main.group()
def sheet():
    """Google Sheets operations"""
    pass
```

**Observation:** Pattern A is cleaner for large modules. mail.py (1739 lines) would benefit from being split into submodules like gsheet/.

### 2. Multipart vs Frontmatter: Two Parsers

There are two parsing approaches:

| Module | Format | Used By |
|--------|--------|---------|
| `frontmatter.py` | Simple YAML + body | Sheet single-tab (`.sheet.gax`) |
| `multipart.py` | Multi-section YAML+body | Docs, Mail, multi-tab sheets |

**Observation:** `frontmatter.py` is sheet-specific (`SheetConfig` dataclass). The generic multipart parser handles all multi-section formats. Some duplication exists but serves different purposes.

### 3. Service Module Patterns

Each service module follows a similar structure:

```python
# 1. Dataclasses for domain objects
@dataclass
class MailSection:
    title: str
    thread_id: str
    ...

# 2. Conversion helpers (to/from multipart)
def _mail_section_to_multipart(section: MailSection) -> multipart.Section:
    ...

# 3. API functions (core logic)
def pull_thread(thread_id: str) -> list[MailSection]:
    ...

# 4. CLI commands
@click.command()
def clone(...):
    ...
```

**Observation:** Good separation of concerns. API functions are testable independently of CLI.

### 4. Authentication: Lazy Initialization

```python
# In gsheet/client.py
@property
def gc(self) -> gspread.Client:
    if self._gc is None:
        creds = get_authenticated_credentials()
        self._gc = gspread.authorize(creds)
    return self._gc
```

Most modules call `get_authenticated_credentials()` directly and build services inline. The GSheetClient wraps this for testability.

**Observation:** Inconsistent patterns for auth/service creation. Some modules are easier to test than others.

### 5. Unified Pull: Type Detection

`cli.py:_detect_file_type()` implements file type detection for the unified `gax pull` command:

```python
def _detect_file_type(file_path: Path) -> str | None:
    # 1. Check `type` field in YAML header
    # 2. Infer from header fields (thread_id → mail, spreadsheet_id → sheet)
    # 3. Fallback to file extension (.doc.gax, .mail.gax, etc.)
```

**Observation:** Good defense-in-depth approach. Works with old files lacking `type` field.

### 6. IaC Pattern: Labels, Filters, List

Three modules implement the plan/apply pattern:

| Module | State File | Operations |
|--------|------------|------------|
| `label.py` | `labels.yaml` | create, rename, update, delete |
| `filter.py` | `filters.yaml` | create, update (delete+create), delete |
| `mail.py` (list) | `.gax` (TSV) | add/remove labels on threads |

Each implements:
- `clone/pull` - fetch current state
- `plan` - compute diff, write plan file
- `apply` - execute plan

**Observation:** Clean separation. Could potentially share more code (plan/apply base class) but current duplication is manageable.

### 7. Sheet Submodule: Best-Structured

```
gsheet/
├── __init__.py   # Public API exports
├── client.py     # GSheetClient class
├── clone.py      # Multi-tab operations
├── pull.py       # Single-tab pull
└── push.py       # Single-tab push
```

**Observation:** This is the cleanest module organization. Other large modules (mail.py at 1739 lines) would benefit from similar splitting.

### 8. Format System: Pluggable

```python
# formats/__init__.py
FORMATS: dict[str, Format] = {
    "csv": CSVFormat(),
    "tsv": TSVFormat(),
    "md": MarkdownFormat(),
    ...
}

def get_format(name: str) -> Format:
    return FORMATS[name]
```

Each format implements:
```python
class Format(Protocol):
    def read(self, text: str) -> pd.DataFrame: ...
    def write(self, df: pd.DataFrame) -> str: ...
```

**Observation:** Excellent design. Easy to add new formats.

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `click` | >=8.0 | CLI framework |
| `pandas` | >=2.0 | DataFrame for sheets |
| `gspread` | >=6.0 | Sheets API (higher-level than raw API) |
| `google-api-python-client` | >=2.0 | Docs, Gmail, Calendar, Drive APIs |
| `google-auth-oauthlib` | >=1.0 | OAuth2 flow |
| `pyyaml` | >=6.0 | YAML parsing |

**Dev dependencies:** pytest, ruff, pre-commit

**Observation:** Minimal, focused dependencies. Using gspread for Sheets (convenience) while using raw google-api-python-client for other services (flexibility).

## Testing

### Test Suite Overview

```
tests/
├── test_multipart.py  (415 lines) - Format parsing/serialization
├── test_gsheet.py     (477 lines) - Sheet operations with mocks
├── test_gdoc.py       (~400 lines) - Doc operations with mocks
├── test_mail.py       (228 lines) - Mail parsing
├── test_md2docs.py    (~80 lines) - Markdown conversion
├── test_e2e.py        (600+ lines) - Real API tests (marked @e2e)
└── fixtures/
    ├── sample_thread_response.json  - Gmail API response
    ├── sample_doc_response.json     - Docs API response
    └── e2e_test*.md                 - Test content files
```

### Testing Layers

The test suite implements three distinct layers:

| Layer | Files | Speed | Requires Auth | Purpose |
|-------|-------|-------|---------------|---------|
| Unit | `test_multipart.py` | Fast | No | Pure function tests |
| Integration | `test_gsheet.py`, `test_mail.py`, `test_gdoc.py` | Fast | No | Service modules with mocked APIs |
| E2E | `test_e2e.py` | Slow | Yes | Full CLI against real Google APIs |

### Layer 1: Unit Tests (Pure Functions)

**File:** `test_multipart.py`

Tests the core parsing/formatting logic without any I/O or mocking:

```python
class TestNeedsContentLength:
    """Tests for content-length detection."""

    def test_dashes_in_middle(self):
        """Content with --- in middle needs content-length."""
        assert needs_content_length("Before\n---\nAfter") is True

class TestRoundTrip:
    """Tests for format -> parse round-trip consistency."""

    def test_dangerous_content_roundtrip(self):
        """Content with --- should round-trip correctly."""
        dangerous = "Start\n---\nMiddle\n---\nEnd"
        original = [Section(headers={"title": "Dangerous"}, content=dangerous)]
        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)
        assert parsed[0].content == dangerous
```

**Strengths:**
- Comprehensive edge case coverage (unicode, empty content, trailing newlines)
- Round-trip tests verify format stability
- Tests the `content-length` quoting mechanism thoroughly

**Test Classes:**
- `TestNeedsContentLength` - Detection of ambiguous content
- `TestParseHeader` - YAML header parsing
- `TestFormatSection` - Single section output
- `TestFormatMultipart` - Multi-section output
- `TestParseMultipart` - Parsing including content-length
- `TestRoundTrip` - Format → parse consistency

### Layer 2: Integration Tests (Mocked APIs)

**Files:** `test_gsheet.py`, `test_mail.py`, `test_gdoc.py`

Tests service modules with mock API clients injected via constructor/parameter:

```python
def make_mock_gc(sheet_data: list[list[str]]):
    """Create a mock gspread client that returns the given sheet data."""
    gc = MagicMock()
    worksheet = MagicMock()
    spreadsheet = MagicMock()

    gc.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.return_value = worksheet
    worksheet.get_all_values.return_value = sheet_data

    return gc, worksheet

class TestGSheetClientRead:
    def test_read_simple_sheet(self):
        sheet_data = [
            ["Name", "Age", "City"],
            ["Alice", "30", "NYC"],
        ]
        gc, _ = make_mock_gc(sheet_data)
        client = GSheetClient(gc=gc)
        df = client.read("spreadsheet-123", "Sheet1")

        assert len(df) == 1
        assert df.iloc[0]["Name"] == "Alice"
```

**Mock Injection Patterns:**

| Module | Injection Point | Pattern |
|--------|-----------------|---------|
| `gsheet/client.py` | `GSheetClient(gc=mock)` | Constructor injection |
| `mail.py` | `pull_thread(id, service=mock)` | Parameter injection |
| `gdoc.py` | `pull_doc(id, url, docs_service=mock)` | Parameter injection |

**Test Coverage:**

| File | Classes | Key Tests |
|------|---------|-----------|
| `test_gsheet.py` | 5 | Read, write, pull, push, clone_all, pull_all |
| `test_mail.py` | 4 | Thread ID extraction, pull_thread, multipart format, MIME handling |
| `test_gdoc.py` | ~4 | Doc parsing, section handling |

**Fixtures:**
- `sample_thread_response.json` - Two-message Gmail thread
- `sample_doc_response.json` - Multi-tab Google Doc

### Layer 3: E2E Tests (Real APIs)

**File:** `test_e2e.py`

Full integration tests that:
1. Require authentication (`gax auth login`)
2. Use real Google Docs/Sheets specified via environment variables
3. Clean up test artifacts before/after each test

```python
@pytest.mark.e2e
class TestDocE2E:
    def test_import_pull_cycle(self, check_auth, test_doc, temp_dir):
        """Test: import markdown -> pull -> verify content."""
        # Uses real Google Doc via GAX_TEST_DOC env var
        result = _run_gax("doc", "tab", "import", test_doc["url"], str(test_file))
        assert result.returncode == 0

        # Pull and verify
        result = _run_gax("doc", "tab", "pull", str(tracking_file))
        content = tracking_file.read_text()
        assert "expected content" in content
```

**Configuration:**
```bash
# Required environment variables
export GAX_TEST_DOC="1DofO8emfHx8bhENkw23hQRj2T6pizH1X7Isq7uHH5f0"
export GAX_TEST_SHEET="1NtUmXPsF5XBBSRO8kzbSnGJX4YdkHWFzlfqPYlJtdoo"
```

**Fixtures:**
- `check_auth` - Skips tests if not authenticated
- `test_doc` - Provides doc ID/URL, clears extra tabs before/after
- `test_sheet` - Provides sheet ID/URL, clears extra sheets before/after
- `temp_dir` - Temporary directory for test files

**Test Scenarios:**
- Import markdown → pull → verify round-trip
- Clone → modify → push → pull cycle
- Multi-tab operations
- Tab creation and deletion

### Test Patterns

**1. Round-Trip Testing**
```python
def test_pull_modify_push(self, tmp_path):
    """Test pulling, modifying locally, and pushing back."""
    # Pull from mock server
    pull(file_path, client=pull_client)

    # Simulate user edit
    content = file_path.read_text().replace("100", "999")
    file_path.write_text(content)

    # Push to mock server
    push(file_path, client=push_client)

    # Verify modified data was sent
    values = push_worksheet.update.call_args[1]["values"]
    assert ["Item1", "999"] in values
```

**2. Fixture-Based API Mocking**
```python
def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()

def test_two_message_thread(self):
    thread_response = json.loads(load_fixture("sample_thread_response.json"))
    service = make_mock_service(thread_response)
    sections = pull_thread("thread-abc123", service=service)
    assert len(sections) == 2
```

**3. Temporary File Testing**
```python
def test_pull_updates_file(self, tmp_path):
    file_path = tmp_path / "test.sheet.gax"
    file_path.write_text(initial_content)

    pull(file_path, client=client)

    content = file_path.read_text()
    assert "new data" in content
```

### Running Tests

```bash
# All unit + integration tests (fast, no auth needed)
make test
# or: uv run pytest tests/ -v --ignore=tests/test_e2e.py

# E2E tests only (requires auth + env vars)
make test-e2e
# or: uv run pytest tests/test_e2e.py -v -m e2e

# Specific test file
uv run pytest tests/test_multipart.py -v

# With coverage
uv run pytest --cov=gax tests/
```

### Test Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "e2e: end-to-end integration tests (require auth, use real Google APIs)",
]
```

### What's Tested Well

| Area | Coverage | Notes |
|------|----------|-------|
| Multipart parsing | Excellent | Edge cases, unicode, round-trips |
| Sheet read/write | Good | Mock-based, all operations |
| Mail parsing | Good | MIME types, thread structure |
| Format converters | Moderate | Basic CSV/JSON tests |
| E2E workflows | Good | Import→pull→push cycles |

### What's Missing or Weak

| Area | Gap | Impact |
|------|-----|--------|
| Label/Filter IaC | No unit tests | Plan/diff logic untested |
| Calendar module | No tests | `gcal.py` not covered |
| Draft module | No tests | `draft.py` not covered |
| Error handling | Minimal | API errors, malformed files |
| CLI parsing | None | Relies on Click |
| Auth module | None | OAuth flow not tested |

### Testability Design Decisions

**Good:**
1. **Optional service parameters** - `pull_thread(id, service=None)` allows mock injection
2. **Client class for sheets** - `GSheetClient(gc=mock)` is cleanly testable
3. **Separate parsing functions** - `parse_multipart()` is pure, easy to test
4. **Dataclasses for domain objects** - Easy to construct test data

**Could Improve:**
1. **Inline service creation** - Some modules create services inline, harder to mock
2. **No test doubles for auth** - Auth module not mockable without patching
3. **CLI tests missing** - No Click CliRunner tests for command parsing

## Data Flow Examples

### Clone Thread

```
URL/ID
   │
   ▼
extract_thread_id()  ──────────────►  Gmail API threads.get()
   │                                           │
   │                                           ▼
   │                                   pull_thread()
   │                                           │
   │                                  ┌────────┴────────┐
   │                                  │  For each msg   │
   │                                  │  - parse headers│
   │                                  │  - extract body │
   │                                  │  - store attach │
   │                                  └────────┬────────┘
   │                                           │
   │                                           ▼
   │                                   list[MailSection]
   │                                           │
   │                                           ▼
   │                           _mail_section_to_multipart()
   │                                           │
   │                                           ▼
   │                              format_multipart()
   │                                           │
   │                                           ▼
   └──────────────────────────────────►  .mail.gax file
```

### Plan/Apply (Labels)

```
labels.yaml (desired)     Gmail API (current)
         │                        │
         ▼                        ▼
   _parse_labels_file()    labels.list()
         │                        │
         └──────────┬─────────────┘
                    │
                    ▼
            Diff computation
         ┌──────────┴──────────┐
         │  - create (new)     │
         │  - rename (from→to) │
         │  - update (changed) │
         │  - delete (removed) │
         └──────────┬──────────┘
                    │
                    ▼
            labels.plan.yaml
                    │
           ┌────────┴────────┐
           │  label_apply()  │
           │  - labels.create│
           │  - labels.patch │
           │  - labels.delete│
           └────────┬────────┘
                    │
                    ▼
               Gmail updated
```

## Strengths

1. **Clear file formats** - YAML frontmatter + body is human-readable, machine-parseable, git-friendly

2. **Consistent patterns** - clone/pull across all services, plan/apply for IaC operations

3. **Testable architecture** - Service functions take optional API objects for mocking

4. **Pluggable formats** - Easy to add CSV, JSON, Markdown table support

5. **Incremental operations** - Skips existing files, shows diffs before push

6. **Type field for dispatch** - Unified `gax pull` can detect file type

## Areas for Improvement

### 1. Large Module: mail.py

At 1739 lines, mail.py handles too much:
- Thread clone/pull
- List/relabel operations
- TSV parsing
- System label abbreviation logic

**Suggestion:** Split into `mail/thread.py`, `mail/list.py`, `mail/relabel.py`

### 2. Inconsistent API Client Patterns

Some modules create services inline:
```python
# In mail.py
creds = get_authenticated_credentials()
service = build("gmail", "v1", credentials=creds)
```

Others use lazy client classes:
```python
# In gsheet/client.py
class GSheetClient:
    @property
    def gc(self):
        if self._gc is None:
            ...
```

**Suggestion:** Standardize on client classes or a service factory

### 3. Duplicate TSV Parsing

TSV parsing appears in multiple places:
- `mail.py:_parse_tsv_line()`
- Custom YAML header parsing in several modules

**Suggestion:** Consolidate into a shared utility

### 4. Missing: Error Recovery

Operations are atomic - a failure mid-batch leaves partial state. No resume mechanism for bulk operations.

**Suggestion:** Consider checkpointing for long-running operations

### 5. Missing: Rate Limit Handling

No explicit retry/backoff for API rate limits. Relies on google-api-python-client defaults.

**Suggestion:** Add exponential backoff for bulk operations

## Summary

The codebase is well-designed for its purpose: a CLI tool for syncing Google Workspace data. Key strengths are the consistent file format, clear separation between CLI and service logic, and testable architecture. The main improvement opportunity is splitting large modules (mail.py) and standardizing patterns across services.

| Metric | Value |
|--------|-------|
| Total Python LOC | ~6,900 |
| Number of modules | 24 |
| Test coverage | Good for parsing, moderate for services |
| Dependencies | 6 runtime, minimal |
| Complexity | Low-medium (straightforward data flows) |
