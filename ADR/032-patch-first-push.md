# ADR 032: Patch-First Push with Bulk Fallback

## Status

Proposed

## Context

ADR 027 introduced diff-based push (`--patch`) as an experimental second path behind a flag. The default remained full-replace ("bulk") with the `--patch` flag required to opt in. The rationale was caution — the patch path was new and untested at scale.

Since then, `diff_push.py` has been validated across markdown-native, human-authored, and adversarial documents (see ADR 027 experiments). The patch path now passes the e2e test suite. The primary failure mode is well-understood: `diff_to_mutations` raises `ValueError` when a structural change cannot be translated (e.g., table dimension change, type mismatch between base and edit block). This failure is deterministic and detected at preview time, before any write.

Keeping bulk as the default has a significant cost: every push destroys all non-markdown formatting (colors, fonts, alignment, comments, suggestions). This is the wrong default for a tool that is increasingly used on collaboratively-edited documents. Users who do not know to pass `--patch` silently lose their collaborators' formatting work on every push.

## Decision

**Patch is the default.** Bulk (full-replace) is the explicit fallback, exposed as `--bulk`.

When the patch path fails (detected at preview time), gax offers an interactive fallback to bulk rather than aborting. With `--bulk -y`, the command pushes immediately with no output.

## New Push Flow

```
gax doc tab push <file> [--bulk] [-y] [--body <file>]
```

### Step-by-step

```
1. Resolve content
   content = body.read_text() if --body else parse_multipart(file)[0].content
   content = inline_images_from_store(content)

2. If --bulk → skip to BULK path

3. PATCH path (default):
   a. preview = preview_diff(document_id, tab_name, content)
   b. if not preview.ops:
        print "No differences to push." → exit 0
   c. if preview.warnings:
        print warning(s)
        if -y:
          → fall through to BULK silently
        else:
          prompt "Patch cannot be applied. Fall back to bulk push? [y/N]"
          no  → "Aborted." → exit 1
          yes → fall through to BULK
   d. Patch is clean:
        print patch summary (N update(s), M insert(s), K delete(s))
        if not -y: prompt "Apply patch? [Y/n]"
        t.push(patch=True) → exit 0

4. BULK path:
   a. diff_text = t.diff(body=body)
   b. if not diff_text:
        print "No differences to push." → exit 0
   c. if not (--bulk and -y):
        print diff
        print unsupported-feature warnings
        print destructive-push warning
        if not -y: prompt "Push these changes? [y/N]"
   d. t.push(body=body) → exit 0
```

### Invariants

| Flags | Behavior |
|-------|----------|
| _(none)_ | Try patch → show summary → confirm → apply. On patch fail → ask to fall back. |
| `-y` | Try patch → apply silently. On patch fail → fall back to bulk silently. |
| `--bulk` | Skip patch → show diff → confirm → bulk push. |
| `--bulk -y` | Skip patch → bulk push immediately, no output. |
| `--body <file>` | Use external file as content source (patch + bulk both respect it). |

### Folder-level push (`gax doc push <folder>`)

Same strategy, applied per tab:
- Each tab is pushed with patch by default.
- If a tab's patch fails, the fallback prompt applies to that tab individually.
- `--bulk` and `-y` apply to all tabs.

## Implementation Plan

### Context for the implementer

The codebase is a Python CLI built with Click (`gax/gdoc/cli.py`). The relevant
existing classes and functions:

- `Tab.from_file(path)` — loads a single-tab tracking file
- `Tab.push(**kw)` — `patch=True` kwarg uses diff-based push, default is bulk
- `Tab.diff(**kw)` — returns unified diff string vs remote, or `None` if clean
- `Doc.from_file(folder)` — loads a checkout folder
- `preview_diff(document_id, tab_name, content)` → `DiffPreview` — dry-run patch
- `DiffPreview.ops` — list of `EditOp`; empty means no differences
- `DiffPreview.warnings` — currently overloaded (see Step 1 below)
- `DiffPreview.summary_lines` — human-readable patch summary
- `inline_images_from_store(content)` — pre-process content before push
- `parse_multipart(text)` → `list[DocSection]` — parse tracking file
- `DocSection.content`, `.source`, `.section_title` — fields on tracking file section
- `extract_doc_id(url)` — extract document ID from a Google Docs URL

