# Design Concerns — Vortex Hybrid Clock + Power Gating

Review of `VORTEX_GATING_PROJECT.md` (hybrid CG + history-guided PG version), checked against the
Vortex checkout at `vortex/` (commit `ca18837bd`). Concerns are ordered so that the ones that change
*what the design is* come first. Work through them one at a time; record the decision under each.

> **Repository change (2026-11-26):** work moves to `vortex_cs752/` (user's fork of upstream,
> commit `2221ee286`, 39 commits after the `ca18837bd` checkout these concerns were verified on).
> Re-verified in the new tree: `ex_type` encoding, KMU `running`, `VX_cta_dispatch` signals, TCU
> accumulator from `rs3_data`, `VX_tcu_meta` inside `VX_tcu_core`, `abuf` first-uop refetch, SFU
> contents, `VX_csr_fpu` in `VX_sfu_unit`, `ISSUE_WIDTH` rule. Changes to read through everywhere below:
> - **MUFU was renamed TFU** upstream (`e6fd1c31f`): `hw/rtl/tfu/VX_tfu_unit.sv`,
>   `VX_CFG_EXT_TFU_ENABLE` (default off), SFU op `INST_SFU_TFU`. Same hardware.
> - `flash_attn_tcu_wg` pins `VX_CFG_ISSUE_WIDTH=4` with 4 warps, so `VX_CFG_NUM_TCU_BLOCKS` = 4
>   (one TCU block per issue slice, i.e. per warp), while CI pins ALU/LSU/FPU to 1 block. TCU gating
>   and drain must therefore be per TCU block in that config.
> - `flash_attn` / `flash_attn_tcu_wg` run on rtlsim only at XLEN=64 (RV32 soft-float conversions
>   inflate instruction counts 4–8×), so a `build64/` is needed alongside `build/`.

> **Correction (2026-11-26): the history key is the kernel entry, not `cta_PC`.** Traces of
> `multikernel` show the KMU launches every kernel at the same start PC (`cta_PC` =
> `VX_DCR_KMU_STARTUP_ADDR` = `_start`, 0x80000000); the per-kernel address is written to
> `VX_DCR_KMU_KERNEL_ENTRY` → `dcr_entry` → `kmu_req.entry`, latched per core in `entry_r`
> (`hw/rtl/core/VX_cta_dispatch.sv:696`) and exposed as `VX_CSR_CTA_ENTRY`. Earlier text claiming
> `cta_PC` and `dcr_entry` are the same value was wrong; all references below now use `entry_r`.

| # | Concern | Affects | Status |
|---|---|---|---|
| 1 | When the CG→PG decision is made (+ CG/PG sequencing and energy accounting) | §3.1, §3.3, §3.4, §4.1, §4.3 | **Decided: option B** (parameters TBD in step 4) |
| 2 | Definition of the static baseline B4 | §4.1, step 4 | **Decided: compiler B4 dropped; B4-UB (trace upper bound) kept** |
| 3 | Misprediction definition and training | §3.1, §3.3, §4.2 | **Decided: perf-capped energy objective, ±1-bucket hysteresis, fallback N = BET** |
| 4 | Region-level (Level 2) lookup in hardware | §3.2, §3.3, step 8 | **Decided: dropped entirely** |
| 5 | Pre-wake and power gating between kernels | §3.1, §4.2 | **Decided: per-core epoch, PG between kernels (excluded from energy), launch-time pre-wake by N\*** |
| 6 | Long-lived state inside units (TCU, SFU) | §3.4, step 6 | **Decided: explicit PG domains, instruction-level drain, reset on wake** |
| 7 | Small doc fixes | various | **Decided: drop SLVT, install ASAP7 at step 3, reword §3.2, fix testbench list** |
| 8 | Baseline for clock-gating savings (tool-inserted CG vs own ICGs) | §4.1, §4.2, §4.3, step 9 | **Decided: two synthesis stacks (with and without Yosys `clockgate`)** |

---

## Summary of chosen handling (one line per concern)

