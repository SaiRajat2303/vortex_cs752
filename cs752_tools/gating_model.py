#!/usr/bin/env python3
"""Offline power-gating policy study (task-order step 4).

Inputs
  - unit_activity.py outputs (<id>.json, <id>_windows.csv), pg view: per launch and
    unit, the idle windows of kind lead (launch -> first use), mid, tail.
  - step-3 ablation reports (per-domain dynamic power and leakage), via domain_power.py.
  - a sequences file: each line `name: <test>[@<launch>|@*] <test>... ` lists launches in
    the order a history table would see them (`test@*` = all launches of that run).

Energy model (per launch, per PG domain; savings are relative to CG only = B2)
  - PG cycles in a window of length L under timeout N: max(0, L - N).
  - Every wake-up costs E_trans (a power-down/up pair; power-down is paired with the
    wake that follows it) and stalls max(0, WAKE_LAT - lead) cycles, lead = the unit's
    decode->dispatch lead time (step 2).
  - Lead window (launch -> first use): pre-wake (N != 0) starts the launch ON and the
    timeout applies; stay (N == 0) starts in PG until first use (1 wake + stall), or for the
    whole launch if the unit is never used. Starting state is free (cost-neutral boundary).
  - Tail window: timeout applies; no wake inside the launch.
  - Saved energy = P_leak * T * pg_cycles - E_trans * wakes.

Policies: B3-global, B3-BET, B3-perkernel-oracle, B4-UB, P1 (history), P1-C (history,
concern 11 option C: global-timeout fallback, N*=0 only at launch, theta=2), O (oracle).

The launch boundary is cost-neutral (see evaluate()).
"""
import argparse
import bisect
import csv
import glob
import itertools
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import domain_power  # noqa: E402

INF = None                         # "never power gate"
CANDIDATES = [0, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, INF]
DOMAIN_OF = {'TCU': 'TCU', 'FPU': 'FPU', 'TFU': 'TFU'}


# ---------------------------------------------------------------- data ----------
class LaunchUnit:
    """Idle windows of one PG domain in one launch."""
    __slots__ = ('lead', 'used', 'mids', 'suffix', 'tail', 'cycles')

    def __init__(self, cycles):
        self.lead, self.used, self.mids, self.tail, self.cycles = 0, False, [], 0, cycles
        self.suffix = None

    def finalize(self):
        self.mids.sort()
        s, acc = [0] * (len(self.mids) + 1), 0
        for i in range(len(self.mids) - 1, -1, -1):
            acc += self.mids[i]
            s[i] = acc
        self.suffix = s

    def mid_over(self, n):
        """(count, sum of (L - n)) over mid windows with L > n."""
        i = bisect.bisect_right(self.mids, n)
        cnt = len(self.mids) - i
        return cnt, self.suffix[i] - n * cnt


def load_runs(dirs, app_of):
    runs = {}
    for d in dirs:
        for jf in sorted(glob.glob(os.path.join(d, '*.json'))):
            j = json.load(open(jf))
            test = j['test']
            app_lead = {u: v['min'] for u, v in j['lead_time_decode_to_dispatch'].items()}
            launches = []
            for ep in j['epochs']:
                launches.append({'kernel': ep['kernel'], 'cycles': ep['cycles'],
                                 'prologue': (ep['prologue'] or {}).get('median') or 0,
                                 'units': {}})
            with open(jf[:-5] + '_windows.csv') as f:
                for r in csv.DictReader(f):
                    if r['view'] != 'pg' or r['unit'] not in DOMAIN_OF:
                        continue
                    L = launches[int(r['launch'])]
                    key = f"{r['unit']}.{r['block']}"
                    lu = L['units'].get(key)
                    if lu is None:
                        lu = L['units'][key] = LaunchUnit(L['cycles'])
                        lu.used = False
                    ln, kind = int(r['length']), r['kind']
                    if kind == 'lead':
                        lu.lead = ln
                    elif kind == 'mid':
                        lu.mids.append(ln)
                    else:
                        lu.tail = ln
            for L in launches:
                for key, lu in L['units'].items():
                    lu.used = lu.lead < L['cycles']
                    lu.finalize()
            runs[test] = {'launches': launches, 'lead': app_lead, 'app_key': app_of.get(test, test)}
    return runs


