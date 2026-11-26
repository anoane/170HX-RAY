# 170HX-XRAY usage guide

This guide covers the complete command surface without turning examples into
hardware authorization. Commands marked **offline** do not access a GPU.
Commands marked **observation** can access PCI BAR/controller state. Commands
marked **benchmark** execute workloads and transiently control the selected
cards' power limits. Commands marked **experimental** are templates only and
must not be run merely because they parse.

All output paths must be new. XRAY refuses to overwrite evidence.

The command names and option spellings in this guide were checked against the
local release CLI. That is parser/documentation validation only. A live example
is not claimed to have passed on the current checkout unless a cited report
contains its exact command, source hash and result.

## Conventions

Run from the repository root:

```sh
export XRAY=./170HX-XRAY.py
export XRAY_BDF=0000:01:00.0
export XRAY_OUT=/absolute/path/to/new-evidence-root
```

Replace the BDF and paths with values established on the target host. Do not
use a shell variable as an OTP target until its literal expanded value has been
reviewed. The variables above are convenience for read/offline examples only.
Row analysis additionally requires specimen-qualified FF/IFF bounds. Set these
only from the capture's retained boundary evidence; there is no universal
default:

```sh
export XRAY_FF_START=<reviewed-start-row>
export XRAY_FF_END=<reviewed-end-row>
```

The access levels are:

| Level | Name | Device behavior |
|---:|---|---|
| 1 | `passive` | sysfs/PCI and optional query-only driver APIs; no XRAY BAR access |
| 2 | `bar-read` | allowlisted direct BAR reads; no device-MMIO write by XRAY |
| 2G | `glitch-read` | Level-2 transient-state qualification; zero XRAY device writes |
| 3 | `semantic-read` | controller address/read/RIR-RWL transactions with checked restoration |
| 4 | `plan` | offline only; zero device access |
| 5 | `volatile-write` | named policy level; no volatile executor is implemented |
| 6 | `otp-write` | narrow experimental v2 blank-FF append backend only |

`nvidia-smi`/NVML calls are query-only at their public boundary, but proprietary
driver/firmware internal effects are opaque. Use `--no-driver-queries` when the
narrowest observation boundary matters.

## Help, schema, and offline release checks

These commands are offline:

```sh
python3 "$XRAY" --help
python3 "$XRAY" dump-formats --output "$XRAY_OUT/formats.json"
python3 "$XRAY" schema --output "$XRAY_OUT/schema.json"
python3 "$XRAY" self-test --output "$XRAY_OUT/self-test.json"
python3 -m unittest discover -s tests -p 'test_*.py' -v

make -C kernel abi-test
make -C kernel -f Makefile.therm abi-test
make -C kernel -f Makefile.otp-spec test
```

`helper-info` is not classified as offline when a controller-helper device is
present: it opens that device and requests its fixed INFO record, although it
does not perform a BAR capture or controller transaction:

```sh
python3 "$XRAY" helper-info \
  --controller-helper /dev/170hx_xray \
  --output "$XRAY_OUT/helper-info.json"
```

Audit Python/kernel read-registry parity:

```sh
python3 "$XRAY" registry-audit \
  --kernel-source kernel/170hx_xray.c \
  --output "$XRAY_OUT/registry-audit.json"
```

## Build the fixed-operation helpers

Building does not load a module or access a GPU:

```sh
make -C kernel
make -C kernel -f Makefile.therm
modinfo kernel/170hx_xray.ko
modinfo kernel/170hx_xray_therm.ko
sha256sum kernel/170hx_xray.ko kernel/170hx_xray_therm.ko
```

Loading a helper is a privileged live-host action. Establish the exact BDF,
driver ownership, workload state, module parameters and matching kernel first.
The normal Level-3 procedure loads the controller helper only after the target
is exclusively owned and cleanly unbound. Do not carry a helper BAR mapping
across proprietary-driver detach.

The thermal helper's pure capture opens `O_RDONLY`. Semantic snapshot or HBM
DATA capture opens `O_RDWR`, and the kernel independently requires
`FMODE_WRITE`. Module 1.5.1/ABI 5 always omits seven selector-dependent reads
(`selector_dependent_skipped_mask=0xf`). Its read contracts are 255 pure reads,
385 plus 1..1000 trigger polls for semantic snapshot, or 409 plus polls with
the separately acknowledged 24 HBM DATA reads.

## Level 1: passive metadata

All present stock CMP functions, with no driver queries:

```sh
python3 "$XRAY" capture \
  --access passive --backend metadata --all-targets \
  --no-driver-queries \
  --output "$XRAY_OUT/passive-no-driver.json"
```

One exact BDF with query-only NVML/`nvidia-smi` enabled:

```sh
python3 "$XRAY" capture \
  --access passive --backend metadata --bdf "$XRAY_BDF" \
  --output "$XRAY_OUT/passive-with-driver-queries.json"
```

When GPUs are busy, declare that fact. A controller request is then refused or
downgraded rather than racing the owner:

```sh
python3 "$XRAY" capture \
  --access semantic-read --owner-busy --bdf "$XRAY_BDF" \
  --output "$XRAY_OUT/busy-owner-refusal.json"
```

## Level 2: allowlisted BAR read