| # | Concern | How it is handled |
|---|---|---|
| 1 | When the CG→PG decision is made | **Learned timeout N\* per (kernel, unit).** Unit is CG'd when idle and drained, PG'd after N\* idle cycles (N\* = 0 → PG on drain; N\* = ∞ → CG only). BET is a design-time constant used only to pick N\*. PG always implies CG (clock off in PG and WAKING); PG savings are counted only as leakage on top of CG. |
| 2 | Static baseline B4 | **No compiler work.** Replaced by **B4-UB**: offline upper bound that PGs every unit never dispatched to during a launch, computed from RTL traces. |
| 3 | Misprediction and training | N\* = lowest-energy candidate whose predicted stalls stay under a performance cap X%. ±1-bucket match → confidence +1, else −1; replace N\* only at confidence 0 (hysteresis). Miss / low confidence → fallback N = BET. Report gap to O as three energy terms (premature PG, waiting cost, skipped windows) plus stalls. |
| 4 | Region-level (Level 2) table | **Dropped entirely.** Key is the kernel entry PC only; P2 and step 8 removed. |
| 5 | Pre-wake and between kernels | Per-core launch epoch: starts on first CTA with a new kernel entry (`entry_r`); ends when core drained ∧ KMU not running, or a CTA with a different entry arrives. All gateable units PG'd between kernels; inter-kernel time excluded from energy. At launch: N\* ≠ 0 → pre-wake; N\* = 0 → stay PG'd; miss → wake all, N = BET. |
| 6 | Long-lived state inside units | PG domains: TCU datapath + tile buffers, MUFU, FPU. CG only: TCU metadata RAM, SFU CSR / warp-control / `csr_fpu` / `dp_ram`, ALU, LSU. Drain is per instruction (never mid-uop-sequence). Scramble on PG entry, reset on wake (simulation model). MUFU demand = `EX_SFU` + SFU op type. |
| 7 | Small doc fixes | Drop SLVT (LVT main, RVT sensitivity). Install ASAP7 at step 3. Reword §3.2 keying text. Fix testbench list (`issue`, `fma_unit`, `tcu_unit`, `mufu_unit`, `core`). |
| 8 | Baseline for CG savings | Two synthesis stacks from the same RTL: without Yosys `clockgate` (B0 → B2′ → P1′, headline) and with it (B1 → B2 → P1, credibility). Report dynamic and leakage savings separately in both. |

**Final configuration set** (each in both synthesis stacks where it applies):
B0 / B1 (no unit gating), B2 (reactive unit CG), B3-global (timeout PG, one N), B3-perkernel-oracle
(timeout PG, best N per kernel, offline), B4-UB (trace-based static upper bound, offline), P1
(history-learned N\*), O (per-window oracle, offline).

---

## Revised task order

Replaces §5 of `VORTEX_GATING_PROJECT.md`. Concern numbers in brackets.

1. **Baseline runs.** Build Vortex; run `./ci/blackbox.sh --driver=rtlsim --app=<kernel>` with
   `PERF=1` on the workload set; collect per-unit utilization.
2. **Instrumentation and traces.**
   - Per-unit busy/idle trace per cycle at dispatch; idle-run histograms per launch epoch,
     counting only windows that end inside the epoch [3d, 5a].
   - Issue→dispatch lead time per unit.
   - Prologue length (launch → first kernel-body instruction) vs `WAKE_LAT`; per-unit first-use
     delay from launch [5d].
   - Check whether the KMU ever places two kernels on one core [3d].
3. **Per-unit synthesis.** Install ASAP7 (`hw/syn/libs/asap7/`); verify ASAP7 ICG and Nangate
   `CLKGATETST_X1` cell/pin names [7]. Yosys + OpenSTA per unit / PG domain → dynamic and leakage
   power, LVT main corner, RVT sensitivity [7]. Compute BET_u for a sweep of `E_trans` [1].
4. **Offline study (GO/NO-GO).** From the traces, compute B3-global, B3-perkernel-oracle, B4-UB,
   history-chosen N\* (P1 model) and O, across BET / `WAKE_LAT` sweeps [1, 2, 3]. Fix the candidate
   N set, histogram bins and counter width, confidence threshold θ, and performance cap X [1, 3].
   Report the three-term energy gap to O [3e].
   **GO/NO-GO:** if P1's model captures little beyond B4-UB and B3-perkernel-oracle, report that
   and stop after step 6.
5. **Unit CG (B2).** Behavioral latch-based ICG under `SIMULATION`, per-unit enable plumbing,
   default off. Reactive controller with a drain check at instruction granularity (CG is allowed
   in gaps inside a uop sequence; PG, added in step 6, is not) and an always-on demand detector; MUFU
   demand via `EX_SFU` + op type [6b, 6d]. Unit tests (`issue`, `fma_unit`, `tcu_unit`,
   `mufu_unit`, `core`). Regression with gating on and off.
6. **Behavioral PG + baselines (B3).** PG only on the agreed domains; clock forced off in PG and
   WAKING; isolation; `WAKE_LAT` counter; scramble on PG entry, reset on wake [1, 6a, 6c].
   Per-core epoch detection and PG between kernels [5a, 5b]. Timeout PG (B3). Regression on/off,
   including TCU tests with WGMMA, sparse and MX enabled [6e].
7. **History table (P1).** Per-core table keyed on the kernel entry PC (`entry_r` in `VX_cta_dispatch`, not `cta_PC`); per unit N\* index + 2-bit
   confidence; per-unit idle-run histograms; kernel-end N\* selection under the performance cap;
   ±1-bucket hysteresis; fallback N = BET; launch-time pre-wake for N\* ≠ 0 [1, 3, 5c]. Perf
   counters: cycles in ON / CG / PG / WAKING, PG entries, wake stalls, pre-wakes, fallback launches.
8. **Synthesis and power, both stacks.** Every config synthesized without and with Yosys
   `clockgate` [8]; gate-level SAIF; OpenSTA power and timing; area of controller + ICGs + table.
   Analytical PG model with PG savings counted as leakage on top of CG [1]. Report dynamic and
   leakage savings separately; headline P1′ vs B0, credibility P1 vs B1 [8].
