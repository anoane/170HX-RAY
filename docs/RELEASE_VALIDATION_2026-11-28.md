# 170HX-XRAY 4.4.0 release validation — 2026-11-28

This is the controlling source-release ledger for XRAY 4.4.0. It records
exactly what was exercised, what failed closed during validation, and what was
deliberately not attempted. A green software test does not establish that an
OTP waveform is electrically safe, that an unmarked unit is healthy, or that a
card will cold-boot after an irreversible change.

No validation in this ledger performed a voltage glitch, loaded either XRAY
kernel module, accessed a GPU, issued BAR/MMIO transactions, changed a power
limit, flashed a VBIOS, invoked the OTP backend, or programmed a fuse.

## Frozen component set

| Component | Version / contract |
|---|---|
| XRAY frontend | 4.4.0 |
| Full-report orchestrator | 0.8.0 |
| Diagnostics orchestrator | 2.0.0 |
| Temperature probe | 1.7.0 |
| Controller helper | 4.2.0 / native LP64 UAPI ABI 6 |
| Thermal helper | 1.5.1 / native LP64 UAPI ABI 5 |
| Candidate planner | 0.6.0 |
| VFI manifest compiler | 0.6.0 |
| Full-release bundler | 0.6.0 / bundle schema v2 |
| Experimental VFI backend | 1.1.0 / sealed manifest v3 only |
| Hardware-inert OTP oracle | 0.2.0 |
| ArchBench | 0.2.0 at `6209b36e3c40a5a964842194c4851bdcdf79bcff` |

Bundle schema v2 is the self-contained package format. It does not make a v2
OTP manifest current: the only current irreversible artifact schema is sealed
manifest v3.

## Final runtime matrix

Every run used `PYTHONDONTWRITEBYTECODE=1`. The Python 3.9 workspace was
mounted read-only into the official arm64 `python:3.9-slim` image at digest
`sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b`.

| Edition | CPython 3.15.2 | CPython 3.14.8 | CPython 3.9.25 |
|---|---:|---:|---:|
| Research | 342 pass, 1 intentional skip | 342 pass, 1 intentional skip | 342 pass, 1 intentional skip |
| Sanitized | 343 pass, 1 intentional skip | 343 pass, 1 intentional skip | 343 pass, 1 intentional skip |
| Public | 345 pass, 1 intentional skip | 345 pass, 1 intentional skip | 345 pass, 1 intentional skip |

The edition totals differ intentionally: sanitized adds a provenance-boundary
regression, while public adds two release/submodule-boundary cases. Each
edition also passed:

- the 28-check CLI `self-test`;
- `registry-audit` with an exact Python/kernel set match: 1,109 catalogued
  addresses, 1,084 kernel-safe reads, and 771 generated names over 770 unique
  addresses; and
- `tools/update_unlock_plan_contract.py --check`, which parses and compares the
  complete generated `UNLOCK_PLAN.md` contract rather than checking tokens.

The first post-repin public run correctly failed because the ArchBench
worktree and lock named `6209b36` while the parent Git index still named the
older gitlink. The parent gitlink was aligned and the entire three-runtime
matrix was restarted from scratch. The failed preliminary run is retained as
evidence that the lock is fail-closed, not counted as a pass.

The exact runtime transcript has SHA-256
`9231f27f023171bde2d30503b07d5b7b70a4bcd44e3d55080d7e6c063c04fce0`.

## Upstream freshness and reconciliation

The final sweep fetched/pruned 47 retained worktrees representing 38 normalized
GitHub/Gist remotes. Forty-six succeeded; no default or tracked non-default
branch moved after the detailed reconciliation. The only failure remains the
deleted or private `ggualerz/cmp170hx10g-to-80g` remote; retained commit
`7c8525579f98` is historical, not current evidence.

