# Step 4 — Offline power-gating policy study (go/no-go)

Branch `mix_gating_cg_pg`. **No RTL changes.** Step 4 combines the idle windows from the rtlsim
traces (steps 2 and 4) with the per-domain power from step 3 in an offline energy model, and
compares the history-guided policy (P1) with the timeout, static and oracle baselines before any
gating RTL is written. Full tables: `step4_results.md`.

## 1. Goals (task order, step 4)

- Compute B3-global, B3-perkernel-oracle, B4-UB, the history-chosen timeout (P1) and the oracle O
  across BET / `WAKE_LAT` sweeps; report the three-way gap to O.
- Fix the model parameters left open by concerns 1 and 3 (candidate N set, θ, stall cap).
- **GO/NO-GO** for the history table.

## 2. Workloads (concern 10)

History needs repeated launches, which the step-1/2 set mostly lacks, so new runs were traced
(`cs752_tools/cases/step4_xlen{32,64}.txt`, same flow as step 2) and composed into launch sequences
(`cs752_tools/cases/step4_sequences.txt`):

| Sequence | Class | Launches | Contents |
|---|---|---|---|
| `w1_sgemm_tcu_r8` | W1 repeated | 8 | `sgemm_tcu -r8`: 8 hardware relaunches in one session |
| `w1_tfu_r15` | W1 repeated | 15 | `tfu` (15 launches, the test cycles through TFU ops) |
| `w2_gemmwg_mixed` | W2 varying size | 6 | `sgemm_tcu_wg` 32/64/128 in mixed order |
| `w2_softmax_mixed` | W2 | 5 | `softmax` n = 32/64/128 |
| `w2_sgemm_mixed` | W2 | 6 | `sgemm` n = 16/32/64 |
| `w2_fawg_mixed` | W2 (XLEN 64, 4 TCU blocks) | 4 | `flash_attn_tcu_wg` N = 16/32 |
| `w3_transformer` | W3 interleaved | 12 | 4 layers of [GEMM → softmax → GEMM], size 64/32 alternating |

New traces: 9 runs, all pass, every dispatched instruction matched to its commit. `softmax` grows
fast (n = 128: 7.1 M instructions, 19 M cycles, 78 min to trace and analyze); keep n ≤ 64 in later
steps. Composed sequences start each launch from a fresh run's state (accepted in concern 10).
In a composed sequence, a unit that a launch's build lacks (e.g. the TCU in a `softmax` launch) is
modelled as present and idle for that whole launch, as it would be on the target hardware.

## 3. Model (`cs752_tools/gating_model.py`)

Per launch and PG domain (TCU, FPU, TFU), from the step-2 pg-view idle windows (lead = launch →
first use, mid, tail):
- timeout N → power-gated cycles Σ max(0, L − N);
- each wake-up costs one transition energy E_trans and stalls max(0, WAKE_LAT − lead) cycles,
  lead = the unit's decode→dispatch lead time (6–7 cycles, step 2);
- **launch boundary is cost-neutral:** inter-kernel time is excluded (concern 5b), so the
  transition implied by between-kernel PG is not charged either; a policy starts a launch with the
  unit ON (pre-wake) or in PG (stay) for free;
- saving = P_leak·T·(PG cycles) − E_trans·(wakes), relative to CG only (B2), reported as % of the
  domain's leakage energy over the launches, with wake stalls as % of cycles.

Policies:

| Policy | Definition |
|---|---|
| B3-global | One timeout N for every kernel and unit, chosen offline to maximize total saving with pooled stalls ≤ cap |
| B3-BET | N = BET rounded up to a candidate (ski-rental); ignores the cap |
| B3-perkernel-oracle | Best N per kernel, chosen offline over that kernel's launches, stalls ≤ cap |
| B4-UB | PG a unit for a whole launch iff that launch never uses it (upper bound on static/compiler PG) |
| **P1** | Per-kernel history as originally designed (key = app + kernel; hardware uses `entry_r`). After each launch, N\* = the candidate maximizing that launch's saving with stalls ≤ cap; ±1-bucket match → confidence+1, else −1, replace at 0 (max 3). Use the stored N\* if confidence ≥ θ, else fallback N = BET. N\* = 0 → stay in PG at launch; otherwise pre-wake |
| **P1-C** | P1 with concern 11 option C: fallback = B3-global's N, N\* = 0 only at launch, θ = 2 (§5.1) |
| O | Per-window oracle with wake-on-demand: PG every window longer than BET for its full length (energy upper bound for wake-on-demand policies); its stalls are reported |

