# Static and kernel validation — 2026-11-28

This is the controlling source-only validation for controller helper 4.2.0,
thermal helper 1.5.1 and the CUDA diagnostic after the 2026-11-28 upstream
reconciliation. It supersedes current-tense use of the dated 2026-11-26
helper-4.1 result; it does not rewrite that historical ledger.

No module was loaded. No GPU command, BAR/MMIO transaction, reset, benchmark,
voltage glitch, VBIOS action or OTP operation was performed.

## Host and toolchain

Validation ran on `ai-ws`, Linux `6.12.116+deb13-amd64`, from a new
source-only directory under `/tmp`.

| Tool | Version or source |
|---|---|
| GCC | 14.2.0 |
| Clang / clang-format / clang-tidy | Debian LLVM 19.1.7 |
| CUDA compiler | nvcc 13.5.37 |
| LLVM Linux build tree | Debian Linux 6.12.116 |
| Running GCC headers | `/lib/modules/6.12.116+deb13-amd64/build` |
| Linux formatter policy | SHA-256 `32679a26a81ca0346045382f17c1e3af78922cc7f09ae0a65914a5958f55da44` |

## Exact validated source identities

| File | SHA-256 |
|---|---|
| `kernel/170hx_xray.c` | `a3d4195e77827c5d06777c63aad6b3940802f691b70951b7a44378e06b6fa408` |
| `kernel/170hx_xray_therm.c` | `e368edce88a64e51b6180ce385a6b068780bc812a3f23a11753881ef62352edf` |
| `kernel/170hx_xray_uapi.h` | `7f19a2878095c81ef50345c71dee5a51238861b5e972d1fb3ccdf2103c6c9a5f` |
| `kernel/170hx_xray_therm_uapi.h` | `b6eac88e942c465d7ee5f91bb0668836fbf6313ac8663a7016072dbf8dde39bb` |
| `kernel/170hx_xray_otp_spec.h` | `8e7f6c8353da93be4a5606d599c54325b35ca0a471eb6b28806da66b7f008a18` |
| `kernel/170hx_xray_otp_spec_test.c` | `040caa88cd3e6b81ca46220f9364a8a4f6f5a8b2fdb67909ba710c1078675f69` |
| `kernel/170hx_xray_uapi_abi_test.c` | `86ea291e6f697bfd51b9d030a55f49b5490f5ef9e1b714a7fb27f3e93e9132e8` |
| `kernel/170hx_xray_therm_uapi_abi_test.c` | `28c471ea39236fca73bd1b731653d86205fac5e122a396ba7e00af11c8b8c936` |
| `diagnostics/ga100_diag.cu` | `c1288b9f7778f13d6d2008537af3976602fb7804f2f8e812014f445b585ff95d` |

The controller source was formatted with clang-format 19 and the exact Linux
6.12.116 policy above after the expanded read allowlist exposed 1,065 format
diagnostics. The change was layout-only.

## Passed gates

The following all passed against the exact hashes above:

- controller UAPI ABI 6 and thermal UAPI ABI 5 host tests with GCC and Clang;
- hardware-inert OTP state-machine and fault-injection test with both
  compilers, explicitly reporting that no hardware backend exists;
- external-module LLVM Kbuild with `LLVM=1 W=1 KCFLAGS=-Werror`;
- external-module GCC Kbuild with `W=1 KCFLAGS=-Werror`;
- Sparse with `C=2 CHECK=sparse W=1 KCFLAGS=-Werror CF=-Wsparse-error`;
- clang-format 19 `--dry-run --Werror` over the eight first-party kernel
  C/header files using the pinned Linux policy;
- clang-tidy 19 with `-checks=-*,linuxkernel-*` over both modules; and
- CUDA 13.5 compile-only for `sm_80` with
  `-std=c++17 -Xcompiler=-Wall,-Wextra,-Werror`.

The CUDA gate initially found use of NVML's API-13-deprecated
`nvmlDeviceGetTemperature()`. The source now uses
`nvmlDeviceGetTemperatureV()` on NVML API 13 or newer and retains a
compile-time legacy fallback for older supported headers. The final strict
compile passes; the resulting object SHA-256 is
`7fee16538e7526bb55adeef8187a5b1c9d5459629cc6558073e142c3bfacf416`.

## Format-equivalence and build identities

Stripped pre/post-format artifacts are byte-identical:

| Artifact | SHA-256 |
|---|---|
| LLVM controller object | `d92dac801bcdf3513f8829aab187abce20fc4691a09b1d31a9ea928a4fdbce83` |
| LLVM thermal object | `7220f180f9ef7306d0f9569c30be49ca4d56213de7fd5afa356401fa6dfe0910` |
| Controller ABI executable | `f85269a4ddbe03ae1c96540553c34e757996e21e8bb8e026b09c3034e5044136` |
| Thermal ABI executable | `b3aa9811f8596af75e596cd6bab6348869664fc11f98d458160d25e163a8af04` |
| OTP host-test executable | `3ad006b2e284edf35c541afdc1921a87f553557644d5a87e12f5209c85087fef` |

Final module SHA-256 values:

| Compiler | Controller | Thermal |
|---|---|---|
| LLVM 19 | `6e2ac3d3d05786ac9d76ada914054cb403883469a3e30dd9ae8b83363888d799` | `c55591ef99be90ba62a887182a724e1bc26617e14cc4d3bc91513df291691dc9` |
| GCC 14 | `3a80a476e1bbfd2c239e2bb7e7e9ba94f609695597e0c9ffb5281c14c120281d` | `9836466574b355a9ce738927ff96ed4dadde47ea742cae6132a93ef655826417` |

## Advisory checkpatch result

Linux `checkpatch.pl --strict --no-tree` is **not clean**: zero errors, 57
warnings and 78 checks. Most findings are advisory preferences around split
literals/comments, `BIT()`, userspace UAPI fallback typedefs, host-test SPDX
comment style and macro-side-effect heuristics. They are not hidden or called a
pass; successful Kbuild, Sparse, the pinned formatter, clang-tidy and ABI tests
remain the controlling compile/static evidence.

## Boundary

These green gates establish source compilation, ABI shape, format consistency
and hardware-inert state-machine behavior only. They do not establish that a
fuse waveform is electrically safe, close the userspace irreversible-backend
ownership race, validate a held `2080` state, prove cold recovery or authorize
an OTP pulse.
