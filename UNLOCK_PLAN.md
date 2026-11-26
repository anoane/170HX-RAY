# 170HX-XRAY unlock-plan contract

<!-- XRAY-PLAN-CONTRACT-SHA256: 66ee5c383ef4201601742f042c276257589f8b5f20a83e3c10c0cdf3a609348d -->

This document is the code-level contract for XRAY planning. It describes what
the software actually represents, which coordinates it uses, which ordering it
enforces, which formats preserve plan semantics, and which research targets are
still deliberately non-executable. It is not a generic fuse-burning guide and
does not turn an offline candidate into hardware authorization.

The digest above is recomputed by `tests/test_unlock_plan_doc.py` from the
frontend goal/catalog tables, candidate profiles and formats, compiler
allowlists, and the backend's independent runtime allowlists. A code change to
any of those objects must update this document and digest together.

## Status vocabulary and exact counts

- **Observation goal**: a `170HX-XRAY.py plan` checklist. It emits no physical
  operations.
- **Candidate profile**: an offline, specimen-bound model from
  `tools/170hx_candidate_plan.py`. Every candidate artifact has
  `execution_authorized=false` and `executable=false`, even when it contains
  encoded append operations.
- **Review-only profile**: a first-class candidate entry with exactly zero
  physical operations. It records an unresolved plane, coordinate, value, or
  hardware-population question without inventing a recipe.
- **Manifest-eligible profile**: one of the compiler's seven exact profile
  allowlists. Eligibility means only that a sealed experimental manifest can be
  compiled after all exact-current checks; it is not electrical qualification
  or permission to burn.
- **Runtime-eligible profile**: the same seven profiles independently repeated
  by the backend. The runtime does not trust the compiler's table.
- **Unsupported**: no defensible physical target exists. No output format or
  confirmation prompt upgrades an unsupported target.

Current inventory: 11 observation goals, 18 candidate profiles, six of them
zero-operation review-only profiles, and seven compiler/runtime-eligible
profiles. The compiler is version `0.5.0`; its allowlist reduction is a safety
policy and changes its source hash even though the sealed schema and backend ID
`ga100-vfi-ff-append-v1` remain unchanged.

## Three planning layers

| Layer | Entry point | Result | Can contain physical rows? | Can authorize hardware? |
|---|---|---|---:|---:|
| Goal/checklist | `170HX-XRAY.py plan` | evidence, unknowns and checks | No | No |
| Candidate planner | `tools/170hx_candidate_plan.py` | complete old/new 512-row MAIN images, eight-word RIR preservation, prefix replay and recovery model | Yes, but always non-executable | No |
| Sealed append compiler/runtime | `tools/170hx_vfi_append_manifest.py` and `otp_vfi_append.py` | independently allowlisted blank-FF append manifest and receipt | Yes, for seven profiles only | Not by compilation alone |

These layers must not be described as interchangeable. In particular, the
presence of a field in the frontend catalog, a row dump, or a candidate profile
does not imply that the v2 compiler accepts it.

## Input and output formats

### Row observations

The frontend dump importer accepts `auto`, `json`, `jsonl`, `indexed-text`, and
`raw-u32le`. The row exporter supports `xray-json`, `xray-jsonl`,
`indexed-text`, `raw-u32le`, `ffcalc-json`, `xpatrik-json`,
`ga100-fuse-re-json`, `dstoro-projection-json`, and `dstoro-jsonl`.

Those formats describe observations or row projections. The public-shaped
formats can lose structure, provenance, IFF/RIR boundaries, or certainty. They
are not semantic fuse-plan formats and are never accepted as burn authority.

### Candidate-plan inputs

The candidate planner has two distinct input roles:

- Physical MAIN/RIR: `auto` or `xray-native-level3-json` only. The validator
  requires a native certified transaction, supported helper ABI, complete row
  counts, restored controller/address/debug state, full-domain identity and
  coherent transaction counters.
- Separate live register context: `auto` or `xray-level2-json` only.
  `--same-capture-context` instead binds stable ordinary registers from the same
  native Level-3 artifact; supplying `--live-input-format` in that mode is an
  error.

Format declarations are enforced against the immutable on-disk schema and tool
identity before compatibility normalization. `auto` may detect and explicitly
record `legacy-ray-native-level3-json` or `legacy-ray-level2-json`, including
`legacy_normalization_applied=true`; an explicit current-format label rejects
those legacy envelopes. Every physical/live source binding records its declared
and detected format. The flags are therefore validation constraints, not
decorative CLI metadata.

There is intentionally no candidate-plan input adapter for FFCalc, xPatrik,
`ga100-fuse-re`, dstoro projections, arbitrary JSONL, indexed text, or raw row
bytes. Those containers do not carry the exact transaction and register
binding required by the planner.

### Candidate-plan outputs

`--output-format` accepts four lossless representations of the same semantic
document:

| Label | Representation | Round-trip rule |
|---|---|---|
| `json` | sorted, indented UTF-8 JSON plus newline | decodes to the complete plan object |
| `jsonl` | exactly one canonical JSON object and newline | more than one record is rejected |
| `text` | human header plus a marked, complete JSON object | missing markers and duplicate keys are rejected |
| `canonical-json-bin` | canonical ASCII JSON bytes, no newline | **not** an OTP image, row image or command stream |

JSON remains the default for backward compatibility. `--export-all-dir`
creates all four files with create-only semantics, mode `0600`, in a real
non-symlink directory forced to mode `0700`. Every format is round-trip tested
for semantic parity.

The built-in checklist planner and sealed manifest compiler still emit JSON.
There is no honest representation of a semantic plan in `ffcalc-json`,
`xpatrik-json`, `ga100-fuse-re-json`, or dstoro row formats, so XRAY does not
pretend otherwise.

## Frontend observation goals

| Goal ID | What the goal does | What it does not establish |
|---|---|---|
| `persistent_dsov0` | Tracks DSOV/EN_SW/reset-domain observations | cold persistence or a permanent bypass mechanism |
| `fuse_write_eligibility` | Classifies observed controller, privilege and program gates | electrical readiness or exclusive ownership |
| `80gb_hbm` | Checks per-die FBP/FBPA/FBIO/ROP-L2 closure | a universal 80-GiB target |
| `more_sms` | Checks GPC/TPC/PES/ROP-L2 topology | post-VBIOS software floorsweep policy or silicon health |
| `pcie_gen4_x16` | Inventories speed and lane gates | signal integrity, equalization or a universal MAGIC_D word |
| `nvlink` | Inventories global and per-link option/defect/status | a physical per-link fuse map or board links |
| `ecc` | Inventories ECC enable/control and runtime domains | a complete conventional-fuse recipe |
| `mig` | Inventories SYS_PIPE/GPC/memory/firmware dependencies | complete MIG sufficiency |
| `nvdec_nvjpg` | Inventories NVDEC, NVJPG, OFA and NVENC evidence, including NVENC option/CP/control/defect/status plus PTOP instance count | individual bits 146/371 attribution or NVENC population; PTOP count is inventory only |
| `a100_identity_vbios` | Couples redundant device IDs, signed ROM and recovery | that an A100 ROM unlocks CMP speed/floorsweep policy |
| `oc_frequencies` | Inventories bins, Vmin, clocks and speed selectors | portable or reliability-qualified permanent targets |

The selectable planning models `evidence-only`, `public-model`,
`cross-checked-theory`, and `compare` affect explanation and comparison, not
execution eligibility.

## Exact-current arithmetic and invariants

### HBM ceiling

For the qualified CMP `20c2/1585` 8-GiB-per-FBP population:

```text
usable_fbps = 12 - popcount(FBP_DEFECTIVE & 0x0fff)
defect_preserving_hbm_gib = usable_fbps * 8
```

The result is admitted only when the 24-bit FBPA, FBIO and ROP/L2 defect planes
match the two-bits-per-FBP expansion and the decoder's HALF_FBPA/density
invariants. The exact supported classes for this package are 64, 80 and 96 GiB.
An A100 `20b5` device ID alone does not distinguish package density; converted
or native A100 input needs an explicit specimen-qualified density.

### SM ceiling

```text
for each GPC n, iterate to a fixed point:
  normalized_tpc_defect[n] = raw_tpc_defect[n]
                             OR expand_3_3_2(raw_pes_defect[n])
                             OR (0xff if raw_gpc_defect[n] else 0)
  a fully disabled 3/3/2 TPC group closes its PES parent
  all three PES groups or all eight TPCs close the GPC parent

defect_preserving_sm =
  sum(8 - popcount(normalized_tpc_defect[n]) for n in 0..7) * 2
```

The fixed-point parent/child closure means a PES defect is never ignored and a
disabled descendant group cannot leave an impossible enabled parent. The
planner also requires the shared ROP/L2 dependency. The architecture maximum
is 128 SMs. This is a defect-preserving
topology ceiling, not a health guarantee. A100 VBIOS/GSP policy or a driver/EFI
floorsweep may still expose fewer SMs, so software follow-up must remain within
that ceiling and be measured after each identity/ROM stage.

### Target rules

- A `disable_mask` target is the normalized topology target when available;
  otherwise it is the same die's direct defect mask, width-masked.
- The NVJPG/OFA paired candidates target zero only when both direct defect bits
  are zero. Either defect bit causes a hard refusal because individual
  attribution is unknown.
- `OPT_PCIE_DEVIDA` and `OPT_PCIE_DEVIDB` both target `0x20b5`.
- The four isolated PCIe gate bits target zero.
- Raw whole-word targets are admitted only by the offline profiles that declare
  exact expected old values. They are not in the v2 execution allowlist.

### FF record and allocation rules

An FF record is encoded as:

```text
word = (logical_data_16 << 16) | (logical_block_10 << 6) | chain_5
```

Chain zero, chain 31+, block 1024+, data wider than 16 bits, or a word colliding
with the IFF type bit is rejected. The shared physical scan uses empirical
`S=222` and physical end `E=511`; these are not an official descriptor. For
each specimen, `B` is the measured first active IFF record and `F` is the first
row after the last occupied/spent FF row below `B`. `gap=B-F` is observed
capacity, not blanket authorization.

Operations allocate first-fit contiguous blank MAIN rows from measured `F`, stay
below `B`, quarantine row 487, and preserve at least sixteen unquarantined rows
after append-only recovery records. There are no canary/no-op records. Every
prefix is replayed as a complete 512-row MAIN image and must remain structurally and
GA100-semantically valid, preserve the active IFF signature, preserve all
undeclared bits, and keep the separate eight-u32 RIR vector byte-identical.

Recovery means appending a later record that restores the old logical block.
It does not erase OTP and is not guaranteed reachable after an unbootable
prefix.

## Complete catalog-to-plan audit