9. **(Final) OpenROAD-flow-scripts** for clock-tree-aware CG numbers.
10. **Stretch:** gating-aware warp scheduling to lengthen idle windows (compare to Warped Gates).

Removed from the original plan: region-level history table (old step 8) [4]; compiler-based static
PG baseline [2].

---

## 1. When the CG→PG decision is made

### What the doc says
- §3.1: *CG → PG only if the history table predicts **remaining** idle > BET with confidence ≥ θ.*
- §3.3: each (kernel, unit) entry stores one **predicted idle class** (2–3 bits) + confidence.

### Problem
The table holds **one value per (kernel, unit)**, a summary of all that unit's idle runs in a
launch. But the PG decision has to be made **once per idle window**, and windows within one kernel
vary widely. At idle onset the controller can only ask "what is the typical idle run for this unit
in this kernel?", which gives the same answer for every window.

**(a) All-or-nothing within a kernel.** If the entry says "≫ BET", every idle window is power-gated,
including the many short gaps between instructions; each pays transition energy and a wake stall,
a net loss. If it says "< BET", nothing is power-gated, including the one long window that held all
the savings.

**(b) Bimodal idle distributions can't be represented by one bucket.** Example: FPU in a GEMM with
an FP epilogue:

```
FPU idle runs in one launch:
  ~2000 runs of 2–8 cycles     (gaps between FMAs in the inner loop)
  1 run of ~40,000 cycles      (address math / LSU-bound phase)
```

- Typical run (by count) is short → entry says "< BET" → misses the big window.
- Typical run (time-weighted) is long → entry says "≫ BET" → thrashes 2000 times.

The right policy is "wait a bit; if still idle, power-gate", which is a **timeout**, i.e. B3.

**(c) "Remaining idle" can't be computed from the stored fields.** It is a conditional quantity
(given *t* idle cycles so far, how many more?) and requires the shape of the distribution, which a
single bucket does not keep.

### Why it matters for the evaluation
As written, P1 collapses into an existing baseline:
- Decided once at launch ("unit off for this kernel") → nearly **B4**; wins only on units reachable
  in code but unused at runtime.
- Decided at idle onset → a **timeout**, i.e. **B3** with a learned N instead of a swept N.

A reviewer will ask "how is P1 different from B3/B4?"; the doc has to answer that explicitly.

### Options
**A. Launch-time decision.** Entry stores "unused / rarely used". At launch those units go to PG on
drain and stay there; all other units use CG only.
- ✅ Simplest hardware and training (used bit + confidence).
- ❌ Very close to B4.

**B. Learned per-(kernel, unit) timeout (recommended).** Entry stores a quantized timeout N\*
(3 bits, e.g. {0, 8, 16, 32, 64, 128, 256, ∞}). When the unit goes idle it stays in CG for N\*
cycles, then power-gates.
- N\* = 0 → PG on drain (subsumes option A); N\* = ∞ → CG only.
- Training: per-unit log2 idle-run histogram during the launch (≈ 8 bins × 12-bit counters ≈ 100
  flops per unit). At kernel end, evaluate expected energy for each of the 8 candidate N against
  BET (a few cycles of simple arithmetic) and write back the best.
- Confidence: same N\* as stored → increment; different → decrement, replace at 0. Below θ, use a
  safe default (the global B3 N).
- ✅ Principled (timeouts are the classic answer to unknown idle lengths); handles bimodal
  distributions.
- ✅ Sharp research question: *can last launch's histogram choose N as well as a per-kernel oracle?*

**C. Region-entry decision.** Decide when a warp enters a code region (Level 2). Belongs with
concern 4 and still needs B's timeout inside each region.

### Doc changes if B is chosen
- §3.1: replace "predict(idle > BET) & conf ≥ θ" with "CG for N\*[kernel][unit] cycles → PG
  (N\* = 0 → PG on drain; N\* = ∞ → never PG)".
- §3.3: per-unit field = 3-bit N\* index + 2-bit confidence. ≈ 5 units × 5 bits + ~8 tag + valid
  ≈ 34 bits/entry → ≈ 1.1K flops for 32 entries, plus ≈ 0.5K flops of training histograms per core.
- §4.1: split B3 into **B3-global** (best single N for all kernels) and **B3-perkernel-oracle**
  (best N per kernel, chosen offline). P1 is reported as where it lands between them. O remains the
  per-window oracle.
- Step 4 can compute global N, per-kernel oracle N, and history-chosen N offline from the idle
  traces before any RTL is written.

### Addendum: CG and PG together (applies whichever option is chosen)

PG always includes CG: a power-gated domain also has its clock stopped. The ICG sits in the
always-on domain; leaving the clock running into an unpowered domain wastes clock-net power and
risks glitches / false captures while the rail ramps.

**Required sequencing:**
```
Power down:  gate clock → isolate outputs (clamp valid=0) → [save retention state] → header switches off
Power up:    header switches on → wait for rail to settle (WAKE_LAT) → reset/[restore] → remove isolation → ungate clock
```

**Per-state accounting:**

| State | Clock | Supply | Saved vs ON |
|---|---|---|---|
| ON | running | on | nothing |
| CG | gated | on | unit dynamic power (clock tree below ICG, flop clock pins, datapath toggling) |
| PG | **gated** | off | CG savings **+ unit leakage** |
| WAKING | **gated** | ramping | CG savings only; transition energy and wake stall are charged |

