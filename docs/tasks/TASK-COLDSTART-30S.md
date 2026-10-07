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

## Checkpoint: tokenizer integrated, pipeline pending

AI2 `df71b42` was received as `c817e37`. Targeted integrated validation:
103 core tests passed / 4 pre-existing xfails, plus 33 service usage/deferred-sync
tests passed. All six full-corpus checks match logical SQLite contents, raw FTS
text, all 20,931 vectors and Tool retrieval output (elapsed-age display excluded).

| Mode | Run 1 | Run 2 | Run 3 | Median | Saved vs original | Gap to 30 |
|---|---:|---:|---:|---:|---:|---:|
| debug | 51.026994 | 48.774738 | 45.117031 | 48.774738 | 2.639285 | 18.774738 |
| release | 38.288822 | 39.214250 | 39.106592 | 39.106592 | 16.900217 | 9.106592 |

Relative to the SQL version, release median improved 2.179487s while debug
worsened 2.909225s. These sequential cohorts have visible host variability;
final interleaved controls and removal of non-contributing changes remain due.
No <30s claim is made. The measured configuration is the original release
profile plus graph/SQL/tokenizer changes, without the rejected page-cache change.

**Timing interpretation:** this is offline complete-Tool replay using existing
real Voyage vectors. Fixture lookup, float materialization, runtime vector writes,
scan/upload/index/graph/checkpoint/readiness/query/return are timed. Remote
embedding inference, remote network transfer and HTTP response decoding are not.
Consequently 39.11s is neither a paid Voyage end-to-end result nor a duration to
which an independently measured API duration can simply be added (work overlaps).
No paid embedding calls were made in these experiments.

AI1 delivered `f3dc9fc` on `ai1/bounded-sync-pipeline`, but it is NOT integrated
or performance-accepted. Its handoff runs a complete ingest for each batch.
A minimal real Store/resolver comparison demonstrates a semantic blocker:

- caller references bare `helper`; helpers `a.helper` and `b.helper` are in
  different files from the caller.
- One final graph pass after all files produces both synthesized target edges.
- Resolving caller + `a.helper` first, then adding `b.helper`, leaves only the
  first target edge with parsed provenance. The consumed reference is not retried.

Thus per-batch complete ingest cannot be accepted as equivalent merely because
its mocked protocol tests pass. Preserve the useful session/seal/backpressure
work, but ensure graph resolution sees the complete symbol set and preserve file
ordering/recovery boundaries before integration. Rust compilation, heavy recovery
tests and full pipeline timings remain outstanding. AI1's branch is retained
separately as work in progress, not deployed.

The checkpoint was backed up locally and pushed remotely. At the user's explicit
request, the effective optimization commits were then cherry-picked directly onto
`main`, preserving its newer UI/branding changes. The frozen-runtime bootstrap,
rejected cache experiment and unaccepted AI1 pipeline were not included. Main's
five changed runtime modules match the measured integration version exactly.
Main regression checks: 201 core tests passed / 4 documented pre-existing xfails,
plus 33 service tests passed. The 30-second target remains unmet.

`paired_acceptance.py` is ready for final control/candidate runs: three trials
per mode and side with alternating pair order. Its 12-trial scheduling logic was
validated with a temporary mocked runner; real paired acceptance is NOT yet run.

## Reproduction

Use `benches/embed-bench/acceptance_suite.py` with `--source` pointing to the
frozen runtime worktree, `--repo`, `--fixture`, `--bins`, and a NEW `--out`.
The bins directory contains `baseline-debug` and `baseline-release`. It invokes
`sync_probe.py --client <binary>` with phase timing disabled. Compare suites using
`compare_acceptance.py <baseline-directory> <candidate-directory>`; its content
checks must pass before a performance improvement is accepted.

## Round 4 candidate: arrow-native vectors, base64, startup preload (NOT yet measured)

