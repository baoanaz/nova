# Round 4 VPS verification — 2026-10-07

Tested candidate `178eefd` against control `b243484`. No optimization code was
changed. Release only: three interleaved control/default-candidate pairs, followed
by three candidate runs with `NOVA_PRELOAD=0`. Original frozen release binary,
fixed LangChain commit `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`, 2 vCPU / 1919 MiB
VPS, localhost-only network, MemoryHigh=1200M / MemoryMax=1500M / SwapMax=256M.
Fresh runtime stores/processes and corpus/fixture fadvise policy match the existing
acceptance harness. No detailed tracing, profiler, paid embedding or live API call.

## Complete Tool results

| Source/configuration | Run 1 (s) | Run 2 (s) | Run 3 (s) | Median (s) | Median service CPU (core-s) |
|---|---:|---:|---:|---:|---:|
| Control `b243484` | 46.021181 | 45.130049 | 41.494909 | 45.130049 | 36.527201 |
| Candidate `178eefd`, preload enabled | 34.402256 | 38.581569 | 30.343090 | 34.402256 | 25.806520 |
| Candidate `178eefd`, `NOVA_PRELOAD=0` | 32.775224 | 32.495990 | 34.156888 | 32.775224 | 27.876983 |

Interleaved control/default median difference: 10.727794s (23.77%). The no-preload
cohort's median is 12.354826s below this control, but that cohort ran afterwards,
not interleaved; do not attribute the entire difference to code or infer that
preloading inherently makes the application slower. The old historical 39.11s
median is not this round's control.

**The <30s target is not met.** Default median gap: 4.402256s. No-preload median
gap: 2.775224s. None of the nine complete Tool measurements was below 30s.

## Preload accounting

Recorded startup components, outside the Tool timer:

| Candidate run | lancedb (s) | jieba (s) | parsers (s) | Component sum (s) |
|---|---:|---:|---:|---:|
| 1 | 1.481 | 0.919 | 0.017 | 2.417 |
| 2 | 1.688 | 1.065 | 0.018 | 2.771 |
| 3 | 1.616 | 0.968 | 0.019 | 2.603 |

No-preload runs reported no preload. This experiment does not establish a reliable
end-to-end preload saving: the default/no-preload cohorts have substantial timing
variation. Startup work moved outside the timer is not a reduction of required
cold-start work under the earlier strict timing boundary.

Correction to the Round 4 handoff: the top-level `result.json.server_cpu_s` is
`after['cpu_s'] - before['cpu_s']` in `sync_probe.py`, so it EXCLUDES startup/preload
CPU. The nested `server.cpu_s` is lifetime CPU and includes startup. The table uses
the top-level delta. No GIL profiling was performed; CPU utilization alone does
not identify the proportion of GIL waits.

## Correctness and tests

- Both `compare_acceptance.py` comparisons passed: logical SQLite rows, raw FTS
  text, all 20,931 vectors and complete Tool output match the control. Only the
  existing volatile fields/elapsed-age text are excluded by the harness.
- No equal-score output-order exception was needed; retrieval output matched.
- Every run: 2,986 files, 20,931 chunks/vectors, 15,947 symbols, 72,080 edges,
  538 unresolved refs, 960 spec blocks, 15,768 spec references; one ingest, no errors.
- VPS targeted tests: 72 passed / 1 skipped across vector store/cache,
  embedding sink, preload and mocked API provider tests.
- Real Voyage base64-vs-JSON equivalence remains UNTESTED. Offline replay cannot
  validate this transport. Zero paid calls were made.

These are offline runs with existing real vectors. Fixture decoding and runtime
vector writes are timed; remote embedding inference/network/HTTP response decode
are excluded. Results are not paid-API end-to-end timings.

## Local evidence

Under `/root/xuwenzheng/ace/nova/.local/perf-30s/`:

- `results/round4-paired/`: six interleaved runs, control/candidate aggregates,
  conditions, resource boundaries, raw Tool responses and content hashes.
- `results/round4-no-preload/`: three no-preload runs and comparison.
- `logs/round4.log`: complete execution log; final statuses all zero.
- Frozen worktrees: `round4-control` and `round4-candidate`.

Runtime index/client caches were removed by the existing verifier after hashing.
The vector fixture and raw evidence remain local and are not committed. Main's
pre-existing Web edits were preserved. This commit contains only this report.
