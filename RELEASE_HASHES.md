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
| Embedded ArchBench | 0.2.0, source-only, MIT |

## Validation

- CPython 3.9: research 307/307 and sanitized 308/308 passed.
- CPython 3.14: research 307/307 and sanitized 308/308 passed.
- CPython 3.15: research 307/307 and sanitized 308/308 passed.
- An isolated public-tree run discovered 308 tests and passed; 64 were
  explicitly skipped because retained card evidence, the peer edition, or
  unredistributed public-source lineage was absent.
- The generated `UNLOCK_PLAN.md` contract exactly matches the frontend,
  candidate planner, compiler and independent runtime allowlists.
- Ruff `E9,F` and Pylint errors-only are clean.
- Bandit reports zero high, two bounded medium and 20 low findings; their
  disposition is recorded in the static-validation ledger.
- Semgrep reports ten reviewed owner-only-mode false positives, zero timeouts,
  and two explicit Linux-macro partial-parse warnings. Kbuild, Sparse and
  clang-tidy remain the controlling C evidence.
- The embedded ArchBench source snapshot preserves its MIT license and passed
  its independent 532,480-transition host ALU check before vendoring.

Software validation does not establish electrical fuse-programming safety,
post-cold-boot stability, or health of any unmarked silicon unit.

## Package boundary

The root `SHA256SUMS` covers every regular release file except itself and this
non-self-referential ledger. Its entry count and digest are filled only after
the final source tree is frozen:

| Manifest entries | `SHA256SUMS` SHA-256 |
|---:|---|
| 156 | `f35692792e8ded9f671ebd3e7425d1e6b4b5cc961888dde9c5e083c50dde4313` |

Repository commit identity is recorded by Git after the manifest is generated;
a commit cannot embed its own final hash. Daybreak scan identity/status is
tracked against that pushed commit rather than written back into the scanned
tree.

## Licensing

Original first-party userspace source and documentation are copyright 2026
Fabiano Anemone (`fabiano.anemone@gmail.com`) and licensed under
AGPL-3.0-or-later. Kernel helpers retain their file-level GPL-2.0 terms and
Linux-syscall-note exceptions. Embedded ArchBench retains its original MIT
license and ArchBench contributor copyright. See `LICENSE`, `NOTICE`, and the
vendored subtree notices.
