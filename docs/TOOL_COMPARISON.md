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
`3d157f254991904674fb0f47d5e236ac4f9b5ce7` (2026-11-25), which is newer than
the snapshot in the initial research inventory and adds SYS_PIPE/media and
identity options. The `ga100-ff-tools` baseline remains revision
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