Implemented without VPS access; all timings below are local micro-benchmarks only.
The VPS run decides acceptance. Code-only analysis motivating it: the previous
architecture report measured ~38 service CPU core-seconds for ~52s wall on 2 vCPU,
i.e. the run is effectively GIL-serial, so Python-level CPU removed from vector
workers directly frees the main (parse/SQLite) thread.

1. **Vector path without Python floats.** Each 1024-d vector used to be converted
   to Python floats several times per window (`list(vector)`, `[float(v)…]` dict
   rows for LanceDB, again for the shared cache, plus pyarrow inference).
   `EmbeddingSink` now keeps a float32 `(n, dim)` matrix end to end;
   `VectorStore.upsert_matrix` / `EmbeddingCache.put_matrix` build
   `FixedSizeListArray` tables directly; `get_vector_arrays_by_hash` /
   `lookup_arrays` read via `to_arrow()`. Old list-returning APIs are kept as
   wrappers. Values are bit-identical for float32 sources (float64 inputs round
   to nearest float32, same as the old pyarrow path); tests assert equality.
2. **Fewer LanceDB merges/commits.** Ids that the window's `get_hashes` just
   reported absent (and unique within the call) are appended with `add()`;
   existing or duplicated ids still use `merge_insert` exactly as before. One
   commit per window per table (batch 1024/512 → 4096). The shared cache keeps
   `merge_insert` because two consumers may write the same content concurrently.
3. **Voyage base64.** The Voyage transport requests `encoding_format=base64`
   (float32 little-endian per Voyage docs) and decodes with `np.frombuffer`;
   JSON float arrays are still accepted. Only enabled when the endpoint is the
   official Voyage URL (relays behind a custom base_url keep JSON). Other
   transports are unchanged. Not
   exercised by the offline replay; needs one small paid sanity call before relying
   on it in production.
4. **Startup preload.** `nova_core.preload.preload_runtime()` imports lancedb,
   initializes jieba and tree-sitter parsers. `nova-service serve/local` call it
   before listening; `NOVA_PRELOAD=0` disables it. `sync_probe.py` calls it in the
   server process when the source tree has it (older trees skip it) and records
   `preload_s` in `/bench/metrics`. Startup is outside the Tool timer by design
   (user decision: everything preloadable counts as the real scenario). Note
   `server_cpu_s` (RUSAGE_SELF) now includes preload CPU.

Replay note: `ReplayEmbedding.embed_array` returns the fixture's float32 rows as a
matrix (mirrors the base64 path). Baseline trees never call it and keep the old
`.tolist()` path, so their numbers are unchanged.

Local evidence (fast dev box, not the VPS): 20k×1024 vectors in 4000-row windows
through lookup + store + cache writes: old path 3.02s → new path 0.70s, with exact
float32 equality of every stored and cached vector. Preload on the dev box: lancedb
0.96s, jieba 0.27s, parsers 0.01s.

Validation: 1420 core+service tests passed / 4 documented xfails; ruff and the
dependency-direction check pass.

**Requested VPS verification** (same acceptance protocol as above):

- Content checks via `compare_acceptance.py` must pass (logical SQLite, FTS, all
  20,931 vectors, Tool output). LanceDB row order may differ from merge-based
  writes; if Tool output differs ONLY in the order of equal-score hits, report it
  rather than accept or reject silently.
- Run the candidate twice: default, and with `NOVA_PRELOAD=0`, so the preload
  contribution is separable from items 1–2.
- Interleaved control vs candidate with `paired_acceptance.py` is preferred.
- If budget allows, enable phase probes in one separate untimed run (or
  `py-spy record --gil` on the server PID) to decide whether to move
  parse/split/segment into a worker process next.

Independent read-only subagent review: no correctness break found. Noted risks,
not fixed: `upsert_matrix(new_ids=…)` relies on the caller's per-ingest id
uniqueness (guaranteed today by splitter/PK/path dedup); exception types for null
or non-numeric vectors changed (no caller depends on them); base64 vs JSON
bit-equality from the real Voyage service is unverified.

