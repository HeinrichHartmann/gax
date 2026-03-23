# ADR 013: Emacs Major Mode

## Status

Proposed

## Context

Gax files have a distinctive structure: YAML frontmatter followed by a body in various formats
(markdown, CSV, TSV, etc.). Standard text editors don't provide:

- Syntax highlighting for both YAML header and format-specific body
- Keybindings for gax operations (pull)
- Format-aware editing features

An Emacs major mode would provide a native editing experience for gax files, leveraging
polymode for proper multi-mode support.

## Decision

### Package Structure

Ship as a standard Emacs package within the gax repository:

```
gax/
  editors/
    emacs/
      gax-mode.el
```

### Dependencies

| Package | Purpose | MELPA |
|---------|---------|-------|
| `polymode` | Multi-mode support (YAML header + body) | Yes |
| `yaml-mode` | YAML syntax highlighting | Yes |
| `markdown-mode` | Markdown body highlighting | Yes |

All dependencies are available on MELPA and included in Doom Emacs by default.

### Polymode Architecture

```
┌─────────────────────────────────┐
│ gax-mode (polymode)             │
├─────────────────────────────────┤
│ ---                             │  ← yaml-mode innermode
│ type: gax/sheet                 │
│ format: tsv                     │
│ source: https://...             │
│ ---                             │
├─────────────────────────────────┤
│ name    email                   │  ← hostmode (tsv-mode)
│ Alice   alice@example.com       │
│ Bob     bob@example.com         │
└─────────────────────────────────┘
```

**V1 Scope:** Single header + body only. Multipart files (multiple `---` sections) will
display but only the first header gets yaml-mode treatment.

### Content-Type Header Convention

All gax files use `content-type:` with standard MIME types (HTTP style) to specify
the body format:

```yaml
---
type: gax/list
content-type: text/tab-separated-values
source: https://...
---
```

| Content-Type | Body Mode | Features |
|--------------|-----------|----------|
| `text/csv` | `csv-mode` + `csv-align-mode` | Aligned columns |
| `text/tab-separated-values` | `csv-mode` + `csv-align-mode` | Aligned columns |
| `application/json` | `js-mode` | JSON syntax |
| `application/yaml` | `yaml-mode` | YAML syntax |
| (absent) | `markdown-mode` | Default |

Markdown is the default and does not require an explicit `content-type:` field.

For tabular data (CSV/TSV), `csv-align-mode` is enabled by default to visually
align columns without modifying the file content.

### Keybindings

| Key | Command | Description |
|-----|---------|-------------|
| `C-c C-c` | `gax-pull` | Update file from source |

### Commands

```elisp
(defun gax-pull ()
  "Run `gax pull` on current file."
  (interactive)
  (save-buffer)
  (compile (format "gax pull %s" (shell-quote-argument (buffer-file-name)))))
```

### Auto-Mode Registration

```elisp
(add-to-list 'auto-mode-alist '("\\.gax\\'" . gax-mode))
```

### Installation

**Doom Emacs** (`~/.doom.d/packages.el`):
```elisp
(package! gax-mode :recipe
  (:host github :repo "HeinrichHartmann/gax" :files ("editors/emacs/*.el")))
```

**straight.el**:
```elisp
(straight-use-package
 '(gax-mode :host github :repo "HeinrichHartmann/gax" :files ("editors/emacs/*.el")))
```

**use-package + straight**:
```elisp
(use-package gax-mode
  :straight (:host github :repo "HeinrichHartmann/gax" :files ("editors/emacs/*.el"))
  :mode "\\.gax\\'")
```

**Manual**:
```elisp
(add-to-list 'load-path "/path/to/gax/editors/emacs")
(require 'gax-mode)
```

### Package Header

```elisp
;;; gax-mode.el --- Major mode for gax files -*- lexical-binding: t; -*-

;; Author: Heinrich Hartmann
;; URL: https://github.com/HeinrichHartmann/gax
;; Version: 0.1.0
;; Package-Requires: ((emacs "27.1") (polymode "0.2.2") (markdown-mode "2.5") (yaml-mode "0.0.15"))
;; Keywords: tools, google, sync

;;; Commentary:
;; Major mode for editing gax (Google Access CLI) files.
;; Provides polymode support for YAML headers with format-specific bodies.
```

## Consequences

- **Native feel**: Proper syntax highlighting for both header and body
- **Efficient workflow**: Pull without leaving Emacs
- **Format-aware**: Body highlighting matches `format:` header field
- **Easy install**: Works with standard Emacs package managers
- **Consistent headers**: All gax files use `format:` field for body type

## Future Considerations

- **Push support**: `C-c C-p` for sheets/docs/drafts
- **Multipart support**: Highlight all YAML sections in multipart files
- **Imenu integration**: Navigate between sections in multipart files
- **Eldoc**: Show field documentation on hover
- **Flycheck**: Validate YAML header structure
- **MELPA submission**: Once stable, submit to MELPA for easier discovery
