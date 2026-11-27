# Step 5 — Unit-level clock gating (B2)

Branch `mix_gating_cg_pg`. **First RTL change of the project.** Adds reactive clock gating of the
execution units: each unit's clock stops while it has nothing dispatched to it and nothing in
flight, and restarts in the same cycle an instruction is dispatched. Off by default.

## 1. Goals (task order, step 5)

- Behavioral ICG + per-unit enable plumbing, default off.
- Reactive CG controller (B2) with a drain check at instruction granularity and an always-on
  demand detector (concerns 6b, 6d).
- Regression with gating off and on.

## 2. Design

### 2.1 Clock-gate cell
Reuses upstream's `hw/rtl/libs/VX_clockgate.sv`: a latch transparent while `clk` is low, ANDed
with `clk` (glitch-free; the standard latch-based ICG). Upstream only uses it in a commented-out
core-level gate in `VX_socket.sv`.

### 2.2 Controller `hw/rtl/core/VX_unit_cg.sv` (new)
One per gated unit, on the free clock:

```
enable = reset ∨ (any dispatch valid) ∨ (in-flight ≠ 0) ∨ busy_in
in-flight += dispatch fires with sop;  in-flight −= commit fires with eop
```

- **Drain at instruction granularity (6b):** in-flight counts instructions, not packets, so
  gaps between the packets/micro-ops of one instruction never stop the clock early. (For CG
  this is conservative; it matters for PG in step 6.)
- **Demand wake (6d), 0 cycles:** the enable is combinational in the dispatch `valid`;
  `VX_clockgate` samples it while `clk` is low, so the edge that captures the new dispatch is
  passed. Gating therefore cannot change cycle counts, and this is the main correctness check.
- `busy_in`: unit-specific pending work. Only the LSU uses it (`lsu_busy` = scheduler or store
  path not empty), since its memory requests and stores outlive the commit.
- Simulation-only (`SIMULATION`): gated cycles, total cycles, wake events, reported at end of
  simulation as `CG: <instance> gated=… total=… wakes=…`; an assertion flags in-flight underflow.
- With `VX_CFG_UNIT_CG_ENABLE` undefined the module is `assign clk_out = clk`.

### 2.3 Where it is wired
Vortex has **two back-end structures** (`VX_gpu_pkg.sv`, `BACKEND_SLICES`):

| Structure | When | Execute module | Gated |
|---|---|---|---|
| Sliced | every unit has one block per issue slice (all configs used so far except `flash_attn_tcu_wg`) | `VX_execute_slice` → `VX_{alu,lsu,fpu,tcu}_block` | each **block**; lane dispatch/gather adapters stay on the free clock (an instruction buffered there already counts as in flight) |
| Stage-sliced | otherwise (e.g. `flash_attn_tcu_wg`: 4 issue slices, 1 ALU/LSU/FPU block) | `VX_execute` → `VX_{alu,lsu,fpu,tcu}_unit` | each **unit** |

The TFU is gated inside `VX_sfu_block` (shared by both structures) on its processing-element
handshake: one result per accepted execute packet.

| Unit | Gated in step 5 | Extra busy |
|---|---|---|
| ALU | yes | – |
| LSU | yes | `lsu_busy` |
| FPU | yes | – |
| TCU | yes (whole unit/block; per-block gating for 4-slice configs comes with PG) | – |
| TFU | yes | – |
| SFU control (warp control, CSR, `csr_fpu`, CTA context RAM) | **no** | – |

**Deviation from concern 6a:** SFU control was planned as CG-only, but it receives CTA broadcast,
CSR and warp-control inputs that arrive independently of dispatch, so a dispatch-based drain check
is not safe for it. It stays ungated; recorded in the concerns doc.

## 3. Files changed