Implement the four steps below in order. Each step is independently testable.
Run `uv run pytest tests/test_cli_patterns.py -q` after each step to catch
regressions early.

---

### Step 1 — Refactor `DiffPreview` in `gax/gdoc/diff_push.py`

`DiffPreview.warnings` is currently overloaded: it holds both an informational
"No differences found." string (not an error) and fatal error strings like
"Patch cannot be applied: ...". The no-diff case is already handled by `not
preview.ops` so the informational string is never needed. Replace `warnings`
with a clean `error`/`fatal` pair.

**Replace the dataclass definition** (around line 655):

```python
# Before
@dataclass
class DiffPreview:
    ops: list[EditOp]
    summary_lines: list[str]
    warnings: list[str]
    docs_service: object = field(default=None, repr=False)

# After
@dataclass
class DiffPreview:
    ops: list[EditOp]
    summary_lines: list[str]
    error: str | None = None      # None = patch applicable; str = reason it is not
    fatal: bool = False           # True = tab not found; don't offer bulk fallback
    docs_service: object = field(default=None, repr=False)
```

**Update `preview_diff`** (around line 685) — three call sites that construct
`DiffPreview`:

1. `_fetch_tab` raises `ValueError` (tab not found) → fatal error, no fallback:
   ```python
   return DiffPreview(ops=[], summary_lines=[], error=str(e), fatal=True)
   ```

2. `not ops` (no differences) → clean, no error:
   ```python
   return DiffPreview(ops=[], summary_lines=[])
   ```

3. `diff_to_mutations` raises `ValueError` → patch cannot apply, bulk fallback ok:
   ```python
   # replace: warnings.append(f"Patch cannot be applied: {e}")
   # with:
   error = str(e)
   ```
   Then at the final return:
   ```python
   return DiffPreview(ops=ops, summary_lines=summary, error=error or None)
   ```
   Remove the `warnings: list[str] = []` local variable entirely.

**Verify:** `DiffPreview.warnings` is only referenced in `cli.py` lines 142–145.
After this step those references are dead and will be fixed in Step 3.

---

### Step 2 — Add `_push_tab_patch_or_bulk` helper in `gax/gdoc/cli.py`

Add this function near the top of the file, before the Click command definitions.
It encapsulates the full patch→fallback→bulk decision for one tab, and is called
by both `doc_tab_push` (Step 3) and `doc_push` (Step 4).

Returns `True` if a push was performed, `False` if there were no differences.
Exits with `sys.exit(1)` only on fatal errors (tab not found).

```python
def _push_tab_patch_or_bulk(
    t: "Tab",
    content: str,
    yes: bool,
    bulk: bool,
    label: str = "",
) -> bool:
    """Patch-first push for one tab. Returns True if pushed, False if no diff."""
    from .doc import parse_multipart, extract_doc_id
    from .ir import from_markdown, check_unsupported

    prefix = f"[{label}] " if label else ""

    if not bulk:
        from .diff_push import preview_diff

        section = parse_multipart(t.path.read_text(encoding="utf-8"))[0]
        document_id = extract_doc_id(section.source)
        tab_name = section.section_title

        preview = preview_diff(document_id, tab_name, content)

        if not preview.ops:
            return False  # no differences

        if preview.error:
            if preview.fatal:
                click.echo(f"{prefix}Error: {preview.error}", err=True)
                sys.exit(1)
            # Non-fatal: offer bulk fallback
            click.echo(f"{prefix}Patch cannot be applied: {preview.error}")
            if yes:
                bulk = True  # silent fallback
            else:
                if not click.confirm(f"{prefix}Fall back to bulk push?", default=False):
                    click.echo("Aborted.")
                    sys.exit(1)
                bulk = True

        if not bulk:
            # Clean patch — show summary and confirm
            click.echo(f"{prefix}Patch operations:")
            click.echo("-" * 40)
            for line in preview.summary_lines:
                click.echo(line)
            click.echo("-" * 40)
            if not yes:
                if not click.confirm("Apply patch?"):
                    click.echo("Aborted.")
                    sys.exit(1)
            t.push(patch=True)
            return True

    # BULK path
    diff_text = t.diff()
    if diff_text is None:
        return False  # no differences

    if not (bulk and yes):
        click.echo(f"{prefix}Changes to push:")
        click.echo("-" * 40)
        click.echo(diff_text)
        click.echo("-" * 40)
        section = parse_multipart(t.path.read_text(encoding="utf-8"))[0]
        for w in check_unsupported(from_markdown(section.content)):
            click.echo(f"  Warning: {w.feature}: {w.detail}")
        click.echo(
            "Warning: markdown cannot faithfully represent a Google Doc. "
            "Non-markdown formatting (colors, fonts, alignment, comments, "
            "suggestions, images) may be lost."
        )
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                sys.exit(1)

    t.push()
    return True
```

