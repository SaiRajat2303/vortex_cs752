#!/usr/bin/env python3
"""Per-domain power/area by ablation, and power-gating break-even times.

Reads ablation_synth.sh outputs: <dir>/step3_<variant>_<vt>_<corner>_power.rpt
and step3_<variant>_<vt>_stat_lib.rpt. A domain's numbers are full - variant:
  TCU = full - notcu, TFU = full - notfu, FPU = notfu - nof (no-F also drops TFU).

Break-even time (cycles) for a PG transition energy E_trans:
  BET = E_trans / (P_leak * T_clk)
E_trans is modelled two ways:
  relative: k x the domain's own dynamic energy per cycle (P_dyn * T_clk),
            so BET = k * P_dyn / P_leak
  absolute: fixed pJ values
Dynamic power here is vectorless (OpenSTA default activity), so the relative
model is a rough scale only; step 8 replaces it with gate-level SAIF activity.

Usage: domain_power.py <ablation dir> [--clock-mhz 800] > step3_results.md
"""
import argparse
import glob
import os
import re

# (domain, reference variant, variant without the domain)
# FPU uses notfu as reference: the TFU requires F, so the no-F variant has no TFU.
DOMAINS = (('TCU', 'full', 'notcu'), ('TFU', 'full', 'notfu'), ('FPU', 'notfu', 'nof'))
KS = (1, 3, 10, 30)
EABS_PJ = (1, 10, 100)


def read_power(path):
    """Group -> (internal, switching, leakage, total) in W."""
    out = {}
    for line in open(path):
        m = re.match(r'^(Sequential|Combinational|Total)\s+([\d.e+-]+)\s+([\d.e+-]+)\s+'
                     r'([\d.e+-]+)\s+([\d.e+-]+)', line)
        if m:
            out[m.group(1)] = tuple(float(m.group(i)) for i in range(2, 6))
    return out


def read_area(path):
    txt = open(path).read()
    m = re.search(r'Chip area for (?:top )?module.*?:\s*([\d.]+)', txt)
    cells = re.search(r'^\s*(\d+)\s+\S+\s+cells\s*$', txt, re.M) or \
        re.search(r'Number of cells:\s*(\d+)', txt)
    return (float(m.group(1)) if m else None, int(cells.group(1)) if cells else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('--clock-mhz', type=float, default=800.0)
    a = ap.parse_args()
    t_clk = 1e-6 / a.clock_mhz  # s

    def f(variant, vt, corner):
        return os.path.join(a.dir, f'step3_{variant}_{vt}_{corner}_power.rpt')

    vts = sorted({re.match(r'step3_full_(\w+?)_tt_power', os.path.basename(p)).group(1)
                  for p in glob.glob(os.path.join(a.dir, 'step3_full_*_tt_power.rpt'))})
    corners = ('tt', 'ss', 'ff')

    print('# Step 3 results — per-domain power, area and break-even time\n')
    print(f'Clock {a.clock_mhz:.0f} MHz. Power is vectorless (OpenSTA default activity); '
          'leakage is activity-independent. Domain = full core − core without that domain.\n')

    print('## Whole core\n')
    print('| VT | Corner | Total (mW) | Dynamic (mW) | Leakage (mW) | Leakage share | Area (µm²) | Cells | Worst slack (ns) |')
    print('|---|---|---|---|---|---|---|---|---|')
    for vt in vts:
        area, cells = read_area(os.path.join(a.dir, f'step3_full_{vt}_stat_lib.rpt'))
        ws = open(os.path.join(a.dir, f'step3_full_{vt}_worst_slack.rpt')).read().split()[-1]
        for c in corners:
            p = read_power(f('full', vt, c))['Total']
            dyn = p[0] + p[1]
            print(f'| {vt.upper()} | {c.upper()} | {p[3]*1e3:.1f} | {dyn*1e3:.1f} | {p[2]*1e3:.4f} | '
                  f'{p[2]/p[3]:.3%} | {area:.0f} | {cells} | {ws if c == "tt" else ""} |')

    print('\n## Power-gating domains (reference − variant; area share of the full core)\n')
    print('| Domain | VT | Corner | Area (µm²) | Area share | Dynamic (mW) | Leakage (µW) | '
          'Dyn/Leak | ' + ' | '.join(f'BET k={k}' for k in KS) + ' | '
          + ' | '.join(f'BET {e} pJ' for e in EABS_PJ) + ' |')
    print('|---|---|---|---|---|---|---|---|' + '---|' * (len(KS) + len(EABS_PJ)))
    for dom, ref, var in DOMAINS:
        for vt in vts:
            fa, _ = read_area(os.path.join(a.dir, f'step3_{ref}_{vt}_stat_lib.rpt'))
            core_a, _ = read_area(os.path.join(a.dir, f'step3_full_{vt}_stat_lib.rpt'))
            va, _ = read_area(os.path.join(a.dir, f'step3_{var}_{vt}_stat_lib.rpt'))
            for c in corners:
                if not os.path.exists(f(var, vt, c)):
                    continue
                full = read_power(f(ref, vt, c))['Total']
                part = read_power(f(var, vt, c))['Total']
                dyn = (full[0] + full[1]) - (part[0] + part[1])
                leak = full[2] - part[2]
                if leak <= 0:
                    print(f'| {dom} | {vt.upper()} | {c.upper()} | – | – | – | {leak*1e6:.2f} | – |'
                          + ' – |' * (len(KS) + len(EABS_PJ)))
                    continue
                ratio = dyn / leak
                bet_k = ' | '.join(f'{k * ratio:,.0f}' for k in KS)
                bet_e = ' | '.join(f'{(e * 1e-12) / (leak * t_clk):,.0f}' for e in EABS_PJ)
                print(f'| {dom} | {vt.upper()} | {c.upper()} | {fa - va:.0f} | {(fa - va) / core_a:.1%} | '
                      f'{dyn*1e3:.2f} | {leak*1e6:.2f} | {ratio:,.0f} | {bet_k} | {bet_e} |')


if __name__ == '__main__':
    main()
