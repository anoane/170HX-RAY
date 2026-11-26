# Legacy OTP executor safety specification (hardware-inert v1)

Status: **offline v1 tooling and host tests only**. This document describes the
older inert specification/oracle, not the experimental v2 append backend. There
is still no OTP programming ioctl, generic MMIO write primitive, or GPIO backend
in the observation module, and its UAPI remains read-only. The distinct
`170hx-xray-otp-sealed-manifest-v2` lifecycle and user-space blank-FF transport
are documented in the README; a v1 file is never upgraded implicitly.

## What is implemented

The repository-level `tools/170hx_otp_executor_spec.py` independently validates
one exact candidate profile and emits a sealed, explicitly non-executable
manifest. It binds:

- one canonical PCI BDF and one profile ID;
- all 512 baseline MAIN words, all eight RIR words, and their byte hashes;
- all 512 proposed MAIN words and their byte hash;
- the exact ordered row allowlist and every expected blank old word;
- every append prefix hash, unchanged active-IFF signature, quarantine rows,
  and a contiguous append-only recovery reserve;
- one-attempt/no-retry, source-low, full postverify, and durable-journal rules.

The compiler rejects duplicate JSON keys, a changed report self-digest,
nonblank target rows, 1-to-0 physical transitions, no-op records, record-envelope
mismatches, row gaps or duplicates, rows outside the measured FF/IFF boundary,
quarantine/recovery overlap, changed RIR, stale prefix hashes, incomplete capacity
proofs, or changes outside the exact allowlist.

`kernel/170hx_xray_otp_spec.h` is a pure C state-machine specification. It has no
kernel or device dependency. `XRAY_OTP_ENABLE_REAL_HW` is a compile error. Its
fault-injection hook is compile-time-disabled by default and is also a compile
error under `__KERNEL__`; the host test explicitly enables it. The default
kernel `Makefile` does not include these files.

## State contract

For each row, the only successful path is:

```text
fresh complete-image preflight
  -> independent source-low watchdog armed
  -> qualified source asserted and measured
  -> exact allowlisted blank row/word armed
  -> one-attempt token durably consumed
  -> exactly one command issued
  -> bounded completion (never retry)
  -> unconditional source-low command and independent low proof
  -> exact-row readback plus complete-image/prefix verification
```

Any timeout, signal, journal failure, readback mismatch, full-image mismatch, or
source-low verification failure ends the session. An error after command issue
is an uncertain outcome and quarantines the card/session. Append-only recovery
records are reserve evidence, not an automatic rollback mechanism.

## Offline usage

From the research-workspace root:

```bash
python3 tools/170hx_otp_executor_spec.py compile \
  --candidate /absolute/path/to/170hx-xray-specimen-plans-exact-current-level3-native-v4.0.0.json \
  --bdf 0000:01:00.0 \
  --profile specimen-80gb-hbm \
  --output /secure/create-only/manifest.json

python3 tools/170hx_otp_executor_spec.py verify \
  --manifest /secure/create-only/manifest.json

python3 tools/170hx_otp_executor_spec.py simulate \
  --manifest /secure/create-only/manifest.json \
  --journal /secure/create-only/simulation.jsonl

python3 tools/170hx_otp_executor_spec.py simulate \
  --manifest /secure/create-only/manifest.json \
  --journal /secure/create-only/timeout-simulation.jsonl \
  --fault poll-timeout

python3 tools/170hx_otp_executor_spec.py verify-journal \
  --manifest /secure/create-only/manifest.json \
  --journal /secure/create-only/simulation.jsonl
```

`compile` can additionally take `--fresh-main-bin` (exactly 2048-byte u32le)
and `--fresh-rir-bin` (exactly 32-byte u32le). They must be supplied together
and match every byte of the sealed baseline. This still does not make the
manifest executable.

`review` requires an interactive TTY and exact entry of BDF, profile, manifest
digest, baseline digest, and the displayed acknowledgement. It creates only a
review receipt; the receipt contains `execution_enabled=false`.

All output paths are create-only. Existing manifests, receipts, and journals
are never overwritten.

The SHA-256 self-digest binds content but is not authentication. A future real
executor also requires a detached signature and configured trust root over the
manifest and qualified hardware profile.

## Host test

```bash
make -C kernel -f Makefile.otp-spec clean test
make -C kernel -f Makefile.otp-spec clean
```

This test covers the successful path, blank/allowlist refusal, retry refusal,
post-command quarantine, emergency source-low proof without clearing
quarantine, and the hardware-inert fault hook.

## Why a generic/kernel write ABI is still absent

A general implementation requires measured, board-specific evidence for the
program source voltage/polarity/routing, source-low observation, clock and pulse
timing, security/privilege transition, controller reset state, resense and
fundamental-reset lifecycle, and cold-boot recovery. Register names and reset
defaults alone do not qualify those electrical contracts. Until they are
independently established, inventing constants or exposing `write32` would turn
an offline proof into an irreversible brick path.

Any future kernel ABI must accept only a sealed manifest and exact device identity. It
must never accept an arbitrary offset/value pair. It must journal and fsync the
one-attempt token before the command boundary, use an independently supervised
source-low cleanup path, and refuse all execution after any uncertain outcome.
The narrow v2 user-space backend does not establish those missing electrical
facts and deliberately exposes no arbitrary address/value interface.