Note: `--body` is intentionally not threaded through `_push_tab_patch_or_bulk`.
`body` is only applicable to single-tab push (`doc_tab_push`), not folder push.
The `content` parameter already carries the resolved content (with body applied
if needed), and `t.push()` in the bulk path uses `t.push()` directly (no body
kwarg needed because content is already in the tracking file after the body
update that `Tab.push(body=body)` performs). This means the bulk path in the
helper uses the tracking file content, which is correct for folder push. For
single-tab push with `--body`, the caller (`doc_tab_push`) handles the body
write-back via `Tab.push(body=body)` directly rather than delegating to this
helper. See Step 3 for how `doc_tab_push` handles this.

---

### Step 3 — Rewrite `doc_tab_push` in `gax/gdoc/cli.py`

Replace the entire `doc_tab_push` command (lines 86–188) with:

```python
@doc_tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--bulk", is_flag=True, help="Full-replace push, skipping patch attempt")
@click.option(
    "--body",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Push content from this external markdown file instead of the tracking file.",
)
@gax_command
def doc_tab_push(file: Path, yes: bool, bulk: bool, body: Path | None):
    """Push local changes to a single tab.

    Patch is the default: applies only changed elements, preserving collaborator
    formatting, comments, and suggestions. Use ``--bulk`` to force a full-replace
    push (faster, but destroys all non-markdown formatting).

    When patch cannot be applied (e.g. structural changes like adding a table),
    gax offers to fall back to bulk. With ``-y`` the fallback happens silently.

    Use ``--body`` to push content from an external markdown file. The tracking
    file is updated in place so subsequent ``pull`` round-trips stay consistent.
    """
    from .doc import parse_multipart, extract_doc_id
    from . import native_md as _native_md

    t = Tab.from_file(file)

    # Resolve content (respects --body)
    if body:
        raw = body.read_text(encoding="utf-8")
    else:
        raw = parse_multipart(file.read_text(encoding="utf-8"))[0].content
    content = _native_md.inline_images_from_store(raw)

    # For --body with bulk path, we need Tab.push(body=body) so the tracking
    # file is updated. Handle this case separately.
    if bulk:
        if not (bulk and yes):
            from .doc import parse_multipart as _pm
            from .ir import from_markdown, check_unsupported
            diff_text = t.diff(body=body)
            if diff_text is None:
                click.echo("No differences to push.")
                return
            click.echo("Changes to push:")
            click.echo("-" * 40)
            click.echo(diff_text)
            click.echo("-" * 40)
            raw_for_warn = body.read_text(encoding="utf-8") if body else (
                _pm(file.read_text(encoding="utf-8"))[0].content
            )
            for w in check_unsupported(from_markdown(raw_for_warn)):
                click.echo(f"  Warning: {w.feature}: {w.detail}")
            click.echo(
                "Warning: markdown cannot faithfully represent a Google Doc. "
                "Non-markdown formatting (colors, fonts, alignment, comments, "
                "suggestions, images) may be lost."
            )
            if not yes:
                if not click.confirm("Push these changes?"):
                    click.echo("Aborted.")
                    return
        else:
            # --bulk -y: check for diff silently, skip if none
            if t.diff(body=body) is None:
                return
        t.push(body=body)
        success("Pushed successfully.")
        return

    # PATCH path (default)
    from .diff_push import preview_diff

    section = parse_multipart(file.read_text(encoding="utf-8"))[0]
    document_id = extract_doc_id(section.source)
    tab_name = section.section_title

    preview = preview_diff(document_id, tab_name, content)

    if not preview.ops:
        click.echo("No differences to push.")
        return

    if preview.error:
        if preview.fatal:
            click.echo(f"Error: {preview.error}", err=True)
            sys.exit(1)
        click.echo(f"Patch cannot be applied: {preview.error}")
        if yes:
            pass  # fall through to bulk silently
        else:
            if not click.confirm("Fall back to bulk push?", default=False):
                click.echo("Aborted.")
                return
        # Bulk fallback
        if t.diff(body=body) is None:
            click.echo("No differences to push.")
            return
        if not yes:
            # diff already shown above implicitly — just confirm
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
        t.push(body=body)
        success("Pushed successfully (bulk fallback).")
        return

    # Clean patch
    click.echo("Patch operations:")
    click.echo("-" * 40)
    for line in preview.summary_lines:
        click.echo(line)
    click.echo("-" * 40)
    if not yes:
        if not click.confirm("Apply patch?"):
            click.echo("Aborted.")
            return
    t.push(patch=True)
    success("Patched successfully.")
```