**Doc changes:**
- §4.3: state that `E_saved,u = P_leak,u × T_PG,u − N_PG,u × E_trans,u` is the **incremental**
  saving of PG *on top of* CG. Dynamic savings during PG windows are already counted under CG; do
  not count them twice. WAKING cycles count as CG with no leakage saving, plus the stall cost.
- §3.1 / §3.4 (RTL rule): the unit's ICG enable is forced off whenever the controller is in PG or
  WAKING. Demand or pre-wake while in PG must go through the `WAKE_LAT` countdown; the ~1-cycle
  CG wake applies only from the CG state.
- Mixed gating within one unit is allowed and is the intended resolution for concern 6: e.g. PG the
  TCU datapath while only clock-gating the always-on metadata RAM; PG MUFU while only clock-gating
  the SFU's CSR / warp-control logic.

**Decision (2026-11-24): Option B — learned timeout N\* per (kernel, unit).**
- P1 = CG for N\*[kernel][unit] idle cycles, then PG (N\* = 0 → PG on drain; N\* = ∞ → CG only).
- Table stores N\* index + confidence per unit; BET_u is a design-time constant used only at
  training time (kernel end) to pick N\* from the launch's idle-run histogram, never stored.
- Baselines: B3 is split into **B3-global** and **B3-perkernel-oracle**; P1 reported as where it
  lands between them, with O as the per-window bound.
- CG/PG sequencing and incremental-energy accounting per the addendum above.
- Still to fix, with numbers from the step-4 offline study: candidate N set, histogram bin
  boundaries and counter width, confidence threshold θ, and the fallback N used below θ.

---

## 2. Definition of the static baseline B4

### What the doc says
§4.1 B4: *"PG units the kernel binary never uses."* Called "the trap — history must beat this."

### Problem
"Never uses" is ambiguous in four ways, and each changes how strong B4 is:

1. **Shared prologue.** Every kernel runs `__vx_cta_entry` (`sw/kernel/src/vx_start.S`): SATP
   setup, stack/TLS init, `__libc_init_array` constructors. These use ALU and LSU, and possibly other
   units through library code. If prologue instructions count, B4 loses units the kernel body never
   touches. If they don't count, B4 must wake those units during the prologue anyway.
2. **Binary vs kernel.** A multi-entry `.vxbin` holds several kernels. "The binary never uses FP"
   is much weaker than "this kernel never uses FP". B4 should be **per kernel**.
3. **Reachability.** Library calls (soft-float helpers, `printf`, libm) and indirect jumps make the
   static reachable set conservative. An indirect jump with an unknown target must be treated as
   "may use every unit".
4. **Granularity.** P2 decides per region. A fair static counterpart at the same granularity is a
   compiler that marks regions (e.g. "this loop contains no FP ops → PG FPU on loop entry"). If B4
   is kernel-level only, P2's win over B4 partly reflects granularity, not history.

### Proposed fix
- Define **B4-kernel** = units in the static instruction mix reachable from that kernel's entry
  stub through its call graph (from `objdump` disassembly), prologue excluded but prologue wake cost
  charged. Indirect jumps → conservative (all units).
- Optionally define **B4-region** = same analysis per loop/region, to pair with P2.
- Map opcodes → execution units using the decoder (`hw/rtl/core/VX_decode.sv`) so the mapping
  matches the hardware. Verified: the decoder tags each instruction with `ex_type`, defined in
  `hw/rtl/VX_gpu_pkg.sv:295–301` as EX_ALU=0, EX_LSU=1, EX_SFU=2, EX_FPU (if F), EX_TCU (if TCU).
  There is **no separate MUFU ex_type**: MUFU ops dispatch as EX_SFU, so gating MUFU separately
  (concern 6) needs `ex_type` + the SFU op type, both for the static script and the RTL demand
  detector.
- This is a script, run once per workload; no RTL needed.

### Questions to decide
- Kernel-level only, or also region-level static baseline?
- Charge the prologue's unit usage to B4, or exclude it for all configs equally?

**Decision (2026-11-24): No compiler/static-analysis baseline.** All mechanisms in the project are
hardware-only; the binary-analysis B4 (kernel and region) is dropped.

**Replacement (confirmed 2026-11-24): B4-UB, trace-derived upper bound on static PG.**
Evaluation-only (offline, step 4); not implemented in hardware.
A unit that static analysis could prove unused in a kernel is necessarily also unused at runtime
in every launch of that kernel. So "PG every unit that the RTL trace shows was never dispatched to
during this launch, from launch to end" is an **upper bound** on what any compiler-based B4 could
achieve. It needs no compiler and no RTL: it is computed offline in step 4 from the same per-unit
busy/idle traces. If P1 beats B4-UB, it beats any static baseline; if not, the write-up must say
history mostly re-learns "unit unused in this kernel".

---

## 3. Misprediction definition and training

### What the doc says
§3.1: *Misprediction (woken before BET elapsed) → decrement confidence.* §4.2 reports "premature
wakes" and "pre-wake accuracy".