The helper must already be loaded and must attest the same full-domain BDF and
supported PCI tuple:

```sh
python3 "$XRAY" capture \
  --access bar-read --backend xray-helper \
  --controller-helper /dev/170hx_xray \
  --register-profile all-safe --bdf "$XRAY_BDF" \
  --no-driver-queries \
  --output "$XRAY_OUT/bar-all-safe.json"
```

The legacy `gpcprobe` backend can perform only Level 2 and lacks the integrated
full-domain/controller transaction contract:

```sh
python3 "$XRAY" capture \
  --access bar-read --backend gpcprobe --gpcprobe /dev/gpcprobe \
  --bdf "$XRAY_BDF" --no-driver-queries \
  --output "$XRAY_OUT/bar-gpcprobe-comparison.json"
```

## Level 2G: held glitch-state observation

This is **experimental observation**, not a glitch recipe. It neither produces
the fault nor authorizes programming. Use it only while an independently
controlled setup is already holding the exact target state:

```sh
python3 "$XRAY" capture \
  --access glitch-read --backend xray-helper \
  --controller-helper /dev/170hx_xray \
  --bdf "$XRAY_BDF" --no-driver-queries \
  --output "$XRAY_OUT/glitch-state-observation.json"
```

XRAY requires the complete state predicate; PCI ID `2080` or DSOV=0 alone is
not success. This branch cannot read MAIN/RIR.

The current external `pico-ga100vfi-console` classifier is read-only by default;
its persistent-in-session `EN_SW_OVERRIDE` write now requires explicit opt-in.
Its campaign also defaults to a 10-second inter-shot rest after one reported
VRM-fatigue event. Those are upstream safeguards, not XRAY glitch support or a
universal electrical timing qualification.

## Level 3: semantic MAIN and RIR read

Use only in an exclusive maintenance window with the proprietary driver cleanly
unbound unless the separately compiled high-risk bound-controller gate has been
deliberately enabled. One exact card at a time is the supported posture:

```sh
python3 "$XRAY" capture \
  --access semantic-read --backend xray-helper \
  --controller-helper /dev/170hx_xray \
  --bdf "$XRAY_BDF" --passes 2 \
  --register-profile all-safe --no-driver-queries \
  --output "$XRAY_OUT/level3-main-rir.json"
```

The output must show coherent repeated passes, complete MAIN and RIR images,
checked controller restoration, zero programming commands and zero OTP writes.
Any mismatch is a failed acquisition, not a row to repair by hand.

## Complete dump bundles and formats

Capture passive, BAR and semantic levels separately into one create-only bundle:

```sh
python3 "$XRAY" full-dump "$XRAY_OUT/full-dump" \
  --level passive --level bar-read --level semantic-read \
  --max-access semantic-read \
  --backend xray-helper --controller-helper /dev/170hx_xray \
  --all-targets --passes 2 --register-profile all-safe \
  --no-driver-queries --bind-workspace-candidate

python3 "$XRAY" verify-full-dump "$XRAY_OUT/full-dump" \
  --output "$XRAY_OUT/full-dump-verification.json"
```

That command always emits every admissible XRAY-native format. To request all
burner-shaped compatibility projections as well, add the following options to
the create command *before* running it:

```sh
  --include-unsafe-projections \
  --fuseless-start "$XRAY_FF_START" \
  --fuseless-end "$XRAY_FF_END" \
  --bounds-provenance 'reviewed specimen boundary evidence'
```

The acknowledgement expands serialization only. It grants no write authority,
and a projection can still be marked unavailable when its completeness gate is
not met.

`glitch-read` cannot share a bundle acquisition with prior ordinary/controller
levels because those transactions could contaminate the transient state. When
a real state is already held, capture it into a separate bundle:

```sh
python3 "$XRAY" full-dump "$XRAY_OUT/glitch-full-dump" \
  --level glitch-read --max-access glitch-read \
  --backend xray-helper --controller-helper /dev/170hx_xray \
  --bdf "$XRAY_BDF" --no-driver-queries
```

Package existing evidence without live discovery/access:

```sh
python3 "$XRAY" full-dump "$XRAY_OUT/offline-full-dump" \
  --no-live-capture \
  --source /absolute/path/to/capture.json \
  --input-format json \
  --rir-input /absolute/path/to/rir.raw \
  --rir-input-format raw-u32le \
  --row-view locked_controller \
  --provenance 'retained immutable capture'
```

Safe XRAY-native conversions are explicit:

```sh
python3 "$XRAY" convert-dump capture.json \
  --output-format xray-json --output converted.json
python3 "$XRAY" convert-dump capture.json \
  --output-format xray-jsonl --output rows.jsonl
python3 "$XRAY" convert-dump capture.json \
  --output-format indexed-text --output rows.txt
python3 "$XRAY" convert-dump capture.json \
  --output-format raw-u32le --output rows.bin
```

Public burner-shaped projections (`ffcalc-json`, `xpatrik-json`,
`ga100-fuse-re-json`, `dstoro-projection-json`, `dstoro-jsonl`) require
`--allow-unsafe-projection`. That switch authorizes serialization only; it does
not make a projection complete, current, or writable.

## Offline row analysis and planning

Decode a retained dump:

