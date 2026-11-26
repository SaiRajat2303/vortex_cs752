# Step 1 — Baseline on unmodified RTL

Part of the CS/ECE 752 hybrid clock + power gating project (branch `mix_gating_cg_pg`).
Step 1 makes no RTL or source changes. It establishes that the toolchain, build and rtlsim flow
work on this machine, records a pass/fail and cycle-count reference for later steps, and fixes
the evaluation configuration.

- Base commit: `2221ee286` (upstream `vortexgpgpu/vortex` master)
- Date: 2026-11-26

## 1. Goals

1. **Setup works.** The host is Ubuntu 26.04 / GCC 15 / CMake 4 with no root access, newer than
   what Vortex supports, so the build needs a few compatibility settings.
2. **Reference for later steps.** Pass/fail and cycle counts on unmodified RTL are the reference
   for correctness (every gated build must pass the same tests) and for performance overhead
   (cycles added by wake-up stalls).
3. **Evaluation configuration and workloads.** Choose the build used for all experiments, check
   which units each kernel exercises, and check what the existing perf counters report.

## 2. Environment

### Toolchain
This commit pins `TOOLCHAIN_REV=v3.0.3` (`VERSION`). The prebuilt toolchain was installed into a
dedicated directory, separate from any older toolchain, because `toolchain_install.sh` replaces
each component in place:

```bash
# from a configured build dir; components needed for rtlsim + Yosys/OpenSTA, 32- and 64-bit
TOOLDIR=/nobackup/<user>/tools_v303
./ci/toolchain_install.sh --verilator --llvm --libcrt32 --libc32 --riscv32 \
                          --circt --yosys --sta
./ci/toolchain_install.sh --riscv64 --libcrt64 --libc64
```

Installed: Verilator 5.050, Yosys 0.69, OpenSTA, CIRCT, llvm-vortex, RISC-V GNU toolchains and
libc/libcrt (32 and 64 bit). The `ERROR 404` lines in the install log are expected: the installer
requests archive parts until one does not exist.

### Environment script (sourced before every build or run)
```bash
export VORTEX_TOOLDIR=/nobackup/<user>/tools_v303
export PATH=$VORTEX_TOOLDIR/verilator/bin:$VORTEX_TOOLDIR/yosys/bin:$VORTEX_TOOLDIR/sta/bin:$PATH
export OBJCACHE=                              # prebuilt Verilator calls ccache, not installed
export CMAKE_POLICY_VERSION_MINIMUM=3.5       # Ramulator's yaml-cpp vs CMake 4
# GCC 15 warnings the repo's -Werror builds were never tested against (host g++ only)
export CXXFLAGS="-Wno-error=dangling-reference -Wno-error=maybe-uninitialized \
                 -Wno-error=pedantic -Wno-error=format${CXXFLAGS:+ $CXXFLAGS}"
```

## 3. Build

```bash
git submodule update --init --recursive

# 32-bit build (most tests)
mkdir -p build && cd build
../configure --xlen=32 --tooldir=$VORTEX_TOOLDIR --osversion=ubuntu/focal
make -C ../third_party          # serially first: parallel builds race in third_party
make -s -j16

# 64-bit build (attention tests; see §4)
mkdir -p ../build64 && cd ../build64
../configure --xlen=64 --tooldir=$VORTEX_TOOLDIR --osversion=ubuntu/focal
make -s -j8
```

- `--osversion=ubuntu/focal`: Ubuntu 26.04 is not a supported prebuilt target; the focal
  prebuilts work.
- Both builds stop with `make: *** [Makefile:19: tests] Error 2` caused only by
  `tests/opencl/*` (`CL/opencl.h` / `CL/cl.h` not found; no OpenCL headers or POCL installed).
  This is expected and does not affect rtlsim or the regression tests.

## 4. Evaluation configuration

| Setting | Value | Why |
|---|---|---|
| Topology | 1 cluster, 1 core, 4 warps, 4 threads (default) | Small enough for gate-level simulation later |
| FPU | `-DVX_CFG_FPU_TYPE_STD` | rtlsim defaults to the DPI (C-model) FPU; ASIC synthesis uses STD. Traces and power must come from the same RTL |
| TCU | default `VX_CFG_TCU_TYPE = "TFR"` | Synthesizable. CI's TCU rtlsim tests use `TCU_TYPE_DPI`, which we avoid for the same reason |
| TFU / TCU / WGMMA | enabled per test (`EXT_TFU_ENABLE`, `EXT_TCU_ENABLE`, `TCU_WGMMA_ENABLE`) | Off by default |
| XLEN | 32, except attention tests at 64 | CI runs `flash_attn*` on rtlsim only at XLEN=64: on RV32 the FP conversions are software, inflating instruction counts 4–8× |

Notes:
- `flash_attn_tcu_wg` pins `VX_CFG_ISSUE_WIDTH=4` with 4 warps, so `VX_CFG_NUM_TCU_BLOCKS` = 4
  (one TCU block per issue slice). ALU/LSU/FPU are pinned to 1 block as in CI.
