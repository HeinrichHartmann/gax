# ADR 021: Consistent Resource Operations

## Status

**PROPOSED**

## Context

The gax CLI currently has inconsistent operation support across resource types:

| Resource | clone | pull | plan | apply | push | checkout |
|----------|-------|------|------|-------|------|----------|
| sheet    | ✅    | ✅   | ✅   | ✅    | ✅   | ✅       |
| sheet tab| ✅    | ✅   | ❌   | ❌    | ✅   | N/A      |
| doc      | ✅    | ✅   | ❌   | ❌    | ❌   | ✅       |
| doc tab  | ✅    | ✅   | ❌   | ❌    | ✅   | N/A      |
| mail     | ❌    | ✅   | ❌   | ❌    | ❌   | N/A      |
| mailbox  | ✅    | ✅   | ✅   | ✅    | ❌   | ❌       |
| contacts | ✅    | ✅   | ✅   | ✅    | ❌   | ❌       |
| form     | ✅    | ✅   | ✅   | ✅    | ❌   | ❌       |

### Problems

1. **Doc resources are read-only** - No plan/apply/push workflow at document level, only tab-level push
2. **Mail/mailbox asymmetry** - Mailbox has plan/apply but no push; mail has neither
3. **Tab-level gaps** - Sheet/doc tabs support push but not plan/apply
4. **Pull safety** - Pull operations directly overwrite local files without preview or confirmation
5. **Unclear push vs plan+apply** - Some resources use push (sheets, doc tabs), others use plan+apply (mailbox, contacts, forms)

### User Request

Make operations consistent across ALL resources (mail, mailbox, sheet, tab, doc) with these operations:
- **clone** - Sync the resource itself to local
- **pull** - Update local state from remote (with preview y/n prompt before clobbering)
- **plan** - View changes (declarative diff)
- **apply** - Apply changes (separate from push)
- **push** - Update remote, show diff

Consider whether we need both `apply` and `push` or can consolidate.

## Decision

### Operation Definitions

We establish a consistent set of operations with clear semantics:

| Operation | Direction | Safety | Interactive | Use Case |
|-----------|-----------|--------|-------------|----------|
| `clone`   | Remote → Local | Safe (new file) | No | Download resource for first time |
| `pull`    | Remote → Local | **Destructive** | **Yes (preview + confirm)** | Refresh local copy from remote |
| `plan`    | Local ↔ Remote | Safe (read-only) | No | Preview what would change |
| `push`    | Local → Remote | **Destructive** | **Yes (diff + confirm)** | Upload local changes to remote |

**Key decision: Remove `apply` command.** It's redundant with `push`. The plan+apply pattern was borrowed from Terraform, but in practice:
- Users want to see the diff AND push in one step
- Having `plan` and `push` is clearer than `plan` and `apply`
- `push` implies "upload my changes" (intuitive)
- `apply` is ambiguous (apply plan file? apply local file?)

### Standard Operation Matrix

**All resources MUST implement:**

| Operation | Required | Flag Support | Behavior |
|-----------|----------|--------------|----------|
| `clone`   | ✅ Yes   | `-o/--output`, `--format` | Download from URL/query → new file |
| `pull`    | ✅ Yes   | `-y/--yes`, `-f/--force` | Refresh from remote, **show diff + confirm** unless `-y` |
| `plan`    | ✅ Yes   | `-o/--output` | Show what would change, output plan file |
| `push`    | ✅ Yes   | `-y/--yes`, `--dry-run` | Show diff + confirm, then push changes |

**Collection resources SHOULD implement:**

| Operation | Optional | Behavior |
|-----------|----------|----------|
| `checkout` | For multi-item collections | Download as folder of individual files (`.gax.d/`) |

### Pull Safety: Preview Before Clobber

**Current problem:** Pull operations silently overwrite local files, destroying any local edits.

**Solution:** Add confirmation flow to pull (similar to push):

