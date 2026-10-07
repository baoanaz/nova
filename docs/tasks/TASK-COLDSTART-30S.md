# Complete Tool cold start: 30-second target

User authorization: implement graph batching, first-build SQL paths, tokenizer,
transaction changes and scan/upload/index pipelining in separate commits. Preserve
complete index/graph/query results, durability, failure isolation and recovery.
The task is not complete until the complete Tool meets the target.

## Fixed experiment

- Frozen runtime snapshot: `ee130cb86532e7ce7213bdc510d2ca08227add4d` (isolated
  worktree; the main worktree's ongoing Nova rename/UI changes are untouched).
- LangChain: `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`.
- Independent read-only fixture: `.local/fixtures/langchain-voyage-4-lite.sqlite`.
  Its SHA256 is recorded in each suite's `conditions.json`; misses fail locally.
- Original debug and release binaries are frozen and SHA256 recorded. Release
  retains the original Cargo profile (`opt-level=z`, LTO, one codegen unit).
- Fresh server/client processes, SQLite/LanceDB/shared embedding/client caches
  for every sample. File-data cache uses `POSIX_FADV_DONTNEED` on the fixture and
  tracked corpus before each sample; no global cache drop or vector preloading.
  This is an advisory OS policy, not a claim of physically identical disk state.
- Vector fixture decoding and all runtime index/vector/graph writes occur inside
  the complete Tool measurement. Server startup is outside the timer, as in the
  original 52.71s benchmark; no project is prebuilt. Timing runs from client launch
  through response and client exit. Required checkpoint/readiness/query work stays
  in the existing real client path.
- No phase/native tracer. Two embedding workers and cache enabled in all cases.
- systemd: localhost-only network, MemoryHigh=1200M, MemoryMax=1500M,
  MemorySwapMax=256M. Builds, tests and timed suites run serially.
- Logical SQLite contents, all vectors and Tool output are fingerprinted AFTER
  timing in a separate process. Only indexed timestamps, internal unresolved-ref
  IDs and the human-readable `fresh (Ns ago)` age are excluded. Freshness class,
  retrieved content/order, graph properties and vectors remain part of equality.
- Generated runtime stores are removed after verification with a link audit;
  raw responses, reports, hashes, timing samples and logs remain.

The earlier `results/baseline` pilot is excluded: its verifier retained library
state in the suite parent. Official baseline is `results/baseline-fixed`.

## Original baseline (seconds)

| Mode | Run 1 | Run 2 | Run 3 | Median |
|---|---:|---:|---:|---:|
| debug | 50.650379 | 53.642454 | 51.414023 | 51.414023 |
| release | 56.006809 | 68.116407 | 53.436782 | 56.006809 |

All logical tables and vectors match across all six runs. Tool output differs
only in elapsed-age text. Release's server ingest wall time varied substantially
(46.16/58.22/43.26s); these samples do not establish that release compilation makes
the service slower. Report code gains within the SAME compilation mode, and keep
all individual samples visible rather than subtracting build modes.

## Graph batch implementation

- Add `Store.resolve_ref_targets`: batch prefetch reference metadata, insert all
  synthesized edges with existing conflict behavior, then delete consumed refs
  in the same transaction. Original `resolve_refs` API stays unchanged. No schema
  change. The user's explicit batch-processing authorization covers this storage
  API extension. A failed new batch rolls back and retains its source refs for
  retry; already committed unrelated graph state remains intact.
- Batch spec-reference existence checks through the existing symbol index and
  use plain INSERT. The schema has NO composite unique constraint, so blindly
  replacing the old logic with INSERT OR IGNORE would not preserve deduplication.
  Existing stale/provenance values and insertion errors are preserved.
- Share one graph-pass symbol lookup cache. Its lifetime ends with the graph
  pass; no symbol cache survives a later file mutation or ingest.
- Tests cover legacy fan-out equivalence, duplicate/missing refs, prior edge
  provenance, NULL line conflicts, injected failures, reopen/retry, outer
  rollback, >500 reference IDs and >1000 spec pairs.

Validation before performance run: 66 core resolver/storage/pipeline tests and
13 service deferred-sync recovery tests passed. Independent advisor was called
with the patch only (no repository indexing), but returned HTTP 502; no independent
review approval is claimed.

Graph commit `ecbb582`, all content equality checks passed:

| Mode | Run 1 | Run 2 | Run 3 | Median | Saved vs original |
|---|---:|---:|---:|---:|---:|
| debug | 46.370635 | 50.978797 | 64.333217 | 50.978797 | 0.435226 |
| release | 64.776540 | 55.073626 | 48.992020 | 55.073626 | 0.933182 |

These small differences are not reliable gains given the observed variation.
The change remains experimental; later acceptance must confirm its end-to-end
contribution or remove it. The remaining median gaps are 20.98s/25.07s.

## SQL batch candidate

File writes now use bounded multi-row statements for chunks, symbols, spec
blocks, edges and multi-chunk FTS payloads. Existing deletion/overwrite and
SAVEPOINT/outer-transaction boundaries remain unchanged. FTS row IDs are mapped
by chunk identity, independent of RETURNING ordering. SQLite before 3.35 uses a
post-insert indexed lookup instead of requiring RETURNING. The connection's real
parameter limit bounds each statement.

105 existing storage/resolver/pipeline tests passed. Three additional cases
cover lowered parameter limits, reordered RETURNING rows, older SQLite fallback,
and a late-statement constraint failure that restores the old file and permits
retry. Commit `2ce75ab` passed all six complete-Tool content checks:

| Mode | Run 1 | Run 2 | Run 3 | Median | Saved vs original |
|---|---:|---:|---:|---:|---:|
| debug | 46.172572 | 45.310423 | 45.865514 | 45.865514 | 5.548509 |
| release | 41.582043 | 41.049195 | 41.286079 | 41.286079 | 14.720730 |

This combines graph+SQL. The original release baseline's variation means the
entire 14.72s difference cannot confidently be attributed to code alone. Final
acceptance will use interleaved controls. Remaining gaps: 15.87s/debug and
11.29s/release. Untimed cgroup counters showed zero memory.high/max/OOM events;
whole-probe full I/O pressure was 3.99–5.30s (includes setup, not an additive Tool
stage). No phase probes were enabled.

## Rejected page-cache experiment

Commit `4536e07` temporarily raised SQLite cache to 32 MiB only for an empty
initial write batch and restored it after commit/rollback. 116 tests passed,
including abrupt process exit and replay, and all six Tool content checks passed.

| Mode | Run 1 | Run 2 | Run 3 | Median | Change vs SQL version |
|---|---:|---:|---:|---:|---:|
| debug | 54.899745 | 54.309725 | 51.737064 | 54.309725 | 8.444211 slower |
| release | 43.338594 | 44.781601 | 50.083057 | 44.781601 | 3.495523 slower |

No end-to-end benefit was demonstrated, so `56d897c` reverted the candidate.
The frozen worktree/results remain for audit; production code retains the
original cache policy. Do not claim the cache experiment as an optimization.

## Independent review

AI3's `2db64fe` review was received as `c7e72b8`. Its new graph parameter-limit
regression is fixed, with 17 passed/4 xfailed in targeted review/batch tests.
The four xfails document three pre-existing graph ownership/error-handling
issues; they are not claimed fixed. See `AI3-RECOVERY-REVIEW-2ce75ab.md`.

## Reproduction

Use `benches/embed-bench/acceptance_suite.py` with `--source` pointing to the
frozen runtime worktree, `--repo`, `--fixture`, `--bins`, and a NEW `--out`.
The bins directory contains `baseline-debug` and `baseline-release`. It invokes
`sync_probe.py --client <binary>` with phase timing disabled. Compare suites using
`compare_acceptance.py <baseline-directory> <candidate-directory>`; its content
checks must pass before a performance improvement is accepted.
