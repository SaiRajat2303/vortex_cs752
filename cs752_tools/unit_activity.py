#!/usr/bin/env python3
"""Per-unit activity analysis from a filtered rtlsim pipeline trace (trace_run.sh).

A unit is busy for an instruction from its first dispatch packet to its last
commit packet (eop=1), i.e. while the instruction is in flight in that unit.
Two idle views are reported per unit:
  cg  - gaps between in-flight micro-ops (clock gating may use these)
  pg  - gaps between whole instructions: micro-ops of one instruction (same
        parent in the uop sequencer) form one span, since power gating must
        not occur inside a partially issued uop sequence
Idle windows are measured inside each launch epoch: from the KMU start of a
launch until the core has no active warps and nothing in flight after the
launch's last CTA was sent.

Outputs: <prefix>.json (summary) and <prefix>_windows.csv (every idle window).
"""

import argparse
import bisect
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict

LINE_RE = re.compile(r'^\s*(\d+): (\S+) ([\w-]+): (.*)$')
UUID_RE = re.compile(r'\(#(\d+)\)\s*$')
FIELD_RE = re.compile(r'(\w+)=([^,\s]+)')
SLICE_RE = re.compile(r'issue(\d+)')
DECODE_RE = re.compile(r'-decode\d*$')   # decode0..N when FETCH_WIDTH > 1

# log2 histogram bins: [1], [2,3], [4,7], ... ; label = lower bound
def bin_of(n):
    return 1 << (n.bit_length() - 1)


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('trace')
    ap.add_argument('--elf', help='kernel.elf, to name kernels and measure prologue length')
    ap.add_argument('--nm', default='llvm-nm')
    ap.add_argument('--test', default='', help='test id written to the outputs')
    ap.add_argument('--per-slice', default='TCU',
                    help='comma list of units reported per issue slice (one block per slice)')
    ap.add_argument('--out', required=True, help='output prefix')
    return ap.parse_args()


def load_kernels(elf, nm):
    """Map kernel-body address -> kernel name, from __vx_kentry_<name> symbols."""
    if not elf:
        return {}
    out = subprocess.run([nm, '-n', elf], capture_output=True, text=True, check=True).stdout
    kernels = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith('__vx_kentry_'):
            kernels[int(parts[0], 16)] = parts[2][len('__vx_kentry_'):]
    return kernels


def merge(intervals):
    """Union of closed [s, e] intervals, sorted."""
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return out


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