| File | Change |
|---|---|
| `VX_config.toml` | new `[gating]` section: `VX_CFG_UNIT_CG_ENABLE = false` |
| `hw/rtl/core/VX_unit_cg.sv` | new controller |
| `hw/rtl/core/VX_execute_slice.sv` | per-block gated clocks (ALU, LSU, FPU, TCU) |
| `hw/rtl/core/VX_execute.sv` | per-unit gated clocks (ALU, LSU, FPU, TCU) |
| `hw/rtl/core/VX_sfu_block.sv` | gated TFU clock |
| `sim/rtlsim/processor.cpp` | calls `device_->final()` before teardown so `final` blocks run (the RTL had none before) |

Enable with `CONFIGS="... -DVX_CFG_UNIT_CG_ENABLE"`. Regression runner: `cs752_tools/regress_cg.sh <build> <case file> <out>`
(case files `cs752_tools/cases/xlen{32,64}.txt` = off, `step5_cgon_xlen{32,64}.txt` = on). Re-run `../configure` after pulling (the toml
changed).

## 4. Verification

### 4.1 Regression, gating off vs on (rtlsim, `--perf=1`)
18 tests: the 10 step-1 tests (both builds, both back-end structures, every gated unit including
TCU with WGMMA and TFU) plus `dogfood` (25 ISA sub-tests), `diverge`, `fence`, `printf`, `conv3`,
`sort`, `dotproduct`, `stencil3d`.

**All pass in both modes, and every cycle count is identical with gating off and on.** Gating off
also reproduces the step-1 baseline exactly.

### 4.2 Gated cycles (gating on; % of cycles the unit's clock was stopped)
| Test | ALU | LSU | FPU | TCU | TFU |
|---|---|---|---|---|---|
| vecadd | 79% | 63% | 95% | | |
| sgemm | 61% | 27% | 47% | | |
| mixfu | 35% | 99% | 10% | | |
| multikernel | 54% | 46% | 100% | | |
| tfu | 53% | 80% | 96% | | 97% |
| sgemm_tcu | 57% | 16% | 95% | 47% | |
| sgemm_tcu_wg | 33% | 80% | 99% | 97% | |
| flash_attn (XLEN 64) | 51% | 62% | 81% | | |
| flash_attn_tcu_wg (XLEN 64, stage-sliced) | 32% | 55% | 75% | 98% | |

Totals include the cycles before the first launch (the counters start at reset).

### 4.3 Cross-check against the step-2 trace analysis
The RTL counters and the independent trace analysis agree:

| Check | Step 2 (trace) | Step 5 (RTL) |
|---|---|---|
| `sgemm` FPU: idle windows vs wakes | 10,437 | 10,436 |
| `sgemm` FPU: busy vs gated | 52.9% busy | 47.2% gated |
| `sgemm` ALU | 39.0% busy | 61.0% gated |
| `flash_attn_tcu_wg` TCU | ~1.5% busy | 98.2% gated |

### 4.4 Synthesis sanity check (Yosys, LVT, gating on)
- Elaborates and maps. Each ICG becomes a `DLLx1_ASAP7_75t_L` latch plus an AND gate (5 latches),
  **not** the library `ICGx1_ASAP7_75t_L` cell.
- Area of the 5 controllers + ICGs: +48 µm² (+0.12% of the core).
- Vectorless power drops 650 → 237 mW, but this is **not a valid saving**: OpenSTA's default
  activity does not propagate clock activity through the latch/AND, so flops behind the gate get
  data-net activity instead of clock activity. Real CG savings need gate-level SAIF (step 8).

## 5. Limitations and follow-ups
- **ICG mapping:** step 8 should map `VX_clockgate` to the library ICG cell (ASIC builds) so
  synthesis, timing (clock-gating checks) and power treat it as a clock gate.
- **SFU control** is not gated (see §2.3).
- **TCU in 4-slice configs** is gated as one unit; per-block gating comes with PG (step 6).
- Counters are simulation-only; hardware perf counters (MPM) come with PG in step 6.
- Coverage is the 18-test set above, not the full `tests/regression` suite.

## 6. Next: step 6
Behavioral power gating on the agreed domains (TCU, FPU, TFU): clock forced off in PG and WAKING,
isolation, `WAKE_LAT` counter, scramble on PG entry and reset on wake; per-core epoch detection;
timeout PG (B3).
