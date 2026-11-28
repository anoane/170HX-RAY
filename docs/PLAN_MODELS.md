# Fuse-plan models

170HX-XRAY separates source reproduction, correction, specimen derivation and execution
readiness. A plan model is an argument about a physical image; it is not permission to
program that image.

The core `170HX-XRAY.py plan` command emits no physical operations. The separate workspace
companion `tools/170hx_candidate_plan.py` can emit non-empty offline candidate operations,
but has no MMIO backend, controller-write primitive or apply mode. Every
candidate remains `executable=false`. A separate v3 compiler may transform only
independently provable append operations into a distinct sealed manifest; that
does not reinterpret or execute the candidate itself.

## Model types

| Model | Purpose | May correct the source? | Required output |
|---|---|---:|---|
| `public-faithful` | Reproduce a named public plan byte for byte and preserve its claims | no | Original records, source hash, parser notes and contradictions |
| `public-corrected` | Apply only evidenced corrections to one public model | yes, explicitly | Source-to-correction diff, changed assumptions and regenerated records |
| `specimen-derived` | Apply a selected field policy to one complete card state | not applicable | Complete old/new physical images and per-card proofs |
| `xray-best-evidence` | Use the strongest mutually consistent evidence known to XRAY | yes | Provenance and confidence for every coordinate, target and dependency |
| `comparison` | Compare any two or more models without merging their authority | no | Field, record, image, proof and blocker deltas |
| `recovery` | Describe logical restoration after a verified prefix | no erasure assumed | Append-only restoration records, remaining capacity and limitations |

A public model is preserved even when it is demonstrably wrong. Its corrected form is a new
artifact with a different model identifier and source lineage.

## Current companion output

The current planner source uses schema `170hx-xray-candidate-plan-v1` and emits
24 profiles per specimen. Twelve are operational/offline candidate models:
isolated HBM, SM, the combined common permanent topology foundation, an
explicit global-IOCTRL-group comparison, two PCIe policies, identity,
experimental SYS_PIPE, the defect-gated MIG/NVDEC/NVJPG/OFA profile, two
corrected-public intents, and a staged XRAY-best A100-conversion intent. Twelve
more are zero-operation review-only profiles for SS0 IFF, SS1 IFF, DP and ECC
conventional bits, unsupported NVENC, unknown-map per-link NVLink, reported
per-specimen RECONFIG and speed-select FF records, and proposed conventional
row-remapper, VGX, OpenGL and TTU coordinates. It binds complete
old/proposed MAIN and RIR images, every `n+1` prefix, active-IFF preservation,
recovery records, field evidence, and blockers. The execution-input candidate
must bind MAIN, RIR, identity, and ordinary register context to one native
exact-current Level-3 capture; cross-date artifacts remain comparison-only. The
retained XRAY 4.0.0 candidate is a historical pre-media baseline:
`research-evidence/170hx-xray-specimen-plans-exact-current-level3-native-v4.0.0-2026-11-23.json`:
four specimens, eleven profiles per specimen, and candidate SHA-256
`21716beb9b9b9cb85e55a8f3078ee9c66dc5a843b995e0c26abe6ce92388ed9a`.
It was regenerated from the immutable legacy ABI-5 capture through explicit
read-only normalization and emits XRAY schemas and profile names. It must not be
relabeled as current 24-profile output. The RAY 3.7.0 and earlier v3.6.1
artifacts remain historical evidence. A current execution candidate is always
regenerated from a new exact-current same-session Level-3 capture.

