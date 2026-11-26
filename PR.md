# Contribution and release gates

Changes to XRAY are reviewed as evidence tooling for irreversible hardware, not
as ordinary application changes. A pull request is not releasable until every
applicable gate below has a retained command/result or a precise, justified
waiver. “Tests pass” without versions and scope is not evidence.

## Pull-request description

State:

- the problem and exact user-visible outcome;
- files, schemas, ABI fields, registers and access levels changed;
- whether device reads, operational MMIO writes, volatile state, reset, flash,
  OTP, benchmarks or external tools are reachable;
- the source, commit/date, specimen and confidence for every new hardware claim;
- contradictions with older sources and why the selected interpretation wins;
- backward-compatibility and migration behavior;
- tests run, exact runtimes/kernels, and artifacts retained;
- untested paths, external blockers and rollback/recovery limits.

Never use a newer timestamp as the sole reason to trust a source. Direct
descriptor, raw capture, controlled A/B and independently reproduced functional
evidence outrank prose or screenshots.

## Safety and scope review

- [ ] Every CLI path has an explicit access/risk level.
- [ ] Observation commands cannot call volatile/OTP backends.
- [ ] Driver-query side effects are labeled opaque.
- [ ] Every controller read records operational writes and restoration.
- [ ] Unknown fields remain unknown; missing values are not changed to zero.
- [ ] Defect, disable, control, override, status and software-policy planes stay
      distinct.
- [ ] SS0, SS1 and the dedicated FP64 selector/control path are not conflated;
      scalar override names are not presented as Tensor-throughput proof.
- [ ] Media/MIG plans read direct defect words; `STATUS == DISABLE` is never
      used as a zero-defect proof, and bits 146/371 remain an unordered pair.
- [ ] Physical defects are preserved in every candidate.
- [ ] No generic MMIO, arbitrary row, RIR, IFF, key/security or occupied-row
      programming surface was introduced.
- [ ] Experimental execution still requires exact identity, complete images,
      independent replay, receipt, journal, held-state evidence, interactive
      confirmation, prefix checks and cold verification.
- [ ] No test performs a real OTP pulse, glitch, VBIOS flash, GPU reset, MIG
      reconfiguration or ECC injection without a separate hardware protocol and
      explicit authorization.

## Python compatibility and tests

Run the complete suite in both normal and sanitized editions on every supported
runtime available to CI/release engineering. The minimum matrix is CPython 3.9
and the current 3.15; include 3.14 when available.

```sh
python3 -m compileall -q .
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/update_unlock_plan_contract.py --check
python3 170HX-XRAY.py self-test --output /tmp/xray-self-test-new.json
python3 170HX-XRAY.py registry-audit \
  --kernel-source kernel/170hx_xray.c \
  --output /tmp/xray-registry-new.json
```

Required test classes include:

- parser/import/export round trips for every supported JSON, JSONL, text and
  binary format;
- duplicate-key, NaN/Inf, bool/int/float alias, missing/extra key and malformed
  container mutation tests;
- full BDF/PCI/UUID identity and multi-card ordering;
- Python/kernel register allowlist equality and direct-header conflicts;
- FF/IFF/RIR replay, complete image, frontier/gap, prefix and recovery proofs;
- per-specimen HBM/SM/pipe/media defect arithmetic;
- manifest compilation, receipt, journal, interruption and cold-verifier fault
  injection with a fake controller only;
- full-dump create-only modes, hash manifest, relocation and tamper refusal;
- full-report five-state inventory (`completed`, `failed`, `skipped`,
  `not-yet-supported`, `not-requested`) and independent benchmark-power
  aggregate recomputation;
- normal/sanitized behavior parity and the sanitized boundary test.

Do not update expected counts simply to make a test pass. Explain every changed
count and independently verify the new set.

## Python static analysis

Use pinned tool versions in CI when they are available. A tool being unavailable
is a recorded waiver, not a silent pass.

```sh
ruff check --select E9,F 170HX-XRAY.py otp_vfi_append.py diagnostics tools
pylint --errors-only --jobs=1 \
  170HX-XRAY.py otp_vfi_append.py diagnostics/*.py tools/*.py
black --check .
mypy --strict 170HX-XRAY.py diagnostics tools
bandit -r 170HX-XRAY.py diagnostics tools
semgrep scan --config p/python --config p/c --metrics=off \
  170HX-XRAY.py otp_vfi_append.py diagnostics tools kernel
pip-audit
```

Repository style is the controlling format where an automatic formatter would
produce a broad unrelated rewrite. Resolve or document each warning; never hide
a hardware-safety finding behind a global ignore.