def domain_params(ablation_dir, clock_mhz):
    """Per domain: P_dyn (TT, W), P_leak at TT and SS (W). LVT."""
    out = {}
    for dom, ref, var in domain_power.DOMAINS:
        p = {}
        for c in ('tt', 'ss'):
            full = domain_power.read_power(os.path.join(ablation_dir, f'step3_{ref}_lvt_{c}_power.rpt'))['Total']
            part = domain_power.read_power(os.path.join(ablation_dir, f'step3_{var}_lvt_{c}_power.rpt'))['Total']
            p[f'dyn_{c}'] = (full[0] + full[1]) - (part[0] + part[1])
            p[f'leak_{c}'] = full[2] - part[2]
        out[dom] = p
    return out


# ---------------------------------------------------------------- model ---------
def evaluate(lu, n, prewake, P, wake, lead, prologue):
    """(pg_cycles, wakes, stall_cycles) of one launch-unit under timeout n.

    The launch boundary is cost-neutral: inter-kernel time is excluded (concern 5b), so the
    transition that the between-kernel power-down implies is not charged either. A policy
    starts the launch with the unit ON (pre-wake; the prologue hides it) or in PG (stay)
    for free; only transitions inside the launch are charged."""
    dstall = max(0, wake - lead)
    if not lu.used:                               # the whole launch is one idle window
        if not prewake:
            return lu.cycles, 0, 0
        return (lu.cycles - n if n is not INF and lu.cycles > n else 0), 0, 0
    pg = wakes = stall = 0
    if prewake:                                   # ON at launch; timeout applies until first use
        if n is not INF and lu.lead > n:
            pg += lu.lead - n
            wakes += 1
            stall += dstall
    else:                                         # stays in PG until first use
        pg += lu.lead
        wakes += 1
        stall += dstall
    if n is not INF:
        cnt, s = lu.mid_over(n)
        pg += s
        wakes += cnt
        stall += cnt * dstall
        if lu.tail > n:
            pg += lu.tail - n
    return pg, wakes, stall


def evaluate_stay_then(lu, n_after, P, wake, lead):
    """P1-C for a stored N* = 0: start the launch in PG; if the unit is used after all, wake on
    first use and apply the fallback timeout n_after for the rest of the launch."""
    if not lu.used:
        return lu.cycles, 0, 0
    dstall = max(0, wake - lead)
    pg, wakes, stall = lu.lead, 1, dstall
    if n_after is not INF:
        cnt, s = lu.mid_over(n_after)
        pg += s
        wakes += cnt
        stall += cnt * dstall
        if lu.tail > n_after:
            pg += lu.tail - n_after
    return pg, wakes, stall


def oracle(lu, P, wake, bet, lead):
    """Per-window oracle with wake-on-demand: PG every idle window longer than BET for its
    whole length (an energy upper bound for any wake-on-demand policy); stalls reported."""
    if not lu.used:
        return lu.cycles, 0, 0
    dstall = max(0, wake - lead)
    pg = wakes = stall = 0
    for L in [lu.lead] + lu.mids:
        if L > bet:
            pg += L
            wakes += 1
            stall += dstall
    pg += lu.tail                                 # no wake needed inside the launch
    return pg, wakes, stall


def saved(pg, wakes, P):
    return P['leak'] * P['T'] * pg - P['etrans'] * wakes


