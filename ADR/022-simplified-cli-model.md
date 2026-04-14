# ADR 022: Simplified CLI Model - new/pull/diff/push

## Status

**PROPOSED**

Supersedes ADR 021 (Consistent Resource Operations)

## Context

ADR 021 proposed a clone/pull/plan/apply/push model with 5 operations. While comprehensive, it had issues:

1. **Clone vs pull overlap** - Both download content, distinction is artificial
2. **Plan terminology** - Terraform-specific, not intuitive
3. **Apply confusion** - Users don't know if it applies a plan file or a local file
4. **Creation unclear** - How do users create NEW resources?
5. **Template guesswork** - Users must manually write correct YAML headers

The git-inspired pull/diff/push model is simpler, but we need to handle creation explicitly.

## Decision

### Core Operations

Four commands with clear, distinct purposes:

| Command | Direction | Creates Remote | Modifies Local | Interactive |
|---------|-----------|----------------|----------------|-------------|
| `new`   | → Both → | Yes | Yes (template) | No |
| `pull`  | Remote → Local | No | Yes | Yes (if exists) |
| `diff`  | Read-only | No | No | No |
| `push`  | Local → Remote | No | Yes (adds source) | Yes |

### Operation Details

#### `gax new <type> <name> [options]`

**Purpose:** Create a NEW Google resource with local template.

**What it does:**
1. Creates remote Google resource (doc, sheet, form, etc.)
2. Creates local `.gax` file with proper template
3. Fills in `source:` URL with remote resource ID
4. Includes helpful comments, examples, proper structure

**Examples:**
```bash
gax new doc "Project Notes"
# → Creates Google Doc
# → Creates project-notes.doc.gax with source: URL

gax new sheet "Budget 2026" --tabs Revenue,Expenses
# → Creates Google Sheet with 2 tabs
# → Creates budget-2026.sheet.gax

gax new form "Customer Survey"
# → Creates Google Form
# → Creates customer-survey.form.gax

gax doc tab new <doc-url> "New Section"
# → Creates tab in existing doc
# → Creates new-section.tab.gax
```

**Options:**
- `-o/--output <path>` - Custom output filename
- `--format <fmt>` - Template format where applicable
- Type-specific options (`--tabs`, `--calendar`, etc.)

#### `gax pull <url-or-file> [options]`

**Purpose:** Download or refresh content from remote.

**Smart behavior:**
- **If argument is URL** → Download to new file (like old `clone`)
- **If argument is file** → Refresh from remote (show diff + confirm)
- **If file exists** → Preview changes, confirm before overwriting
- **If `-y/--yes`** → Skip confirmation

**Examples:**
```bash
# First time: download from URL
gax pull https://docs.google.com/document/d/abc123
# → Creates document-name.doc.gax

# Later: refresh existing file
gax pull document-name.doc.gax
# → Shows diff:
#   ~ Line 15: "Q4 2025" → "Q1 2026"
#   Apply changes? [y/N]

# Force refresh without prompt
gax pull -y document-name.doc.gax

# Dry-run: show diff but don't apply
gax pull --dry-run document-name.doc.gax
```

**Options:**
- `-y/--yes` - Skip confirmation
- `--dry-run` - Preview only (same as `diff`)
- `-o/--output <path>` - Custom output (when pulling from URL)
- `--format <fmt>` - Output format

**Safety:**
When updating existing file:
1. Read current local content
2. Fetch remote content
3. Compute diff
4. Display changes
5. Prompt for confirmation (unless `-y`)
6. Write remote content to file

#### `gax diff <file>`

**Purpose:** Show differences between local and remote.

**What it does:**
1. Read local file
2. Fetch remote content
3. Display diff (unified format)
4. No modifications

**Examples:**
```bash
gax diff document.doc.gax
# → Shows:
# --- Remote
# +++ Local
# @@ -15,1 +15,1 @@
# -Q4 2025
# +Q1 2026

gax diff *.gax
# → Shows diff for each file
```

**Options:**
- `--format <fmt>` - Diff format (unified, side-by-side, stat)
- `--quiet` - Exit code only (0=no diff, 1=has diff)

**Use cases:**
- Check what pull would do
- Check what push would do
- Verify local changes before pushing
- CI/CD drift detection

#### `gax push <file> [options]`

**Purpose:** Upload local changes to remote.

**What it does:**
1. Read local file
2. Fetch remote content (for diff)
3. Display diff
4. Prompt for confirmation (unless `-y`)
5. Push changes to remote
6. If no `source:` in header, show error (use `new` instead)