```sh
python3 "$XRAY" analyze-rows capture.json \
  --input-format json \
  --fuseless-start "$XRAY_FF_START" \
  --fuseless-end "$XRAY_FF_END" \
  --sparse-policy reject \
  --row-view locked_controller \
  --provenance 'reviewed retained capture' \
  --output "$XRAY_OUT/analysis.json"
```

Generate all evidence/public/theory comparison plans with zero device access:

```sh
for model in evidence-only public-model cross-checked-theory compare; do
  python3 "$XRAY" plan "$XRAY_OUT/analysis.json" \
    --access plan --planning-model "$model" \
    --output "$XRAY_OUT/plan-$model.json"
done
```

Validate one built-in plan through the inert Level-5 surface. `apply` has no
write backend, and `apply --execute` is a deliberate refusal:

```sh
python3 "$XRAY" apply "$XRAY_OUT/plan-evidence-only.json" \
  --access volatile-write \
  --output "$XRAY_OUT/volatile-apply-validation.json"
```

Generate a specimen-bound 18-profile candidate from one native exact-current
Level-3 capture:

```sh
python3 tools/170hx_candidate_plan.py \
  --root .. \
  --physical-capture "$XRAY_OUT/level3-main-rir.json" \
  --physical-card "$XRAY_BDF" \
  --same-capture-context --xray ./170HX-XRAY.py \
  --output "$XRAY_OUT/candidate.json"
```

The candidate computes memory from direct FBP defects plus paired
FBPA/FBIO/ROP-L2 closure (64, 80 or 96 GiB at the qualified CMP density) and SM
ceiling from direct GPC/TPC/PES closure (up to 128 SM). A defect ceiling is not
health proof. The two route models remain distinct:

1. `retain-cmp-20c2`: cold-validate common topology, recapture and rebase before
   isolated PCIe work, retain `20c2`, then perform any required external CMP
   software continuation;
2. `convert-a100-20b5`: cold-validate and rebase after common topology and again
   after isolated PCIe, fuse paired `20b5` identity as the final OTP segment,
   migrate only to a matching recoverable signed A100 VBIOS, then perform any
   required external software SM continuation.

These are staged route models, not two precompiled whole-card manifests. The
initial exact-current evidence may compile only the common-topology segment.

### Feature goals and plan boundaries

`plan` assesses every goal by default. To make the requested scope reviewable,
name each goal explicitly and repeat `--goal`:

```sh
python3 "$XRAY" plan "$XRAY_OUT/analysis.json" --access plan \
  --planning-model compare \
  --goal 80gb_hbm --goal more_sms --goal pcie_gen4_x16 \
  --goal nvlink --goal ecc --goal mig --goal nvdec_nvjpg \
  --goal a100_identity_vbios --goal oc_frequencies \
  --goal persistent_dsov0 --goal fuse_write_eligibility \
  --output "$XRAY_OUT/all-goals-compare.json"
```

The output does not give every goal the same support level:

| Goal | Current XRAY disposition |
|---|---|
| HBM/SM topology | Specimen-derived candidate profiles preserve direct defects and compute 64/80/96-GiB and up-to-128-SM ceilings. A ceiling is not a health result. |
| PCIe Gen4 and global NVLink | Separate candidate profiles exist. NVLink's option/CP/defect mask is 3-bit, its per-link plane is 12-bit, and its control/status plane is 16-bit; none substitutes for board routing, power, firmware or cold training. |
| MIG/SYS_PIPE | Experimental, defect-gated candidate. Chain-1 bits 372..379 have a strong one-card result; every specimen must preserve `OPT_SYS_PIPE_DEFECTIVE`. |
| NVDEC/NVJPG/OFA | Experimental candidate. NVDEC bits 139..143 are modeled; bits 146 and 371 are only the inseparable unordered NVJPG/OFA pair. |
| NVENC | `not-yet-supported`: production A100 is documented without NVENC. The option/control/defect/status words and PTOP count remain read-only inventory and do not supply a physical programming coordinate or prove a usable engine is populated. |
| ECC | Inventory-only for permanent planning. Row 6/7 bits 28/29 have 99% coordinate confidence, but the complete domain, row-remap, firmware, InfoROM and functional-error path is unproved. |
| SS0/SS1/FP64 | Inventory and comparison only. `0x82381c` is the broad SS0-style IMLA0-3/FMLA/FFMA/DP override; the reported IFF pair is `0x082381fd`/`0x88888888` at recipe-specific rows 484/485. `0x823820` is the SS1-style IMLA4 override, not dedicated FP64; its reported pair is `0x0823823d`/`0x00000008` at recipe-specific rows 486/487. XRAY exposes both as non-executable 85%-confidence candidates only. FP64 has `OPT_SM_SPEED_SELECT_DP` at `0x820224`/row 6/7 bit 26; adjacent bit 27 is a 65%-confidence undo/control hypothesis. None is an admitted FF-only manifest operation. |
| Persistent DSOV=0 and OC | Research/inventory only; no cold-persistent DSOV mechanism or portable, reliability-qualified frequency-fuse recipe is known. |

Scalar speed-select names do not prove Tensor throughput. Use the separate
ArchBench scalar, integer, packed, Tensor and GEMM families, and retain
unsupported versus measurement-only results instead of inferring them from an
override register.

