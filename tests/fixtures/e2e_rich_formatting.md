# Markdown Round-Trip Test Fixture

This fixture covers all supported markdown constructs for Google Docs round-trip testing. Each section isolates one feature. Google Docs normalizes list items to have blank lines between them.

## Headings

### H3 Heading

#### H4 Heading

##### H5 Heading

###### H6 Heading

## Paragraphs

Single paragraph of text.

Two paragraphs with a blank line between them.

Second paragraph here.

A paragraph with a longer sentence that contains multiple clauses, separated by commas, to test line handling.

## Bold

This has **bold** text.

A line with **multiple bold** segments and **more bold** later.

## Italic

This has *italic* text.

A line with *multiple italic* segments and *more italic* later.

## Bold Italic

This has ***bold italic*** text.

Mixed: **bold** then *italic* then ***both*** in one line.

## Unordered Lists

- First item

- Second item

- Third item

## Ordered Lists

1. First item

1. Second item

1. Third item

## Nested Unordered Lists

- Top level A

    - Nested A1

    - Nested A2

- Top level B

    - Nested B1

        - Deep B1a

        - Deep B1b

    - Nested B2

- Top level C

## Nested Ordered Lists

1. First

    1. Sub-first

    1. Sub-second

1. Second

    1. Sub-first

1. Third

## Lists With Formatting

- **Bold item** with text

- *Italic item* with text

- Plain item

- **Bold** and *italic* in one item

## Tables

### Simple Table

| Name | Value |
| :---- | :---- |
| Alpha | 100 |
| Beta | 200 |

### Table With Bold

| Category | Score |
| :---- | :---- |
| **Setup** | 5 |
| **Deploy** | 4 |

### Minimal Table

| A |
| :---- |
| 1 |

### Wide Table

| Col1 | Col2 | Col3 | Col4 | Col5 | Col6 | Col7 | Col8 |
| :---- | :---- | :---- | :---- | :---- | :---- | :---- | :---- |
| a | b | c | d | e | f | g | h |
| **bold** | *italic* | plain | **bold** | *italic* | plain | **bold** | *italic* |

### Table With Emoji

| Status | Meaning | Icon |
| :---- | :---- | :---- |
| 🟢 Pass | All checks OK | ✅ |
| 🟡 Warning | Review needed | ⚠️ |
| 🔴 Fail | Blocking issue | ❌ |
| 🟣 Deferred | Postponed | 🔮 |

### Table With Empty Cells

| # | Requirement | Score | Comment |
| :---- | :---- | :---- | :---- |
| **Setup** |  |  |  |
| R01 | **Onboard a new user** | 🟠 2 | Not self-explanatory |
| R02 | Set up dev environment |  |  |
| **Workflow** |  |  |  |
| R03 | Install dependencies | 🟡 3 | Public packages work |

## Emoji

Inline emoji: ✅ done, 🟢 pass, 🟡 warning, 🟠 caution, 🔴 fail, ⛔ blocked, ⬜ skipped.

## Special Characters

Prices: $100, $1,000, $10,000.00

Percentages: 50%, 99.9%

Underscores: ________

## Mixed Structures

Text before a list.

- Item A

- Item B

Text between list and table.

| X | Y |
| :---- | :---- |
| 1 | 2 |

Text after a table.

1. Ordered after table

1. Second item

Final paragraph.