**Note on `--body` + patch path:** `--body` resolves the `content` sent to
`preview_diff`, so the patch diff is computed against the external file's content.
After a successful patch, `t.push(patch=True)` currently reads from the tracking
file, not from `body`. This means `--body` with patch will push the wrong content.
For now, restrict `--body` to the bulk path: if `body` is set, force `bulk=True`
at the top of the function and proceed directly to the bulk branch. Add a note
in the docstring.

Simplified `--body` handling (add at the top of the function body, before the
patch/bulk split):

```python
if body:
    bulk = True  # --body only supported with bulk path
```

This avoids the complexity of threading `body` through the patch pipeline.

---

### Step 4 — Update `doc_push` (folder) in `gax/gdoc/cli.py`

Replace the current `doc_push` command (lines 246–280) with a per-tab loop using
`_push_tab_patch_or_bulk`. The folder-level command does not support `--body`.

```python
@doc.command("push")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--bulk", is_flag=True, help="Full-replace push for all tabs")
@gax_command
def doc_push(folder: Path, yes: bool, bulk: bool):
    """Push all changed tabs in a checkout folder to Google Docs.

    Patch is the default: applies only changed elements per tab, preserving
    collaborator formatting. Use ``--bulk`` to force full-replace for all tabs.

    When patch cannot be applied for a tab, gax offers to fall back to bulk
    for that tab individually. With ``-y`` the fallback happens silently.
    """
    from .doc import parse_multipart
    from . import native_md as _native_md

    d = Doc.from_file(folder)
    # Iterate tabs via Doc's internal helpers (same as Doc.push)
    from .doc import _known_tab_files, _read_checkout_metadata
    metadata = _read_checkout_metadata(folder)
    tab_files = list(_known_tab_files(folder, metadata))

    if not tab_files:
        click.echo("No tab files found.")
        return

    pushed = 0
    for tab_file in tab_files:
        t = Tab.from_file(tab_file)
        label = tab_file.relative_to(folder).as_posix()
        section = parse_multipart(tab_file.read_text(encoding="utf-8"))[0]
        content = _native_md.inline_images_from_store(section.content)
        did_push = _push_tab_patch_or_bulk(t, content=content, yes=yes, bulk=bulk, label=label)
        if did_push:
            pushed += 1

    if pushed == 0:
        click.echo("No differences to push.")
    else:
        success(f"Pushed {pushed} tab(s).")
```

---

### Step 5 — Update tests

**`tests/test_cli_patterns.py`:** No changes needed. The push commands still have
`-y/--yes` flag. `--patch` was not tested by pattern tests.

**`tests/test_e2e.py` — existing tests:** `test_checkout_push_cycle` calls
`gax doc push -y`. With patch-first this attempts patch per tab; `-y` skips
confirm. For a document with content, patch will find diffs and apply them. The
test should pass unchanged.

**`tests/test_e2e.py` — add `test_tab_push_bulk_flag`** in `TestDocE2E`:

