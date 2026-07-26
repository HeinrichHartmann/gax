---
title: ADR Map
description: Status and grouping of all Architecture Decision Records
status: current
updated: 2026-07-26
sources:
  - ADR/001-gsheet-sync.md
  - ADR/002-multipart-markdown-format.md
  - ADR/009-mail-relabel.md
  - ADR/012-unified-pull.md
  - ADR/019-clone-checkout-explode.md
  - ADR/020-mail-command-cleanup.md
  - ADR/021-get-command.md
  - ADR/034-faithful-surgical-push.md
  - ADR/035-faithful-tree-ir.md
---

## Status summary

| Status | Count | ADRs |
|---|---|---|
| Implemented | 1 | 001 |
| Accepted | 7 | 002, 009, 012, 019, 020, 021b, 034, 035 |
| Proposed | 25 | 003-008, 010-011, 013-015, 017-018, 021a, 022-028, 030-032a, 032b |
| Draft | 1 | 016 |

"021b" = `021-get-command.md` (accepted); "021a" = `021-consistent-resource-operations.md` (proposed, superseded by 021b in practice).
"032b" = `032-patch-first-push.md` (proposed, superseded by 034); "032a" = `032-get-command.md` (proposed).

## Accepted ADRs (active design decisions)

| ADR | Title | Key decision |
|---|---|---|
| 001 | gax — Google Access CLI | Founding design: sync Google Sheets to local CSV/Markdown via CLI |
| 002 | Multipart YAML-Markdown Format | Self-describing `.gax.md` files with YAML headers; multipart for threads |
| 009 | Mail Relabel | Bulk relabeling of Gmail threads |
| 012 | Unified Pull Command | Single `gax pull` works across all resource types |
| 019 | Clone vs Checkout Pattern | `clone` creates files, `checkout` creates directories (`.gax.md.d/`) |
| 020 | Mail Command Structure Cleanup | Reorganized mail commands into `mail`, `draft`, `mailbox` groups |
| 021 | `gax get` | Stateless read-only fetch to stdout |
| 034 | Faithful Surgical Push | Pull-time baseline in CAS, three-way diff, run-level splicing for Docs |
| 035 | Faithful Tree IR | Index-free YAML tree as a machine-editing surface for Docs |

## Proposed ADRs by area

### Resource coverage

| ADR | Title | Resource |
|---|---|---|
| 003 | Google Docs Sync | Docs |
| 004 | Gmail Sync | Gmail |
| 006 | Mail Draft Sync | Drafts |
| 007 | Calendar Sync | Calendar |
| 008 | Gmail Filters and Labels as Code | Filters/Labels |
| 010 | Declarative Label Management | Labels |
| 011 | Declarative Gmail Filter Management | Filters |
| 013 | Emacs Major Mode | Editor integration |
| 014 | Google Forms Sync | Forms |
| 017 | Google Contacts Support | Contacts |
| 028 | Google Drive Folder Sync | Drive |
| 031a | Google Slides Support | Slides |
| 031b | Google Tasks Sync | Tasks |

### CLI and file model

| ADR | Title |
|---|---|
| 005 | CLI Structure and Tab-Level Operations |
| 015 | Unified Clone Command |
| 018 | Calendar Date Ranges |
| 022 | Simplified CLI Model (new/pull/diff/push) |
| 024 | File Extension Convention (`.gax.md`) |
| 025 | Directory-Only Collections |
| 026 | Clone Creates Files, Checkout Creates Directories |
| 032a | `get` Command for Stateless Remote Inspection |

### Push and sync architecture

| ADR | Title |
|---|---|
| 023 | Markdown-to-Google-Docs Conversion and Testing |
| 027 | Diff-Based Document Push |
| 030 | Markdown Strategy (Unified IR via Mistune) |
| 032b | Patch-First Push with Bulk Fallback |

### Abstractions

| ADR | Title | Status |
|---|---|---|
| 016 | Resource Abstraction | Draft (not implemented) |
| 021a | Consistent Resource Operations | Proposed (effectively superseded by 021b) |

## ADR numbering note

Two pairs share a number: ADR 021 (`consistent-resource-operations`
and `get-command`) and ADR 031 (`google-slides-support` and
`task-sync`). ADR 032 similarly has `get-command` and
`patch-first-push`. These are independent proposals that happened to
reuse numbers.