def main():
    a = parse_args()
    per_slice = {u for u in a.per_slice.split(',') if u}
    kernels = load_kernels(a.elf, a.nm)

    decode_cyc = {}                 # uuid -> decode cycle
    decode_pc = {}                  # uuid -> (wid, pc)
    parent = {}                     # uop uuid -> parent uuid
    disp = {}                       # uuid -> [first dispatch cycle, unit, slice]
    commit_end = {}                 # uuid -> last eop commit cycle
    launches = []                   # dicts: start, n_ctas, fires[], entry_dcr
    warp_launch = []                # (cycle, wid)
    warp_done = []                  # (cycle, wid)
    first_kbody = []                # (cycle, wid, kernel) first decode at a kernel body address
    dcr_writes = []                 # (cycle, text)
    perf = {}
    passed = None

    with open(a.trace, errors='replace') as f:
        for line in f:
            if line.startswith('PERF: instrs='):
                perf = dict(FIELD_RE.findall(line))
                continue
            if line.startswith('PASSED'):
                passed = True
                continue
            if line.startswith('FAILED'):
                passed = False
                continue
            m = LINE_RE.match(line)
            if not m:
                continue
            ts = int(m.group(1))
            if ts < 2:                  # pre-reset garbage
                continue
            cyc = ts // 2               # rtlsim evaluates twice per clock cycle
            inst, ev, rest = m.group(2), m.group(3), m.group(4)
            fields = dict(FIELD_RE.findall(rest))
            um = UUID_RE.search(rest)
            uuid = int(um.group(1)) if um else None

            if inst.endswith('-dispatcher') and ev == 'dispatch':
                ex = fields.get('ex', '?')
                unit = 'TFU' if ex == 'SFU' and fields.get('op', '').startswith('TFU') else ex
                sm = SLICE_RE.search(inst)
                sl = int(sm.group(1)) if sm else 0
                if uuid not in disp:
                    disp[uuid] = [cyc, unit, sl]
            elif inst.endswith('-commit') and ev == 'commit':
                if fields.get('eop') == '1':
                    commit_end[uuid] = cyc
            elif DECODE_RE.search(inst) and ev == 'decode':
                decode_cyc.setdefault(uuid, cyc)
                pc = int(fields.get('PC', '0'), 16)
                decode_pc[uuid] = (int(fields.get('wid', 0)), pc)
                if pc in kernels:
                    first_kbody.append((cyc, int(fields.get('wid', 0)), kernels[pc]))
            elif inst.endswith('-uop') and ev == 'decode':
                pm = re.search(r'parent=#(\d+)', rest)
                if pm and uuid is not None:
                    parent[uuid] = int(pm.group(1))
                    decode_cyc.setdefault(uuid, cyc)
            elif inst == 'kmu' and ev == 'start':
                g = re.search(r'grid=\[(\d+),(\d+),(\d+)\]', rest)
                n = int(g.group(1)) * int(g.group(2)) * int(g.group(3)) if g else 0
                launches.append({'start': cyc, 'n_ctas': n, 'fires': [],
                                 'start_pc': fields.get('PC')})
            elif inst == 'kmu' and ev == 'cta-fire':
                if launches:
                    launches[-1]['fires'].append(cyc)
            elif inst == 'kmu' and ev == 'dcr-write':
                dcr_writes.append((cyc, rest.strip()))
            elif inst.endswith('cta_dispatch') and ev == 'dispatch':
                warp_launch.append((cyc, int(fields.get('wid', 0))))
            elif inst.endswith('cta_dispatch') and ev == 'warp-done':
                warp_done.append((cyc, int(fields.get('wid', 0))))

    # ---- instruction intervals -------------------------------------------------
    unmatched = 0
    uop_iv = defaultdict(list)       # (unit, blk) -> [(s, e)]  cg view
    span = {}                        # (unit, blk, top) -> [s, e]  pg view
    lead = defaultdict(list)         # unit -> dispatch - decode
    for uuid, (d, unit, sl) in disp.items():
        c = commit_end.get(uuid)
        if c is None:
            unmatched += 1
            continue
        blk = sl if unit in per_slice else 0
        uop_iv[(unit, blk)].append((d, c))
        top = parent.get(uuid, uuid)
        k = (unit, blk, top)
        if k in span:
            span[k][0] = min(span[k][0], d)
            span[k][1] = max(span[k][1], c)
        else:
            span[k] = [d, c]
        if uuid in decode_cyc:
            lead[unit].append(d - decode_cyc[uuid])
    pg_iv = defaultdict(list)
    for (unit, blk, _), (s, e) in span.items():
        pg_iv[(unit, blk)].append((s, e))
    busy = {'cg': {k: merge(v) for k, v in uop_iv.items()},
            'pg': {k: merge(v) for k, v in pg_iv.items()}}

    # ---- launch epochs ---------------------------------------------------------
    # active-warp count over time, and in-flight end times, to find each epoch's end
    wevents = sorted([(c, +1) for c, _ in warp_launch] + [(c, -1) for c, _ in warp_done])
    all_commit_ends = sorted(commit_end.values())
    epochs = []
    for i, L in enumerate(launches):
        nxt = launches[i + 1]['start'] if i + 1 < len(launches) else None
        last_fire = L['fires'][-1] if L['fires'] else L['start']
        # last warp-done before the next launch (or end of trace)
        dones = [c for c, _ in warp_done if c >= L['start'] and (nxt is None or c < nxt)]
        end = max([last_fire] + dones)
        # nothing in flight: last commit before the next launch
        j = bisect.bisect_left(all_commit_ends, nxt) if nxt is not None else len(all_commit_ends)
        if j > 0 and all_commit_ends[j - 1] >= L['start']:
            end = max(end, all_commit_ends[j - 1])
        # a warp of this launch still active when the next launch starts
        active = sum(d for c, d in wevents if c < nxt) if nxt is not None else 0
        overlap = nxt is not None and (end >= nxt or active > 0)
        # kernel name: first kernel-body decode in this launch
        kname = next((k for c, _, k in first_kbody
                      if c >= L['start'] and (nxt is None or c < nxt)), None)
        entry_dcr = [t for c, t in dcr_writes if c < L['start']
                     and (i == 0 or c > launches[i - 1]['start'])]
        epochs.append({'launch': i, 'kernel': kname, 'start': L['start'], 'end': end,
                       'cycles': end - L['start'] + 1, 'n_ctas': L['n_ctas'],
                       'ctas_fired': len(L['fires']), 'last_cta_fire': last_fire,
                       'start_pc': L['start_pc'], 'overlaps_next_launch': overlap,
                       'dcr_writes': len(entry_dcr)})

    # prologue: warp launch -> that warp's first decode at a kernel body address
    for ep in epochs:
        lens = []
        for c, wid in warp_launch:
            if not (ep['start'] <= c <= ep['end']):
                continue
            nb = next((cc for cc, w, _ in first_kbody if w == wid and cc >= c), None)
            if nb is not None and nb <= ep['end']:
                lens.append(nb - c)
        lens.sort()
        ep['prologue'] = ({'warps': len(lens), 'min': lens[0], 'median': pct(lens, 0.5),
                           'max': lens[-1]} if lens else None)

    # ---- idle windows per epoch -------------------------------------------------
    units = sorted(set(k for k in busy['cg']))
    rows = []
    summary_units = {}
    for view in ('cg', 'pg'):
        for key in units:
            unit, blk = key
            ivs = busy[view].get(key, [])
            starts = [s for s, _ in ivs]
            for ep in epochs:
                S, E = ep['start'], ep['end']
                lo = bisect.bisect_right(starts, S) - 1
                lo = max(lo, 0)
                inside = [(max(s, S), min(e, E)) for s, e in ivs[lo:] if s <= E and e >= S]
                busy_cyc = sum(e - s + 1 for s, e in inside)
                wins = []
                prev = S - 1
                for s, e in inside:
                    if s > prev + 1:
                        wins.append((prev + 1, s - prev - 1, 'lead' if prev == S - 1 else 'mid'))
                    prev = max(prev, e)
                if prev < E:
                    wins.append((prev + 1, E - prev, 'lead' if prev == S - 1 else 'tail'))
                hist = defaultdict(int)
                for st, ln, kind in wins:
                    rows.append([a.test, ep['launch'], ep['kernel'], unit, blk, view,
                                 st, ln, kind])
                    if kind == 'mid':
                        hist[bin_of(ln)] += 1
                mids = sorted(ln for _, ln, k in wins if k == 'mid')
                rec = summary_units.setdefault(f'{unit}.{blk}', {}).setdefault(
                    str(ep['launch']), {})
                rec[view] = {
                    'busy_cycles': busy_cyc,
                    'util': round(busy_cyc / ep['cycles'], 4) if ep['cycles'] else 0,
                    'first_use_delay': next((ln for _, ln, k in wins if k == 'lead'), 0)
                                       if inside else None,
                    'mid_windows': len(mids),
                    'mid_idle_cycles': sum(mids),
                    'mid_max': mids[-1] if mids else 0,
                    'mid_median': pct(mids, 0.5),
                    'hist_log2': {str(k): v for k, v in sorted(hist.items())},
                }

    lead_stats = {}
    for unit, v in lead.items():
        v.sort()
        lead_stats[unit] = {'n': len(v), 'min': v[0], 'p10': pct(v, 0.1),
                            'median': pct(v, 0.5), 'max': v[-1]}

    out = {
        'test': a.test, 'trace': a.trace, 'passed': passed, 'perf': perf,
        'instructions_dispatched': len(disp), 'unmatched_dispatch': unmatched,
        'uops_with_parent': len(parent),
        'epochs': epochs, 'lead_time_decode_to_dispatch': lead_stats,
        'units': summary_units,
    }
    with open(a.out + '.json', 'w') as f:
        json.dump(out, f, indent=1)
    with open(a.out + '_windows.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['test', 'launch', 'kernel', 'unit', 'block', 'view', 'start', 'length', 'kind'])
        w.writerows(rows)
    print(f'{a.test}: {len(epochs)} launch(es), {len(disp)} dispatched, '
          f'{unmatched} unmatched, {len(rows)} idle windows -> {a.out}.json')


if __name__ == '__main__':
    sys.exit(main())