The controlling heads therefore remain console `e9e9a33`, ffcalc `e28e03a`,
darkmaniac `f422f80`, ESP32 `37d14e9`, cmpunlocker `8b6c556`, ga100vfi
`39b7bfe5`, pico `8be8296`, gpcprobe `c68e7fc`, media/MIG `e3826fe`, JRex gist
`0db3baa`, ga100-ff-tools `594e83d`, and ga100-fuse-re `74dfb166`. No later
fuse coordinate, read register, planning profile, unlock route, or safety delta
was found. See
[`UPSTREAM_RECONCILIATION_2026-11-27.md`](UPSTREAM_RECONCILIATION_2026-11-27.md)
and [`TOOL_COMPARISON.md`](TOOL_COMPARISON.md).

The exact final freshness transcript has SHA-256
`b848c9d53d30710e843f2c8182e26adb84aa25f93fb04aa0de85d58b61826c38`.

## Planning and end-to-end package replay

The inventory contains 24 profiles per specimen:

- 12 operational/offline candidate profiles;
- 12 zero-operation review-only profiles; and
- exactly seven compiler/runtime execution-eligible profile classes.

Review-only RECONFIG, speed-select, row-remapper, VGX, OpenGL, TTU, SS0,
SS1, DP, ECC, NVENC and per-link NVLink evidence cannot become executable by
changing an export format. Missing co-resident FF blocks stop planning; zero is
not substituted for an absent block. HBM targets admit only exact
density-scaled 16/20/24-subpartition classes—64/80/96 GiB at 4 GiB per
subpartition. Diagnostic 72/88-GiB arithmetic is never target-admissible.

A fresh source-only replay over four retained specimen captures produced:

| Artifact | Result |
|---|---:|
| Candidate profiles | 96 = 4 cards × 24 |
| Review-only profiles | 48 = 4 × 12 |
| Candidate profile exports | 96 |
| Per-profile plan/image files | 576 = 96 × (2 plan + 4 image) |
| Proposed-image files | 384 across JSON, JSONL, indexed text and U32LE |
| Sealed-v3 manifests | 28 = 4 × 7 |
| Manifest compilation | 28/28 pass |
| Independent manifest verification | 28/28 pass |
| Release checksum inventory | 824/824 pass |
| Hardware access / OTP backend / program writes | false / false / 0 |

All 28 manifests say `execution_eligible=true` and
`execution_authorized=false`. The release bundle independently verifies as
schema `170hx-xray-complete-release-bundle-v2` with no errors.

The external demonstration directory is
`170HX-XRAY-FULL-OUTPUT-2026-11-28`; it is deliberately outside the source
repositories. Its final self-contained bundle has:

| Item | SHA-256 |
|---|---|
| Release `index.json` | `787f5f8bd46e15093d3fa56d2ad19d44304440804eba3a80b786e5e294f37358` |
| Release `SHA256SUMS` | `a8685e69f849ea5a70d11bdfcba8de47fa77424bc449c224bec31fc2ee7f29f6` |
| Source-bundle index | `9819d35fa7c40196ef32e543d544d56d4071bee95ac4fa6383dda30bdd5ab74f` |
| Manifest-set `SHA256SUMS` | `496fc5c31d2e7d7faa722cf2897c458ac410145103292fa3179bd3f085c3a7c4` |
| Build/verify result | `8ddebcb2b00fcd2bb043ac9c0b8d22ecb1ec63f97ff1d8e2f19629728d3b769a` |
| Bundler source | `4027d246fb4e82518f4161c5a1a5b1575bce37f91d0f55db1faff50052ca9446` |

The four cards retain a 64-GiB current class and an 80-GiB defect-preserving
target. Their topology projections are:

| BDF | Current SM | Defect ceiling | Post-target without RECONFIG override | Architecture maximum |
|---|---:|---:|---:|---:|
| `0000:01:00.0` | 74 | 112 | 108 | 128 |
| `0000:41:00.0` | 74 | 124 | 120 | 128 |
| `0000:82:00.0` | 74 | 124 | 120 | 128 |
| `0000:c1:00.0` | 74 | 126 | 122 | 128 |

The 96-GiB class exists in the model but is not defect-preserving for these
four specimens. A capacity or SM ceiling is arithmetic, not proof that the
corresponding silicon is stable.