```python
def test_tab_push_bulk_flag(self, check_auth, test_doc, temp_dir):
    """Test: --bulk -y produces a push with no interactive output."""
    uid = uuid.uuid4().hex[:8]
    # Clone a tab
    tab_name = "Tab 1"  # use first tab of test doc
    tracking_file = temp_dir / f"{E2E_PREFIX}_bulk_{uid}.doc.gax.md"
    result = _run_gax("doc", "tab", "clone", test_doc["url"], tab_name,
                      "-o", str(tracking_file))
    assert result.returncode == 0

    # Modify locally
    content = tracking_file.read_text()
    tracking_file.write_text(content + f"\n\nBulk push test {uid}\n")

    # Push with --bulk -y: should succeed silently
    result = _run_gax("doc", "tab", "push", str(tracking_file), "--bulk", "-y")
    assert result.returncode == 0
    # --bulk -y: diff/warning output suppressed, only success line
    assert "Pushed" in result.stdout or result.stdout.strip() == ""
```

**`tests/` — search for `.warnings` on `DiffPreview`:** Run
`grep -r "\.warnings" tests/` — if any test accesses `DiffPreview.warnings`
directly, update to use `.error` instead.

---

### File change summary

| File | What changes |
|------|-------------|
| `gax/gdoc/diff_push.py` | `DiffPreview`: `warnings: list[str]` → `error: str\|None`, `fatal: bool` |
| `gax/gdoc/diff_push.py` | `preview_diff`: three `DiffPreview(...)` construction sites updated |
| `gax/gdoc/cli.py` | Add `_push_tab_patch_or_bulk` helper function |
| `gax/gdoc/cli.py` | `doc_tab_push`: remove `--patch`, add `--bulk`, rewrite body |
| `gax/gdoc/cli.py` | `doc_push`: add `--bulk`, replace `d.push()` with per-tab loop |
| `tests/test_e2e.py` | Add `test_tab_push_bulk_flag` to `TestDocE2E` |

**No changes to:** `doc.py`, `ir.py`, `native_md.py`, `test_cli_patterns.py`.

### Known limitation after this change

`--body` forces `bulk=True` internally. Patch push with an external body file is
not supported: the patch pipeline reads remote state via `preview_diff` and would
need the external content threaded through to `Tab.push(patch=True, body=body)`.
That is a separate concern; leave a `# TODO: --body with patch` comment at the
forced-bulk point.

## Alternatives Considered

### Keep `--patch` as opt-in indefinitely

**Rejected.** The patch path is now validated. Keeping bulk as default means users
silently lose collaborator formatting on every push. The cost of the wrong default
is high and borne by users, not by the developer.

### Fail hard when patch cannot be applied

When `preview.warnings` is non-empty, abort instead of offering a fallback.
**Rejected.** Structural changes (e.g., adding a new section) are legitimate and
common. Failing hard would make `gax doc tab push` unusable for any document with
structural edits. The fallback path makes it safe: the user is informed and
consents.

### Auto-fall-back without prompt

When patch fails, silently proceed to bulk without asking.
**Rejected.** Bulk destroys non-markdown formatting. The user should be aware
this is happening. The `-y` flag covers the automation case — it opts in to
silent fallback explicitly.

### Per-tab `--bulk` flag for folder push

Require users to specify which tabs get bulk treatment.
**Rejected.** Too granular. The `--bulk` flag on `gax doc push` applies to all
tabs, which is the common case when a structural change spans the document.

## Consequences

**Positive:**
- Collaborator formatting is preserved by default
- Users who do not know the flags get the safer, better behavior
- `--bulk -y` covers automation pipelines that need speed and simplicity
- The patch failure surface is visible and actionable (not a silent abort)

**Negative:**
- First push on a new file will always attempt patch (an extra API call to fetch
  remote state) before proceeding. For net-new content this produces zero ops and
  falls through to bulk. This is one extra `documents().get()` call per push on
  new content. Acceptable cost given the benefit.
- `--patch` flag is removed — any scripts using `--patch` explicitly will need
  to remove the flag (it is now the default) or be updated.

## References

- ADR 023: Markdown-to-Google-Docs Conversion and Testing Strategy
- ADR 027: Diff-Based Document Push
- ADR 030: Markdown Strategy — Unified IR via Mistune
- `gax/gdoc/diff_push.py`: `preview_diff`, `DiffPreview`, `diff_push`
- `gax/gdoc/doc.py`: `Tab.push`, `Doc.push`
- `gax/gdoc/cli.py`: `doc_tab_push`, `doc_push`
