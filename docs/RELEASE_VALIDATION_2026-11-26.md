# 170HX-XRAY release validation — 2026-11-26

This ledger records the current 4.3.0 source admission and references the
accepted 4.2.0 demonstration report as private immutable historical evidence. It
distinguishes passes, bounded warnings, pending external scans, and actions
that were deliberately not executed. A source hash proves byte identity; it
does not prove that an irreversible fuse plan is electrically safe.

## Pre-submodule 4.3.0 runtime matrix

| Surface | Python 3.9 (Docker) | Python 3.14.8 | Python 3.15.2 |
|---|---:|---:|---:|
| Research edition | 307/307 pass | 307/307 pass | 307/307 pass |
| Sanitized edition | 308/308 pass | 308/308 pass | 308/308 pass |

All runs used `PYTHONDONTWRITEBYTECODE=1`. The sanitized extra test enforces
the provenance boundary. The current plan-document test parses the generated
canonical appendix and requires exact equality with the frontend, candidate,
compiler and independent runtime contracts; it no longer relies only on token
presence plus a digest. `git diff --check` passes for the research worktree.

Those research/sanitized counts predate the source-only ArchBench extraction
and are retained rather than silently rewritten. After the extraction, the
initialized clean public tree passed all 312 discovered tests with one explicit
skip for an unavailable optional fixture. A clean recursive clone passed all
312 tests with 64 explicit skips for retained card evidence, the missing peer
edition and unredistributed source lineage. A clean non-recursive clone passed
the same suite with 65 skips: the additional skip is the test requiring
initialized submodule Git metadata. Attempting an ArchBench report in that
state failed with the actionable initialization instruction. Those skips are
visible prerequisites, not synthetic passes; deterministic parser, planner and
VFI state-machine coverage remains active.

## Current addendum (supersedes historical toolchain blockers)

The current source has completed the formerly unavailable Ruff, Pylint,
Bandit, and Semgrep passes. The former Clang Kbuild flag conflict and the 780
Linux-format differences are also resolved. Exact versions, commands,
bounded findings, the clean LLVM/GCC/Sparse/clang-tidy gates, and byte-level
semantic comparison are recorded in
[`STATIC_AND_KERNEL_VALIDATION_2026-11-26.md`](STATIC_AND_KERNEL_VALIDATION_2026-11-26.md).
The counts and report hashes below remain a historical 4.2 validation snapshot;
they are not current admission counts.

## Frozen-source runtime matrix

| Surface | Python 3.9 (Docker) | Python 3.14 | Python 3.15 |
|---|---:|---:|---:|
| Research edition | 287/287 pass | 287/287 pass | 287/287 pass |
| Sanitized edition | 288/288 pass | 288/288 pass | 288/288 pass |

Additional Python checks:

- Python 3.9 grammar parsing passed for 24 research-edition and 25
  sanitized-edition Python files.
- Mypy passed for all 11 selected operational files in each edition with zero
  findings.
- Every command-line `--help` entry point returned zero.
- Offline self-test and registry audit passed in both editions.
- The four new public-model regressions passed in both editions. They bind the
  current poison-first/opt-in classifier model, the inter-shot cooldown, the
  exact-but-non-executable SS0/SS1 IFF candidates, and the non-executable
  conventional DP/ECC candidates.

## Linux helper validation

The exact frozen kernel sources were checked on `ai-ws` with Linux
`6.12.116+deb13-amd64`.

| Check | Result |
|---|---|
| Controller helper UAPI ABI 6 | PASS |
| Thermal helper UAPI ABI 5 | PASS |
| Hardware-inert OTP state-machine test | PASS |
| GCC Kbuild with `W=1` and `KCFLAGS=-Werror` | PASS |
| Sparse with `C=2` and `CF=-Wsparse-error` | PASS |
| Clang Kbuild with `LLVM=1 W=1 KCFLAGS=-Werror` | PASS against an exact Linux 6.12.116 source tree configured and built with LLVM 19; this is intentionally distinct from the GCC-configured running-header tree |
| Clang-tidy Linux-kernel checks | PASS; LLVM 19 compilation database generated from the successful external-module Kbuild, zero displayed source findings |
| Linux 6.12 pinned `clang-format` | PASS; exactly the eight first-party C/H files were formatted and the `--dry-run --Werror` recheck is clean |
| Research/sanitized kernel byte parity | PASS; 11/11 files identical |

