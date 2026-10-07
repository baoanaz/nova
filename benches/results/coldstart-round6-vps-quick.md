# Round 6 VPS quick test

Per the user's time limit: **one complete Tool run per cohort**, in order control,
default candidate, candidate with `NOVA_UPLOAD_PREFETCH=0`. This is a quick
correctness/performance check, not a three-run median acceptance or completed
independent review. No implementation changes or additional investigation.

Control `d1196bc` uses the unchanged frozen original release binary. Candidate
`01b74b0` uses a newly built `cargo build --release --locked --offline --jobs 1`
binary, including its new per-crate optimization settings. Candidate binary SHA256:
`1e516a0f0710d4150fbfa486968dd74b112c2685b0a111a06fa48351e69310da`.

| Cohort | Complete Tool (s) | Main CPU (core-s) | Child CPU | Total CPU | Cgroup peak (MiB) | Swap peak (MiB) | Residual PIDs after probe |
|---|---:|---:|---:|---:|---:|---:|---:|
| Control | 28.800299 | 19.762907 | 8.21 | 27.972907 | 984.79 | 86.11 | 3 |
| Default candidate | 30.191795 | 21.830891 | 8.23 | 30.060891 | 924.13 | 0.004 | 0 |
| Prefetch disabled | 31.672633 | 27.167776 | 0 | 27.167776 | 887.41 | 0 | 0 |

This sample shows no candidate speedup over the control: default is 1.39s slower.
Default is 1.48s faster than disabled prefetch in these single observations. Neither
candidate sample is below 30s. Do not infer a stable regression/gain from one run.

Both candidates match control in logical SQLite rows, FTS text, all vectors and
complete Tool output under the existing volatile-field exclusions. No tie-order
exception was needed. All runs have 2,986 files / 20,931 chunks and vectors and no
ingest errors. Targeted VPS regression tests: **34 passed**.

## Collected stage data

| Wall time metric (s unless noted) | Default | Prefetch disabled |
|---|---:|---:|
| prefetched files | 2466 | 0 |
| Client scan + upload | 4.046 | 3.560 |
| Client wait_index | 24.126 | 26.642 |
| Client total sync | 28.221 | 30.258 |
| Client retrieval | 1.938 | 1.381 |
| Flush loadBlobsS | 0.3416 | 0.2048 |
| Flush ingestS | 23.5887 | 26.3107 |
| Flush ledgerS | 0.0131 | 0.0114 |
| prepare | 4.4244 | 11.4651 |
| prefetch_take | 1.2234 | absent |
| persist | 8.1213 | 6.3145 |
| write_batch | 20.0992 | 23.0937 |
| graph | 3.3211 | 3.0672 |
| vec_lookup | 1.0780 | 1.1911 |
| vec_embed (offline fixture) | 3.0989 | 3.3647 |
| vec_upsert | 2.5124 | 1.2230 |
| vec_cache_put | 2.3846 | 2.0910 |

These stages overlap or nest; **do not add the rows**. Full ingest timing tuples,
including their reported CPU fields, original flush/search log lines, client
sync/retrieval lines, binary/fixture hashes and equality fingerprints are exported
in [raw/round6-vps-quick.json](raw/round6-vps-quick.json). Control predates these
built-in stage logs; its ingest wrapper measured 21.812925s.

The control's three remaining processes had PPID 1: a resource tracker and two
spawn workers. All disappeared after the systemd unit exited. Neither candidate
had additional PIDs when checked after probe shutdown and verification, before
the trial wrapper exited. No additional shutdown investigation was performed.

## Measurement boundary

Same fixed LangChain revision / real-vector fixture, fresh runtime stores and
processes, corpus+fixture fadvise policy, release mode, two embedding consumers,
default startup preload. Per-trial independent systemd cgroup: MemoryHigh1200M,
MemoryMax1500M, SwapMax256M; localhost-only networking. No high/max/OOM events.
Cgroup peak includes page cache, startup and verification; it is not just RSS.

Tool timing excludes service startup/prewarming and remote embedding inference,
network and response decode. Zero paid API calls. Candidate's built-in timing is
active; external tracing/phase patches are disabled. Review remains pending.

Local raw evidence: `.local/perf-30s/results/round6/` and
`.local/perf-30s/logs/round6{,-build}.log`. Runtime data was cleaned by the existing
verifier; fixture and frozen source/binaries retained. This commit contains only
the report and metrics export.