**Examples:**
```bash
gax push document.doc.gax
# → Shows diff:
#   Local → Remote changes:
#   ~ Line 15: "Q4 2025" → "Q1 2026"
#   Push these changes? [y/N]

gax push -y *.doc.gax
# → Batch push with auto-confirm

gax push --dry-run document.doc.gax
# → Preview only (same as diff)
```

**Options:**
- `-y/--yes` - Skip confirmation
- `--dry-run` - Preview only
- Type-specific options (`--with-formulas`, etc.)

**Error handling:**
```bash
gax push new-file.md
# Error: No source URL in file header
# Hint: Use 'gax new' to create a new resource, or add source: URL manually
```

## Multi-Item Collections: checkout

For resources with multiple items (sheets with tabs, docs with tabs, mailboxes), add `checkout`:

```bash
# Download as single multipart file
gax pull https://docs.google.com/spreadsheets/d/abc123
# → budget.sheet.gax (all tabs in one file)

# Download as folder of individual files
gax checkout https://docs.google.com/spreadsheets/d/abc123
# → budget.sheet.gax.d/
#   ├── revenue.tab.gax
#   ├── expenses.tab.gax
#   └── .gax.yaml (metadata)

# Then work with individual tabs
gax diff budget.sheet.gax.d/revenue.tab.gax
gax push budget.sheet.gax.d/revenue.tab.gax
```

**Checkout is optional** - for users who want to work with individual items separately.

## Templates Created by `new`

### Document Template

```bash
gax new doc "Project Notes"
```

Creates `project-notes.doc.gax`:
```yaml
---
type: gax/doc
title: Project Notes
source: https://docs.google.com/document/d/abc123xyz
created: 2026-04-07T10:00:00Z
---
# Project Notes

Write your content here using markdown.

## Section 1

Start editing...
```

### Sheet Template

```bash
gax new sheet "Budget 2026"
```

Creates `budget-2026.sheet.gax`:
```yaml
---
type: gax/sheet
title: Budget 2026
source: https://docs.google.com/spreadsheets/d/abc123xyz
tabs: [Sheet1]
created: 2026-04-07T10:00:00Z
---
# Tab: Sheet1

Month,Income,Expenses
Jan,0,0
Feb,0,0
Mar,0,0
```

### Form Template

```bash
gax new form "Customer Survey"
```

Creates `customer-survey.form.gax`:
```yaml
---
type: gax/form
title: Customer Survey
source: https://docs.google.com/forms/d/abc123xyz
created: 2026-04-07T10:00:00Z
---
# Customer Survey

questions:
  # Multiple choice question
  - type: choice
    title: "How satisfied are you with our service?"
    required: true
    options:
      - "Very satisfied"
      - "Satisfied"
      - "Neutral"
      - "Dissatisfied"
      - "Very dissatisfied"

  # Text question
  - type: text
    title: "Any additional comments?"
    required: false
```

### Tab Templates

```bash
gax doc tab new <doc-url> "New Section"
```

Creates `new-section.tab.gax`:
```yaml
---
type: gax/doc-tab
title: Original Doc Title
source: https://docs.google.com/document/d/abc123
tab_id: t.xyz789
tab_title: New Section
created: 2026-04-07T10:00:00Z
---
# New Section

Content for this tab...
```

## Resource-Specific Commands

### Documents

```bash
# Create new document
gax new doc "Title"
gax doc new "Title"                    # Equivalent

# Download/refresh document
gax pull <url>
gax pull <file>
gax doc pull <url-or-file>             # Equivalent

# Check differences
gax diff <file>
gax doc diff <file>                    # Equivalent

# Push changes
gax push <file>
gax doc push <file>                    # Equivalent

# Work with tabs
gax doc tab new <doc-url> "Tab Name"
gax doc tab pull <file>
gax doc tab diff <file>
gax doc tab push <file>

# Checkout as folder
gax checkout <url>
gax doc checkout <url>                 # Equivalent
```

### Sheets

```bash
# Create new sheet
gax new sheet "Title" --tabs A,B,C
gax sheet new "Title" --tabs A,B,C

# Download/refresh
gax pull <url>
gax sheet pull <file>

# Work with tabs
gax sheet tab new <sheet-url> "Tab Name"
gax sheet tab pull <file>
gax sheet tab diff <file>
gax sheet tab push <file>

# Checkout as folder
gax checkout <url>
gax sheet checkout <url>
```

### Mail/Mailbox