The 24 research profiles are not 24 execution profiles. The v3 compiler and
runtime independently admit only seven field/stage sets: `specimen-80gb-hbm`
(historical model ID; `specimen-hbm-to-defect-ceiling` is the accurate CLI alias),
`specimen-more-sm`, `xray-common-permanent-topology`, `specimen-pcie-gen4-isolated`,
`specimen-a100-20b5-identity`, `specimen-sys-pipe-to-defect-experimental`, and
`specimen-media-mig-to-defect-experimental`. Every compiled manifest remains
`execution_authorized=false`. The identity profile forbids a test-row-only run
and an optional first-row pause and requires a minimum two-operation commit
group. The media profile preserves direct defect bits, orders SYS_PIPE before
NVDEC, and treats candidate bits 146 and 371 as one inseparable logical intent
because their individual NVJPG/OFA attribution is unresolved. Hardware still
receives two sequential row appends: the first creates an unavoidable
asymmetric prefix. Inseparability is a planner constraint, not hardware
atomicity. The
three-bit global IOCTRL-group candidate is review-only and is not the unknown
12-bit per-link map. A complete four-card current matrix therefore contains 28
independently compiled manifests. The 32-manifest count belongs only to the
historical 4.2 report/release validation performed before global NVLink was
removed from the eight-class execution policy; it is not current authority.

An independent oracle reopens all source captures and reconstructs source bindings, images,
FF replay, allocation, every prefix, PCIe ordering, identity, RIR, defect ceilings, recovery,
model separation and unresolved-target inventories without importing the planner or XRAY.
Passing that oracle establishes deterministic offline structure only. A fresh
held-window session, official GA100 descriptor, electrical qualification, real
cold persistence, and feature validation remain separate execution/evidence
gates.

## Complete field-model disposition

The frontend catalog, offline candidate planner, compiler and runtime are four
different trust boundaries. Every frontend catalog entry has
`planning_allowed=false`: the catalog records a coordinate or an explicit lack
of one, not permission to change it. The candidate planner independently
duplicates only the coordinates it can model and still emits
`executable=false`. The compiler and runtime then independently agree on a
smaller seven-profile allowlist. “Eligible” below means eligible for that
sealed-manifest review path only; it never means electrically qualified or
authorized to program OTP.

