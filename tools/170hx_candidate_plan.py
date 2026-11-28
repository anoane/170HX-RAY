#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build non-executable, specimen-bound GA100 FF candidate plans.

This tool deliberately stops at offline planning. It consumes certified repeated
MAIN/RIR captures and either a separate Level-2 register survey or stable registers
bound to the same native Level-3 artifact, reconstructs complete candidate images,
and validates every append prefix with 170HX-XRAY's decoder. It does not contain an
MMIO backend, a controller-write primitive, or an apply mode.

The report is evidence, not authorization.  In particular, an encoded FF word in
the output is not a recommendation to program it: electrical qualification,
same-die re-attestation, independent review, and separate operator authorization
remain mandatory blockers.
"""

import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys


SCHEMA = "170hx-xray-candidate-plan-v1"
SCRIPT_VERSION = "0.6.0"
LEGACY_CAPTURE_SCHEMA = "170hx-ray-v1"
LEGACY_PUBLIC_AUDIT_SCHEMA = "170hx-ray-public-plan-audit-v1"
LEGACY_PUBLIC_AUDIT_TOOL_SHA256 = (
    "4c3a9c0cdebc2412e63d58ea8ed2453cdb84230c8796e71546eb648d8d1ca4b1"
)
FUSELESS_START = 222
FUSELESS_END = 511
QUARANTINED_ROWS = {487}
RECOVERY_MARGIN_ROWS = 16
PHYSICAL_INPUT_FORMATS = ("auto", "xray-native-level3-json")
LIVE_INPUT_FORMATS = ("auto", "xray-level2-json")
PLAN_OUTPUT_FORMATS = ("json", "jsonl", "text", "canonical-json-bin")
PLAN_EXPORT_FILENAMES = {
    "json": "candidate-plan.json",
    "jsonl": "candidate-plan.jsonl",
    "text": "candidate-plan.txt",
    "canonical-json-bin": "candidate-plan.canonical-json.bin",
}
PLAN_TEXT_BEGIN = "----- BEGIN CANONICAL CANDIDATE PLAN JSON -----"
PLAN_TEXT_END = "----- END CANONICAL CANDIDATE PLAN JSON -----"


class PlanFailure(RuntimeError):
    pass


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def reject_json_constant(value):
    raise PlanFailure("non-finite JSON constant is forbidden: %s" % value)


def strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", reject_json_constant)
    return _JSON_LOADS(*args, **kwargs)


def strict_json_dumps(*args, **kwargs):
    kwargs.setdefault("allow_nan", False)
    return _JSON_DUMPS(*args, **kwargs)


def positive_finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return result


def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanFailure("duplicate JSON object key: %r" % key)
        result[key] = value
    return result


def _normalize_legacy_input(value):
    """Translate only known RAY envelope tokens; never rewrite source paths."""
    if isinstance(value, list):
        return [_normalize_legacy_input(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = {
                "ray_helper_identity": "xray_helper_identity",
            }.get(key, key)
            if normalized_key in result:
                raise PlanFailure(
                    "legacy normalization key collision at %r" % normalized_key
                )
            result[normalized_key] = _normalize_legacy_input(item)
        return result
    if isinstance(value, str):
        return {
            LEGACY_CAPTURE_SCHEMA: "170hx-xray-v1",
            "170hx-ray-helper-allowlisted-read32":
                "170hx-xray-helper-allowlisted-read32",
        }.get(value, value)
    return value


def load_json_file_with_sha256(path, normalize_legacy=True):
    payload = path.read_bytes()
    try:
        document = strict_json_loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise PlanFailure("invalid UTF-8 JSON source artifact %s: %s" % (path, exc))
    if normalize_legacy:
        document = _normalize_legacy_input(document)
    return document, sha256_bytes(payload)


def load_json_file(path, normalize_legacy=True):
    return load_json_file_with_sha256(path, normalize_legacy)[0]


def immutable_source_tool(path):
    """Return the source tool identity exactly as recorded in the input bytes.

    Legacy normalization is an internal parser compatibility mechanism.  It must
    never manufacture a historically impossible tool/version pair in emitted
    provenance (for example, XRAY did not exist at a RAY 3.x version).
    """
    document, source_sha256 = load_json_file_with_sha256(
        path, normalize_legacy=False
    )
    tool = document.get("tool")
    if not isinstance(tool, dict):
        raise PlanFailure("source artifact lacks an immutable tool identity: %s" % path)
    if not isinstance(tool.get("name"), str) or not isinstance(
            tool.get("version"), str):
        raise PlanFailure("source artifact tool identity/version is invalid: %s" % path)
    return copy.deepcopy(tool), source_sha256


def detect_xray_input_format(raw_document, role):
    """Classify the immutable on-disk envelope before legacy normalization."""
    if role not in ("physical", "live"):
        raise PlanFailure("unknown candidate-plan input role: %s" % role)
    schema = raw_document.get("schema") if isinstance(raw_document, dict) else None
    tool_name = (
        raw_document.get("tool", {}).get("name")
        if isinstance(raw_document, dict) and
        isinstance(raw_document.get("tool"), dict) else None
    )
    current_label = (
        "xray-native-level3-json" if role == "physical"
        else "xray-level2-json"
    )
    if schema == "170hx-xray-v1" and tool_name == "170HX-XRAY":
        return current_label
    if schema == LEGACY_CAPTURE_SCHEMA and tool_name == "170HX-RAY":
        return "legacy-ray-%s-json" % (
            "native-level3" if role == "physical" else "level2"
        )
    return "unknown-json"


def enforce_declared_input_format(declared, detected, allowed, role):
    if declared not in allowed:
        raise PlanFailure(
            "unsupported %s input-format label: %s" % (role, declared)
        )
    if declared != "auto" and declared != detected:
        raise PlanFailure(
            "declared %s input format %s does not match immutable on-disk format %s"
            % (role, declared, detected)
        )


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(path.read_bytes())


def write_new_private_bytes(path, payload):
    """Create one evidence artifact without following or replacing a name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    created = False
    try:
        descriptor = os.open(str(path), flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while sealing candidate plan")
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_new_private_json(path, value):
    payload = (strict_json_dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_new_private_bytes(path, payload)


def serialize_plan_export(value, output_format):
    """Serialize one complete plan without changing its semantic document.

    The ``canonical-json-bin`` representation is canonical JSON bytes in a
    binary container.  It is deliberately not a native fuse image, row image,
    manifest, receipt, or controller command stream.
    """
    if output_format == "json":
        return (strict_json_dumps(value, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    canonical = strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    if output_format == "jsonl":
        return (canonical + "\n").encode("ascii")
    if output_format == "canonical-json-bin":
        return canonical.encode("ascii")
    if output_format == "text":
        header = [
            "170HX-XRAY non-executable candidate-plan review",
            "Execution authorized: false",
            "This is a lossless human-review wrapper around canonical JSON; it is not an OTP image.",
            "Schema: %s" % value.get("schema", "unknown"),
            "Content SHA-256: %s" % canonical_json_sha256(value),
            PLAN_TEXT_BEGIN,
            strict_json_dumps(value, indent=2, sort_keys=True),
            PLAN_TEXT_END,
            "",
        ]
        return "\n".join(header).encode("utf-8")
    raise PlanFailure("unsupported plan output format: %s" % output_format)


def deserialize_plan_export(payload, input_format):
    """Decode a plan export for parity tests and offline review only."""
    if input_format not in PLAN_OUTPUT_FORMATS:
        raise PlanFailure("unsupported plan export format: %s" % input_format)
    try:
        text = payload.decode("ascii" if input_format != "text" else "utf-8")
    except UnicodeError as exc:
        raise PlanFailure("plan export is not valid text: %s" % exc)
    if input_format == "text":
        begin = text.find(PLAN_TEXT_BEGIN + "\n")
        end = text.rfind("\n" + PLAN_TEXT_END)
        if begin < 0 or end < 0 or end <= begin:
            raise PlanFailure("human plan export lacks canonical JSON markers")
        text = text[begin + len(PLAN_TEXT_BEGIN) + 1:end]
    if input_format == "jsonl":
        lines = text.splitlines()
        if len(lines) != 1:
            raise PlanFailure("candidate-plan JSONL must contain exactly one record")
        text = lines[0]
    try:
        value = strict_json_loads(
            text, object_pairs_hook=reject_duplicate_json_keys
        )
    except (TypeError, ValueError) as exc:
        raise PlanFailure("invalid candidate-plan %s: %s" % (input_format, exc))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PlanFailure("candidate-plan export has an unsupported schema")
    return value


def write_plan_export(path, value, output_format):
    write_new_private_bytes(path, serialize_plan_export(value, output_format))


def write_all_plan_exports(directory, value):
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = os.lstat(str(directory))
    except FileNotFoundError:
        os.mkdir(str(directory), 0o700)
        status = os.lstat(str(directory))
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise PlanFailure(
            "plan export directory must be a real directory: %s" % directory
        )
    os.chmod(str(directory), 0o700, follow_symlinks=False)
    paths = {
        output_format: directory / PLAN_EXPORT_FILENAMES[output_format]
        for output_format in PLAN_OUTPUT_FORMATS
    }
    for path in paths.values():
        if path.exists() or path.is_symlink():
            raise PlanFailure("refusing to replace plan export: %s" % path)
    for output_format in PLAN_OUTPUT_FORMATS:
        write_plan_export(paths[output_format], value, output_format)
    return paths


def path_for_report(root, path):
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def packed_u32_sha256(words):
    return sha256_bytes(b"".join(struct.pack("<I", value) for value in words))


def canonical_json_sha256(value):
    encoded = strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return sha256_bytes(encoded)


def parse_int(value, label):
    if isinstance(value, bool):
        raise PlanFailure("%s is boolean, not an integer" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as exc:
            raise PlanFailure("%s is not an integer: %r" % (label, value)) from exc
    else:
        raise PlanFailure("%s is not an integer: %r" % (label, value))
    if not 0 <= result <= 0xFFFFFFFF:
        raise PlanFailure("%s is outside unsigned 32-bit range" % label)
    return result


def hex32(value):
    return "0x%08x" % (value & 0xFFFFFFFF)


def hex16(value):
    return "0x%04x" % (value & 0xFFFF)


def popcount(value):
    return bin(int(value)).count("1")


def load_python_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise PlanFailure("cannot load Python module %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# These coordinates are duplicated intentionally and checked against the selected
# XRAY implementation before use.  A planner silently inheriting changed coordinates
# is less useful than a hard mismatch.  Their evidence/confidence comes from XRAY's
# catalog; none is promoted to an official GA100 descriptor.
FIELD_SPECS = {
    "OPT_FBIO_DISABLE": {
        "chain": 1, "bit": 83, "width": 24, "defect": "FBIO_DEFECTIVE",
        "class": "disable_mask", "domain": "memory",
    },
    "OPT_FBPA_DISABLE": {
        "chain": 1, "bit": 107, "width": 24, "defect": "FBPA_DEFECTIVE",
        "class": "disable_mask", "domain": "memory",
    },
    "OPT_GPC_DISABLE": {
        "chain": 1, "bit": 131, "width": 8, "defect": "GPC_DEFECTIVE",
        "class": "disable_mask", "domain": "compute",
    },
    "OPT_NVDEC_DISABLE": {
        "chain": 1, "bit": 139, "width": 5,
        "defect": "OPT_NVDEC_DEFECTIVE",
        "class": "disable_mask", "domain": "media_decode",
    },
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": {
        "chain": 1, "bit": 146, "width": 1,
        "live_register": None,
        "comparison_registers": ["OPT_NVJPG_DISABLE", "OPT_OFA_DISABLE"],
        "defects": ["OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE"],
        "class": "paired_disable_candidate", "domain": "media_pair",
        "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
    },
    "OPT_PES_DISABLE": {
        "chain": 1, "bit": 251, "width": 24, "defect": "PES_DEFECTIVE",
        "class": "disable_mask", "domain": "compute",
    },
    "OPT_FBP_DISABLE": {
        "chain": 1, "bit": 307, "width": 12, "defect": "FBP_DEFECTIVE",
        "class": "disable_mask", "domain": "memory",
    },
    "OPT_ROP_L2_DISABLE": {
        "chain": 1, "bit": 319, "width": 24, "defect": "ROP_L2_DEFECTIVE",
        "class": "disable_mask", "domain": "memory_compute_dependency",
    },
    "OPT_NVLINK_DISABLE": {
        "chain": 1, "bit": 368, "width": 3, "defect": "NVLINK_DEFECTIVE",
        "class": "disable_mask", "domain": "nvlink_global_group",
    },
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": {
        "chain": 1, "bit": 371, "width": 1,
        "live_register": None,
        "comparison_registers": ["OPT_NVJPG_DISABLE", "OPT_OFA_DISABLE"],
        "defects": ["OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE"],
        "class": "paired_disable_candidate", "domain": "media_pair",
        "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
    },
    "OPT_SYS_PIPE_DISABLE": {
        "chain": 1, "bit": 372, "width": 8, "defect": "SYS_PIPE_DEFECTIVE",
        "class": "disable_mask", "domain": "system_topology",
    },
    "OPT_PCIE_DEVIDA": {
        "chain": 2, "bit": 593, "width": 16,
        "class": "identity", "domain": "identity",
    },
    "OPT_PCIE_DEVIDB": {
        "chain": 2, "bit": 609, "width": 16,
        "class": "identity", "domain": "identity",
    },
}
for _gpc in range(8):
    FIELD_SPECS["OPT_TPC_GPC%d_DISABLE" % _gpc] = {
        "chain": 1,
        "bit": 187 + 8 * _gpc,
        "width": 8,
        "defect": "TPC_GPC%d_DEFECTIVE" % _gpc,
        "class": "disable_mask",
        "domain": "compute",
    }


PCIE_BIT_SPECS = {
    "OPT_DISABLE_GEN3_SPEED": {
        "chain": 2, "block": 39, "bit_in_block": 9,
        "live_register": "OPT_DISABLE_GEN3_SPEED", "live_bit": 0,
        "certainty_percent": 99,
        "reason": (
            "2026-11-19 isolated cold-boot experiment: clearing block-39 bit 1 "
            "alone left the option set; a later record restored bit 1 and cleared "
            "bit 9, after which the option read zero"
        ),
    },
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": {
        "chain": 2, "block": 47, "bit_in_block": 3,
        "live_register": "OPT_PCIE_MAGIC_BITS_D", "live_bit": 25,
        "certainty_percent": 97,
        "reason": "differential map and direct GA100 MAGIC_D field agree",
    },
    "OPT_PCIE_BOOT_GEN23_DISABLE": {
        "chain": 2, "block": 49, "bit_in_block": 6,
        "live_register": "OPT_PCIE_BOOT_GEN23_DISABLE", "live_bit": 0,
        "certainty_percent": 96,
        "reason": "differential map and direct option field agree",
    },
    "OPT_PCIE_BOOT_GEN3_DISABLE": {
        "chain": 2, "block": 49, "bit_in_block": 7,
        "live_register": "OPT_PCIE_BOOT_GEN3_DISABLE", "live_bit": 0,
        "certainty_percent": 96,
        "reason": "differential map and direct option field agree",
    },
}


MEMORY_PREREQ_FIELDS = [
    "OPT_FBIO_DISABLE", "OPT_FBPA_DISABLE", "OPT_ROP_L2_DISABLE",
]
MEMORY_ACTIVATION_FIELDS = ["OPT_FBP_DISABLE"]
COMPUTE_PREREQ_FIELDS = [
    "OPT_TPC_GPC%d_DISABLE" % index for index in range(8)
] + ["OPT_PES_DISABLE"]
COMPUTE_ACTIVATION_FIELDS = ["OPT_GPC_DISABLE"]
NVLINK_FIELDS = ["OPT_NVLINK_DISABLE"]
SYS_PIPE_FIELDS = ["OPT_SYS_PIPE_DISABLE"]
MEDIA_NVDEC_FIELDS = ["OPT_NVDEC_DISABLE"]
MEDIA_PAIR_FIELDS = [
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
]
IDENTITY_FIELDS = ["OPT_PCIE_DEVIDA", "OPT_PCIE_DEVIDB"]
PCIE_ISOLATED_BITS = list(PCIE_BIT_SPECS)

CONTEXT_REGISTER_TO_PHYSICAL_KEY = {
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
CURRENT_ONLY_CONTEXT_REGISTERS = [
    "SECURED_BURN_FEATURE_ENABLE",
    "OPT_SECURE_FUSE_WDATA_WR_SECURE",
]


def pcie_exact_reported_order():
    """Order with a reported functional intermediate: block 47, then 46."""
    return [
        stage("pcie-a100-exact-block47", raw_blocks=[
            {"chain": 2, "block": 47, "expected": 0x0051, "target": 0x0000},
        ], purpose="Reported functional sequence first clears the remaining block-47 MAGIC_D projection."),
        stage("pcie-a100-exact-block46", raw_blocks=[
            {"chain": 2, "block": 46, "expected": 0xA000, "target": 0x8000},
        ], purpose="Reported functional sequence then clears the block-46 empirical bit."),
    ]


def pcie_exact_dstoro_order():
    """Preserve Danylo's source order for its corrected-public lineage."""
    return [
        stage("pcie-a100-exact-block46-dstoro-order", raw_blocks=[
            {"chain": 2, "block": 46, "expected": 0xA000, "target": 0x8000},
        ], purpose="Danylo source order: block 46 before block 47."),
        stage("pcie-a100-exact-block47-dstoro-order", raw_blocks=[
            {"chain": 2, "block": 47, "expected": 0x0051, "target": 0x0000},
        ], purpose="Danylo source order: block 47 after block 46."),
    ]


def stage(name, fields=None, pcie_bits=None, raw_blocks=None, purpose=None):
    return {
        "name": name,
        "fields": list(fields or []),
        "pcie_bits": list(pcie_bits or []),
        "raw_blocks": list(raw_blocks or []),
        "purpose": purpose,
    }


def review_only_profile(profile_id, intent, subjects):
    """Represent a research target without inventing a programmable operation."""
    return {
        "id": profile_id,
        "model_type": "review-only-unsupported",
        "intent": intent,
        "stages": [],
        "review_only": {
            "operation_count_required": 0,
            "compiler_eligible": False,
            "runtime_eligible": False,
            "subjects": subjects,
        },
    }


PROFILE_SPECS = [
    {
        "id": "specimen-80gb-hbm",
        "model_type": "specimen-derived",
        "intent": (
            "Reduce HBM/FB disable masks to this die's observed defect masks; "
            "the computed target can be 64, 80 or 96 GiB."
        ),
        "stages": [
            stage("memory-prerequisites", MEMORY_PREREQ_FIELDS,
                  purpose="Prepare FBIO/FBPA/ROP-L2 before exposing more FBPs."),
            stage("memory-activation", MEMORY_ACTIVATION_FIELDS,
                  purpose="Activate only non-defective FBPs after prerequisites."),
        ],
    },
    {
        "id": "specimen-more-sm",
        "model_type": "specimen-derived",
        "intent": "Reduce compute disable masks to this die's defect masks.",
        "stages": [
            stage("rop-l2-dependency", ["OPT_ROP_L2_DISABLE"],
                  purpose="Address the public GSP-failure dependency before SM activation."),
            stage("compute-prerequisites", COMPUTE_PREREQ_FIELDS,
                  purpose="Prepare TPC and PES topology before changing GPC activation."),
            stage("compute-activation", COMPUTE_ACTIVATION_FIELDS,
                  purpose="GPC is the final compute activation boundary."),
        ],
    },
    {
        "id": "xray-common-permanent-topology",
        "model_type": "xray-best-evidence-segment",
        "intent": (
            "One exact-current permanent-topology segment: reduce HBM and "
            "compute disable masks only to the normalized per-die defect closure, "
            "then stop for a cold validation before PCIe or identity work."
        ),
        "stages": [
            stage(
                "memory-and-shared-prerequisites",
                MEMORY_PREREQ_FIELDS,
                purpose=(
                    "Program FBIO, FBPA and the shared ROP/L2 dependency before "
                    "either FBP or GPC becomes less disabled."
                ),
            ),
            stage(
                "compute-prerequisites",
                COMPUTE_PREREQ_FIELDS,
                purpose=(
                    "Program normalized TPC and 3/3/2 PES closure while the "
                    "parent GPC activation mask is unchanged."
                ),
            ),
            stage(
                "memory-activation",
                MEMORY_ACTIVATION_FIELDS,
                purpose=(
                    "Reduce FBP disable only after every memory companion plane."
                ),
            ),
            stage(
                "compute-activation",
                COMPUTE_ACTIVATION_FIELDS,
                purpose=(
                    "Reduce GPC disable last, after TPC/PES and shared ROP/L2."
                ),
            ),
        ],
    },
    {
        "id": "specimen-nvlink-global",
        "model_type": "specimen-derived",
        "intent": "Reduce the three-bit global NVLink group mask to the defect mask.",
        "stages": [stage("nvlink-global-group", NVLINK_FIELDS)],
    },
    {
        "id": "specimen-pcie-gen4-isolated",
        "model_type": "specimen-derived",
        "intent": "Clear only the mapped/candidate PCIe generation gates.",
        "stages": [stage("pcie-isolated-gates", pcie_bits=PCIE_ISOLATED_BITS)],
    },
    {
        "id": "specimen-pcie-gen4-a100-exact",
        "model_type": "specimen-derived",
        "intent": "Isolated gates plus the reported whole-word A100 MAGIC_D projection.",
        "stages": [
            stage("pcie-isolated-gates", pcie_bits=PCIE_ISOLATED_BITS),
        ] + pcie_exact_reported_order(),
    },
    {
        "id": "specimen-a100-20b5-identity",
        "model_type": "specimen-derived",
        "intent": "Change both redundant logical device-ID fields to 0x20b5, identity last.",
        "stages": [stage("identity-last", IDENTITY_FIELDS)],
    },
    {
        "id": "specimen-sys-pipe-to-defect-experimental",
        "model_type": "specimen-derived",
        "intent": "Reduce SYS_PIPE to its observed defect mask as a separately reviewable experiment.",
        "stages": [stage("sys-pipe-experimental", SYS_PIPE_FIELDS)],
    },
    {
        "id": "specimen-media-mig-to-defect-experimental",
        "model_type": "specimen-derived-experimental",
        "intent": (
            "Reduce SYS_PIPE and NVDEC disable masks to direct per-die defect "
            "masks, then clear the inseparable NVJPG/OFA candidate pair only "
            "when both direct defect words are zero. NVENC is not included."
        ),
        "stages": [
            stage(
                "mig-system-pipe-to-defect",
                SYS_PIPE_FIELDS,
                purpose=(
                    "Prepare only non-defective system-pipe instances; this is "
                    "the reported FF prerequisite for eight-way MIG enumeration."
                ),
            ),
            stage(
                "nvdec-to-defect",
                MEDIA_NVDEC_FIELDS,
                purpose=(
                    "Enable only decoder instances not marked defective by the "
                    "direct GA100 defect word."
                ),
            ),
            stage(
                "nvjpg-ofa-inseparable-pair",
                MEDIA_PAIR_FIELDS,
                purpose=(
                    "Treat both candidate bits as one inseparable logical intent; "
                    "hardware still receives sequential append operations with an "
                    "unavoidable asymmetric prefix, and individual NVJPG-versus-"
                    "OFA attribution remains unknown."
                ),
            ),
        ],
    },
    {
        "id": "public-corrected-cmpglitch-intent",
        "model_type": "public-corrected",
        "public_source": "cmpglitch-reported-card",
        "intent": "Rebuild the reported conversion intent per specimen with corrected dependencies and rows.",
        "stages": [
            stage("memory-prerequisites", MEMORY_PREREQ_FIELDS),
            stage("memory-activation", MEMORY_ACTIVATION_FIELDS),
            stage("compute-prerequisites", COMPUTE_PREREQ_FIELDS),
            stage("compute-activation", COMPUTE_ACTIVATION_FIELDS),
            stage("pcie-isolated-gates", pcie_bits=PCIE_ISOLATED_BITS),
        ] + pcie_exact_reported_order() + [
            stage("identity-last", IDENTITY_FIELDS),
        ],
    },
    {
        "id": "public-corrected-dstoro-intent",
        "model_type": "public-corrected",
        "public_source": "dstoro-plan-2026-11-17",
        "intent": "Rebuild the public no-identity topology/PCIe intent per specimen with dependency ordering.",
        "stages": [
            stage("memory-prerequisites", MEMORY_PREREQ_FIELDS),
            stage("memory-activation", MEMORY_ACTIVATION_FIELDS),
            stage("compute-prerequisites", COMPUTE_PREREQ_FIELDS),
            stage("compute-activation", COMPUTE_ACTIVATION_FIELDS),
            stage("pcie-isolated-gates", pcie_bits=PCIE_ISOLATED_BITS),
        ] + pcie_exact_dstoro_order(),
    },
    {
        "id": "xray-best-evidence-a100-conversion",
        "model_type": "xray-best-evidence",
        "intent": "Full strongest-evidence conversion candidate, with uncertain features kept separate.",
        "stages": [
            stage("memory-prerequisites", MEMORY_PREREQ_FIELDS),
            stage("memory-activation", MEMORY_ACTIVATION_FIELDS),
            stage("compute-prerequisites", COMPUTE_PREREQ_FIELDS),
            stage("compute-activation", COMPUTE_ACTIVATION_FIELDS),
            stage("pcie-isolated-gates", pcie_bits=PCIE_ISOLATED_BITS),
        ] + pcie_exact_reported_order() + [
            stage("identity-last", IDENTITY_FIELDS),
        ],
    },
    review_only_profile(
        "review-ss0-iff",
        "Inventory the reported SS0 feature-override IFF record without treating its values or rows as portable.",
        [{
            "feature": "SS0",
            "plane": "IFF",
            "target_register": "FEATURE_OVERRIDE_SM_SPEED_SELECT",
            "target_mmio": "0x0082381c",
            "reported_words": ["0x082381fd", "0x88888888"],
            "reported_rows": [484, 485],
            "meaning": "IMLA0-3/FMLA/FFMA/DP throttle override",
            "coordinate_certainty_percent": 85,
            "portable_placement_certainty_percent": 0,
            "blocker": "IFF placement depends on the measured boundary and record grammar; no executable IFF allocator exists",
        }],
    ),
    review_only_profile(
        "review-ss1-iff",
        "Inventory the reported SS1 feature-override IFF record without conflating IMLA4 with FP64.",
        [{
            "feature": "SS1",
            "plane": "IFF",
            "target_register": "FEATURE_OVERRIDE_SM_SPEED_SELECT_1",
            "target_mmio": "0x00823820",
            "reported_words": ["0x0823823d", "0x00000008"],
            "reported_rows": [486, 487],
            "meaning": "IMLA4 throttle override; not the FP64 selector",
            "coordinate_certainty_percent": 85,
            "portable_placement_certainty_percent": 0,
            "blocker": "row 487 is quarantined and no executable IFF allocator or portable value proof exists",
        }],
    ),
    review_only_profile(
        "review-dp-conventional",
        "Inventory the FP64 speed selector and proposed adjacent control/undo fuse as conventional-fuse research only.",
        [{
            "feature": "FP64/DP",
            "plane": "conventional-primary-redundant",
            "rows": [6, 7],
            "selector_bit": 26,
            "selector_name": "OPT_SM_SPEED_SELECT_DP",
            "control_or_undo_bit": 27,
            "control_name": "OPT_SM_SPEED_SELECT_DP_CONTROL",
            "selector_coordinate_certainty_percent": 99,
            "control_semantics_certainty_percent": 65,
            "blocker": "primary/redundant sense, RIR interaction and isolated cold proof of bit 27 are unresolved",
        }],
    ),
    review_only_profile(
        "review-ecc-conventional",
        "Inventory the ECC enable and adjacent control fuse without claiming a complete ECC recipe.",
        [{
            "feature": "ECC",
            "plane": "conventional-primary-redundant",
            "rows": [6, 7],
            "enable_bit": 28,
            "enable_name": "OPT_ECC_EN",
            "control_bit": 29,
            "control_name": "OPT_ECC_EN_CONTROL",
            "coordinate_certainty_percent": 99,
            "feature_sufficiency_certainty_percent": 45,
            "blocker": "sense/control polarity, RIR, memory-domain gates, firmware policy and error-injection validation are unresolved",
        }],
    ),
    review_only_profile(
        "review-nvenc-unsupported",
        "Make NVENC's absence from the physical plan explicit rather than silently omitting it.",
        [{
            "feature": "NVENC",
            "plane": "unknown",
            "option_control_defect_status_width_bits": 2,
            "instance_count_register_width_bits": 5,
            "direct_registers": [
                "OPT_NVENC_DISABLE", "OPT_NVENC_DISABLE_CP",
                "OPT_NVENC_DEFECTIVE", "CTRL_OPT_NVENC",
                "STATUS_OPT_NVENC", "PTOP_SCAL_NUM_NVENCS",
            ],
            "ptop_instance_count_is_inventory_only": True,
            "physical_coordinate_certainty_percent": 0,
            "populated_engine_evidence": "none",
            "production_a100_absence_certainty_percent": 99,
            "retained_four_card_unusable_engine_inference_percent": 97,
            "supported_permanent_unlock_target_percent": 0,
            "blocker": "no defensible GA100 physical coordinate or populated-engine evidence exists",
        }],
    ),
    review_only_profile(
        "review-nvlink-per-link-unsupported",
        "Separate the unknown twelve-link disable map from the three global IOCTRL group bits.",
        [{
            "feature": "per-link NVLink",
            "plane": "unknown",
            "direct_register": "OPT_PERLINK_DISABLE",
            "direct_width_bits": 12,
            "physical_coordinate_certainty_percent": 0,
            "global_group_candidate": "Chain-1 bits 368..370 are not this field",
            "blocker": "physical coordinate, board routing, ROM/GSP policy and link training are unresolved",
        }],
    ),
    review_only_profile(
        "review-reconfig-last-sm-ff",
        "Inventory the reported per-specimen RECONFIG FF record for the last SMs without treating its GPC pair or word as portable.",
        [{
            "feature": "TPC RECONFIG / last SMs",
            "plane": "FF",
            "reported_chain": 1,
            "reported_block": 26,
            "reported_word": "0x00000681",
            "reported_effect": "one Allspark specimen cleared its GPC3/GPC4 RECONFIG pair and gained four SMs",
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:150-153,280",
            "reported_coordinate_certainty_percent": 80,
            "portable_target_certainty_percent": 0,
            "blocker": "the affected GPC pair differs by card, the report is not independently reproduced, and no specimen-derived RECONFIG allocator exists",
        }],
    ),
    review_only_profile(
        "review-speed-select-ff",
        "Inventory the reported FF speed-select records without turning specimen words into a portable throttle-unlock recipe.",
        [{
            "feature": "scalar FP/integer speed selectors",
            "plane": "FF",
            "reported_records": [
                {"chain": 2, "block": 78, "word": "0x00001382"},
                {"chain": 2, "block": 79, "word": "0x2e0013c2"},
            ],
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:281-282",
            "reported_coordinate_certainty_percent": 75,
            "reported_semantics_certainty_percent": 50,
            "portable_target_certainty_percent": 0,
            "blocker": "the values are community specimen records, include nonzero data, and lack an independently qualified field-to-FF encoder and cold-boot proof",
        }],
    ),
    review_only_profile(
        "review-row-remapper-conventional",
        "Inventory the proposed conventional row-remapper enable pair without claiming an ECC or repair recipe.",
        [{
            "feature": "row remapper",
            "plane": "conventional-primary-redundant",
            "rows": [48, 49],
            "bit": 5,
            "reported_name": "OPT_ROW_REMAPPER_EN",
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:324",
            "coordinate_certainty_percent": 60,
            "feature_sufficiency_certainty_percent": 20,
            "blocker": "primary/redundant sense, RIR interaction, polarity, remap data and isolated cold validation are unresolved",
        }],
    ),
    review_only_profile(
        "review-vgx-conventional",
        "Inventory the proposed conventional VGX/vGPU enable pair without claiming license or firmware enablement.",
        [{
            "feature": "VGX/vGPU",
            "plane": "conventional-primary-redundant",
            "rows": [22, 23],
            "bit": 19,
            "reported_name": "OPT_VGX_EN",
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:325",
            "coordinate_certainty_percent": 60,
            "feature_sufficiency_certainty_percent": 10,
            "blocker": "coordinate provenance is community-only and vGPU also depends on signed software, licensing and firmware policy",
        }],
    ),
    review_only_profile(
        "review-opengl-conventional",
        "Inventory the proposed conventional OpenGL enable pair without claiming a complete graphics enablement path.",
        [{
            "feature": "OpenGL enable",
            "plane": "conventional-primary-redundant",
            "rows": [6, 7],
            "bit": 18,
            "reported_name": "OPT_OPENGL_EN",
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:326",
            "coordinate_certainty_percent": 60,
            "feature_sufficiency_certainty_percent": 10,
            "blocker": "polarity, sense/control behavior, RIR and the remaining display/graphics policy dependencies are unresolved",
        }],
    ),
    review_only_profile(
        "review-ttu-conventional",
        "Inventory the proposed TTU/poison conventional pair without equating one fuse with a safe poison-policy change.",
        [{
            "feature": "TTU / poison feature readout",
            "plane": "conventional-primary-redundant",
            "rows": [2, 3],
            "bit": 21,
            "reported_effect": "FEATURE_READOUT bit 9 clear",
            "source_revision": "darkmaniac7-cmp170hx-8gb-vfi-tools@f422f80:docs/FUSES.md:328",
            "coordinate_certainty_percent": 55,
            "feature_sufficiency_certainty_percent": 10,
            "blocker": "field identity, polarity, safety consequences, primary/redundant sense and isolated validation are unresolved",
        }],
    ),
]

# The accurate spelling is accepted as an input alias while the historical
# model ID remains stable for signed/staged v1 artifacts.  The report's layered
# capacity projection is authoritative; the legacy ID never forces 80 GiB.
PROFILE_ALIASES = {
    "specimen-hbm-to-defect-ceiling": "specimen-80gb-hbm",
}


PUBLIC_SOURCE_PATHS = {
    "cmpglitch-reported-card": (
        "research-sources/EthanZhangCN-cmp170hx-final-crack/CMPGlitch/cmp_burn.md"
    ),
    "dstoro-plan-2026-11-17": "research-sources/dstoro-2026-11-17-fuse-dumps/plan.json",
}

BUNDLED_PUBLIC_SOURCE_PATHS = {
    "cmpglitch-reported-card": "evidence/public/cmp_burn.md",
    "dstoro-plan-2026-11-17": "evidence/public/dstoro-plan.json",
}

BUNDLED_PUBLIC_LINEAGE_PATH = "evidence/public/public-source-lineage.json"

PUBLIC_SOURCE_METADATA = {
    "cmpglitch-reported-card": {
        "repository_commit": "feb61e250b357cae5acd424a6685b2f27c1e806a",
        "commit_date": "2026-11-21",
        "expected_sha256": "46431a51e57994f1b61edc779071a879c908ab141268e6590f96a29751d65841",
    },
    "dstoro-plan-2026-11-17": {
        "repository_commit": None,
        "source_date": "2026-11-17",
        "note": "retained evidence package; no repository commit is asserted",
        "expected_sha256": "49e43976220319853f7c30a2829e01bd937aa3ed33c0c22bf6d3f5c3438642f2",
    },
}


def load_bundled_public_lineage(package_root):
    """Load hash-only lineage when third-party raw sources are not shipped."""
    path = package_root / BUNDLED_PUBLIC_LINEAGE_PATH
    if not path.is_file():
        raise PlanFailure("missing public-source lineage %s" % path)
    document = load_json_file(path, normalize_legacy=False)
    if (document.get("schema") != "170hx-xray-public-source-lineage-v1" or
            document.get("execution_authorized") is not False or
            document.get("raw_sources_redistributed") is not False):
        raise PlanFailure("public-source lineage policy mismatch")
    sources = document.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(PUBLIC_SOURCE_METADATA):
        raise PlanFailure("public-source lineage inventory mismatch")
    for name, metadata in PUBLIC_SOURCE_METADATA.items():
        source = sources.get(name)
        if (not isinstance(source, dict) or
                source.get("source_sha256") != metadata["expected_sha256"] or
                source.get("raw_source_redistributed") is not False):
            raise PlanFailure("public-source lineage mismatch for %s" % name)
    return path, document


def validate_public_faithful_audit(root, public_hashes):
    current_relative = (
        "research-evidence/170hx-xray-public-plan-audit-2026-11-23.json"
    )
    legacy_relative = (
        "research-evidence/170hx-ray-public-plan-audit-2026-11-23.json"
    )
    package_root = Path(__file__).resolve().parent.parent
    if (root / current_relative).is_file():
        relative = current_relative
        path = root / relative
    elif (root / legacy_relative).is_file():
        relative = legacy_relative
        path = root / relative
    else:
        relative = "evidence/public/public-plan-audit.json"
        path = package_root / relative
    if not path.is_file():
        raise PlanFailure("missing public-faithful audit %s" % path)
    document = load_json_file(path, normalize_legacy=False)
    schema = document.get("schema")
    if schema not in ("170hx-xray-public-plan-audit-v1",
                      LEGACY_PUBLIC_AUDIT_SCHEMA):
        raise PlanFailure("public-faithful audit schema mismatch")
    if document.get("execution_authorized") is not False:
        raise PlanFailure("public-faithful audit is not explicitly non-executable")
    if not all(model.get("status") == "PASS" for model in document.get("models", [])):
        raise PlanFailure("public-faithful audit contains a failed model")
    models = {model.get("name"): model for model in document.get("models", [])}
    for name, source in public_hashes.items():
        model = models.get(name)
        if model is None or model.get("source_sha256") != source["sha256"]:
            raise PlanFailure("public-faithful lineage mismatch for %s" % name)
    recorded_tool_sha = document.get("tool", {}).get("sha256")
    if schema == LEGACY_PUBLIC_AUDIT_SCHEMA:
        auditor_sha = LEGACY_PUBLIC_AUDIT_TOOL_SHA256
    else:
        auditor_path = root / "tools/170hx_plan_source_audit.py"
        auditor_sha = sha256_file(auditor_path)
    if recorded_tool_sha != auditor_sha:
        raise PlanFailure("public-faithful audit tool hash is stale")
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "schema": schema,
        "legacy_input": schema == LEGACY_PUBLIC_AUDIT_SCHEMA,
        "tool": document["tool"],
        "all_models_pass": True,
        "lineage_hashes_match": True,
    }


def effective_block_state(ff_analysis):
    state = {}
    source_rows = {}
    for record in ff_analysis["effective_ff_records"]:
        key = (record["chain"], record["block"])
        state[key] = int(record["data"], 0)
        source_rows[key] = record["row"]
    return state, source_rows


def extract_bits(state, chain, start_bit, width):
    value = 0
    missing_blocks = []
    components = []
    cursor = start_bit
    output_shift = 0
    remaining = width
    while remaining:
        block = cursor // 16
        bit_in_block = cursor % 16
        take = min(remaining, 16 - bit_in_block)
        key = (chain, block)
        block_value = state.get(key, 0)
        if key not in state:
            missing_blocks.append({"chain": chain, "block": block})
        piece = (block_value >> bit_in_block) & ((1 << take) - 1)
        value |= piece << output_shift
        components.append({
            "chain": chain,
            "block": block,
            "bit_in_block": bit_in_block,
            "width": take,
            "piece": piece,
            "effective_record_present": key in state,
        })
        cursor += take
        output_shift += take
        remaining -= take
    return value, missing_blocks, components


def apply_bits(state, chain, start_bit, width, target):
    if target < 0 or target >= (1 << width):
        raise PlanFailure("target 0x%x exceeds %d-bit field" % (target, width))
    changed_blocks = set()
    cursor = start_bit
    input_shift = 0
    remaining = width
    while remaining:
        block = cursor // 16
        bit_in_block = cursor % 16
        take = min(remaining, 16 - bit_in_block)
        key = (chain, block)
        before = state.get(key, 0)
        mask = ((1 << take) - 1) << bit_in_block
        piece = ((target >> input_shift) & ((1 << take) - 1)) << bit_in_block
        after = (before & ~mask) | piece
        if before != after:
            state[key] = after
            changed_blocks.add(key)
        cursor += take
        input_shift += take
        remaining -= take
    return changed_blocks


def encode_ff_record(chain, block, data):
    if not 0 < chain < 31:
        raise PlanFailure("reserved FF chain %d" % chain)
    if not 0 <= block <= 1023 or not 0 <= data <= 0xFFFF:
        raise PlanFailure("invalid FF block/data")
    word = (data << 16) | (block << 6) | chain
    if word & 0x20:
        raise PlanFailure("encoded FF word has IFF type bit set")
    return word


def validate_catalog_alignment(xray):
    catalog = {item["name"]: item for item in xray.LOGICAL_FIELD_CATALOG}
    errors = []
    evidence = {}
    for name, local in sorted(FIELD_SPECS.items()):
        remote = catalog.get(name)
        if remote is None:
            errors.append("XRAY catalog lacks %s" % name)
            continue
        for key in ("chain", "bit", "width"):
            if remote.get(key) != local.get(key):
                errors.append(
                    "%s %s differs: planner=%r XRAY=%r"
                    % (name, key, local.get(key), remote.get(key))
                )
        evidence[name] = {
            "certainty_percent": remote.get("certainty_percent"),
            "sources": remote.get("sources"),
            "certainty_reason": remote.get("certainty_reason"),
            "source_class": remote.get("source_class"),
            "xray_planning_allowed": remote.get("planning_allowed"),
        }
    remote_pcie = {
        item["name"]: item for item in xray.LOGICAL_FIELD_CATALOG
        if item["name"] in (
            "OPT_DISABLE_GEN3_SPEED",
            "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
            "OPT_PCIE_BOOT_GEN23_DISABLE",
            "OPT_PCIE_BOOT_GEN3_DISABLE",
        )
    }
    gen3_remote = remote_pcie.get("OPT_DISABLE_GEN3_SPEED", {})
    if (
        gen3_remote.get("chain") != 2
        or gen3_remote.get("block") != 39
        or gen3_remote.get("bit_in_block") != 9
    ):
        errors.append("XRAY Gen3 coordinate is not chain 2 block 39 bit 9")
    for name in (
        "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
        "OPT_PCIE_BOOT_GEN23_DISABLE",
        "OPT_PCIE_BOOT_GEN3_DISABLE",
    ):
        local = PCIE_BIT_SPECS[name]
        remote = remote_pcie.get(name, {})
        if (
            remote.get("chain") != local["chain"]
            or remote.get("block") != local["block"]
            or remote.get("bit_in_block") != local["bit_in_block"]
        ):
            errors.append("XRAY PCIe coordinate differs for %s" % name)
    if errors:
        raise PlanFailure("catalog alignment failed: " + "; ".join(errors))
    return evidence


def required_live_registers():
    names = set()
    for name, spec in FIELD_SPECS.items():
        live_name = spec.get("live_register", name)
        if live_name:
            names.add(live_name)
        if spec.get("defect"):
            names.add(spec["defect"])
        for defect_name in spec.get("defects", []):
            names.add(defect_name)
        names.update(spec.get("comparison_registers", []))
    for spec in PCIE_BIT_SPECS.values():
        names.add(spec["live_register"])
    names.update(CONTEXT_REGISTER_TO_PHYSICAL_KEY)
    names.update(CURRENT_ONLY_CONTEXT_REGISTERS)
    return sorted(names)


def live_register_value(card, name):
    register = card.get("registers", {}).get(name)
    if not isinstance(register, dict) or register.get("status") != "ok":
        raise PlanFailure("live card lacks successful register %s" % name)
    return parse_int(register.get("raw"), "live register %s" % name)


def validate_live_capture(path, xray, declared_input_format="auto"):
    raw_document, source_sha256 = load_json_file_with_sha256(
        path, normalize_legacy=False
    )
    source_tool = raw_document.get("tool")
    if not isinstance(source_tool, dict):
        raise PlanFailure("live capture lacks immutable source tool provenance")
    detected_input_format = detect_xray_input_format(raw_document, "live")
    enforce_declared_input_format(
        declared_input_format, detected_input_format, LIVE_INPUT_FORMATS, "live"
    )
    document = _normalize_legacy_input(raw_document)
    errors = []
    if document.get("schema") != "170hx-xray-v1":
        errors.append("root schema is not 170hx-xray-v1")
    if document.get("command") not in ("capture", "live") or document.get("status") != "captured":
        errors.append("root is not a completed capture/live artifact")
    tool = document.get("tool", {})
    if tool.get("name") not in ("170HX-RAY", "170HX-XRAY") or not isinstance(
            tool.get("version"), str):
        errors.append("root tool identity/version is absent or invalid")
    tool_sha256 = tool.get("sha256")
    if not isinstance(tool_sha256, str) or len(tool_sha256) != 64:
        errors.append("root tool source SHA-256 is absent or malformed")
    root_access = document.get("access", {})
    if root_access.get("direct_user_mmio_writes") != 0:
        errors.append("root capture reports direct user MMIO writes")
    if root_access.get("helper_operational_mmio_writes") != 0:
        errors.append("root capture reports helper operational writes")
    if root_access.get("controller_transaction_attempted") is True:
        errors.append("root capture attempted a controller transaction")
    cards = {}
    for index, card in enumerate(document.get("cards", [])):
        identity = card.get("card", {})
        bdf = identity.get("domain_bdf")
        if not isinstance(bdf, str) or bdf in cards:
            errors.append("live card %d has missing/duplicate BDF" % index)
            continue
        capture = card.get("capture", {})
        coherence = capture.get("coherence", {})
        access = card.get("access", {})
        if coherence.get("coherent") is not True:
            errors.append("%s live capture is not coherent" % bdf)
        if coherence.get("required_stability_changes"):
            errors.append("%s has required-stability changes" % bdf)
        if coherence.get("read_errors"):
            errors.append("%s has read errors" % bdf)
        if capture.get("passes") != 3 or capture.get("register_profile") != "all-safe":
            errors.append("%s is not a three-pass all-safe capture" % bdf)
        if capture.get("backend") != "170hx-xray-helper-allowlisted-read32":
            errors.append("%s did not use the allowlisted READ32 backend" % bdf)
        if capture.get("direct_user_mmio_writes") != 0:
            errors.append("%s reports direct user MMIO writes" % bdf)
        if capture.get("controller_read_commands") != 0:
            errors.append("%s reports controller read commands in Level 2" % bdf)
        if access.get("effective_access") != "bar-read":
            errors.append("%s live access is not bar-read" % bdf)
        if access.get("helper_operational_mmio_writes") != 0:
            errors.append("%s reports helper operational writes" % bdf)
        if access.get("controller_transaction_attempted") is not False:
            errors.append("%s reports a controller transaction" % bdf)
        if access.get("physical_ff_transaction") != "not_performed":
            errors.append("%s unexpectedly reports a physical FF transaction" % bdf)
        if access.get("rir_transaction") != "not_performed":
            errors.append("%s unexpectedly reports an RIR transaction" % bdf)
        helper_identity = identity.get("xray_helper_identity")
        if not isinstance(helper_identity, dict):
            errors.append("%s lacks helper identity" % bdf)
        else:
            for key in (
                "domain_bdf", "vendor", "device", "revision",
                "subsystem_vendor", "subsystem_device",
            ):
                if helper_identity.get(key) != identity.get(key):
                    errors.append("%s helper/card identity differs at %s" % (bdf, key))
        passes = card.get("raw_passes")
        if not isinstance(passes, list) or len(passes) != 3:
            errors.append("%s lacks three raw passes" % bdf)
            passes = []
        for name in required_live_registers():
            try:
                final = live_register_value(card, name)
            except PlanFailure as exc:
                errors.append("%s: %s" % (bdf, exc))
                continue
            final_register = card["registers"][name]
            current_definition = xray.REGISTER_BY_NAME.get(name)
            if current_definition is None:
                errors.append("current XRAY registry lacks %s" % name)
            else:
                captured_address = parse_int(
                    final_register.get("address"), "%s %s address" % (bdf, name)
                )
                if captured_address != current_definition["address"]:
                    errors.append("%s %s address differs from current registry" % (bdf, name))
                if current_definition.get("capture_safe") is not True:
                    errors.append("current XRAY registry no longer marks %s capture-safe" % name)
            if final_register.get("access_effect") != "pure_read":
                errors.append("%s %s is not marked pure-read" % (bdf, name))
            values = []
            for pass_index, item in enumerate(passes):
                raw = item.get("registers", {}).get(name)
                if not isinstance(raw, dict) or raw.get("status") != "ok":
                    errors.append("%s pass %d lacks %s" % (bdf, pass_index + 1, name))
                    continue
                values.append(parse_int(raw.get("value"), "%s pass %s" % (bdf, name)))
            if len(values) == 3 and (len(set(values)) != 1 or values[0] != final):
                errors.append("%s register %s is not stable/final-equal" % (bdf, name))
        cards[bdf] = card
    if errors:
        raise PlanFailure("live capture validation failed: " + "; ".join(errors))
    return (
        document, cards, copy.deepcopy(source_tool), source_sha256,
        {
            "declared": declared_input_format,
            "detected": detected_input_format,
            "legacy_normalization_applied": detected_input_format.startswith(
                "legacy-ray-"
            ),
        },
    )


def validate_same_capture_register_context(path, xray, expected_bdf):
    """Validate the register half of one certified Level-3 native artifact.

    Unlike ``validate_live_capture``, this path intentionally permits the
    operational FUSEADDR/FUSECTRL/DEBUGCTRL writes needed for the certified
    MAIN/RIR read transaction. It still rejects OTP commands, incomplete
    restoration, unstable ordinary registers, identity drift, or a context
    collected outside the exact physical-capture artifact.
    """
    document = load_json_file(path)
    errors = []
    if document.get("schema") != "170hx-xray-v1":
        errors.append("root schema is not 170hx-xray-v1")
    if document.get("command") not in ("capture", "live") or document.get("status") != "captured":
        errors.append("root is not a completed capture/live artifact")
    tool = document.get("tool", {})
    if tool.get("name") not in ("170HX-RAY", "170HX-XRAY") or not isinstance(
            tool.get("version"), str):
        errors.append("root tool identity/version is absent or invalid")
    tool_sha256 = tool.get("sha256")
    if not isinstance(tool_sha256, str) or len(tool_sha256) != 64:
        errors.append("root tool source SHA-256 is absent or malformed")

    root_access = document.get("access", {})
    if root_access.get("effective_access") != "semantic-read":
        errors.append("root effective access is not semantic-read")
    if root_access.get("controller_snapshot_attempted") is not True:
        errors.append("root did not attest a controller-snapshot attempt")
    if root_access.get("controller_snapshot_performed") is not True:
        errors.append("root did not attest a performed controller snapshot")
    if root_access.get("counts_complete") is not True:
        errors.append("root operational counts are incomplete")
    if root_access.get("otp_program_commands") != 0:
        errors.append("root reports an OTP program command")

    cards = [
        card for card in document.get("cards", [])
        if card.get("card", {}).get("domain_bdf") == expected_bdf
    ]
    if len(cards) != 1:
        errors.append(
            "native artifact must contain exactly one selected %s card" % expected_bdf
        )
        card = {}
    else:
        card = cards[0]

    identity = card.get("card", {})
    helper_identity = identity.get("xray_helper_identity")
    if not isinstance(helper_identity, dict):
        errors.append("%s lacks helper identity" % expected_bdf)
    else:
        for key in (
            "domain_bdf", "vendor", "device", "revision",
            "subsystem_vendor", "subsystem_device",
        ):
            if helper_identity.get(key) != identity.get(key):
                errors.append(
                    "%s helper/card identity differs at %s" % (expected_bdf, key)
                )

    capture = card.get("capture", {})
    coherence = capture.get("coherence", {})
    access = card.get("access", {})
    if coherence.get("coherent") is not True:
        errors.append("%s ordinary-register capture is not coherent" % expected_bdf)
    if coherence.get("required_stability_changes"):
        errors.append("%s has required-stability changes" % expected_bdf)
    if coherence.get("read_errors"):
        errors.append("%s has ordinary-register read errors" % expected_bdf)
    if capture.get("backend") != "170hx-xray-helper-allowlisted-read32":
        errors.append("%s did not use the allowlisted READ32 backend" % expected_bdf)
    if access.get("effective_access") != "semantic-read":
        errors.append("%s card access is not semantic-read" % expected_bdf)
    if access.get("controller_transaction_attempted") is not True:
        errors.append("%s did not attempt its controller transaction" % expected_bdf)
    if access.get("controller_transaction_error") is not None:
        errors.append("%s reports a controller transaction error" % expected_bdf)
    if access.get("counts_complete") is not True:
        errors.append("%s card operational counts are incomplete" % expected_bdf)

    for key in ("physical_ff_transaction", "rir_transaction"):
        transaction = access.get(key)
        if not isinstance(transaction, dict):
            errors.append("%s lacks %s" % (expected_bdf, key))
            continue
        if transaction.get("certified_by_helper") is not True:
            errors.append("%s %s is not helper-certified" % (expected_bdf, key))
        if transaction.get("coherent") is not True:
            errors.append("%s %s is not coherent" % (expected_bdf, key))
        if transaction.get("counts_complete") is not True:
            errors.append("%s %s counts are incomplete" % (expected_bdf, key))
        if transaction.get("address_restored") is not True:
            errors.append("%s %s did not restore FUSEADDR" % (expected_bdf, key))
        if transaction.get("debug_restored") is not True:
            errors.append("%s %s did not restore DEBUGCTRL" % (expected_bdf, key))
        if transaction.get("otp_commands") != 0:
            errors.append("%s %s reports an OTP command" % (expected_bdf, key))
        if transaction.get("entry") != transaction.get("exit"):
            errors.append("%s %s entry/exit context differs" % (expected_bdf, key))

    passes = card.get("raw_passes")
    declared_passes = capture.get("passes")
    if not isinstance(passes, list) or not isinstance(declared_passes, int):
        errors.append("%s lacks ordinary raw passes" % expected_bdf)
        passes = []
    elif declared_passes < 2 or len(passes) != declared_passes:
        errors.append("%s needs at least two complete ordinary passes" % expected_bdf)

    for name in required_live_registers():
        try:
            final = live_register_value(card, name)
        except PlanFailure as exc:
            errors.append("%s: %s" % (expected_bdf, exc))
            continue
        final_register = card["registers"][name]
        current_definition = xray.REGISTER_BY_NAME.get(name)
        if current_definition is None:
            errors.append("current XRAY registry lacks %s" % name)
        else:
            captured_address = parse_int(
                final_register.get("address"), "%s %s address" % (expected_bdf, name)
            )
            if captured_address != current_definition["address"]:
                errors.append(
                    "%s %s address differs from current registry" % (expected_bdf, name)
                )
            if current_definition.get("capture_safe") is not True:
                errors.append("current XRAY registry no longer marks %s capture-safe" % name)
        if final_register.get("access_effect") != "pure_read":
            errors.append("%s %s is not marked pure-read" % (expected_bdf, name))
        values = []
        for pass_index, item in enumerate(passes):
            raw = item.get("registers", {}).get(name)
            if not isinstance(raw, dict) or raw.get("status") != "ok":
                errors.append(
                    "%s pass %d lacks %s" % (expected_bdf, pass_index + 1, name)
                )
                continue
            values.append(
                parse_int(raw.get("value"), "%s pass %s" % (expected_bdf, name))
            )
        if len(values) == len(passes) and (
            len(set(values)) != 1 or values[0] != final
        ):
            errors.append("%s register %s is not stable/final-equal" % (expected_bdf, name))

    if errors:
        raise PlanFailure(
            "same-capture register-context validation failed: " + "; ".join(errors)
        )
    return document, card


def controller_context_crosscheck(physical, live_card, context_mode="cross-date-level2"):
    physical_entry = physical["physical_entry"]
    entries = []
    errors = []
    same_capture = context_mode == "same-capture-level3"
    value_key = (
        "same_capture_register_context" if same_capture else "live_level2"
    )
    comparison = (
        "same-native-artifact-exact-context-check"
        if same_capture else "cross-date-exact-context-check"
    )
    for register_name, physical_key in CONTEXT_REGISTER_TO_PHYSICAL_KEY.items():
        live_value = live_register_value(live_card, register_name)
        if physical_key not in physical_entry:
            entries.append({
                "register": register_name,
                "comparison": "controller-entry-unavailable",
                value_key: hex32(live_value),
            })
            errors.append("physical transaction entry lacks %s" % physical_key)
            continue
        physical_value = parse_int(
            physical_entry[physical_key], "physical entry %s" % physical_key
        )
        match = physical_value == live_value
        entries.append({
            "register": register_name,
            "physical_transaction_entry": hex32(physical_value),
            value_key: hex32(live_value),
            "exact_match": match,
            "comparison": comparison,
        })
        if not match:
            errors.append("%s differs across physical/live captures" % register_name)
    for register_name in CURRENT_ONLY_CONTEXT_REGISTERS:
        live_value = live_register_value(live_card, register_name)
        entries.append({
            "register": register_name,
            "physical_transaction_entry": None,
            value_key: hex32(live_value),
            "comparison": (
                "same-artifact ordinary register; ABI-5 transaction journal does not include it"
                if same_capture else
                "current-only; historical ABI-5 snapshot did not include this register"
            ),
            "exact_match": None,
        })
    values = {
        name: live_register_value(live_card, name)
        for name in list(CONTEXT_REGISTER_TO_PHYSICAL_KEY) + CURRENT_ONLY_CONTEXT_REGISTERS
    }
    iff_raw = values["IFF_RECORD"]
    iff_start = iff_raw & 0x1FF
    iff_last = (iff_raw >> 16) & 0x1FF
    write_window_observed = bool(
        values["DISABLE_SW_OVERRIDE_STATUS"] == 0
        and values["SECURED_BURN_FEATURE_ENABLE"] == 0
        and values["ENABLE_FUSE_PROGRAM_STATUS"] == 1
        and values["FUSECTRL_PRIV_LEVEL_MASK"] == 0xFFFFFFFF
        and values["FUSEWDATA_PRIV_LEVEL_MASK"] == 0xFFFFFFFF
    )
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "entries": entries,
        "essential_state": {
            "iff_start": iff_start,
            "iff_last": iff_last,
            "software_iff_inactive": (values["IFF_SW_FUSING"] & 1) == 0,
            "software_override_inactive": (values["EN_SW_OVERRIDE"] & 1) == 0,
            "main_array_route": (values["DEBUGCTRL"] & 0x2) == 0,
            "crc_h0_h1_clean_0x55": (values["CRC_STATUS"] & 0x77) == 0x55,
            "write_window_observed": write_window_observed,
            "write_window_interpretation": (
                "the current locked capture is not a public Goldilocks/write window"
                if not write_window_observed else
                "window-like status observed; this alone never authorizes programming"
            ),
        },
        "context_mode": context_mode,
        "scope_limit": (
            "same-native-artifact equality binds ordinary registers to the certified "
            "MAIN/RIR transaction, but does not create an official descriptor or prove "
            "electrical programmability"
            if same_capture else
            "cross-date exact equality strengthens continuity but does not prove unique-die "
            "identity or replace a same-session physical recapture"
        ),
    }


def validate_physical_capture(
        xray, path, card_selector=None, declared_input_format="auto"):
    raw_document = load_json_file(path, normalize_legacy=False)
    detected_input_format = detect_xray_input_format(raw_document, "physical")
    enforce_declared_input_format(
        declared_input_format, detected_input_format,
        PHYSICAL_INPUT_FORMATS, "physical",
    )
    source_tool, source_sha256 = immutable_source_tool(path)
    loaded = xray.load_row_passes(
        str(path), card_selector=card_selector, input_format="json"
    )
    if loaded["sha256"] != source_sha256:
        raise PlanFailure(
            "%s changed between immutable provenance and row-image reads" % path
        )
    normalized = [
        xray.normalize_row_pass(row_pass, "reject")[0]
        for row_pass in loaded["passes"]
    ]
    if len(normalized) != 2 or normalized[0] != normalized[1]:
        raise PlanFailure("%s does not contain two identical complete MAIN passes" % path)
    main_attestation = xray.verify_native_capture_attestation(
        loaded.get("selected_capture"), loaded.get("selected_card"),
        "main_ff", normalized, loaded["sha256"],
    )
    if not main_attestation.get("available"):
        raise PlanFailure(
            "%s MAIN native attestation failed: %s"
            % (path, "; ".join(main_attestation.get("errors", [])))
        )
    main_attestation = copy.deepcopy(main_attestation)
    main_attestation["source_tool"] = copy.deepcopy(source_tool)
    embedded_rir = xray.load_embedded_rir(loaded)
    if embedded_rir is None or not embedded_rir.get("coherent_repeated_passes"):
        raise PlanFailure("%s lacks two coherent embedded RIR passes" % path)
    rir_attestation = xray.verify_native_capture_attestation(
        embedded_rir.get("selected_capture"), embedded_rir.get("selected_card"),
        "rir", embedded_rir["passes"], embedded_rir["sha256"],
    )
    if not rir_attestation.get("available"):
        raise PlanFailure(
            "%s RIR native attestation failed: %s"
            % (path, "; ".join(rir_attestation.get("errors", [])))
        )
    rir_attestation = copy.deepcopy(rir_attestation)
    rir_attestation["source_tool"] = copy.deepcopy(source_tool)
    card = loaded["selected_capture"].get("card", {})
    bdf = card.get("domain_bdf")
    if not isinstance(bdf, str):
        raise PlanFailure("%s lacks BDF identity" % path)
    rows = normalized[0]
    rir = embedded_rir["passes"][0]
    rir_analysis = xray.decode_ga100_rir_words(rir)
    if rir_analysis.get("effective_replay_available") is not True:
        raise PlanFailure("%s RIR replay is ambiguous or incomplete" % path)
    ff = xray.decode_ff_iff(rows, FUSELESS_START, FUSELESS_END)
    if not ff.get("ga100_semantically_valid"):
        raise PlanFailure("%s baseline FF/IFF decode is invalid" % path)
    if ff["boundary"].get("iff_start") is None:
        raise PlanFailure("%s has no established IFF boundary" % path)
    return {
        "path": path,
        "sha256": loaded["sha256"],
        "bdf": bdf,
        "card": card,
        "rows": rows,
        "rir": rir,
        "rir_analysis": rir_analysis,
        "ff": ff,
        "main_attestation": main_attestation,
        "rir_attestation": rir_attestation,
        "root_generated_utc": loaded["selected_card"]["root_capture"].get("generated_utc"),
        "root_tool": source_tool,
        "input_format": {
            "declared": declared_input_format,
            "detected": detected_input_format,
            "legacy_normalization_applied": detected_input_format.startswith(
                "legacy-ray-"
            ),
        },
        "physical_entry": dict(
            loaded["selected_capture"]["physical_rows"]["transaction"]["entry"]
        ),
        "physical_exit": dict(
            loaded["selected_capture"]["physical_rows"]["transaction"]["exit"]
        ),
    }


def identity_tuple(identity):
    return tuple(identity.get(key) for key in (
        "domain_bdf", "vendor", "device", "revision",
        "subsystem_vendor", "subsystem_device",
    ))


def field_crosscheck(baseline_state, live_card, field_evidence):
    results = []
    errors = []
    for name, spec in sorted(FIELD_SPECS.items()):
        physical, missing, components = extract_bits(
            baseline_state, spec["chain"], spec["bit"], spec["width"]
        )
        live_name = spec.get("live_register", name)
        live = None
        match = None
        if live_name is not None:
            live = live_register_value(live_card, live_name) & (
                (1 << spec["width"]) - 1
            )
            match = physical == live
        result = {
            "field": name,
            "physical_projection": hex32(physical),
            "live_register_name": live_name,
            "live_register": None if live is None else hex32(live),
            "exact_match": match,
            "implicit_zero_blocks": missing,
            "components": components,
            "coordinate_evidence": field_evidence[name],
            "interpretation": (
                "missing effective FF blocks are projected as logical zero only for this "
                "cross-check; XRAY's authoritative decoder deliberately reports them incomplete"
                if missing else "all contributing blocks have explicit effective FF records"
            ),
        }
        if spec.get("defect"):
            defect = live_register_value(live_card, spec["defect"]) & (
                (1 << spec["width"]) - 1
            )
            result["defect_register"] = spec["defect"]
            result["defect_value"] = hex32(defect)
            result["defects_are_disabled_in_current_mask"] = (physical & defect) == defect
            if (physical & defect) != defect:
                errors.append("%s current mask does not contain its defect mask" % name)
        if spec.get("defects"):
            result["paired_defect_registers"] = {
                defect_name: hex32(live_register_value(live_card, defect_name) & 1)
                for defect_name in spec["defects"]
            }
            pair_names = sorted(
                item_name for item_name, item_spec in FIELD_SPECS.items()
                if item_spec.get("domain") == "media_pair"
            )
            pair_projection = sorted(
                field_value_document(baseline_state, item_name)["value"]
                for item_name in pair_names
            )
            option_projection = sorted(
                live_register_value(live_card, register_name) & 1
                for register_name in spec.get("comparison_registers", [])
            )
            result["unordered_candidate_pair_projection"] = pair_projection
            result["unordered_direct_option_projection"] = option_projection
            result["unordered_pair_exact_match"] = (
                pair_projection == option_projection
            )
            result["individual_attribution"] = "unknown"
            if pair_projection != option_projection and name == pair_names[0]:
                errors.append(
                    "NVJPG/OFA candidate pair does not match the unordered direct option pair"
                )
        if match is False:
            errors.append("%s physical/live mismatch" % name)
        results.append(result)
    for name, spec in sorted(PCIE_BIT_SPECS.items()):
        physical = (baseline_state.get((spec["chain"], spec["block"]), 0)
                    >> spec["bit_in_block"]) & 1
        live_full = live_register_value(live_card, spec["live_register"])
        live = (live_full >> spec["live_bit"]) & 1
        match = physical == live
        results.append({
            "field": name,
            "physical_projection": physical,
            "live_register": spec["live_register"],
            "live_register_bit": spec["live_bit"],
            "live_projection": live,
            "exact_match": match,
            "certainty_percent": spec["certainty_percent"],
            "certainty_reason": spec["reason"],
        })
        if not match:
            errors.append("%s physical/live mismatch" % name)
    return results, errors


def qualified_hbm_density(live_card, override=None):
    """Return a density plus explicit provenance, or fail rather than guess.

    The retained 20c2/1585 specimens are the 8-GiB-per-FBP (4 GiB per
    subpartition) population independently cross-checked by their 64-GiB
    software-visible state.  A 20b5 identity alone does not distinguish a
    40-GiB from an 80-GiB A100, so converted/native A100 inputs require an
    explicit caller override until package or telemetry evidence is captured.
    """
    identity = live_card.get("card", {})
    if override is not None:
        value = float(override)
        if not math.isfinite(value) or value <= 0:
            raise PlanFailure("HBM density override must be positive and finite")
        return value, {
            "kind": "explicit_planner_argument",
            "qualified": True,
            "gib_per_subpartition": value,
            "scope": "caller-qualified specimen; identity alone is insufficient",
        }
    device = parse_int(identity.get("device", 0), "card.device")
    subsystem = parse_int(
        identity.get("subsystem_device", 0), "card.subsystem_device"
    )
    if device == 0x20C2 and subsystem == 0x1585:
        return 4, {
            "kind": "retained_cmp170hx_20c2_1585_population",
            "qualified": True,
            "gib_per_subpartition": 4,
            "basis": (
                "retained four-card 64-GiB state with eight enabled FBPs, "
                "cross-checked against the 8-GiB-per-FBP package population"
            ),
            "does_not_generalize_to_20b5_by_identity": True,
        }
    raise PlanFailure(
        "HBM density cannot be inferred from PCI identity %04x/%04x; pass "
        "--gib-per-subpartition from package/telemetry evidence" % (device, subsystem)
    )


def normalized_topology_targets(xray, live_card, gib_per_subpartition=None):
    density, provenance = qualified_hbm_density(
        live_card, override=gib_per_subpartition
    )
    topology = xray.normalize_ga100_topology(
        live_card.get("registers", {}),
        gib_per_subpartition=density,
        density_provenance=provenance,
    )
    memory = topology["memory"]
    compute = topology["compute"]
    if not memory["target_admissible"]:
        raise PlanFailure(
            "memory defect planes/density/HALF_FBPA invariants do not admit a target: %s"
            % "; ".join(memory["invariant_violations"])
        )
    defect_compute = compute["layers"]["defect"]
    if not defect_compute["assessable"]:
        raise PlanFailure("compute defect topology is incomplete")
    if compute["invariant_violations"]:
        raise PlanFailure(
            "compute defect/disable invariants fail: %s"
            % "; ".join(compute["invariant_violations"])
        )
    defect_memory = memory["layers"]["defect"]["masks"]
    targets = {
        "OPT_GPC_DISABLE": defect_compute["gpc_mask"],
        "OPT_PES_DISABLE": defect_compute["pes_mask"],
        "OPT_FBP_DISABLE": defect_memory["fbp"],
        "OPT_FBPA_DISABLE": defect_memory["fbpa"],
        "OPT_FBIO_DISABLE": defect_memory["fbio"],
        "OPT_ROP_L2_DISABLE": defect_memory["rop_l2"],
    }
    for gpc, value in enumerate(defect_compute["tpc_masks"]):
        targets["OPT_TPC_GPC%d_DISABLE" % gpc] = value
    return topology, targets, density, provenance


def specimen_ceiling_projection(xray, live_card, gib_per_subpartition=None,
                                normalized=None):
    """Project layered topology states without conflating them with policy.

    The XRAY decoder is the shared arithmetic oracle, while this planner retains
    its independently checked FF coordinates.  Refuse to produce an optimistic
    candidate when the 12-bit FBP plane and its 24-bit companion planes do not
    describe the same paired topology.
    """
    def popcount(value):
        return bin(value).count("1")

    if normalized is None:
        topology, _targets, density, provenance = normalized_topology_targets(
            xray, live_card, gib_per_subpartition=gib_per_subpartition
        )
    else:
        topology, density, provenance = normalized
    memory_analysis = topology["memory"]
    compute_analysis = topology["compute"]
    memory_layers = memory_analysis["layers"]
    compute_layers = compute_analysis["layers"]
    defect_compute = compute_layers["defect"]
    disable_compute = compute_layers["disable"]
    effective_compute = compute_layers["effective"]

    gpc_defect = live_register_value(live_card, "GPC_DEFECTIVE") & 0xFF
    per_gpc = []
    per_gpc_layers = []
    for index in range(8):
        tpc_defect = defect_compute["tpc_masks"][index]
        gpc_defective = bool((defect_compute["gpc_mask"] >> index) & 1)
        usable_tpcs = 8 - popcount(tpc_defect)
        per_gpc.append({
            "gpc": index,
            "gpc_defective": gpc_defective,
            "tpc_defect_mask": "0x%02x" % tpc_defect,
            "projected_usable_tpcs": usable_tpcs,
            "projected_sm_at_two_per_tpc": usable_tpcs * 2,
        })
        per_gpc_layers.append({
            "gpc": index,
            "defect_preserving_target_tpcs": usable_tpcs,
            "programmed_disable_layer_tpcs": (
                8 - popcount(disable_compute["tpc_masks"][index])
                if disable_compute["assessable"] else None
            ),
            "effective_runtime_tpcs": (
                8 - popcount(effective_compute["tpc_masks"][index])
                if effective_compute["assessable"] else None
            ),
            "post_target_current_policy_tpcs": (
                8 - popcount(compute_layers["post_target_current_policy"]["tpc_masks"][index])
                if compute_layers["post_target_current_policy"]["assessable"] else None
            ),
            "post_target_reconfig_active_no_override_tpcs": (
                8 - popcount(compute_layers["post_target_reconfig_active_no_override"]["tpc_masks"][index])
                if compute_layers["post_target_reconfig_active_no_override"]["assessable"] else None
            ),
        })
    fbp_defect = live_register_value(live_card, "FBP_DEFECTIVE") & 0xFFF
    usable_fbps = 12 - popcount(fbp_defect)
    target_hbm_gib = memory_layers["defect"]["capacity_gib"]
    nvlink_defect = live_register_value(live_card, "NVLINK_DEFECTIVE") & 0x7
    sys_pipe_defect = live_register_value(live_card, "SYS_PIPE_DEFECTIVE") & 0xFF
    return {
        "compute": {
            "gpc_defect_mask": "0x%02x" % gpc_defect,
            "per_gpc": per_gpc,
            "per_gpc_layers": per_gpc_layers,
            "architecture_maximum_sm": 128,
            "current_effective_sm": effective_compute["sms"],
            "programmed_disable_layer_sm": disable_compute["sms"],
            "defect_preserving_target_sm": defect_compute["sms"],
            "post_target_current_policy_sm": compute_layers[
                "post_target_current_policy"
            ]["sms"],
            "post_target_reconfig_active_no_override_sm": compute_layers[
                "post_target_reconfig_active_no_override"
            ]["sms"],
            "software_override_ceiling_sm": compute_layers[
                "software_override_ceiling"
            ]["sms"],
            "additional_sm_from_effective_to_defect_target": (
                defect_compute["sms"] - effective_compute["sms"]
                if effective_compute["sms"] is not None else None
            ),
            "projected_usable_tpcs": defect_compute["enabled_tpcs"],
            "projected_sm_ceiling": defect_compute["sms"],
            "calculation": (
                "sum(TPCs clear in TPC_DEFECTIVE only for GPCs clear in "
                "GPC_DEFECTIVE) * 2 SM/TPC"
            ),
            "confidence_percent": 96,
            "software_followup": {
                "post_identity_or_vbios_count_predictable_from_current_boot": False,
                "reason": (
                    "A100 VBIOS/GSP and patched drivers may repopulate RECONFIG/override; "
                    "measure after each identity/VBIOS stage and apply EFI/driver "
                    "floorsweep only within the defect-preserving ceiling."
                ),
            },
            "limitations": [
                "a defect mask is a topology ceiling, not a silicon-stability guarantee",
                "GSP/VBIOS/software floorsweep and speed policy can reduce the observed count",
            ],
        },
        "memory": {
            "fbp_defect_mask": "0x%03x" % fbp_defect,
            "projected_usable_fbps": usable_fbps,
            "architecture_maximum_hbm_gib": int(24 * density),
            "current_effective_hbm_gib": memory_layers["effective"]["capacity_gib"],
            "programmed_disable_layer_hbm_gib": memory_layers["disable"]["capacity_gib"],
            "defect_preserving_target_hbm_gib": target_hbm_gib,
            "additional_hbm_gib_from_current_to_target": (
                target_hbm_gib - memory_layers["effective"]["capacity_gib"]
                if memory_layers["effective"]["capacity_gib"] is not None else None
            ),
            "projected_hbm_gib": target_hbm_gib,
            "supported_exact_capacity_classes_gib": [
                int(units * density) for units in (16, 20, 24)
            ],
            "density": memory_analysis["density"],
            "half_fbpa": memory_analysis["half_fbpa"],
            "calculation": (
                "(12 - popcount(FBP_DEFECTIVE[11:0])) * 8 GiB/FBP, "
                "accepted only when FBPA/FBIO/ROP-L2 defect masks equal the "
                "two-bits-per-FBP expansion"
            ),
            "companion_plane_crosscheck": {
                name: layer["companion_matches"]
                for name, layer in memory_layers.items()
            },
            "confidence_percent": 96,
            "limitations": [
                "the selected HBM density is specimen-qualified and must not be inferred from 20b5 identity alone",
                "FBPA/FBIO/ROP-L2 masks, training, VBIOS and memory test remain independent gates",
            ],
        },
        "nvlink": {
            "global_group_defect_mask": "0x%x" % nvlink_defect,
            "projected_nondefective_global_groups": 3 - popcount(nvlink_defect),
            "confidence_percent": 70,
            "limitation": "global groups are not a per-link count and do not prove board/firmware bring-up",
        },
        "system_pipe": {
            "defect_mask": "0x%02x" % sys_pipe_defect,
            "projected_nondefective_bits": 8 - popcount(sys_pipe_defect),
            "confidence_percent": 65,
            "limitation": "the exact architectural meaning and dependencies of each bit are unresolved",
        },
    }


def target_for_field(name, live_card, normalized_targets=None):
    spec = FIELD_SPECS[name]
    if spec["class"] == "disable_mask":
        if normalized_targets is not None and name in normalized_targets:
            return normalized_targets[name] & ((1 << spec["width"]) - 1)
        return live_register_value(live_card, spec["defect"]) & (
            (1 << spec["width"]) - 1
        )
    if spec["class"] == "paired_disable_candidate":
        defects = {
            defect_name: live_register_value(live_card, defect_name) & 1
            for defect_name in spec["defects"]
        }
        if any(defects.values()):
            raise PlanFailure(
                "%s cannot be cleared because individual bit attribution is "
                "unknown and at least one paired feature is defective: %s"
                % (
                    name,
                    ", ".join(
                        "%s=%d" % item for item in sorted(defects.items())
                    ),
                )
            )
        return 0
    if name in IDENTITY_FIELDS:
        return 0x20B5
    raise PlanFailure("no target rule for field %s" % name)


def selected_fields(profile):
    result = []
    for item in profile["stages"]:
        for name in item["fields"]:
            if name not in result:
                result.append(name)
    return result


def selected_pcie_bits(profile):
    result = []
    for item in profile["stages"]:
        for name in item["pcie_bits"]:
            if name not in result:
                result.append(name)
    return result


def field_value_document(state, name):
    spec = FIELD_SPECS[name]
    value, missing, _components = extract_bits(
        state, spec["chain"], spec["bit"], spec["width"]
    )
    return {
        "value": value,
        "value_hex": "0x%0*x" % ((spec["width"] + 3) // 4, value),
        "implicit_zero_blocks": missing,
    }


def iff_signature(ff):
    return canonical_json_sha256([
        {
            "row": record["row"],
            "raw_words": record["raw_words"],
            "opcode": record["opcode"],
            "kind": record["kind"],
        }
        for record in ff["iff_records"]
    ])


def catalog_status_for_fields(xray, ff, rows, names):
    decoded = xray.decode_logical_field_catalog(ff, rows)
    by_name = {item["name"]: item for item in decoded["entries"]}
    return {
        name: {
            "status": by_name[name]["status"],
            "value": by_name[name].get("value"),
            "value_hex": by_name[name].get("value_hex"),
        }
        for name in names
    }


def known_fields_intersecting_block(chain, block):
    start = block * 16
    end = start + 15
    names = []
    for name, spec in FIELD_SPECS.items():
        if spec["chain"] != chain:
            continue
        field_start = spec["bit"]
        field_end = field_start + spec["width"] - 1
        if field_start <= end and field_end >= start:
            names.append(name)
    for name, spec in PCIE_BIT_SPECS.items():
        if spec["chain"] == chain and spec["block"] == block:
            names.append(name)
    return sorted(names)


def dependency_risks(state, target_fields, completed_stage):
    risks = []
    if "OPT_GPC_DISABLE" in target_fields:
        gpc = field_value_document(state, "OPT_GPC_DISABLE")["value"]
        gpc_target = target_fields["OPT_GPC_DISABLE"]
        if gpc == gpc_target:
            for dependency in COMPUTE_PREREQ_FIELDS + ["OPT_ROP_L2_DISABLE"]:
                if dependency not in target_fields:
                    continue
                actual = field_value_document(state, dependency)["value"]
                if actual != target_fields[dependency]:
                    risks.append(
                        "GPC activation reached target before dependency %s" % dependency
                    )
    if "OPT_FBP_DISABLE" in target_fields:
        fbp = field_value_document(state, "OPT_FBP_DISABLE")["value"]
        if fbp == target_fields["OPT_FBP_DISABLE"]:
            for dependency in MEMORY_PREREQ_FIELDS:
                if dependency not in target_fields:
                    continue
                if field_value_document(state, dependency)["value"] != target_fields[dependency]:
                    risks.append(
                        "FBP activation reached target before dependency %s" % dependency
                    )
    if set(MEDIA_PAIR_FIELDS).issubset(target_fields):
        pair_values = [
            field_value_document(state, name)["value"]
            for name in MEDIA_PAIR_FIELDS
        ]
        if pair_values[0] != pair_values[1]:
            risks.append(
                "NVJPG/OFA candidate pair is temporarily asymmetric; individual feature attribution and boot behavior are not qualified"
            )
    if completed_stage:
        return risks
    return risks


def build_operations(profile, baseline_state, live_card, first_row, boundary,
                     normalized_targets=None):
    state = dict(baseline_state)
    operations = []
    selected = selected_fields(profile)
    for name in selected:
        partner = FIELD_SPECS[name].get("must_change_with")
        if partner is not None and partner not in selected:
            raise PlanFailure(
                "%s selects %s without inseparable partner %s"
                % (profile["id"], name, partner)
            )
    target_fields = {
        name: target_for_field(name, live_card, normalized_targets)
        for name in selected
    }
    target_bits = {name: 0 for name in selected_pcie_bits(profile)}
    changed_keys = set()
    stage_errors = []

    for stage_index, stage_spec in enumerate(profile["stages"]):
        before_stage = dict(state)
        touched = set()
        field_masks = {}
        for name in stage_spec["fields"]:
            spec = FIELD_SPECS[name]
            changed = apply_bits(
                state, spec["chain"], spec["bit"], spec["width"],
                target_fields[name],
            )
            touched.update(changed)
            for key in changed:
                field_masks.setdefault(key, []).append(name)
        for name in stage_spec["pcie_bits"]:
            spec = PCIE_BIT_SPECS[name]
            key = (spec["chain"], spec["block"])
            before = state.get(key, 0)
            after = before & ~(1 << spec["bit_in_block"])
            if after != before:
                state[key] = after
                touched.add(key)
                field_masks.setdefault(key, []).append(name)
        for raw in stage_spec["raw_blocks"]:
            key = (raw["chain"], raw["block"])
            before = state.get(key, 0)
            if before != raw["expected"]:
                stage_errors.append(
                    "%s expects %d:%d=0x%04x after earlier stages; found 0x%04x"
                    % (stage_spec["name"], key[0], key[1], raw["expected"], before)
                )
                continue
            if before != raw["target"]:
                state[key] = raw["target"]
                touched.add(key)
                field_masks.setdefault(key, []).append("WHOLE_WORD_EMPIRICAL")

        for key in sorted(touched):
            chain, block = key
            if key not in baseline_state:
                raise PlanFailure(
                    "%s would change chain %d block %d without a prior effective "
                    "FF record; zero is not evidence for unknown co-resident bits"
                    % (profile["id"], chain, block)
                )
            old = before_stage.get(key, baseline_state.get(key, 0))
            # If another touched key was emitted first, it does not affect this key.
            new = state[key]
            row = first_row + len(operations)
            if row >= boundary or row in QUARANTINED_ROWS:
                raise PlanFailure(
                    "%s operation allocation reaches boundary/quarantined row %d"
                    % (profile["id"], row)
                )
            word = encode_ff_record(chain, block, new)
            operations.append({
                "index": len(operations),
                "stage_index": stage_index,
                "stage": stage_spec["name"],
                "stage_purpose": stage_spec.get("purpose"),
                "row": row,
                "expected_physical_old_word": "0x00000000",
                "proposed_physical_word": hex32(word),
                "chain": chain,
                "block": block,
                "logical_block_old": hex16(old),
                "logical_block_new": hex16(new),
                "logical_cleared_bits": hex16(old & ~new),
                "logical_set_bits": hex16(new & ~old),
                "fields_intentionally_changed": sorted(field_masks.get(key, [])),
                "unmapped_bits_preserved": "WHOLE_WORD_EMPIRICAL" not in field_masks.get(key, []),
                "ordering_reason": stage_spec.get("purpose") or stage_spec["name"],
                "executable": False,
            })
            before_stage[key] = new
            changed_keys.add(key)
    return operations, state, target_fields, target_bits, changed_keys, stage_errors


def build_prefixes(xray, baseline_rows, baseline_state, operations,
                   target_fields, target_bits, boundary):
    rows = list(baseline_rows)
    state = dict(baseline_state)
    baseline_ff = xray.decode_ff_iff(rows, FUSELESS_START, FUSELESS_END)
    baseline_iff = iff_signature(baseline_ff)
    selected = sorted(target_fields)
    prefixes = []

    def snapshot(prefix_count, operation):
        ff = xray.decode_ff_iff(rows, FUSELESS_START, FUSELESS_END)
        errors = []
        if not ff.get("structurally_complete"):
            errors.extend(ff.get("structural_errors", []))
        if not ff.get("ga100_semantically_valid"):
            errors.extend(ff.get("validation_errors", []))
        current_iff = iff_signature(ff)
        if current_iff != baseline_iff:
            errors.append("active IFF record sequence changed")
        field_values = {}
        targets_reached = []
        for name, target in sorted(target_fields.items()):
            value_doc = field_value_document(state, name)
            value_doc["target"] = target
            value_doc["target_hex"] = "0x%x" % target
            value_doc["at_target"] = value_doc["value"] == target
            if value_doc["at_target"]:
                targets_reached.append(name)
            field_values[name] = value_doc
        bit_values = {}
        for name, target in sorted(target_bits.items()):
            spec = PCIE_BIT_SPECS[name]
            value = (
                state.get((spec["chain"], spec["block"]), 0)
                >> spec["bit_in_block"]
            ) & 1
            bit_values[name] = {"value": value, "target": target, "at_target": value == target}
        identity_values = {
            name: field_value_document(state, name)["value"]
            for name in IDENTITY_FIELDS
        }
        identity_consistent = identity_values[IDENTITY_FIELDS[0]] == identity_values[IDENTITY_FIELDS[1]]
        semantic_risks = dependency_risks(state, target_fields, operation is None)
        if not identity_consistent:
            semantic_risks.append(
                "DEVIDA/DEVIDB are temporarily inconsistent; boot/firmware behavior is not qualified"
            )
        prefixes.append({
            "prefix_operation_count": prefix_count,
            "last_operation": None if operation is None else {
                "index": operation["index"], "row": operation["row"],
                "stage": operation["stage"],
            },
            "complete_image_u32le_sha256": packed_u32_sha256(rows),
            "ff_structurally_complete": ff.get("structurally_complete"),
            "ga100_semantically_valid": ff.get("ga100_semantically_valid"),
            "decoder_errors": errors,
            "boundary": ff.get("boundary"),
            "capacity": ff.get("capacity_snapshot"),
            "active_iff_signature": current_iff,
            "active_iff_unchanged": current_iff == baseline_iff,
            "selected_field_values": field_values,
            "selected_pcie_bit_values": bit_values,
            "targets_reached": targets_reached,
            "identity_pair": {
                "devida": "0x%04x" % identity_values[IDENTITY_FIELDS[0]],
                "devidb": "0x%04x" % identity_values[IDENTITY_FIELDS[1]],
                "consistent": identity_consistent,
            },
            "semantic_risks": semantic_risks,
            "catalog_decoder": catalog_status_for_fields(xray, ff, rows, selected),
            "bootability": "not-established-by-offline-prefix-analysis",
            "structural_pass": not errors,
        })

    snapshot(0, None)
    for operation in operations:
        row = operation["row"]
        if rows[row] != 0:
            raise PlanFailure("operation row %d is not physically blank" % row)
        word = int(operation["proposed_physical_word"], 0)
        if 0 & ~word:
            raise PlanFailure("impossible physical monotonicity failure")
        rows[row] = word
        state[(operation["chain"], operation["block"])] = int(
            operation["logical_block_new"], 0
        )
        snapshot(operation["index"] + 1, operation)
    return prefixes, rows, state


def build_recovery(baseline_state, final_state, operations, first_free, boundary):
    changed = sorted({(item["chain"], item["block"]) for item in operations})
    recovery = []
    row = first_free
    for chain, block in changed:
        while row in QUARANTINED_ROWS:
            row += 1
        if row >= boundary:
            raise PlanFailure("recovery allocation reaches active IFF boundary")
        old = baseline_state.get((chain, block), 0)
        current = final_state.get((chain, block), 0)
        recovery.append({
            "row": row,
            "chain": chain,
            "block": block,
            "logical_block_old_at_recovery": hex16(current),
            "logical_block_restored": hex16(old),
            "proposed_physical_word": hex32(encode_ff_record(chain, block, old)),
            "expected_physical_old_word": "0x00000000",
            "executable": False,
        })
        row += 1
    remaining = [
        index for index in range(row, boundary)
        if index not in QUARANTINED_ROWS
    ]
    return {
        "model_type": "recovery",
        "kind": "append-only-logical-restoration",
        "records": recovery,
        "record_count": len(recovery),
        "first_row_after_recovery": row,
        "remaining_unquarantined_rows": len(remaining),
        "required_additional_margin_rows": RECOVERY_MARGIN_ROWS,
        "margin_satisfied": len(remaining) >= RECOVERY_MARGIN_ROWS,
        "limitations": [
            "restoration appends more OTP records and does not erase programmed rows",
            "recovery requires a functioning externally accessible controller window",
            "a non-booting identity prefix may require out-of-band recovery",
            "recovery records are not electrically qualified or executable",
        ],
    }


def profile_blockers(profile, stage_errors, operations, prefixes, recovery,
                     pairing_authority, context_mode="cross-date-level2"):
    blockers = [
        "no official GA100 FF descriptor was recovered; coordinates remain evidence-backed hypotheses",
    ]
    if context_mode == "same-capture-level3":
        blockers.extend([
            "the same native artifact binds MAIN, RIR and required ordinary registers, but it remains unsigned operator-collected evidence",
            "a fresh final pre-pulse capture must still reproduce this exact image and identity after the electrical path is qualified",
        ])
    else:
        blockers.extend([
            "physical MAIN and live registers were captured on different dates",
            "same BDF/PCI tuple is not a cryptographic die identity: %s" % pairing_authority,
            "the historical F/gap can be stale; a new same-session complete MAIN/RIR capture must reproduce every baseline byte before any execution review",
        ])
    blockers.extend([
        "program-source voltage, pulse, controller and cell behavior are not electrically qualified",
        "no prefix is proven cold-bootable by offline decoding",
        "the independent offline oracle checks structure and replay only; no independent electrical/execution review exists",
        "no OTP execution is authorized",
    ])
    if profile.get("review_only"):
        blockers.append(
            "review-only profile has no physical operations and is categorically absent from compiler/runtime execution allowlists"
        )
        blockers.extend(
            subject["blocker"] for subject in profile["review_only"]["subjects"]
        )
    blockers.extend(stage_errors)
    if any(
        "WHOLE_WORD_EMPIRICAL" in operation["fields_intentionally_changed"]
        for operation in operations
    ):
        blockers.append(
            "A100-exact MAGIC_D whole-word mapping was empirical on another card; perform a volatile per-card differential first"
        )
    if any(name in selected_fields(profile) for name in IDENTITY_FIELDS):
        blockers.extend([
            "identity changes create an unavoidable one-record DEVIDA/DEVIDB mismatch prefix",
            "signed VBIOS compatibility, flash sequencing and hardware recovery are separate prerequisites",
            "post-conversion 0x20b5 read support is statically implemented but not live-validated on a converted specimen",
        ])
    if "OPT_NVLINK_DISABLE" in selected_fields(profile):
        blockers.append(
            "global NVLink disable is necessary at most; per-link, board, ROM and firmware prerequisites remain unresolved"
        )
    if "OPT_SYS_PIPE_DISABLE" in selected_fields(profile):
        blockers.append(
            "SYS_PIPE coordinate correlation does not establish feature sufficiency or safe topology dependencies"
        )
    if not recovery["margin_satisfied"]:
        blockers.append("recovery margin is not satisfied")
    if any(not prefix["structural_pass"] for prefix in prefixes):
        blockers.append("one or more prefixes fail structural decoder validation")
    return blockers


def validation_lifecycle(profile):
    fields = set(selected_fields(profile))
    pcie_selected = bool(selected_pcie_bits(profile)) or any(
        item["raw_blocks"] for item in profile["stages"]
    )
    feature_checks = []
    if fields.intersection(
        set(MEMORY_PREREQ_FIELDS + MEMORY_ACTIVATION_FIELDS)
    ):
        feature_checks.extend([
            "confirm reported HBM capacity and partition topology after a cold boot",
            "run full-address memory integrity, ECC-off/on as applicable, and sustained thermal/error telemetry",
        ])
    if fields.intersection(
        set(COMPUTE_PREREQ_FIELDS + COMPUTE_ACTIVATION_FIELDS)
    ):
        feature_checks.extend([
            "compare GPC/TPC/PES/SM enumeration against the specimen ceiling projection",
            "stress every exposed SM class and monitor Xid, ECC and thermal behavior",
        ])
    if "OPT_NVLINK_DISABLE" in fields:
        feature_checks.extend([
            "do not infer link availability from the global group option alone",
            "validate board modification, per-link masks, firmware policy, training and peer traffic separately",
        ])
    if pcie_selected:
        feature_checks.extend([
            "cold-enumerate endpoint and root-port link capabilities before retraining",
            "if using the experimental runtime ladder, request Gen3 at both ends, "
            "prove 8 GT/s at both ends, replay E1/equalization, then request the "
            "target at both ends; LINK_CONFIG[19:18] is reverse encoded",
            "validate negotiated width/speed, AER/Xid counters and sustained bidirectional transfers",
        ])
    if fields.intersection(set(IDENTITY_FIELDS)):
        feature_checks.extend([
            "verify both fused identity shadows agree before any in-band VBIOS action",
            "retain an external SPI recovery path and a byte-verified original ROM",
            "re-audit driver recipe selection and software floorsweep under 0x20b5",
        ])
    if "OPT_SYS_PIPE_DISABLE" in fields:
        feature_checks.append(
            "inventory every engine/topology/status delta; no feature is inferred from SYS_PIPE alone"
        )
    return {
        "execution_state": "design-only-not-authorized",
        "same_session_preflight": [
            "recapture two identical complete locked-controller MAIN passes and two identical RIR passes",
            "reproduce the plan's exact baseline MAIN/RIR hashes and PCI identity",
            "capture stable direct option, defect, controller, privilege and IFF-execution registers",
            "prove every selected append row is physically zero and below the observed active IFF boundary",
            "prove software IFF redirection is inactive during physical-state attestation",
            "independently review the complete image, every prefix and recovery allocation",
        ],
        "unresolved_controller_preparation": {
            "status": "blocked",
            "reason": (
                "public VFI tooling and recovered production flow disagree about software "
                "override during physical programming; no hybrid sequence is adopted"
            ),
            "observations_required_if_later_resolved": [
                "controller idle/sense complete", "CRC H0/H1 clean",
                "DSOV/EFP/SBFE state", "FUSECTRL/FUSEWDATA/DEBUG/OPT privilege masks",
                "DEBUGCTRL.RWL routes MAIN rather than RIR", "software-IFF state",
            ],
        },
        "per_record_invariants_for_future_review": [
            "revalidate controller/context and exact blank target row before the one attempted operation",
            "never retry a partial, timeout or mismatched operation",
            "read back the physical row, re-read controller status, and replay the entire candidate image",
            "stop on any unexpected bit, boundary, IFF, RIR, field, privilege or identity change",
        ],
        "cleanup_and_latch_checks": [
            "restore every changed volatile controller/debug/PCI/runtime-power value and verify restoration",
            "do not treat a warm resense or warm reset as proof of persistent cold behavior",
            "perform a controlled cold power cycle, then reacquire MAIN, RIR, IFF and direct shadows",
            "compare the cold image to the exact proposed image before feature testing",
        ],
        "feature_validation": feature_checks,
    }


def profile_topology_outcome(xray, live_card, final_state, profile,
                             density, density_provenance):
    """Re-evaluate topology after the profile's logical option-mask result.

    Effective-status registers are intentionally retained as a *current boot*
    reference.  They are not rewritten to fabricate a post-reset observation;
    the programmed-disable layer is the only direct profile outcome, while
    software-visible enumeration remains an explicit measurement gate.
    """
    stable = {}
    for name, reading in live_card.get("registers", {}).items():
        stable[name] = dict(reading) if isinstance(reading, dict) else reading
    selected = [
        name for name in selected_fields(profile)
        if FIELD_SPECS[name]["class"] == "disable_mask"
    ]
    for name in selected:
        value = field_value_document(final_state, name)["value"]
        previous = stable.get(name, {})
        stable[name] = {
            **(previous if isinstance(previous, dict) else {}),
            "status": "ok",
            "raw": hex32(value),
        }
    outcome = xray.normalize_ga100_topology(
        stable,
        gib_per_subpartition=density,
        density_provenance=density_provenance,
    )
    compute_layers = outcome["compute"]["layers"]
    memory_layers = outcome["memory"]["layers"]

    def compute_summary(name):
        layer = compute_layers[name]
        return {
            "assessable": layer["assessable"],
            "sms": layer["sms"],
            "enabled_tpcs": layer["enabled_tpcs"],
            "gpc_mask": hex32(layer["gpc_mask"]),
            "pes_mask": hex32(layer["pes_mask"]),
            "tpc_masks": ["0x%02x" % value for value in layer["tpc_masks"]],
        }

    def memory_summary(name):
        layer = memory_layers[name]
        return {
            "assessable": layer["assessable"],
            "pairing_exact": layer["pairing_exact"],
            "enabled_subpartitions": layer["enabled_subpartitions"],
            "capacity_gib": layer["capacity_gib"],
            "masks": {
                plane: hex32(value) for plane, value in layer["masks"].items()
            },
        }

    return {
        "selected_topology_fields": selected,
        "programmed_disable_after_profile": {
            "compute": compute_summary("disable"),
            "memory": memory_summary("disable"),
        },
        "physical_defect_ceiling": {
            "compute": compute_summary("defect"),
            "memory": memory_summary("defect"),
        },
        "policy_scenarios_after_topology_target": {
            "current_reconfig_and_override": compute_summary(
                "post_target_current_policy"
            ),
            "reconfig_active_without_override": compute_summary(
                "post_target_reconfig_active_no_override"
            ),
            "software_override_ceiling": compute_summary(
                "software_override_ceiling"
            ),
        },
        "current_effective_status_reference_only": {
            "compute": compute_summary("effective"),
            "memory": memory_summary("effective"),
        },
        "software_visible_post_reset_count": None,
        "software_visible_measurement_required": True,
        "reason": (
            "identity/VBIOS/GSP/EFI/driver policy can repopulate RECONFIG and "
            "override state; a plan cannot manufacture a future status reading"
        ),
        "invariant_violations": {
            "compute": outcome["compute"]["invariant_violations"],
            "memory": outcome["memory"]["invariant_violations"],
        },
    }


def profile_media_outcome(live_card, baseline_state, final_state, profile):
    """Describe media/MIG masks without claiming future enumeration.

    The direct defect words, not STATUS==DISABLE, define the per-die ceiling.
    Bits 146 and 371 are only meaningful as an unordered pair until an
    individual-attribution experiment exists.
    """
    selected = set(selected_fields(profile))

    def raw(name, mask):
        return live_register_value(live_card, name) & mask

    nvdec_defect = raw("OPT_NVDEC_DEFECTIVE", 0x1F)
    nvjpg_defect = raw("OPT_NVJPG_DEFECTIVE", 0x1)
    ofa_defect = raw("OPT_OFA_DEFECTIVE", 0x1)
    sys_pipe_defect = raw("SYS_PIPE_DEFECTIVE", 0xFF)
    nvdec_final = field_value_document(
        final_state, "OPT_NVDEC_DISABLE"
    )["value"]
    sys_pipe_final = field_value_document(
        final_state, "OPT_SYS_PIPE_DISABLE"
    )["value"]
    pair_names = MEDIA_PAIR_FIELDS
    pair_before = sorted(
        field_value_document(baseline_state, name)["value"]
        for name in pair_names
    )
    pair_after = sorted(
        field_value_document(final_state, name)["value"]
        for name in pair_names
    )
    direct_pair = sorted([
        raw("OPT_NVJPG_DISABLE", 0x1), raw("OPT_OFA_DISABLE", 0x1)
    ])
    return {
        "selected_media_fields": sorted(
            name for name in selected
            if FIELD_SPECS[name]["domain"] in (
                "media_decode", "media_pair", "system_topology"
            )
        ),
        "direct_defect_ceiling": {
            "nvdec_instances": 5 - popcount(nvdec_defect),
            "nvjpg_instances": 1 - nvjpg_defect,
            "ofa_instances": 1 - ofa_defect,
            "system_pipe_bits": 8 - popcount(sys_pipe_defect),
            "nvenc_instances": None,
            "raw": {
                "OPT_NVDEC_DEFECTIVE": hex32(nvdec_defect),
                "OPT_NVJPG_DEFECTIVE": hex32(nvjpg_defect),
                "OPT_OFA_DEFECTIVE": hex32(ofa_defect),
                "SYS_PIPE_DEFECTIVE": hex32(sys_pipe_defect),
            },
        },
        "projected_disable_masks_after_profile": {
            "OPT_NVDEC_DISABLE": hex32(nvdec_final),
            "OPT_SYS_PIPE_DISABLE": hex32(sys_pipe_final),
            "NVJPG_OFA_candidate_pair_unordered": pair_after,
        },
        "candidate_pair_crosscheck": {
            "before_unordered_physical_bits": pair_before,
            "current_unordered_direct_options": direct_pair,
            "before_matches_current_direct_options": pair_before == direct_pair,
            "after_unordered_physical_bits": pair_after,
            "individual_bit_attribution": "not-yet-supported",
            "pair_clear_allowed_only_when_both_direct_defects_zero": True,
        },
        "post_reset_enumeration": None,
        "measurement_required": True,
        "boundaries": [
            "NVENC has no recovered physical coordinate and is never changed",
            "SYS_PIPE is evidence for a reported MIG result, not proof that every bit is a MIG instance",
            "PTOP, GSP, VBIOS, power, HS/ucode and authoritative codec APIs remain validation gates",
        ],
    }


def build_profile(xray, profile, baseline_rows, baseline_state, live_card,
                  field_evidence, boundary, first_row, pairing_authority,
                  rir_words, rir_analysis,
                  context_mode="cross-date-level2", normalized_targets=None,
                  density=None, density_provenance=None):
    operations, intended_state, target_fields, target_bits, changed_keys, stage_errors = (
        build_operations(
            profile, baseline_state, live_card, first_row, boundary,
            normalized_targets=normalized_targets,
        )
    )
    prefixes, proposed_rows, final_state = build_prefixes(
        xray, baseline_rows, baseline_state, operations,
        target_fields, target_bits, boundary,
    )
    if final_state != intended_state:
        raise PlanFailure("%s prefix replay differs from intended state" % profile["id"])
    recovery = build_recovery(
        baseline_state, final_state, operations,
        first_row + len(operations), boundary,
    )
    recovery_by_key = {
        (item["chain"], item["block"]): item for item in recovery["records"]
    }
    for operation in operations:
        prefix = prefixes[operation["index"] + 1]
        intentional = set(operation["fields_intentionally_changed"])
        co_resident = [
            name for name in known_fields_intersecting_block(
                operation["chain"], operation["block"]
            ) if name not in intentional
        ]
        evidence = []
        for name in operation["fields_intentionally_changed"]:
            if name in field_evidence:
                evidence.append({"field": name, **field_evidence[name]})
            elif name in PCIE_BIT_SPECS:
                evidence.append({
                    "field": name,
                    "certainty_percent": PCIE_BIT_SPECS[name]["certainty_percent"],
                    "certainty_reason": PCIE_BIT_SPECS[name]["reason"],
                    "sources": ["XRAY PCIe coordinate catalog and retained differential evidence"],
                })
            elif name == "WHOLE_WORD_EMPIRICAL":
                evidence.append({
                    "field": name,
                    "certainty_percent": 65,
                    "certainty_reason": (
                        "reported functional single-card MAGIC_D differential; "
                        "not independently isolated on these four cards"
                    ),
                    "sources": ["hash-bound CMPGlitch report", "hash-bound Danylo plan"],
                })
        recovery_record = recovery_by_key[(operation["chain"], operation["block"])]
        operation.update({
            "known_co_resident_fields": co_resident,
            "coordinate_and_target_evidence": evidence,
            "prefix_complete_image_u32le_sha256": prefix["complete_image_u32le_sha256"],
            "prefix_decoder_structural_pass": prefix["structural_pass"],
            "prefix_active_iff_unchanged": prefix["active_iff_unchanged"],
            "immediate_verification_required": {
                "read_back_exact_target_row": True,
                "redecode_complete_image": True,
                "retry_on_partial_or_mismatch": False,
            },
            "cold_latch_verification_required": {
                "separate_power_cycle": True,
                "reread_complete_MAIN_and_RIR": True,
                "recompare_direct_option_and_defect_registers": True,
                "feature_validation_is_separate": True,
            },
            "recovery_alternative": {
                "append_row": recovery_record["row"],
                "logical_restore_word": recovery_record["proposed_physical_word"],
                "not_guaranteed_reachable": True,
            },
        })
    final_ff = xray.decode_ff_iff(proposed_rows, FUSELESS_START, FUSELESS_END)
    field_targets = []
    implicit_projection_fields = []
    for name, target in sorted(target_fields.items()):
        spec = FIELD_SPECS[name]
        current_document = field_value_document(baseline_state, name)
        final_document = field_value_document(final_state, name)
        current = current_document["value"]
        final = final_document["value"]
        if current_document["implicit_zero_blocks"] or final_document["implicit_zero_blocks"]:
            implicit_projection_fields.append(name)
        defect = None
        defect_preserved = None
        paired_defects = None
        if spec.get("defect"):
            defect = live_register_value(live_card, spec["defect"]) & (
                (1 << spec["width"]) - 1
            )
            defect_preserved = (final & defect) == defect
        if spec.get("defects"):
            paired_defects = {
                defect_name: live_register_value(live_card, defect_name) & 1
                for defect_name in spec["defects"]
            }
            defect_preserved = not any(paired_defects.values())
        field_targets.append({
            "field": name,
            "current": "0x%x" % current,
            "current_implicit_zero_blocks": current_document["implicit_zero_blocks"],
            "target": "0x%x" % target,
            "final": "0x%x" % final,
            "final_implicit_zero_blocks": final_document["implicit_zero_blocks"],
            "implicit_zero_projection_authority": (
                "corroborated by exact stable live option equality; not promoted to an authoritative FF decode"
                if current_document["implicit_zero_blocks"] or final_document["implicit_zero_blocks"]
                else "not used"
            ),
            "target_reached": final == target,
            "target_is_subset_of_current_disable_mask": (
                (target & ~current) == 0
                if spec["class"] in ("disable_mask", "paired_disable_candidate")
                else None
            ),
            "defect": None if defect is None else "0x%x" % defect,
            "paired_direct_defects": paired_defects,
            "all_observed_defects_remain_disabled": defect_preserved,
            "coordinate_evidence": field_evidence[name],
        })
    raw_rows = [hex32(value) for value in proposed_rows]
    blockers = profile_blockers(
        profile, stage_errors, operations, prefixes, recovery, pairing_authority,
        context_mode=context_mode,
    )
    for name in sorted(set(implicit_projection_fields)):
        blockers.append(
            "%s target/current projection treats absent effective FF blocks as zero; "
            "stable live-shadow equality corroborates this specimen but XRAY correctly "
            "keeps the catalog decode incomplete" % name
        )
    return {
        "model_id": profile["id"],
        "model_type": profile["model_type"],
        "public_source": profile.get("public_source"),
        "intent": profile["intent"],
        "completeness_state": (
            "review-only-no-physical-operation"
            if profile.get("review_only")
            else (
                "prefix-analyzed" if not stage_errors
                else "specimen-qualified-blocked"
            )
        ),
        "execution_authorized": False,
        "executable": False,
        "review_only": copy.deepcopy(profile.get("review_only")),
        "stages": profile["stages"],
        "field_targets": field_targets,
        "pcie_bit_targets": [
            {"field": name, "target": 0, **PCIE_BIT_SPECS[name]}
            for name in sorted(target_bits)
        ],
        "operations": operations,
        "operation_count": len(operations),
        "changed_logical_blocks": ["%d:%d" % key for key in sorted(changed_keys)],
        "baseline_complete_image_u32le_sha256": packed_u32_sha256(baseline_rows),
        "proposed_complete_image_u32le_sha256": packed_u32_sha256(proposed_rows),
        "proposed_complete_image_rows": raw_rows,
        "physical_transition_proof": {
            "all_target_rows_were_zero": all(
                baseline_rows[item["row"]] == 0 for item in operations
            ),
            "all_operations_are_zero_to_programmed_word": True,
            "no_existing_physical_row_is_modified": True,
            "logical_clear_is_last-record-wins_not_physical_erasure": True,
        },
        "allocation_policy": {
            "policy": "first-fit-contiguous-from-measured-F",
            "first_operation_row": first_row,
            "last_operation_row": (
                operations[-1]["row"] if operations else first_row - 1
            ),
            "active_iff_boundary_B": boundary,
            "quarantined_rows": sorted(QUARANTINED_ROWS),
            "tail_first_allocation_rejected": True,
            "tail_first_rejection_reason": (
                "a retained cold-failure capture proves a late row can override an "
                "earlier correction for the same block"
            ),
            "canary_or_noop_records_forbidden": True,
            "alternative_low_front_placement": (
                "equivalent only after a new complete capture proves the selected rows blank"
            ),
        },
        "prefixes": prefixes,
        "every_prefix_structurally_valid": all(
            prefix["structural_pass"] for prefix in prefixes
        ),
        "active_iff_unchanged_at_every_prefix": all(
            prefix["active_iff_unchanged"] for prefix in prefixes
        ),
        "rir_preservation_proof": {
            "baseline_rir_u32le_sha256": packed_u32_sha256(rir_words),
            "proposed_rir_u32le_sha256": packed_u32_sha256(rir_words),
            "physical_rir_words_changed": False,
            "effective_replay_available": rir_analysis.get("effective_replay_available"),
            "effective_repairs": rir_analysis.get("effective_repairs"),
            "duplicate_address_groups": rir_analysis.get("duplicate_address_groups"),
            "repairs_target_fuseless_interval": any(
                FUSELESS_START <= item.get("target_row", -1) <= FUSELESS_END
                for item in rir_analysis.get("effective_repairs", [])
            ),
            "interpretation": (
                "RIR is preserved byte-for-byte; its effective conventional-fuse repair "
                "view is recorded separately and never applied to FF/IFF records"
            ),
        },
        "topology_outcome": profile_topology_outcome(
            xray, live_card, final_state, profile,
            density, density_provenance,
        ),
        "media_and_mig_outcome": profile_media_outcome(
            live_card, baseline_state, final_state, profile
        ),
        "final_capacity": final_ff["capacity_snapshot"],
        "quarantined_rows": sorted(QUARANTINED_ROWS),
        "recovery": recovery,
        "post_operation_validation_lifecycle": validation_lifecycle(profile),
        "blockers": blockers,
        "claims_not_made": [
            "bootability", "electrical programmability", "feature sufficiency",
            "silicon stability", "VBIOS compatibility", "execution safety",
        ],
    }


def dual_path_plan_models(projection, profiles):
    """Compose per-specimen multi-session paths from separately sealed profiles."""
    by_id = {item["model_id"]: item for item in profiles}

    def component(profile_id):
        profile = by_id.get(profile_id)
        return {
            "profile_id": profile_id,
            "present_in_this_report": profile is not None,
            "proposed_image_sha256": (
                profile.get("proposed_complete_image_u32le_sha256")
                if profile else None
            ),
            "operation_count": profile.get("operation_count") if profile else None,
            "execution_authorized": False,
            "regenerate_from_exact_current_capture_before_use": True,
        }

    common_target = {
        "hbm_gib": projection["memory"]["defect_preserving_target_hbm_gib"],
        "sm_defect_ceiling": projection["compute"]["defect_preserving_target_sm"],
        "sm_with_current_reconfig_retained_no_override": projection["compute"][
            "post_target_reconfig_active_no_override_sm"
        ],
        "current_hbm_gib": projection["memory"]["current_effective_hbm_gib"],
        "current_sm": projection["compute"]["current_effective_sm"],
        "density_provenance": projection["memory"]["density"],
        "functional_health_proven_by_masks": False,
    }
    common_preflight = {
        "purpose": (
            "Bind the plan to one die and one exact controller/FF/RIR/context "
            "state before any irreversible operation."
        ),
        "why_here": (
            "BDF, option shadows and free-row locations can change after reset, "
            "glitching or identity conversion."
        ),
        "required_checks": [
            "two identical complete native MAIN reads and two identical RIR reads",
            "exact full-domain PCI identity and helper-generation binding",
            "controller idle, parsed CRC clean, DSOV held at the reviewed state",
            "software-IFF inactive and DEBUGCTRL/RWL plane explicitly attested",
            "every append and recovery row physically zero below measured B",
            "every prefix replays structurally and preserves undeclared fields",
        ],
        "failure_action": "stop without programming and regenerate nothing implicitly",
    }
    topology_step = {
        "purpose": (
            "Permanently reduce memory and compute disable fields to the same "
            "die's normalized physical-defect closure."
        ),
        "component": component("xray-common-permanent-topology"),
        "why_here": (
            "FBIO/FBPA/ROP-L2 and TPC/PES must precede FBP/GPC activation; "
            "topology is isolated from identity and ROM failures."
        ),
        "ordered_activation_boundaries": [
            "FBIO/FBPA/ROP-L2 before FBP",
            "TPC[0..7]/PES/ROP-L2 before GPC",
            "GPC is the final compute activation field",
        ],
        "target": common_target,
        "required_checks": [
            "per-record physical readback and complete-image replay",
            "no automatic retry after timeout, partial result or mismatch",
            "two matching post-session MAIN/RIR captures before source removal",
            "full power removal and exact cold re-attestation",
            "new-range anti-alias HBM tests and per-GPC/TPC numerical tests",
        ],
        "recovery": (
            "append-only logical restore is only a preplanned held-session "
            "alternative; it is not erasure and may be unreachable after cold boot"
        ),
    }
    pcie_step = {
        "purpose": (
            "Clear only the independently mapped permanent PCIe generation gates; "
            "PHY programming and retraining remain software/firmware work."
        ),
        "component": component("specimen-pcie-gen4-isolated"),
        "why_here": (
            "It follows validated topology and precedes identity so link/PHY faults "
            "are distinguishable from A100 ROM and BAR-aperture faults."
        ),
        "required_checks": [
            "cold endpoint and upstream-root-port LnkCap/LnkSta capture",
            "16-GT/s x16 must be measured under transfer load, not inferred from fuses",
            "AER/Xid/error-rate comparison against the pre-change baseline",
        ],
        "experimental_runtime_ladder": {
            "source_revision": (
                "sayhellotojungle/cmpunlocker@test-gen4-rootport-ladder@"
                "91a7b060:driver/dynfb/hooks/nv_gen4.c:3-74"
            ),
            "authority": (
                "runtime bring-up evidence only; it adds no fuse coordinate "
                "and is not part of the OTP target"
            ),
            "encoding": (
                "endpoint LINK_CONFIG[19:18] is reverse encoded "
                "(0=Gen4..3=Gen1); PCIe LNKCTL2 uses forward encoding"
            ),
            "sequence": [
                "request Gen3 on endpoint and upstream root port",
                "require both ends to hold 8 GT/s",
                "replay E1/equalization with A100-like PHY initialization",
                "request the final target on endpoint and root port",
                "measure both ends and fall back without changing fuse intent",
            ],
            "unresolved": [
                "experimental branch, not default upstream",
                "board signal integrity and root-port policy remain host-specific",
                "success does not prove the permanent fuse recipe",
            ],
        },
        "not_sufficient_by_itself": [
            "board capacitor mode and signal integrity",
            "DevInit/PHY lane configuration", "root-port policy", "link retraining",
        ],
    }

    path_a_steps = [
        {"index": 0, "name": "exact-current-preflight", **common_preflight},
        {"index": 1, "name": "permanent-topology-segment", **topology_step},
        {
            "index": 2,
            "name": "cold-validate-original-20c2-stack",
            "purpose": (
                "Boot the original CMP identity and ROM first and prove the new "
                "physical topology independently of conversion."
            ),
            "why_here": "identity and VBIOS remain unchanged, minimizing failure variables",
            "required_checks": [
                "recapture all topology layers", "memory training and full-range integrity",
                "SM count, per-unit workloads, clocks, power, thermals and repeated cold boots",
            ],
            "advance_only_if": "topology is stable and exact-current evidence is resealed",
        },
        {"index": 3, "name": "isolated-permanent-pcie-gates", **pcie_step},
        {
            "index": 4,
            "name": "fuse-a100-device-identity-last",
            "purpose": "Set both redundant logical device-ID fields to 0x20b5.",
            "component": component("specimen-a100-20b5-identity"),
            "why_here": (
                "Identity changes driver binding and ROM compatibility, so it is "
                "the last OTP segment after topology and PCIe are cold-validated."
            ),
            "required_checks": [
                "both DEVID fields reach 0x20b5", "card enumerates as 10de:20b5",
                "post-conversion XRAY read path works without widening OTP authority",
            ],
            "recovery_limit": "SPI recovery cannot undo an OTP identity transition",
        },
        {
            "index": 5,
            "name": "flash-matching-signed-a100-vbios",
            "purpose": (
                "Install an authentic signed A100 PCIe ROM matching 20b5, the "
                "calculated HBM density, stack/subpartition topology and total "
                "capacity, using an externally recoverable procedure."
            ),
            "why_here": (
                "The A100 ROM is introduced only after hardware identity matches; "
                "mixing an A100 ROM with 20c2 is not the validated boundary."
            ),
            "prerequisites": [
                "two matching original SPI backups", "tested external programmer",
                "host Above-4G/UEFI and target BAR aperture capability",
            ],
            "capacity_compatibility_gate": {
                "calculated_target_hbm_gib": common_target["hbm_gib"],
                "known_public_reference_class": "A100 PCIe 80-GB",
                "rule": (
                    "never substitute an 80-GiB image for a calculated 64- or "
                    "96-GiB topology; stop unless the signed image's memory "
                    "descriptor is independently shown to match the exact target"
                ),
                "match_proven_here": False,
            },
            "required_checks": [
                "byte hash and NVIDIA signature", "read-after-write compare",
                "exact HBM density/topology/capacity descriptor match",
                "rescue-slot/original-ROM recovery rehearsal",
            ],
        },
        {
            "index": 6,
            "name": "boot-a100-native-memory-and-gen4",
            "purpose": (
                "Use the A100 firmware path for native matching HBM geometry and "
                "Gen4 policy, then measure rather than assume both."
            ),
            "expected_if_all_gates_hold": {
                "hbm_gib": common_target["hbm_gib"],
                "pcie": "16 GT/s x16 after link training",
            },
            "required_checks": [
                "BAR allocation", "HBM size/training/integrity", "endpoint+root LnkSta",
                "GSP/SEC2 boot and Xid/AER baseline",
            ],
        },
        {
            "index": 7,
            "name": "pre-gsp-software-sm-continuation",
            "purpose": (
                "Apply an EFI/pre-GSP override or version-pinned driver patch only "
                "for RECONFIG/policy-disabled TPCs within the physical defect ceiling."
            ),
            "why_here": (
                "A100 VBIOS/GSP can retain a lower policy count even after permanent "
                "disable masks are corrected; current RECONFIG values do not predict "
                "the future firmware's exact count."
            ),
            "bounds": {
                "never_exceed_sm": common_target["sm_defect_ceiling"],
                "illustrative_current_reconfig_layer": common_target[
                    "sm_with_current_reconfig_retained_no_override"
                ],
            },
            "required_checks": [
                "measure pre-patch SM/TPC topology", "verify override precedes GSP consumption",
                "per-SM numerical/stability tests", "separate FMA-rate patch verification",
            ],
        },
    ]

    path_b_steps = [
        {"index": 0, "name": "exact-current-preflight", **common_preflight},
        {"index": 1, "name": "permanent-topology-segment", **topology_step},
        {
            "index": 2,
            "name": "cold-validate-and-retain-20c2-cmp-stack",
            "purpose": (
                "Keep device ID 20c2 and the original CMP ROM, then measure the "
                "permanently corrected memory/compute option shadows."
            ),
            "why_here": "this is the final identity/VBIOS branch, not an intermediate conversion",
            "required_checks": [
                "both device-ID fields remain 0x20c2", "original ROM hash unchanged",
                "measure native SM count; use software floorsweep only if below the defect ceiling",
            ],
        },
        {"index": 3, "name": "isolated-permanent-pcie-gates", **pcie_step},
        {
            "index": 4,
            "name": "pre-gsp-cmp-software-continuation",
            "purpose": (
                "Use EFI/pre-GSP or version-pinned driver changes for CMP HBM "
                "geometry and PCIe Gen4 PHY/retrain policy; preserve the measured "
                "post-fuse compute topology unless an explicit floorsweep override is needed."
            ),
            "why_here": (
                "The CMP ROM does not supply the A100 Gen4/geometry policy path; "
                "software must act before the immutable consumer/GSP boundary."
            ),
            "expected_reported_behavior_not_universal_fact": {
                "sm": "CMP stack may enumerate the fused defect-preserving SM target natively",
                "pcie": "Gen4 still requires geometry/PHY/retrain patching",
                "hbm": "CMP memory geometry remains a per-boot software policy gate",
            },
            "bounds": {"never_exceed_sm": common_target["sm_defect_ceiling"]},
            "required_checks": [
                "pre/post-GSP register timing", "software-visible HBM and anti-alias test",
                "endpoint+root 16-GT/s x16 under load", "SM topology and numerical tests",
            ],
        },
    ]

    common_finish = [
        "repeated cold boots and resets", "full HBM integrity over newly exposed range",
        "per-GPC/TPC stress and numerical comparison", "power/clock/thermal telemetry",
        "AER/Xid/driver/GSP logs", "bandwidth/P2P/GDS/media tests where capability-gated",
    ]
    return [
        {
            "id": "path-a-a100-identity-vbios-software-sm",
            "execution_authorized": False,
            "state": "ordered-design-with-external-gates",
            "final_identity": "10de:20b5",
            "final_vbios": (
                "authentic signed A100 PCIe image matching the calculated HBM "
                "geometry; otherwise this path stops before flashing"
            ),
            "native_policy_expectation": "A100 HBM/Gen4; SM may need pre-GSP software",
            "steps": path_a_steps,
            "final_validation": common_finish,
        },
        {
            "id": "path-b-retain-cmp-software-geometry-gen4",
            "execution_authorized": False,
            "state": "ordered-design-with-external-gates",
            "final_identity": "10de:20c2",
            "final_vbios": "original CMP 170HX image",
            "native_policy_expectation": (
                "reported native post-fuse SM topology; CMP geometry/Gen4 needs pre-GSP software"
            ),
            "steps": path_b_steps,
            "final_validation": common_finish,
        },
    ]


def build_card_report(xray, physical, live_card, field_evidence, public_hashes, root,
                      profile_specs, context_mode="cross-date-level2",
                      gib_per_subpartition=None):
    physical_identity = physical["card"]
    live_identity = live_card["card"]
    physical_tuple = identity_tuple(physical_identity)
    live_tuple = identity_tuple(live_identity)
    pairing_matches = physical_tuple == live_tuple
    if not pairing_matches:
        raise PlanFailure(
            "%s physical/live PCI identity tuple differs: %r vs %r"
            % (physical["bdf"], physical_tuple, live_tuple)
        )
    same_capture = context_mode == "same-capture-level3"
    pairing_authority = (
        "MAIN, RIR and required ordinary registers are bound to one native "
        "helper artifact and full-domain PCI identity"
        if same_capture else
        "the historical physical capture has no stable unique die identifier; "
        "pairing is BDF plus PCI identity only"
    )
    rows = physical["rows"]
    ff = physical["ff"]
    baseline_state, source_rows = effective_block_state(ff)
    boundary = ff["boundary"]["iff_start"]
    first_row = ff["boundary"]["first_available_ff_row"]
    if any(rows[index] != 0 for index in range(first_row, boundary)
           if index not in QUARANTINED_ROWS):
        raise PlanFailure("%s gap contains an unexpected nonzero row" % physical["bdf"])
    crosschecks, crosscheck_errors = field_crosscheck(
        baseline_state, live_card, field_evidence
    )
    if crosscheck_errors:
        raise PlanFailure(
            "%s physical/live field cross-check failed: %s"
            % (physical["bdf"], "; ".join(crosscheck_errors))
        )
    context_crosscheck = controller_context_crosscheck(
        physical, live_card, context_mode=context_mode
    )
    if context_crosscheck["status"] != "PASS":
        raise PlanFailure(
            "%s physical/live controller context differs: %s"
            % (physical["bdf"], "; ".join(context_crosscheck["errors"]))
        )
    topology, normalized_targets, density, density_provenance = (
        normalized_topology_targets(
            xray, live_card, gib_per_subpartition=gib_per_subpartition
        )
    )
    profiles = []
    for profile in profile_specs:
        result = build_profile(
            xray, profile, rows, baseline_state, live_card, field_evidence,
            boundary, first_row, pairing_authority,
            physical["rir"], physical["rir_analysis"],
            context_mode=context_mode,
            normalized_targets=normalized_targets,
            density=density,
            density_provenance=density_provenance,
        )
        if result.get("public_source"):
            result["public_source_binding"] = public_hashes[result["public_source"]]
            result["corrections_from_public_model"] = [
                "allocate from this specimen's measured F rather than reuse public row numbers",
                "derive every disable target from this specimen's stable defect registers",
                "complete ROP-L2/PES/TPC prerequisites before GPC activation",
                "keep NVLink global-group changes separate from SYS_PIPE",
                "quarantine disputed row 487 and reserve append-only recovery capacity",
                "place identity last and expose the unavoidable mismatched-ID prefix",
                "retain the A100-exact MAGIC_D step as empirically blocked per specimen",
            ]
        profiles.append(result)
    comparison_keys = sorted({
        key for profile in profiles for key in profile["changed_logical_blocks"]
    }, key=lambda item: tuple(int(part) for part in item.split(":")))
    comparisons = []
    for key_text in comparison_keys:
        chain, block = (int(value) for value in key_text.split(":"))
        values = {}
        for profile in profiles:
            rows_final = [int(value, 0) for value in profile["proposed_complete_image_rows"]]
            decoded = xray.decode_ff_iff(rows_final, FUSELESS_START, FUSELESS_END)
            state, _source = effective_block_state(decoded)
            values[profile["model_id"]] = hex16(state.get((chain, block), 0))
        comparisons.append({
            "chain_block": key_text,
            "profile_final_values": values,
            "all_profiles_same": len(set(values.values())) == 1,
        })
    operation_sequence_comparison = []
    for profile in profiles:
        sequence = [
            {
                "stage": item["stage"],
                "chain": item["chain"],
                "block": item["block"],
                "logical_new": item["logical_block_new"],
            }
            for item in profile["operations"]
        ]
        operation_sequence_comparison.append({
            "model_id": profile["model_id"],
            "operation_count": len(sequence),
            "sequence_sha256": canonical_json_sha256(sequence),
            "sequence": sequence,
        })
    field_crosscheck_document = {
        "status": "PASS",
        "context_mode": context_mode,
        "entry_count": len(crosschecks),
        "entries": crosschecks,
        "important_scope_limit": (
            "exact same-artifact equality binds these empirical coordinates to this "
            "specimen state; it does not create an official descriptor or prove "
            "feature sufficiency"
            if same_capture else
            "exact equality strongly corroborates the empirical coordinates on these "
            "specimens; it does not create an official descriptor or prove feature sufficiency"
        ),
    }
    ceiling_projection = specimen_ceiling_projection(
        xray, live_card,
        normalized=(topology, density, density_provenance),
    )
    result = {
        "bdf": physical["bdf"],
        "identity_binding": {
            "physical_identity": physical_identity,
            "live_identity": live_identity,
            "context_mode": context_mode,
            "same_native_capture_artifact": same_capture,
            "pci_tuple_exact_match": pairing_matches,
            "pairing_authority": pairing_authority,
            "same_die_cryptographically_proven": False,
        },
        "physical_capture": {
            "path": path_for_report(root, physical["path"]),
            "sha256": physical["sha256"],
            "input_format": copy.deepcopy(physical["input_format"]),
            "generated_utc": physical["root_generated_utc"],
            "tool": physical["root_tool"],
            "main_native_attestation": physical["main_attestation"],
            "rir_native_attestation": physical["rir_attestation"],
        },
        "baseline": {
            "complete_main_u32le_sha256": packed_u32_sha256(rows),
            "complete_rir_u32le_sha256": packed_u32_sha256(physical["rir"]),
            "main_rows": [hex32(value) for value in rows],
            "rir_rows": [hex32(value) for value in physical["rir"]],
            "ff_iff_boundary": ff["boundary"],
            "capacity": ff["capacity_snapshot"],
            "active_iff_signature": iff_signature(ff),
            "effective_block_source_rows": {
                "%d:%d" % key: value for key, value in sorted(source_rows.items())
            },
            "RIR_changed_by_any_plan": False,
            "rir_analysis": physical["rir_analysis"],
        },
        "physical_entry_to_live_controller_context": context_crosscheck,
        "specimen_ceiling_projection": ceiling_projection,
        "profiles": profiles,
        "dual_path_plans": dual_path_plan_models(ceiling_projection, profiles),
        "profile_comparison": comparisons,
        "comparison_model": {
            "model_type": "comparison",
            "execution_authorized": False,
            "executable": False,
            "models_compared": [item["model_id"] for item in profiles],
            "logical_block_differences": comparisons,
            "operation_sequences": operation_sequence_comparison,
            "merge_authority": False,
            "note": "comparison preserves disagreements and never synthesizes a target",
        },
    }
    if same_capture:
        result["physical_to_same_capture_field_crosscheck"] = field_crosscheck_document
    else:
        result["physical_to_live_field_crosscheck"] = field_crosscheck_document
    return result


def unsupported_targets():
    return [
        {
            "target": "ECC",
            "plan_state": "inventory-only",
            "planner_profile": "review-ecc-conventional",
            "reason": "primary/redundant conventional bits are located, but sense/control/RIR and datapath sufficiency are unresolved",
            "likely_fields": [
                "OPT_ECC_EN primary/redundant row 6/7 bit 28",
                "OPT_ECC_EN_CONTROL primary/redundant row 6/7 bit 29",
            ],
            "coordinate_certainty_percent": 99,
            "feature_sufficiency_certainty_percent": 45,
            "required_checks": [
                "recover primary/redundant/control sense rule and RIR interaction",
                "inventory all FBPA/HBM ECC gates, firmware and InfoROM policy",
                "cold error injection, correction counters and full-memory validation",
            ],
        },
        {
            "target": "MIG",
            "plan_state": "experimental-ff-candidate-plus-inventory",
            "reason": (
                "SYS_PIPE Chain-1 bits 372..379 have one reported functional "
                "eight-way-MIG campaign and are supported by the experimental "
                "defect-preserving profile; SW_SMC_DISABLE remains conventional-"
                "fuse inventory only and policy sufficiency is unresolved"
            ),
            "planner_profile": "specimen-media-mig-to-defect-experimental",
            "likely_fields": [
                "OPT_SW_SMC_DISABLE primary/redundant row 14/15 bit 11",
                "SYS_PIPE/GPC/TPC/PES/FBP topology and GSP/VBIOS policy",
            ],
            "coordinate_certainty_percent": {
                "SYS_PIPE_FF": 94,
                "OPT_SW_SMC_DISABLE_conventional": 99,
            },
            "feature_sufficiency_certainty_percent": 55,
            "required_checks": [
                "recover conventional primary/redundant sense semantics",
                "prove profile enumeration, memory partition ownership, reset and channel isolation",
            ],
        },
        {
            "target": "NVDEC/NVJPG/OFA",
            "plan_state": "experimental-defect-gated-candidate",
            "reason": (
                "NVDEC bits 139..143 and the unordered NVJPG/OFA candidate pair "
                "{146,371} have one reported functional campaign. XRAY plans the "
                "pair only together, only when both direct defect words are zero; "
                "firmware/power/API validation remains mandatory."
            ),
            "planner_profile": "specimen-media-mig-to-defect-experimental",
            "likely_fields": [
                "OPT_NVDEC_DISABLE/DEFECTIVE plus HS/security/ucode revision gates",
                "unordered pair {Chain-1 bit 146, Chain-1 bit 371} for NVJPG/OFA",
                "OPT_NVJPG_DEFECTIVE and OPT_OFA_DEFECTIVE as hard gates",
            ],
            "coordinate_certainty_percent": {
                "NVDEC": 90,
                "NVJPG_OFA_pair_set": 88,
                "individual_NVJPG_or_OFA_attribution": 0,
            },
            "feature_sufficiency_certainty_percent": 60,
            "required_checks": [
                "preserve every direct defect and treat bits 146/371 as inseparable",
                "prove PTOP discovery, power/HS gates, firmware load and authoritative codec APIs",
            ],
        },
        {
            "target": "NVENC",
            "plan_state": "not-yet-supported",
            "planner_profile": "review-nvenc-unsupported",
            "reason": (
                "the two-bit direct option, CP, control, defect and status planes "
                "and the five-bit PTOP instance-count inventory are readable, but "
                "no defensible GA100 physical FF or conventional coordinate is recovered and no populated-engine evidence exists"
            ),
            "likely_fields": [
                "OPT_NVENC_DISABLE/CP/DEFECTIVE/CTRL/STATUS",
                "PTOP_SCAL_NUM_NVENCS (inventory only, never a burn target)",
                "power, HS/security and ucode-revision gates",
            ],
            "coordinate_certainty_percent": 0,
            "feature_sufficiency_certainty_percent": 0,
            "production_a100_absence_certainty_percent": 99,
            "retained_four_card_unusable_engine_inference_percent": 97,
            "supported_permanent_unlock_target_percent": 0,
            "required_checks": [
                "recover a GA100 coordinate from isolated evidence",
                "treat a PTOP count as inventory only and validate firmware/API exposure independently",
            ],
        },
        {
            "target": "per-link NVLink",
            "plan_state": "inventory-only",
            "planner_profile": "review-nvlink-per-link-unsupported",
            "reason": "the 12-bit direct option exists, but no physical FF coordinate is recovered",
            "likely_fields": ["OPT_PERLINK_DISABLE", "per-link defect/status/control"],
            "coordinate_certainty_percent": 0,
            "feature_sufficiency_certainty_percent": 20,
            "required_checks": [
                "recover the physical coordinate and prove disable/defect/sense semantics",
                "prove board routing and termination for every intended link",
                "validate per-link status/control, ROM/GSP policy, training and peer traffic",
            ],
        },
        {
            "target": "OC/frequencies/SM-speed",
            "plan_state": "inventory-only",
            "reason": "no portable physical map or reliability-qualified target exists",
            "likely_fields": [
                "GPC2CLK_CAP", "ATE_VMIN", "CP/SAMPLE/SRAM_VMIN bins",
                "SPEEDO0/1/2", "SM_SPEED_SELECT families", "OVERLOCK_INFO",
            ],
            "coordinate_certainty_percent": 0,
            "feature_sufficiency_certainty_percent": 5,
            "required_checks": [
                "separate runtime driver limits from permanent fuse policy",
                "characterize voltage/frequency/thermal/reliability coupling before any permanent target",
            ],
        },
        {
            "target": "SS0 IFF override",
            "plan_state": "first-class-review-only",
            "planner_profile": "review-ss0-iff",
            "reason": "reported IFF words and target register are retained, but row placement and portable values are not established",
            "coordinate_certainty_percent": 85,
            "portable_placement_certainty_percent": 0,
            "required_checks": [
                "recover a portable IFF allocator and exact record grammar",
                "prove cold behavior and compute-class effects independently",
            ],
        },
        {
            "target": "SS1 IFF override",
            "plan_state": "first-class-review-only",
            "planner_profile": "review-ss1-iff",
            "reason": "the reported target is IMLA4, not FP64; row 487 is quarantined and placement is not portable",
            "coordinate_certainty_percent": 85,
            "portable_placement_certainty_percent": 0,
            "required_checks": [
                "recover a portable IFF allocator and exact record grammar",
                "measure IMLA4 behavior without relabeling it as DP",
            ],
        },
        {
            "target": "FP64/DP conventional control",
            "plan_state": "first-class-review-only",
            "planner_profile": "review-dp-conventional",
            "reason": "rows 6/7 bit 26 are mapped, but adjacent bit 27 undo/control semantics lack isolated cold proof",
            "coordinate_certainty_percent": {"selector_bit26": 99, "control_bit27": 65},
            "required_checks": [
                "recover conventional primary/redundant and RIR sense semantics",
                "isolate bit 27 behavior before any permanent operation",
            ],
        },
        {
            "target": "persistent DSOV=0",
            "plan_state": "research-only",
            "reason": "no evidenced persistent control path has been found; firmware tracing is separate from FF planning",
            "likely_fields": [
                "DISABLE_SW_OVERRIDE_STATUS observation", "EN_SW_OVERRIDE volatile control",
                "DevInit/FWSEC/reset-domain policy (unresolved)",
            ],
            "coordinate_certainty_percent": 99,
            "cold_persistence_mechanism_certainty_percent": 0,
            "required_checks": [
                "identify the early immutable consumer and reset domain",
                "prove state across a fundamental cold reset before any mutable stage can replay it",
            ],
        },
    ]


def build_report(root, physical_dir, live_path, xray_path, selected_profile_ids=None,
                 physical_capture_paths=None,
                 physical_card_selectors=None,
                 context_mode="cross-date-level2",
                 gib_per_subpartition=None,
                 physical_input_format="auto", live_input_format="auto"):
    enforce_declared_input_format(
        physical_input_format, physical_input_format,
        PHYSICAL_INPUT_FORMATS, "physical",
    )
    if context_mode == "cross-date-level2":
        enforce_declared_input_format(
            live_input_format, live_input_format, LIVE_INPUT_FORMATS, "live"
        )
    elif live_input_format != "auto":
        raise PlanFailure(
            "live input format is inapplicable outside cross-date-level2 context"
        )
    xray = load_python_module(xray_path, "xray_candidate_planner_decoder")
    xray_version = getattr(xray, "TOOL_VERSION", None)
    if not isinstance(xray_version, str) or not xray_version:
        raise PlanFailure("selected XRAY decoder lacks a non-empty TOOL_VERSION")
    field_evidence = validate_catalog_alignment(xray)
    profile_by_id = {item["id"]: item for item in PROFILE_SPECS}
    if selected_profile_ids:
        resolved_profile_ids = [
            PROFILE_ALIASES.get(profile_id, profile_id)
            for profile_id in selected_profile_ids
        ]
        unknown = sorted(set(resolved_profile_ids) - set(profile_by_id))
        if unknown:
            raise PlanFailure("unknown profile IDs: %s" % ", ".join(unknown))
        # Preserve the caller's order while rejecting duplicates.
        profile_specs = []
        seen_profiles = set()
        for profile_id in resolved_profile_ids:
            if profile_id in seen_profiles:
                raise PlanFailure("duplicate --profile: %s" % profile_id)
            seen_profiles.add(profile_id)
            profile_specs.append(profile_by_id[profile_id])
    else:
        profile_specs = list(PROFILE_SPECS)
    public_hashes = {}
    required_public_sources = sorted({
        item["public_source"] for item in profile_specs
        if item.get("public_source")
    })
    package_root = Path(__file__).resolve().parent.parent
    bundled_lineage = None
    for name in required_public_sources:
        relative = PUBLIC_SOURCE_PATHS[name]
        path = root / relative
        reported_path = relative
        if not path.is_file():
            reported_path = BUNDLED_PUBLIC_SOURCE_PATHS[name]
            path = package_root / reported_path
        expected_public_sha256 = PUBLIC_SOURCE_METADATA[name]["expected_sha256"]
        if path.is_file():
            actual_public_sha256 = sha256_file(path)
            if actual_public_sha256 != expected_public_sha256:
                raise PlanFailure(
                    "public source hash mismatch for %s: expected %s, got %s"
                    % (reported_path, expected_public_sha256,
                       actual_public_sha256)
                )
            public_hashes[name] = {
                "path": reported_path,
                "sha256": actual_public_sha256,
                "hash_verified": True,
                "raw_source_present": True,
                **PUBLIC_SOURCE_METADATA[name],
            }
            continue
        if bundled_lineage is None:
            bundled_lineage = load_bundled_public_lineage(package_root)
        lineage_path, lineage_document = bundled_lineage
        source = lineage_document["sources"][name]
        public_hashes[name] = {
            "path": source["canonical_source"],
            "sha256": expected_public_sha256,
            "hash_verified": False,
            "raw_source_present": False,
            "hash_attestation_only": True,
            "lineage_descriptor": BUNDLED_PUBLIC_LINEAGE_PATH,
            "lineage_descriptor_sha256": sha256_file(lineage_path),
            **PUBLIC_SOURCE_METADATA[name],
        }
    if physical_capture_paths:
        # Preserve explicit caller order because --physical-card selectors are
        # paired positionally. Directory discovery remains sorted below.
        physical_paths = list(physical_capture_paths)
    else:
        if physical_dir is None:
            raise PlanFailure("physical capture input is missing")
        physical_paths = sorted(physical_dir.glob("*physical.json"))
    if not physical_paths:
        raise PlanFailure(
            "no physical captures selected%s"
            % (" in %s" % physical_dir if physical_dir is not None else "")
        )
    for path in physical_paths:
        if not path.is_file():
            raise PlanFailure("physical capture is not a file: %s" % path)
    selectors = list(physical_card_selectors or [])
    if selectors and len(selectors) != len(physical_paths):
        raise PlanFailure(
            "--physical-card count must equal --physical-capture count"
        )
    if not selectors:
        selectors = [None] * len(physical_paths)
    physical_cards = [
        validate_physical_capture(
            xray, path, selector, declared_input_format=physical_input_format
        )
        for path, selector in zip(physical_paths, selectors)
    ]
    bdfs = [item["bdf"] for item in physical_cards]
    if len(set(bdfs)) != len(bdfs):
        raise PlanFailure("duplicate physical-capture BDF")
    if context_mode == "same-capture-level3":
        live_document = None
        live_cards = {}
        same_capture_context_sources = []
        for physical in physical_cards:
            document, card = validate_same_capture_register_context(
                physical["path"], xray, physical["bdf"]
            )
            live_cards[physical["bdf"]] = card
            same_capture_context_sources.append({
                "path": path_for_report(root, physical["path"]),
                "sha256": physical["sha256"],
                "generated_utc": document.get("generated_utc"),
                "tool": copy.deepcopy(physical["root_tool"]),
                "bdf": physical["bdf"],
                "binding": "same native artifact as certified MAIN/RIR",
                "input_format": copy.deepcopy(physical["input_format"]),
            })
    elif context_mode == "cross-date-level2":
        if live_path is None:
            raise PlanFailure("--live is required for cross-date-level2 context")
        (live_document, live_cards, live_source_tool, live_source_sha256,
         live_input_format_binding) = (
            validate_live_capture(
                live_path, xray, declared_input_format=live_input_format
            )
        )
        same_capture_context_sources = []
    else:
        raise PlanFailure("unknown context mode %r" % context_mode)
    missing_live = sorted(set(bdfs) - set(live_cards))
    if missing_live:
        raise PlanFailure("live capture lacks physical BDFs: %s" % missing_live)
    card_reports = [
        build_card_report(xray, physical, live_cards[physical["bdf"]],
                          field_evidence, public_hashes, root, profile_specs,
                          context_mode=context_mode,
                          gib_per_subpartition=gib_per_subpartition)
        for physical in physical_cards
    ]
    if public_hashes:
        public_audit_binding = validate_public_faithful_audit(root, public_hashes)
    else:
        not_applicable = None
        public_audit_binding = {
            "status": "not-required",
            "path": None,
            "sha256": None,
            "schema": None,
            "legacy_input": False,
            "all_models_pass": not_applicable,
            "lineage_hashes_match": not_applicable,
            "note": "no selected profile depends on a public-faithful source",
        }
    script_path = Path(__file__).resolve()
    sources = {
        "current_decoder": {
            "path": path_for_report(root, xray_path), "sha256": sha256_file(xray_path),
            "version": xray_version,
        },
        "public_models": public_hashes,
    }
    if context_mode == "same-capture-level3":
        sources["same_capture_level3_register_contexts"] = same_capture_context_sources
    else:
        sources["live_capture"] = {
            "path": path_for_report(root, live_path), "sha256": sha256_file(live_path),
            "generated_utc": live_document.get("generated_utc"),
            "tool": live_source_tool,
            "immutable_source_sha256": live_source_sha256,
            "input_format": live_input_format_binding,
        }
    global_blockers = [
        "captures are unsigned evidence artifacts",
        "no official GA100 physical descriptor establishes every coordinate or allocator bound",
        "no live 0x20b5 specimen has validated post-conversion read behavior",
        "no electrical write path or cold-boot result is exercised here",
        "independent electrical and execution-procedure review is required; the offline oracle is not hardware qualification",
    ]
    if context_mode != "same-capture-level3":
        global_blockers.insert(
            1,
            "the physical and live captures are cross-date and BDF-bound, not uniquely die-authenticated",
        )
    report = {
        "schema": SCHEMA,
        "tool": {
            "name": "170hx_candidate_plan",
            "version": SCRIPT_VERSION,
            "sha256": sha256_file(script_path),
        },
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "execution_authorized": False,
        "executable": False,
        "safety_boundary": {
            "offline_only": True,
            "contains_mmio_backend": False,
            "contains_controller_write_primitive": False,
            "contains_otp_apply_mode": False,
            "separate_future_authorization_required": True,
        },
        "sources": sources,
        "register_context_mode": context_mode,
        "model_types_emitted_or_bound": sorted(
            ({"comparison", "recovery", "public-faithful"}
             if public_hashes else {"comparison", "recovery"}).union(
                item["model_type"] for item in profile_specs
            )
        ),
        "plan_format_contract": {
            "physical_input_formats": list(PHYSICAL_INPUT_FORMATS),
            "live_input_formats": list(LIVE_INPUT_FORMATS),
            "output_formats": list(PLAN_OUTPUT_FORMATS),
            "candidate_profile_count": len(PROFILE_SPECS),
            "review_only_profile_count": sum(
                1 for item in PROFILE_SPECS if item.get("review_only")
            ),
            "output_semantics": (
                "all output formats encode the same non-executable semantic JSON document; canonical-json-bin is not an OTP image"
            ),
        },
        "selected_profiles": [item["id"] for item in profile_specs],
        "public_faithful_model_reference": {
            **public_audit_binding,
            "note": "faithful public records stay separate; this artifact emits corrected/specimen/best-evidence candidates",
        },
        "coordinate_authority": {
            "official_ga100_ff_descriptor_available": False,
            "planner_catalog_matches_selected_xray_catalog": True,
            "planning_override_of_xray_planning_allowed_false": (
                "explicit research artifact only; every output remains non-executable"
            ),
        },
        "shared_ff_iff_bounds_model": {
            "S": FUSELESS_START,
            "S_confidence_percent": 95,
            "S_authority": "cross-specimen empirical scan origin; official descriptor absent",
            "E": FUSELESS_END,
            "E_confidence_percent": 85,
            "E_authority": "last address in the complete 512-row controller image; allocator descriptor absent",
            "B": "per-specimen first active IFF word, cross-checked with current IFF_RECORD",
            "F": "per-specimen first row after the last occupied FF/spent row below B",
            "gap": "B-F; this is observed capacity, not an authorization to allocate every row",
            "do_not_conflate": (
                "E=511 is the final physical row; observed IFF LAST=507 and the "
                "last appendable FF row are separate state-dependent values"
            ),
        },
        "cards": card_reports,
        "unsupported_or_unresolved_targets": unsupported_targets(),
        "global_blockers": global_blockers,
    }
    report["report_content_sha256_excluding_self"] = canonical_json_sha256(report)
    return report


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    physical_group = parser.add_mutually_exclusive_group(required=True)
    physical_group.add_argument("--physical-dir", type=Path)
    physical_group.add_argument(
        "--physical-capture", type=Path, action="append",
        help="exact native MAIN/RIR capture; repeat once per card",
    )
    parser.add_argument(
        "--physical-card", action="append",
        help=(
            "card index/BDF/name paired by order with --physical-capture; "
            "required when a selected native capture contains multiple cards"
        ),
    )
    parser.add_argument(
        "--physical-input-format", choices=PHYSICAL_INPUT_FORMATS,
        default="auto",
        help="declare the physical input container; unsupported labels are rejected",
    )
    context_group = parser.add_mutually_exclusive_group(required=True)
    context_group.add_argument("--live", type=Path)
    context_group.add_argument(
        "--same-capture-context", action="store_true",
        help=(
            "use each selected Level-3 artifact's stable ordinary registers as "
            "the planning context; rejects non-native, unrestored or incomplete captures"
        ),
    )
    parser.add_argument(
        "--live-input-format", choices=LIVE_INPUT_FORMATS, default="auto",
        help="declare the separate Level-2 input container; unused with --same-capture-context",
    )
    parser.add_argument("--xray", type=Path)
    parser.add_argument(
        "--profile", action="append",
        choices=sorted({item["id"] for item in PROFILE_SPECS}.union(PROFILE_ALIASES)),
        help="emit only this profile (repeatable); default emits every profile",
    )
    parser.add_argument(
        "--gib-per-subpartition", type=positive_finite_float,
        help=(
            "explicit specimen-qualified HBM density; required for identities "
            "other than the retained 20c2/1585 population because 20b5 alone "
            "does not distinguish 40-GiB from 80-GiB A100 boards"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--output-format", choices=PLAN_OUTPUT_FORMATS, default="json",
        help="lossless candidate-plan representation; JSON remains the default",
    )
    parser.add_argument(
        "--export-all-dir", type=Path,
        help="also create all lossless plan representations in this directory",
    )
    return parser.parse_args(argv)


def default_xray_path(root):
    """Prefer a decoder shipped beside a bundled ``tools`` directory.

    The source-tree layout keeps this tool in ``ROOT/tools`` and the decoder
    in ``ROOT/170HX-XRAY``.  A distributable edition keeps both under the
    project directory.  Package-local resolution must win so one edition can
    never silently import a sibling edition from the surrounding workspace.
    """
    package_local = Path(__file__).resolve().parent.parent / "170HX-XRAY.py"
    if package_local.is_file():
        return package_local
    return root / "170HX-XRAY" / "170HX-XRAY.py"


def main(argv=None):
    args = parse_args(argv)
    if args.same_capture_context and args.live_input_format != "auto":
        raise PlanFailure(
            "--live-input-format is inapplicable with --same-capture-context"
        )
    root = args.root.resolve()
    physical_dir = None
    if args.physical_dir is not None:
        physical_dir = (
            args.physical_dir if args.physical_dir.is_absolute()
            else root / args.physical_dir
        ).resolve()
    physical_capture_paths = []
    for path in args.physical_capture or []:
        physical_capture_paths.append(
            (path if path.is_absolute() else root / path).resolve()
        )
    if args.physical_card and not physical_capture_paths:
        raise PlanFailure("--physical-card requires --physical-capture")
    live_path = None
    if args.live is not None:
        live_path = (
            args.live if args.live.is_absolute() else root / args.live
        ).resolve()
    xray_path = args.xray or default_xray_path(root)
    xray_path = xray_path.resolve()
    requested_output = args.output if args.output.is_absolute() else root / args.output
    output_parent = requested_output.parent.resolve()
    output = output_parent / requested_output.name
    export_all_dir = None
    if args.export_all_dir is not None:
        export_all_dir = (
            args.export_all_dir
            if args.export_all_dir.is_absolute()
            else root / args.export_all_dir
        ).resolve()
        reserved = {
            (export_all_dir / filename).resolve()
            for filename in PLAN_EXPORT_FILENAMES.values()
        }
        if output.resolve() in reserved:
            raise PlanFailure(
                "--output must not collide with an --export-all-dir artifact"
            )
    report = build_report(
        root, physical_dir, live_path, xray_path,
        selected_profile_ids=args.profile,
        physical_capture_paths=physical_capture_paths,
        physical_card_selectors=args.physical_card,
        context_mode=(
            "same-capture-level3" if args.same_capture_context
            else "cross-date-level2"
        ),
        gib_per_subpartition=args.gib_per_subpartition,
        physical_input_format=args.physical_input_format,
        live_input_format=args.live_input_format,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_plan_export(output, report, args.output_format)
    extra_outputs = (
        write_all_plan_exports(export_all_dir, report)
        if export_all_dir is not None else {}
    )
    print(strict_json_dumps({
        "schema": report["schema"],
        "cards": len(report["cards"]),
        "profiles_per_card": len(report["cards"][0]["profiles"]) if report["cards"] else 0,
        "output": str(output),
        "output_format": args.output_format,
        "additional_outputs": {
            key: str(value) for key, value in sorted(extra_outputs.items())
        },
        "sha256": sha256_file(output),
        "execution_authorized": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PlanFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
