# Why 170HX-XRAY exists

170HX-XRAY is not a renamed copy of one public unlock utility. It combines
card-bound acquisition, complete physical images, multiple logical planes,
offline planning, transport conversion, independent replay, diagnostics and
fail-closed execution gates. Public projects remain indispensable sources of
ideas and field evidence, but each solves a narrower problem.

This comparison is deliberately evidence-oriented. “Covered” means XRAY has an
equivalent or stricter way to represent and verify the useful behavior. It does
not mean that every proposed fuse semantic is proven, or that an XRAY candidate
plan is permission to program OTP.

## Pinned upstream reconciliation (2026-11-27 through 2026-11-28)

The comparisons below are bound to exact commits. A project date or a passing
offline suite does not promote an electrically irreversible recipe by itself.
The detailed disposition of every material delta is recorded in
[UPSTREAM_RECONCILIATION_2026-11-27.md](UPSTREAM_RECONCILIATION_2026-11-27.md).

| Project | Audited commit | Commit date | Scope of the current delta |
|---|---|---|---|
| pico-ga100vfi-console | [e9e9a33c8b3515563959f7239a0df2a1a3b6eef3](https://github.com/portz3r0/pico-ga100vfi-console/tree/e9e9a33c8b3515563959f7239a0df2a1a3b6eef3) | 2026-11-28 +11:00 | Two-step FF transaction, paired A100 identity queue, VBIOS workflow and additional readouts; no commit exists after this requested baseline |
| cmp170hx-ffcalc | [e28e03a40ade83079f518df7416072daea047bf9](https://github.com/sayhellotojungle/cmp170hx-ffcalc/tree/e28e03a40ade83079f518df7416072daea047bf9) | 2026-11-27 +08:00 | Expanded status/readout map and write-window gates, plus an explicitly untested SW-IFF path |
| cmp170hx-8gb-vfi-tools | [f422f80a716fff2c1dc112abcf2e9d43a04b6614](https://github.com/darkmaniac7/cmp170hx-8gb-vfi-tools/tree/f422f80a716fff2c1dc112abcf2e9d43a04b6614) | 2026-11-27 -06:00 | Consolidated fuse reference, corrected PCIe isolation and additional review-only community hypotheses |
| esp32-ga100vfi | [37d14e9698e5926ca1b1f7e86b61d340f93414a2](https://github.com/sayhellotojungle/esp32-ga100vfi/tree/37d14e9698e5926ca1b1f7e86b61d340f93414a2) | 2026-11-27 +08:00 | Operational workflow and post-cold checks; no new physical fuse coordinates |
| cmpunlocker | [8b6c556fedaa0150a6fc40c74297b20cb5e508a6](https://github.com/sayhellotojungle/cmpunlocker/tree/8b6c556fedaa0150a6fc40c74297b20cb5e508a6) | 2026-11-26 +08:00 | Experimental HBM idle governor registers and state transitions; no fuse-plan delta |

### Offline suite results at those pins

Offline tests are useful for parser and planner regressions; they are not
hardware qualification. The 2026-11-28 source-only rerun produced these exact
results without opening a GPU or invoking a glitch/program path:

| Project | Current-pin result | What it establishes—and does not establish |
|---|---|---|
| `cmp170hx-ffcalc@e28e03a` | 101/101 pass on CPython 3.14.8 and 3.15.2 | Its own offline examples are internally reproducible. They do not cure the fail-open/missing-block, ordering, boundary or physical-validation gaps below. |
| `cmp170hx-8gb-vfi-tools@f422f80` | Refuses current ffcalc `e28e03a` with exit 3; 487/487 checks and 10/10 mutants pass only against pinned ffcalc `4e089b5` | The advertised parity suite is stale relative to current ffcalc and cannot establish current cross-tool agreement. |
| `ga100-ff-tools@594e83d` | 7/7 pass on CPython 3.14.8; CPython 3.15.2 fails during import because `SourceFileLoader.load_module()` was removed, so zero real tests run | The supported-runtime suite passes narrowly; it is not Python-3.15 compatible and does not validate physical programming. |
| `esp32-ga100vfi@37d14e9` | Clean-source wrapper fails on macOS BSD `sed -i`; direct CPython 3.14 run executes 287 tests with five errors and one skip because tracked tests require absent, untracked `temp/` fixtures | The clean-clone offline suite is not reproducible at this pin. Resource warnings also remain. |
| `pico-ga100vfi-console@e9e9a33` | 41/41 host tests pass on CPython 3.14.8 and 3.15.2; standalone dedupe, die-gate and Pico-flash self-checks also pass | This validates those host-side checks. It does not negate the timeout-as-absence, reserve, plane-gate, abort/re-entry, journal or VBIOS-binding defects documented below. |

All tested upstream worktrees remained clean. No result in this table promoted a
new fuse coordinate, execution profile, electrical waveform or OTP authority.

## Current `ga100-ff-tools` comparison

The current audited upstream revision is
[`594e83ddd336469aea303d466d0c1d2a1edb2d05`](https://github.com/kprinssu/ga100-ff-tools/tree/594e83ddd336469aea303d466d0c1d2a1edb2d05),
committed 2026-11-20. It adds useful safeguards, per-card FF planning, readback
checks, stress/benchmark code and clear warnings. It is not, however, a
superset of XRAY.

| Area | Current `ga100-ff-tools` behavior | XRAY behavior and advantage |
|---|---|---|
| Bad-controller pre-state | [The `ff_dump.py` contract says](https://github.com/kprinssu/ga100-ff-tools/blob/594e83ddd336469aea303d466d0c1d2a1edb2d05/planner/ff_dump.py#L16-L17) raw reads are refused unless the controller is idle and CRC is `0x55`, but [the implementation only records a warning and continues](https://github.com/kprinssu/ga100-ff-tools/blob/594e83ddd336469aea303d466d0c1d2a1edb2d05/planner/ff_dump.py#L183-L192). | Level 3 fails closed before the first controller transaction and records the exact entry, transaction and restored state. |
| Physical extent | [Main rows are hard-coded to `222..487`](https://github.com/kprinssu/ga100-ff-tools/blob/594e83ddd336469aea303d466d0c1d2a1edb2d05/planner/ff_dump.py#L30-L31) in the reader, planner and burner. | XRAY retains all 512 MAIN words and the complete eight-word GA100 RIR vector, derives specimen-local boundaries, represents implicit zeroes and refuses an ambiguous allocation interval. |
| Logical model | FF replay skips IFF records; RIR and variable `F/B/gap/S/E` boundaries are not represented. | XRAY preserves raw MAIN/RIR, FF and state-qualified IFF, repair/correction evidence, last-record-wins state and uncertainty separately. |
| Evidence binding | Plans bind a truncated textual hash of occupied rows `222..last`. | XRAY binds complete binary images, RIR, exact PCI identity, helper ABI/generation, register/context passes, decoder identity and canonical self-digests. |
| No-op record | [The optional mechanism test](https://github.com/kprinssu/ga100-ff-tools/blob/594e83ddd336469aea303d466d0c1d2a1edb2d05/planner/ffcard.py#L180-L182) consumes a fresh row while writing an unchanged logical value. | XRAY rejects no-op/canary OTP records. Simulation, dry-run and separately qualified hardware are not substitutes for finite OTP space. |
| Programming transaction | Arming is split across scripts; the burner performs a direct single-row command and immediate readback. | The experimental backend requires a sealed whole-plan receipt, fresh same-session recapture, every-prefix replay, durable journal, poison/stop states, independent cold verification and exact profile allowlisting. Electrical qualification is still an explicit external blocker. |
| Plan coverage | SM/HBM, PCIe and the three-bit global NVLink-disable field are planned. Identity is intentionally forbidden; structured IFF, ECC, MIG/SYS_PIPE, media and per-link NVLink are absent. | XRAY represents HBM/SM, PCIe, identity/VBIOS routes, SS0, SS1/IMLA4, FP64/DP, ECC, MIG/SYS_PIPE, NVDEC, paired NVJPG/OFA, global and per-link NVLink boundaries, plus unsupported NVENC and DSOV hypotheses. Evidence-only entries remain non-executable. |
| NVLink safety | The three C1 bits at 368–370 are treated as the NVLink plan. | XRAY distinguishes those three global IOCTRL-group disables from the independent 12-bit `OPT_PERLINK_DISABLE` plane. It keeps the global plan outside execution until exact-card per-link state proves that no per-link edit is needed (or its physical map is recovered) and the board routing/power/ROM path is qualified. |
| Benchmarks | Useful HBM/PCIe/CUDA/tensor tests and feature probes are included. | XRAY separates capability queries from functional tests; covers typed integer/storage traffic, ArchBench, P2P, GDS, media, MIG/ECC inventory and thermal telemetry; and requires requested/enforced limit plus sampled minimum, maximum and mean power for every executed benchmark family. |
| Reports and formats | Primarily tool-specific JSON/text outputs. | XRAY produces a manifest-bound Markdown report, per-level artifacts, complete JSON/JSONL/text/binary transports and explicitly labeled public compatibility projections. |

The important conclusion is not that the public tool is “bad.” Its constrained
planner and benchmark are valuable. The contradictions and retained coverage
gaps above explain why XRAY does not import its behavior blindly.

## Fuse-by-fuse coverage audit

The second public baseline in this table is `cmp170hx-ffcalc` revision
`e28e03a40ade83079f518df7416072daea047bf9` (2026-11-27). It retains the
SYS_PIPE/media and identity options from the earlier snapshot and adds a much
broader read-only status map, stricter write-window observations and an
explicitly hardware-untested SW-IFF path. The `ga100-ff-tools` baseline remains revision
`594e83ddd336469aea303d466d0c1d2a1edb2d05` (2026-11-20). Dates matter, but a
newer claim still needs an exact-card proof. “Eligible” below means only that
XRAY's independent compiler/runtime policy can review a sealed candidate;
every candidate is still `executable=false` and externally gated.

| Frontend catalog field | `ga100-ff-tools` | `cmp170hx-ffcalc` current snapshot | XRAY model and boundary |
|---|---|---|---|
| `OPT_FBIO_DISABLE` | Per-card defect target | Mapped floorsweep target | 95%; normalized/direct defect; manifest-eligible memory prerequisite |
| `OPT_FBPA_DISABLE` | Per-card defect target | Mapped floorsweep target | 95%; normalized/direct defect; manifest-eligible memory prerequisite |
| `OPT_GPC_DISABLE` | Per-card defect target | Mapped floorsweep target | 97%; fixed-point parent/child closure; manifest-eligible activation last |
| `OPT_PES_DISABLE` | Per-card defect target | Mapped floorsweep target | 91%; 3/3/2 closure; manifest-eligible before GPC activation |
| `OPT_FBP_DISABLE` | Per-card defect target | Mapped floorsweep target | 98%; normalized/direct defect; manifest-eligible after FB prerequisites |
| `OPT_ROP_L2_DISABLE` | Per-card defect target | Mapped floorsweep target | 95%; explicit shared HBM/SM dependency; manifest-eligible |
| `OPT_NVLINK_DISABLE` | Plans C1 bits 368–370 as NVLink | Maps and may clear the same three bits | 94% as **global IOCTRL groups only**; offline comparison, rejected by compiler/runtime |
| `OPT_SYS_PIPE_DISABLE` | Preserved unchanged, not planned | Mapped and used by its MIG path | 94%; defect-gated experimental candidate; does not alone prove MIG |
| `OPT_PCIE_DEVIDA` | Explicitly forbidden | Optional A100 identity target | 98%; paired `0x20b5` candidate, identity-last, minimum two-operation commit |
| `OPT_PCIE_DEVIDB` | Explicitly forbidden | Optional A100 identity target | 96%; paired `0x20b5` candidate, identity-last, minimum two-operation commit |
| `OPT_DISABLE_GEN3_SPEED` | Raw block-39 `0x0782→0x0580` clears bits 1 and 9 together | Still treats bits 1/9 as unresolved and normally clears both | 99% at block 39 bit 9; the isolated cold experiment falsified bit 1, so XRAY preserves it |
| `PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE` | Raw block-47 recipe changes co-resident bits | Maps block 47 bit 3/direct bit 25 but also offers an unproved whole-word target | 97%; isolated bit only is eligible; all whole-word MAGIC_D policies remain offline |
| `OPT_PCIE_BOOT_GEN23_DISABLE` | Raw block-49 recipe clears the field | Mapped bit | 96%; isolated block-49 bit 6 is eligible |
| `OPT_PCIE_BOOT_GEN3_DISABLE` | Raw block-49 recipe clears the field | Mapped bit | 96%; isolated block-49 bit 7 is eligible |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT` | Absent; IFF is rejected by its replay | Absent | SS0 IFF review only: target/value 85%, portable placement 0%, non-executable |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT_1` | Absent; IFF is rejected by its replay | Absent | SS1/IMLA4 IFF review only: target/value 85%, portable placement 0%, row 487 quarantined |
| `OPT_TPC_GPC0_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC0 closure; manifest-eligible |
| `OPT_TPC_GPC1_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC1 closure; manifest-eligible |
| `OPT_TPC_GPC2_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC2 closure; manifest-eligible |
| `OPT_TPC_GPC3_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC3 closure; manifest-eligible |
| `OPT_TPC_GPC4_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC4 closure; manifest-eligible |
| `OPT_TPC_GPC5_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC5 closure; manifest-eligible |
| `OPT_TPC_GPC6_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC6 closure; manifest-eligible |
| `OPT_TPC_GPC7_DISABLE` | Per-card defect target | Mapped floorsweep target | 93%; fixed-point GPC7 closure; manifest-eligible |
| `OPT_PERLINK_DISABLE` | Absent; global bits are treated as the NVLink plan | Absent; global bits are treated as the NVLink plan | Direct width 12 but 0% physical coordinate; review-only and rejected |
| `OPT_ECC_EN` | Absent | Absent | Rows 6/7 bit 28 coordinate 99%, complete recipe 45%; conventional review only |
| `OPT_ECC_EN_CONTROL` | Absent | Absent | Rows 6/7 bit 29 coordinate 99%, control semantics incomplete; conventional review only |
| `OPT_SM_SPEED_SELECT_DP` | Absent | Absent | Rows 6/7 bit 26 and direct selector 99%; conventional FP64 review only |
| `OPT_SM_SPEED_SELECT_DP_CONTROL` | Absent | Absent | Rows 6/7 bit 27 name/coordinate known, proposed undo semantics 65%; review only |
| `OPT_SW_SMC_DISABLE` | Absent | Absent from the physical plan | Rows 14/15 bit 11 coordinate 99%, but MIG sufficiency unproved; frontend evidence only |
| `OPT_NVDEC_DISABLE` | Absent | Opt-in five-bit media target | 90%; direct-defect-gated experimental candidate; manifest-eligible only in media/MIG profile |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT146` | Absent | Opt-in member of the two-bit media pair | 88% unordered-set mapping, 0% individual attribution; inseparable experimental pair only |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT371` | Absent | Opt-in member of the two-bit media pair | 88% unordered-set mapping, 0% individual attribution; inseparable experimental pair only |
| `OPT_NVJPG_DISABLE` | Absent | Direct word is used as proof for the unordered pair, not as a recovered individual coordinate | 0% individual coordinate; observation alias only |
| `OPT_NVENC_DISABLE` | Absent | Absent | Direct width 2, 0% physical coordinate and no populated-engine evidence; unsupported review only |
| `OPT_OFA_DISABLE` | Absent | Direct word is used as proof for the unordered pair, not as a recovered individual coordinate | 0% individual coordinate; observation alias only |

Three recurring mismatches are therefore explicit rather than hidden:

1. A direct register name is not a physical fuse coordinate. This blocks
   per-link NVLink, individual NVJPG/OFA, NVENC and every other unknown map.
2. A physical coordinate is not a complete feature recipe. This blocks ECC,
   SW_SMC/MIG, DP-control and the reported SS0/SS1 values from execution.
3. A useful public whole-word transition is not an isolated field proof. XRAY
   preserves the public PCIe transformations as comparison models but admits
   only the four isolated bits to its compiler/runtime policy.

### Support-plane comparison

Public planners chiefly consume current option/defect values and raw FF rows.
XRAY additionally binds the planes below, because each can falsify an otherwise
plausible target. This is broader observation, not broader write authority.

| Domain | Public-tool gap retained for review | XRAY closure requirement |
|---|---|---|
| MAIN/IFF/RIR | Fixed row interval, IFF omitted/rejected, RIR not part of the logical plan | Complete 512-word MAIN, separate eight-word RIR, specimen-local `F/B/gap`, active IFF preservation and every-prefix replay |
| HBM | Per-field masks may be planned without proving all cross-planes | FBIO/FBPA/FBP/ROP-L2 option, CP, defect and status plus half-FBPA/LTC and density invariants |
| SM | Direct defect substitution can miss parent/child closure | GPC/PES/TPC option, CP, defect, reconfig/control/override/extend/status and privilege evidence, iterated to a fixed point |
| PCIe | Whole-word recipes include unmapped/co-resident policy | Isolated gates plus lane option/CP/control/status; endpoint and root-port training remain post-cold tests |
| NVLink | Three global bits are commonly called the whole unlock | Global group and independent 12-bit per-link option/control/status, PTOP count, reset/security gates and board/ROM prerequisites |
| ECC/MIG/media | Most tools omit the dependency planes; current ffcalc adds a narrower media path | Conventional controls, SYS_PIPE/GPC/memory partitions, media option/CP/defect/control/status, PTOP, HS/power/ucode/security and functional tests |
| Identity | Some tools forbid it; others emit two ID rows and optionally guess MAGIC_D | Paired IDs only, identity last, signed-ROM compatibility, 128-GiB BAR/platform check and external SPI recovery |
| DSOV/write gates | A glitch phenotype can be treated as sufficient | Controller entry state, privilege/security/program gates, fresh held-state attestation, electrical qualification and cold verification are separate gates |

## Pinned upstream deltas and retained boundaries

The following table records both the useful improvement and the reason it was
not copied blindly. References are exact paths and line ranges at the pinned
commit in the table above.

| Upstream | Material improvement | Retained contradiction or unsafe gap |
|---|---|---|
| pico-ga100vfi-console | The two-step FF transaction writes and verifies a chain-zero inert word before adding only the chain bits (host/ga100vfi/console/service.py:1052-1177). The A100 queue pairs DEVIDA and DEVIDB and the later correction removes the unproved 40-GB SKU (service.py:1418-1469). VBIOS flashing now requires a fresh backup, an unbound device and unloaded NVIDIA modules (service.py:3310-3453). | The campaign path calls the burner without the standalone plane gate (service.py:2523-2570), and an ABORT resumes glitch attempts (service.py:2579-2583). Row-read timeouts are discarded as if the row were absent (service.py:1324-1329); the FF end remains fixed at 487 (service.py:254-255); per-block placement bypasses buffer and recovery reserves (service.py:1377-1410). A command timeout still reaches volatile cleanup writes (service.py:1145-1184), and journal uncertainty can be operator-overridden (service.py:1072-1076). The VBIOS endpoint accepts any existing .rom path without a sealed image digest, parsed target identity, board-policy binding or exact-card recovery artifact; a missing serial can share the generic noserial backup namespace. The repository still documents five pre-existing full-suite failures (evidence/63-two-step-burn.md:112-120). |
| cmp170hx-ffcalc | The read-only catalog now covers controller/debug/IFF/CRC/EFP/program/security state, feature overrides, speed selectors, ECC/geometry and GFW/lane/HBM-PLL observations (ffcalc/data/ga100-fuses.json:65-105). The write-window probe checks poison/idle, DEBUGCTRL, CRC, EFP, program-disable, DSOV, privilege and armed PLM state (ffcalc/ffmacro.py:156-199,240-254). | Its reader/planner still enforces static rows 222..487 even though a separate helper exposes an IFF-derived window (ffmacro.py:267-278,308-319). Idle means only state 4 plus command 0 rather than the exact qualified word (ffmacro.py:64-65), and timeout/poison paths still restore FUSEADDR (ffmacro.py:68-119,202-219). A missing starting block becomes zero instead of an error (generate.py:142-169,178), so unknown co-resident bits can be erased logically. Defect mismatches are warning-only, there is no memory-pair invariant, and media defect inference uses STATUS AND NOT OPT even though equality cannot prove defect-free silicon (analyze.py:48-101,120-152). Operations sort lexically by chain/block rather than dependency-safe prefixes and there is no enforced correction/revert reserve (generate.py:176-193). The only row-487 exercise is fake, and 101/101 offline tests do not establish a physical burn. |
| cmp170hx-ffcalc PCIe | The current map names the relevant PCIe option groups and distinguishes identity from link policy. | It still clears block-39 bits 1 and 9 together although the isolated cold result falsified bit 1 (optionmap.py:151-156). The A100 route offers a whole-word MAGIC_D target of 0x00200000 which its own text calls an unmeasured guess (optionmap.py:163-174,204-208; generate.py:36-53,108-127). XRAY keeps only isolated bit 9 and MAGIC_D bit 25 eligible. |
| cmp170hx-ffcalc SW-IFF | A separate software-IFF path makes the proposed mechanism inspectable (ffmacro.py:308-387). | The source explicitly says it was never hardware-tested (ffmacro.py:341-347). It does not validate record bit 5, chain/block encoding, live occupancy, pulse completion or readback, and it does not restore EN_SW_OVERRIDE, IFF_RECORD or IFF_SW_FUSING. The burn artifact is a list rather than a UUID/fingerprint-bound complete state (burntool.py:188 onward; dump.py:204-212). Its initial gate requires EFP=1 before the later sense command, while the pinned ESP32 workflow reports fresh Goldilocks EFP=0 followed by sense-to-1 (burntool.py:119-131; esp32 README.md:112-118,245-257). Execution remains rejected. |
| cmp170hx-8gb-vfi-tools | docs/FUSES.md consolidates the dynamic-boundary warning, SM and HBM topology arithmetic, the 64/80/96-GB pair rule, isolated PCIe bit 9/MAGIC_D bit 25/boot bits 6 and 7, identity and media/MIG evidence (docs/FUSES.md:43-53,122-204,220-312). The planner correctly refuses a field whose starting block is unknown (tools/fuseplan/src/core.js:982-984). | The implementation still fixes its usable end at row 486 (core.js:5-9), while its documentation describes a dynamic boundary and discusses row 487. Its docs say to skip 0xffffffff but replay treats it as a bit-5 stop record (docs/FUSES.md:350-352; core.js:210-233). The 16-row reserve is only a warning (core.js:841-844); physical planning covers only compute, memory, PCIe, global NVLink and SYS_PIPE, with media and identity omitted (core.js:21-67; README.md:74-76). Its 487 offline checks compare against older ffcalc revision 4e089b5 and do not establish current parity or electrical safety. |
| cmp170hx-8gb-vfi-tools hypotheses | It records chain-1 block 26 RECONFIG, chain-2 blocks 78/79 speed-select candidates and several conventional-row hypotheses, making community claims reviewable (docs/FUSES.md:147-155,269-338). | Block 26, blocks 78/79, ECC/remapper/VGX/OpenGL/DP-control/TTU conventional rows remain review-only. No isolated cold result or complete feature dependency closure justifies an executable operation. |
| esp32-ga100vfi | The README now documents a two-pass capture, generate/simulate/verify workflow, independent cross-check, explicit sense/arm/program sequence and post-cold validation (README.md:192-275). It adds no new fuse coordinate. | It hard-codes FF rows 222..486 and IFF rows 487..511 (README.md:71-79,203,215,228), contradicting the same-day ffcalc specimen model with IFF beginning at 488. It proposes a real one-row canary (README.md:232-238), consuming finite OTP space. Its new AND NOT old equals zero check (README.md:81-88,228) is a conservative local policy, not FF last-wins physics: an append goes into a physically blank row and its payload replaces the earlier logical block, so the new logical word may contain both cleared and set bits relative to the previous winner. |
| cmpunlocker | Master adds a carefully documented HBM idle-governor register set: FBPA geometry/refresh, HBM PLL and serial-priv aliases, PRI fence, framebuffer-stop handshake, PMU counters, GFW boot and GSP status (common/constants.yaml:117-154; driver/hbm-pm/cmp_hbm_pm.c:91-149). | This is a stateful runtime power mechanism, not a fuse-map discovery. The write sequence must not be promoted into a read-only level. Registers not already allowlisted remain review-only until access class, ownership, workload exclusion and recovery are independently established. The separate test-gen4-rootport-ladder branch is also runtime link training, not a fuse delta. |

The console's advanced-unlock heading also claims its plans were executed and
confirmed, while its own evidence says signature checking was skipped for one
decode and that all 31 expected writes were absent from the alleged GPU2 post
image, which belonged to a different card
(evidence/30-advanced-unlocks.md:25-31;
evidence/60-gpu2-burn-truth.md:155-172). Its TTU, ECC and row-remapper direct
rows therefore remain attribution hypotheses, not a PLM-unlock recipe.

## Other public projects

| Project | Particularly useful contribution | Boundary XRAY adds |
|---|---|---|
| [`cmp170hx-ffcalc`](https://github.com/sayhellotojungle/cmp170hx-ffcalc) | Current option mapping, topology arithmetic and public burn workflow. | Complete specimen binding, independent prefix replay, explicit IFF/RIR state, corrected effective-disable arithmetic and separated public-faithful versus evidence-led models. |
| [`ga100-fuse-re`](https://github.com/ahnguyen17/ga100-fuse-re) | Valuable physical dumps, hypotheses and RIR experiments. | Every assertion is graded by direct evidence; unanchored extrapolations, primary/redundant aliases and specimen-specific values are not promoted to architecture law. |
| [`gpcprobe`](https://github.com/matstatman/gpcprobe) | Small BAR-read kernel-module shape and broad community register exploration. | Full PCI-domain identity, correct 64-bit BAR handling, actual resource length, capability checks, exclusive sessions, generation binding, allowlisted offsets, controller locking and fixed-operation helper ABIs. |
| [`170hx-rir-tool`](https://github.com/lixiangwuxian/170hx-rir-tool) | Public physical RIR-macro work and cold-persistence evidence. | MAIN and RIR are separate planes with separate provenance; a RIR result is never treated as proof of a main-FF sequence. |
| [`pico-ga100vfi`](https://github.com/dalzyu/pico-ga100vfi) and [`pico-ga100vfi-console`](https://github.com/portz3r0/pico-ga100vfi-console) | Reproducible glitch hardware, classifier, campaign controls and community timing results. | XRAY stores glitch state as evidence, not authority; it separates `2080`/`DSOV=0`, controller validity, EFP/write eligibility and post-glitch functional state. |
| [`cmp170hx-mig-nvdec-nvjpg-ofa-unlock`](https://github.com/repeaterdroid/cmp170hx-mig-nvdec-nvjpg-ofa-unlock) | Current media/MIG candidate coordinates and one-card results. | Candidate fields are defect-gated, cross-plane checked, certainty-labelled and kept non-executable until per-specimen and functional closure exists. |
| CMPGlitch/xPatrik materials | End-to-end community evidence for glitching, topology changes, conversion and software follow-up. | Archive provenance, byte identity, dated supersession, complete-image checks and explicit separation of demonstrated result from reusable procedure. |

## NVLink is not one field

The global option, copy and defect words at `0x00820684`, `0x00820688` and
`0x0082068c` are each three bits wide. They describe global IOCTRL groups. The
separate per-link option/control/status words at `0x008207f0`, `0x00820820`
and `0x00820c20` are 12 bits wide, while global `CTRL_OPT_NVLINK` and
`STATUS_OPT_NVLINK` at `0x008209b8` and `0x00820db8` are 16-bit result fields.

All four retained cards read global option/status `7`, global copy/defect `0`,
per-link option/control/status `0`, and a PTOP architectural count of 12. That
combination says no per-link edit is currently indicated, but it does not prove
that links are routed, powered or trained. The newest mutable CMP Wiki revision
reports one trained-link experiment after board power work, but publishes no
sealed numeric capture and contains stale statements elsewhere on the same
page. XRAY therefore keeps global clearing as an explicit offline comparison,
not a hidden step in the broad HBM/SM/identity plans.

Production A100's 12-link architectural ceiling is documented in
[NVIDIA's Ampere architecture article](https://developer.nvidia.com/blog/nvidia-ampere-architecture-in-depth/).
The current community report is retained as
[CMP Wiki NVLink oldid 2488](https://cmp.wiki/index.php?title=NVLink&oldid=2488).

## NVENC is not an unlock target

The generic GA100 register namespace contains NVENC-named option, defect,
control and status aliases. That does not establish a populated encoder.
NVIDIA's current data-center documentation lists A100 as having five NVDEC
engines and **no NVENC**, while its GA100 architecture material calls out five
NVDEC blocks and an optical-flow accelerator. On the four observed CMP 170HX
cards, the retained capture has NVENC option, CP, defect and throttle inputs at
zero while `STATUS_OPT_NVENC` remains `3` across repeated reads. The newly
added `CTRL_OPT_NVENC` and PTOP instance-count observations were not present in
that historical capture and must not be backfilled as zero. The observed
disable and defect planes therefore do not explain or change the status.

XRAY consequently inventories and reports the NVENC-named registers, but does
not invent a permanent NVENC coordinate or plan. Confidence is 99% for the
documented production A100 configuration and 97% that the four tested CMP
boards have no usable NVENC engine. An engineering-sample counterexample would
be new evidence, not a reason to silently reinterpret the present cards.

Primary architecture references:

- [NVIDIA Dynamo: Video Decode GPU Requirements](https://docs.nvidia.com/dynamo/v1.6.2/multimodal/video-decode-gpu-requirements)
- [NVIDIA Ampere Architecture In-Depth](https://developer.nvidia.com/blog/nvidia-ampere-architecture-in-depth/)
- [NVIDIA A100 Tensor Core GPU Architecture whitepaper](https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/nvidia-ampere-architecture-whitepaper.pdf)

## Claim boundary

XRAY's advantages are mostly stronger *negative guarantees*: it preserves more
of the input, labels what is unknown, rejects incoherent combinations and makes
the boundary between observation, planning and irreversible execution visible.
It cannot turn an undocumented physical descriptor, missing board wiring or an
unqualified programming supply into established fact. `UNLOCK_PLAN.md` is the
code-synchronized review surface for every currently represented target and
its execution disposition.