### Original problem
Only one error type (premature PG) was counted and could only lower confidence, so missed
opportunities were never detected and the table would drift toward "never PG". Counts also
weighed a wasted 5-cycle PG the same as a missed 40,000-cycle window.

### Effect of concern 1 (option B)
Training no longer reacts to individual mistakes: N\* is recomputed from each launch's full idle
histogram. Long windows land in high bins and pull N\* down; many short windows push it up, so
feedback is two-sided. The remaining issues are the details below.

### 3a. Objective used to choose N\*
Energy alone tends to pick N\* = 0 (PG on drain), which stalls dispatch for `WAKE_LAT` on every
return to the unit and can wreck performance. Options considered: energy with stall energy charged
at whole-core power; energy with a performance cap; energy-delay product.

### 3b. Confidence and replacement
Each launch produces N\*_new; the table holds N\*_stored + 2-bit confidence. Questions: exact vs
±1-bucket match (adjacent buckets are often near-equal in energy, so exact matching churns), and
replace-at-zero hysteresis vs last-value (HPCA 2017 shows "predict previous kernel" fails on
irregular apps, which favours hysteresis).

### 3c. Cold start / table miss
First launch of each kernel, or an evicted kernel, has no entry. Options: N = BET (ski-rental
timeout, provably ≤ 2× the oracle's waste, a pure hardware constant) or the offline-tuned B3
global N.

### 3d. Histogram collection details
- The last idle run of a launch is cut off at kernel end, and runs can straddle launches.
- Counters saturate (12 bits ≈ 4K windows per bin).
- Concurrent kernels on one core make the histogram's owner ambiguous. **VERIFY** whether the KMU
  can place two kernels on one core.

### 3e. Reporting
"Misprediction rate" by count is replaced by an energy breakdown of the gap to oracle O, per
window of length L under timeout N\*:
1. **Premature PG:** L > N\* but L − N\* < BET → PG'd and lost energy on the transition.
2. **Waiting cost:** long windows (L ≫ N\*) spend N\* cycles in CG before PG → leakage the oracle
   would not pay.
3. **Skipped windows:** BET < L ≤ N\* → never PG'd although PG would have paid off.

**Decision (2026-11-24):**
- **3a:** N\* = lowest-energy candidate whose predicted stall cycles stay under a performance cap
  of X% of kernel cycles (X to be chosen, e.g. 1–2%; report sensitivity). Energy terms: premature-PG
  transition losses + leakage while waiting in CG (incremental over CG, per concern 1 addendum).
- **3b:** ±1-bucket match → confidence +1; otherwise −1; replace N\*_stored with N\*_new only when
  confidence reaches 0 (hysteresis). Measure a last-value predictor as a sensitivity point.
  Below θ, use the fallback N but keep training.
- **3c:** fallback N = BET_u (per unit). Report the fraction of launches that run on the fallback.
- **3d (adopted as proposed):** count only idle windows that end inside the launch; inter-kernel
  idle tracked separately (concern 5). Counter width / saturation policy chosen in step 4. With
  several kernels resident on one core: no training, and use the largest N\* (least gating).
- **3e (adopted as proposed):** report the three energy terms above per unit per kernel, plus stall
  cycles, instead of a count-based misprediction rate. Pre-wake accuracy deferred to concern 5.

---

## 4. Region-level (Level 2) lookup in hardware

### What the doc says
§3.2: *hash a warp's current PC region (loop head / backward-branch target, or `PC >> R`)…
per warp, never per thread.* §3.3: region level may need 32–64 entries.

### Problem
1. **A unit is shared; regions are per warp.** Several warps can be in different regions at the
   same time. The unit is idle only if *no* warp needs it, so per-warp predictions must be
   **combined** (e.g. PG only if every active warp's region predicts no use / long idle). The doc
   doesn't define the combine rule.
2. **Lookup bandwidth.** If lookups happen on every region change for every warp, several can occur
   in one cycle → multiple read ports, or an arbitrated single port with delayed predictions. Not
   specified.
3. **Region detection.** `PC >> R` aliases unrelated code in the same address block and splits
   loops crossing a boundary. Loop-head detection needs logic in the branch path (taken backward
   branch) feeding a per-warp "current region" register.
4. **Training attribution.** Unit idle time is a property of the core, not of a warp. When warps
   interleave, which region "owns" an idle window? Ambiguous.
5. **Cost.** Per-warp region registers + lookup port + combine logic + larger table. Must stay
   within budget and its own power must be counted (§3.3 already requires this).
6. **Shared vs per-slice units.** Vortex has issue/execute slices (`VX_issue_slice.sv`,
   `VX_execute_slice.sv`). **VERIFY** whether each unit is per slice or per core; this decides
   which warps' predictions are combined.

### A simpler direction to consider
Train per (region, unit) "**used**" bits from each warp's own instructions (attribution is
unambiguous per warp). Per warp, keep a cached prediction register updated on region change (one
lookup per change, arbitrated). Core-level gate = PG eligible only if no active warp's current
region predicts use; timeout from concern 1 still applies on top.

### Note
Only build this if step 4's offline study shows intra-kernel headroom beyond B4-kernel (and
B4-region, if adopted in concern 2).