The former Clang and format blockers are resolved rather than filtered. The
official Debian 6.12.116 source was configured and built with `LLVM=1`, so the
external module did not inherit GCC-only flags from the running-header tree.
Formatting changed layout only: stripped pre/post-format objects for both
kernel modules are byte-identical. Both ABI-test executables are also
byte-identical; the OTP host-test executable differs only in assertion line
metadata and passes under GCC and Clang.

## Package and sanitization audit

- Diagnostics, kernel, tools, and the guarded OTP backend are byte-identical
  across editions.
- Forty-five common package files are byte-identical. Seven differences are
  expected provenance/test substitutions, and the sanitized edition has one
  additional boundary test.
- The final sanitized raw-byte scan covers the complete source and bundled
  demonstration-report tree and finds zero prohibited provenance tokens.
- No bytecode, cache, `.DS_Store`, kernel build product, symlink, or special
  file remained in either frozen package.
- All shipped text files are valid UTF-8 with LF line endings, final newlines,
  and no trailing whitespace.
- The local Markdown-link and fragment audit found zero fragment failures.
  The package-wide checksum and release ledger are regenerated only after the
  demonstration report and this validation record are final.

## Live non-destructive validation

The final standalone diagnostics 2.0.0 campaign completed on all four selected
CMP 170HX functions. It covered feature inventory, the temperature census,
typed storage bandwidth, direct and host-staged P2P, GDS, and media tests. Each
executed benchmark scope records the requested 150 W limit, observed limit
before/after enforcement and restoration, power-draw minimum/maximum/average,
sample count, failures, interval, and command provenance.

- Artifact:
  `research-evidence/170hx-xray-ai-ws-diagnostics-final-v2.0.0-2026-11-26/diagnostics.json`
- SHA-256:
  `3a4447cda8110510daa70b2d6d0d9121e0762d6a0f2cc8348625d0d59f2ad0e8`
- Result: `completed`; all four selected cards restored to 150 W and
  persistence mode; no Xid, kernel BUG, or kernel WARNING was observed.

This result is evidence for the tested current `20c2` state. It is not proof
of post-conversion, held-glitch, MIG-isolation, ECC-correction, or permanently
unlocked media behavior.

## Historical report and public-source seal

The research-edition demonstration report and its sanitized counterpart remain
private sealed `ai-ws` artifacts. They are not part of this source release.
Their historical hashes below identify the validation input without implying
that the files can be downloaded from this repository. A fresh clone instead
generates specimen-local output with `full-report` or `full-report-bench`.

| Item | Status |
|---|---|
| Authoritative report status | `complete-with-explicit-skips`; 8 completed, 1 not requested, 2 not yet supported, 1 skipped, 0 failed |
| Research report index SHA-256 | `4fd314991d8b3201924a232ba17f37d0a228c7b15cb5b75ede4f0dc58368c882` |
| Sanitized report index SHA-256 | `72660782d29ccf31b520bf7a713324042e7feecea1e14c00953081da0ece8271` |
| Rendered Markdown report SHA-256, both editions | `d6f082bb698f4654ba2782cf8fe38cf0ec88ab63b07ad85acd58b02311540cf1` |
| Research report `SHA256SUMS` SHA-256 | `e5bdb1dd9c8675c79e60cdc7a20de31d67cf58fa1d02baf298379c59a24b247e` |
| Sanitized report `SHA256SUMS` SHA-256 | `3df741b588faa1ae676e9bbc8431763b6e315eba444b67b678ea3f993edc801f` |
| Package `SHA256SUMS` | `SEALED`; exact digest is recorded in the non-self-referential `RELEASE_HASHES.md` ledger |
| Git commit | recorded by repository history; query `git rev-parse HEAD` because a commit cannot embed its own hash |
| GitHub contents | source-only; reports, raw/private evidence, caches and build products are excluded |
| Licensing | first-party userspace/docs AGPL-3.0-or-later; kernel retains its stated GPL terms; the independent ArchBench submodule is `AGPL-3.0-or-later OR MIT` |
| Daybreak security scan | recorded after the clean repository is pushed; an absent or unfinished scan is never represented as clean |

A failed or unfinished external scan with zero findings must never be reported
as clean.

## Deliberately unexecuted actions

No validation in this ledger performed a voltage glitch, OTP program pulse,
conventional-row or IFF burn, VBIOS flash, GPU reset, MIG reconfiguration, ECC
error injection, or cold-power-cycle migration. The blank-FF append executor
remains experimental and requires a fresh exact-card held-state capture,
sealed manifest and receipt, explicit transaction strategy, interactive
confirmations, external electrical qualification, and a separate maintenance
window.