Three refusal cases were also exercised successfully: a historical bundle
whose checksum inventory omitted candidate captures, a four-capture bundle
whose cold MAIN/RIR baselines remained external, and noncanonical manifest
filenames. All refused before producing an authorizing artifact.

## ArchBench source lock

The independently published AGPL ArchBench dependency is locked to:

- commit `6209b36e3c40a5a964842194c4851bdcdf79bcff`;
- tree `d7150409058f37c18b9a7fde2677b73ae817fc73`;
- 104 reviewed files; and
- tracked-content manifest SHA-256
  `f018760a12b15c6b97d7abd6198b453f98f930bed076b4b610c2b8da1c41b129`.

The exact pin passed 134/134 host tests, 532,480 independently checked integer
transitions, 3/3 CUDA-disabled CTest targets, and Ruff `E9,F`. No CUDA GPU
benchmark was run for this source-only release admission.

## Static, security and kernel gates

Across all editions:

- Ruff 0.17.6 `E9,F` passes every Python file, including ArchBench;
- Pylint 4.1.4 errors-only passes both the operational corpus and the complete
  test corpus without suppressions;
- Bandit 1.9.4 reports zero high, two bounded medium and 19 low findings;
- Semgrep OSS 1.187.0 reports ten reviewed false positives that incorrectly
  describe owner-only mode `0700` as permissive; and
- binary-safe sanitized/public provenance scans, targeted working-tree and Git
  history secret scans, and cross-edition parity checks are clean.

The two Bandit medium boundaries are the bounded local `nvidia-smi` XML parser
and execution of an already-read, SHA-256-recorded local sibling module buffer.
The static transcript has SHA-256
`a1c75c576685a0101756c73f0be32965e3211d789de066fed767c65e961c3680`.

Controller/thermal ABI tests, hardware-inert OTP state tests, LLVM and GCC
Kbuild with warnings as errors, Sparse, pinned Linux clang-format,
`linuxkernel-*` clang-tidy, and the strict CUDA 13.5 SM80 compile all pass.
Linux checkpatch remains advisory and explicitly non-clean: zero errors, 57
warnings and 78 checks. Exact compiler commands and binary identities are in
[`STATIC_AND_KERNEL_VALIDATION_2026-11-28.md`](STATIC_AND_KERNEL_VALIDATION_2026-11-28.md).

## Release and provenance boundary

The public edition is source-only. It excludes card reports, raw/private
evidence, build output, bytecode, caches and macOS metadata. Its root
`SHA256SUMS` covers every regular parent-repository release file except itself
and the non-self-referential `RELEASE_HASHES.md`; its mode-160000 ArchBench
gitlink is bound separately by URL, commit, tree and the 104-file tracked-byte
manifest.

The normal and sanitized research editions instead embed that exact ArchBench
source as ordinary package files and retain their edition-appropriate reports
and evidence. Their root manifests cover every regular package file except
`SHA256SUMS` and `RELEASE_HASHES.md`. All three editions record their final
entry count and manifest digest in their own ledger only after cleanup and
freeze. The sanitized and public trees contain no occurrence or pathname of
the prohibited private provenance token. Intentional edition differences are
classified; no unexplained operational/test drift remains.

## Deliberately unexecuted and still unqualified

This release does **not** establish any of the following:

- a live held-glitch `2080` transaction;
- electrical pulse, voltage, timing or power-source qualification;
- kernel-owned irreversible transport or closure of the userspace
  `resource0` bind/remove race;
- an owner-card OTP program pulse or conventional/IFF/FPF/RIR write;
- cold recovery after programming;
- a converted `20b5` capture or signed A100 VBIOS migration;
- post-unlock HBM, SM, Gen4, NVLink, ECC, MIG or media stability; or
- authoritative physical meaning for every catalogued register or field.

Those are hardware evidence gaps, not software-test failures. Authorization
cannot manufacture the missing measurements. The v3 backend therefore remains
experimental, narrowly allowlisted and explicitly unauthorised by every
generated demonstration manifest.