def best_n(lus, P, wake, lead_of, prologue_of, cap, prewake_rule):
    """Timeout minimizing energy over a list of (lu, unit, launch) under a stall cap."""
    best = (None, -1e30)
    total_cycles = sum(lu.cycles for lu, _, _ in lus)
    for n in CANDIDATES:
        e = st = 0.0
        for lu, unit, L in lus:
            pw = prewake_rule(n)
            pg, w, s = evaluate(lu, n, pw, P, wake, lead_of(unit, L), prologue_of(L))
            e += saved(pg, w, P)
            st += s
        if st <= cap * total_cycles and e > best[1]:
            best = (n, e)
    return best[0]   # INF (None) also when no candidate meets the cap


def bucket(n):
    return len(CANDIDATES) - 1 if n is INF else CANDIDATES.index(n)


# ---------------------------------------------------------------- main ----------
def parse_sequences(path, runs):
    seqs = []
    for line in open(path):
        line = line.split('#')[0].strip()
        if not line:
            continue
        name, rest = line.split(':', 1)
        items = []
        for tok in rest.split():
            test, _, which = tok.partition('@')
            if test not in runs:
                sys.exit(f'sequence {name}: unknown test {test}')
            idx = range(len(runs[test]['launches'])) if which in ('', '*') else [int(which)]
            items += [(test, i) for i in idx]
        seqs.append((name.strip(), items))
    return seqs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--runs', nargs='+', required=True, help='dirs with unit_activity outputs')
    ap.add_argument('--ablation', required=True, help='step-3 ablation dir')
    ap.add_argument('--sequences', required=True)
    ap.add_argument('--clock-mhz', type=float, default=800.0)
    ap.add_argument('--cap', type=float, default=0.01, help='stall cap, fraction of cycles')
    ap.add_argument('--theta', type=int, default=1, help='min confidence to use a stored N*')
    ap.add_argument('--cases', nargs='*', default=[], help='case files mapping test id -> app')
    ap.add_argument('--json', help='write all results here')
    a = ap.parse_args()

    T = 1e-6 / a.clock_mhz
    app_of = {}
    for cf in a.cases:
        for line in open(cf):
            if line.strip() and not line.startswith('#'):
                tid, app = line.split('|')[:2]
                app_of[tid] = app
    runs = load_runs(a.runs, app_of)
    seqs = parse_sequences(a.sequences, runs)
    dom = domain_params(a.ablation, a.clock_mhz)

    leak_scen = {'ASAP7-TT': lambda d: dom[d]['leak_tt'],
                 'ASAP7-SS100C': lambda d: dom[d]['leak_ss']}
    for s in (0.05, 0.10, 0.20, 0.30):
        leak_scen[f'share{int(s*100)}%'] = (lambda s: lambda d: s / (1 - s) * dom[d]['dyn_tt'])(s)
    ks, wakes_lat = (1, 3, 10), (10, 20, 50)

    results = []
    synthetic = {}      # (sequence, test, launch, unit) -> idle LaunchUnit
    for (scen, leakf), k, wake in itertools.product(leak_scen.items(), ks, wakes_lat):
        for sname, items in seqs:
            # per domain-unit key -> list of (lu, unit, (test, idx))
            per_unit = defaultdict(list)
            seq_units = sorted({u for test, i in items for u in runs[test]['launches'][i]['units']})
            for test, i in items:
                L = runs[test]['launches'][i]
                for unit in seq_units:
                    lu = L['units'].get(unit)
                    if lu is None:
                        # the sequence runs on hardware that has this unit; a launch whose
                        # build lacked it leaves it idle for the whole launch
                        key = (sname, test, i, unit)
                        lu = synthetic.get(key)
                        if lu is None:
                            lu = LaunchUnit(L['cycles'])
                            lu.lead, lu.used = L['cycles'], False
                            lu.finalize()
                            synthetic[key] = lu
                    per_unit[unit].append((lu, unit, (test, i)))
            for unit, lst in sorted(per_unit.items()):
                d = DOMAIN_OF[unit.split('.')[0]]
                P = {'leak': leakf(d), 'T': T, 'etrans': k * dom[d]['dyn_tt'] * T}
                bet = P['etrans'] / (P['leak'] * T)
                lead = lambda u, L: runs[L[0]]['lead'].get(u.split('.')[0], 6)
                prol = lambda L: runs[L[0]]['launches'][L[1]]['prologue']
                leak_energy = P['leak'] * T * sum(lu.cycles for lu, _, _ in lst)
                total_cycles = sum(lu.cycles for lu, _, _ in lst)

                def run_policy(n_for):
                    e = st = 0.0
                    for lu, u, L in lst:
                        n, pw = n_for(lu, u, L)
                        pg, w, s = evaluate(lu, n, pw, P, wake, lead(u, L), prol(L))
                        e += saved(pg, w, P)
                        st += s
                    return e, st

                res = {}
                # O
                e = st = 0.0
                for lu, u, L in lst:
                    pg, w, sst = oracle(lu, P, wake, bet, lead(u, L))
                    e += saved(pg, w, P)
                    st += sst
                res['O'] = (e, st)
                # B4-UB: PG whole launch if unused, else never
                e = sum(saved(lu.cycles, 0, P) for lu, _, _ in lst if not lu.used)
                res['B4-UB'] = (e, 0.0)
                # B3-BET
                nb = min((c for c in CANDIDATES if c is not INF and c >= bet), default=INF)
                res['B3-BET'] = run_policy(lambda lu, u, L: (nb, True))
                # B3-perkernel-oracle and B3-global need the global search below
                res['_n_bet'] = nb
                # P1: history per kernel key, walking the sequence in order
                table = {}
                e = st = 0.0
                used_fallback = 0
                for lu, u, L in lst:
                    key = (runs[L[0]]['app_key'], runs[L[0]]['launches'][L[1]]['kernel'])
                    ent = table.get(key)
                    if ent and ent['conf'] >= a.theta:
                        n = ent['n']
                        pw = n != 0
                    else:
                        n, pw = nb, True
                        used_fallback += 1
                    pg, w, s = evaluate(lu, n, pw, P, wake, lead(u, L), prol(L))
                    e += saved(pg, w, P)
                    st += s
                    # train on this launch
                    nn = best_n([(lu, u, L)], P, wake, lead, prol, a.cap, lambda n: n != 0)
                    if ent is None:
                        table[key] = {'n': nn, 'conf': 1}
                    elif abs(bucket(nn) - bucket(ent['n'])) <= 1:
                        ent['conf'] = min(3, ent['conf'] + 1)
                    else:
                        ent['conf'] -= 1
                        if ent['conf'] <= 0:
                            table[key] = {'n': nn, 'conf': 1}
                res['P1'] = (e, st)
                res['_p1_fallback'] = used_fallback
                # B3-perkernel-oracle: best N per kernel over its launches (offline)
                byk = defaultdict(list)
                for lu, u, L in lst:
                    byk[(runs[L[0]]['app_key'], runs[L[0]]['launches'][L[1]]['kernel'])].append((lu, u, L))
                e = st = 0.0
                for kl in byk.values():
                    n = best_n(kl, P, wake, lead, prol, a.cap, lambda n: True)
                    for lu, u, L in kl:
                        pg, w, s = evaluate(lu, n, True, P, wake, lead(u, L), prol(L))
                        e += saved(pg, w, P)
                        st += s
                res['B3-perkernel-oracle'] = (e, st)
                results.append({'scenario': scen, 'k': k, 'wake': wake, 'seq': sname, 'unit': unit,
                                'bet': bet, 'leak_energy': leak_energy, 'cycles': total_cycles,
                                'launches': len(lst), 'res': res})

    # B3-global: one N per (scenario, k, wake) over every sequence and unit
    groups = defaultdict(list)
    for r in results:
        groups[(r['scenario'], r['k'], r['wake'])].append(r)
    for g, rs in groups.items():
        scen, k, wake = g
        best = (INF, -1e30)
        cache = {}
        for n in CANDIDATES:
            tot, st, cyc = 0.0, 0.0, 0
            per = []
            for r in rs:
                d = DOMAIN_OF[r['unit'].split('.')[0]]
                P = {'leak': leak_scen[scen](d), 'T': T, 'etrans': k * dom[d]['dyn_tt'] * T}
                e = s_ = 0.0
                for test, i in dict(seqs)[r['seq']]:
                    lu = runs[test]['launches'][i]['units'].get(r['unit']) or \
                        synthetic.get((r['seq'], test, i, r['unit']))
                    if lu is None:
                        continue
                    pg, w, s = evaluate(lu, n, True, P, wake,
                                        runs[test]['lead'].get(r['unit'].split('.')[0], 6),
                                        runs[test]['launches'][i]['prologue'])
                    e += saved(pg, w, P)
                    s_ += s
                per.append((e, s_))
                tot += e
                st += s_
                cyc += r['cycles']
            cache[n] = per
            if st <= a.cap * cyc and tot > best[1]:
                best = (n, tot)
        for r, val in zip(rs, cache[best[0]]):
            r['res']['B3-global'] = val
            r['res']['_n_global'] = best[0]

    # P1-C (concern 11 option C): fallback = the B3-global timeout (a design-time constant),
    # a stored N* = 0 only keeps the unit in PG at launch (fallback timeout once it is used),
    # and an entry is trusted only at confidence >= 2.
    for r in results:
        scen, k, wake = r['scenario'], r['k'], r['wake']
        n_fb = r['res']['_n_global']
        d = DOMAIN_OF[r['unit'].split('.')[0]]
        P = {'leak': leak_scen[scen](d), 'T': T, 'etrans': k * dom[d]['dyn_tt'] * T}
        lead = lambda u, L: runs[L[0]]['lead'].get(u.split('.')[0], 6)
        prol = lambda L: runs[L[0]]['launches'][L[1]]['prologue']
        table = {}
        e = st = 0.0
        fallback = 0
        for test, i in dict(seqs)[r['seq']]:
            lu = runs[test]['launches'][i]['units'].get(r['unit']) or \
                synthetic.get((r['seq'], test, i, r['unit']))
            if lu is None:
                continue
            u, L = r['unit'], (test, i)
            key = (runs[test]['app_key'], runs[test]['launches'][i]['kernel'])
            ent = table.get(key)
            if ent and ent['conf'] >= P1C_THETA:
                if ent['n'] == 0:
                    pg, w, s_ = evaluate_stay_then(lu, n_fb, P, wake, lead(u, L))
                else:
                    pg, w, s_ = evaluate(lu, ent['n'], True, P, wake, lead(u, L), prol(L))
            else:
                fallback += 1
                pg, w, s_ = evaluate(lu, n_fb, True, P, wake, lead(u, L), prol(L))
            e += saved(pg, w, P)
            st += s_
            nn = best_n([(lu, u, L)], P, wake, lead, prol, a.cap, lambda n: n != 0)
            if ent is None:
                table[key] = {'n': nn, 'conf': 1}
            elif abs(bucket(nn) - bucket(ent['n'])) <= 1:
                ent['conf'] = min(3, ent['conf'] + 1)
            else:
                ent['conf'] -= 1
                if ent['conf'] <= 0:
                    table[key] = {'n': nn, 'conf': 1}
        r['res']['P1-C'] = (e, st)
        r['res']['_p1c_fallback'] = fallback

    if a.json:
        with open(a.json, 'w') as f:
            json.dump(results, f, indent=1, default=str)
    report(results, dom, T)


