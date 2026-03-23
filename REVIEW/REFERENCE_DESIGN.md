# Reference Design: Google Workspace Sync CLI

A high-level architecture for a CLI tool that syncs Google Workspace data to local, git-friendly files.

## Core Components

### 1. CLI Layer
Entry point and command routing. Thin layer that parses arguments and delegates to service modules.

```
cli.py
├── main group
├── auth group (login, logout, status)
├── sheet group (clone, pull, tab/*)
├── doc group (clone, pull, tab/*)
├── mail group (thread/*, draft/*, list/*, label/*, filter/*)
├── cal group (list, event/*)
└── pull (unified dispatcher)
```

**Responsibility:** Argument parsing, output formatting, error presentation. No business logic.

### 2. Auth Module
OAuth2 flow and credential management.

```
auth.py
├── login() → opens browser, stores token
├── logout() → removes token
├── status() → checks token validity
├── get_credentials() → returns valid credentials (refreshing if needed)
└── build_service(api, version) → returns authenticated API client
```

**State:** `~/.config/gax/credentials.json` (client secrets), `~/.config/gax/token.json` (access token)

### 3. Frontmatter Module
YAML header parsing and serialization. The foundation for all file formats.

```
frontmatter.py
├── parse(text) → (header: dict, body: str)
├── format(header: dict, body: str) → text
├── parse_multipart(text) → list[Section]
├── format_multipart(sections: list[Section]) → text
└── Section = dataclass(header: dict, body: str)
```

**Key insight:** Multipart is just repeated frontmatter blocks. The `content-length` header handles quoting edge cases.

### 4. Format Converters
Transform between DataFrame and text representations.

```
formats/
├── base.py      → Format protocol (read/write)
├── csv.py       → CSV, TSV, PSV
├── json.py      → JSON, JSONL
└── markdown.py  → Markdown tables
```

Interface:
```python
class Format(Protocol):
    def read(self, text: str) -> pd.DataFrame: ...
    def write(self, df: pd.DataFrame) -> str: ...
```

### 5. Service Modules
One module per Google service. Each handles API interaction and file format specifics.

```
sheet.py   → Sheets API ↔ DataFrame ↔ .sheet.gax
doc.py     → Docs API → Markdown ↔ .doc.gax
mail.py    → Gmail API ↔ .mail.gax, .draft.gax
cal.py     → Calendar API ↔ .cal.gax
label.py   → Gmail Labels API ↔ labels.yaml
filter.py  → Gmail Filters API ↔ filters.yaml
list.py    → Gmail batch labeling ↔ .gax (TSV state)
```

Each module follows the same pattern:
```python
def clone(url_or_id, output) → file     # API → local
def pull(file) → updated file           # refresh from API
def push(file) → API                    # local → API (where applicable)
```

### 6. Plan/Diff Engine
For IaC-style operations (labels, filters, relabeling).

```
plan.py
├── diff(current: State, desired: State) → Plan
├── Plan = dataclass(create, update, delete)
└── apply(plan, api_client) → results
```

**Pattern:** fetch current state → user edits → compute diff → show plan → apply

### 7. Content-Addressable Store (optional)
For mail attachments. Deduplicates binary content.

```
store.py
├── put(data: bytes, metadata: dict) → hash
├── get(hash) → bytes
└── path(hash) → Path
```

Location: `~/.gax/store/blob/<hash>`

## Component Interaction

```
┌─────────────────────────────────────────────────────────────┐
│                         CLI Layer                           │
│  (click commands, argument parsing, output formatting)      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     Service Modules                         │
│  (sheet.py, doc.py, mail.py, cal.py, label.py, filter.py)  │
│                                                             │
│  Orchestrate: API calls ↔ format conversion ↔ file I/O     │
└─────────────────────────────────────────────────────────────┘
          │                   │                    │
          ▼                   ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   Auth Module   │  │ Frontmatter/    │  │ Format Layer    │
│                 │  │ Multipart       │  │                 │
│ get_credentials │  │ parse/format    │  │ DataFrame ↔ CSV │
│ build_service   │  │ sections        │  │ DataFrame ↔ JSON│
└─────────────────┘  └─────────────────┘  └─────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Google APIs                              │
│  (Sheets, Docs, Gmail, Calendar via google-api-python-client)│
└─────────────────────────────────────────────────────────────┘
```