This is the human review surface for every entry in the live
`LOGICAL_FIELD_CATALOG`, including entries that intentionally generate no
operation. The frontend never authorizes planning from this table: all 36
catalog entries have `planning_allowed=false`. The companion candidate planner
duplicates a subset of coordinates and checks them against the catalog; the v2
compiler and runtime then independently pin a still smaller subset. A catalog
coordinate, a candidate operation, manifest eligibility and authorization are
four different statements.

| Field | Coordinate and certainty | Exact target rule and reason | Current plan disposition |
|---|---|---|---|
| `OPT_FBIO_DISABLE` | C1 bits 83–106, 24 bits, 95% | Normalized/direct `FBIO_DEFECTIVE`; prepare memory I/O before exposing FBPs | Candidate; eligible in HBM/common-topology profiles |
| `OPT_FBPA_DISABLE` | C1 bits 107–130, 24 bits, 95% | Normalized/direct `FBPA_DEFECTIVE`; prepare partition agents before exposing FBPs | Candidate; eligible in HBM/common-topology profiles |
| `OPT_GPC_DISABLE` | C1 bits 131–138, 8 bits, 97% | Fixed-point normalized/direct `GPC_DEFECTIVE`; parent activation occurs after TPC/PES prerequisites | Candidate; eligible in SM/common-topology profiles |
| `OPT_PES_DISABLE` | C1 bits 251–274, 24 bits, 91% | Fixed-point 3/3/2 PES closure; a fully disabled child group closes its parent | Candidate; eligible in SM/common-topology profiles |
| `OPT_FBP_DISABLE` | C1 bits 307–318, 12 bits, 98% | Normalized/direct `FBP_DEFECTIVE`; activate only after FBIO/FBPA/ROP-L2 prerequisites | Candidate; eligible in HBM/common-topology profiles |
| `OPT_ROP_L2_DISABLE` | C1 bits 319–342, 24 bits, 95% | Normalized/direct `ROP_L2_DEFECTIVE`; shared HBM and compute dependency | Candidate; eligible in HBM, SM and common-topology profiles |
| `OPT_NVLINK_DISABLE` | C1 bits 368–370, 3 bits, 94% | Direct `NVLINK_DEFECTIVE` projection for global IOCTRL groups only | Offline `specimen-nvlink-global` crosscheck; compiler/runtime reject it |
| `OPT_SYS_PIPE_DISABLE` | C1 bits 372–379, 8 bits, 94% | Direct `SYS_PIPE_DEFECTIVE`; reported prerequisite but not complete MIG proof | Experimental candidate; eligible only in SYS_PIPE/media profiles |
| `OPT_PCIE_DEVIDA` | C2 bits 593–608, 16 bits, 98% | Exact target `0x20b5`; redundant identity half, so it must be paired and scheduled last | Candidate; eligible only in paired identity profile |
| `OPT_PCIE_DEVIDB` | C2 bits 609–624, 16 bits, 96% | Exact target `0x20b5`; redundant identity half, so it must be paired and scheduled last | Candidate; eligible only in paired identity profile |
| `OPT_DISABLE_GEN3_SPEED` | C2 block 39 bit 9, 99% | Clear bit 9 only; the 2026-11-19 isolated cold result falsified the older bit-1 interpretation | Candidate; eligible in isolated-PCIe profile |
| `PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE` | C2 block 47 bit 3/direct MAGIC_D bit 25, 97% | Clear the isolated Gen4-disable declaration without importing a whole-word guess | Candidate; eligible in isolated-PCIe profile |
| `OPT_PCIE_BOOT_GEN23_DISABLE` | C2 block 49 bit 6, 96% | Clear this isolated boot-speed gate and preserve co-resident policy | Candidate; eligible in isolated-PCIe profile |
| `OPT_PCIE_BOOT_GEN3_DISABLE` | C2 block 49 bit 7, 96% | Clear this isolated boot-speed gate and preserve co-resident policy | Candidate; eligible in isolated-PCIe profile |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT` | IFF target `0x0082381c`; reported words `0x082381fd,0x88888888`; 85% target/value, 0% portable placement | Review the reported SS0 override for IMLA0–3/FMLA/FFMA/DP without copying rows 484/485 to another card | Zero-operation `review-ss0-iff`; rejected |
| `FEATURE_OVERRIDE_SM_SPEED_SELECT_1` | IFF target `0x00823820`; reported words `0x0823823d,0x00000008`; 85% target/value, 0% portable placement | Review the reported SS1/IMLA4 override; it is not FP64 and reported row 487 is quarantined | Zero-operation `review-ss1-iff`; rejected |
| `OPT_TPC_GPC0_DISABLE` | C1 bits 187–194, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC0_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC1_DISABLE` | C1 bits 195–202, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC1_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC2_DISABLE` | C1 bits 203–210, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC2_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC3_DISABLE` | C1 bits 211–218, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC3_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC4_DISABLE` | C1 bits 219–226, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC4_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC5_DISABLE` | C1 bits 227–234, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC5_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC6_DISABLE` | C1 bits 235–242, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC6_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_TPC_GPC7_DISABLE` | C1 bits 243–250, 8 bits, 93% | Fixed-point normalized/direct `TPC_GPC7_DEFECTIVE` | Candidate; eligible in SM/common-topology profiles |
| `OPT_PERLINK_DISABLE` | Direct width 12; physical coordinate unknown, 0% | No target can be encoded; the three global bits are not a substitute | Zero-operation `review-nvlink-per-link-unsupported`; rejected |
| `OPT_ECC_EN` | Conventional rows 6/7 bit 28, 99% coordinate; 45% complete-feature sufficiency | Preserve the literal primary/redundant alias as evidence; do not infer polarity, RIR, firmware or datapath closure | Zero-operation `review-ecc-conventional`; rejected |
| `OPT_ECC_EN_CONTROL` | Conventional rows 6/7 bit 29, 99% coordinate; semantics incomplete | Review adjacent control with enable; never treat adjacency as complete-enable proof | Zero-operation `review-ecc-conventional`; rejected |
| `OPT_SM_SPEED_SELECT_DP` | Conventional rows 6/7 bit 26/direct `0x00820224`, 99% | Review the one-bit FP64 speed selector independently of SS1/IMLA4 | Zero-operation `review-dp-conventional`; rejected |
| `OPT_SM_SPEED_SELECT_DP_CONTROL` | Conventional rows 6/7 bit 27, 65% proposed undo/control semantics | The name/adjacency is real, but available static specimens and cold tests do not prove undo behavior | Zero-operation `review-dp-conventional`; rejected |
| `OPT_SW_SMC_DISABLE` | Conventional rows 14/15 bit 11, 99% coordinate | MIG evidence input only; no primary/redundant/RIR execution rule or complete MIG sufficiency exists | Frontend-only unsupported hypothesis; no candidate operation |
| `OPT_NVDEC_DISABLE` | C1 bits 139–143, 5 bits, 90% | Direct `OPT_NVDEC_DEFECTIVE`; enable only non-defective decoder instances | Experimental candidate; eligible only in media/MIG profile |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT146` | C1 bit 146; 88% unordered-set relation, 0% individual attribution | Target zero only with bit 371 and only when both NVJPG/OFA direct defect words are zero | Experimental inseparable pair; two-operation commit only |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT371` | C1 bit 371; 88% unordered-set relation, 0% individual attribution | Target zero only with bit 146 and only when both NVJPG/OFA direct defect words are zero | Experimental inseparable pair; two-operation commit only |
| `OPT_NVJPG_DISABLE` | Direct width 1; individual physical coordinate 0%; set `{146,371}` 88% | Direct option/defect/status evidence validates the pair but cannot name one bit | Observation alias only; rejected individually |
| `OPT_NVENC_DISABLE` | Direct width 2; physical coordinate and supported target 0% | Inventory option/CP/control/defect/status/PTOP only; production A100 has no NVENC and retained cards show no usable engine | Zero-operation `review-nvenc-unsupported`; rejected |
| `OPT_OFA_DISABLE` | Direct width 1; individual physical coordinate 0%; set `{146,371}` 88% | Direct option/defect/status evidence validates the pair but cannot name one bit | Observation alias only; rejected individually |

Certainty always applies to the narrow statement in its row. For example, the
ECC and SW_SMC row aliases are 99% certain while their complete recipes are
not; the media pair is 88% certain as an unordered set and 0% certain per
individual feature. No target is derived from a missing register by defaulting
it to zero.

### Compiler/runtime component policy

The v2 compiler and runtime keep independent copies of the following physical
decomposition. Components are `(chain, logical block, bit in block, width)` in
least-significant-piece-first order. Exact equality of component list, field
class, direct-register projection and defect requirement is mandatory; a
candidate cannot move a familiar name to a different coordinate.

