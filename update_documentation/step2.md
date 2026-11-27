# Step 2 — Per-unit activity traces and idle-window measurements

Branch `mix_gating_cg_pg`. Builds on step 1 (`step1.md`): same builds, configs and 10 tests.
**No RTL changes.** Step 2 adds analysis tooling under `cs752_tools/` and the measured results
in `step2_results.md`.

## 1. Goals

From the task order (step 2):
- Per-unit busy/idle activity per launch epoch, and idle-window length histograms.
- Issue→dispatch lead time per unit (how early a demand detector can see an instruction coming).
- Prologue length (launch → first kernel-body instruction) and per-unit first-use delay.
- Check whether two kernels ever share a core at the same time (open check from concerns 3d).

## 2. Approach: reuse the rtlsim pipeline trace instead of new RTL

Vortex's debug trace already records every event needed, with a cycle stamp and a per-instruction
`uuid`:

| Trace event (level 1) | Source | Used for |
|---|---|---|
| `decode` | `core/VX_decode.sv` (`decodeN` when `FETCH_WIDTH > 1`) | lead time, kernel-body detection |
| `ibuffer-uop decode` (`parent=#uuid`) | `core/VX_uop_sequencer.sv` | grouping micro-ops into their instruction |
| `dispatcher dispatch` (`ex`, `op`, `sid`, `sop/eop`) | `core/VX_dispatcher.sv` | start of a unit's busy interval |
| `commit` (`ex`, `sop/eop`) | `core/VX_commit_slice.sv` | end of a unit's busy interval |
| `kmu start`, `cta-fire` | `VX_kmu.sv` | launch start, last CTA sent |
| `cta_dispatch dispatch` / `warp-done` | `core/VX_cta_dispatch.sv` | warp launch/finish |

Tracing does not perturb timing: cycle counts with the trace equal the step-1 counts exactly.

### Build issue worked around
`--debug=1` does not build: the generated config already defines `VX_DBG_DEBUG_LEVEL=3` and
`--debug=1` adds `-DVX_DBG_DEBUG_LEVEL=1`; with `-Werror` the redefinition is fatal. CI only uses
`--debug=3`. Instead of patching upstream build files, runs use `--debug=3` and the log is filtered
on the fly (cache events at levels 2–3 dominate the raw log).

### Timestamps
rtlsim calls `eval()` twice per clock (`sim/rtlsim/processor.cpp`, `++timestamp` per eval), so trace
timestamps are half-cycles; the analyzer uses `cycle = timestamp / 2`. Check: the launch epochs of
every test sum to the perf cycle count within 6 cycles.

## 3. Tools added (`cs752_tools/`)

| File | What it does |
|---|---|
| `trace_run.sh` | Rebuilds an app with `$CONFIGS`, runs it on rtlsim with `--perf=1 --debug=3`, and writes the log through a named pipe filtered to launch/decode/dispatch/commit events with operand data stripped (~5× smaller). |
| `unit_activity.py` | Rebuilds per-unit busy intervals and idle windows per launch epoch; writes `<id>.json` (summary) and `<id>_windows.csv` (every idle window: test, launch, kernel, unit, block, view, start, length, kind). |
| `run_cases.sh` | Traces and analyzes every case in a case file. |
| `summarize.py` | Produces the markdown tables in `step2_results.md`. |
| `cases/xlen32.txt`, `cases/xlen64.txt` | The step-1 test list (`id|app|CONFIGS|args`). |

### Definitions used by `unit_activity.py`
- **Busy:** a unit is busy for an instruction from its first dispatch packet to its last commit
  packet (`eop=1`), i.e. while the instruction is in flight in the unit. This is exactly the
  "not drained" condition the gating controller will use.
- **Two idle views:**
  - `cg`: gaps between in-flight micro-ops (clock gating may use these).
  - `pg`: micro-ops with the same parent instruction form one span, so windows never split a
    partially issued uop sequence (concern 6b: power gating is not allowed there).
- **Launch epoch** (concern 5a): from `kmu start` to the later of the last CTA sent, the last
  warp finishing, and the last commit before the next launch.
- **Window kinds:** `lead` (epoch start → first use = first-use delay), `mid`, `tail`.
- **Kernel name / prologue:** the first decode at a `__vx_kentry_<name>` address (from
  `kernel.elf` via `llvm-nm`) marks the kernel body; prologue = warp launch → that decode.
- **TFU** is separated from the SFU by op name (`TFU.*`). The TCU is reported per issue slice
  (one block per slice).

## 4. How to reproduce

```bash
source <env script from step1.md>
cs752_tools/run_cases.sh build   cs752_tools/cases/xlen32.txt <out32>
cs752_tools/run_cases.sh build64 cs752_tools/cases/xlen64.txt <out64>
cs752_tools/summarize.py <out32> <out64> > summary.md
```
Traces total ~1.3 GB and stay outside the repo; the JSON/CSV outputs are regenerated from them.
Wall time ≈ 40 min for all 10 tests (the two long ones: `tfu` 14 min, `flash_attn_tcu_wg` 13 min).

## 5. Results

Full tables: `step2_results.md`. All 10 tests pass; every dispatched instruction is matched to a
commit (dispatch counts equal the perf instruction counts).