## Compile and verify guarded manifests

The compiler admits seven profile IDs. Compilation and verification are offline:

```sh
python3 tools/170hx_vfi_append_manifest.py compile \
  --candidate "$XRAY_OUT/candidate.json" \
  --bdf "$XRAY_BDF" \
  --profile xray-common-permanent-topology \
  --cold-main "$XRAY_OUT/cold-main.bin" \
  --cold-rir "$XRAY_OUT/cold-rir.bin" \
  --output "$XRAY_OUT/manifest.json"

python3 tools/170hx_vfi_append_manifest.py verify \
  --manifest "$XRAY_OUT/manifest.json"
```

The seven admitted profiles are `specimen-80gb-hbm` (dynamic 64/80/96 target),
`specimen-more-sm`, `xray-common-permanent-topology`,
`specimen-pcie-gen4-isolated`, `specimen-a100-20b5-identity`,
`specimen-sys-pipe-to-defect-experimental`, and
`specimen-media-mig-to-defect-experimental`. Four cards produce 28 manifests.
The candidate planner exports 18 profiles in total, including six
zero-operation review-only profiles. `specimen-nvlink-global` is candidate-only
and is rejected by both compiler and runtime: Chain-1 bits 368--370 are global
IOCTRL group disables, not the twelve-link physical map. The historical 4.2
validation retained 32 manifests; that count is not the current admission
contract. See [`UNLOCK_PLAN.md`](UNLOCK_PLAN.md).
NVJPG/OFA bits 146/371 remain inseparable; NVENC has no supported physical
coordinate. The 28 manifests in a four-card current-baseline matrix are seven
siblings per card, not seven ordered operations per card. Applying one sibling
invalidates the other six. Route execution begins, if independently reviewed
and authorized elsewhere, with only the common-topology segment and then
requires a cold capture and exact-current rebase before the next segment.

## Temperature diagnostics

Pure fixed capture:

```sh
python3 diagnostics/run_diagnostics.py temperature \
  --bdf "$XRAY_BDF" --temperature-access bar-read \
  --temperature-no-driver-queries \
  --output "$XRAY_OUT/temperature-pure.json"
```

An ordinary unbound semantic snapshot requires selector-write acknowledgement
and a quiescent target:

```sh
python3 diagnostics/run_diagnostics.py temperature \
  --bdf "$XRAY_BDF" --temperature-access semantic-read \
  --temperature-acknowledge-selector-write \
  --output "$XRAY_OUT/temperature-semantic.json"
```

The optional `--temperature-acknowledge-hbm-data-read-to-advance` adds 24
stateful DATA reads. Do not use it merely to fill null temperatures. A helper
intentionally loaded with its bound-snapshot gate additionally requires
`--temperature-acknowledge-bound-snapshot`; that flag is not a substitute for
exclusive ownership.

## Benchmarks and mandatory power evidence

Every executed benchmark family defaults to a transient 150 W limit. XRAY
queries the original limit, applies and re-reads 150 W, samples draw during the
benchmark, verifies the limit afterward, restores the exact original and
verifies restoration. A passing family includes draw minimum, maximum,
arithmetic mean, sample/failure counts, interval/span, exact BDF/UUID and
executable hash. Missing power evidence fails the component.

Typed storage traffic (not arithmetic or Tensor throughput):

```sh
python3 diagnostics/run_diagnostics.py bandwidth \
  --bdf "$XRAY_BDF" --power-limit-watts 150 \
  --power-sample-interval-ms 100 \
  --bandwidth-bytes 512M --bandwidth-iterations 200 \
  --output "$XRAY_OUT/bandwidth.json"
```

This covers FP16/BF16/FP32/FP64, packed INT4/UINT4 and signed/unsigned
8/16/32/64-bit storage types.

P2P selects the two cards with the highest negotiated PCIe generation/width and
tests direct and host-staged paths across power-of-two sizes:

```sh
python3 diagnostics/run_diagnostics.py p2p \
  --power-limit-watts 150 --power-sample-interval-ms 100 \
  --p2p-min-bytes 4K --p2p-max-bytes 1G --p2p-iterations 20 \
  --output "$XRAY_OUT/p2p.json"
```

GDS requires an explicitly approved writable NVMe scratch. XRAY chooses the
best GPU/NVMe pair, creates only a uniquely owned child, verifies the transfer,
and removes that child:

```sh
python3 diagnostics/run_diagnostics.py gds \
  --gds-scratch /absolute/path/to/nvme-scratch \
  --gds-size 4K --gds-size 64K --gds-size 1M \
  --gds-size 16M --gds-size 256M --gds-size 1G \
  --gds-io-size 16M --power-limit-watts 150 \
  --output "$XRAY_OUT/gds.json"
```

Media requires an exact-hash owner-supplied suite:

```sh
python3 diagnostics/run_diagnostics.py media \
  --media-suite /absolute/path/to/nvmedia-suite.tar.gz \
  --media-suite-sha256 <exact-sha256> \
  --power-limit-watts 150 \
  --output "$XRAY_OUT/media.json"
```