| Manifest field | Approved components | Class | Required live evidence |
|---|---|---|---|
| `OPT_FBIO_DISABLE` | `(1,5,3,13)`, `(1,6,0,11)` | `disable_mask` | Direct option plus `FBIO_DEFECTIVE` |
| `OPT_FBPA_DISABLE` | `(1,6,11,5)`, `(1,7,0,16)`, `(1,8,0,3)` | `disable_mask` | Direct option plus `FBPA_DEFECTIVE` |
| `OPT_FBP_DISABLE` | `(1,19,3,12)` | `disable_mask` | Direct option plus `FBP_DEFECTIVE` |
| `OPT_ROP_L2_DISABLE` | `(1,19,15,1)`, `(1,20,0,16)`, `(1,21,0,7)` | `disable_mask` | Direct option plus `ROP_L2_DEFECTIVE` |
| `OPT_GPC_DISABLE` | `(1,8,3,8)` | `disable_mask` | Direct option plus `GPC_DEFECTIVE` |
| `OPT_NVDEC_DISABLE` | `(1,8,11,5)` | `disable_mask` | Direct option plus `OPT_NVDEC_DEFECTIVE` |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT146` | `(1,9,2,1)` | `paired_disable_candidate` | Both direct defects exactly zero and pair membership |
| `OPT_TPC_GPC0_DISABLE` | `(1,11,11,5)`, `(1,12,0,3)` | `disable_mask` | Direct option plus `TPC_GPC0_DEFECTIVE` |
| `OPT_TPC_GPC1_DISABLE` | `(1,12,3,8)` | `disable_mask` | Direct option plus `TPC_GPC1_DEFECTIVE` |
| `OPT_TPC_GPC2_DISABLE` | `(1,12,11,5)`, `(1,13,0,3)` | `disable_mask` | Direct option plus `TPC_GPC2_DEFECTIVE` |
| `OPT_TPC_GPC3_DISABLE` | `(1,13,3,8)` | `disable_mask` | Direct option plus `TPC_GPC3_DEFECTIVE` |
| `OPT_TPC_GPC4_DISABLE` | `(1,13,11,5)`, `(1,14,0,3)` | `disable_mask` | Direct option plus `TPC_GPC4_DEFECTIVE` |
| `OPT_TPC_GPC5_DISABLE` | `(1,14,3,8)` | `disable_mask` | Direct option plus `TPC_GPC5_DEFECTIVE` |
| `OPT_TPC_GPC6_DISABLE` | `(1,14,11,5)`, `(1,15,0,3)` | `disable_mask` | Direct option plus `TPC_GPC6_DEFECTIVE` |
| `OPT_TPC_GPC7_DISABLE` | `(1,15,3,8)` | `disable_mask` | Direct option plus `TPC_GPC7_DEFECTIVE` |
| `OPT_PES_DISABLE` | `(1,15,11,5)`, `(1,16,0,16)`, `(1,17,0,3)` | `disable_mask` | Direct option plus `PES_DEFECTIVE` |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT371` | `(1,23,3,1)` | `paired_disable_candidate` | Both direct defects exactly zero and pair membership |
| `OPT_SYS_PIPE_DISABLE` | `(1,23,4,8)` | `disable_mask` | Direct option plus `SYS_PIPE_DEFECTIVE` |
| `OPT_PCIE_DEVIDA` | `(2,37,1,15)`, `(2,38,0,1)` | `identity` | Direct ID A; exact paired target `0x20b5` |
| `OPT_PCIE_DEVIDB` | `(2,38,1,15)`, `(2,39,0,1)` | `identity` | Direct ID B; exact paired target `0x20b5` |
| `OPT_DISABLE_GEN3_SPEED` | `(2,39,9,1)` | `disable_bit` | Direct field bit 0 |
| `PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE` | `(2,47,3,1)` | `disable_bit` | Direct MAGIC_D bit 25 |
| `OPT_PCIE_BOOT_GEN23_DISABLE` | `(2,49,6,1)` | `disable_bit` | Direct field bit 0 |
| `OPT_PCIE_BOOT_GEN3_DISABLE` | `(2,49,7,1)` | `disable_bit` | Direct field bit 0 |

The global `OPT_NVLINK_DISABLE` projection `(1,23,0,3)` exists only in the
compiler's non-executable crosscheck table. It is deliberately absent from the
approved field table and the runtime field policy. IFF, conventional and
unknown-coordinate catalog entries are absent from both policies rather than
being represented by placeholder coordinates.

### Supporting observations that never become implicit writes

The plan frontend binds more than the candidate coordinate table. These are
the exact evidence families consumed by each goal and explain why an otherwise
plausible field target can still be blocked:

| Goal | Bound evidence families |
|---|---|
| `persistent_dsov0` | DSOV and EN_SW state; secure-resense gate; program enable/disable/bypass and secured-burn state; CRC/FUSECTRL; IFF software-fusing/record state; FUSEWDATA/FUSECTRL/OPT privilege masks |
| `fuse_write_eligibility` | FUSECTRL/DEBUGCTRL; program enable/disable/bypass/DSOV; all three write/control privilege masks; secure WDATA/resense and secured-burn gates |
| `80gb_hbm` | FBIO, FBPA, FBP and ROP/L2 option/CP/defect planes; FBIO/FBPA/FBP and FB-config/half-FBPA/half-LTC status |
| `more_sms` | GPC/PES option/CP/defect/status; TPC GPC0–7 option/reconfig/CP/defect/control/reconfig-override/disable-extend/status; TPC/reconfig/floorsweep privilege masks |
| `pcie_gen4_x16` | Four isolated speed gates plus PCIe lane option/CP/control/status |
| `nvlink` | Global option/CP/defect/status, per-link option/control/status, global control, PTOP count, NVLIPT reset-condition/sequence and secure mask/buffer/physical-damage gates |
| `ecc` | Conventional enable/control, FB config, three feature-override/readout domains, override privilege, FBPA ECC control and secure feature/FBPA/SRAM/PLTS ECC gates |
| `mig` | Conventional SW_SMC, SYS_PIPE option/CP/defect/status, GPC planes and FBHUB bottom/middle/boundary memory partitions |
| `nvdec_nvjpg` | NVDEC/NVJPG/NVENC/OFA option/CP/defect/control/status and PTOP inventory where defined; HEVC/VP8-VP9, throttle, HS-disable, ucode-revision, PKC/security and NVDEC FPF versions 1–16 |
| `a100_identity_vbios` | Both redundant fused IDs; signed VBIOS compatibility and recovery remain external preconditions |
| `oc_frequencies` | GPC2CLK cap; ATE Vmin; CP/sample/SRAM-Vmin/SPEEDO bins and revisions; FPF overclock info; DP/FFMA/FMLA16/FMLA32/IMLA0–4 selectors; SS0/SS1 override registers |

CP, defect, status, control, privilege, secure and firmware-revision fields are
not inferred physical fuse coordinates. The planner never turns an observation
family into a write merely because another public tool names it or because a
direct MMIO alias exists.

## Candidate field coordinates

Bits below are logical concatenated chain bits; a field may cross 16-bit logical
blocks. “Manifest” means eligible only when selected by one of the seven exact
profile allowlists and all other gates pass.

