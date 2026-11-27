#!/usr/bin/env python3
"""Summarize unit_activity.py outputs into markdown tables.

Usage: summarize.py <dir> [<dir> ...] > summary.md

For each test and unit it reports, over all launch epochs:
  util         busy cycles / epoch cycles
  idle>=X      fraction of epoch cycles inside idle windows of length >= X
               (pg view: windows never split a uop sequence)
  windows      number of idle windows (all kinds)
"""
import csv
import glob
import json
import os
import sys
from collections import defaultdict

THRESH = (16, 64, 256, 1024)
# units present in every build; TCU/TFU appear only when a test enables them
ALWAYS_PRESENT = ('ALU', 'LSU', 'SFU', 'FPU')


def main():
    files = []
    for d in sys.argv[1:]:
        files += sorted(glob.glob(os.path.join(d, '*.json')))
    tests = []
    for jf in files:
        d = json.load(open(jf))
        prefix = jf[:-len('.json')]
        win = defaultdict(list)  # (unit.blk, view) -> lengths
        with open(prefix + '_windows.csv') as f:
            for r in csv.DictReader(f):
                win[(f"{r['unit']}.{r['block']}", r['view'])].append(int(r['length']))
        tests.append((d, win))

    print('## Launch epochs\n')
    print('| Test | Pass | Perf cycles | Launches | Epoch cycles | Kernels | Launches overlap on core | '
          'Prologue median (min–max) | Lead decode→dispatch min/median |')
    print('|---|---|---|---|---|---|---|---|---|')
    for d, _ in tests:
        eps = d['epochs']
        ks = ', '.join(sorted({e['kernel'] or '?' for e in eps}))
        pro = [e['prologue'] for e in eps if e['prologue']]
        pro_s = (f"{pro[0]['median']} ({min(p['min'] for p in pro)}–{max(p['max'] for p in pro)})"
                 if pro else '–')
        lead = d['lead_time_decode_to_dispatch']
        lead_s = ', '.join(f"{u} {v['min']}/{v['median']}" for u, v in sorted(lead.items()))
        print(f"| {d['test']} | {'yes' if d['passed'] else 'NO'} | {d['perf'].get('cycles')} | "
              f"{len(eps)} | {sum(e['cycles'] for e in eps)} | {ks} | "
              f"{'yes' if any(e['overlaps_next_launch'] for e in eps) else 'no'} | {pro_s} | {lead_s} |")

    for view, title in (('pg', 'PG view (instruction spans; uop sequences not split)'),
                        ('cg', 'CG view (individual uops)')):
        print(f'\n## Per-unit idle time — {title}\n')
        hdr = ' | '.join(f'idle≥{t}' for t in THRESH)
        print(f'| Test | Unit | Util | Windows | {hdr} | Longest |')
        print('|---|---|---|---|' + '---|' * len(THRESH) + '---|')
        for d, win in tests:
            total = sum(e['cycles'] for e in d['epochs'])
            for ub in sorted(d['units']):
                recs = d['units'][ub]
                busy = sum(r[view]['busy_cycles'] for r in recs.values())
                lens = win.get((ub, view), [])
                cells = ' | '.join(f"{sum(l for l in lens if l >= t) / total:.0%}" for t in THRESH)
                print(f"| {d['test']} | {ub} | {busy / total:.1%} | {len(lens)} | {cells} | "
                      f"{max(lens) if lens else 0} |")
            seen = {ub.split('.')[0] for ub in d['units']}
            for u in ALWAYS_PRESENT:
                if u not in seen:
                    print(f"| {d['test']} | {u} | 0% (never dispatched) | – |"
                          + ' 100% |' * len(THRESH) + ' – |')


if __name__ == '__main__':
    main()