Sweeps: leakage {ASAP7 LVT TT, ASAP7 LVT SS 100 °C, leakage share 5/10/20/30% of the domain's
power (concern 9 option C)} × E_trans k ∈ {1, 3, 10} × WAKE_LAT ∈ {10, 20, 50} × cap ∈
{0.5, 1, 2, 5%} × θ ∈ {1, 2}. Each run of the model takes ~7 s.

Candidate timeouts: {0, 8, 16, …, 16384, ∞} (14 values, 4 bits). Concern 1's 3-bit set stopped at
256, but the step-3 BETs (130–8,000 cycles) need timeouts up to ~16 K.

### Modelling fixes made during this step
1. The first oracle woke early to avoid stalls, so it gave up WAKE_LAT gated cycles per window and
   P1 could exceed it; O is now the wake-on-demand bound.
2. Charging the launch wake without crediting the between-kernel PG made every policy, including
   the oracle, lose energy at TT; hence the cost-neutral launch boundary.
3. Synthetic idle units (W3) are kept per sequence so they do not leak into other sequences.

## 4. Results

### 4.1 Pooled over all sequences (θ = 2, cap 1%, k = 1, WAKE_LAT = 20; % of the oracle's saving, pooled stall)
| Leakage | O (% of leakage energy) | B3-global | B3-BET | B3-perkernel-oracle | B4-UB | **P1** |
|---|---|---|---|---|---|---|
| ASAP7 TT | 36.8% | 85% (0.00%) | 86% (0.02%) | 91% (0.01%) | 62% | **74%** (0.03%) |
| ASAP7 SS 100 °C | 70.4% | 87% (0.61%) | 73% (0.39%) | 86% (0.49%) | 32% | **84%** (0.48%) |
| share 5% | 91.6% | 94% (0.75%) | 96% (1.24%) | 90% (0.51%) | 10% | **94%** (0.81%) |
| share 10% | 93.7% | 93% (0.75%) | 97% (2.09%) | 88% (0.51%) | 10% | **93%** (1.11%) |
| share 30% | 95.4% | 91% (0.75%) | 98% (2.64%) | 87% (0.56%) | 10% | **92%** (1.33%) |

With θ = 1, P1 drops to 48% at TT (one bad launch is trusted immediately); at other points θ
changes P1 by ≤ 4 points. Raising the cap to 2% moves all timeout policies to 94–96% at the
leakage-share points.

### 4.2 Where history wins and loses (θ = 2, cap 1%; % of O)
| Case | O | P1 | B3-global | Why |
|---|---|---|---|---|
| `w2_fawg_mixed` TCU, ASAP7 TT | 80% | **87%** | 62% | the global N (8192) is set by other kernels; per-kernel N fits attention |
| `w2_fawg_mixed` FPU, ASAP7 SS | 24% | **56%** | −27% | same: the global N loses energy on this kernel |
| `w1_tfu_r15` FPU, ASAP7 SS | 43% | **93%** | 51% | per-kernel N learned after one launch |
| `w3_transformer` TCU, all | 99–100% | 98–100% | 98–100% | TCU idle through softmax; every timeout catches it |
| `w1_tfu_r15` TFU, ASAP7 SS | 32% | **−23%** | 41% | `tfu` changes op every launch, so the previous launch is a poor predictor |
| `w1_tfu_r15` FPU, ASAP7 TT | 43% | **−229%** | 95% | a launch with no FPU use trains N\* = 0; the next launch uses the FPU and pays a wake per short gap |
| `w2_gemmwg_mixed` TCU, ASAP7 SS | 75% | 75% | 84% | size changes between launches |

### 4.3 TCU vs FPU vs TFU
- **TCU:** the dominant opportunity. In WGMMA/attention workloads the oracle saves 80–99% of TCU
  leakage at every leakage point, including ASAP7 TT, and all timeout policies reach most of it.
- **FPU:** kernel-dependent: near-zero opportunity in FP-heavy kernels at ASAP7 (BET ~8,000), large
  in TCU kernels; this is where per-kernel N helps most and where mispredictions hurt most.
- **TFU:** its windows (≤ 1,014 cycles) sit at or below the BET at ASAP7, so PG rarely pays; at
  leakage shares ≥ 5% it pays but only with stalls above a 1% cap.

