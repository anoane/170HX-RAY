# 170HX-XRAY 4.3.0 public-source release identity

Release date: 2026-11-26 (Europe/Rome)

This is the clean, minimally sanitized source edition prepared from
`170HX-XRAY-SANITIZED`. It contains no generated card report, raw/private
evidence, third-party public-plan capture, cache, bytecode, build product, or
macOS metadata. The normal and full sanitized research editions remain outside
this Git history.

## Version set

| Component | Version |
|---|---:|
| Main XRAY frontend | 4.3.0 |
| Full-report orchestrator | 0.7.0 |
| Diagnostics orchestrator | 2.0.0 |
| Temperature probe | 1.7.0 |
| Controller helper | 4.1.0 / UAPI ABI 6 |
| Thermal helper | 1.5.1 / UAPI ABI 5 |
| Candidate planner | 0.5.0 |
| VFI manifest compiler | 0.5.0 |
| Full-release bundler | 0.4.0 |
| ArchBench submodule | 0.2.0 at `72ec2b6543e036012d7028f39333e8b16c699af7`, `AGPL-3.0-or-later` |

## Validation

- Before the submodule-only packaging change, CPython 3.9, 3.14 and 3.15 each
  passed 307/307 research-edition and 308/308 sanitized-edition tests.
- The current initialized public tree passed all 326 discovered tests with one
  intentional skip for raw third-party source fixtures that are represented by
  a hash-only lineage descriptor and are not redistributed.
- A standalone clean recursive clone at commit `8b457abe0369d84f325c10888c3c635652a30666`
  also discovered all 326 tests and passed with 64 explicit skips. Those skips
  cover the intentionally unshipped retained-hardware fixtures, peer editions,
  and raw third-party fixture—not test failures. Its initialized ArchBench
  submodule matched the pinned `72ec2b6543e036012d7028f39333e8b16c699af7`.
- A clean non-recursive clone passed the same 326-test suite with 65 explicit
  skips: the same 64 standalone-release skips plus the intentionally
  uninitialized ArchBench submodule check. Both clones verified the complete
  release-file set; the final 63-entry manifest is sealed below.
- The generated `UNLOCK_PLAN.md` contract exactly matches the frontend,
  candidate planner, compiler and independent runtime allowlists.
- Retained four-card evidence replay completed all three non-authorizing
  report-planning layers: 4 checklist models, 18 candidate profiles/card,
  72 proposed row-image sets, and 28 compiled-and-verified sealed manifests;
  every applicable row transport was independently checked. The two separated
  route models produced eight per-card statuses and remained historical
  review-only with zero authoritative current manifests.
- Ruff `E9,F` and Pylint errors-only are clean.
- Bandit reports zero high, two bounded medium and 20 low findings; their
  disposition is recorded in the static-validation ledger.
- Semgrep reports ten reviewed owner-only-mode false positives, zero timeouts,
  and two explicit Linux-macro partial-parse warnings. Kbuild, Sparse and
  clang-tidy remain the controlling C evidence.
- The pinned ArchBench repository passed 132/132 host tests, its independent
  532,480-transition ALU check and 3/3 CUDA-disabled CTest targets.

Software validation does not establish electrical fuse-programming safety,
post-cold-boot stability, or health of any unmarked silicon unit.

## Package boundary

The root `SHA256SUMS` covers every regular parent-repository release file except
itself and this non-self-referential ledger. It intentionally excludes the
mode-`160000` gitlink and all submodule content; `.gitmodules`, the ArchBench
lock JSON and the gitlink commit/tree bind that separately hashed repository.
Its entry count and digest are filled only after the final source tree is
frozen:

| Manifest entries | `SHA256SUMS` SHA-256 |
|---:|---|
| 63 | `2b411ba701462e00fb2b3d8f92e48ef117227e1b16ddcf4b2e1471a171fcae1f` |

Repository commit identity is recorded by Git after the manifest is generated;
a commit cannot embed its own final hash. Daybreak scan identity/status is
tracked against that pushed commit rather than written back into the scanned
tree.

## Licensing

Original first-party userspace source and documentation are copyright 2026
Fabiano Anemone (`fabiano.anemone@gmail.com`) and licensed under
AGPL-3.0-or-later. Kernel helpers retain their file-level GPL-2.0 terms and
Linux-syscall-note exceptions. The independent ArchBench submodule is licensed
under `AGPL-3.0-or-later`. See `LICENSE`, `NOTICE`, `.gitmodules`, and the pinned
submodule's own licensing notices. `evidence/public/` contains a first-party
audit and hash-only provenance descriptor; it does not ship or relicense the
referenced raw third-party plans.
