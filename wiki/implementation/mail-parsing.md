---
title: Mail Parsing
description: Email body extraction, quoted-text stripping, HTML conversion, and attachment handling
status: current
updated: 2026-07-26
sources:
  - gax/mail/shared.py
  - gax/mail/draft.py
  - gax/mail/thread.py
  - gax/gaxfile.py
---

## Body extraction priority

`_extract_text_body()` walks the MIME tree with a preference
hierarchy:

1. Exact `text/plain` part — decode and return.
2. Exact `text/html` part (no alternatives) — convert to markdown.
3. Multipart scanning (recursive):
   - First pass: search nested parts for `text/plain`.
   - Second pass: search for `text/html`, convert to markdown.
   - Third pass: recurse into deeper multipart structures.
4. Returns empty string if nothing found (no exception).

Gmail's API can nest `multipart/mixed` within
`multipart/alternative` — the recursive search handles arbitrary
nesting depth.

Base64 body data is decoded with `errors="replace"` so non-UTF-8
bytes produce the Unicode replacement character instead of crashing.

## HTML-to-markdown conversion

Uses `html2text` with two critical settings:

```python
h.body_width = 0        # no line wrapping — preserves structure
h.ignore_links = False   # keep markdown links
```

`body_width = 0` is intentional: automatic wrapping would mangle
nested lists, tables, and preformatted blocks in email HTML.

## Quoted-text stripping

`_strip_quoted_text()` detects and removes reply-quoted content.
Two detection patterns:

1. **RFC 2822 quoting**: lines starting with `>` mark the quote
   boundary. Function returns immediately at first `>` line.
2. **Attribution line** ("On ... wrote:"): scans up to 3 lines ahead
   (Gmail/Outlook can wrap long sender names across multiple lines).

### False-positive prevention

To avoid triggering on prose like "On Monday we wrote: the report",
the function requires **either**:

- An email address (`<...@...>`) found in the lookahead span, **or**
- The line immediately after the attribution block starts with `>`
  quoting.

This dual-condition gate was hardened after false positives in
production (see commit `gax-ami`).

## Content-length for ambiguous bodies

When a message body contains literal `---` on its own line, gax adds
a `content-length` header to the section. Without it, the multipart
parser would misinterpret `---` as a section boundary.

`needs_content_length()` checks for `\n---\n`, leading `---\n`, or
trailing `\n---` in the content. `format_section()` computes and adds
the header automatically. The parser (`parse_multipart`) reads exactly
`content-length` bytes when present, bypassing boundary scanning.

## Attachment handling

Attachments require a separate API call after the initial message
fetch:

1. Walk the MIME part tree recursively (`walk_parts`).
2. For each attachment part, call `attachments().get()` to fetch the
   binary data (not included in `messages.get`).
3. Base64-decode, compute SHA-256 hash.
4. Store to `~/.gax/store/blob/` via `store_blob()` (deduplicated
   by content hash). Returns a `file://` URL.
5. Record metadata: filename, size (from decoded bytes, not base64
   size), MIME type, store URL.

In drafts, attachment paths are resolved **relative to the draft
file's directory** if not absolute. MIME types are guessed from
filenames with `mimetypes.guess_type()`, falling back to
`application/octet-stream`.

## Thread ID extraction

`extract_thread_id()` handles multiple Gmail URL formats:

- Modern Gmail: `#inbox/<alphanumeric_id>`
- URL-encoded: `thread-f%3A<numeric_id>` (where `%3A` = `:`)
- Raw format: `thread-f:<numeric_id>`
- Plain ID: validated as `[A-Za-z0-9]+` and returned directly

Thread IDs have no consistent length — they can be 16-char hex or
15+ char alphanumeric strings.

## Multipart section conventions

Sections in multipart emails are numbered from 1. Only **section 1**
carries the `historyId` (Gmail's sync cursor) in its sync header.
Subsequent sections carry `message_id` but no history tracking.

Date headers are parsed from RFC 2822 format to ISO 8601. Malformed
dates fall through as raw strings rather than failing.