```bash
$ gax pull document.doc.gax

Checking for changes...

Remote → Local changes:
  Tab "Introduction":
    ~ Line 15: "Q4 2025" → "Q1 2026"
    + Line 23: New paragraph added
  Tab "Summary":
    - Line 8: Paragraph removed

⚠️  This will overwrite your local file.

Apply these changes? [y/N]:
```

**Flags:**
- `-y/--yes` - Skip confirmation (for scripts)
- `-f/--force` - Force overwrite even if local changes detected (future: git-style dirty tracking)
- `--dry-run` - Show diff but don't apply (equivalent to viewing the diff)

**Implementation:**
1. Read local file
2. Fetch remote content
3. Compute diff (local → remote changes)
4. Display diff
5. If differences exist AND not `-y`, prompt for confirmation
6. Write remote content to local file

### Push vs Plan: Unified Model

**Current inconsistency:**
- Sheets/doc tabs: Use `push` (one command, shows diff + confirms)
- Mailbox/contacts/forms: Use `plan` + `apply` (two commands, plan generates file)

**Decision:** Standardize on `push` workflow, keep `plan` for dry-run:

```bash
# Standard workflow
gax push document.doc.gax
# Shows diff, confirms, pushes

# Alternative: plan first, review later
gax plan document.doc.gax -o changes.yaml
# Review changes.yaml
gax push document.doc.gax
# (Re-computes and pushes - plan file is just for review)
```

**Plan file format:**
```yaml
---
type: gax/plan
resource: gax/doc
source: https://docs.google.com/document/d/abc123
generated: 2026-04-07T10:00:00Z
---
changes:
  - tab: Introduction
    operations:
      - type: update
        location: line 15
        old: "Q4 2025"
        new: "Q1 2026"
      - type: insert
        location: line 23
        content: "New paragraph..."
  - tab: Summary
    operations:
      - type: delete
        location: line 8
        content: "Old paragraph..."
```

**Key point:** Plan files are **informational only**. They don't drive the push operation. This is simpler than Terraform's two-phase plan+apply model and matches how git works (diff is informational, push recomputes).

## Resource-Specific Implementations

### Google Docs

**Document-level operations:**

| Operation | Behavior |
|-----------|----------|
| `gax doc clone <url>` | Download all tabs → `Document.doc.gax` (multipart, read-only) |
| `gax doc pull <file>` | Refresh all tabs, **show diff + confirm** |
| `gax doc plan <file>` | Show what would change if pushed |
| `gax doc push <file>` | **NEW:** Push all tabs (diff + confirm) |
| `gax doc checkout <url>` | Download as folder → `Document.doc.gax.d/*.tab.gax` |

**Tab-level operations:**

| Operation | Behavior |
|-----------|----------|
| `gax doc tab clone <url> <tab>` | Download single tab → `Tab.tab.gax` |
| `gax doc tab pull <file>` | Refresh tab, **show diff + confirm** |
| `gax doc tab plan <file>` | **NEW:** Show what would change |
| `gax doc tab push <file>` | Push tab (already exists, add confirm if missing) |

### Google Sheets

**Sheet-level operations:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax sheet clone <url>` | ✅ Multipart | Add pull safety |
| `gax sheet pull <file>` | ✅ Refresh | **Add diff + confirm** |
| `gax sheet plan <file>` | ✅ Exists | Keep as-is |
| `gax sheet push <file>` | ✅ Exists | Ensure diff + confirm |
| `gax sheet checkout <url>` | ✅ Folder | Keep as-is |

**Tab-level operations:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax sheet tab clone <url> <tab>` | ✅ | Add pull safety |
| `gax sheet tab pull <file>` | ✅ | **Add diff + confirm** |
| `gax sheet tab plan <file>` | ❌ | **NEW:** Preview changes |
| `gax sheet tab push <file>` | ✅ | Ensure diff + confirm |

### Gmail (Mail & Mailbox)

