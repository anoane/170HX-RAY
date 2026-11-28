# Upstream reconciliation — 2026-11-27 through 2026-11-28

This document records how the 2026-11-27/28 upstream pass affected XRAY's
evidence model. It is not an irreversible-operation guide. Nothing is promoted
merely because an upstream repository labels it tested, because an offline
suite passes, or because a direct register has a plausible name.

## Exact audit pins

| Project | Commit | Date |
|---|---|---|
| pico-ga100vfi-console | [e9e9a33c8b3515563959f7239a0df2a1a3b6eef3](https://github.com/portz3r0/pico-ga100vfi-console/tree/e9e9a33c8b3515563959f7239a0df2a1a3b6eef3) | 2026-11-28 03:12:35 +11:00 |
| cmp170hx-ffcalc | [e28e03a40ade83079f518df7416072daea047bf9](https://github.com/sayhellotojungle/cmp170hx-ffcalc/tree/e28e03a40ade83079f518df7416072daea047bf9) | 2026-11-27 10:29:13 +08:00 |
| cmp170hx-8gb-vfi-tools | [f422f80a716fff2c1dc112abcf2e9d43a04b6614](https://github.com/darkmaniac7/cmp170hx-8gb-vfi-tools/tree/f422f80a716fff2c1dc112abcf2e9d43a04b6614) | 2026-11-27 03:38:05 -06:00 |
| esp32-ga100vfi | [37d14e9698e5926ca1b1f7e86b61d340f93414a2](https://github.com/sayhellotojungle/esp32-ga100vfi/tree/37d14e9698e5926ca1b1f7e86b61d340f93414a2) | 2026-11-27 09:05:58 +08:00 |
| cmpunlocker | [8b6c556fedaa0150a6fc40c74297b20cb5e508a6](https://github.com/sayhellotojungle/cmpunlocker/tree/8b6c556fedaa0150a6fc40c74297b20cb5e508a6) | 2026-11-26 17:54:13 +08:00 |

The console pin is still its fetched origin/main head: there are no commits in
the range e9e9a33..origin/HEAD. Commit timestamps are preserved with their
original offsets because ordering claims depend on them.

The sweep covered every configured retained Git worktree, not only the five
repositories with material deltas above. The final post-change freshness pass
checked 47 worktrees representing 38 normalized GitHub/Gist remotes; 46
fetch/prune operations succeeded and no default or tracked non-default branch
moved. The sole failure was the deleted or private
`ggualerz/cmp170hx10g-to-80g` remote (GitHub returned “Repository not found”);
its retained local `7c8525579f98` snapshot is historical and is not represented
as current.

Material unchanged baselines were also rechecked at these exact heads:

| Project | Rechecked head | Commit date | Disposition |
|---|---|---|---|
| kate-land/ga100vfi current | `39b7bfe5a148` | 2026-11-11 | Current README replacement checked; recovered `b71e3990` history remains separately labeled historical |
| dalzyu/pico-ga100vfi | `8be8296afe2a` | 2026-11-20 | Adaptive glitch telemetry retained as tuning evidence, not fuse authority |
| matstatman/gpcprobe | `c68e7fc05df6` | 2026-11-13 | No newer helper semantic displaced XRAY's hardened kernel boundary |
| repeaterdroid media/MIG work | `e3826fee3958` | 2026-11-22 | Existing defect-gated SYS_PIPE/NVDEC and inseparable `{146,371}` model remains current |
| JRex286 feature-override gist | `0db3baa71954` | 2026-05-31 | SS0/SS1/DP/ECC distinctions remain review evidence |
| kprinssu/ga100-ff-tools | `594e83ddd336` | 2026-11-20 | No newer revision; detailed non-superset comparison retained |
| ahnguyen17/ga100-fuse-re | `74dfb1666e35` | 2026-11-13 | Hypotheses remain graded rather than promoted wholesale |
| lixiangwuxian/170hx-rir-tool | `03a612af5f6e` | 2026-10-21 | RIR remains a separate physical/effective plane |

Intentional non-default branch checkouts (including Zagitta beta and the clee
KB2040 branch) were not fast-forwarded to a different branch merely to make
their names match `origin/HEAD`. The recovered historical ga100vfi checkout
likewise remains intentionally distinct from `ga100vfi-current`.

## Disposition vocabulary

- **Imported** means the useful observation or safety rule is already
  represented by XRAY's current model. It does not imply executable OTP.
- **Review-only** means the evidence is retained with uncertainty and may
  appear in documentation or a zero-operation review surface.
- **Rejected** means the proposed inference, target or transaction must not
  enter plan generation or execution.
- **Unchanged** means the audit independently agrees with an existing XRAY
  result and requires no model change.

## Evidence and promotion matrix

| Source and exact anchor | Finding | Disposition | Reconciliation reason |
|---|---|---|---|
| console service.py:169-195 | GFW_BOOT, FEAT_READOUT_1 and external never-write warnings | **Imported** | The readouts already exist in XRAY. External effect descriptions remain warnings rather than local-silicon proof. |
| console service.py:1052-1177 | Two-step FF programming: verified chain-zero word followed by chain bits | **Review-only** | It usefully reduces one partial-landing class, but doubles program commands. It needs sealed per-substep journaling, timeout quarantine, exact-prefix verification and cold validation before execution eligibility. |
| console service.py:1418-1469 | Paired DEVIDA/DEVIDB A100 80-GB queue; unproved 40-GB target removed | **Unchanged** | XRAY already requires the exact paired 20b5 identity commit, identity-last ordering and no guessed 40-GB SKU. |
| console advanced_unlocks.py:141-150 | Whole-word PCIe values for blocks 39, 47 and 49 | **Rejected** | The source calls block 39 ambiguous and block 49 unresolved. XRAY retains isolated block-39 bit 9, block-47 bit 3 and block-49 bits 6/7 only. |
| console advanced_unlocks.py:154-180; evidence/60-gpu2-burn-truth.md:148-153 | Rows 2/3 bit 21, 6/7 bit 28 and 48/49 bit 5 attributed to TTU, ECC and row-remapper or PLM unlock | **Review-only** coordinates; **Rejected** recipe | Newer evidence says the same rows served the DEVID column route and specifically says TTU column fuses were absent. No isolated cold feature attribution exists. |
| console advanced_unlocks.py:183-212 | SS0/SS1 IFF words and reported rows 484..487 | **Unchanged** | Target/value evidence is already review-only. Placement remains specimen-specific and row 487 remains quarantined. |
| console service.py:1025-1041 | Automatically clear volatile PROGRAM_DIS under EN_SW_OVERRIDE | **Rejected** for automatic use | This is a state-changing recovery step based on external guidance. It is not a passive observation and lacks isolated electrical/cold qualification. |
| console service.py:1324-1329,1377-1410,2523-2583 | Timeout-as-absent scan, reserve bypass, campaign plane-gate bypass and re-glitch after ABORT | **Rejected** | These violate fail-closed acquisition, correction-budget preservation and terminal uncertainty requirements. |
| console service.py:3310-3453 | Backup-first VBIOS UI with unbound/module-unloaded guards | **Review-only** workflow; **Rejected** execution import | Useful UX, but the selected ROM is not digest-sealed or parsed/bound to exact card identity and recovery; missing serials share a generic backup namespace. |
| console advanced_unlocks.py:1-9; evidence/30-advanced-unlocks.md:25-31; evidence/60-gpu2-burn-truth.md:155-172 | Broad executed-and-confirmed claim | **Rejected** | Signature verification was skipped in one decode; all 31 expected writes were absent from the alleged GPU2 post image, which belonged to another die. |
| ffcalc ffcalc/data/ga100-fuses.json:65-105 | Broader controller, feature, speed, ECC, geometry, lane and HBM-PLL readout catalog | **Imported** where already allowlisted; otherwise **Review-only** | Names and offsets improve inventory, but each new address still needs access-class and exact-card capture evidence. |
| ffcalc ffcalc/ffmacro.py:156-199,240-254 | Expanded poison/idle, debug, CRC, EFP, program, DSOV, privilege and PLM checks | **Unchanged** | XRAY already requires these categories and a stricter exact qualified idle state plus complete image binding. |
| ffcalc ffcalc/optionmap.py:3-10; ffcalc/ffmacro.py:267-278,308-319 | Observed IFF start 488 but static FF interval 222..487 | **Rejected** as a universal extent | A two-card observation is useful evidence, not an architectural constant. Runtime code must use the specimen-local boundary and refuse ambiguity. The only row-487 exercise is a fake test; 101/101 offline checks are not physical proof. |
| ffcalc ffcalc/ffmacro.py:64-119,202-219 | Weak idle predicate and restoration writes after timeout/poison | **Rejected** | State 4 plus command 0 is weaker than exact qualification; restoration after uncertain controller completion can compound a poison or in-flight command. |
| ffcalc ffcalc/generate.py:142-169,178 | Missing starting block defaults to zero | **Rejected** | Unknown co-resident bits must stop planning. Zero is not evidence. |
| ffcalc ffcalc/analyze.py:48-101,120-152 | Defect mismatch warnings and media STATUS AND NOT OPT inference | **Rejected** | Warning-only defect handling can expose dead units. STATUS equal to OPT cannot prove defect-free silicon when an already-disabled defective bit is masked. |
| ffcalc ffcalc/generate.py:176-193 | Lexical chain/block ordering with no hard reserve | **Rejected** | Prefix safety requires child/dependency preparation before parent activation and a protected correction/revert budget. |
| ffcalc ffcalc/optionmap.py:151-156 | Clear PCIe block-39 bits 1 and 9 together | **Rejected** | The isolated cold result falsified bit 1. XRAY preserves bit 1 and targets bit 9 only. |
| ffcalc ffcalc/optionmap.py:163-174,204-208; ffcalc/generate.py:36-53,108-127 | A100 whole-word MAGIC_D value 0x00200000 | **Rejected** | The source describes it as an unmeasured guess. Only isolated MAGIC_D bit 25 is represented as a candidate. |
| ffcalc ffcalc/ffmacro.py:308-387 | SW-IFF implementation | **Review-only** mechanism; **Rejected** execution | It is explicitly untested on hardware and lacks record-shape, live-occupancy, pulse, readback and restore closure. |
| ffcalc burntool.py:119-131; esp32 README.md:112-118,245-257 | Burn gate requires EFP=1 before a later sense, while fresh Goldilocks is reported EFP=0 then sense-to-1 | **Rejected** sequence | The precondition is circular for the reported fresh state. Sense ordering must be explicitly qualified and state-bound. |
| darkmaniac docs/FUSES.md:43-53,122-204,220-312 | Dynamic-boundary warning, topology formulas, HBM pair rule and isolated PCIe map | **Imported** / **Unchanged** | These independently agree with XRAY's current boundary model, fixed-point topology, 64/80/96 calculation and four isolated PCIe bits. |
| darkmaniac tools/fuseplan/src/core.js:982-984 | Refuse planning with no starting record | **Imported** principle | XRAY already fails closed on unknown initial field/co-resident state. |
| darkmaniac docs/FUSES.md:147-155,269-338 | Block-26 RECONFIG, blocks 78/79 speed selects and conventional ECC/remapper/VGX/OpenGL/DP/TTU hypotheses | **Review-only** | Coordinates and reported words remain community hypotheses without isolated cold feature proof or dependency closure. |
| darkmaniac core.js:5-9,210-233,841-844; docs/FUSES.md:350-352 | Fixed row 486 end, contradictory all-ones handling and warning-only reserve | **Rejected** | These conflict with its own dynamic-boundary documentation and XRAY's hard reserve/ambiguity gates. |
| darkmaniac README.md:74-76; core.js:21-67 | Planner omits media and identity despite broader documentation | **Unchanged** | XRAY remains broader; documentation is not plan implementation. |
| esp32 README.md:192-275 | Two-pass capture, independent simulation/cross-check, explicit sequence and post-cold checks | **Imported** principle | These agree with XRAY's separation of acquisition, planning, execution and independent cold validation. |
| esp32 README.md:71-79,203,215,228 | FF 222..486 and IFF 487..511 hard-coded | **Rejected** | It conflicts with the same-day ffcalc boundary evidence and cannot be generalized across specimens. |
| esp32 README.md:232-238 | Real one-row OTP canary | **Rejected** | A canary consumes finite correction space without proving the subsequent electrical sequence. |
| esp32 README.md:81-88,228 | Require new AND NOT old to be zero | **Rejected** as physical law | FF replay is last-record-wins on a new physical row. The new logical payload can set and clear bits relative to the prior winner even though the physical blank-row burn remains OTP 0-to-1. |
| cmpunlocker common/constants.yaml:117-154; driver/hbm-pm/cmp_hbm_pm.c:91-149 | FBPA refresh/geometry, HBM PLL, PRI fence, FB-stop, PMU, GFW and GSP register set | **Imported** for already known GFW/PLL observations; otherwise **Review-only** | It is valuable diagnostics provenance. Unknown access classes and runtime ownership prevent automatic allowlisting. |
| cmpunlocker driver/hbm-pm/cmp_hbm_pm.c | HBM idle transition mechanism | **Rejected** from read levels | The mechanism writes clocks, refresh controls, privilege and framebuffer-stop state. It belongs to a separately authorized runtime-power experiment, not fuse acquisition. |
| cmpunlocker branch test-gen4-rootport-ladder at 91a7b060 | Endpoint/root-port Gen3-to-Gen4 training ladder | **Review-only** | This may inform post-unlock validation, but it is an experimental non-master runtime route and contributes no physical fuse coordinate. |

## Route-level result

| Route | Result after reconciliation |
|---|---|
| HBM and SM topology | **Unchanged.** Dynamic per-die defect closure and 64/80/96-GB plus up-to-128-SM arithmetic remain the authoritative model. |
| PCIe Gen4 | **Unchanged.** Isolated block-39 bit 9, block-47 bit 3 and block-49 bits 6/7 remain the only candidate fields. Public whole-word recipes remain comparison-only. |
| A100 identity | **Unchanged.** Paired 20b5 fields, identity last; no guessed 40-GB identity. |
| SS0/SS1, DP and conventional rows | **Review-only.** No portable IFF allocator or complete conventional-fuse execution model was promoted. |
| MIG and media | **Unchanged.** Existing defect-gated SYS_PIPE/NVDEC and inseparable NVJPG/OFA candidate model remains broader and more conservative than the pinned public planners. |
| NVLink | **Unchanged.** Three global IOCTRL-group bits remain distinct from the unmapped 12-bit per-link plane. |
| VBIOS migration | **No execution promotion.** Backup-first UX is useful, but image, card, platform and recovery bindings remain incomplete. |
| OTP transaction | **Review-only delta.** Two-step inert-then-arm is retained for a future explicitly named strategy; it does not replace XRAY's current transaction contract. |
| HBM idle governor | **No fuse-plan delta.** New registers are diagnostics/runtime-power evidence only. |

## Promotion rule

A later pass may promote a review-only item only when it supplies all relevant
parts of the chain: exact source and commit, physical coordinate rather than a
direct-register name, same-specimen complete pre/post images, defect and
support-plane agreement, dependency-safe ordering, specimen-local boundary and
reserve proof, transaction/journal closure, independent cold readback and the
required functional test. Missing information fails closed; recency raises
review priority but never substitutes for those proofs.
