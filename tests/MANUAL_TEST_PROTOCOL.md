# Manual Testing Protocol

## Instructions (for agents running this protocol)

**This is a strict checklist. Execute it exactly as written. Do not improvise, do not add extra tests, do not skip items, do not substitute your own judgment for what should be tested. If a step is unclear, note it in Other findings and move on -- do not invent an alternative.**

1. **Write your run to `TEST_REPORT.md`** in the repo root. This file is gitignored and clobbered on each run -- do not edit this protocol file itself.
2. **Copy the checklist** from this file into `TEST_REPORT.md` as your working document.
3. **Run each test** in order. Execute the exact commands shown -- no variations.
4. **Validate the result carefully** -- don't just check the exit code. Open files, inspect contents, verify against the Google UI where the test says so. A command that "ran" but produced garbage is a failure.
5. **Check the box** (`[x]`) in `TEST_REPORT.md` only if the test passed its validation.
6. **Leave the box unchecked and add a note** directly under the test item if it failed or was skipped. Include the error, exit code, or what was wrong with the output.
7. **Record "other findings"** in the dedicated section: warnings that appeared, surprising output, UX quirks, performance issues, anything a human should know even if the test "passed".
8. **Write the final report** at the bottom of `TEST_REPORT.md` using this structure:

   ```
   ## Report

   18/20 checks passed

   Failed checks:
   - <section>: <test description> -- <what went wrong>
   - ...

   Other findings:
   - ...
   ```

   If all checks passed, write `20/20 checks passed` and omit the "Failed checks" block.

## Scope

gax does not yet have full automated test coverage. This checklist exercises
the advertised read paths before a release, using whatever Google account is
already configured. Push paths are only touched superficially. Unified
commands and unstable features are excluded from baseline testing.

Later this should be converted into an automated smoke test.

## Setup

- [ ] `gax auth status` -- shows authenticated
- [ ] Scratch directory: `mkdir /tmp/gax-smoke && cd /tmp/gax-smoke`
- [ ] Pick existing resources from your own account:
  - A Google Doc with 2+ tabs (`DOC_URL`)
  - A Google Sheet with 2+ tabs (`SHEET_URL`)
  - A recent email thread (`THREAD_URL`)
  - An existing Gmail draft (`DRAFT_URL`)

## Help & manual

- [ ] `gax --version` -- prints version
- [ ] `gax --help` -- lists commands
- [ ] `gax man` -- prints manual

## Mail

- [ ] `gax mail clone THREAD_URL` -- creates `.mail.gax.md`, attachments saved to `~/.gax/store/`
- [ ] `gax mail pull <file>` -- refreshes, no errors
- [ ] `gax mail reply <file>` -- creates a `.draft.gax.md` file (don't send)

## Drafts

- [ ] `gax draft list` -- TSV listing of drafts
- [ ] `gax draft clone DRAFT_URL` -- clones existing draft
- [ ] `gax draft pull <file>` -- refreshes, no errors
- [ ] Push (light): `gax draft new`, fill to/subject/body, `gax draft push <file>`, verify draft appears in Gmail, delete

## Mailbox

- [ ] `gax mailbox clone -q "in:inbox" --limit 10` -- creates list file
- [ ] `gax mailbox pull <file>` -- refreshes
- [ ] `gax mailbox fetch -q "in:inbox" --limit 3 -o threads/` -- materializes full threads

## Mail labels

- [ ] `gax mail-label clone` -- creates `label.mail.gax.md` snapshot of current labels
- [ ] `gax mail-label plan <file>` -- shows plan with no changes

## Mail filters

- [ ] `gax mail-filter clone` -- creates `filter.mail.gax.md` snapshot of current filters
- [ ] `gax mail-filter plan <file>` -- shows plan with no changes

## Calendar

- [ ] `gax cal calendars` -- lists calendars
- [ ] `gax cal clone primary` -- creates `primary.cal.gax.md` with events

## Error handling

- [ ] `gax doc clone https://docs.google.com/document/d/NONEXISTENT/edit` -- readable error, non-zero exit
- [ ] `gax pull nonexistent.gax.md` -- readable error

## Unstable / lower priority

Exercise only when changes touch these areas.

### Forms

- [ ] `gax form clone FORM_URL` -- clone form definition
- [ ] `gax form pull <file>` -- refresh

### Contacts

- [ ] `gax contacts clone` -- clone all contacts
- [ ] `gax contacts pull <file>` -- refresh

### Drive files

- [ ] `gax file clone DRIVE_FILE_URL` -- download file + tracking `.gax.md`
- [ ] `gax file pull <file>` -- refresh

## Cleanup

- [ ] Remove any drafts/files created during testing
- [ ] `rm -rf /tmp/gax-smoke`
