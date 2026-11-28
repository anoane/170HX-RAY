# 170HX-XRAY release validation — 2026-11-26

This is a **historical 2026-11-26 source-release ledger**. It records the
then-current 4.3.0 source admission and references the accepted 4.2.0
demonstration report as private immutable historical evidence. It
distinguishes passes, bounded warnings, pending external scans, and actions
that were deliberately not executed. A source hash proves byte identity; it
does not prove that an irreversible fuse plan is electrically safe.

The counts, hashes, clone results and private-report references below must not
be read as validation of the later 2026-11-28 source.

## 2026-11-28 current-source pointer

The source now identifies frontend **4.4.0**, kernel helper **4.2.0**,
compiler and candidate planner **0.6.0**, runtime backend **1.1.0**, and
packager **0.6.0** with **bundle schema v2**. The separate irreversible
artifact is now **sealed manifest v3**; bundle schema v2 does not make a v2 OTP
manifest current. The planner inventory is 24 profiles: 12 operational/offline
and 12 zero-operation review-only profiles. Exactly seven profile classes
remain compiler/runtime executable.

The private demonstration report remains historical evidence and is still not
redistributed by this source-only edition. Exact current-source commands,
results, skips and qualifications are recorded separately by the final
2026-11-28 validation pass.

## Historical pre-submodule 4.3.0 runtime matrix

| Surface | Python 3.9 (Docker) | Python 3.14.8 | Python 3.15.2 |
|---|---:|---:|---:|
| Research edition | 307/307 pass | 307/307 pass | 307/307 pass |
| Sanitized edition | 308/308 pass | 308/308 pass | 308/308 pass |

All runs used `PYTHONDONTWRITEBYTECODE=1`. The sanitized extra test enforces
the provenance boundary. The then-current plan-document test parsed the generated
canonical appendix and requires exact equality with the frontend, candidate,
compiler and independent runtime contracts; it no longer relies only on token
presence plus a digest. `git diff --check` passes for the research worktree.

Those research/sanitized counts predate the source-only ArchBench extraction
and are retained rather than silently rewritten. After the extraction, the
initialized public tree passed all 326 discovered tests with one intentional
skip for raw third-party source fixtures that are not redistributed. Their
hashes and provenance remain bound by `evidence/public/public-source-lineage.json`,
and the first-party public-plan audit is still exercised. Deterministic parser,
planner and VFI state-machine coverage was exercised.

Two source-release clone gates were then run at
`b313530c716679c08c1fe22f38786ee8c7020bd3`. A clean recursive clone passed all
326 discovered tests with 64 explicit skips and initialized ArchBench at the
exact pinned commit `e0852fa34933c605fca780180d23c13af2927265`. A clean
non-recursive clone passed all 326 with 65 explicit skips; the only additional
skip was the deliberately uninitialized submodule check. The other standalone
clone skips are the expected retained-hardware, peer-edition, cold-verifier,
receipt-integration, and raw-third-party fixtures that this source release does
not redistribute. Neither clone had a failure. The final 63-entry root
`SHA256SUMS` file is sealed after this ledger update and reverified below.

The 0.7.0 full-report contract was also replayed end to end against retained
four-card evidence. The verified full-dump contained all nine MAIN formats,
all four RIR formats, all four register formats, and all four checklist models
for four cards. The historical candidate layer emitted
the exact 18-profile inventory per card; all four whole-plan transports and
all 72 proposed-image sets (four cards times 18 profiles, each in four row
formats) replayed identically. The sealed layer compiled and independently
verified all 28 admitted same-baseline sibling manifests (four cards times seven
profiles). The route layer emitted both final-state models, eight per-card
statuses and four semantically equal transports for each route and the route
index. Because the input was offline retained evidence, every route was marked
historical review-only and exposed zero authoritative live manifests.
Full-report 0.7 explicitly supports only initial `000-baseline` packaging; the
epoch-001/002 transitions remain forecasts, not implemented promotion logic.
This was non-authorizing validation: no benchmark, power setter, live capture,
OTP command, VBIOS write or reset ran.

The final external demonstration copy is
`170HX-XRAY_FULL_OUTPUT_2026-11-26_192125Z` (not committed to this source
repository). Its `report-index.json` SHA-256 is
`22dcee983d84e4b2a65e8ee92ccae1e4e35798d45a4a191b1f2a7d7e676ffbaa`,
the rendered Markdown SHA-256 is
`ca7d69b916b10b3fce1469aded657d95346559b0515b1cd75e78f46219ae89a2`,
and its 1,357-entry outer `SHA256SUMS` SHA-256 is
`333976d7edd921a088f24884977582db90ab76dc3d9f1d2b0fef4c9a72333fb6`.
The exact-file-set, nested sibling checksum, route transports, modes, command
journals and source-tool hashes were independently rechecked after transfer.
The report binds full-report source SHA-256
`77f07bf46bca87c578df1af75c5bd1966665ef97e90b20039355396f5e0fca28`;
its supplied historical capture is classified by explicit provenance rather
than timestamp and yielded zero authoritative live manifests.

## Historical 4.3.0 toolchain addendum

The then-current 4.3.0 source completed the formerly unavailable Ruff, Pylint,
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
- After correcting stale expected profile/census counts exposed by the final
  NVENC registry additions, the offline self-test and registry audit passed;
  the discovered test suite now invokes the CLI self-test as a regression gate.
- The four new public-model regressions passed in both editions. They bind the
  then-current poison-first/opt-in classifier model, the inter-shot cooldown, the
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

This result is evidence for the then-tested `20c2` state. It is not proof
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
| Licensing | first-party userspace/docs AGPL-3.0-or-later; kernel retains its stated GPL terms; the independent ArchBench submodule is `AGPL-3.0-or-later` |
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
