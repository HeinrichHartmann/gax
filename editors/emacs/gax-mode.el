;;; gax-mode.el --- Major mode for gax files -*- lexical-binding: t; -*-

;; Author: Heinrich Hartmann
;; URL: https://github.com/HeinrichHartmann/gax
;; Version: 0.1.0
;; Package-Requires: ((emacs "27.1") (polymode "0.2.2") (markdown-mode "2.5") (yaml-mode "0.0.15") (csv-mode "1.21"))
;; Keywords: tools, google, sync

;;; Commentary:

;; Major mode for editing gax (Google Access CLI) files.
;; Provides polymode support for YAML headers with format-specific bodies.
;;
;; Gax files have a YAML frontmatter header followed by a body in various
;; formats (markdown, CSV, TSV, JSON, etc.).  The `format:' field in the
;; header determines the body mode.
;;
;; Keybindings:
;;   C-c C-c  - gax pull (update from source)
;;
;; Installation (Doom Emacs):
;;   (package! gax-mode :recipe
;;     (:host github :repo "HeinrichHartmann/gax" :files ("editors/emacs/*.el")))
;;
;; Installation (straight.el):
;;   (straight-use-package
;;    '(gax-mode :host github :repo "HeinrichHartmann/gax" :files ("editors/emacs/*.el")))

;;; Code:

(require 'polymode)
(require 'yaml-mode)
(require 'markdown-mode)

(defgroup gax nil
  "Major mode for gax files."
  :group 'tools
  :prefix "gax-")

(defcustom gax-executable "gax"
  "Path to gax executable."
  :type 'string
  :group 'gax)

;;; Commands

(defun gax-pull ()
  "Run `gax pull' on current file."
  (interactive)
  (save-buffer)
  (let ((file (buffer-file-name)))
    (if file
        (compile (format "%s pull %s" gax-executable (shell-quote-argument file)))
      (error "Buffer is not visiting a file"))))

;;; Content-type detection

(defun gax--parse-content-type ()
  "Parse the `content-type:' field from the YAML header.
Returns the MIME type string or nil if not found."
  (save-excursion
    (goto-char (point-min))
    (when (looking-at "^---\n")
      (forward-line 1)
      (let ((header-end (save-excursion
                          (if (re-search-forward "^---\n" nil t)
                              (match-beginning 0)
                            (point-max)))))
        (when (re-search-forward "^content-type:\\s-*\\(.+\\)$" header-end t)
          (string-trim (match-string 1)))))))

(defun gax--content-type-to-mode (content-type)
  "Return the appropriate major mode for CONTENT-TYPE MIME string."
  (pcase content-type
    ((or "text/csv" "text/tab-separated-values") 'csv-mode)
    ("application/json" 'js-mode)  ; js-mode is built-in
    ("application/yaml" 'yaml-mode)
    (_ 'markdown-mode)))

(defun gax--content-type-tabular-p (content-type)
  "Return non-nil if CONTENT-TYPE is a tabular format (CSV/TSV)."
  (member content-type '("text/csv" "text/tab-separated-values")))

;;; Polymode definitions

;; Inner mode for YAML header (between --- markers)
(define-innermode gax--yaml-innermode
  :mode 'yaml-mode
  :head-matcher "\\`---\n"
  :tail-matcher "^---\n"
  :head-mode 'host
  :tail-mode 'host)

;; Host mode - defaults to markdown, but we'll override based on format
(define-hostmode gax--hostmode
  :mode 'markdown-mode)

;; Polymode definition
(define-polymode gax-mode
  :hostmode 'gax--hostmode
  :innermodes '(gax--yaml-innermode))

;; Keymap
(defvar gax-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-c") #'gax-pull)
    map)
  "Keymap for `gax-mode'.")

;; Hook to set up content-type-specific body mode
(defun gax--setup-body-mode ()
  "Set up the body mode based on the content-type: header field."
  (let ((content-type (gax--parse-content-type)))
    ;; Enable csv-align-mode for tabular data
    (when (gax--content-type-tabular-p content-type)
      (when (fboundp 'csv-mode)
        ;; Set separator for TSV
        (when (equal content-type "text/tab-separated-values")
          (setq-local csv-separators '("\t")))
        ;; Enable alignment
        (when (fboundp 'csv-align-mode)
          (csv-align-mode 1)))
      ;; Disable visual-fill-column-mode for tabular data
      (when (bound-and-true-p visual-fill-column-mode)
        (visual-fill-column-mode -1))
      ;; Truncate long lines (no wrapping)
      (setq-local truncate-lines t))))

(add-hook 'gax-mode-hook #'gax--setup-body-mode)

;; Auto-mode registration
(add-to-list 'auto-mode-alist '("\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.mail\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.sheet\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.doc\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.draft\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.cal\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.tab\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.form\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.label\\.mail\\.gax\\.md\\'" . gax-mode))
(add-to-list 'auto-mode-alist '("\\.filter\\.mail\\.gax\\.md\\'" . gax-mode))

(provide 'gax-mode)
;;; gax-mode.el ends here