**Analysis after concern 1 (option B):** with a learned timeout, P1 already catches intra-kernel
phases (e.g. TCU idle during softmax in fused attention) as long idle windows, PG'd N\* cycles in.
Region knowledge could only add (1) skipping the N\* wait at phase start and (2) pre-waking before
the phase ends, a fixed ~(N\* + WAKE_LAT) cycles per phase boundary. Verified: with ≤16 warps
`VX_CFG_ISSUE_WIDTH` = 1 (`VX_config.toml:67`), so each unit is one block shared by all warps,
making per-warp region predictions need a core-level combine rule, extra lookup ports and
ambiguous training attribution.

**Decision (2026-11-24): Level 2 / P2 dropped entirely (not kept as a stretch goal).**
Doc changes implied:
- §3.2: remove "Level 2 — per region"; the key is the kernel entry PC only.
- §3.3: remove the region-level entry count (32–64).
- §4.1: remove P2. Configs: B0, B1, B2, B3-global, B3-perkernel-oracle, B4-UB, P1, O.
- §4.4: fused attention stays as a workload, now as a test of P1's timeout on intra-kernel phases
  (not "key case for region-level PG").
- §5: remove step 8.
- Report the per-phase-boundary waiting cost + wake stalls (concern 3e, terms 2 and stalls) as the
  limitation a region-level scheme could address; leave it as future work in the write-up.

---

## 5. Pre-wake and power gating between kernels

### What the doc says
§3.1: *PG → WAKING on demand or on history **pre-wake** (e.g. at a kernel launch whose entry says
the unit will be used).*

### Problem
1. **Between-kernel state is unspecified.** What are units doing when no kernel is running on the
   core? If every gate-eligible unit is PG'd while the core is idle, then B3, B4, P1, P2 and O all
   get those savings. That's real energy, but it must not be credited to history.
2. **Launch pre-wake may be unnecessary.** Each launch first runs the prologue
   (`__vx_cta_entry`: SATP, stack, TLS, constructors). If the prologue takes longer than
   `WAKE_LAT`, a unit woken on first demand in the kernel body never stalls, or a simple "wake all
   predicted units at launch" works without history. **Measure** prologue length in cycles.
3. **Mid-kernel pre-wake has no mechanism.** With per-kernel keys (and option B), nothing predicts
   *when* an idle window ends, so pre-wake can only happen at launch. Mid-kernel pre-wake requires
   region-level prediction (concern 4).

### Proposed fix
- Specify the inter-kernel policy (e.g. all PG-eligible units PG'd when the core has no active
  warps), apply it identically to every config, and report **inter-kernel** and **intra-kernel**
  savings separately.
- Measure prologue length (step 1/2). If it exceeds `WAKE_LAT`, drop launch pre-wake from the
  design or keep it only as a measured non-factor.
- Limit pre-wake claims to what the chosen key level can actually support.

### RTL facts (verified 2026-11-25)
- KMU (`hw/rtl/VX_kmu.sv`) runs one kernel at a time; `running` rises on `start` (line 251) and
  drops when **all CTAs have been sent** (line 292), not when they finish.
- Per core, `VX_cta_dispatch` (instantiated in `VX_scheduler.sv`) sees each CTA (`cta_fire`,
  start PC `cta_PC`, plus the kernel entry latched in `entry_r`), `active_warps`, and `busy`. There is no per-core "kernel finished" signal.
- A core processes many CTAs of one kernel in sequence and may briefly have no active warps
  between two of them.
- With region-level prediction dropped (concern 4), launch is the only point where history can
  pre-wake a unit.

**Decision (2026-11-25): recommended approach for all parts.**
- **5a — per-core epoch.** Start: first CTA whose kernel entry (`entry_r`) differs from the current kernel's, or
  any CTA after the previous epoch ended (table lookup happens here). End: (core drained: no
  active warps, CTA dispatch idle, all units drained) **and** (KMU `running` = 0), or arrival of a
  CTA with a different entry PC. Requires one global KMU→core wire; no activity data flows back.
- **5b — between kernels.** At epoch end all PG-eligible units go to PG (same rule in every config
  except B0–B2). Energy is accounted only from launch to epoch end per core; inter-kernel time is
  excluded from headline numbers (reported separately at most), since its length in RTL sim is an
  artifact of the host driver / harness.
- **5c — launch rule.** All gateable units start a launch in PG. On lookup: N\* ≠ 0 → pre-wake
  immediately (hidden behind the `__vx_cta_entry` prologue); N\* = 0 → stay in PG, wake on demand;
  miss or confidence < θ → wake all, timeout N = BET (consistent with 3c). Baselines: B3 wakes all
  units at launch; B4-UB never wakes units unused in that launch; O wakes each unit exactly
  `WAKE_LAT` before first use. P1's launch-time advantage over B3 is therefore not waking units
  learned to be unused.
- **5d (adopted as proposed) — step 2 measurements.** Prologue length (launch → first kernel-body
  instruction) vs `WAKE_LAT`; per-unit first-use delay from launch.

---