POLICIES = ('B3-global', 'B3-BET', 'B3-perkernel-oracle', 'B4-UB', 'P1', 'P1-C', 'O')
P1C_THETA = 2


def report(results, dom, T):
    print('# Step 4 results — offline power-gating policy study\n')
    print('Savings are leakage energy saved by PG minus wake-transition energy, relative to CG '
          'only (B2), as a percentage of the domain\'s leakage energy over the launches '
          '(100% = the domain is power-gated the whole time at no cost). "of O" = share of '
          'the oracle\'s saving. Cells: saving (wake-stall cycles as % of launch cycles). '
          'O power-gates every window longer than BET for its full length and wakes on demand, '
          'so it bounds the energy of any wake-on-demand policy.\n')
    print('## Domain parameters (LVT)\n')
    print('| Domain | P_dyn TT (mW, vectorless) | P_leak TT (µW) | P_leak SS 100°C (µW) |')
    print('|---|---|---|---|')
    for d, p in dom.items():
        print(f"| {d} | {p['dyn_tt']*1e3:.1f} | {p['leak_tt']*1e6:.1f} | {p['leak_ss']*1e6:.1f} |")

    def table(filt, title):
        rs = [r for r in results if filt(r)]
        if not rs:
            return
        print(f'\n## {title}\n')
        print('| Sequence | Unit | Launches | BET | ' + ' | '.join(POLICIES) + ' | P1 of O | P1 fallback |')
        print('|---|---|---|---|' + '---|' * len(POLICIES) + '---|---|')
        for r in rs:
            le = r['leak_energy']
            cells = ' | '.join(f"{100*r['res'][p][0]/le:.1f}% ({100*r['res'][p][1]/r['cycles']:.2f}%)"
                               for p in POLICIES)
            o = r['res']['O'][0]
            p1 = r['res']['P1']
            of_o = f'{100*p1[0]/o:.0f}%' if o > 0 else '–'
            print(f"| {r['seq']} | {r['unit']} | {r['launches']} | {r['bet']:,.0f} | {cells} | {of_o} | "
                  f"{r['res']['_p1_fallback']} |")

    table(lambda r: r['scenario'] == 'share10%' and r['k'] == 1 and r['wake'] == 20,
          'Leakage share 10% (concern 9 option C), E_trans k=1, WAKE_LAT=20')
    table(lambda r: r['scenario'] == 'ASAP7-SS100C' and r['k'] == 1 and r['wake'] == 20,
          'ASAP7 measured, SS 100 °C, k=1, WAKE_LAT=20')
    table(lambda r: r['scenario'] == 'ASAP7-TT' and r['k'] == 1 and r['wake'] == 20,
          'ASAP7 measured, TT 25 °C, k=1, WAKE_LAT=20')

    # sensitivity summary: totals over all sequences/units, P1 vs others, per scenario/k/wake
    print('\n## Sensitivity (all sequences and units pooled; % of oracle saving (pooled stall % of cycles))\n')
    print('| Leakage | k | WAKE_LAT | ' + ' | '.join(p for p in POLICIES if p != 'O') + ' | O (% of leak energy) | N global |')
    print('|---|---|---|' + '---|' * (len(POLICIES) - 1) + '---|---|')
    groups = defaultdict(list)
    for r in results:
        groups[(r['scenario'], r['k'], r['wake'])].append(r)
    for (scen, k, wake), rs in groups.items():
        o = sum(r['res']['O'][0] for r in rs)
        le = sum(r['leak_energy'] for r in rs)
        cyc = sum(r['cycles'] for r in rs)
        cells = ' | '.join((f"{100*sum(r['res'][p][0] for r in rs)/o:.0f}% ({100*sum(r['res'][p][1] for r in rs)/cyc:.2f}%)"
                            if o > 0 else '–') for p in POLICIES if p != 'O')
        ng = rs[0]['res'].get('_n_global')
        print(f"| {scen} | {k} | {wake} | {cells} | {100*o/le:.1f}% | {'∞' if ng is None else ng} |")


if __name__ == '__main__':
    main()
