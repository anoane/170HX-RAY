#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compile and review GA100 VFI blank-FF append manifests.

This compiler emits the distinct sealed-v2 schema consumed only by the
``ga100-vfi-ff-append-v1`` XRAY backend. Compilation performs no hardware
access. Retired RAY artifacts are accepted only through explicit input
normalization and are never emitted.
"""

from __future__ import print_function

import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid


TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parent
SPEC_PATH = TOOL_DIR / "170hx_otp_executor_spec.py"
_PACKAGE_BACKEND = ROOT / "otp_vfi_append.py"
BACKEND_PATH = (_PACKAGE_BACKEND if _PACKAGE_BACKEND.is_file()
                else ROOT / "170HX-XRAY" / "otp_vfi_append.py")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_SPEC = _load_module("xray_otp_inert_spec_for_v2", SPEC_PATH)
BACKEND = _load_module("xray_vfi_append_for_compiler", BACKEND_PATH)

TOOL_VERSION = "0.5.0"
MANIFEST_SCHEMA = BACKEND.MANIFEST_SCHEMA
RECEIPT_SCHEMA = BACKEND.RECEIPT_SCHEMA
BACKEND_ID = BACKEND.BACKEND_ID
MIN_BLANK_RESERVE = 16
CORE_SUPERSET_REGISTER_PROFILES = frozenset((
    "core", "fuse-reference", "shadow-full", "hbm-static",
    "hbm-i1500-latches", "all-safe",
))

SPECIAL_FIELD_COORDINATES = {
    "OPT_DISABLE_GEN3_SPEED": (2, 39, 9, 1),
    # Preserved only to replay and falsify the historical bit-1 hypothesis;
    # it is not accepted as the Gen3-disable target.
    "OPT_DISABLE_GEN3_SPEED_CANDIDATE_BIT1": (2, 39, 1, 1),
    "OPT_DISABLE_GEN3_SPEED_CANDIDATE_BIT9": (2, 39, 9, 1),
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": (2, 47, 3, 1),
    "OPT_PCIE_BOOT_GEN23_DISABLE": (2, 49, 6, 1),
    "OPT_PCIE_BOOT_GEN3_DISABLE": (2, 49, 7, 1),
}

STAGE_RANK = {
    "memory-prerequisites": 10,
    "memory-and-shared-prerequisites": 10,
    "rop-l2-dependency": 25,
    "compute-prerequisites": 30,
    # The combined topology profile deliberately establishes both memory and
    # compute child topology before either parent activation boundary.  Keep
    # memory activation after compute prerequisites so its exact four-stage
    # policy can also be checked by the generic monotonic-rank guard.
    "memory-activation": 35,
    "compute-activation": 40,
    "nvlink-global-group": 50,
    "sys-pipe-experimental": 50,
    "mig-system-pipe-to-defect": 50,
    "nvdec-to-defect": 51,
    "nvjpg-ofa-inseparable-pair": 52,
    "pcie-isolated-gates": 60,
    "pcie-a100-exact-block46-dstoro-order": 70,
    "pcie-a100-exact-block47-dstoro-order": 70,
    "pcie-a100-exact-block47": 70,
    "pcie-a100-exact-block46": 70,
    "identity-last": 90,
}

# Execution-eligible v2 is intentionally smaller than the candidate-plan
# language.  These are the only profile/field models the compiler will accept.
# Coordinates are (chain, block, bit-in-block, width), least-significant piece
# first.  Keeping this table independent of candidate JSON prevents a resealed
# candidate from inventing a field name or moving a known field to another bit.
APPROVED_FIELD_MODELS = {
    "OPT_FBIO_DISABLE": (
        ((1, 5, 3, 13), (1, 6, 0, 11)), "OPT_FBIO_DISABLE",
        None, "FBIO_DEFECTIVE", "disable_mask"),
    "OPT_FBPA_DISABLE": (
        ((1, 6, 11, 5), (1, 7, 0, 16), (1, 8, 0, 3)),
        "OPT_FBPA_DISABLE", None, "FBPA_DEFECTIVE", "disable_mask"),
    "OPT_FBP_DISABLE": (
        ((1, 19, 3, 12),), "OPT_FBP_DISABLE",
        None, "FBP_DEFECTIVE", "disable_mask"),
    "OPT_ROP_L2_DISABLE": (
        ((1, 19, 15, 1), (1, 20, 0, 16), (1, 21, 0, 7)),
        "OPT_ROP_L2_DISABLE", None, "ROP_L2_DEFECTIVE", "disable_mask"),
    "OPT_GPC_DISABLE": (
        ((1, 8, 3, 8),), "OPT_GPC_DISABLE",
        None, "GPC_DEFECTIVE", "disable_mask"),
    "OPT_NVDEC_DISABLE": (
        ((1, 8, 11, 5),), "OPT_NVDEC_DISABLE",
        None, "OPT_NVDEC_DEFECTIVE", "disable_mask"),
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": (
        ((1, 9, 2, 1),), None,
        None, None, "paired_disable_candidate"),
    "OPT_TPC_GPC0_DISABLE": (
        ((1, 11, 11, 5), (1, 12, 0, 3)), "OPT_TPC_GPC0_DISABLE",
        None, "TPC_GPC0_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC1_DISABLE": (
        ((1, 12, 3, 8),), "OPT_TPC_GPC1_DISABLE",
        None, "TPC_GPC1_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC2_DISABLE": (
        ((1, 12, 11, 5), (1, 13, 0, 3)), "OPT_TPC_GPC2_DISABLE",
        None, "TPC_GPC2_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC3_DISABLE": (
        ((1, 13, 3, 8),), "OPT_TPC_GPC3_DISABLE",
        None, "TPC_GPC3_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC4_DISABLE": (
        ((1, 13, 11, 5), (1, 14, 0, 3)), "OPT_TPC_GPC4_DISABLE",
        None, "TPC_GPC4_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC5_DISABLE": (
        ((1, 14, 3, 8),), "OPT_TPC_GPC5_DISABLE",
        None, "TPC_GPC5_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC6_DISABLE": (
        ((1, 14, 11, 5), (1, 15, 0, 3)), "OPT_TPC_GPC6_DISABLE",
        None, "TPC_GPC6_DEFECTIVE", "disable_mask"),
    "OPT_TPC_GPC7_DISABLE": (
        ((1, 15, 3, 8),), "OPT_TPC_GPC7_DISABLE",
        None, "TPC_GPC7_DEFECTIVE", "disable_mask"),
    "OPT_PES_DISABLE": (
        ((1, 15, 11, 5), (1, 16, 0, 16), (1, 17, 0, 3)),
        "OPT_PES_DISABLE", None, "PES_DEFECTIVE", "disable_mask"),
    # Chain-1 bits 368..370 are retained in the offline candidate model only.
    # They are global IOCTRL-group disables, not a proved physical mapping of
    # the 12-bit OPT_PERLINK_DISABLE field, and therefore are deliberately
    # absent from this execution allowlist.
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": (
        ((1, 23, 3, 1),), None,
        None, None, "paired_disable_candidate"),
    "OPT_SYS_PIPE_DISABLE": (
        ((1, 23, 4, 8),), "OPT_SYS_PIPE_DISABLE",
        None, "SYS_PIPE_DEFECTIVE", "disable_mask"),
    "OPT_PCIE_DEVIDA": (
        ((2, 37, 1, 15), (2, 38, 0, 1)), "OPT_PCIE_DEVIDA",
        None, None, "identity"),
    "OPT_PCIE_DEVIDB": (
        ((2, 38, 1, 15), (2, 39, 0, 1)), "OPT_PCIE_DEVIDB",
        None, None, "identity"),
    "OPT_DISABLE_GEN3_SPEED": (
        ((2, 39, 9, 1),), "OPT_DISABLE_GEN3_SPEED",
        0, None, "disable_bit"),
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": (
        ((2, 47, 3, 1),), "OPT_PCIE_MAGIC_BITS_D",
        25, None, "disable_bit"),
    "OPT_PCIE_BOOT_GEN23_DISABLE": (
        ((2, 49, 6, 1),), "OPT_PCIE_BOOT_GEN23_DISABLE",
        0, None, "disable_bit"),
    "OPT_PCIE_BOOT_GEN3_DISABLE": (
        ((2, 49, 7, 1),), "OPT_PCIE_BOOT_GEN3_DISABLE",
        0, None, "disable_bit"),
}

# Preserve this projection for exact-current evidence validation only.  Chain-1
# bits 368..370 are global IOCTRL-group disables, not the physical map of the
# 12-bit per-link field, so this model is categorically non-executable.
NON_EXECUTABLE_CROSSCHECK_FIELD_MODELS = {
    "OPT_NVLINK_DISABLE": (
        ((1, 23, 0, 3),), "OPT_NVLINK_DISABLE",
        None, "NVLINK_DEFECTIVE", "disable_mask"),
}
CROSSCHECK_FIELD_MODELS = dict(APPROVED_FIELD_MODELS)
CROSSCHECK_FIELD_MODELS.update(NON_EXECUTABLE_CROSSCHECK_FIELD_MODELS)

APPROVED_PROFILES = {
    "specimen-80gb-hbm": {
        "fields": frozenset((
            "OPT_FBIO_DISABLE", "OPT_FBPA_DISABLE", "OPT_FBP_DISABLE",
            "OPT_ROP_L2_DISABLE",
        )),
        "stages": ("memory-prerequisites", "memory-activation"),
    },
    "specimen-more-sm": {
        "fields": frozenset((
            "OPT_GPC_DISABLE", "OPT_PES_DISABLE", "OPT_ROP_L2_DISABLE",
            "OPT_TPC_GPC0_DISABLE", "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE", "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE", "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE", "OPT_TPC_GPC7_DISABLE",
        )),
        "stages": (
            "rop-l2-dependency", "compute-prerequisites",
            "compute-activation",
        ),
    },
    "xray-common-permanent-topology": {
        "fields": frozenset((
            "OPT_FBIO_DISABLE", "OPT_FBPA_DISABLE", "OPT_FBP_DISABLE",
            "OPT_ROP_L2_DISABLE", "OPT_GPC_DISABLE", "OPT_PES_DISABLE",
            "OPT_TPC_GPC0_DISABLE", "OPT_TPC_GPC1_DISABLE",
            "OPT_TPC_GPC2_DISABLE", "OPT_TPC_GPC3_DISABLE",
            "OPT_TPC_GPC4_DISABLE", "OPT_TPC_GPC5_DISABLE",
            "OPT_TPC_GPC6_DISABLE", "OPT_TPC_GPC7_DISABLE",
        )),
        "stages": (
            "memory-and-shared-prerequisites", "compute-prerequisites",
            "memory-activation", "compute-activation",
        ),
    },
    "specimen-pcie-gen4-isolated": {
        "fields": frozenset((
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE", "OPT_PCIE_BOOT_GEN3_DISABLE",
        )),
        "stages": ("pcie-isolated-gates",),
    },
    "specimen-a100-20b5-identity": {
        "fields": frozenset(("OPT_PCIE_DEVIDA", "OPT_PCIE_DEVIDB")),
        "stages": ("identity-last",),
    },
    "specimen-sys-pipe-to-defect-experimental": {
        "fields": frozenset(("OPT_SYS_PIPE_DISABLE",)),
        "stages": ("sys-pipe-experimental",),
    },
    "specimen-media-mig-to-defect-experimental": {
        "fields": frozenset((
            "OPT_SYS_PIPE_DISABLE", "OPT_NVDEC_DISABLE",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
            "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
        )),
        "stages": (
            "mig-system-pipe-to-defect", "nvdec-to-defect",
            "nvjpg-ofa-inseparable-pair",
        ),
    },
}

PAIRED_MEDIA_FIELDS = frozenset((
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
))

# Human-facing planner aliases are accepted only at the compiler boundary.
# The sealed manifest always carries the canonical profile identifier so the
# backend policy table and review receipt have one unambiguous spelling.
PROFILE_ALIASES = {
    "specimen-hbm-to-defect-ceiling": "specimen-80gb-hbm",
    "ray-common-permanent-topology": "xray-common-permanent-topology",
    "ray-best-evidence-a100-conversion":
        "xray-best-evidence-a100-conversion",
}

IDENTITY_PREFIX_RISK = (
    "DEVIDA/DEVIDB are temporarily inconsistent; boot/firmware behavior is not qualified"
)
PAIRED_MEDIA_PREFIX_RISK = (
    "NVJPG/OFA candidate pair is temporarily asymmetric; individual feature "
    "attribution and boot behavior are not qualified"
)
DEFAULT_COMMIT_POLICY = {
    "test_row_only_allowed": True,
    "optional_continuation_pause_after_first_row": True,
    "minimum_commit_group_operations": 1,
    "partial_prefix_residual": (
        "a crash can leave a verified append-only prefix; cold capture and replan "
        "are mandatory before any continuation"
    ),
}
IDENTITY_COMMIT_POLICY = {
    "test_row_only_allowed": False,
    "optional_continuation_pause_after_first_row": False,
    "minimum_commit_group_operations": 2,
    "partial_prefix_residual": (
        "DEVIDA/DEVIDB may be temporarily inconsistent after operation 1; "
        "crash/partial-prefix boot and firmware behavior is unqualified"
    ),
}
PAIRED_MEDIA_PARTIAL_PREFIX_RESIDUAL = (
    "NVJPG/OFA bits 146 and 371 may be temporarily asymmetric after the first "
    "pair operation; individual attribution and partial-prefix boot behavior are unqualified"
)
PAIRED_MEDIA_COMMIT_POLICY = {
    "test_row_only_allowed": False,
    "optional_continuation_pause_after_first_row": False,
    "minimum_commit_group_operations": 2,
    "partial_prefix_residual": PAIRED_MEDIA_PARTIAL_PREFIX_RESIDUAL,
}

CONTEXT_REGISTER_TO_TRANSACTION_KEY = {
    "FUSECTRL": "fusectrl",
    "CRC_STATUS": "crc_status",
    "DEBUGCTRL": "debugctrl",
    "EN_SW_OVERRIDE": "en_sw_override",
    "IFF_RECORD": "iff_record",
    "IFF_SW_FUSING": "iff_sw_fusing",
    "ENABLE_FUSE_PROGRAM_STATUS": "enable_fuse_program_status",
    "DISABLE_FUSE_PROGRAM_STATUS": "disable_fuse_program_status",
    "DISABLE_SW_OVERRIDE_STATUS": "disable_sw_override_status",
    "BYPASS_FUSES_STATUS": "bypass_fuses_status",
    "DEBUGCTRL_PRIV_LEVEL_MASK": "debugctrl_priv_mask",
    "FUSEWDATA_PRIV_LEVEL_MASK": "fusewdata_priv_mask",
    "FUSECTRL_PRIV_LEVEL_MASK": "fusectrl_priv_mask",
    "OPT_PRIV_LEVEL_MASK": "opt_priv_mask",
}


class CompileError(RuntimeError):
    pass


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def reject_json_constant(value):
    raise CompileError("non-finite JSON constant is forbidden: %s" % value)


def strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", reject_json_constant)
    return _JSON_LOADS(*args, **kwargs)


def strict_json_dumps(*args, **kwargs):
    kwargs.setdefault("allow_nan", False)
    return _JSON_DUMPS(*args, **kwargs)


def require(condition, message):
    if not condition:
        raise CompileError(message)


def canonical_bytes(value):
    return strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def packed_sha256(words):
    return hashlib.sha256(
        b"".join(struct.pack("<I", value) for value in words)
    ).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def atomic_write_json(path, document):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + ".tmp-" + uuid.uuid4().hex)
    payload = strict_json_dumps(document, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(str(temporary), str(destination))
        temporary.unlink()
        parent_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _read_image(path, count, label):
    data = Path(path).read_bytes()
    require(len(data) == count * 4,
            "%s must be exactly %d bytes" % (label, count * 4))
    return list(struct.unpack("<%dI" % count, data))


def _validate_corrected_pcie(operations):
    for operation in operations:
        word = int(operation["proposed_physical_word"], 0)
        require(word != 0x058009C2,
                "superseded 0x058009c2 Gen3 record is forbidden")
        if operation["chain"] == 2 and operation["block"] == 39:
            old = int(operation["logical_block_old"], 0)
            new = int(operation["logical_block_new"], 0)
            fields = operation.get("fields_intentionally_changed", [])
            if "OPT_DISABLE_GEN3_SPEED" in fields:
                require((old ^ new) == (1 << 9),
                        "Gen3 operation must change block-39 bit 9 only")
                require(old & (1 << 9) and not (new & (1 << 9)),
                        "Gen3 operation must clear block-39 bit 9")
                require((old & (1 << 1)) == (new & (1 << 1)),
                        "Gen3 operation must preserve block-39 bit 1")


def _u32(value, label):
    try:
        result = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise CompileError("%s is not an integer" % label) from exc
    require(not isinstance(value, bool) and 0 <= result <= 0xFFFFFFFF,
            "%s is outside u32" % label)
    return result


def _resolve_bound_file(candidate_path, named_path):
    path = Path(str(named_path))
    if path.is_absolute():
        choices = [path]
    else:
        # A candidate is commonly stored beside ``research-evidence/`` while
        # its sealed binding is workspace-relative (for example,
        # ``research-evidence/.../capture.json``).  Resolve from stable file
        # locations only: the packaged tool root, the candidate directory,
        # and each of that directory's ancestors.  Never make acceptance
        # depend on the caller's cwd.
        candidate_parent = Path(candidate_path).resolve().parent
        choices = [ROOT / path, candidate_parent / path]
        choices.extend(parent / path for parent in ROOT.parents)
        choices.extend(parent / path
                       for parent in candidate_parent.parents)
    unique_choices = []
    seen = set()
    for choice in choices:
        key = str(choice)
        if key not in seen:
            seen.add(key)
            unique_choices.append(choice)
    for choice in unique_choices:
        if choice.is_file() and not choice.is_symlink():
            return choice.resolve()
    raise CompileError("bound exact-current capture artifact is absent: %s" % path)


def _strict_json_file(path, label, expected_sha256=None):
    try:
        payload = Path(path).read_bytes()
        if expected_sha256 is not None:
            require(hashlib.sha256(payload).hexdigest() == expected_sha256,
                    "%s bytes do not match the sealed digest" % label)
        return strict_json_loads(
            payload.decode("utf-8"),
            object_pairs_hook=BACKEND._strict_object,
            parse_constant=BACKEND._reject_json_constant,
        )
    except CompileError:
        raise
    except (OSError, UnicodeError, ValueError, BACKEND.VfiAppendError) as exc:
        raise CompileError("%s is not strict JSON: %s" % (label, exc)) from exc


def _normalize_legacy_capture(value):
    if isinstance(value, list):
        return [_normalize_legacy_capture(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = {
                "ray_helper_identity": "xray_helper_identity",
            }.get(key, key)
            if normalized_key in result:
                raise CompileError(
                    "legacy normalization key collision at %r" % normalized_key
                )
            result[normalized_key] = _normalize_legacy_capture(item)
        return result
    if isinstance(value, str):
        return {
            "170hx-ray-v1": "170hx-xray-v1",
            "170hx-ray-helper-allowlisted-read32":
                "170hx-xray-helper-allowlisted-read32",
        }.get(value, value)
    return value


def _word_array(value, count, label):
    require(isinstance(value, list) and len(value) == count,
            "%s must contain exactly %d words" % (label, count))
    return [_u32(word, "%s[%d]" % (label, index))
            for index, word in enumerate(value)]


def _capture_register_value(capture_card, name):
    registers = capture_card.get("registers")
    require(isinstance(registers, dict) and name in registers,
            "native capture lacks register %s" % name)
    record = registers[name]
    require(isinstance(record, dict) and record.get("status") == "ok" and
            record.get("stability_policy") == "required",
            "native capture register %s is unavailable or not required-stable" % name)
    value = _u32(record.get("raw"), "capture register %s" % name)
    raw_passes = capture_card.get("raw_passes")
    require(isinstance(raw_passes, list) and len(raw_passes) >= 2,
            "native capture has fewer than two ordinary-register passes")
    for pass_index, raw_pass in enumerate(raw_passes):
        pass_registers = raw_pass.get("registers") if isinstance(raw_pass, dict) else None
        pass_record = pass_registers.get(name) if isinstance(pass_registers, dict) else None
        require(isinstance(pass_record, dict) and
                pass_record.get("status") == "ok" and
                _u32(pass_record.get("value"),
                     "capture pass %d register %s" % (pass_index, name)) == value,
                "native capture register %s changed or is missing in pass %d" %
                (name, pass_index))
    return value


def _validate_capture_transaction(transaction, kind, count, words,
                                  helper_identity):
    label = "native %s transaction" % kind
    digest = packed_sha256(words)
    require(isinstance(transaction, dict), "%s is absent" % label)
    required_true = (
        "protocol_valid", "certified_by_helper", "kernel_reported_coherent",
        "coherent", "counts_complete", "address_restored", "debug_restored",
    )
    require(all(transaction.get(key) is True for key in required_true),
            "%s is not certified/coherent/restored" % label)
    expected_flags = "0x00000006" if kind == "main_ff" else "0x00000004"
    expected_response_kind = 1 if kind == "main_ff" else 2
    expected_writes = count * 4 + (1 if kind == "main_ff" else 3)
    require(transaction.get("kind") == kind and
            transaction.get("status") == 0 and
            transaction.get("status_text") == "ok" and
            transaction.get("transport_error") is None and
            transaction.get("protocol_errors") == [] and
            transaction.get("failed_pass") is None and
            transaction.get("failed_row") is None and
            transaction.get("recovered_from_last") is False and
            transaction.get("driver_bound_at_entry") is False and
            transaction.get("otp_commands") == 0 and
            transaction.get("row_count") == count and
            transaction.get("requested_passes") == 2 and
            transaction.get("completed_passes") == 2 and
            transaction.get("cmd_read_commands_issued") == count * 2 and
            transaction.get("completed_data_reads_lower_bound") == count * 2 and
            transaction.get("operational_writes_issued") == expected_writes and
            transaction.get("helper_abi") in
            BACKEND.SUPPORTED_NATIVE_HELPER_ABIS and
            transaction.get("helper_struct_size") == 4320 and
            transaction.get("flags") == expected_flags and
            transaction.get("response_kind") == expected_response_kind and
            transaction.get("identity") == helper_identity,
            "%s metadata/counter/identity contract failed" % label)
    passes = transaction.get("passes")
    require(isinstance(passes, list) and len(passes) == 2,
            "%s does not contain two passes" % label)
    parsed_passes = [
        _word_array(pass_words, count, "%s pass %d" % (label, index))
        for index, pass_words in enumerate(passes)
    ]
    require(parsed_passes == [words, words] and
            transaction.get("pass_hashes") == [digest, digest],
            "%s pass bytes/hashes do not equal the candidate baseline" % label)
    entry = transaction.get("entry")
    exit_state = transaction.get("exit")
    require(isinstance(entry, dict) and entry == exit_state,
            "%s did not restore its exact entry state" % label)
    require(_u32(entry.get("fusectrl"), "%s entry FUSECTRL" % label) ==
            BACKEND.IDLE_VALUE and
            _u32(entry.get("crc_status"), "%s entry CRC" % label) == 0x55 and
            _u32(entry.get("debugctrl"), "%s entry DEBUGCTRL" % label) == 0 and
            _u32(entry.get("en_sw_override"), "%s entry EN_SW" % label) == 0 and
            _u32(entry.get("iff_sw_fusing"), "%s entry SWIFF" % label) == 0,
            "%s entry state is not the required locked-controller baseline" % label)
    return entry


def _validate_native_capture(capture_path, capture_sha256, bdf, baseline, rir):
    """Independently validate the native artifact named by the candidate.

    The candidate's synthesized attestations are deliberately not inputs here.
    Every row, identity, transaction invariant and ordinary-register value is
    recovered from the named capture itself.
    """
    document = _normalize_legacy_capture(_strict_json_file(
        capture_path, "bound native capture", capture_sha256))
    require(isinstance(document, dict) and
            document.get("schema") == "170hx-xray-v1" and
            document.get("command") == "capture" and
            document.get("status") == "captured",
            "bound file is not a successful native XRAY capture")
    tool = document.get("tool")
    require(isinstance(tool, dict) and
            tool.get("name") in ("170HX-RAY", "170HX-XRAY") and
            isinstance(tool.get("sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", tool.get("sha256", "")) and
            tool.get("sha256") != "0" * 64,
            "native capture tool identity is absent or invalid")
    access = document.get("access")
    safety = document.get("safety")
    require(isinstance(access, dict) and
            access.get("canonical_requested_access") == "semantic-read" and
            access.get("effective_access") == "semantic-read" and
            access.get("owner_busy_declared") is False and
            access.get("fallback_reasons") == [] and
            access.get("counts_complete") is True and
            access.get("otp_program_commands") == 0,
            "native capture root access is not exact Level-3 semantic-read")
    require(isinstance(safety, dict) and
            safety.get("otp_execution_supported") is False and
            safety.get("has_live_write_primitive") is False and
            safety.get("counts_complete") is True,
            "native capture root safety counters are inconsistent")
    cards = document.get("cards")
    require(isinstance(cards, list), "native capture cards are absent")
    matches = [item for item in cards if isinstance(item, dict) and
               str(item.get("card", {}).get("domain_bdf", "")).lower() == bdf]
    require(len(matches) == 1,
            "native capture must contain exactly one matching BDF")
    capture_card = matches[0]
    card_identity = capture_card.get("card")
    helper_identity = (card_identity.get("xray_helper_identity")
                       if isinstance(card_identity, dict) else None)
    require(isinstance(card_identity, dict) and
            str(card_identity.get("domain_bdf", "")).lower() == bdf and
            _u32(card_identity.get("vendor"), "capture vendor") == 0x10DE and
            _u32(card_identity.get("device"), "capture device") ==
            BACKEND.EXECUTION_DEVICE_ID and
            card_identity.get("driver") is None and
            isinstance(helper_identity, dict) and
            str(helper_identity.get("domain_bdf", "")).lower() == bdf and
            _u32(helper_identity.get("vendor"), "helper vendor") == 0x10DE and
            _u32(helper_identity.get("device"), "helper device") ==
            BACKEND.EXECUTION_DEVICE_ID,
            "native capture card/helper identity is not exact unbound 20c2")

    card_access = capture_card.get("access")
    ordinary = capture_card.get("capture")
    require(isinstance(card_access, dict) and
            card_access.get("canonical_requested_access") == "semantic-read" and
            card_access.get("effective_access") == "semantic-read" and
            card_access.get("controller_transaction_attempted") is True and
            card_access.get("controller_transaction_error") is None and
            card_access.get("fallback_reasons") == [] and
            card_access.get("counts_complete") is True,
            "native capture card did not complete exact semantic-read")
    coherence = ordinary.get("coherence") if isinstance(ordinary, dict) else None
    require(isinstance(ordinary, dict) and ordinary.get("passes", 0) >= 2 and
            ordinary.get("register_profile") in
            CORE_SUPERSET_REGISTER_PROFILES and
            isinstance(coherence, dict) and coherence.get("coherent") is True and
            all(coherence.get(key) == [] for key in (
                "read_errors", "required_stability_changes", "snapshot_changes",
                "sentinel_changes_within_pass", "controller_context_issues",
                "context_warnings")),
            "native ordinary-register capture is not a coherent core-superset")

    physical = capture_card.get("physical_rows")
    rir_rows = capture_card.get("rir_rows")
    require(isinstance(physical, dict) and physical.get("available") is True and
            physical.get("coherent") is True and
            physical.get("view") == "locked_controller_main",
            "native MAIN rows are unavailable or not locked-controller")
    require(isinstance(rir_rows, dict) and rir_rows.get("available") is True and
            rir_rows.get("coherent") is True,
            "native RIR rows are unavailable or incoherent")
    require(_word_array(physical.get("rows"), 512, "native MAIN rows") == baseline and
            _word_array(rir_rows.get("rows"), 8, "native RIR rows") == rir,
            "native capture rows do not equal candidate baseline")
    require([
        _word_array(item, 512, "native MAIN envelope pass")
        for item in physical.get("passes", [])
    ] == [baseline, baseline] and
            [_word_array(item, 8, "native RIR envelope pass")
             for item in rir_rows.get("passes", [])] == [rir, rir],
            "native capture envelope passes do not repeat exact baseline")
    require(physical.get("pass_hashes") == [packed_sha256(baseline)] * 2 and
            rir_rows.get("pass_hashes") == [packed_sha256(rir)] * 2,
            "native capture envelope hashes do not match exact rows")
    main_cert = physical.get("certification")
    rir_cert = rir_rows.get("certification")
    require(isinstance(main_cert, dict) and
            all(main_cert.get(key) is True for key in (
                "available", "driver_unbound", "full_domain_identity_bound",
                "same_atomic_helper_transaction_per_pass",
                "software_iff_redirection_excluded")) and
            isinstance(rir_cert, dict) and
            all(rir_cert.get(key) is True for key in (
                "available", "driver_unbound", "full_domain_identity_bound",
                "same_atomic_helper_transaction_per_pass",
                "debug_rwl_restored")),
            "native MAIN/RIR certification flags are incomplete")
    main_tx = physical.get("transaction")
    rir_tx = rir_rows.get("transaction")
    require(main_tx == card_access.get("physical_ff_transaction") and
            rir_tx == card_access.get("rir_transaction"),
            "native transaction envelopes disagree across capture sections")
    main_entry = _validate_capture_transaction(
        main_tx, "main_ff", 512, baseline, helper_identity)
    rir_entry = _validate_capture_transaction(
        rir_tx, "rir", 8, rir, helper_identity)
    require(main_entry == rir_entry,
            "native MAIN and RIR transactions did not share one entry state")
    return {
        "document": document,
        "card": capture_card,
        "card_identity": card_identity,
        "helper_identity": helper_identity,
        "tool": tool,
        "main_entry": main_entry,
    }


def _effective_blocks(rows, boundary):
    effective = {}
    source_rows = {}
    for row in range(BACKEND.FF_FIRST, boundary):
        word = rows[row]
        if word in (0, 0xFFFFFFFF) or word & 0x20:
            continue
        chain = word & 0x1F
        block = (word >> 6) & 0x3FF
        require(0 <= chain <= 0x1F,
                "baseline FF row %d has invalid chain %d" % (row, chain))
        effective[(chain, block)] = (word >> 16) & 0xFFFF
        source_rows[(chain, block)] = row
    return effective, source_rows


def _project_component_field(entry, effective):
    value = 0
    shift = 0
    components = entry.get("components")
    require(isinstance(components, list) and components,
            "field %s has no coordinate components" % entry.get("field"))
    for component in components:
        chain = _u32(component.get("chain"), "component chain")
        block = _u32(component.get("block"), "component block")
        bit = _u32(component.get("bit_in_block"), "component bit")
        width = _u32(component.get("width"), "component width")
        require(width and bit + width <= 16,
                "field component escapes a 16-bit logical block")
        block_value = effective.get((chain, block), 0)
        piece = (block_value >> bit) & ((1 << width) - 1)
        value |= piece << shift
        shift += width
    return value


def _project_field(entry, effective):
    if "components" in entry:
        return _project_component_field(entry, effective)
    field = entry.get("field")
    require(field in SPECIAL_FIELD_COORDINATES,
            "field %s lacks independently known coordinates" % field)
    chain, block, bit, width = SPECIAL_FIELD_COORDINATES[field]
    return (effective.get((chain, block), 0) >> bit) & ((1 << width) - 1)


def _validate_exact_current_and_replay(candidate, validated, candidate_path,
                                       requested_bdf):
    require(candidate.get("schema") == "170hx-xray-candidate-plan-v1",
            "candidate schema is not candidate-plan-v1")
    require(candidate.get("register_context_mode") == "same-capture-level3",
            "candidate is not exact-current same-capture Level-3")
    card = validated["card"]
    bdf = str(requested_bdf).lower()
    identity = card.get("identity_binding")
    require(isinstance(identity, dict) and
            identity.get("context_mode") == "same-capture-level3" and
            identity.get("same_native_capture_artifact") is True and
            identity.get("pci_tuple_exact_match") is True,
            "card identity binding is not exact-current same-capture Level-3")
    live_identity = identity.get("live_identity")
    physical_identity = identity.get("physical_identity")
    require(isinstance(live_identity, dict) and
            live_identity == physical_identity and
            str(live_identity.get("domain_bdf", "")).lower() == bdf and
            _u32(live_identity.get("vendor"), "live vendor") == 0x10DE and
            _u32(live_identity.get("device"), "live device") ==
            BACKEND.EXECUTION_DEVICE_ID,
            "card identity is not the exact 20c2 execution specimen")

    capture = card.get("physical_capture")
    require(isinstance(capture, dict), "physical capture binding is missing")
    capture_sha = capture.get("sha256")
    require(isinstance(capture_sha, str) and
            re.fullmatch(r"[0-9a-f]{64}", capture_sha) and
            capture_sha != "0" * 64,
            "physical capture digest is missing or placeholder")
    capture_path = _resolve_bound_file(candidate_path, capture.get("path"))
    require(sha256_file(capture_path) == capture_sha,
            "physical capture file does not match its candidate binding")
    native = _validate_native_capture(
        capture_path, capture_sha, bdf,
        validated["baseline"], validated["rir"])
    require(native["card_identity"] == live_identity and
            native["helper_identity"] ==
            live_identity.get("xray_helper_identity"),
            "candidate identity does not equal the selected native-capture card")
    require(capture.get("generated_utc") ==
            native["document"].get("generated_utc"),
            "candidate capture timestamp does not equal the native artifact")
    main_att = capture.get("main_native_attestation")
    rir_att = capture.get("rir_native_attestation")
    for attestation, kind, digest in (
        (main_att, "main_ff", packed_sha256(validated["baseline"])),
        (rir_att, "rir", packed_sha256(validated["rir"])),
    ):
        require(isinstance(attestation, dict) and
                attestation.get("available") is True and
                attestation.get("errors") == [] and
                attestation.get("kind") == kind and
                attestation.get("artifact_sha256") == capture_sha and
                attestation.get("pass_hashes") == [digest, digest] and
                attestation.get("identity") ==
                native["helper_identity"] and
                attestation.get("source_tool") == native["tool"],
                "%s native attestation is incomplete or inconsistent" % kind)

    context = card.get("physical_entry_to_live_controller_context")
    require(isinstance(context, dict) and
            context.get("context_mode") == "same-capture-level3" and
            context.get("status") == "PASS" and
            context.get("errors") == [],
            "same-capture controller context did not pass")
    entries = context.get("entries")
    require(isinstance(entries, list),
            "same-capture controller context entries are absent")
    context_by_register = {item.get("register"): item for item in entries
                           if isinstance(item, dict)}
    require(None not in context_by_register and
            len(context_by_register) == len(entries) and
            set(CONTEXT_REGISTER_TO_TRANSACTION_KEY).issubset(
                context_by_register),
            "same-capture controller context lacks unique required registers")
    for register, transaction_key in CONTEXT_REGISTER_TO_TRANSACTION_KEY.items():
        item = context_by_register[register]
        actual_entry = _u32(
            native["main_entry"].get(transaction_key),
            "native transaction entry %s" % transaction_key)
        actual_register = _capture_register_value(native["card"], register)
        require(item.get("comparison") ==
                "same-native-artifact-exact-context-check" and
                item.get("exact_match") is True and
                _u32(item.get("physical_transaction_entry"),
                     "%s candidate transaction entry" % register) ==
                actual_entry and
                _u32(item.get("same_capture_register_context"),
                     "%s candidate register context" % register) ==
                actual_register and
                actual_entry == actual_register,
                "candidate controller context does not equal native %s" %
                register)
    essential = context.get("essential_state")
    iff_record = _u32(native["main_entry"].get("iff_record"),
                      "native transaction IFF_RECORD")
    iff_start = iff_record & 0x1FF
    iff_last = (iff_record >> 16) & 0x1FF
    require(isinstance(essential, dict) and
            essential.get("crc_h0_h1_clean_0x55") is True and
            essential.get("main_array_route") is True and
            essential.get("software_iff_inactive") is True and
            essential.get("software_override_inactive") is True and
            essential.get("write_window_observed") is False and
            essential.get("iff_start") == iff_start and
            essential.get("iff_last") == iff_last and
            iff_start == validated["iff_boundary"] and iff_last == 507,
            "capture entry state is incomplete or misclassified")

    cross = card.get("physical_to_same_capture_field_crosscheck")
    cross_entries = cross.get("entries", []) if isinstance(cross, dict) else []
    require(isinstance(cross, dict) and
            cross.get("context_mode") == "same-capture-level3" and
            cross.get("status") == "PASS" and
            cross.get("entry_count") == len(cross_entries) and
            cross.get("entry_count", 0) > 0 and
            all(
                item.get("exact_match") is True or
                (
                    item.get("field") in PAIRED_MEDIA_FIELDS and
                    item.get("exact_match") is None and
                    item.get("unordered_pair_exact_match") is True
                )
                for item in cross_entries
            ),
            "same-capture field crosscheck is missing, empty, or failed")
    cross_by_field = {item.get("field"): item
                      for item in cross_entries}
    optional_media_fields = PAIRED_MEDIA_FIELDS.union(("OPT_NVDEC_DISABLE",))
    required_legacy_fields = set(CROSSCHECK_FIELD_MODELS).difference(
        optional_media_fields
    )
    require(None not in cross_by_field and
            len(cross_by_field) == len(cross_entries) and
            required_legacy_fields.issubset(cross_by_field) and
            set(cross_by_field).issubset(CROSSCHECK_FIELD_MODELS),
            "field crosscheck names are missing or duplicated")

    effective, source_rows = _effective_blocks(
        validated["baseline"], validated["iff_boundary"])
    for field, entry in cross_by_field.items():
        (approved_components, live_register, live_bit, defect_register,
         _field_class) = CROSSCHECK_FIELD_MODELS[field]
        actual_live_register = (
            _capture_register_value(native["card"], live_register)
            if live_register is not None else None
        )
        if live_bit is None:
            components = entry.get("components")
            require(isinstance(components, list) and
                    len(components) == len(approved_components),
                    "%s component count differs from approved model" % field)
            expected_implicit = []
            for component, approved in zip(components, approved_components):
                chain, block, bit, width = approved
                present = (chain, block) in source_rows
                piece = ((effective.get((chain, block), 0) >> bit) &
                         ((1 << width) - 1))
                require(
                    _u32(component.get("chain"), "component chain") == chain and
                    _u32(component.get("block"), "component block") == block and
                    _u32(component.get("bit_in_block"), "component bit") == bit and
                    _u32(component.get("width"), "component width") == width and
                    component.get("effective_record_present") is present and
                    _u32(component.get("piece"), "component piece") == piece,
                    "%s component differs from approved coordinates/baseline" %
                    field)
                if not present:
                    expected_implicit.append({"chain": chain, "block": block})
            require(entry.get("implicit_zero_blocks") == expected_implicit,
                    "%s implicit-zero proof is not native" % field)
            if field in PAIRED_MEDIA_FIELDS:
                direct_options = sorted((
                    _capture_register_value(
                        native["card"], "OPT_NVJPG_DISABLE") & 1,
                    _capture_register_value(
                        native["card"], "OPT_OFA_DISABLE") & 1,
                ))
                direct_defects = {
                    name: _capture_register_value(native["card"], name) & 1
                    for name in (
                        "OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE"
                    )
                }
                require(
                    entry.get("live_register_name") is None and
                    entry.get("live_register") is None and
                    entry.get("individual_attribution") == "unknown" and
                    entry.get("unordered_direct_option_projection") ==
                    direct_options and
                    entry.get("unordered_pair_exact_match") is True and
                    entry.get("paired_defect_registers") == {
                        name: "0x%08x" % value
                        for name, value in direct_defects.items()
                    },
                    "%s unordered pair/defect proof is not native" % field,
                )
            else:
                require(
                    _u32(entry.get("live_register"),
                         "%s candidate live register" % field) ==
                    actual_live_register,
                    "%s live-register proof is not native" % field,
                )
        else:
            require("components" not in entry and
                    entry.get("live_register") == live_register and
                    _u32(entry.get("live_register_bit"),
                         "%s live register bit" % field) == live_bit and
                    _u32(entry.get("live_projection"),
                         "%s live projection" % field) ==
                    ((actual_live_register >> live_bit) & 1),
                    "%s special-field live projection is not native" % field)
        projection = _project_field(entry, effective)
        expected_projection = _u32(
            entry.get("physical_projection"), "%s physical projection" % field)
        require(projection == expected_projection,
                "independent baseline projection disagrees for %s" % field)
        if field not in PAIRED_MEDIA_FIELDS:
            require(
                projection == (
                    actual_live_register if live_bit is None else
                    ((actual_live_register >> live_bit) & 1)
                ),
                "independent live projection disagrees for %s" % field,
            )
        if defect_register is None:
            require(entry.get("defect_register") is None and
                    entry.get("defect_value") is None,
                    "%s invents an unapproved defect source" % field)
        else:
            actual_defect = _capture_register_value(
                native["card"], defect_register)
            require(entry.get("defect_register") == defect_register and
                    _u32(entry.get("defect_value"),
                         "%s candidate defect" % field) == actual_defect and
                    entry.get("defects_are_disabled_in_current_mask") is True and
                    (projection & actual_defect) == actual_defect,
                    "%s defect proof does not equal native capture" % field)

    profile = validated["profile"]
    require(profile.get("model_id") in APPROVED_PROFILES,
            "profile is not in the execution-approved compiler policy")
    profile_policy = APPROVED_PROFILES[profile["model_id"]]
    fields = copy.deepcopy(profile.get("field_targets", []))
    require(isinstance(fields, list),
            "candidate field-target proof is not an array")
    for bit_target in profile.get("pcie_bit_targets", []):
        field = bit_target.get("field")
        require(field in SPECIAL_FIELD_COORDINATES and field in cross_by_field,
                "PCIe bit target lacks an exact coordinate crosscheck")
        current = _project_field(cross_by_field[field], effective)
        fields.append({
            "field": field,
            "current": "0x%x" % current,
            "target": "0x%x" % _u32(
                bit_target.get("target"), "%s target" % field),
            "target_reached": True,
            "target_is_subset_of_current_disable_mask": True,
            "defect": None,
            "all_observed_defects_remain_disabled": None,
            "generated_from_exact_pcie_bit_target": True,
        })
    require(fields, "candidate has no semantically described field target")
    field_names = [item.get("field") for item in fields]
    require(None not in field_names and len(field_names) == len(set(field_names)),
            "candidate field-target names are missing or duplicated")
    require(set(field_names) == set(profile_policy["fields"]),
            "profile field set differs from the approved compiler policy")
    changed_fields = set()
    replay = dict(effective)
    replay_rows = dict(source_rows)
    for index, operation in enumerate(profile.get("operations", [])):
        word = _u32(operation.get("proposed_physical_word"),
                    "operation physical word")
        chain = word & 0x3F
        block = (word >> 6) & 0x3FF
        data = (word >> 16) & 0xFFFF
        require(chain == _u32(operation.get("chain"), "operation chain") and
                block == _u32(operation.get("block"), "operation block"),
                "operation FF envelope disagrees with chain/block")
        old = _u32(operation.get("logical_block_old"), "logical block old")
        new = _u32(operation.get("logical_block_new"), "logical block new")
        require(old <= 0xFFFF and new <= 0xFFFF and
                replay.get((chain, block), 0) == old and data == new,
                "operation %d fails independent last-record-wins replay" % index)
        declared_fields = operation.get("fields_intentionally_changed")
        require(isinstance(declared_fields, list) and declared_fields and
                all(name in field_names for name in declared_fields),
                "operation %d has missing/unproved field targets" % index)
        changed_fields.update(declared_fields)
        replay[(chain, block)] = data
        replay_rows[(chain, block)] = _u32(operation.get("row"), "operation row")
    require(changed_fields.issubset(set(field_names)),
            "operation ledger contains a field without a target proof")

    target_proofs = []
    field_replay_models = []
    for item in fields:
        field = item["field"]
        require(field in cross_by_field and field in APPROVED_FIELD_MODELS,
                "field target %s lacks a same-capture crosscheck" % field)
        cross_entry = cross_by_field[field]
        current = _project_field(cross_entry, effective)
        final = _project_field(cross_entry, replay)
        target = _u32(item.get("target"), "%s target" % field)
        declared_current = _u32(item.get("current"), "%s current" % field)
        if item.get("generated_from_exact_pcie_bit_target") is True:
            declared_final = final
        else:
            declared_final = _u32(item.get("final"), "%s final" % field)
        require(current == declared_current and final == declared_final and
                final == target and item.get("target_reached") is True,
                "field target %s fails independent projection/replay" % field)
        approved_model = APPROVED_FIELD_MODELS[field]
        field_class = approved_model[4]
        if field_class != "identity":
            require((target | current) == current and
                    item.get("target_is_subset_of_current_disable_mask") is True,
                    "field target %s is not a monotonic disable reduction" % field)
        defect_register = approved_model[3]
        defect = item.get("defect")
        if field in PAIRED_MEDIA_FIELDS:
            expected_paired_defects = {
                name: _capture_register_value(native["card"], name) & 1
                for name in ("OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE")
            }
            require(
                defect is None and
                item.get("paired_direct_defects") == expected_paired_defects and
                not any(expected_paired_defects.values()) and
                item.get("all_observed_defects_remain_disabled") is True and
                target == 0 and final == 0,
                "paired NVJPG/OFA target is not a zero-defect inseparable clear",
            )
        elif defect_register is not None:
            defect_value = _capture_register_value(
                native["card"], defect_register)
            require(_u32(defect, "%s defect" % field) == defect_value,
                    "field target %s defect is not native capture data" % field)
            require((final & defect_value) == defect_value and
                    item.get("all_observed_defects_remain_disabled") is True,
                    "field target %s does not preserve observed defects" % field)
        else:
            require(defect is None,
                    "field target %s invents an unapproved defect mask" % field)
        target_proofs.append({
            "field": field,
            "current": "0x%x" % current,
            "final": "0x%x" % final,
            "target": "0x%x" % target,
            "field_class": field_class,
            "target_reached": True,
            "all_observed_defects_remain_disabled":
                item.get("all_observed_defects_remain_disabled"),
            "paired_direct_defects": item.get("paired_direct_defects"),
            "independently_replayed": True,
        })
        components = [{
            "chain": chain,
            "block": block,
            "bit_in_block": bit,
            "width": width,
        } for chain, block, bit, width in approved_model[0]]
        field_replay_models.append({
            "field": field,
            "components_lsb_first": components,
            "defect": item.get("defect"),
            "field_class": field_class,
            "paired_direct_defects": item.get("paired_direct_defects"),
        })

    if PAIRED_MEDIA_FIELDS.intersection(field_names):
        require(
            PAIRED_MEDIA_FIELDS.issubset(field_names),
            "NVJPG/OFA candidate fields are not both present",
        )
        pair_operations = [
            operation for operation in profile.get("operations", [])
            if PAIRED_MEDIA_FIELDS.intersection(
                operation.get("fields_intentionally_changed", [])
            )
        ]
        require(
            len(pair_operations) == 2 and
            all(
                operation.get("stage") == "nvjpg-ofa-inseparable-pair"
                for operation in pair_operations
            ) and
            [operation.get("block") for operation in pair_operations] == [9, 23],
            "NVJPG/OFA candidate pair is not the approved ordered two-operation unit",
        )

    stages = [stage.get("name") for stage in profile.get("stages", [])]
    require(stages and None not in stages and len(stages) == len(set(stages)) and
            all(name in STAGE_RANK for name in stages),
            "profile has unknown, empty, or duplicate stages")
    require(tuple(stages) == tuple(profile_policy["stages"]),
            "profile stage sequence differs from approved compiler policy")
    ranks = [STAGE_RANK[name] for name in stages]
    require(ranks == sorted(ranks), "profile violates canonical dependency order")
    if "identity-last" in stages:
        require(stages[-1] == "identity-last",
                "device identity transition is not the final stage")

    prefixes = profile.get("prefixes")
    require(isinstance(prefixes, list) and
            len(prefixes) == len(profile.get("operations", [])) + 1,
            "profile prefix ledger is missing or incomplete")
    risk_by_prefix = {}
    for expected_count, prefix in enumerate(prefixes):
        require(isinstance(prefix, dict) and
                prefix.get("prefix_operation_count") == expected_count and
                prefix.get("structural_pass") is True and
                prefix.get("ga100_semantically_valid") is True,
                "profile prefix %d is malformed or invalid" % expected_count)
        risks = prefix.get("semantic_risks")
        require(isinstance(risks, list) and
                all(isinstance(item, str) and item for item in risks),
                "profile prefix %d has a malformed semantic-risk ledger" %
                expected_count)
        if risks:
            risk_by_prefix[expected_count] = list(risks)
    if profile["model_id"] == "specimen-a100-20b5-identity":
        expected_risks = {1: [IDENTITY_PREFIX_RISK]}
    elif profile["model_id"] == "specimen-media-mig-to-defect-experimental":
        first_pair_index = next(
            index for index, operation in enumerate(profile["operations"])
            if operation.get("stage") == "nvjpg-ofa-inseparable-pair"
        )
        expected_risks = {
            first_pair_index + 1: [PAIRED_MEDIA_PREFIX_RISK]
        }
    else:
        expected_risks = {}
    require(risk_by_prefix == expected_risks,
            "profile intermediate-prefix risks differ from approved policy")

    return {
        "context_mode": "same-capture-level3",
        "same_native_capture_artifact": True,
        "pci_tuple_exact_match": True,
        "capture_path": str(capture_path),
        "capture_artifact_sha256": capture_sha,
        "capture_generated_utc": native["document"].get("generated_utc"),
        "capture_identity": copy.deepcopy(native["card_identity"]),
        "native_capture_independently_parsed": True,
        "approved_profile_policy": profile["model_id"],
        "native_baseline_main_u32le_sha256":
            packed_sha256(validated["baseline"]),
        "native_baseline_rir_u32le_sha256": packed_sha256(validated["rir"]),
        "main_native_artifact_sha256": capture_sha,
        "rir_native_artifact_sha256": capture_sha,
        "main_pass_hashes": [packed_sha256(validated["baseline"])] * 2,
        "rir_pass_hashes": [packed_sha256(validated["rir"])] * 2,
        "controller_context_status": "PASS",
        "controller_context_errors": [],
        "field_crosscheck_status": "PASS",
        "all_field_crosschecks_exact": True,
        "independent_last_record_wins_replay": True,
        "independent_field_target_replay": target_proofs,
        "field_coordinate_replay_model": field_replay_models,
        "canonical_stage_order_verified": True,
    }


def _blank_reserve(validated):
    baseline = validated["baseline"]
    proposed = validated["proposed"]
    boundary = validated["iff_boundary"]
    unavailable = set(validated["operation_rows"])
    unavailable.update(validated["quarantined_rows"])
    unavailable.update(validated["recovery_rows"])
    candidates = [
        row for row in range(BACKEND.FF_FIRST, boundary)
        if row not in unavailable and baseline[row] == 0 and proposed[row] == 0
    ]
    # Keep the reserve at the tail so ordinary append allocation cannot consume
    # it accidentally without changing the sealed list.
    reserve = candidates[-MIN_BLANK_RESERVE:]
    require(len(reserve) == MIN_BLANK_RESERVE,
            "fewer than 16 blank unquarantined FF rows remain")
    return reserve


def _current_residual_risks(profile):
    risks = []
    for item in profile.get("blockers", []):
        if item == "no OTP execution is authorized":
            continue
        if item.startswith("a fresh final pre-pulse capture"):
            item = (
                "the live executor must reproduce the exact sealed rows 0-487 "
                "and PCI identity in the held session; this is enforced at runtime"
            )
        elif item.startswith("program-source voltage, pulse"):
            item = (
                "the controller's internal electrical programming mechanism remains "
                "undocumented; confidence comes from the November multi-card BAR0 append results"
            )
        risks.append(item)
    return risks


def _semantic_proofs(profile, replay_provenance):
    transition = copy.deepcopy(profile.get("physical_transition_proof", {}))
    rir = copy.deepcopy(profile.get("rir_preservation_proof", {}))
    fields = copy.deepcopy(
        replay_provenance["independent_field_target_replay"])
    for key in (
        "all_operations_are_zero_to_programmed_word",
        "all_target_rows_were_zero",
        "logical_clear_is_last-record-wins_not_physical_erasure",
        "no_existing_physical_row_is_modified",
    ):
        require(transition.get(key) is True,
                "candidate transition proof is missing %s" % key)
    require(profile.get("every_prefix_structurally_valid") is True and
            profile.get("active_iff_unchanged_at_every_prefix") is True,
            "candidate prefix/IFF proof is incomplete")
    require(rir.get("physical_rir_words_changed") is False and
            rir.get("baseline_rir_u32le_sha256") ==
            rir.get("proposed_rir_u32le_sha256"),
            "candidate does not preserve RIR byte-for-byte")
    require(bool(fields) and
            all(item.get("target_reached") is True for item in fields),
            "one or more logical field targets were not reached")
    require(all(item.get("all_observed_defects_remain_disabled") is not False
                for item in fields),
            "one or more field targets enable an observed defect")
    stage_names = [stage.get("name") for stage in profile.get("stages", [])]
    operation_stages = [operation.get("stage")
                        for operation in profile.get("operations", [])]
    stage_indexes = [stage_names.index(name) for name in operation_stages]
    require(stage_indexes == sorted(stage_indexes),
            "operation order violates declared stage order")
    for activation_name, chain, block in (
        ("compute-activation", 1, 8),
        ("memory-activation", 1, 19),
    ):
        indexes = [index for index, operation in enumerate(
                   profile.get("operations", []))
                   if operation.get("stage") == activation_name]
        if indexes:
            last = profile["operations"][indexes[-1]]
            require(last.get("chain") == chain and last.get("block") == block,
                    "%s does not end with its activation block" % activation_name)
    return {
        "physical_transition": transition,
        "independent_last_record_wins_replay": True,
        "every_prefix_structurally_valid": True,
        "active_iff_unchanged_at_every_prefix": True,
        "rir_preservation": rir,
        "field_targets": fields,
        "stages": copy.deepcopy(profile.get("stages", [])),
        "declared_stage_order": stage_names,
    }


def build_manifest(candidate_path, bdf, profile_id, cold_main_path,
                   cold_rir_path, generated_utc=None):
    profile_id = PROFILE_ALIASES.get(profile_id, profile_id)
    candidate_file_sha = sha256_file(candidate_path)
    candidate = _strict_json_file(
        candidate_path, "candidate", candidate_file_sha)
    source_was_legacy = (
        candidate.get("schema") == OLD_SPEC.LEGACY_CANDIDATE_SCHEMA
    )
    try:
        validated = OLD_SPEC.validate_candidate_profile(
            candidate, str(bdf).lower(), profile_id)
    except OLD_SPEC.SpecFailure as exc:
        raise CompileError(str(exc)) from exc

    cold_main = _read_image(cold_main_path, 512, "cold MAIN")
    cold_rir = _read_image(cold_rir_path, 8, "cold RIR")
    require(cold_main == validated["baseline"],
            "cold MAIN does not equal candidate baseline")
    require(cold_rir == validated["rir"],
            "cold RIR does not equal candidate baseline")
    canonical_candidate = OLD_SPEC._normalize_legacy_candidate(candidate)
    exact_current = _validate_exact_current_and_replay(
        canonical_candidate, validated, candidate_path, bdf)
    _validate_corrected_pcie(validated["profile"]["operations"])
    blank_reserve = _blank_reserve(validated)
    semantic_proofs = _semantic_proofs(validated["profile"], exact_current)

    operations = []
    prefix = list(cold_main)
    for index, source in enumerate(validated["profile"]["operations"]):
        row = int(source["row"])
        word = int(source["proposed_physical_word"], 0)
        prefix[row] = word
        operations.append({
            "index": index,
            "row": row,
            "expected_old_word": "0x00000000",
            "program_word": "0x%08x" % word,
            "chain": source["chain"],
            "block": source["block"],
            "logical_data": "0x%04x" % ((word >> 16) & 0xFFFF),
            "logical_block_old": source["logical_block_old"],
            "logical_block_new": source["logical_block_new"],
            "fields_intentionally_changed": copy.deepcopy(
                source.get("fields_intentionally_changed", [])),
            "stage": source.get("stage"),
            "expected_prefix_compare_u32le_sha256": packed_sha256(prefix[:488]),
            "one_attempt_only": True,
            "retry_forbidden_after_command_issue": True,
        })

    identity = copy.deepcopy(validated["card"].get("identity_binding"))
    require(isinstance(identity, dict), "candidate identity binding is missing")
    live_identity = identity.get("live_identity", {})
    expected_post_device = _u32(
        live_identity.get("device"), "live identity device")
    for target in semantic_proofs["field_targets"]:
        if target.get("field") == "OPT_PCIE_DEVIDA":
            expected_post_device = _u32(
                target.get("target"), "post-cold PCI device target")
    document = {
        "schema": MANIFEST_SCHEMA,
        "tool": {
            "name": "170hx_vfi_append_manifest",
            "version": TOOL_VERSION,
        },
        "generated_utc": generated_utc or utc_now(),
        "backend_profile": {
            "id": BACKEND_ID,
            "scope": "verified-blank GA100 main-array FF append only",
            "external_gpio_required": False,
            "generic_mmio_write_api": False,
        },
        "implementation_binding": {
            "backend_file_sha256": sha256_file(BACKEND_PATH),
            "compiler_file_sha256": sha256_file(Path(__file__).resolve()),
            "old_candidate_validator_file_sha256": sha256_file(SPEC_PATH),
            "compiler_semantics": "v2-exact-current-replay-2026-11-23",
            "candidate_validator_semantics":
                "candidate-plan-v1-strict-plus-independent-replay",
        },
        "candidate_source": {
            "path": str(Path(candidate_path).resolve()),
            "file_sha256": candidate_file_sha,
            "report_content_sha256_excluding_self":
                validated["candidate_digest"],
            "schema": OLD_SPEC.CANDIDATE_SCHEMA,
            "legacy_input_normalized": source_was_legacy,
            "register_context_mode": canonical_candidate.get(
                "register_context_mode"),
        },
        "binding": {
            "bdf": str(bdf).lower(),
            "profile_id": profile_id,
            "model_type": validated["profile"].get("model_type"),
            "identity": identity,
            "baseline_main_u32le_sha256": packed_sha256(cold_main),
            "baseline_rir_u32le_sha256": packed_sha256(cold_rir),
            "proposed_main_u32le_sha256": packed_sha256(
                validated["proposed"]),
            "baseline_compare_0_487_u32le_sha256": packed_sha256(
                cold_main[:488]),
            "proposed_compare_0_487_u32le_sha256": packed_sha256(
                validated["proposed"][:488]),
        },
        "images": {
            "baseline_main_rows": ["0x%08x" % value for value in cold_main],
            "baseline_rir_rows": ["0x%08x" % value for value in cold_rir],
            "proposed_main_rows": [
                "0x%08x" % value for value in validated["proposed"]
            ],
        },
        "cold_baseline_attestation": {
            "complete": True,
            "exact_main_match": True,
            "exact_rir_match": True,
            "main_path": str(Path(cold_main_path).resolve()),
            "rir_path": str(Path(cold_rir_path).resolve()),
            "main_file_sha256": packed_sha256(cold_main),
            "rir_file_sha256": packed_sha256(cold_rir),
            "capture_artifact_sha256":
                exact_current["capture_artifact_sha256"],
            "provenance_boundary": (
                "exact bytes and hashes are bound; acquisition authenticity "
                "still depends on the named capture session"
            ),
        },
        "exact_current_provenance": exact_current,
        "live_policy": {
            "command_completion_timeout_seconds": 10.0,
            "command_poll_interval_seconds": 0.001,
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
            "held_state_capture_required": True,
            "held_state_main_cold_equal_rows": [0, 487],
            "held_state_iff_suffix_rows": [488, 511],
            "live_rir_read_required": True,
            "armed_state_capture_before_first_program_required": True,
            "setup_variants_by_initial_efp_status": {
                "0": "enable-override-sense-arm",
                "1": "enable-override-skip-sense-arm",
            },
        },
        "allocation": {
            "iff_boundary": validated["iff_boundary"],
            "exact_row_allowlist": list(validated["operation_rows"]),
            "exact_row_allowlist_sha256": canonical_sha256(
                list(validated["operation_rows"])),
            "quarantined_rows": list(validated["quarantined_rows"]),
            "recovery_reserve_rows": list(validated["recovery_rows"]),
            "blank_reserve_rows": blank_reserve,
            "minimum_blank_reserve": MIN_BLANK_RESERVE,
            "canary_or_noop_rows_forbidden": True,
        },
        "operations": operations,
        "semantic_proofs": semantic_proofs,
        "authorization_policy": {
            "review_receipt_schema": RECEIPT_SCHEMA,
            "review_receipt_required": True,
            "runtime_exact_tty_confirmations_required": True,
            "detached_signatures_required_for_production": 2,
            "experimental_owner_review_is_not_a_production_signature": True,
        },
        "runtime_preconditions": [
            "one exact unbound PCI function and exact sealed PCI tuple",
            "PCI memory decode already enabled; executor will not enable it",
            "held DSOV=0 CRC=0x55 goldilocks state with open privilege masks",
            "a native held-state capture with two equal live MAIN/RIR passes, "
            "exact cold equality for rows 0-487, and a bound state-qualified suffix",
            "EN_SW_OVERRIDE=1 readback, then branch on initial EFP: one SENSE "
            "only when EFP=0, no SENSE when EFP=1, followed by SBFE/WDATA arm",
            "two equal complete MAIN passes after arming and before the first "
            "program command, binding the EN_SW=1 state-qualified IFF suffix",
            "new create-only durable journal and no unresolved earlier attempt",
            "one program command per blank sealed row and no retry",
            "full power removal and independent cold verification afterward",
        ],
        "commit_policy": copy.deepcopy(
            IDENTITY_COMMIT_POLICY
            if profile_id == "specimen-a100-20b5-identity"
            else PAIRED_MEDIA_COMMIT_POLICY
            if profile_id == "specimen-media-mig-to-defect-experimental"
            else DEFAULT_COMMIT_POLICY
        ),
        "post_cold_expectations": {
            "domain_bdf": str(bdf).lower(),
            "vendor": "0x%04x" % _u32(
                live_identity.get("vendor"), "live identity vendor"),
            "device": "0x%04x" % expected_post_device,
            "subsystem_vendor": live_identity.get("subsystem_vendor"),
            "subsystem_device": live_identity.get("subsystem_device"),
            "revision": live_identity.get("revision"),
            "two_equal_complete_main_passes": True,
            "two_equal_complete_rir_passes": True,
            "planner_must_replay_as_noop_for_verified_prefix": True,
            "feature_validation_is_separate": True,
        },
        "residual_research_risks": _current_residual_risks(
            validated["profile"]),
        "executable": True,
        "execution_eligible": True,
        "execution_authorized": False,
        "hardware_backend_invoked": False,
    }
    document["content_sha256_excluding_self"] = canonical_sha256(document)
    return document


def collect_review_receipt(validated, input_func=input, generated_utc=None):
    operation_count = len(validated["operations"])
    baseline_digest = validated["manifest"]["binding"][
        "baseline_compare_0_487_u32le_sha256"]
    expected = [
        ("exact BDF", validated["bdf"]),
        ("exact profile", validated["profile_id"]),
        ("manifest content digest", validated["content_sha256"]),
        ("baseline rows-0-487 digest", baseline_digest),
        ("operation count", str(operation_count)),
        ("irreversible-risk phrase",
         "I REVIEWED EXPERIMENTAL GA100 OTP %d ROWS" % operation_count),
    ]
    confirmations = []
    for label, wanted in expected:
        got = input_func("Type %s [%s]: " % (label, wanted))
        require(got == wanted, "confirmation mismatch for %s" % label)
        confirmations.append({"label": label, "exact_match": True})
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "generated_utc": generated_utc or utc_now(),
        "backend_profile": BACKEND_ID,
        "manifest_content_sha256": validated["content_sha256"],
        "manifest_file_sha256": validated["file_sha256"],
        "bdf": validated["bdf"],
        "profile_id": validated["profile_id"],
        "operation_count": operation_count,
        "confirmations": confirmations,
        "experimental_execution_reviewed": True,
        "production_signature": False,
        "hardware_backend_invoked": False,
    }
    receipt["content_sha256_excluding_self"] = canonical_sha256(receipt)
    return receipt


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    compile_parser = sub.add_parser("compile")
    compile_parser.add_argument("--candidate", required=True)
    compile_parser.add_argument("--bdf", required=True)
    compile_parser.add_argument("--profile", required=True)
    compile_parser.add_argument("--cold-main", required=True)
    compile_parser.add_argument("--cold-rir", required=True)
    compile_parser.add_argument("--output", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--manifest", required=True)
    review_parser = sub.add_parser("review")
    review_parser.add_argument("--manifest", required=True)
    review_parser.add_argument("--output", required=True)
    receipt_parser = sub.add_parser("verify-receipt")
    receipt_parser.add_argument("--manifest", required=True)
    receipt_parser.add_argument("--receipt", required=True)
    cold_parser = sub.add_parser("cold-verify")
    cold_parser.add_argument("--manifest", required=True)
    cold_parser.add_argument("--journal", required=True)
    cold_parser.add_argument(
        "--post-cold-capture", required=True,
        help=(
            "native helper-certified XRAY JSON with two coherent MAIN/RIR "
            "passes from the exact post-power-cycle specimen"
        ),
    )
    cold_parser.add_argument("--power-attestation", required=True)
    cold_parser.add_argument(
        "--output", required=True,
        help="must be JOURNAL.cold.json so the canonical registry can close it",
    )
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "compile":
        document = build_manifest(
            args.candidate, args.bdf, args.profile,
            args.cold_main, args.cold_rir,
        )
        atomic_write_json(args.output, document)
        print(strict_json_dumps({
            "status": "v2_manifest_written",
            "output": str(Path(args.output).resolve()),
            "content_sha256": document["content_sha256_excluding_self"],
            "execution_authorized": False,
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    validated = BACKEND.load_and_validate_manifest(args.manifest)
    if args.command == "verify":
        print(strict_json_dumps({
            "status": "valid_vfi_append_v2_manifest",
            "bdf": validated["bdf"],
            "profile_id": validated["profile_id"],
            "operations": len(validated["operations"]),
            "content_sha256": validated["content_sha256"],
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    if args.command == "review":
        require(sys.stdin.isatty() and sys.stderr.isatty(),
                "review requires an interactive TTY")
        receipt = collect_review_receipt(validated)
        atomic_write_json(args.output, receipt)
        print(strict_json_dumps({
            "status": "experimental_review_receipt_written",
            "output": str(Path(args.output).resolve()),
            "content_sha256": receipt["content_sha256_excluding_self"],
            "production_signature": False,
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    if args.command == "cold-verify":
        expected_output = str(Path(args.journal).resolve()) + ".cold.json"
        require(str(Path(args.output).resolve()) == expected_output,
                "cold receipt output must be JOURNAL.cold.json")
        receipt = BACKEND.verify_cold_persistence(
            validated, args.journal,
            args.post_cold_capture,
            args.power_attestation,
        )
        atomic_write_json(args.output, receipt)
        BACKEND.load_and_validate_cold_receipt(
            args.output, validated=validated, journal_path=args.journal)
        print(strict_json_dumps({
            "status": receipt["status"],
            "output": str(Path(args.output).resolve()),
            "content_sha256": receipt["content_sha256_excluding_self"],
            "planner_noop_verified": False,
            "feature_validation_complete": False,
        }, sort_keys=True))
        return 0
    receipt = BACKEND.load_and_validate_receipt(args.receipt, validated)
    print(strict_json_dumps({
        "status": "valid_experimental_review_receipt",
        "receipt_content_sha256": receipt["content_sha256"],
        "manifest_content_sha256": validated["content_sha256"],
        "hardware_backend_invoked": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CompileError, BACKEND.VfiAppendError) as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
