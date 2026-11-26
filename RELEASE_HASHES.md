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
| Full-report orchestrator | 0.5.0 |
| Diagnostics orchestrator | 2.0.0 |
| Temperature probe | 1.7.0 |
| Controller helper | 4.1.0 / UAPI ABI 6 |
| Thermal helper | 1.5.1 / UAPI ABI 5 |
| Candidate planner | 0.5.0 |
| VFI manifest compiler | 0.5.0 |
| Full-release bundler | 0.4.0 |
| ArchBench submodule | 0.2.0 at `ff3cc65815b71602760ff569d33f9a2fcdf32add`, `AGPL-3.0-or-later OR MIT` |

## Validation

- Before the submodule-only packaging change, CPython 3.9, 3.14 and 3.15 each
  passed 307/307 research-edition and 308/308 sanitized-edition tests.
- The current initialized public tree passed all 312 discovered tests with one
  explicit skip for an unavailable optional fixture.
- A clean recursive clone passed all 312 tests with 64 explicit skips for
  retained card evidence, the missing peer edition and unredistributed source
  lineage. A clean non-recursive clone passed the same suite with 65 skips and
  its ArchBench path failed closed with the documented submodule-init command.
- The generated `UNLOCK_PLAN.md` contract exactly matches the frontend,
  candidate planner, compiler and independent runtime allowlists.
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
| 60 | `e7bdc8e749de433ccddeb516d543b26551d5be1a2f7ecdd10554e9b4aa4589dd` |

Repository commit identity is recorded by Git after the manifest is generated;
a commit cannot embed its own final hash. Daybreak scan identity/status is
tracked against that pushed commit rather than written back into the scanned
tree.

## Licensing

Original first-party userspace source and documentation are copyright 2026
Fabiano Anemone (`fabiano.anemone@gmail.com`) and licensed under
AGPL-3.0-or-later. Kernel helpers retain their file-level GPL-2.0 terms and
Linux-syscall-note exceptions. The independent ArchBench submodule is offered
under `AGPL-3.0-or-later OR MIT`, preserving its original MIT grant and
contributor copyright. See `LICENSE`, `NOTICE`, `.gitmodules`, and the pinned
submodule's own licensing notices.
