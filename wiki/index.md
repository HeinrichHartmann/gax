# Wiki Index

Catalog of all wiki pages. Read this first, then drill into relevant
pages. Maintenance rules: [schema.md](schema.md). Change history:
[log.md](log.md).

## Product

- [Positioning](product/positioning.md) — What gax is, the problem it solves, and who it is for

## Solution

- [CLI Model](solution/cli-model.md) — Command structure, operation patterns, and resource dispatch
- [File Conventions](solution/file-conventions.md) — The .gax.md file format, extensions, and checkout folder structure

## Technical

- [Architecture](technical/architecture.md) — Module structure, key abstractions, and how resource implementations plug in
- [ADR Map](technical/adr-map.md) — Status and grouping of all Architecture Decision Records
- [Resource Types](technical/resource-types.md) — All resource types with supported operations, file extensions, and maturity

## Implementation

- [Docs Push Pipeline](implementation/docs-push-pipeline.md) — How gax pushes edits to Google Docs — IR, patch vs bulk, UTF-16 indexing, tables
- [Mail Parsing](implementation/mail-parsing.md) — Email body extraction, quoted-text stripping, HTML conversion, and attachment handling
