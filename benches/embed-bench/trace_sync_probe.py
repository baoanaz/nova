#!/usr/bin/env python3
"""Run the unchanged client replay with additional indexing-only trace hooks.

Build once (Linux):
  mkdir -p .local/trace
  cc -shared -fPIC -O2 -Wall -Wextra -o .local/trace/libtrace_native.so \
    benches/embed-bench/trace_native.c -ldl
Run exactly the sync_probe.py arguments, but use this entry point and set:
  LD_PRELOAD=$PWD/.local/trace/libtrace_native.so
Trace data lives in OUT/trace; sqlite3_step and native fsync/pwrite hooks record
actual C calls, including the interval before Python can reacquire the GIL.
Use a fresh output directory, the original debug client, real-vector fixture,
and IPAddressDeny=any / IPAddressAllow=localhost. No provider fallback exists.
"""
from __future__ import annotations

import sync_probe
from phase_trace import install

_original_serve = sync_probe.serve


def serve(args, provider):
    install(args.out)
    return _original_serve(args, provider)


if __name__ == '__main__':
    # run_client starts its child using the module's __file__; this ensures the
    # server child installs the same hooks without touching production code.
    sync_probe.__file__ = __file__
    sync_probe.serve = serve
    sync_probe.main()
