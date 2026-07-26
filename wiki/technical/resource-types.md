---
title: Resource Types
description: All resource types with supported operations, file extensions, and maturity
status: current
updated: 2026-07-26
sources:
  - gax/resource.py
  - gax/cli.py
  - gax/docs.py
---

## Overview

gax has 12 resource groups covering Google Workspace. Each resource
subclass declares dispatch metadata (`URL_PATTERN`, `FILE_TYPE`,
`FILE_EXTENSIONS`) and implements a subset of the standard operations.

## Resource map

| Resource | Class(es) | Extension(s) | clone | checkout | pull | push | diff | get |
|---|---|---|---|---|---|---|---|---|
| doc | Tab, Doc | `.doc.gax.md`, `.tab.gax.md` | yes | yes | yes | yes | yes | yes |
| sheet | SheetTab, Sheet | `.sheet.gax.md`, `.tab.sheet.gax.md` | yes | yes | yes | yes | yes | - |
| mail | Thread | `.mail.gax.md` | yes | - | yes | - | - | - |
| draft | Draft | `.draft.gax.md` | yes | - | yes | yes | yes | - |
| cal | Cal, Event | `.cal.gax.md` | yes | yes | yes | yes | yes | - |
| task | TaskList, Task | `.tasks.gax.md`, `.task.gax.yaml` | yes | yes | yes | yes | yes | - |
| contacts | Contacts | `.contacts.gax.md`, `.contact.gax.yaml` | yes | yes | yes | yes | yes | - |
| form | Form | `.form.gax.md` | yes | - | yes | - | - | - |
| slides | Slide, Presentation | `.slides.gax.md` | yes | yes | yes | yes | - | - |
| file | File, Folder | (varies) | yes | yes | yes | yes | - | - |
| mail-label | Label | `.label.mail.gax.md` | yes | - | yes | - | - | - |
| mail-filter | Filter | `.filter.mail.gax.md` | yes | - | yes | - | - | - |

Labels, filters, forms, and mailbox use a **plan/apply** workflow
instead of direct push. `gax mail-label plan` generates a changeset;
`gax mail-label apply` executes it.

## Two-class resources

Several resource types have two classes: one for a single item and one
for a collection (checkout folder).

| Single-item class | Collection class | Checkout folder |
|---|---|---|
| Tab | Doc | `.doc.gax.md.d/` |
| SheetTab | Sheet | `.sheet.gax.md.d/` |
| Event | Cal | `.cal.gax.md.d/` (per-event files) |
| Task | TaskList | `.tasks.gax.md.d/` |

The collection class handles `checkout` and delegates per-item
operations to the single-item class.

## Maturity

The CLI marks some resource groups as `[unstable]`:

- **file** (Google Drive) — unstable
- **form** (Google Forms) — unstable

All other resources are considered stable.

## Format options

Some resources support multiple output formats via `-f`/`--format`:

| Resource | Formats | Default | Notes |
|---|---|---|---|
| sheet | md, csv, tsv, psv, json, jsonl | md | All tab-separated variants available |
| contacts | md, jsonl | md | md is read-only; jsonl required for push |
| form | md, yaml | md | yaml is round-trip safe |
| task | md, yaml | md | md uses checkbox syntax |
| slides | md, json | md | md read-only; json required for push |

## OAuth scopes

Each resource declares the scopes it needs. `gax auth login` requests
all scopes from registered resources. Scope short names (e.g.
`calendar.readonly`) are expanded to full OAuth URLs by the auth
module.