| Catalog field | Plane and current coordinate | Certainty | Candidate model | Compiler/runtime disposition |
|---|---|---:|---|---|
| `OPT_FBIO_DISABLE` | FF C1 bits 83–106, width 24 | 95% | Per-die normalized/direct `FBIO_DEFECTIVE`; memory prerequisite | Eligible in HBM and common-topology profiles |
| `OPT_FBPA_DISABLE` | FF C1 bits 107–130, width 24 | 95% | Per-die normalized/direct `FBPA_DEFECTIVE`; memory prerequisite | Eligible in HBM and common-topology profiles |
| `OPT_GPC_DISABLE` | FF C1 bits 131–138, width 8 | 97% | Fixed-point topology/`GPC_DEFECTIVE`; compute activation last | Eligible in SM and common-topology profiles |
| `OPT_PES_DISABLE` | FF C1 bits 251–274, width 24 | 91% | Fixed-point 3/3/2 PES closure/`PES_DEFECTIVE` | Eligible in SM and common-topology profiles |
| `OPT_FBP_DISABLE` | FF C1 bits 307–318, width 12 | 98% | Normalized/direct `FBP_DEFECTIVE`; memory activation last | Eligible in HBM and common-topology profiles |
| `OPT_ROP_L2_DISABLE` | FF C1 bits 319–342, width 24 | 95% | Shared normalized/direct `ROP_L2_DEFECTIVE` dependency | Eligible in HBM, SM and common-topology profiles |
| `OPT_NVLINK_DISABLE` | FF C1 bits 368–370, width 3 | 94% | Three global IOCTRL-group comparison only | Rejected; not the per-link field |
| `OPT_SYS_PIPE_DISABLE` | FF C1 bits 372–379, width 8 | 94% | Direct `SYS_PIPE_DEFECTIVE`; experimental prerequisite | Eligible only in the two experimental SYS_PIPE/media profiles |
| `OPT_PCIE_DEVIDA` | FF C2 bits 593–608, width 16 | 98% | Target `0x20b5`, paired with DEVIDB, identity last | Eligible only as the paired identity profile |
| `OPT_PCIE_DEVIDB` | FF C2 bits 609–624, width 16 | 96% | Target `0x20b5`, paired with DEVIDA, identity last | Eligible only as the paired identity profile |
| `OPT_DISABLE_GEN3_SPEED` | FF C2 block 39 bit 9, width 1 | 99% | Clear only this isolated bit; block bit 1 was experimentally falsified | Eligible in the isolated-PCIe profile |
| `PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE` | FF C2 block 47 bit 3; direct MAGIC_D bit 25 | 97% | Clear only the mapped Gen4-disable bit | Eligible in the isolated-PCIe profile |
| `OPT_PCIE_BOOT_GEN23_DISABLE` | FF C2 block 49 bit 6 | 96% | Clear isolated boot-speed gate | Eligible in the isolated-PCIe profile |
| `OPT_PCIE_BOOT_GEN3_DISABLE` | FF C2 block 49 bit 7 | 96% | Clear isolated boot-speed gate | Eligible in the isolated-PCIe profile |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT` | IFF target `0x0082381c`; reported value `0x88888888` and words `0x082381fd,0x88888888` | 85% target/value; 0% portable placement | `review-ss0-iff`, reported rows 484/485 only | Rejected; no portable IFF allocator/value proof |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT_1` | IFF target `0x00823820`; reported value `0x8` and words `0x0823823d,0x00000008` | 85% target/value; 0% portable placement | `review-ss1-iff`, reported rows 486/487 only | Rejected; row 487 is quarantined and no portable allocator exists |
| `OPT_TPC_GPC0_DISABLE` | FF C1 bits 187–194, width 8 | 93% | Fixed-point GPC0 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC1_DISABLE` | FF C1 bits 195–202, width 8 | 93% | Fixed-point GPC1 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC2_DISABLE` | FF C1 bits 203–210, width 8 | 93% | Fixed-point GPC2 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC3_DISABLE` | FF C1 bits 211–218, width 8 | 93% | Fixed-point GPC3 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC4_DISABLE` | FF C1 bits 219–226, width 8 | 93% | Fixed-point GPC4 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC5_DISABLE` | FF C1 bits 227–234, width 8 | 93% | Fixed-point GPC5 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC6_DISABLE` | FF C1 bits 235–242, width 8 | 93% | Fixed-point GPC6 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_TPC_GPC7_DISABLE` | FF C1 bits 243–250, width 8 | 93% | Fixed-point GPC7 TPC defect closure | Eligible in SM and common-topology profiles |
| `OPT_PERLINK_DISABLE` | Direct 12-bit field; physical FF coordinate unknown | 0% physical coordinate | `review-nvlink-per-link-unsupported` | Rejected; global bits 368–370 cannot substitute for it |
| `OPT_ECC_EN` | Conventional primary/redundant rows 6/7 bit 28 | 99% coordinate; 45% complete-enable sufficiency | `review-ecc-conventional` | Rejected; sense, RIR, firmware and datapath closure missing |
| `OPT_ECC_EN_CONTROL` | Conventional primary/redundant rows 6/7 bit 29 | 99% coordinate; semantics incomplete | `review-ecc-conventional` | Rejected; control polarity and complete-enable sufficiency missing |
| `OPT_SM_SPEED_SELECT_DP` | Conventional primary/redundant rows 6/7 bit 26; direct `0x00820224` | 99% | `review-dp-conventional`; one-bit FP64 selector | Rejected; no conventional-fuse execution model |
| `OPT_SM_SPEED_SELECT_DP_CONTROL` | Conventional primary/redundant rows 6/7 bit 27 | 65% proposed undo/control semantics | `review-dp-conventional` | Rejected; adjacent name is known but undo behavior lacks isolated cold proof |
| `OPT_SW_SMC_DISABLE` | Conventional primary/redundant rows 14/15 bit 11 | 99% coordinate; MIG sufficiency unproved | Frontend/MIG evidence only; no candidate operation | Rejected; no conventional-fuse or complete MIG model |
| `OPT_NVDEC_DISABLE` | FF C1 bits 139–143, width 5 | 90% | Direct `OPT_NVDEC_DEFECTIVE`; experimental media prerequisite | Eligible only in the experimental media/MIG profile |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT146` | FF C1 bit 146 | 88% set mapping; 0% individual attribution | Must change with bit 371; both direct defect words must be zero | One inseparable logical intent, emitted as sequential appends with an asymmetric prefix |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT371` | FF C1 bit 371 | 88% set mapping; 0% individual attribution | Must change with bit 146; both direct defect words must be zero | One inseparable logical intent, emitted as sequential appends with an asymmetric prefix |
| `OPT_NVJPG_DISABLE` | Direct width 1; physical coordinate unknown; candidate set `{146,371}` | 0% individual; 88% set mapping | Observation alias only, never an individual candidate | Rejected individually |
| `OPT_NVENC_DISABLE` | Direct width 2; physical FF coordinate unknown | 0% physical coordinate and supported target | `review-nvenc-unsupported` plus option/CP/control/defect/status/PTOP inventory | Rejected; no populated-engine evidence or defensible target |
| `OPT_OFA_DISABLE` | Direct width 1; physical coordinate unknown; candidate set `{146,371}` | 0% individual; 88% set mapping | Observation alias only, never an individual candidate | Rejected individually |

The frontend certainty is confidence in the stated coordinate or set relation,
not in a complete feature unlock. In particular, 99% confidence in an ECC,
SW_SMC or DP row alias does not raise confidence in its proposed feature recipe.
Likewise the two media bits are 88% as an unordered set and 0% as individually
named NVJPG/OFA coordinates.

### Observation and closure planes

Candidate targets are not accepted from an option word alone. The frontend
keeps the following supporting planes in each goal so missing, stale or
contradictory evidence becomes a blocker rather than an assumed zero. These
registers are evidence inputs; they are not additional physical coordinates.

| Goal/family | Required supporting observations |
|---|---|
| DSOV persistence | `DISABLE_SW_OVERRIDE_STATUS`, `EN_SW_OVERRIDE`, `OPT_SECURE_FUSE_RESENSE_DISABLE`, `ENABLE_FUSE_PROGRAM_STATUS`, `DISABLE_FUSE_PROGRAM_STATUS`, `BYPASS_FUSES_STATUS`, `SECURED_BURN_FEATURE_ENABLE`, `CRC_STATUS`, `FUSECTRL`, `IFF_SW_FUSING`, `IFF_RECORD`, and the FUSEWDATA/FUSECTRL/OPT privilege masks |
| Fuse-write eligibility | `FUSECTRL`, `DEBUGCTRL`, all enable/disable/bypass/DSOV program-status gates, FUSEWDATA/FUSECTRL/OPT privilege masks, `OPT_SECURE_FUSE_WDATA_WR_SECURE`, `OPT_SECURE_FUSE_RESENSE_DISABLE`, and `SECURED_BURN_FEATURE_ENABLE` |
| HBM/FB | Option, `_CP`, direct defect and status planes for FBIO, FBPA, FBP and ROP/L2, plus `STATUS_FB_CONFIG`, `STATUS_HALF_FBPA` and `STATUS_HALF_LTC` |
| GPC/PES/TPC | GPC and PES option/CP/defect/status; TPC GPC0–7 option, reconfig, CP, defect, control, reconfig-override, disable-extend and status; TPC/reconfig/floorsweep privilege masks |
| PCIe | The four isolated speed gates plus lane option/CP/control/status; whole MAGIC_D words remain comparison-only |
| NVLink | Global option/CP/defect/status, 12-bit per-link option/control/status, 16-bit global control/status, PTOP link count, NVLIPT reset-condition/sequence gates and secure mask/buffer/physical-damage gates |
| ECC | Conventional enable/control, FB config, all three feature-override/readout words, override privilege, FBPA ECC control and secure feature/FBPA/SRAM/PLTS ECC gates |
| MIG | Conventional SW_SMC, SYS_PIPE option/CP/defect/status, GPC closure and FBHUB bottom/middle/boundary partition registers |
| Media | NVDEC/NVJPG/NVENC/OFA option/CP/defect/control/status and PTOP inventory where defined; HEVC/VP8-VP9, NVENC throttle, HS-disable, ucode-revision, PKC/security, and all sixteen NVDEC FPF-version observations |
| Identity/VBIOS | Both redundant device-ID fields, signed-ROM compatibility and an external recovery path; no single ID word is sufficient |
| SS/OC | GPC2CLK cap, ATE Vmin, CP/sample/SRAM-Vmin bins, SPEEDO0–2 and revisions, FPF overclock info, DP/FFMA/FMLA16/FMLA32/IMLA0–4 selectors and both SS override registers |

None of the supporting observation families is silently converted into a fuse
operation. Unsupported coordinates, secure gates, shadows, controls and status
words remain observations until a distinct coordinate, target, dependency and
cold-functional proof exists.

## Completeness states

Plans progress monotonically through these states:

1. `inventory-only`: source and declarations are recorded, but records are not trusted.
2. `record-valid`: each encoded word agrees with its declared chain, block and data.
3. `logical-replayable`: records replay deterministically under a stated FF/IFF/RIR model.
4. `physical-image-complete`: complete attested old and proposed 512-row images exist.
5. `prefix-analyzed`: baseline and every operation prefix have been decoded and checked.
6. `specimen-qualified`: field targets are bounded by this die's defect and topology evidence.
7. `electrically-qualified`: program-source and controller behavior are established for the
   exact board and procedure.
8. `independently-reviewed`: a second implementation reproduces the complete result.

No state implies executable authority. Failure at a later state does not erase earlier
evidence; it records a blocker.

## Required source binding

Every plan must carry:

- plan schema and model type;
- exact source byte SHA-256 and, for repositories, commit ID and commit date;
- parser/normalizer version and SHA-256;
- complete card identity and capture SHA-256;
- capture attestation result, not just copied `coherent=true` claims;
- register/map/decoder revisions;
- old MAIN, RIR and relevant IFF/shadow/runtime-plane hashes;
- source chronology and whether a newer source supersedes the interpretation;
- all assumptions, confidence values and unresolved contradictions.

An internal plan digest proves only self-consistency. It does not authenticate the acquisition
unless the original capture is reopened, rehashed and re-attested.

## Physical-operation record

Each proposed append must include at least:

```text
row
expected_physical_old_word
proposed_physical_word
chain / type / block / data
logical_block_old / logical_block_new
fields intentionally changed
co-resident fields preserved
unmapped bits preserved
source and confidence for every coordinate
operation ordering reason
prefix result hash
immediate verification
cold-latch verification
recovery alternative
```

`expected_physical_old_word` must normally be zero for an append. Logical `old -> new` block
values are not physical bit transitions and must never be used as an OTP monotonicity proof.

## Whole-image and prefix proofs

For `n` operations, XRAY must validate `n+1` complete states. At every prefix it must prove or
block on:

- the exact physical image and append-row occupancy;
- FF grammar, ordering and last-wins replay;
- active permanent and software IFF effects;
- conventional primary/redundant/RIR results;
- no assumed `1 -> 0` physical transition;
- preservation of known-defective units;
- preservation of every unselected or unknown co-resident bit;
- boot-critical memory, compute, identity and firmware consistency;
- remaining FF/IFF capacity and recovery reserve;
- cold-reset outcome and recovery classification.

An interruption with uncertain programming state is never automatically retried.

## Initial public-source inventory

This table is the starting inventory, not a trust ranking. Dates refer to the source snapshot
or containing commit and must be retained because later work often corrects earlier attempts.

| Source | Snapshot | SHA-256 | Initial classification |
|---|---|---|---|
| `FF-BURN-MAP-2026-11-17.md` | 2026-11-17, corrected 2026-11-23 warning | `3d1a55b1b4e61a0de1553159eb2654990a9eecc2fbfea15f575ccee35afec25f` | Two reported per-card record histories plus a field map; the added warning marks `0x0580` superseded by the isolated `0x0582` result |
| Ethan/CMPGlitch `cmp_burn.md` | repository commit `feb61e250b357cae5acd424a6685b2f27c1e806a`, 2026-11-21 | `46431a51e57994f1b61edc779071a879c908ab141268e6590f96a29751d65841` | Reported 20-record 80-GB/128-SM/PCIe/NVLink/20b5 history |
| Archived CMPGlitch `cmp_burn.md` | archive snapshot | `46431a51e57994f1b61edc779071a879c908ab141268e6590f96a29751d65841` | Byte-identical duplicate of the preceding source; one authority, not independent corroboration |
| Danylo `plan.json` | 2026-11-17 evidence set | `49e43976220319853f7c30a2829e01bd937aa3ed33c0c22bf6d3f5c3438642f2` | Hash-bound 16-record specimen plan for compute/memory/NVLink/PCIe-full; excludes identity/VBIOS |
| Danylo `ga100_ff.py` | 2026-11-21 package | `04c13b043a7003a13306c55e99b4585a4aa7bde2a97641dc91df5d06cb331a01` | Per-card backup/calculation/program/verification implementation; disputed row 487 excluded |
| Ethan `ga100_ff.py` | commit above | `17a6b968360725bd9613d4ad8a9f180232eb3901a719b1180b9f7cbed341d015` | Different integrated implementation; missing defect keys can default to zero and require correction |
| cmp170hx-ffcalc generator | commit `94a16e5572a7b4464ca38073cc399919dcc1895e`, 2026-11-17 | `d1573ffae3bb6c81ee71d33ee5d0862e17a7d496d21be5d2cd2d90018b19a203` | Generic field-based generator; map and boundary assumptions must be isolated |
| cmp170hx-ffcalc burn tool | same commit | `3158dabc9316dff7f2135b03a028c73364f4b49276c033d3be6406e73549a33a` | Programming procedure, not an independent target planner |
| kprinssu `ffcard.py` | commit `594e83ddd336469aea303d466d0c1d2a1edb2d05`, 2026-11-20 | `f90bbcf590f5c68a67c503a17cdce5c670232ffc9ac99a93eeee1ab35dd843dd` | Offline specimen planner; intentionally forbids device-ID blocks and uses fixed PCIe baselines |
| root `ffplan.py3` | retained public-source-only experiment | `e8e3972f28a0b8d0dc3d33810584e2c16f2c843c01b7d3bc448b7e658e848ba2` | Logical public-map planner; excludes ambiguous Gen3 mapping and has no programming model |
| R38x W1/RIR recipe attachment | 2026-10-07 | `a56f056b30493cfac6d2290fd9fe997c498da4d6710d79e09c64b64298670db0` | Unexecuted analog/RIR proposal; its analog hypothesis is later constrained by the 2026-11-02 experiment |
| RIR-keystone plan attachment | 2026-10-09 | `4a6149f6ba743f3d14b31fe7a6ccd509d955ee192ed93ee6002e28d8157f03a6` | Unexecuted 10-GB/free-RIR `Addr=5,Data=0` proposal; not evidence for spent-RIR 8-GB cards |
| Host-class burn experiment note | 2026-11-02 | `8b0dd4d7cece78a01c9c7a68ff7e52a312670fa4e55257dc1d89e59998c59323` | Real-payload timing/GPIO trials: pulse width scaled but zero bits landed; supersedes the earlier zero-WDATA timing inference |
| Row-486 failure report and capture | 2026-11-20 | report `842c3a675cf52c1ad6f38931ea7acffc1d522c4343c2215fb0feeb8624eb833b`, dump `b5296a7fd32852372e5f48464b92bca965b0b6c254b990de9f8cef72385deaf8` | Cold capture of a last-row canary overriding an earlier block-5 fix; direct prefix/order failure evidence |

Additional Discord attachments and newer public repositories must be added by byte hash before
their claims enter any corrected or XRAY model.

### First faithful normalization result

The offline source auditor normalized 19 `FF-BURN-MAP`, 20 CMPGlitch, 16 Danylo and
15 records from the 2026-11-20 row-486 failure capture. All four sets pass their own
declared `(new << 16) | (block << 6) | chain`
encoding and repeated-block transition continuity. That establishes transcription
consistency only.

The first comparison already preserves meaningful differences instead of averaging them:

- chain-1 blocks 8, 11, 12, 15 and 16 differ with per-die GPC/TPC/PES targets;
- chain-1 block 23 differs because one history clears SYS_PIPE while two retain it;
- chain-2 blocks 46/47 distinguish the isolated-bit PCIe profile from the
  A100-exact whole-word profile;
- Danylo excludes device-ID blocks 37/38 while the two conversion histories target `20b5`.
- the failure capture proves by physical row ordering that its row-486 block-5 canary
  overrides the intended row-297 block-5 correction (`0x8007 -> 0x9867`).

Legacy artifact: `research-evidence/170hx-ray-public-plan-audit-2026-11-23.json`, SHA-256
`ab96b4f03b2e258e7efe4763d4b377e3e60cfe6d20314e76b3b080df3c3759c2`.
Auditor 0.3.0 now refuses any primary or supporting source whose bytes differ from the
pinned SHA-256 before parsing; a tamper regression test covers that boundary. The report
remains `executable=false` and makes no coordinate, electrical or functional claim.

## Unlock target decomposition

A combined A100 conversion is represented as staged subplans rather than one undifferentiated
record list:

1. memory topology and HBM capacity;
2. GPC/TPC/PES/ROP-L2 compute topology;
3. PCIe speed and lane policy;
4. NVLink fuse, board and firmware prerequisites;
5. device identity migration;
6. VBIOS migration and recovery;
7. software floorsweep/runtime policy after A100 VBIOS;
8. ECC, MIG and media-engine policy only when their independent dependencies are proven;
9. post-change enumeration, full-memory, all-SM, link, error and cold-cycle validation.

Each stage has its own prefix and recovery analysis. Device-ID migration is scheduled only when
the post-conversion `20b5` observation and recovery path is already available.

## Current execution boundary

Core observation and plan development remain non-mutating. Plan development may
produce non-empty proposed operations, but:

- they are offline artifacts;
- `executable` remains false;
- `apply` and the legacy v1 `full-write` path remain incapable of MMIO or OTP
  programming;
- only a separately compiled `170hx-xray-otp-sealed-manifest-v3` can reach the
  narrow experimental blank-FF append backend; and
- no candidate plan by itself authorizes glitching, voltage changes, reset,
  VBIOS flashing, or fuse writes. V3 additionally requires exact live gates, a
  reviewed receipt, controlling-TTY confirmation, a fresh same-function native
  DSOV=0 held-state capture, conditional EFP0-SENSE/EFP1-skip behavior, a
  helper-serialized (explicitly not hardware-atomic) double-pass capture, exact
  equality of both complete armed 512-row images to the held-state image, two
  full expected-image passes after every row,
  durable journaling, and native post-cold capture verification.

The v3 backend remains an experimental userspace `resource0` mmap transport.
Its PCI ownership sentinel, resource-inode binding and immediate pre-command
re-attestation do not atomically close the bind/remove TOCTOU; only a reviewed
kernel-owned irreversible transaction boundary could close that gap. The
`inert-then-arm-experimental-trace-only` two-pulse model is emitted for offline
review only and is excluded from both executable strategies and authorization.