- Per `AGENTS.md`, each app is rebuilt with the same `CONFIGS` that `blackbox.sh` uses.

## 5. How the tests were run

For each case: clean the app, rebuild it with the case's `CONFIGS`, then run on rtlsim with the
core perf counters.

```bash
make -C tests/regression/$app clean
CONFIGS="$cfg" make -C tests/regression/$app
CONFIGS="$cfg" ./ci/blackbox.sh --driver=rtlsim --app=$app --perf=1 --args="$args"
```

Cases (`id|app|CONFIGS|args`):
```
# build (XLEN=32)
vecadd|vecadd|-DVX_CFG_FPU_TYPE_STD|
sgemm|sgemm|-DVX_CFG_FPU_TYPE_STD|
relu|relu|-DVX_CFG_FPU_TYPE_STD|
multikernel|multikernel|-DVX_CFG_FPU_TYPE_STD|
mixfu|mixfu|-DVX_CFG_FPU_TYPE_STD|
tfu|tfu|-DVX_CFG_FPU_TYPE_STD -DVX_CFG_EXT_TFU_ENABLE|
sgemm_tcu|sgemm_tcu|-DVX_CFG_FPU_TYPE_STD -DVX_CFG_EXT_TCU_ENABLE -DITYPE=fp16 -DOTYPE=fp32|
sgemm_tcu_wg|sgemm_tcu_wg|-DVX_CFG_FPU_TYPE_STD -DVX_CFG_NUM_THREADS=8 -DVX_CFG_NUM_WARPS=8 -DVX_CFG_ISSUE_WIDTH=1 -DVX_CFG_EXT_TCU_ENABLE -DVX_CFG_TCU_WGMMA_ENABLE -DITYPE=fp16 -DOTYPE=fp32 -DWGMMA_NRC=8|
# build64 (XLEN=64)
flash_attn64|flash_attn|-DVX_CFG_FPU_TYPE_STD|-N16 -D8
flash_attn_tcu_wg64|flash_attn_tcu_wg|-DVX_CFG_FPU_TYPE_STD -DVX_CFG_NUM_ALU_BLOCKS=1 -DVX_CFG_NUM_LSU_BLOCKS=1 -DVX_CFG_NUM_FPU_BLOCKS=1|-N 32 -D 16 -c 8 -t 8
```

For `flash_attn*`, `-c` and `-t` are tile sizes (`flash_attn/flash_attn.h`), not core counts, so
CI's arguments are used unchanged on one core.

## 6. Results

All 10 tests pass on unmodified RTL.

| Test | XLEN | Config notes | Instrs | Cycles | IPC | Mix ALU/LSU/SFU/FPU/TCU % |
|---|---|---|---|---|---|---|
| vecadd | 32 | base | 448 | 1,465 | 0.31 | 38/25/33/4/– |
| sgemm | 32 | base | 334,864 | 787,625 | 0.43 | |
| relu | 32 | base | 512 | 1,700 | 0.30 | |
| multikernel | 32 | 3 kernels in one binary | 15,968 | 44,429 | 0.36 | |
| mixfu | 32 | base | 125,008 | 301,129 | 0.42 | |
| tfu | 32 | + TFU | 847,088 | 2,636,384 | 0.32 | 62/13/22/3/– |
| sgemm_tcu | 32 | + TCU, fp16→fp32 | 46,864 | 80,859 | 0.58 | 31/30/2/2/35 |
| sgemm_tcu_wg | 32 | + TCU WGMMA, 8 warps × 8 threads | 3,916 | 9,500 | 0.41 | 85/9/2/1/3 |
| flash_attn | 64 | `-N16 -D8` | 72,163 | 203,591 | 0.35 | 60/20/10/10/– |
| flash_attn_tcu_wg | 64 | 4 TCU blocks, tf32 | 125,985 | 360,406 | 0.35 | 64/18/5/9/3 |

Blank mix cells were not extracted; the values are in the run logs. Numerical checks:
`flash_attn` worst absolute error 1.19e-07, `flash_attn_tcu_wg` 5.29e-05 (tolerance 1e-3).

## 7. Findings

1. **The existing perf counters are not enough for gating analysis.** `--perf=1` reports only the
   per-unit share of issued instructions and stall percentages. There are no per-unit busy
   cycles or idle-window lengths, so step 2 needs a per-unit activity tracer.
2. **The FPU is lightly used in every kernel** (1–10% of instructions), making it a consistent
   power-gating candidate.
3. **The TCU is ~3% of instructions under WGMMA.** Each WGMMA instruction occupies the unit for
   many cycles, so the TCU's actual idle time must be traced, not inferred from instruction counts.
4. `sgemm_tcu_wg` prints two perf blocks, probably one per kernel launch; to confirm in step 2.
5. Runs are short (≤ 2.6M cycles), so per-cycle traces in step 2 are cheap to collect.

## 8. Next: step 2

A simulation-only per-unit activity tracer behind a define (normal builds unchanged). Per launch
epoch it records idle-run histograms per unit, issue→dispatch lead time, prologue length and
per-unit first-use delay.
