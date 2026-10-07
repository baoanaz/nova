#!/usr/bin/env python3
"""Compare complete Tool samples by build mode, with content equality gates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median

from acceptance_suite import canonical_tool_result


def compare(baseline, candidate):
    before = json.loads((baseline/'samples.json').read_text())
    after = json.loads((candidate/'samples.json').read_text())
    expected = before[0]['equivalence']
    content_keys = set(expected['hashes']) - {'tool_result', 'tool_result_raw'}
    query = canonical_tool_result(Path(before[0]['output']))
    for item in before + after:
        actual = item['equivalence']
        assert actual['counts'] == expected['counts'], item['output']
        for key in content_keys:
            assert actual['hashes'][key] == expected['hashes'][key], (item['output'], key)
        assert canonical_tool_result(Path(item['output'])) == query, item['output']
    result = {}
    for mode in sorted({r['mode'] for r in after}):
        old = [r['wall_s'] for r in before if r['mode'] == mode]
        new = [r['wall_s'] for r in after if r['mode'] == mode]
        assert len(old) >= 3 and len(new) >= 3
        result[mode] = dict(baseline_samples=old, samples=new,
                            baseline_median_s=median(old), median_s=median(new),
                            net_saved_s=median(old)-median(new),
                            remaining_to_30_s=max(0, median(new)-30))
    result['equivalence'] = 'logical rows, vectors and Tool match; elapsed-age text excluded'
    (candidate/'comparison.json').write_text(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline, args.candidate), indent=2))