| Field | Chain | Start bit | Width | Target basis | Manifest status |
|---|---:|---:|---:|---|---|
| `OPT_FBIO_DISABLE` | 1 | 83 | 24 | normalized/direct `FBIO_DEFECTIVE` | eligible |
| `OPT_FBPA_DISABLE` | 1 | 107 | 24 | normalized/direct `FBPA_DEFECTIVE` | eligible |
| `OPT_GPC_DISABLE` | 1 | 131 | 8 | normalized/direct `GPC_DEFECTIVE` | eligible |
| `OPT_NVDEC_DISABLE` | 1 | 139 | 5 | `OPT_NVDEC_DEFECTIVE` | experimental eligible |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT146` | 1 | 146 | 1 | paired direct defects | experimental, inseparable |
| `OPT_TPC_GPC0_DISABLE` | 1 | 187 | 8 | normalized/direct GPC0 TPC defect | eligible |
| `OPT_TPC_GPC1_DISABLE` | 1 | 195 | 8 | normalized/direct GPC1 TPC defect | eligible |
| `OPT_TPC_GPC2_DISABLE` | 1 | 203 | 8 | normalized/direct GPC2 TPC defect | eligible |
| `OPT_TPC_GPC3_DISABLE` | 1 | 211 | 8 | normalized/direct GPC3 TPC defect | eligible |
| `OPT_TPC_GPC4_DISABLE` | 1 | 219 | 8 | normalized/direct GPC4 TPC defect | eligible |
| `OPT_TPC_GPC5_DISABLE` | 1 | 227 | 8 | normalized/direct GPC5 TPC defect | eligible |
| `OPT_TPC_GPC6_DISABLE` | 1 | 235 | 8 | normalized/direct GPC6 TPC defect | eligible |
| `OPT_TPC_GPC7_DISABLE` | 1 | 243 | 8 | normalized/direct GPC7 TPC defect | eligible |
| `OPT_PES_DISABLE` | 1 | 251 | 24 | normalized/direct `PES_DEFECTIVE` | eligible |
| `OPT_FBP_DISABLE` | 1 | 307 | 12 | normalized/direct `FBP_DEFECTIVE` | eligible |
| `OPT_ROP_L2_DISABLE` | 1 | 319 | 24 | normalized/direct `ROP_L2_DEFECTIVE` | eligible |
| `OPT_NVLINK_DISABLE` | 1 | 368 | 3 | global IOCTRL-group defect projection | **offline crosscheck/candidate only** |
| `NVJPG_OFA_DISABLE_CANDIDATE_BIT371` | 1 | 371 | 1 | paired direct defects | experimental, inseparable |
| `OPT_SYS_PIPE_DISABLE` | 1 | 372 | 8 | `SYS_PIPE_DEFECTIVE` | experimental eligible |
| `OPT_PCIE_DEVIDA` | 2 | 593 | 16 | `0x20b5` | eligible, paired identity commit |
| `OPT_PCIE_DEVIDB` | 2 | 609 | 16 | `0x20b5` | eligible, paired identity commit |

The compiler retains the `OPT_NVLINK_DISABLE` projection in a separate
non-executable crosscheck table so it can authenticate old exact-current
artifacts. It is absent from both execution field allowlists. Chain-1 bits
368–370 are global IOCTRL group disables; they are not the 12-bit
`OPT_PERLINK_DISABLE` physical map.

On all four retained cards the exact observed register condition was: global
option/status `0x7`, global CP/control/defect `0x0`; per-link
option/control/status `0x0`; and PTOP at `0x0002246c` reports twelve links.
Per-link zero therefore indicates that no
per-link disable change is currently called for. It does **not** prove that any
link is routed, powered, terminated, discoverable by firmware, trained, or able
to carry peer traffic. The global-group target remains offline-only. The newest
community-wiki revision (`NVLink`, oldid 2488) reports a one-trained-link and
power-bridge result, but provides no sealed numeric capture that can authorize
or parameterize a per-card XRAY plan.

### Isolated PCIe gates

| Field | Coordinate | Live projection | Certainty | Manifest status |
|---|---|---|---:|---|
| `OPT_DISABLE_GEN3_SPEED` | chain 2, block 39, bit 9 | option bit 0 | 99% | eligible |
| `PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE` | chain 2, block 47, bit 3 | MAGIC_D bit 25 | 97% | eligible |
| `OPT_PCIE_BOOT_GEN23_DISABLE` | chain 2, block 49, bit 6 | option bit 0 | 96% | eligible |
| `OPT_PCIE_BOOT_GEN3_DISABLE` | chain 2, block 49, bit 7 | option bit 0 | 96% | eligible |

The isolated profile clears only those bits while preserving co-resident bits.
The empirical whole-word transformations block 47 `0x0051→0x0000` and block 46
`0xa000→0x8000` remain offline-only because they contain unmapped/co-resident
policy.

## Candidate profiles and exact stage order

All stage names and order below come directly from `PROFILE_SPECS`.

### Seven manifest/runtime-eligible profiles

1. `specimen-80gb-hbm`
   - `memory-prerequisites`: `OPT_FBIO_DISABLE`, `OPT_FBPA_DISABLE`,
     `OPT_ROP_L2_DISABLE`.
   - `memory-activation`: `OPT_FBP_DISABLE`.
   - The historical name does not force 80 GiB; the target is the specimen's
     calculated 64/80/96-GiB defect ceiling.

2. `specimen-more-sm`
   - `rop-l2-dependency`: `OPT_ROP_L2_DISABLE`.
   - `compute-prerequisites`: all eight `OPT_TPC_GPCn_DISABLE` fields and
     `OPT_PES_DISABLE`.
   - `compute-activation`: `OPT_GPC_DISABLE` last.

3. `xray-common-permanent-topology`
   - `memory-and-shared-prerequisites`.
   - `compute-prerequisites`.
   - `memory-activation`.
   - `compute-activation`.
   - This is the combined memory/compute segment; it deliberately stops before
     PCIe, media, identity or ROM migration.

4. `specimen-pcie-gen4-isolated`
   - `pcie-isolated-gates`: the four isolated PCIe bits in the table above.

5. `specimen-a100-20b5-identity`
   - `identity-last`: both redundant device-ID fields.

6. `specimen-sys-pipe-to-defect-experimental`
   - `sys-pipe-experimental`: `OPT_SYS_PIPE_DISABLE` only.

7. `specimen-media-mig-to-defect-experimental`
   - `mig-system-pipe-to-defect`: `OPT_SYS_PIPE_DISABLE`.
   - `nvdec-to-defect`: `OPT_NVDEC_DISABLE`.
   - `nvjpg-ofa-inseparable-pair`: both candidate pair bits together.
   - This models reported prerequisites; it does not prove MIG/media
     sufficiency.

### Offline candidates categorically excluded from manifests

- `specimen-nvlink-global` / `nvlink-global-group`: builds an offline
  three-global-group comparison, but the compiler and runtime reject the
  profile and field. It is not per-link NVLink support.
- `specimen-pcie-gen4-a100-exact`: `pcie-isolated-gates`, then
  `pcie-a100-exact-block47`, then `pcie-a100-exact-block46`. Excluded because
  the whole-word steps are empirical and include unmapped bits.
- `public-corrected-cmpglitch-intent`: `memory-prerequisites`,
  `memory-activation`, `compute-prerequisites`, `compute-activation`,
  `pcie-isolated-gates`, `pcie-a100-exact-block47`,
  `pcie-a100-exact-block46`, then `identity-last`.
  It preserves and corrects public intent for review; it is not a manifest.
- `public-corrected-dstoro-intent`: memory/compute and isolated PCIe,
  then `pcie-a100-exact-block46-dstoro-order` and
  `pcie-a100-exact-block47-dstoro-order`. It preserves source order for
  comparison and omits identity.
- `xray-best-evidence-a100-conversion`: memory/compute and isolated
  PCIe, `pcie-a100-exact-block47`, `pcie-a100-exact-block46`, and
  `identity-last`. It is a research composition, not an executable superset.
  Global IOCTRL/NVLink is deliberately absent from every broad composite and
  exists only as the explicit `specimen-nvlink-global` offline comparison.

The alias `specimen-hbm-to-defect-ceiling` resolves to `specimen-80gb-hbm`.

### Six zero-operation review-only profiles

- `review-ss0-iff`: IFF target `FEATURE_OVERRIDE_SM_SPEED_SELECT` at
  `0x0082381c`; reported words `0x082381fd, 0x88888888`, reported rows 484/485.
  Coordinate/value evidence is 85%; portable placement is 0%. SS0 covers
  IMLA0-3/FMLA/FFMA/DP override policy.
- `review-ss1-iff`: IFF target `FEATURE_OVERRIDE_SM_SPEED_SELECT_1` at
  `0x00823820`; reported words `0x0823823d, 0x00000008`, reported rows 486/487.
  Coordinate/value evidence is 85%; portable placement is 0%; row 487 is
  quarantined. SS1 is IMLA4, not FP64.
- `review-dp-conventional`: conventional primary/redundant rows 6/7,
  `OPT_SM_SPEED_SELECT_DP` bit 26 (99%) and proposed
  `OPT_SM_SPEED_SELECT_DP_CONTROL` undo/control bit 27 (65%). No isolated cold
  proof or complete RIR/sense rule exists.
- `review-ecc-conventional`: conventional primary/redundant rows 6/7,
  `OPT_ECC_EN` bit 28 and `OPT_ECC_EN_CONTROL` bit 29. Coordinate certainty is
  99%; complete-enable sufficiency is 45%. Datapath, firmware, RIR and error
  injection remain unresolved.
- `review-nvenc-unsupported`: direct option/CP/control/defect/status width is
  two bits and the PTOP instance-count field is five bits, but the count is
  inventory only; physical-coordinate certainty and supported permanent target are 0%.
  Production A100 absence is 99% and the retained four-card unusable-engine
  inference is 97%; there is no populated-engine evidence to justify a plan.
- `review-nvlink-per-link-unsupported`: the direct
  `OPT_PERLINK_DISABLE` width is twelve bits, but physical-coordinate certainty
  is 0%. Global bits 368–370 must not be substituted.

These six entries always have `operation_count=0`, no stages, and explicit
`compiler_eligible=false` / `runtime_eligible=false` metadata.

## Compiler/runtime safety boundary

The compiler and runtime independently pin exactly the same seven profile IDs,
stage tuples, field sets and field components. The documentation test compares
them for exact equality. The runtime also reconstructs the sealed manifest with
the pinned compiler bytes and canonical-compares the result.

For every accepted manifest:

1. Candidate source, native capture, cold MAIN and cold RIR are hash-bound.
2. Full-domain BDF/identity, exact current 512-row MAIN image and eight-word RIR
   vector must match.
3. Every selected target must equal the independently approved coordinates and
   current direct defect evidence.
4. Every target row must be physically blank and below the measured boundary.
5. Every prefix must pass replay and preserve active IFF and RIR.
6. Undeclared and co-resident logical bits must remain unchanged.
7. The manifest is sealed with a canonical content hash and source hashes.
8. Runtime policy, live state, authorization receipt, journal and immediate
   readback remain separate gates.

Default eligible profiles permit a one-operation minimum commit group and an
optional pause after the first row. Identity and the paired media profile both
require a minimum two-operation commit group, forbid test-row-only mode, and
forbid a continuation pause after the first member because the intermediate
identity/pair state is explicitly unqualified. A crash can still leave a
physical prefix; “group” is not atomic OTP.

## Feature coverage and remaining gaps

| Target | First-class representation | Physical-plan status | Manifest status | Required follow-up |
|---|---|---|---|---|
| HBM 64/80/96 GiB | `specimen-80gb-hbm` | defect-preserving FF candidate | eligible | cold training, full-address memory test, ECC/thermal telemetry |
| SMs up to 128 | `specimen-more-sm` | defect-preserving FF candidate | eligible | cold GSP/VBIOS enumeration, per-class stress, possible EFI/driver floorsweep |
| PCIe Gen4 | `specimen-pcie-gen4-isolated` | four isolated gates | eligible | cold endpoint/root-port training and stability; no lane-width claim |
| PCIe exact A100 words | `specimen-pcie-gen4-a100-exact` | empirical offline comparison | rejected | per-card volatile differential and mapped co-resident bits |
| Device ID `20b5` | `specimen-a100-20b5-identity` | paired identity candidate | eligible | signed compatible ROM, SPI recovery, post-ID read and driver validation |
| Global IOCTRL groups | `specimen-nvlink-global` | offline comparison only | **rejected** | do not call it per-link NVLink |
| Per-link NVLink | `review-nvlink-per-link-unsupported` | unknown coordinate | rejected | physical map, bridge/board routing, ROM/GSP policy, training and traffic |
| MIG prerequisite | media/MIG and SYS_PIPE profiles | experimental SYS_PIPE FF candidate | eligible experimentally | SW_SMC, GPC/memory ownership, reset/channel, firmware/profile validation |
| NVDEC | media/MIG profile | five-bit defect-gated candidate | eligible experimentally | HS/power/PTOP/ucode/API validation |
| NVJPG/OFA | media/MIG profile | unordered inseparable `{146,371}` pair | eligible experimentally | individual attribution remains unknown; both defects must be zero |
| NVENC | `review-nvenc-unsupported` | no coordinate or population evidence | rejected | no supported unlock target |
| SS0 | `review-ss0-iff` | IFF review only | rejected | portable IFF allocator/value and cold compute proof |
| SS1/IMLA4 | `review-ss1-iff` | IFF review only | rejected | portable IFF allocator/value and cold IMLA4 proof |
| FP64/DP | `review-dp-conventional` | conventional review only | rejected | bit-27 semantics, primary/redundant/RIR proof, cold benchmark |
| ECC | `review-ecc-conventional` | conventional review only | rejected | sense/control semantics, all ECC domains, cold injection and counters |
| OC/frequencies | frontend inventory only | no portable target | rejected | reliability-qualified voltage/bin/clock model |
| Persistent DSOV=0 | frontend research goal | no evidenced cold-persistent mechanism | rejected | prove immutable early consumer and fundamental-reset persistence |

## Sequencing models

XRAY separates permanent segments so that a cold validation can occur after
each irreversible domain. A defensible high-level review sequence is:

1. Exact-current native MAIN/RIR/register capture and specimen ceiling.
2. Memory/compute topology segment; cold boot and validate HBM/SM topology.
3. Optional isolated PCIe segment; cold train and validate.
4. Optional experimental media/SYS_PIPE segment; cold enumerate and validate.
5. Identity last, only with signed-ROM compatibility and external SPI recovery
   already rehearsed.

Two post-topology branches remain intentionally distinct:

- Keep CMP identity/VBIOS: exposed SM topology may work with CMP policy, while
  PCIe Gen4 can still need software/driver treatment.
- Migrate to A100 identity/VBIOS: PCIe policy may improve, but SM enumeration
  can still need EFI/driver floorsweep within the defect ceiling.

The combined public/best-evidence profiles exist to compare those intentions,
not to bypass the segmented cold-validation sequence. SS0, SS1, DP, ECC,
per-link NVLink, NVENC and a complete MIG/media solution are not silently
inserted into either branch.

## Mechanical synchronization

`tests/test_unlock_plan_doc.py` imports the live frontend, candidate planner,
compiler and backend and hashes a normalized contract containing:

- tool/backend versions;
- all goal IDs and planning models;
- every row and plan input/output format;
- every candidate profile, stage, field and PCIe bit;
- compiler and runtime profile/field allowlists; and
- non-executable IFF/conventional/unknown catalog entries.

The test also requires every ID and stage token to occur in this document,
asserts the 18/6/7 candidate/review/execution counts, checks compiler/runtime
parity, and proves the global NVLink candidate remains outside both execution
allowlists. `tests/test_candidate_bundle.py` separately proves format
round-trips, strict duplicate-key and one-record JSONL handling, text markers,
private create-only files, symlink refusal and CLI input-format misuse.

If this digest fails, update code and prose from the same reviewed change. Do
not refresh the digest merely to silence the test.

## Canonical code-contract appendix

The JSON below is generated directly from the same frontend, candidate,
compiler and independent runtime objects used by the test. The test parses it
back and requires exact object equality, including profile/stage/field
associations—not just token presence or a matching digest. Regenerate it with
`python3 tools/update_unlock_plan_contract.py`; `--check` is the non-mutating
release/PR gate.

<!-- XRAY-PLAN-CONTRACT-JSON:BEGIN -->

```json
{
  "candidate_field_specs": {
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": {
      "bit": 146,
      "chain": 1,
      "class": "paired_disable_candidate",
      "comparison_registers": [
        "OPT_NVJPG_DISABLE",
        "OPT_OFA_DISABLE"
      ],
      "defects": [
        "OPT_NVJPG_DEFECTIVE",
        "OPT_OFA_DEFECTIVE"
      ],
      "domain": "media_pair",
      "live_register": null,
      "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
      "width": 1
    },
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": {
      "bit": 371,
      "chain": 1,
      "class": "paired_disable_candidate",
      "comparison_registers": [
        "OPT_NVJPG_DISABLE",
        "OPT_OFA_DISABLE"
      ],
      "defects": [
        "OPT_NVJPG_DEFECTIVE",
        "OPT_OFA_DEFECTIVE"
      ],
      "domain": "media_pair",
      "live_register": null,
      "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
      "width": 1
    },
    "OPT_FBIO_DISABLE": {
      "bit": 83,
      "chain": 1,
      "class": "disable_mask",
      "defect": "FBIO_DEFECTIVE",
      "domain": "memory",
      "width": 24
    },
    "OPT_FBPA_DISABLE": {
      "bit": 107,
      "chain": 1,
      "class": "disable_mask",
      "defect": "FBPA_DEFECTIVE",
      "domain": "memory",
      "width": 24
    },
    "OPT_FBP_DISABLE": {
      "bit": 307,
      "chain": 1,
      "class": "disable_mask",
      "defect": "FBP_DEFECTIVE",
      "domain": "memory",
      "width": 12
    },
    "OPT_GPC_DISABLE": {
      "bit": 131,
      "chain": 1,
      "class": "disable_mask",
      "defect": "GPC_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_NVDEC_DISABLE": {
      "bit": 139,
      "chain": 1,
      "class": "disable_mask",
      "defect": "OPT_NVDEC_DEFECTIVE",
      "domain": "media_decode",
      "width": 5
    },
    "OPT_NVLINK_DISABLE": {
      "bit": 368,
      "chain": 1,
      "class": "disable_mask",
      "defect": "NVLINK_DEFECTIVE",
      "domain": "nvlink_global_group",
      "width": 3
    },
    "OPT_PCIE_DEVIDA": {
      "bit": 593,
      "chain": 2,
      "class": "identity",
      "domain": "identity",
      "width": 16
    },
    "OPT_PCIE_DEVIDB": {
      "bit": 609,
      "chain": 2,
      "class": "identity",
      "domain": "identity",
      "width": 16
    },
    "OPT_PES_DISABLE": {
      "bit": 251,
      "chain": 1,
      "class": "disable_mask",
      "defect": "PES_DEFECTIVE",
      "domain": "compute",
      "width": 24
    },
    "OPT_ROP_L2_DISABLE": {
      "bit": 319,
      "chain": 1,
      "class": "disable_mask",
      "defect": "ROP_L2_DEFECTIVE",
      "domain": "memory_compute_dependency",
      "width": 24
    },
    "OPT_SYS_PIPE_DISABLE": {
      "bit": 372,
      "chain": 1,
      "class": "disable_mask",
      "defect": "SYS_PIPE_DEFECTIVE",
      "domain": "system_topology",
      "width": 8
    },
    "OPT_TPC_GPC0_DISABLE": {
      "bit": 187,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC0_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC1_DISABLE": {
      "bit": 195,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC1_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC2_DISABLE": {
      "bit": 203,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC2_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC3_DISABLE": {
      "bit": 211,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC3_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC4_DISABLE": {
      "bit": 219,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC4_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC5_DISABLE": {
      "bit": 227,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC5_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC6_DISABLE": {
      "bit": 235,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC6_DEFECTIVE",
      "domain": "compute",
      "width": 8
    },
    "OPT_TPC_GPC7_DISABLE": {
      "bit": 243,
      "chain": 1,
      "class": "disable_mask",
      "defect": "TPC_GPC7_DEFECTIVE",
      "domain": "compute",
      "width": 8
    }
  },
  "candidate_live_input_formats": [
    "auto",
    "xray-level2-json"
  ],
  "candidate_output_formats": [
    "json",
    "jsonl",
    "text",
    "canonical-json-bin"
  ],
  "candidate_pcie_bit_specs": {
    "OPT_DISABLE_GEN3_SPEED": {
      "bit_in_block": 9,
      "block": 39,
      "certainty_percent": 99,
      "chain": 2,
      "live_bit": 0,
      "live_register": "OPT_DISABLE_GEN3_SPEED",
      "reason": "2026-11-19 isolated cold-boot experiment: clearing block-39 bit 1 alone left the option set; a later record restored bit 1 and cleared bit 9, after which the option read zero"
    },
    "OPT_PCIE_BOOT_GEN23_DISABLE": {
      "bit_in_block": 6,
      "block": 49,
      "certainty_percent": 96,
      "chain": 2,
      "live_bit": 0,
      "live_register": "OPT_PCIE_BOOT_GEN23_DISABLE",
      "reason": "differential map and direct option field agree"
    },
    "OPT_PCIE_BOOT_GEN3_DISABLE": {
      "bit_in_block": 7,
      "block": 49,
      "certainty_percent": 96,
      "chain": 2,
      "live_bit": 0,
      "live_register": "OPT_PCIE_BOOT_GEN3_DISABLE",
      "reason": "differential map and direct option field agree"
    },
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": {
      "bit_in_block": 3,
      "block": 47,
      "certainty_percent": 97,
      "chain": 2,
      "live_bit": 25,
      "live_register": "OPT_PCIE_MAGIC_BITS_D",
      "reason": "differential map and direct GA100 MAGIC_D field agree"
    }
  },
  "candidate_physical_input_formats": [
    "auto",
    "xray-native-level3-json"
  ],
  "compiler_field_models": {
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": [
      [
        [
          1,
          9,
          2,
          1
        ]
      ],
      null,
      null,
      null,
      "paired_disable_candidate"
    ],
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": [
      [
        [
          1,
          23,
          3,
          1
        ]
      ],
      null,
      null,
      null,
      "paired_disable_candidate"
    ],
    "OPT_DISABLE_GEN3_SPEED": [
      [
        [
          2,
          39,
          9,
          1
        ]
      ],
      "OPT_DISABLE_GEN3_SPEED",
      0,
      null,
      "disable_bit"
    ],
    "OPT_FBIO_DISABLE": [
      [
        [
          1,
          5,
          3,
          13
        ],
        [
          1,
          6,
          0,
          11
        ]
      ],
      "OPT_FBIO_DISABLE",
      null,
      "FBIO_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_FBPA_DISABLE": [
      [
        [
          1,
          6,
          11,
          5
        ],
        [
          1,
          7,
          0,
          16
        ],
        [
          1,
          8,
          0,
          3
        ]
      ],
      "OPT_FBPA_DISABLE",
      null,
      "FBPA_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_FBP_DISABLE": [
      [
        [
          1,
          19,
          3,
          12
        ]
      ],
      "OPT_FBP_DISABLE",
      null,
      "FBP_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_GPC_DISABLE": [
      [
        [
          1,
          8,
          3,
          8
        ]
      ],
      "OPT_GPC_DISABLE",
      null,
      "GPC_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_NVDEC_DISABLE": [
      [
        [
          1,
          8,
          11,
          5
        ]
      ],
      "OPT_NVDEC_DISABLE",
      null,
      "OPT_NVDEC_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_PCIE_BOOT_GEN23_DISABLE": [
      [
        [
          2,
          49,
          6,
          1
        ]
      ],
      "OPT_PCIE_BOOT_GEN23_DISABLE",
      0,
      null,
      "disable_bit"
    ],
    "OPT_PCIE_BOOT_GEN3_DISABLE": [
      [
        [
          2,
          49,
          7,
          1
        ]
      ],
      "OPT_PCIE_BOOT_GEN3_DISABLE",
      0,
      null,
      "disable_bit"
    ],
    "OPT_PCIE_DEVIDA": [
      [
        [
          2,
          37,
          1,
          15
        ],
        [
          2,
          38,
          0,
          1
        ]
      ],
      "OPT_PCIE_DEVIDA",
      null,
      null,
      "identity"
    ],
    "OPT_PCIE_DEVIDB": [
      [
        [
          2,
          38,
          1,
          15
        ],
        [
          2,
          39,
          0,
          1
        ]
      ],
      "OPT_PCIE_DEVIDB",
      null,
      null,
      "identity"
    ],
    "OPT_PES_DISABLE": [
      [
        [
          1,
          15,
          11,
          5
        ],
        [
          1,
          16,
          0,
          16
        ],
        [
          1,
          17,
          0,
          3
        ]
      ],
      "OPT_PES_DISABLE",
      null,
      "PES_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_ROP_L2_DISABLE": [
      [
        [
          1,
          19,
          15,
          1
        ],
        [
          1,
          20,
          0,
          16
        ],
        [
          1,
          21,
          0,
          7
        ]
      ],
      "OPT_ROP_L2_DISABLE",
      null,
      "ROP_L2_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_SYS_PIPE_DISABLE": [
      [
        [
          1,
          23,
          4,
          8
        ]
      ],
      "OPT_SYS_PIPE_DISABLE",
      null,
      "SYS_PIPE_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC0_DISABLE": [
      [
        [
          1,
          11,
          11,
          5
        ],
        [
          1,
          12,
          0,
          3
        ]
      ],
      "OPT_TPC_GPC0_DISABLE",
      null,
      "TPC_GPC0_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC1_DISABLE": [
      [
        [
          1,
          12,
          3,
          8
        ]
      ],
      "OPT_TPC_GPC1_DISABLE",
      null,
      "TPC_GPC1_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC2_DISABLE": [
      [
        [
          1,
          12,
          11,
          5
        ],
        [
          1,
          13,
          0,
          3
        ]
      ],
      "OPT_TPC_GPC2_DISABLE",
      null,
      "TPC_GPC2_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC3_DISABLE": [
      [
        [
          1,
          13,
          3,
          8
        ]
      ],
      "OPT_TPC_GPC3_DISABLE",
      null,
      "TPC_GPC3_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC4_DISABLE": [
      [
        [
          1,
          13,
          11,
          5
        ],
        [
          1,
          14,
          0,
          3
        ]
      ],
      "OPT_TPC_GPC4_DISABLE",
      null,
      "TPC_GPC4_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC5_DISABLE": [
      [
        [
          1,
          14,
          3,
          8
        ]
      ],
      "OPT_TPC_GPC5_DISABLE",
      null,
      "TPC_GPC5_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC6_DISABLE": [
      [
        [
          1,
          14,
          11,
          5
        ],
        [
          1,
          15,
          0,
          3
        ]
      ],
      "OPT_TPC_GPC6_DISABLE",
      null,
      "TPC_GPC6_DEFECTIVE",
      "disable_mask"
    ],
    "OPT_TPC_GPC7_DISABLE": [
      [
        [
          1,
          15,
          3,
          8
        ]
      ],
      "OPT_TPC_GPC7_DISABLE",
      null,
      "TPC_GPC7_DEFECTIVE",
      "disable_mask"
    ],
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": [
      [
        [
          2,
          47,
          3,
          1
        ]
      ],
      "OPT_PCIE_MAGIC_BITS_D",
      25,
      null,
      "disable_bit"
    ]
  },
  "compiler_profiles": {
    "specimen-80gb-hbm": {
      "fields": [
        "OPT_FBIO_DISABLE",
        "OPT_FBPA_DISABLE",
        "OPT_FBP_DISABLE",
        "OPT_ROP_L2_DISABLE"
      ],
      "stages": [
        "memory-prerequisites",
        "memory-activation"
      ]
    },
    "specimen-a100-20b5-identity": {
      "fields": [
        "OPT_PCIE_DEVIDA",
        "OPT_PCIE_DEVIDB"
      ],
      "stages": [
        "identity-last"
      ]
    },
    "specimen-media-mig-to-defect-experimental": {
      "fields": [
        "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
        "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
        "OPT_NVDEC_DISABLE",
        "OPT_SYS_PIPE_DISABLE"
      ],
      "stages": [
        "mig-system-pipe-to-defect",
        "nvdec-to-defect",
        "nvjpg-ofa-inseparable-pair"
      ]
    },
    "specimen-more-sm": {
      "fields": [
        "OPT_GPC_DISABLE",
        "OPT_PES_DISABLE",
        "OPT_ROP_L2_DISABLE",
        "OPT_TPC_GPC0_DISABLE",
        "OPT_TPC_GPC1_DISABLE",
        "OPT_TPC_GPC2_DISABLE",
        "OPT_TPC_GPC3_DISABLE",
        "OPT_TPC_GPC4_DISABLE",
        "OPT_TPC_GPC5_DISABLE",
        "OPT_TPC_GPC6_DISABLE",
        "OPT_TPC_GPC7_DISABLE"
      ],
      "stages": [
        "rop-l2-dependency",
        "compute-prerequisites",
        "compute-activation"
      ]
    },
    "specimen-pcie-gen4-isolated": {
      "fields": [
        "OPT_DISABLE_GEN3_SPEED",
        "OPT_PCIE_BOOT_GEN23_DISABLE",
        "OPT_PCIE_BOOT_GEN3_DISABLE",
        "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE"
      ],
      "stages": [
        "pcie-isolated-gates"
      ]
    },
    "specimen-sys-pipe-to-defect-experimental": {
      "fields": [
        "OPT_SYS_PIPE_DISABLE"
      ],
      "stages": [
        "sys-pipe-experimental"
      ]
    },
    "xray-common-permanent-topology": {
      "fields": [
        "OPT_FBIO_DISABLE",
        "OPT_FBPA_DISABLE",
        "OPT_FBP_DISABLE",
        "OPT_GPC_DISABLE",
        "OPT_PES_DISABLE",
        "OPT_ROP_L2_DISABLE",
        "OPT_TPC_GPC0_DISABLE",
        "OPT_TPC_GPC1_DISABLE",
        "OPT_TPC_GPC2_DISABLE",
        "OPT_TPC_GPC3_DISABLE",
        "OPT_TPC_GPC4_DISABLE",
        "OPT_TPC_GPC5_DISABLE",
        "OPT_TPC_GPC6_DISABLE",
        "OPT_TPC_GPC7_DISABLE"
      ],
      "stages": [
        "memory-and-shared-prerequisites",
        "compute-prerequisites",
        "memory-activation",
        "compute-activation"
      ]
    }
  },
  "dump_input_formats": [
    "auto",
    "json",
    "jsonl",
    "indexed-text",
    "raw-u32le"
  ],
  "dump_output_formats": [
    "xray-json",
    "xray-jsonl",
    "indexed-text",
    "raw-u32le",
    "ffcalc-json",
    "xpatrik-json",
    "ga100-fuse-re-json",
    "dstoro-projection-json",
    "dstoro-jsonl"
  ],
  "goal_ids": [
    "80gb_hbm",
    "a100_identity_vbios",
    "ecc",
    "fuse_write_eligibility",
    "mig",
    "more_sms",
    "nvdec_nvjpg",
    "nvlink",
    "oc_frequencies",
    "pcie_gen4_x16",
    "persistent_dsov0"
  ],
  "non_executable_catalog": [
    {
      "certainty": 85,
      "certainty_percent": 85,
      "certainty_reason": "The target register identity is strong and one tested campaign reports the exact IFF WRITE_DW pair/value; physical rows 484/485 are recipe-specific placement, not a universal allocation.",
      "execution_supported": false,
      "name": "FEATURE_OVERRIDE_SM_SPEED_SELECT",
      "placement_boundary": "rows are one tested recipe, not a universal allocation",
      "plane": "IFF-candidate",
      "planning_allowed": false,
      "reported_rows": [
        484,
        485
      ],
      "reported_value": 2290649224,
      "reported_words": [
        136544765,
        2290649224
      ],
      "target_address": 8534044
    },
    {
      "certainty": 85,
      "certainty_percent": 85,
      "certainty_reason": "The IMLA4 target register identity is strong and one tested campaign reports the exact IFF WRITE_DW pair/value; physical rows 486/487 are recipe-specific placement, not a universal allocation.",
      "execution_supported": false,
      "name": "FEATURE_OVERRIDE_SM_SPEED_SELECT_1",
      "placement_boundary": "rows are one tested recipe, not a universal allocation",
      "plane": "IFF-candidate",
      "planning_allowed": false,
      "reported_rows": [
        486,
        487
      ],
      "reported_value": 8,
      "reported_words": [
        136544829,
        8
      ],
      "target_address": 8534048
    },
    {
      "certainty": 0,
      "certainty_percent": 0,
      "certainty_reason": "Twelve-bit register identity is direct; no physical FF coordinate is known.",
      "coordinate": null,
      "name": "OPT_PERLINK_DISABLE",
      "plane": "FF",
      "planning_allowed": false,
      "width": 12
    },
    {
      "bit": 28,
      "certainty": 99,
      "certainty_percent": 99,
      "certainty_reason": "Rows 6/7 bit 28 are literal aliases; feature sufficiency is not implied.",
      "name": "OPT_ECC_EN",
      "plane": "conventional",
      "planning_allowed": false,
      "primary_row": 6,
      "redundant_row": 7,
      "width": 1
    },
    {
      "bit": 29,
      "certainty": 99,
      "certainty_percent": 99,
      "certainty_reason": "Rows 6/7 bit 29 are literal aliases; sense semantics remain incomplete.",
      "name": "OPT_ECC_EN_CONTROL",
      "plane": "conventional",
      "planning_allowed": false,
      "primary_row": 6,
      "redundant_row": 7,
      "width": 1
    },
    {
      "bit": 26,
      "certainty": 99,
      "certainty_percent": 99,
      "certainty_reason": "The one-bit DP selector is direct at 0x820224 and aliases row 6/7 bit 26; its reduced/full semantics are directly named.",
      "name": "OPT_SM_SPEED_SELECT_DP",
      "plane": "conventional",
      "planning_allowed": false,
      "primary_row": 6,
      "redundant_row": 7,
      "width": 1
    },
    {
      "bit": 27,
      "candidate_role": "DP control/undo",
      "certainty": 65,
      "certainty_percent": 65,
      "certainty_reason": "Rows 6/7 bit 27 are directly named as DP_CONTROL, but available static specimens leave the bit invariant and the proposed undo behavior has no isolated cold functional proof.",
      "mechanism_confirmed": false,
      "name": "OPT_SM_SPEED_SELECT_DP_CONTROL",
      "plane": "conventional",
      "planning_allowed": false,
      "primary_row": 6,
      "redundant_row": 7,
      "width": 1
    },
    {
      "bit": 11,
      "certainty": 99,
      "certainty_percent": 99,
      "certainty_reason": "Rows 14/15 bit 11 are literal aliases; MIG sufficiency is unproved.",
      "name": "OPT_SW_SMC_DISABLE",
      "plane": "conventional",
      "planning_allowed": false,
      "primary_row": 14,
      "redundant_row": 15,
      "width": 1
    },
    {
      "candidate_coordinate_set": [
        146,
        371
      ],
      "certainty": 0,
      "certainty_percent": 0,
      "certainty_reason": "The set {bit146, bit371} maps to {NVJPG,OFA} in one functional campaign, but neither individual attribution is known; direct defect/status words are required.",
      "coordinate": null,
      "defect_register": "OPT_NVJPG_DEFECTIVE",
      "name": "OPT_NVJPG_DISABLE",
      "paired_with": "OPT_OFA_DISABLE",
      "plane": "FF",
      "planning_allowed": false,
      "set_mapping_certainty_percent": 88,
      "width": 1
    },
    {
      "certainty": 0,
      "certainty_percent": 0,
      "certainty_reason": "Two-bit option exists, but no physical FF coordinate is recovered.",
      "coordinate": null,
      "name": "OPT_NVENC_DISABLE",
      "plane": "FF",
      "planning_allowed": false,
      "width": 2
    },
    {
      "candidate_coordinate_set": [
        146,
        371
      ],
      "certainty": 0,
      "certainty_percent": 0,
      "certainty_reason": "The set {bit146, bit371} maps to {NVJPG,OFA} in one functional campaign, but neither individual attribution is known; direct defect/status words are required.",
      "coordinate": null,
      "defect_register": "OPT_OFA_DEFECTIVE",
      "name": "OPT_OFA_DISABLE",
      "paired_with": "OPT_NVJPG_DISABLE",
      "plane": "FF",
      "planning_allowed": false,
      "set_mapping_certainty_percent": 88,
      "width": 1
    }
  ],
  "planning_models": [
    "evidence-only",
    "public-model",
    "cross-checked-theory",
    "compare"
  ],
  "profile_aliases": {
    "specimen-hbm-to-defect-ceiling": "specimen-80gb-hbm"
  },
  "research_profiles": [
    {
      "id": "specimen-80gb-hbm",
      "intent": "Reduce HBM/FB disable masks to this die's observed defect masks; the computed target can be 64, 80 or 96 GiB.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [
            "OPT_FBIO_DISABLE",
            "OPT_FBPA_DISABLE",
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "memory-prerequisites",
          "pcie_bits": [],
          "purpose": "Prepare FBIO/FBPA/ROP-L2 before exposing more FBPs.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_FBP_DISABLE"
          ],
          "name": "memory-activation",
          "pcie_bits": [],
          "purpose": "Activate only non-defective FBPs after prerequisites.",
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-more-sm",
      "intent": "Reduce compute disable masks to this die's defect masks.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "rop-l2-dependency",
          "pcie_bits": [],
          "purpose": "Address the public GSP-failure dependency before SM activation.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_TPC_GPC0_DISABLE",
            "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE",
            "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE",
            "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE",
            "OPT_TPC_GPC7_DISABLE",
            "OPT_PES_DISABLE"
          ],
          "name": "compute-prerequisites",
          "pcie_bits": [],
          "purpose": "Prepare TPC and PES topology before changing GPC activation.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_GPC_DISABLE"
          ],
          "name": "compute-activation",
          "pcie_bits": [],
          "purpose": "GPC is the final compute activation boundary.",
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "xray-common-permanent-topology",
      "intent": "One exact-current permanent-topology segment: reduce HBM and compute disable masks only to the normalized per-die defect closure, then stop for a cold validation before PCIe or identity work.",
      "model_type": "xray-best-evidence-segment",
      "stages": [
        {
          "fields": [
            "OPT_FBIO_DISABLE",
            "OPT_FBPA_DISABLE",
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "memory-and-shared-prerequisites",
          "pcie_bits": [],
          "purpose": "Program FBIO, FBPA and the shared ROP/L2 dependency before either FBP or GPC becomes less disabled.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_TPC_GPC0_DISABLE",
            "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE",
            "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE",
            "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE",
            "OPT_TPC_GPC7_DISABLE",
            "OPT_PES_DISABLE"
          ],
          "name": "compute-prerequisites",
          "pcie_bits": [],
          "purpose": "Program normalized TPC and 3/3/2 PES closure while the parent GPC activation mask is unchanged.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_FBP_DISABLE"
          ],
          "name": "memory-activation",
          "pcie_bits": [],
          "purpose": "Reduce FBP disable only after every memory companion plane.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_GPC_DISABLE"
          ],
          "name": "compute-activation",
          "pcie_bits": [],
          "purpose": "Reduce GPC disable last, after TPC/PES and shared ROP/L2.",
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-nvlink-global",
      "intent": "Reduce the three-bit global NVLink group mask to the defect mask.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [
            "OPT_NVLINK_DISABLE"
          ],
          "name": "nvlink-global-group",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-pcie-gen4-isolated",
      "intent": "Clear only the mapped/candidate PCIe generation gates.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [],
          "name": "pcie-isolated-gates",
          "pcie_bits": [
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE"
          ],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-pcie-gen4-a100-exact",
      "intent": "Isolated gates plus the reported whole-word A100 MAGIC_D projection.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [],
          "name": "pcie-isolated-gates",
          "pcie_bits": [
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE"
          ],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block47",
          "pcie_bits": [],
          "purpose": "Reported functional sequence first clears the remaining block-47 MAGIC_D projection.",
          "raw_blocks": [
            {
              "block": 47,
              "chain": 2,
              "expected": 81,
              "target": 0
            }
          ]
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block46",
          "pcie_bits": [],
          "purpose": "Reported functional sequence then clears the block-46 empirical bit.",
          "raw_blocks": [
            {
              "block": 46,
              "chain": 2,
              "expected": 40960,
              "target": 32768
            }
          ]
        }
      ]
    },
    {
      "id": "specimen-a100-20b5-identity",
      "intent": "Change both redundant logical device-ID fields to 0x20b5, identity last.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [
            "OPT_PCIE_DEVIDA",
            "OPT_PCIE_DEVIDB"
          ],
          "name": "identity-last",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-sys-pipe-to-defect-experimental",
      "intent": "Reduce SYS_PIPE to its observed defect mask as a separately reviewable experiment.",
      "model_type": "specimen-derived",
      "stages": [
        {
          "fields": [
            "OPT_SYS_PIPE_DISABLE"
          ],
          "name": "sys-pipe-experimental",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "specimen-media-mig-to-defect-experimental",
      "intent": "Reduce SYS_PIPE and NVDEC disable masks to direct per-die defect masks, then clear the inseparable NVJPG/OFA candidate pair only when both direct defect words are zero. NVENC is not included.",
      "model_type": "specimen-derived-experimental",
      "stages": [
        {
          "fields": [
            "OPT_SYS_PIPE_DISABLE"
          ],
          "name": "mig-system-pipe-to-defect",
          "pcie_bits": [],
          "purpose": "Prepare only non-defective system-pipe instances; this is the reported FF prerequisite for eight-way MIG enumeration.",
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_NVDEC_DISABLE"
          ],
          "name": "nvdec-to-defect",
          "pcie_bits": [],
          "purpose": "Enable only decoder instances not marked defective by the direct GA100 defect word.",
          "raw_blocks": []
        },
        {
          "fields": [
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT371"
          ],
          "name": "nvjpg-ofa-inseparable-pair",
          "pcie_bits": [],
          "purpose": "Clear both candidate bits as one atomic planning unit; the individual NVJPG-versus-OFA attribution remains unknown.",
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "public-corrected-cmpglitch-intent",
      "intent": "Rebuild the reported conversion intent per specimen with corrected dependencies and rows.",
      "model_type": "public-corrected",
      "public_source": "cmpglitch-reported-card",
      "stages": [
        {
          "fields": [
            "OPT_FBIO_DISABLE",
            "OPT_FBPA_DISABLE",
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "memory-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_FBP_DISABLE"
          ],
          "name": "memory-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_TPC_GPC0_DISABLE",
            "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE",
            "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE",
            "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE",
            "OPT_TPC_GPC7_DISABLE",
            "OPT_PES_DISABLE"
          ],
          "name": "compute-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_GPC_DISABLE"
          ],
          "name": "compute-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-isolated-gates",
          "pcie_bits": [
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE"
          ],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block47",
          "pcie_bits": [],
          "purpose": "Reported functional sequence first clears the remaining block-47 MAGIC_D projection.",
          "raw_blocks": [
            {
              "block": 47,
              "chain": 2,
              "expected": 81,
              "target": 0
            }
          ]
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block46",
          "pcie_bits": [],
          "purpose": "Reported functional sequence then clears the block-46 empirical bit.",
          "raw_blocks": [
            {
              "block": 46,
              "chain": 2,
              "expected": 40960,
              "target": 32768
            }
          ]
        },
        {
          "fields": [
            "OPT_PCIE_DEVIDA",
            "OPT_PCIE_DEVIDB"
          ],
          "name": "identity-last",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "public-corrected-dstoro-intent",
      "intent": "Rebuild the public no-identity topology/PCIe intent per specimen with dependency ordering.",
      "model_type": "public-corrected",
      "public_source": "dstoro-plan-2026-11-17",
      "stages": [
        {
          "fields": [
            "OPT_FBIO_DISABLE",
            "OPT_FBPA_DISABLE",
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "memory-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_FBP_DISABLE"
          ],
          "name": "memory-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_TPC_GPC0_DISABLE",
            "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE",
            "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE",
            "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE",
            "OPT_TPC_GPC7_DISABLE",
            "OPT_PES_DISABLE"
          ],
          "name": "compute-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_GPC_DISABLE"
          ],
          "name": "compute-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-isolated-gates",
          "pcie_bits": [
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE"
          ],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block46-dstoro-order",
          "pcie_bits": [],
          "purpose": "Danylo source order: block 46 before block 47.",
          "raw_blocks": [
            {
              "block": 46,
              "chain": 2,
              "expected": 40960,
              "target": 32768
            }
          ]
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block47-dstoro-order",
          "pcie_bits": [],
          "purpose": "Danylo source order: block 47 after block 46.",
          "raw_blocks": [
            {
              "block": 47,
              "chain": 2,
              "expected": 81,
              "target": 0
            }
          ]
        }
      ]
    },
    {
      "id": "xray-best-evidence-a100-conversion",
      "intent": "Full strongest-evidence conversion candidate, with uncertain features kept separate.",
      "model_type": "xray-best-evidence",
      "stages": [
        {
          "fields": [
            "OPT_FBIO_DISABLE",
            "OPT_FBPA_DISABLE",
            "OPT_ROP_L2_DISABLE"
          ],
          "name": "memory-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_FBP_DISABLE"
          ],
          "name": "memory-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_TPC_GPC0_DISABLE",
            "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE",
            "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE",
            "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE",
            "OPT_TPC_GPC7_DISABLE",
            "OPT_PES_DISABLE"
          ],
          "name": "compute-prerequisites",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [
            "OPT_GPC_DISABLE"
          ],
          "name": "compute-activation",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-isolated-gates",
          "pcie_bits": [
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE"
          ],
          "purpose": null,
          "raw_blocks": []
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block47",
          "pcie_bits": [],
          "purpose": "Reported functional sequence first clears the remaining block-47 MAGIC_D projection.",
          "raw_blocks": [
            {
              "block": 47,
              "chain": 2,
              "expected": 81,
              "target": 0
            }
          ]
        },
        {
          "fields": [],
          "name": "pcie-a100-exact-block46",
          "pcie_bits": [],
          "purpose": "Reported functional sequence then clears the block-46 empirical bit.",
          "raw_blocks": [
            {
              "block": 46,
              "chain": 2,
              "expected": 40960,
              "target": 32768
            }
          ]
        },
        {
          "fields": [
            "OPT_PCIE_DEVIDA",
            "OPT_PCIE_DEVIDB"
          ],
          "name": "identity-last",
          "pcie_bits": [],
          "purpose": null,
          "raw_blocks": []
        }
      ]
    },
    {
      "id": "review-ss0-iff",
      "intent": "Inventory the reported SS0 feature-override IFF record without treating its values or rows as portable.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "IFF placement depends on the measured boundary and record grammar; no executable IFF allocator exists",
            "coordinate_certainty_percent": 85,
            "feature": "SS0",
            "meaning": "IMLA0-3/FMLA/FFMA/DP throttle override",
            "plane": "IFF",
            "portable_placement_certainty_percent": 0,
            "reported_rows": [
              484,
              485
            ],
            "reported_words": [
              "0x082381fd",
              "0x88888888"
            ],
            "target_mmio": "0x0082381c",
            "target_register": "FEATURE_OVERRIDE_SM_SPEED_SELECT"
          }
        ]
      },
      "stages": []
    },
    {
      "id": "review-ss1-iff",
      "intent": "Inventory the reported SS1 feature-override IFF record without conflating IMLA4 with FP64.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "row 487 is quarantined and no executable IFF allocator or portable value proof exists",
            "coordinate_certainty_percent": 85,
            "feature": "SS1",
            "meaning": "IMLA4 throttle override; not the FP64 selector",
            "plane": "IFF",
            "portable_placement_certainty_percent": 0,
            "reported_rows": [
              486,
              487
            ],
            "reported_words": [
              "0x0823823d",
              "0x00000008"
            ],
            "target_mmio": "0x00823820",
            "target_register": "FEATURE_OVERRIDE_SM_SPEED_SELECT_1"
          }
        ]
      },
      "stages": []
    },
    {
      "id": "review-dp-conventional",
      "intent": "Inventory the FP64 speed selector and proposed adjacent control/undo fuse as conventional-fuse research only.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "primary/redundant sense, RIR interaction and isolated cold proof of bit 27 are unresolved",
            "control_name": "OPT_SM_SPEED_SELECT_DP_CONTROL",
            "control_or_undo_bit": 27,
            "control_semantics_certainty_percent": 65,
            "feature": "FP64/DP",
            "plane": "conventional-primary-redundant",
            "rows": [
              6,
              7
            ],
            "selector_bit": 26,
            "selector_coordinate_certainty_percent": 99,
            "selector_name": "OPT_SM_SPEED_SELECT_DP"
          }
        ]
      },
      "stages": []
    },
    {
      "id": "review-ecc-conventional",
      "intent": "Inventory the ECC enable and adjacent control fuse without claiming a complete ECC recipe.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "sense/control polarity, RIR, memory-domain gates, firmware policy and error-injection validation are unresolved",
            "control_bit": 29,
            "control_name": "OPT_ECC_EN_CONTROL",
            "coordinate_certainty_percent": 99,
            "enable_bit": 28,
            "enable_name": "OPT_ECC_EN",
            "feature": "ECC",
            "feature_sufficiency_certainty_percent": 45,
            "plane": "conventional-primary-redundant",
            "rows": [
              6,
              7
            ]
          }
        ]
      },
      "stages": []
    },
    {
      "id": "review-nvenc-unsupported",
      "intent": "Make NVENC's absence from the physical plan explicit rather than silently omitting it.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "no defensible GA100 physical coordinate or populated-engine evidence exists",
            "direct_registers": [
              "OPT_NVENC_DISABLE",
              "OPT_NVENC_DISABLE_CP",
              "OPT_NVENC_DEFECTIVE",
              "CTRL_OPT_NVENC",
              "STATUS_OPT_NVENC",
              "PTOP_SCAL_NUM_NVENCS"
            ],
            "feature": "NVENC",
            "instance_count_register_width_bits": 5,
            "option_control_defect_status_width_bits": 2,
            "physical_coordinate_certainty_percent": 0,
            "plane": "unknown",
            "populated_engine_evidence": "none",
            "production_a100_absence_certainty_percent": 99,
            "ptop_instance_count_is_inventory_only": true,
            "retained_four_card_unusable_engine_inference_percent": 97,
            "supported_permanent_unlock_target_percent": 0
          }
        ]
      },
      "stages": []
    },
    {
      "id": "review-nvlink-per-link-unsupported",
      "intent": "Separate the unknown twelve-link disable map from the three global IOCTRL group bits.",
      "model_type": "review-only-unsupported",
      "review_only": {
        "compiler_eligible": false,
        "operation_count_required": 0,
        "runtime_eligible": false,
        "subjects": [
          {
            "blocker": "physical coordinate, board routing, ROM/GSP policy and link training are unresolved",
            "direct_register": "OPT_PERLINK_DISABLE",
            "direct_width_bits": 12,
            "feature": "per-link NVLink",
            "global_group_candidate": "Chain-1 bits 368..370 are not this field",
            "physical_coordinate_certainty_percent": 0,
            "plane": "unknown"
          }
        ]
      },
      "stages": []
    }
  ],
  "runtime_field_policy": {
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": [
      [
        [
          1,
          9,
          2,
          1
        ]
      ],
      "paired_disable_candidate",
      false
    ],
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": [
      [
        [
          1,
          23,
          3,
          1
        ]
      ],
      "paired_disable_candidate",
      false
    ],
    "OPT_DISABLE_GEN3_SPEED": [
      [
        [
          2,
          39,
          9,
          1
        ]
      ],
      "disable_bit",
      false
    ],
    "OPT_FBIO_DISABLE": [
      [
        [
          1,
          5,
          3,
          13
        ],
        [
          1,
          6,
          0,
          11
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_FBPA_DISABLE": [
      [
        [
          1,
          6,
          11,
          5
        ],
        [
          1,
          7,
          0,
          16
        ],
        [
          1,
          8,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_FBP_DISABLE": [
      [
        [
          1,
          19,
          3,
          12
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_GPC_DISABLE": [
      [
        [
          1,
          8,
          3,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_NVDEC_DISABLE": [
      [
        [
          1,
          8,
          11,
          5
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_PCIE_BOOT_GEN23_DISABLE": [
      [
        [
          2,
          49,
          6,
          1
        ]
      ],
      "disable_bit",
      false
    ],
    "OPT_PCIE_BOOT_GEN3_DISABLE": [
      [
        [
          2,
          49,
          7,
          1
        ]
      ],
      "disable_bit",
      false
    ],
    "OPT_PCIE_DEVIDA": [
      [
        [
          2,
          37,
          1,
          15
        ],
        [
          2,
          38,
          0,
          1
        ]
      ],
      "identity",
      false
    ],
    "OPT_PCIE_DEVIDB": [
      [
        [
          2,
          38,
          1,
          15
        ],
        [
          2,
          39,
          0,
          1
        ]
      ],
      "identity",
      false
    ],
    "OPT_PES_DISABLE": [
      [
        [
          1,
          15,
          11,
          5
        ],
        [
          1,
          16,
          0,
          16
        ],
        [
          1,
          17,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_ROP_L2_DISABLE": [
      [
        [
          1,
          19,
          15,
          1
        ],
        [
          1,
          20,
          0,
          16
        ],
        [
          1,
          21,
          0,
          7
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_SYS_PIPE_DISABLE": [
      [
        [
          1,
          23,
          4,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC0_DISABLE": [
      [
        [
          1,
          11,
          11,
          5
        ],
        [
          1,
          12,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC1_DISABLE": [
      [
        [
          1,
          12,
          3,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC2_DISABLE": [
      [
        [
          1,
          12,
          11,
          5
        ],
        [
          1,
          13,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC3_DISABLE": [
      [
        [
          1,
          13,
          3,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC4_DISABLE": [
      [
        [
          1,
          13,
          11,
          5
        ],
        [
          1,
          14,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC5_DISABLE": [
      [
        [
          1,
          14,
          3,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC6_DISABLE": [
      [
        [
          1,
          14,
          11,
          5
        ],
        [
          1,
          15,
          0,
          3
        ]
      ],
      "disable_mask",
      true
    ],
    "OPT_TPC_GPC7_DISABLE": [
      [
        [
          1,
          15,
          3,
          8
        ]
      ],
      "disable_mask",
      true
    ],
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": [
      [
        [
          2,
          47,
          3,
          1
        ]
      ],
      "disable_bit",
      false
    ]
  },
  "runtime_profiles": {
    "specimen-80gb-hbm": [
      [
        "OPT_FBIO_DISABLE",
        "OPT_FBPA_DISABLE",
        "OPT_FBP_DISABLE",
        "OPT_ROP_L2_DISABLE"
      ],
      [
        "memory-prerequisites",
        "memory-activation"
      ]
    ],
    "specimen-a100-20b5-identity": [
      [
        "OPT_PCIE_DEVIDA",
        "OPT_PCIE_DEVIDB"
      ],
      [
        "identity-last"
      ]
    ],
    "specimen-media-mig-to-defect-experimental": [
      [
        "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
        "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
        "OPT_NVDEC_DISABLE",
        "OPT_SYS_PIPE_DISABLE"
      ],
      [
        "mig-system-pipe-to-defect",
        "nvdec-to-defect",
        "nvjpg-ofa-inseparable-pair"
      ]
    ],
    "specimen-more-sm": [
      [
        "OPT_GPC_DISABLE",
        "OPT_PES_DISABLE",
        "OPT_ROP_L2_DISABLE",
        "OPT_TPC_GPC0_DISABLE",
        "OPT_TPC_GPC1_DISABLE",
        "OPT_TPC_GPC2_DISABLE",
        "OPT_TPC_GPC3_DISABLE",
        "OPT_TPC_GPC4_DISABLE",
        "OPT_TPC_GPC5_DISABLE",
        "OPT_TPC_GPC6_DISABLE",
        "OPT_TPC_GPC7_DISABLE"
      ],
      [
        "rop-l2-dependency",
        "compute-prerequisites",
        "compute-activation"
      ]
    ],
    "specimen-pcie-gen4-isolated": [
      [
        "OPT_DISABLE_GEN3_SPEED",
        "OPT_PCIE_BOOT_GEN23_DISABLE",
        "OPT_PCIE_BOOT_GEN3_DISABLE",
        "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE"
      ],
      [
        "pcie-isolated-gates"
      ]
    ],
    "specimen-sys-pipe-to-defect-experimental": [
      [
        "OPT_SYS_PIPE_DISABLE"
      ],
      [
        "sys-pipe-experimental"
      ]
    ],
    "xray-common-permanent-topology": [
      [
        "OPT_FBIO_DISABLE",
        "OPT_FBPA_DISABLE",
        "OPT_FBP_DISABLE",
        "OPT_GPC_DISABLE",
        "OPT_PES_DISABLE",
        "OPT_ROP_L2_DISABLE",
        "OPT_TPC_GPC0_DISABLE",
        "OPT_TPC_GPC1_DISABLE",
        "OPT_TPC_GPC2_DISABLE",
        "OPT_TPC_GPC3_DISABLE",
        "OPT_TPC_GPC4_DISABLE",
        "OPT_TPC_GPC5_DISABLE",
        "OPT_TPC_GPC6_DISABLE",
        "OPT_TPC_GPC7_DISABLE"
      ],
      [
        "memory-and-shared-prerequisites",
        "compute-prerequisites",
        "memory-activation",
        "compute-activation"
      ]
    ]
  },
  "schema": "170hx-xray-unlock-plan-doc-contract-v1",
  "versions": {
    "backend_id": "ga100-vfi-ff-append-v1",
    "candidate": "0.5.0",
    "compiler": "0.5.0",
    "frontend": "4.3.0"
  }
}
```

<!-- XRAY-PLAN-CONTRACT-JSON:END -->