### Data Flow: Clone Operation

```
URL → extract_id() → API.get() → transform() → format_multipart() → write file
```

### Data Flow: Pull Operation

```
file → parse header → extract source/id → API.get() → transform() → write file
```

### Data Flow: Push Operation

```
file → parse_multipart() → transform → API.update()
                                ↓
                          (for IaC: diff with current state first)
```

### Data Flow: Plan/Apply (IaC)

```
file → parse desired state
                ↓
API.list() → current state
                ↓
diff(current, desired) → Plan(create, update, delete)
                ↓
user confirms → apply(plan) → API.create/update/delete
```

## Core Dependencies

### Must Have

| Package | Purpose | Why not hand-roll |
|---------|---------|-------------------|
| `click` | CLI framework | Handles argparse complexity, subcommands, help generation |
| `google-api-python-client` | Google API access | Generated client, handles API quirks |
| `google-auth-oauthlib` | OAuth2 flow | Security-critical, handles token refresh |
| `pyyaml` | YAML parsing | Standard, well-tested |
| `pandas` | DataFrame ops | Robust CSV/JSON handling, type inference |

### Nice to Have

| Package | Purpose | Alternative |
|---------|---------|-------------|
| `markdownify` | HTML→Markdown (for Docs) | Hand-roll with regex (fragile) |
| `python-dateutil` | Date parsing | stdlib datetime (less flexible) |
| `rich` | Pretty terminal output | Plain print (functional but ugly) |

### Avoid

| Package | Why avoid |
|---------|-----------|
| Heavy web frameworks | Overkill for CLI |
| ORM/database | Files are the database |
| Async libraries | API calls are sequential, not a bottleneck |

## Hand-Roll vs Import

### Hand-Roll

**Frontmatter/Multipart Parser**
- Custom format (YAML header + body with content-length quoting)
- ~100 lines, stable, no edge cases from external libs
- Full control over format evolution

**Plan/Diff Engine**
- Domain-specific logic (label matching, filter criteria hashing)
- Different rules per resource type
- ~200 lines per resource type

**File Naming/Path Utilities**
- Slugify titles, handle conflicts
- Simple string operations

**ID Extraction from URLs**
- Regex for Google URLs
- Different patterns per service

### Import

**OAuth Flow**
- Security-critical, easy to get wrong
- Token refresh, scope handling
- `google-auth-oauthlib` is battle-tested

**API Clients**
- Generated from Google's discovery docs
- Handles pagination, error codes, retries
- `google-api-python-client`

**Data Serialization**
- CSV edge cases (quoting, escaping, unicode)
- JSON handling
- `pandas` handles this robustly

**YAML Parsing**
- Spec is complex, `pyyaml` is standard

## Testing Strategy

### Layer 1: Unit Tests (fast, isolated)

**Target:** Pure functions with no I/O

```python
# frontmatter_test.py
def test_parse_simple():
    text = "---\ntitle: Test\n---\nBody"
    header, body = parse(text)
    assert header == {"title": "Test"}
    assert body == "Body"

def test_parse_multipart():
    text = "---\na: 1\n---\nfirst\n---\na: 2\n---\nsecond"
    sections = parse_multipart(text)
    assert len(sections) == 2

def test_content_length_quoting():
    # Body contains --- delimiter
    header = {"title": "Test"}
    body = "Line 1\n---\nLine 2"
    text = format(header, body)
    h2, b2 = parse(text)
    assert b2 == body  # Round-trip
```

**Coverage:**
- Frontmatter parsing/formatting
- Multipart parsing/formatting
- Format converters (CSV, JSON, Markdown)
- URL/ID extraction
- Plan diffing logic

