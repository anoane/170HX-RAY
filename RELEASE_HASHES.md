# 170HX-XRAY 4.4.0 public-source release identity

Release date: 2026-11-28 (Europe/Rome)

This is the clean, minimally sanitized public source edition. It contains no
generated card report, raw/private evidence, cache, bytecode, build product or
macOS metadata. ArchBench is a separately published AGPL dependency bound by a
mode-160000 gitlink, lock record, tree identity and tracked-byte manifest; its
worktree contents are outside this parent package manifest.

This ledger and the root `SHA256SUMS` are excluded from that manifest to avoid
self-reference. Every other regular indexed parent-repository release file is
covered. The ArchBench gitlink is the only non-regular indexed object.

## Version set

| Component | Version / contract |
|---|---|
| XRAY frontend | 4.4.0 |
| Full-report orchestrator | 0.8.0 |
| Diagnostics orchestrator | 2.0.0 |
| Temperature probe | 1.7.0 |
| Controller helper | 4.2.0 / native LP64 UAPI ABI 6 |
| Thermal helper | 1.5.1 / native LP64 UAPI ABI 5 |
| Candidate planner | 0.6.0 |
| VFI manifest compiler | 0.6.0 / sealed manifest v3 |
| Full-release bundler | 0.6.0 / bundle schema v2 |
| Experimental append backend | 1.1.0 / sealed manifest v3 only |
| Hardware-inert OTP oracle | 0.2.0 |
| ArchBench submodule | 0.2.0 at `6209b36e3c40a5a964842194c4851bdcdf79bcff` |

## Package seal

| Indexed regular files | Manifest entries | `SHA256SUMS` SHA-256 |
|---:|---:|---|
| 69 | 67 | `6bc749a6db638a4c501fc9511f7fc96884dbcd733288301b2325c30be60c7c6a` |

All manifest entries pass `shasum -a 256 -c SHA256SUMS`. The sanitized binary
and pathname scan, targeted working-tree/history secret scan and edition-drift
classification are clean.

## Runtime and contract validation

CPython 3.9.25, 3.14.8 and 3.15.2 each passed 345 tests with one intentional
skip. The CLI self-test passed 28 checks. Registry audit matched the Python and
kernel sets exactly at 1,109 catalogued addresses and 1,084 kernel-safe reads.
The generated `UNLOCK_PLAN.md` contract matched the frontend, candidate
planner, compiler and runtime allowlists exactly.

The research and sanitized editions independently passed 342 and 343 tests,
respectively, with one intentional skip on each of the same three runtimes.
Those edition totals are evidence-bound differences, not missing coverage.

## Planning and package replay

The final source-only four-card replay produced 96 profiles, including 48
review-only profiles, 576 per-profile plan/image files, 384 proposed-image
transports and 28/28 independently verified sealed-v3 manifests. All 824
release-bundle checksum entries verified. Hardware access, backend invocation
and programming-write count were false, false and zero. Every manifest remains
`execution_authorized=false`. The replay output is deliberately outside this
source repository.

ArchBench is bound to tree `d7150409058f37c18b9a7fde2677b73ae817fc73`
through a 104-file tracked-content inventory whose SHA-256 is
`f018760a12b15c6b97d7abd6198b453f98f930bed076b4b610c2b8da1c41b129`.
That pin passed 134/134 host tests, 532,480 independently checked integer
transitions and 3/3 CUDA-disabled CTest targets.

## Static and kernel validation

Ruff and operational/test-wide Pylint passed. Bandit reported zero high, two
bounded medium and 19 low findings. Ten Semgrep findings were reviewed as false
positives about owner-only mode `0700`. Controller and thermal ABI tests,
hardware-inert OTP tests, LLVM/GCC Kbuild, Sparse, pinned Linux clang-format,
`linuxkernel-*` clang-tidy and strict CUDA 13.5 SM80 compilation passed.
Checkpatch remains explicitly advisory and non-clean: zero errors, 57 warnings
and 78 checks.

## Licensing and boundary

Original first-party userspace source and documentation are copyright 2026
Fabiano Anemone (`fabiano.anemone@gmail.com`) and AGPL-3.0-or-later. ArchBench
is independently AGPL-3.0-or-later. Kernel helpers retain their file-level
GPL-2.0 terms and Linux-syscall-note exceptions where identified. The public
evidence directory contains first-party audit/lineage metadata; it does not
ship or relicense the referenced raw third-party plans.

No 4.4 release validation performed a voltage glitch, GPU access, module load,
BAR/MMIO transaction, VBIOS action or OTP pulse. The seal proves byte identity
and tested software behavior, not electrical safety, silicon health, cold-boot
recovery or permission to perform an irreversible operation. Git records the
final commit identity after this non-self-referential ledger is frozen.