### 5.1 Launches, prologue, lead time
| Test | Launches | Kernels | Prologue median (cycles) | Lead decode→dispatch min |
|---|---|---|---|---|
| vecadd, relu, sgemm, mixfu, flash_attn | 1 | kernel_main | 43 | 7 |
| multikernel | 3 | add_k, mul_k, acc_k | 43 | 7 |
| tfu | 15 | kernel_main | 43 | 7 |
| sgemm_tcu | 1 | kernel_main | 44 | 7 (TCU 6) |
| sgemm_tcu_wg (8 warps) | 1 | kernel_main | 115 | 7 (TCU 6) |
| flash_attn_tcu_wg (4 slices) | 1 | kernel_main | 148 | 7 (TCU 6) |

No test ever has two launches active on a core at the same time.

### 5.2 Power-gating candidates: share of epoch time in long idle windows (pg view)
| Test | Unit | Util | idle ≥ 256 | idle ≥ 1024 | Longest window |
|---|---|---|---|---|---|
| flash_attn_tcu_wg | TCU (each of 4 blocks) | 1.3–1.8% | 97% | 97% | ~32,700 |
| sgemm_tcu_wg | TCU | 3.3% | 97% | 93% | 4,767 |
| sgemm_tcu | TCU | 53.0% | 32% | 0% | 887 |
| sgemm_tcu | FPU | 4.8% | 91% | 91% | 2,631 |
| sgemm_tcu_wg | FPU | 1.0% | 96% | 96% | 9,158 |
| tfu | FPU | 4.4% | 94% | 43% | 168,890 |
| flash_attn_tcu_wg | FPU | 24.3% | 41% | 31% | 6,511 |
| flash_attn | FPU | 19.4% | 26% | 4% | 1,939 |
| sgemm | FPU | 52.9% | 9% | 0% | 344 |
| mixfu | FPU | 89.8% | 3% | 0% | 599 |
| tfu | TFU | 3.9% | 96% | 0% | 1,014 |
| multikernel | FPU | never dispatched | 100% | 100% | – |

### 5.3 Units that stay clock-gating only
- **ALU:** 26–70% busy; never more than ~2% of time in windows ≥ 64 cycles (longest 266).
- **LSU:** idle windows mostly < 256 cycles, except `mixfu` (LSU barely used).
- **SFU:** often idle for long stretches (e.g. `sgemm` 94% in windows ≥ 1024), but it holds CSR and
  warp-control state, so per concern 6a it is clock-gated only; only its TFU sub-unit is a PG domain.

## 6. Findings

1. **Lead time is 7 cycles** (6 for the TCU) from decode to dispatch, minimum and median, for all
   units. A demand detector at decode hides any wake-up of ≤ 6–7 cycles: the 1-cycle CG wake is
   always hidden; for PG, `WAKE_LAT` beyond ~6 cycles stalls unless the unit is pre-woken.
2. **The prologue is 43 cycles** per warp in the default config (115–148 with 8 warps or 4 issue
   slices). Launch-time pre-wake (concern 5c) fully hides `WAKE_LAT` ≤ ~43 cycles at kernel start.
3. **Launches never overlap on a core** in any test, including `multikernel` (3 kernels) and
   `tfu` (15 launches). Concern 3d's concurrent-kernel case does not arise in these workloads.
4. **The TCU under WGMMA is the strongest PG target:** busy < 4% of the time, with ~97% of each
   launch in idle windows ≥ 1024 cycles. Without WGMMA (`sgemm_tcu`) it is 53% busy and its idle
   windows are < 900 cycles, so PG pays off only for a small BET.
5. **The FPU's behaviour depends strongly on the kernel:** near-idle in TCU and TFU kernels (91–96%
   of time in windows ≥ 256), but busy in FP-heavy kernels (`sgemm` 53%, `mixfu` 90%), and in
   between for attention (`flash_attn`: 26% in ≥ 256 but only 4% in ≥ 1024). One global
   timeout cannot fit all of these, which supports the per-kernel learned timeout (concern 1,
   option B).
6. **TFU idle windows sit just below 1024 cycles** (96% of time in windows ≥ 256, 0% ≥ 1024,
   longest 1,014): whether PG pays off depends directly on BET, a key sensitivity point for step 4.
7. **Not splitting uop sequences costs almost nothing:** the `pg` and `cg` views differ by at most
   one window (WGMMA micro-ops issue back-to-back), so the concern-6b rule does not reduce PG
   opportunity.
8. **History-key correction:** every kernel launches at the same start PC (`_start`, 0x80000000).
   The per-kernel address is `VX_DCR_KMU_KERNEL_ENTRY` → `dcr_entry` → `entry_r` in
   `core/VX_cta_dispatch.sv` (→ `VX_CSR_CTA_ENTRY`). The history table must be keyed on `entry_r`,
   not `cta_PC`. (Design notes updated accordingly.)

## 7. Limitations

- Busy ends at commit, so it includes any wait between a unit's result and the commit stage;
  utilization is therefore a slight over-estimate (conservative for gating).
- Lead time is measured from decode; there is no trace point at the scoreboard/issue stage.
- Workloads are small (≤ 2.6 M cycles); `multikernel` and `tfu` are the only multi-launch tests.

## 8. Next: step 3

Per-unit synthesis (Yosys + OpenSTA, ASAP7 LVT, RVT as sensitivity) for dynamic and leakage power
per unit / PG domain, then BET for a sweep of transition energies. Install ASAP7 first and verify the
ICG cell and pin names.