### Layer 2: Integration Tests (mocked APIs)

**Target:** Service modules with mocked Google API responses

```python
# sheet_test.py
def test_clone_sheet(mock_sheets_api):
    mock_sheets_api.get.return_value = {
        "properties": {"title": "Test Sheet"},
        "sheets": [{"properties": {"title": "Tab1"}}]
    }
    mock_sheets_api.values.get.return_value = {
        "values": [["A", "B"], ["1", "2"]]
    }

    result = clone("https://docs.google.com/spreadsheets/d/abc123")

    assert "title: Test Sheet" in result
    assert "A,B" in result
```

**Strategy:**
- Mock `build_service()` to return fake API clients
- Fixture files with realistic API responses
- Test error handling (404, rate limits, auth failures)

### Layer 3: Snapshot Tests

**Target:** Output format stability

```python
def test_doc_output_format(snapshot):
    sections = [
        Section({"title": "Doc", "tab": "Tab1"}, "# Heading\n\nBody"),
        Section({"title": "Doc", "tab": "Tab2"}, "More content"),
    ]
    output = format_multipart(sections)
    assert output == snapshot  # Compare against saved snapshot
```

**Use for:**
- File format output (ensures backwards compatibility)
- CLI help text
- Plan output formatting

### Layer 4: E2E Tests (optional, slow)

**Target:** Full flow against real APIs

```python
@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("GAX_TEST_CREDENTIALS"))
def test_sheet_roundtrip():
    # Clone → modify → push → pull → verify
    ...
```

**Constraints:**
- Requires test Google account
- Slow, rate-limited
- Run in CI nightly, not on every commit
- Use dedicated test spreadsheet/doc

### Test Organization

```
tests/
├── unit/
│   ├── test_frontmatter.py
│   ├── test_formats.py
│   ├── test_plan.py
│   └── test_utils.py
├── integration/
│   ├── test_sheet.py
│   ├── test_doc.py
│   ├── test_mail.py
│   ├── conftest.py          # API mocks
│   └── fixtures/
│       ├── sheet_response.json
│       └── doc_response.json
├── snapshots/
│   └── *.txt
└── e2e/
    └── test_roundtrip.py
```

### What NOT to Test

- Google API behavior (trust the library)
- Click argument parsing (trust Click)
- YAML parsing (trust PyYAML)
- OAuth flow details (trust google-auth)

### Test Utilities

```python
# conftest.py
@pytest.fixture
def mock_credentials():
    """Bypass auth for tests."""
    with patch("gax.auth.get_credentials") as mock:
        mock.return_value = MagicMock()
        yield mock

@pytest.fixture
def temp_gax_file(tmp_path):
    """Create temporary .gax file."""
    def _create(content):
        f = tmp_path / "test.gax"
        f.write_text(content)
        return f
    return _create
```

## Directory Structure

```
gax/
├── __init__.py
├── cli.py              # Click commands (thin)
├── auth.py             # OAuth, credentials
├── frontmatter.py      # YAML+body parsing
├── multipart.py        # Multi-section format
├── plan.py             # Diff/plan engine
├── store.py            # Content-addressable store
├── formats/
│   ├── __init__.py
│   ├── base.py
│   ├── csv.py
│   ├── json.py
│   └── markdown.py
├── sheet.py            # Sheets service
├── doc.py              # Docs service
├── mail.py             # Gmail threads/drafts
├── cal.py              # Calendar service
├── label.py            # Gmail labels
├── filter.py           # Gmail filters
└── list.py             # Bulk labeling
```

## Key Design Principles

1. **Files are the source of truth** - No local database, state lives in .gax files
2. **Headers enable re-sync** - Every file contains its source URL/ID
3. **Multipart for composition** - Same format scales from 1 to N sections
4. **Plan before apply** - IaC pattern for bulk operations
5. **Tab-level granularity for writes** - Limits blast radius
6. **Pandas as intermediate** - Leverage robust serialization
7. **Thin CLI layer** - Logic lives in service modules, not commands