**Mailbox (list) operations:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax mailbox clone <query>` | ✅ Fetch threads | Add pull safety |
| `gax mailbox pull <file>` | ✅ Refresh | **Add diff + confirm** |
| `gax mailbox plan <file>` | ✅ Label changes | Keep as-is |
| `gax mailbox push <file>` | ❌ | **NEW:** Replace apply with push |
| `gax mailbox checkout <query>` | ❌ | **NEW:** Download as folder of threads |

**Mail (thread) operations:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax mail clone <url/query>` | ❌ | **NEW:** Download thread(s) |
| `gax mail pull <file>` | ✅ Refresh thread | **Add diff + confirm** |
| `gax mail plan <file>` | ❌ | **NEW:** Preview label changes |
| `gax mail push <file>` | ❌ | **NEW:** Push label changes |

**Note:** Mail content is read-only (Gmail API limitation), but labels are mutable. Plan/push for mail means label changes only.

### Other Resources

**Contacts:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax contacts clone` | ✅ | Add pull safety |
| `gax contacts pull <file>` | ✅ | **Add diff + confirm** |
| `gax contacts plan <file>` | ✅ | Keep as-is |
| `gax contacts push <file>` | ❌ | **NEW:** Replace apply with push |

**Forms:**

| Operation | Current | New Behavior |
|-----------|---------|--------------|
| `gax form clone <url>` | ✅ | Add pull safety |
| `gax form pull <file>` | ✅ | **Add diff + confirm** |
| `gax form plan <file>` | ✅ | Keep as-is |
| `gax form push <file>` | ❌ | **NEW:** Replace apply with push |

**Labels, Filters, Calendar:** (Same pattern as forms)

## Implementation Strategy

### Phase 1: Add Pull Safety (High Priority)

**Goal:** Prevent accidental data loss from pull operations.

**Changes:**
1. Add diff computation to all pull commands
2. Add confirmation prompt (skip if `-y/--yes`)
3. Add `--dry-run` flag (show diff, no change)

**Files to modify:**
- `gax/doc.py` - `pull()`, `tab_pull()`
- `gax/gsheet.py` - `sheet_pull()`, `tab_pull()`
- `gax/mail.py` - `mail_pull()`
- `gax/mailbox.py` - `mailbox_pull()`
- `gax/contacts.py` - `pull()`
- `gax/form.py` - `pull()`
- `gax/cal.py` - `pull()`

**Test coverage:**
- `tests/test_cli_patterns.py` - Add pull safety test
- Verify all pull commands have `-y/--yes` flag

### Phase 2: Rename Apply → Push (Medium Priority)

**Goal:** Consistent terminology across all resources.

**Changes:**
1. Rename `apply()` → `push()` in:
   - `gax/mailbox.py`
   - `gax/contacts.py`
   - `gax/form.py`
   - `gax/label.py`
   - `gax/filter.py`
2. Update CLI commands: `gax <resource> apply` → `gax <resource> push`
3. Keep plan file format the same (still generate `*-plan.yaml`)
4. Update help text and docs

**Migration:**
- Keep `apply` as deprecated alias for one release
- Add deprecation warning: "apply is deprecated, use push instead"

### Phase 3: Add Missing Operations (Low Priority)

**Goal:** Complete the matrix for all resources.

**New commands:**
- `gax doc push <file>` - Document-level push (all tabs)
- `gax doc tab plan <file>` - Tab-level plan
- `gax sheet tab plan <file>` - Tab-level plan
- `gax mail clone <url>` - Clone email thread
- `gax mail plan <file>` - Plan label changes
- `gax mail push <file>` - Push label changes
- `gax mailbox checkout <query>` - Download threads to folder
- `gax mailbox push <file>` - Push mailbox changes (replaces apply)

**Implementation priority:**
1. Doc push (high user value)
2. Tab-level plan commands (completeness)
3. Mail operations (nice-to-have)
4. Mailbox checkout (nice-to-have)

### Phase 4: Unified Type Detection (Future)

**Goal:** Make unified `gax push`, `gax pull`, `gax plan` work for all file types.

**Current state:**
- `gax pull <file>` - ✅ Implemented (ADR 012)
- `gax push <file>` - ✅ Implemented (recent commit)
- `gax plan <file>` - ❌ Not implemented

**Add:**
- `gax plan <file>` - Detect type, route to appropriate plan command

## Consequences

### Positive

1. **Consistent UX** - Same operations work the same way across all resources
2. **Safer pulls** - No more accidental overwrites
3. **Clearer terminology** - `push` is more intuitive than `apply`
4. **Scriptable** - `-y/--yes` flag for automation
5. **Discoverable** - Users can predict what commands exist
6. **Complete workflows** - Every resource has full clone → pull → plan → push cycle

### Negative

1. **Breaking changes** - `apply` renamed to `push` (mitigated by deprecation alias)
2. **More confirmation prompts** - Pull now prompts (mitigated by `-y` flag)
3. **Implementation work** - Many commands to update

### Neutral

1. **Plan files remain informational** - Not used as input to push (simpler model)
2. **Checkout remains optional** - Only for multi-item collections

## Migration Path

### For Users

**v1.x (Current):**
```bash
gax pull document.doc.gax          # Silent overwrite
gax contacts apply plan.yaml       # Apply changes
```

**v2.0 (This ADR):**
```bash
gax pull document.doc.gax          # Shows diff, asks confirmation
gax pull -y document.doc.gax       # Skip confirmation (old behavior)
gax contacts push contacts.gax     # Shows diff, asks confirmation
gax contacts apply contacts.gax    # DEPRECATED: Use push instead
```

### For Developers

1. Update pull commands to compute diff and confirm
2. Rename apply → push, add deprecation alias
3. Add missing operations incrementally
4. Update tests to verify new safety features

## Alternatives Considered

### Alternative 1: Keep Plan + Apply Separate

**Description:** Maintain Terraform-style workflow where plan generates a file, apply executes it.

**Rejected because:**
- More complex: Plan file must be serializable, versioned, validated
- Worse UX: Two commands for one operation
- Git/SVN don't work this way: diff is informational, commit recomputes

### Alternative 2: Make Pull Always Safe (Never Overwrite)

**Description:** Pull only updates if no local changes detected (git-style dirty checking).

**Rejected because:**
- Requires tracking dirty state (added complexity)
- Users often WANT to discard local changes and refresh from remote
- Confirmation prompt is simpler and more explicit

### Alternative 3: Separate Read and Write Resources

**Description:** Mail/docs are read-only, sheets/forms are read-write. Don't force consistency.

**Rejected because:**
- Docs ARE writable (via tab push)
- Mail labels ARE writable
- Inconsistency confuses users

## Related ADRs

- **ADR 005**: CLI Structure and Tab-Level Operations
- **ADR 012**: Unified Pull Command (type detection)
- **ADR 016**: Resource Abstraction (DRAFT - defines long-term architecture)
- **ADR 019**: Clone vs Checkout Pattern (defines checkout semantics)
- **ADR 020**: Mail Command Cleanup

## Open Questions

1. **Should plan files be usable as input to push?**
   - Current decision: No (informational only)
   - Alternative: Yes, but requires plan file validation and versioning
   - Recommendation: Start with informational, add input support if users request it

2. **How to handle conflicts in push?**
   - Current: Show diff, user decides
   - Future: Three-way merge? Conflict markers?
   - Recommendation: Defer until we see real user conflicts

3. **Should we support partial push?**
   - Example: `gax doc push <file> --tabs "Introduction,Summary"`
   - Current: All-or-nothing
   - Recommendation: Add later if needed

## Success Metrics

1. **Zero accidental data loss reports** - Pull confirmation prevents overwrites
2. **Reduced confusion** - Fewer "how do I push this?" support requests
3. **High script adoption** - `-y` flag used in automation
4. **Complete operation matrix** - All resources support clone/pull/plan/push
