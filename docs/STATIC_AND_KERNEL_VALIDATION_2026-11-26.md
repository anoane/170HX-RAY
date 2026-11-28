# Static and kernel validation — 2026-11-26

This is a **historical 2026-11-26 addendum** for the then-current research and
sanitized trees. Its Python results and helper-4.1 source hashes must not be
presented as validation of the later helper-4.2 tree. The fresh controlling
source-only helper result is
[STATIC_AND_KERNEL_VALIDATION_2026-11-28.md](STATIC_AND_KERNEL_VALIDATION_2026-11-28.md).

It was a reproducible addendum for those then-current trees.
It supersedes the historical 4.2 notes that Ruff, Pylint, Bandit and Semgrep
were unavailable, that Clang Kbuild was blocked by GCC-only Debian-header flags,
and that Linux `clang-format` still reported 780 differences.

No module was loaded and no GPU register or workload was touched by this pass.

## Tool versions

Local Python checks ran on Python 3.15.2 with:

| Tool | Version |
|---|---:|
| Ruff | 0.17.6 |
| Pylint / astroid | 4.1.4 / 4.3.4 |
| Bandit | 1.9.4 |
| Semgrep OSS | 1.187.0 |

Kernel checks ran on `ai-ws`, Linux `6.12.116+deb13-amd64`, with:

| Tool | Version |
|---|---:|
| Linux source | Debian `linux-source-6.12=6.12.116-1` |
| Clang / clang-format / clang-tidy | Debian LLVM 19.1.7 |
| GCC | Debian 14.2.0 |
| Sparse | 0.6.4 |
| GNU Make | 4.4.1 |

The pinned source-tree `.clang-format` SHA-256 is
`32679a26a81ca0346045382f17c1e3af78922cc7f09ae0a65914a5958f55da44`.

## Python static analysis

The operational set is the frontend, `otp_vfi_append.py`,
`diagnostics/*.py`, and `tools/*.py`, independently checked in both
editions.

```sh
ruff check --select E9,F 170HX-XRAY.py otp_vfi_append.py diagnostics tools
pylint --errors-only --jobs=1 170HX-XRAY.py otp_vfi_append.py diagnostics/*.py tools/*.py
bandit -q -r 170HX-XRAY.py otp_vfi_append.py diagnostics tools -x '*/tests/*'
semgrep scan --config p/python --config p/c --metrics=off \
  --timeout=60 --timeout-threshold=0 \
  --exclude reports --exclude tests --exclude '**/__pycache__/**' \
  170HX-XRAY.py otp_vfi_append.py diagnostics tools kernel
```

Results in each edition:

- Ruff `E9,F`: zero findings. Five real unused/control-flow findings were
  removed before the clean rerun. The repository's inherited all-rules profile
  still reports 1,755 mostly modernization/style findings; those were not
  mass-rewritten into safety-critical code.
- Pylint errors-only: zero findings with `--jobs=1`. Pylint's multiprocess
  `--jobs=0` path is incompatible with Python 3.15's removed `co_lnotab`;
  that host-tool crash is not a source pass.
- Bandit: zero high, two medium, and 20 low findings in each edition, over
  37,933 research-edition and 37,889 sanitized-edition lines; no `nosec`
  exclusions.
  The two medium findings are the bounded stdlib XML parse of local
  `nvidia-smi` output (B314) and deliberate execution of the already-read,
  SHA-256-recorded sibling Python module buffer (B102). The low findings are
  subprocess/import heuristics and false hard-coded-secret matches on schema
  booleans and counters.
- Semgrep: 153 community rules over the operational/kernel set found ten
  warnings, all from one rule incorrectly calling mode `0700`
  "widely permissive"; `0700` is deliberately owner-only and changing it to
  the rule's suggested `0644` would weaken confidentiality. There were no
  substantive security findings. The final extended-timeout run had zero rule
  timeouts. The OSS parser still reports two partial-parse errors on Linux
  module macros, so successful Kbuild, Sparse and clang-tidy are the
  controlling C evidence.

## Kernel format and compiler gates

Exactly these eight first-party files were formatted; no generated, report, or
vendor file was included:

```text
kernel/170hx_xray.c
kernel/170hx_xray_otp_spec.h
kernel/170hx_xray_otp_spec_test.c
kernel/170hx_xray_therm.c
kernel/170hx_xray_therm_uapi.h
kernel/170hx_xray_therm_uapi_abi_test.c
kernel/170hx_xray_uapi.h
kernel/170hx_xray_uapi_abi_test.c
```

The source tree was prepared with `make LLVM=1 defconfig`,
`make LLVM=1 olddefconfig`, `make LLVM=1 prepare modules_prepare`, and a
complete `make -j32 LLVM=1`. This produced a Clang-configured
`Module.symvers` and avoided filtering the running Debian header tree's
GCC-only `-fmin-function-alignment=16`, `-fconserve-stack`, and
`-mrecord-mcount` flags.

The following all passed:

```sh
clang-format-19 --dry-run --Werror   --style=file:/tmp/170hx-linux-6.12.116-llvm/.clang-format kernel/*.[ch]

make -C /tmp/170hx-linux-6.12.116-llvm M=/tmp/xray-module   LLVM=1 W=1 KCFLAGS=-Werror modules

make -C /lib/modules/$(uname -r)/build M=/tmp/xray-module-gcc   W=1 KCFLAGS=-Werror modules

make -C /lib/modules/$(uname -r)/build M=/tmp/xray-module-sparse   C=2 CHECK=sparse W=1 KCFLAGS=-Werror CF=-Wsparse-error modules

clang-tidy-19 -p /tmp/xray-module   -checks=-*,linuxkernel-*   /tmp/xray-module/170hx_xray.c   /tmp/xray-module/170hx_xray_therm.c

make -C kernel abi-test HOSTCC=gcc
make -C kernel -f Makefile.therm abi-test HOSTCC=gcc
make -C kernel -f Makefile.otp-spec test CC=gcc
# The same three host tests also passed with clang-19.
```

The formatter changed layout only:

- stripped pre/post-format `170hx_xray.o` files are byte-identical;
- stripped pre/post-format `170hx_xray_therm.o` files are byte-identical;
- stripped controller and thermal ABI-test executables are byte-identical;
- the OTP test's executable differs only in assertion `__LINE__` metadata
  caused by line wrapping, and its behavior passes under both GCC and Clang.

The research and sanitized kernel source sets remain byte-identical.