## 6. Long-lived state inside units (TCU, SFU)

### What the doc says
§3.4: a unit may be PG'd only if drained and holding no architectural/long-lived state.
**VERIFY** TCU accumulators; **VERIFY** FP flags live in CSRs.

### Findings so far (partly traced; confirm before step 6)
- **FPU:** pipeline datapath only. `frm`/`fflags` are held in `VX_csr_fpu`
  (`hw/rtl/core/VX_csr_fpu.sv`), which is instantiated inside **`VX_sfu_unit`**, not the FPU.
  → FPU is PG-safe after drain.
- **SFU is not one PG domain.** `VX_sfu_block.sv` instantiates `VX_wctl_unit` (warp control),
  `VX_csr_unit`, a `VX_dp_ram`, `VX_mufu_unit`, and tex/rtu/dxa units; `VX_sfu_unit` adds
  `VX_csr_fpu`. CSRs and warp-control state are architectural. → Only sub-units (e.g. MUFU,
  possibly tex/rtu) can be PG domains; "SFU" as listed in the project doc cannot be power-gated
  as a whole.
- **TCU:**
  - Accumulator: no evidence found of an accumulator held in-unit across instructions (C/D appear
    to come via the register file). **Not fully confirmed.**
  - `setup_valid_r` / `setup_header_r` in `VX_tcu_core.sv` (WGMMA setup result) is pending-result
    state that clears on retire. Covered by the drain check; not a concern.
  - **Sparse metadata RAM** (`VX_tcu_meta.sv`, filled by the separate `TCU_LD` instruction via
    `VX_tcu_block.sv`) persists across instructions. Losing it corrupts later sparse MMAs →
    retention, exclusion from PG, or a software contract (reload after wake). Only present with
    `VX_CFG_TCU_SPARSE_ENABLE`.
  - **Tile buffers** `VX_tcu_abuf.sv`, `VX_tcu_bbuf.sv` (core-wide, in `VX_tcu_shared.sv`). `bbuf`
    is keyed by a refill key `{desc_b, bank_row_index}`, i.e. cache-like: losing data costs only
    performance, **but the key/valid bits must be invalidated on PG** or stale data is returned.
    `abuf` refill behaviour not yet traced.
  - TCU is off by default (`VX_CFG_EXT_TCU_ENABLE = false`); WGMMA and sparse are further options.

### Proposed fix
- Define PG **domains** explicitly per unit (datapath vs state-holding blocks), not per "unit name".
- TCU: PG the datapath (`VX_tcu_core` FEDP); keep meta RAM always-on (or exclude sparse configs);
  invalidate tile-buffer keys on PG entry.
- SFU: PG only MUFU (and tex/rtu if enabled); CSR / wctl / barrier logic always-on.
- The behavioral PG model (state scramble under `SIMULATION`) + regression is the check that the
  domain boundaries are right.

### Remaining work
Trace `abuf` refill/valid logic, `VX_tcu_meta` write/read path, and confirm the accumulator path.

### Tracing results (2026-11-25) — supersede the "findings so far" above where they differ
- **TCU accumulator: confirmed safe.** C comes from the register file every instruction
  (`hw/rtl/tcu/VX_tcu_core.sv:546`, `c_val = rs3_data[...]`). No in-unit accumulator.
- **Metadata RAM** (`VX_tcu_meta.sv`, instantiated **inside** `VX_tcu_core` at line 342): per-warp
  sparse 2:4 masks and MX scale factors, written by `TCU_LD`, read by later MMAs. Long-lived;
  present only with `TCU_META_ENABLE` (sparse or MX).
- **Tile buffers** (`VX_tcu_tbuf.sv`: per-block `VX_tcu_abuf`, one shared `VX_tcu_bbuf`): cache-like.
  `abuf` forces a refetch on every WGMMA's first compute uop (`VX_tcu_abuf.sv:143–152`); both clear
  their valid bits on reset. Content loss costs only a refetch if wake applies reset.
- **Intra-instruction state (new):** the uop sequencer (`VX_uop_sequencer.sv`, in `VX_ibuffer.sv`)
  expands one WGMMA into many uops; `abuf` latches the descriptor on the first compute uop
  (`VX_tcu_abuf.sv:299`) and later uops reuse it. PG in a gap between uops of one WGMMA would
  silently corrupt results.

**Decision (2026-11-25): recommended approach.**
- **6a — PG domains.**
  - TCU: FEDP datapath + tile buffers (`tbuf`). Metadata RAM stays powered, CG only; the RTL change
    must place it outside the PG region (the behavioral model excludes it from scrambling).
  - MUFU: `VX_mufu_unit` only. CSR unit, `VX_wctl_unit`, `VX_csr_fpu`, SFU `dp_ram`: CG only.
  - FPU: whole FPU block.
  - ALU, LSU: CG only, never PG (also resolves concern 7, item 5).
- **6b — drain at instruction granularity.** "Drained" = no uops in flight in the unit **and** no
  warp mid-way through a uop sequence targeting it. PG is blocked during a partially issued
  sequence; CG in such gaps is still allowed (state retained).
