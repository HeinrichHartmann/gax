# ADR 013: Emacs Major Mode

## Status

Proposed

## Context

Gax files have a distinctive structure: YAML frontmatter followed by a body in various formats
(markdown, CSV, TSV, etc.). Standard text editors don't provide:

- Syntax highlighting for both YAML header and format-specific body
- Keybindings for gax operations (pull, push)
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
│ format: csv                     │
│ source: https://...             │
│ ---                             │
├─────────────────────────────────┤
│ name,email,role                 │  ← hostmode (format-specific)
│ Alice,alice@example.com,admin   │
│ Bob,bob@example.com,user        │
└─────────────────────────────────┘
```

**V1 Scope:** Single header + body only. Multipart files (multiple `---` sections) will
display but only the first header gets yaml-mode treatment.

### Format Detection

The body mode is determined by header fields:

| Header Field | Value | Body Mode |
|--------------|-------|-----------|
| `format` | `csv`, `tsv`, `psv` | `csv-mode` |
| `format` | `json`, `jsonl` | `json-mode` |
| `format` | `md`, `markdown` | `markdown-mode` |
| `type` | `gax/mail`, `gax/doc` | `markdown-mode` |
| `type` | `gax/labels`, `gax/filters` | `yaml-mode` |
| (default) | - | `markdown-mode` |

Format is read from the YAML header on mode initialization.

### Keybindings

| Key | Command | Description |
|-----|---------|-------------|
| `C-c C-c` | `gax-pull` | Update file from source |
| `C-c C-p` | `gax-push` | Push changes to source |
| `C-c C-d` | `gax-diff` | Show diff with remote |

### Commands

```elisp
(defun gax-pull ()
  "Run `gax pull` on current file."
  (interactive)
  (save-buffer)
  (compile (format "gax pull %s" (shell-quote-argument (buffer-file-name)))))

(defun gax-push ()
  "Run appropriate push command based on file type."
  (interactive)
  (save-buffer)
  (let* ((file (buffer-file-name))
         (cmd (cond
               ((string-match-p "\\.sheet\\.gax$" file) "sheet tab push")
               ((string-match-p "\\.tab\\.gax$" file) "doc tab push")
               ((string-match-p "\\.draft\\.gax$" file) "mail draft push")
               (t (error "Push not supported for this file type")))))
    (compile (format "gax %s %s" cmd (shell-quote-argument file)))))
```

### Auto-Mode Registration

```elisp
(add-to-list 'auto-mode-alist '("\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.mail\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.sheet\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.doc\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.draft\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.cal\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.tab\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.label\\.mail\\.gax\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.filter\\.mail\\.gax\\'" . gax-mode))
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
- **Efficient workflow**: Pull/push without leaving Emacs
- **Format-aware**: Body highlighting matches actual content format
- **Easy install**: Works with standard Emacs package managers
- **No MELPA**: GitHub-only distribution initially (can add MELPA later)

## Future Considerations

- **Multipart support**: Highlight all YAML sections in multipart files
- **Imenu integration**: Navigate between sections in multipart files
- **Eldoc**: Show field documentation on hover
- **Flycheck**: Validate YAML header structure
- **Company**: Auto-complete header fields
- **MELPA submission**: Once stable, submit to MELPA for easier discovery
