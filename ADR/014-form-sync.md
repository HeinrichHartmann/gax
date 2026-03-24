# ADR 014: Google Forms Sync

## Status

Proposed

## Context

gax supports Google Docs, Sheets, Gmail, and Calendar. Google Forms is another key Workspace tool for surveys, quizzes, and data collection. Users need to:

1. View form structure locally (questions, options, settings)
2. Version control form definitions
3. Potentially edit forms via CLI (future)

The [Google Forms API](https://developers.google.com/workspace/forms) provides:
- `forms.get()` - Retrieve form structure and settings
- `forms.batchUpdate()` - Modify form content
- `forms.responses` - Access submitted responses (out of scope for now)

This ADR proposes form definition sync, focusing on faithful round-trip of form structure.

## Constraints

1. **Faithful representation** - Form definition must round-trip without data loss
2. **Human readable** - Easy to review form structure
3. **AI editable** - Structured format that LLMs can modify
4. **Consistent with gax patterns** - Follow established clone/pull/push conventions

## Decision

### CLI Structure

```
gax form
├── clone <id-or-url>              # Clone form definition → .form.gax
├── pull <file>                    # Update from API
└── push <file>                    # Push changes (future)
```

**Note:** No `gax form list` command - forms are discovered via Drive (`gax drive search "mimeType='application/vnd.google-apps.form'"`).

### File Format (.form.gax)

YAML header with form metadata, body contains form structure:

```yaml
---
type: gax/form
id: "1FAIpQLSc..."
title: "Customer Feedback Survey"
source: https://docs.google.com/forms/d/1FAIpQLSc.../edit
synced: 2026-03-24T10:00:00Z
content-type: application/yaml
---
documentTitle: "Customer Feedback Survey"
info:
  title: "Customer Feedback Survey"
  description: "Please share your feedback"
settings:
  quizSettings:
    isQuiz: false
items:
  - itemId: "abc123"
    title: "How satisfied are you?"
    questionItem:
      question:
        questionId: "q1"
        required: true
        scaleQuestion:
          low: 1
          high: 5
          lowLabel: "Not satisfied"
          highLabel: "Very satisfied"
  - itemId: "def456"
    title: "What could we improve?"
    questionItem:
      question:
        questionId: "q2"
        required: false
        textQuestion:
          paragraph: true
  - itemId: "ghi789"
    pageBreakItem: {}
  - itemId: "jkl012"
    title: "Select all that apply"
    questionItem:
      question:
        questionId: "q3"
        choiceQuestion:
          type: CHECKBOX
          options:
            - value: "Speed"
            - value: "Quality"
            - value: "Price"
            - value: "Support"
```

### Content Format Options

**Default: Markdown (read-only, human-friendly)**

```yaml
---
type: gax/form
content-type: text/markdown
---
# Customer Feedback Survey

Please share your feedback

---

## 1. How satisfied are you? *

Scale: 1 (Not satisfied) - 5 (Very satisfied)

## 2. What could we improve?

_Long answer text_

---

## 3. Select all that apply

- [ ] Speed
- [ ] Quality
- [ ] Price
- [ ] Support
```

**YAML format (round-trip safe, for push support)**

Use `--format yaml` or `-f yaml` with clone:

```bash
gax form clone FORM_ID -f yaml    # Full YAML structure
gax form clone FORM_ID            # Markdown (default)
```

Only YAML format supports `push` - markdown is view-only.

### Question Type Mapping

| API Type | Markdown Representation |
|----------|------------------------|
| `textQuestion` (short) | _Short answer text_ |
| `textQuestion` (paragraph) | _Long answer text_ |
| `scaleQuestion` | Scale: 1 (low) - 5 (high) |
| `choiceQuestion` RADIO | - ( ) Option 1 |
| `choiceQuestion` CHECKBOX | - [ ] Option 1 |
| `choiceQuestion` DROP_DOWN | Dropdown: Option 1, Option 2 |
| `dateQuestion` | _Date_ |
| `timeQuestion` | _Time_ |
| `fileUploadQuestion` | _File upload_ |
| `rowQuestion` (grid) | Grid layout |

### Field Mapping (YAML format)

| Field | Type | Editable | Notes |
|-------|------|----------|-------|
| `id` | string | No | Form ID (from URL) |
| `documentTitle` | string | Yes | Form document title |
| `info.title` | string | Yes | Displayed form title |
| `info.description` | string | Yes | Form description |
| `settings` | object | Yes | Quiz settings, response settings |
| `items` | array | Yes | Questions and sections |
| `items[].itemId` | string | No* | Question/item ID |
| `items[].title` | string | Yes | Question text |
| `items[].description` | string | Yes | Help text |
| `items[].questionItem` | object | Yes | Question configuration |

*Item IDs are assigned by API on creation.

### OAuth Scope

Add to `auth.py`:

```python
"https://www.googleapis.com/auth/forms.body.readonly"  # Read form structure
# Future: "https://www.googleapis.com/auth/forms.body" for push
```

### Implementation Notes

1. **Form Discovery**: Use Drive API to list forms:
   ```python
   service.files().list(q="mimeType='application/vnd.google-apps.form'")
   ```

2. **Form ID Extraction**: Support multiple URL formats:
   - `https://docs.google.com/forms/d/{FORM_ID}/edit`
   - `https://docs.google.com/forms/d/{FORM_ID}/viewform`
   - Raw form ID

3. **YAML Serialization**: Use `ruamel.yaml` for round-trip fidelity (preserves comments, ordering).

### Example Workflow

```bash
# Clone form for review
gax form clone "https://docs.google.com/forms/d/1FAI.../edit"
# Creates: Customer_Feedback_Survey.form.gax

# View form structure
cat Customer_Feedback_Survey.form.gax

# Update local copy
gax pull Customer_Feedback_Survey.form.gax

# Or unified pull
gax pull .
```

### Future: Push Support

```bash
# Edit YAML file locally
vim survey.form.gax

# Push changes (shows diff, prompts for confirmation)
gax form push survey.form.gax
```

Push would use `forms.batchUpdate()` to apply changes. This requires:
- Detecting added/removed/modified items
- Generating appropriate update requests
- Handling item ID assignment for new questions

## Consequences

### Positive

- **Version control** - Form definitions in git
- **Review workflow** - See form changes in PRs
- **AI assistance** - LLMs can analyze/modify form structure
- **Backup** - Local copy of form definitions
- **Consistent UX** - Same clone/pull pattern as other gax resources

### Negative

- **New OAuth scope** - Users need to re-authenticate
- **Complex structure** - Forms API has many question types
- **Read-only initially** - Push requires more work
- **No responses** - Form submissions not included (separate concern)

### Future Extensions

- `gax form push` - Push changes to API
- `gax form responses clone` - Export responses to TSV
- `gax form new` - Create new form from template
- Quiz answer key support

## References

- Google Forms API: https://developers.google.com/workspace/forms
- Forms API Reference: https://developers.google.com/workspace/forms/api/reference/rest
- ADR 003: GDoc Sync (similar pattern)
- ADR 012: Unified Pull Command