It records NVDEC capability/decode, NVJPEG and ffmpeg NVENC outcomes verbatim.
The query-only feature census separately retains exact-BDF/UUID `nvidia-smi -q`
text/XML, ECC current/pending mode and error counters, MIG current/pending mode
and profile listings, NVLink status, and encoder/decoder session counters. OFA
is limited to a hashed host-library maximum-API-version query: no CUDA context,
OFA session, selected-GPU binding, or functional OFA test. NVJPEG has no
`nvidia-smi` capability proof and requires the external suite. Runtime MIG
reconfiguration and destructive ECC injection are not performed.

Run every available non-persistent diagnostic family in one invocation. Omit a
prerequisite rather than substituting a different device or unverified binary;
the result will record the family as unavailable or skipped:

```sh
python3 diagnostics/run_diagnostics.py full \
  --power-limit-watts 150 --power-sample-interval-ms 100 \
  --bandwidth-bytes 512M --bandwidth-iterations 200 \
  --p2p-min-bytes 4K --p2p-max-bytes 1G --p2p-iterations 20 \
  --gds-scratch /absolute/path/to/nvme-scratch \
  --gds-size 4K --gds-size 64K --gds-size 1M \
  --gds-size 16M --gds-size 256M --gds-size 1G \
  --gds-io-size 16M \
  --media-suite /absolute/path/to/nvmedia-suite.tar.gz \
  --media-suite-sha256 <exact-sha256> \
  --temperature-probe-device /dev/170hx_xray_therm \
  --temperature-probe-backend xray-therm \
  --temperature-access semantic-read \
  --temperature-acknowledge-selector-write \
  --output "$XRAY_OUT/diagnostics-full.json"
```

Every benchmark component, including each typed-storage card, both selected
P2P cards, the selected GDS card and every media-selected card, must retain the
requested/enforced power limit plus observed minimum, maximum and arithmetic
mean draw. Inventory, MIG/ECC queries and OFA's host-library version query are
not benchmarks and must not be assigned fabricated watt statistics.

ArchBench option A expands 192 base requests plus 43 software-counterpart cases
into 235 selected cases across 20 families: packed, tensor, GEMM, quantized
GEMM, arbitrary-width integer, memory, collective, workload, transfer, sparse,
FFT, conversion, integer, scalar, bit, SFU, BLAS, quantization, atomic and
launch. A completed run reports selected, recorded, missing, validated,
unsupported and measurement-only cases separately. It is a broad initial
correctness/capability gate, not exhaustive CUDA operation/dtype/shape
coverage, sustained-peak proof, a full-memory sweep, long-duration stability
evidence, sanitizer coverage or profiler evidence:

Initialize and build the pinned ArchBench SM80 worker once on the Linux/CUDA host:

```sh
git -C "$XRAY_ROOT" submodule update --init --recursive
cd "$XRAY_ROOT/third_party/archbench_cuda13_5_v0_2_0"
./bench selftest
./bench build --arch 80 --nvcc /usr/local/cuda-13.5/bin/nvcc --jobs 4
cd "$XRAY_ROOT"
```

The report command defaults to that commit-pinned submodule and verifies its
commit, tree, tracked-file count and clean tracked state against
`docs/ARCHBENCH_SUBMODULE_LOCK.json` before execution. `--archbench-root`
selects a different reviewed tree and derives `build/bin/archbench_sm80` below
it; pass `--archbench-binary` only when that reviewed worker is elsewhere.

```sh
python3 tools/170hx_full_report.py "$XRAY_OUT/report-archbench" \
  --report-mode report+bench \
  --bdf "$XRAY_BDF" --no-live-capture \
  --power-limit-watts 150
```

## Full reports

`full-report` is the fast inventory/read/dump/plan path. It performs no
workload benchmark and refuses benchmark-only options before host access;
`diagnostics-benchmarks`, `archbench-acceptance-235`, and benchmark power
control remain explicit `not-requested` components. Use the separate
`full-report-bench` command (alias `full-report+bench`) for diagnostics,
ArchBench, transient 150-W-by-default power control, and sampled power
envelopes. The two commands run the same read, export, and non-authorizing
planning pipeline; the `-bench` form only adds the benchmark campaign.

### Full-report planning and exporter contract

For every selected card, both report modes request three independently checked
planning views. A higher numbered view is not permission to execute the lower
numbered view and does not turn independent profile results into a sequence:

1. The full-dump goal/checklist layer emits `evidence-only`, `public-model`,
   `cross-checked-theory`, and `compare`. Each model has JSON, JSONL, text, and
   canonical-JSON binary transports, plus an inert apply dry-run and OTP
   readiness record.
2. The exact-current option matrix emits all 18 profiles per card. The whole
   candidate is replay-checked as JSON, JSONL, text, and canonical-JSON binary;
   every profile's proposed complete 512-row MAIN image is also independently
   replay-checked as JSON, JSONL, indexed text, and raw u32le. Each profile is
   an independent answer from the same baseline, not a step to add to another
   profile.
3. The current-baseline sibling-manifest layer compiles and verifies the seven
   admitted v2 profiles per card. All seven start from the same baseline and
   are mutually exclusive alternatives. It does not review, execute, pulse, or
   authorize them.

### Route-aware output and sequencing

Both commands generate both final-state route models automatically; there is no
route-selection CLI flag and generating the route documents does not select a
route for execution. The top-level layout is:

```text
<report>/
|-- read-dump-plan-bundle/                 # compatibility evidence/export view
|-- candidate-plan-exports/                # compatibility candidate view
|   `-- candidate-plan.{json,jsonl,txt,canonical-json.bin}
|-- candidate-proposed-images/<BDF>/<PROFILE>/  # compatibility row view
|   `-- proposed-main.{json,jsonl,indexed.txt,raw-u32le.bin}
|-- shared/
|   |-- index.json
|   |-- evidence/index.json
|   |-- layer-1-checklists/index.json
|   `-- baselines/<BDF>/
|       |-- baseline-binding.json
|       |-- main.u32le.bin
|       `-- rir.u32le.bin
|-- option-matrix/
|   |-- option-matrix.json
|   |-- SHA256SUMS
|   |-- candidate-plan.{json,jsonl,txt,canonical-json.bin}
|   |-- cards/<BDF>/profiles/<PROFILE>/
|   |   |-- profile-binding.json           # all 18 options
|   |   `-- proposed-main.{json,jsonl,indexed.txt,raw-u32le.bin}
|   `-- current-baseline-sibling-manifests/<BDF>/
|       |-- README-NOT-CUMULATIVE.md
|       `-- <PROFILE>.json                 # exactly 7 sibling manifests
|-- routes/
|   |-- route-index.{json,jsonl,txt,canonical-json.bin}
|   |-- retain-cmp-20c2/
|   |   |-- route.{json,jsonl,txt,canonical-json.bin}
|   |   `-- cards/<BDF>/
|   |       |-- route-status.json
|   |       |-- epochs/000-baseline/evidence-binding.json
|   |       `-- segments/{010-common-topology,020-isolated-pcie,
|   |                     030-retain-identity,040-software-continuation}/
|   |-- convert-a100-20b5/
|   |   |-- route.{json,jsonl,txt,canonical-json.bin}
|   |   `-- cards/<BDF>/
|   |       |-- route-status.json
|   |       |-- epochs/000-baseline/evidence-binding.json
|   |       `-- segments/{010-common-topology,020-isolated-pcie,
|   |                     030-identity-20b5-last,040-signed-a100-vbios,
|   |                     050-software-sm-continuation}/
|   `-- comparison/route-comparison.json
`-- overlays/
    |-- index.json
    |-- media-mig-experimental/overlay.json
    |-- sys-pipe-experimental/overlay.json
    `-- nvlink-rejected/overlay.json
```

This derived tree exists only when candidate generation and the complete
verified sibling-manifest matrix succeed. Otherwise
`route-aware-output-layout` is `skipped` or `failed`, and the report does not
manufacture partial routes.

`<BDF>` directory names use the safe full-BDF form, such as `0000-01-00-0`;
the documents retain the canonical `0000:01:00.0` spelling. Every segment
directory contains `segment-status.json`; `010-common-topology` additionally
contains `next-required-capture.json` and a bound reference to the option
matrix's authoritative manifest.

`read-dump-plan-bundle/` contains observed evidence and the layer-1 checklist
products. `shared/` adds immutable MAIN/RIR copies and small evidence/checklist
indexes consumed by both routes. The legacy `candidate-plan-exports/` and
`candidate-proposed-images/` paths remain for compatibility. `option-matrix/`
contains hash-identical physical copies of all four candidate transports, all
18 independent profile bindings and all four proposed-row transports per
profile. HBM-only and SM-only are alternatives/subsets of common topology;
SYS_PIPE-only and media/MIG overlap. Do not concatenate their row changes. The
seven JSON files in each `current-baseline-sibling-manifests/<BDF>/` directory
are also siblings, not an ordered manifest set: after any one is applied, all
other siblings are stale.

`routes/route-index.*` records both generated routes and their shared
option/overlay bindings in JSON, JSONL, human text, and canonical-JSON binary.
Each route has the same four representations plus one
`route-status.json` per card. A card status identifies its baseline epoch, next
segment and expected terminal identity, then references every segment status.
Those records bind the sole current manifest or give the withheld/external
reason, and always record `execution_authorized: false`.

Segment status values are deliberately literal:

| `state` | Meaning |
|---|---|
| `compiled-current-baseline-review-only` | The exact-current common-topology sibling is referenced as the sole next manifest; it remains unauthorized. |
| `historical-review-only-requires-exact-current-recapture` | Historical evidence was explicitly accepted for review, but no segment is current. |
| `withheld-requires-cold-recapture-and-rebase` | A later OTP segment has no route manifest yet; compile only after the preceding cold capture and rebase. |
| `withheld-post-cold-device-20c2-checkpoint` | The CMP route must observe retained `20c2`; this is not an identity OTP operation. |
| `withheld-external-action` | VBIOS or software work is outside full-report and has no fuse manifest. |

These are route-segment states. The report component itself separately uses
`completed`, `failed`, `skipped`, `not-yet-supported`, or `not-requested`.

Only `010-common-topology` is eligible to reference a compiled manifest from
an exact-current initial `000-baseline`; a historical review has no current
segment. Its authoritative manifest is stored once in the option matrix rather
than copied into both routes. Every subsequent hardware segment is withheld
until a new cold capture and exact-current rebase. Version 0.7 packages only
the initial `000-baseline` route state; it does **not** promote a changed-card
capture to epoch 001/002 or compile the next route segment automatically:

```text
implemented now: 000-baseline -> review 010-common-topology only
forecast only:   001-post-topology -> isolated PCIe after future rebase support
forecast only:   002-post-pcie -> A100 identity after future rebase support
```

Never reuse the initial report's sibling manifests after the card changes, and
never construct a later epoch by editing names or copying predicted row images.
Retain the report unchanged and obtain a newly reviewed plan with tooling that
explicitly supports the observed post-write epoch. The route document's
`final_device_id` is a forecast, not an executable final image; only post-cold
observation establishes terminal state.

The `retain-cmp-20c2` route stops OTP planning after separately rebased common
topology and PCIe stages. `030-retain-identity` is an expectation-only record,
not a fuse operation; `040-software-continuation` describes an external EFI or
driver policy and its validation obligations.

The `convert-a100-20b5` route places paired identity at
`030-identity-20b5-last`, after the independently rebased topology and PCIe
epochs. It is terminal: the qualified append backend starts from a live `20c2`,
so the route represents no OTP work after conversion. The signed matching A100
VBIOS migration and any required software SM continuation are external-action
records, never OTP manifests.

Media/MIG and SYS_PIPE are experimental overlays; they are not silently added
to either route. NVLink remains a rejected/research-only overlay because the
known global mask does not provide a complete physical per-link enable plan.
No route status presents these overlays as completed terminal-route segments.

The ordinary full-dump request asks for every covered observation transport
(JSON, JSONL, text, and binary register records), all nine MAIN row transports
(`xray-json`, `xray-jsonl`, `indexed-text`, `raw-u32le`, `ffcalc-json`,
`xpatrik-json`, `ga100-fuse-re-json`, `dstoro-projection-json`, and
`dstoro-jsonl`), all four exact RIR transports, and all four register transports.
The RIR set is raw u32le, indexed text, JSON, and JSONL; the register set is
JSON, JSONL, text, and address/value u32le. The glitch-read branch requests and
verifies the same applicable exporter set when `--include-glitch-read` is
explicitly selected.

These are fail-closed requests, not promises to manufacture missing evidence.
A layer is `completed` only after its artifacts are independently replayed or
verified. If no eligible complete 512-row MAIN or other prerequisite exists,
the report records that layer as `skipped`; if eligible MAIN evidence exists
but a required product is absent or inconsistent, it records `failed`. A
verified live semantic capture in the same report invocation may supply the
exact-current candidate input. `--no-live-capture` inputs, including a recently
created imported bundle or `--physical-capture`, are always labeled historical
review-only because file age cannot prove no intervening device-state change.
The exporter uses the
empirical `S=222`, `E=511` retained-population boundary only for compatible
serialization. That interval is not an official GA100 physical descriptor,
does not identify OTP physics, and grants no write authority.

Report with every XRAY BAR/controller capture level explicitly skipped. Host,
PCI and query-only driver inventory still run and retain their opaque-effects
qualification:

```sh
python3 "$XRAY" full-report "$XRAY_OUT/report-inventory" -- \
  --no-live-capture
```

Import a previously verified full-dump without a new XRAY BAR/controller
capture. Host/PCI and query-only driver inventory still run:

```sh
python3 "$XRAY" full-report "$XRAY_OUT/report-imported" -- \
  --no-live-capture --read-bundle /absolute/path/to/verified/full-dump
```

Busy-owner report: passive/BAR requests may proceed, semantic reads are listed
as skipped/refused:

```sh
python3 "$XRAY" full-report "$XRAY_OUT/report-busy" -- \
  --owner-busy --read-level passive --read-level bar-read \
  --read-level semantic-read
```

Maintenance-window report with exact-current plans:

```sh
python3 "$XRAY" full-report "$XRAY_OUT/report-maintenance" -- \
  --read-level passive --read-level bar-read --read-level semantic-read \
  --physical-capture /absolute/path/to/native-level3.json
```

The command emits both route models. Inspect the route index, every card status,
the non-cumulative warning, and the available first-segment manifest reference:

```sh
XRAY_REPORT="$XRAY_OUT/report-maintenance"
python3 -m json.tool "$XRAY_REPORT/routes/route-index.json"
find "$XRAY_REPORT/routes" -name route-status.json -print \
  -exec python3 -m json.tool '{}' \;
find "$XRAY_REPORT/routes" -path '*/segments/010-common-topology/*' -type f \
  -print
find "$XRAY_REPORT/option-matrix/current-baseline-sibling-manifests" \
  -name '*.json' -print
python3 -m json.tool "$XRAY_REPORT/option-matrix/option-matrix.json"
```

To inspect all four equivalent route representations without confusing them for
four different plans:

```sh
ls -l "$XRAY_REPORT/routes"/route-index.*
ls -l "$XRAY_REPORT/routes/retain-cmp-20c2"/route.*
ls -l "$XRAY_REPORT/routes/convert-a100-20b5"/route.*
```

After an independently authorized segment and required cold validation, retain
the old report unchanged. Capture the changed card, but do not feed it to v0.7
and assume that a newly generated `000-baseline` tree represents epoch 001.
Automatic post-write epoch advancement is not implemented. Do not copy an old
manifest, edit `current_epoch`, or rename a withheld marker; use separately
reviewed tooling that explicitly supports the observed post-write epoch.