## 5. Go/no-go

The plan's criterion (concerns doc, revised task order step 4): *if P1 captures little beyond B4-UB
and B3-perkernel-oracle, report that and stop after step 6.*

- **Timeout-based PG: GO.** Every timeout policy captures far more than static PG (B4-UB 10–62% of
  O), and the TCU opportunity holds even at the measured ASAP7 corners.
- **History (P1): mixed.** Pooled, P1 is within ±3 points of the best single global timeout at SS
  and at leakage shares 5–30%, and 11 points behind it at TT. It wins clearly where kernels need
  very different timeouts and a global N is set by other kernels (attention: +25 points at TT),
  and loses where consecutive launches of one kernel differ (op or size changes), even with θ = 2.
  By the plan's criterion P1 captures much more than B4-UB but nothing beyond the offline
  per-kernel oracle on these workloads.

Steps 5 and 6 (CG, then PG with the timeout baseline B3) are needed in either case. Whether to
build step 7 (the history table) was raised as concern 11; **option C was chosen** (§5.1).

### 5.1 Option C — robust history policy (P1-C)

P1's losses all come from trusting a misprediction: a launch that did not use a unit trains
N\* = 0 ("power-gate on drain"), and when the next launch does use the unit it pays a wake on
every short gap. P1-C keeps the per-kernel table but makes a wrong entry cheap:

1. **Fallback = B3-global's timeout** (a design-time constant), not BET: a miss or a low-confidence
   entry behaves exactly like the best single timeout.
2. **N\* = 0 only means "start the launch in PG".** If the unit is used after all, it wakes on first
   use and the fallback timeout applies for the rest of the launch, so a wrong "unused"
   prediction costs one wake instead of one per short gap.
3. **θ = 2:** an entry is trusted only once a second launch agrees with it.

Training is unchanged (best N under the stall cap on each launch, ±1-bucket hysteresis).

Pooled (θ = 2, cap 1%, k = 1, WAKE_LAT = 20; % of the oracle's saving, pooled stall):

| Leakage | B3-global | P1 | **P1-C** |
|---|---|---|---|
| ASAP7 TT | 85% (0.00%) | 74% (0.03%) | **85%** (0.01%) |
| ASAP7 SS 100 °C | 87% (0.61%) | 84% (0.48%) | **88%** (0.55%) |
| share 5% | 94% (0.75%) | 94% (0.81%) | **93%** (0.68%) |
| share 10% | 93% (0.75%) | 93% (1.11%) | **91%** (0.68%) |
| share 30% | 91% (0.75%) | 92% (1.33%) | **90%** (0.68%) |

Per sequence, P1-C removes every large loss (worst cases ~0%, where P1 reached −229%, −25% and
−23%) and keeps most of P1's wins (attention TCU at ASAP7 TT: P1-C 79.5%, P1 86.8%, B3-global
62.0% of O). It is more conservative than B3-global on a few sequences at the leakage-share
points (`tfu`), where B3-global saves more partly by exceeding the 1% stall cap on that
sequence. At a 2% cap, P1-C equals B3-global within 1 point everywhere. Step 7 builds P1-C.

## 6. Limitations
- Dynamic power, E_trans and the relative BET model come from vectorless synthesis power (step 3).
- Composed sequences (W2/W3) lose cache and branch state across launches.
- Stalls are counted but not converted to energy; the stall cap stands in for performance cost.
- Workload set is small; `tfu`'s per-launch op changes dominate the irregular case.

## 7. Tools added (`cs752_tools/`)
| File | Purpose |
|---|---|
| `gating_model.py` | Offline policy model; markdown report + JSON of every scenario |
| `cases/step4_xlen32.txt`, `cases/step4_xlen64.txt` | New traced runs (concern 10) |
| `cases/step4_sequences.txt` | Launch sequences W1–W3 |

Reproduce:
```bash
cs752_tools/run_cases.sh build   cs752_tools/cases/step4_xlen32.txt <t32>
cs752_tools/run_cases.sh build64 cs752_tools/cases/step4_xlen64.txt <t64>
python3 cs752_tools/gating_model.py --runs <step2> <step2_64> <t32> <t64> \
    --ablation <step3 ablation dir> --sequences cs752_tools/cases/step4_sequences.txt \
    --cases cs752_tools/cases/*.txt --cap 0.01 --theta 2 > step4_results.md
```