- **6c — reset on wake.** Under `SIMULATION`, scramble/X-inject the PG domain's flops on PG entry;
  apply reset to the domain when leaving WAKING (clears tile-buffer valid bits). Regression with
  gating on proves no dependence on lost state.
- **6d (adopted as proposed) — MUFU demand detection** uses `ex_type == EX_SFU` plus the SFU op
  type, since MUFU has no ex_type of its own.
- **6e (adopted as proposed) — test coverage.** Gating-on regression must include TCU tests with
  WGMMA, sparse and MX enabled. **VERIFY** which tests in `tests/regression/` cover those configs.

---

## 7. Small doc fixes

1. **No SLVT library.** §4.3 says choose SLVT/LVT/RVT, but the pinned ASAP7 manifest
   (`hw/syn/libs/asap7/manifest.txt`) has only **LVT and RVT** (TT/FF/SS). Drop SLVT or extend
   `prepare.py` / the manifest.
2. **ASAP7 not installed.** `hw/syn/libs/asap7/` contains only `install.sh.in`, `prepare.py`,
   `manifest.txt`. Install before step 3. The ICG cell name/pins (`ICGx1_ASAP7_75t_R`) can only be
   verified after that. Nangate `CLKGATETST_X1` exists in `NangateOpenCellLibrary_typical.lib`
   (pins not yet checked).
3. **Prologue description (§3.2).** "reads the real kernel address from `VX_CSR_CTA_ENTRY` and
   `jalr`s to it" doesn't match `vx_start.S`, whose header describes per-kernel entry stubs
   (`lla s11, <kernel>; j __vx_cta_entry`) dispatching via `s11` (an inline comment there also
   mentions `VX_CSR_CTA_ENTRY`; the file is internally inconsistent). The conclusion, key on
   `dcr_entry` / `VX_CSR_CTA_ENTRY`, not the warp start PC, is still correct.
4. **No `alu` unit testbench.** §2 lists `hw/unittest/alu`; it doesn't exist. Available targets:
   `issue`, `fma_unit`, `tcu_unit`, `mufu_unit`, `mem_unit`, `core`, among others.
5. **Exclude ALU and LSU from PG.** (Decided in concern 6a.) They are almost never idle long enough; excluding them
   simplifies step 6 (keep them in CG).
6. **"SFU" in the unit list** should become MUFU (and optional tex/rtu); see concern 6.

**Decision (2026-11-25): recommended approach.**
1. Drop SLVT. Report ASAP7 **LVT** as the main corner and **RVT** as the leakage sensitivity point.
2. Install ASAP7 (`hw/syn/libs/asap7/`, downloads from OpenROAD-flow-scripts) at the start of
   step 3, not now; verify the ASAP7 ICG cell name/pins and Nangate `CLKGATETST_X1` pins then.
3. Reword §3.2 to: "All kernels share the `__vx_cta_entry` prologue, so the warp start PC does not
   identify the kernel; key on the kernel entry PC (`dcr_entry` → `entry_r` → `VX_CSR_CTA_ENTRY`)." Do not describe the
   dispatch mechanism.
4. Replace the testbench list in §2 with the ones relevant here: `issue` (dispatch / demand
   detection), `fma_unit` (FPU), `tcu_unit`, `mufu_unit`, `core`.
5. Decided in concern 6a.
6. Follows from concern 6a: list MUFU (not SFU) as the gated unit.

---

## 8. Baseline for clock-gating savings (tool-inserted CG vs own ICGs)

### Question raised
To show a higher saving, disable Yosys's automatic clock-gating insertion and rely only on the
project's own unit-level ICGs, comparing against an ungated baseline.

### Analysis
- Inserting our own ICGs is already the design (B2, P1); the question is only which baseline the
  savings are measured against.
- Every production ASIC flow inserts register-level CG automatically (Design Compiler, Genus,
  Yosys `clockgate`). Without a tool-CG baseline, a reviewer cannot tell how much of the reported
  dynamic saving the tool would have given for free.
- Tool CG and unit-level ICGs overlap: tool CG already stops clocks to flops whose enables are off.
  Our CG contribution is the remainder (clock tree above those flops, coarser gating).
- PG leakage savings (the novel part) are unaffected by tool CG, since tool CG saves no leakage.

**Decision (2026-11-25): synthesize two stacks from the same RTL and report both.**

| Stack | Yosys `clockgate` | Configs |
|---|---|---|
| No tool CG | off | B0 → B2′ → P1′ (+ B3′, B4-UB′, O′ as needed) |
| With tool CG | on (after `dfflibmap`) | B1 → B2 → P1 (+ B3, B4-UB, O) |

- **Headline:** P1′ vs B0, total savings from the scheme without tool CG.
- **Credibility:** P1 vs B1, savings that remain on top of a standard tool-CG flow.
- **Breakdown:** report dynamic (CG) and leakage (PG) savings separately in both stacks; leakage
  savings should be nearly identical across stacks.
- Cost: one extra synthesis setting per config, no extra RTL.
- Doc changes: §4.1 config table gets the two-stack structure (primed configs = no tool CG);
  §4.3 states that both stacks are synthesized; step 9 synthesizes every config in both stacks.