Repeat `--physical-capture` and the corresponding card selector as required by
the exact-current multi-card campaign. A report with missing or historical
inputs preserves the candidate/manifest layers as explicit skips or failures;
it does not silently substitute an older card image.

Full diagnostics plus ArchBench and 150 W campaign control:

```sh
python3 "$XRAY" full-report-bench "$XRAY_OUT/report-complete" -- \
  --sudo-read-inventory \
  --read-level passive --read-level bar-read --read-level semantic-read \
  --physical-capture /absolute/path/to/native-level3.json \
  --power-limit-watts 150 \
  --power-sample-interval-ms 100 \
  --bandwidth-bytes 512M --bandwidth-iterations 200 \
  --p2p-min-bytes 4K --p2p-max-bytes 1G --p2p-iterations 20 \
  --gds-scratch /absolute/path/to/nvme-scratch \
  --gds-size 4K --gds-size 64K --gds-size 1M \
  --gds-size 16M --gds-size 256M --gds-size 1G \
  --gds-io-size 16M \
  --media-suite /absolute/path/to/nvmedia-suite.tar.gz \
  --media-suite-sha256 <exact-sha256>
```

`--sudo-read-inventory` is limited to the orchestrator's fixed non-interactive
read-only DMI query. It enables no GPU setter or arbitrary elevated command;
omit it when the host sudo policy has not been reviewed.

The output contains `170HX-XRAY_REPORT_<UTC-timestamp>.md`,
`report-index.json`, raw command evidence, component artifacts and
`SHA256SUMS`. It gives every level and component one of five first-class states:
`completed`, `failed`, `skipped`, `not-yet-supported`, or `not-requested`.
`Skipped` means requested work was blocked by a prerequisite or safety gate;
`not-requested` is never folded into that state or presented as success.

## Remote `ai-ws` deployment template

This is a deployment template, not a claim that the current checkout has passed
the remote campaign. Preserve the exact source hash and never overwrite an old
remote evidence directory:

```sh
rsync -a --delete --exclude '__pycache__' ./ ai-ws:/tmp/170hx-xray-release/

ssh ai-ws 'cd /tmp/170hx-xray-release && \
  python3 170HX-XRAY.py self-test --output /tmp/xray-self-test.json && \
  python3 -m unittest discover -s tests -p "test_*.py" -v'
```

Before a live report, run the command once with `--no-live-capture`, inspect all
resolved paths and capabilities, then schedule the maintenance window. The
release evidence must retain the exact successful SSH command, stdout/stderr,
source/module hashes, kernel/driver versions, selected BDFs and skip reasons.

## Experimental Level 6 review and preflight

The following does not program OTP. It creates a human review receipt, verifies
it, and runs the manifest-bound live preflight only:

```sh
python3 tools/170hx_vfi_append_manifest.py review \
  --manifest "$XRAY_OUT/manifest.json" \
  --output "$XRAY_OUT/review-receipt.json"

python3 tools/170hx_vfi_append_manifest.py verify-receipt \
  --manifest "$XRAY_OUT/manifest.json" \
  --receipt "$XRAY_OUT/review-receipt.json"

python3 "$XRAY" full-write "$XRAY_OUT/manifest.json" \
  --access otp-write --backend ga100-vfi-ff-append-v1 \
  --bdf "$XRAY_BDF" --profile xray-common-permanent-topology \
  --receipt "$XRAY_OUT/review-receipt.json" \
  --journal "$XRAY_OUT/0000-01-00-0.preflight.journal" \
  --live-preflight --output "$XRAY_OUT/preflight.json"
```

Actual `--execute` is irreversible and intentionally not presented as a tested
release command. It additionally requires an explicit write strategy, fresh
native DSOV=0 held-state capture, controlling TTY acknowledgements, the canonical
sysfs root, electrical qualification, a recovery path and a separately reviewed
cold-validation plan. A parser pass, favorable glitch classifier, prompt or
protected initial state is not authorization.

After a separately authorized physical power cycle, cold verification is
offline with respect to programming but consumes new native evidence:

```sh
python3 tools/170hx_vfi_append_manifest.py cold-verify \
  --manifest "$XRAY_OUT/manifest.json" \
  --journal "$XRAY_OUT/0000-01-00-0.execution.journal" \
  --post-cold-capture /absolute/path/to/post-cold-native-level3.json \
  --power-attestation /absolute/path/to/power-cycle-attestation.json \
  --output "$XRAY_OUT/0000-01-00-0.execution.journal.cold.json"
```

## Interpreting success

- `status: completed` means the named software component completed its defined
  scope; it is not a feature-unlock claim.
- `complete-with-explicit-skips` means every omission is recorded and no
  component failed.
- A candidate or manifest is non-authoritative even when verification passes.
- A physical defect mask is a ceiling, never proof that every unmarked unit is
  healthy.
- An A100 device ID/VBIOS does not by itself unlock SMs, HBM, NVLink, media,
  MIG or ECC.
- Preserve source bytes and hashes. Put new interpretations in derived files;
  never edit a raw capture to make tools agree.