## Kernel and C/CUDA gates

Use headers matching the test kernel and retain compiler/module hashes:

```sh
make -C kernel clean
make -C kernel
make -C kernel abi-test
make -C kernel -f Makefile.therm clean
make -C kernel -f Makefile.therm
make -C kernel -f Makefile.therm abi-test
make -C kernel -f Makefile.otp-spec test
```

When installed:

```sh
make -C kernel C=1 CHECK='sparse'
scripts/checkpatch.pl --strict kernel/*.c kernel/*.h
clang-format --style=file:/path/to/linux/.clang-format --dry-run --Werror \
  kernel/*.c kernel/*.h
clang --analyze kernel/170hx_xray.c
clang-tidy diagnostics/ga100_diag.cu -- <reviewed CUDA compile flags>
```

Use the matching Linux kernel tree's `.clang-format` for kernel sources; never
substitute a userspace preset or blindly rewrite kernel C. For CUDA/userspace
C, first compare the formatter result and reject changes that obscure CUDA or
ABI semantics.

For the recorded Linux 6.12.116 Clang gate, configure and build an exact source
tree with `LLVM=1`, generate its `Module.symvers`, and then run external-module
Kbuild with `LLVM=1 W=1 KCFLAGS=-Werror`. Do not filter the GCC-only flags from
the running Debian-header compilation database and call that a Clang Kbuild.
The formerly unavailable-tool, GCC-flag, and 780-format-difference notes are
historical; the resolved commands and results are in
[`docs/STATIC_AND_KERNEL_VALIDATION_2026-11-26.md`](docs/STATIC_AND_KERNEL_VALIDATION_2026-11-26.md).

KASAN/KCSAN/lockdep stress requires a dedicated compatible host and must record
kernel configuration, workload, duration, dmesg/Xid deltas and module lifetime.
Absence of that host is an explicit release limitation.

## Live observation and benchmark validation

Safe live validation is proportional to the change:

- Level 1 on all selected functions;
- Level 2 with exact helper identity and before/after Xid/utilization checks;
- Level 3 only in an exclusive maintenance window, normally unbound, two passes
  per MAIN/RIR image, checked entry-state restoration and zero program commands;
- converted `20b5` and held `2080` specimens only when physically present;
- benchmark families at the declared limit (release default 150 W), with
  pre/enforced/post/restored limit and sampled min/max/average draw;
- typed storage, P2P direct/host, GDS, media, MIG/ECC inventory and ArchBench
  only when their prerequisites are available.

The final report must say exactly which levels and families are `completed`,
`failed`, `skipped`, `not-yet-supported`, or `not-requested`. It must not fold
unrequested work into `skipped`.

## Documentation and evidence synchronization

- [ ] `README.md`, `USAGE.md`, plan models and credits match implementation.
- [ ] Register/field entries include certainty, source and limitation.
- [ ] Historical artifacts are labeled historical; no old 11/7-profile bundle
      is relabeled as current 12/8-profile output.
- [ ] Normal and sanitized trees differ only at documented provenance boundaries.
- [ ] The sanitized boundary scan includes regular files, binaries and caches;
      caches/build products are removed before the final scan.
- [ ] Release hashes are generated only after every file is frozen.
- [ ] Demo reports contain no secret unless publication was explicitly approved.
- [ ] Links and Markdown render correctly on GitHub.
- [ ] Upstream glitch-tool safety deltas (VRM-unlock checks, cooldown and
      rollback reports) are pinned by commit and not promoted to electrical
      qualification.

## Independent review

At least one reviewer should reproduce security-critical decoders, arithmetic,
manifest/prefix checks and access gates without importing the producer's result.
When available, an additional review may use Astra, Fable, Neon, Daybreak
Blue/Red, Astra Cyber, Fable/Mythos Cyber or another independent security/code
review system. These names are suggestions, not claims that a service is
installed or that an automated pass replaces human/hardware review.

For a security-relevant finding, preserve the exact tool/version/configuration,
input hash, raw finding, disposition and regression test. Do not paste secrets,
private source or proprietary binaries into an external service without explicit
authorization.

## Release closure

Before merge/release:

1. freeze implementation and docs;
2. rerun all normal/sanitized runtime suites and boundary scans;
3. rebuild helpers and ABI tests on the target Linux kernel;
4. generate a fresh complete report with explicit skips;
5. regenerate `RELEASE_HASHES.md` and verify every listed file;
6. verify the repository from a clean clone;
7. tag/commit the exact tested tree; and
8. publish only after the commit, report and hashes agree.

No green software gate proves that a fuse image is electrically safe or that an
unmarked hardware unit is healthy.
