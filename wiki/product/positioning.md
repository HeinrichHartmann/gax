---
title: Positioning
description: What gax is, the problem it solves, and who it is for
status: current
updated: 2026-07-26
sources:
  - README.md
  - ADR/001-gsheet-sync.md
---

## What gax is

gax (Google Access CLI) syncs Google Workspace resources to local files
that are human-readable, LLM-readable, and git-friendly. It covers
Docs, Sheets, Gmail, Calendar, Forms, Tasks, Contacts, Slides, and
Drive.

## Problem

Google Workspace content lives behind browser UIs and proprietary APIs.
There is no first-class way to:

- Read and search Google Docs, Sheets, or Gmail from a terminal or
  editor.
- Let an AI agent read, analyze, or modify Workspace content through
  file operations.
- Track changes to Workspace resources in git.
- Script bulk operations (label hundreds of emails, update spreadsheet
  tabs) without writing throwaway API code.

## Users

- **Developers and power users** who live in the terminal and want
  Workspace content accessible alongside code.
- **AI agents** (Claude, Copilot, etc.) that operate on local files
  and need structured access to Workspace data without direct API
  integration.
- **Teams** that want git-tracked snapshots of Workspace resources for
  auditability or automation.

## Value proposition

- Every synced file is a self-describing `.gax.md` file: YAML header
  with provenance metadata, plain-text body (Markdown, CSV, TSV).
  Human-readable, LLM-readable, diffable.
- `clone`/`checkout`/`pull`/`push` vocabulary borrowed from git makes
  the mental model immediately familiar.
- Push is non-destructive by design: diff preview before write,
  confirmation prompt, patch-level granularity where possible.
- One tool across 10+ resource types, installed via
  `uv tool install`.
