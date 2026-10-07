#!/usr/bin/env python3
"""Interleave frozen control/candidate complete Tool runs by compilation mode.

Run inside the same localhost-only systemd resource envelope as acceptance_suite.
Every child uses a fresh output tree, source/fixture eviction and isolated verifier.
Alternate pair order to limit chronological host-load bias. No phase tracing.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from statistics import median

from compare_acceptance import compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--control-prefix', default='baseline')
    parser.add_argument('--candidate-prefix', default='baseline')
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--bins', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--modes', default='debug,release')
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error('At least three runs per side and mode are required')
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    samples = {'control': [], 'candidate': []}
    for side in samples:
        (args.out/side).mkdir()
    for number in range(args.rounds):
        for mode_index, mode in enumerate(args.modes.split(',')):
            order = ('control', 'candidate') if (number + mode_index) % 2 == 0 else (
                'candidate', 'control')
            for side in order:
                destination = args.out/f'{number+1}-{mode}-{side}'
                command = [
                    sys.executable, str(Path(__file__).with_name('acceptance_suite.py')),
                    '--source', str(getattr(args, side).resolve()),
                    '--binary-prefix', getattr(args, f'{side}_prefix'),
                    '--repo', str(args.repo.resolve()), '--fixture', str(args.fixture.resolve()),
                    '--bins', str(args.bins.resolve()), '--out', str(destination),
                    '--modes', mode, '--rounds', '1',
                ]
                print(json.dumps(dict(pair=number+1, mode=mode, side=side)), flush=True)
                subprocess.run(command, check=True)
                records = json.loads((destination/'samples.json').read_text())
                assert len(records) == 1
                records[0]['pair'] = number + 1
                samples[side].extend(records)
                aggregate = args.out/side
                (aggregate/'samples.json').write_text(json.dumps(samples[side], indent=2))
                summary = {
                    current: dict(
                        samples=[r['wall_s'] for r in samples[side] if r['mode'] == current],
                        median_s=median(r['wall_s'] for r in samples[side]
                                        if r['mode'] == current),
                    ) for current in {r['mode'] for r in samples[side]}
                }
                (aggregate/'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(compare(args.out/'control', args.out/'candidate'), indent=2), flush=True)


if __name__ == '__main__':
    main()