## Round 5 candidate: parse/split/segment in worker processes (NOT yet measured)

Motivation from Round 4: ~33s wall vs ~26 service core-seconds on 2 vCPU — the
Python work is still effectively single-core. Parsing (tree-sitter extraction),
chunk splitting, file hashing, `is_generated` and FTS segmentation are pure
functions of file content, so `nova_core.pipeline.prepare` computes them in a
persistent `spawn` process pool, in the main thread's processing order, up to 64
files ahead. The main thread still applies every file in the original sorted
order inside the same write batch with the same per-file SAVEPOINTs; graph
resolution, vector submission and recovery semantics are untouched.

- Equivalence: any worker exception returns "miss" and the original inline code
  path runs, reproducing the same error records; a crashed pool
  (`BrokenProcessPool`, e.g. OOM kill) falls back inline for the rest of the
  ingest; prepared results are discarded if the language (R1 `.h` lift) differs.
  `Store.apply_file_change(fts_segments=…)` accepts the precomputed segments.
  A test indexes 64 real files plus binary/oversize/syntax-error/Chinese/`.h`
  samples both ways and asserts identical rows in every logical table (only
  `indexed_at` excluded) and identical reports; another kills the pool mid-ingest.
- Enablement: `NOVA_PARSE_WORKERS` (default `min(2, cpu_count)`, `0` disables).
  Used when the pool was prewarmed by startup preload (new `parse_worker` step),
  or cold when an ingest has ≥500 files. Each worker holds ~120 MB RSS after
  warmup (jieba dictionary + grammars): about +240 MB with two workers.
- Local evidence (dev box pinned to 2 cores with `taskset`, cheap fake embedding,
  433 repo files / 7,043 chunks, identical DB hash each time): inline 6.2–6.9s;
  one worker 5.5–5.7s (main-thread CPU 6.6 → 1.5s, worker became the bottleneck);
  two workers 3.8–3.9s.
- Measurement: worker CPU is NOT in `RUSAGE_SELF`. `sync_probe.py` now also records
  `server_children_cpu_s` (live descendant CPU from `/proc`, delta over the timed
  window). Report total service CPU as `server_cpu_s + server_children_cpu_s`.
- Known cosmetic issue: when uvicorn re-raises SIGTERM on shutdown, Python's
  resource tracker may warn about leaked semaphores; workers exit with the parent
  (verified no orphan processes).

Independent read-only review findings, all fixed before commit:

1. Recursion-depth divergence (HIGH). Extractors recurse and
   `TreeSitterParser.parse` turns `RecursionError` into a fallback result, so output
   depended on the caller's stack depth (reproduced at ~480 nesting levels). The
   main thread now passes the inline `parser.parse` call depth and recursion limit;
   the worker pads its stack to the same depth (or misses → inline). A test with
   Python classes and C++ namespaces nested 100–985 deep straddles the threshold
   and asserts identical rows/reports; it fails with the alignment removed.
2. Spawn re-imported the entry module (MEDIUM): under the `nova-service` console
   script each worker loaded uvicorn/fastapi/MCP (~176 MB). Heavy imports in
   `nova_service/__main__.py` are now function-local (881 → 92 modules on import);
   measured worker RSS under the console script ~122 MB.
3. Worker initializer failures are swallowed (pool stays usable).

Validation: 1426 core+service tests passed / 4 documented xfails; ruff and the
dependency-direction check pass. The equivalence test pins one embedding
consumer because `chunks_deduped` varies with the two-consumer race (documented
in `embedding_sink`, unrelated to prefetch).

**Requested VPS verification:** same protocol as Round 4 (content comparison
first). Suggested cohorts, interleaved where possible: control `46edccc`,
candidate default (2 workers), candidate `NOVA_PARSE_WORKERS=1`, and candidate
`NOVA_PARSE_WORKERS=0` (equals Round 4 code path). Please record peak RSS of the
whole cgroup (workers are separate processes) and `server_children_cpu_s`.