```bash
# Mail is read-only content, mutable labels
gax pull <thread-url>
gax pull <thread-file>
gax diff <thread-file>                 # Shows label changes
gax push <thread-file>                 # Push label changes

# Mailbox (query results)
gax pull "in:inbox"                    # Creates mailbox file
gax mailbox pull <file>
gax mailbox diff <file>                # Shows label changes across threads
gax mailbox push <file>                # Batch label changes

# Checkout threads individually
gax mailbox checkout "in:inbox"        # → mailbox.gax.d/*.mail.gax
```

### Contacts

```bash
# Create empty contacts file
gax new contacts

# Download/refresh
gax pull contacts                      # Uses default account
gax contacts pull <file>

# Diff and push
gax contacts diff <file>
gax contacts push <file>
```

### Forms

```bash
# Create new form
gax new form "Title"

# Download/refresh
gax pull <form-url>
gax form pull <file>

# Diff and push
gax form diff <file>
gax form push <file>
```

### Calendar

```bash
# No "new calendar" (use Google UI)
# But create individual events:
gax new event --calendar Work "Team Meeting"

# Download events
gax pull --calendar Work --days 7      # → work.cal.gax
gax cal pull <file>

# Work with events
gax cal event new "Meeting Name"
gax cal event pull <file>
gax cal event diff <file>
gax cal event push <file>

# Checkout events individually
gax cal checkout --calendar Work       # → work.cal.gax.d/*.event.gax
```

## Unified Commands

Top-level commands work with any resource type (type detection from file or URL):

```bash
# Type-detected from URL pattern
gax pull <any-google-url>

# Type-detected from file header
gax pull <any-gax-file>
gax diff <any-gax-file>
gax push <any-gax-file>

# Batch operations
gax diff *.gax
gax push -y *.gax
gax pull -y *.gax
```

## Complete Operation Matrix

| Resource | new | pull | diff | push | checkout |
|----------|-----|------|------|------|----------|
| doc | ✅ | ✅ | ✅ | ✅ | ✅ |
| doc tab | ✅ | ✅ | ✅ | ✅ | N/A |
| sheet | ✅ | ✅ | ✅ | ✅ | ✅ |
| sheet tab | ✅ | ✅ | ✅ | ✅ | N/A |
| mail | ❌ | ✅ | ✅ | ✅ (labels) | N/A |
| mailbox | ❌ | ✅ | ✅ | ✅ (labels) | ✅ |
| contacts | ✅ | ✅ | ✅ | ✅ | ❌ |
| form | ✅ | ✅ | ✅ | ✅ | ❌ |
| cal | ❌ | ✅ | ✅ | ❌ | ✅ |
| event | ✅ | ✅ | ✅ | ✅ | N/A |
| label | ❌ | ✅ | ✅ | ✅ | ❌ |
| filter | ❌ | ✅ | ✅ | ✅ | ❌ |

## Implementation Strategy

### Phase 1: Add `new` Commands

**Goal:** Enable template-based creation for all resources.

**New commands:**
- `gax new doc <name>`
- `gax new sheet <name>`
- `gax new form <name>`
- `gax new contacts`
- `gax doc tab new <url> <name>`
- `gax sheet tab new <url> <name>`
- `gax cal event new <name>`

**Implementation:**
1. Define templates for each resource type
2. Create remote resource via API
3. Generate local file with pre-filled headers
4. Include helpful comments and examples

### Phase 2: Rename `plan` → `diff`

**Goal:** Use standard terminology.

**Changes:**
- Rename all `plan()` functions → `diff()`
- Update CLI commands: `gax <resource> plan` → `gax <resource> diff`
- Keep `plan` as deprecated alias
- Plan files (if saved) remain same format, just called diff output

**Migration:**
```bash
gax sheet plan file.gax        # DEPRECATED
gax sheet diff file.gax        # NEW
```

### Phase 3: Smart `pull` Behavior

**Goal:** Make pull handle both download and refresh.

**Changes:**
1. Detect if argument is URL or file path
2. If URL: download to new file (old `clone` behavior)
3. If file: refresh with diff + confirm
4. Merge `clone()` logic into `pull()`

**Migration:**
```bash
gax sheet clone <url>          # DEPRECATED
gax sheet pull <url>           # NEW

gax sheet pull <file>          # Already works, add confirmation
```

### Phase 4: Add Pull Safety

**Goal:** Prevent accidental overwrites (from ADR 021).

**Changes:**
1. Compute diff before overwriting
2. Display changes
3. Prompt for confirmation (unless `-y`)
4. Add `--dry-run` flag (alias for `diff`)

### Phase 5: Rename `apply` → `push`

**Goal:** Consistent push terminology.

**Changes:**
- Rename `apply()` → `push()` in contacts, form, label, filter, mailbox
- Update CLI commands
- Keep `apply` as deprecated alias

