# Round 5 VPS verification — 2026-10-07

Candidate: `bad49ed`; control: `46edccc`. No implementation changes were made.
Release only, same frozen Rust binary and real-vector fixture as Round 4.
LangChain `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`, 2 vCPU / 1919 MiB VPS.
All cohorts enable startup preload and retain two embedding consumers. Default
candidate leaves `NOVA_PARSE_WORKERS` unset (two workers on this VPS); other
candidate cohorts explicitly set it to 1 or 0. Control uses its old inline path.

Twelve serial trials, with cohort order rotated:

1. control, default2, workers1, workers0
2. default2, workers1, workers0, control
3. workers1, workers0, control, default2

Each trial has fresh stores/processes and its own systemd cgroup: MemoryHigh
1200M, MemoryMax 1500M, MemorySwapMax 256M, network restricted to localhost.
Corpus/fixture fadvise and untimed content verification follow the existing
acceptance harness. No paid API calls or profiling. Test wrappers are local-only;
production and public benchmark implementations were not changed.

## Complete Tool wall time

| Cohort | Run 1 (s) | Run 2 (s) | Run 3 (s) | Median (s) | Median difference vs control |
|---|---:|---:|---:|---:|---:|
| Control `46edccc` | 32.864809 | 30.455534 | 30.278658 | 30.455534 | — |
| Candidate default, 2 workers | 30.051522 | 29.967487 | 29.535166 | 29.967487 | 0.488048 faster |
| Candidate, 1 worker | 32.633046 | 31.731912 | 27.510577 | 31.731912 | 1.276377 slower |
| Candidate, 0 workers | 32.318731 | 29.099708 | 28.303808 | 29.099708 | 1.355827 faster |

The default median is below 30 seconds by just 0.032513s; one of its three runs
exceeds 30s. This is not evidence of reliably sub-30s operation. The zero-worker
candidate also has a lower median (0.867779s below default), so these samples do
not establish a consistent benefit from two workers over the inline candidate.
There is visible chronological variability; do not infer gains from the previous
round's historical median. No further optimization/tuning was attempted here.

## CPU and cgroup memory

Total timed service CPU is `server_cpu_s + server_children_cpu_s`, excluding
startup preload. Child CPU uses the harness's live-descendant counters.

| Cohort | Total CPU samples (core-s) | Median total CPU | Cgroup memory peaks (MiB), runs 1/2/3 | Swap peaks (MiB), runs 1/2/3 |
|---|---|---:|---|---|
| Control | 24.795478 / 25.202228 / 25.253841 | 25.202228 | 841.10 / 934.04 / 901.30 | 33.65 / 0 / 0 |
| Default 2 | 27.528287 / 27.917260 / 28.567741 | 27.917260 | 1068.95 / 1033.80 / 959.06 | 0 / 47.96 / 135.95 |
| Workers 1 | 27.224194 / 28.640194 / 26.773052 | 27.224194 | 949.93 / 993.13 / 967.98 | 23.29 / 0 / 0 |
| Workers 0 | 25.074473 / 24.371762 / 23.866053 | 24.371762 | 822.52 / 923.84 / 951.59 | 0 / 0 / 0 |

Default main-process CPU: 19.408287 / 19.757260 / 20.057741 core-s; child CPU:
8.12 / 8.16 / 8.51. One-worker main CPU: 19.524194 / 20.310194 / 19.033052;
child CPU: 7.70 / 8.33 / 7.74. Reporting only main CPU would hide work moved into
children. Total CPU rises with the pool in this experiment.

Memory is kernel `memory.peak` for the entire trial cgroup, not main-process RSS.
It includes page cache, startup and post-run verification. Swap is separately
recorded `memory.swap.peak`; the two peaks need not occur simultaneously. Every
trial recorded zero memory.high/max/oom/oom_kill events; nonzero swap still occurred.

Pool warmup is outside the Tool timer: default parse_worker preload was
1.475 / 1.435 / 1.324s; one-worker warmup was 1.514 / 1.253 / 1.651s. Other preload
components are recorded in raw samples. These are service-preloaded offline Tool
timings, not service-launch-to-result timings or paid embedding end-to-end timings.

## Correctness and process-exit observation

All twelve trials match the control's logical SQLite contents, raw FTS text,
20,931 vectors and Tool output under existing volatile-field exclusions. No
retrieval tie-order exception was needed. Each run indexed 2,986 files / 20,931
chunks and vectors with one ingest and no reported errors. All three final
`compare_acceptance.py` comparisons passed. The race-sensitive `chunks_deduped`
report field is not promoted into a new equality requirement; embedding consumers
remain at two throughout.

VPS targeted tests: **12 passed**, covering `test_prepare.py`, storage batch and
preload tests. The full 1426-test suite was not repeated on the VPS.

**Exit observation for Claude:** after `acceptance_suite.py` completed (including
`sync_probe` service termination and verification), but while the tiny trial
wrapper was still alive, `cgroup.procs` contained three additional PIDs in EACH
two-worker trial, two in EACH one-worker trial, and none in control/zero-worker
trials. These PIDs disappeared after their systemd units finished; a final check
found none still present. Thus this harness does not independently confirm the
handoff's claim that service exit itself leaves no processes. Recorded IDs and
unit boundaries are preserved; no shutdown-code investigation or fix was made.
This observation is from sync_probe, not a repeat of the console-script test.

## Evidence

Local root: `/root/xuwenzheng/ace/nova/.local/perf-30s/`.

- `results/round5/summary.json`: all cohort timings, CPU and memory peaks.
- `results/round5/{control,default2,workers1,workers0}/`: aggregate samples and
  comparison reports.
- `results/round5/<round>-<cohort>/`: per-trial conditions, result, raw response,
  equivalence hashes, resource boundaries and `trial-cgroup.json`.
- `logs/round5.log`: serial trial log.
- Frozen source trees: `round5-control`, `round5-candidate`.

Original fixture retained; generated runtime stores cleaned by the existing
verifier. No real Voyage/base64 request was made. Pre-existing Web changes remain
untouched. This commit contains only this measurement report.
