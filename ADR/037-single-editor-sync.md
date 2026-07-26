# ADR 037: Single-Editor Sync — Simplify the Push/Pull Model

## Status

Proposed. Supersedes the three-way/drift sections of ADR 034 (§2
drift rebasing, conflict classification). Guiding rule for this ADR:
**if it can be simpler, it must be simpler.**

## Context

gax documents have a single editor in practice. ADR 034 nevertheless
grew multi-editor machinery: drift detection, disjoint-drift
rebasing, alignment-based index transfer, conflict block
classification. That machinery cost ~1,000 lines, one P0 revision
cycle, and a permanent "baseline exists?" dual code path — insurance
against concurrent editing that does not happen.

The concurrency mechanism we actually wanted already exists: a
revision stamp taken at pull time, checked at push time.

## The Model

One loop, two guards, one sentence each:

```
pull  →  edit  →  push
```

- **push** refuses when the remote moved since your pull:
  `Remote changed (rev abc → def). Pull first.`
- **pull** refuses when you have unpushed local edits:
  `You have unpushed local edits. Push first, or pull --force to
  discard them.`

Nothing merges. Nothing rebases. Nothing is silently destroyed in
either direction.

## The Push Path, Before and After

```
BEFORE (ADR 034):                       AFTER:
fetch remote + revisionId               fetch remote + revisionId
load baseline from CAS                  revision != stamp?  → refuse
render baseline → base md               diff remote-rendered vs local
diff base vs local                      splice → batchUpdate
revision changed?                       re-stamp + refresh baseline
  → compute drift blocks
  → classify overlap
  → disjoint: align base↔remote
    blocks, remap doc_ranges
  → overlap: abort w/ block list
map mutations via alignment
splice → batchUpdate
```

Under the guard, the remote *is* the state you pulled — so diffing
against the remote directly gives exactly your edits, with correct
indexes, no baseline load, no alignment, no drift logic. Push has
**one code path**; the "no baseline" fallback branch disappears.

## What Stays, and Its Single Job

| Kept | Job |
|---|---|
| Run-level splicing | edits touch only changed spans — the anti-corruption core |
| `revision:` stamp | the lock |
| Baseline (CAS) + post-push refresh | **only** consumer: pull's unpushed-edit guard (`local != render(baseline)` ⇒ you have edits) |
| Tree IR | the faithful surface (ADR 035) |

## What Gets Deleted

- Drift detection/classification in `compute_three_way_plan`
  (render-remote-and-re-diff, `drift_blocks`, overlap sets)
- Alignment-based base↔remote `doc_range` transfer (only existed to
  survive drift)
- Conflict block-listing and its error surface
- Baseline loading from the push path (pull keeps it)
- All tests exercising the above (~500 lines)

## Consequences

- Push explanation fits in one sentence; contributors reason about
  local/remote, not base/local/remote.
- If multi-editor ever becomes real, the guard degrades gracefully:
  you pull more often. The deleted design remains documented in
  ADR 034 and the git history — nothing is lost, only carried.
- Removal is one bead, executed after the in-flight CLI/tree/fidelity
  beads land (all touch the same file).
