#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Experimental, fail-closed GA100 VFI blank-FF-row append backend.

This module is intentionally narrow.  It can consume one hash-sealed XRAY OTP
manifest, bind it to one exact PCI function, compare controller rows 0..487
with the sealed image, verify a held VFI window, and append only the manifest's
ordered blank-row FF records.  It cannot write occupied rows, FPF, IFF, RIR,
keys, arbitrary BAR0 addresses, or caller-provided address/value pairs.

Importing the module has no hardware side effects.  Real programming is
reachable only through ``run_live(..., execute=True)`` after the caller has
performed its own interactive confirmations.
"""

from __future__ import print_function

import copy
import datetime
import fcntl
import hashlib
import importlib.util
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import time
import uuid


BACKEND_ID = "ga100-vfi-ff-append-v1"
BACKEND_VERSION = "1.1.0"
MANIFEST_SCHEMA = "170hx-xray-otp-sealed-manifest-v3"
RECEIPT_SCHEMA = "170hx-xray-vfi-append-review-receipt-v1"
JOURNAL_SCHEMA = "170hx-xray-vfi-append-journal-v1"
COLD_ATTESTATION_SCHEMA = "170hx-xray-power-cycle-attestation-v1"
COLD_RECEIPT_SCHEMA = "170hx-xray-vfi-append-cold-receipt-v2"
MAIN_ROWS = 512
COMPARE_FIRST = 0
COMPARE_LAST = 487
FF_FIRST = 222
FF_LAST = 487
MAP_SIZE = 0x01000000
# Observation supports all three GA100 identities.  Irreversible execution is
# deliberately narrower: only the exact-current 20c2 path has a live specimen.
OBSERVATION_DEVICE_IDS = frozenset((0x20C2, 0x20B5, 0x2080))
EXECUTION_DEVICE_ID = 0x20C2
CANONICAL_STATE_DIR = "/var/lib/170hx-xray/otp"
CANONICAL_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]$")
DRIVER_OVERRIDE_SENTINEL = "170hx-xray-otp-lock"
COMMAND_COMPLETION_TIMEOUT_SECONDS = 10.0
COMMAND_POLL_INTERVAL_SECONDS = 0.001
HELD_CAPTURE_MAX_AGE_SECONDS = 300.0

# Native two-pass artifacts exist under the retired RAY ABI 5 and the current
# XRAY ABI 6.  The dump payload size/counter contract is identical for the
# fields consumed by this backend; every remaining envelope invariant is
# checked independently below.  No other ABI is accepted.
LEGACY_NATIVE_HELPER_ABI = 5
CURRENT_NATIVE_HELPER_ABI = 6
SUPPORTED_NATIVE_HELPER_ABIS = frozenset((
    LEGACY_NATIVE_HELPER_ABI, CURRENT_NATIVE_HELPER_ABI,
))

STRATEGY_PUBLIC_DIRECT = "public-direct-compat"
STRATEGY_EVIDENCE_GUARDED = "evidence-led-guarded"
STRATEGY_INERT_THEN_ARM_TRACE = "inert-then-arm-experimental-trace-only"
WRITE_STRATEGIES = (STRATEGY_PUBLIC_DIRECT, STRATEGY_EVIDENCE_GUARDED)
TRANSACTION_TRACE_STRATEGIES = WRITE_STRATEGIES + (
    STRATEGY_INERT_THEN_ARM_TRACE,
)

FUSECTRL = 0x00820000
FUSEADDR = 0x00820004
FUSERDATA = 0x00820008
FUSEWDATA = 0x0082000C
FUSETIME_RD1 = 0x00820010
FUSETIME_RD2 = 0x00820014
FUSETIME_PGM1 = 0x00820018
FUSETIME_PGM2 = 0x0082001C
IFF_CTRL = 0x00820028
EN_SW_OVERRIDE = 0x00820040
FUSETIME_RD3 = 0x00820054
FUSEDEBUG = 0x00820060
CRC_STATUS = 0x00820070
EFP_STATUS = 0x00820078
PROGRAM_DISABLE_STATUS = 0x0082007C
BYPASS_FUSES_STATUS = 0x00820080
DSOV_STATUS = 0x00820084
SWIFF = 0x00820088
FUSEWDATA_PLM = 0x008200F4
FUSECTRL_PLM = 0x008200F8
OPT_PRIV_MASK = 0x008200FC
SENTINEL_ALIAS = 0x00820410
WDATA_WR_SECURE = 0x0082048C
SBFE_SHADOW = 0x00821108
RESENSE_DISABLE = 0x00821058
FPF_SENTINEL = 0x00824314
GFW_BOOT = 0x00118234
LOADER_QUIESCENT = 0x00120060

CMD_READ = 1
CMD_PROGRAM = 2
CMD_SENSE = 3
IDLE_VALUE = 0xE0040000
VALID_FUSECTRL_STATES = frozenset(range(0x00, 0x10))


def validate_write_strategy(value):
    _require(value in WRITE_STRATEGIES,
             "write strategy must be explicitly selected from: %s" %
             ", ".join(WRITE_STRATEGIES))
    return value


def validate_trace_strategy(value):
    _require(value in TRANSACTION_TRACE_STRATEGIES,
             "transaction trace strategy must be selected from: %s" %
             ", ".join(TRANSACTION_TRACE_STRATEGIES))
    return value


def transaction_trace(validated, write_strategy):
    """Return a deterministic, non-executing MMIO transaction specification."""
    strategy = validate_trace_strategy(write_strategy)
    operations = [
        {
            "index": item["index"], "row": item["row"],
            "word": item["word_hex"], "stage": item["stage"],
        }
        for item in validated["operations"]
    ]
    common = {
        "schema": "170hx-xray-vfi-append-transaction-trace-v1",
        "strategy": strategy,
        "manifest_content_sha256": validated["content_sha256"],
        "bdf": validated["bdf"],
        "profile_id": validated["profile_id"],
        "execution_performed": False,
        "execution_eligible": strategy in WRITE_STRATEGIES,
        "controller_command_field": "FUSECTRL.CMD[1:0]",
        "exact_current_20c2_idle_word": "0xe0040000",
        "ownership_model": {
            "transport": "userspace resource0 mmap",
            "kernel_owned_irreversible_transaction": False,
            "bind_remove_toctou_closed": False,
            "interlocks": [
                "exact unbound PCI tuple and resource inode",
                "private exact-BDF process lock",
                "driver_override sentinel",
                "identity/resource re-attestation immediately before CMD_PROGRAM",
            ],
            "qualification": (
                "experimental only; a reviewed manifest and confirmations do "
                "not close the userspace-to-kernel ownership race"
            ),
        },
        "timing_registers": {
            "snapshot_only": [
                "0x%08x" % value for value in (
                    FUSETIME_RD1, FUSETIME_RD2, FUSETIME_PGM1,
                    FUSETIME_PGM2, FUSETIME_RD3,
                )
            ],
            "writes_permitted": False,
            "electrical_qualification_claimed": False,
        },
        "row_operations": operations,
        "program_loop": [
            "re-attest exact ownership and all gates",
            "CMD_READ sealed target and require zero",
            "fsync one-attempt journal record",
            "FUSEADDR <- sealed row; verify latch",
            "FUSEWDATA <- sealed complete FF word",
            "re-attest ownership immediately before command",
            "FUSECTRL <- CMD_PROGRAM(2) exactly once",
            "bounded poll to exact-current idle; never retry",
            "CMD_READ exact row and verify prefix",
        ],
        "terminal_policy": (
            "no post-program sense, reset, volatile cleanup, or driver bind; "
            "park for complete slot/AUX power removal and independent cold verification"
        ),
        "forbidden": [
            "GPIO19 write", "timing-register write", "automatic retry/resume",
            "software-IFF load", "post-program CMD_SENSE", "hot reset",
            "MMIO cleanup after poison or uncertain command",
        ],
    }
    if strategy == STRATEGY_INERT_THEN_ARM_TRACE:
        two_step_rows = []
        for item in validated["operations"]:
            word = int(item["word"])
            chain = word & 0x1F
            two_step_rows.append({
                "index": item["index"],
                "row": item["row"],
                "stage": item["stage"],
                "chain": chain,
                "chain_is_supported_one_hot": chain in (1, 2, 4, 8, 16),
                "step_a_inert_word": "0x%08x" % (word & ~0x1F),
                "step_a_requirement": (
                    "one irreversible command, exact readback, then a complete "
                    "two-pass image/prefix proof before step B"
                ),
                "step_b_chain_only_delta": "0x%08x" % chain,
                "step_b_armed_word": "0x%08x" % word,
                "step_b_requirement": (
                    "a distinct irreversible command and authorization; never "
                    "issue after any uncertain or partial step-A result"
                ),
            })
        common["execution_eligible"] = False
        common["program_loop"] = [
            "re-attest exact ownership and all gates",
            "CMD_READ sealed target and require zero",
            "fsync a unique step-A one-attempt journal record",
            "program chain-zero inert word exactly once",
            "bounded poll; exact row readback and complete two-pass image proof",
            "stop permanently before step B after any uncertainty or mismatch",
            "fsync a distinct step-B authorization and one-attempt record",
            "program only the sealed one-hot chain delta exactly once",
            "bounded poll; exact armed-word readback and complete prefix proof",
            "never retry either substep",
        ]
        common["trace_only_reason"] = (
            "the public two-step idea is useful for failure containment but "
            "doubles irreversible pulses and lacks independent electrical, "
            "journal, continuation, and cold-recovery qualification"
        )
        common["setup"] = [
            "use a separately selected qualified setup strategy; this trace "
            "does not choose or authorize EN_SW/SENSE ordering"
        ]
        common["two_step_row_operations"] = two_step_rows
        common["additional_required_gates"] = [
            "every chain is supported one-hot",
            "unique durable authorization and attempt token per substep",
            "step-A exact complete-image proof before step B",
            "step-A failure or timeout permanently forbids step B and retry",
            "no cleanup/re-glitch after an uncertain command",
            "independent cold recovery and persistence qualification",
        ]
        common["verification_cadence"] = (
            "exact row plus complete two-pass MAIN/prefix proof after each substep"
        )
        common["compatibility_note"] = (
            "review model of the e9e9a33 public-console algorithm; absent from "
            "WRITE_STRATEGIES and unreachable by live execution"
        )
    elif strategy == STRATEGY_PUBLIC_DIRECT:
        common["setup"] = [
            "journal setup intent",
            "require RESENSE_DISABLE(0x821058)=0; never write it",
            "with EN_SW_OVERRIDE still zero, issue one unconditional CMD_SENSE(3)",
            "require CMD[1:0]=0, STATE=4, SENSE_DONE=1, EFP=1, DFP=0",
            "set/read EN_SW_OVERRIDE=1 and re-prove all gates",
            "clear/read SBFE; clear/read WDATA_WR_SECURE; require WDATA PLM open",
            "bind two equal armed-plane MAIN images before CMD_PROGRAM",
        ]
        common["verification_cadence"] = (
            "public-compatible row/prior-prefix/next-blank checks plus XRAY's "
            "two complete MAIN passes after every row and at terminal boundaries"
        )
        common["compatibility_note"] = (
            "reproduces public sense-before-EN controller ordering inside XRAY's "
            "identity, manifest, poison, journal and cold-verification shell"
        )
    else:
        common["setup"] = [
            "journal setup intent",
            "set/read EN_SW_OVERRIDE=1 and re-prove all gates",
            "if sealed initial EFP=0 require RESENSE_DISABLE=0 and issue exactly one CMD_SENSE(3); if EFP=1 skip it and record the bit",
            "after sense require CMD[1:0]=0, STATE=4, SENSE_DONE=1, EFP=1, DFP=0",
            "clear/read SBFE; clear/read WDATA_WR_SECURE; require WDATA PLM open",
            "bind two equal armed-plane MAIN images before CMD_PROGRAM",
        ]
        common["verification_cadence"] = (
            "two complete MAIN passes and exact expected-prefix digest after every row"
        )
        common["compatibility_note"] = (
            "minimizes controller commands and retains the strongest complete-image policy"
        )
    return common

EXPECTED_LIVE_POLICY = {
    "capture_passes_before_setup": 2,
    "capture_rows_each_pass": [0, 511],
    "passes_must_agree": True,
    "cold_equality_required_rows": [0, 487],
    "state_variant_suffix_rows": [488, 511],
    "target_rows_must_be_zero": True,
    "goldilocks_required": True,
    "initial_en_sw_override": 0,
    "first_operation_is_test_row": True,
    "second_tty_confirmation_after_test_row": True,
    "cold_power_cycle_verification_required": True,
    "live_rir_read_required": True,
    "held_state_capture_required": True,
    "held_state_main_cold_equal_rows": [0, 487],
    "held_state_iff_suffix_rows": [488, 511],
    "armed_state_capture_before_first_program_required": True,
    "armed_complete_image_must_equal_held_image": True,
    "setup_variants_by_initial_efp_status": {
        "0": "enable-override-sense-arm",
        "1": "enable-override-skip-sense-arm",
    },
    "command_completion_timeout_seconds": COMMAND_COMPLETION_TIMEOUT_SECONDS,
    "command_poll_interval_seconds": COMMAND_POLL_INTERVAL_SECONDS,
}

EXPECTED_AUTHORIZATION_POLICY = {
    "review_receipt_schema": RECEIPT_SCHEMA,
    "review_receipt_required": True,
    "runtime_exact_tty_confirmations_required": True,
    "detached_signatures_required_for_production": 2,
    "experimental_owner_review_is_not_a_production_signature": True,
}

# This runtime copy is intentionally independent of the compiler.  The final
# deterministic reconstruction below is a second implementation/trust check;
# neither side may accept a field name, coordinate, defect class, profile, or
# stage that the other did not authorize.
RUNTIME_FIELD_POLICY = {
    "OPT_FBIO_DISABLE": (((1, 5, 3, 13), (1, 6, 0, 11)), "disable_mask", True),
    "OPT_FBPA_DISABLE": (((1, 6, 11, 5), (1, 7, 0, 16), (1, 8, 0, 3)), "disable_mask", True),
    "OPT_FBP_DISABLE": (((1, 19, 3, 12),), "disable_mask", True),
    "OPT_ROP_L2_DISABLE": (((1, 19, 15, 1), (1, 20, 0, 16), (1, 21, 0, 7)), "disable_mask", True),
    "OPT_GPC_DISABLE": (((1, 8, 3, 8),), "disable_mask", True),
    "OPT_NVDEC_DISABLE": (((1, 8, 11, 5),), "disable_mask", True),
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": (
        ((1, 9, 2, 1),), "paired_disable_candidate", False),
    "OPT_TPC_GPC0_DISABLE": (((1, 11, 11, 5), (1, 12, 0, 3)), "disable_mask", True),
    "OPT_TPC_GPC1_DISABLE": (((1, 12, 3, 8),), "disable_mask", True),
    "OPT_TPC_GPC2_DISABLE": (((1, 12, 11, 5), (1, 13, 0, 3)), "disable_mask", True),
    "OPT_TPC_GPC3_DISABLE": (((1, 13, 3, 8),), "disable_mask", True),
    "OPT_TPC_GPC4_DISABLE": (((1, 13, 11, 5), (1, 14, 0, 3)), "disable_mask", True),
    "OPT_TPC_GPC5_DISABLE": (((1, 14, 3, 8),), "disable_mask", True),
    "OPT_TPC_GPC6_DISABLE": (((1, 14, 11, 5), (1, 15, 0, 3)), "disable_mask", True),
    "OPT_TPC_GPC7_DISABLE": (((1, 15, 3, 8),), "disable_mask", True),
    "OPT_PES_DISABLE": (((1, 15, 11, 5), (1, 16, 0, 16), (1, 17, 0, 3)), "disable_mask", True),
    # Chain-1 bits 368..370 are global IOCTRL-group disables, not a proved
    # physical mapping of the 12-bit OPT_PERLINK_DISABLE field.  Keep them out
    # of the independent runtime execution allowlist.
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": (
        ((1, 23, 3, 1),), "paired_disable_candidate", False),
    "OPT_SYS_PIPE_DISABLE": (((1, 23, 4, 8),), "disable_mask", True),
    "OPT_PCIE_DEVIDA": (((2, 37, 1, 15), (2, 38, 0, 1)), "identity", False),
    "OPT_PCIE_DEVIDB": (((2, 38, 1, 15), (2, 39, 0, 1)), "identity", False),
    "OPT_DISABLE_GEN3_SPEED": (((2, 39, 9, 1),), "disable_bit", False),
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": (((2, 47, 3, 1),), "disable_bit", False),
    "OPT_PCIE_BOOT_GEN23_DISABLE": (((2, 49, 6, 1),), "disable_bit", False),
    "OPT_PCIE_BOOT_GEN3_DISABLE": (((2, 49, 7, 1),), "disable_bit", False),
}

RUNTIME_PROFILE_POLICY = {
    "specimen-80gb-hbm": (
        frozenset(("OPT_FBIO_DISABLE", "OPT_FBPA_DISABLE", "OPT_FBP_DISABLE",
                   "OPT_ROP_L2_DISABLE")),
        ("memory-prerequisites", "memory-activation"),
    ),
    "specimen-more-sm": (
        frozenset(("OPT_GPC_DISABLE", "OPT_PES_DISABLE", "OPT_ROP_L2_DISABLE",
                   "OPT_TPC_GPC0_DISABLE", "OPT_TPC_GPC1_DISABLE",
                   "OPT_TPC_GPC2_DISABLE", "OPT_TPC_GPC3_DISABLE",
                   "OPT_TPC_GPC4_DISABLE", "OPT_TPC_GPC5_DISABLE",
                   "OPT_TPC_GPC6_DISABLE", "OPT_TPC_GPC7_DISABLE")),
        ("rop-l2-dependency", "compute-prerequisites", "compute-activation"),
    ),
    "xray-common-permanent-topology": (
        frozenset((
            "OPT_FBIO_DISABLE", "OPT_FBPA_DISABLE", "OPT_FBP_DISABLE",
            "OPT_ROP_L2_DISABLE", "OPT_GPC_DISABLE", "OPT_PES_DISABLE",
            "OPT_TPC_GPC0_DISABLE", "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE", "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE", "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE", "OPT_TPC_GPC7_DISABLE",
        )),
        (
            "memory-and-shared-prerequisites", "compute-prerequisites",
            "memory-activation", "compute-activation",
        ),
    ),
    "specimen-pcie-gen4-isolated": (
        frozenset(("OPT_DISABLE_GEN3_SPEED",
                   "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
                   "OPT_PCIE_BOOT_GEN23_DISABLE", "OPT_PCIE_BOOT_GEN3_DISABLE")),
        ("pcie-isolated-gates",),
    ),
    "specimen-a100-20b5-identity": (
        frozenset(("OPT_PCIE_DEVIDA", "OPT_PCIE_DEVIDB")),
        ("identity-last",),
    ),
    "specimen-sys-pipe-to-defect-experimental": (
        frozenset(("OPT_SYS_PIPE_DISABLE",)), ("sys-pipe-experimental",)),
    "specimen-media-mig-to-defect-experimental": (
        frozenset((
            "OPT_SYS_PIPE_DISABLE", "OPT_NVDEC_DISABLE",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
        )),
        (
            "mig-system-pipe-to-defect", "nvdec-to-defect",
            "nvjpg-ofa-inseparable-pair",
        ),
    ),
}

PARTIAL_PREFIX_RESIDUAL = (
    "a crash can leave a verified append-only prefix; cold capture and replan "
    "are mandatory before any continuation"
)
IDENTITY_PARTIAL_PREFIX_RESIDUAL = (
    "DEVIDA/DEVIDB may be temporarily inconsistent after operation 1; "
    "crash/partial-prefix boot and firmware behavior is unqualified"
)
PAIRED_MEDIA_PARTIAL_PREFIX_RESIDUAL = (
    "NVJPG/OFA bits 146 and 371 may be temporarily asymmetric after the first "
    "pair operation; individual attribution and partial-prefix boot behavior are unqualified"
)


def _expected_commit_policy(profile_id):
    if profile_id == "specimen-a100-20b5-identity":
        return {
            "test_row_only_allowed": False,
            "optional_continuation_pause_after_first_row": False,
            "minimum_commit_group_operations": 2,
            "partial_prefix_residual": IDENTITY_PARTIAL_PREFIX_RESIDUAL,
        }
    if profile_id == "specimen-media-mig-to-defect-experimental":
        return {
            "test_row_only_allowed": False,
            "optional_continuation_pause_after_first_row": False,
            "minimum_commit_group_operations": 2,
            "partial_prefix_residual": PAIRED_MEDIA_PARTIAL_PREFIX_RESIDUAL,
        }
    return {
        "test_row_only_allowed": True,
        "optional_continuation_pause_after_first_row": True,
        "minimum_commit_group_operations": 1,
        "partial_prefix_residual": PARTIAL_PREFIX_RESIDUAL,
    }


class VfiAppendError(RuntimeError):
    """A pre-command refusal or a verified, non-poison failure."""


class VfiAppendPoison(VfiAppendError):
    """A BADF/BAD0/all-ones state after which no more BAR0 access is allowed."""


class VfiAppendUncertain(VfiAppendError):
    """A command may have reached OTP but its exact effect was not verified."""


def _require(condition, message):
    if not condition:
        raise VfiAppendError(message)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VfiAppendError("duplicate JSON object key: %r" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise VfiAppendError("non-finite JSON constant is forbidden: %s" % value)


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    return _sha256_bytes(Path(path).read_bytes())


def _packed_sha256(words):
    return _sha256_bytes(b"".join(struct.pack("<I", value) for value in words))


def _parse_u32(value, label):
    if isinstance(value, bool):
        raise VfiAppendError("%s is boolean, not u32" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as exc:
            raise VfiAppendError("%s is not an integer" % label) from exc
    else:
        raise VfiAppendError("%s is not an integer" % label)
    _require(0 <= result <= 0xFFFFFFFF, "%s is outside u32" % label)
    return result


def _parse_words(value, count, label):
    _require(isinstance(value, list), "%s must be an array" % label)
    _require(len(value) == count, "%s must have %d words" % (label, count))
    return [_parse_u32(item, "%s[%d]" % (label, index))
            for index, item in enumerate(value)]


def _normalize_bdf(value):
    result = str(value).lower()
    _require(BDF_RE.fullmatch(result) is not None,
             "BDF must be exact dddd:bb:dd.f")
    return result


def _verify_content_digest(document):
    claimed = document.get("content_sha256_excluding_self")
    _require(isinstance(claimed, str) and re.fullmatch(r"[0-9a-f]{64}", claimed),
             "manifest content digest is missing or malformed")
    unsigned = copy.deepcopy(document)
    del unsigned["content_sha256_excluding_self"]
    actual = _sha256_bytes(_canonical_bytes(unsigned))
    _require(actual == claimed, "manifest content digest mismatch")
    return actual


def _require_sha256(value, label, allow_zero=False):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
             "%s is missing or malformed" % label)
    if not allow_zero:
        _require(value != "0" * 64, "%s is an all-zero placeholder" % label)
    return value


def _require_exact_policy(actual, expected, label):
    _require(isinstance(actual, dict), "%s is missing" % label)
    _require(set(actual) == set(expected),
             "%s field set differs from the required policy" % label)
    for key, value in expected.items():
        _require(key in actual and actual[key] == value,
                 "%s.%s differs from the required policy" % (label, key))


def _parse_identity_u32(identity, key):
    return _parse_u32(identity.get(key), "sealed identity %s" % key)


def _effective_logical_blocks(rows, boundary):
    effective = {}
    for row in range(FF_FIRST, boundary):
        word = rows[row]
        if word in (0, 0xFFFFFFFF) or word & 0x20:
            continue
        effective[(word & 0x1F, (word >> 6) & 0x3FF)] = (
            word >> 16) & 0xFFFF
    return effective


def _project_replay_model(model, effective):
    components = model.get("components_lsb_first")
    _require(isinstance(components, list) and components,
             "field replay model has no components")
    value = 0
    shift = 0
    for index, component in enumerate(components):
        _require(isinstance(component, dict),
                 "field replay component is not an object")
        chain = _parse_u32(component.get("chain"), "component chain")
        block = _parse_u32(component.get("block"), "component block")
        bit = _parse_u32(component.get("bit_in_block"), "component bit")
        width = _parse_u32(component.get("width"), "component width")
        _require(chain <= 0x1F and block <= 0x3FF and width and
                 bit + width <= 16,
                 "field replay component is outside a logical block")
        piece = (effective.get((chain, block), 0) >> bit) & ((1 << width) - 1)
        value |= piece << shift
        shift += width
    return value


def _runtime_reconstruct_manifest(manifest):
    """Rebuild the complete sealed document with the pinned compiler bytes."""
    implementation = manifest.get("implementation_binding", {})
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "tools" / "170hx_vfi_append_manifest.py",
        here.parents[1] / "tools" / "170hx_vfi_append_manifest.py",
    )
    compiler_path = next((item for item in candidates if item.is_file()), None)
    _require(compiler_path is not None,
             "pinned manifest compiler is unavailable at runtime")
    _require(_sha256_file(compiler_path) ==
             implementation.get("compiler_file_sha256"),
             "runtime compiler bytes differ from the manifest binding")
    module_name = "xray_vfi_runtime_rebuild_%s" % _sha256_file(compiler_path)[:16]
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(compiler_path))
        _require(spec is not None and spec.loader is not None,
                 "cannot construct pinned compiler import")
        compiler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiler)
        source = manifest["candidate_source"]
        binding = manifest["binding"]
        cold = manifest["cold_baseline_attestation"]
        rebuilt = compiler.build_manifest(
            source["path"], binding["bdf"], binding["profile_id"],
            cold["main_path"], cold["rir_path"],
            generated_utc=manifest.get("generated_utc"),
        )
    except VfiAppendError:
        raise
    except Exception as exc:
        raise VfiAppendError(
            "pinned runtime manifest reconstruction failed: %s" % exc
        ) from exc
    _require(_canonical_bytes(rebuilt) == _canonical_bytes(manifest),
             "manifest differs from deterministic pinned compiler reconstruction")


def load_and_validate_manifest(path, expected_bdf=None, expected_profile=None):
    """Strictly validate the sealed recipe used by the live backend."""
    raw = Path(path).read_bytes()
    file_sha256 = _sha256_bytes(raw)
    try:
        manifest = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise VfiAppendError("cannot parse strict manifest JSON: %s" % exc) from exc
    _require(isinstance(manifest, dict), "manifest root must be an object")
    _require(manifest.get("schema") == MANIFEST_SCHEMA,
             "unsupported manifest schema")
    content_sha256 = _verify_content_digest(manifest)
    profile = manifest.get("backend_profile")
    _require(isinstance(profile, dict) and
             profile.get("id") == BACKEND_ID and
             profile.get("version") == BACKEND_VERSION,
             "manifest is not bound to the exact VFI append backend version")
    _require(manifest.get("executable") is True and
             manifest.get("execution_eligible") is True,
             "v3 manifest is not structurally execution-eligible")
    _require(manifest.get("execution_authorized") is False,
             "authorization must be supplied interactively at runtime")
    _require(manifest.get("hardware_backend_invoked") is False,
             "input manifest falsely claims prior hardware execution")

    implementation = manifest.get("implementation_binding")
    _require(isinstance(implementation, dict),
             "implementation binding is missing")
    for key in (
        "backend_file_sha256", "compiler_file_sha256",
        "old_candidate_validator_file_sha256",
    ):
        _require_sha256(implementation.get(key),
                        "implementation binding %s" % key)
    _require(implementation["backend_file_sha256"] == _sha256_file(__file__),
             "live backend bytes differ from the manifest-bound implementation")
    _require(implementation.get("compiler_semantics") ==
             "v3-exact-current-replay-2026-11-27" and
             implementation.get("candidate_validator_semantics") ==
             "candidate-plan-v1-strict-plus-independent-replay",
             "implementation semantic-version binding is absent or stale")
    candidate_source = manifest.get("candidate_source")
    _require(isinstance(candidate_source, dict), "candidate source binding missing")
    for key in ("file_sha256", "report_content_sha256_excluding_self"):
        _require_sha256(candidate_source.get(key),
                        "candidate source digest %s" % key)
    _require(candidate_source.get("schema") == "170hx-xray-candidate-plan-v1" and
             candidate_source.get("register_context_mode") ==
             "same-capture-level3",
             "candidate source is not exact-current same-capture Level-3")

    binding = manifest.get("binding")
    _require(isinstance(binding, dict), "manifest binding is missing")
    bdf = _normalize_bdf(binding.get("bdf", ""))
    profile_id = binding.get("profile_id")
    _require(isinstance(profile_id, str) and profile_id,
             "manifest profile_id is missing")
    if expected_bdf is not None:
        _require(bdf == _normalize_bdf(expected_bdf),
                 "CLI BDF does not equal sealed BDF")
    if expected_profile is not None:
        _require(profile_id == expected_profile,
                 "CLI profile does not equal sealed profile")
    commit_policy = manifest.get("commit_policy")
    _require(isinstance(commit_policy, dict) and
             set(commit_policy) == set(_expected_commit_policy(profile_id)) and
             commit_policy == _expected_commit_policy(profile_id),
             "manifest commit policy differs from the exact runtime profile policy")

    images = manifest.get("images")
    _require(isinstance(images, dict), "manifest images are missing")
    baseline = _parse_words(images.get("baseline_main_rows"), MAIN_ROWS,
                            "baseline MAIN")
    proposed = _parse_words(images.get("proposed_main_rows"), MAIN_ROWS,
                            "proposed MAIN")
    rir = _parse_words(images.get("baseline_rir_rows"), 8, "baseline RIR")
    _require(_packed_sha256(baseline) ==
             binding.get("baseline_main_u32le_sha256"),
             "baseline MAIN digest mismatch")
    _require(_packed_sha256(proposed) ==
             binding.get("proposed_main_u32le_sha256"),
             "proposed MAIN digest mismatch")
    _require(_packed_sha256(rir) == binding.get("baseline_rir_u32le_sha256"),
             "baseline RIR digest mismatch")
    _require(_packed_sha256(baseline[:COMPARE_LAST + 1]) ==
             binding.get("baseline_compare_0_487_u32le_sha256"),
             "baseline compare-region digest mismatch")
    _require(_packed_sha256(proposed[:COMPARE_LAST + 1]) ==
             binding.get("proposed_compare_0_487_u32le_sha256"),
             "proposed compare-region digest mismatch")

    fresh = manifest.get("cold_baseline_attestation")
    _require(isinstance(fresh, dict) and fresh.get("complete") is True and
             fresh.get("exact_main_match") is True and
             fresh.get("exact_rir_match") is True,
             "manifest lacks a complete fresh MAIN/RIR attestation")
    _require(fresh.get("main_file_sha256") ==
             binding.get("baseline_main_u32le_sha256") and
             fresh.get("rir_file_sha256") ==
             binding.get("baseline_rir_u32le_sha256"),
             "cold attestation hashes do not bind the sealed images")

    provenance = manifest.get("exact_current_provenance")
    _require(isinstance(provenance, dict),
             "exact-current provenance is missing")
    _require(provenance.get("context_mode") == "same-capture-level3" and
             provenance.get("same_native_capture_artifact") is True and
             provenance.get("pci_tuple_exact_match") is True and
             provenance.get("controller_context_status") == "PASS" and
             provenance.get("controller_context_errors") == [] and
             provenance.get("field_crosscheck_status") == "PASS" and
             provenance.get("all_field_crosschecks_exact") is True,
             "exact-current provenance is incomplete or downgraded")
    capture_sha256 = _require_sha256(
        provenance.get("capture_artifact_sha256"),
        "exact-current capture artifact digest")
    _require(provenance.get("main_native_artifact_sha256") == capture_sha256 and
             provenance.get("rir_native_artifact_sha256") == capture_sha256,
             "MAIN/RIR are not bound to one native capture artifact")
    _require(provenance.get("main_pass_hashes") == [
                 binding.get("baseline_main_u32le_sha256")
             ] * 2 and provenance.get("rir_pass_hashes") == [
                 binding.get("baseline_rir_u32le_sha256")
             ] * 2,
             "native pass hashes do not reproduce the sealed MAIN/RIR images")

    _require_exact_policy(manifest.get("live_policy"), EXPECTED_LIVE_POLICY,
                          "live_policy")
    _require_exact_policy(manifest.get("authorization_policy"),
                          EXPECTED_AUTHORIZATION_POLICY,
                          "authorization_policy")

    allocation = manifest.get("allocation")
    _require(isinstance(allocation, dict), "manifest allocation is missing")
    allowlist = allocation.get("exact_row_allowlist")
    _require(isinstance(allowlist, list) and allowlist,
             "exact row allowlist is empty")
    allowlist = [_parse_u32(row, "allowlist row") for row in allowlist]
    _require(allowlist == sorted(set(allowlist)),
             "row allowlist must be unique and ordered")
    _require(allowlist == list(range(allowlist[0], allowlist[-1] + 1)),
             "row allowlist must be contiguous")
    _require(all(FF_FIRST <= row <= FF_LAST for row in allowlist),
             "row allowlist escapes the blank FF append region")
    _require(all(row <= COMPARE_LAST for row in allowlist),
             "operation row escapes the stable controller prefix 0..487")
    _require(_sha256_bytes(_canonical_bytes(allowlist)) ==
             allocation.get("exact_row_allowlist_sha256"),
             "row allowlist digest mismatch")
    boundary = _parse_u32(allocation.get("iff_boundary"), "IFF boundary")
    _require(max(allowlist) < boundary <= MAIN_ROWS - 1,
             "allowlist overlaps the active IFF suffix")
    quarantine = set(_parse_u32(row, "quarantine row")
                     for row in allocation.get("quarantined_rows", []))
    recovery = set(_parse_u32(row, "recovery row")
                   for row in allocation.get("recovery_reserve_rows", []))
    blank_reserve = [_parse_u32(row, "blank reserve row")
                     for row in allocation.get("blank_reserve_rows", [])]
    _require(set(allowlist).isdisjoint(quarantine | recovery),
             "allowlist overlaps quarantine/recovery rows")
    _require(bool(recovery), "manifest has no append-only recovery reserve")
    _require(len(blank_reserve) >= 16 and
             len(blank_reserve) == len(set(blank_reserve)),
             "manifest has fewer than 16 distinct blank reserve rows")
    _require(set(blank_reserve).isdisjoint(
        set(allowlist) | quarantine | recovery),
        "blank reserve overlaps operations/quarantine/recovery")
    _require(all(row < boundary and baseline[row] == 0 and proposed[row] == 0
                 for row in blank_reserve),
             "blank reserve is not blank below the IFF boundary")

    operations = manifest.get("operations")
    _require(isinstance(operations, list) and len(operations) == len(allowlist),
             "operation count does not equal exact allowlist")
    validated_ops = []
    reconstructed = list(baseline)
    initial_effective = _effective_logical_blocks(baseline, boundary)
    effective = dict(initial_effective)
    changed_field_names = set()
    for index, operation in enumerate(operations):
        _require(isinstance(operation, dict), "operation is not an object")
        op_index = _parse_u32(operation.get("index"), "operation index")
        row = _parse_u32(operation.get("row"), "operation row")
        word = _parse_u32(operation.get("program_word"), "operation word")
        old = _parse_u32(operation.get("expected_old_word"), "operation old")
        _require(op_index == index and row == allowlist[index],
                 "operation order differs from allowlist")
        _require(old == 0 and baseline[row] == 0,
                 "append target is not sealed as blank")
        _require(word != 0, "zero/no-op record is forbidden")
        _require((word & 0x20) == 0, "record has the IFF type bit set")
        _require(word != 0x058009C2,
                 "superseded 0x058009c2 Gen3 record is forbidden")
        decoded = {
            "chain": word & 0x1F,
            "block": (word >> 6) & 0x3FF,
            "data": (word >> 16) & 0xFFFF,
        }
        _require(decoded["chain"] in (1, 2), "record chain is not FF chain 1/2")
        declared_chain = _parse_u32(operation.get("chain"), "operation chain")
        declared_block = _parse_u32(operation.get("block"), "operation block")
        _require(decoded["chain"] == declared_chain and
                 decoded["block"] == declared_block,
                 "record envelope disagrees with operation metadata")
        _require(operation.get("one_attempt_only") is True and
                 operation.get("retry_forbidden_after_command_issue") is True,
                 "operation lacks one-attempt/no-retry interlock")
        logical_old = _parse_u32(
            operation.get("logical_block_old"), "operation logical old")
        logical_new = _parse_u32(
            operation.get("logical_block_new"), "operation logical new")
        _require(logical_old <= 0xFFFF and logical_new <= 0xFFFF and
                 effective.get((decoded["chain"], decoded["block"]), 0) ==
                 logical_old and decoded["data"] == logical_new,
                 "operation fails independent last-record-wins replay")
        fields_changed = operation.get("fields_intentionally_changed")
        _require(isinstance(fields_changed, list) and fields_changed and
                 all(isinstance(name, str) and name for name in fields_changed),
                 "operation field-change ledger is empty or malformed")
        changed_field_names.update(fields_changed)
        if (decoded["chain"] == 2 and decoded["block"] == 39 and
                "OPT_DISABLE_GEN3_SPEED" in
                operation.get("fields_intentionally_changed", [])):
            _require((logical_old ^ logical_new) == (1 << 9) and
                     logical_old & (1 << 9) and
                     not (logical_new & (1 << 9)) and
                     (logical_old & 2) == (logical_new & 2),
                     "Gen3 record must clear bit 9 only and preserve bit 1")
        reconstructed[row] = word
        effective[(decoded["chain"], decoded["block"])] = decoded["data"]
        expected_prefix = operation.get("expected_prefix_compare_u32le_sha256")
        _require(_packed_sha256(reconstructed[:COMPARE_LAST + 1]) == expected_prefix,
                 "operation prefix digest mismatch")
        validated_ops.append({
            "index": index,
            "row": row,
            "word": word,
            "word_hex": "0x%08x" % word,
            "expected_prefix_compare_u32le_sha256": expected_prefix,
            "stage": operation.get("stage"),
            "logical_old": logical_old,
            "logical_new": logical_new,
            "fields_intentionally_changed": list(fields_changed),
        })
    _require(reconstructed == proposed,
             "proposed image is not the ordered operation reconstruction")
    changed = [index for index, pair in enumerate(zip(baseline, proposed))
               if pair[0] != pair[1]]
    _require(changed == allowlist,
             "proposed image changes rows outside exact allowlist")

    proofs = manifest.get("semantic_proofs")
    _require(isinstance(proofs, dict), "semantic proofs are missing")
    transition = proofs.get("physical_transition", {})
    for key in (
        "all_operations_are_zero_to_programmed_word",
        "all_target_rows_were_zero",
        "logical_clear_is_last-record-wins_not_physical_erasure",
        "no_existing_physical_row_is_modified",
    ):
        _require(transition.get(key) is True,
                 "semantic transition proof missing %s" % key)
    _require(proofs.get("every_prefix_structurally_valid") is True and
             proofs.get("active_iff_unchanged_at_every_prefix") is True,
             "prefix/IFF proof is incomplete")
    rir_proof = proofs.get("rir_preservation", {})
    _require(rir_proof.get("physical_rir_words_changed") is False and
             rir_proof.get("baseline_rir_u32le_sha256") ==
             binding.get("baseline_rir_u32le_sha256") and
             rir_proof.get("proposed_rir_u32le_sha256") ==
             binding.get("baseline_rir_u32le_sha256"),
             "RIR preservation proof disagrees with binding")
    field_targets = proofs.get("field_targets")
    _require(isinstance(field_targets, list) and field_targets,
             "field target proofs are missing or vacuous")
    _require(all(isinstance(item, dict) and item.get("target_reached") is True
                 for item in field_targets),
             "one or more field targets are not reached")
    _require(all(item.get("all_observed_defects_remain_disabled") is not False
                 for item in field_targets),
             "one or more field targets enable an observed defect")
    target_names = [item.get("field") for item in field_targets]
    _require(None not in target_names and
             len(target_names) == len(set(target_names)) and
             changed_field_names.issubset(set(target_names)),
             "operation field-change ledger escapes the field targets")
    _require(profile_id in RUNTIME_PROFILE_POLICY,
             "profile is absent from the independent runtime allowlist")
    approved_fields, approved_stages = RUNTIME_PROFILE_POLICY[profile_id]
    _require(set(target_names) == set(approved_fields) and
             provenance.get("approved_profile_policy") == profile_id,
             "manifest field set differs from the independent profile policy")
    replay_models = provenance.get("field_coordinate_replay_model")
    _require(isinstance(replay_models, list) and replay_models and
             {item.get("field") for item in replay_models} == set(target_names),
             "exact-current field coordinate replay model is missing or incomplete")
    target_by_name = {item["field"]: item for item in field_targets}
    for model in replay_models:
        field = model.get("field")
        _require(field in RUNTIME_FIELD_POLICY,
                 "field is absent from the independent runtime allowlist")
        approved_components, approved_class, requires_defect = \
            RUNTIME_FIELD_POLICY[field]
        components = model.get("components_lsb_first")
        _require(isinstance(components, list),
                 "field coordinate model is not an array")
        actual_components = []
        for component in components:
            _require(isinstance(component, dict),
                     "field coordinate component is not an object")
            actual_components.append((
                _parse_u32(component.get("chain"), "runtime component chain"),
                _parse_u32(component.get("block"), "runtime component block"),
                _parse_u32(component.get("bit_in_block"), "runtime component bit"),
                _parse_u32(component.get("width"), "runtime component width"),
            ))
        _require(tuple(actual_components) == tuple(approved_components) and
                 model.get("field_class") == approved_class and
                 ((model.get("defect") is not None) == requires_defect),
                 "field coordinates/class/defect policy differ from the "
                 "independent runtime allowlist")
        proof = target_by_name[field]
        current = _project_replay_model(model, initial_effective)
        final = _project_replay_model(model, effective)
        target = _parse_u32(proof.get("target"), "%s target" % field)
        declared_current = _parse_u32(
            proof.get("current"), "%s current" % field)
        declared_final = _parse_u32(proof.get("final"), "%s final" % field)
        field_class = model.get("field_class")
        _require(field_class in (
                     "disable_mask", "disable_bit", "identity",
                     "paired_disable_candidate",
                 ) and
                 proof.get("field_class") == field_class,
                 "field %s has an invalid replay class" % field)
        _require(current == declared_current and final == declared_final and
                 final == target and proof.get("target_reached") is True and
                 proof.get("independently_replayed") is True,
                 "field %s fails independent runtime semantic replay" % field)
        if field_class != "identity":
            _require((target | current) == current,
                     "field %s is not a monotonic disable reduction" % field)
        defect = model.get("defect")
        if defect is not None:
            defect_value = _parse_u32(defect, "%s defect" % field)
            _require((final & defect_value) == defect_value and
                     proof.get("all_observed_defects_remain_disabled") is True,
                     "field %s does not preserve its observed defect mask" % field)
        if field_class == "paired_disable_candidate":
            expected_pair_defects = {
                "OPT_NVJPG_DEFECTIVE": 0,
                "OPT_OFA_DEFECTIVE": 0,
            }
            _require(
                model.get("paired_direct_defects") == expected_pair_defects and
                proof.get("paired_direct_defects") == expected_pair_defects and
                proof.get("all_observed_defects_remain_disabled") is True and
                target == 0 and final == 0,
                "paired media field lacks the sealed zero-defect proof",
            )
    stage_order = proofs.get("declared_stage_order")
    _require(isinstance(stage_order, list) and
             len(stage_order) == len(set(stage_order)),
             "declared stage order is malformed")
    _require(tuple(stage_order) == tuple(approved_stages),
             "declared stages differ from the independent profile policy")
    if profile_id == "specimen-media-mig-to-defect-experimental":
        pair_fields = {
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
        }
        pair_operations = [
            operation for operation in validated_ops
            if pair_fields.intersection(
                operation.get("fields_intentionally_changed", [])
            )
        ]
        _require(
            len(pair_operations) == 2 and
            [
                (operation["stage"],
                 (operation["word"] >> 6) & 0x3FF)
                for operation in pair_operations
            ] == [
                ("nvjpg-ofa-inseparable-pair", 9),
                ("nvjpg-ofa-inseparable-pair", 23),
            ],
            "paired media operations are not the approved ordered unit",
        )
    operation_stage_indexes = []
    for operation in operations:
        _require(operation.get("stage") in stage_order,
                 "operation stage is absent from declared order")
        operation_stage_indexes.append(stage_order.index(operation["stage"]))
    _require(operation_stage_indexes == sorted(operation_stage_indexes),
             "operation sequence violates declared stage order")

    identity = binding.get("identity")
    _require(isinstance(identity, dict), "sealed PCI identity is missing")
    _require(identity.get("context_mode") == "same-capture-level3" and
             identity.get("same_native_capture_artifact") is True and
             identity.get("pci_tuple_exact_match") is True,
             "sealed identity is not exact-current same-capture Level-3")
    live_identity = identity.get("live_identity")
    if not isinstance(live_identity, dict):
        live_identity = identity.get("physical_identity")
    _require(isinstance(live_identity, dict), "sealed live identity is missing")
    _require(_parse_identity_u32(live_identity, "vendor") == 0x10DE and
             _parse_identity_u32(live_identity, "device") == EXECUTION_DEVICE_ID,
             "VFI append execution is qualified only for exact-current 20c2")
    _require(str(live_identity.get("domain_bdf", "")).lower() == bdf,
             "sealed identity BDF differs from manifest BDF")
    _require(provenance.get("capture_identity") == live_identity,
             "capture identity differs from sealed live identity")
    post_cold = manifest.get("post_cold_expectations")
    _require(isinstance(post_cold, dict) and
             str(post_cold.get("domain_bdf", "")).lower() == bdf and
             _parse_u32(post_cold.get("vendor"), "post-cold vendor") == 0x10DE and
             _parse_u32(post_cold.get("device"), "post-cold device") in
             OBSERVATION_DEVICE_IDS and
             post_cold.get("two_equal_complete_main_passes") is True and
             post_cold.get("two_equal_complete_rir_passes") is True and
             post_cold.get("planner_must_replay_as_noop_for_verified_prefix") is True and
             post_cold.get("feature_validation_is_separate") is True,
             "post-cold expectation policy is missing or malformed")

    # Last, independently re-open every bound source and reproduce the entire
    # document with the exact compiler implementation whose hash was sealed.
    # This rejects a hand-resealed manifest even when its local arithmetic is
    # internally consistent.
    _runtime_reconstruct_manifest(manifest)

    return {
        "manifest": manifest,
        "path": str(Path(path).resolve()),
        "file_sha256": file_sha256,
        "content_sha256": content_sha256,
        "bdf": bdf,
        "profile_id": profile_id,
        "identity": live_identity,
        "baseline": baseline,
        "proposed": proposed,
        "rir": rir,
        "operations": validated_ops,
        "compare_range": [COMPARE_FIRST, COMPARE_LAST],
        "iff_boundary": boundary,
        "residual_research_risks": list(
            manifest.get("residual_research_risks", [])),
        "post_cold_expectations": copy.deepcopy(post_cold),
        "commit_policy": copy.deepcopy(commit_policy),
    }


def load_and_validate_receipt(path, validated):
    raw = Path(path).read_bytes()
    try:
        receipt = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise VfiAppendError("cannot parse strict review receipt: %s" % exc) from exc
    _require(receipt.get("schema") == RECEIPT_SCHEMA,
             "unsupported review receipt schema")
    digest = _verify_content_digest(receipt)
    _require(receipt.get("backend_profile") == BACKEND_ID,
             "receipt backend mismatch")
    _require(receipt.get("manifest_content_sha256") ==
             validated["content_sha256"], "receipt manifest mismatch")
    _require(receipt.get("manifest_file_sha256") == validated["file_sha256"],
             "receipt manifest file hash mismatch")
    _require(receipt.get("bdf") == validated["bdf"], "receipt BDF mismatch")
    _require(receipt.get("profile_id") == validated["profile_id"],
             "receipt profile mismatch")
    _require(type(receipt.get("operation_count")) is int and
             receipt.get("operation_count") == len(validated["operations"]),
             "receipt operation count mismatch")
    _require(receipt.get("experimental_execution_reviewed") is True,
             "receipt does not record experimental execution review")
    _require(receipt.get("production_signature") is False and
             receipt.get("hardware_backend_invoked") is False,
             "receipt misstates signature or hardware status")
    confirmations = receipt.get("confirmations")
    expected_labels = [
        "exact BDF", "exact profile", "manifest content digest",
        "baseline rows-0-487 digest", "operation count",
        "irreversible-risk phrase",
    ]
    _require(isinstance(confirmations, list) and
             [item.get("label") for item in confirmations
              if isinstance(item, dict)] == expected_labels and
             all(item.get("exact_match") is True for item in confirmations),
             "receipt confirmation ledger is incomplete or reordered")
    return {
        "path": str(Path(path).resolve()),
        "file_sha256": _sha256_bytes(raw),
        "content_sha256": digest,
        "receipt": receipt,
    }


def _read_sysfs_hex(path, label):
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        return int(value, 0)
    except (OSError, ValueError) as exc:
        raise VfiAppendError("cannot read %s: %s" % (label, exc)) from exc


def _read_driver_override(path):
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise VfiAppendError("cannot read PCI driver_override: %s" % exc) from exc


def inspect_live_identity(sysfs_root, bdf, sealed_identity,
                          require_ownership=False):
    """Bind the BDF to one unbound GA100 PCI tuple before opening BAR0."""
    bdf = _normalize_bdf(bdf)
    root_link = Path(sysfs_root) / bdf
    try:
        root = root_link.resolve(strict=True)
    except OSError as exc:
        raise VfiAppendError("exact PCI function is absent: %s" % exc) from exc
    _require(root.is_dir(), "exact PCI function does not resolve to a directory")
    driver_link = root / "driver"
    _require(not driver_link.exists() and not driver_link.is_symlink(),
             "a kernel driver owns the exact target; unbind it first")
    driver_override = _read_driver_override(root / "driver_override")
    if require_ownership:
        _require(driver_override == DRIVER_OVERRIDE_SENTINEL,
                 "driver_override does not contain the exact XRAY ownership sentinel")
    actual = {
        "domain_bdf": bdf,
        "vendor": _read_sysfs_hex(root / "vendor", "PCI vendor"),
        "device": _read_sysfs_hex(root / "device", "PCI device"),
        "subsystem_vendor": _read_sysfs_hex(
            root / "subsystem_vendor", "PCI subsystem vendor"),
        "subsystem_device": _read_sysfs_hex(
            root / "subsystem_device", "PCI subsystem device"),
        "revision": _read_sysfs_hex(root / "revision", "PCI revision"),
    }
    _require(actual["vendor"] == 0x10DE, "target is not an NVIDIA PCI function")
    _require(actual["device"] in OBSERVATION_DEVICE_IDS,
             "target is not an allowlisted GA100 identity")
    expected = {
        "domain_bdf": str(sealed_identity.get("domain_bdf", "")).lower(),
        "vendor": _parse_u32(sealed_identity.get("vendor"), "sealed vendor"),
        "device": _parse_u32(sealed_identity.get("device"), "sealed device"),
        "subsystem_vendor": _parse_u32(
            sealed_identity.get("subsystem_vendor"), "sealed subsystem vendor"),
        "subsystem_device": _parse_u32(
            sealed_identity.get("subsystem_device"), "sealed subsystem device"),
        "revision": _parse_u32(sealed_identity.get("revision"), "sealed revision"),
    }
    _require(actual == expected,
             "live PCI tuple does not exactly match the sealed identity")

    config = root / "config"
    try:
        fd = os.open(str(config), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            command = struct.unpack("<H", os.pread(fd, 2, 4))[0]
        finally:
            os.close(fd)
    except (OSError, struct.error) as exc:
        raise VfiAppendError("cannot read PCI command register: %s" % exc) from exc
    _require(command & 0x2, "PCI memory decode is disabled; backend will not enable it")
    resource_table = root / "resource"
    try:
        first_resource = resource_table.read_text(encoding="ascii").splitlines()[0]
        start_text, end_text, _flags_text = first_resource.split()[:3]
        bar_size = int(end_text, 16) - int(start_text, 16) + 1
    except (OSError, ValueError, IndexError) as exc:
        raise VfiAppendError("cannot establish BAR0 size: %s" % exc) from exc
    _require(bar_size >= MAP_SIZE,
             "BAR0 is smaller than the required mapping aperture")
    resource = root / "resource0"
    _require(resource.exists() and not resource.is_symlink(),
             "resource0 is absent or unsafe")
    return {
        "identity": actual,
        "pci_command": "0x%04x" % command,
        "resource0": str(resource),
        "resolved_device_path": str(root),
        "bar0_size": bar_size,
        "driver_unbound": True,
        "driver_override": driver_override,
        "device_inode": [os.lstat(str(root)).st_dev, os.lstat(str(root)).st_ino],
    }


def _validate_state_directory(path):
    """Require one pre-created, private, non-symlink directory owned by euid."""
    path = Path(path)
    try:
        info = os.lstat(str(path))
    except OSError as exc:
        raise VfiAppendError("state directory is unavailable: %s" % exc) from exc
    _require(stat.S_ISDIR(info.st_mode), "state directory is not a directory")
    _require(not stat.S_ISLNK(info.st_mode), "state directory may not be a symlink")
    _require(info.st_uid == os.geteuid(),
             "state directory is not owned by the executing uid")
    _require(stat.S_IMODE(info.st_mode) == 0o700,
             "state directory mode must be exactly 0700")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise VfiAppendError("cannot resolve state directory: %s" % exc) from exc
    # Ancestor aliases such as macOS /var -> /private/var are permitted.  The
    # state-root inode itself was lstat'ed above and must not be a symlink; all
    # child opens remain relative/O_NOFOLLOW and journal containment compares
    # fully resolved parent directories.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | \
        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise VfiAppendError("cannot safely open state directory: %s" % exc) from exc
    try:
        opened = os.fstat(fd)
        _require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino),
                 "state directory changed while it was opened")
    finally:
        os.close(fd)
    return resolved


class DeviceLock(object):
    """Process-wide advisory lock scoped to one BDF and journal directory."""

    def __init__(self, directory, bdf):
        self.directory = Path(directory)
        _validate_state_directory(self.directory)
        self.path = self.directory / (".%s.lock" % bdf.replace(":", "-").replace(".", "-"))
        self.fd = None

    def __enter__(self):
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | \
            getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(str(self.directory), dir_flags)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | \
            getattr(os, "O_CLOEXEC", 0)
        try:
            self.fd = os.open(self.path.name, flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(self.fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o600):
            os.close(self.fd)
            self.fd = None
            raise VfiAppendError("exact-BDF lock file is not a private regular file")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise VfiAppendError("another process holds the exact-BDF lock") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


class MappedBar0(object):
    """Exact resource0 mapping.  It exposes no CLI-facing address/value API."""

    def __init__(self, path):
        self.path = path
        self.fd = None
        self.mapping = None

    def __enter__(self):
        flags = (os.O_RDWR | os.O_SYNC | getattr(os, "O_CLOEXEC", 0) |
                 getattr(os, "O_NOFOLLOW", 0))
        self.fd = os.open(self.path, flags)
        try:
            size = os.fstat(self.fd).st_size
            if size < MAP_SIZE:
                raise VfiAppendError(
                    "opened resource0 is smaller than the required aperture")
            self.mapping = mmap.mmap(
                self.fd, MAP_SIZE, flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        return False

    def read32(self, offset):
        return struct.unpack_from("<I", self.mapping, offset)[0]

    def write32(self, offset, value):
        struct.pack_into("<I", self.mapping, offset, value & 0xFFFFFFFF)


def _journal_name_pattern(bdf):
    return re.compile(
        r"^%s[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.jsonl$" %
        re.escape(_journal_name_prefix(bdf))
    )


def _require_journal_basename(path, bdf):
    name = Path(path).name
    _require(_journal_name_pattern(bdf).fullmatch(name) is not None,
             "journal filename must be an exact safe BDF-prefixed .jsonl name")
    return name


class DurableJournal(object):
    """Create-only, fsync-per-event, hash-chained live-session journal."""

    def __init__(self, path, validated, boot_id, backend_file_sha256,
                 write_strategy=STRATEGY_EVIDENCE_GUARDED):
        write_strategy = validate_write_strategy(write_strategy)
        self.path = Path(path)
        _require_journal_basename(self.path, validated["bdf"])
        _validate_state_directory(self.path.parent)
        _require(not self.path.exists() and not self.path.is_symlink(),
                 "journal path already exists; automatic resume is forbidden")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND |
                 getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | \
            getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(str(self.path.parent), dir_flags)
        try:
            self.fd = os.open(
                self.path.name, flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(self.fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                 stat.S_IMODE(info.st_mode) == 0o600,
                 "new journal is not a private regular file")
        self.sequence = 0
        self.previous = "0" * 64
        self.session_id = uuid.uuid4().hex
        self.immutable = {
            "manifest_content_sha256": validated["content_sha256"],
            "manifest_file_sha256": validated["file_sha256"],
            "manifest_path": str(Path(validated["path"]).resolve()),
            "bdf": validated["bdf"],
            "profile_id": validated["profile_id"],
            "write_strategy": write_strategy,
            "boot_id": boot_id,
            "backend_file_sha256": backend_file_sha256,
        }
        parent_fd = os.open(str(self.path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def append(self, state, event, **details):
        record = {
            "schema": JOURNAL_SCHEMA,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "utc": datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0).isoformat(),
            "state": state,
            "event": event,
            "details": details,
            "previous_event_sha256": self.previous,
        }
        record.update(self.immutable)
        record["event_sha256"] = _sha256_bytes(_canonical_bytes(record))
        payload = _canonical_bytes(record) + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise VfiAppendError("durable journal write failed")
            view = view[written:]
        os.fsync(self.fd)
        self.previous = record["event_sha256"]
        self.sequence += 1
        return record

    def close(self):
        if self.fd is not None:
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


_AUTHORIZATION_SEAL = object()


class RuntimeAuthorization(object):
    """Opaque result of the mandatory runtime TTY confirmation sequence."""

    __slots__ = (
        "manifest_content_sha256", "receipt_content_sha256", "bdf",
        "profile_id", "write_strategy", "nonce", "_seal",
    )

    def __init__(self, validated, receipt, write_strategy, seal):
        _require(seal is _AUTHORIZATION_SEAL,
                 "RuntimeAuthorization can only be minted by TTY confirmation")
        self.write_strategy = validate_write_strategy(write_strategy)
        self.manifest_content_sha256 = validated["content_sha256"]
        self.receipt_content_sha256 = receipt["content_sha256"]
        self.bdf = validated["bdf"]
        self.profile_id = validated["profile_id"]
        self.nonce = uuid.uuid4().hex
        self._seal = seal


def perform_runtime_confirmations(
        validated, receipt, write_strategy=STRATEGY_EVIDENCE_GUARDED,
        input_func=input, stdin_isatty=None, stderr_isatty=None):
    if stdin_isatty is None:
        stdin_isatty = os.isatty(0)
    if stderr_isatty is None:
        stderr_isatty = os.isatty(2)
    _require(stdin_isatty and stderr_isatty,
             "execution confirmations require interactive stdin and stderr")
    strategy = validate_write_strategy(write_strategy)
    contract = confirmation_contract(validated, strategy)
    expected = [
        ("Do you really want to proceed? Type yes: ", contract["answer"]),
        ("Type manifest file SHA-256: ", contract["manifest_file_sha256"]),
        ("Type manifest content SHA-256: ", contract["manifest_content_sha256"]),
        ("Type exact BDF: ", contract["bdf"]),
        ("Type exact profile ID: ", contract["profile_id"]),
        ("Type exact write strategy: ", contract["write_strategy"]),
        ("Type irreversible-risk phrase: ", contract["risk_phrase"]),
        ("Type review receipt content SHA-256: ", receipt["content_sha256"]),
    ]
    for prompt, wanted in expected:
        _require(input_func(prompt) == wanted,
                 "runtime confirmation mismatch")
    return RuntimeAuthorization(validated, receipt, strategy, _AUTHORIZATION_SEAL)


class TtyContinuationAuthorizer(object):
    """One-use, TTY-bound second-stage authorization after the test row."""

    __slots__ = ("authorization_nonce", "_input_func", "_seal", "_used")

    def __init__(self, authorization, input_func, seal):
        _require(seal is _AUTHORIZATION_SEAL and
                 getattr(authorization, "_seal", None) is _AUTHORIZATION_SEAL,
                 "continuation authorizer lacks a confirmed runtime token")
        self.authorization_nonce = authorization.nonce
        self._input_func = input_func
        self._seal = seal
        self._used = False

    def __call__(self, operation):
        _require(not self._used, "continuation authorization is one-use")
        self._used = True
        _require(os.isatty(0) and os.isatty(2),
                 "continuation confirmation lost its controlling TTY")
        wanted = test_row_continuation_phrase(operation)
        prompt = "Test row verified in-session; type '%s' to continue: " % wanted
        return self._input_func(prompt) == wanted


def make_continuation_authorizer(authorization, input_func=input):
    return TtyContinuationAuthorizer(
        authorization, input_func, _AUTHORIZATION_SEAL)


def _read_boot_id(path="/proc/sys/kernel/random/boot_id"):
    try:
        value = Path(path).read_text(encoding="ascii").strip().lower()
    except OSError as exc:
        raise VfiAppendError("cannot bind host boot ID: %s" % exc) from exc
    _require(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value) is not None,
             "host boot ID is malformed")
    return value


def verify_journal(path, expected=None):
    """Verify hash chain, exact event order, one-shot tokens and manifest rows."""
    expected_content_sha256 = (
        expected.get("content_sha256") if isinstance(expected, dict) else expected
    )
    previous = "0" * 64
    records = []
    immutable = None
    immutable_manifest_path = None
    terminal_states = {
        "REFUSED", "QUARANTINED", "PENDING_COLD_VERIFY",
        "TEST_ROW_PENDING_COLD_VERIFY", "COLD_PERSISTENCE_VERIFIED",
    }
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(
                line, object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
        except ValueError as exc:
            raise VfiAppendError("invalid journal JSON line %d" % (index + 1)) from exc
        _require(record.get("schema") == JOURNAL_SCHEMA, "journal schema mismatch")
        _require(record.get("sequence") == index, "journal sequence discontinuity")
        _require(record.get("previous_event_sha256") == previous,
                 "journal hash-chain discontinuity")
        claimed = record.get("event_sha256")
        unsigned = copy.deepcopy(record)
        del unsigned["event_sha256"]
        _require(_sha256_bytes(_canonical_bytes(unsigned)) == claimed,
                 "journal event digest mismatch")
        if expected_content_sha256 is not None:
            _require(record.get("manifest_content_sha256") ==
                     expected_content_sha256,
                     "journal/manifest binding mismatch")
        current_immutable = tuple(record.get(key) for key in (
            "session_id", "manifest_content_sha256", "manifest_file_sha256",
            "bdf", "profile_id", "write_strategy", "boot_id",
            "backend_file_sha256",
        ))
        _require(all(current_immutable), "journal immutable binding is incomplete")
        if immutable is None:
            immutable = current_immutable
            immutable_manifest_path = record.get("manifest_path")
        else:
            _require(current_immutable == immutable,
                     "journal immutable binding changed")
            _require(record.get("manifest_path") == immutable_manifest_path,
                     "journal manifest path binding changed")
        _require(not records or records[-1].get("state") not in terminal_states,
                 "journal contains activity after a terminal state")
        previous = claimed
        records.append(record)
    _require(bool(records), "journal is empty")
    _require(records[0].get("event") == "session-created",
             "journal does not start with session-created")
    _require(records[-1].get("state") in terminal_states,
             "journal has no terminal state")
    stage_events = [record.get("event") for record in records]
    ordered_setup_sense = [
        "session-created", "identity-bound", "runtime-authorized",
        "goldilocks-passive",
        "live-image-bound", "setup-intent", "en-latched",
        "sense-command-boundary", "sense-verified", "armed",
        "armed-image-bound",
    ]
    ordered_setup_skip = [
        "session-created", "identity-bound", "runtime-authorized",
        "goldilocks-passive", "live-image-bound", "setup-intent",
        "en-latched", "sense-skipped-already-complete", "armed",
        "armed-image-bound",
    ]
    ordered_setup_public = [
        "session-created", "identity-bound", "runtime-authorized",
        "goldilocks-passive", "live-image-bound", "setup-intent",
        "sense-command-boundary", "sense-verified", "en-latched", "armed",
        "armed-image-bound",
    ]
    lifecycle_events = (
        "attempt-committed", "program-command-boundary",
        "program-command-issued", "program-command-idle", "row-verified",
        "prefix-image-verified",
    )
    if "setup-intent" in stage_events:
        setup_names = (set(ordered_setup_sense) | set(ordered_setup_skip) |
                       set(ordered_setup_public))
        observed_setup = [event for event in stage_events
                          if event in setup_names]
        valid = any(observed_setup == order[:len(observed_setup)]
                    for order in (
                        ordered_setup_sense, ordered_setup_skip,
                        ordered_setup_public,
                    ))
        _require(observed_setup and valid and
                 all(stage_events.count(event) == 1 for event in observed_setup),
                 "journal setup lifecycle is skipped, repeated, or reordered")
    runtime_records = [record for record in records
                       if record.get("event") == "runtime-authorized"]
    if "setup-intent" in stage_events or any(
            event in stage_events for event in lifecycle_events):
        _require(len(runtime_records) == 1,
                 "live journal lacks one exact runtime authorization")
    lifecycles = {}
    token_order = []
    for position, record in enumerate(records):
        event = record.get("event")
        if event not in lifecycle_events:
            continue
        details = record.get("details", {})
        token = details.get("attempt_token")
        _require(isinstance(token, str) and
                 re.fullmatch(r"[0-9a-f]{32}", token),
                 "journal lifecycle event lacks an exact attempt token")
        if token not in lifecycles:
            lifecycles[token] = []
            token_order.append(token)
        lifecycles[token].append((event, position, details))
    attempted_rows = []
    verified_rows = []
    for operation_index, token in enumerate(token_order):
        lifecycle = lifecycles[token]
        events = [item[0] for item in lifecycle]
        valid_prefixes = [list(lifecycle_events[:count])
                          for count in range(1, len(lifecycle_events) + 1)]
        _require(events in valid_prefixes,
                 "journal attempt lifecycle is repeated, skipped, or reordered")
        first = lifecycle[0][2]
        row = first.get("row")
        word = first.get("word")
        _require(first.get("operation_index") == operation_index,
                 "journal operation index is not the exact sealed order")
        for _event, _position, details in lifecycle[1:]:
            _require(details.get("row") == row and
                     details.get("word") == word,
                     "journal lifecycle row/word changed within an attempt")
        attempted_rows.append(row)
        if events[-1] == "prefix-image-verified":
            verified_rows.append(row)
        if isinstance(expected, dict):
            operations = expected.get("operations", [])
            _require(operation_index < len(operations),
                     "journal has more attempts than the sealed manifest")
            operation = operations[operation_index]
            _require(row == operation["row"] and
                     word == operation["word_hex"],
                     "journal attempt differs from sealed row/word")
            expected_before = list(expected["baseline"][:488])
            for earlier in operations[:operation_index]:
                expected_before[earlier["row"]] = earlier["word"]
            _require(first.get("current_compare_u32le_sha256") ==
                     _packed_sha256(expected_before),
                     "journal attempt is not bound to the measured expected prefix")
            if events[-1] == "prefix-image-verified":
                _require(lifecycle[-1][2].get(
                    "prefix_compare_u32le_sha256") ==
                    operation["expected_prefix_compare_u32le_sha256"],
                    "journal verified-prefix digest differs from manifest")
    _require(len(attempted_rows) == len(set(attempted_rows)),
             "journal attempts a physical row more than once")
    continuation_records = [
        (index, record) for index, record in enumerate(records)
        if record.get("event") == "continuation-authorized"
    ]
    commit_group_profiles = {
        "specimen-a100-20b5-identity": IDENTITY_PARTIAL_PREFIX_RESIDUAL,
        "specimen-media-mig-to-defect-experimental":
            PAIRED_MEDIA_PARTIAL_PREFIX_RESIDUAL,
    }
    if immutable[4] in commit_group_profiles and token_order:
        commit_records = [
            (index, record) for index, record in enumerate(records)
            if record.get("event") == "commit-group-bound"
        ]
        _require(len(commit_records) == 1 and not continuation_records and
                 commit_records[0][0] < lifecycles[token_order[0]][0][1] and
                 commit_records[0][1].get("details", {}).get(
                     "minimum_commit_group_operations") == 2 and
                 commit_records[0][1].get("details", {}).get(
                     "partial_prefix_residual") ==
                 commit_group_profiles[immutable[4]],
                 "attempt lacks its exact minimum two-operation commit-group binding")
    elif len(attempted_rows) > 1:
        continuation_positions = [item[0] for item in continuation_records]
        _require(len(continuation_positions) == 1 and
                 continuation_positions[0] < lifecycles[token_order[1]][0][1],
                 "second irreversible attempt lacks journal-bound continuation")
        _require(len(runtime_records) == 1 and
                 continuation_records[0][1].get("details", {}).get(
                     "authorization_nonce") ==
                 runtime_records[0].get("details", {}).get(
                     "authorization_nonce") and
                 re.fullmatch(
                     r"[0-9a-f]{32}",
                     str(runtime_records[0].get("details", {}).get(
                         "authorization_nonce", ""))),
                 "continuation authorization is not bound to the runtime nonce")
    terminal = records[-1]["state"]
    if terminal in (
        "PENDING_COLD_VERIFY", "TEST_ROW_PENDING_COLD_VERIFY",
        "COLD_PERSISTENCE_VERIFIED",
    ):
        _require(bool(token_order) and attempted_rows == verified_rows and
                 all(len(lifecycles[token]) == len(lifecycle_events)
                     for token in token_order),
                 "pending/verified terminal lacks a complete row lifecycle")
        _require("final-image-verified" in stage_events,
                 "pending/verified terminal lacks final image verification")
        _require("armed-image-bound" in stage_events,
                 "pending/verified terminal lacks armed-plane image binding")
    if terminal == "REFUSED":
        _require(not token_order and "setup-intent" not in stage_events,
                 "REFUSED journal contains an irreversible attempt")
    return {
        "valid": True,
        "records": len(records),
        "terminal_state": terminal,
        "attempted_rows": attempted_rows,
        "verified_rows": verified_rows,
        "last_event_sha256": previous,
        "manifest_content_sha256": immutable[1],
        "manifest_file_sha256": immutable[2],
        "bdf": immutable[3],
        "profile_id": immutable[4],
        "write_strategy": immutable[5],
        "pre_boot_id": immutable[6],
        "backend_file_sha256": immutable[7],
        "manifest_path": immutable_manifest_path,
    }


def _load_strict_json_file(path, label):
    raw = Path(path).read_bytes()
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise VfiAppendError("cannot parse strict %s JSON: %s" % (label, exc)) from exc
    _require(isinstance(document, dict), "%s root must be an object" % label)
    return document, raw


def _expected_image_for_journal(validated, journal):
    attempted_rows = journal["verified_rows"]
    _require(bool(attempted_rows), "cold verification has no verified OTP row")
    expected = list(validated["baseline"])
    expected_rows = []
    for operation in validated["operations"][:len(attempted_rows)]:
        expected[operation["row"]] = operation["word"]
        expected_rows.append(operation["row"])
    _require(attempted_rows == expected_rows,
             "journal rows are not an exact manifest prefix")
    if journal["terminal_state"] == "PENDING_COLD_VERIFY":
        _require(len(attempted_rows) == len(validated["operations"]),
                 "full-session terminal does not contain the full manifest")
    else:
        _require(journal["terminal_state"] ==
                 "TEST_ROW_PENDING_COLD_VERIFY" and len(attempted_rows) == 1,
                 "journal terminal is not an exact cold-verifiable prefix")
    return expected, attempted_rows


def _native_identity(identity, label):
    _require(isinstance(identity, dict), "%s identity is missing" % label)
    return {
        "domain_bdf": _normalize_bdf(identity.get("domain_bdf")),
        "vendor": _parse_u32(identity.get("vendor"), "%s vendor" % label),
        "device": _parse_u32(identity.get("device"), "%s device" % label),
        "subsystem_vendor": _parse_u32(
            identity.get("subsystem_vendor"), "%s subsystem vendor" % label),
        "subsystem_device": _parse_u32(
            identity.get("subsystem_device"), "%s subsystem device" % label),
        "revision": _parse_u32(identity.get("revision"), "%s revision" % label),
    }


def _validate_native_transaction(transaction, kind, count, identity,
                                 required_dsov=None):
    _require(isinstance(transaction, dict), "%s transaction is missing" % kind)
    expected_commands = count * 2
    _require(transaction.get("kind") == kind and
             transaction.get("certified_by_helper") is True and
             transaction.get("protocol_valid") is True and
             transaction.get("coherent") is True and
             transaction.get("kernel_reported_coherent") is True and
             transaction.get("counts_complete") is True and
             transaction.get("driver_bound_at_entry") is False and
             transaction.get("requested_passes") == 2 and
             transaction.get("completed_passes") == 2 and
             transaction.get("row_count") == count and
             transaction.get("cmd_read_commands_issued") == expected_commands and
             transaction.get("completed_data_reads_lower_bound") ==
             expected_commands and
             transaction.get("otp_commands") == 0 and
             transaction.get("status") == 0 and
             transaction.get("status_text") == "ok" and
             transaction.get("failed_pass") is None and
             transaction.get("failed_row") is None and
             transaction.get("transport_error") is None and
             transaction.get("protocol_errors") == [] and
             transaction.get("recovered_from_last") is False and
             transaction.get("address_restored") is True and
             transaction.get("debug_restored") is True and
             transaction.get("helper_abi") in SUPPORTED_NATIVE_HELPER_ABIS,
             "%s transaction is not an exact native certified two-pass read" % kind)
    _require(_native_identity(transaction.get("identity"), kind) == identity,
             "%s transaction identity differs from the card" % kind)
    entry = transaction.get("entry")
    exit_snapshot = transaction.get("exit")
    _require(isinstance(entry, dict) and entry == exit_snapshot,
             "%s transaction did not restore its exact entry snapshot" % kind)
    _require(entry.get("fusectrl") == "0x%08x" % IDLE_VALUE and
             entry.get("en_sw_override") == "0x00000000" and
             entry.get("iff_sw_fusing") == "0x00000000" and
             entry.get("debugctrl") == "0x00000000" and
             entry.get("bypass_fuses_status") == "0x00000000",
             "%s transaction entry/exit state is not the safe native envelope" % kind)
    if required_dsov is not None:
        _require(entry.get("disable_sw_override_status") ==
                 "0x%08x" % required_dsov,
                 "%s transaction is not in the required DSOV state" % kind)
    passes = transaction.get("passes")
    _require(isinstance(passes, list) and len(passes) == 2,
             "%s transaction must contain exactly two passes" % kind)
    parsed = [_parse_words(item, count, "%s pass" % kind) for item in passes]
    hashes = [_packed_sha256(item) for item in parsed]
    _require(parsed[0] == parsed[1] and
             transaction.get("pass_hashes") == hashes,
             "%s transaction passes/hashes are not coherent" % kind)
    return parsed, hashes


def _serialized_helper_certification(certification):
    """Accept current wording or an explicitly interpreted legacy artifact."""
    if not isinstance(certification, dict):
        return False
    current_key = "two_passes_in_one_helper_serialized_ioctl_transaction"
    current = certification.get(current_key)
    legacy = certification.get("same_atomic_helper_transaction_per_pass")
    if current is not None:
        return (
            current is True and
            certification.get("hardware_atomic_snapshot") is False
        )
    return (
        legacy is True and
        certification.get("hardware_atomic_snapshot") is not True
    )


def _validate_native_cold_capture(path, validated, expected,
                                  expected_identity=None,
                                  required_dsov=None):
    source_path = Path(path)
    _require(source_path.is_absolute() and source_path.is_file() and
             not source_path.is_symlink(),
             "native XRAY capture path must be an absolute regular file")
    capture, raw = _load_strict_json_file(path, "post-cold native XRAY capture")
    _require(capture.get("schema") == "170hx-xray-v1" and
             capture.get("command") == "capture" and
             capture.get("status") == "captured",
             "post-cold file is not a native successful XRAY capture")
    tool = capture.get("tool")
    _require(isinstance(tool, dict) and tool.get("name") == "170HX-XRAY" and
             isinstance(tool.get("version"), str) and tool.get("version") and
             re.fullmatch(r"[0-9a-f]{64}", str(tool.get("sha256", ""))),
             "post-cold capture lacks native XRAY tool provenance")
    access = capture.get("access")
    _require(isinstance(access, dict) and
             access.get("canonical_requested_access") == "semantic-read" and
             access.get("effective_access") == "semantic-read" and
             access.get("counts_complete") is True and
             access.get("controller_snapshot_performed") is True and
             access.get("otp_program_commands") == 0 and
             access.get("write_level_requested") is False,
             "post-cold capture root access envelope is not certified read-only")
    safety = capture.get("safety")
    _require(isinstance(safety, dict) and
             safety.get("has_live_write_primitive") is False and
             safety.get("otp_execution_supported") is False and
             safety.get("volatile_write_execution_supported") is False,
             "post-cold capture safety envelope permits a write primitive")
    target_scope = capture.get("target_scope")
    _require(isinstance(target_scope, dict) and
             target_scope.get("all_targets_acknowledged") is True and
             target_scope.get("controller_sequence_stopped") is False and
             target_scope.get("skipped_bdfs") == [] and
             validated["bdf"] in target_scope.get("resolved_bdfs", []),
             "post-cold capture target scope does not certify the exact BDF")
    cards = capture.get("cards")
    _require(isinstance(cards, list), "post-cold capture cards are missing")
    matches = [card for card in cards if isinstance(card, dict) and
               str(card.get("card", {}).get("domain_bdf", "")).lower() ==
               validated["bdf"]]
    _require(len(matches) == 1,
             "post-cold capture must contain the exact BDF exactly once")
    card = matches[0]
    card_identity = _native_identity(card.get("card"), "post-cold card")
    identity_policy = (validated["post_cold_expectations"]
                       if expected_identity is None else expected_identity)
    identity_keys = ("domain_bdf", "vendor", "device")
    if expected_identity is not None:
        identity_keys = identity_keys + (
            "subsystem_vendor", "subsystem_device", "revision")
    for key in identity_keys:
        wanted = (str(identity_policy[key]).lower() if key == "domain_bdf"
                  else _parse_u32(identity_policy[key], "expected %s" % key))
        _require(card_identity[key] == wanted,
                 "post-cold native identity differs at %s" % key)
    _require(card.get("card", {}).get("driver") is None and
             _native_identity(card.get("card", {}).get("xray_helper_identity"),
                              "helper card") == card_identity,
             "post-cold card is driver-bound or helper identity differs")
    card_access = card.get("access")
    _require(isinstance(card_access, dict) and
             card_access.get("canonical_requested_access") == "semantic-read" and
             card_access.get("effective_access") == "semantic-read" and
             card_access.get("counts_complete") is True and
             card_access.get("controller_transaction_error") is None,
             "post-cold card access envelope is incomplete")

    physical = card.get("physical_rows")
    rir = card.get("rir_rows")
    _require(isinstance(physical, dict) and physical.get("available") is True and
             physical.get("coherent") is True and
             isinstance(rir, dict) and rir.get("available") is True and
             rir.get("coherent") is True,
             "post-cold native MAIN/RIR objects are unavailable or incoherent")
    physical_cert = physical.get("certification")
    rir_cert = rir.get("certification")
    _require(isinstance(physical_cert, dict) and
             physical_cert.get("available") is True and
             physical_cert.get("driver_unbound") is True and
             physical_cert.get("full_domain_identity_bound") is True and
             _serialized_helper_certification(physical_cert) and
             physical_cert.get("software_iff_redirection_excluded") is True,
             "post-cold MAIN certification is incomplete")
    _require(isinstance(rir_cert, dict) and rir_cert.get("available") is True and
             rir_cert.get("driver_unbound") is True and
             rir_cert.get("full_domain_identity_bound") is True and
             _serialized_helper_certification(rir_cert) and
             rir_cert.get("debug_rwl_restored") is True,
             "post-cold RIR certification is incomplete")
    main_passes, main_hashes = _validate_native_transaction(
        physical.get("transaction"), "main_ff", MAIN_ROWS, card_identity,
        required_dsov=required_dsov)
    rir_passes, rir_hashes = _validate_native_transaction(
        rir.get("transaction"), "rir", 8, card_identity,
        required_dsov=required_dsov)
    _require(physical.get("passes") == physical["transaction"].get("passes") and
             physical.get("rows") == physical["transaction"].get("passes")[0] and
             physical.get("pass_hashes") == main_hashes,
             "post-cold MAIN object/transaction envelopes disagree")
    _require(rir.get("passes") == rir["transaction"].get("passes") and
             rir.get("rows") == rir["transaction"].get("passes")[0] and
             rir.get("pass_hashes") == rir_hashes,
             "post-cold RIR object/transaction envelopes disagree")
    _require(main_passes[0] == expected,
             "post-cold native MAIN differs from the exact attempted prefix")
    _require(rir_passes[0] == validated["rir"],
             "post-cold native RIR differs from the sealed baseline")
    validate_live_ff_iff_structure(main_passes[0], validated["iff_boundary"])
    return {
        "path": str(Path(path).resolve()),
        "file_sha256": _sha256_bytes(raw),
        "tool_version": tool["version"],
        "tool_sha256": tool["sha256"],
        "identity": card_identity,
        "main": main_passes[0],
        "rir": rir_passes[0],
        "main_pass_hashes": main_hashes,
        "rir_pass_hashes": rir_hashes,
    }


def validate_held_state_capture(path, validated):
    """Bind the glitched DSOV=0 held-window MAIN suffix and live RIR.

    Rows 0..487 remain exact cold/OTP evidence.  Rows 488..511 are deliberately
    state-qualified and are therefore taken only from the two-pass coherent
    native capture in the held window.  The session must reproduce this entire
    512-row image before setup and after every programmed row.
    """
    document, _raw = _load_strict_json_file(path, "held-state native XRAY capture")
    cards = document.get("cards")
    _require(isinstance(cards, list), "held-state capture cards are missing")
    matches = [card for card in cards if isinstance(card, dict) and
               str(card.get("card", {}).get("domain_bdf", "")).lower() ==
               validated["bdf"]]
    _require(len(matches) == 1,
             "held-state capture must contain the exact BDF exactly once")
    transaction = matches[0].get("physical_rows", {}).get("transaction", {})
    passes = transaction.get("passes")
    _require(isinstance(passes, list) and len(passes) == 2,
             "held-state capture lacks two native MAIN passes")
    observed = _parse_words(passes[0], MAIN_ROWS, "held-state MAIN pass")
    runtime_baseline = list(validated["baseline"])
    runtime_baseline[COMPARE_LAST + 1:] = observed[COMPARE_LAST + 1:]
    native = _validate_native_cold_capture(
        path, validated, runtime_baseline,
        expected_identity=validated["identity"], required_dsov=0)
    _require(native["main"][:COMPARE_LAST + 1] ==
             validated["baseline"][:COMPARE_LAST + 1],
             "held-state capture differs from cold OTP rows 0..487")
    _require(native["rir"] == validated["rir"],
             "held-state live RIR differs from the sealed cold RIR")
    native["runtime_baseline"] = runtime_baseline
    native["held_suffix_u32le_sha256"] = _packed_sha256(
        runtime_baseline[COMPARE_LAST + 1:])
    return native


def _validate_power_attestation(path, validated, journal, capture_identity):
    source_path = Path(path)
    _require(source_path.is_absolute() and source_path.is_file() and
             not source_path.is_symlink(),
             "power-cycle attestation path must be an absolute regular file")
    attestation, raw = _load_strict_json_file(path, "power-cycle attestation")
    required_keys = {
        "schema", "operator_confirmed", "method", "slot_power_removed",
        "aux_power_removed", "complete_power_loss_measured",
        "manifest_content_sha256", "journal_last_event_sha256", "bdf",
        "powered_off_utc", "powered_on_utc", "operator",
        "host_remained_powered", "post_cold_identity",
    }
    _require(set(attestation) == required_keys,
             "power-cycle attestation fields do not match the strict schema")
    _require(attestation.get("schema") == COLD_ATTESTATION_SCHEMA and
             attestation.get("operator_confirmed") is True and
             attestation.get("method") ==
             "complete-slot-and-aux-power-removal" and
             attestation.get("slot_power_removed") is True and
             attestation.get("aux_power_removed") is True and
             attestation.get("complete_power_loss_measured") is True and
             type(attestation.get("host_remained_powered")) is bool,
             "power-cycle attestation does not record complete measured power loss")
    _require(attestation.get("manifest_content_sha256") ==
             validated["content_sha256"] and
             attestation.get("journal_last_event_sha256") ==
             journal["last_event_sha256"] and
             str(attestation.get("bdf", "")).lower() == validated["bdf"],
             "power-cycle attestation binding mismatch")
    for key in ("powered_off_utc", "powered_on_utc", "operator"):
        _require(isinstance(attestation.get(key), str) and attestation[key],
                 "power-cycle attestation lacks %s" % key)
    _require(_native_identity(attestation.get("post_cold_identity"),
                              "attested post-cold") == capture_identity,
             "owner-attested identity differs from the native captured identity")
    return attestation, raw


_COLD_RECEIPT_KEYS = {
    "schema", "generated_utc", "status", "bdf", "profile_id",
    "manifest_path", "manifest_content_sha256", "manifest_file_sha256",
    "journal_path", "journal_file_sha256", "journal_last_event_sha256",
    "journal_terminal_state", "verified_rows", "post_cold_capture_path",
    "post_cold_capture_file_sha256", "xray_tool_version", "xray_tool_sha256",
    "main_u32le_sha256", "rir_u32le_sha256", "main_native_pass_sha256",
    "rir_native_pass_sha256", "power_attestation_path",
    "power_attestation_file_sha256", "pre_boot_id", "post_boot_id",
    "post_cold_identity", "native_capture_certified", "planner_noop_verified",
    "feature_validation_complete", "evidence_boundary",
    "content_sha256_excluding_self",
}


def load_and_validate_cold_receipt(path, validated=None, journal_path=None):
    receipt, raw = _load_strict_json_file(path, "cold receipt")
    _require(set(receipt) == _COLD_RECEIPT_KEYS,
             "cold receipt fields do not match the strict certified schema")
    _require(receipt.get("schema") == COLD_RECEIPT_SCHEMA,
             "unsupported cold receipt schema")
    content_sha256 = _verify_content_digest(receipt)
    _require(receipt.get("status") == "COLD_PERSISTENCE_VERIFIED" and
             receipt.get("native_capture_certified") is True and
             receipt.get("planner_noop_verified") is False and
             receipt.get("feature_validation_complete") is False,
             "cold receipt overclaims or lacks certified persistence")
    _require(isinstance(receipt.get("generated_utc"), str) and
             receipt["generated_utc"] and
             isinstance(receipt.get("evidence_boundary"), str) and
             receipt["evidence_boundary"] and
             isinstance(receipt.get("verified_rows"), list) and
             all(type(row) is int for row in receipt["verified_rows"]),
             "cold receipt closure metadata is malformed")
    manifest_path = Path(receipt.get("manifest_path", ""))
    _require(manifest_path.is_absolute() and manifest_path.is_file() and
             not manifest_path.is_symlink(),
             "cold receipt manifest path is unavailable or unsafe")
    historical = load_and_validate_manifest(
        manifest_path, receipt.get("bdf"), receipt.get("profile_id"))
    _require(receipt.get("manifest_content_sha256") ==
             historical["content_sha256"] and
             receipt.get("manifest_file_sha256") == historical["file_sha256"],
             "cold receipt historical manifest binding mismatch")
    if validated is not None:
        _require(receipt.get("manifest_content_sha256") ==
                 validated["content_sha256"] and
                 receipt.get("manifest_file_sha256") ==
                 validated["file_sha256"] and
                 receipt.get("bdf") == validated["bdf"] and
                 receipt.get("profile_id") == validated["profile_id"],
                 "cold receipt/manifest binding mismatch")
    bound_journal = Path(journal_path or receipt.get("journal_path", ""))
    _require(bound_journal.is_absolute() and bound_journal.is_file() and
             not bound_journal.is_symlink(),
             "cold receipt journal path is unavailable or unsafe")
    _require(str(bound_journal.resolve()) == receipt.get("journal_path"),
             "cold receipt journal path binding mismatch")
    journal = verify_journal(bound_journal, historical)
    _require(receipt.get("journal_file_sha256") == _sha256_file(bound_journal) and
             receipt.get("journal_last_event_sha256") ==
             journal["last_event_sha256"] and
             receipt.get("journal_terminal_state") ==
             journal["terminal_state"] and
             receipt.get("verified_rows") == journal["verified_rows"],
             "cold receipt/journal closure binding mismatch")
    expected, _attempted = _expected_image_for_journal(historical, journal)
    native = _validate_native_cold_capture(
        receipt.get("post_cold_capture_path"), historical, expected)
    _require(Path(receipt["post_cold_capture_path"]).is_absolute() and
             str(Path(receipt["post_cold_capture_path"]).resolve()) ==
             receipt["post_cold_capture_path"],
             "cold receipt native capture path is not canonical")
    _require(receipt.get("post_cold_capture_file_sha256") ==
             native["file_sha256"] and
             receipt.get("xray_tool_version") == native["tool_version"] and
             receipt.get("xray_tool_sha256") == native["tool_sha256"] and
             receipt.get("main_u32le_sha256") == _packed_sha256(native["main"]) and
             receipt.get("rir_u32le_sha256") == _packed_sha256(native["rir"]) and
             receipt.get("main_native_pass_sha256") ==
             native["main_pass_hashes"] and
             receipt.get("rir_native_pass_sha256") == native["rir_pass_hashes"] and
             receipt.get("post_cold_identity") == native["identity"],
             "cold receipt/native capture binding mismatch")
    _require(Path(receipt.get("power_attestation_path", "")).is_absolute() and
             str(Path(receipt["power_attestation_path"]).resolve()) ==
             receipt["power_attestation_path"],
             "cold receipt power attestation path is not canonical")
    _attestation, attestation_raw = _validate_power_attestation(
        receipt.get("power_attestation_path"), historical, journal,
        native["identity"])
    _require(receipt.get("power_attestation_file_sha256") ==
             _sha256_bytes(attestation_raw) and
             receipt.get("pre_boot_id") == journal["pre_boot_id"],
             "cold receipt power/boot binding mismatch")
    _require(isinstance(receipt.get("post_boot_id"), str) and
             re.fullmatch(
                 r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                 receipt["post_boot_id"]),
             "cold receipt post-boot ID is malformed")
    return {
        "receipt": receipt,
        "path": str(Path(path).resolve()),
        "file_sha256": _sha256_bytes(raw),
        "content_sha256": content_sha256,
    }


def verify_cold_persistence(validated, journal_path, post_cold_capture_path,
                            power_attestation_path,
                            post_boot_id_path=CANONICAL_BOOT_ID_PATH):
    """Close one journal only from a certified native post-cold XRAY capture."""
    journal_path = Path(journal_path).resolve()
    journal = verify_journal(journal_path, validated)
    _require(journal["terminal_state"] in (
        "PENDING_COLD_VERIFY", "TEST_ROW_PENDING_COLD_VERIFY"),
        "journal is not awaiting cold verification")
    expected, attempted_rows = _expected_image_for_journal(validated, journal)
    native = _validate_native_cold_capture(
        post_cold_capture_path, validated, expected)
    attestation, attestation_raw = _validate_power_attestation(
        power_attestation_path, validated, journal, native["identity"])
    post_boot_id = _read_boot_id(post_boot_id_path)
    pre_boot_id = journal["pre_boot_id"]
    _require(post_boot_id != pre_boot_id or
             attestation.get("host_remained_powered") is True,
             "host boot ID did not change and attestation does not state a "
             "measured slot-only power cycle")

    receipt = {
        "schema": COLD_RECEIPT_SCHEMA,
        "generated_utc": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "status": "COLD_PERSISTENCE_VERIFIED",
        "bdf": validated["bdf"],
        "profile_id": validated["profile_id"],
        "manifest_path": str(Path(validated["path"]).resolve()),
        "manifest_content_sha256": validated["content_sha256"],
        "manifest_file_sha256": validated["file_sha256"],
        "journal_path": str(journal_path),
        "journal_file_sha256": _sha256_file(journal_path),
        "journal_last_event_sha256": journal["last_event_sha256"],
        "journal_terminal_state": journal["terminal_state"],
        "verified_rows": attempted_rows,
        "post_cold_capture_path": native["path"],
        "post_cold_capture_file_sha256": native["file_sha256"],
        "xray_tool_version": native["tool_version"],
        "xray_tool_sha256": native["tool_sha256"],
        "main_u32le_sha256": _packed_sha256(native["main"]),
        "rir_u32le_sha256": _packed_sha256(native["rir"]),
        "main_native_pass_sha256": native["main_pass_hashes"],
        "rir_native_pass_sha256": native["rir_pass_hashes"],
        "power_attestation_path": str(Path(power_attestation_path).resolve()),
        "power_attestation_file_sha256": _sha256_bytes(attestation_raw),
        "pre_boot_id": pre_boot_id,
        "post_boot_id": post_boot_id,
        "post_cold_identity": native["identity"],
        "native_capture_certified": True,
        "planner_noop_verified": False,
        "feature_validation_complete": False,
        "evidence_boundary": (
            "exact native helper-certified two-pass MAIN/RIR persistence plus "
            "hash-bound owner power evidence; planner no-op replay and feature "
            "stability remain separate gates"
        ),
    }
    receipt["content_sha256_excluding_self"] = _sha256_bytes(
        _canonical_bytes(receipt))
    return receipt


def _is_poison_value(value):
    return value == 0xFFFFFFFF or (value >> 16) in (0xBADF, 0xBAD0)


def _is_idle(value):
    return value == IDLE_VALUE


def validate_live_ff_iff_structure(rows, expected_boundary):
    """Validate the FF/IFF envelope without assigning field semantics."""
    _require(isinstance(rows, list) and len(rows) == MAIN_ROWS,
             "live MAIN image must contain 512 rows")
    boundary = MAIN_ROWS
    for row in range(FF_FIRST, MAIN_ROWS):
        value = rows[row]
        _require(isinstance(value, int) and 0 <= value <= 0xFFFFFFFF,
                 "live MAIN contains a non-u32 row")
        if value != 0xFFFFFFFF and value & 0x20:
            boundary = row
            break
    _require(boundary == expected_boundary,
             "live IFF boundary %d differs from sealed boundary %d" %
             (boundary, expected_boundary))
    lengths = {0x00: 1, 0x1B: 3, 0x1C: 3, 0x1D: 2, 0x1E: 1, 0x1F: 1}
    opcodes = []
    cursor = boundary
    while cursor < MAIN_ROWS:
        value = rows[cursor]
        if value == 0xFFFFFFFF:
            opcodes.append({"row": cursor, "opcode": "invalidated", "length": 1})
            cursor += 1
            continue
        _require(value & 0x20, "IFF row %d has its type bit clear" % cursor)
        opcode = value & 0x1F
        _require(opcode in lengths, "unknown IFF opcode 0x%02x" % opcode)
        length = lengths[opcode]
        _require(cursor + length <= MAIN_ROWS, "IFF record overruns row 511")
        opcodes.append({"row": cursor, "opcode": "0x%02x" % opcode,
                        "length": length})
        cursor += length
    return {
        "structurally_valid": True,
        "iff_boundary": boundary,
        "records": opcodes,
        "suffix_u32le_sha256": _packed_sha256(rows[boundary:]),
        "semantic_claim": False,
    }


class VfiAppendSession(object):
    """One held-window transaction.  No automatic retry or resume exists."""

    def __init__(self, bar, validated, journal=None, clock=time.monotonic,
                 sleeper=time.sleep,
                 command_timeout=COMMAND_COMPLETION_TIMEOUT_SECONDS,
                 poll_interval=COMMAND_POLL_INTERVAL_SECONDS,
                 ownership_check=None, authorization_nonce=None,
                 held_baseline=None,
                 write_strategy=STRATEGY_EVIDENCE_GUARDED):
        self.bar = bar
        self.validated = validated
        self.journal = journal
        self.clock = clock
        self.sleeper = sleeper
        self.command_timeout = command_timeout
        self.poll_interval = poll_interval
        _require(command_timeout == COMMAND_COMPLETION_TIMEOUT_SECONDS and
                 poll_interval == COMMAND_POLL_INTERVAL_SECONDS,
                 "controller timeout/poll interval differs from sealed policy")
        self.ownership_check = ownership_check
        self.authorization_nonce = authorization_nonce
        self.write_strategy = validate_write_strategy(write_strategy)
        if held_baseline is not None:
            _require(isinstance(held_baseline, list) and
                     len(held_baseline) == MAIN_ROWS,
                     "held-state baseline must contain 512 words")
            self.runtime_baseline = list(held_baseline)
        else:
            self.runtime_baseline = None
        self.poisoned = False
        self.command_uncertain = False
        self.program_commands = 0
        self.attempts_committed = 0
        self.verified_program_commands = 0
        self.controller_read_commands = 0
        self.operational_writes = 0
        self.event_log = []
        self._write_phase = None
        self._active_operation = None
        self.phase = "new"
        self.setup_started = False
        self.next_operation_index = 0
        self.current_compare_image = None
        self.initial_efp_status = None

    def _raw_read(self, offset):
        if self.poisoned:
            raise VfiAppendPoison("BAR0 access forbidden after poison")
        try:
            value = self.bar.read32(offset) & 0xFFFFFFFF
        except Exception as exc:
            self.poisoned = True
            raise VfiAppendPoison(
                "BAR0 read failed; no further MMIO: %s" % exc) from exc
        self.event_log.append(("read", offset, value))
        return value

    def _ctrl(self, require_exact=False):
        value = self._raw_read(FUSECTRL)
        if _is_poison_value(value):
            self.poisoned = True
            raise VfiAppendPoison(
                "FUSECTRL poison/unreachable value 0x%08x; no further BAR0 access" % value)
        if require_exact:
            if value != IDLE_VALUE:
                self.poisoned = True
                raise VfiAppendPoison(
                    "FUSECTRL is 0x%08x, expected exact 0x%08x; session stopped" %
                    (value, IDLE_VALUE))
        return value

    def _gate_read(self, offset, label, allow_all_ones=False):
        self._ctrl(require_exact=True)
        value = self._raw_read(offset)
        tagged = (value >> 16) in (0xBADF, 0xBAD0)
        if tagged or (value == 0xFFFFFFFF and not allow_all_ones):
            self.poisoned = True
            raise VfiAppendPoison(
                "%s returned poison 0x%08x; no further BAR0 access" %
                (label, value))
        return value

    def _write(self, offset, value, role):
        if self.poisoned:
            raise VfiAppendPoison("write forbidden after poison")
        op = self._active_operation
        allowed = False
        if role == "read-address":
            allowed = offset == FUSEADDR and COMPARE_FIRST <= value < MAIN_ROWS
        elif role == "read-command":
            allowed = offset == FUSECTRL and value == CMD_READ
        elif role == "enable-override":
            allowed = offset == EN_SW_OVERRIDE and value == 1
        elif role == "sense-command":
            allowed = offset == FUSECTRL and value == CMD_SENSE
        elif role == "clear-sbfe":
            allowed = offset == SBFE_SHADOW and value == 0
        elif role == "open-wdata":
            allowed = offset == WDATA_WR_SECURE and value == 0
        elif role == "program-address":
            allowed = bool(op and offset == FUSEADDR and value == op["row"])
        elif role == "program-word":
            allowed = bool(op and offset == FUSEWDATA and value == op["word"])
        elif role == "program-command":
            allowed = bool(op and offset == FUSECTRL and value == CMD_PROGRAM)
        _require(allowed, "internal write-alphabet violation for %s" % role)
        try:
            self.bar.write32(offset, value & 0xFFFFFFFF)
        except Exception as exc:
            self.poisoned = True
            raise VfiAppendPoison(
                "BAR0 write failed; no further MMIO: %s" % exc) from exc
        self.operational_writes += 1
        self.event_log.append(("write", offset, value & 0xFFFFFFFF, role))

    def _wait_idle(self, command, row=None):
        deadline = self.clock() + self.command_timeout
        while True:
            value = self._ctrl()
            if _is_idle(value):
                return value
            state = (value >> 16) & 0x1F
            if state not in VALID_FUSECTRL_STATES:
                self.poisoned = True
                if command == CMD_PROGRAM:
                    self.command_uncertain = True
                raise VfiAppendPoison(
                    "FUSECTRL entered invalid state %d (0x%08x); "
                    "no further BAR0 access" % (state, value))
            if self.clock() >= deadline:
                self.poisoned = True
                if command == CMD_PROGRAM:
                    self.command_uncertain = True
                    raise VfiAppendUncertain(
                        "CMD_PROGRAM completion uncertain at row %s" % row)
                raise VfiAppendPoison(
                    "controller timeout after command %d; no further MMIO" % command)
            self.sleeper(self.poll_interval)

    def _attest_ownership(self):
        _require(callable(self.ownership_check),
                 "live OTP execution lacks an ownership re-attestation callback")
        result = self.ownership_check()
        _require(result is True,
                 "live OTP ownership re-attestation did not return exact True")
        return True

    def _read_address_latch(self, expected):
        value = self._raw_read(FUSEADDR)
        if value != expected:
            self.poisoned = True
            raise VfiAppendPoison(
                "FUSEADDR latch is 0x%08x, expected row %d; "
                "no further BAR0 access" % (value, expected))
        return value

    def read_row(self, row, expected=None):
        _require(COMPARE_FIRST <= row < MAIN_ROWS, "row outside controller range")
        ctrl = self._ctrl()
        _require(_is_idle(ctrl), "controller not idle before row %d" % row)
        self._write(FUSEADDR, row, "read-address")
        self._read_address_latch(row)
        self._write(FUSECTRL, CMD_READ, "read-command")
        self.controller_read_commands += 1
        self._wait_idle(CMD_READ, row)
        value = self._raw_read(FUSERDATA)
        # FUSERDATA is data, so 0xffffffff and BADx-shaped words can be valid.
        # Treat them as transport poison only when the sealed expected word for
        # this exact row proves that they cannot be the intended data.
        if expected is not None and value != expected and _is_poison_value(value):
            self.poisoned = True
            raise VfiAppendPoison(
                "FUSERDATA row %d returned unexpected poison-shaped 0x%08x; "
                "no further BAR0 access" % (row, value))
        return value

    def read_compare_region(self):
        return [self.read_row(row)
                for row in range(COMPARE_FIRST, COMPARE_LAST + 1)]

    def read_all_rows(self, expected=None):
        return [self.read_row(
            row,
            expected[row] if expected is not None else None,
        ) for row in range(MAIN_ROWS)]

    def gate_snapshot(self, require_armed=False, expected_en=None):
        ctrl = self._ctrl(require_exact=True)
        values = {
            "FUSECTRL": ctrl,
            "GFW_BOOT": self._gate_read(GFW_BOOT, "GFW_BOOT"),
            "DSOV": self._gate_read(DSOV_STATUS, "DSOV"),
            "CRC_STATUS": self._gate_read(CRC_STATUS, "CRC_STATUS"),
            "FUSECTRL_PLM": self._gate_read(
                FUSECTRL_PLM, "FUSECTRL_PLM", allow_all_ones=True),
            "OPT_PRIV_MASK": self._gate_read(
                OPT_PRIV_MASK, "OPT_PRIV_MASK", allow_all_ones=True),
            "FUSEDEBUG": self._gate_read(FUSEDEBUG, "FUSEDEBUG"),
            "SWIFF": self._gate_read(SWIFF, "SWIFF"),
            "IFF_CTRL": self._gate_read(IFF_CTRL, "IFF_CTRL"),
            "LOADER_QUIESCENT": self._gate_read(
                LOADER_QUIESCENT, "LOADER_QUIESCENT"),
            "SENTINEL_ALIAS": self._gate_read(SENTINEL_ALIAS, "SENTINEL_ALIAS"),
            "FPF_SENTINEL": self._gate_read(FPF_SENTINEL, "FPF_SENTINEL"),
            "EN_SW_OVERRIDE": self._gate_read(
                EN_SW_OVERRIDE, "EN_SW_OVERRIDE"),
            "EFP_STATUS": self._gate_read(EFP_STATUS, "EFP_STATUS"),
            "PROGRAM_DISABLE_STATUS": self._gate_read(
                PROGRAM_DISABLE_STATUS, "PROGRAM_DISABLE_STATUS"),
            "BYPASS_FUSES_STATUS": self._gate_read(
                BYPASS_FUSES_STATUS, "BYPASS_FUSES_STATUS"),
            "SBFE_SHADOW": self._gate_read(SBFE_SHADOW, "SBFE_SHADOW"),
            "WDATA_WR_SECURE": self._gate_read(
                WDATA_WR_SECURE, "WDATA_WR_SECURE"),
            "FUSEWDATA_PLM": self._gate_read(
                FUSEWDATA_PLM, "FUSEWDATA_PLM", allow_all_ones=True),
            "RESENSE_DISABLE": self._gate_read(
                RESENSE_DISABLE, "RESENSE_DISABLE"),
            "FUSETIME_RD1": self._gate_read(FUSETIME_RD1, "FUSETIME_RD1"),
            "FUSETIME_RD2": self._gate_read(FUSETIME_RD2, "FUSETIME_RD2"),
            "FUSETIME_PGM1": self._gate_read(FUSETIME_PGM1, "FUSETIME_PGM1"),
            "FUSETIME_PGM2": self._gate_read(FUSETIME_PGM2, "FUSETIME_PGM2"),
            "FUSETIME_RD3": self._gate_read(FUSETIME_RD3, "FUSETIME_RD3"),
        }
        _require((values["GFW_BOOT"] & 0xFF) == 0xFF,
                 "GFW is not ready (low byte != 0xff)")
        _require(values["DSOV"] == 0, "DSOV is not zero")
        _require(values["CRC_STATUS"] == 0x55, "CRC_STATUS is not 0x55")
        _require(values["FUSECTRL_PLM"] == 0xFFFFFFFF,
                 "FUSECTRL_PLM is not fully open")
        _require(values["OPT_PRIV_MASK"] == 0xFFFFFFFF,
                 "OPT_PRIV_MASK is not fully open")
        _require(values["FUSEDEBUG"] == 0, "FUSEDEBUG is nonzero")
        _require(values["SWIFF"] == 0 and values["IFF_CTRL"] == 0,
                 "software IFF state is active")
        _require(values["LOADER_QUIESCENT"] == 0,
                 "loader is not quiescent")
        _require(values["FPF_SENTINEL"] != 0xFFFFFFFF,
                 "FPF sentinel is unreadable")
        _require(values["EN_SW_OVERRIDE"] in (0, 1),
                 "EN_SW_OVERRIDE is not canonical")
        if expected_en is not None:
            _require(values["EN_SW_OVERRIDE"] == expected_en,
                     "EN_SW_OVERRIDE differs from the required phase value")
        _require(values["EFP_STATUS"] in (0, 1), "EFP_STATUS is not canonical")
        _require(values["PROGRAM_DISABLE_STATUS"] == 0,
                 "fuse programming is disabled")
        _require(values["BYPASS_FUSES_STATUS"] == 0,
                 "BYPASS_FUSES_STATUS is not zero")
        _require(values["RESENSE_DISABLE"] in (0, 1),
                 "RESENSE_DISABLE is not canonical")
        if require_armed:
            _require(values["EN_SW_OVERRIDE"] == 1,
                     "EN_SW_OVERRIDE is not set")
            _require(values["EFP_STATUS"] == 1,
                     "EFP_STATUS is not complete in the armed phase")
            _require(values["SBFE_SHADOW"] == 0,
                     "SBFE shadow is not clear")
            _require(values["WDATA_WR_SECURE"] == 0,
                     "WDATA_WR_SECURE is not clear")
            _require(values["FUSEWDATA_PLM"] == 0xFFFFFFFF,
                     "FUSEWDATA_PLM is not fully open")
        return values

    def capture_pair_with_expected(self, expected, label,
                                   allow_state_variant_suffix=False):
        read_expected = None if allow_state_variant_suffix else expected
        first = self.read_all_rows(read_expected)
        second = self.read_all_rows(read_expected)
        _require(first == second, "%s two 512-row passes disagree" % label)
        if allow_state_variant_suffix:
            _require(first[:COMPARE_LAST + 1] ==
                     expected[:COMPARE_LAST + 1],
                     "%s differs from sealed rows 0..487" % label)
        else:
            _require(first == expected,
                     "%s differs from the complete expected 512-row image" % label)
        wanted = expected[COMPARE_FIRST:COMPARE_LAST + 1]
        actual = first[COMPARE_FIRST:COMPARE_LAST + 1]
        mismatches = [index for index, pair in enumerate(zip(actual, wanted))
                      if pair[0] != pair[1]]
        _require(not mismatches,
                 "%s differs at controller rows %s" %
                 (label, mismatches[:16]))
        structure = validate_live_ff_iff_structure(
            first, self.validated["iff_boundary"])
        self.current_compare_image = list(actual)
        return {
            "label": label,
            "passes": 2,
            "rows_per_pass": MAIN_ROWS,
            "compare_rows": len(actual),
            "pass_u32le_sha256": _packed_sha256(first),
            "compare_u32le_sha256": _packed_sha256(actual),
            "live_suffix_rows_488_511": ["0x%08x" % value
                                          for value in first[488:512]],
            "ff_iff_structure": structure,
            "matches": True,
        }

    def preflight(self):
        _require(self.phase == "new", "preflight is one-shot per session")
        gates = self.gate_snapshot(require_armed=False, expected_en=0)
        self.initial_efp_status = gates["EFP_STATUS"]
        if self.runtime_baseline is None:
            comparison = self.capture_pair_with_expected(
                self.validated["baseline"], "same-session sealed baseline",
                allow_state_variant_suffix=True)
            self.runtime_baseline = list(self.validated["baseline"])
            self.runtime_baseline[COMPARE_LAST + 1:] = [
                _parse_u32(value, "captured held suffix")
                for value in comparison["live_suffix_rows_488_511"]
            ]
        else:
            comparison = self.capture_pair_with_expected(
                self.runtime_baseline,
                "same-session held-state certified baseline")
        post_capture_gates = self.gate_snapshot(
            require_armed=False, expected_en=0)
        _require(post_capture_gates["EFP_STATUS"] == self.initial_efp_status,
                 "EFP_STATUS changed during passive preflight capture")
        for operation in self.validated["operations"]:
            _require(self.validated["baseline"][operation["row"]] == 0,
                     "target row is not blank in sealed baseline")
        if self.journal is not None:
            self.journal.append(
                "GOLDILOCKS_PASSIVE", "goldilocks-passive",
                gates={key: "0x%08x" % value for key, value in gates.items()})
            self.journal.append(
                "LIVE_IMAGE_BOUND", "live-image-bound",
                compare_u32le_sha256=comparison["compare_u32le_sha256"],
                suffix_u32le_sha256=
                comparison["ff_iff_structure"]["suffix_u32le_sha256"])
        self.phase = "preflight"
        return {"gates": gates, "comparison": comparison,
                "post_capture_gates": post_capture_gates}

    def sense_and_arm(self):
        _require(self.phase == "preflight",
                 "SENSE/arm requires exactly one completed preflight")
        self.phase = "setup-started"
        self.setup_started = True
        if self.journal is not None:
            self.journal.append("SETUP_INTENT", "setup-intent")
        if self.write_strategy == STRATEGY_PUBLIC_DIRECT:
            setup_variant = "sense-enable-override-arm"
            before_sense = self.gate_snapshot(
                require_armed=False, expected_en=0)
            _require(before_sense["RESENSE_DISABLE"] == 0,
                     "RESENSE_DISABLE blocks the unconditional public sense")
            if self.journal is not None:
                self.journal.append("SENSE_BOUNDARY", "sense-command-boundary")
            self._write(FUSECTRL, CMD_SENSE, "sense-command")
            sense_ctrl = self._wait_idle(CMD_SENSE)
            _require((sense_ctrl & (1 << 30)) != 0,
                     "SENSE completed without FUSE_SENSE_DONE")
            after_sense = self.gate_snapshot(
                require_armed=False, expected_en=0)
            _require(after_sense["EFP_STATUS"] == 1,
                     "SENSE did not set EFP_STATUS")
            _require(after_sense["RESENSE_DISABLE"] == 1,
                     "SENSE did not consume the one-shot RESENSE_DISABLE gate")
            if self.journal is not None:
                self.journal.append("SENSE_VERIFIED", "sense-verified",
                                    efp_status="0x00000001",
                                    fusectrl="0x%08x" % sense_ctrl,
                                    resense_disable="0x%08x" %
                                    after_sense["RESENSE_DISABLE"])
            self._write(EN_SW_OVERRIDE, 1, "enable-override")
            _require(self._gate_read(EN_SW_OVERRIDE, "EN_SW_OVERRIDE") == 1,
                     "EN_SW_OVERRIDE=1 did not latch")
            after_en = self.gate_snapshot(
                require_armed=False, expected_en=1)
            if self.journal is not None:
                self.journal.append("EN_LATCHED", "en-latched",
                                    en_sw_override="0x00000001")
        else:
            self._write(EN_SW_OVERRIDE, 1, "enable-override")
            _require(self._gate_read(EN_SW_OVERRIDE, "EN_SW_OVERRIDE") == 1,
                     "EN_SW_OVERRIDE=1 did not latch")
            after_en = self.gate_snapshot(
                require_armed=False, expected_en=1)
            if self.journal is not None:
                self.journal.append("EN_LATCHED", "en-latched",
                                    en_sw_override="0x00000001")
            _require(after_en["EFP_STATUS"] == self.initial_efp_status,
                     "EFP_STATUS changed before the sealed setup-variant branch")
            if self.initial_efp_status == 0:
                setup_variant = "enable-override-sense-arm"
                _require(after_en["RESENSE_DISABLE"] == 0,
                         "RESENSE_DISABLE blocks the required EFP recovery sense")
                if self.journal is not None:
                    self.journal.append(
                        "SENSE_BOUNDARY", "sense-command-boundary")
                self._write(FUSECTRL, CMD_SENSE, "sense-command")
                sense_ctrl = self._wait_idle(CMD_SENSE)
                _require((sense_ctrl & (1 << 30)) != 0,
                         "SENSE completed without FUSE_SENSE_DONE")
                after_sense = self.gate_snapshot(
                    require_armed=False, expected_en=1)
                _require(after_sense["EFP_STATUS"] == 1,
                         "SENSE did not set EFP_STATUS")
                _require(after_sense["RESENSE_DISABLE"] == 1,
                         "SENSE did not consume the one-shot RESENSE_DISABLE gate")
                if self.journal is not None:
                    self.journal.append(
                        "SENSE_VERIFIED", "sense-verified",
                        efp_status="0x00000001",
                        fusectrl="0x%08x" % sense_ctrl,
                        resense_disable="0x%08x" %
                        after_sense["RESENSE_DISABLE"])
            else:
                _require(self.initial_efp_status == 1,
                         "initial EFP_STATUS has no sealed setup variant")
                setup_variant = "enable-override-skip-sense-arm"
                after_sense = after_en
                if self.journal is not None:
                    self.journal.append(
                        "SENSE_SKIPPED", "sense-skipped-already-complete",
                        efp_status="0x00000001",
                        resense_disable="0x%08x" %
                        after_sense["RESENSE_DISABLE"])
        self._write(SBFE_SHADOW, 0, "clear-sbfe")
        _require(self._gate_read(SBFE_SHADOW, "SBFE_SHADOW") == 0,
                 "SBFE_SHADOW=0 did not latch")
        after_sbfe = self.gate_snapshot(require_armed=False, expected_en=1)
        self._write(WDATA_WR_SECURE, 0, "open-wdata")
        _require(self._gate_read(WDATA_WR_SECURE, "WDATA_WR_SECURE") == 0,
                 "WDATA_WR_SECURE=0 did not latch")
        armed = self.gate_snapshot(require_armed=True)
        if self.journal is not None:
            self.journal.append("ARMED", "armed",
                                sbfe="0x00000000",
                                wdata_secure="0x00000000")
        self.phase = "armed"
        return {"write_strategy": self.write_strategy,
                "setup_variant": setup_variant,
                "after_en": after_en, "after_sense": after_sense,
                "after_sbfe": after_sbfe, "armed": armed}

    def _expected_prefix(self, operation_index):
        _require(self.runtime_baseline is not None,
                 "held-state baseline was not established")
        expected = list(self.runtime_baseline)
        for operation in self.validated["operations"][:operation_index + 1]:
            expected[operation["row"]] = operation["word"]
        return expected

    def finalize_armed_runtime_baseline(self):
        """Prove arming did not alter the sealed held image before OTP."""
        _require(self.phase == "armed",
                 "armed-plane finalization requires the newly armed phase")
        expected = list(self.runtime_baseline)
        capture = self.capture_pair_with_expected(
            expected, "same-session armed-plane baseline",
            allow_state_variant_suffix=False)
        # A structurally valid but different IFF suffix is not enough.  Until
        # the EN=1 transformation has a separately qualified semantic model,
        # changing even one held row is terminal and no OTP command may issue.
        armed_baseline = list(expected)
        gates = self.gate_snapshot(require_armed=True)
        if self.journal is not None:
            self.journal.append(
                "ARMED_IMAGE_BOUND", "armed-image-bound",
                complete_main_u32le_sha256=_packed_sha256(armed_baseline),
                compare_u32le_sha256=capture["compare_u32le_sha256"],
                suffix_u32le_sha256=
                capture["ff_iff_structure"]["suffix_u32le_sha256"],
                passes=2, rows_per_pass=MAIN_ROWS)
        self.phase = "armed-finalized"
        return {"capture": capture, "gates": gates,
                "complete_main_u32le_sha256":
                _packed_sha256(armed_baseline)}

    def _verify_programmed_prefix_targets(self, operation_index):
        checked = []
        operations = self.validated["operations"]
        for operation in operations[:operation_index + 1]:
            value = self.read_row(operation["row"], expected=operation["word"])
            _require(value == operation["word"],
                     "previously programmed row %d changed" % operation["row"])
            checked.append(operation["row"])
        if operation_index + 1 < len(operations):
            next_operation = operations[operation_index + 1]
            value = self.read_row(next_operation["row"], expected=0)
            _require(value == 0, "next target row is no longer blank")
            checked.append(next_operation["row"])
        return checked

    def program_one(self, operation):
        _require(self.phase == "armed-finalized",
                 "programming requires the finalized armed-plane image")
        _require(operation["index"] == self.next_operation_index and
                 operation is self.validated["operations"][self.next_operation_index],
                 "operation is not the next sealed one-shot operation")
        row = operation["row"]
        word = operation["word"]
        self._attest_ownership()
        self.gate_snapshot(require_armed=True)
        current = self.read_row(row, expected=0)
        _require(current == 0,
                 "target row %d is 0x%08x, not blank; no resume/retry" %
                 (row, current))
        self._active_operation = operation
        attempt_token = uuid.uuid4().hex
        _require(self.current_compare_image is not None,
                 "live compare image was not retained from preflight")
        if self.journal is not None:
            self.journal.append(
                "ATTEMPT_COMMITTED", "attempt-committed",
                operation_index=operation["index"], row=row,
                word="0x%08x" % word, expected_old="0x00000000",
                attempt_token=attempt_token,
                current_compare_u32le_sha256=
                _packed_sha256(self.current_compare_image))
        self.attempts_committed += 1
        self.command_uncertain = True
        try:
            self._write(FUSEADDR, row, "program-address")
            self._read_address_latch(row)
            self._write(FUSEWDATA, word, "program-word")
            self._read_address_latch(row)
            self.gate_snapshot(require_armed=True)
            if self.journal is not None:
                self.journal.append(
                    "COMMAND_BOUNDARY", "program-command-boundary",
                    row=row, word="0x%08x" % word,
                    attempt_token=attempt_token)
            # Re-attest immediately before the sole irreversible controller
            # write.  The immutable driver_override sentinel plus an unbound,
            # unchanged PCI-function binding is the userspace ownership
            # interlock; any inability to prove it fails closed here.
            self._attest_ownership()
            self._write(FUSECTRL, CMD_PROGRAM, "program-command")
            self.program_commands += 1
            if self.journal is not None:
                self.journal.append(
                    "COMMAND_ISSUED", "program-command-issued",
                    row=row, word="0x%08x" % word,
                    attempt_token=attempt_token)
            self._wait_idle(CMD_PROGRAM, row)
            if self.journal is not None:
                self.journal.append(
                    "COMMAND_IDLE", "program-command-idle",
                    row=row, word="0x%08x" % word,
                    attempt_token=attempt_token)
            got = self.read_row(row, expected=word)
            if got != word:
                raise VfiAppendUncertain(
                    "row %d readback 0x%08x != 0x%08x" % (row, got, word))
            expected = self._expected_prefix(operation["index"])
            checked_rows = self._verify_programmed_prefix_targets(
                operation["index"])
            _require(_packed_sha256(expected[:COMPARE_LAST + 1]) ==
                     operation["expected_prefix_compare_u32le_sha256"],
                     "internal prefix digest mismatch")
            if self.journal is not None:
                self.journal.append(
                    "ROW_VERIFIED", "row-verified", row=row,
                    word="0x%08x" % word,
                    attempt_token=attempt_token,
                    prefix_compare_u32le_sha256=
                    operation["expected_prefix_compare_u32le_sha256"])
            complete_capture = self.capture_pair_with_expected(
                expected,
                "post-program row %d complete expected prefix" % row,
            )
            if self.journal is not None:
                self.journal.append(
                    "PREFIX_IMAGE_VERIFIED", "prefix-image-verified",
                    row=row, word="0x%08x" % word,
                    attempt_token=attempt_token,
                    complete_main_u32le_sha256=
                    complete_capture["pass_u32le_sha256"],
                    prefix_compare_u32le_sha256=
                    complete_capture["compare_u32le_sha256"],
                    passes=2, rows_per_pass=MAIN_ROWS)
            self.verified_program_commands += 1
            self.command_uncertain = False
            self.current_compare_image[row] = word
            self.next_operation_index += 1
            return {"checked_rows": checked_rows,
                    "complete_capture": complete_capture,
                    "prefix_compare_u32le_sha256":
                    operation["expected_prefix_compare_u32le_sha256"]}
        finally:
            self._active_operation = None

    def run(self, execute=False, test_row_only=False, continue_after_test=None):
        commit_policy = self.validated.get("commit_policy")
        _require(commit_policy == _expected_commit_policy(
            self.validated["profile_id"]),
            "validated session commit policy is missing or mismatched")
        if test_row_only:
            _require(commit_policy["test_row_only_allowed"] is True,
                     "this profile forbids stopping at the first-row prefix")
        if execute:
            _require(self.runtime_baseline is not None,
                     "execution requires a certified native held-state MAIN/RIR capture")
        report = {
            "backend": BACKEND_ID,
            "write_strategy": self.write_strategy,
            "transaction_trace": transaction_trace(
                self.validated, self.write_strategy),
            "execute_requested": bool(execute),
            "bdf": self.validated["bdf"],
            "profile_id": self.validated["profile_id"],
            "program_commands": 0,
            "controller_read_commands": 0,
            "operational_mmio_writes": 0,
            "rows_verified": [],
            "commit_policy": copy.deepcopy(commit_policy),
        }
        preflight = self.preflight()
        report["preflight"] = preflight
        if not execute:
            report["status"] = "LIVE_PREFLIGHT_PASS_NO_OTP"
            report["next_gate"] = (
                "rerun with explicit execution and interactive confirmations "
                "while this exact held window remains valid")
            report["controller_read_commands"] = self.controller_read_commands
            report["operational_mmio_writes"] = self.operational_writes
            return report

        operations = self.validated["operations"]
        selected = operations[:1] if test_row_only else operations
        optional_pause = commit_policy[
            "optional_continuation_pause_after_first_row"]
        if len(selected) > 1 and optional_pause:
            _require(callable(continue_after_test),
                     "second-stage test-row confirmation callback is missing")
            if self.journal is not None:
                _require(isinstance(
                    continue_after_test, TtyContinuationAuthorizer) and
                    continue_after_test._seal is _AUTHORIZATION_SEAL,
                    "live continuation is not bound to the confirmed TTY session")
                _require(isinstance(self.authorization_nonce, str) and
                         re.fullmatch(r"[0-9a-f]{32}", self.authorization_nonce) and
                         continue_after_test.authorization_nonce ==
                         self.authorization_nonce,
                         "continuation authorizer nonce differs from the current "
                         "runtime authorization")
        self._attest_ownership()
        armed = self.sense_and_arm()
        report["armed_gates"] = armed
        armed_image = self.finalize_armed_runtime_baseline()
        report["armed_runtime_baseline"] = armed_image
        if not optional_pause and len(selected) > 1:
            _require(len(selected) >=
                     commit_policy["minimum_commit_group_operations"],
                     "sealed minimum commit group is incomplete")
            if self.journal is not None:
                self.journal.append(
                    "COMMIT_GROUP_BOUND", "commit-group-bound",
                    minimum_commit_group_operations=
                    commit_policy["minimum_commit_group_operations"],
                    partial_prefix_residual=
                    commit_policy["partial_prefix_residual"])
        continuation_declined = False
        for position, operation in enumerate(selected):
            comparison = self.program_one(operation)
            report["rows_verified"].append({
                "row": operation["row"],
                "word": operation["word_hex"],
                "prefix_comparison": comparison,
            })
            if position == 0 and len(selected) > 1 and optional_pause:
                approved = continue_after_test(operation) is True
                if self.journal is not None:
                    self.journal.append(
                        "CONTINUATION_AUTHORIZED" if approved else
                        "CONTINUATION_DECLINED",
                        "continuation-authorized" if approved else
                        "continuation-declined",
                        after_row=operation["row"],
                        authorization_nonce=getattr(
                            continue_after_test, "authorization_nonce", None),
                    )
                if not approved:
                    selected = operations[:1]
                    continuation_declined = True
                    break

        final_expected = self._expected_prefix(selected[-1]["index"])
        final_capture = self.capture_pair_with_expected(
            final_expected, "final same-session image")
        report["final_capture"] = final_capture
        report["final_gates"] = self.gate_snapshot(require_armed=True)
        if self.journal is not None:
            self.journal.append(
                "FINAL_IMAGE_VERIFIED", "final-image-verified",
                compare_u32le_sha256=final_capture["compare_u32le_sha256"],
                suffix_u32le_sha256=
                final_capture["ff_iff_structure"]["suffix_u32le_sha256"],
                verified_program_commands=self.verified_program_commands)
        report["status"] = (
            "TEST_ROW_COMPLETE_COLD_VERIFY_REQUIRED" if (
                test_row_only or continuation_declined)
            else "SESSION_ROWS_VERIFIED_COLD_VERIFY_REQUIRED")
        report["continuation_declined"] = continuation_declined
        self.phase = "final-verified"
        report["authoritative_next_step"] = (
            "park safely, remove complete slot/AUX power, cold boot, take two "
            "matching 512-row captures, regenerate a no-op plan, then perform "
            "feature-specific stability validation")
        report["program_commands"] = self.program_commands
        report["controller_read_commands"] = self.controller_read_commands
        report["operational_mmio_writes"] = self.operational_writes
        return report


def _journal_name_prefix(bdf):
    return bdf.replace(":", "-").replace(".", "-") + "-"


def assert_no_unresolved_journals(directory, validated, new_path):
    directory = Path(directory)
    _validate_state_directory(directory)
    prefix = _journal_name_prefix(validated["bdf"])
    new_resolved = Path(new_path).resolve()
    blockers = []
    journals = []
    closure_names = set()
    try:
        entries = sorted(os.scandir(str(directory)), key=lambda item: item.name)
    except OSError as exc:
        raise VfiAppendError("cannot scan OTP state directory: %s" % exc) from exc
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        path = directory / entry.name
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            blockers.append("%s (unsafe/non-regular BDF-prefixed entry)" % path)
            continue
        if _journal_name_pattern(validated["bdf"]).fullmatch(entry.name):
            journals.append(path)
            continue
        if (entry.name.endswith(".jsonl.cold.json") and
                _journal_name_pattern(validated["bdf"]).fullmatch(
                    entry.name[:-len(".cold.json")])):
            closure_names.add(entry.name)
            continue
        blockers.append("%s (unknown BDF-prefixed state-file grammar)" % path)
    for path in journals:
        if path.resolve() == new_resolved:
            continue
        try:
            # Historical sessions are authenticated against their own immutable
            # journal/manifest binding, never against the proposed new manifest.
            status = verify_journal(path)
        except (OSError, VfiAppendError) as exc:
            blockers.append("%s (invalid/unresolved: %s)" % (path, exc))
            continue
        cold_path = Path(str(path) + ".cold.json")
        if cold_path.name in closure_names:
            try:
                load_and_validate_cold_receipt(
                    cold_path, validated=None, journal_path=path)
                continue
            except (OSError, VfiAppendError) as exc:
                blockers.append(
                    "%s (invalid cold closure %s: %s)" %
                    (path, cold_path, exc))
                continue
        # REFUSED is the sole journal terminal that needs no cold closure.
        # Even a journal claiming COLD_PERSISTENCE_VERIFIED is not closure:
        # only the separately strict, native-capture-bound receipt can close it.
        if status["terminal_state"] != "REFUSED":
            blockers.append("%s (%s)" % (path, status["terminal_state"]))
    orphan_closures = closure_names.difference(
        path.name + ".cold.json" for path in journals)
    blockers.extend("%s (orphan cold closure)" % (directory / name)
                    for name in sorted(orphan_closures))
    _require(not blockers,
             "earlier unresolved exact-BDF journal(s): %s" % "; ".join(blockers))


def run_live(validated, sysfs_root="/sys/bus/pci/devices", execute=False,
             journal_path=None, test_row_only=False, continue_after_test=None,
             receipt_path=None, authorization=None,
             held_state_capture_path=None,
             bar_factory=MappedBar0, write_strategy=None):
    """Run a live preflight or an explicitly authorized append transaction."""
    if execute:
        strategy = validate_write_strategy(write_strategy)
    else:
        strategy = (validate_write_strategy(write_strategy)
                    if write_strategy is not None
                    else STRATEGY_EVIDENCE_GUARDED)
    receipt = None
    held_state = None
    if execute:
        if test_row_only:
            _require(validated["commit_policy"]["test_row_only_allowed"] is True,
                     "this profile forbids stopping at the first-row prefix")
        _require(journal_path is not None, "execution requires a new journal path")
        _require(receipt_path is not None,
                 "execution requires a separately reviewed receipt file")
        receipt = load_and_validate_receipt(receipt_path, validated)
        _require(isinstance(authorization, RuntimeAuthorization),
                 "runtime TTY authorization token is missing")
        _require(getattr(authorization, "_seal", None) is _AUTHORIZATION_SEAL and
                 isinstance(getattr(authorization, "nonce", None), str) and
                 re.fullmatch(r"[0-9a-f]{32}", authorization.nonce),
                 "runtime authorization token was not minted by this backend")
        _require(authorization.manifest_content_sha256 ==
                 validated["content_sha256"] and
                 authorization.receipt_content_sha256 ==
                 receipt["content_sha256"] and
                 authorization.bdf == validated["bdf"] and
                 authorization.profile_id == validated["profile_id"] and
                 getattr(authorization, "write_strategy", None) == strategy,
                 "runtime authorization token binding mismatch")
        if (not test_row_only and len(validated["operations"]) > 1 and
                validated["commit_policy"][
                    "optional_continuation_pause_after_first_row"]):
            _require(isinstance(continue_after_test,
                                TtyContinuationAuthorizer) and
                     continue_after_test._seal is _AUTHORIZATION_SEAL and
                     continue_after_test.authorization_nonce ==
                     authorization.nonce,
                     "continuation authorizer is not bound to the current "
                     "runtime authorization")
        _require(held_state_capture_path is not None,
                 "execution requires a native DSOV=0 held-state capture")
        _require(Path(sysfs_root).resolve() ==
                 Path("/sys/bus/pci/devices").resolve(),
                 "execution requires the canonical PCI sysfs registry")
        held_path = Path(held_state_capture_path)
        _require(held_path.is_absolute() and held_path.is_file() and
                 not held_path.is_symlink(),
                 "held-state capture path must be an absolute regular file")
        try:
            age = time.time() - held_path.stat().st_mtime
        except OSError as exc:
            raise VfiAppendError(
                "cannot stat held-state capture: %s" % exc) from exc
        _require(-5.0 <= age <= HELD_CAPTURE_MAX_AGE_SECONDS,
                 "held-state capture is stale or has a future timestamp")
        held_state = validate_held_state_capture(held_path, validated)
    state_root = Path(CANONICAL_STATE_DIR)
    _require(state_root.is_absolute(), "state directory must be absolute")
    if str(state_root) == "/var/lib/170hx-xray/otp":
        _require(os.geteuid() == 0,
                 "canonical OTP state directory requires root execution")
    _validate_state_directory(state_root)
    journal = None
    session = None
    boot_id = _read_boot_id(CANONICAL_BOOT_ID_PATH)
    backend_sha256 = _sha256_file(__file__)
    with DeviceLock(state_root, validated["bdf"]):
        live = inspect_live_identity(
            sysfs_root, validated["bdf"], validated["identity"],
            require_ownership=execute)
        if execute:
            _require(live["identity"]["device"] == EXECUTION_DEVICE_ID,
                     "irreversible backend is qualified only for live 20c2")
        if execute:
            journal_path_obj = Path(journal_path)
            _require(journal_path_obj.parent.resolve() == state_root.resolve(),
                     "journal must be created directly in --state-dir")
            _require_journal_basename(journal_path_obj, validated["bdf"])
            assert_no_unresolved_journals(
                state_root, validated, journal_path_obj)
            journal = DurableJournal(
                journal_path_obj, validated, boot_id, backend_sha256,
                write_strategy=strategy)
        try:
            if journal is not None:
                journal.append(
                    "CREATED", "session-created", backend=BACKEND_ID,
                    write_strategy=strategy,
                    live_identity=live["identity"], compare_range=[0, 487],
                    operation_count=len(validated["operations"]),
                    review_receipt_content_sha256=receipt["content_sha256"],
                    held_state_capture_path=held_state["path"],
                    held_state_capture_file_sha256=held_state["file_sha256"],
                    held_main_u32le_sha256=
                    _packed_sha256(held_state["runtime_baseline"]),
                    held_suffix_u32le_sha256=
                    held_state["held_suffix_u32le_sha256"],
                    held_rir_u32le_sha256=_packed_sha256(held_state["rir"]))
            with bar_factory(live["resource0"]) as bar:
                rebound = inspect_live_identity(
                    sysfs_root, validated["bdf"], validated["identity"],
                    require_ownership=execute)
                _require(rebound["identity"] == live["identity"] and
                         rebound["resource0"] == live["resource0"] and
                         rebound["bar0_size"] == live["bar0_size"] and
                         rebound["resolved_device_path"] ==
                         live["resolved_device_path"] and
                         rebound["device_inode"] == live["device_inode"] and
                         rebound["driver_override"] ==
                         live["driver_override"],
                         "identity/resource changed across BAR0 open")

                def ownership_check():
                    current = inspect_live_identity(
                        sysfs_root, validated["bdf"], validated["identity"],
                        require_ownership=True)
                    _require(current["identity"] == live["identity"] and
                             current["resource0"] == live["resource0"] and
                             current["bar0_size"] == live["bar0_size"] and
                             current["resolved_device_path"] ==
                             live["resolved_device_path"] and
                             current["device_inode"] == live["device_inode"] and
                             current["driver_override"] ==
                             DRIVER_OVERRIDE_SENTINEL,
                             "PCI ownership/resource binding changed")
                    return True
                if journal is not None:
                    journal.append(
                        "IDENTITY_BOUND", "identity-bound",
                        live_identity=rebound["identity"],
                        resolved_device_path=rebound["resolved_device_path"],
                        bar0_size=rebound["bar0_size"],
                        pci_command=rebound["pci_command"])
                    journal.append(
                        "RUNTIME_AUTHORIZED", "runtime-authorized",
                        authorization_nonce=authorization.nonce,
                        review_receipt_content_sha256=
                        receipt["content_sha256"])
                session = VfiAppendSession(
                    bar, validated, journal=journal,
                    ownership_check=ownership_check if execute else None,
                    authorization_nonce=(authorization.nonce
                                         if authorization is not None else None),
                    held_baseline=(held_state["runtime_baseline"]
                                   if held_state is not None else None),
                    write_strategy=strategy,
                )
                report = session.run(
                    execute=execute, test_row_only=test_row_only,
                    continue_after_test=continue_after_test)
                post = inspect_live_identity(
                    sysfs_root, validated["bdf"], validated["identity"],
                    require_ownership=execute)
                _require(post["identity"] == live["identity"] and
                         post["resource0"] == live["resource0"] and
                         post["device_inode"] == live["device_inode"] and
                         post["driver_override"] == live["driver_override"],
                         "identity/resource changed before terminal seal")
                if journal is not None:
                    terminal = (
                        "TEST_ROW_PENDING_COLD_VERIFY" if
                        report["status"].startswith("TEST_ROW") else
                        "PENDING_COLD_VERIFY"
                    )
                    journal.append(
                        terminal, "session-terminal",
                        status=report["status"],
                        rows_verified=report["rows_verified"],
                        cold_verify_required=True)
                report["live_binding"] = live
                report["post_binding"] = post
                report["host_boot_id"] = boot_id
                report["manifest"] = {
                    "path": validated["path"],
                    "file_sha256": validated["file_sha256"],
                    "content_sha256": validated["content_sha256"],
                }
                if receipt is not None:
                    report["review_receipt"] = {
                        "path": receipt["path"],
                        "file_sha256": receipt["file_sha256"],
                        "content_sha256": receipt["content_sha256"],
                    }
                if held_state is not None:
                    report["held_state_capture"] = {
                        "path": held_state["path"],
                        "file_sha256": held_state["file_sha256"],
                        "main_u32le_sha256":
                        _packed_sha256(held_state["runtime_baseline"]),
                        "rir_u32le_sha256": _packed_sha256(held_state["rir"]),
                        "held_suffix_u32le_sha256":
                        held_state["held_suffix_u32le_sha256"],
                    }
                report["poisoned"] = session.poisoned
                report["command_uncertain"] = session.command_uncertain
                return report
        except Exception as exc:
            if journal is not None:
                poisoned = bool(session and session.poisoned)
                uncertain = bool(session and session.command_uncertain)
                attempts = session.attempts_committed if session else 0
                programs = session.program_commands if session else 0
                verified = session.verified_program_commands if session else 0
                setup_started = bool(session and session.setup_started)
                if (poisoned or uncertain or attempts > verified or
                        setup_started):
                    terminal = "QUARANTINED"
                    rule = (
                        "volatile setup or an uncertain attempt occurred; no retry, "
                        "complete power removal and cold capture are required"
                    )
                elif programs > 0:
                    terminal = "PENDING_COLD_VERIFY"
                    rule = "verified OTP rows exist; cold verification is still mandatory"
                else:
                    terminal = "REFUSED"
                    rule = "no OTP program command was issued"
                try:
                    journal.append(
                        terminal, "session-terminal", reason=str(exc),
                        poison=poisoned, command_uncertain=uncertain,
                        attempts_committed=attempts,
                        program_commands=programs,
                        verified_program_commands=verified, rule=rule)
                except Exception as journal_exc:
                    raise VfiAppendUncertain(
                        "%s; terminal journal write also failed: %s" %
                        (exc, journal_exc)) from exc
            raise
        finally:
            if journal is not None:
                journal.close()


def confirmation_contract(
        validated, write_strategy=STRATEGY_EVIDENCE_GUARDED):
    """Values the CLI must require verbatim before it calls ``run_live``."""
    strategy = validate_write_strategy(write_strategy)
    return {
        "answer": "yes",
        "manifest_file_sha256": validated["file_sha256"],
        "manifest_content_sha256": validated["content_sha256"],
        "bdf": validated["bdf"],
        "profile_id": validated["profile_id"],
        "write_strategy": strategy,
        "risk_phrase": (
            "BURN GA100 VFI FF APPEND %d ROWS USING %s; "
            "USERSPACE OWNERSHIP RACE UNRESOLVED" %
            (len(validated["operations"]), strategy)
        ),
    }


def test_row_continuation_phrase(operation):
    return "CONTINUE AFTER VERIFIED TEST ROW %d 0x%08x" % (
        operation["row"], operation["word"])