**Migration:**
```bash
gax contacts apply file.gax    # DEPRECATED
gax contacts push file.gax     # NEW
```

### Phase 6: Unified Top-Level Commands

**Goal:** Make type detection work at top level.

**Add:**
- `gax diff <file>` - Type-detected diff
- Enhance `gax pull <url-or-file>` - Handle any type
- Enhance `gax push <file>` - Handle any type

## Consequences

### Positive

1. **Simpler mental model** - 4 operations vs 5
2. **Matches git** - pull/diff/push are familiar
3. **Clear creation** - `new` makes templates, no guessing
4. **One way to download** - `pull` handles both clone and refresh
5. **Explicit safety** - diff + confirm prevents accidents
6. **Scriptable** - `-y` flag for automation
7. **Discoverable** - Consistent across all resources

### Negative

1. **Breaking changes** - Renames clone→pull, plan→diff, apply→push
2. **Migration effort** - Existing scripts need updates
3. **Pull is overloaded** - Does two things (download vs refresh)

### Neutral

1. **Checkout remains separate** - Still needed for multi-item collections
2. **Tab operations stay explicit** - `doc tab new` vs `doc new`

## Migration Path

### Deprecation Timeline

**v2.0 (Breaking):**
- Rename commands, keep old names as deprecated aliases
- Add warnings: "Warning: 'gax sheet clone' is deprecated, use 'gax pull' instead"

**v2.1 (6 months later):**
- Remove deprecated aliases
- Update all documentation

### User Migration

**Old (v1.x):**
```bash
gax sheet clone <url> -o file.gax
gax sheet pull file.gax              # Silent overwrite
gax sheet plan file.gax
gax sheet apply plan.yaml
```

**New (v2.0):**
```bash
gax pull <url> -o file.gax           # Or: gax sheet pull <url>
gax pull file.gax                    # Shows diff, confirms
gax diff file.gax
gax push file.gax                    # Shows diff, confirms
```

### Script Migration

Scripts using `-o` and `-y` flags continue to work:
```bash
# Old script
gax sheet clone <url> -o data.gax
gax sheet pull data.gax              # Silent

# New script
gax pull <url> -o data.gax
gax pull -y data.gax                 # Add -y for silent
```

## Alternatives Considered

### Alternative 1: Keep clone/pull Separate

**Description:** Maintain distinction between first-time download (clone) and refresh (pull).

**Rejected because:**
- Users don't care about the distinction
- Git uses `pull` for both (after initial `clone`)
- More commands to remember
- `pull` can easily detect if file exists

### Alternative 2: Use `create` Instead of `new`

**Description:** `gax create doc "Title"` instead of `gax new doc "Title"`.

**Rejected because:**
- `new` is shorter and equally clear
- `new` matches Rails/Django conventions
- `create` might be confused with "create local file only"

### Alternative 3: Keep `plan` Name

**Description:** Keep Terraform-style `plan` terminology.

**Rejected because:**
- `diff` is more universal (git, svn, diff command)
- `plan` implies separate apply step (we don't have that)
- `diff` is clearer: "show me the differences"

### Alternative 4: Make push create if no source

**Description:** `gax push new-file.md` creates remote if no `source:` URL.

**Rejected because:**
- Push doing two different things is confusing
- Can't generate helpful templates on the fly
- Explicit `new` command is clearer for creation intent

## Related ADRs

- **ADR 021**: Consistent Resource Operations (superseded by this ADR)
- **ADR 012**: Unified Pull Command (extended: pull now handles clone too)
- **ADR 019**: Clone vs Checkout Pattern (checkout still applies)
- **ADR 005**: CLI Structure and Tab-Level Operations
- **ADR 016**: Resource Abstraction (DRAFT - long-term architecture)

## Open Questions

1. **Should `new` work offline?**
   - Current: Creates remote immediately
   - Alternative: Create local file, push creates remote
   - Recommendation: Create remote immediately (simpler, source: URL is filled in)

2. **Should unified `gax pull <url>` guess resource type?**
   - Current: Yes, from URL pattern
   - Risk: Ambiguous URLs
   - Recommendation: Support explicit type: `gax doc pull <url>`

3. **Should `diff` support comparing two files?**
   - Example: `gax diff file1.gax file2.gax`
   - Current: Only local vs remote
   - Recommendation: Add later if requested

## Success Metrics

1. **Reduced support questions** - Fewer "how do I create/download/push" questions
2. **Higher script adoption** - `-y` flag used in automation
3. **Zero data loss reports** - Pull confirmation prevents accidents
4. **Template usage** - Most users start with `gax new` instead of manual files
5. **Complete coverage** - All resources support new/pull/diff/push
