#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""170HX-XRAY: GA100/CMP 170HX observer, decoder, and fail-closed planner.

Safety model
============

XRAY exposes six ordinal levels plus a zero-write glitch-read branch. ``passive`` is OS/driver metadata only;
``bar-read`` adds allowlisted direct MMIO reads; ``glitch-read`` qualifies a
transient 2080/DSOV=0 phenotype using only those zero-write reads; and
``semantic-read`` adds, only
in an exclusive unbound maintenance state, controller writes whose sole purpose
is reading physical FF/RIR rows. ``plan`` is a strictly offline evidence and
conversion level between observation and mutation. ``volatile-write`` remains
non-executable. ``otp-write`` has one deliberately narrow experimental backend:
it can append hash-sealed records only to verified blank GA100 FF rows after an
exact-current Level-3 binding, independent semantic replay, a reviewed receipt,
interactive authorization, a held DSOV=0 window, and durable journaling. It is
not a generic MMIO/fuse API. The legacy name ``read-write`` aliases
``volatile-write``. Live capture above semantic-read remains outside full-dump.

Optional NVML/nvidia-smi metadata queries are explicitly reported as opaque
driver queries: this program invokes no setter, but cannot prove the driver's
internal side effects.  They run only for devices whose runtime-PM state is
already ``active`` or ``unsupported``.

Physical rows captured by some separately authorized procedure can be decoded
offline with ``analyze-rows``.  All goal assessments are evidence reports and
are deliberately non-executable.

The program uses only Python 3.9-compatible standard-library APIs.

Special thanks
==============

Petri Krohn (PetriKrohn, ``@petrikrohn_26497``) for CMP 170HX research
and public chronicling, together with Jon Pry, Cab, BlackSun, Humvee55,
Amogh Munikote, meatsus, Rubbra_F, duggasco,
orrorin, mrpandagod, kostakoff, InnovativeOSS117, mg94301-2007 (requested but
not independently verified), jetcat8848 and arabel1a.  Voltage-glitch and
capture work is credited to Kate Land/Kater, Mello Jello/JosiahWhite, and
Danylo Storozhev/``@dstoro``.  See ``docs/CREDITS.md`` for qualified primary-
source attribution and the complete repository/source list.  Acknowledgement
does not endorse every claim or make any public plan programming authority.
"""

from __future__ import print_function

import argparse
import ctypes
import ctypes.util
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import mmap
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


TOOL_NAME = "170HX-XRAY"
TOOL_VERSION = "4.4.0"
SCHEMA_VERSION = "170hx-xray-v1"
# Immutable pre-migration artifacts remain valid inputs.  These constants are
# input-only compatibility labels; XRAY never emits them.
LEGACY_TOOL_NAME = "170HX-RAY"
LEGACY_SCHEMA_VERSION = "170hx-ray-v1"
LEGACY_HELPER_ABI = 5
DEFAULT_SYSFS_ROOT = "/sys/bus/pci/devices"
DEFAULT_GPCPROBE = "/dev/gpcprobe"
DEFAULT_XRAY_HELPER = "/dev/170hx_xray"
DEFAULT_DEVICE_IDS = frozenset((0x20C2,))
# Read/attestation identities are broader than the default discovery scope.
# 20B5 is the post-conversion A100 PCI identity seen in the research evidence;
# keeping the capture default at 20C2 avoids silently widening live scope.
GA100_READ_TARGET_DEVICE_IDS = frozenset((0x20B5, 0x20C2, 0x2080))
MAX_FUSE_ROWS = 512
GA100_GPC_COUNT = 8
GA100_TPCS_PER_GPC = 8
GA100_SMS_PER_TPC = 2
GA100_PES_PER_GPC = 3
GA100_PES_TPC_MASKS = (0x07, 0x38, 0xC0)
GA100_FBP_COUNT = 12
GA100_SUBPARTITION_COUNT = 24
# These density values describe the retained Hynix CMP/A100 specimens, not a
# universal consequence of GA100's 12-FBP/24-subpartition geometry.  New
# captures must carry an explicit, qualified density provenance before XRAY
# emits GiB values.
GA100_GIB_PER_FBP = 8
GA100_GIB_PER_SUBPARTITION = 4
BAR0_MIN_LENGTH = 0x0082110C
XRAY_BAR0_MIN_LENGTH = 0x00A00000
ACCESS_LEVELS = (
    "passive", "bar-read", "glitch-read", "semantic-read", "plan",
    "volatile-write", "otp-write",
)
ACCESS_ALIASES = {
    "read-write": "volatile-write",
    "full-write": "otp-write",
}
ACCESS_MODES = ACCESS_LEVELS + tuple(ACCESS_ALIASES)
ACCESS_RANK = {name: index for index, name in enumerate(ACCESS_LEVELS)}

DUMP_INPUT_FORMATS = (
    "auto", "json", "jsonl", "indexed-text", "raw-u32le",
)
DUMP_OUTPUT_FORMATS = (
    "xray-json", "xray-jsonl", "indexed-text", "raw-u32le", "ffcalc-json",
    "xpatrik-json", "ga100-fuse-re-json", "dstoro-projection-json",
    "dstoro-jsonl",
)

FULL_DUMP_OBSERVATION_LEVELS = (
    "passive", "bar-read", "semantic-read", "glitch-read",
)
FULL_DUMP_EFFECT_COUNTERS = (
    "direct_user_mmio_reads",
    "direct_user_mmio_writes",
    "helper_operational_mmio_writes",
    "controller_read_commands_issued",
    "completed_data_reads_lower_bound",
    "pci_config_writes",
    "runtime_pm_writes",
    "otp_program_commands",
)
FULL_DUMP_FORMAT_FILENAMES = {
    "xray-json": "xray.json",
    "xray-jsonl": "xray.jsonl",
    "indexed-text": "indexed.txt",
    "raw-u32le": "raw-u32le.bin",
    "ffcalc-json": "ffcalc.json",
    "xpatrik-json": "xpatrik.json",
    "ga100-fuse-re-json": "ga100-fuse-re.json",
    "dstoro-projection-json": "dstoro-projection.json",
    "dstoro-jsonl": "dstoro.jsonl",
}

FULL_DUMP_SAFETY_BOUNDARY = (
    "full-dump never raises access above --max-access, never executes a "
    "candidate planner, and invokes apply/full-write in non-executing "
    "offline validation modes only"
)

XRAY_HELPER_ABI = 6
XRAY_MAX_CARDS = 16
XRAY_MAX_PASSES = 2
XRAY_MAIN_ROWS = 512
XRAY_RIR_ROWS = 8
XRAY_CAP_READ32 = 1 << 0
XRAY_CAP_DUMP_MAIN = 1 << 1
XRAY_CAP_DUMP_RIR = 1 << 2
XRAY_CAP_BOUND_READ32 = 1 << 3
XRAY_CAP_BOUND_CONTROLLER = 1 << 4
XRAY_CAP_LAST_JOURNAL = 1 << 5
XRAY_CAP_A100_CONTROLLER = 1 << 6
XRAY_DUMP_MAIN = 1
XRAY_DUMP_RIR = 2
XRAY_DUMP_F_ALLOW_BOUND = 1 << 0
XRAY_DUMP_F_NO_SW_IFF = 1 << 1
XRAY_DUMP_F_CLEAN_CRC = 1 << 2

GP_IOC_MAGIC = ord("G")
GP_IOC_INFO = (GP_IOC_MAGIC << 8) | 1
GP_IOC_READ32 = (3 << 30) | (12 << 16) | (GP_IOC_MAGIC << 8) | 2


def _linux_ioc(direction, type_value, number, size):
    return (
        (direction << 30)
        | (size << 16)
        | (type_value << 8)
        | number
    )


class XrayIdentity(ctypes.Structure):
    _fields_ = [
        ("generation", ctypes.c_uint64),
        ("domain", ctypes.c_uint16),
        ("bus", ctypes.c_uint8),
        ("devfn", ctypes.c_uint8),
        ("vendor", ctypes.c_uint16),
        ("device", ctypes.c_uint16),
        ("subvendor", ctypes.c_uint16),
        ("subdevice", ctypes.c_uint16),
        ("revision", ctypes.c_uint8),
        ("reserved0", ctypes.c_uint8 * 7),
    ]


class XrayCardInfo(ctypes.Structure):
    _fields_ = [
        ("identity", XrayIdentity),
        ("class_code", ctypes.c_uint32),
        ("pci_command", ctypes.c_uint16),
        ("driver_bound", ctypes.c_uint8),
        ("runtime_status", ctypes.c_uint8),
        ("bar0_start", ctypes.c_uint64),
        ("bar0_length", ctypes.c_uint64),
        ("driver", ctypes.c_char * 32),
    ]


class XrayInfo(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("max_cards", ctypes.c_uint32),
        ("reserved0", ctypes.c_uint32),
        ("module_generation", ctypes.c_uint64),
        ("cards", XrayCardInfo * XRAY_MAX_CARDS),
    ]


class XrayReg32(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("identity", XrayIdentity),
        ("offset", ctypes.c_uint32),
        ("value", ctypes.c_uint32),
    ]


class XrayControllerSnapshot(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "pmc_boot0", "fusectrl", "fuseaddr", "en_sw_override",
        "iff_record", "debugctrl", "crc_status",
        "enable_fuse_program_status", "disable_fuse_program_status",
        "bypass_fuses_status", "disable_sw_override_status",
        "iff_sw_fusing", "debugctrl_priv_mask", "fusewdata_priv_mask",
        "fusectrl_priv_mask", "opt_priv_mask",
    )]


class XrayDump(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("identity", XrayIdentity),
        ("kind", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("requested_passes", ctypes.c_uint32),
        ("row_count", ctypes.c_uint32),
        ("completed_passes", ctypes.c_uint32),
        ("coherent", ctypes.c_uint32),
        ("status", ctypes.c_int32),
        ("failed_pass", ctypes.c_uint32),
        ("failed_row", ctypes.c_uint32),
        ("writes_issued", ctypes.c_uint32),
        ("reads_issued", ctypes.c_uint32),
        ("address_restored", ctypes.c_uint32),
        ("debug_restored", ctypes.c_uint32),
        ("driver_bound_at_entry", ctypes.c_uint32),
        ("entry", XrayControllerSnapshot),
        ("exit", XrayControllerSnapshot),
        ("words", (ctypes.c_uint32 * XRAY_MAIN_ROWS) * XRAY_MAX_PASSES),
    ]


XRAY_IOC_INFO = _linux_ioc(2, ord("X"), 0x01, ctypes.sizeof(XrayInfo))
XRAY_IOC_READ32 = _linux_ioc(3, ord("X"), 0x02, ctypes.sizeof(XrayReg32))
XRAY_IOC_DUMP = _linux_ioc(3, ord("X"), 0x03, ctypes.sizeof(XrayDump))
XRAY_IOC_LAST = _linux_ioc(3, ord("X"), 0x04, ctypes.sizeof(XrayDump))

BDF_RE = re.compile(
    r"^(?P<domain>[0-9a-fA-F]{4}):(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<device>[0-9a-fA-F]{2})\.(?P<function>[0-7])$"
)


class AuditError(Exception):
    """Expected input, platform, or evidence error."""


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def _reject_json_constant(value):
    raise AuditError("non-finite JSON constant is forbidden: %s" % value)


def _strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", _reject_json_constant)
    return _JSON_LOADS(*args, **kwargs)


def _strict_json_dumps(*args, **kwargs):
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


def canonical_access_mode(value):
    """Return one canonical access name for a CLI spelling."""
    canonical = ACCESS_ALIASES.get(value, value)
    if canonical not in ACCESS_RANK:
        raise AuditError("unknown access mode: %r" % value)
    return canonical


def access_at_least(value, threshold):
    return ACCESS_RANK[canonical_access_mode(value)] >= ACCESS_RANK[threshold]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def hex32(value):
    return "0x%08x" % (value & 0xFFFFFFFF)


def parse_int(value, label="integer"):
    if isinstance(value, bool):
        raise AuditError("%s must not be boolean" % label)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value.strip(), 0)
        except ValueError as exc:
            raise AuditError("invalid %s: %r" % (label, value)) from exc
    else:
        raise AuditError("invalid %s type: %s" % (label, type(value).__name__))
    return result


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tool_sha256():
    try:
        return sha256_file(os.path.realpath(__file__))
    except OSError:
        return None


def emit_json(value, output):
    text = _strict_json_dumps(value, indent=2, sort_keys=True) + "\n"
    if output in (None, "-"):
        sys.stdout.write(text)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(output, flags, 0o600)
    except FileExistsError as exc:
        raise AuditError("refusing to overwrite output: %s" % output) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def emit_bytes(value, output):
    if not isinstance(value, bytes):
        raise AuditError("binary output must be bytes")
    if output in (None, "-"):
        sys.stdout.buffer.write(value)
        sys.stdout.buffer.flush()
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(output, flags, 0o600)
    except FileExistsError as exc:
        raise AuditError("refusing to overwrite output: %s" % output) from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def emit_text(value, output):
    if not isinstance(value, str):
        raise AuditError("text output must be a string")
    if output in (None, "-"):
        sys.stdout.write(value)
        sys.stdout.flush()
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(output, flags, 0o600)
    except FileExistsError as exc:
        raise AuditError("refusing to overwrite output: %s" % output) from exc
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _reg(
    name, address, category, role="observation", fields=None, source=None,
    capture_safe=True, access_effect="pure_read", stability="required",
    all_ones="suspicious", badf="error", profile="default", aliases=None,
    identity_confidence_percent=99, interpretation_confidence_percent=None,
    confidence_reason=None,
):
    if access_effect not in ("pure_read", "read_to_advance"):
        raise ValueError("invalid register access effect: %s" % access_effect)
    if stability not in ("required", "snapshot", "volatile"):
        raise ValueError("invalid register stability policy: %s" % stability)
    if all_ones not in ("suspicious", "valid", "availability"):
        raise ValueError("invalid all-ones policy: %s" % all_ones)
    if badf not in ("error", "availability"):
        raise ValueError("invalid BADF policy: %s" % badf)
    if access_effect != "pure_read":
        capture_safe = False
    result = {
        "name": name,
        "address": address,
        "category": category,
        "role": role,
        "fields": fields or [],
        "source": source or "curated GA100 direct-header/local-map audit",
        "capture_safe": bool(capture_safe),
        "access_effect": access_effect,
        "stability": stability,
        "all_ones": all_ones,
        "badf": badf,
        "profile": profile,
        "aliases": list(aliases or []),
        "identity_confidence_percent": identity_confidence_percent,
        "interpretation_confidence_percent": interpretation_confidence_percent,
        "confidence_reason": confidence_reason or (
            "curated direct-header/register-map identity; safe recipe is not implied"
        ),
        "planning_allowed": False,
    }
    return result


# This list is deliberately explicit.  The header parser below is a schema
# builder, not permission to sweep every register it discovers.
CURATED_REGISTERS = [
    _reg("PMC_BOOT_0", 0x00000000, "sentinel"),
    _reg("PBUS_IFR_STATUS0", 0x00001720, "iff_execution", fields=[
        {"name": "ROMADDR", "msb": 20, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("PBUS_IFR_STATUS1", 0x00001724, "iff_execution", fields=[
        {"name": "BAR0ADDR", "msb": 23, "lsb": 0},
        {"name": "IFF_DONE", "msb": 27, "lsb": 27},
        {"name": "IDLE", "msb": 29, "lsb": 29},
        {"name": "LASTEXEC", "msb": 30, "lsb": 30},
        {"name": "READINRMW", "msb": 31, "lsb": 31},
    ], source="GA100 register specification"),
    _reg("PBUS_IFR_ERROR", 0x00001728, "iff_execution", fields=[
        {"name": "BADSIG", "msb": 0, "lsb": 0},
        {"name": "FAILSAFE_TIMEOUT", "msb": 1, "lsb": 1},
        {"name": "BADPARITY", "msb": 8, "lsb": 8},
        {"name": "BADROMLEN", "msb": 12, "lsb": 12},
        {"name": "BADCHECKSUM", "msb": 16, "lsb": 16},
        {"name": "BADFORMAT", "msb": 20, "lsb": 20},
        {"name": "PRI_ERROR", "msb": 21, "lsb": 21},
        {"name": "IFF_RESENSE_TIMEOUT", "msb": 23, "lsb": 23},
        {"name": "IFF_BADFIELDSPEC", "msb": 24, "lsb": 24},
        {"name": "IFF_BADSPACEID", "msb": 25, "lsb": 25},
        {"name": "IFF_BADFUSELEN", "msb": 26, "lsb": 26},
        {"name": "IFF_BADCMDOP", "msb": 27, "lsb": 27},
        {"name": "IFF_PRI_ERROR", "msb": 29, "lsb": 29},
    ], source="GA100 register specification"),
    _reg("PBUS_IFR_INFO0", 0x0000172C, "iff_execution", fields=[
        {"name": "ROMLEN", "msb": 19, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("FUSECTRL", 0x00820000, "controller", fields=[
        {"name": "CMD", "msb": 1, "lsb": 0},
        {"name": "STATE", "msb": 20, "lsb": 16},
        {"name": "FUSE_INIT_SENSE_DONE", "msb": 29, "lsb": 29},
        {"name": "FUSE_SENSE_DONE", "msb": 30, "lsb": 30},
        {"name": "RECORD_SHIFT_DONE", "msb": 31, "lsb": 31},
    ], source="GA100 register specification"),
    _reg("FUSEADDR", 0x00820004, "controller", source="GA100 register specification"),
    _reg("FUSERDATA_LATCHED", 0x00820008, "controller", source="GA100 register specification"),
    _reg("EN_SW_OVERRIDE", 0x00820040, "controller", source="GA100 register specification"),
    _reg("IFF_RECORD", 0x00820050, "controller", fields=[
        {"name": "START", "msb": 8, "lsb": 0},
        {"name": "LAST", "msb": 24, "lsb": 16},
    ], source="GA100 register specification"),
    _reg("DEBUGCTRL", 0x00820060, "controller", fields=[
        {"name": "MARGIN_READ", "msb": 0, "lsb": 0},
        {"name": "RWL", "msb": 1, "lsb": 1},
        {"name": "TRCS", "msb": 2, "lsb": 2},
        {"name": "AT1", "msb": 3, "lsb": 3},
        {"name": "AT0", "msb": 4, "lsb": 4},
        {"name": "PD_CTRL", "msb": 5, "lsb": 5},
    ], source="GA100 register specification"),
    _reg("SPARE_ADDR_START", 0x00820068, "controller", source="GA100 register specification"),
    _reg("SPARE_ENDIS_ADDR_START", 0x0082006C, "controller", source="GA100 register specification"),
    _reg("CRC_STATUS", 0x00820070, "controller", fields=[
        {"name": "H0", "msb": 2, "lsb": 0},
        {"name": "H1", "msb": 6, "lsb": 4},
    ], source="GA100 register specification"),
    _reg("ENABLE_FUSE_PROGRAM_STATUS", 0x00820078, "security", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("DISABLE_FUSE_PROGRAM_STATUS", 0x0082007C, "security", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("BYPASS_FUSES_STATUS", 0x00820080, "security", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("DISABLE_SW_OVERRIDE_STATUS", 0x00820084, "security", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("IFF_SW_FUSING", 0x00820088, "controller", fields=[
        {"name": "VAL", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("FUSEWDATA_PRIV_LEVEL_MASK", 0x008200F4, "security", source="GA100 register specification"),
    _reg("FUSECTRL_PRIV_LEVEL_MASK", 0x008200F8, "security", source="GA100 register specification"),
    _reg("OPT_PRIV_LEVEL_MASK", 0x008200FC, "security", source="GA100 register specification"),
    _reg("TPC_FLOORSWEEP_PRIV_LEVEL_MASK", 0x008200D0, "security", source="GA100 register specification"),
    _reg("RECONFIG_PRIV_LEVEL_MASK", 0x008200D8, "security", source="GA100 register specification"),
    _reg("FLOORSWEEP_PRIV_LEVEL_MASK", 0x008200DC, "security", source="GA100 register specification"),
    _reg("OPT_ECC_EN", 0x00820228, "ecc", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_DISABLE_GEN3_SPEED", 0x00820250, "pcie", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_ROP_L2_DISABLE", 0x008202C4, "topology", "option"),
    _reg("OPT_GPC_DISABLE", 0x00820350, "topology", "option"),
    _reg("OPT_FBP_DISABLE", 0x00820364, "memory", "option"),
    _reg("OPT_FBPA_DISABLE", 0x00820368, "memory", "option"),
    _reg("OPT_FBIO_DISABLE", 0x0082036C, "memory", "option"),
    _reg("OPT_NVDEC_DISABLE", 0x00820378, "media", "option"),
    _reg("OPT_PCIE_LANE_DISABLE", 0x00820394, "pcie", "option", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_SPARE_FS", 0x00820398, "floorsweep", "option", source="GA100 register specification"),
    _reg("OPT_SYS_PIPE_DISABLE", 0x008203A4, "mig", "option"),
    _reg("SYS_PIPE_DEFECTIVE", 0x008203AC, "mig", "defect"),
    _reg("OPT_PCIE_DEVIDA", 0x008204D8, "identity", "option"),
    _reg("OPT_SECURE_FUSE_WDATA_WR_SECURE", 0x0082048C, "security", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("TPC_GPC1_DEFECTIVE", 0x00820508, "topology", "defect"),
    _reg("TPC_GPC2_DEFECTIVE", 0x0082050C, "topology", "defect"),
    _reg("TPC_GPC3_DEFECTIVE", 0x00820514, "topology", "defect"),
    _reg("OPT_PCIE_MAGIC_BITS_D", 0x00820520, "pcie", "option", fields=[
        {"name": "GEN4_SPEED_DISABLE", "msb": 25, "lsb": 25},
    ], source="GA100 register specification"),
    _reg("TPC_GPC4_DEFECTIVE", 0x00820564, "topology", "defect"),
    _reg("OPT_PCIE_DEVIDB", 0x0082056C, "identity", "option"),
    _reg("OPT_PCIE_BOOT_GEN23_DISABLE", 0x0082057C, "pcie", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_PCIE_BOOT_GEN3_DISABLE", 0x00820580, "pcie", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("TPC_GPC5_DEFECTIVE", 0x008205AC, "topology", "defect"),
    _reg("GPC_DEFECTIVE", 0x008205C4, "topology", "defect"),
    _reg("TPC_GPC0_DEFECTIVE", 0x008205C8, "topology", "defect"),
    _reg("FBP_DEFECTIVE", 0x008205CC, "memory", "defect"),
    _reg("FBPA_DEFECTIVE", 0x008205D0, "memory", "defect"),
    _reg("FBIO_DEFECTIVE", 0x008205D4, "memory", "defect"),
    _reg("ROP_L2_DEFECTIVE", 0x008205E8, "topology", "defect"),
    _reg("OPT_NVLINK_DISABLE", 0x00820684, "nvlink", "option", fields=[
        {"name": "DATA", "msb": 2, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("NVLINK_DEFECTIVE", 0x0082068C, "nvlink", "defect", fields=[
        {"name": "DATA", "msb": 2, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_PES_DISABLE", 0x00820690, "topology", "option"),
    _reg("PES_DEFECTIVE", 0x00820698, "topology", "defect"),
]

for _gpc_index in range(8):
    CURATED_REGISTERS.append(_reg(
        "OPT_TPC_GPC%d_DISABLE" % _gpc_index,
        0x00820760 + 4 * _gpc_index,
        "topology",
        "option",
    ))

_TPC_RECONFIG_OFFSETS = (
    0x00820788, 0x0082078C, 0x00820790, 0x00820794,
    0x00820798, 0x0082079C, 0x00820B94, 0x00820B90,
)
for _gpc_index, _reconfig_offset in enumerate(_TPC_RECONFIG_OFFSETS):
    CURATED_REGISTERS.append(_reg(
        "OPT_TPC_GPC%d_RECONFIG" % _gpc_index,
        _reconfig_offset,
        "topology",
        "reconfig_option",
        fields=[{"name": "DATA", "msb": 7, "lsb": 0}],
        source="GA100 TPC RECONFIG option map",
    ))

CURATED_REGISTERS.extend([
    _reg("OPT_PERLINK_DISABLE", 0x008207F0, "nvlink", "option", fields=[
        {"name": "DATA", "msb": 11, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_PERLINK", 0x00820820, "nvlink", "control", fields=[
        {"name": "DATA", "msb": 11, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_PCIE_LANE", 0x0082082C, "pcie", "control", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_SPARE_FS", 0x00820830, "floorsweep", "control", source="GA100 register specification"),
    _reg("TPC_GPC7_DEFECTIVE", 0x00820B80, "topology", "defect"),
    _reg("TPC_GPC6_DEFECTIVE", 0x00820B84, "topology", "defect"),
    _reg("STATUS_HALF_FBPA", 0x00820C00, "memory", "status"),
    _reg("STATUS_NVJPG", 0x00820C0C, "media", "status"),
    _reg("STATUS_HALF_LTC", 0x00820C10, "memory", "status"),
    _reg("STATUS_OPT_FBIO", 0x00820C14, "memory", "status"),
    _reg("STATUS_OPT_FBPA", 0x00820C18, "memory", "status"),
    _reg("STATUS_OPT_GPC", 0x00820C1C, "topology", "status"),
    _reg("STATUS_OPT_PERLINK", 0x00820C20, "nvlink", "status", fields=[
        {"name": "DATA", "msb": 11, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_NVDEC", 0x00820C24, "media", "status"),
    _reg("STATUS_OPT_PCIE_LANE", 0x00820C2C, "pcie", "status", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_FB_CONFIG", 0x00820C34, "memory", "status"),
])

for _gpc_index in range(8):
    CURATED_REGISTERS.append(_reg(
        "CTRL_OPT_TPC_GPC%d" % _gpc_index,
        0x00820838 + 4 * _gpc_index,
        "topology",
        "control",
        source="GA100 indexed TPC control map",
    ))
    CURATED_REGISTERS.append(_reg(
        "RECONFIG_OVERRIDE_TPC_GPC%d" % _gpc_index,
        0x00820A40 + 4 * _gpc_index,
        "topology",
        "reconfig_override",
        source="GA100 indexed TPC RECONFIG-override map",
    ))
    CURATED_REGISTERS.append(_reg(
        "DISABLE_EXTEND_TPC_GPC%d" % _gpc_index,
        0x00822800 + 4 * _gpc_index,
        "topology",
        "disable_extend",
        source="GA100 indexed TPC disable-extend map",
    ))

CURATED_REGISTERS.append(_reg(
    "KFUSE_STATE", 0x00825C00, "security", "status",
    source="GA100 KFUSE state register",
))

for _gpc_index in range(8):
    CURATED_REGISTERS.append(_reg(
        "STATUS_OPT_TPC_GPC%d" % _gpc_index,
        0x00820C38 + 4 * _gpc_index,
        "topology",
        "status",
    ))

CURATED_REGISTERS.extend([
    _reg("STATUS_OPT_FBP", 0x00820D38, "memory", "status"),
    _reg("STATUS_OPT_NVENC", 0x00820D68, "media", "status", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_OPT_NVLINK", 0x00820DB8, "nvlink", "status", fields=[
        {"name": "DATA", "msb": 15, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_OPT_SYS_PIPE", 0x00820E30, "mig", "status"),
    _reg("STATUS_OPT_OFA", 0x00820E34, "media", "status", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_SECURE_FUSE_RESENSE_DISABLE", 0x00821058, "security", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("SECURED_BURN_FEATURE_ENABLE", 0x00821108, "security", "status"),

    # Software-override control-point shadows.  These are kept separate from
    # physical option, later CTRL_OPT, effective status, and defect planes.
    _reg("OPT_GPC_DISABLE_CP", 0x0082022C, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC0_DISABLE_CP", 0x00820230, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_FBP_DISABLE_CP", 0x00820234, "memory", "control_shadow", fields=[
        {"name": "DATA", "msb": 11, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_PCIE_LANE_DISABLE_CP", 0x00820248, "pcie", "control_shadow", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_SW_SMC_DISABLE", 0x0082029C, "mig", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_OFA_DISABLE", 0x008202D8, "media", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_ROP_L2_DISABLE_CP", 0x008202E4, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 23, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_OFA_DISABLE_CP", 0x008202EC, "media", "control_shadow", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_OFA_DEFECTIVE", 0x008202F0, "media", "defect"),
    _reg("OPT_NVJPG_DISABLE", 0x00820370, "media", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_SYS_PIPE_DISABLE_CP", 0x008203A8, "mig", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVENC_DISABLE", 0x00820414, "media", "option", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_FBPA_DISABLE_CP", 0x00820454, "memory", "control_shadow", fields=[
        {"name": "DATA", "msb": 23, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVDEC_DISABLE_CP", 0x00820458, "media", "control_shadow", fields=[
        {"name": "DATA", "msb": 4, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVENC_DISABLE_CP", 0x0082045C, "media", "control_shadow", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_FBIO_DISABLE_CP", 0x00820460, "memory", "control_shadow", fields=[
        {"name": "DATA", "msb": 23, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVJPG_DISABLE_CP", 0x00820488, "media", "control_shadow", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC4_DISABLE_CP", 0x008204C4, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC5_DISABLE_CP", 0x008204CC, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC1_DISABLE_CP", 0x00820504, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC3_DISABLE_CP", 0x008205A0, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVJPG_DEFECTIVE", 0x008205BC, "media", "defect"),
    _reg("OPT_NVENC_DEFECTIVE", 0x008205DC, "media", "defect", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVDEC_DEFECTIVE", 0x008205E0, "media", "defect"),
    _reg("OPT_TPC_GPC2_DISABLE_CP", 0x008205F4, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_NVLINK_DISABLE_CP", 0x00820688, "nvlink", "control_shadow", fields=[
        {"name": "DATA", "msb": 2, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_PES_DISABLE_CP", 0x00820694, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 23, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC7_DISABLE_CP", 0x00820B88, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_TPC_GPC6_DISABLE_CP", 0x00820B8C, "topology", "control_shadow", fields=[
        {"name": "DATA", "msb": 7, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("FEATURE_OVERRIDE_ECC", 0x0082380C, "ecc", "feature_override_observation",
         source="GA100 register specification; defined bits mask 0x00999999"),
    _reg("FEATURE_OVERRIDE_ECC_1", 0x00823810, "ecc", "feature_override_observation",
         source="GA100 register specification; defined bits mask 0x003fffff"),
    _reg("FEATURE_READOUT", 0x00823814, "feature", "feature_readout",
         source="GA100 register specification"),
    _reg("FEATURE_READOUT_1", 0x00823818, "feature", "feature_readout",
         source="GA100 register specification"),
    _reg("FEATURE_READOUT_2", 0x00823828, "feature", "feature_readout",
         source="GA100 register specification"),
    _reg("FEATURE_OVERRIDE_ECC_2", 0x0082382C, "ecc", "feature_override_observation",
         source="GA100 register specification; defined bits mask 0x0000000f"),
])

# Direct-header topology scalars and missing option/control/status planes.  They
# close the distinction between physical option, software control and effective
# status; none is a physical OTP-row coordinate.
CURATED_REGISTERS.extend([
    _reg("PTOP_FS4", 0x0002241C, "topology_runtime", "runtime_strap", fields=[
        {"name": "GEN2_PCIE", "msb": 0, "lsb": 0},
        {"name": "FORCE_MULTIMEDIA_DEV", "msb": 1, "lsb": 1},
        {"name": "GEN2_PCIE_SPEED", "msb": 7, "lsb": 7},
    ], source="GA100 register specification", stability="snapshot"),
    _reg("PTOP_SCAL_NUM_GPCS", 0x00022430, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_TPC_PER_GPC", 0x00022434, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_FBPS", 0x00022438, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_FBPAS", 0x0002243C, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_NVENCS", 0x0002244C, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_LTCS", 0x00022454, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_FBPA_PER_FBP", 0x00022458, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_SCAL_NUM_NVLINK", 0x0002246C, "topology_runtime", "physical_ceiling",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("PTOP_FLOORSWEEP_STATUS", 0x00022470, "topology_runtime", "effective_status",
         source="GA100 register specification", stability="snapshot"),
    _reg("DEBUGCTRL_PRIV_LEVEL_MASK", 0x008200E0, "security", "privilege_mask",
         source="GA100 register specification"),
    _reg("OPT_FB_CONFIG", 0x00820328, "memory", "option", fields=[
        {"name": "DATA", "msb": 3, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_HALF_FBPA_ENABLE", 0x0082049C, "memory", "option", fields=[
        {"name": "DATA", "msb": 23, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_PCIE_MAGIC_BITS_B", 0x00820518, "pcie", "option",
         source="GA100 register specification"),
    _reg("OPT_PCIE_MAGIC_BITS_C", 0x0082051C, "pcie", "option",
         source="GA100 register specification"),
    _reg("OPT_PCIE_MAGIC_BITS_A", 0x00820558, "pcie", "option",
         source="GA100 register specification"),
    _reg("OPT_PCIE_LANE_DEFECTIVE", 0x008205E4, "pcie", "defect", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("OPT_SECURE_GSP_DEBUG_DIS", 0x0082074C, "security", "option", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_HALF_FBPA", 0x00820800, "memory", "control", fields=[
        {"name": "DATA", "msb": 31, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_FBIO", 0x00820814, "memory", "control", fields=[
        {"name": "DATA", "msb": 31, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_FBPA", 0x00820818, "memory", "control", fields=[
        {"name": "DATA", "msb": 31, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_GPC", 0x0082081C, "topology", "control", fields=[
        {"name": "DATA", "msb": 31, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_FB_CONFIG", 0x00820834, "memory", "control", fields=[
        {"name": "DATA", "msb": 3, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_FBP", 0x00820938, "memory", "control", fields=[
        {"name": "DATA", "msb": 15, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_NVENC", 0x00820968, "media", "control", fields=[
        {"name": "DATA", "msb": 1, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("CTRL_OPT_NVLINK", 0x008209B8, "nvlink", "control", fields=[
        {"name": "DATA", "msb": 15, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_OPT_DISPLAY", 0x00820C04, "media", "status", fields=[
        {"name": "DATA", "msb": 0, "lsb": 0},
    ], source="GA100 register specification"),
    _reg("STATUS_OPT_SPARE_FS", 0x00820C30, "floorsweep", "status",
         source="GA100 register specification"),
])

# Read-only observation of broadcast FBPA/HBM configuration.  I1500 is runtime
# state because the FB Falcon may poll it.  DATA is catalogued, but excluded
# from ordinary capture and from the kernel READ32 allowlist because reading it
# advances the I1500 transaction state.
CURATED_REGISTERS.extend([
    _reg("FBPA_NUM_ACTIVE_FBPS", 0x009A0164, "hbm", "runtime_topology",
         fields=[{"name": "VALUE", "msb": 4, "lsb": 0}], source="GA100 register specification"),
    _reg("FBPA_CFG0_BROADCAST", 0x009A0200, "hbm", "configuration",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_CFG1_BROADCAST", 0x009A0204, "hbm", "geometry_configuration",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_CSTATUS_RAMAMOUNT_BROADCAST", 0x009A020C, "hbm", "capacity_liveness",
         source="cmp170hx-ffcalc@e28e03a:ffcalc/data/ga100-fuses.json:98; docs/BURNING.md:126",
         profile="hbm-static", stability="snapshot", aliases=["CSTATUS_RAMAMOUNT"],
         identity_confidence_percent=95, interpretation_confidence_percent=65,
         confidence_reason="broadcast address and observed value are current public evidence; it is a liveness/capacity correlate, not a standalone usable-memory proof"),
    _reg("FBPA_GENERIC_MRS", 0x009A0300, "hbm", "mode_register_latch",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_GENERIC_EMRS", 0x009A0304, "hbm", "mode_register_latch",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_GENERIC_EMRS2", 0x009A0320, "hbm", "mode_register_latch",
         source="GA100 register specification; density interpretation remains empirical",
         stability="snapshot"),
    _reg("FBPA_GENERIC_MRS2", 0x009A0334, "hbm", "mode_register_latch",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_GENERIC_MRS3", 0x009A0338, "hbm", "mode_register_latch",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_HBM_CFG0", 0x009A038C, "hbm", "configuration",
         source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_ECC_CONTROL", 0x009A0470, "hbm", "ecc_runtime", fields=[
        {"name": "MASTER_EN", "msb": 0, "lsb": 0},
    ], source="GA100 register specification", stability="snapshot"),
    _reg("FBPA_FBIO_VEND_ID_C0", 0x009A0838, "hbm", "vendor_id",
         source="GA100 register specification"),
    _reg("FBPA_FBIO_VEND_ID_C1", 0x009A083C, "hbm", "vendor_id",
         source="GA100 register specification"),
    _reg("FBPA_TRAINING_STATUS", 0x009A0974, "hbm", "training_status",
         source="GA100 register specification", stability="volatile"),
    _reg("FBPA_HBM_PLL_COEFF_MULTICAST", 0x0098BC98, "hbm", "clock_coefficient_observation",
         source="cmp170hx-ffcalc@e28e03a:ffcalc/data/ga100-fuses.json:105; docs/BURNING.md:129",
         profile="hbm-static", stability="volatile", aliases=["HBM_PLL_COEFF"],
         identity_confidence_percent=95, interpretation_confidence_percent=70,
         confidence_reason="current public loaded/idle observations support a runtime clock coefficient; no fuse-state meaning is claimed"),
    _reg("FBPA_HBM_PLL_PRIV_LEVEL_MASK", 0x009A3C7C, "hbm", "privilege_mask_observation",
         source="cmp170hx-ffcalc@e28e03a:ffcalc/data/ga100-fuses.json:104",
         profile="hbm-static", stability="snapshot", aliases=["HBM_PLL_PLM"],
         all_ones="valid", identity_confidence_percent=90,
         interpretation_confidence_percent=55,
         confidence_reason="current public map identifies a PLL privilege-mask observation; exact bit policy is not independently qualified"),
    _reg("I1500_INSTR", 0x009A3CB4, "hbm_i1500", "runtime_latch",
         source="GA100 register specification", stability="volatile"),
    _reg("I1500_MODE", 0x009A3CB8, "hbm_i1500", "runtime_latch",
         source="GA100 register specification", stability="volatile"),
    _reg("I1500_DATA", 0x009A3CBC, "hbm_i1500", "stateful_data_port",
         source="GA100 register specification; read-to-advance semantics",
         capture_safe=False, access_effect="read_to_advance", stability="volatile",
         profile="stateful-maintenance"),
    _reg("I1500_SHADOW_WIR", 0x009A3CC0, "hbm_i1500", "runtime_latch",
         source="GA100 register specification", stability="volatile"),
    _reg("I1500_SHADOW_WDR", 0x009A3CC4, "hbm_i1500", "runtime_latch",
         source="GA100 register specification", stability="volatile"),
    _reg("I1500_STATUS", 0x009A3CC8, "hbm_i1500", "runtime_status",
         source="GA100 register specification", stability="volatile"),
])

# HBM power-management diagnostics imported from the pinned public
# cmpunlocker map.  These are raw, pure-read observations only: the nearby
# SET/CLEAR and transition registers remain outside every read allowlist.
CURATED_REGISTERS.extend([
    _reg("GSP_RISCV_STATUS", 0x00111240, "firmware_runtime", "riscv_status_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=95, interpretation_confidence_percent=70,
         confidence_reason="current public hbm_pm map and independent public GA100 documentation agree on the address; values remain runtime state"),
    _reg("FB_STOP_OUTPUT", 0x0010A7C0, "hbm_power", "stop_request_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=95, interpretation_confidence_percent=65,
         confidence_reason="current public hbm_pm map exposes the request latch; reading it does not authorize writing the transition control"),
    _reg("FB_STOP_INPUT", 0x0010A7C4, "hbm_power", "stop_ack_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=95, interpretation_confidence_percent=65,
         confidence_reason="current public hbm_pm map exposes the acknowledgement latch; no transition write is included"),
    _reg("PMU_IDLE_THRESHOLD", 0x0010BE00, "hbm_power", "idle_threshold_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=55,
         confidence_reason="current public hbm_pm map; exact GA100 field layout and units are not qualified"),
    _reg("PMU_IDLE_MASK0", 0x0010BE40, "hbm_power", "idle_mask_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=50,
         confidence_reason="current public hbm_pm map; raw mask semantics only"),
    _reg("PMU_IDLE_MASK1", 0x0010BE80, "hbm_power", "idle_mask_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=50,
         confidence_reason="current public hbm_pm map; raw mask semantics only"),
    _reg("PMU_IDLE_MASK2", 0x0010BEC0, "hbm_power", "idle_mask_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=50,
         confidence_reason="current public hbm_pm map; raw mask semantics only"),
    _reg("PMU_IDLE_COUNT", 0x0010BF80, "hbm_power", "idle_count_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=50,
         confidence_reason="current public hbm_pm map; counter width, units and reset behavior are not qualified"),
    _reg("PMU_IDLE_CONTROL", 0x0010BFC0, "hbm_power", "idle_control_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=45,
         confidence_reason="current public hbm_pm map; read observation only and no control fields are decoded"),
    _reg("HBM_PLL_CFG_MC2", 0x0098BC90, "hbm", "pll_configuration_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=90, interpretation_confidence_percent=50,
         confidence_reason="current public hbm_pm map; DVFS-sensitive raw PLL configuration only"),
    _reg("PRI_FENCE", 0x001211FC, "hbm_power", "pri_fence_observation",
         source="cmpunlocker@8b6c556:common/constants.yaml:hbm_pm",
         profile="hbm-static", stability="volatile",
         identity_confidence_percent=95, interpretation_confidence_percent=55,
         confidence_reason="current public hbm_pm map and older public driver implementations agree on the address; no fence write is authorized"),
])


# Complete targeted inventory from fuse-reference-table.md, corrected against
# literal GA100 header identities where available.  Public aliases are retained
# for interoperability, but an alias is not treated as an authoritative field
# interpretation.
CURATED_REGISTERS.extend([
    _reg("PMC_BOOT_42", 0x0000A800, "identity", "silicon_revision_observation",
         source="fuse-reference-table.md:210; direct GA100 macro not located",
         profile="fuse-reference", aliases=["PMC_BOOT_42"],
         badf="availability",
         identity_confidence_percent=85, interpretation_confidence_percent=70,
         confidence_reason="cross-card empirical coordinate; no matching literal macro in audited GA100 headers"),
    _reg("LEGACY_FBIO_FUSE_OBSERVATION", 0x00021C14, "legacy", "unsupported_old_architecture_probe",
         source="fuse-reference-table.md:211", profile="fuse-reference",
         aliases=["FUSE_OPT_FBIO_OLD"], badf="availability",
         identity_confidence_percent=80, interpretation_confidence_percent=95,
         confidence_reason="intentional legacy-address reachability probe; BADF is expected on GA100"),
    _reg("FBHUB_NUM_ACTIVE_LTCS", 0x00100800, "memory_runtime", "runtime_topology",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot"),
    _reg("FBHUB_MEM_PARTITION_BOTTOM", 0x00100B88, "mig", "partition_runtime",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot",
         aliases=["FBHUB_MEM_PART_BOT"]),
    _reg("FBHUB_MEM_PARTITION_MIDDLE", 0x00100B8C, "mig", "partition_runtime",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot",
         aliases=["FBHUB_MEM_PART_MID"]),
    _reg("FBHUB_MEM_PARTITION_BOUNDARY_CFG0", 0x00100B90, "mig", "partition_runtime",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot",
         aliases=["FBHUB_MEM_PART_BCFG0"]),
    _reg("FBHUB_SYSMEM_HSHUB_CONNECTION_CFG", 0x00100B98, "memory_runtime", "routing_configuration",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot",
         aliases=["FBHUB_HSHUB_SYS_CFG"]),
    _reg("MMU_LOCAL_MEMORY_RANGE", 0x00100CE0, "memory_runtime", "local_memory_range_observation",
         source="cmp170hx-ffcalc@e28e03a:ffcalc/data/ga100-fuses.json:99; field width remains unresolved",
         profile="fuse-reference", stability="snapshot", aliases=["MMU_LMR"],
         identity_confidence_percent=95, interpretation_confidence_percent=70,
         confidence_reason="current public GA100 observation agrees on the address and 80-GiB specimen value; the exact field width is not independently qualified"),
    _reg("MMU_NUM_ACTIVE_LTCS", 0x00100EC0, "memory_runtime", "runtime_topology",
         source="GA100 register specification", profile="fuse-reference", stability="snapshot"),
    _reg("GFW_BOOT_PROGRESS", 0x00118234, "firmware_runtime", "boot_progress_observation",
         source="cmp170hx-ffcalc@e28e03a:ffcalc/data/ga100-fuses.json:101; docs/BURNING.md:127",
         profile="fuse-reference", stability="volatile", aliases=["GFW_BOOT"],
         identity_confidence_percent=95, interpretation_confidence_percent=60,
         confidence_reason="address and completed-boot specimen value are public and current; upper-bit semantics and cross-platform terminal value are not qualified"),
    _reg("SKED_CG", 0x00407000, "graphics_runtime", "clock_gating_control",
         source="GA100 register specification", profile="fuse-reference",
         stability="volatile", aliases=["SKED_HW_BLK"], badf="availability"),
    _reg("SKED_PM", 0x00407010, "graphics_runtime", "perfmon_control",
         source="GA100 register specification", profile="fuse-reference",
         stability="volatile", aliases=["SKED_PM_UNK10"], badf="availability"),
    _reg("SKED_HWW_ESR", 0x00407020, "graphics_runtime", "exception_status",
         source="GA100 register specification", profile="fuse-reference",
         stability="volatile", aliases=["SKED_TRAP"], badf="availability"),
    _reg("SKED_HWW_ESR_EN", 0x00407024, "graphics_runtime", "exception_enable",
         source="GA100 register specification", profile="fuse-reference",
         stability="snapshot", aliases=["SKED_TRAP_EN"], badf="availability"),
    _reg("SKED_ACTIVITY", 0x00407054, "graphics_runtime", "activity_status",
         source="GA100 register specification", profile="fuse-reference",
         stability="volatile", aliases=["SKED_UNK54"], badf="availability",
         interpretation_confidence_percent=99,
         confidence_reason="literal direct-header identity supersedes older undocumented SKED label"),
    _reg("FECS_FEAT_OVERRIDE_OBSERVATION", 0x00409664, "graphics_runtime", "undocumented_observation",
         source="fuse-reference-table.md:70; literal GA100 macro not located",
         profile="fuse-reference", stability="volatile", badf="availability",
         aliases=["FECS_FEAT_OVERRIDE"], identity_confidence_percent=80,
         interpretation_confidence_percent=30,
         confidence_reason="empirical address/name only; GA100 commonly returns BADF and no matching audited macro was found"),
    _reg("FECS_FEAT_READOUT_1_OBSERVATION", 0x00409668, "graphics_runtime", "undocumented_observation",
         source="fuse-reference-table.md:71; literal GA100 macro not located",
         profile="fuse-reference", stability="volatile", badf="availability",
         aliases=["FECS_FEAT_READOUT_1"], identity_confidence_percent=80,
         interpretation_confidence_percent=30,
         confidence_reason="empirical address/name only; GA100 commonly returns BADF and no matching audited macro was found"),
    _reg("GPC0_TPC0_TEX_LOD_DBG", 0x00504204, "graphics_runtime", "texture_lod_debug",
         source="GA100 register specification", profile="fuse-reference",
         stability="snapshot", aliases=["SM_ISSUE_RATE_MODIFIER"], badf="availability",
         interpretation_confidence_percent=99,
         confidence_reason="literal header identifies TEX_LOD_DBG; public SM-throttle label/conclusion is contradicted"),
    _reg("OPT_SM_SPEED_SELECT_DP", 0x00820224, "performance", "option",
         fields=[{"name": "DATA", "msb": 0, "lsb": 0}],
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_DP"]),
    _reg("OPT_MEMORY_LOCKED_ENABLED", 0x00820340, "memory", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_MEM_LOCKED"]),
    _reg("OPT_SECURE_FEATURE_OVERRIDE_QUADRO_WR_SECURE", 0x0082038C, "security", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_QUADRO_WR_SEC"]),
    _reg("OPT_FEATURE_FUSES_OVERRIDE_DISABLE", 0x008203F0, "feature", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_FEAT_OVR_DIS"]),
    _reg("OPT_INTERNAL_SKU", 0x008203F4, "identity", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_INTERNAL_SKU"]),
    _reg("OPT_DEVID_SW_OVERRIDE_DIS", 0x00820584, "identity", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_DEVID_SW_OVR_DIS"]),
    _reg("OPT_SM_SPEED_SELECT_FFMA", 0x0082059C, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_FFMA"]),
    _reg("OPT_SECURE_FBPA_MEM_WR_SECURE", 0x00820618, "security", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_FBPA_MEM_WR_SEC"]),
    _reg("OPT_SECURE_NVLINK_MASK_WR_SECURE", 0x00820704, "nvlink", "security_option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_NVLINK_MASK_SEC"]),
    _reg("OPT_SM_SPEED_SELECT_FMLA16", 0x008207D4, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_FMLA16"]),
    _reg("OPT_SM_SPEED_SELECT_FMLA32", 0x008207D8, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_FMLA32"]),
    _reg("OPT_SM_SPEED_SELECT_IMLA0", 0x008207DC, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_IMLA0"]),
    _reg("OPT_SM_SPEED_SELECT_IMLA1", 0x008207E0, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_IMLA1"]),
    _reg("OPT_SM_SPEED_SELECT_IMLA2", 0x008207E4, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_IMLA2"]),
    _reg("OPT_SM_SPEED_SELECT_IMLA3", 0x008207E8, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_IMLA3"]),
    _reg("OPT_SM_SPEED_SELECT_IMLA4", 0x008207EC, "performance", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SS_IMLA4"]),
    _reg("OPT_SECURE_NVLINKS_PHYSICAL_DAMAGE_WR_SECURE", 0x00820BD4, "nvlink", "security_option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_NVLINK_PHYS_DMG"]),
    _reg("OPT_SKU_ID", 0x00821060, "identity", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_SKU_ID"]),
    _reg("OPT_NVLIPT_RESET_COND_DISABLE", 0x00821100, "nvlink", "option",
         source="GA100 register specification", profile="fuse-reference", aliases=["FUSE_NVLIPT_RST_DIS"]),
    _reg("FEATURE_OVERRIDE_ECC_PRIV_LEVEL_MASK", 0x00823800, "security", "privilege_mask",
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_ECC_PLM"],
         all_ones="valid"),
    _reg("FEATURE_OVERRIDE_PRIV_LEVEL_MASK", 0x00823804, "security", "privilege_mask",
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_PLM"],
         all_ones="valid"),
    _reg("FEATURE_OVERRIDE_QUADRO", 0x00823808, "feature", "feature_override_observation",
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_QUADRO"],
         stability="snapshot"),
    _reg("FEATURE_OVERRIDE_SM_SPEED_SELECT", 0x0082381C, "performance", "feature_override_observation",
         fields=[
             {"name": "IMLA0", "msb": 2, "lsb": 0},
             {"name": "IMLA0_OVERRIDE", "msb": 3, "lsb": 3},
             {"name": "FMLA16", "msb": 6, "lsb": 4},
             {"name": "FMLA16_OVERRIDE", "msb": 7, "lsb": 7},
             {"name": "DP", "msb": 8, "lsb": 8},
             {"name": "DP_OVERRIDE", "msb": 11, "lsb": 11},
             {"name": "FMLA32", "msb": 14, "lsb": 12},
             {"name": "FMLA32_OVERRIDE", "msb": 15, "lsb": 15},
             {"name": "FFMA", "msb": 18, "lsb": 16},
             {"name": "FFMA_OVERRIDE", "msb": 19, "lsb": 19},
             {"name": "IMLA1", "msb": 22, "lsb": 20},
             {"name": "IMLA1_OVERRIDE", "msb": 23, "lsb": 23},
             {"name": "IMLA2", "msb": 26, "lsb": 24},
             {"name": "IMLA2_OVERRIDE", "msb": 27, "lsb": 27},
             {"name": "IMLA3", "msb": 30, "lsb": 28},
             {"name": "IMLA3_OVERRIDE", "msb": 31, "lsb": 31},
         ],
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_SM_SPD", "SS0"],
         stability="snapshot"),
    _reg("FEATURE_OVERRIDE_SM_SPEED_SELECT_1", 0x00823820, "performance", "feature_override_observation",
         fields=[
             {"name": "IMLA4", "msb": 2, "lsb": 0},
             {"name": "IMLA4_OVERRIDE", "msb": 3, "lsb": 3},
         ],
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_SM_SPD_1", "SS1"],
         stability="snapshot"),
    _reg("FEATURE_OVERRIDE_ROW_REMAPPER", 0x00823824, "memory", "feature_override_observation",
         source="GA100 register specification", profile="fuse-reference", aliases=["FEAT_OVR_ROW_REMAP"],
         stability="snapshot"),
])

# All 24 GA100 FBPA unicast apertures. Static CFG/CSTATUS reads are separated
# from volatile IEEE-1500 latches, and every DATA port remains catalogued but
# excluded because each read advances the selected WDR stream.
for _fbpa_index in range(24):
    _fbpa_base = 0x00900000 + _fbpa_index * 0x00004000
    CURATED_REGISTERS.extend([
        _reg("FBPA%02d_CFG0" % _fbpa_index, _fbpa_base + 0x0200,
             "hbm_unicast", "configuration", source="GA100 FBPA unicast aperture +0x200",
             profile="hbm-static", stability="snapshot", badf="availability"),
        _reg("FBPA%02d_CFG1" % _fbpa_index, _fbpa_base + 0x0204,
             "hbm_unicast", "geometry_configuration", source="GA100 FBPA unicast aperture +0x204",
             profile="hbm-static", stability="snapshot", badf="availability"),
        _reg("FBPA%02d_CSTATUS_RAMAMOUNT" % _fbpa_index, _fbpa_base + 0x020C,
             "hbm_unicast", "capacity_liveness", source="GA100 FBPA unicast aperture +0x20c",
             profile="hbm-static", stability="snapshot", badf="availability"),
        _reg("FBPA%02d_I1500_INSTR" % _fbpa_index, _fbpa_base + 0x3CB4,
             "hbm_i1500_unicast", "runtime_latch", source="GA100 FBPA unicast aperture +0x3cb4",
             profile="hbm-i1500-latches", stability="volatile", badf="availability"),
        _reg("FBPA%02d_I1500_MODE" % _fbpa_index, _fbpa_base + 0x3CB8,
             "hbm_i1500_unicast", "runtime_latch", source="GA100 FBPA unicast aperture +0x3cb8",
             profile="hbm-i1500-latches", stability="volatile", badf="availability"),
        _reg("FBPA%02d_I1500_DATA" % _fbpa_index, _fbpa_base + 0x3CBC,
             "hbm_i1500_unicast", "stateful_data_port",
             source="GA100 FBPA unicast aperture +0x3cbc; read-to-advance semantics",
             capture_safe=False, access_effect="read_to_advance", stability="volatile",
             badf="availability", profile="stateful-maintenance"),
        _reg("FBPA%02d_I1500_SHADOW_WIR" % _fbpa_index, _fbpa_base + 0x3CC0,
             "hbm_i1500_unicast", "runtime_latch", source="GA100 FBPA unicast aperture +0x3cc0",
             profile="hbm-i1500-latches", stability="volatile", badf="availability"),
        _reg("FBPA%02d_I1500_SHADOW_WDR" % _fbpa_index, _fbpa_base + 0x3CC4,
             "hbm_i1500_unicast", "runtime_latch", source="GA100 FBPA unicast aperture +0x3cc4",
             profile="hbm-i1500-latches", stability="volatile", badf="availability"),
        _reg("FBPA%02d_I1500_STATUS" % _fbpa_index, _fbpa_base + 0x3CC8,
             "hbm_i1500_unicast", "runtime_status", source="GA100 FBPA unicast aperture +0x3cc8",
             profile="hbm-i1500-latches", stability="volatile", badf="availability"),
    ])

# Complete generated GA100 fuse-shadow inventory (15 core, 672 option, 84
# status names).  The exact source hashes are recorded so a newer manifest can
# be diffed rather than silently replacing identities.  Missing coordinates
# are optional shadow-full reads; names that overlap the curated registry are
# retained as aliases.
GA100_SHADOW_MANIFEST_HASHES = {
    "core": "6da9cf37e2855d950ea85317eb5f28586e547c1d69e06df559e9323d8f177554",
    "options": "6bd9a4510729f7072b417bf79c85a2e5a8979984324da3753d2994f9efea646c",
    "status": "1342ed35f2ec0e44add38015717560989f97780607f759feafe49d4e95b48301",
}
_GA100_SHADOW_MANIFEST = """core|CRC_STATUS|00820070\ncore|DISABLE_FUSE_PROGRAM_STATUS|0082007c\ncore|DISABLE_SW_OVERRIDE_STATUS|00820084\ncore|ENABLE_FUSE_PROGRAM_STATUS|00820078\ncore|EN_SW_OVERRIDE|00820040\ncore|FUSEADDR|00820004\ncore|FUSECTRL|00820000\ncore|FUSECTRL_PLM|008200f8\ncore|FUSEDEBUG|00820060\ncore|FUSEOPT_PLM|008200fc\ncore|FUSEWDATA_PLM|008200f4\ncore|OPT_SECURED_BURN_FEATURE_ENABLE|00821108\ncore|PMC_BOOT_0|00000000\ncore|RING_GLOBAL_CTL|00120060\ncore|RING_RESET_PLM|001200a0\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R1_0|0082052c\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R1_1|00820528\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R2_0|00820534\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R2_1|00820530\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R3_0|0082053c\noptions|NV_FUSE_OPT_3GIO_PADCFG_LUT_ADR_R3_1|00820544\noptions|NV_FUSE_OPT_ADC_CAL_FUSE_REV|0082064c\noptions|NV_FUSE_OPT_ADC_CAL_FUSE_TESTSTAGE|008205b0\noptions|NV_FUSE_OPT_ADC_CAL_GPC0|00820650\noptions|NV_FUSE_OPT_ADC_CAL_GPC1|00820654\noptions|NV_FUSE_OPT_ADC_CAL_GPC2|00820658\noptions|NV_FUSE_OPT_ADC_CAL_GPC3|0082065c\noptions|NV_FUSE_OPT_ADC_CAL_GPC4|00820660\noptions|NV_FUSE_OPT_ADC_CAL_GPC5|00820664\noptions|NV_FUSE_OPT_ADC_CAL_GPC6|00821084\noptions|NV_FUSE_OPT_ADC_CAL_GPC7|008210ec\noptions|NV_FUSE_OPT_ADC_CAL_SENSE|00820e1c\noptions|NV_FUSE_OPT_ADC_CAL_SENSE_FUSE_REV|008210e4\noptions|NV_FUSE_OPT_ADC_CAL_SHARED|00820668\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_FUSE_REV|008203b4\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC0|008210b0\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC1|008210b4\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC2|008210b8\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC3|008210bc\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC4|008210d0\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC5|008210d4\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC6|008210d8\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_GPC7|008210dc\noptions|NV_FUSE_OPT_ADC_CODE_ERR_HT_SHARED|008210e0\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_FUSE_REV|00820380\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC0|008210f0\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC1|008210f4\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC2|008210f8\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC3|00821098\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC4|0082109c\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC5|008210a0\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC6|008210a4\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_GPC7|008210a8\noptions|NV_FUSE_OPT_ADC_CODE_ERR_LT_SHARED|008210ac\noptions|NV_FUSE_OPT_ASSR_STICKY_DISABLE|00820594\noptions|NV_FUSE_OPT_ATE_VMIN|00820588\noptions|NV_FUSE_OPT_BAR1_SIZE|00820550\noptions|NV_FUSE_OPT_CP1_COMPLETE|00820570\noptions|NV_FUSE_OPT_CP1_DVS_COMPLETE|0082055c\noptions|NV_FUSE_OPT_CP1_TDIODE_OFFSET|008204c8\noptions|NV_FUSE_OPT_CP2_COMPLETE|00820574\noptions|NV_FUSE_OPT_CP2_DVS_COMPLETE|00820560\noptions|NV_FUSE_OPT_CP2_TDIODE_OFFSET|00820120\noptions|NV_FUSE_OPT_CPM0|00821088\noptions|NV_FUSE_OPT_CPM1|0082108c\noptions|NV_FUSE_OPT_CPM2|00821090\noptions|NV_FUSE_OPT_CPM_REV|00821094\noptions|NV_FUSE_OPT_CP_BIN|008202c8\noptions|NV_FUSE_OPT_CP_REV|008204b8\noptions|NV_FUSE_OPT_CTRL_BLOCK_FUSE_RESET_JTAG_OVERRIDE|008207d0\noptions|NV_FUSE_OPT_CUST_ATE_REV|008204dc\noptions|NV_FUSE_OPT_DEBUG_FEATURE_SW_OVERRIDE_ENABLE|00820148\noptions|NV_FUSE_OPT_DEBUG_SW_OVERRIDE_ENABLE|00820e20\noptions|NV_FUSE_OPT_DEVID_SW_OVERRIDE_DIS|00820584\noptions|NV_FUSE_OPT_DISABLE_GEN3_SPEED|00820250\noptions|NV_FUSE_OPT_DISPLAY_DEFECTIVE|008205d8\noptions|NV_FUSE_OPT_DISPLAY_DISABLE|0082037c\noptions|NV_FUSE_OPT_DISPLAY_DISABLE_CP|0082024c\noptions|NV_FUSE_OPT_DISP_HEAD_DISABLE|00820418\noptions|NV_FUSE_OPT_DISP_HEAD_DISABLE_CP|00820450\noptions|NV_FUSE_OPT_DONE_MAP|008202d0\noptions|NV_FUSE_OPT_ECC_EN|00820228\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_HEADER_PLAINTEXT|008207a0\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_0|00820b20\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_1|00820b24\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_10|00820b48\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_11|00820b4c\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_12|00820b50\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_13|00820b54\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_14|00820b58\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_15|00820b5c\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_16|00820b60\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_17|00820b64\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_18|00820b68\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_19|00820b6c\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_2|00820b28\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_3|00820b2c\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_4|00820b30\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_5|00820b34\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_6|00820b38\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_7|00820b3c\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_8|00820b40\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_KEYS_CIPHERTEXT_9|00820b44\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_PROTECTINFO_PLAINTEXT_0|008203bc\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_PROTECTINFO_PLAINTEXT_1|008203c0\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_PROTECTINFO_PLAINTEXT_2|008203c4\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_PAYLOAD_PROTECTINFO_PLAINTEXT_3|008203c8\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_SIGNATURE_0|008203cc\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_SIGNATURE_1|008203d0\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_SIGNATURE_2|008203d4\noptions|NV_FUSE_OPT_ENC_FUSE_KEY_SIGNATURE_3|008203d8\noptions|NV_FUSE_OPT_FAB_CODE|00820260\noptions|NV_FUSE_OPT_FAB_ID|008202c0\noptions|NV_FUSE_OPT_FBIO_DEFECTIVE|008205d4\noptions|NV_FUSE_OPT_FBIO_DISABLE|0082036c\noptions|NV_FUSE_OPT_FBIO_DISABLE_CP|00820460\noptions|NV_FUSE_OPT_FBPA_DEFECTIVE|008205d0\noptions|NV_FUSE_OPT_FBPA_DISABLE|00820368\noptions|NV_FUSE_OPT_FBPA_DISABLE_CP|00820454\noptions|NV_FUSE_OPT_FBP_DEFECTIVE|008205cc\noptions|NV_FUSE_OPT_FBP_DISABLE|00820364\noptions|NV_FUSE_OPT_FBP_DISABLE_CP|00820234\noptions|NV_FUSE_OPT_FB_CONFIG|00820328\noptions|NV_FUSE_OPT_FB_FALCON_PRI_ACCESS_DISABLE|00820670\noptions|NV_FUSE_OPT_FEATURE_FUSES_OVERRIDE_DISABLE|008203f0\noptions|NV_FUSE_OPT_FIB_REVISION_ID|008202b4\noptions|NV_FUSE_OPT_FPF_CTKID|00824240\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_0|0082426c\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_1|00824270\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_10|00824294\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_11|00824298\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_12|0082429c\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_13|008242a0\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_14|008242a4\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_15|008242a8\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_16|008242ac\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_17|008242b0\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_18|008242b4\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_19|008242b8\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_2|00824274\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_20|008242bc\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_21|008242c0\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_22|008242c4\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_23|008242c8\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_24|008242cc\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_25|008242d0\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_26|008242d4\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_27|008242d8\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_28|008242dc\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_29|008242e0\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_3|00824278\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_30|008242e4\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_31|008242e8\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_4|0082427c\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_5|00824280\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_6|00824284\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_7|00824288\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_8|0082428c\noptions|NV_FUSE_OPT_FPF_FIELD_FEATURE_CONTROL_9|00824290\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_0|008242fc\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_1|00824304\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_10|00824328\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_11|0082432c\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_12|00824330\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_13|00824334\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_14|00824338\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_15|0082433c\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_16|00824340\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_2|00824308\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_3|0082430c\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_4|00824310\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_5|00824314\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_6|00824318\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_7|0082431c\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_8|00824320\noptions|NV_FUSE_OPT_FPF_FIELD_SPARE_FUSES_9|00824324\noptions|NV_FUSE_OPT_FPF_GSP_UCODE10_VERSION|008241e4\noptions|NV_FUSE_OPT_FPF_GSP_UCODE11_VERSION|008241e8\noptions|NV_FUSE_OPT_FPF_GSP_UCODE12_VERSION|008241ec\noptions|NV_FUSE_OPT_FPF_GSP_UCODE13_VERSION|008241f0\noptions|NV_FUSE_OPT_FPF_GSP_UCODE14_VERSION|008241f4\noptions|NV_FUSE_OPT_FPF_GSP_UCODE15_VERSION|008241f8\noptions|NV_FUSE_OPT_FPF_GSP_UCODE16_VERSION|008241fc\noptions|NV_FUSE_OPT_FPF_GSP_UCODE1_VERSION|008241c0\noptions|NV_FUSE_OPT_FPF_GSP_UCODE2_VERSION|008241c4\noptions|NV_FUSE_OPT_FPF_GSP_UCODE3_VERSION|008241c8\noptions|NV_FUSE_OPT_FPF_GSP_UCODE4_VERSION|008241cc\noptions|NV_FUSE_OPT_FPF_GSP_UCODE5_VERSION|008241d0\noptions|NV_FUSE_OPT_FPF_GSP_UCODE6_VERSION|008241d4\noptions|NV_FUSE_OPT_FPF_GSP_UCODE7_VERSION|008241d8\noptions|NV_FUSE_OPT_FPF_GSP_UCODE8_VERSION|008241dc\noptions|NV_FUSE_OPT_FPF_GSP_UCODE9_VERSION|008241e0\noptions|NV_FUSE_OPT_FPF_MINION_UCODE1_VERSION|00824220\noptions|NV_FUSE_OPT_FPF_MINION_UCODE2_VERSION|00824224\noptions|NV_FUSE_OPT_FPF_MINION_UCODE3_VERSION|00824228\noptions|NV_FUSE_OPT_FPF_MINION_UCODE4_VERSION|0082422c\noptions|NV_FUSE_OPT_FPF_MINION_UCODE5_VERSION|00824230\noptions|NV_FUSE_OPT_FPF_MINION_UCODE6_VERSION|00824234\noptions|NV_FUSE_OPT_FPF_MINION_UCODE7_VERSION|00824238\noptions|NV_FUSE_OPT_FPF_MINION_UCODE8_VERSION|0082423c\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE10_VERSION|00824124\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE11_VERSION|00824128\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE12_VERSION|0082412c\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE13_VERSION|00824130\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE14_VERSION|00824134\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE15_VERSION|00824138\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE16_VERSION|0082413c\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE1_VERSION|00824100\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE2_VERSION|00824104\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE3_VERSION|00824108\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE4_VERSION|0082410c\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE5_VERSION|00824110\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE6_VERSION|00824114\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE7_VERSION|00824118\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE8_VERSION|0082411c\noptions|NV_FUSE_OPT_FPF_NVDEC_UCODE9_VERSION|00824120\noptions|NV_FUSE_OPT_FPF_OVERLOCK_INFO|008242f8\noptions|NV_FUSE_OPT_FPF_PMU_UCODE10_VERSION|008241a4\noptions|NV_FUSE_OPT_FPF_PMU_UCODE11_VERSION|008241a8\noptions|NV_FUSE_OPT_FPF_PMU_UCODE12_VERSION|008241ac\noptions|NV_FUSE_OPT_FPF_PMU_UCODE13_VERSION|008241b0\noptions|NV_FUSE_OPT_FPF_PMU_UCODE14_VERSION|008241b4\noptions|NV_FUSE_OPT_FPF_PMU_UCODE15_VERSION|008241b8\noptions|NV_FUSE_OPT_FPF_PMU_UCODE16_VERSION|008241bc\noptions|NV_FUSE_OPT_FPF_PMU_UCODE1_VERSION|00824180\noptions|NV_FUSE_OPT_FPF_PMU_UCODE2_VERSION|00824184\noptions|NV_FUSE_OPT_FPF_PMU_UCODE3_VERSION|00824188\noptions|NV_FUSE_OPT_FPF_PMU_UCODE4_VERSION|0082418c\noptions|NV_FUSE_OPT_FPF_PMU_UCODE5_VERSION|00824190\noptions|NV_FUSE_OPT_FPF_PMU_UCODE6_VERSION|00824194\noptions|NV_FUSE_OPT_FPF_PMU_UCODE7_VERSION|00824198\noptions|NV_FUSE_OPT_FPF_PMU_UCODE8_VERSION|0082419c\noptions|NV_FUSE_OPT_FPF_PMU_UCODE9_VERSION|008241a0\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE10_VERSION|00824164\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE11_VERSION|00824168\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE12_VERSION|0082416c\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE13_VERSION|00824170\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE14_VERSION|00824174\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE15_VERSION|00824178\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE16_VERSION|0082417c\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE1_VERSION|00824140\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE2_VERSION|00824144\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE3_VERSION|00824148\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE4_VERSION|0082414c\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE5_VERSION|00824150\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE6_VERSION|00824154\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE7_VERSION|00824158\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE8_VERSION|0082415c\noptions|NV_FUSE_OPT_FPF_SEC2_UCODE9_VERSION|00824160\noptions|NV_FUSE_OPT_FPF_SKU_CONVERSION|008242ec\noptions|NV_FUSE_OPT_FPF_TPC_ENABLED_COUNT0|008242f0\noptions|NV_FUSE_OPT_FPF_TPC_ENABLED_COUNT1|008242f4\noptions|NV_FUSE_OPT_FPF_UCODE_ACR_HS_REV|00824250\noptions|NV_FUSE_OPT_FPF_UCODE_CTXSW_REV|00824260\noptions|NV_FUSE_OPT_FPF_UCODE_FBFALCON_REV|00824264\noptions|NV_FUSE_OPT_FPF_UCODE_GFW_REV|00824254\noptions|NV_FUSE_OPT_FPF_UCODE_GSP_REV|00824268\noptions|NV_FUSE_OPT_FPF_UCODE_NVDEC_REV|0082425c\noptions|NV_FUSE_OPT_FPF_UCODE_PR_VPR_REV|0082424c\noptions|NV_FUSE_OPT_FPF_UCODE_ROM_FLASH_REV|00824258\noptions|NV_FUSE_OPT_FPF_UCODE_SCRUBBER_BIN_REV|00824244\noptions|NV_FUSE_OPT_FPF_UCODE_SEC2_REV|00824248\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE1_VERSION|00824200\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE2_VERSION|00824204\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE3_VERSION|00824208\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE4_VERSION|0082420c\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE5_VERSION|00824210\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE6_VERSION|00824214\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE7_VERSION|00824218\noptions|NV_FUSE_OPT_FPF_XUSB_UCODE8_VERSION|0082421c\noptions|NV_FUSE_OPT_FT_DONE|008204f4\noptions|NV_FUSE_OPT_FT_REV|008204b4\noptions|NV_FUSE_OPT_FUSE_CLK_SWITCH_DISABLE|00820374\noptions|NV_FUSE_OPT_FUSE_FILE_VERSION|0082105c\noptions|NV_FUSE_OPT_FUSE_FUSEBLOW_REV|00820590\noptions|NV_FUSE_OPT_FUSE_UCODE_ACR_HS_REV|00820254\noptions|NV_FUSE_OPT_FUSE_UCODE_CTXSW_REV|008206f8\noptions|NV_FUSE_OPT_FUSE_UCODE_FBFALCON_REV|00820710\noptions|NV_FUSE_OPT_FUSE_UCODE_GFW_REV|00820258\noptions|NV_FUSE_OPT_FUSE_UCODE_GSP_REV|00820404\noptions|NV_FUSE_OPT_FUSE_UCODE_MINION_REV|00820700\noptions|NV_FUSE_OPT_FUSE_UCODE_NVDEC_REV|008202cc\noptions|NV_FUSE_OPT_FUSE_UCODE_NVENC_REV|0082017c\noptions|NV_FUSE_OPT_FUSE_UCODE_NVJPG_REV|008207fc\noptions|NV_FUSE_OPT_FUSE_UCODE_OFA_REV|008202f4\noptions|NV_FUSE_OPT_FUSE_UCODE_PR_VPR_REV|008202a4\noptions|NV_FUSE_OPT_FUSE_UCODE_ROM_FLASH_REV|008202a0\noptions|NV_FUSE_OPT_FUSE_UCODE_SCRUBBER_BIN_REV|00820180\noptions|NV_FUSE_OPT_FUSE_UCODE_SEC2_REV|00820184\noptions|NV_FUSE_OPT_FUSE_UCODE_XUSB_REV|00820e14\noptions|NV_FUSE_OPT_GC6_ISLAND_DISABLE|00820568\noptions|NV_FUSE_OPT_GC6_NVVDD_OK_THRESHOLD|0082054c\noptions|NV_FUSE_OPT_GLOBAL_POISON_DIS|0082039c\noptions|NV_FUSE_OPT_GPC2CLK_CAP|0082042c\noptions|NV_FUSE_OPT_GPC_DEFECTIVE|008205c4\noptions|NV_FUSE_OPT_GPC_DISABLE|00820350\noptions|NV_FUSE_OPT_GPC_DISABLE_CP|0082022c\noptions|NV_FUSE_OPT_HALF_FBPA_ENABLE|0082049c\noptions|NV_FUSE_OPT_HALF_LTC_ENABLE|0082060c\noptions|NV_FUSE_OPT_HDCP_EN|008202fc\noptions|NV_FUSE_OPT_HDCP_RAM_ACCESS_DIS|008207f4\noptions|NV_FUSE_OPT_HEVC_DEC_DISABLE|00820e04\noptions|NV_FUSE_OPT_HEVC_ENC_DISABLE|00820e00\noptions|NV_FUSE_OPT_HS_DIS_NVDEC1|00820648\noptions|NV_FUSE_OPT_HS_DIS_NVDEC2|0082066c\noptions|NV_FUSE_OPT_HS_DIS_NVDEC3|0082028c\noptions|NV_FUSE_OPT_HS_DIS_NVDEC4|008202f8\noptions|NV_FUSE_OPT_I2CS_ADDR_SEL|00820294\noptions|NV_FUSE_OPT_I2CS_WIDE_EN|00820220\noptions|NV_FUSE_OPT_IDDQ|008202d4\noptions|NV_FUSE_OPT_IDDQ_CP|00820578\noptions|NV_FUSE_OPT_IDDQ_REV|008204a4\noptions|NV_FUSE_OPT_INTERNAL_SKU|008203f4\noptions|NV_FUSE_OPT_INT_TS_A|00820280\noptions|NV_FUSE_OPT_INT_TS_B|00820284\noptions|NV_FUSE_OPT_INT_TS_OTOB_EN|0082027c\noptions|NV_FUSE_OPT_INT_TS_VALID|00820290\noptions|NV_FUSE_OPT_INVALID_BITS_ATE|008201fc\noptions|NV_FUSE_OPT_ISENSE_ADC_FUSE_TESTSTAGE|008210e8\noptions|NV_FUSE_OPT_IST_DEBUG_FA|00820158\noptions|NV_FUSE_OPT_IST_DISABLE|00820e18\noptions|NV_FUSE_OPT_IST_DISABLE_DEBUG_FEATURES|00820150\noptions|NV_FUSE_OPT_JTAG2HOST_PRIV_SEC_EN|00820218\noptions|NV_FUSE_OPT_KAPPA_INFO|00820178\noptions|NV_FUSE_OPT_LOT_CODE_0|00820264\noptions|NV_FUSE_OPT_LOT_CODE_1|00820268\noptions|NV_FUSE_OPT_LTSSM_FAILSAFE_TIMEOUT_DIS|0082058c\noptions|NV_FUSE_OPT_MAJOR_REVISION_ID|008204d0\noptions|NV_FUSE_OPT_MASK_REVISION_ID|008202b8\noptions|NV_FUSE_OPT_MCHIP_EN|00820204\noptions|NV_FUSE_OPT_MEMORY_LOCKED_ENABLED|00820340\noptions|NV_FUSE_OPT_MINOR_EXT_REVISION_ID|008202bc\noptions|NV_FUSE_OPT_MINOR_REVISION_ID|008204d4\noptions|NV_FUSE_OPT_NNE_ID|008204f0\noptions|NV_FUSE_OPT_NOTEBOOK|0082020c\noptions|NV_FUSE_OPT_NVDEC_DEFECTIVE|008205e0\noptions|NV_FUSE_OPT_NVDEC_DISABLE|00820378\noptions|NV_FUSE_OPT_NVDEC_DISABLE_CP|00820458\noptions|NV_FUSE_OPT_NVENC_DEFECTIVE|008205dc\noptions|NV_FUSE_OPT_NVENC_DISABLE|00820414\noptions|NV_FUSE_OPT_NVENC_DISABLE_CP|0082045c\noptions|NV_FUSE_OPT_NVENC_THROTTLE|00820610\noptions|NV_FUSE_OPT_NVJPG_DEFECTIVE|008205bc\noptions|NV_FUSE_OPT_NVJPG_DISABLE|00820370\noptions|NV_FUSE_OPT_NVJPG_DISABLE_CP|00820488\noptions|NV_FUSE_OPT_NVJTAG_PROTECTION_ENABLE|008207f8\noptions|NV_FUSE_OPT_NVLINK_DEFECTIVE|0082068c\noptions|NV_FUSE_OPT_NVLINK_DISABLE|00820684\noptions|NV_FUSE_OPT_NVLINK_DISABLE_CP|00820688\noptions|NV_FUSE_OPT_NVLIPT_RESET_COND_DISABLE|00821100\noptions|NV_FUSE_OPT_NVLIPT_RESET_SEQ_DISABLE|00821104\noptions|NV_FUSE_OPT_NVS_EN|008204f8\noptions|NV_FUSE_OPT_OFA_DEFECTIVE|008202f0\noptions|NV_FUSE_OPT_OFA_DISABLE|008202d8\noptions|NV_FUSE_OPT_OFA_DISABLE_CP|008202ec\noptions|NV_FUSE_OPT_OPENGL_EN|00820200\noptions|NV_FUSE_OPT_OPS_RESERVED|00820278\noptions|NV_FUSE_OPT_PCIE_BOOT_GEN23_DISABLE|0082057c\noptions|NV_FUSE_OPT_PCIE_BOOT_GEN3_DISABLE|00820580\noptions|NV_FUSE_OPT_PCIE_DEVIDA|008204d8\noptions|NV_FUSE_OPT_PCIE_DEVIDB|0082056c\noptions|NV_FUSE_OPT_PCIE_LANE_DEFECTIVE|008205e4\noptions|NV_FUSE_OPT_PCIE_LANE_DISABLE|00820394\noptions|NV_FUSE_OPT_PCIE_LANE_DISABLE_CP|00820248\noptions|NV_FUSE_OPT_PCIE_MAGIC_BITS_A|00820558\noptions|NV_FUSE_OPT_PCIE_MAGIC_BITS_B|00820518\noptions|NV_FUSE_OPT_PCIE_MAGIC_BITS_C|0082051c\noptions|NV_FUSE_OPT_PCIE_MAGIC_BITS_D|00820520\noptions|NV_FUSE_OPT_PDI_0|00820344\noptions|NV_FUSE_OPT_PDI_1|00820348\noptions|NV_FUSE_OPT_PERLINK_DISABLE|008207f0\noptions|NV_FUSE_OPT_PES_DEFECTIVE|00820698\noptions|NV_FUSE_OPT_PES_DISABLE|00820690\noptions|NV_FUSE_OPT_PES_DISABLE_CP|00820694\noptions|NV_FUSE_OPT_PEX_PLL_EN_TERM100|00820524\noptions|NV_FUSE_OPT_PFG|008202dc\noptions|NV_FUSE_OPT_PKC_GSP_ALGO_DISABLE|00820754\noptions|NV_FUSE_OPT_PKC_NVDEC_ALGO_DISABLE|00820740\noptions|NV_FUSE_OPT_PKC_PK_0|00820718\noptions|NV_FUSE_OPT_PKC_PK_1|0082071c\noptions|NV_FUSE_OPT_PKC_PK_2|00820720\noptions|NV_FUSE_OPT_PKC_PK_3|00820724\noptions|NV_FUSE_OPT_PKC_PK_4|00820728\noptions|NV_FUSE_OPT_PKC_PK_5|0082072c\noptions|NV_FUSE_OPT_PKC_PK_6|00820730\noptions|NV_FUSE_OPT_PKC_PK_7|00820734\noptions|NV_FUSE_OPT_PKC_PK_ARRAY_INDEX|00820748\noptions|NV_FUSE_OPT_PKC_PK_SELECTION|00820738\noptions|NV_FUSE_OPT_PKC_PMU_ALGO_DISABLE|0082073c\noptions|NV_FUSE_OPT_PKC_SEC_ALGO_DISABLE|00820744\noptions|NV_FUSE_OPT_PPC_EXT_REG_ACCESS_DIS|008207c4\noptions|NV_FUSE_OPT_PRIVKEY_ACC_DIS|00820324\noptions|NV_FUSE_OPT_PRIV_SEC_EN|00820434\noptions|NV_FUSE_OPT_PRIV_SFK_0|00820210\noptions|NV_FUSE_OPT_PRIV_SFK_1|00820214\noptions|NV_FUSE_OPT_PRIV_SFK_2|0082021c\noptions|NV_FUSE_OPT_PRIV_SFK_3|00820238\noptions|NV_FUSE_OPT_RAM_KP_TSMCDP|008206b8\noptions|NV_FUSE_OPT_RAM_MTSEL_TSMCPDP|008206a8\noptions|NV_FUSE_OPT_RAM_RCT_TSMCDP|008206b0\noptions|NV_FUSE_OPT_RAM_RTSEL_TSMCPDP|008206a4\noptions|NV_FUSE_OPT_RAM_RTSEL_TSMCSP|0082069c\noptions|NV_FUSE_OPT_RAM_SVOP_DP|00820384\noptions|NV_FUSE_OPT_RAM_SVOP_PDP|00820388\noptions|NV_FUSE_OPT_RAM_SVOP_PDPF|00821040\noptions|NV_FUSE_OPT_RAM_SVOP_PDPS|00820430\noptions|NV_FUSE_OPT_RAM_SVOP_PDP_SMCACHE|008203b0\noptions|NV_FUSE_OPT_RAM_SVOP_PDP_SMRF|008207a4\noptions|NV_FUSE_OPT_RAM_SVOP_SP|00820390\noptions|NV_FUSE_OPT_RAM_WCT_TSMCDP|008206b4\noptions|NV_FUSE_OPT_RAM_WTSEL_TSMCPDP|008206ac\noptions|NV_FUSE_OPT_RAM_WTSEL_TSMCSP|008206a0\noptions|NV_FUSE_OPT_REFURBISHED|00820634\noptions|NV_FUSE_OPT_RESET_DELAY_DISABLE|008205c0\noptions|NV_FUSE_OPT_ROM_DRV_STR|008203f8\noptions|NV_FUSE_OPT_ROP_L2_DEFECTIVE|008205e8\noptions|NV_FUSE_OPT_ROP_L2_DISABLE|008202c4\noptions|NV_FUSE_OPT_ROP_L2_DISABLE_CP|008202e4\noptions|NV_FUSE_OPT_ROW_REMAPPER_EN|0082102c\noptions|NV_FUSE_OPT_SAMPLE_BIN|0082032c\noptions|NV_FUSE_OPT_SAMPLE_TYPE|00820330\noptions|NV_FUSE_OPT_SCI_BG_TEMP_COEFF|00820100\noptions|NV_FUSE_OPT_SCI_BG_VALID|00820110\noptions|NV_FUSE_OPT_SCI_BG_VOLTAGE|00820108\noptions|NV_FUSE_OPT_SCI_TEMP_COEFF_ADJ_A|008206d4\noptions|NV_FUSE_OPT_SCI_TEMP_COEFF_ADJ_B|008206d8\noptions|NV_FUSE_OPT_SECURED_BURN_FEATURE_ENABLE|00821108\noptions|NV_FUSE_OPT_SECURE_ADCS_WR_SECURE|00820140\noptions|NV_FUSE_OPT_SECURE_BA_WR_SECURE|008205a4\noptions|NV_FUSE_OPT_SECURE_CLOCKS_AZA2XBITCLK_WR_SECURE|00821048\noptions|NV_FUSE_OPT_SECURE_CLOCKS_FMON_WR_SECURE|00820bfc\noptions|NV_FUSE_OPT_SECURE_CLOCKS_MAUDCLK_WR_SECURE|0082104c\noptions|NV_FUSE_OPT_SECURE_CLOCKS_POISON_PILL_WR_SECURE|00820678\noptions|NV_FUSE_OPT_SECURE_CLOCKS_PWRCLK_WR_SECURE|00821050\noptions|NV_FUSE_OPT_SECURE_DECODE_TRAP_WR_SECURE|008205fc\noptions|NV_FUSE_OPT_SECURE_DISPPLL_PHYSICAL_DAMAGE_WR_SECURE|00820bf8\noptions|NV_FUSE_OPT_SECURE_FBIO_WR_SECURE|00820620\noptions|NV_FUSE_OPT_SECURE_FBPA_ECC_WR_SECURE|00820194\noptions|NV_FUSE_OPT_SECURE_FBPA_I1500_WR_SECURE|008205f8\noptions|NV_FUSE_OPT_SECURE_FBPA_ILA_WR_SECURE|008204ec\noptions|NV_FUSE_OPT_SECURE_FBPA_IOBIST_WR_SECURE|008204e0\noptions|NV_FUSE_OPT_SECURE_FBPA_MEM_WR_SECURE|00820618\noptions|NV_FUSE_OPT_SECURE_FB_FALCON_FUNCTION_RD_SECURE|00820624\noptions|NV_FUSE_OPT_SECURE_FB_FALCON_FUNCTION_WR_SECURE|00820628\noptions|NV_FUSE_OPT_SECURE_FEATURE_OVERRIDE_ECC_WR_SECURE|00820188\noptions|NV_FUSE_OPT_SECURE_FEATURE_OVERRIDE_QUADRO_WR_SECURE|0082038c\noptions|NV_FUSE_OPT_SECURE_FEATURE_OVERRIDE_REMAPPER_WR_SECURE|00820298\noptions|NV_FUSE_OPT_SECURE_FUSE_CLK_WR_SECURE|00820310\noptions|NV_FUSE_OPT_SECURE_FUSE_CTRL_WR_SECURE|008205b4\noptions|NV_FUSE_OPT_SECURE_FUSE_DEBUGCTRL_WR_SECURE|00820b7c\noptions|NV_FUSE_OPT_SECURE_FUSE_FLOORSWEEP_WR_SECURE|00820598\noptions|NV_FUSE_OPT_SECURE_FUSE_IDDQINFO_RD_SECURE|008205b8\noptions|NV_FUSE_OPT_SECURE_FUSE_KAPPAINFO_RD_SECURE|008201f8\noptions|NV_FUSE_OPT_SECURE_FUSE_RESENSE_DISABLE|00821058\noptions|NV_FUSE_OPT_SECURE_FUSE_SPEEDOINFO_RD_SECURE|00820630\noptions|NV_FUSE_OPT_SECURE_FUSE_SRAM_VMIN_RD_SECURE|0082041c\noptions|NV_FUSE_OPT_SECURE_FUSE_WDATA_WR_SECURE|0082048c\noptions|NV_FUSE_OPT_SECURE_GC6_BSI_DEBUG|008201c0\noptions|NV_FUSE_OPT_SECURE_GC6_BSI_RAM|008201b8\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_DEBUG|008201c8\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_FAN|008201c4\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_MAST_FSM|008201f4\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_OVERT|00820354\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_PWR_SEQ|008201f0\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_PWR_SEQ_MISC|00820118\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_SMBUS_WR_SECURE|00820e10\noptions|NV_FUSE_OPT_SECURE_GC6_SCI_SYSTEM|008201cc\noptions|NV_FUSE_OPT_SECURE_GCC_DBG_WR_SECURE|00821044\noptions|NV_FUSE_OPT_SECURE_GPCPLL_PHYSICAL_DAMAGE_WR_SECURE|00820bd0\noptions|NV_FUSE_OPT_SECURE_GSP_DEBUG_DIS|0082074c\noptions|NV_FUSE_OPT_SECURE_HDCP22_CTRL_RD_SECURE|00820320\noptions|NV_FUSE_OPT_SECURE_HDCP22_CTRL_WR_SECURE|00820680\noptions|NV_FUSE_OPT_SECURE_HDCP22_SECRET_RD_SECURE|00820490\noptions|NV_FUSE_OPT_SECURE_HDCP22_SECRET_WR_SECURE|0082046c\noptions|NV_FUSE_OPT_SECURE_HDCP_VPR_POLICY_WR_SECURE|00820240\noptions|NV_FUSE_OPT_SECURE_HOST2JTAG_BOUNDARY_SCAN_DISABLE|0082070c\noptions|NV_FUSE_OPT_SECURE_HOST2JTAG_SHA2_EN|00820750\noptions|NV_FUSE_OPT_SECURE_HOST2JTAG_STANDARD_MODE|0082075c\noptions|NV_FUSE_OPT_SECURE_HOST_DBG_BUS_WR_SECURE|00820128\noptions|NV_FUSE_OPT_SECURE_HOST_FB_TIMEOUT_WR_SECURE|00820160\noptions|NV_FUSE_OPT_SECURE_HOST_PBUS_ACCESS_WR_SECURE|00820758\noptions|NV_FUSE_OPT_SECURE_HOST_PFIFO_FB_IFACE_WR_SECURE|0082061c\noptions|NV_FUSE_OPT_SECURE_ISM_WR_SECURE|00820318\noptions|NV_FUSE_OPT_SECURE_JTAG_SECUREID_VALID|00820638\noptions|NV_FUSE_OPT_SECURE_LOCAL_TRAP_WR_SECURE|00820334\noptions|NV_FUSE_OPT_SECURE_MINION_DEBUG_DIS|008206fc\noptions|NV_FUSE_OPT_SECURE_MMU_PLM_WR_SECURE|00820198\noptions|NV_FUSE_OPT_SECURE_NVDEC_DEBUG_DIS|00820644\noptions|NV_FUSE_OPT_SECURE_NVDPLL_PHYSICAL_DAMAGE_WR_SECURE|00820bf4\noptions|NV_FUSE_OPT_SECURE_NVLINKS_PHYSICAL_DAMAGE_WR_SECURE|00820bd4\noptions|NV_FUSE_OPT_SECURE_NVLINK_BUFFER_READY_SECURE|00820708\noptions|NV_FUSE_OPT_SECURE_NVLINK_MASK_WR_SECURE|00820704\noptions|NV_FUSE_OPT_SECURE_NV_P2P_PLM_WR_SECURE|00821030\noptions|NV_FUSE_OPT_SECURE_PFB_AMAP_WR_SECURE|008203fc\noptions|NV_FUSE_OPT_SECURE_PFB_CYA_WR_SECURE|00820400\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_CYA_WR_SECURE|0082040c\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_DBG_WR_SECURE|00820428\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_LTC_CFG_WR_SECURE|00820440\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_MSCG_WR_SECURE|00820438\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_NVL_CFG_WR_SECURE|00820444\noptions|NV_FUSE_OPT_SECURE_PFB_HSHUB_PERF_WR_SECURE|0082043c\noptions|NV_FUSE_OPT_SECURE_PFB_NISO_SCRUB_WR_SECURE|00820408\noptions|NV_FUSE_OPT_SECURE_PJTAG_ACCESS_WR_SECURE|00820170\noptions|NV_FUSE_OPT_SECURE_PLTS_CBC_WR_SECURE|00820464\noptions|NV_FUSE_OPT_SECURE_PLTS_CGATE_WR_SECURE|00820484\noptions|NV_FUSE_OPT_SECURE_PLTS_CYA_COH_WR_SECURE|008203a0\noptions|NV_FUSE_OPT_SECURE_PLTS_CYA_ECC_WR_SECURE|00820478\noptions|NV_FUSE_OPT_SECURE_PLTS_CYA_WR_SECURE|0082047c\noptions|NV_FUSE_OPT_SECURE_PLTS_DSTG_WR_SECURE|00820474\noptions|NV_FUSE_OPT_SECURE_PLTS_IQ_WR_SECURE|0082034c\noptions|NV_FUSE_OPT_SECURE_PLTS_TSTG_WR_SECURE|00820480\noptions|NV_FUSE_OPT_SECURE_PLTS_VPR_LEAKAGE_WR_SECURE|00820470\noptions|NV_FUSE_OPT_SECURE_PMC_HOST_GRP0_WR_SECURE|00820600\noptions|NV_FUSE_OPT_SECURE_PMGR_FAN_WR_SECURE|00821054\noptions|NV_FUSE_OPT_SECURE_PMGR_GPIO_INPUT_CNTL_WR_SECURE|008206d0\noptions|NV_FUSE_OPT_SECURE_PMGR_GPIO_OUT_WR_SECURE|008206cc\noptions|NV_FUSE_OPT_SECURE_PMGR_GPIO_WR_SECURE|00820154\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C0_WR_SECURE|00820104\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C1_WR_SECURE|0082010c\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C2_WR_SECURE|00820114\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C3_WR_SECURE|0082011c\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C4_WR_SECURE|00820124\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C5_WR_SECURE|0082012c\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C6_WR_SECURE|00820134\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C7_WR_SECURE|0082013c\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C8_WR_SECURE|00820144\noptions|NV_FUSE_OPT_SECURE_PMGR_I2C9_WR_SECURE|0082014c\noptions|NV_FUSE_OPT_SECURE_PMGR_PWR_WR_SECURE|0082015c\noptions|NV_FUSE_OPT_SECURE_PMGR_ROM_WR_SECURE|00820164\noptions|NV_FUSE_OPT_SECURE_PMU_DEBUG_DIS|00820640\noptions|NV_FUSE_OPT_SECURE_PMU_ELPG_DBG_WR_SECURE|008207cc\noptions|NV_FUSE_OPT_SECURE_PMU_ELPG_WR_SECURE|0082016c\noptions|NV_FUSE_OPT_SECURE_PMU_POWER_CONTROL_WR_SECURE|00820130\noptions|NV_FUSE_OPT_SECURE_PMU_RST_WR_SECURE|00820174\noptions|NV_FUSE_OPT_SECURE_PMU_SPLIT_RAIL_WR_SECURE|008201ec\noptions|NV_FUSE_OPT_SECURE_PMU_SW_CLIENT_WR_SECURE|008207c8\noptions|NV_FUSE_OPT_SECURE_PRI_CONFIG_REG_WR_SECURE|00820190\noptions|NV_FUSE_OPT_SECURE_PRI_SEQUENCER_WR_SECURE|00820338\noptions|NV_FUSE_OPT_SECURE_PRI_SOURCE_ISOLATION_EN|00820410\noptions|NV_FUSE_OPT_SECURE_RAMCHAIN_CMD_WR_SECURE|00820494\noptions|NV_FUSE_OPT_SECURE_RING_PRIV_LEVEL_WR_SECURE|00820604\noptions|NV_FUSE_OPT_SECURE_RING_RESET_PRIV_LEVEL_WR_SECURE|00820608\noptions|NV_FUSE_OPT_SECURE_SCANDEBUG_ACCESS_DISABLE|008202e0\noptions|NV_FUSE_OPT_SECURE_SCC_DIS|0082023c\noptions|NV_FUSE_OPT_SECURE_SECENGINE_DEBUG_DIS|0082063c\noptions|NV_FUSE_OPT_SECURE_SE_CONTROL_SECURE|008203dc\noptions|NV_FUSE_OPT_SECURE_SPPLL0_PRIV_SECURE_WR_SECURE|00820be0\noptions|NV_FUSE_OPT_SECURE_SPPLL1_PRIV_SECURE_WR_SECURE|00820bdc\noptions|NV_FUSE_OPT_SECURE_SRAM_ECC_WR_SECURE|00820138\noptions|NV_FUSE_OPT_SECURE_SYSPLL_PHYSICAL_DAMAGE_WR_SECURE|00820bf0\noptions|NV_FUSE_OPT_SECURE_TARGET_MASK_WR_SECURE|00820614\noptions|NV_FUSE_OPT_SECURE_THERM_FANTACH_WR_SECURE|0082018c\noptions|NV_FUSE_OPT_SECURE_THERM_I2CSLAVE_WR_SECURE|0082019c\noptions|NV_FUSE_OPT_SECURE_THERM_INTRUPT_WR_SECURE|008201a0\noptions|NV_FUSE_OPT_SECURE_THERM_PP_WR_SECURE|008201a8\noptions|NV_FUSE_OPT_SECURE_THERM_PWRGATE_0_WR_SECURE|008201ac\noptions|NV_FUSE_OPT_SECURE_THERM_PWRGATE_1_WR_SECURE|008201b0\noptions|NV_FUSE_OPT_SECURE_THERM_SLEEPCTL_WR_SECURE|008201b4\noptions|NV_FUSE_OPT_SECURE_THERM_SLOWDOWN_0_WR_SECURE|008201bc\noptions|NV_FUSE_OPT_SECURE_THERM_SLOWDOWN_1_WR_SECURE|008201d0\noptions|NV_FUSE_OPT_SECURE_THERM_SMARTFAN_0_WR_SECURE|008201d4\noptions|NV_FUSE_OPT_SECURE_THERM_SMARTFAN_1_WR_SECURE|008201d8\noptions|NV_FUSE_OPT_SECURE_THERM_THERMAL_LOW_WR_SECURE|008201dc\noptions|NV_FUSE_OPT_SECURE_THERM_THERMAL_MED_HIGH_0_WR_SECURE|008201e0\noptions|NV_FUSE_OPT_SECURE_THERM_THERMAL_MED_HIGH_1_WR_SECURE|008201e4\noptions|NV_FUSE_OPT_SECURE_THERM_VIDCTRL_WR_SECURE|008201e8\noptions|NV_FUSE_OPT_SECURE_TRIMMER_CTRL_PRIV_SECURE_WR_SECURE|008202a8\noptions|NV_FUSE_OPT_SECURE_TSENSE_CLK_PRIV_SECURE_WR_SECURE|00820208\noptions|NV_FUSE_OPT_SECURE_TSENSE_WR_SECURE|00821000\noptions|NV_FUSE_OPT_SECURE_UPHY_CFG_WR_SECURE|008201a4\noptions|NV_FUSE_OPT_SECURE_UPSTREAM_WBOR_HDCP_WR_SECURE|00820420\noptions|NV_FUSE_OPT_SECURE_UTILSCLK_PRIV_SECURE_WR_SECURE|00820674\noptions|NV_FUSE_OPT_SECURE_UTMIPLL_PHYSICAL_DAMAGE_WR_SECURE|00820be4\noptions|NV_FUSE_OPT_SECURE_VPLLS_PHYSICAL_DAMAGE_WR_SECURE|00820be8\noptions|NV_FUSE_OPT_SECURE_WBOR0_HDCP_WR_SECURE|00820424\noptions|NV_FUSE_OPT_SECURE_WBOR1_HDCP_WR_SECURE|00820468\noptions|NV_FUSE_OPT_SECURE_XBARPLL_PHYSICAL_DAMAGE_WR_SECURE|00820bec\noptions|NV_FUSE_OPT_SECURE_XBAR_CYA_WR_SECURE|00820448\noptions|NV_FUSE_OPT_SECURE_XBAR_PM_WR_SECURE|0082044c\noptions|NV_FUSE_OPT_SECURE_XP_PROTECT_DROPOFF_CYA_GRP_WR_SECURE|00821078\noptions|NV_FUSE_OPT_SECURE_XP_PROTECT_DROPOFF_NON_CYA_GRP_WR_SECURE|0082107c\noptions|NV_FUSE_OPT_SECURE_XP_PROTECT_DROPOFF_OTHERS_WR_SECURE|00821080\noptions|NV_FUSE_OPT_SECURE_XTAL4X_PRIV_SECURE_WR_SECURE|00820bd8\noptions|NV_FUSE_OPT_SECURE_XUSB_DEBUG_DIS|00820b98\noptions|NV_FUSE_OPT_SECURE_XVE_DEFAULT_SECURE_WR_SECURE|00821038\noptions|NV_FUSE_OPT_SECURE_XVE_PROTECT_DROPOFF_CYA_GRP_WR_SECURE|0082106c\noptions|NV_FUSE_OPT_SECURE_XVE_PROTECT_DROPOFF_NON_CYA_GRP_WR_SECURE|00821070\noptions|NV_FUSE_OPT_SECURE_XVE_PROTECT_DROPOFF_OTHERS_WR_SECURE|00821074\noptions|NV_FUSE_OPT_SECURE_XVE_RESET_CTRL_WR_SECURE|00821034\noptions|NV_FUSE_OPT_SEC_DISABLE|008204fc\noptions|NV_FUSE_OPT_SFK_ACC_DIS|008205a8\noptions|NV_FUSE_OPT_SKU_ID|00821060\noptions|NV_FUSE_OPT_SLT_REV|008204bc\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_DP|00820224\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_FFMA|0082059c\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_FMLA16|008207d4\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_FMLA32|008207d8\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_IMLA0|008207dc\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_IMLA1|008207e0\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_IMLA2|008207e4\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_IMLA3|008207e8\noptions|NV_FUSE_OPT_SM_SPEED_SELECT_IMLA4|008207ec\noptions|NV_FUSE_OPT_SM_TTU_EN|00820168\noptions|NV_FUSE_OPT_SPARE_ADDR_DUMMY|00820e38\noptions|NV_FUSE_OPT_SPARE_FS|00820398\noptions|NV_FUSE_OPT_SPEEDO0|008204a8\noptions|NV_FUSE_OPT_SPEEDO1|008204ac\noptions|NV_FUSE_OPT_SPEEDO2|008204b0\noptions|NV_FUSE_OPT_SPEEDO_REV|008204a0\noptions|NV_FUSE_OPT_SPLIT_RAIL_OVERT_BYPASS|008204e8\noptions|NV_FUSE_OPT_SPLIT_RAIL_SD_CLAMP_BYPASS|00820244\noptions|NV_FUSE_OPT_SRAM_REPAIR|008202b0\noptions|NV_FUSE_OPT_SRAM_VMIN_BIN|00820e08\noptions|NV_FUSE_OPT_SRAM_VMIN_BIN_REV|00820e0c\noptions|NV_FUSE_OPT_SUBREVISION|0082033c\noptions|NV_FUSE_OPT_SUB_REGION_NO_OVERRIDE|008205f0\noptions|NV_FUSE_OPT_SW_SMC_DISABLE|0082029c\noptions|NV_FUSE_OPT_SW_VPR_ENABLED|008206f4\noptions|NV_FUSE_OPT_SYS_PIPE_DEFECTIVE|008203ac\noptions|NV_FUSE_OPT_SYS_PIPE_DISABLE|008203a4\noptions|NV_FUSE_OPT_SYS_PIPE_DISABLE_CP|008203a8\noptions|NV_FUSE_OPT_TITAN_EN|008202ac\noptions|NV_FUSE_OPT_TPC_GPC0_DEFECTIVE|008205c8\noptions|NV_FUSE_OPT_TPC_GPC0_DISABLE|00820760\noptions|NV_FUSE_OPT_TPC_GPC0_DISABLE_CP|00820230\noptions|NV_FUSE_OPT_TPC_GPC0_RECONFIG|00820788\noptions|NV_FUSE_OPT_TPC_GPC1_DEFECTIVE|00820508\noptions|NV_FUSE_OPT_TPC_GPC1_DISABLE|00820764\noptions|NV_FUSE_OPT_TPC_GPC1_DISABLE_CP|00820504\noptions|NV_FUSE_OPT_TPC_GPC1_RECONFIG|0082078c\noptions|NV_FUSE_OPT_TPC_GPC2_DEFECTIVE|0082050c\noptions|NV_FUSE_OPT_TPC_GPC2_DISABLE|00820768\noptions|NV_FUSE_OPT_TPC_GPC2_DISABLE_CP|008205f4\noptions|NV_FUSE_OPT_TPC_GPC2_RECONFIG|00820790\noptions|NV_FUSE_OPT_TPC_GPC3_DEFECTIVE|00820514\noptions|NV_FUSE_OPT_TPC_GPC3_DISABLE|0082076c\noptions|NV_FUSE_OPT_TPC_GPC3_DISABLE_CP|008205a0\noptions|NV_FUSE_OPT_TPC_GPC3_RECONFIG|00820794\noptions|NV_FUSE_OPT_TPC_GPC4_DEFECTIVE|00820564\noptions|NV_FUSE_OPT_TPC_GPC4_DISABLE|00820770\noptions|NV_FUSE_OPT_TPC_GPC4_DISABLE_CP|008204c4\noptions|NV_FUSE_OPT_TPC_GPC4_RECONFIG|00820798\noptions|NV_FUSE_OPT_TPC_GPC5_DEFECTIVE|008205ac\noptions|NV_FUSE_OPT_TPC_GPC5_DISABLE|00820774\noptions|NV_FUSE_OPT_TPC_GPC5_DISABLE_CP|008204cc\noptions|NV_FUSE_OPT_TPC_GPC5_RECONFIG|0082079c\noptions|NV_FUSE_OPT_TPC_GPC6_DEFECTIVE|00820b84\noptions|NV_FUSE_OPT_TPC_GPC6_DISABLE|00820778\noptions|NV_FUSE_OPT_TPC_GPC6_DISABLE_CP|00820b8c\noptions|NV_FUSE_OPT_TPC_GPC6_RECONFIG|00820b94\noptions|NV_FUSE_OPT_TPC_GPC7_DEFECTIVE|00820b80\noptions|NV_FUSE_OPT_TPC_GPC7_DISABLE|0082077c\noptions|NV_FUSE_OPT_TPC_GPC7_DISABLE_CP|00820b88\noptions|NV_FUSE_OPT_TPC_GPC7_RECONFIG|00820b90\noptions|NV_FUSE_OPT_TS_CALIBRATE|00820288\noptions|NV_FUSE_OPT_TS_CAL_AVG|00821008\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_0|0082100c\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_1|00821010\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_2|00821014\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_3|00821018\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_4|0082101c\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_5|00821020\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_6|00821024\noptions|NV_FUSE_OPT_TS_CAL_OFFSET_7|00821028\noptions|NV_FUSE_OPT_TS_DIS_MASK|00821004\noptions|NV_FUSE_OPT_TS_TC|008204c0\noptions|NV_FUSE_OPT_UPHY_PLL_BANDGAP_TRIM_NVHS_0|00820300\noptions|NV_FUSE_OPT_UPHY_PLL_BANDGAP_TRIM_NVHS_1|00820304\noptions|NV_FUSE_OPT_UPHY_PLL_BANDGAP_TRIM_NVHS_2|00820308\noptions|NV_FUSE_OPT_UPHY_PLL_BANDGAP_TRIM_PCIE|0082030c\noptions|NV_FUSE_OPT_USB2_CAL|00820b9c\noptions|NV_FUSE_OPT_VENDOR_CODE|0082025c\noptions|NV_FUSE_OPT_VGX_EN|00820510\noptions|NV_FUSE_OPT_VP8_VP9_DISABLE|00820500\noptions|NV_FUSE_OPT_VPR_ENABLED|0082067c\noptions|NV_FUSE_OPT_WAFER_ID|0082026c\noptions|NV_FUSE_OPT_WPR_ENABLED|008205ec\noptions|NV_FUSE_OPT_XP_DC_GAIN_ADAPTIVE_CTRL|00820540\noptions|NV_FUSE_OPT_XP_DFE_ENABLE_CTRL|00820538\noptions|NV_FUSE_OPT_XP_DYNAMIC_EQ_TRAINING_CTRL|00820548\noptions|NV_FUSE_OPT_XTAL16X_FSM_DISABLE|008202e8\noptions|NV_FUSE_OPT_XUSB_REG_SECURE_EN|008207c0\noptions|NV_FUSE_OPT_XV2CLK_RESET_RETIME_DISABLE|00821064\noptions|NV_FUSE_OPT_XVE_10B_TAG_REQUESTER_DISABLE|00821068\noptions|NV_FUSE_OPT_X_COORDINATE|00820270\noptions|NV_FUSE_OPT_Y_COORDINATE|00820274\noptions|NV_FUSE_OPT_ZCULL_DISABLE|0082035c\nstatus|STATUS_OPT_DISPLAY|00820c04\nstatus|STATUS_OPT_DISP_HEAD|00820c08\nstatus|STATUS_OPT_FBIO|00820c14\nstatus|STATUS_OPT_FBP|00820d38\nstatus|STATUS_OPT_FBPA|00820c18\nstatus|STATUS_OPT_FB_CONFIG|00820c34\nstatus|STATUS_OPT_GC6_ISLAND|00820d6c\nstatus|STATUS_OPT_GPC|00820c1c\nstatus|STATUS_OPT_HALF_FBPA|00820c00\nstatus|STATUS_OPT_HALF_LTC|00820c10\nstatus|STATUS_OPT_NVDEC|00820c24\nstatus|STATUS_OPT_NVENC|00820d68\nstatus|STATUS_OPT_NVJPG|00820c0c\nstatus|STATUS_OPT_NVLINK|00820db8\nstatus|STATUS_OPT_OFA|00820e34\nstatus|STATUS_OPT_PCIE_LANE|00820c2c\nstatus|STATUS_OPT_PERLINK|00820c20\nstatus|STATUS_OPT_PES_GPC[0]|00820dbc\nstatus|STATUS_OPT_PES_GPC[10]|00820de4\nstatus|STATUS_OPT_PES_GPC[11]|00820de8\nstatus|STATUS_OPT_PES_GPC[12]|00820dec\nstatus|STATUS_OPT_PES_GPC[13]|00820df0\nstatus|STATUS_OPT_PES_GPC[14]|00820df4\nstatus|STATUS_OPT_PES_GPC[15]|00820df8\nstatus|STATUS_OPT_PES_GPC[1]|00820dc0\nstatus|STATUS_OPT_PES_GPC[2]|00820dc4\nstatus|STATUS_OPT_PES_GPC[3]|00820dc8\nstatus|STATUS_OPT_PES_GPC[4]|00820dcc\nstatus|STATUS_OPT_PES_GPC[5]|00820dd0\nstatus|STATUS_OPT_PES_GPC[6]|00820dd4\nstatus|STATUS_OPT_PES_GPC[7]|00820dd8\nstatus|STATUS_OPT_PES_GPC[8]|00820ddc\nstatus|STATUS_OPT_PES_GPC[9]|00820de0\nstatus|STATUS_OPT_ROP_L2_FBP[0]|00820d70\nstatus|STATUS_OPT_ROP_L2_FBP[10]|00820d98\nstatus|STATUS_OPT_ROP_L2_FBP[11]|00820d9c\nstatus|STATUS_OPT_ROP_L2_FBP[12]|00820da0\nstatus|STATUS_OPT_ROP_L2_FBP[13]|00820da4\nstatus|STATUS_OPT_ROP_L2_FBP[14]|00820da8\nstatus|STATUS_OPT_ROP_L2_FBP[15]|00820dac\nstatus|STATUS_OPT_ROP_L2_FBP[1]|00820d74\nstatus|STATUS_OPT_ROP_L2_FBP[2]|00820d78\nstatus|STATUS_OPT_ROP_L2_FBP[3]|00820d7c\nstatus|STATUS_OPT_ROP_L2_FBP[4]|00820d80\nstatus|STATUS_OPT_ROP_L2_FBP[5]|00820d84\nstatus|STATUS_OPT_ROP_L2_FBP[6]|00820d88\nstatus|STATUS_OPT_ROP_L2_FBP[7]|00820d8c\nstatus|STATUS_OPT_ROP_L2_FBP[8]|00820d90\nstatus|STATUS_OPT_ROP_L2_FBP[9]|00820d94\nstatus|STATUS_OPT_SEC|00820c28\nstatus|STATUS_OPT_SPARE_FS|00820c30\nstatus|STATUS_OPT_SYS_PIPE|00820e30\nstatus|STATUS_OPT_TPC_GPC[0]|00820c38\nstatus|STATUS_OPT_TPC_GPC[10]|00820c60\nstatus|STATUS_OPT_TPC_GPC[11]|00820c64\nstatus|STATUS_OPT_TPC_GPC[12]|00820c68\nstatus|STATUS_OPT_TPC_GPC[13]|00820c6c\nstatus|STATUS_OPT_TPC_GPC[14]|00820c70\nstatus|STATUS_OPT_TPC_GPC[15]|00820c74\nstatus|STATUS_OPT_TPC_GPC[16]|00820c78\nstatus|STATUS_OPT_TPC_GPC[17]|00820c7c\nstatus|STATUS_OPT_TPC_GPC[18]|00820c80\nstatus|STATUS_OPT_TPC_GPC[19]|00820c84\nstatus|STATUS_OPT_TPC_GPC[1]|00820c3c\nstatus|STATUS_OPT_TPC_GPC[20]|00820c88\nstatus|STATUS_OPT_TPC_GPC[21]|00820c8c\nstatus|STATUS_OPT_TPC_GPC[22]|00820c90\nstatus|STATUS_OPT_TPC_GPC[23]|00820c94\nstatus|STATUS_OPT_TPC_GPC[24]|00820c98\nstatus|STATUS_OPT_TPC_GPC[25]|00820c9c\nstatus|STATUS_OPT_TPC_GPC[26]|00820ca0\nstatus|STATUS_OPT_TPC_GPC[27]|00820ca4\nstatus|STATUS_OPT_TPC_GPC[28]|00820ca8\nstatus|STATUS_OPT_TPC_GPC[29]|00820cac\nstatus|STATUS_OPT_TPC_GPC[2]|00820c40\nstatus|STATUS_OPT_TPC_GPC[30]|00820cb0\nstatus|STATUS_OPT_TPC_GPC[31]|00820cb4\nstatus|STATUS_OPT_TPC_GPC[3]|00820c44\nstatus|STATUS_OPT_TPC_GPC[4]|00820c48\nstatus|STATUS_OPT_TPC_GPC[5]|00820c4c\nstatus|STATUS_OPT_TPC_GPC[6]|00820c50\nstatus|STATUS_OPT_TPC_GPC[7]|00820c54\nstatus|STATUS_OPT_TPC_GPC[8]|00820c58\nstatus|STATUS_OPT_TPC_GPC[9]|00820c5c"""
_curated_by_address = {item["address"]: item for item in CURATED_REGISTERS}
_shadow_manifest_names = set()
_shadow_manifest_addresses = set()
_shadow_manifest_kind_counts = {"core": 0, "options": 0, "status": 0}
for _shadow_line in _GA100_SHADOW_MANIFEST.splitlines():
    _shadow_kind, _shadow_name, _shadow_address_hex = _shadow_line.split("|")
    _shadow_address = int(_shadow_address_hex, 16)
    _shadow_manifest_names.add(_shadow_name)
    _shadow_manifest_addresses.add(_shadow_address)
    _shadow_manifest_kind_counts[_shadow_kind] += 1
    if _shadow_address in _curated_by_address:
        _existing = _curated_by_address[_shadow_address]
        if _shadow_name != _existing["name"] and _shadow_name not in _existing["aliases"]:
            _existing["aliases"].append(_shadow_name)
        continue
    if "NVLINK" in _shadow_name or "NVLIPT" in _shadow_name:
        _shadow_category = "nvlink"
    elif "NVDEC" in _shadow_name:
        _shadow_category = "media_nvdec"
    elif "NVENC" in _shadow_name:
        _shadow_category = "media_nvenc"
    elif "NVJPG" in _shadow_name or "JPEG" in _shadow_name:
        _shadow_category = "media_nvjpg"
    elif "SYS_PIPE" in _shadow_name or "SW_SMC" in _shadow_name:
        _shadow_category = "mig"
    else:
        _shadow_category = "fuse_shadow"
    _new_shadow = _reg(
        _shadow_name, _shadow_address, _shadow_category,
        "status" if _shadow_kind == "status" else (
            "core_observation" if _shadow_kind == "core" else "option"
        ),
        source=(
            "generated GA100 %s shadow manifest sha256=%s"
            % (_shadow_kind, GA100_SHADOW_MANIFEST_HASHES[_shadow_kind])
        ),
        profile="shadow-full", badf="availability",
        all_ones=(
            "availability" if _shadow_name.startswith("NV_FUSE_OPT_PRIV_SFK_")
            else "suspicious"
        ),
        interpretation_confidence_percent=75,
        confidence_reason=(
            "literal generated address/name identity; functional interpretation "
            "requires direct field/header and runtime corroboration"
        ),
    )
    CURATED_REGISTERS.append(_new_shadow)
    _curated_by_address[_shadow_address] = _new_shadow
_registered_shadow_names = {
    name
    for item in CURATED_REGISTERS
    for name in (item["name"],) + tuple(item.get("aliases", ()))
}
_missing_shadow_names = sorted(_shadow_manifest_names - _registered_shadow_names)
if _missing_shadow_names:
    raise RuntimeError(
        "generated GA100 shadow manifest names were dropped: %s"
        % ", ".join(_missing_shadow_names[:8])
    )
GA100_SHADOW_MANIFEST_SUMMARY = {
    "name_count": len(_shadow_manifest_names),
    "unique_address_count": len(_shadow_manifest_addresses),
    "kind_counts": dict(_shadow_manifest_kind_counts),
    "represented_name_count": len(_shadow_manifest_names),
    "missing_names": [],
}
del _curated_by_address
del _registered_shadow_names
del _missing_shadow_names
del _shadow_manifest_names
del _shadow_manifest_addresses
del _shadow_manifest_kind_counts
del _GA100_SHADOW_MANIFEST


# Full privilege masks legitimately read all-ones on GA100.  Make that policy
# explicit rather than globally accepting 0xffffffff from unrelated apertures.
for _register in CURATED_REGISTERS:
    if "PRIV_LEVEL_MASK" in _register["name"] or _register["role"] == "privilege_mask":
        _register["all_ones"] = "valid"

CURATED_REGISTERS.sort(key=lambda item: (item["address"], item["name"]))
REGISTER_BY_NAME = {item["name"]: item for item in CURATED_REGISTERS}
for _register in CURATED_REGISTERS:
    for _alias in _register.get("aliases", ()):
        # Primary spellings always win.  An alias that names another address
        # is a source-map conflict and must never be rebound silently.
        if _alias in REGISTER_BY_NAME and (
            REGISTER_BY_NAME[_alias]["address"] != _register["address"]
        ):
            raise RuntimeError("ambiguous register alias: %s" % _alias)
        REGISTER_BY_NAME.setdefault(_alias, _register)
REGISTER_BY_ADDRESS: dict = {}
for _register in CURATED_REGISTERS:
    REGISTER_BY_ADDRESS.setdefault(_register["address"], []).append(_register)
SAFE_READ_REGISTERS = tuple(
    item for item in CURATED_REGISTERS
    if item["capture_safe"] and item["access_effect"] == "pure_read"
)
SAFE_READ_OFFSETS = frozenset(item["address"] for item in SAFE_READ_REGISTERS)
DEFAULT_CAPTURE_REGISTERS = tuple(
    item for item in SAFE_READ_REGISTERS if item["profile"] == "default"
)
DEFAULT_CAPTURE_OFFSETS = frozenset(item["address"] for item in DEFAULT_CAPTURE_REGISTERS)
REGISTER_PROFILES = (
    "core", "fuse-reference", "shadow-full", "hbm-static",
    "hbm-i1500-latches", "all-safe",
)
_REGISTER_PROFILE_TAGS = {
    "core": frozenset(("default",)),
    "fuse-reference": frozenset(("default", "fuse-reference")),
    # Complete generated shadow-map observation is intentionally opt-in.  It
    # does not imply the HBM/I1500 aperture profiles, whose volatility and
    # interpretation are different from the fuse shadow plane.
    "shadow-full": frozenset(("default", "fuse-reference", "shadow-full")),
    "hbm-static": frozenset(("default", "fuse-reference", "hbm-static")),
    "hbm-i1500-latches": frozenset((
        "default", "fuse-reference", "hbm-static", "hbm-i1500-latches",
    )),
}


def registers_for_profile(profile):
    if profile not in REGISTER_PROFILES:
        raise AuditError("unknown register profile: %s" % profile)
    if profile == "all-safe":
        return SAFE_READ_REGISTERS
    tags = _REGISTER_PROFILE_TAGS[profile]
    return tuple(item for item in SAFE_READ_REGISTERS if item["profile"] in tags)

SENTINEL_NAMES = (
    "PMC_BOOT_0",
    "PBUS_IFR_STATUS0",
    "PBUS_IFR_STATUS1",
    "PBUS_IFR_ERROR",
    "PBUS_IFR_INFO0",
    "FUSECTRL",
    "FUSEADDR",
    "EN_SW_OVERRIDE",
    "IFF_RECORD",
    "DEBUGCTRL",
    "CRC_STATUS",
    "IFF_SW_FUSING",
)


GOALS = {
    "persistent_dsov0": {
        "registers": [
            "DISABLE_SW_OVERRIDE_STATUS", "EN_SW_OVERRIDE",
            "OPT_SECURE_FUSE_RESENSE_DISABLE", "ENABLE_FUSE_PROGRAM_STATUS",
            "DISABLE_FUSE_PROGRAM_STATUS", "BYPASS_FUSES_STATUS",
            "SECURED_BURN_FEATURE_ENABLE", "CRC_STATUS", "FUSECTRL",
            "IFF_SW_FUSING", "IFF_RECORD", "FUSEWDATA_PRIV_LEVEL_MASK",
            "FUSECTRL_PRIV_LEVEL_MASK", "OPT_PRIV_LEVEL_MASK",
        ],
        "strategy": (
            "Treat DSOV as a sampled latch and test persistence separately across "
            "host reads, SENSE_CTRL, GSP reset, FLR, secondary-bus/PEX reset, warm "
            "host reboot, and true slot/AUX power loss; never promote same-session "
            "survival to cold persistence."
        ),
        "unknowns": [
            "which reset domains re-sense the physical DSOV fuse",
            "whether a volatile software-IFF/override image is consumed before SEC2/GSP policy",
            "whether any non-OTP retention domain survives complete slot/AUX power loss",
        ],
    },
    "fuse_write_eligibility": {
        "registers": [
            "FUSECTRL", "DEBUGCTRL", "ENABLE_FUSE_PROGRAM_STATUS",
            "DISABLE_FUSE_PROGRAM_STATUS", "BYPASS_FUSES_STATUS",
            "DISABLE_SW_OVERRIDE_STATUS", "FUSEWDATA_PRIV_LEVEL_MASK",
            "FUSECTRL_PRIV_LEVEL_MASK", "OPT_PRIV_LEVEL_MASK",
            "OPT_SECURE_FUSE_WDATA_WR_SECURE",
            "OPT_SECURE_FUSE_RESENSE_DISABLE", "SECURED_BURN_FEATURE_ENABLE",
        ],
        "strategy": "Classify passive gate observations only; never equate a glitch phenotype with burn eligibility.",
        "unknowns": [
            "programming voltage/source and electrical state",
            "exclusive controller ownership", "secure raw aliases",
            "complete image and partial-program recovery plan",
        ],
    },
    "80gb_hbm": {
        "registers": [
            "OPT_FBIO_DISABLE", "OPT_FBIO_DISABLE_CP", "FBIO_DEFECTIVE", "STATUS_OPT_FBIO",
            "OPT_FBPA_DISABLE", "OPT_FBPA_DISABLE_CP", "FBPA_DEFECTIVE", "STATUS_OPT_FBPA",
            "OPT_FBP_DISABLE", "OPT_FBP_DISABLE_CP", "FBP_DEFECTIVE", "STATUS_OPT_FBP",
            "OPT_ROP_L2_DISABLE", "OPT_ROP_L2_DISABLE_CP", "ROP_L2_DEFECTIVE", "STATUS_FB_CONFIG",
            "STATUS_HALF_FBPA", "STATUS_HALF_LTC",
        ],
        "strategy": "Per-card memory topology closure; never copy another die's disable mask.",
        "unknowns": ["HBM site/routing map", "L2/NoC closure", "physical and RIR row state"],
    },
    "more_sms": {
        "registers": [
            "OPT_GPC_DISABLE", "GPC_DEFECTIVE", "STATUS_OPT_GPC",
            "OPT_GPC_DISABLE_CP", "OPT_PES_DISABLE", "OPT_PES_DISABLE_CP", "PES_DEFECTIVE",
            "TPC_FLOORSWEEP_PRIV_LEVEL_MASK", "RECONFIG_PRIV_LEVEL_MASK",
            "FLOORSWEEP_PRIV_LEVEL_MASK",
        ] + ["OPT_TPC_GPC%d_DISABLE" % i for i in range(8)]
          + ["OPT_TPC_GPC%d_RECONFIG" % i for i in range(8)]
          + ["OPT_TPC_GPC%d_DISABLE_CP" % i for i in range(8)]
          + ["TPC_GPC%d_DEFECTIVE" % i for i in range(8)]
          + ["CTRL_OPT_TPC_GPC%d" % i for i in range(8)]
          + ["RECONFIG_OVERRIDE_TPC_GPC%d" % i for i in range(8)]
          + ["DISABLE_EXTEND_TPC_GPC%d" % i for i in range(8)]
          + ["STATUS_OPT_TPC_GPC%d" % i for i in range(8)],
        "strategy": "Enable only non-defective, topology-consistent GPC/TPC/PES units.",
        "unknowns": ["per-die health", "routing closure", "physical and RIR row state"],
    },
    "pcie_gen4_x16": {
        "registers": [
            "OPT_DISABLE_GEN3_SPEED", "OPT_PCIE_MAGIC_BITS_D",
            "OPT_PCIE_BOOT_GEN23_DISABLE", "OPT_PCIE_BOOT_GEN3_DISABLE",
            "OPT_PCIE_LANE_DISABLE", "OPT_PCIE_LANE_DISABLE_CP",
            "CTRL_OPT_PCIE_LANE", "STATUS_OPT_PCIE_LANE",
        ],
        "strategy": "Resolve only documented speed/lane fields, then validate endpoint and root-port training.",
        "unknowns": [
            "logical FF mapping for all fields",
            "which dated Magic-D profile applies to this card rather than a universal whole-word target",
            "firmware/equalization", "board signal integrity",
        ],
    },
    "nvlink": {
        "registers": [
            "OPT_NVLINK_DISABLE", "OPT_NVLINK_DISABLE_CP", "NVLINK_DEFECTIVE", "STATUS_OPT_NVLINK",
            "OPT_PERLINK_DISABLE", "CTRL_OPT_PERLINK", "STATUS_OPT_PERLINK",
            "CTRL_OPT_NVLINK", "PTOP_SCAL_NUM_NVLINK",
            "OPT_NVLIPT_RESET_COND_DISABLE", "NV_FUSE_OPT_NVLIPT_RESET_SEQ_DISABLE",
            "OPT_SECURE_NVLINK_MASK_WR_SECURE",
            "NV_FUSE_OPT_SECURE_NVLINK_BUFFER_READY_SECURE",
            "OPT_SECURE_NVLINKS_PHYSICAL_DAMAGE_WR_SECURE",
        ],
        "strategy": "Per-link option/defect/control/status analysis plus bridge and ROM prerequisites.",
        "unknowns": ["board bridge", "ROM policy", "per-link physical mapping"],
    },
    "ecc": {
        "registers": [
            "OPT_ECC_EN", "OPT_ECC_EN_CONTROL", "STATUS_FB_CONFIG",
            "FEATURE_OVERRIDE_ECC", "FEATURE_OVERRIDE_ECC_1", "FEATURE_OVERRIDE_ECC_2",
            "FEATURE_READOUT", "FEATURE_READOUT_1", "FEATURE_READOUT_2",
            "FEATURE_OVERRIDE_ECC_PRIV_LEVEL_MASK", "FBPA_ECC_CONTROL",
            "NV_FUSE_OPT_SECURE_FEATURE_OVERRIDE_ECC_WR_SECURE",
            "NV_FUSE_OPT_SECURE_FBPA_ECC_WR_SECURE",
            "NV_FUSE_OPT_SECURE_SRAM_ECC_WR_SECURE",
            "NV_FUSE_OPT_SECURE_PLTS_CYA_ECC_WR_SECURE",
        ],
        "strategy": "Treat ECC_EN identity as confirmed but recipe as unvalidated.",
        "unknowns": ["all ECC domain dependencies", "primary/redundant state", "firmware policy"],
    },
    "mig": {
        "registers": [
            "OPT_SW_SMC_DISABLE", "OPT_SYS_PIPE_DISABLE", "OPT_SYS_PIPE_DISABLE_CP",
            "SYS_PIPE_DEFECTIVE", "STATUS_OPT_SYS_PIPE",
            "OPT_GPC_DISABLE", "OPT_GPC_DISABLE_CP", "GPC_DEFECTIVE", "STATUS_OPT_GPC",
            "FBHUB_MEM_PARTITION_BOTTOM", "FBHUB_MEM_PARTITION_MIDDLE",
            "FBHUB_MEM_PARTITION_BOUNDARY_CFG0",
        ],
        "strategy": "Analyze SYS_PIPE/GPC topology and firmware policy together.",
        "unknowns": [
            "SW_SMC and DevInit scratch dependencies",
            "GPC-to-SYS_PIPE mapping, engine database and profile construction",
            "memory topology, reset and free-channel closure", "VBIOS/GSP/driver support",
        ],
    },
    "nvdec_nvjpg": {
        "registers": [
            "OPT_NVDEC_DISABLE", "OPT_NVDEC_DISABLE_CP", "OPT_NVDEC_DEFECTIVE", "STATUS_NVDEC",
            "OPT_NVJPG_DISABLE", "OPT_NVJPG_DISABLE_CP", "OPT_NVJPG_DEFECTIVE", "STATUS_NVJPG",
            "OPT_NVENC_DISABLE", "OPT_NVENC_DISABLE_CP", "OPT_NVENC_DEFECTIVE",
            "CTRL_OPT_NVENC", "STATUS_OPT_NVENC", "PTOP_SCAL_NUM_NVENCS",
            "OPT_OFA_DISABLE", "OPT_OFA_DISABLE_CP", "OPT_OFA_DEFECTIVE", "STATUS_OPT_OFA",
            "NV_FUSE_OPT_HEVC_DEC_DISABLE", "NV_FUSE_OPT_HEVC_ENC_DISABLE",
            "NV_FUSE_OPT_VP8_VP9_DISABLE", "NV_FUSE_OPT_NVENC_THROTTLE",
            "NV_FUSE_OPT_HS_DIS_NVDEC1", "NV_FUSE_OPT_HS_DIS_NVDEC2",
            "NV_FUSE_OPT_HS_DIS_NVDEC3", "NV_FUSE_OPT_HS_DIS_NVDEC4",
            "NV_FUSE_OPT_FUSE_UCODE_NVDEC_REV", "NV_FUSE_OPT_FUSE_UCODE_NVENC_REV",
            "NV_FUSE_OPT_FUSE_UCODE_NVJPG_REV", "NV_FUSE_OPT_FUSE_UCODE_OFA_REV",
            "NV_FUSE_OPT_FPF_UCODE_NVDEC_REV", "NV_FUSE_OPT_PKC_NVDEC_ALGO_DISABLE",
            "NV_FUSE_OPT_SECURE_NVDEC_DEBUG_DIS",
        ] + [
            "NV_FUSE_OPT_FPF_NVDEC_UCODE%d_VERSION" % index
            for index in range(1, 17)
        ],
        "strategy": (
            "Require direct option/defect/status observations per specimen; model "
            "NVDEC Chain-1 bits 139..143 and the inseparable {146,371} "
            "NVJPG/OFA candidate pair, while leaving NVENC and all unproved "
            "HS/power/firmware dependencies unsupported."
        ),
        "unknowns": [
            "individual attribution of Chain-1 bits 146 and 371 to NVJPG versus OFA",
            "physical FF coordinate and actual engine population for NVENC",
            "HS-control, power, PTOP discovery and firmware dependencies",
            "cross-specimen sealed reproduction of the one-card media/MIG burn report",
        ],
    },
    "a100_identity_vbios": {
        "registers": ["OPT_PCIE_DEVIDA", "OPT_PCIE_DEVIDB"],
        "strategy": (
            "Treat DEVIDA/DEVIDB, signed VBIOS and recovery as one identity migration; "
            "do not claim that the A100 ROM also unlocks CMP SS0/SS1 floorsweep policy; "
            "re-audit driver recipe selection after the ID changes to 20b5."
        ),
        "unknowns": [
            "validated ID target", "signed image compatibility", "recovery path",
            "separate SS0/SS1 and software-floorsweep policy after conversion",
            "20b5 selecting a reduced runtime recipe that omits CMP-specific ECC/relink/SM policy",
        ],
    },
    "oc_frequencies": {
        "registers": [
            "NV_FUSE_OPT_GPC2CLK_CAP", "NV_FUSE_OPT_ATE_VMIN",
            "NV_FUSE_OPT_CP_BIN", "NV_FUSE_OPT_SAMPLE_BIN",
            "NV_FUSE_OPT_SRAM_VMIN_BIN", "NV_FUSE_OPT_SRAM_VMIN_BIN_REV",
            "NV_FUSE_OPT_SPEEDO0", "NV_FUSE_OPT_SPEEDO1", "NV_FUSE_OPT_SPEEDO2",
            "NV_FUSE_OPT_SPEEDO_REV", "NV_FUSE_OPT_FPF_OVERLOCK_INFO",
            "NV_FUSE_OPT_SM_SPEED_SELECT_DP", "OPT_SM_SPEED_SELECT_DP_CONTROL",
            "NV_FUSE_OPT_SM_SPEED_SELECT_FFMA",
            "NV_FUSE_OPT_SM_SPEED_SELECT_FMLA16", "NV_FUSE_OPT_SM_SPEED_SELECT_FMLA32",
            "NV_FUSE_OPT_SM_SPEED_SELECT_IMLA0", "NV_FUSE_OPT_SM_SPEED_SELECT_IMLA1",
            "NV_FUSE_OPT_SM_SPEED_SELECT_IMLA2", "NV_FUSE_OPT_SM_SPEED_SELECT_IMLA3",
            "NV_FUSE_OPT_SM_SPEED_SELECT_IMLA4", "FEATURE_OVERRIDE_SM_SPEED_SELECT",
            "FEATURE_OVERRIDE_SM_SPEED_SELECT_1",
        ],
        "strategy": (
            "Inventory rate/Vmin/bin/NAFLL fields and compare the reported SS0/SS1 "
            "IFF macros and DP-control conventional fuse without treating their tested "
            "row placement or values as a portable recipe."
        ),
        "unknowns": [
            "rate selectors", "Vmin/bin coupling", "reliability limits",
            "whether the reported SS0/SS1 IFF values cover tensor issue rates",
            "isolated cold proof for the DP control/undo bit",
        ],
    },
}


# Empirical logical-FF coordinates are hypotheses unless an official GA100
# descriptor is named.  Zero-confidence entries are deliberately retained so
# the planner cannot silently turn a register name into a physical recipe.
LOGICAL_FIELD_CATALOG = [
    {"name": "OPT_FBIO_DISABLE", "plane": "FF", "chain": 1, "bit": 83, "width": 24, "certainty": 95},
    {"name": "OPT_FBPA_DISABLE", "plane": "FF", "chain": 1, "bit": 107, "width": 24, "certainty": 95},
    {"name": "OPT_GPC_DISABLE", "plane": "FF", "chain": 1, "bit": 131, "width": 8, "certainty": 97},
    {"name": "OPT_PES_DISABLE", "plane": "FF", "chain": 1, "bit": 251, "width": 24, "certainty": 91},
    {"name": "OPT_FBP_DISABLE", "plane": "FF", "chain": 1, "bit": 307, "width": 12, "certainty": 98},
    {"name": "OPT_ROP_L2_DISABLE", "plane": "FF", "chain": 1, "bit": 319, "width": 24, "certainty": 95},
    {"name": "OPT_NVLINK_DISABLE", "plane": "FF", "chain": 1, "bit": 368, "width": 3, "certainty": 94},
    {"name": "OPT_SYS_PIPE_DISABLE", "plane": "FF", "chain": 1, "bit": 372, "width": 8, "certainty": 94,
     "defect_register": "SYS_PIPE_DEFECTIVE"},
    {"name": "OPT_PCIE_DEVIDA", "plane": "FF", "chain": 2, "bit": 593, "width": 16, "certainty": 98},
    {"name": "OPT_PCIE_DEVIDB", "plane": "FF", "chain": 2, "bit": 609, "width": 16, "certainty": 96},
    {"name": "OPT_DISABLE_GEN3_SPEED", "plane": "FF", "chain": 2, "block": 39, "bit_in_block": 9, "width": 1, "certainty": 99},
    {"name": "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE", "plane": "FF", "chain": 2, "block": 47, "bit_in_block": 3, "mmio_bit": 25, "width": 1, "certainty": 97},
    {"name": "OPT_PCIE_BOOT_GEN23_DISABLE", "plane": "FF", "chain": 2, "block": 49, "bit_in_block": 6, "width": 1, "certainty": 96},
    {"name": "OPT_PCIE_BOOT_GEN3_DISABLE", "plane": "FF", "chain": 2, "block": 49, "bit_in_block": 7, "width": 1, "certainty": 96},
    {"name": "FEATURE_OVERRIDE_SM_SPEED_SELECT", "plane": "IFF-candidate", "target_address": 0x0082381C,
     "reported_value": 0x88888888, "reported_words": [0x082381FD, 0x88888888], "reported_rows": [484, 485],
     "certainty": 85, "execution_supported": False,
     "placement_boundary": "rows are one tested recipe, not a universal allocation"},
    {"name": "FEATURE_OVERRIDE_SM_SPEED_SELECT_1", "plane": "IFF-candidate", "target_address": 0x00823820,
     "reported_value": 0x00000008, "reported_words": [0x0823823D, 0x00000008], "reported_rows": [486, 487],
     "certainty": 85, "execution_supported": False,
     "placement_boundary": "rows are one tested recipe, not a universal allocation"},
]
for _catalog_gpc in range(8):
    LOGICAL_FIELD_CATALOG.append({
        "name": "OPT_TPC_GPC%d_DISABLE" % _catalog_gpc,
        "plane": "FF", "chain": 1, "bit": 187 + 8 * _catalog_gpc,
        "width": 8, "certainty": 93,
    })
LOGICAL_FIELD_CATALOG.extend([
    {"name": "OPT_PERLINK_DISABLE", "plane": "FF", "coordinate": None, "width": 12, "certainty": 0},
    {"name": "OPT_ECC_EN", "plane": "conventional", "primary_row": 6, "redundant_row": 7, "bit": 28, "width": 1, "certainty": 99},
    {"name": "OPT_ECC_EN_CONTROL", "plane": "conventional", "primary_row": 6, "redundant_row": 7, "bit": 29, "width": 1, "certainty": 99},
    {"name": "OPT_SM_SPEED_SELECT_DP", "plane": "conventional", "primary_row": 6, "redundant_row": 7, "bit": 26, "width": 1, "certainty": 99},
    {"name": "OPT_SM_SPEED_SELECT_DP_CONTROL", "plane": "conventional", "primary_row": 6, "redundant_row": 7, "bit": 27, "width": 1, "certainty": 65,
     "mechanism_confirmed": False, "candidate_role": "DP control/undo"},
    {"name": "OPT_SW_SMC_DISABLE", "plane": "conventional", "primary_row": 14, "redundant_row": 15, "bit": 11, "width": 1, "certainty": 99},
    {"name": "OPT_NVDEC_DISABLE", "plane": "FF", "chain": 1, "bit": 139, "width": 5,
     "certainty": 90, "defect_register": "OPT_NVDEC_DEFECTIVE"},
    {"name": "NVJPG_OFA_DISABLE_CANDIDATE_BIT146", "plane": "FF", "chain": 1,
     "bit": 146, "width": 1, "certainty": 88,
     "individual_feature_attribution_certainty_percent": 0,
     "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT371",
     "defect_registers": ["OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE"]},
    {"name": "NVJPG_OFA_DISABLE_CANDIDATE_BIT371", "plane": "FF", "chain": 1,
     "bit": 371, "width": 1, "certainty": 88,
     "individual_feature_attribution_certainty_percent": 0,
     "must_change_with": "NVJPG_OFA_DISABLE_CANDIDATE_BIT146",
     "defect_registers": ["OPT_NVJPG_DEFECTIVE", "OPT_OFA_DEFECTIVE"]},
    {"name": "OPT_NVJPG_DISABLE", "plane": "FF", "coordinate": None, "width": 1,
     "certainty": 0, "candidate_coordinate_set": [146, 371],
     "paired_with": "OPT_OFA_DISABLE", "set_mapping_certainty_percent": 88,
     "defect_register": "OPT_NVJPG_DEFECTIVE"},
    {"name": "OPT_NVENC_DISABLE", "plane": "FF", "coordinate": None, "width": 2, "certainty": 0},
    {"name": "OPT_OFA_DISABLE", "plane": "FF", "coordinate": None, "width": 1,
     "certainty": 0, "candidate_coordinate_set": [146, 371],
     "paired_with": "OPT_NVJPG_DISABLE", "set_mapping_certainty_percent": 88,
     "defect_register": "OPT_OFA_DEFECTIVE"},
])

LOGICAL_FIELD_EVIDENCE = {
    "OPT_FBIO_DISABLE": (["Kate GA100 differential map", "runbook 24.3"], "Differential/raw correlations agree; official GA100 FF descriptor is absent."),
    "OPT_FBPA_DISABLE": (["Kate GA100 differential map", "runbook 24.3"], "Separate 24-bit consumer and correlations agree; official descriptor is absent."),
    "OPT_GPC_DISABLE": (["Kate GA100 differential map", "GA100 header width", "runbook 24.3"], "Map, correlations and exact option width agree."),
    "OPT_PES_DISABLE": (["Kate GA100 differential map", "GA100 RM topology", "runbook 24.3"], "Coordinate is consistent with three-PES/GPC topology; descriptor is absent."),
    "OPT_FBP_DISABLE": (["ga100-fuse-re raw pair", "executed replacement evidence", "runbook 24.3"], "Exact block correlation and replacement evidence agree."),
    "OPT_ROP_L2_DISABLE": (["Kate GA100 differential map", "completed-topology evidence", "runbook 24.3"], "Expanded masks and failed-then-completed topology evidence agree."),
    "OPT_NVLINK_DISABLE": (["Kate GA100 differential map", "GA100 header width", "runbook 24.5"], "Three-bit coordinate and IOCTRL-group width agree; it is not per-link."),
    "OPT_SYS_PIPE_DISABLE": (["Kate GA100 differential map", "repeaterdroid e3826fee", "runbook 48.1"], "Differential mapping plus a reported exact append, direct/status delta, PTOP exposure and eight-pipe MIG function agree on bits 372..379; the report remains one specimen."),
    "OPT_PCIE_DEVIDA": (["Kate GA100 map", "exact retained extraction", "runbook 24.4"], "Exact extraction and map agree."),
    "OPT_PCIE_DEVIDB": (["Kate GA100 map", "exact retained extraction", "runbook 24.4"], "Exact extraction agrees, with fewer differentiating samples."),
    "OPT_DISABLE_GEN3_SPEED": (["Kate GA100 map", "2026-11-19 isolated cold-boot lab result", "runbook 24.4"], "Bit 1 alone was experimentally falsified; restoring bit 1 while clearing bit 9 made the option read zero after a cold boot."),
    "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE": (["Kate differential/functional evidence", "GA100 register specification", "runbook 24.4"], "Logical block bit 3 maps to direct MMIO MAGIC_D bit 25."),
    "OPT_PCIE_BOOT_GEN23_DISABLE": (["Kate GA100 map", "GA100 register specification", "runbook 24.4"], "Map and direct field identity agree."),
    "OPT_PCIE_BOOT_GEN3_DISABLE": (["Kate GA100 map", "GA100 register specification", "runbook 24.4"], "Map and direct field identity agree."),
    "OPT_PERLINK_DISABLE": (["GA100 register specification", "runbook 24.5"], "Twelve-bit register identity is direct; no physical FF coordinate is known."),
    "OPT_ECC_EN": (["GA100 register specification primary/redundant aliases", "runbook 24.5"], "Rows 6/7 bit 28 are literal aliases; feature sufficiency is not implied."),
    "OPT_ECC_EN_CONTROL": (["GA100 register specification primary/redundant aliases", "runbook 24.5"], "Rows 6/7 bit 29 are literal aliases; sense semantics remain incomplete."),
    "OPT_SM_SPEED_SELECT_DP": (["GA100 direct and primary/redundant aliases", "CMP/A100 row differential", "runbook 48.3"], "The one-bit DP selector is direct at 0x820224 and aliases row 6/7 bit 26; its reduced/full semantics are directly named."),
    "OPT_SM_SPEED_SELECT_DP_CONTROL": (["GA100 primary/redundant alias name", "Double_Precision__SS1.txt", "runbook 48.3"], "Rows 6/7 bit 27 are directly named as DP_CONTROL, but available static specimens leave the bit invariant and the proposed undo behavior has no isolated cold functional proof."),
    "OPT_SW_SMC_DISABLE": (["GA100 register specification primary/redundant aliases", "runbook 24.5"], "Rows 14/15 bit 11 are literal aliases; MIG sufficiency is unproved."),
    "OPT_NVDEC_DISABLE": (["GA100 direct register specification", "repeaterdroid e3826fee", "runbook 48.1"], "Chain-1 bits 139..143 are supported by one reported exact append, register delta, PTOP enumeration and functional decoder result; direct OPT_NVDEC_DEFECTIVE remains the planning gate."),
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT146": (["repeaterdroid e3826fee", "runbook 48.1"], "The inseparable set {Chain-1 bit 146, Chain-1 bit 371} maps bijectively to {NVJPG disable, OFA disable} in one reported functional campaign. Which bit is which remains unknown; XRAY may only propose clearing both together when both direct defect words are zero."),
    "NVJPG_OFA_DISABLE_CANDIDATE_BIT371": (["repeaterdroid e3826fee", "runbook 48.1"], "The inseparable set {Chain-1 bit 146, Chain-1 bit 371} maps bijectively to {NVJPG disable, OFA disable} in one reported functional campaign. Which bit is which remains unknown; XRAY may only propose clearing both together when both direct defect words are zero."),
    "OPT_NVJPG_DISABLE": (["GA100 direct register specification", "repeaterdroid e3826fee", "runbook 48.1"], "The set {bit146, bit371} maps to {NVJPG,OFA} in one functional campaign, but neither individual attribution is known; direct defect/status words are required."),
    "OPT_NVENC_DISABLE": (["GA100 register specification", "runbook 24.5"], "Two-bit option exists, but no physical FF coordinate is recovered."),
    "OPT_OFA_DISABLE": (["GA100 direct register specification", "repeaterdroid e3826fee", "runbook 48.1"], "The set {bit146, bit371} maps to {NVJPG,OFA} in one functional campaign, but neither individual attribution is known; direct defect/status words are required."),
    "FEATURE_OVERRIDE_SM_SPEED_SELECT": (["GA100 FEATURE_OVERRIDE register specification", "portz3r0 aba33dc external recipe", "runbook 48.3"], "The target register identity is strong and one tested campaign reports the exact IFF WRITE_DW pair/value; physical rows 484/485 are recipe-specific placement, not a universal allocation."),
    "FEATURE_OVERRIDE_SM_SPEED_SELECT_1": (["GA100 FEATURE_OVERRIDE register specification", "portz3r0 aba33dc external recipe", "runbook 48.3"], "The IMLA4 target register identity is strong and one tested campaign reports the exact IFF WRITE_DW pair/value; physical rows 486/487 are recipe-specific placement, not a universal allocation."),
}
for _catalog_gpc in range(8):
    LOGICAL_FIELD_EVIDENCE["OPT_TPC_GPC%d_DISABLE" % _catalog_gpc] = (
        ["Kate GA100 differential map", "compute-result correlations", "runbook 24.3"],
        "Eight-bit-per-GPC pattern is strongly correlated; official descriptor is absent.",
    )
for _catalog_item in LOGICAL_FIELD_CATALOG:
    _catalog_name = str(_catalog_item["name"])
    _catalog_sources, _catalog_reason = LOGICAL_FIELD_EVIDENCE[_catalog_name]
    _catalog_item["certainty_percent"] = _catalog_item["certainty"]
    _catalog_item["sources"] = _catalog_sources
    _catalog_item["certainty_reason"] = _catalog_reason
    _catalog_item.setdefault(
        "source_class",
        "GA100 differential/header evidence; official physical descriptor absent",
    )
    _catalog_item["planning_allowed"] = False


DIRECT_SOURCE_ANCHORS = {
    "iff_execution_status": "GA100 bus execution-register evidence",
    "controller_registers": "GA100 fuse-controller register evidence",
    "physical_row_read_requires_writes": "GA100 controller-read sequence evidence",
    "rir_read_requires_writes": "GA100 RIR-read sequence evidence",
    "ga100_rir_linear_format": "GA100-specific linear RIR record evidence",
    "ff_format": "GA100 FF record-format evidence",
    "iff_format": "GA100 IFF record-format evidence",
    "iff_decode": "GA100 IFF decode and execution evidence",
    "iff_set_field_spaces": "GA100 IFF SET_FIELD space evidence",
    "ga100_iff_crc_unsupported": "GA100 IFF CRC-support evidence",
}

NVML_QUERY_SYMBOLS = (
    "nvmlInit_v2",
    "nvmlShutdown",
    "nvmlDeviceGetHandleByPciBusId_v2",
    "nvmlDeviceGetNumGpuCores",
    "nvmlDeviceGetEccMode",
    "nvmlDeviceGetMigMode",
    "nvmlDeviceGetNvLinkState",
    "nvmlErrorString",
)


def decode_fields(register, value):
    decoded = {}
    for field in register.get("fields", []):
        width = field["msb"] - field["lsb"] + 1
        mask = (1 << width) - 1
        decoded[field["name"]] = (value >> field["lsb"]) & mask
    return decoded


CRC_CLASSES = {
    0: "not_parsed_yet",
    4: "not_present",
    5: "present_no_errors",
    6: "present_corrected_errors",
    7: "present_uncorrected_errors",
}


def decode_crc_status(value):
    h0 = value & 0x7
    h1 = (value >> 4) & 0x7
    severities = {
        "present_uncorrected_errors": 5,
        "present_corrected_errors": 4,
        "not_parsed_yet": 3,
        "reserved_unknown": 2,
        "not_present": 1,
        "present_no_errors": 0,
    }
    names = [CRC_CLASSES.get(h0, "reserved_unknown"), CRC_CLASSES.get(h1, "reserved_unknown")]
    return {
        "raw": hex32(value),
        "h0": {"value": h0, "classification": names[0]},
        "h1": {"value": h1, "classification": names[1]},
        "overall": max(names, key=lambda name: severities[name]),
        "reserved_bits": hex32(value & ~0x77),
        "note": "macro parse/ECC status; not an IFF payload checksum",
    }


def decode_fusectrl(value):
    states = {
        0: "reset", 1: "post_reset", 2: "load_row0", 3: "load_row1",
        4: "idle", 5: "read_setup", 6: "read_strobe", 7: "sample_fuses",
        8: "read_hold", 9: "fuse_src_setup", 10: "write_setup",
        11: "write_addr_setup", 12: "write_program", 13: "write_addr_hold",
        14: "fuse_src_hold", 15: "load_rir",
    }
    command = value & 0x3
    state = (value >> 16) & 0x1F
    return {
        "raw": hex32(value),
        "command": command,
        "command_name": {0: "idle", 1: "read", 2: "write", 3: "sense_ctrl"}.get(command, "unknown"),
        "state": state,
        "state_name": states.get(state, "unknown"),
        "fuse_init_sense_done": bool(value & (1 << 29)),
        "fuse_sense_done": bool(value & (1 << 30)),
        "record_shift_done": bool(value & (1 << 31)),
    }


def decode_iff_record_register(value):
    return {
        "raw": hex32(value),
        "start": value & 0x1FF,
        "last": (value >> 16) & 0x1FF,
    }


def _read_text(path):
    try:
        return Path(path).read_text(encoding="ascii", errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _read_hex_file(path):
    value = _read_text(path)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def parse_bdf(bdf):
    match = BDF_RE.fullmatch(bdf)
    if match is None:
        raise AuditError("invalid full PCI BDF: %s" % bdf)
    return {
        "domain": int(match.group("domain"), 16),
        "bus": int(match.group("bus"), 16),
        "device": int(match.group("device"), 16),
        "function": int(match.group("function"), 16),
    }


def discover_pci_cards(sysfs_root):
    root = Path(sysfs_root)
    cards = []
    if not root.is_dir():
        return cards
    for device_path in sorted(root.iterdir(), key=lambda item: item.name):
        if BDF_RE.fullmatch(device_path.name) is None:
            continue
        vendor = _read_hex_file(device_path / "vendor")
        device = _read_hex_file(device_path / "device")
        pci_class = _read_hex_file(device_path / "class")
        if vendor != 0x10DE or pci_class is None or (pci_class >> 16) != 0x03:
            continue
        driver = None
        try:
            driver = os.path.basename(os.path.realpath(str(device_path / "driver")))
            if not (device_path / "driver").exists():
                driver = None
        except OSError:
            driver = None
        parts = parse_bdf(device_path.name)
        card = {
            "domain_bdf": device_path.name.lower(),
            "domain": parts["domain"],
            "bus": parts["bus"],
            "slot": parts["device"],
            "function": parts["function"],
            "vendor": vendor,
            "device": device,
            "subsystem_vendor": _read_hex_file(device_path / "subsystem_vendor"),
            "subsystem_device": _read_hex_file(device_path / "subsystem_device"),
            "revision": _read_hex_file(device_path / "revision"),
            "class": pci_class,
            "driver": driver,
            "runtime_status": _read_text(device_path / "power" / "runtime_status"),
            "pci_power_state": _read_text(device_path / "power_state"),
            "current_link_speed": _read_text(device_path / "current_link_speed"),
            "current_link_width": _read_text(device_path / "current_link_width"),
            "max_link_speed": _read_text(device_path / "max_link_speed"),
            "max_link_width": _read_text(device_path / "max_link_width"),
            "sysfs_path": str(device_path),
        }
        cards.append(card)
    return cards


def public_card_metadata(card):
    result = {}
    hex_keys = {
        "vendor": 4, "device": 4, "subsystem_vendor": 4,
        "subsystem_device": 4, "revision": 2, "class": 6,
    }
    for key, value in card.items():
        if key in ("domain", "bus", "slot", "function", "sysfs_path"):
            continue
        if key in hex_keys and value is not None:
            result[key] = "0x%0*x" % (hex_keys[key], value)
        else:
            result[key] = value
    return result


class GpcProbeDevice:
    """Existing gpcprobe descriptor.  This class exposes reads only."""

    def __init__(self, path):
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        self.path = path
        self.fd = os.open(path, flags)
        info = bytearray(4 + 16 * 4)
        try:
            fcntl.ioctl(self.fd, GP_IOC_INFO, info, True)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        unpacked = struct.unpack("=17I", info)
        self.count = min(unpacked[0], 16)
        self.encoded_bdfs = list(unpacked[1:1 + self.count])
        self.read_count = 0

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read32(self, index, offset):
        if self.fd is None:
            raise AuditError("gpcprobe is closed")
        if not 0 <= index < self.count:
            raise AuditError("gpcprobe card index out of range")
        if offset & 3:
            raise AuditError("unaligned BAR0 read: 0x%x" % offset)
        request = bytearray(struct.pack("=III", index, offset, 0))
        fcntl.ioctl(self.fd, GP_IOC_READ32, request, True)
        # Count the completed ioctl before response validation so malformed
        # replies remain visible in the operational ledger.
        self.read_count += 1
        out_index, out_offset, value = struct.unpack("=III", request)
        if out_index != index or out_offset != offset:
            raise AuditError("gpcprobe returned a mismatched request")
        return value


def _ioctl_structure(fd, command, value):
    size = ctypes.sizeof(value)
    raw = bytearray(ctypes.string_at(ctypes.addressof(value), size))
    fcntl.ioctl(fd, command, raw, True)
    return type(value).from_buffer_copy(raw)


def _copy_xray_identity(identity):
    return XrayIdentity.from_buffer_copy(
        ctypes.string_at(ctypes.addressof(identity), ctypes.sizeof(identity))
    )


def _xray_structure_bytes(value):
    return ctypes.string_at(ctypes.addressof(value), ctypes.sizeof(value))


def _xray_identity_equal(left, right):
    return _xray_structure_bytes(left) == _xray_structure_bytes(right)


def _xray_identity_bdf(identity):
    return "%04x:%02x:%02x.%x" % (
        identity.domain,
        identity.bus,
        identity.devfn >> 3,
        identity.devfn & 7,
    )


def _xray_identity_document(identity):
    return {
        "generation": "0x%016x" % identity.generation,
        "domain_bdf": _xray_identity_bdf(identity),
        "vendor": "0x%04x" % identity.vendor,
        "device": "0x%04x" % identity.device,
        "subsystem_vendor": "0x%04x" % identity.subvendor,
        "subsystem_device": "0x%04x" % identity.subdevice,
        "revision": "0x%02x" % identity.revision,
    }


def _xray_snapshot_document(snapshot):
    return {
        name: hex32(getattr(snapshot, name))
        for name, _ctype in XrayControllerSnapshot._fields_
    }


class XrayHelperDevice:
    """Versioned full-domain helper with atomic GA100 MAIN/RIR transactions."""

    def __init__(self, path):
        flags = os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        self.path = path
        self.fd = os.open(path, flags)
        try:
            info = _ioctl_structure(self.fd, XRAY_IOC_INFO, XrayInfo())
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        def reject(message):
            self.close()
            raise AuditError(message)

        if info.abi_version != XRAY_HELPER_ABI:
            reject(
                "170hx_xray helper ABI mismatch: kernel=%d userspace=%d"
                % (info.abi_version, XRAY_HELPER_ABI)
            )
        if info.struct_size != ctypes.sizeof(XrayInfo):
            reject(
                "170hx_xray INFO size mismatch: kernel=%d userspace=%d"
                % (info.struct_size, ctypes.sizeof(XrayInfo))
            )
        if info.max_cards != XRAY_MAX_CARDS or info.count > XRAY_MAX_CARDS:
            reject("170hx_xray returned invalid card bounds")
        if info.module_generation == 0:
            reject("170hx_xray returned a zero module generation")
        self.abi_version = info.abi_version
        self.capabilities = info.capabilities
        self.module_generation = info.module_generation
        self.cards = []
        self._identities = {}
        self.read_count = 0
        for index in range(info.count):
            ci = info.cards[index]
            identity = _copy_xray_identity(ci.identity)
            bdf = _xray_identity_bdf(identity)
            if identity.generation != info.module_generation:
                reject("170hx_xray card identity has a mismatched generation")
            if (identity.vendor != 0x10DE
                    or identity.device not in GA100_READ_TARGET_DEVICE_IDS):
                reject("170hx_xray returned a non-allowlisted PCI identity")
            if bdf in self._identities:
                reject("170hx_xray returned a duplicate full-domain BDF")
            if ci.driver_bound not in (0, 1):
                reject("170hx_xray returned a non-boolean driver-bound field")
            if ci.bar0_start == 0 or ci.bar0_length < XRAY_BAR0_MIN_LENGTH:
                reject("170hx_xray returned an invalid BAR0 resource")
            driver = bytes(ci.driver).split(b"\x00", 1)[0].decode(
                "ascii", errors="replace"
            )
            public = {
                "index": index,
                "identity": _xray_identity_document(identity),
                "class": "0x%06x" % ci.class_code,
                "pci_command": "0x%04x" % ci.pci_command,
                "driver_bound": bool(ci.driver_bound),
                "driver": driver or None,
                "runtime_status_numeric": ci.runtime_status,
                "bar0_start": "0x%016x" % ci.bar0_start,
                "bar0_length": "0x%016x" % ci.bar0_length,
            }
            self.cards.append(public)
            self._identities[bdf] = identity

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def info_document(self):
        return {
            "path": self.path,
            "abi_version": self.abi_version,
            "module_generation": "0x%016x" % self.module_generation,
            "capabilities": "0x%08x" % self.capabilities,
            "capability_flags": {
                "read32": bool(self.capabilities & XRAY_CAP_READ32),
                "dump_main": bool(self.capabilities & XRAY_CAP_DUMP_MAIN),
                "dump_rir": bool(self.capabilities & XRAY_CAP_DUMP_RIR),
                "bound_read32": bool(self.capabilities & XRAY_CAP_BOUND_READ32),
                "bound_controller": bool(
                    self.capabilities & XRAY_CAP_BOUND_CONTROLLER
                ),
                "last_journal": bool(self.capabilities & XRAY_CAP_LAST_JOURNAL),
                "a100_20b5_controller": bool(
                    self.capabilities & XRAY_CAP_A100_CONTROLLER
                ),
            },
            "cards": self.cards,
            "otp_programming_surface": False,
        }

    def identity_for_bdf(self, bdf):
        identity = self._identities.get(bdf.lower())
        if identity is None:
            raise AuditError("170hx_xray helper has no exact full-domain BDF %s" % bdf)
        return _copy_xray_identity(identity)

    def validate_sysfs_identity(self, card):
        """Bind a helper target to the independently discovered sysfs identity."""
        identity = self.identity_for_bdf(card["domain_bdf"])
        comparisons = (
            (identity.vendor, card.get("vendor"), "vendor"),
            (identity.device, card.get("device"), "device"),
            (identity.subvendor, card.get("subsystem_vendor"), "subsystem vendor"),
            (identity.subdevice, card.get("subsystem_device"), "subsystem device"),
            (identity.revision, card.get("revision"), "revision"),
        )
        for helper_value, sysfs_value, label in comparisons:
            if sysfs_value is None or helper_value != sysfs_value:
                raise AuditError(
                    "170hx_xray/sysfs %s mismatch for %s"
                    % (label, card["domain_bdf"])
                )
        return identity

    def read32(self, bdf, offset):
        if self.fd is None:
            raise AuditError("170hx_xray helper is closed")
        if not self.capabilities & XRAY_CAP_READ32:
            raise AuditError("170hx_xray helper does not advertise READ32")
        request = XrayReg32()
        request.abi_version = XRAY_HELPER_ABI
        request.struct_size = ctypes.sizeof(XrayReg32)
        request.identity = self.identity_for_bdf(bdf)
        request.offset = offset
        response = _ioctl_structure(self.fd, XRAY_IOC_READ32, request)
        # The ioctl completed even if its response is malformed.  Count it before
        # validation so a protocol failure can never be reported as zero I/O.
        self.read_count += 1
        if (
            response.abi_version != XRAY_HELPER_ABI
            or response.struct_size != ctypes.sizeof(XrayReg32)
            or not _xray_identity_equal(response.identity, request.identity)
            or response.offset != offset
        ):
            raise AuditError("170hx_xray returned a mismatched READ32 response")
        return response.value

    def _dump_document(self, response, request, recovered_from_last=False,
                       transport_error=None):
        rows = XRAY_MAIN_ROWS if request.kind == XRAY_DUMP_MAIN else XRAY_RIR_ROWS
        protocol_errors = []
        checks = (
            (response.abi_version == XRAY_HELPER_ABI, "ABI mismatch"),
            (response.struct_size == ctypes.sizeof(XrayDump), "structure-size mismatch"),
            (_xray_identity_equal(response.identity, request.identity), "identity mismatch"),
            (response.kind == request.kind, "kind mismatch"),
            (response.flags == request.flags, "flags mismatch"),
            (response.requested_passes == XRAY_MAX_PASSES, "requested-pass mismatch"),
            (response.row_count == rows, "row-count mismatch"),
            (response.completed_passes <= XRAY_MAX_PASSES, "completed-pass overflow"),
            (-4095 <= int(response.status) <= 0, "out-of-range helper status"),
            (response.coherent in (0, 1), "non-boolean coherent field"),
            (response.address_restored in (0, 1), "non-boolean address-restored field"),
            (response.debug_restored in (0, 1), "non-boolean debug-restored field"),
            (response.driver_bound_at_entry in (0, 1), "non-boolean driver-bound field"),
        )
        for condition, message in checks:
            if not condition:
                protocol_errors.append(message)
        completed = min(int(response.completed_passes), XRAY_MAX_PASSES)
        passes = [
            [int(response.words[pass_index][row]) for row in range(rows)]
            for pass_index in range(completed)
        ]
        computed_coherent = bool(
            len(passes) == XRAY_MAX_PASSES and passes[0] == passes[1]
        )
        status = int(response.status)
        if bool(response.coherent) and not computed_coherent:
            protocol_errors.append("kernel coherence flag disagrees with row data")
        if status == 0 and bool(response.coherent) != computed_coherent:
            protocol_errors.append("successful response has an invalid coherence flag")
        failed_pass_raw = int(response.failed_pass)
        failed_row_raw = int(response.failed_row)
        if ((failed_pass_raw == 0xFFFFFFFF) != (failed_row_raw == 0xFFFFFFFF)):
            protocol_errors.append("failure pass/row sentinel mismatch")
        completed_data_reads = int(response.completed_passes) * rows
        if (
            failed_pass_raw != 0xFFFFFFFF
            and failed_row_raw != 0xFFFFFFFF
            and failed_pass_raw == int(response.completed_passes)
            and 0 <= failed_row_raw < rows
        ):
            completed_data_reads += failed_row_raw
        if status == 0:
            if completed != XRAY_MAX_PASSES:
                protocol_errors.append("successful response lacks both passes")
            if int(response.reads_issued) != rows * XRAY_MAX_PASSES:
                protocol_errors.append("successful response has an invalid CMD_READ count")
            expected_writes = 2049 if request.kind == XRAY_DUMP_MAIN else 35
            if int(response.writes_issued) != expected_writes:
                protocol_errors.append("successful response has an invalid MMIO-write count")
            if response.failed_pass != 0xFFFFFFFF or response.failed_row != 0xFFFFFFFF:
                protocol_errors.append("successful response contains a failure index")
        elif protocol_errors == [] and not (
            completed_data_reads <= int(response.reads_issued)
            <= min(rows * XRAY_MAX_PASSES, completed_data_reads + 1)
        ):
            protocol_errors.append("failed response has an impossible CMD_READ count")

        if status != 0:
            minimum_failed_writes = 2 * int(response.reads_issued)
            if request.kind == XRAY_DUMP_RIR and int(response.reads_issued) > 0:
                minimum_failed_writes += 1  # DEBUGCTRL.RWL setup precedes RIR reads.
            if int(response.writes_issued) < minimum_failed_writes:
                protocol_errors.append("failed response undercounts required MMIO writes")

        if int(response.writes_issued) > (
            2049 if request.kind == XRAY_DUMP_MAIN else 35
        ):
            protocol_errors.append("response has an impossible MMIO-write count")

        entry_values = {
            name: int(getattr(response.entry, name))
            for name, _ctype in XrayControllerSnapshot._fields_
        }
        exit_values = {
            name: int(getattr(response.exit, name))
            for name, _ctype in XrayControllerSnapshot._fields_
        }
        if status == 0:
            ctrl_exact_idle = 0xE0040000
            for label, values in (("entry", entry_values), ("exit", exit_values)):
                if values["pmc_boot0"] in (0, 0xFFFFFFFF):
                    protocol_errors.append("%s PMC_BOOT0 is invalid" % label)
                if values["fusectrl"] != ctrl_exact_idle:
                    protocol_errors.append(
                        "%s FUSECTRL is not exact completed idle 0xe0040000" % label
                    )
                if values["debugctrl"] != 0:
                    protocol_errors.append("%s DEBUGCTRL is not restored to zero" % label)
                if values["iff_sw_fusing"] & 1:
                    protocol_errors.append("%s IFF_SW_FUSING is active" % label)
                if values["crc_status"] & 0x77 != 0x55:
                    protocol_errors.append("%s CRC_STATUS is not clean" % label)
            stable_fields = (
                "pmc_boot0", "fuseaddr", "en_sw_override", "iff_record",
                "debugctrl", "crc_status", "enable_fuse_program_status",
                "disable_fuse_program_status", "bypass_fuses_status",
                "disable_sw_override_status", "iff_sw_fusing",
                "debugctrl_priv_mask", "fusewdata_priv_mask",
                "fusectrl_priv_mask", "opt_priv_mask",
            )
            for name in stable_fields:
                if entry_values[name] != exit_values[name]:
                    protocol_errors.append("entry/exit %s changed" % name)
            if request.kind == XRAY_DUMP_MAIN and (
                entry_values["en_sw_override"] & 1
                or exit_values["en_sw_override"] & 1
            ):
                protocol_errors.append("MAIN response has software override active")
            if not response.address_restored or not response.debug_restored:
                protocol_errors.append("successful response lacks verified restoration")
        for value, limit, label in (
            (int(response.failed_pass), XRAY_MAX_PASSES, "failed pass"),
            (int(response.failed_row), rows, "failed row"),
        ):
            if value != 0xFFFFFFFF and not 0 <= value < limit:
                protocol_errors.append("%s is out of range" % label)
        if recovered_from_last:
            protocol_errors.append(
                "DUMP transport failed; LAST has no sequence token proving this invocation"
            )
        pass_hashes = [
            hashlib.sha256(
                b"".join(struct.pack("<I", word) for word in row_pass)
            ).hexdigest()
            for row_pass in passes
        ]
        if status == 0:
            status_text = "ok"
        elif -4095 <= status < 0:
            status_text = os.strerror(-status)
        else:
            status_text = "invalid helper status"
        protocol_valid = not protocol_errors
        return {
            "helper_abi": response.abi_version,
            "helper_struct_size": response.struct_size,
            "identity": _xray_identity_document(response.identity),
            "kind": "main_ff" if request.kind == XRAY_DUMP_MAIN else "rir",
            "response_kind": response.kind,
            "flags": "0x%08x" % response.flags,
            "status": status,
            "status_text": status_text,
            "requested_passes": response.requested_passes,
            "completed_passes": response.completed_passes,
            "row_count": response.row_count,
            "coherent": computed_coherent,
            "kernel_reported_coherent": bool(response.coherent),
            "failed_pass": (
                None if response.failed_pass == 0xFFFFFFFF else response.failed_pass
            ),
            "failed_row": (
                None if response.failed_row == 0xFFFFFFFF else response.failed_row
            ),
            "operational_writes_issued": int(response.writes_issued),
            "cmd_read_commands_issued": int(response.reads_issued),
            "completed_data_reads_lower_bound": completed_data_reads,
            "counts_complete": bool(protocol_valid and not recovered_from_last),
            "address_restored": bool(response.address_restored),
            "debug_restored": bool(response.debug_restored),
            "driver_bound_at_entry": bool(response.driver_bound_at_entry),
            "entry": _xray_snapshot_document(response.entry),
            "exit": _xray_snapshot_document(response.exit),
            "passes": [[hex32(word) for word in row_pass] for row_pass in passes],
            "pass_hashes": pass_hashes,
            "protocol_valid": protocol_valid,
            "protocol_errors": protocol_errors,
            "recovered_from_last": bool(recovered_from_last),
            "transport_error": transport_error,
            "certified_by_helper": bool(
                protocol_valid
                and status == 0
                and completed == XRAY_MAX_PASSES
                and computed_coherent
                and bool(response.address_restored)
                and bool(response.debug_restored)
                and not bool(response.driver_bound_at_entry)
            ),
            "otp_commands": 0,
        }

    def _last_after_dump_error(self, request, transport_error):
        last_request = XrayDump()
        last_request.abi_version = XRAY_HELPER_ABI
        last_request.struct_size = ctypes.sizeof(XrayDump)
        last_request.identity = _copy_xray_identity(request.identity)
        try:
            response = _ioctl_structure(self.fd, XRAY_IOC_LAST, last_request)
        except (OSError, AuditError, ValueError):
            return None
        return self._dump_document(
            response, request, recovered_from_last=True,
            transport_error="%s: %s" % (type(transport_error).__name__, transport_error),
        )

    def dump(self, bdf, kind, allow_bound=False):
        if self.fd is None:
            raise AuditError("170hx_xray helper is closed")
        required_capability = (
            XRAY_CAP_DUMP_MAIN if kind == XRAY_DUMP_MAIN else XRAY_CAP_DUMP_RIR
        )
        if not self.capabilities & required_capability:
            raise AuditError("170hx_xray helper lacks the requested dump capability")
        request = XrayDump()
        request.abi_version = XRAY_HELPER_ABI
        request.struct_size = ctypes.sizeof(XrayDump)
        request.identity = self.identity_for_bdf(bdf)
        request.kind = kind
        request.flags = XRAY_DUMP_F_CLEAN_CRC
        if kind == XRAY_DUMP_MAIN:
            request.flags |= XRAY_DUMP_F_NO_SW_IFF
        if allow_bound:
            request.flags |= XRAY_DUMP_F_ALLOW_BOUND
        request.requested_passes = XRAY_MAX_PASSES
        try:
            response = _ioctl_structure(self.fd, XRAY_IOC_DUMP, request)
        except OSError as exc:
            recovered = self._last_after_dump_error(request, exc)
            if recovered is not None:
                return recovered
            raise
        return self._dump_document(response, request)


class BoundXrayHelperBackend:
    name = "170hx-xray-helper-allowlisted-read32"

    def __init__(self, helper, bdf, allowed_offsets):
        self.helper = helper
        self.bdf = bdf
        self.allowed_offsets = frozenset(allowed_offsets)
        self.start_count = helper.read_count

    @property
    def read_count(self):
        return self.helper.read_count - self.start_count

    def read32(self, offset):
        if offset not in self.allowed_offsets:
            raise AuditError("offset is not in the passive-read allowlist: 0x%x" % offset)
        return self.helper.read32(self.bdf, offset)


def correlate_gpcprobe_index(probe, target, all_cards):
    encoded = (target["bus"] << 8) | (target["slot"] << 3) | target["function"]
    same_location = [
        card for card in all_cards
        if card["bus"] == target["bus"]
        and card["slot"] == target["slot"]
        and card["function"] == target["function"]
    ]
    if len(same_location) != 1:
        raise AuditError(
            "gpcprobe omits PCI domain; bus/devfn %04x maps to %d sysfs devices"
            % (encoded, len(same_location))
        )
    indexes = [index for index, value in enumerate(probe.encoded_bdfs) if value == encoded]
    if len(indexes) != 1:
        raise AuditError(
            "gpcprobe bus/devfn %04x appears %d times" % (encoded, len(indexes))
        )
    return indexes[0]


class BoundGpcProbeBackend:
    """Allowlisted read-only view of one gpcprobe index."""

    name = "existing-gpcprobe-read32"

    def __init__(self, probe, index, allowed_offsets):
        self.probe = probe
        self.index = index
        self.allowed_offsets = frozenset(allowed_offsets)
        self.start_count = probe.read_count

    @property
    def read_count(self):
        return self.probe.read_count - self.start_count

    def read32(self, offset):
        if offset not in self.allowed_offsets:
            raise AuditError("offset is not in the passive-read allowlist: 0x%x" % offset)
        return self.probe.read32(self.index, offset)


class SysfsResourceBackend:
    """O_RDONLY/PROT_READ sysfs resource0 mapping; never changes PM/config."""

    name = "sysfs-resource0-readonly"

    def __init__(self, card, allowed_offsets):
        self.allowed_offsets = frozenset(allowed_offsets)
        self.read_count = 0
        runtime_status = card.get("runtime_status")
        power_state = card.get("pci_power_state")
        if runtime_status not in ("active", "unsupported") or power_state != "D0":
            raise AuditError(
                "device runtime/PCI power state is %r/%r; refusing MMIO"
                % (runtime_status, power_state)
            )
        if card.get("driver") is not None:
            raise AuditError(
                "device is bound to %s; refusing direct sysfs BAR mapping"
                % card.get("driver")
            )
        sysfs_path = card["sysfs_path"]
        config_path = os.path.join(sysfs_path, "config")
        config_flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            config_flags |= os.O_CLOEXEC
        config_fd = os.open(config_path, config_flags)
        try:
            command_raw = os.pread(config_fd, 2, 4)
        finally:
            os.close(config_fd)
        if len(command_raw) != 2:
            raise AuditError("short PCI command read")
        command = struct.unpack("<H", command_raw)[0]
        if not command & 0x2:
            raise AuditError("PCI memory decoding is disabled; refusing to enable it")
        resource_path = os.path.join(sysfs_path, "resource0")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        self.fd = os.open(resource_path, flags)
        length = max(max(self.allowed_offsets) + 4, BAR0_MIN_LENGTH)
        try:
            stat_size = os.fstat(self.fd).st_size
            if stat_size and stat_size < length:
                raise AuditError(
                    "BAR0 resource is too small: 0x%x < 0x%x" % (stat_size, length)
                )
            self.mapping = mmap.mmap(
                self.fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ
            )
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if getattr(self, "mapping", None) is not None:
            self.mapping.close()
            self.mapping = None
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def read32(self, offset):
        if offset not in self.allowed_offsets:
            raise AuditError("offset is not in the passive-read allowlist: 0x%x" % offset)
        value = struct.unpack("<I", self.mapping[offset:offset + 4])[0]
        self.read_count += 1
        return value


class NvmlReadonlyQueries:
    """Small ctypes NVML query surface with no state-changing symbols bound."""

    def __init__(self, library=None):
        candidates = []
        if library:
            candidates.append(library)
        discovered = ctypes.util.find_library("nvidia-ml")
        if discovered:
            candidates.append(discovered)
        candidates.extend(("libnvidia-ml.so.1", "libnvidia-ml.so"))
        last_error = None
        self.lib = None
        for candidate in candidates:
            try:
                self.lib = ctypes.CDLL(candidate)
                self.library = candidate
                break
            except OSError as exc:
                last_error = exc
        if self.lib is None:
            raise AuditError("NVML library unavailable: %s" % last_error)

        self._bind_required("nvmlInit_v2", [], ctypes.c_int)
        self._bind_required("nvmlShutdown", [], ctypes.c_int)
        self._bind_required(
            "nvmlDeviceGetHandleByPciBusId_v2",
            [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int,
        )
        self._bind_optional("nvmlErrorString", [ctypes.c_int], ctypes.c_char_p)
        self._bind_optional(
            "nvmlDeviceGetNumGpuCores",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        self._bind_optional(
            "nvmlDeviceGetEccMode",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        self._bind_optional(
            "nvmlDeviceGetMigMode",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        self._bind_optional(
            "nvmlDeviceGetNvLinkState",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        result = self.lib.nvmlInit_v2()
        if result != 0:
            raise AuditError("nvmlInit_v2 failed: %s" % self.error(result))
        self.initialized = True

    def _bind_required(self, name, argtypes, restype):
        try:
            function = getattr(self.lib, name)
        except AttributeError as exc:
            raise AuditError("NVML lacks required query symbol %s" % name) from exc
        function.argtypes = argtypes
        function.restype = restype

    def _bind_optional(self, name, argtypes, restype):
        try:
            function = getattr(self.lib, name)
        except AttributeError:
            return
        function.argtypes = argtypes
        function.restype = restype

    def error(self, result):
        function = getattr(self.lib, "nvmlErrorString", None)
        if function is not None:
            try:
                message = function(result)
                if message:
                    return "%d (%s)" % (result, message.decode("utf-8", errors="replace"))
            except (ValueError, OSError):
                pass
        return str(result)

    def close(self):
        if getattr(self, "initialized", False):
            self.lib.nvmlShutdown()
            self.initialized = False

    def _uint_query(self, name, handle):
        function = getattr(self.lib, name, None)
        if function is None:
            return {"status": "symbol_unavailable"}
        value = ctypes.c_uint()
        result = function(handle, ctypes.byref(value))
        if result != 0:
            return {"status": "error", "error": self.error(result), "nvml_result": result}
        return {"status": "ok", "value": value.value}

    def _pair_query(self, name, handle):
        function = getattr(self.lib, name, None)
        if function is None:
            return {"status": "symbol_unavailable"}
        current = ctypes.c_uint()
        pending = ctypes.c_uint()
        result = function(handle, ctypes.byref(current), ctypes.byref(pending))
        if result != 0:
            return {"status": "error", "error": self.error(result), "nvml_result": result}
        names = {0: "disabled", 1: "enabled"}
        return {
            "status": "ok",
            "current": current.value,
            "current_name": names.get(current.value, "unknown"),
            "pending": pending.value,
            "pending_name": names.get(pending.value, "unknown"),
        }

    def query_card(self, bdf):
        handle = ctypes.c_void_p()
        result = self.lib.nvmlDeviceGetHandleByPciBusId_v2(
            bdf.encode("ascii"), ctypes.byref(handle)
        )
        if result != 0:
            return {
                "status": "handle_error",
                "error": self.error(result),
                "nvml_result": result,
                "query_only": True,
            }
        core_count = self._uint_query("nvmlDeviceGetNumGpuCores", handle)
        if core_count.get("status") == "ok":
            cores = core_count["value"]
            core_count["ga100_fp32_cores_per_sm_assumption"] = 64
            if cores % 64 == 0:
                core_count["derived_ga100_sm_count"] = cores // 64
                core_count["derivation_confidence_percent"] = 95
            else:
                core_count["derived_ga100_sm_count"] = None
                core_count["derivation_warning"] = "core count is not divisible by 64"
        nvlink = {"links": []}
        nvlink_function = getattr(self.lib, "nvmlDeviceGetNvLinkState", None)
        if nvlink_function is None:
            nvlink["status"] = "symbol_unavailable"
        else:
            results = []
            for link in range(18):
                state = ctypes.c_uint()
                link_result = nvlink_function(handle, link, ctypes.byref(state))
                results.append(link_result)
                nvlink["links"].append({
                    "link": link,
                    "status": "ok" if link_result == 0 else "error",
                    "active": bool(state.value) if link_result == 0 else None,
                    "nvml_result": link_result,
                    "error": None if link_result == 0 else self.error(link_result),
                })
            if any(item == 0 for item in results):
                nvlink["status"] = "queried"
            elif results and len(set(results)) == 1:
                nvlink["status"] = "uniformly_unavailable"
                nvlink["uniform_nvml_result"] = results[0]
            else:
                nvlink["status"] = "unavailable"
        return {
            "status": "ok",
            "query_only": True,
            "library": self.library,
            "gpu_cores": core_count,
            "ecc_mode": self._pair_query("nvmlDeviceGetEccMode", handle),
            "mig_mode": self._pair_query("nvmlDeviceGetMigMode", handle),
            "nvlink_state": nvlink,
        }


def _bounded_process_text(data, limit=256 * 1024):
    complete = len(data) <= limit
    retained = data[:limit]
    return {
        "text": retained.decode("utf-8", errors="replace"),
        "sha256": sha256_bytes(data),
        "complete": complete,
        "byte_count": len(data),
    }


def _run_query_command(argv, timeout):
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "argv": argv,
            "query_only": True,
            "status": "error",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    return {
        "argv": argv,
        "query_only": True,
        "status": "ok" if completed.returncode == 0 else "command_error",
        "returncode": completed.returncode,
        "stdout": _bounded_process_text(completed.stdout),
        "stderr": _bounded_process_text(completed.stderr),
    }


def _smi_csv_query(executable, bdf, fields, timeout):
    field_text = ",".join(fields)
    result = _run_query_command([
        executable,
        "--query-gpu=" + field_text,
        "--format=csv,noheader,nounits",
        "--id=" + bdf,
    ], timeout)
    result["requested_fields"] = list(fields)
    if result.get("status") == "ok":
        lines = result["stdout"]["text"].strip().splitlines()
        if len(lines) == 1:
            values = [item.strip() for item in lines[0].split(",")]
            if len(values) == len(fields):
                result["parsed"] = dict(zip(fields, values))
            else:
                result["parse_warning"] = "CSV column count does not match requested fields"
    return result


def passive_smi_queries(executable, bdf, timeout):
    if os.path.sep in executable:
        resolved = executable if os.path.isfile(executable) else None
    else:
        resolved = shutil.which(executable)
    if resolved is None:
        return {"status": "unavailable", "error": "nvidia-smi executable not found"}
    feature_fields = (
        "pci.bus_id", "uuid", "name", "vbios_version", "memory.total",
        "compute_cap", "driver_version", "ecc.mode.current", "ecc.mode.pending",
        "ecc.errors.corrected.volatile.total",
        "ecc.errors.corrected.aggregate.total",
        "ecc.errors.uncorrected.volatile.total",
        "ecc.errors.uncorrected.aggregate.total",
        "mig.mode.current", "mig.mode.pending",
    )
    feature = _smi_csv_query(resolved, bdf, feature_fields, timeout)
    pcie = _smi_csv_query(resolved, bdf, (
        "pci.bus_id", "pcie.link.gen.current", "pcie.link.gen.max",
        "pcie.link.width.current",
    ), timeout)
    clocks_power = _smi_csv_query(resolved, bdf, (
        "pci.bus_id", "power.limit", "clocks.current.graphics",
        "clocks.current.memory", "clocks.max.graphics", "clocks.max.memory",
    ), timeout)
    mig_profiles = _run_query_command(
        [resolved, "mig", "-lgip", "-i", bdf], timeout
    )
    if mig_profiles.get("status") == "ok":
        profile_lines = []
        for line in mig_profiles["stdout"]["text"].splitlines():
            if "MIG " in line and "gb" in line.lower():
                profile_lines.append(" ".join(line.split()))
        mig_profiles["profile_lines"] = profile_lines
    nvlink = _run_query_command(
        [resolved, "nvlink", "-s", "-i", bdf], timeout
    )
    return {
        "status": "queried",
        "query_only": True,
        "executable": resolved,
        "features": feature,
        "pcie": pcie,
        "clocks_power": clocks_power,
        "mig_profiles": mig_profiles,
        "nvlink_status": nvlink,
        "note": "Only query/list commands are issued; no MIG creation or state setter is present.",
    }


def classify_mmio_value(value, register=None):
    if (value >> 16) == 0xBADF:
        if register is not None and register.get("badf") == "availability":
            return "unavailable_expected"
        return "badf_response"
    if value == 0xFFFFFFFF:
        if register is not None and register.get("all_ones") == "valid":
            return "ok"
        if register is not None and register.get("all_ones") == "availability":
            return "unavailable_expected"
        return "suspicious_all_ones"
    return "ok"


def safe_read(backend, register):
    try:
        value = backend.read32(register["address"])
    except (OSError, AuditError, ValueError) as exc:
        return {"status": "io_error", "error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "status": classify_mmio_value(value, register),
        "value": hex32(value),
        "u32": value,
        "access_effect": register["access_effect"],
        "stability_policy": register["stability"],
    }


def read_sentinels(backend):
    return {
        name: safe_read(backend, REGISTER_BY_NAME[name])
        for name in SENTINEL_NAMES
    }


def capture_pass(backend, number, capture_registers=None, register_profile="core"):
    capture_registers = capture_registers or DEFAULT_CAPTURE_REGISTERS
    before = read_sentinels(backend)
    registers = {}
    for register in capture_registers:
        registers[register["name"]] = safe_read(backend, register)
    after = read_sentinels(backend)
    canonical = _strict_json_dumps(registers, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "number": number,
        "register_profile": register_profile,
        "register_count": len(capture_registers),
        "before_sentinels": before,
        "registers": registers,
        "after_sentinels": after,
        "register_sha256": sha256_bytes(canonical),
    }


def _reading_identity(reading):
    return reading.get("status"), reading.get("value"), reading.get("error")


def _captured_register_definitions(passes):
    if not passes:
        return ()
    names = set(passes[0].get("registers", {}))
    for item in passes[1:]:
        names.intersection_update(item.get("registers", {}))
    return tuple(
        register for register in SAFE_READ_REGISTERS if register["name"] in names
    )


def summarize_passes(passes):
    changed = []
    changed_required = []
    changed_snapshot = []
    changed_volatile = []
    errors = []
    sentinel_changes = []
    controller_context_issues = []
    context_warnings = []
    for register in _captured_register_definitions(passes):
        name = register["name"]
        readings = [item["registers"].get(name, {}) for item in passes]
        identities = [_reading_identity(reading) for reading in readings]
        if any(reading.get("status") not in ("ok", "unavailable_expected")
               for reading in readings):
            errors.append(name)
        if len(set(identities)) != 1:
            changed.append(name)
            if register["stability"] == "required":
                changed_required.append(name)
            elif register["stability"] == "snapshot":
                changed_snapshot.append(name)
            else:
                changed_volatile.append(name)
    for item in passes:
        for name in SENTINEL_NAMES:
            before = item["before_sentinels"].get(name, {})
            after = item["after_sentinels"].get(name, {})
            if _reading_identity(before) != _reading_identity(after):
                sentinel_changes.append({"pass": item["number"], "register": name})
        registers = item["registers"]
        fusectrl = registers.get("FUSECTRL", {})
        if "u32" in fusectrl:
            command = fusectrl["u32"] & 0x3
            state = (fusectrl["u32"] >> 16) & 0x1F
            if command != 0 or state != 4:
                controller_context_issues.append(
                    "pass %d: FUSECTRL command=%d state=%d; expected command=0 state=4"
                    % (item["number"], command, state)
                )
        debugctrl = registers.get("DEBUGCTRL", {})
        if "u32" in debugctrl:
            if debugctrl["u32"] & 0x2:
                controller_context_issues.append(
                    "pass %d: DEBUGCTRL.RWL is active" % item["number"]
                )
            elif debugctrl["u32"] != 0:
                context_warnings.append(
                    "pass %d: DEBUGCTRL is non-zero (%s)"
                    % (item["number"], hex32(debugctrl["u32"]))
                )
        iff_fusing = registers.get("IFF_SW_FUSING", {})
        if "u32" in iff_fusing and iff_fusing["u32"] & 1:
            controller_context_issues.append(
                "pass %d: IFF_SW_FUSING is active" % item["number"]
            )
        sw_override = registers.get("EN_SW_OVERRIDE", {})
        if "u32" in sw_override and sw_override["u32"] & 1:
            context_warnings.append(
                "pass %d: EN_SW_OVERRIDE is active; option reads are effective/override-plane observations, not physical-row proof"
                % item["number"]
            )
    controller_context_issues = sorted(set(controller_context_issues))
    context_warnings = sorted(set(context_warnings))
    coherent = (
        not changed_required and not errors and not sentinel_changes
        and not controller_context_issues
    )
    return {
        "coherent": coherent,
        "changed_between_passes": sorted(changed),
        "required_stability_changes": sorted(changed_required),
        "snapshot_changes": sorted(changed_snapshot),
        "volatile_changes": sorted(changed_volatile),
        "read_errors": sorted(errors),
        "sentinel_changes_within_pass": sentinel_changes,
        "controller_context_issues": controller_context_issues,
        "context_warnings": context_warnings,
    }


def stable_register_values(passes):
    stable = {}
    if not passes:
        return stable
    for register in _captured_register_definitions(passes):
        readings = [item["registers"].get(register["name"], {}) for item in passes]
        identities = [_reading_identity(reading) for reading in readings]
        if len(set(identities)) != 1:
            continue
        reading = readings[0]
        if "u32" not in reading:
            continue
        value = reading["u32"]
        stable[register["name"]] = {
            "address": hex32(register["address"]),
            "raw": hex32(value),
            "status": reading["status"],
            "decoded_fields": decode_fields(register, value),
            "category": register["category"],
            "role": register["role"],
            "source": register["source"],
            "access_effect": register["access_effect"],
            "stability_policy": register["stability"],
        }
    return stable


def unknown_register_census(stable_values, register_profile):
    """Describe every catalogued word, including the words we refuse to read.

    "Unknown" never means "safe to sweep".  A catalog entry is either backed
    by a stable allowlisted read, absent from the selected cumulative profile,
    unstable/failed, or deliberately not read because its access semantics are
    stateful.  This makes the negative evidence machine-readable without
    widening the kernel allowlist or inventing uncatalogued offsets.
    """
    selected_names = {
        item["name"] for item in registers_for_profile(register_profile)
    }
    entries = []
    counts = {}
    for register in CURATED_REGISTERS:
        reading = stable_values.get(register["name"])
        if not register["capture_safe"] or register["access_effect"] != "pure_read":
            disposition = "not_read_stateful_or_unsafe"
            reason = (
                "ordinary read changes hidden state or lacks a proved pure-read contract"
            )
        elif register["name"] not in selected_names:
            disposition = "not_requested_by_profile"
            reason = "catalogued pure-read word is outside the selected profile"
        elif not isinstance(reading, dict) or reading.get("status") != "ok":
            disposition = "not_acquired_stably"
            reason = "read failed, was unavailable, or differed across passes"
        elif register.get("fields"):
            disposition = "acquired_with_field_decode"
            reason = None
        else:
            disposition = "acquired_raw_semantics_unknown"
            reason = "raw u32 is retained; field semantics are not established"
        counts[disposition] = counts.get(disposition, 0) + 1
        entry = {
            "name": register["name"],
            "address": hex32(register["address"]),
            "access_effect": register["access_effect"],
            "capture_safe": bool(register["capture_safe"]),
            "disposition": disposition,
            "reason": reason,
            "source": register["source"],
            "interpretation_confidence": register.get(
                "interpretation_confidence"
            ),
        }
        if isinstance(reading, dict):
            entry["reading"] = reading
        entries.append(entry)
    return {
        "schema": "170hx-xray-unknown-register-census-v1",
        "profile": register_profile,
        "catalogued_entries": len(entries),
        "counts_by_disposition": counts,
        "arbitrary_bar_sweep_performed": False,
        "uncatalogued_offsets_read": 0,
        "stateful_ports_read": 0,
        "scope": (
            "complete disposition of the retained register catalog, not a claim "
            "that every GA100 BAR0 offset is known or safe to read"
        ),
        "entries": entries,
    }


def qualify_zero_write_glitch_observation(captured):
    """Qualify a transient 2080/DSOV=0 phenotype from repeated pure reads.

    This deliberately does not claim glitch causality, persistence, fuse-write
    eligibility, or a physical MAIN/RIR view.  It also cannot be used when any
    controller command was issued: the level's defining contract is literally
    zero device-MMIO writes by XRAY.
    """
    card = captured.get("card", {})
    registers = captured.get("registers", {})
    capture = captured.get("capture", {})
    required = {
        "DISABLE_SW_OVERRIDE_STATUS": (0x1, 0),
        "EN_SW_OVERRIDE": (0x1, 0),
        "IFF_SW_FUSING": (0x1, 0),
        "DEBUGCTRL": (0x2, 0),
    }
    checks = []
    values = {}
    for name, (mask, expected) in required.items():
        value = _stable_register_u32(registers, name)
        values[name] = hex32(value) if value is not None else None
        checks.append({
            "check": name,
            "passed": value is not None and (value & mask) == expected,
            "mask": hex32(mask),
            "expected_masked": hex32(expected),
            "observed": values[name],
        })
    fusectrl = _stable_register_u32(registers, "FUSECTRL")
    values["FUSECTRL"] = hex32(fusectrl) if fusectrl is not None else None
    checks.extend((
        {
            "check": "identity_device_is_2080",
            "passed": str(card.get("device", "")).lower() == "0x2080",
            "observed": card.get("device"),
        },
        {
            "check": "repeated_snapshot_coherent",
            "passed": capture.get("coherence", {}).get("coherent") is True,
            "observed": capture.get("coherence", {}).get("coherent"),
        },
        {
            "check": "fusectrl_idle_command_and_state",
            "passed": bool(
                fusectrl is not None
                and (fusectrl & 0x3) == 0
                and ((fusectrl >> 16) & 0x1F) == 4
            ),
            "observed": values["FUSECTRL"],
            "expected": "CMD[1:0]=0 and STATE[20:16]=4",
        },
    ))
    qualified = all(item["passed"] is True for item in checks)
    informational = {}
    for name in (
        "ENABLE_FUSE_PROGRAM_STATUS", "DISABLE_FUSE_PROGRAM_STATUS",
        "BYPASS_FUSES_STATUS", "CRC_STATUS",
    ):
        value = _stable_register_u32(registers, name)
        informational[name] = hex32(value) if value is not None else None
    return {
        "schema": "170hx-xray-glitch-phenotype-v1",
        "requested": "transient_glitched_state",
        "status": (
            "qualified_transient_zero_write_phenotype"
            if qualified else "not_qualified"
        ),
        "qualified": qualified,
        "checks": checks,
        "stable_values": values,
        "informational_not_gates": informational,
        "identity_is_supporting_not_sufficient": True,
        "programming_writes": 0,
        "operational_device_mmio_writes": 0,
        "controller_rows_available": False,
        "otp_eligibility_claimed": False,
        "glitch_causality_claimed": False,
        "persistence_claimed": False,
        "scope": (
            "repeated allowlisted pure-read phenotype only; MAIN/RIR, write-gate "
            "eligibility and cold persistence remain unproved"
        ),
    }


def _stable_register_u32(stable_values, name, mask=0xFFFFFFFF):
    """Return one stable successful register value, or ``None``.

    Keeping this conversion in one place prevents absent/error readings from
    being silently coerced to zero, which would overstate an unlock ceiling.
    """
    reading = stable_values.get(name)
    if not isinstance(reading, dict) or reading.get("status") != "ok":
        return None
    try:
        return int(reading["raw"], 0) & mask
    except (KeyError, TypeError, ValueError):
        return None


def _popcount(value):
    """Python-3.9-compatible population count for non-negative bitmasks."""
    return bin(int(value)).count("1")


def _expand_fbp_pairs(mask):
    """Expand each of 12 FBP bits into its two 24-bit subpartition bits."""
    expanded = 0
    for fbp in range(GA100_FBP_COUNT):
        if mask & (1 << fbp):
            expanded |= 3 << (2 * fbp)
    return expanded


def _close_ga100_compute_masks(gpc_mask, pes_mask, tpc_masks):
    """Return the fixed-point GA100 parent/child floorsweep closure.

    GA100 exposes three PES groups per GPC with a 3/3/2 TPC split.  A disabled
    parent disables all descendants; conversely a completely disabled child
    group closes its parent.  Applying both directions to a fixed point avoids
    both double subtraction and impossible partial-parent planner targets.
    """
    gpc_mask &= (1 << GA100_GPC_COUNT) - 1
    pes_mask &= (1 << (GA100_GPC_COUNT * GA100_PES_PER_GPC)) - 1
    tpc_masks = [int(value) & 0xFF for value in tpc_masks[:GA100_GPC_COUNT]]
    tpc_masks += [0] * (GA100_GPC_COUNT - len(tpc_masks))
    changed = True
    while changed:
        changed = False
        for gpc in range(GA100_GPC_COUNT):
            old_gpc = (gpc_mask >> gpc) & 1
            old_pes = (pes_mask >> (GA100_PES_PER_GPC * gpc)) & 0x7
            old_tpc = tpc_masks[gpc]
            pes = old_pes
            tpc = old_tpc
            if old_gpc:
                pes = 0x7
                tpc = 0xFF
            for index, group_mask in enumerate(GA100_PES_TPC_MASKS):
                if pes & (1 << index):
                    tpc |= group_mask
                if (tpc & group_mask) == group_mask:
                    pes |= 1 << index
            if pes == 0x7 or tpc == 0xFF:
                gpc_mask |= 1 << gpc
                pes = 0x7
                tpc = 0xFF
            pes_shift = GA100_PES_PER_GPC * gpc
            pes_mask = (pes_mask & ~(0x7 << pes_shift)) | (pes << pes_shift)
            tpc_masks[gpc] = tpc
            if (
                old_gpc != ((gpc_mask >> gpc) & 1)
                or old_pes != pes
                or old_tpc != tpc
            ):
                changed = True
    return gpc_mask, pes_mask, tpc_masks


def _ga100_compute_layer(gpc_mask, pes_mask, tpc_masks, assessable=True,
                         unavailable=None, evidence_class=None):
    """Normalize one raw compute layer and count each disabled TPC once."""
    gpc, pes, tpcs = _close_ga100_compute_masks(gpc_mask, pes_mask, tpc_masks)
    disabled_tpcs = sum(_popcount(value) for value in tpcs)
    return {
        "assessable": bool(assessable),
        "gpc_mask": gpc,
        "pes_mask": pes,
        "tpc_masks": tpcs,
        "enabled_tpcs": (
            GA100_GPC_COUNT * GA100_TPCS_PER_GPC - disabled_tpcs
            if assessable else None
        ),
        "sms": (
            GA100_SMS_PER_TPC
            * (GA100_GPC_COUNT * GA100_TPCS_PER_GPC - disabled_tpcs)
            if assessable else None
        ),
        "unavailable": sorted(set(unavailable or [])),
        "evidence_class": evidence_class,
    }


def normalize_ga100_topology(stable_values, gib_per_subpartition=None,
                             density_provenance=None):
    """Normalize GA100 memory and compute topology into independent layers.

    This is the planner-facing arithmetic oracle.  It deliberately separates
    physical defects, programmed disables, current effective status, current
    RECONFIG policy, and the defect-preserving software-override ceiling.
    Numeric GiB values are emitted only when the caller supplies qualified
    density provenance and all FBP/FBPA/FBIO/ROP-L2 plus HALF-FBPA invariants
    needed for the corresponding layer are exact.
    """
    unavailable = []

    def get_mask(name, mask):
        value = _stable_register_u32(stable_values, name, mask)
        if value is None:
            unavailable.append(name)
        return value

    gpc_defect = get_mask("GPC_DEFECTIVE", 0xFF)
    gpc_disable = get_mask("OPT_GPC_DISABLE", 0xFF)
    gpc_effective = get_mask("STATUS_OPT_GPC", 0xFF)
    pes_defect = get_mask("PES_DEFECTIVE", 0xFFFFFF)
    pes_disable = get_mask("OPT_PES_DISABLE", 0xFFFFFF)
    tpc_defect = []
    tpc_disable = []
    tpc_effective = []
    pes_effective = 0
    reconfig = []
    reconfig_override = []
    runtime_controls_zero = True
    effective_runtime_missing = []
    policy_runtime_missing = []
    for gpc in range(GA100_GPC_COUNT):
        names = {
            "defect": "TPC_GPC%d_DEFECTIVE" % gpc,
            "disable": "OPT_TPC_GPC%d_DISABLE" % gpc,
            "effective": "STATUS_OPT_TPC_GPC%d" % gpc,
            "pes_effective": "STATUS_OPT_PES_GPC[%d]" % gpc,
            "reconfig": "OPT_TPC_GPC%d_RECONFIG" % gpc,
            "override": "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc,
            "control": "CTRL_OPT_TPC_GPC%d" % gpc,
            "extend": "DISABLE_EXTEND_TPC_GPC%d" % gpc,
        }
        values = {key: get_mask(name, 0xFF) for key, name in names.items()}
        tpc_defect.append(values["defect"] or 0)
        tpc_disable.append(values["disable"] or 0)
        tpc_effective.append(values["effective"] or 0)
        reconfig.append(values["reconfig"] or 0)
        reconfig_override.append(values["override"] or 0)
        if values["pes_effective"] is not None:
            pes_effective |= (values["pes_effective"] & 0x7) << (
                GA100_PES_PER_GPC * gpc
            )
        # STATUS_OPT_TPC is the effective leaf witness; a missing PES status
        # does not make the count unknowable because parent closure can be
        # derived from complete leaf groups.  Control/extend remain required:
        # their nonzero semantics are not established by the restricted rule.
        for key in ("effective", "control", "extend"):
            if values[key] is None:
                effective_runtime_missing.append(names[key])
        for key in ("reconfig", "override", "control", "extend"):
            if values[key] is None:
                policy_runtime_missing.append(names[key])
        if values["control"] not in (None, 0) or values["extend"] not in (None, 0):
            runtime_controls_zero = False

    defect_missing = [name for name in unavailable if (
        name in ("GPC_DEFECTIVE", "PES_DEFECTIVE")
        or (name.startswith("TPC_GPC") and name.endswith("_DEFECTIVE"))
    )]
    disable_missing = [name for name in unavailable if (
        name in ("OPT_GPC_DISABLE", "OPT_PES_DISABLE")
        or (name.startswith("OPT_TPC_GPC") and name.endswith("_DISABLE"))
    )]
    defect_assessable = not defect_missing
    disable_assessable = not disable_missing
    runtime_assessable = (
        gpc_effective is not None
        and not effective_runtime_missing
        and runtime_controls_zero
    )
    policy_assessable = (
        defect_assessable and not policy_runtime_missing and runtime_controls_zero
    )

    defect_layer = _ga100_compute_layer(
        gpc_defect or 0, pes_defect or 0, tpc_defect,
        assessable=defect_assessable, unavailable=defect_missing,
        evidence_class="physical_defect_shadows",
    )
    disable_layer = _ga100_compute_layer(
        gpc_disable or 0, pes_disable or 0, tpc_disable,
        assessable=disable_assessable, unavailable=disable_missing,
        evidence_class="programmed_disable_shadows",
    )
    effective_layer = _ga100_compute_layer(
        gpc_effective or 0, pes_effective, tpc_effective,
        assessable=runtime_assessable,
        unavailable=effective_runtime_missing,
        evidence_class="current_effective_status",
    )
    current_policy_tpcs = [
        tpc_defect[gpc] | (reconfig[gpc] & ~reconfig_override[gpc] & 0xFF)
        for gpc in range(GA100_GPC_COUNT)
    ]
    no_override_tpcs = [
        tpc_defect[gpc] | reconfig[gpc]
        for gpc in range(GA100_GPC_COUNT)
    ]
    current_policy_layer = _ga100_compute_layer(
        gpc_defect or 0, pes_defect or 0, current_policy_tpcs,
        assessable=policy_assessable,
        unavailable=defect_missing + policy_runtime_missing,
        evidence_class="defect_target_plus_current_reconfig_policy",
    )
    no_override_layer = _ga100_compute_layer(
        gpc_defect or 0, pes_defect or 0, no_override_tpcs,
        assessable=policy_assessable,
        unavailable=defect_missing + policy_runtime_missing,
        evidence_class="defect_target_plus_reconfig_without_override",
    )

    density_value = None
    density_qualified = False
    density_reason = None
    if gib_per_subpartition is not None:
        try:
            candidate_density = float(gib_per_subpartition)
        except (TypeError, ValueError):
            candidate_density = -1
        provenance_density = None
        if isinstance(density_provenance, dict):
            provenance_density = density_provenance.get("gib_per_subpartition")
        density_qualified = bool(
            math.isfinite(candidate_density)
            and candidate_density > 0
            and isinstance(density_provenance, dict)
            and density_provenance.get("qualified") is True
            and provenance_density is not None
            and float(provenance_density) == candidate_density
        )
        if density_qualified:
            density_value = candidate_density
        else:
            density_reason = "density lacks matching qualified provenance"
    else:
        density_reason = "density was not supplied"

    half_names = (
        "OPT_HALF_FBPA_ENABLE", "CTRL_OPT_HALF_FBPA", "STATUS_HALF_FBPA",
    )
    half_values = {name: get_mask(name, 0xFFFFFFFF) for name in half_names}
    half_complete = all(value is not None for value in half_values.values())
    half_nonzero = any(
        value != 0 for value in half_values.values() if value is not None
    )

    memory_specs = {
        "defect": {
            "fbp": "FBP_DEFECTIVE", "fbpa": "FBPA_DEFECTIVE",
            "fbio": "FBIO_DEFECTIVE", "rop_l2": "ROP_L2_DEFECTIVE",
        },
        "disable": {
            "fbp": "OPT_FBP_DISABLE", "fbpa": "OPT_FBPA_DISABLE",
            "fbio": "OPT_FBIO_DISABLE", "rop_l2": "OPT_ROP_L2_DISABLE",
        },
        "effective": {
            "fbp": "STATUS_OPT_FBP", "fbpa": "STATUS_OPT_FBPA",
            "fbio": "STATUS_OPT_FBIO",
        },
    }
    memory_layers = {}
    memory_raw = {}
    for layer_name, names in memory_specs.items():
        masks = {}
        layer_missing = []
        for plane, name in names.items():
            mask = 0xFFF if plane == "fbp" else 0xFFFFFF
            value = get_mask(name, mask)
            if value is None:
                layer_missing.append(name)
            else:
                masks[plane] = value
        memory_raw[layer_name] = masks
        expected = _expand_fbp_pairs(masks["fbp"]) if "fbp" in masks else None
        companion_matches = {
            plane: (masks.get(plane) == expected if expected is not None and plane in masks else None)
            for plane in names if plane != "fbp"
        }
        pairing_exact = (
            not layer_missing
            and expected is not None
            and all(value is True for value in companion_matches.values())
        )
        enabled = (
            GA100_SUBPARTITION_COUNT - _popcount(expected)
            if pairing_exact else None
        )
        numeric_exact = bool(
            pairing_exact and half_complete and not half_nonzero and density_qualified
        )
        capacity = enabled * density_value if numeric_exact else None
        if isinstance(capacity, float) and capacity.is_integer():
            capacity = int(capacity)
        memory_layers[layer_name] = {
            "assessable": not layer_missing,
            "masks": masks,
            "expected_subpartition_mask": expected,
            "companion_matches": companion_matches,
            "pairing_exact": pairing_exact,
            "enabled_subpartitions": enabled,
            "capacity_gib": capacity,
            "unavailable": layer_missing,
        }

    memory_violations = []
    for plane in sorted(set(memory_raw["defect"]) & set(memory_raw["disable"])):
        defect = memory_raw["defect"][plane]
        disable = memory_raw["disable"][plane]
        if defect & ~disable:
            memory_violations.append(
                "%s defect mask is not a subset of the disable mask" % plane
            )
    for plane in sorted(set(memory_raw["defect"]) & set(memory_raw["effective"])):
        defect = memory_raw["defect"][plane]
        effective = memory_raw["effective"][plane]
        if defect & ~effective:
            memory_violations.append(
                "%s defect mask is not a subset of effective status" % plane
            )
    if not half_complete:
        memory_violations.append("HALF_FBPA state is incomplete")
    if half_nonzero:
        memory_violations.append(
            "HALF_FBPA is nonzero; whole-subpartition capacity arithmetic is unsafe"
        )
    if not density_qualified:
        memory_violations.append(density_reason)
    for layer_name, layer in memory_layers.items():
        if not layer["pairing_exact"]:
            memory_violations.append(
                "%s FBP and subpartition companion masks are incomplete or inconsistent"
                % layer_name
            )
    capacity_exact = bool(
        all(layer["capacity_gib"] is not None for layer in memory_layers.values())
        and not half_nonzero
    )
    supported_subpartition_counts = (16, 20, 24)
    defect_enabled_subpartitions = memory_layers["defect"][
        "enabled_subpartitions"
    ]
    supported_capacity_classes_gib = (
        [
            count * density_value
            for count in supported_subpartition_counts
        ]
        if density_qualified else []
    )
    if (
        defect_enabled_subpartitions is not None
        and defect_enabled_subpartitions not in supported_subpartition_counts
    ):
        memory_violations.append(
            "defect topology enables %d subpartitions; supported exact "
            "capacity classes require 16, 20 or 24"
            % defect_enabled_subpartitions
        )
    target_admissible = bool(
        memory_layers["defect"]["capacity_gib"] is not None
        and defect_enabled_subpartitions in supported_subpartition_counts
        and not any("defect mask is not a subset" in item for item in memory_violations)
        and not half_nonzero
    )

    compute_invariants = []
    if defect_assessable and disable_assessable:
        if defect_layer["gpc_mask"] & ~disable_layer["gpc_mask"]:
            compute_invariants.append("GPC defect mask is not a subset of disable")
        if defect_layer["pes_mask"] & ~disable_layer["pes_mask"]:
            compute_invariants.append("PES defect mask is not a subset of disable")
        for gpc in range(GA100_GPC_COUNT):
            if defect_layer["tpc_masks"][gpc] & ~disable_layer["tpc_masks"][gpc]:
                compute_invariants.append(
                    "GPC%d TPC defect mask is not a subset of disable" % gpc
                )

    return {
        "state": (
            "complete" if target_admissible and defect_assessable
            else "incomplete_or_inconsistent"
        ),
        "compute": {
            "architecture": {
                "gpc_count": GA100_GPC_COUNT,
                "pes_per_gpc": GA100_PES_PER_GPC,
                "pes_tpc_masks": list(GA100_PES_TPC_MASKS),
                "tpcs_per_gpc": GA100_TPCS_PER_GPC,
                "sms_per_tpc": GA100_SMS_PER_TPC,
                "maximum_sms": (
                    GA100_GPC_COUNT * GA100_TPCS_PER_GPC * GA100_SMS_PER_TPC
                ),
            },
            "layers": {
                "defect": defect_layer,
                "disable": disable_layer,
                "effective": effective_layer,
                "post_target_current_policy": current_policy_layer,
                "post_target_reconfig_active_no_override": no_override_layer,
                "software_override_ceiling": dict(defect_layer),
            },
            "base_formula_assessable": runtime_assessable,
            "runtime_controls_zero": runtime_controls_zero,
            "invariant_violations": compute_invariants,
        },
        "memory": {
            "architecture": {
                "fbp_count": GA100_FBP_COUNT,
                "subpartition_count": GA100_SUBPARTITION_COUNT,
                "subpartitions_per_fbp": 2,
            },
            "supported_exact_subpartition_counts": list(
                supported_subpartition_counts
            ),
            "supported_exact_capacity_classes_gib": (
                [
                    int(value) if float(value).is_integer() else value
                    for value in supported_capacity_classes_gib
                ]
            ),
            "layers": memory_layers,
            "capacity_exact": capacity_exact,
            "target_admissible": target_admissible,
            "density": {
                "qualified": density_qualified,
                "gib_per_subpartition": density_value,
                "gib_per_fbp": (
                    None if density_value is None else density_value * 2
                ),
                "provenance": density_provenance,
                "rejection_reason": density_reason,
            },
            "half_fbpa": {
                "complete": half_complete,
                "any_nonzero": half_nonzero,
                "values": half_values,
            },
            "invariant_violations": sorted(set(memory_violations)),
        },
        "unavailable": sorted(set(unavailable)),
        "scope": (
            "topology arithmetic only; an absent defect bit does not prove that a "
            "unit trains, routes, clocks, remains stable, or is policy-visible"
        ),
    }


def analyze_memory_layers(stable_values):
    """Separate GA100 HBM architecture, defect, disable and status layers.

    The 96-GiB maximum and 12-FBP/24-subpartition geometry are architecture
    facts.  A per-specimen *defect-preserving* target is the most restrictive
    of the FBP, FBPA, FBIO and ROP/L2 defect planes.  It is admitted as an exact
    8-GiB-per-FBP target only when all three 24-bit companion masks are the
    pair-expansion of the 12-bit FBP mask.  A mismatch is reported, never
    rounded into an optimistic capacity.
    """
    planes = {
        "fbp": {
            "width": GA100_FBP_COUNT,
            "unit_gib": GA100_GIB_PER_FBP,
            "defective": "FBP_DEFECTIVE",
            "disable": "OPT_FBP_DISABLE",
            "control_shadow": "OPT_FBP_DISABLE_CP",
            "control": "CTRL_OPT_FBP",
            "effective_status": "STATUS_OPT_FBP",
        },
        "fbpa": {
            "width": GA100_SUBPARTITION_COUNT,
            "unit_gib": GA100_GIB_PER_SUBPARTITION,
            "defective": "FBPA_DEFECTIVE",
            "disable": "OPT_FBPA_DISABLE",
            "control_shadow": "OPT_FBPA_DISABLE_CP",
            "control": "CTRL_OPT_FBPA",
            "effective_status": "STATUS_OPT_FBPA",
        },
        "fbio": {
            "width": GA100_SUBPARTITION_COUNT,
            "unit_gib": GA100_GIB_PER_SUBPARTITION,
            "defective": "FBIO_DEFECTIVE",
            "disable": "OPT_FBIO_DISABLE",
            "control_shadow": "OPT_FBIO_DISABLE_CP",
            "control": "CTRL_OPT_FBIO",
            "effective_status": "STATUS_OPT_FBIO",
        },
        "rop_l2": {
            "width": GA100_SUBPARTITION_COUNT,
            "unit_gib": GA100_GIB_PER_SUBPARTITION,
            "defective": "ROP_L2_DEFECTIVE",
            "disable": "OPT_ROP_L2_DISABLE",
            "control_shadow": "OPT_ROP_L2_DISABLE_CP",
        },
    }
    missing = []
    result_planes = {}
    capacities = {"defective": {}, "disable": {}, "effective_status": {}}
    masks = {"defective": {}, "disable": {}, "effective_status": {}}
    for plane_name, definition in planes.items():
        width_mask = (1 << definition["width"]) - 1
        raw = {}
        unavailable = []
        for layer, register_name in definition.items():
            if layer in ("width", "unit_gib"):
                continue
            value = _stable_register_u32(stable_values, register_name, width_mask)
            if value is None:
                unavailable.append(register_name)
                missing.append(register_name)
                continue
            raw[layer] = value
            if layer in capacities:
                masks[layer][plane_name] = value
                capacities[layer][plane_name] = (
                    definition["width"] - _popcount(value)
                ) * definition["unit_gib"]
        result_planes[plane_name] = {
            "registers": {
                layer: register_name for layer, register_name in definition.items()
                if layer not in ("width", "unit_gib")
            },
            "width_bits": definition["width"],
            "capacity_gib_per_clear_bit": definition["unit_gib"],
            "raw": {layer: hex32(value) for layer, value in raw.items()},
            "enabled_units": {
                layer: definition["width"] - _popcount(value)
                for layer, value in raw.items() if layer in capacities
            },
            "capacity_gib": {
                layer: (definition["width"] - _popcount(value))
                * definition["unit_gib"]
                for layer, value in raw.items() if layer in capacities
            },
            "unavailable": unavailable,
        }

    pair_checks = {}
    for layer in ("defective", "disable", "effective_status"):
        fbp = masks[layer].get("fbp")
        expected = None if fbp is None else _expand_fbp_pairs(fbp)
        comparisons = {}
        for companion in ("fbpa", "fbio", "rop_l2"):
            observed = masks[layer].get(companion)
            comparisons[companion] = (
                observed == expected if observed is not None and expected is not None else None
            )
        assessable = expected is not None and any(
            value is not None for value in comparisons.values()
        )
        pair_checks[layer] = {
            "assessable": assessable,
            "fbp_pair_expansion": None if expected is None else hex32(expected),
            "companion_matches": comparisons,
            "all_available_companions_match": (
                all(value is True for value in comparisons.values() if value is not None)
                if assessable else None
            ),
        }

    def lowest_per_plane_scalar(layer):
        values = list(capacities[layer].values())
        return min(values) if values else None

    # Diagnostic only: disjoint disabled positions can make this scalar larger
    # than the true joint topology.  Exact plans require pair closure below.
    defect_target = lowest_per_plane_scalar("defective")
    disable_capacity = lowest_per_plane_scalar("disable")
    status_capacity = lowest_per_plane_scalar("effective_status")
    exact_defect_pairing = (
        pair_checks["defective"]["all_available_companions_match"] is True
        and all(name in capacities["defective"] for name in planes)
    )
    exact_disable_pairing = (
        pair_checks["disable"]["all_available_companions_match"] is True
        and all(name in capacities["disable"] for name in planes)
    )
    status_required = ("fbp", "fbpa", "fbio")
    exact_status_pairing = (
        pair_checks["effective_status"]["all_available_companions_match"] is True
        and all(name in capacities["effective_status"] for name in status_required)
    )
    supported_classes = (64, 80, 96)
    target_admissible = (
        exact_defect_pairing and defect_target in supported_classes
    )
    current_exact = status_capacity if exact_status_pairing else None
    return {
        "state": (
            "complete" if target_admissible and current_exact is not None
            else "incomplete_or_inconsistent"
        ),
        "planes": result_planes,
        "pair_expansion_checks": pair_checks,
        "architecture_capacity": {
            "fbp_count": GA100_FBP_COUNT,
            "subpartition_count": GA100_SUBPARTITION_COUNT,
            "gib_per_fbp": GA100_GIB_PER_FBP,
            "gib_per_subpartition": GA100_GIB_PER_SUBPARTITION,
            "maximum_gib": GA100_FBP_COUNT * GA100_GIB_PER_FBP,
            "evidence_class": "direct_ga100_geometry_plus_specimen_crosscheck",
        },
        "capacity_projection": {
            "architecture_maximum_gib": GA100_FBP_COUNT * GA100_GIB_PER_FBP,
            "current_effective_gib": current_exact,
            "current_effective_lowest_per_plane_scalar_gib": status_capacity,
            "programmed_disable_layer_gib": (
                disable_capacity if exact_disable_pairing else None
            ),
            "programmed_disable_lowest_per_plane_scalar_gib": disable_capacity,
            "defect_preserving_target_gib": defect_target if target_admissible else None,
            "defect_lowest_per_plane_scalar_gib": defect_target,
            "additional_gib_from_current_to_defect_target": (
                defect_target - current_exact
                if target_admissible and current_exact is not None else None
            ),
            "supported_exact_classes_gib": list(supported_classes),
            "target_admissible": target_admissible,
            "lowest_per_plane_scalar_is_joint_bound": False,
        },
        "missing_or_unusable": sorted(set(missing)),
        "certainty": {
            "architecture_geometry_percent": 100,
            "pair_expansion_on_retained_four_cards_percent": 100,
            "generic_defect_preserving_capacity_rule_percent": 96,
            "functional_health_from_absence_of_defect_bit_percent": 0,
        },
        "scope": (
            "capacity arithmetic and fuse/status consistency only; HBM training, "
            "routing, VBIOS policy, memory integrity and thermal stability remain "
            "independent gates"
        ),
    }


def analyze_tpc_layers(stable_values):
    """Keep physical defect, disable, RECONFIG and effective layers separate.

    Register coordinates and the GA100 topology dimensions are direct-source
    facts.  The restricted base formula is an empirical relation validated by
    the retained captures; it is deliberately not extrapolated to nonzero
    control or disable-extend states.
    """
    per_gpc = []
    missing = []
    totals = {
        "defect_ceiling_tpcs": 0,
        "disable_layer_enabled_tpcs": 0,
        "effective_enabled_tpcs": 0,
        "additional_tpcs_from_effective_to_defect_target": 0,
        "current_reconfig_marked_tpcs": 0,
        "current_unsuppressed_reconfig_tpcs": 0,
    }
    totals_complete = True
    gpc_names = {
        "defective": "GPC_DEFECTIVE",
        "disable": "OPT_GPC_DISABLE",
        "control_shadow": "OPT_GPC_DISABLE_CP",
        "control": "CTRL_OPT_GPC",
        "effective_status": "STATUS_OPT_GPC",
    }
    gpc_values = {}
    for layer, name in gpc_names.items():
        value = _stable_register_u32(stable_values, name, 0xFF)
        if value is None:
            missing.append(name)
        else:
            gpc_values[layer] = value
    gpc_required = ("defective", "disable", "effective_status")
    gpc_complete = all(layer in gpc_values for layer in gpc_required)
    for gpc in range(GA100_GPC_COUNT):
        names = {
            "defective": "TPC_GPC%d_DEFECTIVE" % gpc,
            "disable": "OPT_TPC_GPC%d_DISABLE" % gpc,
            "reconfig": "OPT_TPC_GPC%d_RECONFIG" % gpc,
            "control": "CTRL_OPT_TPC_GPC%d" % gpc,
            "reconfig_override": "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc,
            "disable_extend": "DISABLE_EXTEND_TPC_GPC%d" % gpc,
            "effective_status": "STATUS_OPT_TPC_GPC%d" % gpc,
        }
        values = {}
        unavailable = []
        for layer, name in names.items():
            reading = stable_values.get(name)
            if not isinstance(reading, dict) or reading.get("status") != "ok":
                unavailable.append(name)
                missing.append(name)
                continue
            values[layer] = int(reading["raw"], 0) & 0xFFFFFFFF
        item = {
            "gpc": gpc,
            "registers": names,
            "unavailable": unavailable,
            "raw": {key: hex32(value) for key, value in values.items()},
        }
        required = (
            "defective", "disable", "reconfig", "control",
            "reconfig_override", "disable_extend", "effective_status",
        )
        if all(name in values for name in required) and gpc_complete:
            low = {name: values[name] & 0xFF for name in required}
            item["low_byte"] = {name: "0x%02x" % value for name, value in low.items()}
            item["gpc_bits"] = {
                layer: (gpc_values[layer] >> gpc) & 1 for layer in gpc_required
            }
            item["defect_ceiling_enabled_tpcs"] = 0 if item["gpc_bits"]["defective"] else (
                GA100_TPCS_PER_GPC - _popcount(low["defective"])
            )
            item["disable_layer_enabled_tpcs"] = 0 if item["gpc_bits"]["disable"] else (
                GA100_TPCS_PER_GPC - _popcount(low["disable"])
            )
            item["effective_enabled_tpcs"] = 0 if item["gpc_bits"]["effective_status"] else (
                GA100_TPCS_PER_GPC - _popcount(low["effective_status"])
            )
            item["additional_tpcs_from_effective_to_defect_target"] = max(
                0, item["defect_ceiling_enabled_tpcs"] - item["effective_enabled_tpcs"]
            )
            item["current_reconfig_marked_tpcs"] = _popcount(low["reconfig"])
            item["current_unsuppressed_reconfig_tpcs"] = _popcount(
                low["reconfig"] & ~low["reconfig_override"] & 0xFF
            )
            totals["defect_ceiling_tpcs"] += item["defect_ceiling_enabled_tpcs"]
            totals["disable_layer_enabled_tpcs"] += item["disable_layer_enabled_tpcs"]
            totals["effective_enabled_tpcs"] += item["effective_enabled_tpcs"]
            totals["additional_tpcs_from_effective_to_defect_target"] += item[
                "additional_tpcs_from_effective_to_defect_target"
            ]
            totals["current_reconfig_marked_tpcs"] += item[
                "current_reconfig_marked_tpcs"
            ]
            totals["current_unsuppressed_reconfig_tpcs"] += item[
                "current_unsuppressed_reconfig_tpcs"
            ]
            assessable = low["control"] == 0 and low["disable_extend"] == 0
            expected = (
                low["disable"]
                | (low["reconfig"] & ~low["reconfig_override"])
            ) & 0xFF
            item["base_formula"] = (
                "effective=disable|(reconfig&~reconfig_override) only when "
                "control=0 and disable_extend=0"
            )
            item["base_formula_evidence_class"] = "empirical_four_card_capture"
            item["base_formula_assessable"] = assessable
            item["base_formula_expected_status"] = "0x%02x" % expected
            item["base_formula_matches"] = (
                low["effective_status"] == expected if assessable else None
            )
        else:
            totals_complete = False
        per_gpc.append(item)
    normalized_compute = normalize_ga100_topology(stable_values)["compute"]
    normalized_layers = normalized_compute["layers"]
    normalized_core_complete = all(
        normalized_layers[name]["assessable"]
        for name in ("defect", "disable", "effective")
    )
    if normalized_core_complete:
        # Replace the legacy GPC+TPC-only arithmetic with the full
        # GPC/PES(3/3/2)/TPC closure whenever all PES witnesses are present.
        defect_layer = normalized_layers["defect"]
        disable_layer = normalized_layers["disable"]
        effective_layer = normalized_layers["effective"]
        totals.update({
            "defect_ceiling_tpcs": defect_layer["enabled_tpcs"],
            "disable_layer_enabled_tpcs": disable_layer["enabled_tpcs"],
            "effective_enabled_tpcs": effective_layer["enabled_tpcs"],
            "additional_tpcs_from_effective_to_defect_target": max(
                0, defect_layer["enabled_tpcs"] - effective_layer["enabled_tpcs"]
            ),
        })
        for gpc, item in enumerate(per_gpc):
            defect_enabled = 8 - _popcount(defect_layer["tpc_masks"][gpc])
            disable_enabled = 8 - _popcount(disable_layer["tpc_masks"][gpc])
            effective_enabled = 8 - _popcount(effective_layer["tpc_masks"][gpc])
            item.update({
                "defect_ceiling_enabled_tpcs": defect_enabled,
                "disable_layer_enabled_tpcs": disable_enabled,
                "effective_enabled_tpcs": effective_enabled,
                "additional_tpcs_from_effective_to_defect_target": max(
                    0, defect_enabled - effective_enabled
                ),
                "normalized_parent_child_closure_applied": True,
                "normalized_pes_defect_bits": "0x%x" % (
                    (defect_layer["pes_mask"] >> (3 * gpc)) & 0x7
                ),
                "normalized_pes_disable_bits": "0x%x" % (
                    (disable_layer["pes_mask"] >> (3 * gpc)) & 0x7
                ),
                "normalized_pes_effective_bits": "0x%x" % (
                    (effective_layer["pes_mask"] >> (3 * gpc)) & 0x7
                ),
            })
    if not totals_complete:
        totals = None
    return {
        "state": "complete" if not missing else "incomplete",
        "per_gpc": per_gpc,
        "totals": totals,
        "gpc_layers": {
            "registers": gpc_names,
            "raw_low_byte": {
                layer: "0x%02x" % value for layer, value in gpc_values.items()
            },
            "complete_for_capacity_gating": gpc_complete,
        },
        "normalized_layers": normalized_layers,
        "normalized_parent_child_closure_applied": normalized_core_complete,
        "architecture_capacity": {
            "gpc_count": GA100_GPC_COUNT,
            "tpcs_per_gpc": GA100_TPCS_PER_GPC,
            "sms_per_tpc": GA100_SMS_PER_TPC,
            "maximum_tpcs": GA100_GPC_COUNT * GA100_TPCS_PER_GPC,
            "maximum_sms": (
                GA100_GPC_COUNT * GA100_TPCS_PER_GPC * GA100_SMS_PER_TPC
            ),
            "evidence_class": "direct_ga100_architecture_constants",
        },
        "sm_count_if_two_sms_per_tpc": (
            {
                "defect_ceiling_sms": GA100_SMS_PER_TPC * totals["defect_ceiling_tpcs"],
                "disable_layer_sms": GA100_SMS_PER_TPC * totals["disable_layer_enabled_tpcs"],
                "effective_sms": GA100_SMS_PER_TPC * totals["effective_enabled_tpcs"],
                "additional_sms_from_effective_to_defect_target": (
                    GA100_SMS_PER_TPC
                    * totals["additional_tpcs_from_effective_to_defect_target"]
                ),
                "current_reconfig_marked_sms": (
                    GA100_SMS_PER_TPC * totals["current_reconfig_marked_tpcs"]
                ),
                "current_unsuppressed_reconfig_sms": (
                    GA100_SMS_PER_TPC
                    * totals["current_unsuppressed_reconfig_tpcs"]
                ),
            }
            if totals is not None else None
        ),
        "missing_or_unusable": sorted(set(missing)),
        "certainty": {
            "register_coordinates_percent": 99,
            "base_formula_percent": 95,
            "two_sms_per_tpc_percent": 100,
            "functional_health_from_absence_of_defect_bit_percent": 0,
        },
        "evidence_separation": {
            "authoritative_facts": [
                "GA100 has eight GPCs, eight TPCs per GPC and two SMs per TPC",
                "the reported register coordinates and low-byte field widths",
            ],
            "empirical_observations": [
                "the restricted base formula matches all assessable retained samples",
            ],
            "unresolved": [
                "formula semantics when control or disable_extend is nonzero",
                "functional health of a unit whose defect bit is clear",
                "the post-fuse software-visible count under each VBIOS/GSP/driver policy",
            ],
        },
        "scope": (
            "observation arithmetic only; the defect ceiling is not proof that every "
            "unmarked unit is healthy, routed, powered, clocked or safe to enable"
        ),
        "future_policy_warning": (
            "current RECONFIG and override values describe only this boot policy. "
            "An A100 VBIOS/GSP or patched driver can repopulate them differently, so "
            "the post-fuse software-visible SM count must be measured and may require "
            "a volatile EFI/driver floorsweep override."
        ),
    }


def analyze_ga100_topology(stable_values, gib_per_subpartition=None,
                           density_provenance=None):
    """Return legacy reports plus the authoritative normalized topology oracle."""
    memory = analyze_memory_layers(stable_values)
    compute = analyze_tpc_layers(stable_values)
    normalized = normalize_ga100_topology(
        stable_values,
        gib_per_subpartition=gib_per_subpartition,
        density_provenance=density_provenance,
    )
    return {
        "state": (
            normalized["state"]
        ),
        "memory": memory,
        "compute": compute,
        "normalized": normalized,
        "policy_separation": {
            "architecture_maximum": "layout ceiling, not a specimen unlock promise",
            "defect_preserving_target": "permanent disable masks reduced only to observed defect masks",
            "effective_runtime": "current GPC/TPC/FB status after firmware and overrides",
            "software_visible": (
                "must be measured after the selected identity/VBIOS/GSP/driver path; "
                "RECONFIG-class units may require a volatile override"
            ),
        },
    }


def capture_topology_density(card):
    """Qualify density only for the retained 20c2/1585 board population."""
    if card.get("device") == 0x20C2 and card.get("subsystem_device") == 0x1585:
        return 4, {
            "kind": "retained_cmp170hx_20c2_1585_population",
            "qualified": True,
            "gib_per_subpartition": 4,
            "basis": (
                "retained 64-GiB/eight-enabled-FBP population and package evidence"
            ),
            "does_not_generalize_to_20b5_by_identity": True,
        }
    return None, {
        "kind": "unqualified_identity",
        "qualified": False,
        "gib_per_subpartition": None,
        "reason": (
            "PCI identity alone does not establish HBM package density; report "
            "enabled units without GiB until package or telemetry evidence is bound"
        ),
    }


def goal_assessments(stable_values, physical_rows_available=False, rir_available=False):
    assessments = {}
    for goal_name, definition in sorted(GOALS.items()):
        observed = {}
        missing = []
        unusable = {}
        for register_name in definition["registers"]:
            if register_name in stable_values:
                reading = stable_values[register_name]
                if reading.get("status") == "ok":
                    observed[register_name] = reading
                else:
                    unusable[register_name] = reading
            else:
                missing.append(register_name)
        blockers = [
            "no validated universal burn recipe",
            "this audit tool deliberately emits no executable fuse records",
        ]
        if not physical_rows_available:
            blockers.append("certified locked-controller 512-row MAIN view unavailable")
        if not rir_available:
            blockers.append("RIR primary/redundant state unavailable")
        if missing or unusable:
            blockers.append("missing or incoherent live registers")
        assessments[goal_name] = {
            "status": "blocked",
            "executable": False,
            "strategy": definition["strategy"],
            "observed_registers": observed,
            "unusable_registers": unusable,
            "missing_registers": missing,
            "unknowns": list(definition["unknowns"]),
            "blockers": blockers,
            "candidate_records": None,
        }
    return assessments


def capture_card(card, backend, passes_count, register_profile="core"):
    started = utc_now()
    capture_registers = registers_for_profile(register_profile)
    passes = [
        capture_pass(backend, index + 1, capture_registers, register_profile)
        for index in range(passes_count)
    ]
    summary = summarize_passes(passes)
    stable = stable_register_values(passes)
    controller = {}
    if "FUSECTRL" in stable:
        controller["fusectrl"] = decode_fusectrl(int(stable["FUSECTRL"]["raw"], 0))
    if "CRC_STATUS" in stable:
        controller["crc_status"] = decode_crc_status(int(stable["CRC_STATUS"]["raw"], 0))
    if "IFF_RECORD" in stable:
        controller["iff_record"] = decode_iff_record_register(
            int(stable["IFF_RECORD"]["raw"], 0)
        )
    iff_execution = {}
    for register_name in (
        "PBUS_IFR_STATUS0", "PBUS_IFR_STATUS1", "PBUS_IFR_ERROR",
        "PBUS_IFR_INFO0",
    ):
        if register_name in stable:
            iff_execution[register_name] = stable[register_name]
    if iff_execution:
        controller["iff_execution"] = {
            "registers": iff_execution,
            "interpretation": (
                "post-reset execution/error observability; a clear error register "
                "does not prove that every intended IFF effect occurred"
            ),
        }
    controller["debugctrl"] = stable.get("DEBUGCTRL")
    sw_override_active = None
    if "EN_SW_OVERRIDE" in stable:
        sw_override_active = bool(int(stable["EN_SW_OVERRIDE"]["raw"], 0) & 1)
    if sw_override_active is True:
        option_interpretation = (
            "effective option/override-plane observation; not physical-row evidence"
        )
    elif sw_override_active is False:
        option_interpretation = (
            "option shadow observation; still not physical-row evidence"
        )
    else:
        option_interpretation = (
            "software-override state unavailable; option-plane interpretation is incomplete"
        )
    topology_density, topology_density_provenance = capture_topology_density(card)
    normalized_topology = normalize_ga100_topology(
        stable,
        gib_per_subpartition=topology_density,
        density_provenance=topology_density_provenance,
    )
    result = {
        "card": public_card_metadata(card),
        "capture": {
            "started_utc": started,
            "finished_utc": utc_now(),
            "backend": backend.name,
            "direct_user_mmio_writes": 0,
            "controller_read_commands": 0,
            "passes": passes_count,
            "register_profile": register_profile,
            "register_count_per_pass": len(capture_registers),
            "read_count": backend.read_count,
            "coherence": summary,
        },
        "raw_passes": passes,
        "registers": stable,
        "controller": controller,
        "plane_context": {
            "software_override_active": sw_override_active,
            "option_read_interpretation": option_interpretation,
            "control_shadow_role": "OPT_*_CP registers are retained separately",
        },
        "tpc_layer_analysis": analyze_tpc_layers(stable),
        "ga100_topology_analysis": normalized_topology,
        "topology_density_qualification": topology_density_provenance,
        "physical_rows": {
            "available": False,
            "reason": "requires FUSEADDR and FUSECTRL.CMD_READ MMIO writes",
        },
        "rir_rows": {
            "available": False,
            "reason": "requires DEBUGCTRL.RWL and fuse-controller MMIO writes",
        },
        "iff": {"state": "not_analyzable_without_physical_rows"},
        "goal_assessments": goal_assessments(stable),
    }
    result["unknown_register_census"] = unknown_register_census(
        stable, register_profile
    )
    return result


def metadata_only_card(card, warnings, attempted_reads=0, attempted_backend=None,
                       register_profile="core"):
    result = {
        "card": public_card_metadata(card),
        "capture": {
            "backend": (
                "metadata-only"
                if not attempted_reads else
                "metadata-fallback-after-%s" % (attempted_backend or "BAR-attempt")
            ),
            "direct_user_mmio_writes": 0,
            "controller_read_commands": 0,
            "passes": 0,
            "read_count": attempted_reads,
            "counts_complete": True,
            "coherence": {"coherent": None},
            "warnings": warnings,
        },
        "raw_passes": [],
        "registers": {},
        "controller": {},
        "physical_rows": {
            "available": False,
            "reason": "metadata-only fallback; controller writes are forbidden",
        },
        "rir_rows": {
            "available": False,
            "reason": "metadata-only fallback; DEBUGCTRL.RWL writes are forbidden",
        },
        "iff": {"state": "not_analyzable_without_physical_rows"},
        "goal_assessments": goal_assessments({}),
    }
    result["unknown_register_census"] = unknown_register_census(
        {}, register_profile
    )
    return result


def base_document(command):
    return {
        "schema": SCHEMA_VERSION,
        "command": command,
        "generated_utc": utc_now(),
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "sha256": tool_sha256(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "safety": {
            "access_ladder": list(ACCESS_LEVELS),
            "access_aliases": dict(ACCESS_ALIASES),
            "direct_user_mmio_writes": 0,
            "has_live_write_primitive": False,
            "otp_execution_supported": False,
            "volatile_write_execution_supported": False,
            "controller_read_commands": 0,
            "optional_driver_query_side_effects": "not provable from userspace; reported separately",
            "forbidden_programming_actions": [
                "FUSEWDATA write", "FUSECTRL.CMD_WRITE", "fuse timing write",
                "program-source/GPIO assertion", "software-IFF load",
                "re-sense", "GPU reset", "OTP retry/resume",
            ],
            "mode_gated_semantic_read_writes": [
                "FUSEADDR", "FUSECTRL.CMD_READ", "DEBUGCTRL.RWL"
            ],
        },
        "direct_source_anchors": dict(DIRECT_SOURCE_ANCHORS),
    }


def _helper_dump_available(dump):
    return bool(
        dump.get("protocol_valid") is True
        and dump.get("status") == 0
        and dump.get("completed_passes") == XRAY_MAX_PASSES
        and dump.get("coherent") is True
        and dump.get("certified_by_helper") is True
    )


def _helper_software_iff_provenance(dump):
    """Classify SW-IFF redirection without conflating it with physical OTP.

    IFF_RECORD is nonzero on the observed GA100s while both software controls
    are inactive, and its 488..507 value matches the locked controller-IFF view
    in the retained image.  A nonzero value is therefore not evidence that
    volatile SW-IFF redirection is active, nor is it sole boundary authority.
    Redirection is excluded only when override/fusing are inactive at both ends
    of a successful transaction and IFF_RECORD remains stable.
    """
    entry = dump.get("entry", {})
    exit_snapshot = dump.get("exit", {})
    try:
        entry_override = parse_int(entry.get("en_sw_override"), "entry EN_SW_OVERRIDE")
        exit_override = parse_int(
            exit_snapshot.get("en_sw_override"), "exit EN_SW_OVERRIDE"
        )
        entry_fusing = parse_int(entry.get("iff_sw_fusing"), "entry IFF_SW_FUSING")
        exit_fusing = parse_int(
            exit_snapshot.get("iff_sw_fusing"), "exit IFF_SW_FUSING"
        )
        entry_record = parse_int(entry.get("iff_record"), "entry IFF_RECORD")
        exit_record = parse_int(exit_snapshot.get("iff_record"), "exit IFF_RECORD")
    except AuditError:
        return {
            "software_iff_redirection_excluded": False,
            "reason": "required entry/exit helper snapshots are absent or malformed",
            "controller_iff_interval_observation": None,
            "physical_iff_interval_observation": None,
        }
    record_stable = entry_record == exit_record
    override_inactive = not bool(entry_override & 1) and not bool(exit_override & 1)
    sw_fusing_inactive = not bool(entry_fusing & 1) and not bool(exit_fusing & 1)
    excluded = bool(
        dump.get("protocol_valid") is True
        and dump.get("status") == 0
        and record_stable
        and override_inactive
        and sw_fusing_inactive
    )
    start = entry_record & 0x1FF
    last = (entry_record >> 16) & 0x1FF
    interval = {
        "raw": hex32(entry_record),
        "start": start,
        "last": last,
        "stable_across_transaction": record_stable,
        "well_ordered": start <= last,
        "interpretation": (
            "observed live IFF_RECORD interval; when software controls are inactive it "
            "qualifies the locked controller-IFF view, but it is neither immutable-OTP "
            "proof, sole boundary authority, nor SW-redirection evidence by itself"
        ),
    }
    return {
        "software_iff_redirection_excluded": excluded,
        "override_bit_inactive_at_entry_and_exit": override_inactive,
        "software_fusing_bit_inactive_at_entry_and_exit": sw_fusing_inactive,
        "iff_record_stable": record_stable,
        "entry_en_sw_override": hex32(entry_override),
        "exit_en_sw_override": hex32(exit_override),
        "entry_iff_sw_fusing": hex32(entry_fusing),
        "exit_iff_sw_fusing": hex32(exit_fusing),
        "controller_iff_interval_observation": interval,
        # Compatibility key retained for ABI-5 artifacts created before the
        # DSOV/EN_SW cross-plane corpus proved that this interval is a
        # state-qualified controller view rather than immutable OTP readback.
        "physical_iff_interval_observation": interval,
        "reason": (
            "successful stable helper transaction with EN_SW_OVERRIDE[0]=0 and "
            "IFF_SW_FUSING[0]=0 at entry and exit"
            if excluded else
            "one or more status, stability, override, or software-fusing predicates failed"
        ),
    }


def attach_serialized_helper_dumps(captured, helper, card, args):
    bdf = card["domain_bdf"]
    main_dump = helper.dump(
        bdf, XRAY_DUMP_MAIN, allow_bound=args.allow_bound_controller
    )
    sw_iff_provenance = _helper_software_iff_provenance(main_dump)
    main_available = bool(
        _helper_dump_available(main_dump)
        and sw_iff_provenance["software_iff_redirection_excluded"]
    )
    # ``physical_rows`` is a compatibility object name used by ABI-5 captures.
    # The controller path is physical for conventional/FF storage, while the
    # active IFF interval is a state-qualified controller plane.
    captured["physical_rows"] = {
        "available": main_available,
        "view": "locked_controller_main" if main_available else "uncertified_or_failed",
        "legacy_object_name": "physical_rows",
        "view_semantics": (
            "conventional and FF rows use the physical controller path; the active "
            "IFF interval is certified only as a locked, software-redirection-excluded "
            "controller view and is not immutable-OTP evidence across unlike planes"
        ),
        "collector": "170hx_xray serialized kernel helper ABI %d" % XRAY_HELPER_ABI,
        "rows": main_dump["passes"][0] if main_dump["passes"] else None,
        "passes": main_dump["passes"],
        "pass_hashes": main_dump["pass_hashes"],
        "coherent": main_dump["coherent"],
        "transaction": main_dump,
        "certification": {
            "available": main_available,
            "two_passes_in_one_helper_serialized_ioctl_transaction": True,
            "hardware_atomic_snapshot": False,
            "full_domain_identity_bound": True,
            "driver_unbound": not main_dump["driver_bound_at_entry"],
            "software_iff_redirection_excluded": sw_iff_provenance[
                "software_iff_redirection_excluded"
            ],
            "software_iff_provenance": sw_iff_provenance,
        },
        "physical_iff_interval_observation": sw_iff_provenance[
            "physical_iff_interval_observation"
        ],
        "controller_iff_interval_observation": sw_iff_provenance[
            "controller_iff_interval_observation"
        ],
    }

    # MAIN is the first transaction and the safety gate for the second.  A
    # failed, malformed, redirected, bound, incoherent, or un-restored MAIN
    # response ends the controller sequence; never compound it with RIR writes.
    if not main_available:
        rir_dump = {
            "kind": "rir",
            "performed": False,
            "status": None,
            "status_text": "not performed after uncertified MAIN transaction",
            "protocol_valid": None,
            "operational_writes_issued": 0,
            "cmd_read_commands_issued": 0,
            "completed_data_reads_lower_bound": 0,
            "counts_complete": True,
            "passes": [],
            "pass_hashes": [],
            "coherent": False,
            "address_restored": False,
            "debug_restored": False,
            "driver_bound_at_entry": None,
            "certified_by_helper": False,
            "otp_commands": 0,
        }
    else:
        try:
            rir_dump = helper.dump(
                bdf, XRAY_DUMP_RIR, allow_bound=args.allow_bound_controller
            )
        except (OSError, AuditError, ValueError) as exc:
            rir_dump = {
                "kind": "rir",
                "performed": True,
                "status": None,
                "status_text": "ioctl/protocol failure: %s: %s"
                % (type(exc).__name__, exc),
                "protocol_valid": False,
                "operational_writes_issued": None,
                "cmd_read_commands_issued": None,
                "completed_data_reads_lower_bound": 0,
                "counts_complete": False,
                "passes": [],
                "pass_hashes": [],
                "coherent": False,
                "address_restored": False,
                "debug_restored": False,
                "driver_bound_at_entry": None,
                "certified_by_helper": False,
                "otp_commands": 0,
            }
    rir_available = _helper_dump_available(rir_dump)
    captured["rir_rows"] = {
        "available": rir_available,
        "collector": "170hx_xray serialized kernel helper ABI %d" % XRAY_HELPER_ABI,
        "rows": rir_dump["passes"][0] if rir_dump["passes"] else None,
        "passes": rir_dump["passes"],
        "pass_hashes": rir_dump["pass_hashes"],
        "coherent": rir_dump["coherent"],
        "transaction": rir_dump,
        "certification": {
            "available": rir_available,
            "two_passes_in_one_helper_serialized_ioctl_transaction": True,
            "hardware_atomic_snapshot": False,
            "full_domain_identity_bound": True,
            "driver_unbound": (
                None if rir_dump["driver_bound_at_entry"] is None
                else not rir_dump["driver_bound_at_entry"]
            ),
            "debug_rwl_restored": rir_dump["debug_restored"],
        },
    }
    if main_dump["passes"] and args.fuseless_start is not None:
        main_rows = [int(word, 0) for word in main_dump["passes"][0]]
        analysis = decode_ff_iff(
            main_rows, args.fuseless_start, args.fuseless_end
        )
        analysis["bounds_provenance"] = args.bounds_provenance
        analysis["logical_field_catalog_decode"] = decode_logical_field_catalog(
            analysis, main_rows
        )
        captured["analysis"] = analysis
        if rir_dump["passes"]:
            rir_words = [int(word, 0) for word in rir_dump["passes"][0]]
            rir_analysis = decode_ga100_rir_words(rir_words)
            captured["rir_analysis"] = rir_analysis
            rir_view, effective_rows = apply_unique_rir_view(
                main_rows, rir_analysis, args.fuseless_start, args.fuseless_end
            )
            rir_view["capture_certified"] = rir_available and main_available
            if effective_rows is not None:
                rir_view["logical_field_catalog_decode"] = (
                    decode_logical_field_catalog(analysis, effective_rows)
                )
            captured["conditional_rir_effective_view"] = rir_view
    for assessment in captured.get("goal_assessments", {}).values():
        assessment["certified_physical_rows_available"] = main_available
        assessment["certified_rir_available"] = rir_available
    return {
        "main": main_dump,
        "rir": rir_dump,
        "direct_writes": (
            None if (
                not main_dump.get("counts_complete")
                or not rir_dump.get("counts_complete")
                or rir_dump["operational_writes_issued"] is None
            ) else
            main_dump["operational_writes_issued"]
            + rir_dump["operational_writes_issued"]
        ),
        "known_minimum_direct_writes": (
            main_dump["operational_writes_issued"]
            if main_dump.get("counts_complete") else 0
        ) + (
            (rir_dump["operational_writes_issued"] or 0)
            if rir_dump.get("counts_complete") else 0
        ),
        "controller_commands": (
            None if (
                not main_dump.get("counts_complete")
                or not rir_dump.get("counts_complete")
                or rir_dump["cmd_read_commands_issued"] is None
            ) else
            main_dump["cmd_read_commands_issued"]
            + rir_dump["cmd_read_commands_issued"]
        ),
        "known_minimum_controller_commands": (
            main_dump["cmd_read_commands_issued"]
            if main_dump.get("counts_complete") else 0
        ) + (
            (rir_dump["cmd_read_commands_issued"] or 0)
            if rir_dump.get("counts_complete") else 0
        ),
        "completed_data_reads_lower_bound": (
            main_dump.get("completed_data_reads_lower_bound", 0)
            + rir_dump.get("completed_data_reads_lower_bound", 0)
        ),
        "counts_complete": bool(
            main_dump.get("counts_complete")
            and rir_dump.get("counts_complete")
        ),
        "performed": True,
        "all_certified": main_available and rir_available,
    }


def command_live(args):
    if not 2 <= args.passes <= 5:
        raise AuditError("--passes must be between 2 and 5")
    if not math.isfinite(args.query_timeout) or args.query_timeout <= 0:
        raise AuditError("--query-timeout must be positive and finite")
    selected_registers = registers_for_profile(args.register_profile)
    if (args.fuseless_start is None) != (args.fuseless_end is None):
        raise AuditError("--fuseless-start and --fuseless-end must be supplied together")
    if args.fuseless_start is not None and not (
        0 <= args.fuseless_start <= args.fuseless_end < MAX_FUSE_ROWS
    ):
        raise AuditError("invalid explicit fuseless bounds")
    invoked_source_sha256 = args.invoked_source_sha256
    if invoked_source_sha256 is not None:
        invoked_source_sha256 = invoked_source_sha256.lower()
        if re.fullmatch(r"[0-9a-f]{64}", invoked_source_sha256) is None:
            raise AuditError("--invoked-source-sha256 must be 64 hex characters")
    requested_access = args.access
    canonical_requested_access = canonical_access_mode(requested_access)
    glitch_requested = canonical_requested_access == "glitch-read"
    if glitch_requested and not args.no_driver_queries:
        raise AuditError(
            "glitch-read requires --no-driver-queries so XRAY can certify zero "
            "operational device-MMIO writes"
        )
    default_scope = (
        GA100_READ_TARGET_DEVICE_IDS
        if args.all_targets or (args.bdf and not args.device)
        else DEFAULT_DEVICE_IDS
    )
    device_values = args.device or ["0x%04x" % value for value in default_scope]
    allowed_devices = set(parse_int(value, "PCI device ID") for value in device_values)
    unsupported_devices = allowed_devices.difference(GA100_READ_TARGET_DEVICE_IDS)
    if unsupported_devices:
        raise AuditError(
            "device selection contains a non-GA100 read target: %s"
            % ", ".join("0x%04x" % value for value in sorted(unsupported_devices))
        )
    all_cards = discover_pci_cards(args.sysfs_root)
    targets = [card for card in all_cards if card.get("device") in allowed_devices]
    selected_bdfs = set(item.lower() for item in args.bdf)
    if selected_bdfs:
        unknown = selected_bdfs.difference(card["domain_bdf"] for card in targets)
        if unknown:
            raise AuditError(
                "selected BDF is absent or not an allowed NVIDIA device: %s"
                % ", ".join(sorted(unknown))
            )
        targets = [card for card in targets if card["domain_bdf"] in selected_bdfs]

    fallback_reasons = []
    bar_read_requested = access_at_least(canonical_requested_access, "bar-read")
    controller_snapshot_requested = access_at_least(
        canonical_requested_access, "semantic-read"
    )
    plan_level_requested = access_at_least(
        canonical_requested_access, "plan"
    )
    write_level_requested = access_at_least(
        canonical_requested_access, "volatile-write"
    )
    helper_present = os.path.exists(args.controller_helper)
    xray_helper = None
    helper_open_error = None
    helper_backend_selected = args.backend in ("auto", "xray-helper")
    if (
        helper_present
        and bar_read_requested
        and canonical_requested_access != "passive"
        and helper_backend_selected
        and not args.owner_busy
    ):
        try:
            xray_helper = XrayHelperDevice(args.controller_helper)
        except (OSError, AuditError, ValueError) as exc:
            helper_open_error = "%s: %s" % (type(exc).__name__, exc)
    if plan_level_requested:
        if canonical_requested_access == "plan":
            fallback_reasons.append(
                "plan is an offline evidence/conversion level; capture performs observation only"
            )
        else:
            fallback_reasons.append(
                "%s is a named policy level only; capture has no volatile or OTP write primitive"
                % canonical_requested_access
            )
    if args.owner_busy and controller_snapshot_requested:
        fallback_reasons.append("owner declared active GPU workloads")
    if controller_snapshot_requested and not helper_backend_selected:
        fallback_reasons.append(
            "backend %s cannot perform complete helper-serialized MAIN/RIR controller transactions"
            % args.backend
        )
    bound_targets = [
        card["domain_bdf"] for card in targets if card.get("driver") is not None
    ]
    a100_controller_targets = [
        card["domain_bdf"] for card in targets if card.get("device") == 0x20B5
    ]
    a100_controller_blocked = bool(
        a100_controller_targets
        and controller_snapshot_requested
        and (
            xray_helper is None
            or not xray_helper.capabilities & XRAY_CAP_A100_CONTROLLER
        )
    )
    if a100_controller_blocked and xray_helper is not None:
        fallback_reasons.append(
            "20b5 MAIN/RIR requires helper load-time allow_a100_controller=1; "
            "falling back before issuing a controller command"
        )
    if (
        bound_targets and controller_snapshot_requested
        and not args.allow_bound_controller
    ):
        fallback_reasons.append(
            "GPU driver owns target(s): %s; no RM-shared fuse-controller lock"
            % ", ".join(bound_targets)
        )
    if (
        bound_targets and controller_snapshot_requested
        and args.allow_bound_controller
        and (
            xray_helper is None
            or not xray_helper.capabilities & XRAY_CAP_BOUND_CONTROLLER
        )
    ):
        fallback_reasons.append(
            "bound-controller override requested but helper/module does not advertise it"
        )
    if controller_snapshot_requested and helper_backend_selected and xray_helper is None:
        fallback_reasons.append(
            "atomic controller helper unavailable: %s%s"
            % (
                args.controller_helper,
                " (%s)" % helper_open_error if helper_open_error else "",
            )
        )
    if canonical_requested_access == "passive" or args.backend == "metadata":
        effective_access = "passive"
        if canonical_requested_access != "passive" and args.backend == "metadata":
            fallback_reasons.append("metadata backend forbids direct BAR/controller access")
    elif canonical_requested_access in ("bar-read", "glitch-read"):
        effective_access = (
            "passive"
            if args.backend == "xray-helper" and xray_helper is None
            else canonical_requested_access
        )
    else:
        # A capture request at plan or either write level may still perform the
        # lower, observation-only semantic-read transaction. Environmental controller
        # blockers reduce that to bar-read; an explicitly busy owner reduces it
        # to passive.  The effective level never claims a write capability.
        effective_access = "semantic-read"
        controller_blocked = a100_controller_blocked or not helper_backend_selected or bool(
            bound_targets and not args.allow_bound_controller
        ) or xray_helper is None or bool(
            bound_targets
            and args.allow_bound_controller
            and (
                xray_helper is None
                or not xray_helper.capabilities & XRAY_CAP_BOUND_CONTROLLER
            )
        )
        if args.owner_busy:
            effective_access = "passive"
        elif controller_blocked:
            effective_access = "bar-read"
        if (
            effective_access == "semantic-read"
            and len(targets) != 1
            and not args.all_targets
        ):
            effective_access = "bar-read"
            fallback_reasons.append(
                "controller mode requires one selected target; use --all-targets "
                "to acknowledge a sequential multi-card attempt"
            )

    query_nvml = (not args.no_driver_queries) or args.nvml
    query_smi = (not args.no_driver_queries) or args.smi_features

    document = base_document(args.command)
    if invoked_source_sha256 is not None:
        on_disk_sha256 = document["tool"].get("sha256")
        if on_disk_sha256 is not None and on_disk_sha256 != invoked_source_sha256:
            raise AuditError(
                "--invoked-source-sha256 does not match the executable source file"
            )
        document["tool"]["caller_asserted_source_sha256"] = invoked_source_sha256
        document["tool"]["source_hash_provenance"] = (
            "caller supplied; checked against __file__ when __file__ was readable"
        )
    document["access"] = {
        "requested_access": requested_access,
        "canonical_requested_access": canonical_requested_access,
        "effective_access": effective_access,
        "legacy_alias_used": requested_access in ACCESS_ALIASES,
        "fallback_reasons": fallback_reasons,
        "owner_busy_declared": bool(args.owner_busy),
        "driver_query_class": (
            "query-only RM/NVML telemetry; internal MMIO/wake effects not provable"
            if (query_nvml or query_smi) else "disabled"
        ),
        "bar_read_requested": bar_read_requested,
        "controller_snapshot_requested": controller_snapshot_requested,
        "plan_level_requested": plan_level_requested,
        "write_level_requested": write_level_requested,
        "controller_snapshot_attempted": False,
        "controller_snapshot_performed": False,
        "controller_helper": {
            "path": args.controller_helper,
            "present": helper_present,
            "enabled": xray_helper is not None,
            "info": xray_helper.info_document() if xray_helper is not None else None,
            "open_error": helper_open_error,
        },
        "direct_user_mmio_reads": 0,
        "direct_user_mmio_writes": 0,
        "controller_read_commands": 0,
        "controller_read_commands_issued": 0,
        "pci_config_writes": 0,
        "runtime_pm_writes": 0,
        "otp_program_commands": 0,
    }
    document["live_policy"] = {
        "tool_name": TOOL_NAME,
        "backend_requested": args.backend,
        "sysfs_root": args.sysfs_root,
        "gpcprobe_path": args.gpcprobe,
        "allowed_device_ids": ["0x%04x" % value for value in sorted(allowed_devices)],
        "catalogued_register_count": len(CURATED_REGISTERS),
        "kernel_safe_read_register_count": len(SAFE_READ_REGISTERS),
        "selected_register_profile": args.register_profile,
        "selected_register_count": len(selected_registers),
        "allowlisted_register_count": len(SAFE_READ_REGISTERS),
        "excluded_stateful_registers": [
            item["name"] for item in CURATED_REGISTERS
            if not item["capture_safe"]
        ],
        "arbitrary_bar_scan": False,
        "optional_driver_queries": {
            "nvml": bool(query_nvml),
            "nvidia_smi_features": bool(query_smi),
            "query_only": True,
            "caveat": "Opt-in driver ioctls may wake/query the device but launch no GPU workload and expose no setter.",
        },
    }
    document["safety"]["driver_queries_requested"] = bool(
        query_nvml or query_smi
    )
    document["safety"]["driver_query_side_effects"] = (
        "not_provable" if (query_nvml or query_smi) else "not_applicable"
    )
    document["cards"] = []
    document["warnings"] = []
    if helper_open_error:
        document["warnings"].append(
            "170hx_xray helper unavailable: %s" % helper_open_error
        )
    if not targets:
        document["status"] = "no_matching_devices"
        if xray_helper is not None:
            xray_helper.close()
        emit_json(document, args.output)
        return 0

    allowed_offsets = SAFE_READ_OFFSETS
    bar_read_enabled = access_at_least(effective_access, "bar-read")
    probe = None
    nvml = None
    if bar_read_enabled and args.backend in ("auto", "gpcprobe"):
        try:
            probe = GpcProbeDevice(args.gpcprobe)
        except (OSError, AuditError) as exc:
            document["warnings"].append(
                "existing gpcprobe unavailable: %s: %s" % (type(exc).__name__, exc)
            )

    if query_nvml:
        try:
            nvml = NvmlReadonlyQueries(args.nvml_library)
        except (OSError, AuditError, AttributeError) as exc:
            document["warnings"].append(
                "optional NVML query interface unavailable: %s: %s"
                % (type(exc).__name__, exc)
            )

    try:
        resolved_bdfs = []
        controller_sequence_stopped = False
        for card in targets:
            card_warnings = []
            captured = None
            helper_transaction = None
            helper_transaction_attempted = False
            helper_transaction_error = None
            partial_bar_reads = 0
            partial_bar_backends = []
            helper_identity = None
            backend = None
            passive_bar_context = bool(
                card.get("runtime_status") in ("active", "unsupported")
                and card.get("pci_power_state") == "D0"
            )
            if not passive_bar_context:
                card_warnings.append(
                    "runtime-PM/PCI power state is %r/%r; all BAR reads skipped"
                    % (card.get("runtime_status"), card.get("pci_power_state"))
                )
            if (
                bar_read_enabled
                and
                xray_helper is not None
                and args.backend in ("auto", "xray-helper")
                and passive_bar_context
            ):
                try:
                    helper_identity = xray_helper.validate_sysfs_identity(card)
                    backend = BoundXrayHelperBackend(
                        xray_helper, card["domain_bdf"], allowed_offsets
                    )
                    captured = capture_card(
                        card, backend, args.passes, args.register_profile
                    )
                    captured["card"]["xray_helper_identity"] = (
                        _xray_identity_document(helper_identity)
                    )
                except (OSError, AuditError, ValueError) as exc:
                    if isinstance(backend, BoundXrayHelperBackend):
                        partial_bar_reads += backend.read_count
                        partial_bar_backends.append(backend.name)
                    card_warnings.append(
                        "170hx_xray allowlisted capture unavailable: %s: %s"
                        % (type(exc).__name__, exc)
                    )
            if (
                bar_read_enabled and passive_bar_context and probe is not None
                and args.backend in ("auto", "gpcprobe")
                and captured is None
            ):
                try:
                    index = correlate_gpcprobe_index(probe, card, all_cards)
                    backend = BoundGpcProbeBackend(probe, index, allowed_offsets)
                    captured = capture_card(
                        card, backend, args.passes, args.register_profile
                    )
                    captured["card"]["gpcprobe_index"] = index
                except (OSError, AuditError) as exc:
                    if isinstance(backend, BoundGpcProbeBackend):
                        partial_bar_reads += backend.read_count
                        partial_bar_backends.append(backend.name)
                    card_warnings.append(
                        "gpcprobe capture unavailable: %s: %s" % (type(exc).__name__, exc)
                    )
            if (
                bar_read_enabled and passive_bar_context and captured is None
                and args.backend in ("auto", "sysfs")
            ):
                backend = None
                try:
                    backend = SysfsResourceBackend(card, allowed_offsets)
                    captured = capture_card(
                        card, backend, args.passes, args.register_profile
                    )
                except (OSError, AuditError, ValueError) as exc:
                    if backend is not None:
                        partial_bar_reads += getattr(backend, "read_count", 0)
                        partial_bar_backends.append(
                            getattr(backend, "name", "sysfs-resource0")
                        )
                    card_warnings.append(
                        "read-only sysfs BAR capture unavailable: %s: %s"
                        % (type(exc).__name__, exc)
                    )
                finally:
                    if backend is not None:
                        backend.close()
            if captured is None:
                captured = metadata_only_card(
                    card, card_warnings, partial_bar_reads,
                    ",".join(partial_bar_backends) or None,
                    args.register_profile,
                )
            else:
                if partial_bar_reads:
                    captured["capture"]["prior_failed_backend_reads"] = partial_bar_reads
                    captured["capture"]["prior_failed_backends"] = partial_bar_backends
                    captured["capture"]["read_count"] += partial_bar_reads
                if card_warnings:
                    captured["capture"].setdefault("warnings", []).extend(card_warnings)
            helper_bar_certified = bool(
                captured.get("capture", {}).get("backend")
                == "170hx-xray-helper-allowlisted-read32"
                and captured.get("capture", {}).get("coherence", {}).get("coherent")
                and helper_identity is not None
            )
            if (
                effective_access == "semantic-read"
                and xray_helper is not None
                and passive_bar_context
                and helper_bar_certified
            ):
                helper_transaction_attempted = True
                try:
                    helper_transaction = attach_serialized_helper_dumps(
                        captured, xray_helper, card, args
                    )
                except (OSError, AuditError, ValueError) as exc:
                    helper_transaction_error = "%s: %s" % (type(exc).__name__, exc)
                    captured["capture"].setdefault("warnings", []).append(
                        "helper-serialized MAIN/RIR transaction failed before a complete journal: %s: %s"
                        % (type(exc).__name__, exc)
                    )
            driver_queries = {}
            if nvml is not None and passive_bar_context:
                try:
                    driver_queries["nvml"] = nvml.query_card(card["domain_bdf"])
                except (OSError, AuditError, ValueError) as exc:
                    driver_queries["nvml"] = {
                        "status": "error",
                        "query_only": True,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                    }
            if query_smi and passive_bar_context:
                driver_queries["nvidia_smi"] = passive_smi_queries(
                    args.nvidia_smi, card["domain_bdf"], args.query_timeout
                )
            if driver_queries:
                captured["passive_driver_queries"] = driver_queries
                captured["capture"]["driver_query_side_effects"] = "not_provable"
                nvml_result = driver_queries.get("nvml", {})
                captured["goal_assessments"]["more_sms"][
                    "passive_driver_observations"
                ] = {"gpu_cores": nvml_result.get("gpu_cores")}
                captured["goal_assessments"]["ecc"][
                    "passive_driver_observations"
                ] = {
                    "ecc_mode": nvml_result.get("ecc_mode"),
                    "nvidia_smi_features": driver_queries.get(
                        "nvidia_smi", {}
                    ).get("features", {}).get("parsed"),
                }
                captured["goal_assessments"]["mig"][
                    "passive_driver_observations"
                ] = {
                    "mig_mode": nvml_result.get("mig_mode"),
                    "profile_listing": driver_queries.get("nvidia_smi", {}).get("mig_profiles"),
                }
                captured["goal_assessments"]["nvlink"][
                    "passive_driver_observations"
                ] = {
                    "nvml_state": nvml_result.get("nvlink_state"),
                    "smi_status": driver_queries.get("nvidia_smi", {}).get("nvlink_status"),
                }
                smi_result = driver_queries.get("nvidia_smi", {})
                captured["goal_assessments"]["pcie_gen4_x16"][
                    "passive_driver_observations"
                ] = {"smi_pcie": smi_result.get("pcie")}
                captured["goal_assessments"]["oc_frequencies"][
                    "passive_driver_observations"
                ] = {"smi_clocks_power": smi_result.get("clocks_power")}
                captured["goal_assessments"]["a100_identity_vbios"][
                    "passive_driver_observations"
                ] = {"smi_identity": smi_result.get("features")}
            if helper_transaction_attempted:
                card_effective_access = "semantic-read"
            elif captured["capture"].get("read_count", 0) > 0:
                card_effective_access = (
                    "glitch-read" if glitch_requested else "bar-read"
                )
            else:
                card_effective_access = "passive"
            writes_value = (
                helper_transaction["direct_writes"]
                if helper_transaction is not None else
                (None if helper_transaction_attempted else 0)
            )
            commands_value = (
                helper_transaction["controller_commands"]
                if helper_transaction is not None else
                (None if helper_transaction_attempted else 0)
            )
            captured["access"] = {
                "requested_access": requested_access,
                "canonical_requested_access": canonical_requested_access,
                "effective_access": card_effective_access,
                "fallback_reasons": list(fallback_reasons),
                "direct_user_mmio_reads": captured["capture"].get("read_count", 0),
                "direct_user_mmio_writes": writes_value,
                "helper_operational_mmio_writes": writes_value,
                "known_minimum_helper_writes": (
                    helper_transaction["known_minimum_direct_writes"]
                    if helper_transaction is not None else 0
                ),
                "controller_read_commands": commands_value,
                "controller_read_commands_issued": commands_value,
                "controller_read_commands_semantics": (
                    "CMD_READ writes issued, including a command that later times out; "
                    "controller_read_commands is a compatibility alias"
                ),
                "known_minimum_controller_commands": (
                    helper_transaction["known_minimum_controller_commands"]
                    if helper_transaction is not None else 0
                ),
                "completed_data_reads_lower_bound": (
                    helper_transaction["completed_data_reads_lower_bound"]
                    if helper_transaction is not None else 0
                ),
                "counts_complete": bool(
                    helper_transaction.get("counts_complete")
                    if helper_transaction is not None else
                    not helper_transaction_attempted
                ),
                "controller_transaction_attempted": helper_transaction_attempted,
                "controller_transaction_error": helper_transaction_error,
                "physical_ff_transaction": (
                    helper_transaction["main"]
                    if helper_transaction is not None else "not_performed"
                ),
                "rir_transaction": (
                    helper_transaction["rir"]
                    if helper_transaction is not None else "not_performed"
                ),
            }
            if glitch_requested:
                captured["state_qualification"] = (
                    qualify_zero_write_glitch_observation(captured)
                )
            document["cards"].append(captured)
            resolved_bdfs.append(card["domain_bdf"])
            if (
                helper_transaction_attempted
                and (
                    helper_transaction is None
                    or not helper_transaction.get("all_certified")
                )
            ):
                document["warnings"].append(
                    "multi-card controller sequence stopped after the first uncertified transaction"
                )
                controller_sequence_stopped = True
                break
    finally:
        if nvml is not None:
            nvml.close()
        if probe is not None:
            probe.close()
        if xray_helper is not None:
            xray_helper.close()
    document["access"]["direct_user_mmio_reads"] = sum(
        card.get("access", {}).get("direct_user_mmio_reads", 0)
        for card in document["cards"]
    )
    write_counts = [
        card.get("access", {}).get("direct_user_mmio_writes")
        for card in document["cards"]
    ]
    command_counts = [
        card.get("access", {}).get("controller_read_commands")
        for card in document["cards"]
    ]
    document["access"]["direct_user_mmio_writes"] = (
        None if any(value is None for value in write_counts) else sum(write_counts)
    )
    document["access"]["helper_operational_mmio_writes"] = document["access"][
        "direct_user_mmio_writes"
    ]
    document["access"]["known_minimum_helper_writes"] = sum(
        card.get("access", {}).get("known_minimum_helper_writes", 0)
        for card in document["cards"]
    )
    document["access"]["controller_read_commands"] = (
        None if any(value is None for value in command_counts) else sum(command_counts)
    )
    document["access"]["controller_read_commands_issued"] = document["access"][
        "controller_read_commands"
    ]
    document["access"]["controller_read_commands_semantics"] = (
        "CMD_READ writes issued, including a command that later times out; "
        "controller_read_commands is a compatibility alias"
    )
    document["access"]["known_minimum_controller_commands"] = sum(
        card.get("access", {}).get("known_minimum_controller_commands", 0)
        for card in document["cards"]
    )
    document["access"]["completed_data_reads_lower_bound"] = sum(
        card.get("access", {}).get("completed_data_reads_lower_bound", 0)
        for card in document["cards"]
    )
    document["access"]["counts_complete"] = all(
        card.get("access", {}).get("counts_complete", False)
        for card in document["cards"]
    )
    document["access"]["controller_snapshot_attempted"] = any(
        card.get("access", {}).get("controller_transaction_attempted", False)
        for card in document["cards"]
    )
    document["access"]["controller_snapshot_performed"] = any(
        card.get("access", {}).get("known_minimum_controller_commands", 0) > 0
        or card.get("access", {}).get("known_minimum_helper_writes", 0) > 0
        for card in document["cards"]
    )
    card_levels = sorted(set(
        card.get("access", {}).get("effective_access", "passive")
        for card in document["cards"]
    ))
    document["access"]["effective_access"] = (
        card_levels[0] if len(card_levels) == 1 else "mixed"
    )
    document["safety"]["direct_user_mmio_writes"] = document["access"][
        "direct_user_mmio_writes"
    ]
    document["safety"]["controller_read_commands"] = document["access"][
        "controller_read_commands"
    ]
    document["safety"]["controller_read_commands_issued"] = document["access"][
        "controller_read_commands_issued"
    ]
    document["safety"]["counts_complete"] = document["access"]["counts_complete"]
    requested_bdfs = [card["domain_bdf"] for card in targets]
    document["target_scope"] = {
        "requested_bdfs": requested_bdfs,
        "resolved_bdfs": resolved_bdfs,
        "skipped_bdfs": [bdf for bdf in requested_bdfs if bdf not in resolved_bdfs],
        "all_targets_acknowledged": bool(args.all_targets),
        "controller_sequence_stopped": controller_sequence_stopped,
    }
    glitch_complete = bool(
        glitch_requested
        and document["cards"]
        and all(
            card.get("state_qualification", {}).get("qualified") is True
            for card in document["cards"]
        )
        and document["access"].get("direct_user_mmio_writes") == 0
        and document["access"].get("controller_read_commands_issued") == 0
    )
    if glitch_requested:
        document["glitch_read_contract"] = {
            "zero_operational_device_mmio_writes_required": True,
            "controller_transactions_forbidden": True,
            "all_cards_qualified": glitch_complete,
            "otp_eligibility_claimed": False,
        }
    if controller_sequence_stopped:
        document["status"] = "partial_stopped_after_uncertified_controller_transaction"
    elif glitch_requested and document["cards"] and not glitch_complete:
        document["status"] = "glitch_phenotype_not_qualified"
    else:
        document["status"] = "captured" if document["cards"] else "no_matching_devices"
    emit_json(document, args.output)
    return 2 if glitch_requested and document["cards"] and not glitch_complete else 0


def parse_header_text(text, source="<memory>", sha256=None):
    definitions = []
    for line_number, original in enumerate(text.splitlines(), 1):
        if "#define" not in original:
            continue
        access = ""
        body = original
        if "/*" in original and "*/" in original:
            body, comment = original.split("/*", 1)
            access = comment.split("*/", 1)[0].strip()
        parts = body.strip().split(None, 2)
        if len(parts) < 3 or parts[0] != "#define":
            continue
        name = parts[1]
        value = parts[2].strip()
        definitions.append({
            "name": name,
            "value": value,
            "access": access,
            "line": line_number,
        })

    registers = []
    for definition in definitions:
        try:
            value = int(definition["value"], 0)
        except ValueError:
            continue
        if not definition["name"].startswith("NV_FUSE_"):
            continue
        if not 0x00820000 <= value <= 0x0082FFFF:
            continue
        if "4R" not in definition["access"]:
            continue
        registers.append({
            "name": definition["name"],
            "address": hex32(value),
            "address_u32": value,
            "access": definition["access"],
            "source_line": definition["line"],
            "fields": [],
            "capture_safe": any(
                item["address"] == value and item["capture_safe"]
                for item in CURATED_REGISTERS
            ),
            "planning_allowed": False,
            "identity_confidence_percent": 99,
            "identity_reason": "literal register identity in generated GA100 header",
        })

    names_by_length = sorted(registers, key=lambda item: len(item["name"]), reverse=True)
    range_re = re.compile(r"^(\d+)\s*:\s*(\d+)$")
    for definition in definitions:
        if "__PRI_ALIAS_" in definition["name"] or "__RED_ALIAS_" in definition["name"]:
            continue
        match = range_re.fullmatch(definition["value"])
        if match is None:
            continue
        for register in names_by_length:
            prefix = register["name"] + "_"
            if definition["name"].startswith(prefix):
                register["fields"].append({
                    "name": definition["name"][len(prefix):],
                    "msb": int(match.group(1)),
                    "lsb": int(match.group(2)),
                    "access": definition["access"],
                    "source_line": definition["line"],
                    "identity_confidence_percent": 99,
                })
                break

    aliases = {}
    alias_re = re.compile(
        r"^(?P<base>NV_FUSE_.+?)__(?P<kind>PRI|RED)_ALIAS_(?P<index>\d+)"
        r"(?:_(?P<suffix>DATA|WIDTH))?$"
    )
    width_by_base = {}
    for definition in definitions:
        width_match = re.fullmatch(r"(NV_FUSE_.+?)__WIDTH", definition["name"])
        if width_match is not None:
            try:
                width_by_base[width_match.group(1)] = int(definition["value"], 0)
            except ValueError:
                pass
        match = alias_re.fullmatch(definition["name"])
        if match is None:
            continue
        key = (match.group("base"), match.group("kind"), int(match.group("index")))
        entry = aliases.setdefault(key, {
            "base": match.group("base"),
            "kind": "primary" if match.group("kind") == "PRI" else "redundant",
            "index": int(match.group("index")),
        })
        suffix = match.group("suffix")
        if suffix is None:
            try:
                entry["row"] = int(definition["value"], 0)
                entry["row_hex"] = hex32(entry["row"])
                entry["row_source_line"] = definition["line"]
            except ValueError:
                entry["row_expression"] = definition["value"]
        elif suffix == "DATA":
            range_match = range_re.fullmatch(definition["value"])
            if range_match is not None:
                entry["msb"] = int(range_match.group(1))
                entry["lsb"] = int(range_match.group(2))
                entry["data_source_line"] = definition["line"]
        elif suffix == "WIDTH":
            try:
                entry["width"] = int(definition["value"], 0)
            except ValueError:
                entry["width_expression"] = definition["value"]

    aliases_by_base = {}
    for entry in aliases.values():
        aliases_by_base.setdefault(entry["base"], []).append(entry)
    alias_schema = []
    for base in sorted(aliases_by_base):
        entries = sorted(
            aliases_by_base[base], key=lambda item: (item["kind"], item["index"])
        )
        alias_schema.append({
            "name": base,
            "width": width_by_base.get(base),
            "aliases": entries,
            "physical_mapping_confidence_percent": 97,
            "confidence_reason": "explicit primary/redundant alias macros",
            "planning_allowed": False,
        })

    registers.sort(key=lambda item: (item["address_u32"], item["name"]))
    return {
        "source": source,
        "sha256": sha256,
        "register_count": len(registers),
        "alias_field_count": len(alias_schema),
        "registers": registers,
        "physical_aliases": alias_schema,
        "note": "Mechanical schema is not a live-read allowlist or an unlock recipe.",
    }


def parse_header_file(path):
    data = Path(path).read_bytes()
    text = data.decode("utf-8", errors="replace")
    return parse_header_text(text, source=str(path), sha256=sha256_bytes(data))


def curated_registry_schema():
    entries = []
    for register in CURATED_REGISTERS:
        entry = dict(register)
        entry["address"] = hex32(entry["address"])
        if register["name"] == "PMC_BOOT_0":
            entry["identity_confidence_percent"] = min(
                entry["identity_confidence_percent"], 97
            )
        entries.append(entry)
    return entries


def default_header_path():
    # The shareable edition never probes a workspace-specific source tree.
    # A caller may still supply an independently obtained compatible header.
    return None


def command_schema(args):
    document = base_document("schema")
    document["access_modes"] = {
        "passive": {
            "direct_tool_writes": 0,
            "allows": [
                "OS/sysfs/proc PCI metadata reads", "query-only NVML/nvidia-smi",
            ],
            "direct_bar_mmio": False,
            "driver_internal_side_effects": "opaque/not provable",
        },
        "bar-read": {
            "meaning": "passive plus allowlisted direct BAR readl",
            "direct_tool_writes": 0,
            "controller_commands": 0,
            "implemented_live": True,
        },
        "glitch-read": {
            "meaning": (
                "allowlisted repeated BAR reads plus strict transient 2080/DSOV=0 "
                "phenotype qualification; no controller transaction"
            ),
            "direct_tool_writes": 0,
            "controller_commands": 0,
            "requires_no_driver_queries": True,
            "identity_2080_is_sufficient": False,
            "otp_eligibility_claimed": False,
            "implemented_live": True,
        },
        "semantic-read": {
            "default_requested": True,
            "meaning": (
                "bar-read plus controller state writes whose only purpose is a read; "
                "requires exclusive unbound maintenance and a helper-serialized ioctl"
            ),
            "bound_or_busy_behavior": (
                "automatic downgrade to bar-read, or passive when owner is declared busy"
            ),
            "implemented_live": True,
        },
        "plan": {
            "meaning": (
                "strictly offline normalization, compatibility export, decode, "
                "goal assessment and manifest construction"
            ),
            "direct_tool_writes": 0,
            "live_device_access": False,
            "mutation_supported": False,
            "missing_values_default_to_zero": False,
        },
        "volatile-write": {
            "meaning": "named policy level; offline validation only in this release",
            "dry_run_default": True,
            "volatile_execution_supported": False,
            "persistence_claimed": False,
            "dsov_requirement": (
                "operation-specific and unresolved; DSOV state is evidence, not a synonym for volatility"
            ),
            "capture_behavior": "downgrade to the highest safe observation level",
        },
        "otp-write": {
            "meaning": (
                "irreversible OTP policy level; only sealed-manifest-v3 blank-FF "
                "append is implemented, while legacy plan-v1 remains refusal-only"
            ),
            "otp_execution_supported": "experimental-v3-blank-FF-append-only",
            "generic_fuse_or_mmio_write_supported": False,
            "persistence_claimed": "only after fresh physical readback and cold validation",
            "dsov_requirement": (
                "write-gate state must be explicitly captured and qualified; DSOV=0 alone is insufficient"
            ),
            "capture_behavior": "downgrade to the highest safe observation level",
        },
    }
    document["access_aliases"] = dict(ACCESS_ALIASES)
    document["read_target_identity_scope"] = {
        "default_discovery_device_ids": [
            "0x%04x" % value for value in sorted(DEFAULT_DEVICE_IDS)
        ],
        "accepted_read_attestation_device_ids": [
            "0x%04x" % value for value in sorted(GA100_READ_TARGET_DEVICE_IDS)
        ],
        "post_conversion_20b5_live_validation": "required",
        "post_conversion_20b5_bar_read": "supported by allowlisted READ32",
        "post_conversion_20b5_controller_default": (
            "kernel rejects MAIN/RIR with EOPNOTSUPP unless explicitly loaded "
            "with allow_a100_controller=1"
        ),
    }
    document["schema_contract"] = {
        "capture": {
            "passive_direct_user_mmio_writes": 0,
            "bar_read_direct_user_mmio_writes": 0,
            "semantic_read_operational_writes": (
                "FUSEADDR/FUSECTRL.CMD_READ and masked DEBUGCTRL.RWL only"
            ),
            "physical_rows_available": (
                "conditional on successful exclusive helper-serialized certification"
            ),
            "metadata_only_fallback": True,
            "minimum_passes": 2,
        },
        "analyze_rows": {
            "explicit_fuseless_bounds_required": True,
            "explicit_sparse_policy_required": True,
            "explicit_row_view_required": True,
            "executable_plans": False,
        },
        "plan": {
            "access_level": "plan",
            "planning_models": list(PLANNING_MODELS),
            "reference_plan_import": (
                "hash/schema/declaration inventory only; never operation authority"
            ),
            "missing_values_default_to_zero": False,
            "complete_old_and_new_images_required_for_execution": True,
            "physical_operations_default": [],
            "integrity": "sha256-canonical-json-v1",
            "executable": False,
        },
        "apply": {
            "manifest_integrity_required": True,
            "dry_run_only": True,
            "live_write_primitive": False,
        },
        "full_write": {
            "manifest_integrity_required": True,
            "interactive_confirmation_on_explicit_execute_request": True,
            "execution_backend": (
                "legacy v1: unavailable/EOPNOTSUPP; v3: experimental "
                "hash-sealed GA100 blank-FF append"
            ),
            "live_write_primitive": "manifest-only-v3; no generic address/value API",
        },
    }
    document["curated_live_registry"] = curated_registry_schema()
    document["generated_shadow_manifest"] = {
        "hashes": dict(GA100_SHADOW_MANIFEST_HASHES),
        "summary": dict(GA100_SHADOW_MANIFEST_SUMMARY),
        "capture_profile": "shadow-full",
        "profile_register_count": len(registers_for_profile("shadow-full")),
        "interpretation_boundary": (
            "address/name identities are inventory evidence; names alone do not "
            "prove behavior or authorize a logical/physical fuse edit"
        ),
    }
    document["goals"] = GOALS
    document["goal_dependency_closure"] = GOAL_DEPENDENCY_CLOSURE
    document["logical_field_catalog"] = LOGICAL_FIELD_CATALOG
    document["iff_machine_spec"] = IFF_MACHINE_SPEC
    document["dsov_persistence_model"] = DSOV_PERSISTENCE_MODEL
    document["dump_formats"] = dump_format_schema()
    document["rir_schema"] = {
        "rows": 8,
        "entries": 16,
        "entry_bits": 16,
        "geometry_certainty_percent": 99,
        "geometry_source": (
            "GA100 RIR implementation hard-codes the row count at 8; retained captures corroborate"
        ),
        "address": "bits 15:2 absolute main-array bit address",
        "forced_data": "bit 1",
        "enable": "bit 0",
        "layout_certainty_percent": 99,
        "layout_source": "GA100 poison-record construction evidence",
        "duplicate_address_precedence": (
            "unconfirmed; duplicate enabled addresses block effective replay"
        ),
    }
    header = getattr(args, "header", None) or default_header_path()
    if header:
        header_schema = parse_header_file(header)
        document["direct_header_registry"] = header_schema
    else:
        header_schema = None
        document["direct_header_registry"] = {
            "available": False,
            "reason": "no --header supplied and workspace default not found",
        }
    document["registry_provenance_census"] = registry_provenance_census(
        header_schema
    )
    emit_json(document, args.output)
    return 0


def _parse_row_container(container, label):
    if isinstance(container, dict):
        wrapper_keys = [
            row_key
            for row_key in (
                "rows", "main_rows", "ff_rows", "rir_rows", "main", "rir"
            )
            if row_key in container
        ]
        if len(wrapper_keys) > 1:
            raise AuditError(
                "%s contains ambiguous row containers: %s"
                % (label, ", ".join(wrapper_keys))
            )
        if wrapper_keys:
            container = container[wrapper_keys[0]]
    rows = {}
    if isinstance(container, list):
        for sequential_index, value in enumerate(container):
            if value is None:
                continue
            if isinstance(value, dict):
                if "row" not in value:
                    raise AuditError(
                        "%s entry %d lacks an explicit row" % (label, sequential_index)
                    )
                index = parse_int(value["row"], "%s row index" % label)
                raw = value.get("raw", value.get("value"))
                if raw is None:
                    # Preserve a failed/unknown public-tool row as missing. The
                    # caller's sparse policy, never a fabricated zero, decides
                    # whether the dump can be used.
                    continue
                parsed_value = parse_int(raw, "%s row %d" % (label, index))
            else:
                index = sequential_index
                parsed_value = parse_int(value, "%s row %d" % (label, index))
            if index in rows:
                raise AuditError("%s contains duplicate row %d" % (label, index))
            rows[index] = parsed_value
    elif isinstance(container, dict):
        for key, value in container.items():
            if value is None:
                continue
            index = parse_int(key, "%s row index" % label)
            if isinstance(value, str) and value.strip().upper() in (
                "TIMEOUT", "ERROR", "UNKNOWN", "UNREAD", "MISSING"
            ):
                # Public collectors serialize failed reads with string
                # sentinels. Preserve them as absent rows, never zero.
                continue
            if index in rows:
                raise AuditError(
                    "%s contains duplicate numeric row %d (key %r)"
                    % (label, index, key)
                )
            rows[index] = parse_int(value, "%s row %d" % (label, index))
    else:
        raise AuditError("%s must be a row list or index/value mapping" % label)
    for index, value in rows.items():
        if not 0 <= index < MAX_FUSE_ROWS:
            raise AuditError("%s row index out of range: %d" % (label, index))
        if not 0 <= value <= 0xFFFFFFFF:
            raise AuditError("%s row value out of range at %d" % (label, index))
    return rows


def _parse_text_rows(text, allow_log_messages=False):
    rows = {}
    next_index = 0
    bracket_re = re.compile(
        r"^\s*\[(\d{1,3})\]\s*(?:[:=]\s*)?(0x[0-9a-fA-F]+|\d+)\s*$"
    )
    row_label_re = re.compile(
        r"^\s*Row\s+(\d+)\s*:\s*(0x[0-9a-fA-F]+|\d+)\s*$",
        re.IGNORECASE,
    )
    indexed_re = re.compile(
        r"^\s*(\d+)\s*[:,=\s]\s*(0x[0-9a-fA-F]+|\d+)\s*$"
    )
    bare_re = re.compile(r"^\s*(0x[0-9a-fA-F]+|\d+)\s*$")
    log_re = re.compile(
        r"^\[(?:INFO|WARN|WARNING|ERROR|DEBUG)\](?:\s.*)?$", re.IGNORECASE
    )
    for line_number, original in enumerate(text.splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        match = (
            bracket_re.fullmatch(line)
            or row_label_re.fullmatch(line)
            or indexed_re.fullmatch(line)
        )
        if match is not None:
            # These three syntaxes define decimal row labels.  Using base=0
            # would reject ordinary zero-padded dump labels such as [001].
            index = int(match.group(1), 10)
            value = parse_int(match.group(2), "line %d value" % line_number)
        else:
            bare_match = bare_re.fullmatch(line)
            if bare_match is not None:
                index = next_index
                value = parse_int(
                    bare_match.group(1), "line %d value" % line_number
                )
            else:
                if allow_log_messages and log_re.fullmatch(line):
                    continue
                raise AuditError(
                    "unrecognized indexed-text content at line %d: %r"
                    % (line_number, original)
                )
        if index in rows:
            raise AuditError("duplicate row %d at input line %d" % (index, line_number))
        rows[index] = value
        next_index = index + 1
    if not rows:
        raise AuditError("text input contains no recognized row values")
    return [_parse_row_container(rows, "text pass 1")]


def _selector_identities(item, index=None):
    identities = set()
    if index is not None:
        identities.add(str(index))
    if not isinstance(item, dict):
        if item is not None:
            identities.add(str(item))
        return identities
    for key in ("domain_bdf", "uuid", "pci", "bdf", "reported_name", "desc", "sku"):
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            identities.add(str(value))
    nested = item.get("card")
    if isinstance(nested, dict):
        identities.update(_selector_identities(nested))
    elif nested is not None and not isinstance(nested, list):
        identities.add(str(nested))
    return identities


def _strong_selector_identities(item):
    identities = set()
    if not isinstance(item, dict):
        return identities
    for key in ("domain_bdf", "uuid", "bdf", "pci"):
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            identities.add(str(value))
    nested = item.get("card")
    if isinstance(nested, dict):
        identities.update(_strong_selector_identities(nested))
    return identities


def _select_card_capture(parsed, selector):
    root_capture = None
    if (
        isinstance(parsed, dict)
        and (
            isinstance(parsed.get("cards"), list)
            or isinstance(parsed.get("gpus"), list)
        )
    ):
        collection_key = "cards" if isinstance(parsed.get("cards"), list) else "gpus"
        # Keep every collection-level claim exactly as supplied.  Conversion
        # treats these values as unauthenticated source metadata, but silently
        # dropping an unfamiliar provenance key makes a later round trip look
        # more authoritative than the evidence actually permits.
        root_capture = {
            key: value for key, value in parsed.items() if key != collection_key
        }
        root_capture["collection_key"] = collection_key
        parsed = parsed[collection_key]
    if not isinstance(parsed, list) or not parsed or not all(
        isinstance(item, dict) for item in parsed
    ):
        return parsed, None
    looks_like_cards = all(
        any(key in item for key in (
            "main_rows", "ff_rows", "physical_rows", "rir_rows", "rir_passes",
            "raw_rows", "pass1", "pass2", "fuse_rows",
        ))
        for item in parsed
    )
    if not looks_like_cards:
        return parsed, None
    if selector is None:
        if len(parsed) != 1:
            identities = [
                "/".join(sorted(_selector_identities(item, index)))
                for index, item in enumerate(parsed)
            ]
            raise AuditError(
                "input contains %d card captures; select one with --card (%s)"
                % (len(parsed), ", ".join(identities))
            )
        selected = parsed[0]
    else:
        matches = []
        for index, item in enumerate(parsed):
            identities = _selector_identities(item, index)
            if selector in identities:
                matches.append(item)
        if len(matches) != 1:
            raise AuditError(
                "--card %r matched %d captures; use an exact card/pci/bdf/index"
                % (selector, len(matches))
            )
        selected = matches[0]
    metadata = {
        key: selected.get(key)
        for key in (
            "card", "pci", "bdf", "domain_bdf", "uuid", "reported_name",
            "desc", "sku", "device_id", "serial", "pci_id",
        )
        if selected.get(key) is not None
    }
    if root_capture is not None:
        metadata["root_capture"] = root_capture
    return selected, metadata


_DSTORO_BACKUP_POLICIES = frozenset((
    "ga100-20c2-xpatrik-pcie-v2",
    "ga100-20c2-known-fields-v1",
))
_DSTORO_BACKUP_REQUIRED_KEYS = frozenset((
    "schema", "policy", "map_hash", "utc", "bdf", "identity", "control",
    "named", "main", "rir", "complete", "shadow_matches_main", "main_hash",
    "rir_hash", "collector_sha256",
))
_DSTORO_CURRENT_PRODUCER_REQUIRED_KEYS = (
    _DSTORO_BACKUP_REQUIRED_KEYS | frozenset(("rir_observation",))
)
_DSTORO_KNOWN_MAP_HASH = (
    "4e931cc33ebb8c55c0ae1c2d0ce3b8107407b1888d76fad3e37db92087714b71"
)
_DSTORO_CONTROL_KEYS = frozenset((
    "BOOT0", "CTRL", "ADDR", "DEBUG", "CRC", "EFP", "DFP", "DSOV",
    "EN_SW", "WPLM", "CPLM", "OPLM", "WSEC", "SBFE",
))


def _is_sha256_text(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _dstoro_backup_profile(parsed):
    """Recognize Danylo Storozhev's backup envelope without trusting it.

    This is deliberately a structural format gate, not the upstream burner's
    validator.  In particular, the claimed register-map hash cannot be
    recomputed unless the exact core/options/status map set accompanies the
    artifact.  Content digests are independently checked and exposed as
    claims, but a damaged backup remains identifiable as that file format.
    """
    result = {
        "profile_claimed": False,
        "matches_profile": False,
        "content_integrity_valid": False,
        "integrity_errors": [],
        "missing_keys": [],
        "current_producer_missing_keys": [],
        "profile_variant": None,
        "shape_errors": [],
        "accepted_policies": sorted(_DSTORO_BACKUP_POLICIES),
        "policy_recognized": False,
        "main_hash_matches": None,
        "rir_hash_matches": None,
        "declared_map_hash": None,
        "known_map_hash_reference": _DSTORO_KNOWN_MAP_HASH,
        "declared_map_hash_matches_known_reference": None,
        "map_hash_recomputed": False,
        "map_hash_derivation": (
            "SHA-256 of canonical JSON (sort_keys=True, separators=(',', ':')) "
            "over the exact {core,options,status} register-map object"
        ),
    }
    if not isinstance(parsed, dict):
        result["shape_errors"].append("top level is not an object")
        return result
    result["profile_claimed"] = (
        parsed.get("schema") == 1
        and parsed.get("policy") in _DSTORO_BACKUP_POLICIES
        and ("main" in parsed or "rir" in parsed)
    )
    missing = sorted(_DSTORO_BACKUP_REQUIRED_KEYS - set(parsed))
    result["missing_keys"] = missing
    result["current_producer_missing_keys"] = sorted(
        _DSTORO_CURRENT_PRODUCER_REQUIRED_KEYS - set(parsed)
    )
    result["policy_recognized"] = parsed.get("policy") in _DSTORO_BACKUP_POLICIES
    result["declared_map_hash"] = parsed.get("map_hash")
    if _is_sha256_text(parsed.get("map_hash")):
        result["declared_map_hash_matches_known_reference"] = (
            parsed["map_hash"].lower() == _DSTORO_KNOWN_MAP_HASH
        )
    if parsed.get("schema") != 1 or isinstance(parsed.get("schema"), bool):
        result["shape_errors"].append("schema is not integer 1")
    if parsed.get("complete") is not True:
        result["shape_errors"].append("complete is not true")
    if not result["policy_recognized"]:
        result["shape_errors"].append("policy is not an accepted upstream policy")
    for key in ("map_hash", "main_hash", "rir_hash", "collector_sha256"):
        if not _is_sha256_text(parsed.get(key)):
            result["shape_errors"].append("%s is not a SHA-256 hex digest" % key)
    if not isinstance(parsed.get("bdf"), str) or BDF_RE.fullmatch(parsed["bdf"]) is None:
        result["shape_errors"].append("bdf is not a canonical full PCI BDF")
    if not isinstance(parsed.get("utc"), str):
        result["shape_errors"].append("utc is not a string")
    if not isinstance(parsed.get("shadow_matches_main"), bool):
        result["shape_errors"].append("shadow_matches_main is not boolean")
    if "rir_observation" in parsed and not isinstance(parsed.get("rir_observation"), dict):
        result["shape_errors"].append("rir_observation is not an object")
    identity = parsed.get("identity")
    if not isinstance(identity, dict) or not all(
        key in identity for key in ("serial", "pci_id", "die", "prom_sha256")
    ):
        result["shape_errors"].append("identity lacks serial/pci_id/die/prom_sha256")
    elif (
        not isinstance(identity.get("serial"), str)
        or not identity["serial"].isdigit()
        or identity.get("pci_id") != "10de:20c2"
        or not isinstance(identity.get("die"), dict)
        or not _is_sha256_text(identity.get("prom_sha256"))
    ):
        result["shape_errors"].append("identity values do not match the backup profile")
    control = parsed.get("control")
    if not isinstance(control, dict) or not _DSTORO_CONTROL_KEYS.issubset(control):
        result["shape_errors"].append("control lacks the exact controller snapshot keys")
    named = parsed.get("named")
    if not isinstance(named, dict) or not all(
        isinstance(named.get(group), dict) for group in ("core", "options", "status")
    ):
        result["shape_errors"].append("named lacks core/options/status objects")
    elif not all(named[group] for group in ("core", "options", "status")):
        result["shape_errors"].append("named core/options/status objects are empty")

    vectors = (("main", MAX_FUSE_ROWS), ("rir", XRAY_RIR_ROWS))
    for key, length in vectors:
        vector = parsed.get(key)
        if not isinstance(vector, list) or len(vector) != length or any(
            type(value) is not int or not 0 <= value <= 0xFFFFFFFF
            for value in vector
        ):
            result["shape_errors"].append(
                "%s is not an exact %d-word u32 vector" % (key, length)
            )
    result["matches_profile"] = not missing and not result["shape_errors"]
    if result["matches_profile"]:
        result["profile_variant"] = (
            "current-producer"
            if not result["current_producer_missing_keys"]
            else "legacy-without-rir-observation"
        )
    main = parsed.get("main")
    if isinstance(main, list) and len(main) == MAX_FUSE_ROWS and all(
        type(value) is int and 0 <= value <= 0xFFFFFFFF for value in main
    ) and _is_sha256_text(parsed.get("main_hash")):
        result["main_hash_matches"] = (
            _packed_u32_pass_sha256(main).lower() == parsed["main_hash"].lower()
        )
    rir = parsed.get("rir")
    if isinstance(rir, list) and len(rir) == XRAY_RIR_ROWS and all(
        type(value) is int and 0 <= value <= 0xFFFFFFFF for value in rir
    ) and _is_sha256_text(parsed.get("rir_hash")):
        result["rir_hash_matches"] = (
            _packed_u32_pass_sha256(rir).lower() == parsed["rir_hash"].lower()
        )
    if result["main_hash_matches"] is False:
        result["integrity_errors"].append("MAIN content digest mismatch")
    if result["rir_hash_matches"] is False:
        result["integrity_errors"].append("RIR content digest mismatch")
    result["content_integrity_valid"] = (
        result["matches_profile"]
        and result["main_hash_matches"] is True
        and result["rir_hash_matches"] is True
    )
    return result


def _gate_claimed_dstoro_profile(profile):
    """Refuse malformed or digest-mismatched files claiming this backup format."""
    if not profile.get("profile_claimed"):
        return
    if not profile.get("matches_profile"):
        details = list(profile.get("missing_keys", [])) + list(
            profile.get("shape_errors", [])
        )
        raise AuditError(
            "claimed Danylo backup is structurally invalid: %s"
            % ("; ".join(details) or "unknown structural error")
        )
    if profile.get("content_integrity_valid") is not True:
        raise AuditError(
            "claimed Danylo backup failed content-integrity validation: %s"
            % (
                "; ".join(profile.get("integrity_errors", []))
                or "MAIN/RIR digest could not be verified"
            )
        )


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("JSON object contains duplicate key %r" % key)
        result[key] = value
    return result


def _normalize_legacy_xray_document(value):
    """Translate only old project identity tokens in retained JSON evidence.

    Paths and free-form prose are deliberately left byte-semantic: this is a
    parser compatibility shim, not a provenance rewrite.  The raw artifact
    digest remains the digest of the original bytes.
    """
    if isinstance(value, list):
        return [_normalize_legacy_xray_document(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            mapped = {
                "ray_helper_identity": "xray_helper_identity",
                "ray_projection_schema": "xray_projection_schema",
                "integrated_170hx_ray_helper": "integrated_170hx_xray_helper",
            }.get(key, key)
            if mapped.startswith("_170hx_ray_"):
                mapped = "_170hx_xray_" + mapped[len("_170hx_ray_"):]
            if mapped in result:
                raise AuditError(
                    "legacy identity normalization collides at key %r" % mapped
                )
            result[mapped] = _normalize_legacy_xray_document(item)
        return result
    if not isinstance(value, str):
        return value
    exact = {
        LEGACY_SCHEMA_VERSION: SCHEMA_VERSION,
        "ray-json": "xray-json",
        "ray-jsonl": "xray-jsonl",
        "ray-capture-json": "xray-capture-json",
        "170hx-ray-helper-allowlisted-read32":
            "170hx-xray-helper-allowlisted-read32",
    }
    if value in exact:
        return exact[value]
    if value.startswith("170hx-ray-") and not value.endswith(
            (".json", ".jsonl", ".bin", ".txt", ".md")):
        return "170hx-xray-" + value[len("170hx-ray-"):]
    if value.startswith("ray-common-"):
        return "xray-common-" + value[len("ray-common-"):]
    if value.startswith("ray-best-"):
        return "xray-best-" + value[len("ray-best-"):]
    return value


def _parse_jsonl_rows(text, label="JSONL pass 1"):
    rows = {}
    seen = 0
    for line_number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        try:
            item = _strict_json_loads(
                line, object_pairs_hook=_json_object_without_duplicates
            )
        except json.JSONDecodeError as exc:
            raise AuditError("invalid JSONL at line %d: %s" % (line_number, exc)) from exc
        if not isinstance(item, dict) or "row" not in item:
            raise AuditError("JSONL line %d must be an object with row" % line_number)
        index = parse_int(item["row"], "JSONL line %d row" % line_number)
        raw = item.get("value", item.get("raw"))
        if raw is None:
            # Explicit failed reads remain absent; never convert them to zero.
            continue
        value = parse_int(raw, "JSONL line %d value" % line_number)
        if index in rows:
            raise AuditError("duplicate JSONL row %d at line %d" % (index, line_number))
        rows[index] = value
        seen += 1
    if not seen:
        raise AuditError("JSONL input contains no known row values")
    return _parse_row_container(rows, label)


def _parse_raw_u32le_rows(data):
    expected = MAX_FUSE_ROWS * 4
    if len(data) != expected:
        raise AuditError(
            "raw-u32le MAIN input must be exactly %d bytes (512 little-endian u32 words), got %d"
            % (expected, len(data))
        )
    values = struct.unpack("<%dI" % MAX_FUSE_ROWS, data)
    return {index: value for index, value in enumerate(values)}


def _reject_ambiguous_main_containers(parsed):
    """Reject JSON whose format would otherwise depend on branch ordering."""
    if not isinstance(parsed, dict):
        return
    present = [
        key for key in (
            "fuse_rows", "raw_rows", "passes", "physical_rows", "main_rows",
            "ff_rows", "main", "rows",
        )
        if key in parsed
    ]
    if "pass1" in parsed or "pass2" in parsed:
        present.append("pass1/pass2")
    if (
        parsed.get("artifact_schema") == "170hx-xray-portable-dump-v1"
        and set(present) == {"passes", "main_rows"}
    ):
        passes = parsed.get("passes")
        selected_pass = (
            parsed.get("source_dump", {}).get("selected_pass")
            if isinstance(parsed.get("source_dump"), dict) else None
        )
        if (
            not isinstance(passes, list)
            or isinstance(selected_pass, bool)
            or not isinstance(selected_pass, int)
            or not 1 <= selected_pass <= len(passes)
        ):
            raise AuditError("portable XRAY dump has an invalid selected MAIN pass")
        projected = _parse_row_container(parsed["main_rows"], "portable main_rows")
        selected = _parse_row_container(
            passes[selected_pass - 1], "portable selected pass"
        )
        if projected != selected:
            raise AuditError(
                "portable XRAY main_rows disagrees with its selected pass"
            )
        return
    if len(present) > 1:
        raise AuditError(
            "JSON MAIN input contains ambiguous row containers: %s"
            % ", ".join(present)
        )


def _reject_ambiguous_rir_containers(parsed):
    if not isinstance(parsed, dict):
        return
    present = [key for key in ("rir_passes", "rir_rows", "rir") if key in parsed]
    if len(present) > 1:
        raise AuditError(
            "JSON RIR input contains ambiguous row containers: %s"
            % ", ".join(present)
        )


def load_row_passes(path, card_selector=None, input_format="auto"):
    if input_format not in DUMP_INPUT_FORMATS:
        raise AuditError("unsupported dump input format: %s" % input_format)
    if path == "-":
        data = sys.stdin.buffer.read()
        source = "stdin"
    else:
        data = Path(path).read_bytes()
        source = path
    if input_format == "raw-u32le":
        return {
            "source": source,
            "sha256": sha256_bytes(data),
            "passes": [_parse_raw_u32le_rows(data)],
            "selected_card": None,
            "selected_capture": None,
            "detected_format": "raw-u32le",
        }
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AuditError(
            "row input is not UTF-8; use --input-format raw-u32le for an exact 2048-byte MAIN image"
        ) from exc
    if input_format == "indexed-text":
        return {
            "source": source,
            "sha256": sha256_bytes(data),
            "passes": _parse_text_rows(text),
            "selected_card": None,
            "selected_capture": None,
            "detected_format": "indexed-text",
        }
    if input_format == "jsonl":
        return {
            "source": source,
            "sha256": sha256_bytes(data),
            "passes": [_parse_jsonl_rows(text)],
            "selected_card": None,
            "selected_capture": None,
            "detected_format": "dstoro-jsonl",
        }
    stripped = text.lstrip()
    if input_format == "json" and not (
        stripped.startswith("[") or stripped.startswith("{")
    ):
        raise AuditError("--input-format json requires a JSON object or array")
    parsed = None
    parsed_as_json = False
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            parsed = _strict_json_loads(
                text, object_pairs_hook=_json_object_without_duplicates
            )
            parsed = _normalize_legacy_xray_document(parsed)
            parsed_as_json = True
        except json.JSONDecodeError as exc:
            if input_format == "json":
                raise AuditError("malformed JSON row input: %s" % exc) from exc
            # Retained line-oriented dumps commonly begin with [INFO] or [000].
            # Object-shaped input, and list-shaped input without one of those
            # explicit text prefixes, must never be reinterpreted permissively.
            first_line = stripped.splitlines()[0] if stripped else ""
            text_dump_prefix = re.match(
                r"^\[(?:INFO|WARN|WARNING|ERROR|DEBUG|\d{1,3})\](?:\s|$)",
                first_line,
                re.IGNORECASE,
            )
            if stripped.startswith("{"):
                # Danylo's captures are strict one-object-per-line JSONL. Only
                # accept the input when every non-empty line parses under that
                # grammar; never fall back to a permissive partial JSON parser.
                try:
                    jsonl_rows = _parse_jsonl_rows(text)
                except AuditError:
                    raise AuditError("malformed JSON row input: %s" % exc) from exc
                return {
                    "source": source,
                    "sha256": sha256_bytes(data),
                    "passes": [jsonl_rows],
                    "selected_card": None,
                    "selected_capture": None,
                    "detected_format": "dstoro-jsonl",
                }
            if text_dump_prefix is None:
                raise AuditError("malformed JSON row input: %s" % exc) from exc
            parsed_as_json = False
    selected_capture = None
    format_claims = {}
    if parsed_as_json:
        parsed, selected_card = _select_card_capture(parsed, card_selector)
        if isinstance(parsed, dict):
            selected_capture = parsed
        _reject_ambiguous_main_containers(parsed)
        dstoro_profile = _dstoro_backup_profile(parsed)
        _gate_claimed_dstoro_profile(dstoro_profile)
        if isinstance(parsed, dict) and "fuse_rows" in parsed:
            containers = [parsed["fuse_rows"]]
            detected_format = "ga100-fusedump-v1"
            format_claims.update({
                "source_schema": (
                    selected_card.get("root_capture", {}).get("schema")
                    if isinstance(selected_card, dict) else None
                ),
                "single_pass_only": True,
                "declared_validation": parsed.get("validation"),
                "declared_row0_gate": parsed.get("row0_gate"),
                "declared_orig_fusectrl": parsed.get("orig_fusectrl"),
                "declared_restored_fusectrl": parsed.get("restored_fusectrl"),
                "declared_orig_fuseaddr": parsed.get("orig_fuseaddr"),
                "declared_restored_fuseaddr": parsed.get("restored_fuseaddr"),
                "aperture_count": (
                    len(parsed["apertures"])
                    if isinstance(parsed.get("apertures"), (list, dict)) else None
                ),
            })
        elif isinstance(parsed, dict) and "raw_rows" in parsed:
            containers = parsed["raw_rows"]
            if not isinstance(containers, list) or not containers:
                raise AuditError("ga100 public raw_rows must be a non-empty pass list")
            if len(containers) > 2 or any(
                not isinstance(item, (list, dict)) for item in containers
            ):
                raise AuditError(
                    "ga100 public raw_rows must contain at most two row containers"
                )
            detected_format = (
                "ga100-public-capture-v1"
                if parsed.get("schema") == "ga100-public-capture-v1"
                else "public-raw-rows-json"
            )
            format_claims.update({
                "source_schema": parsed.get("schema"),
                "declared_complete": parsed.get("complete"),
                "declared_ok": parsed.get("ok"),
                "declared_raw_requested": parsed.get("raw_requested"),
                "declared_physical_rows_captured": parsed.get(
                    "physical_rows_captured"
                ),
                "declared_raw_match": parsed.get("raw_match"),
                "declared_raw_address_restored": parsed.get(
                    "raw_address_restored"
                ),
                "declared_options_match": parsed.get("options_match"),
                "declared_non_programming": parsed.get("non_programming"),
                "declared_mmio_read_only": parsed.get("mmio_read_only"),
                "read_issue_pass_count": (
                    len(parsed["read_issues"])
                    if isinstance(parsed.get("read_issues"), list) else None
                ),
            })
        elif isinstance(parsed, dict) and "pass1" in parsed and "pass2" in parsed:
            if not (
                isinstance(parsed["pass1"], list)
                and isinstance(parsed["pass2"], list)
                and len(parsed["pass1"]) == MAX_FUSE_ROWS
                and len(parsed["pass2"]) == MAX_FUSE_ROWS
            ):
                raise AuditError(
                    "portz3r0 gpu_evidence pass1/pass2 must each contain exactly 512 rows"
                )
            containers = [parsed["pass1"], parsed["pass2"]]
            detected_format = "portz3r0-gpu-evidence-json"
            format_claims["declared_stable"] = parsed.get("stable")
        elif isinstance(parsed, dict) and "passes" in parsed:
            containers = parsed["passes"]
            if not isinstance(containers, list):
                raise AuditError("passes must be a list")
            detected_format = "xray-passes-json"
        elif isinstance(parsed, dict) and "physical_rows" in parsed:
            physical = parsed["physical_rows"]
            if not isinstance(physical, dict):
                raise AuditError("physical_rows must be an object")
            if isinstance(physical.get("passes"), list) and physical["passes"]:
                containers = physical["passes"]
            elif "rows" in physical:
                containers = [physical["rows"]]
            else:
                raise AuditError("physical_rows object has no rows/passes")
            detected_format = "xray-capture-json"
        elif isinstance(parsed, dict) and "main_rows" in parsed:
            containers = [parsed["main_rows"]]
            detected_format = "ga100-fuse-re-json"
        elif isinstance(parsed, dict) and "ff_rows" in parsed:
            _parse_ffcalc_option_words(parsed)
            containers = [parsed["ff_rows"]]
            detected_format = "ffcalc-json"
        elif (
            isinstance(parsed, dict)
            and parsed.get("schema") == "170hx-xray-dstoro-projection-v1"
            and "main" in parsed
        ):
            containers = [parsed["main"]]
            detected_format = "170hx-xray-dstoro-projection-v1"
            format_claims.update({
                "source_schema": parsed.get("schema"),
                "compatible_with_dstoro_backup_validator": False,
                "declared_compatible_with_dstoro_backup_validator": parsed.get(
                    "compatible_with_dstoro_backup_validator"
                ),
                "nonconformance_reasons": parsed.get("nonconformance_reasons"),
                "row_transport_only": True,
            })
        elif dstoro_profile["matches_profile"]:
            containers = [parsed["main"]]
            detected_format = "dstoro-backup-json"
            format_claims["dstoro_profile"] = dstoro_profile
            format_claims["validator_conformance_unverified"] = True
        elif isinstance(parsed, dict) and "main" in parsed:
            containers = [parsed["main"]]
            detected_format = "generic-main-json"
            format_claims["dstoro_profile"] = dstoro_profile
        elif isinstance(parsed, dict) and "rows" in parsed:
            containers = [parsed["rows"]]
            if parsed["rows"] and all(
                isinstance(item, dict) and "row" in item
                for item in parsed["rows"]
            ):
                detected_format = "xpatrik-json"
            elif "rows_match" in parsed or "regs" in parsed:
                detected_format = "darkmaniac-vfi-snapshot-json"
            elif isinstance(parsed["rows"], dict) and any(
                key in parsed for key in (
                    "status", "shadow", "nonblank", "ceilings", "fingerprint"
                )
            ):
                detected_format = "portz3r0-fusedump-json"
            else:
                detected_format = "generic-rows-json"
        else:
            containers = [parsed]
            detected_format = "generic-json"
        passes = [
            _parse_row_container(container, "pass %d" % (index + 1))
            for index, container in enumerate(containers)
        ]
        if detected_format in (
            "ga100-public-capture-v1", "public-raw-rows-json"
        ):
            full_passes = [
                len(row_pass) == MAX_FUSE_ROWS
                and all(index in row_pass for index in range(MAX_FUSE_ROWS))
                for row_pass in passes
            ]
            computed_match = len(passes) == 2 and passes[0] == passes[1]
            format_claims.update({
                "computed_pass_count": len(passes),
                "computed_full_passes": full_passes,
                "computed_raw_match": computed_match,
                "declared_raw_match_matches_computed": (
                    parsed.get("raw_match") is computed_match
                    if isinstance(parsed.get("raw_match"), bool) else None
                ),
                "computed_physical_rows_complete": (
                    len(full_passes) == 2 and all(full_passes)
                ),
            })
        elif detected_format == "portz3r0-gpu-evidence-json":
            computed_stable = passes[0] == passes[1]
            format_claims.update({
                "computed_stable": computed_stable,
                "declared_stable_matches_computed": (
                    parsed.get("stable") is computed_stable
                ),
            })
    else:
        passes = _parse_text_rows(text, allow_log_messages=True)
        selected_card = None
        detected_format = "indexed-text"
    if not passes:
        raise AuditError("input contains no row passes")
    return {
        "source": source,
        "sha256": sha256_bytes(data),
        "passes": passes,
        "selected_card": selected_card,
        # Kept internal so analyze-rows can verify a native XRAY capture's
        # transaction envelope.  It is never treated as authenticated merely
        # because it came from JSON.
        "selected_capture": selected_capture,
        "detected_format": detected_format,
        "format_claims": format_claims,
    }


def load_rir_words(path, card_selector=None, input_format="auto"):
    if input_format not in DUMP_INPUT_FORMATS:
        raise AuditError("unsupported RIR input format: %s" % input_format)
    data = Path(path).read_bytes() if path != "-" else sys.stdin.buffer.read()
    selected_card = None
    selected_capture = None
    format_claims = {}
    if input_format == "raw-u32le":
        if len(data) != XRAY_RIR_ROWS * 4:
            raise AuditError(
                "raw-u32le RIR input must be exactly 32 bytes, got %d" % len(data)
            )
        containers = [list(struct.unpack("<8I", data))]
        detected_format = "raw-u32le-rir"
    else:
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise AuditError(
                "RIR input is not UTF-8; select --rir-input-format raw-u32le for 32-byte binary"
            ) from exc
        if input_format == "jsonl":
            containers = [_parse_jsonl_rows(text, "RIR JSONL pass 1")]
            detected_format = "dstoro-rir-jsonl"
        elif input_format == "indexed-text":
            containers = _parse_text_rows(text)
            detected_format = "indexed-rir-text"
        else:
            stripped = text.lstrip()
            if input_format == "json" and not (
                stripped.startswith("[") or stripped.startswith("{")
            ):
                raise AuditError("--rir-input-format json requires a JSON object or array")
            parsed = None
            if stripped.startswith("[") or stripped.startswith("{"):
                try:
                    parsed = _strict_json_loads(
                        text, object_pairs_hook=_json_object_without_duplicates
                    )
                    parsed = _normalize_legacy_xray_document(parsed)
                except json.JSONDecodeError as exc:
                    if input_format == "json":
                        raise AuditError("malformed RIR JSON: %s" % exc) from exc
                    if stripped.startswith("{"):
                        containers = [_parse_jsonl_rows(text, "RIR JSONL pass 1")]
                        detected_format = "dstoro-rir-jsonl"
                    else:
                        containers = _parse_text_rows(text, allow_log_messages=True)
                        detected_format = "indexed-rir-text"
            elif input_format == "json":
                raise AuditError("RIR JSON input is empty or malformed")
            else:
                containers = _parse_text_rows(text, allow_log_messages=True)
                detected_format = "indexed-rir-text"
            if parsed is not None:
                parsed, selected_card = _select_card_capture(parsed, card_selector)
                selected_capture = parsed if isinstance(parsed, dict) else None
                if not isinstance(parsed, dict):
                    raise AuditError("selected RIR input must be an object")
                _reject_ambiguous_rir_containers(parsed)
                dstoro_profile = _dstoro_backup_profile(parsed)
                _gate_claimed_dstoro_profile(dstoro_profile)
                if "rir_passes" in parsed:
                    containers = parsed["rir_passes"]
                    detected_format = "rir-passes-json"
                elif "rir_rows" in parsed:
                    rir_container = parsed["rir_rows"]
                    if isinstance(rir_container, dict):
                        if rir_container.get("available") is False:
                            reason = rir_container.get("reason")
                            raise AuditError(
                                "source RIR is explicitly unavailable%s"
                                % (
                                    ": %s" % reason
                                    if isinstance(reason, str) and reason else ""
                                )
                            )
                        nested = [
                            key for key in ("passes", "rows", "words")
                            if rir_container.get(key) is not None
                        ]
                        if set(nested) == {"passes", "rows"}:
                            rir_passes = rir_container.get("passes")
                            if (
                                not isinstance(rir_passes, list)
                                or not rir_passes
                                or not isinstance(rir_container.get("rows"), list)
                                or rir_passes[0] != rir_container["rows"]
                            ):
                                raise AuditError(
                                    "rir_rows rows disagree with its first pass"
                                )
                            nested = ["passes"]
                        if len(nested) > 1:
                            raise AuditError(
                                "rir_rows contains ambiguous containers: %s"
                                % ", ".join(nested)
                            )
                    if isinstance(rir_container, dict) and "passes" in rir_container:
                        containers = rir_container["passes"]
                    else:
                        containers = [rir_container]
                    detected_format = "rir-rows-json"
                elif "rir" in parsed:
                    rir_object = parsed["rir"]
                    if isinstance(rir_object, dict):
                        nested = [
                            key for key in ("passes", "rows", "words")
                            if rir_object.get(key) is not None
                        ]
                        if (
                            parsed.get("artifact_schema")
                            == "170hx-xray-portable-dump-v1"
                            and set(nested) == {"passes", "words"}
                        ):
                            rir_passes = rir_object.get("passes")
                            if (
                                not isinstance(rir_passes, list)
                                or not rir_passes
                                or not isinstance(rir_object.get("words"), list)
                                or rir_passes[0] != rir_object["words"]
                            ):
                                raise AuditError(
                                    "portable XRAY RIR words disagree with its first pass"
                                )
                            nested = ["passes"]
                        if len(nested) > 1:
                            raise AuditError(
                                "rir contains ambiguous containers: %s"
                                % ", ".join(nested)
                            )
                    if isinstance(rir_object, dict) and isinstance(
                        rir_object.get("passes"), list
                    ):
                        containers = rir_object["passes"]
                        detected_format = "xray-portable-rir-json"
                    elif isinstance(rir_object, dict) and isinstance(
                        rir_object.get("words"), list
                    ):
                        containers = [rir_object["words"]]
                        detected_format = "xray-portable-rir-json"
                    elif parsed.get("schema") == "170hx-xray-dstoro-projection-v1":
                        containers = [rir_object]
                        detected_format = "170hx-xray-dstoro-projection-rir-v1"
                        format_claims.update({
                            "source_schema": parsed.get("schema"),
                            "compatible_with_dstoro_backup_validator": False,
                            "row_transport_only": True,
                        })
                    else:
                        containers = [rir_object]
                        if dstoro_profile["matches_profile"]:
                            detected_format = "dstoro-backup-json"
                            format_claims["dstoro_profile"] = dstoro_profile
                            format_claims["validator_conformance_unverified"] = True
                        else:
                            detected_format = "generic-rir-json"
                            format_claims["dstoro_profile"] = dstoro_profile
                else:
                    raise AuditError(
                        "selected RIR input has no rir/rir_rows/rir_passes object"
                    )
    if not isinstance(containers, list) or not containers:
        raise AuditError("RIR passes must be a non-empty list")
    passes = []
    for pass_index, container in enumerate(containers):
        rows = _parse_row_container(container, "RIR pass %d" % (pass_index + 1))
        missing = [index for index in range(8) if index not in rows]
        extra = sorted(index for index in rows if index < 0 or index >= 8)
        if missing or extra:
            raise AuditError(
                "RIR pass %d must contain exact rows 0..7; missing=%s extra=%s"
                % (pass_index + 1, missing, extra)
            )
        passes.append([rows[index] for index in range(8)])
    mismatches = []
    if len(passes) >= 2:
        for row_index in range(8):
            values = [row_pass[row_index] for row_pass in passes]
            if len(set(values)) != 1:
                mismatches.append({
                    "row": row_index,
                    "values": [hex32(value) for value in values],
                })
    return {
        "source": "stdin" if path == "-" else path,
        "sha256": sha256_bytes(data),
        "selected_card": selected_card,
        "selected_capture": selected_capture,
        "detected_format": detected_format,
        "format_claims": format_claims,
        "words": passes[0],
        "passes": passes,
        "pass_hashes": [
            sha256_bytes(b"".join(struct.pack("<I", value) for value in row_pass))
            for row_pass in passes
        ],
        "coherent_repeated_passes": len(passes) >= 2 and not mismatches,
        "mismatches": mismatches,
    }


def dump_format_schema():
    return {
        "inputs": {
            "auto": (
                "strict UTF-8 JSON/JSONL/indexed-text structure detection; only explicit log prefixes may be skipped; raw binary is never guessed"
            ),
            "json": "unambiguous XRAY, ffcalc, xPatrik/kprinssu, ga100-fuse-re, or digest-verified Danylo backup JSON",
            "jsonl": "Danylo row/value JSONL",
            "indexed-text": "strict whole-line [NNN] value, Row N: value, N:value, or sequential bare values",
            "raw-u32le": "exact 2048-byte 512-word little-endian MAIN image",
        },
        "outputs": {
            "xray-json": {
                "lossless_rows": True,
                "sparse_allowed": True,
                "inline_loss_report": True,
                "description": "portable XRAY dump with explicit known/missing rows and provenance",
            },
            "xray-jsonl": {
                "lossless_rows": True,
                "sparse_allowed": True,
                "inline_loss_report": False,
                "description": (
                    "native XRAY row/value JSONL; explicit missing rows remain absent"
                ),
            },
            "indexed-text": {
                "lossless_rows": True,
                "sparse_allowed": True,
                "inline_loss_report": False,
                "description": "explicit [NNN] 0xXXXXXXXX lines",
            },
            "raw-u32le": {
                "lossless_rows": True,
                "requires_complete_main": True,
                "inline_loss_report": False,
                "description": "512 packed little-endian u32 words; carries no metadata",
            },
            "ffcalc-json": {
                "lossless_selected_interval": True,
                "logical_option_words_round_trip": True,
                "requires_explicit_fuseless_bounds": True,
                "requires_allow_unsafe_projection": True,
                "inline_loss_report": True,
                "description": "cmp170hx-ffcalc ff_rows/option_words profile",
            },
            "xpatrik-json": {
                "lossless_selected_interval": True,
                "requires_explicit_fuseless_bounds": True,
                "requires_allow_unsafe_projection": True,
                "inline_loss_report": True,
                "description": "CMPGlitch/kprinssu ff_dump rows/blank_rows/shadow profile",
            },
            "ga100-fuse-re-json": {
                "lossless_rows": True,
                "requires_complete_main": True,
                "inline_loss_report": True,
                "description": "fuse_dump_results-style one-card list with main_rows/rir_rows",
            },
            "dstoro-projection-json": {
                "lossless_rows": True,
                "requires_complete_main": True,
                "requires_rir": True,
                "requires_allow_unsafe_projection": True,
                "inline_loss_report": True,
                "compatible_with_dstoro_backup_validator": False,
                "description": (
                    "explicit nonconformant Danylo-shaped MAIN/RIR projection; "
                    "never represented as a validator-accepted backup"
                ),
            },
            "dstoro-jsonl": {
                "lossless_rows": True,
                "requires_complete_main": True,
                "inline_loss_report": False,
                "description": "512 strict row/value JSON objects, one per line",
            },
        },
        "invariants": [
            "unknown or failed rows are never converted to zero",
            "full-image formats refuse incomplete MAIN input",
            "FF-only formats require explicit start/end and complete interval coverage",
            "metadata-free formats are labelled lossy outside the row vector",
            "conversion never creates planning or write authority",
            (
                "actionable/public-burner-shaped projections are refused unless "
                "--allow-unsafe-projection explicitly acknowledges target-format risk"
            ),
            (
                "that acknowledgement permits serialization only and never grants "
                "planning, validation, or write authority"
            ),
        ],
        "dstoro_backup_detection": {
            "structural_profile_required_keys": sorted(_DSTORO_BACKUP_REQUIRED_KEYS),
            "current_producer_additional_keys": ["rir_observation"],
            "legacy_profile_without_rir_observation_recognized": True,
            "accepted_policies": sorted(_DSTORO_BACKUP_POLICIES),
            "known_map_hash_reference": _DSTORO_KNOWN_MAP_HASH,
            "map_hash_derivation": (
                "SHA-256 of canonical JSON (sort_keys=True, separators=(',', ':')) "
                "over the exact {core,options,status} register-map object"
            ),
            "map_hash_is_not_recomputed_without_exact_maps": True,
            "xray_projection_schema": "170hx-xray-dstoro-projection-v1",
            "xray_projection_is_never_detected_as_backup": True,
        },
    }


_CONVERSION_TOP_LEVEL_ROW_KEYS = frozenset((
    "fuse_rows", "raw_rows", "pass1", "pass2", "passes", "physical_rows",
    "main_rows", "ff_rows", "main", "rows", "rir_rows", "rir_passes", "rir",
))
_XRAY_CONVERSION_WRAPPER_KEYS = frozenset((
    "schema", "artifact_schema", "command", "generated_utc", "tool",
    "source_dump", "identity", "row_view", "provenance", "main_complete",
    "main_vector_sha256", "registers", "conversion_authority", "loss_report",
))
_UNSAFE_PROJECTION_OUTPUTS = {
    "ffcalc-json": (
        "the result is directly consumable by a public fuse planner"
    ),
    "xpatrik-json": (
        "the result is directly consumable by public planning/burning tools"
    ),
    "dstoro-projection-json": (
        "the result deliberately resembles a burner backup although the upstream "
        "validator must reject it"
    ),
}


def _physical_capture_metadata(value):
    """Retain exact controller/certification claims, but not duplicate row arrays."""
    if not isinstance(value, dict):
        return None
    result = {
        key: item for key, item in value.items() if key not in ("rows", "passes")
    }
    transaction = result.get("transaction")
    if isinstance(transaction, dict):
        result["transaction"] = {
            key: item for key, item in transaction.items() if key != "passes"
        }
    return result


def _conversion_source_metadata(capture):
    """Return exact non-row source claims for portable/extension embedding.

    Values are copied as JSON claims, not authenticated or promoted to trusted
    evidence.  Re-converting a XRAY wrapper unwraps its original metadata rather
    than recursively nesting wrappers.
    """
    if not isinstance(capture, dict):
        return {}
    portable = capture.get("artifact_schema") == "170hx-xray-portable-dump-v1"
    carried = None
    if portable and isinstance(capture.get("source_metadata"), dict):
        carried = capture["source_metadata"]
    elif isinstance(capture.get("_170hx_xray_source_metadata"), dict):
        carried = capture["_170hx_xray_source_metadata"]
    result = dict(carried) if isinstance(carried, dict) else {}
    excluded = set(_CONVERSION_TOP_LEVEL_ROW_KEYS)
    if carried is not None:
        excluded.update(_XRAY_CONVERSION_WRAPPER_KEYS)
        excluded.update(("source_metadata", "_170hx_xray_source_metadata"))
    for key, value in capture.items():
        if key == "physical_rows":
            physical_metadata = _physical_capture_metadata(value)
            if physical_metadata:
                result.setdefault("physical_rows", physical_metadata)
            continue
        if key in excluded:
            continue
        # The carried original claim wins over any conversion wrapper field.
        result.setdefault(key, value)
    return result


def _conversion_metadata_sources(capture):
    if not isinstance(capture, dict):
        return []
    result = []
    for key in ("source_metadata", "_170hx_xray_source_metadata"):
        value = capture.get(key)
        if isinstance(value, dict) and value not in result:
            result.append(value)
    result.append(capture)
    return result


_FFCALC_LOGICAL_PREFIX = "ffcalc.option_words."


def _parse_ffcalc_option_words(source, label="ffcalc option_words"):
    if not isinstance(source, dict) or "option_words" not in source:
        return {}
    option_words = source.get("option_words")
    if not isinstance(option_words, dict):
        raise AuditError("%s must be an object" % label)
    parsed = {}
    for name, raw in option_words.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise AuditError("%s contains an invalid logical key %r" % (label, name))
        value = parse_int(raw, "%s %s" % (label, name))
        if not 0 <= value <= 0xFFFFFFFF:
            raise AuditError("%s %s is outside u32 range" % (label, name))
        parsed[name] = value
    return parsed


def _conversion_register_values(capture):
    values = {}
    if not isinstance(capture, dict):
        return values

    def add(name, raw, label):
        try:
            values[str(name)] = parse_int(raw, label)
        except AuditError:
            pass

    # Process carried source claims first; normalized wrapper registers, when
    # present, then reproduce the same semantic values without hiding them.
    for source in _conversion_metadata_sources(capture):
        shadow = source.get("shadow")
        if isinstance(shadow, dict):
            for name, raw in shadow.items():
                add(name, raw, "shadow %s" % name)
        registers = source.get("registers")
        if isinstance(registers, dict):
            for name, entry in registers.items():
                raw = (
                    entry.get("raw", entry.get("value"))
                    if isinstance(entry, dict) else entry
                )
                add(name, raw, "register %s" % name)
        elif isinstance(registers, list):
            for entry in registers:
                if not isinstance(entry, dict) or "name" not in entry:
                    continue
                add(
                    entry["name"], entry.get("raw", entry.get("value")),
                    "register %s" % entry["name"],
                )
        options = source.get("options")
        if isinstance(options, list) and options and all(
            isinstance(item, dict) for item in options
        ):
            common_names = set(options[0])
            for option_pass in options[1:]:
                common_names.intersection_update(option_pass)
            for name in sorted(common_names):
                parsed_values = []
                try:
                    for pass_index, option_pass in enumerate(options, 1):
                        parsed_values.append(parse_int(
                            option_pass[name],
                            "option pass %d %s" % (pass_index, name),
                        ))
                except AuditError:
                    continue
                if len(set(parsed_values)) == 1:
                    values[str(name)] = parsed_values[0]
        for name, value in _parse_ffcalc_option_words(source).items():
            values[_FFCALC_LOGICAL_PREFIX + name] = value
        named = source.get("named")
        if isinstance(named, dict):
            for name, raw in named.items():
                if isinstance(raw, dict):
                    for nested_name, nested_raw in raw.items():
                        add(
                            nested_name, nested_raw,
                            "named %s register %s" % (name, nested_name),
                        )
                else:
                    add(name, raw, "named register %s" % name)
    return values


def _conversion_identity(loaded, bdf_override=None):
    capture = loaded.get("selected_capture")
    selected = loaded.get("selected_card")
    identity = {}

    def add(source):
        if not isinstance(source, dict):
            return
        card = source.get("card")
        if card is not None and not isinstance(card, (dict, list)):
            identity.setdefault("card_label", card)
        for key in (
            "domain_bdf", "bdf", "pci", "uuid", "sku", "desc",
            "reported_name", "device", "device_id", "vendor", "pci_vendor",
            "pci_device", "subsystem_device", "subsystem_vendor", "revision",
            "serial", "pci_id", "die", "prom_sha256",
        ):
            value = source.get(key)
            if value is not None and key not in identity:
                identity[key] = value

    # Prefer the original nested identity carried by a portable conversion;
    # fill only absent keys from normalized wrappers and card selectors.
    candidates = []
    if isinstance(capture, dict):
        candidates.extend(_conversion_metadata_sources(capture))
    if isinstance(selected, dict):
        candidates.append(selected)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        nested_identity = candidate.get("identity")
        if isinstance(nested_identity, dict):
            add(nested_identity)
        nested_card = candidate.get("card")
        if isinstance(nested_card, dict):
            if isinstance(nested_card.get("identity"), dict):
                add(nested_card["identity"])
            add(nested_card)
        add(candidate)
    if bdf_override is not None:
        if BDF_RE.fullmatch(bdf_override) is None:
            raise AuditError("--bdf must be a canonical full PCI BDF")
        identity["domain_bdf"] = bdf_override.lower()
        identity["bdf"] = bdf_override.lower()
    elif "domain_bdf" not in identity:
        for key in ("bdf", "pci"):
            value = identity.get(key)
            if isinstance(value, str) and BDF_RE.fullmatch(value):
                identity["domain_bdf"] = value.lower()
                break
    return identity


def _conversion_claim(capture, *names):
    """Return an exact source claim, including stable controller snapshots."""
    for source in _conversion_metadata_sources(capture):
        for name in names:
            if name in source:
                return source[name], True
        for container_name in ("controller", "control"):
            container = source.get(container_name)
            if not isinstance(container, dict):
                continue
            for name in names:
                if name in container:
                    return container[name], True
        physical = source.get("physical_rows")
        transaction = physical.get("transaction") if isinstance(physical, dict) else None
        if not isinstance(transaction, dict):
            continue
        observations = []
        for stage in ("entry", "exit"):
            snapshot = transaction.get(stage)
            if not isinstance(snapshot, dict):
                continue
            for name in names:
                if name in snapshot:
                    observations.append(snapshot[name])
                    break
        if observations and all(value == observations[0] for value in observations[1:]):
            return observations[0], True
    return None, False


def _require_unsafe_projection(output_format, acknowledged):
    reason = _UNSAFE_PROJECTION_OUTPUTS.get(output_format)
    if reason is not None and not acknowledged:
        raise AuditError(
            "%s is an unsafe/actionable target projection (%s); re-run with "
            "--allow-unsafe-projection to acknowledge serialization risk. The "
            "flag does not grant planning, validation, or write authority."
            % (output_format, reason)
        )


def _conversion_loss_report(
    loaded, output_format, selected_pass, represented_rows, rir,
    unsafe_projection_acknowledged, represented_option_words=None,
    rir_pairing=None,
):
    available_main = list(range(1, len(loaded["passes"]) + 1))
    represented_main = (
        list(available_main) if output_format == "xray-json" else [selected_pass]
    )
    omitted_main = [value for value in available_main if value not in represented_main]
    available_rir = list(range(1, len(rir["passes"]) + 1)) if rir else []
    if output_format == "xray-json":
        represented_rir = list(available_rir)
    elif output_format in ("ga100-fuse-re-json", "dstoro-projection-json") and rir:
        represented_rir = [1]
    else:
        represented_rir = []
    omitted_rir = [value for value in available_rir if value not in represented_rir]
    source_rows = set(loaded["passes"][selected_pass - 1])
    output_rows = set(represented_rows)
    omitted_rows = sorted(source_rows - output_rows)
    source_option_words = {}
    capture = loaded.get("selected_capture")
    for source in _conversion_metadata_sources(capture):
        source_option_words.update(_parse_ffcalc_option_words(source))
    represented_option_word_names = sorted(
        set(represented_option_words or {}) & set(source_option_words)
    )
    omitted_option_word_names = sorted(
        set(source_option_words) - set(represented_option_word_names)
    )
    losses = [
        "original byte serialization, key ordering, whitespace, and numeric spelling are not preserved"
    ]
    if omitted_main:
        losses.append("unselected MAIN pass(es) are omitted: %s" % omitted_main)
    if omitted_rows:
        losses.append(
            "%d known row(s) from selected MAIN pass are outside the target row view"
            % len(omitted_rows)
        )
    if omitted_rir:
        losses.append("RIR pass(es) are omitted: %s" % omitted_rir)
    if omitted_option_word_names:
        losses.append(
            "ffcalc option_words omitted from target semantics: %s"
            % omitted_option_word_names
        )
    if rir is None:
        losses.append("no RIR vector was available to the conversion")
    elif not isinstance(rir_pairing, dict) or not rir_pairing.get(
        "certified_same_specimen", False
    ):
        losses.append(
            "MAIN/RIR pairing is not certified as two coherent native passes "
            "from one specimen artifact"
        )
    if output_format != "xray-json":
        losses.append(
            "foreign consumers may ignore XRAY source-metadata and loss-report extensions"
        )
    if output_format in _UNSAFE_PROJECTION_OUTPUTS:
        losses.append(
            "target shape can be consumed by planning/programming software; no target validator was run"
        )
    return {
        "schema": "170hx-xray-conversion-loss-report-v1",
        "source_format": loaded.get("detected_format"),
        "output_format": output_format,
        "selected_main_pass": selected_pass,
        "available_main_passes": available_main,
        "represented_main_passes": represented_main,
        "omitted_main_passes": omitted_main,
        "source_selected_main_known_rows": len(source_rows),
        "represented_selected_main_rows": len(output_rows),
        "omitted_selected_main_rows": omitted_rows,
        "available_rir_passes": available_rir,
        "represented_rir_passes": represented_rir,
        "omitted_rir_passes": omitted_rir,
        "rir_pairing": rir_pairing,
        "source_ffcalc_option_words": sorted(source_option_words),
        "represented_ffcalc_option_words": represented_option_word_names,
        "omitted_ffcalc_option_words": omitted_option_word_names,
        "source_non_row_metadata": (
            "embedded as unauthenticated JSON claims when present"
        ),
        "actionable_target_shape": output_format in _UNSAFE_PROJECTION_OUTPUTS,
        "unsafe_projection_acknowledged": bool(
            unsafe_projection_acknowledged
            and output_format in _UNSAFE_PROJECTION_OUTPUTS
        ),
        "authority_created": False,
        "losses": losses,
    }


def _complete_main_vector(rows, output_format):
    missing = [index for index in range(MAX_FUSE_ROWS) if index not in rows]
    if missing:
        raise AuditError(
            "%s requires exact MAIN rows 0..511; missing %d rows (first=%d)"
            % (output_format, len(missing), missing[0])
        )
    return [rows[index] for index in range(MAX_FUSE_ROWS)]


def _complete_interval(rows, start, end, output_format):
    if start is None or end is None:
        raise AuditError(
            "%s requires explicit --fuseless-start and --fuseless-end"
            % output_format
        )
    if not 0 <= start <= end < MAX_FUSE_ROWS:
        raise AuditError("invalid explicit fuseless bounds")
    missing = [index for index in range(start, end + 1) if index not in rows]
    if missing:
        raise AuditError(
            "%s interval %d..%d is incomplete; missing %d rows (first=%d)"
            % (output_format, start, end, len(missing), missing[0])
        )
    return [rows[index] for index in range(start, end + 1)]


def _public_ff_record(value):
    if value == 0:
        return None
    return {
        "chain": value & 0x1F,
        "iff": (value >> 5) & 1,
        "block": (value >> 6) & 0x3FF,
        "data": (value >> 16) & 0xFFFF,
    }


def _public_ff_chains(rows, start, end):
    chains = {}
    for row in range(start, end + 1):
        record = _public_ff_record(rows[row])
        if record is None or record["iff"] or record["chain"] in (0, 0x1F):
            continue
        chains.setdefault(record["chain"], {})[record["block"]] = record["data"]
    return {
        str(chain): {
            str(block): "0x%04X" % data for block, data in sorted(blocks.items())
        }
        for chain, blocks in sorted(chains.items())
    }


def _register_value_alias(registers, *names):
    for name in names:
        if name in registers:
            return registers[name]
    return None


def _ffcalc_projection(registers):
    """Return ffcalc logical keys without copying its register/FF bit confusions."""
    words = {
        name[len(_FFCALC_LOGICAL_PREFIX):]: value
        for name, value in registers.items()
        if name.startswith(_FFCALC_LOGICAL_PREFIX)
    }
    provenance = {
        name: {
            "source_logical_key": name,
            "transform": "exact source ffcalc option_words value",
        }
        for name in words
    }

    def add(key, source_names, transform=None, note=None):
        value = _register_value_alias(registers, *source_names)
        if value is None:
            return
        words[key] = transform(value) if transform else value
        provenance[key] = {
            "source_register": next(name for name in source_names if name in registers),
            "transform": note or "whole u32",
        }

    plane_fields = (
        ("fbio_disable", ("STATUS_OPT_FBIO", "NV_FUSE_STATUS_OPT_FBIO",
                          "OPT_FBIO_DISABLE", "NV_FUSE_OPT_FBIO_DISABLE")),
        ("fbpa_disable", ("STATUS_OPT_FBPA", "NV_FUSE_STATUS_OPT_FBPA",
                          "OPT_FBPA_DISABLE", "NV_FUSE_OPT_FBPA_DISABLE")),
        ("gpc_disable", ("STATUS_OPT_GPC", "NV_FUSE_STATUS_OPT_GPC",
                         "OPT_GPC_DISABLE", "NV_FUSE_OPT_GPC_DISABLE")),
        ("pes_disable", ("OPT_PES_DISABLE", "NV_FUSE_OPT_PES_DISABLE")),
        ("fbp_disable", ("STATUS_OPT_FBP", "NV_FUSE_STATUS_OPT_FBP",
                         "OPT_FBP_DISABLE", "NV_FUSE_OPT_FBP_DISABLE")),
        ("rop_l2_disable", ("OPT_ROP_L2_DISABLE", "NV_FUSE_OPT_ROP_L2_DISABLE")),
        ("nvlink_disable", ("STATUS_OPT_NVLINK", "NV_FUSE_STATUS_OPT_NVLINK",
                            "OPT_NVLINK_DISABLE", "NV_FUSE_OPT_NVLINK_DISABLE")),
        ("sys_pipe_disable", ("STATUS_OPT_SYS_PIPE", "NV_FUSE_STATUS_OPT_SYS_PIPE",
                              "OPT_SYS_PIPE_DISABLE", "NV_FUSE_OPT_SYS_PIPE_DISABLE")),
        ("pcie_devida", ("OPT_PCIE_DEVIDA", "NV_FUSE_OPT_PCIE_DEVIDA")),
        ("pcie_devidb", ("OPT_PCIE_DEVIDB", "NV_FUSE_OPT_PCIE_DEVIDB")),
    )
    for key, names in plane_fields:
        add(key, names)
    for index in range(8):
        add(
            "tpc_gpc%d_disable" % index,
            (
                "STATUS_OPT_TPC_GPC%d" % index,
                "NV_FUSE_STATUS_OPT_TPC_GPC%d" % index,
                "OPT_TPC_GPC%d_DISABLE" % index,
                "NV_FUSE_OPT_TPC_GPC%d_DISABLE" % index,
            ),
        )

    defect_fields = (
        ("fbio_defective", ("FBIO_DEFECTIVE", "NV_FUSE_OPT_FBIO_DEFECTIVE")),
        ("fbpa_defective", ("FBPA_DEFECTIVE", "NV_FUSE_OPT_FBPA_DEFECTIVE")),
        ("gpc_defective", ("GPC_DEFECTIVE", "NV_FUSE_OPT_GPC_DEFECTIVE")),
        ("pes_defective", ("PES_DEFECTIVE", "NV_FUSE_OPT_PES_DEFECTIVE")),
        ("fbp_defective", ("FBP_DEFECTIVE", "NV_FUSE_OPT_FBP_DEFECTIVE")),
        ("rop_l2_defective", ("ROP_L2_DEFECTIVE", "NV_FUSE_OPT_ROP_L2_DEFECTIVE")),
        ("nvlink_defective", ("NVLINK_DEFECTIVE", "NV_FUSE_OPT_NVLINK_DEFECTIVE")),
        ("sys_pipe_defective", ("SYS_PIPE_DEFECTIVE", "NV_FUSE_OPT_SYS_PIPE_DEFECTIVE")),
    )
    for key, names in defect_fields:
        add(key, names)
    for index in range(8):
        add(
            "tpc_gpc%d_defective" % index,
            (
                "TPC_GPC%d_DEFECTIVE" % index,
                "NV_FUSE_OPT_TPC_GPC%d_DEFECTIVE" % index,
            ),
        )

    # ffcalc's vendored capture map conflates FF record bit positions with
    # MMIO shadow bit positions for these three fields.  Export the direct-
    # header MMIO interpretation instead and record the correction explicitly.
    add(
        "pcie_magic_gen4_dis",
        ("OPT_PCIE_MAGIC_BITS_D", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D"),
        lambda value: (value >> 25) & 1,
        "direct-header DATA[25]; ffcalc capture map's bit 3 is an FF-record coordinate",
    )
    add(
        "pcie_boot_gen23_dis",
        ("OPT_PCIE_BOOT_GEN23_DISABLE", "NV_FUSE_OPT_PCIE_BOOT_GEN23_DISABLE"),
        lambda value: value & 1,
        "direct-header DATA[0]; ffcalc capture map's bit 6 is an FF-record coordinate",
    )
    add(
        "pcie_boot_gen3_dis",
        ("OPT_PCIE_BOOT_GEN3_DISABLE", "NV_FUSE_OPT_PCIE_BOOT_GEN3_DISABLE"),
        lambda value: value & 1,
        "direct-header DATA[0]; ffcalc capture map's bit 7 is an FF-record coordinate",
    )
    add(
        "magic_d",
        ("OPT_PCIE_MAGIC_BITS_D", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D"),
    )
    return words, provenance


def _try_load_conversion_rir(
    path, card_selector, explicit_rir_path=None, input_format="auto"
):
    rir_path = explicit_rir_path or path
    if rir_path == "-":
        return None, "RIR auto-extraction is unavailable when MAIN input is stdin"
    try:
        return load_rir_words(rir_path, card_selector, input_format), None
    except (AuditError, OSError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def _conversion_rir_pairing(loaded, rir):
    """Qualify a MAIN/RIR pair without turning unsigned JSON into authority.

    Burner-shaped combined projections are permitted only when both vectors came
    from the same native helper artifact, select the same strong specimen
    identity, contain two coherent passes, and independently pass the ABI-5
    transaction-envelope verifier.  Raw or separately supplied RIR remains useful
    transport evidence, but is explicitly unqualified for that projection.
    """
    result = {
        "available": rir is not None,
        "same_artifact_sha256": False,
        "same_specimen_identity_bound": False,
        "coherent_repeated_main_passes": False,
        "coherent_repeated_rir_passes": False,
        "main_native_attestation": False,
        "rir_native_attestation": False,
        "certified_same_specimen": False,
        "errors": [],
    }
    if rir is None:
        result["errors"].append("RIR is unavailable")
        return result

    result["same_artifact_sha256"] = loaded.get("sha256") == rir.get("sha256")
    main_identities = _strong_selector_identities(loaded.get("selected_card"))
    rir_identities = _strong_selector_identities(rir.get("selected_card"))
    result["same_specimen_identity_bound"] = bool(
        main_identities and rir_identities
        and main_identities.intersection(rir_identities)
    )

    main_vectors = []
    main_passes = loaded.get("passes")
    if isinstance(main_passes, list):
        for row_pass in main_passes:
            if (
                isinstance(row_pass, dict)
                and sorted(row_pass) == list(range(XRAY_MAIN_ROWS))
            ):
                main_vectors.append(
                    [row_pass[index] for index in range(XRAY_MAIN_ROWS)]
                )
    result["coherent_repeated_main_passes"] = bool(
        len(main_vectors) == XRAY_MAX_PASSES
        and main_vectors[0] == main_vectors[1]
    )
    result["coherent_repeated_rir_passes"] = bool(
        rir.get("coherent_repeated_passes") is True
        and isinstance(rir.get("passes"), list)
        and len(rir["passes"]) == XRAY_MAX_PASSES
    )

    try:
        main_attestation = verify_native_capture_attestation(
            loaded.get("selected_capture"), loaded.get("selected_card"),
            "main_ff", main_vectors, loaded.get("sha256"),
        )
        result["main_native_attestation"] = (
            main_attestation.get("available") is True
        )
        if not result["main_native_attestation"]:
            result["errors"].extend(
                "MAIN: %s" % value
                for value in main_attestation.get("errors", [])
            )
    except (AuditError, KeyError, TypeError, ValueError) as exc:
        result["errors"].append("MAIN native attestation failed: %s" % exc)

    try:
        rir_attestation = verify_native_capture_attestation(
            rir.get("selected_capture"), rir.get("selected_card"),
            "rir", rir.get("passes"), rir.get("sha256"),
        )
        result["rir_native_attestation"] = (
            rir_attestation.get("available") is True
        )
        if not result["rir_native_attestation"]:
            result["errors"].extend(
                "RIR: %s" % value
                for value in rir_attestation.get("errors", [])
            )
    except (AuditError, KeyError, TypeError, ValueError) as exc:
        result["errors"].append("RIR native attestation failed: %s" % exc)

    for key, reason in (
        ("same_artifact_sha256", "MAIN/RIR do not share one artifact hash"),
        ("same_specimen_identity_bound", "MAIN/RIR lack a shared strong identity"),
        ("coherent_repeated_main_passes", "MAIN lacks two coherent complete passes"),
        ("coherent_repeated_rir_passes", "RIR lacks two coherent complete passes"),
        ("main_native_attestation", "MAIN native helper envelope is uncertified"),
        ("rir_native_attestation", "RIR native helper envelope is uncertified"),
    ):
        if not result[key] and reason not in result["errors"]:
            result["errors"].append(reason)
    result["certified_same_specimen"] = not result["errors"]
    return result


def command_dump_formats(args):
    document = base_document("dump-formats")
    document["artifact_schema"] = "170hx-xray-dump-formats-v1"
    document["formats"] = dump_format_schema()
    emit_json(document, args.output)
    return 0


def parse_kernel_read_allowlist_text(text):
    match = re.search(
        r"static\s+const\s+u32\s+xray_read_offsets\s*\[\s*\]\s*=\s*\{(.*?)\n\s*\};",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AuditError("kernel source has no xray_read_offsets array")
    values = [int(raw, 16) for raw in re.findall(r"0x[0-9a-fA-F]+", match.group(1))]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    return values, duplicates


def registry_provenance_census(header_schema=None):
    """Describe what the registry proves, and what remains uninterpreted.

    This deliberately treats an address match as identity corroboration only.
    It never promotes a header register into the live-read allowlist.
    """
    safe = list(SAFE_READ_REGISTERS)
    generic_source = "curated GA100 direct-header/local-map audit"
    generic_source_names = sorted(
        item["name"] for item in CURATED_REGISTERS
        if item.get("source") == generic_source
    )
    source_class_counts = {
        "hash_bound_generated_manifest": sum(
            "sha256=" in item.get("source", "") for item in CURATED_REGISTERS
        ),
        "file_line_anchor": sum(
            re.search(r"\.[A-Za-z0-9_]+:\d+", item.get("source", "")) is not None
            for item in CURATED_REGISTERS
        ),
        "generic_curated_audit": len(generic_source_names),
    }
    source_class_counts["descriptive_or_formula_anchor"] = (
        len(CURATED_REGISTERS) - sum(source_class_counts.values())
    )
    census = {
        "catalogued_entries": len(CURATED_REGISTERS),
        "catalogued_unique_addresses": len(REGISTER_BY_ADDRESS),
        "safe_entries": len(safe),
        "safe_unique_addresses": len(SAFE_READ_OFFSETS),
        "stateful_or_unsafe_entries": len(CURATED_REGISTERS) - len(safe),
        "safe_with_fields": sum(bool(item["fields"]) for item in safe),
        "safe_raw_word_only": sum(not item["fields"] for item in safe),
        "missing_identity_confidence": sorted(
            item["name"] for item in CURATED_REGISTERS
            if item.get("identity_confidence_percent") is None
        ),
        "missing_interpretation_confidence": sorted(
            item["name"] for item in CURATED_REGISTERS
            if item.get("interpretation_confidence_percent") is None
        ),
        "missing_source_anchor": sorted(
            item["name"] for item in CURATED_REGISTERS
            if not isinstance(item.get("source"), str) or not item["source"].strip()
        ),
        "generic_source_anchor": generic_source_names,
        "source_anchor_class_counts": source_class_counts,
        "planning_enabled_entries": sorted(
            item["name"] for item in CURATED_REGISTERS
            if item.get("planning_allowed") is True
        ),
        "access_effect_counts": {
            effect: sum(item["access_effect"] == effect for item in CURATED_REGISTERS)
            for effect in ("pure_read", "read_to_advance")
        },
        "completeness": {
            "address_policy_metadata_complete": True,
            "identity_confidence_complete": all(
                item.get("identity_confidence_percent") is not None
                for item in CURATED_REGISTERS
            ),
            "specific_source_anchor_complete": not generic_source_names,
            "interpretation_confidence_complete": all(
                item.get("interpretation_confidence_percent") is not None
                for item in CURATED_REGISTERS
            ),
            "safe_field_decode_complete": all(item["fields"] for item in safe),
            "planning_authority_complete": False,
        },
        "boundary": (
            "registry/header agreement establishes mechanical address/field provenance; "
            "it does not establish runtime behavior, side-effect freedom on every GA100, "
            "or physical fuse coordinates"
        ),
    }
    if header_schema is None:
        census["direct_header_comparison"] = {
            "available": False,
            "reason": "no compatible direct header supplied or discovered",
        }
        return census

    header_registers = header_schema.get("registers", [])
    header_by_address = {}
    for item in header_registers:
        header_by_address.setdefault(item["address_u32"], []).append(item)
    safe_in_header = sorted(SAFE_READ_OFFSETS.intersection(header_by_address))
    safe_outside_header = sorted(SAFE_READ_OFFSETS.difference(header_by_address))
    header_outside_safe = sorted(set(header_by_address).difference(SAFE_READ_OFFSETS))
    exact_name_matches = []
    address_only_matches = []
    field_conflicts = []
    field_unconfirmed = []
    field_confirmed = []
    for register in safe:
        candidates = header_by_address.get(register["address"], [])
        if not candidates:
            for field in register["fields"]:
                field_unconfirmed.append({
                    "register": register["name"],
                    "field": field["name"],
                    "address": hex32(register["address"]),
                    "reason": "register address absent from this direct header",
                })
            continue
        accepted_names = set((register["name"],) + tuple(register.get("aliases", ())))
        accepted_names.update(
            "NV_FUSE_" + name for name in tuple(accepted_names)
            if not name.startswith("NV_FUSE_")
        )
        named = [item for item in candidates if item["name"] in accepted_names]
        if not named:
            address_only_matches.append({
                "register": register["name"],
                "address": hex32(register["address"]),
                "header_names": sorted(item["name"] for item in candidates),
            })
            for field in register["fields"]:
                field_unconfirmed.append({
                    "register": register["name"],
                    "field": field["name"],
                    "address": hex32(register["address"]),
                    "reason": "exact register name absent at matching address",
                })
            continue
        header_register = named[0]
        exact_name_matches.append({
            "register": register["name"],
            "header_register": header_register["name"],
            "address": hex32(register["address"]),
        })
        header_fields = {item["name"]: item for item in header_register["fields"]}
        for field in register["fields"]:
            observed = header_fields.get(field["name"])
            if observed is None:
                observed = header_fields.get(field["name"] + "_DATA")
            if observed is None:
                field_unconfirmed.append({
                    "register": register["name"],
                    "field": field["name"],
                    "address": hex32(register["address"]),
                    "reason": "field name absent from exact header register",
                })
            elif (field["msb"], field["lsb"]) != (
                    observed["msb"], observed["lsb"]):
                field_conflicts.append({
                    "register": register["name"],
                    "field": field["name"],
                    "address": hex32(register["address"]),
                    "registry": "%d:%d" % (field["msb"], field["lsb"]),
                    "header": "%d:%d" % (observed["msb"], observed["lsb"]),
                })
            else:
                field_confirmed.append({
                    "register": register["name"],
                    "field": field["name"],
                    "address": hex32(register["address"]),
                    "range": "%d:%d" % (field["msb"], field["lsb"]),
                    "header_field": observed["name"],
                })
    census["direct_header_comparison"] = {
        "available": True,
        "source": header_schema.get("source"),
        "sha256": header_schema.get("sha256"),
        "header_register_count": len(header_registers),
        "safe_address_matches": len(safe_in_header),
        "safe_addresses_outside_this_header": [hex32(value) for value in safe_outside_header],
        "header_addresses_not_allowlisted": [hex32(value) for value in header_outside_safe],
        "exact_name_address_matches": exact_name_matches,
        "address_only_matches": address_only_matches,
        "confirmed_curated_fields": field_confirmed,
        "curated_fields_not_confirmed_by_exact_name": field_unconfirmed,
        "field_conflicts": field_conflicts,
        "permission_boundary": (
            "header 4R discovery is evidence only and never expands SAFE_READ_OFFSETS"
        ),
    }
    return census


def command_registry_audit(args):
    path = Path(args.kernel_source)
    data = path.read_bytes()
    values, duplicates = parse_kernel_read_allowlist_text(
        data.decode("utf-8", errors="strict")
    )
    kernel_offsets = set(values)
    python_offsets = set(SAFE_READ_OFFSETS)
    python_only = sorted(python_offsets - kernel_offsets)
    kernel_only = sorted(kernel_offsets - python_offsets)
    document = base_document("registry-audit")
    document["artifact_schema"] = "170hx-xray-register-registry-audit-v2"
    document["kernel_source"] = {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "entry_count": len(values),
        "unique_count": len(kernel_offsets),
    }
    document["python_registry"] = {
        "catalogued_count": len(CURATED_REGISTERS),
        "kernel_safe_read_count": len(SAFE_READ_REGISTERS),
        "generated_shadow_manifest": {
            "hashes": dict(GA100_SHADOW_MANIFEST_HASHES),
            "summary": dict(GA100_SHADOW_MANIFEST_SUMMARY),
        },
        "profiles": {
            name: len(registers_for_profile(name)) for name in REGISTER_PROFILES
        },
        "stateful_excluded": [
            {
                "name": item["name"],
                "address": hex32(item["address"]),
                "access_effect": item["access_effect"],
                "profile": item["profile"],
            }
            for item in CURATED_REGISTERS if not item["capture_safe"]
        ],
    }
    document["read_target_identity_scope"] = {
        "default_discovery_device_ids": [
            "0x%04x" % value for value in sorted(DEFAULT_DEVICE_IDS)
        ],
        "accepted_read_attestation_device_ids": [
            "0x%04x" % value for value in sorted(GA100_READ_TARGET_DEVICE_IDS)
        ],
        "post_conversion_20b5_live_validation": "required",
        "post_conversion_20b5_bar_read": "supported by allowlisted READ32",
        "post_conversion_20b5_controller_default": (
            "kernel rejects MAIN/RIR with EOPNOTSUPP unless explicitly loaded "
            "with allow_a100_controller=1"
        ),
    }
    document["register_provenance"] = curated_registry_schema()
    document["comparison"] = {
        "exact_set_match": not duplicates and not python_only and not kernel_only,
        "duplicate_kernel_offsets": [hex32(value) for value in duplicates],
        "python_only": [hex32(value) for value in python_only],
        "kernel_only": [hex32(value) for value in kernel_only],
        "i1500_data_excluded_from_default": (
            REGISTER_BY_NAME["I1500_DATA"]["address"] not in python_offsets
            and REGISTER_BY_NAME["I1500_DATA"]["address"] not in kernel_offsets
        ),
    }
    header_path = getattr(args, "header", None) or default_header_path()
    header_schema = parse_header_file(header_path) if header_path else None
    document["provenance_census"] = registry_provenance_census(header_schema)
    header_conflicts = document["provenance_census"][
        "direct_header_comparison"
    ].get("field_conflicts", [])
    document["status"] = (
        "PASS" if document["comparison"]["exact_set_match"]
        and document["comparison"]["i1500_data_excluded_from_default"]
        and not header_conflicts
        else "FAIL"
    )
    document["status_scope"] = (
        "PASS means Python/kernel safe-address parity, stateful exclusion and no "
        "direct-header field conflicts; it does not mean semantic or planning completeness"
    )
    emit_json(document, args.output)
    return 0 if document["status"] == "PASS" else 2


def command_convert_dump(args):
    if (args.fuseless_start is None) != (args.fuseless_end is None):
        raise AuditError("--fuseless-start and --fuseless-end must be supplied together")
    unsafe_projection_acknowledged = bool(
        getattr(args, "allow_unsafe_projection", False)
    )
    _require_unsafe_projection(
        args.output_format, unsafe_projection_acknowledged
    )
    loaded = load_row_passes(args.input, args.card, args.input_format)
    if not 1 <= args.pass_index <= len(loaded["passes"]):
        raise AuditError(
            "--pass-index %d is out of range for %d available pass(es)"
            % (args.pass_index, len(loaded["passes"]))
        )
    rows = loaded["passes"][args.pass_index - 1]
    identity = _conversion_identity(loaded, args.bdf)
    capture = loaded.get("selected_capture")
    source_metadata = _conversion_source_metadata(capture)
    selected_card = loaded.get("selected_card")
    source_collection_metadata = (
        selected_card.get("root_capture")
        if isinstance(selected_card, dict)
        and isinstance(selected_card.get("root_capture"), dict)
        else None
    )
    registers = _conversion_register_values(capture)
    rir, rir_error = _try_load_conversion_rir(
        args.input, args.card, args.rir_input, args.rir_input_format
    )
    rir_pairing = _conversion_rir_pairing(loaded, rir)
    output_format = args.output_format
    source = {
        "path": loaded["source"],
        "sha256": loaded["sha256"],
        "detected_format": loaded.get("detected_format"),
        "selected_pass": args.pass_index,
        "available_passes": len(loaded["passes"]),
        "known_rows": len(rows),
        "missing_rows": [index for index in range(MAX_FUSE_ROWS) if index not in rows],
        "format_claims": loaded.get("format_claims", {}),
    }

    if output_format == "xray-json":
        document = base_document("convert-dump")
        document["artifact_schema"] = "170hx-xray-portable-dump-v1"
        document["source_dump"] = source
        # Every retained value is an exact but unauthenticated source claim.
        # Row vectors are normalized below; nested controller/certification
        # metadata is retained without the duplicate row arrays.
        document["source_metadata"] = source_metadata
        if source_collection_metadata is not None:
            document["source_collection_metadata"] = source_collection_metadata
        document["identity"] = identity
        document["row_view"] = args.row_view
        document["provenance"] = args.provenance
        document["main_rows"] = {str(index): hex32(rows[index]) for index in sorted(rows)}
        document["passes"] = [
            {str(index): hex32(row_pass[index]) for index in sorted(row_pass)}
            for row_pass in loaded["passes"]
        ]
        document["main_complete"] = len(rows) == MAX_FUSE_ROWS
        document["main_vector_sha256"] = (
            _packed_u32_pass_sha256([rows[index] for index in range(MAX_FUSE_ROWS)])
            if len(rows) == MAX_FUSE_ROWS else None
        )
        document["registers"] = {
            name: hex32(value) for name, value in sorted(registers.items())
        }
        document["rir"] = {
            "available": rir is not None,
            "error": rir_error,
            "words": [hex32(value) for value in rir["words"]] if rir else None,
            "passes": (
                [[hex32(value) for value in row_pass] for row_pass in rir["passes"]]
                if rir else None
            ),
            "coherent_repeated_passes": (
                rir["coherent_repeated_passes"] if rir else None
            ),
            "source_sha256": rir["sha256"] if rir else None,
            "pairing": rir_pairing,
        }
        document["conversion_authority"] = (
            "row-preserving format conversion only; no planning/write authority"
        )
        document["loss_report"] = _conversion_loss_report(
            loaded, output_format, args.pass_index, sorted(rows), rir,
            unsafe_projection_acknowledged,
            rir_pairing=rir_pairing,
        )
        emit_json(document, args.output)
        return 0

    if output_format == "xray-jsonl":
        text = "".join(
            _strict_json_dumps(
                {
                    "row": index,
                    "value": hex32(rows[index]),
                    "format": "170hx-xray-row-v1",
                },
                sort_keys=True, separators=(",", ":"),
            ) + "\n"
            for index in sorted(rows)
        )
        emit_text(text, args.output)
        return 0

    if output_format == "indexed-text":
        text = "".join(
            "[%03d] %s\n" % (index, hex32(rows[index])) for index in sorted(rows)
        )
        emit_text(text, args.output)
        return 0

    if output_format == "raw-u32le":
        vector = _complete_main_vector(rows, output_format)
        emit_bytes(struct.pack("<%dI" % MAX_FUSE_ROWS, *vector), args.output)
        return 0

    if output_format == "ffcalc-json":
        _complete_interval(
            rows, args.fuseless_start, args.fuseless_end, output_format
        )
        ffcalc_words, ffcalc_provenance = _ffcalc_projection(registers)
        document = {
            "sku": args.sku,
            "ff_rows": {
                str(index): hex32(rows[index])
                for index in range(args.fuseless_start, args.fuseless_end + 1)
            },
            "option_words": {
                name: hex32(value) for name, value in sorted(ffcalc_words.items())
            },
            "info": {
                "secured_burn_feature_enable": hex32(registers["SECURED_BURN_FEATURE_ENABLE"])
                if "SECURED_BURN_FEATURE_ENABLE" in registers else None,
            },
            "source": "%s sha256=%s converted-by=170HX-XRAY-%s"
            % (loaded["source"], loaded["sha256"], TOOL_VERSION),
            "read_only": True,
            "_170hx_xray_conversion": source,
            "_170hx_xray_source_metadata": source_metadata,
            "_170hx_xray_field_provenance": ffcalc_provenance,
            "_170hx_xray_semantic_corrections": [
                "ffcalc logical lower-case keys are emitted instead of XRAY register names",
                "PCIe MMIO DATA bits follow direct-header coordinates, not FF-record bit coordinates",
                "disable_gen3_speed_bit1/bit9 are omitted because one effective shadow bit cannot identify which FF candidate controls it",
            ],
            "loss_report": _conversion_loss_report(
                loaded, output_format, args.pass_index,
                range(args.fuseless_start, args.fuseless_end + 1), rir,
                unsafe_projection_acknowledged,
                represented_option_words=ffcalc_words,
                rir_pairing=rir_pairing,
            ),
        }
        if source_collection_metadata is not None:
            document["_170hx_xray_source_collection_metadata"] = (
                source_collection_metadata
            )
        for key in ("domain_bdf", "bdf", "uuid"):
            if key in identity:
                document["bdf" if key == "domain_bdf" else key] = identity[key]
        emit_json(document, args.output)
        return 0

    if output_format == "xpatrik-json":
        _complete_interval(
            rows, args.fuseless_start, args.fuseless_end, output_format
        )
        output_rows = []
        blanks = []
        for index in range(args.fuseless_start, args.fuseless_end + 1):
            value = rows[index]
            record = _public_ff_record(value)
            output_rows.append({
                "row": index,
                "raw": "0x%08X" % value,
                "record": record,
            })
            if value == 0:
                blanks.append(index)
        field_provenance = {}
        fusectrl_raw, fusectrl_exact = _conversion_claim(
            capture, "fusectrl", "FUSECTRL", "CTRL"
        )
        if not fusectrl_exact and "FUSECTRL" in registers:
            fusectrl_raw = registers["FUSECTRL"]
            field_provenance["fusectrl"] = "normalized source register"
        elif fusectrl_exact:
            field_provenance["fusectrl"] = "exact source controller claim"
        try:
            fusectrl_value = parse_int(fusectrl_raw, "source FUSECTRL")
        except AuditError:
            fusectrl_value = None
        crc_raw, crc_exact = _conversion_claim(
            capture, "crc_status", "CRC_STATUS", "CRC"
        )
        if not crc_exact and "CRC_STATUS" in registers:
            crc_raw = registers["CRC_STATUS"]
            field_provenance["crc_status"] = "normalized source register"
        elif crc_exact:
            field_provenance["crc_status"] = "exact source controller claim"
        try:
            crc_value = parse_int(crc_raw, "source CRC_STATUS")
        except AuditError:
            crc_value = None
        controller_idle, idle_exact = _conversion_claim(
            capture, "controller_idle"
        )
        if not isinstance(controller_idle, bool):
            controller_idle = (
                fusectrl_value == 0xE0040000
                if fusectrl_value is not None else None
            )
            if controller_idle is not None:
                field_provenance["controller_idle"] = (
                    "derived from exact completed-idle FUSECTRL value 0xe0040000"
                )
        elif idle_exact:
            field_provenance["controller_idle"] = "exact source claim"
        read_only, read_only_exact = _conversion_claim(capture, "read_only")
        if not isinstance(read_only, bool):
            read_only = None
        elif read_only_exact:
            field_provenance["read_only"] = "exact source claim"
        shadow_only, shadow_only_exact = _conversion_claim(capture, "shadow_only")
        if not isinstance(shadow_only, bool):
            shadow_only = None
        elif shadow_only_exact:
            field_provenance["shadow_only"] = "exact source claim"
        read_errors, read_errors_exact = _conversion_claim(capture, "read_errors")
        if not isinstance(read_errors, list):
            read_errors = None
        elif read_errors_exact:
            field_provenance["read_errors"] = "exact source claim"
        document = {
            "bdf": identity.get("domain_bdf", identity.get("bdf")),
            "read_only": read_only,
            "shadow_only": shadow_only,
            "fusectrl": "0x%08X" % fusectrl_value
            if fusectrl_value is not None else None,
            "crc_status": "0x%02X" % crc_value
            if crc_value is not None else None,
            "controller_idle": controller_idle,
            "shadow": {
                name: "0x%08X" % value
                for name, value in sorted(registers.items())
            },
            "rows": output_rows,
            "blank_rows": blanks,
            "read_errors": read_errors,
            "chains": _public_ff_chains(
                rows, args.fuseless_start, args.fuseless_end
            ),
            "_170hx_xray_conversion": source,
            "_170hx_xray_source_metadata": source_metadata,
            "_170hx_xray_field_provenance": field_provenance,
            "loss_report": _conversion_loss_report(
                loaded, output_format, args.pass_index,
                range(args.fuseless_start, args.fuseless_end + 1), rir,
                unsafe_projection_acknowledged,
                rir_pairing=rir_pairing,
            ),
        }
        if source_collection_metadata is not None:
            document["_170hx_xray_source_collection_metadata"] = (
                source_collection_metadata
            )
        emit_json(document, args.output)
        return 0

    if output_format == "ga100-fuse-re-json":
        vector = _complete_main_vector(rows, output_format)
        document = [{
            "card": identity.get(
                "card_label",
                identity.get("reported_name", identity.get("uuid", "converted")),
            ),
            "pci": identity.get("domain_bdf", identity.get("bdf")),
            "desc": identity.get("desc", "170HX-XRAY converted complete MAIN image"),
            "rir_rows": (
                {str(index): value for index, value in enumerate(rir["words"])}
                if rir else {}
            ),
            "main_rows": {str(index): value for index, value in enumerate(vector)},
            "_170hx_xray_conversion": source,
            "_170hx_xray_source_metadata": source_metadata,
            "loss_report": _conversion_loss_report(
                loaded, output_format, args.pass_index, range(MAX_FUSE_ROWS), rir,
                unsafe_projection_acknowledged,
                rir_pairing=rir_pairing,
            ),
        }]
        if source_collection_metadata is not None:
            document[0]["_170hx_xray_source_collection_metadata"] = (
                source_collection_metadata
            )
        emit_json(document, args.output)
        return 0

    if output_format == "dstoro-projection-json":
        vector = _complete_main_vector(rows, output_format)
        if rir is None:
            raise AuditError(
                "dstoro-projection-json requires exact embedded RIR or --rir-input: %s"
                % rir_error
            )
        if rir_pairing.get("certified_same_specimen") is not True:
            raise AuditError(
                "dstoro-projection-json requires certified same-artifact, "
                "same-specimen, two-pass native MAIN/RIR: %s"
                % "; ".join(rir_pairing.get("errors", []))
            )
        original = source_metadata
        document = {
            "schema": "170hx-xray-dstoro-projection-v1",
            "compatible_with_dstoro_backup_validator": False,
            "nonconformance_reasons": [
                "source register-map hash and exact accepted policy are not synthesized",
                "source identity serial/pci_id and shadow_matches_main cannot be invented",
                "projection is row transport only and carries no programming authority",
            ],
            "utc": utc_now(),
            "bdf": identity.get("domain_bdf", identity.get("bdf")),
            "row_projection_complete": True,
            "main": vector,
            "rir": list(rir["words"]),
            "main_hash": _packed_u32_pass_sha256(vector),
            "rir_hash": _packed_u32_pass_sha256(rir["words"]),
            "control": original.get("control", {}),
            "identity": original.get("identity", identity),
            "named": original.get("named", {}),
            "policy": "nonconformant row projection; do not pass to a burner",
            "collector_sha256": tool_sha256(),
            "source": source,
            "_170hx_xray_source_metadata": source_metadata,
            "loss_report": _conversion_loss_report(
                loaded, output_format, args.pass_index, range(MAX_FUSE_ROWS), rir,
                unsafe_projection_acknowledged,
                rir_pairing=rir_pairing,
            ),
        }
        if source_collection_metadata is not None:
            document["_170hx_xray_source_collection_metadata"] = (
                source_collection_metadata
            )
        emit_json(document, args.output)
        return 0

    if output_format == "dstoro-jsonl":
        vector = _complete_main_vector(rows, output_format)
        text = "".join(
            _strict_json_dumps({"row": index, "value": value}, sort_keys=True) + "\n"
            for index, value in enumerate(vector)
        )
        emit_text(text, args.output)
        return 0

    raise AuditError("unsupported dump output format: %s" % output_format)


def _packed_u32_pass_sha256(values):
    return sha256_bytes(b"".join(struct.pack("<I", value) for value in values))


def _capture_identity_tuple(identity, label, errors):
    """Return a strict helper identity tuple, recording all validation errors."""
    if not isinstance(identity, dict):
        errors.append("%s identity is not an object" % label)
        return None
    bdf = identity.get("domain_bdf")
    if not isinstance(bdf, str) or BDF_RE.fullmatch(bdf) is None:
        errors.append("%s identity lacks a canonical full BDF" % label)
        bdf_value = None
    else:
        bdf_value = bdf.lower()
        if bdf != bdf_value:
            errors.append("%s identity BDF is not canonical lowercase" % label)
    parsed = []
    for key, maximum in (
        ("generation", 0xFFFFFFFFFFFFFFFF),
        ("vendor", 0xFFFF),
        ("device", 0xFFFF),
        ("subsystem_vendor", 0xFFFF),
        ("subsystem_device", 0xFFFF),
        ("revision", 0xFF),
    ):
        try:
            value = parse_int(identity.get(key), "%s identity %s" % (label, key))
        except AuditError:
            errors.append("%s identity %s is absent or invalid" % (label, key))
            value = None
        if value is not None and not 0 <= value <= maximum:
            errors.append("%s identity %s is out of range" % (label, key))
            value = None
        parsed.append(value)
    generation, vendor, device, _subvendor, _subdevice, _revision = parsed
    if generation == 0:
        errors.append("%s helper generation is zero" % label)
    if vendor != 0x10DE or device not in GA100_READ_TARGET_DEVICE_IDS:
        errors.append("%s identity is outside the GA100 target allowlist" % label)
    if bdf_value is None or any(value is None for value in parsed):
        return None
    return (bdf_value,) + tuple(parsed)


def _parse_exact_native_passes(value, row_count, label, errors):
    if not isinstance(value, list) or len(value) != XRAY_MAX_PASSES:
        errors.append("%s must contain exactly two passes" % label)
        return None
    result = []
    for pass_index, row_pass in enumerate(value):
        if not isinstance(row_pass, list) or len(row_pass) != row_count:
            errors.append(
                "%s pass %d must contain exactly %d rows"
                % (label, pass_index + 1, row_count)
            )
            return None
        parsed = []
        for row_index, raw in enumerate(row_pass):
            try:
                word = parse_int(
                    raw, "%s pass %d row %d" % (label, pass_index + 1, row_index)
                )
            except AuditError:
                errors.append(
                    "%s pass %d row %d is invalid"
                    % (label, pass_index + 1, row_index)
                )
                return None
            if not 0 <= word <= 0xFFFFFFFF:
                errors.append(
                    "%s pass %d row %d is outside u32"
                    % (label, pass_index + 1, row_index)
                )
                return None
            parsed.append(word)
        result.append(parsed)
    return result


def _parse_native_snapshot(value, label, errors):
    if not isinstance(value, dict):
        errors.append("%s controller snapshot is not an object" % label)
        return None
    result = {}
    for name, _ctype in XrayControllerSnapshot._fields_:
        try:
            word = parse_int(value.get(name), "%s %s" % (label, name))
        except AuditError:
            errors.append("%s snapshot field %s is absent or invalid" % (label, name))
            return None
        if not 0 <= word <= 0xFFFFFFFF:
            errors.append("%s snapshot field %s is outside u32" % (label, name))
            return None
        result[name] = word
    return result


def verify_native_capture_attestation(capture, selected_metadata, kind,
                                      expected_passes, artifact_sha256):
    """Verify a native capture envelope without claiming file authenticity.

    The checks independently recompute row hashes, coherence, helper counters,
    identity binding, controller entry/exit invariants and restoration.  A JSON
    file is still forgeable; callers must bind ``artifact_sha256`` to an
    independently recorded acquisition hash when evidentiary authenticity is
    required.
    """
    errors = []
    warnings = []
    row_count = XRAY_MAIN_ROWS if kind == "main_ff" else XRAY_RIR_ROWS
    object_key = "physical_rows" if kind == "main_ff" else "rir_rows"
    response_kind = XRAY_DUMP_MAIN if kind == "main_ff" else XRAY_DUMP_RIR
    expected_flags = (
        XRAY_DUMP_F_CLEAN_CRC | XRAY_DUMP_F_NO_SW_IFF
        if kind == "main_ff" else XRAY_DUMP_F_CLEAN_CRC
    )
    expected_writes = 2049 if kind == "main_ff" else 35
    expected_reads = row_count * XRAY_MAX_PASSES

    if not isinstance(capture, dict):
        return {
            "available": False,
            "kind": kind,
            "errors": ["input is not a selected native capture object"],
            "warnings": [],
            "artifact_sha256": artifact_sha256,
            "authenticity": "not established by unsigned JSON",
        }
    root = selected_metadata.get("root_capture", {}) if isinstance(
        selected_metadata, dict
    ) else {}
    if not isinstance(root, dict):
        root = {}
    if root.get("schema") != SCHEMA_VERSION:
        errors.append("native root schema is absent or unsupported")
    if root.get("command") not in ("capture", "live"):
        errors.append("native root command is not capture/live")
    tool = root.get("tool")
    if not isinstance(tool, dict) or tool.get("name") not in (
            LEGACY_TOOL_NAME, TOOL_NAME):
        errors.append("native root tool identity is absent or mismatched")
    else:
        if not isinstance(tool.get("version"), str):
            errors.append("native root tool version is absent")
        source_hash = tool.get("sha256")
        if not isinstance(source_hash, str) or re.fullmatch(
            r"[0-9a-f]{64}", source_hash
        ) is None:
            errors.append("native root source SHA-256 is absent or malformed")

    outer_card = capture.get("card")
    if not isinstance(outer_card, dict):
        errors.append("native capture card identity is absent")
        outer_card = {}
    helper_identity = outer_card.get("xray_helper_identity")
    helper_identity_tuple = _capture_identity_tuple(
        helper_identity, "capture helper", errors
    )
    card_bdf = outer_card.get("domain_bdf")
    if helper_identity_tuple is not None and card_bdf != helper_identity_tuple[0]:
        errors.append("capture card BDF disagrees with helper identity")

    outer = capture.get(object_key)
    if not isinstance(outer, dict):
        errors.append("native capture lacks %s" % object_key)
        outer = {}
    if outer.get("available") is not True:
        errors.append("native %s is not marked available" % object_key)
    if outer.get("coherent") is not True:
        errors.append("native %s is not marked coherent" % object_key)
    if kind == "main_ff" and outer.get("view") not in (
        "physical", "locked_controller_main"
    ):
        errors.append("native MAIN view is not a recognized controller view")
    if kind == "main_ff" and outer.get("view") == "physical":
        warnings.append(
            "historical view label 'physical' is reinterpreted as a locked controller "
            "MAIN view; its active IFF interval is state-qualified, not immutable OTP"
        )

    outer_passes = _parse_exact_native_passes(
        outer.get("passes"), row_count, "native %s" % object_key, errors
    )
    supplied_passes = expected_passes
    if (
        outer_passes is not None
        and isinstance(supplied_passes, list)
        and outer_passes != supplied_passes
    ):
        errors.append("decoded input passes disagree with native capture passes")
    if outer_passes is not None and outer_passes[0] != outer_passes[1]:
        errors.append("native row passes are not coherent")
    computed_hashes = (
        [_packed_u32_pass_sha256(row_pass) for row_pass in outer_passes]
        if outer_passes is not None else []
    )
    if outer.get("pass_hashes") != computed_hashes:
        errors.append("native outer pass hashes do not match row data")
    if outer_passes is not None:
        raw_outer_rows = outer.get("rows")
        if not isinstance(raw_outer_rows, list):
            outer_rows = None
        else:
            try:
                outer_rows = [
                    parse_int(value, "native outer row %d" % index)
                    for index, value in enumerate(raw_outer_rows)
                ]
            except AuditError:
                outer_rows = None
        if outer_rows != outer_passes[0]:
            errors.append("native outer rows do not equal the first pass")

    transaction = outer.get("transaction")
    if not isinstance(transaction, dict):
        errors.append("native transaction envelope is absent")
        transaction = {}
    transaction_identity = _capture_identity_tuple(
        transaction.get("identity"), "transaction", errors
    )
    if (
        helper_identity_tuple is not None
        and transaction_identity is not None
        and helper_identity_tuple != transaction_identity
    ):
        errors.append("transaction identity disagrees with capture helper identity")
    exact_scalars = {
        "helper_struct_size": ctypes.sizeof(XrayDump),
        "kind": kind,
        "response_kind": response_kind,
        "flags": hex32(expected_flags),
        "status": 0,
        "requested_passes": XRAY_MAX_PASSES,
        "completed_passes": XRAY_MAX_PASSES,
        "row_count": row_count,
        "coherent": True,
        "kernel_reported_coherent": True,
        "failed_pass": None,
        "failed_row": None,
        "operational_writes_issued": expected_writes,
        "cmd_read_commands_issued": expected_reads,
        "completed_data_reads_lower_bound": expected_reads,
        "counts_complete": True,
        "address_restored": True,
        "debug_restored": True,
        "driver_bound_at_entry": False,
        "protocol_valid": True,
        "protocol_errors": [],
        "recovered_from_last": False,
        "transport_error": None,
        "certified_by_helper": True,
        "otp_commands": 0,
    }
    if transaction.get("helper_abi") not in (
            LEGACY_HELPER_ABI, XRAY_HELPER_ABI):
        errors.append("native transaction helper_abi invariant failed")
    for key, expected in exact_scalars.items():
        if transaction.get(key) != expected:
            errors.append("native transaction %s invariant failed" % key)
    transaction_passes = _parse_exact_native_passes(
        transaction.get("passes"), row_count, "native transaction", errors
    )
    if outer_passes is not None and transaction_passes != outer_passes:
        errors.append("native outer and transaction row passes disagree")
    if transaction.get("pass_hashes") != computed_hashes:
        errors.append("native transaction pass hashes do not match row data")

    entry = _parse_native_snapshot(transaction.get("entry"), "entry", errors)
    exit_snapshot = _parse_native_snapshot(
        transaction.get("exit"), "exit", errors
    )
    if entry is not None and exit_snapshot is not None:
        stable_fields = tuple(name for name, _ctype in XrayControllerSnapshot._fields_)
        for name in stable_fields:
            if name == "fusectrl":
                continue
            if entry[name] != exit_snapshot[name]:
                errors.append("native entry/exit %s changed" % name)
        # GA100's evidenced quiescent value includes the three completion bits,
        # not merely CMD_NONE plus the five-bit IDLE state.  Accepting
        # 0x00040000 would certify a controller whose SENSE/READ/WRITE completion
        # envelope is absent.
        ctrl_exact_idle = 0xE0040000
        for label, values in (("entry", entry), ("exit", exit_snapshot)):
            if values["pmc_boot0"] in (0, 0xFFFFFFFF):
                errors.append("native %s PMC_BOOT0 is invalid" % label)
            if values["fusectrl"] != ctrl_exact_idle:
                errors.append(
                    "native %s FUSECTRL is not exact completed idle 0xe0040000"
                    % label
                )
            if values["debugctrl"] != 0:
                errors.append("native %s DEBUGCTRL is not restored" % label)
            if values["iff_sw_fusing"] & 1:
                errors.append("native %s IFF_SW_FUSING is active" % label)
            if values["crc_status"] & 0x77 != 0x55:
                errors.append("native %s CRC_STATUS is not clean" % label)
            if kind == "main_ff" and values["en_sw_override"] & 1:
                errors.append("native %s software override is active" % label)
    certification = outer.get("certification")
    if not isinstance(certification, dict):
        errors.append("native outer certification is absent")
        certification = {}
    serialized_transaction = certification.get(
        "two_passes_in_one_helper_serialized_ioctl_transaction"
    )
    if serialized_transaction is None:
        # Compatibility only for retained ABI-5/early ABI-6 evidence.  The
        # deprecated spelling never meant a hardware-atomic snapshot.
        serialized_transaction = certification.get(
            "same_atomic_helper_transaction_per_pass"
        )
    for key in ("available", "full_domain_identity_bound"):
        if certification.get(key) is not True:
            errors.append("native certification %s is not true" % key)
    if serialized_transaction is not True:
        errors.append(
            "native certification lacks one helper-serialized ioctl transaction per pass"
        )
    if (
        "two_passes_in_one_helper_serialized_ioctl_transaction" in certification
        and certification.get("hardware_atomic_snapshot") is not False
    ):
        errors.append(
            "current native certification must explicitly deny hardware atomicity"
        )
    elif certification.get("hardware_atomic_snapshot") is True:
        errors.append("legacy native certification claims hardware atomicity")
    if certification.get("driver_unbound") is not True:
        errors.append("native certification does not prove driver-unbound capture")

    software_iff = None
    if kind == "main_ff":
        software_iff = _helper_software_iff_provenance(transaction)
        if software_iff.get("software_iff_redirection_excluded") is not True:
            errors.append("native MAIN does not exclude software-IFF redirection")
        if certification.get("software_iff_redirection_excluded") is not True:
            errors.append("native MAIN outer certification lacks redirection exclusion")
        stored_provenance = certification.get("software_iff_provenance")
        if not isinstance(stored_provenance, dict):
            errors.append("native MAIN stored software-IFF provenance is absent")
        else:
            for key in (
                "software_iff_redirection_excluded",
                "override_bit_inactive_at_entry_and_exit",
                "software_fusing_bit_inactive_at_entry_and_exit",
                "iff_record_stable",
                "entry_en_sw_override", "exit_en_sw_override",
                "entry_iff_sw_fusing", "exit_iff_sw_fusing",
            ):
                if stored_provenance.get(key) != software_iff.get(key):
                    errors.append(
                        "native MAIN stored software-IFF provenance disagrees at %s"
                        % key
                    )
            stored_interval = stored_provenance.get(
                "controller_iff_interval_observation",
                stored_provenance.get("physical_iff_interval_observation"),
            )
            recomputed_interval = software_iff.get(
                "controller_iff_interval_observation"
            )
            interval_fields = (
                "raw", "start", "last", "stable_across_transaction", "well_ordered"
            )
            if not isinstance(stored_interval, dict) or any(
                stored_interval.get(field) != recomputed_interval.get(field)
                for field in interval_fields
            ):
                errors.append(
                    "native MAIN stored software-IFF interval disagrees with snapshots"
                )
    elif certification.get("debug_rwl_restored") is not True:
        errors.append("native RIR certification does not prove DEBUGCTRL.RWL restore")

    if not isinstance(artifact_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", artifact_sha256
    ) is None:
        errors.append("artifact SHA-256 binding is absent or malformed")
    warnings.append(
        "unsigned JSON is structurally verified but not cryptographically authenticated; "
        "compare artifact_sha256 with an independently recorded acquisition hash"
    )
    return {
        "available": not errors,
        "kind": kind,
        "errors": errors,
        "warnings": warnings,
        "artifact_sha256": artifact_sha256,
        "source_tool": tool if isinstance(tool, dict) else None,
        "identity": transaction.get("identity"),
        "pass_hashes": computed_hashes,
        "software_iff_provenance": software_iff,
        "authenticity": "not established by unsigned JSON",
        "verification_scope": (
            "native envelope structure, identity consistency, row/hash coherence, "
            "ABI/counter/status invariants, controller quiescence and exact restore"
        ),
    }


def load_embedded_rir(loaded):
    """Parse RIR passes embedded in the already-selected native capture."""
    capture = loaded.get("selected_capture")
    if not isinstance(capture, dict) or "rir_rows" not in capture:
        return None
    rir = capture.get("rir_rows")
    if not isinstance(rir, dict) or not isinstance(rir.get("passes"), list):
        raise AuditError("embedded rir_rows passes are malformed")
    if not rir["passes"]:
        raise AuditError("embedded rir_rows passes must be a non-empty list")
    passes = []
    for pass_index, container in enumerate(rir["passes"]):
        rows = _parse_row_container(
            container, "embedded RIR pass %d" % (pass_index + 1)
        )
        if sorted(rows) != list(range(XRAY_RIR_ROWS)):
            raise AuditError("embedded RIR pass must contain exact rows 0..7")
        passes.append([rows[index] for index in range(XRAY_RIR_ROWS)])
    mismatches = []
    if len(passes) >= 2:
        for row_index in range(XRAY_RIR_ROWS):
            values = [row_pass[row_index] for row_pass in passes]
            if len(set(values)) != 1:
                mismatches.append({
                    "row": row_index,
                    "values": [hex32(value) for value in values],
                })
    return {
        "source": loaded["source"],
        "sha256": loaded["sha256"],
        "selected_card": loaded.get("selected_card"),
        "selected_capture": capture,
        "detected_format": "xray-embedded-rir",
        "words": passes[0],
        "passes": passes,
        "pass_hashes": [_packed_u32_pass_sha256(value) for value in passes],
        "coherent_repeated_passes": len(passes) >= 2 and not mismatches,
        "mismatches": mismatches,
        "embedded_in_main_artifact": True,
    }


def normalize_row_pass(row_pass, sparse_policy):
    missing = [index for index in range(MAX_FUSE_ROWS) if index not in row_pass]
    result = [None] * MAX_FUSE_ROWS
    for index, value in row_pass.items():
        result[index] = value
    assumptions = []
    if sparse_policy == "reject" and missing:
        raise AuditError(
            "sparse policy reject: %d of 512 rows are missing (first: %s)"
            % (len(missing), ",".join(str(item) for item in missing[:8]))
        )
    if sparse_policy == "zero-fill":
        for index in missing:
            result[index] = 0
        if missing:
            assumptions.append("%d missing rows were explicitly assumed zero" % len(missing))
    if sparse_policy == "preserve" and missing:
        assumptions.append("%d missing rows remain unknown" % len(missing))
    return result, missing, assumptions


def row_vector_sha256(rows):
    digest = hashlib.sha256()
    for value in rows:
        if value is None:
            digest.update(b"U\x00\x00\x00\x00")
        else:
            digest.update(b"V" + struct.pack("<I", value))
    return digest.hexdigest()


def build_region_hashes(row_passes, decoded, main_attestation):
    """Hash semantic regions separately so cross-plane changes stay visible."""
    start = decoded.get("fuseless_start")
    end = decoded.get("fuseless_end")
    boundary = decoded.get("boundary", {})
    frontier = boundary.get("first_available_ff_row")
    iff_start = boundary.get("iff_start")
    if not all(isinstance(value, int) for value in (start, end, frontier)):
        return {
            "available": False,
            "reason": "complete decoded bounds/frontier are unavailable",
            "regions": {},
        }
    provenance = main_attestation.get("software_iff_provenance", {})
    interval = {}
    if isinstance(provenance, dict):
        interval = provenance.get(
            "controller_iff_interval_observation",
            provenance.get("physical_iff_interval_observation", {}),
        )
    active_last = interval.get("last") if isinstance(interval, dict) else None
    ranges = [
        ("conventional_prefix", 0, start - 1),
        ("observed_ff_occupied", start, frontier - 1),
        ("observed_zero_gap", frontier, (iff_start if iff_start is not None else end + 1) - 1),
    ]
    if iff_start is not None:
        if not isinstance(active_last, int) or not iff_start <= active_last <= end:
            active_last = None
        ranges.append((
            "active_iff_window",
            iff_start,
            active_last if active_last is not None else end,
        ))
        if active_last is not None and active_last < end:
            ranges.append(("post_active_iff_tail", active_last + 1, end))
    if end < MAX_FUSE_ROWS - 1:
        ranges.append(("post_candidate_interval", end + 1, MAX_FUSE_ROWS - 1))
    regions = {}
    for name, first, last in ranges:
        if not isinstance(first, int) or not isinstance(last, int) or first > last:
            continue
        vectors = [row_pass[first:last + 1] for row_pass in row_passes]
        regions[name] = {
            "first_row": first,
            "last_row": last,
            "row_count": last - first + 1,
            "all_rows_known": all(
                all(value is not None for value in vector) for vector in vectors
            ),
            "pass_hashes": [row_vector_sha256(vector) for vector in vectors],
            "coherent": len(vectors) >= 2 and all(
                vector == vectors[0] for vector in vectors[1:]
            ),
        }
    return {
        "available": True,
        "hash_algorithm": "sha256 over tagged known/unknown little-endian u32 vector",
        "regions": regions,
        "bounds_status": (
            "conditional on caller-supplied fuseless bounds and the decoded frontier; "
            "active-IFF last comes from validated native IFF_RECORD when available; "
            "that region is a state-qualified controller view"
        ),
        "comparison_rule": (
            "compare only like-for-like row views and control planes; a whole-array "
            "hash must never hide which semantic region changed"
        ),
    }


def compare_row_passes(passes):
    mismatches = []
    if len(passes) < 2:
        return mismatches
    for index in range(MAX_FUSE_ROWS):
        values = [item[index] for item in passes]
        if len(set(values)) != 1:
            mismatches.append({
                "row": index,
                "values": [None if value is None else hex32(value) for value in values],
            })
    return mismatches


def decode_ga100_rir_words(words):
    """Decode the GA100 linear-address 8-row / 16-entry RIR format."""
    if not isinstance(words, (list, tuple)) or len(words) != XRAY_RIR_ROWS:
        raise AuditError("GA100 RIR requires exactly eight 32-bit rows")
    for row, word in enumerate(words):
        if word is not None and (
            type(word) is not int or not 0 <= word <= 0xFFFFFFFF
        ):
            raise AuditError(
                "GA100 RIR row %d must be an unsigned 32-bit integer or unknown"
                % row
            )
    entries = []
    enabled_by_address = {}
    for row, word in enumerate(words):
        if word is None:
            entries.extend([
                {"row": row, "half": 0, "state": "unknown"},
                {"row": row, "half": 1, "state": "unknown"},
            ])
            continue
        for half in range(2):
            raw = (word >> (16 * half)) & 0xFFFF
            item = {
                "row": row,
                "half": half,
                "slot": row * 2 + half,
                "raw": "0x%04x" % raw,
            }
            if raw == 0:
                item["state"] = "blank"
            elif raw == 0xFFFF:
                item["state"] = "invalidated_spent"
            else:
                address = (raw >> 2) & 0x3FFF
                enable = bool(raw & 1)
                item.update({
                    "state": "enabled" if enable else "disabled_record",
                    "enable": enable,
                    "forced_data": (raw >> 1) & 1,
                    "absolute_bit_address": address,
                    "target_row": address // 32,
                    "target_bit": address % 32,
                })
                if enable:
                    enabled_by_address.setdefault(address, []).append(item)
            entries.append(item)
    duplicates = [
        {
            "absolute_bit_address": address,
            "slots": [item["slot"] for item in items],
            "forced_data_values": [item["forced_data"] for item in items],
            "values_consistent": len({item["forced_data"] for item in items}) == 1,
            "conflicting_values": len({item["forced_data"] for item in items}) > 1,
            "precedence": "unknown",
        }
        for address, items in sorted(enabled_by_address.items()) if len(items) > 1
    ]
    effective = (
        [items[0] for address, items in sorted(enabled_by_address.items())]
        if not duplicates else None
    )
    return {
        "geometry": {
            "rows": 8,
            "entry_bits": 16,
            "entries": 16,
            "certainty_percent": 99,
            "source": (
                "GA100 RIR implementation hard-codes the row count at 8; retained captures corroborate"
            ),
        },
        "layout_evidence": {
            "certainty_percent": 100,
            "source": (
                "GA100-specific poison-record bit assignments and 0x0921 "
                "construction evidence"
            ),
        },
        "entries": entries,
        "enabled_records": [
            item for address, items in sorted(enabled_by_address.items()) for item in items
        ],
        "duplicate_address_groups": duplicates,
        "effective_repairs": effective,
        "effective_replay_available": not duplicates,
        "used_slots": sum(item.get("state") not in ("blank", "unknown") for item in entries),
        "blank_slots": sum(item.get("state") == "blank" for item in entries),
        "unknown_slots": sum(item.get("state") == "unknown" for item in entries),
        "note": (
            "Exact zero is the only blank representation and all-one is spent. The "
            "generic address-permuting RIR encoder is not wired into Ampere, so it must "
            "not be applied to GA100. Duplicate-address precedence is not established; "
            "every duplicate group blocks replay, including same-value duplicates."
        ),
        "evidence_separation": {
            "authoritative_facts": [
                "GA100 poison records use bits 15:2 as linear Addr[13:0], bit 1 data and bit 0 enable",
                "GA100 has eight RIR rows and two 16-bit entries per row",
                "the Ampere factory does not wire the generic RIR encoder",
            ],
            "empirical_observations": [
                "retained cards use slot 14 for the observed address-584 record",
            ],
            "unresolved": [
                "silicon precedence when two active records target the same address",
                "general non-poison RIR programming and reuse semantics on GA100",
            ],
        },
    }


IFF_LENGTHS = {0x00: 1, 0x1B: 3, 0x1C: 3, 0x1D: 2, 0x1E: 1, 0x1F: 1}
IFF_NAMES = {
    0x00: "null", 0x1B: "poll_dw", 0x1C: "modify_dw",
    0x1D: "write_dw", 0x1E: "set_field", 0x1F: "invalidated",
}

IFF_SET_FIELD_SPACES = {
    0x0: {"name": "NV_PMC_0", "base": 0x00000000, "valid_min": 0x00000000, "valid_max": 0x00000FFF},
    0x1: {"name": "NV_PBUS_0", "base": 0x00001000, "valid_min": 0x00001000, "valid_max": 0x00001FFF},
    0x2: {"name": "NV_PEXTDEV_0", "base": 0x00101000, "valid_min": 0x00101000, "valid_max": 0x00101FFF},
    0x3: {"name": "NV_PCFG_0", "base": 0x00088000, "valid_min": 0x00088000, "valid_max": 0x00088FFF},
    0x4: {"name": "NV_PCFG1_0", "base": 0x0008A000, "valid_min": 0x0008A000, "valid_max": 0x0008AFFF},
    0x5: {"name": "NV_PPRIV_SYS_0", "base": 0x00122000, "valid_min": 0x00122000, "valid_max": 0x001227FF},
    0x6: {"name": "NV_XP3G_0", "base": 0x0008B000, "valid_min": 0x0008B000, "valid_max": 0x0008BFFF},
    0x7: {"name": "NV_XP3G_1", "base": 0x0008C000, "valid_min": 0x0008C000, "valid_max": 0x0008CFFF},
    0x8: {"name": "NV_XP3G_2", "base": 0x0008D000, "valid_min": 0x0008D000, "valid_max": 0x0008DFFF},
    0x9: {"name": "NV_XP3G_3", "base": 0x0008E000, "valid_min": 0x0008E000, "valid_max": 0x0008EFFF},
    0xA: {"name": "NV_PTRIM_7", "base": 0x00137000, "valid_min": 0x00137000, "valid_max": 0x00137FFF},
    0xB: {"name": "NV_PVTRIM_0", "base": 0x0000E000, "valid_min": 0x0000E800, "valid_max": 0x0000FFFF},
    0xC: {"name": "NV_PTRIM_8", "base": 0x00138000, "valid_min": 0x00138000, "valid_max": 0x00138FFF},
    0xD: {"name": "NV_PGC6_0", "base": 0x00118000, "valid_min": 0x00118000, "valid_max": 0x00118FFF},
}

IFF_MACHINE_SPEC = {
    "certainty_scope": "GA100-selected recovered NVIDIA implementation; descriptor file absent",
    "evidence_separation": {
        "authoritative_facts": [
            "FF field masks, null/invalid chain values and later-record replay",
            "IFF opcode values, word lengths, ascending decode order and backward allocation",
            "S comes from MacroInfo.FuseRecordStart and merged-macro E from FuselessEnd/32",
        ],
        "empirical_observations": [
            "S=222 and E=511 fit all four retained complete card images",
            "the four retained images have B=488, F=296/297/298/297 and gaps 192/191/190/191",
        ],
        "unresolved": [
            "the absent official GA100 descriptor means numeric S/E are not authoritative",
            "silicon behavior for type-clear words after B and noncanonical WRITE/MODIFY high bits",
        ],
    },
    "shared_interval": {
        "start": "S = MacroInfo.FuseRecordStart",
        "end": "E = MiscInfo.FuselessEnd / 32 for the merged Ampere macro",
        "boundary": "B = first non-0xffffffff word in [S,E] with bit 5 set; else E+1",
        "ff_region": "[S,B), replayed upward in physical order",
        "iff_region": "[B,E], command stream allocated backward from E",
        "zero_gap": "shared unallocated capacity, not permanently typed FF or IFF rows",
        "official_ga100_bounds_known": False,
        "physical_array_addressable_rows": {"first": 0, "last": 511},
        "empirical_candidate_bounds": {
            "start": 222,
            "end": 511,
            "status": (
                "strong multi-card/recovered-layout hypothesis, not an official "
                "GA100 descriptor fact"
            ),
            "do_not_conflate": (
                "E=511 is the candidate merged-macro end; IFF_RECORD.LAST=507 "
                "on the four owner cards is the last live command word and does "
                "not include spent 0xffffffff suffix rows 508..511"
            ),
        },
    },
    "ff_word": {
        "chain": "bits 4:0", "type": "bit 5 (0=FF, 1=IFF)",
        "block": "bits 15:6", "data": "bits 31:16",
        "null_chain": 0, "invalidated_chain": 31,
        "all_ones_before_boundary": "skipped/invalidation form",
        "precedence": "later reachable record for the same (chain,block) wins",
    },
    "opcodes": {
        "0x00": {"name": "NULL", "words": 1, "ga100_encode": "skip/null"},
        "0x1b": {"name": "POLL_DW", "words": 3, "ga100_encode": "rejected; generic decode only"},
        "0x1c": {"name": "MODIFY_DW", "words": 3},
        "0x1d": {"name": "WRITE_DW", "words": 2},
        "0x1e": {"name": "SET_FIELD", "words": 1},
        "0x1f": {"name": "INVALID", "words": 1, "canonical_word": "0xffffffff"},
        "unknown": "fatal decode stop; length must never be guessed",
        "invalid_terminology": (
            "the IFF opcode is low-five-bit 0x1f; including the mandatory type "
            "bit, the low six bits are 0x3f. Public references using 0x1f and "
            "0x3f can therefore describe the same encoding, not competing formats"
        ),
    },
    "write_modify_address": "((command >> 6) & ((1 << 22) - 1)) << 2",
    "write_modify_high_bits": (
        "bits 31:28 are unconsumed by the recovered address decoder; report as "
        "noncanonical/unknown rather than claiming silicon-reserved semantics"
    ),
    "set_field": {
        "fieldspec": "(word >> 6) & 0x7f",
        "width": "floor(log2(fieldspec)); fieldspec=0 is malformed",
        "bit_index": "(word >> 13) & 0x1f",
        "space": "(word >> 18) & 0xf; GA100 permits 0x0..0xd",
        "offset": "(word >> 22) << 2",
        "value": "fieldspec & ((1 << width) - 1)",
        "and_mask": "~(((1 << width) - 1) << bit_index) & 0xffffffff",
        "or_mask": "value << bit_index",
        "validation": ["fieldspec != 0", "space <= 0xd", "bit_index + width <= 32"],
        "fieldspec_one": "legal zero-bit/no-op encoding in the header; never invent as a programming action",
        "spaces": [
            {
                "id": space_id,
                "name": value["name"],
                "base": hex32(value["base"]),
                "valid_min": hex32(value["valid_min"]),
                "valid_max": hex32(value["valid_max"]),
                "id_certainty_percent": 100,
                "base_certainty_percent": 97,
            }
            for space_id, value in sorted(IFF_SET_FIELD_SPACES.items())
        ],
        "space_evidence": (
            "IDs/names are literal GA100 register specification values; bases combine direct "
            "GA100 block ranges with recovered inherited GM10x/GP10x/GV100 mapping"
        ),
        "target_validation": (
            "absolute target must lie in that space's direct GA100 advertised range; "
            "PVTRIM demonstrates why page-base membership alone is insufficient; "
            "a reviewed register/field allowlist is additionally required"
        ),
    },
    "allocation": {
        "direction": "backward from E",
        "continuation_words": "data, never separately decoded commands",
        "mismatching_old_commands": "invalidated with all ones when OTP-representable",
        "all_ones_reusable": False,
        "snapshot_equation": (
            "FF_span + IFF_side_physical_span + zero_gap = E - S + 1; "
            "IFF_side_physical_span is E-B+1 and includes spent suffix rows"
        ),
        "correct_candidate_check": (
            "mutate a clone with every OTP 0->1 and invalidation, rescan it, set "
            "B_final=first live type-1 word else E+1, set F_final=1+last nonzero "
            "row in [S,B_final), then require B_final-F_final >= reserve"
        ),
        "shrink_warning": (
            "shortening/removing IFF invalidates surplus nonzero rows to 0xffffffff; "
            "they are spent and advance F_final, so capacity is not reclaimed"
        ),
        "recovered_allocator_bug": (
            "it checks old FF span + newRows + reserve before invalidating surplus "
            "old IFF rows, so a shrinking request can pass a stale capacity test"
        ),
        "reserve": "five rows is recovered software policy, not GA100 format",
    },
    "execution_observability": {
        "terminology": (
            "PBUS IFR is the init-from-ROM execution engine; fuse IFF records "
            "feed early-init writes but the terms are not interchangeable"
        ),
        "PBUS_IFR_STATUS0": "0x1720; ROMADDR[20:0]",
        "PBUS_IFR_STATUS1": (
            "0x1724; BAR0ADDR[23:0], IFF_DONE[27], IDLE[29], "
            "LASTEXEC[30], READINRMW[31]"
        ),
        "PBUS_IFR_ERROR": (
            "0x1728; BADSIG[0], FAILSAFE_TIMEOUT[1], BADPARITY[8], "
            "BADROMLEN[12], BADCHECKSUM[16], BADFORMAT[20], PRI_ERROR[21], "
            "IFF_RESENSE_TIMEOUT[23], IFF_BADFIELDSPEC[24], IFF_BADSPACEID[25], "
            "IFF_BADFUSELEN[26], IFF_BADCMDOP[27], IFF_PRI_ERROR[29]"
        ),
        "PBUS_IFR_INFO0": "0x172c; ROMLEN[19:0]",
        "success_rule": (
            "LASTEXEC plus zero relevant error bits is stronger than IFF_DONE "
            "alone; still pair with cold reset, exact row decode and functional checks"
        ),
        "use": "pure-register post-reset evidence; zero errors alone do not prove intended writes occurred",
        "certainty_percent": 100,
    },
    "software_iff": {
        "register": "IFF_RECORD 0x820050 START[8:0], LAST[24:16]",
        "loader": "FUSEADDR/FUSEWDATA plus IFF_SW_FUSING under EN_SW_OVERRIDE",
        "meaning": "software-IFF RAM redirection window, not physical OTP boundary",
        "dump_risk": "base GA100 row reads may be redirected; retain override/window provenance",
        "empty_vector_bug": "E=511 produces unrepresentable start=512 in recovered code",
        "empty_vector_scope": (
            "direct-accessor edge; normal GA100 fuse call flow skips an empty vector, "
            "but the accessor itself remains unsafe for direct use"
        ),
    },
    "crc": {
        "ga100_synthetic_iff_crc_required": False,
        "confidence_percent": 95,
        "reason": "base CalculateCrc unsupported; Ampere loader ignores CRC argument; explicit implementation is Hopper-only",
        "main_macro_crc_status_is_separate": True,
    },
    "known_recovered_implementation_defects": [
        "multiword match indexes currentRow-i before proving the span is in bounds",
        "unsigned exhaustion arithmetic can underflow",
        "candidate storage is mutated before aggregate capacity validation",
        "unknown-opcode error code crosses a UINT32 length API",
        "spare-row setter is routed to FF while IFF performs collision accounting",
        "FF-only requests can bypass shared reserve/collision validation",
        "base GA100 raw reads can be software-IFF redirected",
        "generic VerifySwIffFusing is a no-op",
        "capacity validation precedes surplus-IFF invalidation and can undercount spent rows after shrink/removal",
        "truncated IFF decode warns and returns a partial prefix as success",
        "Decode clears records but does not reset m_HasIff false when no records remain",
        "JSON sequence is parsed but not used to order IFF records",
        "ATE/dontCare information is lost when converted into an IFF record object",
        "the IFF-record collector does not clear its destination vector",
        "the IFF-record validator sets mismatch false but never sets success true",
        "rework encoding clears IFF rather than transforming or preserving it",
    ],
    "strict_decoder_policy": (
        "XRAY stops on a post-boundary command with type bit clear, malformed operands, "
        "truncation, unknown opcode, or GA100-unsupported POLL_DW. The recovered "
        "IFF-record constructor merely warns on type-clear, so silicon behavior there is unconfirmed."
    ),
}


DSOV_PERSISTENCE_MODEL = {
    "target": "DISABLE_SW_OVERRIDE_STATUS.DATA == 0",
    "status_as_of_2026_11_23": (
        "demonstrated only as a same-powered-session latch phenotype; no "
        "evidence establishes persistence across complete slot/AUX power loss"
    ),
    "same_session_evidence": {
        "host_reads": {"supported": True, "confidence_percent": 90},
        "sense_ctrl": {"supported": True, "confidence_percent": 90},
        "note": (
            "SENSE_CTRL can restore ENABLE_FUSE_PROGRAM while the glitched DSOV "
            "latch remains zero; this is not cold persistence"
        ),
    },
    "reset_domains_requiring_separate_trials": [
        "host reads/no reset",
        "one SENSE_CTRL",
        "GSP/SEC2 reset",
        "PCIe FLR",
        "secondary-bus or PEX_RST without slot power loss",
        "warm host reboot",
        "true slot plus auxiliary-power removal",
    ],
    "same_frame_observations": [
        "DISABLE_SW_OVERRIDE_STATUS", "CRC_STATUS", "FUSECTRL",
        "ENABLE_FUSE_PROGRAM_STATUS", "DISABLE_FUSE_PROGRAM_STATUS",
        "BYPASS_FUSES_STATUS", "OPT_SECURE_FUSE_RESENSE_DISABLE",
        "SECURED_BURN_FEATURE_ENABLE", "EN_SW_OVERRIDE", "IFF_SW_FUSING",
        "IFF_RECORD", "FUSEWDATA_PRIV_LEVEL_MASK", "FUSECTRL_PRIV_LEVEL_MASK",
        "OPT_PRIV_LEVEL_MASK", "row-0 read identity", "target-row state",
    ],
    "success_rule": (
        "report persistence only for each reset class whose before/after identity "
        "and complete same-frame gate vector were captured; DSOV=0 alone is never success"
    ),
    "planning_allowed": False,
}


def _validate_decoder_row_vector(rows, first, last, label):
    if not isinstance(rows, (list, tuple)):
        raise AuditError("%s must be a row sequence" % label)
    if len(rows) <= last:
        raise AuditError(
            "%s must contain row %d (length is %d)" % (label, last, len(rows))
        )
    for row_index in range(first, last + 1):
        value = rows[row_index]
        if value is not None and (
            type(value) is not int or not 0 <= value <= 0xFFFFFFFF
        ):
            raise AuditError(
                "%s row %d must be an unsigned 32-bit integer or unknown"
                % (label, row_index)
            )


def decode_iff_operands(opcode, words):
    if type(opcode) is not int or opcode not in IFF_LENGTHS:
        raise AuditError("unknown IFF opcode for operand decode")
    expected_length = IFF_LENGTHS[opcode]
    if not isinstance(words, (list, tuple)) or len(words) != expected_length:
        raise AuditError(
            "IFF opcode 0x%02x requires exactly %d word(s)"
            % (opcode, expected_length)
        )
    for index, value in enumerate(words):
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise AuditError(
                "IFF operand word %d must be an unsigned 32-bit integer" % index
            )
    first = words[0]
    if (first & 0x1F) != opcode:
        raise AuditError(
            "IFF first-word opcode does not match requested operand decoder"
        )
    if opcode == 0x1D:
        result = {
            "address": hex32(((first >> 6) & 0x3FFFFF) << 2),
            "data": hex32(words[1]),
        }
        if first & 0xF0000000:
            result["warnings"] = [
                "command bits 31:28 are unconsumed by the recovered address decoder; silicon semantics are unconfirmed"
            ]
        return result
    if opcode == 0x1C:
        result = {
            "address": hex32(((first >> 6) & 0x3FFFFF) << 2),
            "and_mask": hex32(words[1]),
            "or_mask": hex32(words[2]),
        }
        if first & 0xF0000000:
            result["warnings"] = [
                "command bits 31:28 are unconsumed by the recovered address decoder; silicon semantics are unconfirmed"
            ]
        return result
    if opcode == 0x1E:
        field_spec = (first >> 6) & 0x7F
        width = field_spec.bit_length() - 1 if field_spec else None
        bit_index = (first >> 13) & 0x1F
        space = (first >> 18) & 0xF
        result = {
            "space": space,
            "address_within_space": hex32(((first >> 22) & 0x3FF) << 2),
            "bit_index": bit_index,
            "field_spec": "0x%02x" % field_spec,
            "field_width_bits": width,
        }
        malformed = []
        space_info = IFF_SET_FIELD_SPACES.get(space)
        if space_info is not None:
            relative_address = ((first >> 22) & 0x3FF) << 2
            effective_address = space_info["base"] | relative_address
            result.update({
                "space_name": space_info["name"],
                "space_base": hex32(space_info["base"]),
                "effective_bar0_address": hex32(effective_address),
                "target_in_advertised_space_range": (
                    space_info["valid_min"] <= effective_address
                    <= space_info["valid_max"]
                ),
                "advertised_valid_range": [
                    hex32(space_info["valid_min"]),
                    hex32(space_info["valid_max"]),
                ],
                "space_id_certainty_percent": 100,
                "space_base_certainty_percent": 97,
            })
            if not result["target_in_advertised_space_range"]:
                malformed.append(
                    "SET_FIELD target is outside the direct GA100 block range"
                )
        if space > 0xD:
            malformed.append("space is outside the GA100-defined range 0x0..0xd")
        if width is not None:
            data_mask = (1 << width) - 1
            field_value = field_spec & data_mask
            result["field_value"] = field_value
            result["and_mask"] = hex32(~(data_mask << bit_index))
            result["or_mask"] = hex32(field_value << bit_index)
            if bit_index + width > 32:
                malformed.append("field extends past bit 31")
        else:
            malformed.append("FIELDSPEC is zero")
        if malformed:
            result["malformed"] = malformed
        return result
    if opcode == 0x1B:
        return {
            "raw_operands": [hex32(value) for value in words[1:]],
            "ga100_encode_supported": False,
            "note": "recognized by generic decoder; GA100 encoder rejects POLL_DW",
        }
    return {}


def scan_shared_ff_iff_capacity(rows, fuseless_start, fuseless_end, reserve=5):
    """Recompute the real FF/IFF frontier on a complete candidate image."""
    if not (
        type(fuseless_start) is int
        and type(fuseless_end) is int
        and 0 <= fuseless_start <= fuseless_end < MAX_FUSE_ROWS
    ):
        raise AuditError("invalid explicit fuseless bounds")
    if type(reserve) is not int or reserve < 0:
        raise AuditError("reserve must be a nonnegative integer")
    _validate_decoder_row_vector(
        rows, fuseless_start, fuseless_end, "FF/IFF capacity input"
    )
    entire_interval_known = all(
        rows[index] is not None
        for index in range(fuseless_start, fuseless_end + 1)
    )
    boundary = fuseless_end + 1
    first_available = fuseless_start
    for row_index in range(fuseless_start, fuseless_end + 1):
        value = rows[row_index]
        if value is None:
            return {
                "frontier_complete": False,
                "entire_interval_known": False,
                "unknown_row": row_index,
                "reserve_rows": reserve,
                "observed_gap_meets_reserve": None,
                "reserve_satisfied": None,
                "allocation_proof_complete": False,
            }
        if (value & 0x20) and value != 0xFFFFFFFF:
            boundary = row_index
            break
        if value != 0:
            first_available = row_index + 1
    gap = boundary - first_available
    observed_gap_meets_reserve = gap >= reserve
    ff_prefix_span = first_available - fuseless_start
    iff_side_span = fuseless_end - boundary + 1
    candidate_span = fuseless_end - fuseless_start + 1
    return {
        "frontier_complete": True,
        "entire_interval_known": entire_interval_known,
        "iff_boundary_B": boundary,
        "ff_frontier_F": first_available,
        "zero_gap_rows": gap,
        "reserve_rows": reserve,
        "observed_gap_meets_reserve": observed_gap_meets_reserve,
        "reserve_satisfied": (
            observed_gap_meets_reserve if entire_interval_known else None
        ),
        "allocation_proof_complete": entire_interval_known,
        "physical_partition": {
            "ff_used_prefix_span_rows": ff_prefix_span,
            "zero_gap_rows": gap,
            "iff_side_span_rows_including_spent_suffix": iff_side_span,
            "candidate_interval_rows": candidate_span,
            "partition_matches": (
                ff_prefix_span + gap + iff_side_span == candidate_span
            ),
        },
        "equation": "B-F >= reserve",
        "note": (
            "0xffffffff rows before B are spent nonzero rows and therefore advance F; "
            "candidate completeness additionally requires every row through E to be known"
        ),
    }


def decode_ff_iff(rows, fuseless_start, fuseless_end):
    if not (
        type(fuseless_start) is int
        and type(fuseless_end) is int
        and 0 <= fuseless_start <= fuseless_end < MAX_FUSE_ROWS
    ):
        raise AuditError("invalid explicit fuseless bounds")
    _validate_decoder_row_vector(
        rows, fuseless_start, fuseless_end, "FF/IFF decoder input"
    )
    ff_records = []
    effective = {}
    iff_start = None
    stop_reason = None
    first_available = fuseless_start

    for row_index in range(fuseless_start, fuseless_end + 1):
        value = rows[row_index]
        if value is None:
            stop_reason = "unknown row %d prevents exact FF/IFF boundary scan" % row_index
            break
        if value == 0xFFFFFFFF:
            ff_records.append({
                "row": row_index, "raw": hex32(value), "kind": "invalidated",
            })
            first_available = row_index + 1
            continue
        if value & 0x20:
            iff_start = row_index
            break
        chain = value & 0x1F
        block = (value >> 6) & 0x3FF
        data = (value >> 16) & 0xFFFF
        if value != 0:
            first_available = row_index + 1
        if chain == 0:
            kind = "blank" if value == 0 else "null"
        elif chain == 0x1F:
            kind = "invalidated_chain"
        else:
            kind = "ff"
        record = {
            "row": row_index,
            "raw": hex32(value),
            "kind": kind,
            "chain": chain,
            "block": block,
            "data": "0x%04x" % data,
        }
        ff_records.append(record)
        if kind == "ff":
            key = "%d:%d" % (chain, block)
            previous = effective.get(key)
            effective[key] = {
                "chain": chain,
                "block": block,
                "data": "0x%04x" % data,
                "row": row_index,
                "supersedes_row": previous["row"] if previous else None,
            }

    iff_records = []
    iff_errors = []
    iff_validation_errors = []
    physical_order_index = 0
    execution_order_index = 0
    if iff_start is not None:
        row_index = iff_start
        while row_index <= fuseless_end:
            first = rows[row_index]
            if first is None:
                iff_errors.append("unknown IFF first word at row %d" % row_index)
                break
            if first == 0xFFFFFFFF:
                iff_records.append({
                    "row": row_index,
                    "raw_words": [hex32(first)],
                    "opcode": "0x1f",
                    "kind": "invalidated",
                    "length": 1,
                    "physical_order_index": physical_order_index,
                    "execution_order_index": None,
                })
                physical_order_index += 1
                row_index += 1
                continue
            if not first & 0x20:
                iff_errors.append(
                    "IFF first word at row %d has type bit 5 clear" % row_index
                )
                break
            opcode = first & 0x1F
            length = IFF_LENGTHS.get(opcode)
            if length is None:
                iff_errors.append(
                    "unknown IFF opcode 0x%02x at row %d; record length is unknown"
                    % (opcode, row_index)
                )
                break
            if row_index + length - 1 > fuseless_end:
                iff_errors.append(
                    "IFF record at row %d overruns explicit end %d"
                    % (row_index, fuseless_end)
                )
                break
            words = rows[row_index:row_index + length]
            if any(value is None for value in words):
                iff_errors.append("IFF record at row %d has unknown continuation row" % row_index)
                break
            operands = decode_iff_operands(opcode, words)
            record = {
                "row": row_index,
                "raw_words": [hex32(value) for value in words],
                "opcode": "0x%02x" % opcode,
                "kind": IFF_NAMES[opcode],
                "length": length,
                "decoded": operands,
                "physical_order_index": physical_order_index,
                "execution_order_index": (
                    execution_order_index if opcode in (0x1B, 0x1C, 0x1D, 0x1E)
                    else None
                ),
            }
            for error in operands.get("malformed", []):
                iff_validation_errors.append(
                    "IFF record at row %d is invalid for GA100: %s"
                    % (row_index, error)
                )
            if opcode == 0x1B:
                iff_validation_errors.append(
                    "POLL_DW at row %d is structurally recognized but rejected by the GA100 encoder"
                    % row_index
                )
            iff_records.append(record)
            physical_order_index += 1
            if opcode in (0x1B, 0x1C, 0x1D, 0x1E):
                execution_order_index += 1
            row_index += length

    effective_list = sorted(
        effective.values(), key=lambda item: (item["chain"], item["block"])
    )
    gap_end = iff_start if iff_start is not None else fuseless_end + 1
    free_gap = max(0, gap_end - first_available) if stop_reason is None else None
    structural_errors = ([stop_reason] if stop_reason else []) + iff_errors
    structurally_complete = not structural_errors
    ga100_semantically_valid = structurally_complete and not iff_validation_errors
    return {
        "fuseless_start": fuseless_start,
        "fuseless_end": fuseless_end,
        "boundary": {
            "iff_start": iff_start,
            "first_available_ff_row": first_available,
            "observed_free_gap_rows": free_gap,
            "note": "Observed gap is not a validated allocation budget.",
        },
        "ff_records": ff_records,
        "effective_ff_records": effective_list,
        "iff_records": iff_records,
        "iff_order": {
            "physical_scan": "ascending row order from B through E",
            "command_execution_order": "ascending decoded command order",
            "allocation_direction": "backward from E while preserving command order",
            "certainty_percent": 100,
        },
        "structural_errors": structural_errors,
        "validation_errors": iff_validation_errors,
        "errors": structural_errors + iff_validation_errors,
        "structurally_complete": structurally_complete,
        "ga100_semantically_valid": ga100_semantically_valid,
        "decode_complete": ga100_semantically_valid,
        "capacity_snapshot": scan_shared_ff_iff_capacity(
            rows, fuseless_start, fuseless_end
        ),
    }


def _catalog_result_base(item):
    return {
        "name": item["name"],
        "plane": item["plane"],
        "width": item.get("width"),
        "certainty_percent": item["certainty_percent"],
        "sources": list(item["sources"]),
        "certainty_reason": item["certainty_reason"],
        "planning_allowed": False,
    }


def _decode_effective_ff_bits(effective, chain, start_bit, width):
    components = []
    value = 0
    complete = True
    cursor = start_bit
    output_shift = 0
    remaining = width
    while remaining > 0:
        block = cursor // 16
        bit_in_block = cursor % 16
        take = min(remaining, 16 - bit_in_block)
        record = effective.get((chain, block))
        component = {
            "chain": chain,
            "block": block,
            "bit_in_block": bit_in_block,
            "width": take,
            "value_shift": output_shift,
        }
        if record is None:
            component["status"] = "missing_effective_ff_record"
            complete = False
        else:
            data = int(record["data"], 0)
            piece = (data >> bit_in_block) & ((1 << take) - 1)
            component.update({
                "status": "decoded",
                "source_row": record["row"],
                "source_data": record["data"],
                "piece_value": piece,
                "piece_value_hex": "0x%x" % piece,
            })
            value |= piece << output_shift
        components.append(component)
        cursor += take
        output_shift += take
        remaining -= take
    result = {
        "chain": chain,
        "logical_start_bit": start_bit,
        "logical_end_bit": start_bit + width - 1,
        "components": components,
        "complete": complete,
    }
    if complete:
        digits = max(1, (width + 3) // 4)
        result.update({
            "value": value,
            "value_hex": "0x%0*x" % (digits, value),
        })
    else:
        result["value"] = None
        result["value_hex"] = None
    return result


def decode_logical_field_catalog(ff_analysis, rows):
    """Decode every catalog entry without treating an absent record as zero."""
    effective = {}
    for record in ff_analysis.get("effective_ff_records", []):
        if not isinstance(record, dict):
            continue
        chain = record.get("chain")
        block = record.get("block")
        if isinstance(chain, int) and isinstance(block, int):
            effective[(chain, block)] = record
    results = []
    for item in LOGICAL_FIELD_CATALOG:
        result = _catalog_result_base(item)
        result["coordinate_source_class"] = item.get("source_class")
        if item["plane"] == "FF":
            chain = item.get("chain")
            width = item.get("width")
            if item.get("coordinate", "present") is None:
                result.update({
                    "status": "unlocated",
                    "value": None,
                    "reason": "no defensible physical FF coordinate is known",
                })
            elif "candidate_bits" in item:
                block = item.get("block")
                candidates = []
                for candidate_bit in item["candidate_bits"]:
                    decoded = _decode_effective_ff_bits(
                        effective, chain, block * 16 + candidate_bit, width
                    )
                    candidates.append({
                        "candidate_bit_in_block": candidate_bit,
                        "decoded": decoded,
                    })
                result.update({
                    "status": "unresolved_coordinate_candidates",
                    "block": block,
                    "candidates": candidates,
                    "value": None,
                    "reason": "evidence does not distinguish the candidate bits",
                })
            else:
                if "bit" in item:
                    start_bit = item["bit"]
                elif "block" in item and "bit_in_block" in item:
                    start_bit = item["block"] * 16 + item["bit_in_block"]
                else:
                    result.update({
                        "status": "unlocated",
                        "value": None,
                        "reason": "catalog entry has no complete coordinate",
                    })
                    results.append(result)
                    continue
                decoded = _decode_effective_ff_bits(
                    effective, chain, start_bit, width
                )
                result["decoded"] = decoded
                result["value"] = decoded["value"]
                result["value_hex"] = decoded["value_hex"]
                result["status"] = (
                    "decoded_under_non_authoritative_catalog_coordinate"
                    if decoded["complete"] else "incomplete_missing_ff_record"
                )
        elif item["plane"] == "conventional":
            pieces = []
            complete = True
            bit = item["bit"]
            width = item["width"]
            for kind, row_index in (
                ("primary", item["primary_row"]),
                ("redundant", item["redundant_row"]),
            ):
                piece = {
                    "kind": kind,
                    "row": row_index,
                    "bit": bit,
                    "width": width,
                }
                row_value = rows[row_index] if 0 <= row_index < len(rows) else None
                if row_value is None:
                    piece["status"] = "unknown_row"
                    piece["value"] = None
                    complete = False
                else:
                    value = (row_value >> bit) & ((1 << width) - 1)
                    piece.update({
                        "status": "decoded_raw_alias_piece",
                        "row_value": hex32(row_value),
                        "value": value,
                        "value_hex": "0x%x" % value,
                    })
                pieces.append(piece)
            result.update({
                "status": (
                    "raw_primary_redundant_pieces_only" if complete
                    else "incomplete_primary_redundant_pieces"
                ),
                "pieces": pieces,
                "value": None,
                "sense_rule_applied": False,
                "reason": (
                    "primary/redundant pieces are reported independently; no OR, "
                    "precedence, repair, or control-field sense rule is assumed"
                ),
            })
        else:
            result.update({
                "status": "unsupported_catalog_plane",
                "value": None,
            })
        results.append(result)
    return {
        "entry_count": len(results),
        "entries": results,
        "missing_ff_records_default_to_zero": False,
        "coordinate_authority": (
            "hypothesis/empirical catalog only; official GA100 physical descriptor is absent"
        ),
        "bit_assembly_rule": (
            "increasing chain bit contributes increasing value significance; this is "
            "used only to interpret explicitly located fields, never to authorize writes"
        ),
        "planning_allowed": False,
    }


def decode_conventional_alias_locations(rows, header_registry):
    """Decode direct-header alias pieces without inventing field assembly semantics."""
    fields = []
    for field in header_registry.get("physical_aliases", []):
        pieces = []
        all_known = True
        for alias in field.get("aliases", []):
            piece = {
                key: alias[key]
                for key in ("kind", "index", "row", "msb", "lsb", "width")
                if key in alias
            }
            row_index = alias.get("row")
            msb = alias.get("msb")
            lsb = alias.get("lsb")
            if not all(isinstance(value, int) for value in (row_index, msb, lsb)):
                piece["status"] = "incomplete_header_alias_expression"
                piece["value"] = None
                all_known = False
            elif not (0 <= row_index < len(rows)) or not (0 <= lsb <= msb < 32):
                piece["status"] = "alias_out_of_captured_row_range"
                piece["value"] = None
                all_known = False
            elif rows[row_index] is None:
                piece["status"] = "unknown_row"
                piece["value"] = None
                all_known = False
            else:
                piece_width = msb - lsb + 1
                value = (rows[row_index] >> lsb) & ((1 << piece_width) - 1)
                piece.update({
                    "status": "decoded_raw_alias_piece",
                    "piece_width": piece_width,
                    "row_value": hex32(rows[row_index]),
                    "value": value,
                    "value_hex": "0x%x" % value,
                })
            pieces.append(piece)
        fields.append({
            "name": field.get("name"),
            "declared_width": field.get("width"),
            "pieces": pieces,
            "all_pieces_known": all_known,
            "assembled_value": None,
            "assembly_status": "deliberately_not_assembled",
            "physical_mapping_confidence_percent": field.get(
                "physical_mapping_confidence_percent"
            ),
            "planning_allowed": False,
        })
    return {
        "field_count": len(fields),
        "fields": fields,
        "sense_or_piece_order_assumed": False,
        "note": (
            "Literal primary/redundant row slices are decoded. Cross-piece significance, "
            "primary/redundant sense, control dependencies, and RIR precedence are not inferred."
        ),
        "planning_allowed": False,
    }


def apply_unique_rir_view(
    rows, rir_analysis, fuseless_start=None, fuseless_end=None
):
    """Return a conditional conventional-fuse sense clone without mutating input."""
    if not isinstance(rir_analysis, dict):
        return ({
            "state": "refused_missing_rir_analysis",
            "planning_allowed": False,
        }, None)
    repairs = rir_analysis.get("effective_repairs")
    if not rir_analysis.get("effective_replay_available") or not isinstance(repairs, list):
        return ({
            "state": "refused_duplicate_or_ambiguous_rir",
            "duplicate_address_groups": rir_analysis.get(
                "duplicate_address_groups", []
            ),
            "planning_allowed": False,
        }, None)
    derived = list(rows)
    applications = []
    unresolved = []
    ignored_fuseless_targets = []
    for repair in repairs:
        row_index = repair.get("target_row")
        bit = repair.get("target_bit")
        forced_data = repair.get("forced_data")
        if not (
            isinstance(row_index, int) and 0 <= row_index < len(derived)
            and isinstance(bit, int) and 0 <= bit < 32
            and forced_data in (0, 1)
        ):
            unresolved.append({"repair": repair, "reason": "invalid target"})
            continue
        if (
            isinstance(fuseless_start, int)
            and isinstance(fuseless_end, int)
            and fuseless_start <= row_index <= fuseless_end
        ):
            ignored_fuseless_targets.append({
                "slot": repair.get("slot"),
                "absolute_bit_address": repair.get("absolute_bit_address"),
                "row": row_index,
                "bit": bit,
                "forced_data": forced_data,
                "reason": (
                    "recovered SplitMacroEncoder excludes fuseless fuses from RIR; "
                    "target is anomalous under the recovered GA100 software model"
                ),
            })
            continue
        before = derived[row_index]
        if before is None:
            unresolved.append({
                "slot": repair.get("slot"),
                "row": row_index,
                "bit": bit,
                "reason": "target row is unknown",
            })
            continue
        if forced_data:
            after = before | (1 << bit)
        else:
            after = before & ~(1 << bit)
        derived[row_index] = after & 0xFFFFFFFF
        applications.append({
            "slot": repair.get("slot"),
            "absolute_bit_address": repair.get("absolute_bit_address"),
            "row": row_index,
            "bit": bit,
            "forced_data": forced_data,
            "before": hex32(before),
            "after": hex32(derived[row_index]),
            "changed": before != derived[row_index],
        })
    if unresolved:
        state = "incomplete_unknown_target_rows"
    elif ignored_fuseless_targets:
        state = "derived_conventional_with_fuseless_targets_ignored"
    else:
        state = "derived_conventional"
    summary = {
        "state": state,
        "applications": applications,
        "unresolved": unresolved,
        "ignored_fuseless_targets": ignored_fuseless_targets,
        "derived_row_vector_sha256": row_vector_sha256(derived),
        "derived_rows": [None if value is None else hex32(value) for value in derived],
        "physical_otp_rows_changed": False,
        "meaning": (
            "conditional post-RIR conventional-fuse sense view cloned offline; "
            "FF/IFF is intentionally not repaired or re-decoded through RIR"
        ),
        "planning_allowed": False,
    }
    return summary, (derived if not unresolved else None)


def command_analyze_rows(args):
    if not (0 <= args.fuseless_start <= args.fuseless_end < MAX_FUSE_ROWS):
        raise AuditError("invalid explicit fuseless bounds")
    loaded = load_row_passes(args.input, args.card, args.input_format)
    header_path = args.header or default_header_path()
    header_registry = parse_header_file(header_path) if header_path else None
    normalized = []
    pass_summaries = []
    all_assumptions = []
    for index, row_pass in enumerate(loaded["passes"]):
        rows, missing, assumptions = normalize_row_pass(row_pass, args.sparse_policy)
        normalized.append(rows)
        all_assumptions.extend("pass %d: %s" % (index + 1, item) for item in assumptions)
        pass_summaries.append({
            "pass": index + 1,
            "present_rows": MAX_FUSE_ROWS - len(missing),
            "missing_rows": missing,
            "row_vector_sha256": row_vector_sha256(rows),
        })
    mismatches = compare_row_passes(normalized)
    if mismatches:
        decoded = {
            "state": "refused_incoherent_passes",
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        }
    else:
        decoded = decode_ff_iff(
            normalized[0], args.fuseless_start, args.fuseless_end
        )
        decoded["logical_field_catalog_decode"] = decode_logical_field_catalog(
            decoded, normalized[0]
        )
        decoded["state"] = "decoded"

    exact_under_declared_assumptions = (
        len(normalized) >= 2
        and not mismatches
        and args.sparse_policy == "reject"
        and args.row_view in ("physical", "locked_controller")
        and decoded.get("decode_complete", False)
    )
    main_attestation = verify_native_capture_attestation(
        loaded.get("selected_capture"), loaded.get("selected_card"),
        "main_ff", normalized, loaded["sha256"],
    )
    if not mismatches:
        decoded["region_hashes"] = build_region_hashes(
            normalized, decoded, main_attestation
        )
    # A CLI row-view label remains insufficient.  Certification is available
    # only when the selected object is a native capture whose complete helper
    # envelope survives independent structural and hash validation.
    certified_controller_main_decode = bool(
        exact_under_declared_assumptions
        and main_attestation["available"]
    )
    physical_available = certified_controller_main_decode
    document = base_document("analyze-rows")
    document["input"] = {
        "source": loaded["source"],
        "sha256": loaded["sha256"],
        "detected_format": loaded.get("detected_format"),
        "format_claims": loaded.get("format_claims", {}),
        "requested_input_format": args.input_format,
        "row_view": args.row_view,
        "provenance": args.provenance,
        "selected_card": loaded.get("selected_card"),
        "passes": pass_summaries,
        "pass_count": len(normalized),
        "coherent": not mismatches if len(normalized) >= 2 else None,
        "mismatches": mismatches,
        "sparse_policy": args.sparse_policy,
        "assumptions": all_assumptions,
        "explicit_bounds": {
            "fuseless_start": args.fuseless_start,
            "fuseless_end": args.fuseless_end,
        },
    }
    document["analysis"] = decoded
    if header_registry is None:
        document["direct_header_alias_source"] = {
            "available": False,
            "reason": "no --header supplied and workspace default not found",
        }
    else:
        document["direct_header_alias_source"] = {
            "available": True,
            "source": header_registry["source"],
            "sha256": header_registry["sha256"],
            "register_count": header_registry["register_count"],
            "alias_field_count": header_registry["alias_field_count"],
        }
        if mismatches:
            document["conventional_alias_decode"] = {
                "state": "refused_incoherent_main_passes",
                "planning_allowed": False,
            }
        else:
            document["conventional_alias_decode"] = (
                decode_conventional_alias_locations(
                    normalized[0], header_registry
                )
            )
    rir_available = False
    rir_loaded = None
    if args.rir_input is not None:
        if args.rir_input == "-" and args.input == "-":
            raise AuditError("main rows and RIR cannot both consume stdin")
        rir_loaded = load_rir_words(
            args.rir_input, args.rir_card or args.card, args.rir_input_format
        )
    else:
        rir_loaded = load_embedded_rir(loaded)
    if rir_loaded is not None:
        main_identities = _strong_selector_identities(loaded.get("selected_card"))
        rir_identities = _strong_selector_identities(rir_loaded.get("selected_card"))
        identity_bound = bool(
            main_identities and rir_identities
            and main_identities.intersection(rir_identities)
        )
        same_artifact = loaded["sha256"] == rir_loaded["sha256"]
        rir_attestation = verify_native_capture_attestation(
            rir_loaded.get("selected_capture"), rir_loaded.get("selected_card"),
            "rir", rir_loaded["passes"], rir_loaded["sha256"],
        )
        rir_available = bool(
            certified_controller_main_decode
            and identity_bound
            and same_artifact
            and rir_loaded["coherent_repeated_passes"]
            and rir_attestation["available"]
        )
        document["rir_input"] = {
            "source": rir_loaded["source"],
            "sha256": rir_loaded["sha256"],
            "detected_format": rir_loaded.get("detected_format"),
            "requested_input_format": args.rir_input_format,
            "selected_card": rir_loaded["selected_card"],
            "provenance": args.rir_provenance,
            "pass_count": len(rir_loaded["passes"]),
            "pass_hashes": rir_loaded["pass_hashes"],
            "coherent_repeated_passes": rir_loaded["coherent_repeated_passes"],
            "mismatches": rir_loaded["mismatches"],
            "same_specimen_identity_bound": identity_bound,
            "same_native_capture_artifact": same_artifact,
            "embedded_in_main_artifact": rir_loaded.get(
                "embedded_in_main_artifact", False
            ),
            "main_identity_tokens": sorted(main_identities),
            "rir_identity_tokens": sorted(rir_identities),
        }
        if rir_loaded["mismatches"]:
            document["rir_analysis"] = {
                "state": "refused_incoherent_passes",
                "mismatches": rir_loaded["mismatches"],
            }
        else:
            document["rir_analysis"] = decode_ga100_rir_words(rir_loaded["words"])
            document["rir_analysis"]["state"] = "decoded"
            document["rir_analysis"]["repeated_decodeable_replay"] = (
                rir_loaded["coherent_repeated_passes"]
                and document["rir_analysis"]["effective_replay_available"]
            )
            if not mismatches:
                effective_summary, effective_rows = apply_unique_rir_view(
                    normalized[0], document["rir_analysis"],
                    args.fuseless_start, args.fuseless_end,
                )
                effective_summary["same_specimen_identity_bound"] = identity_bound
                effective_summary["coherent_repeated_rir_passes"] = (
                    rir_loaded["coherent_repeated_passes"]
                )
                effective_summary["capture_certified"] = rir_available
                effective_summary["conditional_pairing_blockers"] = []
                if not identity_bound:
                    effective_summary["conditional_pairing_blockers"].append(
                        "main and RIR inputs lack a shared strong UUID/full-BDF identity"
                    )
                if not rir_loaded["coherent_repeated_passes"]:
                    effective_summary["conditional_pairing_blockers"].append(
                        "fewer than two coherent RIR passes"
                    )
                if not same_artifact:
                    effective_summary["conditional_pairing_blockers"].append(
                        "MAIN and RIR are not bound by the same native artifact hash"
                    )
                if args.row_view not in ("physical", "locked_controller"):
                    effective_summary["conditional_pairing_blockers"].append(
                        "main rows are not a declared locked controller view; software-IFF redirection may affect them"
                    )
                elif not certified_controller_main_decode:
                    effective_summary["conditional_pairing_blockers"].append(
                        "MAIN native helper attestation did not validate"
                    )
                if not rir_attestation["available"]:
                    effective_summary["conditional_pairing_blockers"].append(
                        "RIR native helper attestation did not validate"
                    )
                if effective_rows is not None:
                    effective_summary["ff_iff_effect"] = {
                        "state": "unchanged_not_redecoded_through_rir",
                        "reason": (
                            "recovered RIR-on-fuse setter ignores "
                            "fuseless fuses; RIR applies to the conventional-fuse plane"
                        ),
                    }
                    effective_summary["logical_field_catalog_decode"] = (
                        decode_logical_field_catalog(
                            decoded, effective_rows
                        )
                    )
                    if header_registry is not None:
                        effective_summary["conventional_alias_decode"] = (
                            decode_conventional_alias_locations(
                                effective_rows, header_registry
                            )
                        )
                document["conditional_rir_effective_view"] = effective_summary
        document["rir_capture_certification"] = {
            **rir_attestation,
            "available": rir_available,
            "same_specimen_identity_bound": identity_bound,
            "same_native_capture_artifact": same_artifact,
            "reason": (
                "native MAIN and RIR helper envelopes validated and share one artifact"
                if rir_available else
                "exact RIR requires validated native MAIN/RIR envelopes, repeated "
                "coherence, shared identity, and one artifact hash"
            ),
        }
    document["exact_under_declared_assumptions"] = exact_under_declared_assumptions
    document["certified_controller_main_decode"] = certified_controller_main_decode
    document["conventional_ff_physical_region_certified"] = (
        certified_controller_main_decode
    )
    document["active_iff_region_certification"] = {
        "available": certified_controller_main_decode,
        "scope": "locked, software-redirection-excluded controller view",
        "immutable_otp_across_unlike_dsov_or_override_planes": False,
    }
    document["exact_physical_decode"] = False
    document["exact_physical_decode_deprecation"] = (
        "a whole 512-row immutable-physical claim is invalid because the active IFF "
        "window is controller-plane dependent; use certified_controller_main_decode"
    )
    document["controller_main_capture_certification"] = dict(main_attestation)
    document["controller_main_capture_certification"]["available"] = (
        certified_controller_main_decode
    )
    document["controller_main_capture_certification"]["reason"] = (
        "native ABI-%d helper envelope validated; unsigned-file authenticity "
        "still depends on an independently recorded artifact hash" % XRAY_HELPER_ABI
        if certified_controller_main_decode else
        "controller-view certification requires exact complete rows plus a validated "
        "native helper envelope; a CLI row-view label alone is insufficient"
    )
    document["physical_capture_certification"] = dict(
        document["controller_main_capture_certification"]
    )
    document["physical_capture_certification"]["scope_correction"] = (
        "physical conventional/FF region plus state-qualified active-IFF controller view"
    )
    document["warnings"] = []
    if len(normalized) < 2:
        document["warnings"].append("single pass: row coherence is unverified")
    if args.row_view not in ("physical", "locked_controller"):
        document["warnings"].append(
            "row view is not a certified locked controller view; software-IFF redirection may affect rows"
        )
    elif exact_under_declared_assumptions and not certified_controller_main_decode:
        document["warnings"].append(
            "controller row view is caller-declared but native collector attestation did not validate; planning remains blocked"
        )
    if args.row_view == "physical":
        document["warnings"].append(
            "legacy row-view label 'physical' does not make the active IFF interval immutable OTP"
        )
    if certified_controller_main_decode:
        document["warnings"].extend(main_attestation["warnings"])
    if args.sparse_policy != "reject":
        document["warnings"].append("sparse assumptions prevent an exact full-image claim")
    document["goal_assessments"] = goal_assessments(
        {}, physical_rows_available=physical_available, rir_available=rir_available
    )
    emit_json(document, args.output)
    return 0


def load_json_artifact(path, normalize_legacy=False):
    if path == "-":
        data = sys.stdin.buffer.read()
        source = "stdin"
    else:
        data = Path(path).read_bytes()
        source = path
    try:
        document = _strict_json_loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("artifact must be valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise AuditError("artifact root must be an object")
    if normalize_legacy:
        document = _normalize_legacy_xray_document(document)
    return document, source, sha256_bytes(data)


def canonical_json_sha256(value):
    encoded = _strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return sha256_bytes(encoded)


def attach_integrity(document):
    payload = dict(document)
    payload.pop("integrity", None)
    document["integrity"] = {
        "algorithm": "sha256-canonical-json-v1",
        "payload_sha256": canonical_json_sha256(payload),
        "scope": "entire root object except integrity",
    }
    return document


def verify_integrity(document):
    integrity = document.get("integrity")
    if not isinstance(integrity, dict):
        return {"present": False, "valid": False, "reason": "integrity block absent"}
    expected = integrity.get("payload_sha256")
    algorithm_ok = integrity.get("algorithm") == "sha256-canonical-json-v1"
    scope_ok = integrity.get("scope") == "entire root object except integrity"
    digest_shape_ok = (
        isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
    )
    payload = dict(document)
    payload.pop("integrity", None)
    actual = canonical_json_sha256(payload)
    return {
        "present": True,
        "valid": algorithm_ok and scope_ok and digest_shape_ok and expected == actual,
        "expected": expected,
        "actual": actual,
        "algorithm": integrity.get("algorithm"),
        "algorithm_valid": algorithm_ok,
        "scope_valid": scope_ok,
        "digest_shape_valid": digest_shape_ok,
    }


CATALOG_REGISTER_ALIASES = {
    "OPT_PCIE_MAGIC_BITS_D": "PCIE_MAGIC_BITS_D_GEN4_SPEED_DISABLE",
    "NV_FUSE_OPT_SM_SPEED_SELECT_DP": "OPT_SM_SPEED_SELECT_DP",
}


GOAL_DEPENDENCY_CLOSURE = {
    "persistent_dsov0": [
        "same-frame DSOV/CRC/PLM/row-0 attestation",
        "separate host-read, SENSE, GSP, FLR, bus-reset, warm-reboot and true-power-loss tests",
        "no promotion from same-session retention to cold persistence",
    ],
    "fuse_write_eligibility": [
        "exclusive unbound maintenance state", "controller idle and privilege state",
        "known programming voltage/current/timing", "full-prefix recovery journal",
    ],
    "80gb_hbm": [
        "FBP/FBPA/FBIO", "ROP/L2", "HBM site and stack health",
        "NoC/routing closure", "VBIOS memory policy and cold training",
    ],
    "more_sms": [
        "GPC/TPC/PES physical defect plane",
        "TPC disable/RECONFIG/control/override/effective layers",
        "per-die topology", "ROP/L2 coupling",
        "power/clock closure", "cold functional and stress validation",
    ],
    "pcie_gen4_x16": [
        "speed gates", "lane gates", "endpoint and root-port capability",
        "dated named Magic-D profile selection", "board signal integrity/equalization",
        "AER/Xid and sustained-transfer validation", "cold retraining",
    ],
    "nvlink": [
        "global and per-link option/defect/status", "physical bridge/routing",
        "VBIOS/firmware policy", "peer-link training",
    ],
    "ecc": [
        "primary/redundant conventional fuses", "feature override/readout",
        "memory-domain dependencies", "cold error-injection/telemetry validation",
    ],
    "mig": [
        "SW_SMC", "SYS_PIPE/GPC topology", "VBIOS/GSP/driver policy",
        "DevInit scratch and engine database", "memory/reset/free-channel closure",
        "profile enumeration and isolation validation",
    ],
    "nvdec_nvjpg": [
        "NVDEC/NVJPG/NVENC/OFA option and defect planes", "HS/power gates",
        "PTOP discovery and ucode/security revisions", "firmware/driver exposure",
        "authoritative API codec functional validation",
    ],
    "a100_identity_vbios": [
        "DEVIDA/DEVIDB", "subsystem identity", "signed compatible VBIOS",
        "external ROM recovery", "driver and board-policy compatibility",
        "separate SS0/SS1 software floorsweep after identity migration",
        "post-ID driver recipe re-selection and omitted CMP runtime policies",
    ],
    "oc_frequencies": [
        "rate/Vmin/bin/NAFLL fields", "board power and VRM limits",
        "reliability characterization", "thermal and long-duration validation",
    ],
}


PLANNING_MODELS = (
    "evidence-only", "public-model", "cross-checked-theory", "compare",
)

# These entries describe what dated public tooling/research currently models;
# they are not adopted as programming authority.  The planner keeps each claim
# separate from XRAY's cross-checked field catalog so a newer implementation
# cannot silently launder an older assumption into a proposed physical write.
PUBLIC_MODEL_CLAIMS = {
    "persistent_dsov0": [
        {
            "claim": "A successful voltage dip can produce a same-boot DSOV=0 observation window.",
            "source": "dalzyu/pico-ga100vfi and portz3r0 console",
            "source_date": "2026-11-23",
            "confidence_percent": 94,
            "disposition": "supported for a transient glitch phenotype; no cold-persistence mechanism is shown",
        },
        {
            "claim": "The current public console checks FUSECTRL for a 0xBADF poison tag before DSOV and makes the EN_SW_OVERRIDE=1 classification write opt-in.",
            "source": "portz3r0/pico-ga100vfi-console classify.py at 7a3c7a3, retained at aba33dc",
            "source_date": "2026-11-26",
            "confidence_percent": 100,
            "disposition": "confirmed implementation behavior; this corrects the pre-7a3c7a3 every-shot EN_SW write, while an operator-selected write still changes the boot-session state and is not a passive persistence test",
        },
        {
            "claim": "The current public campaign defaults to a 10-second inter-shot cooldown after a reported 100-shot/two-minute run fatigued the VRM.",
            "source": "portz3r0/pico-ga100vfi-console at aba33dc",
            "source_date": "2026-11-26",
            "confidence_percent": 90,
            "disposition": "the code/default and incident report are direct; ten seconds is a conservative project default, not an electrically qualified universal minimum for every board or fixture",
        },
        {
            "claim": "The repeated ROM tuple at 0x6896 is condition-table entry 147 testing (EN_SW_OVERRIDE & 1) == 0, not an EN_SW write.",
            "source": "deployed LS-UDE table bounds plus CONDITION_ENTRY schema/handler",
            "source_date": "2026-11-23",
            "confidence_percent": 99,
            "disposition": "disproves the address/value/flag write theory; selection and the operation gated by condition 147 remain unresolved",
        },
        {
            "claim": "The Nov-21 Danylo full profile reports Gen4 x16 on six cards and x8 on two cards with 60-second transfer/AER/Xid checks.",
            "source": "dstoro ga100-unlock-tools README and named full-profile implementation",
            "source_date": "2026-11-21",
            "confidence_percent": 72,
            "disposition": "newer multi-card functional report, but raw per-card logs and long-duration/cold automation are absent; keep bit25-only and full-word profiles distinct",
        },
    ],
    "fuse_write_eligibility": [
        {
            "claim": "DSOV=0, CRC_STATUS=0x55, idle controller and open privilege masks characterize the public Goldilocks window.",
            "source": "portz3r0/pico-ga100vfi-console",
            "source_date": "2026-11-23",
            "confidence_percent": 80,
            "disposition": "useful observation model, but insufficient without electrical, ownership, arm-state and recovery proof",
        },
    ],
    "80gb_hbm": [
        {
            "claim": "FBIO/FBPA/FBP/ROP-L2 disable records are the public minimum memory-topology edit set.",
            "source": "Kate GA100 map; dstoro tools; public CMP planners",
            "source_date": "2026-11-21",
            "confidence_percent": 84,
            "disposition": "field families are corroborated; per-die masks, HBM routing, RIR and final-prefix replay remain mandatory",
        },
    ],
    "more_sms": [
        {
            "claim": "GPC/TPC disable masks can expose additional SMs when the corresponding defect masks are clear.",
            "source": "Kate GA100 map; CMP ffcalc/planners; four-card observations",
            "source_date": "2026-11-21",
            "confidence_percent": 88,
            "disposition": "directionally supported; disable-only planners omit RECONFIG/control/override/PES and functional health closure",
        },
    ],
    "pcie_gen4_x16": [
        {
            "claim": "MAGIC_BITS_D plus PCIe boot-speed fields participate in Gen4 enablement.",
            "source": "cmp170hx-ffcalc commit 94a16e5572a7 and GA100 headers",
            "source_date": "2026-11-17",
            "confidence_percent": 70,
            "disposition": "field identities are supported; the public --a100 value is explicitly experimental and FF-bit/MMIO-bit coordinates must not be conflated",
        },
    ],
    "nvlink": [
        {
            "claim": "A three-bit NVLINK disable option and a separate per-link disable plane exist.",
            "source": "GA100 differential map and generated/header register identities",
            "source_date": "2026-11-21",
            "confidence_percent": 86,
            "disposition": "global and per-link planes are distinct; no complete physical edit or board-link recipe is public",
        },
    ],
    "ecc": [
        {
            "claim": "ECC_EN and ECC_EN_CONTROL occupy conventional primary/redundant rows 6/7 bits 28/29.",
            "source": "GA100 direct alias macros",
            "source_date": "2026-11-21",
            "confidence_percent": 99,
            "disposition": "coordinate identity is strong; sufficiency and safe conventional-fuse programming are unproved",
        },
        {
            "claim": "One externally tested conversion recipe added row-6/7 bit 28 on both conventional mirrors, but did not isolate ECC from the rest of the campaign.",
            "source": "portz3r0/pico-ga100vfi-console advanced_unlocks.py/evidence recipe at aba33dc",
            "source_date": "2026-11-26",
            "confidence_percent": 75,
            "disposition": "useful campaign evidence only; XRAY records rows 6/7 bits 28/29 but its FF-only executor deliberately cannot program conventional rows and functional ECC still requires cold mode plus injection/correction tests",
        },
    ],
    "mig": [
        {
            "claim": "SW_SMC_DISABLE and SYS_PIPE/GPC floorsweep planes gate MIG exposure.",
            "source": "GA100 aliases, differential map and generated shadows",
            "source_date": "2026-11-21",
            "confidence_percent": 78,
            "disposition": "necessary-looking dependencies only; GSP/VBIOS/driver policy and isolation validation are missing",
        },
        {
            "claim": "Chain-1 bits 372..379 clear the eight-bit SYS_PIPE disable word and can expose eight MIG pipes on a defect-free specimen.",
            "source": "repeaterdroid/cmp170hx-mig-nvdec-nvjpg-ofa-unlock e3826fee",
            "source_date": "2026-11-22",
            "confidence_percent": 94,
            "disposition": "strong one-card register/PTOP/functional result; direct SYS_PIPE_DEFECTIVE is mandatory and one local card has bit 7 defective, so eight pipes are not universal",
        },
    ],
    "nvdec_nvjpg": [
        {
            "claim": "NVDEC/NVENC/NVJPG/OFA disable, CP, defect, status and security/ucode gates exist.",
            "source": "2026-11-21 generated shadow maps and GA100 headers",
            "source_date": "2026-11-21",
            "confidence_percent": 92,
            "disposition": "register identities are broad; public physical FF coordinates and a complete enable sequence are absent",
        },
        {
            "claim": "A converted 20b5 A100-80GB specimen with 126 SM and Gen4 x16 still exposes zero NVDEC and no hardware NVJPG through authoritative APIs.",
            "source": "Discord attachment nvmedia-test-results-pve1-2026-11-21-e3933d01012650cc.zip",
            "source_date": "2026-11-21",
            "confidence_percent": 95,
            "disposition": "strong negative result for that exact card; later screenshot-only positive claims do not identify or prove an enable mechanism",
        },
        {
            "claim": "Chain-1 bits 139..143 map to five NVDEC disables, while the unordered pair {146,371} maps to the NVJPG/OFA disables.",
            "source": "repeaterdroid/cmp170hx-mig-nvdec-nvjpg-ofa-unlock e3826fee",
            "source_date": "2026-11-22",
            "confidence_percent": 88,
            "disposition": "one-card append/register/PTOP/API report; NVDEC coordinate is stronger (90%), but individual NVJPG/OFA bit attribution is zero and only paired clearing is supported",
        },
        {
            "claim": "STATUS_OPT equals OPT_DISABLE proves that every media unit is defect-free because GA100 has no separate media defect registers.",
            "source": "cmp170hx-ffcalc 3d157f254991904674fb0f47d5e236ac4f9b5ce7",
            "source_date": "2026-11-25",
            "confidence_percent": 0,
            "disposition": "rejected: GA100 exposes distinct NVDEC/NVJPG/OFA defect words, and with an all-one disable mask status equality hides every possible defect; require the direct defect register",
        },
    ],
    "a100_identity_vbios": [
        {
            "claim": "DEVIDA/DEVIDB can move the exposed identity toward A100 before a signed A100 VBIOS is attempted.",
            "source": "Kate map, retained extraction and CMP conversion research",
            "source_date": "2026-11-21",
            "confidence_percent": 88,
            "disposition": "identity fields are well correlated; ROM compatibility, recovery and post-conversion software floorsweep remain separate gates",
        },
        {
            "claim": "Changing the ID from 20c2 to 20b5 changes current gpu-unlock recipe selection from the full CMP GA100 recipe to a reduced ga100_hbm recipe.",
            "source": "Zagitta/gpu-unlock 3.0.0-beta recipes.c",
            "source_date": "2026-11-19",
            "confidence_percent": 99,
            "disposition": "code-level dependency trap: re-prove ECC, PCIe relink, SS/RECONFIG and handover policy after identity migration",
        },
    ],
    "oc_frequencies": [
        {
            "claim": "SS0 at 0x82381c covers IMLA0-3/FMLA16/FMLA32/FFMA/DP, while SS1 at 0x823820 covers IMLA4 rather than FP64.",
            "source": "GA100 FEATURE_OVERRIDE field descriptors and JRex286 fuse gist",
            "source_date": "2026-11-26 revalidation",
            "confidence_percent": 95,
            "disposition": "register/field identity supported; exact runtime values and scalar-versus-tensor coverage require independent benchmarks",
        },
        {
            "claim": "A tested external conversion recipe used IFF WRITE_DW pairs 0x082381fd/0x88888888 and 0x0823823d/0x00000008 to target SS0 at 0x82381c and SS1 at 0x823820.",
            "source": "portz3r0/pico-ga100vfi-console advanced_unlocks.py at aba33dc",
            "source_date": "2026-11-26",
            "confidence_percent": 85,
            "disposition": "exact reported recipe, not an official descriptor: rows 484..487 are specimen allocation evidence, not universal placement; XRAY exposes this only as a non-executable IFF candidate and does not pass it to the FF-only OTP executor",
        },
        {
            "claim": "Conventional row 6/7 bit 27 can undo the adjacent DP throttle at bit 26.",
            "source": "GA100 DP_CONTROL alias name plus current Lab report",
            "source_date": "2026-11-26",
            "confidence_percent": 65,
            "disposition": "plausible candidate only: static samples do not vary bit 27 and no isolated functional/cold result is locally sealed",
        },
        {
            "claim": "A100 runtime clock-control patches expand software clock ranges.",
            "source": "Zagitta/gpu-unlock 3.0.0-beta and supplied a100-oc archive",
            "source_date": "2026-11-19",
            "confidence_percent": 82,
            "disposition": "runtime patch evidence does not establish a portable fuse-burn recipe",
        },
    ],
}


def _normalized_identity(source):
    """Flatten only explicit, scalar specimen identity fields."""
    if not isinstance(source, dict):
        return {}
    result = {}
    nested_candidates = [source]
    for key in ("card", "selected_card", "identity", "xray_helper_identity"):
        nested = source.get(key)
        if isinstance(nested, dict):
            nested_candidates.extend([nested])
            if isinstance(nested.get("card"), dict):
                nested_candidates.append(nested["card"])
    aliases = {
        "domain_bdf": ("domain_bdf", "pci_bdf", "bdf", "pci", "pci_address"),
        "vendor": ("vendor",),
        "device": ("device",),
        "subsystem_vendor": ("subsystem_vendor", "subvendor"),
        "subsystem_device": ("subsystem_device", "subdevice"),
        "revision": ("revision",),
        "uuid": ("uuid",),
        "vbios_version": ("vbios_version",),
        "reported_name": ("reported_name", "name"),
        "helper_generation": ("generation", "helper_generation"),
    }
    for target, source_names in aliases.items():
        for candidate in nested_candidates:
            for source_name in source_names:
                value = candidate.get(source_name)
                if value is not None and not isinstance(value, (dict, list)):
                    result.setdefault(target, value)
                    break
            if target in result:
                break
    bdf = result.get("domain_bdf")
    if isinstance(bdf, str) and BDF_RE.fullmatch(bdf):
        result["domain_bdf"] = bdf.lower()
    return result


def _strict_identity_status(identity):
    errors = []
    if not isinstance(identity, dict):
        return {"valid": False, "errors": ["identity is not an object"]}
    bdf = identity.get("domain_bdf")
    if not isinstance(bdf, str) or BDF_RE.fullmatch(bdf) is None:
        errors.append("canonical full domain BDF is absent")
    elif bdf != bdf.lower():
        errors.append("canonical full domain BDF must be lowercase")
    for key in (
        "vendor", "device", "subsystem_vendor", "subsystem_device", "revision"
    ):
        try:
            value = parse_int(identity.get(key), "identity %s" % key)
        except AuditError:
            errors.append("%s is absent or invalid" % key)
            continue
        limit = 0xFF if key == "revision" else 0xFFFF
        if not 0 <= value <= limit:
            errors.append("%s is out of range" % key)
    try:
        vendor = parse_int(identity.get("vendor"), "identity vendor")
        device = parse_int(identity.get("device"), "identity device")
        if vendor != 0x10DE or device not in GA100_READ_TARGET_DEVICE_IDS:
            errors.append("identity is not an allowlisted GA100 read/attestation target")
    except AuditError:
        pass
    return {"valid": not errors, "errors": errors}


def _artifact_identity(specimen):
    card = specimen.get("card") if isinstance(specimen, dict) else None
    if not isinstance(card, dict):
        card = {}
    identity = _normalized_identity(card)
    if not identity and isinstance(specimen, dict):
        input_object = specimen.get("input", {})
        if isinstance(input_object, dict):
            identity = _normalized_identity(input_object.get("selected_card"))
    driver_queries = specimen.get("passive_driver_queries", {})
    smi = driver_queries.get("nvidia_smi", {}) if isinstance(driver_queries, dict) else {}
    features = smi.get("features", {}) if isinstance(smi, dict) else {}
    parsed = features.get("parsed", {}) if isinstance(features, dict) else {}
    if isinstance(parsed, dict):
        for source_name, target_name in (
            ("uuid", "uuid"), ("vbios_version", "vbios_version"),
            ("name", "reported_name"), ("memory.total", "memory_total_mib"),
            ("driver_version", "driver_version"),
        ):
            if source_name in parsed:
                identity[target_name] = parsed[source_name]
    return identity


def _specimen_matches(specimen, index, selector):
    if selector is None:
        return True
    identity = _artifact_identity(specimen)
    candidates = _selector_identities(specimen, index)
    candidates.update(_selector_identities(identity))
    return selector in candidates


def select_plan_specimens(document, selector):
    cards = document.get("cards")
    if isinstance(cards, list):
        selected = [
            (index, card) for index, card in enumerate(cards)
            if isinstance(card, dict) and _specimen_matches(card, index, selector)
        ]
        if selector is not None and len(selected) != 1:
            raise AuditError("--card %r matched %d capture cards" % (selector, len(selected)))
        if not selected:
            raise AuditError("capture contains no selectable cards")
        return selected
    if selector not in (None, "0"):
        selected_card = document.get("input", {}).get("selected_card")
        identities = _selector_identities(selected_card, 0)
        if selector not in identities:
            raise AuditError("--card does not match this single-card analysis")
    return [(0, document)]


def validate_plan_source(document):
    allowed_schemas = {SCHEMA_VERSION, "ga100-readonly-audit-v1"}
    if document.get("schema") not in allowed_schemas:
        raise AuditError(
            "unsupported plan-source schema: %r" % document.get("schema")
        )
    if document.get("command") not in ("capture", "live", "analyze-rows"):
        raise AuditError(
            "plan source must be a capture/live/analyze-rows artifact"
        )
    if document.get("command") in ("capture", "live"):
        cards = document.get("cards")
        if not isinstance(cards, list) or not all(isinstance(item, dict) for item in cards):
            raise AuditError("capture plan source must contain a cards object list")
        for index, card in enumerate(cards):
            for key in (
                "card", "capture", "physical_rows", "rir_rows", "controller"
            ):
                if key in card and not isinstance(card[key], dict):
                    raise AuditError("card %d %s is not an object" % (index, key))
            if "plane_context" in card and not isinstance(card["plane_context"], dict):
                raise AuditError("card %d plane_context is not an object" % index)
            coherence = card.get("capture", {}).get("coherence", {})
            if not isinstance(coherence, dict):
                raise AuditError("card %d capture coherence is not an object" % index)
            registers = card.get("registers", {})
            if not isinstance(registers, dict) or not all(
                isinstance(value, dict) for value in registers.values()
            ):
                raise AuditError("card %d has malformed register observations" % index)
    else:
        if not isinstance(document.get("analysis"), dict):
            raise AuditError("analyze-rows plan source lacks an analysis object")
        input_info = document.get("input")
        if not isinstance(input_info, dict):
            raise AuditError("analyze-rows plan source lacks an input object")
        passes = input_info.get("passes")
        if not isinstance(passes, list) or not passes or not all(
            isinstance(item, dict) for item in passes
        ):
            raise AuditError("analyze-rows input passes must be a non-empty object list")
        if "rir_input" in document and not isinstance(document["rir_input"], dict):
            raise AuditError("analyze-rows rir_input is not an object")
        for key in ("rir_capture_certification", "rir_analysis"):
            if key in document and not isinstance(document[key], dict):
                raise AuditError("analyze-rows %s is not an object" % key)


def _catalog_for_register(register_name):
    wanted = CATALOG_REGISTER_ALIASES.get(register_name, register_name)
    return [item for item in LOGICAL_FIELD_CATALOG if item["name"] == wanted]


def _register_observation(registers, register_name):
    """Resolve one observation without collapsing distinct register addresses."""
    if register_name in registers:
        return register_name, registers[register_name]
    metadata = REGISTER_BY_NAME.get(register_name)
    if metadata is None:
        return None, None
    candidates = (metadata["name"],) + tuple(metadata.get("aliases", ()))
    matches = [(name, registers[name]) for name in candidates if name in registers]
    if not matches:
        return None, None
    addresses = {
        REGISTER_BY_NAME[name]["address"]
        for name, _value in matches if name in REGISTER_BY_NAME
    }
    if len(addresses) > 1:
        raise AuditError("register aliases resolve to multiple addresses: %s" % register_name)
    return matches[0]


def _planning_model_view(goal_name, definition, planning_model):
    public_claims = [dict(item) for item in PUBLIC_MODEL_CLAIMS.get(goal_name, ())]
    cross_checked = []
    seen = set()
    for register_name in definition["registers"]:
        catalog_name = CATALOG_REGISTER_ALIASES.get(register_name, register_name)
        for item in _catalog_for_register(register_name):
            key = (catalog_name, item.get("plane"), item.get("chain"), item.get("bit"))
            if key in seen:
                continue
            seen.add(key)
            cross_checked.append({
                "field": catalog_name,
                "candidate": dict(item),
                "programming_coordinate_authoritative": False,
                "usable_for_execution": False,
            })
    cross_checked.sort(key=lambda item: item["field"])
    common = {
        "selected_model": planning_model,
        "executable": False,
        "physical_operations": [],
        "authority_boundary": (
            "model output is a hypothesis/evidence comparison; only an official "
            "GA100 descriptor plus per-die complete-image proofs could promote a "
            "coordinate to an execution candidate"
        ),
    }
    if planning_model == "evidence-only":
        common["evidence_only"] = True
        common["public_claims_used"] = False
        common["cross_checked_theory_used"] = False
    elif planning_model == "public-model":
        common["public_model"] = {
            "claims": public_claims,
            "adopted_as_authority": False,
            "date_policy": "newer claims are preferred only when independently corroborated",
        }
    elif planning_model == "cross-checked-theory":
        common["cross_checked_theory"] = {
            "logical_field_candidates": cross_checked,
            "official_descriptor_available": False,
            "all_candidates_non_authoritative": True,
        }
    elif planning_model == "compare":
        common["public_model"] = {
            "claims": public_claims,
            "adopted_as_authority": False,
        }
        common["cross_checked_theory"] = {
            "logical_field_candidates": cross_checked,
            "official_descriptor_available": False,
            "all_candidates_non_authoritative": True,
        }
        common["comparison"] = {
            "shared": (
                "named field families and direct shadow identities may be useful "
                "observation targets"
            ),
            "xray_additions": [
                "preserve unknown/missing values instead of filling zero",
                "separate direct MMIO bits from FF logical coordinates",
                "require defect/control/status/dependency closure per die",
                "replay FF, IFF and RIR after every irreversible prefix",
                "recompute the final shared FF/IFF frontier and reserve",
            ],
            "unresolved": list(definition["unknowns"]),
        }
    else:
        raise AuditError("unknown planning model: %s" % planning_model)
    return common


def _plan_evidence(document, specimen):
    if specimen is document and document.get("command") == "analyze-rows":
        registers = {}
        row_view = document.get("input", {}).get("row_view", "unknown")
        analysis = document.get("analysis", {})
        rir_exact = bool(
            document.get("rir_capture_certification", {}).get("available") is True
            and document.get("rir_input", {}).get("same_specimen_identity_bound") is True
            and document.get("rir_analysis", {}).get("repeated_decodeable_replay") is True
        )
        row_hashes = [
            item.get("row_vector_sha256")
            for item in document.get("input", {}).get("passes", [])
            if item.get("row_vector_sha256")
        ]
        physical_certification = document.get(
            "controller_main_capture_certification",
            document.get("physical_capture_certification", {}),
        )
        if not isinstance(physical_certification, dict):
            physical_certification = {}
        exact_controller_main = bool(
            document.get("certified_controller_main_decode") is True
            and physical_certification.get("available") is True
        )
        legacy_exact_physical_claim_ignored = bool(
            document.get("exact_physical_decode") is True
            and document.get("certified_controller_main_decode") is not True
        )
        software_iff_provenance = physical_certification.get(
            "software_iff_provenance", {}
        )
        if not isinstance(software_iff_provenance, dict):
            software_iff_provenance = {}
        redirection = {
            "caller_declared_row_view": row_view,
            "caller_declared_physical": row_view == "physical",
            "legacy_exact_physical_claim_ignored": (
                legacy_exact_physical_claim_ignored
            ),
            "collector_attestation_verified": bool(
                physical_certification.get("available") is True
            ),
            "software_iff_redirection_excluded": bool(
                physical_certification.get("available") is True
                and software_iff_provenance.get(
                    "software_iff_redirection_excluded"
                ) is True
            ),
            "collector_provenance": software_iff_provenance,
        }
        capture_coherent = document.get("input", {}).get("coherent") is True
        ga100_semantically_valid = analysis.get("ga100_semantically_valid") is True
        catalog_decode = analysis.get("logical_field_catalog_decode", {})
        if not isinstance(catalog_decode, dict):
            catalog_decode = {}
        catalog_entries = catalog_decode.get("entries", [])
        if not isinstance(catalog_entries, list):
            catalog_entries = []
        catalog_fields = {
            item["name"]: item for item in catalog_entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    else:
        physical_rows = specimen.get("physical_rows", {})
        if not isinstance(physical_rows, dict):
            physical_rows = {}
        registers = specimen.get("registers", {})
        if not isinstance(registers, dict):
            registers = {}
        analysis = specimen.get("analysis", {})
        rir_exact = specimen.get("rir_rows", {}).get("available") is True
        row_hashes = [
            value for value in physical_rows.get("pass_hashes", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        ]
        plane_context = specimen.get("plane_context", {})
        physical_certification = physical_rows.get("certification", {})
        if not isinstance(physical_certification, dict):
            physical_certification = {}
        software_iff_provenance = physical_certification.get(
            "software_iff_provenance", {}
        )
        if not isinstance(software_iff_provenance, dict):
            software_iff_provenance = {}
        exact_controller_main = bool(
            physical_rows.get("available") is True
            and physical_rows.get("coherent") is True
            and physical_certification.get("available") is True
            and physical_certification.get(
                "software_iff_redirection_excluded"
            ) is True
            and software_iff_provenance.get(
                "software_iff_redirection_excluded"
            ) is True
            and len(row_hashes) >= 2
            and len(set(row_hashes)) == 1
        )
        redirection = {
            "software_override_active": plane_context.get("software_override_active"),
            "iff_record": specimen.get("controller", {}).get("iff_record"),
            "software_iff_redirection_excluded": bool(
                physical_certification.get("software_iff_redirection_excluded") is True
                and software_iff_provenance.get(
                    "software_iff_redirection_excluded"
                ) is True
            ),
            "physical_iff_interval_observation": physical_rows.get(
                "physical_iff_interval_observation"
            ),
            "controller_iff_interval_observation": physical_rows.get(
                "controller_iff_interval_observation",
                physical_rows.get("physical_iff_interval_observation"),
            ),
            "collector_provenance": software_iff_provenance,
        }
        coherence = specimen.get("capture", {}).get("coherence", {})
        capture_coherent = coherence.get("coherent") is True
        ga100_semantically_valid = False
        catalog_fields = {}
    return {
        "exact_controller_main_512_rows": exact_controller_main,
        "physical_conventional_ff_region_certified": exact_controller_main,
        "immutable_physical_active_iff_certified": False,
        "exact_physical_512_rows": False,
        "row_vector_hashes": row_hashes,
        "registers": registers,
        "rir_exact_repeated_capture": rir_exact,
        "analysis": analysis,
        "capture_coherent": capture_coherent,
        "ga100_semantically_valid": ga100_semantically_valid,
        "offline_catalog_fields": catalog_fields,
        "redirection_provenance": redirection,
        "official_ga100_descriptor_available": False,
    }


def build_goal_plan(goal_name, evidence, planning_model="evidence-only"):
    definition = GOALS[goal_name]
    registers = evidence["registers"]
    resolved = {
        name: _register_observation(registers, name)
        for name in definition["registers"]
    }
    observed = sorted(
        name for name, (_source_name, observation) in resolved.items()
        if observation is not None and observation.get("status") == "ok"
    )
    unusable = sorted(
        name for name, (_source_name, observation) in resolved.items()
        if observation is not None and observation.get("status") != "ok"
    )
    missing = sorted(
        name for name, (_source_name, observation) in resolved.items()
        if observation is None
    )
    likely_fields = []
    for register_name in definition["registers"]:
        catalog_entries = _catalog_for_register(register_name)
        catalog_name = CATALOG_REGISTER_ALIASES.get(register_name, register_name)
        likely_fields.append({
            "register": register_name,
            "observed": register_name in observed,
            "observed_but_unusable": register_name in unusable,
            "observation_source_name": resolved[register_name][0],
            "logical_mapping": catalog_entries[0] if catalog_entries else None,
            "offline_catalog_decode": evidence.get(
                "offline_catalog_fields", {}
            ).get(catalog_name),
            "mapping_confirmed_for_programming": False,
        })
    defect_names = sorted(
        name for name in definition["registers"]
        if name in REGISTER_BY_NAME and REGISTER_BY_NAME[name].get("role") == "defect"
    )
    defect_missing = [name for name in defect_names if name not in observed]
    blockers = [
        "official GA100 fuse descriptor is absent",
        "all logical coordinates remain non-authoritative for programming",
        "no hash-bound complete old/new 512-row image exists",
        (
            "the built-in planner emits non-executable plan-v1 evidence; it does "
            "not produce or authorize the separately sealed v3 append manifest"
        ),
    ]
    if not evidence["capture_coherent"]:
        blockers.append("source capture is absent, single-pass, or not coherent")
    if evidence.get("analysis") and not evidence["ga100_semantically_valid"]:
        blockers.append("FF/IFF analysis is not GA100-semantically valid")
    if not evidence["exact_controller_main_512_rows"]:
        blockers.append("two coherent certified locked-controller 512-row passes are absent")
    if not evidence["rir_exact_repeated_capture"]:
        blockers.append("two coherent RIR passes and effective-repair replay are absent")
    if not evidence["redirection_provenance"].get("software_iff_redirection_excluded"):
        blockers.append("software-IFF redirection has not been excluded for the row view")
    if missing or unusable:
        blockers.append("required option/control/status/defect observations are incomplete")
    if defect_missing:
        blockers.append("per-die defect evidence is incomplete")
    return {
        "goal": goal_name,
        "status": "blocked",
        "executable": False,
        "strategy": definition["strategy"],
        "planning_model": _planning_model_view(
            goal_name, definition, planning_model
        ),
        "dependency_closure": GOAL_DEPENDENCY_CLOSURE.get(goal_name, []),
        "observed_registers": observed,
        "observed_but_unusable_registers": unusable,
        "missing_registers": missing,
        "defect_registers": defect_names,
        "missing_defect_registers": defect_missing,
        "likely_fields": likely_fields,
        "known_unknowns": list(definition["unknowns"]),
        "blockers": blockers,
        "required_sequence": [
            "capture passive identity/status/defect planes repeatedly",
            "maintenance window: obtain two coherent certified locked-controller MAIN and RIR captures",
            "prove exact row view, descriptor bounds and redirection provenance",
            "construct candidate only on a clone of the complete old image",
            "replay FF, RIR and IFF after every proposed physical prefix",
            "reject every 1->0 request, no-op burn, unknown field and dependency gap",
            "rescan final IFF boundary/frontier and prove B_final-F_final reserve",
            "bind identity and hashes; cold-reset and functionally validate",
        ],
        "proposed_physical_operations": [],
    }


def build_manifest_skeleton(identity, evidence, goal_plans, source_sha256):
    analysis = evidence.get("analysis", {})
    boundary = analysis.get("boundary") if isinstance(analysis, dict) else None
    return {
        "manifest_schema": "170hx-xray-write-manifest-v1",
        "identity": identity,
        "source_artifact_sha256": source_sha256,
        "source_row_vector_hashes": evidence["row_vector_hashes"],
        "row_view_and_redirection": evidence["redirection_provenance"],
        "descriptor_bounds": {
            "official": False,
            "observed_decode_boundary": boundary,
        },
        "old_complete_image": None,
        "proposed_complete_image": None,
        "logical_edits": [],
        "physical_operations": [],
        "prefix_replays": [],
        "monotonicity_proof": None,
        "rir_replay_proof": None,
        "iff_final_frontier_proof": None,
        "recovery_reserve_proof": None,
        "requested_goals": [item["goal"] for item in goal_plans],
        "executable": False,
        "execution_blocked_reason": (
            "evidence/planning skeleton only; no operation may be inferred from null fields"
        ),
    }


def summarize_reference_plan(path):
    if path == "-":
        raise AuditError("--reference-plan cannot use stdin while the plan source may use it")
    data = Path(path).read_bytes()
    if len(data) > 128 * 1024 * 1024:
        raise AuditError("reference plan exceeds 128 MiB: %s" % path)
    summary = {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
        "imported_as_authority": False,
        "used_to_construct_operations": False,
    }
    try:
        root = _strict_json_loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError, AuditError):
        summary.update({
            "format": "opaque-text-or-binary",
            "schema": None,
            "declared_executable": None,
            "declared_operation_count": None,
            "reason": "not JSON; retained only by path/hash",
        })
        return summary
    if not isinstance(root, (dict, list)):
        raise AuditError("reference plan JSON root must be an object or list: %s" % path)
    summary["format"] = "json"
    if isinstance(root, list):
        summary.update({
            "schema": None,
            "root_type": "list",
            "root_item_count": len(root),
            "declared_executable": None,
            "declared_operation_count": None,
        })
        return summary
    summary["root_type"] = "object"
    summary["schema"] = root.get("artifact_schema", root.get("schema"))
    summary["policy"] = root.get("policy")
    summary["command"] = root.get("command")
    summary["tool"] = root.get("tool") if isinstance(root.get("tool"), str) else (
        root.get("tool", {}).get("name") if isinstance(root.get("tool"), dict) else None
    )
    summary["declared_executable"] = root.get("executable")
    declared_counts = []
    for key in ("operations", "writes", "records", "burns", "plan"):
        value = root.get(key)
        if isinstance(value, list):
            declared_counts.append({"path": key, "count": len(value)})
    reference_specimens = root.get("specimens", ())
    if not isinstance(reference_specimens, list):
        reference_specimens = ()
    for specimen_index, specimen in enumerate(reference_specimens):
        if not isinstance(specimen, dict):
            continue
        skeleton = specimen.get("manifest_skeleton", {})
        if isinstance(skeleton, dict) and isinstance(skeleton.get("physical_operations"), list):
            declared_counts.append({
                "path": "specimens[%d].manifest_skeleton.physical_operations" % specimen_index,
                "count": len(skeleton["physical_operations"]),
            })
    summary["declared_operation_lists"] = declared_counts
    summary["declared_operation_count"] = sum(item["count"] for item in declared_counts)
    summary["interpretation"] = (
        "for comparison only; schema names, executable flags and operation counts "
        "are untrusted declarations until independently reconstructed"
    )
    return summary


def command_plan(args):
    canonical_access = canonical_access_mode(args.access)
    if canonical_access != "plan":
        raise AuditError("plan command requires --access plan")
    document, source, source_sha256 = load_json_artifact(
        args.input, normalize_legacy=True
    )
    validate_plan_source(document)
    selected_goals = args.goal or sorted(GOALS)
    reference_plans = [summarize_reference_plan(path) for path in args.reference_plan]
    result = base_document("plan")
    result["artifact_schema"] = "170hx-xray-plan-v1"
    result["access"] = {
        "requested_access": args.access,
        "canonical_requested_access": canonical_access,
        "effective_access": "offline-plan",
        "live_device_access": False,
        "writes_performed": 0,
    }
    result["source_artifact"] = {
        "path": source,
        "sha256": source_sha256,
        "schema": document.get("schema"),
        "command": document.get("command"),
    }
    result["requested_goals"] = selected_goals
    result["planning_model"] = args.planning_model
    result["reference_plans"] = reference_plans
    result["specimens"] = []
    for index, specimen in select_plan_specimens(document, args.card):
        evidence = _plan_evidence(document, specimen)
        identity = _artifact_identity(specimen)
        if not identity and specimen is document:
            selected_card = document.get("input", {}).get("selected_card")
            identity = _normalized_identity(selected_card)
        plans = [
            build_goal_plan(goal, evidence, args.planning_model)
            for goal in selected_goals
        ]
        result["specimens"].append({
            "index": index,
            "identity": identity,
            "evidence_state": {
                key: value for key, value in evidence.items()
                if key != "registers" and key != "analysis"
            },
            "goal_plans": plans,
            "manifest_skeleton": build_manifest_skeleton(
                identity, evidence, plans, source_sha256
            ),
            "executable": False,
        })
    result["executable"] = False
    result["operations_total"] = 0
    result["planner_invariant"] = (
        "missing evidence remains unknown; it is never filled with zero or copied from another die"
    )
    attach_integrity(result)
    emit_json(result, args.output)
    return 0


def _validate_write_plan_structure(manifest):
    if (
        manifest.get("artifact_schema") != "170hx-xray-plan-v1"
        or manifest.get("command") != "plan"
        or manifest.get("schema") != SCHEMA_VERSION
    ):
        raise AuditError(
            "write readiness accepts only a 170hx-xray-plan-v1 artifact from this schema"
        )
    specimens = manifest.get("specimens")
    if not isinstance(specimens, list) or not specimens:
        raise AuditError("plan has no specimen manifests")
    if manifest.get("executable") is not False:
        raise AuditError("plan must explicitly declare executable=false")
    operations = []
    structural_checks = []
    canonical_bdfs = []
    source_binding = manifest.get("source_artifact", {}).get("sha256")
    for index, specimen in enumerate(specimens):
        if not isinstance(specimen, dict):
            raise AuditError("specimen %d is not an object" % index)
        skeleton = specimen.get("manifest_skeleton")
        if not isinstance(skeleton, dict):
            raise AuditError("specimen %d lacks a manifest skeleton" % index)
        specimen_operations = skeleton.get("physical_operations")
        if not isinstance(specimen_operations, list):
            raise AuditError("specimen %d physical_operations is not a list" % index)
        operations.extend(specimen_operations)
        if skeleton.get("executable") is not False:
            raise AuditError("specimen %d manifest must declare executable=false" % index)
        skeleton_identity = skeleton.get("identity")
        specimen_identity = specimen.get("identity")
        identity_status = _strict_identity_status(skeleton_identity)
        identity_consistent = bool(
            isinstance(specimen_identity, dict)
            and specimen_identity == skeleton_identity
        )
        bdf = (
            skeleton_identity.get("domain_bdf")
            if isinstance(skeleton_identity, dict) else None
        )
        canonical_bdfs.append(bdf.lower() if isinstance(bdf, str) else bdf)
        structural_checks.append({
            "specimen": index,
            "identity_present": identity_status["valid"] and identity_consistent,
            "identity_schema_valid": identity_status["valid"],
            "identity_consistent_with_specimen": identity_consistent,
            "identity_errors": identity_status["errors"],
            "source_artifact_bound": bool(
                isinstance(source_binding, str)
                and skeleton.get("source_artifact_sha256") == source_binding
            ),
            "old_complete_image_present": skeleton.get("old_complete_image") is not None,
            "proposed_complete_image_present": skeleton.get("proposed_complete_image") is not None,
            "source_row_hashes_present": bool(skeleton.get("source_row_vector_hashes")),
            "official_descriptor_declared": bool(
                skeleton.get("descriptor_bounds", {}).get("official")
                if isinstance(skeleton.get("descriptor_bounds"), dict) else False
            ),
            "monotonicity_proof_present": skeleton.get("monotonicity_proof") is not None,
            "prefix_replays_present": bool(skeleton.get("prefix_replays")),
            "rir_replay_proof_present": skeleton.get("rir_replay_proof") is not None,
            "iff_final_frontier_proof_present": (
                skeleton.get("iff_final_frontier_proof") is not None
            ),
            "recovery_reserve_proof_present": (
                skeleton.get("recovery_reserve_proof") is not None
            ),
            "declared_executable": bool(skeleton.get("executable")),
            "operation_count": len(specimen_operations),
        })
    duplicate_bdfs = {
        bdf for bdf in canonical_bdfs
        if bdf is not None and canonical_bdfs.count(bdf) > 1
    }
    if duplicate_bdfs:
        for check, bdf in zip(structural_checks, canonical_bdfs):
            if bdf in duplicate_bdfs:
                check["identity_present"] = False
                check["identity_unique"] = False
                check["identity_errors"].append("duplicate target BDF")
            else:
                check["identity_unique"] = True
    else:
        for check in structural_checks:
            check["identity_unique"] = True
    return {
        "specimens": specimens,
        "operations": operations,
        "structural_checks": structural_checks,
    }


def _load_validated_write_plan(path):
    manifest, source, source_sha256 = load_json_artifact(path)
    integrity = verify_integrity(manifest)
    if not integrity["valid"]:
        raise AuditError("manifest integrity validation failed: %s" % integrity)
    structure = _validate_write_plan_structure(manifest)
    structure.update({
        "manifest": manifest,
        "source": source,
        "source_sha256": source_sha256,
        "integrity": integrity,
    })
    return structure


def command_apply(args):
    canonical_access = canonical_access_mode(args.access)
    if canonical_access != "volatile-write":
        raise AuditError(
            "apply requires --access volatile-write (legacy alias: read-write)"
        )
    validated = _load_validated_write_plan(args.manifest)
    manifest = validated["manifest"]
    source = validated["source"]
    source_sha256 = validated["source_sha256"]
    integrity = validated["integrity"]
    operations = validated["operations"]
    structural_checks = validated["structural_checks"]
    if operations:
        raise AuditError(
            "this release cannot validate operation semantics; non-empty physical operations are refused"
        )
    if args.execute:
        raise AuditError(
            "real execution is unsupported: this build has no volatile or OTP write backend"
        )
    result = base_document("apply")
    result["artifact_schema"] = "170hx-xray-apply-dry-run-v1"
    result["access"] = {
        "requested_access": args.access,
        "canonical_requested_access": canonical_access,
        "effective_access": "offline-dry-run",
        "direct_user_mmio_writes": 0,
        "controller_read_commands": 0,
        "otp_program_commands": 0,
    }
    result["manifest"] = {
        "path": source,
        "sha256": source_sha256,
        "integrity": integrity,
        "integrity_authenticity": (
            "self-consistency only; an unkeyed canonical hash is not a signature or reviewer authorization"
        ),
        "declared_executable": bool(manifest.get("executable")),
        "operation_count": len(operations),
    }
    result["validation"] = {
        "status": "INTEGRITY_ONLY_BLOCKED",
        "dry_run": True,
        "writes_performed": 0,
        "execution_supported": False,
        "structural_checks": structural_checks,
        "reason": (
            "canonical hash and minimal plan structure are self-consistent; operation "
            "semantics, authenticity, complete images and execution remain unvalidated"
        ),
    }
    emit_json(result, args.output)
    return 0


def _full_write_target_tokens(specimens):
    tokens = []
    for index, specimen in enumerate(specimens):
        skeleton = specimen.get("manifest_skeleton", {})
        identity = skeleton.get("identity", {})
        if not isinstance(identity, dict):
            identity = {}
        token = None
        for key in ("domain_bdf", "pci_bdf", "bdf", "pci_address"):
            candidate = identity.get(key)
            if isinstance(candidate, str) and candidate:
                token = candidate
                break
        if token is None:
            token = "specimen-%d-IDENTITY-MISSING" % index
        tokens.append(token)
    return tokens


def _confirmation_read(prompt):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    value = sys.stdin.readline()
    if value == "":
        return None
    return value.rstrip("\r\n")


def _run_full_write_confirmations(source_sha256, target_tokens, operation_count):
    """Confirmation-only UX; this function has and invokes no write backend."""
    result = {
        "requested": True,
        "interactive_tty_required": True,
        "completed": False,
        "status": "not_started",
        "prompts_completed": 0,
    }
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        result["status"] = "refused_non_interactive"
        result["reason"] = "both stdin and stderr must be interactive TTYs"
        return result
    sys.stderr.write(
        "170HX-XRAY FULL-WRITE CONFIRMATION-ONLY PATH\n"
        "The legacy plan-v1 path has no OTP backend and stops with EOPNOTSUPP.\n"
        "No confirmation authorizes a write or removes any blocker.\n"
    )
    expected = [
        ("Do you really want to proceed to the refusal checkpoint? Type yes: ", "yes"),
        ("Type the exact plan-file SHA-256: ", source_sha256),
        ("Type the exact ordered target list: ", ",".join(target_tokens)),
        (
            "Type the irreversible-risk phrase: ",
            "I UNDERSTAND OTP IS IRREVERSIBLE; %d OPERATIONS; XRAY WILL NOT WRITE"
            % operation_count,
        ),
    ]
    for prompt, wanted in expected:
        got = _confirmation_read(prompt)
        if got != wanted:
            result["status"] = "rejected"
            result["reason"] = "confirmation mismatch at prompt %d" % (
                result["prompts_completed"] + 1
            )
            return result
        result["prompts_completed"] += 1
    result["completed"] = True
    result["status"] = "acknowledged_but_backend_unavailable"
    return result


def command_full_write(args):
    try:
        dispatch_root = _strict_json_loads(
            Path(args.manifest).read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuditError("cannot inspect full-write manifest: %s" % exc) from exc
    dispatch_schema = (
        dispatch_root.get("schema") if isinstance(dispatch_root, dict) else None
    )
    if dispatch_schema == "170hx-xray-otp-sealed-manifest-v2":
        raise AuditError(
            "sealed-manifest-v2 is retired after incompatible armed-image and "
            "backend-version hardening; recompile the unchanged candidate with "
            "the v3 compiler and obtain a new review receipt"
        )
    if dispatch_schema == "170hx-xray-otp-sealed-manifest-v3":
        return _command_vfi_append_full_write(args)
    canonical_access = canonical_access_mode(args.access)
    if canonical_access != "otp-write":
        raise AuditError("full-write requires --access otp-write")
    validated = _load_validated_write_plan(args.manifest)
    manifest = validated["manifest"]
    specimens = validated["specimens"]
    operations = validated["operations"]
    structural_checks = validated["structural_checks"]
    source_sha256 = validated["source_sha256"]
    target_tokens = _full_write_target_tokens(specimens)

    blockers = [
        "observation helper deliberately exposes no FUSEWDATA, CMD_WRITE, generic writel, GPIO, timing, reset, or OTP ioctl",
        "no electrically qualified CMP 170HX programming-source/GPIO19 path or external fail-safe is attested",
        "GA100 pulse timing, fuse/JTAG clock ownership, and program-voltage behavior are not board-qualified",
        "public VFI requires software override while the recovered Ampere burn setup disables it; no certified hybrid sequence exists",
        "no authoritative GA100 descriptor and no authenticated reviewer authorization/signature are present",
        "no transaction kernel executor, interruption-prefix proof, source-low watchdog, resense/reset, or cold-recovery backend exists",
        "secure rework requires authorized signed firmware when privilege security is active",
    ]
    if not operations:
        blockers.append("plan contains zero physical operations")
    if manifest.get("executable") is not True:
        blockers.append("plan explicitly declares executable=false")
    if any(not check["identity_present"] for check in structural_checks):
        blockers.append("one or more specimens lack a bound target identity")
    if any(not check["source_artifact_bound"] for check in structural_checks):
        blockers.append("one or more specimen skeletons are not bound to the plan source artifact")
    if any(not check["old_complete_image_present"] for check in structural_checks):
        blockers.append("one or more specimens lack a complete old physical image")
    if any(not check["proposed_complete_image_present"] for check in structural_checks):
        blockers.append("one or more specimens lack a complete proposed physical image")
    proof_fields = (
        "source_row_hashes_present", "official_descriptor_declared",
        "monotonicity_proof_present", "prefix_replays_present",
        "rir_replay_proof_present", "iff_final_frontier_proof_present",
        "recovery_reserve_proof_present",
    )
    if any(
        not check[field]
        for check in structural_checks
        for field in proof_fields
    ):
        blockers.append(
            "one or more specimens lack required descriptor, monotonicity, prefix, RIR, IFF-frontier, recovery-reserve, or source-hash proofs"
        )

    confirmation = {
        "requested": False,
        "interactive_tty_required": True,
        "completed": False,
        "status": "not_requested",
    }
    if args.execute:
        confirmation = _run_full_write_confirmations(
            source_sha256, target_tokens, len(operations)
        )

    result = base_document("full-write")
    result["artifact_schema"] = "170hx-xray-full-write-readiness-v1"
    result["access"] = {
        "requested_access": args.access,
        "canonical_requested_access": canonical_access,
        "effective_access": "execution-backend-unavailable",
        "direct_user_mmio_writes": 0,
        "controller_read_commands": 0,
        "otp_program_commands": 0,
    }
    result["manifest"] = {
        "path": validated["source"],
        "sha256": source_sha256,
        "integrity": validated["integrity"],
        "integrity_authenticity": (
            "self-consistency only; the unkeyed hash is not reviewer authorization"
        ),
        "target_confirmation_tokens": target_tokens,
        "operation_count": len(operations),
        "structural_checks": structural_checks,
    }
    result["readiness"] = {
        "status": "BLOCKED",
        "ready": False,
        "blockers": blockers,
        "confirmation": confirmation,
        "required_lifecycle": {
            "offline_seal": [
                "bind full PCI/board/UUID/VBIOS identity and tool/helper hashes",
                "seal two identical certified locked-controller 512-row passes and two identical RIR passes",
                "clone complete old image; prove every requested delta is 0->1 and allowlisted",
                "replay FF/IFF/RIR and topology after every interruption prefix",
                "obtain independent authenticated reviewer authorization over the final manifest",
            ],
            "maintenance_preflight": [
                "stop clients, unbind driver, take exclusive ownership, and recapture exact baselines",
                "prove locked controller view with override/fusing inactive and stable IFF_RECORD",
                "verify controller idle, CRC context, EFP/DFP/bypass/DSOV/SBFE and all relevant PLMs",
                "verify qualified timing, fuse/JTAG clock, GPIO19 mux/polarity, and measured program source low",
                "resolve the public-VFI versus recovered-Ampere override contradiction",
                "revalidate every gate after every volatile setup mutation",
            ],
            "commit_executor_requirements": [
                "manifest-only kernel ABI with no arbitrary MMIO address/value interface",
                "fresh per-row old-value read, global and per-row 1->0 rejection, and delta-only programming",
                "durable journal before each irreversible command, bounded polling, and no retry",
                "kernel-owned source-low cleanup plus an independent hardware fail-safe",
                "lower and physically verify program source before complete-image verification",
            ],
            "post_commit": [
                "two exact complete controller-MAIN and fresh RIR verification passes before any reset",
                "quarantine on any unexpected bit or uncertain command; never auto-resume",
                "separately authorized resense/reset or controlled cold power cycle",
                "re-enumerate, recapture all raw/security/IFF state, then validate requested features",
            ],
        },
    }
    result["execution"] = {
        "requested": bool(args.execute),
        "backend_supported": False,
        "backend_invoked": False,
        "result": "EOPNOTSUPP",
        "writes_performed": 0,
        "fusewdata_writes": 0,
        "cmd_write_commands": 0,
        "program_source_assertions": 0,
        "reason": (
            "this release implements readiness inspection and confirmation UX only; "
            "irreversible execution is intentionally absent"
        ),
    }
    emit_json(result, args.output)
    return 3 if args.execute else 0


def _load_vfi_append_backend():
    path = Path(__file__).resolve().with_name("otp_vfi_append.py")
    if not path.is_file() or path.is_symlink():
        raise AuditError("VFI append backend is absent or unsafe: %s" % path)
    spec = importlib.util.spec_from_file_location("xray_vfi_append_runtime", str(path))
    if spec is None or spec.loader is None:
        raise AuditError("cannot load VFI append backend")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _command_vfi_append_full_write(args):
    backend = _load_vfi_append_backend()
    if args.backend not in ("auto", backend.BACKEND_ID):
        raise AuditError("v3 manifest requires --backend %s" % backend.BACKEND_ID)
    if args.execute and args.live_preflight:
        raise AuditError("--execute and --live-preflight are mutually exclusive")
    try:
        validated = backend.load_and_validate_manifest(
            args.manifest, expected_bdf=args.bdf,
            expected_profile=args.profile,
        )
    except backend.VfiAppendError as exc:
        raise AuditError("VFI append manifest refused: %s" % exc) from exc
    if args.execute and args.write_strategy is None:
        raise AuditError(
            "v3 execution requires explicit --write-strategy; there is no default"
        )
    if args.write_strategy is not None:
        try:
            backend.validate_write_strategy(args.write_strategy)
        except backend.VfiAppendError as exc:
            raise AuditError("VFI append write strategy refused: %s" % exc) from exc

    base = base_document("full-write")
    base["safety"] = {
        "access_ladder": list(ACCESS_LEVELS),
        "access_aliases": dict(ACCESS_ALIASES),
        "generic_live_write_primitive": False,
        "sealed_vfi_append_execution_supported": True,
        "otp_execution_supported": "experimental-v3-blank-FF-append-only",
        "direct_user_mmio_writes": (
            "zero during offline validation/preflight; exact sealed setup and "
            "per-row programming writes only after --execute authorization"
        ),
        "volatile_write_execution_supported": False,
        "controller_read_commands": (
            "two complete preflight passes plus bounded per-row verification"
        ),
        "forbidden_programming_actions": [
            "arbitrary address/value MMIO", "unsealed row or payload",
            "automatic OTP retry/resume", "software-IFF load", "GPU reset",
        ],
        "mode_gated_v3_actions": [
            "read-only fuse timing snapshot", "sealed FUSEWDATA",
            "sealed FUSECTRL.CMD_WRITE", "bounded sense/readback",
        ],
    }
    base["artifact_schema"] = "170hx-xray-vfi-append-result-v1"
    base["backend"] = {
        "id": backend.BACKEND_ID,
        "version": backend.BACKEND_VERSION,
        "module_sha256": sha256_file(str(Path(backend.__file__).resolve())),
        "scope": "verified-blank GA100 FF-row append only",
        "generic_mmio_write_interface": False,
        "gpio19_used": False,
        "write_strategies": list(backend.WRITE_STRATEGIES),
        "transaction_trace_strategies": list(
            backend.TRANSACTION_TRACE_STRATEGIES
        ),
        "write_strategy_selected": args.write_strategy,
        "implicit_write_strategy": False,
    }
    base["manifest"] = {
        "path": validated["path"],
        "file_sha256": validated["file_sha256"],
        "content_sha256": validated["content_sha256"],
        "bdf": validated["bdf"],
        "profile_id": validated["profile_id"],
        "operation_count": len(validated["operations"]),
        "compare_range": validated["compare_range"],
        "residual_research_risks": validated["residual_research_risks"],
    }
    base["transaction_traces"] = {
        strategy: backend.transaction_trace(validated, strategy)
        for strategy in backend.TRANSACTION_TRACE_STRATEGIES
    }
    if not args.execute and not args.live_preflight:
        base["status"] = "OFFLINE_V3_VALIDATED"
        base["execution"] = {
            "requested": False,
            "experimental_backend_implemented": True,
            "hardware_backend_invoked": False,
            "otp_program_commands": 0,
            "next_gate": (
                "use --live-preflight on the exact unbound held card; execution "
                "also requires a fresh native DSOV=0 --held-state-capture, "
                "--receipt, --journal, --bdf, --profile and TTY confirmations"
            ),
        }
        emit_json(base, args.output)
        return 0

    if args.execute:
        for name, value in (
            ("--bdf", args.bdf), ("--profile", args.profile),
            ("--receipt", args.receipt), ("--journal", args.journal),
            ("--held-state-capture", args.held_state_capture),
        ):
            if not value:
                raise AuditError("v3 execution requires %s" % name)
        try:
            receipt = backend.load_and_validate_receipt(args.receipt, validated)
            authorization = backend.perform_runtime_confirmations(
                validated, receipt, args.write_strategy,
                input_func=lambda prompt: _confirmation_read(prompt),
                stdin_isatty=sys.stdin.isatty(),
                stderr_isatty=sys.stderr.isatty(),
            )
        except backend.VfiAppendError as exc:
            raise AuditError("VFI append authorization refused: %s" % exc) from exc

        continue_after_test = backend.make_continuation_authorizer(
            authorization, input_func=lambda prompt: _confirmation_read(prompt)
        )
    else:
        authorization = None
        continue_after_test = None

    try:
        live = backend.run_live(
            validated,
            sysfs_root=args.sysfs_root,
            execute=bool(args.execute),
            journal_path=args.journal,
            test_row_only=bool(args.test_row_only),
            continue_after_test=continue_after_test,
            receipt_path=args.receipt,
            authorization=authorization,
            held_state_capture_path=args.held_state_capture,
            write_strategy=args.write_strategy,
        )
        base["status"] = live["status"]
        base["execution"] = live
        emit_json(base, args.output)
        return 0
    except backend.VfiAppendError as exc:
        terminal = None
        if args.journal and Path(args.journal).is_file():
            try:
                terminal = backend.verify_journal(
                    args.journal, validated["content_sha256"])
            except Exception as journal_exc:
                terminal = {"valid": False, "reason": str(journal_exc)}
        base["status"] = "LIVE_REFUSED_OR_QUARANTINED"
        base["execution"] = {
            "requested": bool(args.execute),
            "reason": str(exc),
            "journal": terminal,
            "automatic_retry_forbidden": True,
        }
        emit_json(base, args.output)
        return 3


def _full_dump_make_directory(path):
    """Create a private bundle directory without accepting a pre-existing leaf."""
    if not path.exists() and not path.is_symlink():
        parent = path.parent
        if not parent.exists() and not parent.is_symlink():
            _full_dump_make_directory(parent)
        if parent.is_symlink() or not parent.is_dir():
            raise AuditError("bundle parent is not a real directory: %s" % parent)
        try:
            os.mkdir(str(path), 0o700)
        except FileExistsError:
            pass
    if path.is_symlink() or not path.is_dir():
        raise AuditError("bundle path is not a real directory: %s" % path)
    os.chmod(str(path), 0o700)


def _full_dump_relative(root, path):
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AuditError("artifact escaped full-dump root: %s" % path) from exc
    value = relative.as_posix()
    if not value or value.startswith("/") or ".." in relative.parts:
        raise AuditError("invalid full-dump artifact path: %s" % value)
    return value


def _full_dump_record_artifact(index, root, path, kind, metadata=None):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise AuditError("full-dump artifact is not a regular file: %s" % path)
    os.chmod(str(path), 0o600)
    record = {
        "path": _full_dump_relative(root, path),
        "kind": kind,
        "size": path.stat().st_size,
        "sha256": sha256_file(str(path)),
        "mode": "0600",
    }
    if metadata:
        record["metadata"] = metadata
    if any(item["path"] == record["path"] for item in index["artifacts"]):
        raise AuditError("duplicate full-dump artifact path: %s" % record["path"])
    index["artifacts"].append(record)
    return record


def _full_dump_note_unavailable(index, state, category, reason, required=False,
                                **details):
    record = {
        "category": category,
        "reason": reason,
        "required_for_requested_bundle": bool(required),
    }
    for key, value in sorted(details.items()):
        if value is not None:
            record[key] = value
    index["unavailable"].append(record)
    if required:
        state["requested_failures"] += 1
    return record


def _full_dump_selectors(path, input_format):
    """Return deterministic selectors for recognized multi-card JSON envelopes."""
    if input_format in ("raw-u32le", "indexed-text", "jsonl"):
        return [None]
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        return [None]
    stripped = text.lstrip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return [None]
    try:
        parsed = _strict_json_loads(
            text, object_pairs_hook=_json_object_without_duplicates
        )
    except (json.JSONDecodeError, AuditError):
        return [None]
    collection = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("cards"), list):
            collection = parsed["cards"]
        elif isinstance(parsed.get("gpus"), list):
            collection = parsed["gpus"]
    elif isinstance(parsed, list) and parsed and all(
        isinstance(item, dict) for item in parsed
    ):
        card_keys = (
            "main_rows", "ff_rows", "physical_rows", "rir_rows",
            "rir_passes", "raw_rows", "pass1", "pass2", "fuse_rows",
        )
        if all(any(key in item for key in card_keys) for item in parsed):
            collection = parsed
    if collection is None:
        return [None]
    return [str(index) for index in range(len(collection))]


def _full_dump_capture_assessment(document, requested_access):
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list) or not cards:
        return {
            "status": "unavailable",
            "complete": False,
            "available_cards": 0,
            "total_cards": 0,
            "reason": "capture contains no card observations",
        }
    available = 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        if requested_access == "passive":
            card_available = isinstance(card.get("card"), dict)
        elif requested_access == "bar-read":
            capture = card.get("capture", {})
            coherence = capture.get("coherence", {}) if isinstance(capture, dict) else {}
            card_available = bool(
                isinstance(capture, dict)
                and capture.get("read_count", 0) > 0
                and isinstance(coherence, dict)
                and coherence.get("coherent") is True
            )
        elif requested_access == "glitch-read":
            capture = card.get("capture", {})
            access = card.get("access", {})
            qualification = card.get("state_qualification", {})
            card_available = bool(
                isinstance(capture, dict)
                and capture.get("read_count", 0) > 0
                and capture.get("coherence", {}).get("coherent") is True
                and isinstance(access, dict)
                and access.get("effective_access") == "glitch-read"
                and access.get("direct_user_mmio_writes") == 0
                and access.get("controller_read_commands_issued") == 0
                and isinstance(qualification, dict)
                and qualification.get("qualified") is True
            )
        else:
            physical = card.get("physical_rows", {})
            rir = card.get("rir_rows", {})
            card_available = bool(
                isinstance(physical, dict)
                and physical.get("available") is True
                and physical.get("coherent") is True
                and isinstance(rir, dict)
                and rir.get("available") is True
                and rir.get("coherent") is True
            )
        available += int(card_available)
    complete = available == len(cards)
    return {
        "status": "complete" if complete else (
            "partial" if available else "unavailable"
        ),
        "complete": complete,
        "available_cards": available,
        "total_cards": len(cards),
        "reason": None if complete else (
            "one or more cards did not produce evidence certified for the requested level"
        ),
    }


def _full_dump_capture_operational_effects(document):
    """Copy auditable live-operation counters out of one capture artifact."""
    access = document.get("access") if isinstance(document, dict) else None
    access = access if isinstance(access, dict) else {}
    effects = {
        "source": "capture.access",
        "counts_complete": access.get("counts_complete") is True,
        "missing_or_invalid_counters": [],
    }
    for key in FULL_DUMP_EFFECT_COUNTERS:
        value = access.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            effects[key] = None
            effects["counts_complete"] = False
            effects["missing_or_invalid_counters"].append(key)
        else:
            effects[key] = value
    effects["controller_read_commands"] = effects[
        "controller_read_commands_issued"
    ]
    effects["semantics"] = (
        "operational writes are the MMIO/config/power-state writes used to "
        "perform observation; they are not OTP programming writes"
    )
    return effects


def _full_dump_observation_transports(index, root, directory, document,
                                      observation_id, requested_access):
    """Emit level-local JSONL/text/binary companions to native capture JSON."""
    cards = document.get("cards", []) if isinstance(document, dict) else []
    jsonl_path = directory / "capture.jsonl"
    emit_text("".join(
        _strict_json_dumps(
            {
                "schema": "170hx-xray-capture-card-jsonl-v1",
                "observation": observation_id,
                "requested_access": requested_access,
                "card_index": ordinal,
                "card": card,
            },
            sort_keys=True, separators=(",", ":"),
        ) + "\n"
        for ordinal, card in enumerate(cards)
    ), str(jsonl_path))
    jsonl = _full_dump_record_artifact(
        index, root, jsonl_path, "live-capture-jsonl",
        {"observation": observation_id, "requested_access": requested_access},
    )
    text_path = directory / "capture.txt"
    emit_text("".join(
        "card=%d bdf=%s device=%s status=%s access=%s\n" % (
            ordinal,
            card.get("card", {}).get("domain_bdf"),
            card.get("card", {}).get("device"),
            document.get("status"),
            card.get("access", {}).get("effective_access"),
        )
        for ordinal, card in enumerate(cards)
    ), str(text_path))
    text_record = _full_dump_record_artifact(
        index, root, text_path, "live-capture-text",
        {"observation": observation_id, "requested_access": requested_access},
    )
    binary_records = []
    for ordinal, card in enumerate(cards):
        for name, value in sorted(_conversion_register_values(card).items()):
            definition = REGISTER_BY_NAME.get(name)
            if definition is not None:
                binary_records.append((ordinal, definition["address"], value))
    binary_path = directory / "capture-registers-card-address-value-u32le.bin"
    emit_bytes(b"".join(
        struct.pack("<III", ordinal, address, value)
        for ordinal, address, value in binary_records
    ), str(binary_path))
    binary = _full_dump_record_artifact(
        index, root, binary_path, "live-capture-register-u32le",
        {
            "observation": observation_id,
            "requested_access": requested_access,
            "record_count": len(binary_records),
            "record_layout": "repeated <u32 card_index,u32 address,u32 value>",
        },
    )
    return {
        "json": None,
        "jsonl": jsonl["path"],
        "text": text_record["path"],
        "binary_register_records": binary["path"],
        "binary_record_layout": (
            "repeated <u32 card_index,u32 address,u32 value>"
        ),
    }


def _full_dump_operational_totals(observations):
    """Aggregate only work performed live by this full-dump invocation."""
    totals = {key: 0 for key in FULL_DUMP_EFFECT_COUNTERS}
    counts_complete = True
    live_observations = 0
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("origin") != "live":
            continue
        live_observations += 1
        effects = observation.get("operational_effects")
        if not isinstance(effects, dict):
            counts_complete = False
            continue
        if effects.get("counts_complete") is not True:
            counts_complete = False
        for key in FULL_DUMP_EFFECT_COUNTERS:
            value = effects.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                counts_complete = False
                continue
            totals[key] += value
    operational_writes = (
        totals["direct_user_mmio_writes"]
        + totals["pci_config_writes"]
        + totals["runtime_pm_writes"]
    )
    result = dict(totals)
    result.update({
        "aggregation_scope": (
            "live observations acquired by this full-dump invocation; imported "
            "artifacts are excluded"
        ),
        "live_observations": live_observations,
        "counts_complete": counts_complete,
        "controller_read_commands": totals[
            "controller_read_commands_issued"
        ],
        "operational_writes_performed": operational_writes,
        "programming_writes_performed": totals["otp_program_commands"],
        "writes_performed": operational_writes,
        "writes_performed_semantics": (
            "all counted live operational MMIO/config/power-state writes; OTP "
            "programming is reported separately and is unsupported here"
        ),
    })
    return result


def _full_dump_live_namespace(args, access, output):
    return argparse.Namespace(
        command="capture",
        access=access,
        owner_busy=args.owner_busy,
        controller_helper=args.controller_helper,
        allow_bound_controller=args.allow_bound_controller,
        bdf=list(args.bdf),
        all_targets=args.all_targets,
        device=list(args.device),
        backend=args.backend,
        passes=args.passes,
        register_profile=args.register_profile,
        fuseless_start=args.fuseless_start,
        fuseless_end=args.fuseless_end,
        bounds_provenance=args.bounds_provenance,
        sysfs_root=args.sysfs_root,
        gpcprobe=args.gpcprobe,
        nvml=args.nvml,
        nvml_library=args.nvml_library,
        smi_features=args.smi_features,
        no_driver_queries=args.no_driver_queries,
        nvidia_smi=args.nvidia_smi,
        query_timeout=args.query_timeout,
        invoked_source_sha256=args.invoked_source_sha256,
        output=str(output),
    )


def _full_dump_bindings(args, index, state, root):
    candidate_paths = list(args.candidate_planner)
    if args.bind_workspace_candidate:
        candidate = Path(__file__).resolve().parent.parent / "tools" / "170hx_candidate_plan.py"
        if candidate.exists():
            candidate_paths.append(str(candidate))
        else:
            _full_dump_note_unavailable(
                index, state, "candidate-planner-binding",
                "workspace candidate planner was requested but not found",
                required=True,
            )
    seen = set()
    bindings = []
    for raw_path in candidate_paths:
        path = Path(raw_path)
        canonical = str(path.resolve())
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise AuditError("binding target is not a regular file")
            bindings.append({
                "path": canonical,
                "sha256": sha256_file(str(path)),
                "size": info.st_size,
                "executed": False,
                "authority": "hash/path binding only; never imported as write authority",
            })
        except (AuditError, OSError) as exc:
            _full_dump_note_unavailable(
                index, state, "candidate-planner-binding", str(exc),
                required=True, path=raw_path,
            )

    valid_references = []
    reference_bindings = []
    for raw_path in args.reference_plan:
        try:
            summary = summarize_reference_plan(raw_path)
            reference_bindings.append(summary)
            valid_references.append(raw_path)
        except (AuditError, OSError, UnicodeError) as exc:
            _full_dump_note_unavailable(
                index, state, "reference-plan-binding", str(exc),
                required=True, path=raw_path,
            )
    index["bindings"] = {
        "candidate_planners": bindings,
        "reference_plans": reference_bindings,
        "candidate_execution_performed": False,
    }
    if bindings or reference_bindings or args.bind_workspace_candidate:
        directory = root / "bindings"
        _full_dump_make_directory(directory)
        path = directory / "index.json"
        emit_json(index["bindings"], str(path))
        _full_dump_record_artifact(index, root, path, "binding-index")
    return valid_references


def _full_dump_specimen_root(root, specimen):
    level_directory = specimen.get("level_directory")
    if not isinstance(level_directory, str) or not level_directory:
        raise AuditError("specimen lacks a level-local directory")
    if level_directory.startswith("/") or ".." in Path(level_directory).parts:
        raise AuditError("unsafe level-local specimen directory")
    return root / "levels" / level_directory / "specimens" / specimen["id"]


def _full_dump_emit_rir(index, state, root, specimen, source_path,
                        card_selector, input_format, required,
                        main_loaded=None):
    try:
        loaded = load_rir_words(source_path, card_selector, input_format)
    except (AuditError, OSError, UnicodeError) as exc:
        specimen["rir"] = {"available": False, "error": str(exc)}
        _full_dump_note_unavailable(
            index, state, "rir-export", str(exc), required=required,
            specimen=specimen["id"],
        )
        return None

    rir_root = _full_dump_specimen_root(root, specimen) / "exports" / "rir"
    _full_dump_make_directory(rir_root)
    aggregate = base_document("full-dump-rir-export")
    aggregate.update({
        "artifact_schema": "170hx-xray-portable-rir-v1",
        "source": {
            "path": loaded["source"],
            "sha256": loaded["sha256"],
            "detected_format": loaded.get("detected_format"),
            "format_claims": loaded.get("format_claims", {}),
        },
        "selected_card": loaded.get("selected_card"),
        "passes": [
            [hex32(value) for value in row_pass]
            for row_pass in loaded["passes"]
        ],
        "pass_hashes": loaded["pass_hashes"],
        "coherent_repeated_passes": loaded["coherent_repeated_passes"],
        "mismatches": loaded["mismatches"],
        "authority": "row-preserving RIR transport only; no write authority",
    })
    pairing = (
        _conversion_rir_pairing(main_loaded, loaded)
        if isinstance(main_loaded, dict) else {
            "available": True,
            "certified_same_specimen": False,
            "errors": ["MAIN source was unavailable for pairing"],
        }
    )
    aggregate["main_rir_pairing"] = pairing
    aggregate_path = rir_root / "all-passes.json"
    emit_json(aggregate, str(aggregate_path))
    aggregate_record = _full_dump_record_artifact(
        index, root, aggregate_path, "rir-all-passes-json",
        {"specimen": specimen["id"]},
    )
    pass_entries = []
    for pass_index, words in enumerate(loaded["passes"], 1):
        pass_root = rir_root / ("pass-%03d" % pass_index)
        _full_dump_make_directory(pass_root)
        outputs = []
        raw_path = pass_root / "raw-u32le.bin"
        emit_bytes(struct.pack("<8I", *words), str(raw_path))
        outputs.append(_full_dump_record_artifact(
            index, root, raw_path, "rir-raw-u32le",
            {"specimen": specimen["id"], "pass": pass_index},
        ))
        indexed_path = pass_root / "indexed.txt"
        emit_text("".join(
            "[%03d] %s\n" % (row, hex32(value))
            for row, value in enumerate(words)
        ), str(indexed_path))
        outputs.append(_full_dump_record_artifact(
            index, root, indexed_path, "rir-indexed-text",
            {"specimen": specimen["id"], "pass": pass_index},
        ))
        json_path = pass_root / "rows.json"
        emit_json({
            "rir": [hex32(value) for value in words],
            "pass": pass_index,
            "source_sha256": loaded["sha256"],
        }, str(json_path))
        outputs.append(_full_dump_record_artifact(
            index, root, json_path, "rir-json",
            {"specimen": specimen["id"], "pass": pass_index},
        ))
        jsonl_path = pass_root / "rows.jsonl"
        emit_text("".join(
            _strict_json_dumps(
                {"row": row, "value": hex32(value)},
                sort_keys=True, separators=(",", ":"),
            ) + "\n"
            for row, value in enumerate(words)
        ), str(jsonl_path))
        outputs.append(_full_dump_record_artifact(
            index, root, jsonl_path, "rir-jsonl",
            {"specimen": specimen["id"], "pass": pass_index},
        ))
        pass_entries.append({
            "pass": pass_index,
            "sha256_u32le": loaded["pass_hashes"][pass_index - 1],
            "outputs": [item["path"] for item in outputs],
        })
    specimen["rir"] = {
        "available": True,
        "pass_count": len(loaded["passes"]),
        "coherent_repeated_passes": loaded["coherent_repeated_passes"],
        "certified_same_specimen": pairing.get("certified_same_specimen") is True,
        "main_rir_pairing": pairing,
        "aggregate": aggregate_record["path"],
        "passes": pass_entries,
    }
    return loaded


def _full_dump_export_main(index, state, root, specimen, source_path,
                           card_selector, input_format, rir_path,
                           rir_input_format, args, required):
    try:
        loaded = load_row_passes(source_path, card_selector, input_format)
    except (AuditError, OSError, UnicodeError) as exc:
        specimen["main"] = {"available": False, "error": str(exc)}
        _full_dump_note_unavailable(
            index, state, "main-export", str(exc), required=required,
            specimen=specimen["id"],
        )
        return None

    identity = _conversion_identity(loaded)
    specimen["identity"] = identity
    specimen["main"] = {
        "available": True,
        "detected_format": loaded.get("detected_format"),
        "pass_count": len(loaded["passes"]),
        "passes": [],
    }
    for pass_index, rows in enumerate(loaded["passes"], 1):
        pass_root = (
            _full_dump_specimen_root(root, specimen) / "exports" / "main"
            / ("pass-%03d" % pass_index)
        )
        _full_dump_make_directory(pass_root)
        pass_entry = {
            "pass": pass_index,
            "known_rows": len(rows),
            "complete": len(rows) == MAX_FUSE_ROWS,
            "outputs": [],
        }
        if len(rows) == MAX_FUSE_ROWS:
            pass_entry["sha256_u32le"] = _packed_u32_pass_sha256(
                [rows[index_value] for index_value in range(MAX_FUSE_ROWS)]
            )
        for output_format in DUMP_OUTPUT_FORMATS:
            target = pass_root / FULL_DUMP_FORMAT_FILENAMES[output_format]
            entry = {"format": output_format, "status": None, "path": None}
            unsafe = output_format in _UNSAFE_PROJECTION_OUTPUTS
            if unsafe and not args.include_unsafe_projections:
                entry["status"] = "not_requested_policy_gate"
                _full_dump_note_unavailable(
                    index, state, "main-format-export",
                    "unsafe/actionable projection requires explicit opt-in",
                    required=False, specimen=specimen["id"],
                    pass_index=pass_index, output_format=output_format,
                )
                pass_entry["outputs"].append(entry)
                continue
            if output_format in ("ffcalc-json", "xpatrik-json") and (
                args.fuseless_start is None or args.fuseless_end is None
            ):
                entry["status"] = "unavailable_explicit_bounds_required"
                _full_dump_note_unavailable(
                    index, state, "main-format-export",
                    "format requires explicit --fuseless-start/--fuseless-end",
                    required=unsafe, specimen=specimen["id"],
                    pass_index=pass_index, output_format=output_format,
                )
                pass_entry["outputs"].append(entry)
                continue
            conversion_args = argparse.Namespace(
                input=source_path,
                input_format=input_format,
                output_format=output_format,
                card=card_selector,
                pass_index=pass_index,
                rir_input=rir_path,
                rir_input_format=rir_input_format,
                fuseless_start=args.fuseless_start,
                fuseless_end=args.fuseless_end,
                sku=args.sku,
                bdf=None,
                allow_unsafe_projection=args.include_unsafe_projections,
                row_view=args.row_view,
                provenance=args.provenance,
                output=str(target),
            )
            try:
                command_convert_dump(conversion_args)
                artifact = _full_dump_record_artifact(
                    index, root, target, "main-%s" % output_format,
                    {"specimen": specimen["id"], "pass": pass_index},
                )
                entry.update({"status": "exported", "path": artifact["path"]})
            except (AuditError, OSError, UnicodeError) as exc:
                entry.update({"status": "unavailable", "error": str(exc)})
                _full_dump_note_unavailable(
                    index, state, "main-format-export", str(exc),
                    required=required or unsafe,
                    specimen=specimen["id"], pass_index=pass_index,
                    output_format=output_format,
                )
            pass_entry["outputs"].append(entry)
        specimen["main"]["passes"].append(pass_entry)
    return loaded


def _full_dump_plan_specimen(index, state, root, specimen, source_path,
                             card_selector, args, reference_plans, required):
    specimen["planning"] = []
    planning_root = _full_dump_specimen_root(root, specimen) / "planning"
    _full_dump_make_directory(planning_root)
    for model in PLANNING_MODELS:
        model_root = planning_root / model
        _full_dump_make_directory(model_root)
        plan_path = model_root / "plan.json"
        plan_entry = {"model": model, "status": "unavailable"}
        try:
            command_plan(argparse.Namespace(
                input=source_path,
                access="plan",
                card=card_selector,
                goal=list(args.goal),
                planning_model=model,
                reference_plan=list(reference_plans),
                output=str(plan_path),
            ))
            plan_artifact = _full_dump_record_artifact(
                index, root, plan_path, "plan-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            plan_document, _, _ = load_json_artifact(
                str(plan_path), normalize_legacy=True
            )
            jsonl_path = model_root / "plan.jsonl"
            emit_text(
                _strict_json_dumps(
                    plan_document, sort_keys=True, separators=(",", ":")
                ) + "\n",
                str(jsonl_path),
            )
            jsonl_artifact = _full_dump_record_artifact(
                index, root, jsonl_path, "plan-jsonl-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            text_path = model_root / "plan.txt"
            emit_text(
                "model=%s\nexecutable=%s\noperations_total=%s\nsource_sha256=%s\n"
                % (
                    model,
                    plan_document.get("executable"),
                    plan_document.get("operations_total"),
                    plan_document.get("source_artifact", {}).get("sha256"),
                ),
                str(text_path),
            )
            text_artifact = _full_dump_record_artifact(
                index, root, text_path, "plan-text-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            canonical_plan = _strict_json_dumps(
                plan_document, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            binary_path = model_root / "plan-canonical-json.bin"
            emit_bytes(canonical_plan, str(binary_path))
            binary_artifact = _full_dump_record_artifact(
                index, root, binary_path, "plan-canonical-json-bin-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            plan_entry.update({
                "status": "generated_non_executable",
                "plan": plan_artifact["path"],
                "transports": {
                    "json": plan_artifact["path"],
                    "jsonl": jsonl_artifact["path"],
                    "text": text_artifact["path"],
                    "binary_canonical_json": binary_artifact["path"],
                    "binary_semantics": (
                        "canonical UTF-8 JSON bytes; not executable opcodes"
                    ),
                },
            })
        except (AuditError, OSError, UnicodeError) as exc:
            plan_entry["error"] = str(exc)
            _full_dump_note_unavailable(
                index, state, "planning-model", str(exc), required=required,
                specimen=specimen["id"], planning_model=model,
            )
            specimen["planning"].append(plan_entry)
            continue

        apply_path = model_root / "apply-dry-run.json"
        try:
            command_apply(argparse.Namespace(
                manifest=str(plan_path), access="volatile-write",
                execute=False, output=str(apply_path),
            ))
            artifact = _full_dump_record_artifact(
                index, root, apply_path, "apply-dry-run-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            plan_entry["apply_dry_run"] = artifact["path"]
        except (AuditError, OSError, UnicodeError) as exc:
            plan_entry["apply_error"] = str(exc)
            _full_dump_note_unavailable(
                index, state, "apply-dry-run", str(exc), required=True,
                specimen=specimen["id"], planning_model=model,
            )

        readiness_path = model_root / "otp-readiness.json"
        try:
            command_full_write(argparse.Namespace(
                manifest=str(plan_path), access="otp-write",
                execute=False, output=str(readiness_path),
            ))
            artifact = _full_dump_record_artifact(
                index, root, readiness_path, "otp-readiness-%s" % model,
                {"specimen": specimen["id"], "model": model},
            )
            plan_entry["otp_readiness"] = artifact["path"]
        except (AuditError, OSError, UnicodeError) as exc:
            plan_entry["otp_readiness_error"] = str(exc)
            _full_dump_note_unavailable(
                index, state, "otp-readiness", str(exc), required=True,
                specimen=specimen["id"], planning_model=model,
            )
        specimen["planning"].append(plan_entry)


def _full_dump_export_registers(index, root, specimen, selected_capture):
    """Emit every compatible register transport in the specimen's level.

    The binary transport is an intentionally simple repeated little-endian
    ``address,u32`` pair stream.  Its JSON manifest binds the count and digest;
    it is not a BAR replay script and carries no write authority.
    """
    register_values = _conversion_register_values(selected_capture)
    if not register_values:
        specimen["register_exports"] = {
            "available": False,
            "reason": "source format carries no normalized register snapshot",
            "formats": [],
        }
        return
    census = (
        selected_capture.get("unknown_register_census")
        if isinstance(selected_capture, dict) else None
    )
    disposition_by_name = {}
    if isinstance(census, dict):
        for item in census.get("entries", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                disposition_by_name[item["name"]] = item.get("disposition")
    records = []
    for name, value in sorted(register_values.items(), key=lambda pair: (
        REGISTER_BY_NAME.get(pair[0], {}).get("address", 0xFFFFFFFF), pair[0]
    )):
        definition = REGISTER_BY_NAME.get(name)
        if definition is None:
            address = None
        else:
            address = definition["address"]
        records.append({
            "name": name,
            "address": hex32(address) if address is not None else None,
            "value": hex32(value),
            "disposition": disposition_by_name.get(
                name, "source_value_without_catalog_disposition"
            ),
        })
    export_root = _full_dump_specimen_root(root, specimen) / "exports" / "registers"
    _full_dump_make_directory(export_root)
    artifacts = []
    json_path = export_root / "registers.json"
    emit_json({
        "schema": "170hx-xray-register-export-v1",
        "records": records,
        "unknown_register_census": census,
        "authority": "read evidence only; never a write/replay script",
    }, str(json_path))
    artifacts.append(_full_dump_record_artifact(
        index, root, json_path, "register-json", {"specimen": specimen["id"]}
    ))
    jsonl_path = export_root / "registers.jsonl"
    emit_text("".join(
        _strict_json_dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
        for item in records
    ), str(jsonl_path))
    artifacts.append(_full_dump_record_artifact(
        index, root, jsonl_path, "register-jsonl", {"specimen": specimen["id"]}
    ))
    text_path = export_root / "registers.txt"
    emit_text("".join(
        "%s %s %s %s\n" % (
            item["address"] or "unmapped", item["value"], item["name"],
            item["disposition"],
        )
        for item in records
    ), str(text_path))
    artifacts.append(_full_dump_record_artifact(
        index, root, text_path, "register-text", {"specimen": specimen["id"]}
    ))
    binary_records = [
        (int(item["address"], 0), int(item["value"], 0))
        for item in records if item["address"] is not None
    ]
    binary = b"".join(struct.pack("<II", address, value)
                       for address, value in binary_records)
    bin_path = export_root / "registers-address-value-u32le.bin"
    emit_bytes(binary, str(bin_path))
    artifacts.append(_full_dump_record_artifact(
        index, root, bin_path, "register-address-value-u32le",
        {"specimen": specimen["id"], "record_count": len(binary_records)},
    ))
    specimen["register_exports"] = {
        "available": True,
        "record_count": len(records),
        "binary_record_layout": "repeated <u32 address,u32 value>",
        "formats": [item["path"] for item in artifacts],
    }


def _full_dump_process_observation(index, state, root, observation, args,
                                   reference_plans, rir_source):
    source_path = observation["absolute_path"]
    input_format = observation["input_format"]
    try:
        selectors = _full_dump_selectors(source_path, input_format)
    except (AuditError, OSError, UnicodeError) as exc:
        _full_dump_note_unavailable(
            index, state, "specimen-discovery", str(exc), required=True,
            observation=observation["id"],
        )
        return
    if not selectors:
        _full_dump_note_unavailable(
            index, state, "specimen-discovery",
            "observation contains no selectable cards", required=True,
            observation=observation["id"],
        )
        return

    for selector in selectors:
        specimen_id = "specimen-%03d" % (len(index["specimens"]) + 1)
        specimen = {
            "id": specimen_id,
            "observation": observation["id"],
            "level_directory": observation["level_directory"],
            "card_selector": selector,
            "identity": {},
        }
        index["specimens"].append(specimen)
        specimen_root = _full_dump_specimen_root(root, specimen)
        _full_dump_make_directory(specimen_root)
        main_required = observation["origin"] == "imported" or (
            observation.get("requested_access") == "semantic-read"
        )
        loaded = _full_dump_export_main(
            index, state, root, specimen, source_path, selector, input_format,
            rir_source["path"] if rir_source else source_path,
            rir_source["input_format"] if rir_source else input_format,
            args, main_required,
        )
        selected_capture = (
            loaded.get("selected_capture") if isinstance(loaded, dict) else None
        )
        _full_dump_export_registers(
            index, root, specimen, selected_capture
        )
        rir_claimed = bool(
            isinstance(selected_capture, dict)
            and any(
                key in selected_capture
                for key in ("rir", "rir_rows", "rir_passes")
            )
        )
        rir_required = bool(
            loaded is not None and main_required
            and (rir_source is not None or rir_claimed)
        )
        _full_dump_emit_rir(
            index, state, root, specimen,
            rir_source["path"] if rir_source else source_path,
            selector,
            rir_source["input_format"] if rir_source else input_format,
            rir_required,
            main_loaded=loaded,
        )
        _full_dump_plan_specimen(
            index, state, root, specimen, source_path, selector, args,
            reference_plans, True,
        )


def _full_dump_manifest(root):
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise AuditError("symlink found in full-dump bundle: %s" % path)
        if stat.S_ISREG(info.st_mode):
            relative = _full_dump_relative(root, path)
            if relative == "SHA256SUMS":
                continue
            files.append((relative, sha256_file(str(path))))
        elif stat.S_ISDIR(info.st_mode):
            os.chmod(str(path), 0o700)
        else:
            raise AuditError("non-regular object found in full-dump bundle: %s" % path)
    return "".join("%s  %s\n" % (digest, path) for path, digest in files)


def _full_dump_index_path(root, entries, value, label, errors):
    """Resolve one index path only when it is a manifested regular file."""
    if not isinstance(value, str):
        errors.append("%s is not a path string" % label)
        return None
    parts = Path(value).parts
    if (
        not parts or value.startswith(("/", "\\")) or "\\" in value
        or "." in parts or ".." in parts or Path(value).as_posix() != value
    ):
        errors.append("%s is an unsafe bundle path: %r" % (label, value))
        return None
    if value not in entries:
        errors.append("%s is absent from SHA256SUMS: %s" % (label, value))
        return None
    path = root / value
    if path.is_symlink() or not path.is_file():
        errors.append("%s is not a regular bundle file: %s" % (label, value))
        return None
    return path


def _full_dump_parse_main_export(path, output_format):
    if output_format == "xray-json":
        document, _, _ = load_json_artifact(str(path), normalize_legacy=True)
        if document.get("artifact_schema") != "170hx-xray-portable-dump-v1":
            raise AuditError("portable XRAY export schema mismatch")
        return _parse_row_container(
            document.get("main_rows"), "portable XRAY main_rows"
        ), document
    input_format = {
        "xray-jsonl": "jsonl",
        "indexed-text": "indexed-text",
        "raw-u32le": "raw-u32le",
        "dstoro-jsonl": "jsonl",
    }.get(output_format, "json")
    loaded = load_row_passes(str(path), input_format=input_format)
    if len(loaded.get("passes", [])) != 1:
        raise AuditError("%s export did not decode to one MAIN pass" % output_format)
    return loaded["passes"][0], None


def _full_dump_parse_rir_export(path, kind):
    input_format = {
        "rir-raw-u32le": "raw-u32le",
        "rir-indexed-text": "indexed-text",
        "rir-jsonl": "jsonl",
    }.get(kind, "json")
    loaded = load_rir_words(str(path), input_format=input_format)
    if len(loaded.get("passes", [])) != 1:
        raise AuditError("%s export did not decode to one RIR pass" % kind)
    return loaded["passes"][0]


def _verify_full_dump_semantics(root, index_document, entries, artifact_by_path,
                                errors):
    """Independently replay the level-local layout-v3 index and every transport.

    The SHA manifest remains an unkeyed self-consistency boundary.  This pass
    prevents a re-sealed bundle from laundering contradictory status fields or
    mutually inconsistent MAIN/RIR encodings as a valid full dump.
    """
    if index_document.get("schema") != SCHEMA_VERSION:
        errors.append("full-dump root schema mismatch")
    if index_document.get("command") != "full-dump":
        errors.append("full-dump root command mismatch")
    layout_version = index_document.get("layout_version")
    if layout_version not in (2, 3):
        errors.append("unsupported full-dump layout version")

    request = index_document.get("request")
    if not isinstance(request, dict):
        errors.append("full-dump request object is absent")
        request = {}
    max_access = request.get("max_access")
    if max_access not in FULL_DUMP_OBSERVATION_LEVELS:
        errors.append("full-dump request max_access is invalid")
    requested_levels = request.get("requested_levels")
    if layout_version == 2 and requested_levels is None:
        legacy_levels = ("passive", "bar-read", "semantic-read")
        requested_levels = (
            list(legacy_levels[:legacy_levels.index(max_access) + 1])
            if max_access in legacy_levels else []
        )
    elif (
        not isinstance(requested_levels, list) or not requested_levels
        or len(requested_levels) != len(set(requested_levels))
        or any(value not in FULL_DUMP_OBSERVATION_LEVELS
               for value in requested_levels)
        or ("glitch-read" in requested_levels and requested_levels != ["glitch-read"])
    ):
        errors.append("full-dump requested-level inventory is invalid")
        requested_levels = []
    if request.get("writes_requested") is not False:
        errors.append("full-dump request must declare writes_requested=false")
    if request.get("planning_models") != list(PLANNING_MODELS):
        errors.append("full-dump planning-model inventory mismatch")
    goals = request.get("goals")
    if (
        not isinstance(goals, list) or len(goals) != len(set(goals))
        or any(value not in GOALS for value in goals)
    ):
        errors.append("full-dump requested-goal inventory is invalid")
    bounds = request.get("fuseless_bounds")
    if bounds is not None and (
        not isinstance(bounds, list) or len(bounds) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
        or not 0 <= bounds[0] <= bounds[1] < MAX_FUSE_ROWS
    ):
        errors.append("full-dump fuseless bounds are invalid")
        bounds = None
    if index_document.get("safety_boundary") != FULL_DUMP_SAFETY_BOUNDARY:
        errors.append("full-dump safety-boundary declaration mismatch")

    safety = index_document.get("safety")
    if not isinstance(safety, dict):
        errors.append("full-dump safety object is absent")
        safety = {}
    for key in (
        "has_live_write_primitive", "otp_execution_supported",
        "volatile_write_execution_supported",
    ):
        if safety.get(key) is not False:
            errors.append("full-dump safety %s must be false" % key)

    bindings = index_document.get("bindings")
    if not isinstance(bindings, dict):
        errors.append("full-dump bindings object is absent")
        bindings = {}
    if bindings.get("candidate_execution_performed") is not False:
        errors.append("full-dump candidate execution must be false")
    for binding in bindings.get("candidate_planners", []):
        if not isinstance(binding, dict) or binding.get("executed") is not False:
            errors.append("candidate-planner binding has invalid execution state")
    for binding in bindings.get("reference_plans", []):
        if (
            not isinstance(binding, dict)
            or binding.get("imported_as_authority") is not False
        ):
            errors.append("reference-plan binding was imported as authority")

    unavailable = index_document.get("unavailable")
    if not isinstance(unavailable, list):
        errors.append("full-dump unavailable inventory is not a list")
        unavailable = []
    required_failures = 0
    for ordinal, item in enumerate(unavailable, 1):
        if not isinstance(item, dict):
            errors.append("unavailable record %d is not an object" % ordinal)
            continue
        if not isinstance(item.get("required_for_requested_bundle"), bool):
            errors.append("unavailable record %d lacks a boolean required flag" % ordinal)
        elif item["required_for_requested_bundle"]:
            required_failures += 1
        if not isinstance(item.get("reason"), str) or not item["reason"]:
            errors.append("unavailable record %d lacks a reason" % ordinal)

    completion = index_document.get("completion")
    if not isinstance(completion, dict):
        errors.append("full-dump completion object is absent")
        completion = {}
    expected_status = "COMPLETE" if required_failures == 0 else "PARTIAL"
    if completion.get("requested_failures") != required_failures:
        errors.append("completion requested_failures disagrees with unavailable records")
    if completion.get("unavailable_records") != len(unavailable):
        errors.append("completion unavailable_records disagrees with inventory")
    if completion.get("status") != expected_status:
        errors.append("completion status disagrees with required failures")
    if completion.get("fabricated_rows") != 0:
        errors.append("completion fabricated_rows must be zero")
    if completion.get("otp_program_commands") != 0:
        errors.append("completion contains OTP program commands")
    if completion.get("programming_writes_performed") != 0:
        errors.append("completion contains programming writes")

    observations = index_document.get("observations")
    if not isinstance(observations, list):
        errors.append("layout-v3 observations is not a list")
        return
    expected_observation_ids = [
        "observation-%03d" % value for value in range(1, len(observations) + 1)
    ]
    observation_ids = [
        item.get("id") if isinstance(item, dict) else None for item in observations
    ]
    if observation_ids != expected_observation_ids:
        errors.append("observation IDs are not unique sequential ordinals")
    observation_by_id = {
        item.get("id"): item for item in observations if isinstance(item, dict)
    }
    imported_count = 0
    live_count = 0
    for observation in observations:
        if not isinstance(observation, dict):
            errors.append("observation is not an object")
            continue
        origin = observation.get("origin")
        if origin == "imported":
            imported_count += 1
            if observation.get("requested_access") is not None:
                errors.append("imported observation has a requested live access")
        elif origin == "live":
            live_count += 1
            requested = observation.get("requested_access")
            if requested not in requested_levels:
                errors.append("live observation is outside requested_levels")
        else:
            errors.append("observation origin is invalid")
        level_directory = observation.get("level_directory")
        if layout_version == 3 and (
            not isinstance(level_directory, str) or not level_directory
            or level_directory.startswith("/")
            or ".." in Path(level_directory).parts
        ):
            errors.append("observation lacks a safe level-local directory")
        if "artifact" in observation:
            if layout_version == 3 and not observation.get("artifact", "").startswith(
                "levels/%s/observation/" % level_directory
            ):
                errors.append("observation artifact escaped its level directory")
            path = _full_dump_index_path(
                root, entries, observation.get("artifact"),
                "observation artifact", errors,
            )
            if path is not None:
                relative = observation["artifact"]
                if observation.get("sha256") != entries.get(relative):
                    errors.append("observation artifact SHA-256 mismatch")
    if request.get("imported_source_count") != imported_count:
        errors.append("request imported_source_count mismatch")
    if request.get("live_capture_enabled") is not (live_count > 0):
        errors.append("request live_capture_enabled mismatch")
    if request.get("live_capture_enabled") and [
        item.get("requested_access") for item in observations
        if isinstance(item, dict) and item.get("origin") == "live"
    ] != requested_levels:
        errors.append("live observation order disagrees with requested_levels")

    specimens = index_document.get("specimens")
    if not isinstance(specimens, list):
        errors.append("layout-v3 specimens is not a list")
        return
    expected_specimen_ids = [
        "specimen-%03d" % value for value in range(1, len(specimens) + 1)
    ]
    specimen_ids = [
        item.get("id") if isinstance(item, dict) else None for item in specimens
    ]
    if specimen_ids != expected_specimen_ids:
        errors.append("specimen IDs are not unique sequential ordinals")

    explicit_rir_records = [
        item for item in artifact_by_path.values()
        if item.get("kind") == "imported-rir-source"
    ]
    if len(explicit_rir_records) > 1:
        errors.append("bundle contains multiple imported RIR sources")
    explicit_rir_path = None
    explicit_rir_format = "auto"
    if explicit_rir_records:
        explicit_rir_path = _full_dump_index_path(
            root, entries, explicit_rir_records[0].get("path"),
            "imported RIR source", errors,
        )
        if explicit_rir_path is not None and explicit_rir_path.stat().st_size == 32:
            explicit_rir_format = "raw-u32le"

    for specimen in specimens:
        if not isinstance(specimen, dict):
            errors.append("specimen is not an object")
            continue
        specimen_id = specimen.get("id", "<unknown>")
        observation = observation_by_id.get(specimen.get("observation"))
        if not isinstance(observation, dict) or "artifact" not in observation:
            errors.append("%s lacks a valid source observation" % specimen_id)
            continue
        level_directory = observation.get("level_directory")
        if layout_version == 3 and specimen.get("level_directory") != level_directory:
            errors.append("%s level directory disagrees with observation" % specimen_id)
        if layout_version == 3:
            expected_prefix = "levels/%s/specimens/%s/" % (
                level_directory, specimen_id,
            )
            for record in artifact_by_path.values():
                metadata = record.get("metadata", {})
                if (
                    isinstance(metadata, dict)
                    and metadata.get("specimen") == specimen_id
                    and not record.get("path", "").startswith(expected_prefix)
                ):
                    errors.append(
                        "%s artifact escaped its level-local directory: %s"
                        % (specimen_id, record.get("path"))
                    )
        source_path = _full_dump_index_path(
            root, entries, observation.get("artifact"),
            "%s source observation" % specimen_id, errors,
        )
        if source_path is None:
            continue
        selector = specimen.get("card_selector")
        input_format = observation.get("input_format", "auto")
        try:
            source_main = load_row_passes(
                str(source_path), selector, input_format
            )
        except (AuditError, OSError, UnicodeError) as exc:
            source_main = None
            if specimen.get("main", {}).get("available") is True:
                errors.append("%s source MAIN cannot be replayed: %s" % (specimen_id, exc))

        main = specimen.get("main")
        if not isinstance(main, dict):
            errors.append("%s MAIN index is absent" % specimen_id)
            main = {}
        if main.get("available") is True and source_main is not None:
            source_passes = source_main.get("passes", [])
            if main.get("pass_count") != len(source_passes):
                errors.append("%s MAIN pass_count mismatch" % specimen_id)
            pass_entries = main.get("passes")
            if not isinstance(pass_entries, list) or len(pass_entries) != len(source_passes):
                errors.append("%s MAIN pass index mismatch" % specimen_id)
                pass_entries = []
            for pass_index, (source_rows, pass_entry) in enumerate(
                zip(source_passes, pass_entries), 1
            ):
                label = "%s MAIN pass %d" % (specimen_id, pass_index)
                if pass_entry.get("pass") != pass_index:
                    errors.append("%s ordinal mismatch" % label)
                if pass_entry.get("known_rows") != len(source_rows):
                    errors.append("%s known-row count mismatch" % label)
                outputs = pass_entry.get("outputs")
                expected_output_formats = (
                    list(DUMP_OUTPUT_FORMATS)
                    if layout_version == 3 else
                    [value for value in DUMP_OUTPUT_FORMATS
                     if value != "xray-jsonl"]
                )
                if not isinstance(outputs, list) or [
                    item.get("format") for item in outputs if isinstance(item, dict)
                ] != expected_output_formats:
                    errors.append("%s output-format inventory mismatch" % label)
                    continue
                for output in outputs:
                    if not isinstance(output, dict):
                        errors.append("%s contains a malformed output" % label)
                        continue
                    output_format = output.get("format")
                    if output.get("status") != "exported":
                        if output.get("path") is not None:
                            errors.append("%s %s unavailable output has a path" % (label, output_format))
                        continue
                    output_path = _full_dump_index_path(
                        root, entries, output.get("path"),
                        "%s %s" % (label, output_format), errors,
                    )
                    if output_path is None:
                        continue
                    try:
                        decoded, xray_document = _full_dump_parse_main_export(
                            output_path, output_format
                        )
                    except (AuditError, OSError, UnicodeError, ValueError) as exc:
                        errors.append("%s %s cannot be re-imported: %s" % (label, output_format, exc))
                        continue
                    expected_rows = source_rows
                    if output_format in ("ffcalc-json", "xpatrik-json"):
                        if bounds is None:
                            errors.append("%s %s lacks explicit bounds" % (label, output_format))
                            continue
                        expected_rows = {
                            row: source_rows[row]
                            for row in range(bounds[0], bounds[1] + 1)
                            if row in source_rows
                        }
                    if decoded != expected_rows:
                        errors.append("%s %s rows differ from canonical source" % (label, output_format))
                    if xray_document is not None:
                        raw_passes = xray_document.get("passes")
                        try:
                            decoded_passes = [
                                _parse_row_container(value, "portable XRAY pass")
                                for value in raw_passes
                            ]
                        except (AuditError, TypeError):
                            decoded_passes = None
                        if decoded_passes != source_passes:
                            errors.append("%s portable XRAY all-pass replay mismatch" % label)

        rir_path = explicit_rir_path or source_path
        rir_format = explicit_rir_format if explicit_rir_path else input_format
        try:
            source_rir = load_rir_words(str(rir_path), selector, rir_format)
        except (AuditError, OSError, UnicodeError):
            source_rir = None
        rir_index = specimen.get("rir")
        if isinstance(rir_index, dict) and rir_index.get("available") is True:
            if source_rir is None:
                errors.append("%s RIR is indexed but source replay failed" % specimen_id)
            else:
                if rir_index.get("pass_count") != len(source_rir["passes"]):
                    errors.append("%s RIR pass_count mismatch" % specimen_id)
                if rir_index.get("coherent_repeated_passes") is not source_rir.get(
                    "coherent_repeated_passes"
                ):
                    errors.append("%s RIR coherence claim mismatch" % specimen_id)
                aggregate_path = _full_dump_index_path(
                    root, entries, rir_index.get("aggregate"),
                    "%s RIR aggregate" % specimen_id, errors,
                )
                if aggregate_path is not None:
                    try:
                        aggregate, _, _ = load_json_artifact(
                            str(aggregate_path), normalize_legacy=True
                        )
                        aggregate_passes = [
                            [parse_int(value, "aggregate RIR") for value in row_pass]
                            for row_pass in aggregate.get("passes", [])
                        ]
                        if aggregate_passes != source_rir["passes"]:
                            errors.append("%s RIR aggregate differs from source" % specimen_id)
                    except (AuditError, OSError, TypeError, ValueError) as exc:
                        errors.append("%s RIR aggregate cannot be replayed: %s" % (specimen_id, exc))
                pass_entries = rir_index.get("passes", [])
                if len(pass_entries) != len(source_rir["passes"]):
                    errors.append("%s RIR pass index mismatch" % specimen_id)
                for pass_index, (source_words, pass_entry) in enumerate(
                    zip(source_rir["passes"], pass_entries), 1
                ):
                    for output_path_value in pass_entry.get("outputs", []):
                        output_path = _full_dump_index_path(
                            root, entries, output_path_value,
                            "%s RIR pass %d output" % (specimen_id, pass_index),
                            errors,
                        )
                        if output_path is None:
                            continue
                        record = artifact_by_path.get(output_path_value, {})
                        try:
                            decoded = _full_dump_parse_rir_export(
                                output_path, record.get("kind")
                            )
                            if decoded != source_words:
                                errors.append(
                                    "%s RIR pass %d transport mismatch"
                                    % (specimen_id, pass_index)
                                )
                        except (AuditError, OSError, UnicodeError, ValueError) as exc:
                            errors.append(
                                "%s RIR pass %d cannot be re-imported: %s"
                                % (specimen_id, pass_index, exc)
                            )
                if source_main is not None:
                    pairing = _conversion_rir_pairing(source_main, source_rir)
                    claimed = rir_index.get("certified_same_specimen")
                    if claimed is not None and claimed is not pairing.get(
                        "certified_same_specimen"
                    ):
                        errors.append("%s RIR pairing certification mismatch" % specimen_id)
                    unsafe_exported = any(
                        output.get("format") == "dstoro-projection-json"
                        and output.get("status") == "exported"
                        for pass_entry in main.get("passes", [])
                        for output in pass_entry.get("outputs", [])
                        if isinstance(output, dict)
                    )
                    if unsafe_exported and pairing.get("certified_same_specimen") is not True:
                        errors.append(
                            "%s exported an unsafe combined MAIN/RIR projection "
                            "without certified pairing" % specimen_id
                        )

        planning = specimen.get("planning")
        if not isinstance(planning, list) or [
            item.get("model") for item in planning if isinstance(item, dict)
        ] != list(PLANNING_MODELS):
            errors.append("%s planning-model inventory mismatch" % specimen_id)
            continue
        for plan_entry in planning:
            model = plan_entry.get("model")
            if plan_entry.get("status") != "generated_non_executable":
                continue
            paths = {}
            for key in ("plan", "apply_dry_run", "otp_readiness"):
                paths[key] = _full_dump_index_path(
                    root, entries, plan_entry.get(key),
                    "%s %s %s" % (specimen_id, model, key), errors,
                )
            if any(value is None for value in paths.values()):
                continue
            try:
                plan, _, _ = load_json_artifact(
                    str(paths["plan"]), normalize_legacy=True
                )
                apply, _, _ = load_json_artifact(
                    str(paths["apply_dry_run"]), normalize_legacy=True
                )
                readiness, _, _ = load_json_artifact(
                    str(paths["otp_readiness"]), normalize_legacy=True
                )
            except (AuditError, OSError, UnicodeError) as exc:
                errors.append("%s %s planning artifact parse failed: %s" % (specimen_id, model, exc))
                continue
            if (
                plan.get("artifact_schema") != "170hx-xray-plan-v1"
                or plan.get("planning_model") != model
                or plan.get("executable") is not False
                or plan.get("operations_total") != 0
                or plan.get("access", {}).get("writes_performed") != 0
            ):
                errors.append("%s %s plan is not a zero-operation offline plan" % (specimen_id, model))
            if plan.get("source_artifact", {}).get("sha256") != observation.get("sha256"):
                errors.append("%s %s plan source binding mismatch" % (specimen_id, model))
            if (
                apply.get("artifact_schema") != "170hx-xray-apply-dry-run-v1"
                or apply.get("validation", {}).get("dry_run") is not True
                or apply.get("validation", {}).get("writes_performed") != 0
                or apply.get("access", {}).get("direct_user_mmio_writes") != 0
                or apply.get("access", {}).get("otp_program_commands") != 0
            ):
                errors.append("%s %s apply artifact is not an inert dry run" % (specimen_id, model))
            execution = readiness.get("execution", {})
            if (
                readiness.get("artifact_schema")
                != "170hx-xray-full-write-readiness-v1"
                or execution.get("requested") is not False
                or execution.get("backend_invoked") is not False
                or execution.get("writes_performed") != 0
                or execution.get("cmd_write_commands") != 0
                or execution.get("fusewdata_writes") != 0
                or execution.get("program_source_assertions") != 0
            ):
                errors.append("%s %s readiness artifact is not inert" % (specimen_id, model))


def verify_full_dump_bundle(bundle):
    root = Path(bundle)
    if root.is_symlink() or not root.is_dir():
        raise AuditError("full-dump bundle is not a real directory: %s" % root)
    manifest_path = root / "SHA256SUMS"
    index_path = root / "index.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise AuditError("full-dump bundle lacks a regular SHA256SUMS")
    if not index_path.is_file() or index_path.is_symlink():
        raise AuditError("full-dump bundle lacks a regular index.json")
    entries = {}
    ordering = []
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00\r\n]+)", line)
        if match is None:
            raise AuditError("malformed SHA256SUMS line %d" % line_number)
        digest, relative = match.groups()
        parts = Path(relative).parts
        if (
            relative.startswith("/") or relative.startswith("\\")
            or "\\" in relative or not parts or ".." in parts or "." in parts
            or Path(relative).as_posix() != relative
        ):
            raise AuditError("unsafe SHA256SUMS path at line %d" % line_number)
        if relative in entries:
            raise AuditError("duplicate SHA256SUMS path: %s" % relative)
        entries[relative] = digest
        ordering.append(relative)
    errors = []
    if ordering != sorted(ordering):
        errors.append("SHA256SUMS paths are not sorted")
    actual_files = set()
    file_modes = {}
    directory_modes = {}
    for path in root.rglob("*"):
        info = path.lstat()
        relative = _full_dump_relative(root, path)
        if stat.S_ISLNK(info.st_mode):
            errors.append("symlink present: %s" % relative)
        elif stat.S_ISREG(info.st_mode):
            file_modes[relative] = "%04o" % stat.S_IMODE(info.st_mode)
            if relative != "SHA256SUMS":
                actual_files.add(relative)
        elif stat.S_ISDIR(info.st_mode):
            directory_modes[relative] = "%04o" % stat.S_IMODE(info.st_mode)
        else:
            errors.append("non-regular bundle object: %s" % relative)
    expected_files = set(entries)
    if actual_files != expected_files:
        errors.append(
            "manifest file-set mismatch: missing=%s extra=%s"
            % (sorted(expected_files - actual_files), sorted(actual_files - expected_files))
        )
    mismatches = []
    for relative, expected in sorted(entries.items()):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        actual = sha256_file(str(path))
        if actual != expected:
            mismatches.append({
                "path": relative, "expected": expected, "actual": actual,
            })
    if mismatches:
        errors.append("one or more artifact digests mismatch")
    bad_file_modes = {
        path: mode for path, mode in sorted(file_modes.items())
        if mode != "0600"
    }
    root_mode = "%04o" % stat.S_IMODE(root.lstat().st_mode)
    bad_directory_modes = {
        path: mode for path, mode in sorted(directory_modes.items())
        if mode != "0700"
    }
    if root_mode != "0700":
        errors.append("bundle root mode is %s, expected 0700" % root_mode)
    if bad_file_modes:
        errors.append("one or more artifact modes are not 0600")
    if bad_directory_modes:
        errors.append("one or more directory modes are not 0700")
    try:
        index_document, _, _ = load_json_artifact(
            str(index_path), normalize_legacy=True
        )
    except (AuditError, OSError, UnicodeError) as exc:
        index_document = None
        errors.append("index validation failed: %s" % exc)
    if index_document is not None:
        if index_document.get("artifact_schema") != "170hx-xray-full-dump-v1":
            errors.append("unexpected or absent full-dump index schema")
        indexed_artifacts = index_document.get("artifacts")
        artifact_by_path = {}
        if not isinstance(indexed_artifacts, list):
            errors.append("index artifacts is not a list")
        else:
            indexed_paths = set()
            for item in indexed_artifacts:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    errors.append("malformed artifact record in index")
                    continue
                path = item["path"]
                if path in indexed_paths:
                    errors.append("duplicate artifact record in index: %s" % path)
                indexed_paths.add(path)
                artifact_by_path[path] = item
                if entries.get(path) != item.get("sha256"):
                    errors.append("index/manifest digest mismatch: %s" % path)
                artifact_path = root / path
                if artifact_path.is_file() and artifact_path.stat().st_size != item.get("size"):
                    errors.append("index size mismatch: %s" % path)
            manifest_payload_paths = set(entries) - {"index.json"}
            if indexed_paths != manifest_payload_paths:
                errors.append(
                    "index/manifest artifact-set mismatch: missing=%s extra=%s"
                    % (
                        sorted(manifest_payload_paths - indexed_paths),
                        sorted(indexed_paths - manifest_payload_paths),
                    )
                )
        if index_document.get("layout_version", 0) >= 2:
            observations = index_document.get("observations")
            if not isinstance(observations, list):
                errors.append("layout-v3 observations is not a list")
            else:
                expected_effects = _full_dump_operational_totals(observations)
                if index_document.get("operational_effects") != expected_effects:
                    errors.append(
                        "layout-v3 operational-effect aggregate mismatch"
                    )
                if expected_effects["otp_program_commands"] != 0:
                    errors.append(
                        "layout-v3 full-dump contains OTP program commands"
                    )
                safety = index_document.get("safety")
                completion = index_document.get("completion")
                if not isinstance(safety, dict):
                    errors.append("layout-v3 safety object is absent")
                else:
                    for key in (
                        "counts_complete", "direct_user_mmio_writes",
                        "helper_operational_mmio_writes",
                        "controller_read_commands",
                        "controller_read_commands_issued",
                        "otp_program_commands", "programming_writes_performed",
                    ):
                        if safety.get(key) != expected_effects.get(key):
                            errors.append(
                                "layout-v3 safety counter mismatch: %s" % key
                            )
                if not isinstance(completion, dict):
                    errors.append("layout-v3 completion object is absent")
                else:
                    for key in (
                        "writes_performed", "writes_performed_semantics",
                        "operational_writes_performed",
                        "programming_writes_performed", "otp_program_commands",
                    ):
                        if completion.get(key) != expected_effects.get(key):
                            errors.append(
                                "layout-v3 completion counter mismatch: %s" % key
                            )
            _verify_full_dump_semantics(
                root, index_document, entries, artifact_by_path, errors
            )
    return {
        "artifact_schema": "170hx-xray-full-dump-verification-v1",
        "bundle": str(root),
        "valid": not errors,
        "errors": errors,
        "manifest_entries": len(entries),
        "digest_mismatches": mismatches,
        "root_mode": root_mode,
        "bad_file_modes": bad_file_modes,
        "bad_directory_modes": bad_directory_modes,
        "authenticity_boundary": (
            "SHA256SUMS proves bundle self-consistency only; it is not signed "
            "and does not establish acquisition authenticity"
        ),
    }


def command_verify_full_dump(args):
    result = verify_full_dump_bundle(args.bundle)
    emit_json(result, args.output)
    return 0 if result["valid"] else 2


def _full_dump_requested_levels(args):
    explicit = list(getattr(args, "level", []) or [])
    if explicit:
        if len(explicit) != len(set(explicit)):
            raise AuditError("--level values must be unique")
        if "glitch-read" in explicit and len(explicit) != 1:
            raise AuditError(
                "glitch-read must be captured alone so prior controller writes "
                "cannot contaminate the transient zero-write window"
            )
        return explicit
    if args.max_access == "glitch-read":
        return ["glitch-read"]
    ordinary = ("passive", "bar-read", "semantic-read")
    return list(ordinary[:ordinary.index(args.max_access) + 1])


def command_full_dump(args):
    if args.output_dir in ("", "-"):
        raise AuditError("full-dump requires a new output directory")
    if any(path == "-" for path in args.source):
        raise AuditError("full-dump does not accept stdin sources; use a stable file")
    if (args.fuseless_start is None) != (args.fuseless_end is None):
        raise AuditError("--fuseless-start and --fuseless-end must be supplied together")
    if args.fuseless_start is not None and not (
        0 <= args.fuseless_start <= args.fuseless_end < MAX_FUSE_ROWS
    ):
        raise AuditError("invalid explicit fuseless bounds")
    if not args.source and args.no_live_capture:
        raise AuditError("full-dump has no source: remove --no-live-capture or add --source")

    requested_levels = _full_dump_requested_levels(args)
    root = Path(args.output_dir)
    if root.exists() or root.is_symlink():
        raise AuditError("refusing to overwrite full-dump output: %s" % root)
    parent = root.parent
    if not parent.exists() or not parent.is_dir():
        raise AuditError("full-dump output parent does not exist: %s" % parent)
    try:
        os.mkdir(str(root), 0o700)
    except FileExistsError as exc:
        raise AuditError("refusing to overwrite full-dump output: %s" % root) from exc
    os.chmod(str(root), 0o700)

    state = {"requested_failures": 0}
    index = base_document("full-dump")
    index.update({
        "artifact_schema": "170hx-xray-full-dump-v1",
        "layout_version": 3,
        "request": {
            "max_access": args.max_access,
            "requested_levels": requested_levels,
            "live_capture_enabled": not args.no_live_capture,
            "imported_source_count": len(args.source),
            "input_format": args.input_format,
            "include_unsafe_projections": bool(args.include_unsafe_projections),
            "fuseless_bounds": (
                [args.fuseless_start, args.fuseless_end]
                if args.fuseless_start is not None else None
            ),
            "planning_models": list(PLANNING_MODELS),
            "goals": list(args.goal) if args.goal else sorted(GOALS),
            "writes_requested": False,
        },
        "observations": [],
        "specimens": [],
        "bindings": {},
        "unavailable": [],
        "artifacts": [],
        "safety_boundary": FULL_DUMP_SAFETY_BOUNDARY,
    })

    try:
        reference_plans = _full_dump_bindings(args, index, state, root)
        rir_source = None
        if args.rir_input is not None:
            rir_directory = root / "inputs" / "rir"
            _full_dump_make_directory(rir_directory)
            rir_path = rir_directory / "original.bin"
            emit_bytes(Path(args.rir_input).read_bytes(), str(rir_path))
            artifact = _full_dump_record_artifact(
                index, root, rir_path, "imported-rir-source",
                {"original_path": str(Path(args.rir_input).resolve())},
            )
            rir_source = {
                "path": str(rir_path),
                "input_format": args.rir_input_format,
                "artifact": artifact["path"],
            }

        if not args.no_live_capture:
            for level in requested_levels:
                observation_id = "observation-%03d" % (
                    len(index["observations"]) + 1
                )
                level_directory = level
                directory = root / "levels" / level_directory / "observation"
                _full_dump_make_directory(directory)
                capture_path = directory / "capture.json"
                observation = {
                    "id": observation_id,
                    "origin": "live",
                    "requested_access": level,
                    "level_directory": level_directory,
                    "input_format": "json",
                    "absolute_path": str(capture_path),
                    "status": "unavailable",
                }
                index["observations"].append(observation)
                try:
                    command_live(_full_dump_live_namespace(
                        args, level, capture_path
                    ))
                    artifact = _full_dump_record_artifact(
                        index, root, capture_path, "live-capture",
                        {"requested_access": level},
                    )
                    document, _, _ = load_json_artifact(str(capture_path))
                    transports = _full_dump_observation_transports(
                        index, root, directory, document, observation_id, level
                    )
                    transports["json"] = artifact["path"]
                    assessment = _full_dump_capture_assessment(document, level)
                    effects = _full_dump_capture_operational_effects(document)
                    if effects.get("otp_program_commands") != 0:
                        raise AuditError(
                            "full-dump capture reported one or more OTP program commands"
                        )
                    observation.update({
                        "artifact": artifact["path"],
                        "sha256": artifact["sha256"],
                        "status": assessment["status"],
                        "assessment": assessment,
                        "operational_effects": effects,
                        "transports": transports,
                    })
                    if not assessment["complete"]:
                        _full_dump_note_unavailable(
                            index, state, "observation-level",
                            assessment["reason"], required=True,
                            observation=observation_id,
                            requested_access=level,
                        )
                except (AuditError, OSError, UnicodeError) as exc:
                    observation["error"] = str(exc)
                    _full_dump_note_unavailable(
                        index, state, "observation-level", str(exc),
                        required=True, observation=observation_id,
                        requested_access=level,
                    )

        for source_index, source in enumerate(args.source, 1):
            observation_id = "observation-%03d" % (
                len(index["observations"]) + 1
            )
            level_directory = "imported-%03d" % source_index
            directory = root / "levels" / level_directory / "observation"
            _full_dump_make_directory(directory)
            copied_path = directory / "imported-source.bin"
            observation = {
                "id": observation_id,
                "origin": "imported",
                "requested_access": None,
                "level_directory": level_directory,
                "input_format": args.input_format,
                "absolute_path": str(copied_path),
                "original_path": str(Path(source).resolve()),
                "status": "unavailable",
            }
            index["observations"].append(observation)
            try:
                emit_bytes(Path(source).read_bytes(), str(copied_path))
                artifact = _full_dump_record_artifact(
                    index, root, copied_path, "imported-source",
                    {"source_ordinal": source_index,
                     "original_path": observation["original_path"]},
                )
                observation.update({
                    "artifact": artifact["path"],
                    "sha256": artifact["sha256"],
                    "status": "copied_unvalidated",
                })
            except (AuditError, OSError, UnicodeError) as exc:
                observation["error"] = str(exc)
                _full_dump_note_unavailable(
                    index, state, "imported-source", str(exc), required=True,
                    observation=observation_id,
                )

        for observation in index["observations"]:
            if "artifact" not in observation:
                continue
            _full_dump_process_observation(
                index, state, root, observation, args, reference_plans,
                rir_source,
            )

        for observation in index["observations"]:
            observation.pop("absolute_path", None)
        index["artifacts"] = sorted(
            index["artifacts"], key=lambda item: item["path"]
        )
        operational_totals = _full_dump_operational_totals(
            index["observations"]
        )
        index["operational_effects"] = operational_totals
        index["safety"].update({
            "counts_complete": operational_totals["counts_complete"],
            "direct_user_mmio_writes": operational_totals[
                "direct_user_mmio_writes"
            ],
            "helper_operational_mmio_writes": operational_totals[
                "helper_operational_mmio_writes"
            ],
            "controller_read_commands": operational_totals[
                "controller_read_commands"
            ],
            "controller_read_commands_issued": operational_totals[
                "controller_read_commands_issued"
            ],
            "otp_program_commands": operational_totals[
                "otp_program_commands"
            ],
            "programming_writes_performed": operational_totals[
                "programming_writes_performed"
            ],
        })
        index["completion"] = {
            "status": (
                "COMPLETE" if state["requested_failures"] == 0 else "PARTIAL"
            ),
            "requested_failures": state["requested_failures"],
            "unavailable_records": len(index["unavailable"]),
            "writes_performed": operational_totals["writes_performed"],
            "writes_performed_semantics": operational_totals[
                "writes_performed_semantics"
            ],
            "operational_writes_performed": operational_totals[
                "operational_writes_performed"
            ],
            "programming_writes_performed": operational_totals[
                "programming_writes_performed"
            ],
            "otp_program_commands": operational_totals[
                "otp_program_commands"
            ],
            "fabricated_rows": 0,
        }
        index["manifest"] = {
            "path": "SHA256SUMS",
            "algorithm": "sha256",
            "scope": "every regular bundle file except SHA256SUMS itself",
            "authenticity": "self-consistency only; not a signature",
        }
        index_path = root / "index.json"
        emit_json(index, str(index_path))
        manifest_path = root / "SHA256SUMS"
        emit_text(_full_dump_manifest(root), str(manifest_path))
        os.chmod(str(manifest_path), 0o600)
        verification = verify_full_dump_bundle(str(root))
        if not verification["valid"]:
            raise AuditError(
                "new full-dump bundle failed self-verification: %s"
                % "; ".join(verification["errors"])
            )
        return 0 if state["requested_failures"] == 0 else 2
    except Exception as exc:
        failure_path = root / "failure.json"
        if not failure_path.exists():
            try:
                emit_json({
                    "artifact_schema": "170hx-xray-full-dump-failure-v1",
                    "status": "INCOMPLETE",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recovery": (
                        "retain for diagnosis or remove explicitly; never treat "
                        "a bundle without a valid index and SHA256SUMS as complete"
                    ),
                }, str(failure_path))
            except Exception:
                pass
        raise


def command_helper_info(args):
    result = base_document("helper-info")
    result["existing_gpcprobe"] = {
        "path": args.gpcprobe,
        "present": os.path.exists(args.gpcprobe),
        "capabilities": ["INFO", "allowlisted READ32 through XRAY"],
        "limitations": [
            "ABI omits PCI domain", "fixed 16 MiB BAR mapping",
            "upstream tests 64-bit BAR type with 0x02 instead of PCI value 0x04",
            "no CAP_SYS_RAWIO or serialization",
            "no controller transaction or write ioctl", "no RM-shared lock",
            "maps concurrently with NVIDIA and does not own runtime-PM lifecycle",
        ],
    }
    helper_state = None
    helper_error = None
    if os.path.exists(args.controller_helper):
        helper = None
        try:
            helper = XrayHelperDevice(args.controller_helper)
            helper_state = helper.info_document()
        except (OSError, AuditError, ValueError) as exc:
            helper_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if helper is not None:
                helper.close()
    result["integrated_170hx_xray_helper"] = {
        "path": args.controller_helper,
        "present": os.path.exists(args.controller_helper),
        "implemented_by_this_release": True,
        "never_auto_build_or_load": True,
        "source": "170HX-XRAY/kernel/170hx_xray.c (GPL-2.0)",
        "upstream_lineage": (
            "matstatman/gpcprobe c68e7fc; independently extended and 64-bit BAR bug fixed"
        ),
        "observation_abi": [
            "INFO v%d with generation-bound full domain:BDF/identity/BAR/driver state"
            % XRAY_HELPER_ABI,
            "kernel-allowlisted READ32",
            "atomic journaled two-pass DUMP_MAIN_FF",
            "separate atomic journaled two-pass DUMP_RIR",
            "last-transaction journal retrieval",
        ],
        "guards": [
            "CAP_SYS_RAWIO", "exclusive open and per-card mutex",
            "default refusal while another driver owns the device",
            "generation/full-identity match, verified BAR length and PCI memory decode",
            "bounded polling, exact entry-state checks and retained partial-result journal",
            "no generic WRITE32 and no OTP programming surface",
        ],
        "live_info": helper_state,
        "open_error": helper_error,
    }
    result["all_in_one_meaning"] = (
        "one CLI/distribution with capability separation; Python alone cannot safely "
        "bypass kernel MMIO ownership or acquire proprietary RM controller locks"
    )
    emit_json(result, args.output)
    return 0


def _self_test_dump_formats():
    object_rows = _parse_row_container([
        {"row": 222, "raw": "0x00000141"},
        {"row": 223, "raw": None, "error": "timeout"},
        {"row": 224, "value": 0x00000181},
    ], "self-test object rows")
    _check(
        object_rows == {222: 0x00000141, 224: 0x00000181},
        "public object-row parsing preserves failed rows as unknown",
    )
    duplicate_alias_refused = False
    try:
        _parse_row_container({"1": 0x11, "0x1": 0x22}, "duplicate alias")
    except AuditError:
        duplicate_alias_refused = True
    _check(
        duplicate_alias_refused,
        "numeric row aliases cannot silently overwrite each other",
    )
    strict_text_refused = False
    try:
        _parse_text_rows("[001] 0x11 trailing\n")
    except AuditError:
        strict_text_refused = True
    _check(
        strict_text_refused,
        "indexed-text rejects trailing or unrecognized content",
    )
    ambiguous_container_refused = False
    try:
        _reject_ambiguous_main_containers({"rows": [1], "main": [1]})
    except AuditError:
        ambiguous_container_refused = True
    _check(
        ambiguous_container_refused,
        "multiple JSON MAIN containers fail closed",
    )
    empty_embedded_rir_refused = False
    try:
        load_embedded_rir({
            "source": "self-test", "sha256": "0" * 64,
            "selected_capture": {"rir_rows": {"passes": []}},
        })
    except AuditError:
        empty_embedded_rir_refused = True
    _check(
        empty_embedded_rir_refused,
        "empty embedded RIR pass lists fail with a controlled audit error",
    )
    jsonl_rows = _parse_jsonl_rows(
        '{"row":0,"value":1}\n{"row":1,"value":"0x2"}\n'
    )
    _check(jsonl_rows == {0: 1, 1: 2}, "strict Danylo JSONL parsing")
    raw = struct.pack("<%dI" % MAX_FUSE_ROWS, *range(MAX_FUSE_ROWS))
    parsed_raw = _parse_raw_u32le_rows(raw)
    _check(
        len(parsed_raw) == MAX_FUSE_ROWS and parsed_raw[511] == 511,
        "exact raw-u32le MAIN parsing",
    )
    refused_sparse = False
    try:
        _complete_main_vector({0: 0}, "self-test-full-image")
    except AuditError:
        refused_sparse = True
    _check(refused_sparse, "full-image export rejects sparse rows")
    formats = dump_format_schema()
    _check(
        set(DUMP_OUTPUT_FORMATS) == set(formats["outputs"])
        and set(DUMP_INPUT_FORMATS) == set(formats["inputs"]),
        "dump format registry and implementation choices agree",
    )
    public_rows = ["0x%08x" % index for index in range(MAX_FUSE_ROWS)]
    public_capture = {
        "schema": "ga100-public-capture-v1",
        "bdf": "0000:01:00.0",
        "raw_rows": [public_rows, list(public_rows)],
        "raw_match": False,  # Deliberately false: the importer must recompute it.
        "physical_rows_captured": True,
        "complete": True,
        "ok": True,
        "options": [
            {"NV_FUSE_TEST": "0x1"}, {"NV_FUSE_TEST": "0x1"},
        ],
    }
    main_words = list(range(MAX_FUSE_ROWS))
    rir_words = [0x100 + index for index in range(XRAY_RIR_ROWS)]
    dstoro_backup = {
        "schema": 1,
        "policy": "ga100-20c2-xpatrik-pcie-v2",
        "map_hash": "a" * 64,
        "utc": "2026-11-23T00:00:00+00:00",
        "bdf": "0000:01:00.0",
        "identity": {
            "serial": "12345678",
            "pci_id": "10de:20c2",
            "die": {"LOT_CODE_0": 1},
            "prom_sha256": "b" * 64,
        },
        "control": {
            name: value for name, value in (
                ("BOOT0", 0x170000A1), ("CTRL", 0xE0040000),
                ("ADDR", 0), ("DEBUG", 0), ("CRC", 0x55), ("EFP", 1),
                ("DFP", 0), ("DSOV", 0), ("EN_SW", 0),
                ("WPLM", 0xFFFFFFFF), ("CPLM", 0xFFFFFFFF),
                ("OPLM", 0xFFFFFFFF), ("WSEC", 0), ("SBFE", 0),
            )
        },
        "named": {
            "core": {"NV_FUSE_TEST_CORE": 1},
            "options": {"NV_FUSE_OPT_TEST": 2},
            "status": {"NV_FUSE_STATUS_TEST": 3},
        },
        "main": main_words,
        "rir": rir_words,
        "complete": True,
        "shadow_matches_main": True,
        "main_hash": _packed_u32_pass_sha256(main_words),
        "rir_hash": _packed_u32_pass_sha256(rir_words),
        "rir_observation": {"fresh_readback": True},
        "collector_sha256": "c" * 64,
    }
    profile = _dstoro_backup_profile(dstoro_backup)
    _check(
        profile["matches_profile"]
        and profile["content_integrity_valid"] is True
        and profile["main_hash_matches"] is True
        and profile["rir_hash_matches"] is True
        and profile["profile_variant"] == "current-producer"
        and profile["map_hash_recomputed"] is False,
        "Danylo backup profile is structurally gated without inventing map-hash validation",
    )
    legacy_profile = _dstoro_backup_profile({
        key: value for key, value in dstoro_backup.items()
        if key != "rir_observation"
    })
    _check(
        legacy_profile["matches_profile"]
        and legacy_profile["content_integrity_valid"] is True
        and legacy_profile["profile_variant"] == "legacy-without-rir-observation",
        "legacy Danylo backup variant is recognized without claiming current-producer shape",
    )
    damaged_profile = _dstoro_backup_profile(dict(
        dstoro_backup, main_hash="0" * 64
    ))
    _check(
        damaged_profile["matches_profile"]
        and damaged_profile["main_hash_matches"] is False
        and damaged_profile["content_integrity_valid"] is False,
        "Danylo format recognition exposes a content-digest mismatch",
    )
    refused_projection = False
    try:
        _require_unsafe_projection("xpatrik-json", False)
    except AuditError:
        refused_projection = True
    _check(
        refused_projection,
        "actionable target projection requires explicit unsafe acknowledgement",
    )
    _require_unsafe_projection("xpatrik-json", True)
    _require_unsafe_projection("xray-json", False)
    with tempfile.TemporaryDirectory(prefix="170hx-xray-selftest-") as directory:
        fixture = Path(directory) / "public-capture.json"
        fixture.write_text(_strict_json_dumps(public_capture), encoding="utf-8")
        loaded = load_row_passes(str(fixture))
        _check(
            loaded["detected_format"] == "ga100-public-capture-v1"
            and len(loaded["passes"]) == 2
            and loaded["passes"][1][511] == 511,
            "ga100-public-capture-v1 exact two-pass import",
        )
        _check(
            loaded["format_claims"]["computed_raw_match"] is True
            and loaded["format_claims"]["declared_raw_match_matches_computed"] is False,
            "public capture declarations are independently recomputed",
        )
        _check(
            _conversion_register_values(public_capture).get("NV_FUSE_TEST") == 1,
            "stable public option passes become normalized register evidence",
        )

        dstoro_path = Path(directory) / "dstoro-backup.json"
        dstoro_path.write_text(_strict_json_dumps(dstoro_backup), encoding="utf-8")
        loaded_dstoro = load_row_passes(str(dstoro_path))
        loaded_dstoro_rir = load_rir_words(str(dstoro_path))
        _check(
            loaded_dstoro["detected_format"] == "dstoro-backup-json"
            and loaded_dstoro["format_claims"]["validator_conformance_unverified"]
            and loaded_dstoro["passes"][0][511] == 511
            and loaded_dstoro_rir["detected_format"] == "dstoro-backup-json"
            and loaded_dstoro_rir["words"] == rir_words,
            "true Danylo MAIN/RIR envelope is detected only through its gated profile",
        )
        damaged_path = Path(directory) / "dstoro-damaged.json"
        damaged_path.write_text(
            _strict_json_dumps(dict(dstoro_backup, main_hash="0" * 64)),
            encoding="utf-8",
        )
        damaged_refused = False
        try:
            load_row_passes(str(damaged_path))
        except AuditError:
            damaged_refused = True
        _check(
            damaged_refused,
            "claimed Danylo backups with bad content digests are not imported",
        )

        generic_path = Path(directory) / "generic-main.json"
        generic_path.write_text(
            _strict_json_dumps({"main": main_words, "rir": rir_words}), encoding="utf-8"
        )
        _check(
            load_row_passes(str(generic_path))["detected_format"]
            == "generic-main-json"
            and load_rir_words(str(generic_path))["detected_format"]
            == "generic-rir-json",
            "generic main/rir vectors are never mislabeled as Danylo backups",
        )

        projection_path = Path(directory) / "xray-dstoro-projection.json"
        projection_path.write_text(_strict_json_dumps({
            "schema": "170hx-xray-dstoro-projection-v1",
            "compatible_with_dstoro_backup_validator": False,
            "main": main_words,
            "rir": rir_words,
        }), encoding="utf-8")
        _check(
            load_row_passes(str(projection_path))["detected_format"]
            == "170hx-xray-dstoro-projection-v1"
            and load_rir_words(str(projection_path))["detected_format"]
            == "170hx-xray-dstoro-projection-rir-v1",
            "XRAY's nonconformant projection is recognized separately from a backup",
        )

        xray_path = Path(directory) / "portable-xray.json"
        common_args = {
            "input": str(dstoro_path), "input_format": "auto", "card": None,
            "pass_index": 1, "rir_input": None, "rir_input_format": "auto",
            "fuseless_start": None, "fuseless_end": None, "sku": "8gb",
            "bdf": None, "row_view": "unknown",
            "provenance": "self-test conversion", "output": str(xray_path),
            "allow_unsafe_projection": False,
        }
        command_convert_dump(argparse.Namespace(
            output_format="xray-json", **common_args
        ))
        portable = _strict_json_loads(xray_path.read_text(encoding="utf-8"))
        portable_rows = load_row_passes(str(xray_path))
        portable_rir = load_rir_words(str(xray_path))
        _check(
            portable["source_metadata"]["identity"] == dstoro_backup["identity"]
            and portable["source_metadata"]["control"] == dstoro_backup["control"]
            and portable["source_metadata"]["named"] == dstoro_backup["named"]
            and portable_rows["passes"][0] == loaded_dstoro["passes"][0]
            and portable_rir["words"] == rir_words,
            "portable XRAY round trip preserves nested identity/controller/named claims and rows",
        )
        _check(
            portable["loss_report"]["authority_created"] is False
            and portable["loss_report"]["omitted_main_passes"] == []
            and portable["loss_report"]["omitted_rir_passes"] == [],
            "portable XRAY carries an explicit no-authority loss report",
        )

        xpatrik_path = Path(directory) / "xpatrik.json"
        xpatrik_args = dict(common_args)
        xpatrik_args.update({
            "input": str(xray_path), "output": str(xpatrik_path),
            "fuseless_start": 222, "fuseless_end": 486,
            "allow_unsafe_projection": True,
        })
        command_convert_dump(argparse.Namespace(
            output_format="xpatrik-json", **xpatrik_args
        ))
        xpatrik = _strict_json_loads(xpatrik_path.read_text(encoding="utf-8"))
        reparsed_xpatrik = load_row_passes(str(xpatrik_path))
        _check(
            xpatrik["fusectrl"] == "0xE0040000"
            and xpatrik["crc_status"] == "0x55"
            and xpatrik["controller_idle"] is True
            and xpatrik["read_errors"] is None
            and xpatrik["_170hx_xray_source_metadata"]["identity"]
            == dstoro_backup["identity"],
            "xPatrik projection preserves exact controller/identity claims without inventing read errors",
        )
        _check(
            reparsed_xpatrik["passes"][0]
            == {index: index for index in range(222, 487)}
            and xpatrik["loss_report"]["actionable_target_shape"] is True
            and xpatrik["loss_report"]["unsafe_projection_acknowledged"] is True
            and len(xpatrik["loss_report"]["omitted_selected_main_rows"])
            == MAX_FUSE_ROWS - (487 - 222),
            "xPatrik row projection and explicit loss accounting round trip",
        )

        ffcalc_source_path = Path(directory) / "ffcalc-source.json"
        ffcalc_source = {
            "sku": "8gb",
            "ff_rows": {str(index): hex32(index) for index in range(MAX_FUSE_ROWS)},
            "option_words": {
                "gpc_disable": "0x00000001",
                "pcie_magic_gen4_dis": "0x00000001",
                "future_unknown_word": "0xa5a5a5a5",
            },
        }
        ffcalc_source_path.write_text(_strict_json_dumps(ffcalc_source), encoding="utf-8")
        ffcalc_xray_path = Path(directory) / "ffcalc-portable-xray.json"
        ffcalc_common = dict(common_args)
        ffcalc_common.update({
            "input": str(ffcalc_source_path), "output": str(ffcalc_xray_path),
        })
        command_convert_dump(argparse.Namespace(
            output_format="xray-json", **ffcalc_common
        ))
        ffcalc_roundtrip_path = Path(directory) / "ffcalc-roundtrip.json"
        ffcalc_export = dict(common_args)
        ffcalc_export.update({
            "input": str(ffcalc_xray_path), "output": str(ffcalc_roundtrip_path),
            "fuseless_start": 0, "fuseless_end": MAX_FUSE_ROWS - 1,
            "allow_unsafe_projection": True,
        })
        command_convert_dump(argparse.Namespace(
            output_format="ffcalc-json", **ffcalc_export
        ))
        ffcalc_roundtrip = _strict_json_loads(
            ffcalc_roundtrip_path.read_text(encoding="utf-8")
        )
        _check(
            ffcalc_roundtrip["option_words"] == {
                "future_unknown_word": "0xa5a5a5a5",
                "gpc_disable": "0x00000001",
                "pcie_magic_gen4_dis": "0x00000001",
            }
            and ffcalc_roundtrip["loss_report"]["omitted_ffcalc_option_words"] == [],
            "ffcalc logical option_words survive XRAY semantic round trip",
        )


def _self_test_header_parser():
    sample = """
#define NV_FUSE_TEST 0x00820100 /* R--4R */
#define NV_FUSE_TEST_DATA 7:4 /* R-IVF */
#define NV_FUSE_TEST__WIDTH 4
#define NV_FUSE_TEST__PRI_ALIAS_0 0x00000006
#define NV_FUSE_TEST__PRI_ALIAS_0_DATA 11:8
#define NV_FUSE_TEST__PRI_ALIAS_0_WIDTH 4
"""
    parsed = parse_header_text(sample)
    _check(parsed["register_count"] == 1, "header register count")
    _check(parsed["registers"][0]["fields"][0]["msb"] == 7, "header field parse")
    _check(parsed["physical_aliases"][0]["aliases"][0]["row"] == 6, "header alias parse")
    rows = [0] * MAX_FUSE_ROWS
    rows[6] = 0x00000A00
    aliases = decode_conventional_alias_locations(rows, parsed)
    _check(
        aliases["fields"][0]["pieces"][0]["value"] == 0xA
        and aliases["fields"][0]["assembled_value"] is None,
        "header alias piece decode without invented assembly",
    )


def _self_test_decoder():
    rows = [0] * MAX_FUSE_ROWS
    start = 222
    end = 511
    rows[222] = (0x0001 << 16) | (39 << 6) | 2
    rows[223] = (0x0002 << 16) | (39 << 6) | 2
    rows[224] = 0xFFFFFFFF
    address = 0x001234
    rows[508] = 0x20 | 0x1D | ((address >> 2) << 6)
    rows[509] = 0xA5A5A5A5
    rows[510] = 0xFFFFFFFF
    rows[511] = 0x20
    decoded = decode_ff_iff(rows, start, end)
    _check(decoded["boundary"]["iff_start"] == 508, "IFF boundary")
    effective = decoded["effective_ff_records"]
    _check(len(effective) == 1 and effective[0]["data"] == "0x0002", "FF last-record replay")
    _check(decoded["iff_records"][0]["kind"] == "write_dw", "IFF opcode")
    _check(
        decoded["iff_records"][0]["decoded"]["data"] == "0xa5a5a5a5",
        "IFF continuation data",
    )
    _check(decoded["decode_complete"], "complete FF/IFF decode")
    catalog_fixture = {
        "effective_ff_records": [
            {"chain": 1, "block": 8, "data": "0x0118", "row": 250}
        ]
    }
    catalog = decode_logical_field_catalog(catalog_fixture, rows)
    catalog_by_name = {item["name"]: item for item in catalog["entries"]}
    _check(
        catalog_by_name["OPT_GPC_DISABLE"]["value"] == 0x23,
        "catalog cross-coordinate decode",
    )
    _check(
        catalog_by_name["OPT_FBIO_DISABLE"]["status"]
        == "incomplete_missing_ff_record"
        and catalog_by_name["OPT_FBIO_DISABLE"]["value"] is None,
        "missing FF record never defaults to zero",
    )

    set_field = (
        0x20 | 0x1E | (0x0B << 6) | (5 << 13) | (1 << 18) | (8 << 22)
    )
    field = decode_iff_operands(0x1E, [set_field])
    _check(field["space_name"] == "NV_PBUS_0", "SET_FIELD space map")
    _check(field["effective_bar0_address"] == "0x00001020", "SET_FIELD address")
    _check(field["field_width_bits"] == 3 and field["field_value"] == 3,
           "SET_FIELD fieldspec")

    old = [0] * 10
    old[0] = 1
    old[1] = 2
    old[7] = 0x20 | 0x1D
    old[8] = 0x12345678
    old[9] = 0x20
    old_capacity = scan_shared_ff_iff_capacity(old, 0, 9, reserve=5)
    _check(old_capacity["zero_gap_rows"] == 5, "old IFF capacity snapshot")
    shrunk = list(old)
    shrunk[7] = 0xFFFFFFFF
    shrunk[8] = 0xFFFFFFFF
    shrunk[9] = 0xFFFFFFFF
    shrink_capacity = scan_shared_ff_iff_capacity(shrunk, 0, 9, reserve=5)
    _check(shrink_capacity["zero_gap_rows"] == 0, "IFF shrink does not reclaim rows")

    invalid_set = [0] * MAX_FUSE_ROWS
    invalid_set[511] = 0x20 | 0x1E
    invalid_decoded = decode_ff_iff(invalid_set, start, end)
    _check(
        invalid_decoded["structurally_complete"]
        and not invalid_decoded["ga100_semantically_valid"]
        and not invalid_decoded["decode_complete"],
        "malformed SET_FIELD blocks certified decode",
    )
    poll = [0] * MAX_FUSE_ROWS
    poll[509] = 0x20 | 0x1B
    poll[510] = 0
    poll[511] = 0
    poll_decoded = decode_ff_iff(poll, start, end)
    _check(
        poll_decoded["structurally_complete"]
        and not poll_decoded["ga100_semantically_valid"],
        "GA100-unsupported POLL is forensic-only",
    )
    pvtrim_hole = 0x20 | 0x1E | (0x02 << 6) | (0xB << 18)
    pvtrim = decode_iff_operands(0x1E, [pvtrim_hole])
    _check(
        not pvtrim["target_in_advertised_space_range"]
        and bool(pvtrim.get("malformed")),
        "SET_FIELD PVTRIM sub-page hole",
    )


def _self_test_rir_and_manifest():
    def encode_rir(address, data=0, enable=True):
        return (address << 2) | (int(data) << 1) | int(enable)

    words = [0] * 8
    address = 123
    words[0] = encode_rir(address, data=1)
    words[1] = encode_rir(address, data=0)
    decoded = decode_ga100_rir_words(words)
    _check(
        not decoded["effective_replay_available"]
        and decoded["effective_repairs"] is None
        and decoded["duplicate_address_groups"][0]["slots"] == [0, 2],
        "RIR duplicate precedence remains unknown",
    )
    unique_words = [0] * 8
    unique_words[0] = encode_rir(address)
    unique = decode_ga100_rir_words(unique_words)
    _check(
        unique["effective_replay_available"]
        and len(unique["effective_repairs"]) == 1,
        "RIR unique-address replay",
    )
    force_one_words = [0] * 8
    force_one_words[0] = encode_rir(address, data=1)
    force_one = decode_ga100_rir_words(force_one_words)
    base_rows = [0] * MAX_FUSE_ROWS
    rir_view, derived_rows = apply_unique_rir_view(base_rows, force_one)
    _check(
        rir_view["state"] == "derived_conventional"
        and derived_rows[address // 32] == (1 << (address % 32))
        and not rir_view["physical_otp_rows_changed"],
        "offline conventional RIR effective-sense clone",
    )
    anomalous_view, anomalous_rows = apply_unique_rir_view(
        base_rows, force_one, address // 32, address // 32
    )
    _check(
        anomalous_view["state"]
        == "derived_conventional_with_fuseless_targets_ignored"
        and anomalous_rows[address // 32] == 0
        and len(anomalous_view["ignored_fuseless_targets"]) == 1,
        "RIR is not applied to the recovered fuseless plane",
    )
    disabled_words = [0] * 8
    disabled_words[0] = encode_rir(address, data=1, enable=False)
    disabled = decode_ga100_rir_words(disabled_words)
    _check(
        disabled["entries"][0]["state"] == "disabled_record"
        and not disabled["enabled_records"]
        and disabled["blank_slots"] == 15,
        "RIR enable-clear record is nonblank but inactive",
    )
    sample = {"artifact_schema": "test", "executable": False}
    attach_integrity(sample)
    _check(verify_integrity(sample)["valid"], "manifest integrity")
    sample["executable"] = True
    _check(not verify_integrity(sample)["valid"], "manifest tamper rejection")
    algorithm_tamper = {"artifact_schema": "test", "executable": False}
    attach_integrity(algorithm_tamper)
    algorithm_tamper["integrity"]["algorithm"] = "untrusted"
    _check(
        not verify_integrity(algorithm_tamper)["valid"],
        "manifest algorithm/scope validation",
    )
    evidence = {
        "exact_controller_main_512_rows": False,
        "physical_conventional_ff_region_certified": False,
        "immutable_physical_active_iff_certified": False,
        "exact_physical_512_rows": False,
        "row_vector_hashes": [],
        "registers": {},
        "rir_exact_repeated_capture": False,
        "analysis": {},
        "capture_coherent": False,
        "ga100_semantically_valid": False,
        "redirection_provenance": {
            "software_iff_redirection_excluded": False,
        },
        "official_ga100_descriptor_available": False,
    }
    plan = build_goal_plan("80gb_hbm", evidence)
    _check(not plan["executable"] and not plan["proposed_physical_operations"],
           "planner fail-closed empty operations")
    comparison_plan = build_goal_plan("pcie_gen4_x16", evidence, "compare")
    _check(
        comparison_plan["planning_model"]["selected_model"] == "compare"
        and comparison_plan["planning_model"]["public_model"][
            "adopted_as_authority"
        ] is False
        and comparison_plan["planning_model"]["cross_checked_theory"][
            "all_candidates_non_authoritative"
        ] is True
        and not comparison_plan["planning_model"]["physical_operations"],
        "public-versus-cross-checked planning models remain non-executable",
    )
    with tempfile.TemporaryDirectory(prefix="170hx-xray-plan-reference-") as directory:
        reference_path = Path(directory) / "reference.json"
        reference_path.write_text(
            _strict_json_dumps({
                "schema": "third-party-plan-v1",
                "executable": True,
                "writes": [{"row": 1, "value": 2}],
            }),
            encoding="utf-8",
        )
        reference = summarize_reference_plan(str(reference_path))
    _check(
        reference["declared_executable"] is True
        and reference["declared_operation_count"] == 1
        and reference["imported_as_authority"] is False
        and reference["used_to_construct_operations"] is False,
        "external plan import is comparison-only",
    )
    legacy_analysis = {
        "schema": SCHEMA_VERSION,
        "command": "analyze-rows",
        "exact_physical_decode": True,
        "physical_capture_certification": {
            "available": True,
            "software_iff_provenance": {
                "software_iff_redirection_excluded": True,
            },
        },
        "input": {
            "coherent": True,
            "row_view": "physical",
            "passes": [{"row_vector_sha256": "0" * 64}],
        },
        "analysis": {"ga100_semantically_valid": True},
    }
    legacy_evidence = _plan_evidence(legacy_analysis, legacy_analysis)
    _check(
        not legacy_evidence["exact_controller_main_512_rows"]
        and legacy_evidence["redirection_provenance"][
            "legacy_exact_physical_claim_ignored"
        ],
        "legacy exact-physical claim cannot certify current controller view",
    )


def _self_test_coherence():
    class FakeBackend:
        name = "self-test-fake-readonly"

        def __init__(self, fusectrl):
            self.fusectrl = fusectrl
            self.read_count = 0

        def read32(self, offset):
            self.read_count += 1
            if offset == REGISTER_BY_NAME["FUSECTRL"]["address"]:
                return self.fusectrl
            return 0

    idle = FakeBackend(0xE0040000)
    idle_summary = summarize_passes([capture_pass(idle, 1), capture_pass(idle, 2)])
    _check(idle_summary["coherent"], "idle controller coherence")
    busy = FakeBackend(0xE0050000)
    busy_summary = summarize_passes([capture_pass(busy, 1), capture_pass(busy, 2)])
    _check(not busy_summary["coherent"], "busy controller rejected")
    _check(bool(busy_summary["controller_context_issues"]), "busy controller reason")


def _self_test_helper_protocol():
    helper = object.__new__(XrayHelperDevice)
    request = XrayDump()
    request.abi_version = XRAY_HELPER_ABI
    request.struct_size = ctypes.sizeof(XrayDump)
    request.identity.generation = 1
    request.identity.domain = 0
    request.identity.bus = 1
    request.identity.devfn = 0
    request.identity.vendor = 0x10DE
    request.identity.device = 0x20C2
    request.kind = XRAY_DUMP_MAIN
    request.flags = XRAY_DUMP_F_CLEAN_CRC | XRAY_DUMP_F_NO_SW_IFF
    request.requested_passes = XRAY_MAX_PASSES
    response = XrayDump.from_buffer_copy(_xray_structure_bytes(request))
    response.row_count = XRAY_MAIN_ROWS
    response.completed_passes = XRAY_MAX_PASSES
    response.coherent = 1
    response.status = 0
    response.failed_pass = 0xFFFFFFFF
    response.failed_row = 0xFFFFFFFF
    response.writes_issued = 2049
    response.reads_issued = XRAY_MAIN_ROWS * XRAY_MAX_PASSES
    response.address_restored = 1
    response.debug_restored = 1
    response.entry.pmc_boot0 = 0x170000A1
    response.entry.fusectrl = 0xE0040000
    response.entry.fuseaddr = 0
    response.entry.en_sw_override = 0
    response.entry.iff_record = 0x01FB01E8
    response.entry.debugctrl = 0
    response.entry.crc_status = 0x55
    response.entry.enable_fuse_program_status = 1
    response.entry.disable_fuse_program_status = 0
    response.entry.bypass_fuses_status = 0
    response.entry.disable_sw_override_status = 0
    response.entry.iff_sw_fusing = 0
    response.exit = XrayControllerSnapshot.from_buffer_copy(
        _xray_structure_bytes(response.entry)
    )
    valid = helper._dump_document(response, request)
    _check(valid["protocol_valid"] and valid["certified_by_helper"],
           "strict helper response accepts exact response")
    response.abi_version = 999
    invalid_abi = helper._dump_document(response, request)
    _check(not invalid_abi["protocol_valid"] and not invalid_abi["certified_by_helper"],
           "strict helper response rejects ABI mutation")
    response.abi_version = XRAY_HELPER_ABI
    response.words[1][0] = 1
    invalid_coherence = helper._dump_document(response, request)
    _check(
        not invalid_coherence["protocol_valid"]
        and not invalid_coherence["certified_by_helper"],
        "helper recomputes row coherence",
    )
    response.words[1][0] = 0
    response.exit.crc_status = 0x54
    invalid_context = helper._dump_document(response, request)
    _check(
        not invalid_context["protocol_valid"]
        and not invalid_context["certified_by_helper"],
        "helper rejects changed/unclean controller context",
    )
    response.exit.crc_status = response.entry.crc_status
    response.writes_issued = 0
    invalid_writes = helper._dump_document(response, request)
    _check(
        not invalid_writes["protocol_valid"]
        and not invalid_writes["certified_by_helper"],
        "helper rejects impossible successful write accounting",
    )

    class FailedMainHelper:
        def __init__(self):
            self.calls = []

        def dump(self, bdf, kind, allow_bound=False):
            self.calls.append(kind)
            return {
                "protocol_valid": True, "status": -5,
                "completed_passes": 0, "coherent": False,
                "certified_by_helper": False, "passes": [],
                "pass_hashes": [], "driver_bound_at_entry": False,
                "address_restored": True, "debug_restored": True,
                "operational_writes_issued": 0,
                "cmd_read_commands_issued": 0,
                "completed_data_reads_lower_bound": 0,
                "counts_complete": True, "entry": {}, "exit": {},
            }

    failed_helper = FailedMainHelper()
    capture = {"goal_assessments": {}}
    attach_serialized_helper_dumps(
        capture, failed_helper, {"domain_bdf": "0000:01:00.0"},
        argparse.Namespace(
            allow_bound_controller=False, fuseless_start=None,
            fuseless_end=None, bounds_provenance=None,
        ),
    )
    _check(failed_helper.calls == [XRAY_DUMP_MAIN],
           "failed MAIN never invokes RIR")


def _self_test_native_capture_attestation():
    identity = {
        "generation": "0x0000000000000001",
        "domain_bdf": "0000:01:00.0",
        "vendor": "0x10de",
        "device": "0x20c2",
        "subsystem_vendor": "0x10de",
        "subsystem_device": "0x1585",
        "revision": "0xa1",
    }
    snapshot = {
        "pmc_boot0": "0x170000a1",
        "fusectrl": "0xe0040000",
        "fuseaddr": "0x000001fb",
        "en_sw_override": "0x00000000",
        "iff_record": "0x01fb01e8",
        "debugctrl": "0x00000000",
        "crc_status": "0x00000055",
        "enable_fuse_program_status": "0x00000001",
        "disable_fuse_program_status": "0x00000000",
        "bypass_fuses_status": "0x00000000",
        "disable_sw_override_status": "0x00000001",
        "iff_sw_fusing": "0x00000000",
        "debugctrl_priv_mask": "0xffffffff",
        "fusewdata_priv_mask": "0xffffffff",
        "fusectrl_priv_mask": "0xffffffff",
        "opt_priv_mask": "0xffffffff",
    }
    words = [0] * XRAY_MAIN_ROWS
    passes = [list(words), list(words)]
    encoded_passes = [[hex32(word) for word in row_pass] for row_pass in passes]
    pass_hashes = [_packed_u32_pass_sha256(row_pass) for row_pass in passes]
    transaction = {
        "helper_abi": XRAY_HELPER_ABI,
        "helper_struct_size": ctypes.sizeof(XrayDump),
        "identity": identity,
        "kind": "main_ff",
        "response_kind": XRAY_DUMP_MAIN,
        "flags": hex32(XRAY_DUMP_F_CLEAN_CRC | XRAY_DUMP_F_NO_SW_IFF),
        "status": 0,
        "status_text": "ok",
        "requested_passes": XRAY_MAX_PASSES,
        "completed_passes": XRAY_MAX_PASSES,
        "row_count": XRAY_MAIN_ROWS,
        "coherent": True,
        "kernel_reported_coherent": True,
        "failed_pass": None,
        "failed_row": None,
        "operational_writes_issued": 2049,
        "cmd_read_commands_issued": 1024,
        "completed_data_reads_lower_bound": 1024,
        "counts_complete": True,
        "address_restored": True,
        "debug_restored": True,
        "driver_bound_at_entry": False,
        "entry": dict(snapshot),
        "exit": dict(snapshot),
        "passes": encoded_passes,
        "pass_hashes": pass_hashes,
        "protocol_valid": True,
        "protocol_errors": [],
        "recovered_from_last": False,
        "transport_error": None,
        "certified_by_helper": True,
        "otp_commands": 0,
    }
    provenance = _helper_software_iff_provenance(transaction)
    capture = {
        "card": {
            "domain_bdf": identity["domain_bdf"],
            "xray_helper_identity": identity,
        },
        "physical_rows": {
            "available": True,
            "view": "physical",
            "rows": encoded_passes[0],
            "passes": encoded_passes,
            "pass_hashes": pass_hashes,
            "coherent": True,
            "transaction": transaction,
            "certification": {
                "available": True,
                "two_passes_in_one_helper_serialized_ioctl_transaction": True,
                "hardware_atomic_snapshot": False,
                "full_domain_identity_bound": True,
                "driver_unbound": True,
                "software_iff_redirection_excluded": True,
                "software_iff_provenance": provenance,
            },
        },
    }
    metadata = {
        "root_capture": {
            "schema": SCHEMA_VERSION,
            "command": "capture",
            "tool": {
                "name": TOOL_NAME,
                "version": TOOL_VERSION,
                "sha256": "0" * 64,
            },
        }
    }
    valid = verify_native_capture_attestation(
        capture, metadata, "main_ff", passes, "1" * 64
    )
    _check(valid["available"], "native capture envelope validation")
    capture["physical_rows"]["transaction"]["operational_writes_issued"] = 0
    tampered = verify_native_capture_attestation(
        capture, metadata, "main_ff", passes, "1" * 64
    )
    _check(
        not tampered["available"]
        and any("operational_writes_issued" in item for item in tampered["errors"]),
        "native capture envelope tamper rejection",
    )


def _check(condition, name):
    if not condition:
        raise AuditError("self-test failed: %s" % name)


def command_self_test(args):
    _check(
        GA100_READ_TARGET_DEVICE_IDS == frozenset((0x20B5, 0x20C2, 0x2080))
        and DEFAULT_DEVICE_IDS == frozenset((0x20C2,)),
        "post-conversion identity is attested without widening default discovery",
    )
    for device_id in sorted(GA100_READ_TARGET_DEVICE_IDS):
        identity_status = _strict_identity_status({
            "domain_bdf": "0000:01:00.0",
            "vendor": "0x10de",
            "device": "0x%04x" % device_id,
            "subsystem_vendor": "0x10de",
            "subsystem_device": "0x1585",
            "revision": "0xa1",
        })
        _check(identity_status["valid"], "GA100 read-target identity allowlist")
    _check(
        tuple(ACCESS_LEVELS)
        == (
            "passive", "bar-read", "glitch-read", "semantic-read", "plan",
            "volatile-write", "otp-write",
        ),
        "six ordinal levels plus isolated glitch-read branch",
    )
    _check(
        canonical_access_mode("read-write") == "volatile-write"
        and canonical_access_mode("full-write") == "otp-write",
        "legacy access aliases",
    )
    _check(GP_IOC_INFO == 0x4701, "GP_IOC_INFO ABI")
    _check(GP_IOC_READ32 == 0xC00C4702, "GP_IOC_READ32 ABI")
    _check(ctypes.sizeof(XrayIdentity) == 32, "170hx_xray identity ABI size")
    _check(ctypes.sizeof(XrayCardInfo) == 88, "170hx_xray card-info ABI size")
    _check(ctypes.sizeof(XrayInfo) == 1440, "170hx_xray info ABI size")
    _check(ctypes.sizeof(XrayReg32) == 48, "170hx_xray read ABI size")
    _check(ctypes.sizeof(XrayControllerSnapshot) == 64,
           "170hx_xray snapshot ABI size")
    _check(ctypes.sizeof(XrayDump) == 4320, "170hx_xray dump ABI size")
    _check(XrayInfo.cards.offset == 32, "170hx_xray INFO card offset")
    _check(XrayDump.words.offset == 224, "170hx_xray DUMP word offset")
    _check(XRAY_IOC_INFO == 0x85A05801, "170hx_xray INFO ioctl")
    _check(XRAY_IOC_READ32 == 0xC0305802, "170hx_xray READ32 ioctl")
    _check(XRAY_IOC_DUMP == 0xD0E05803, "170hx_xray DUMP ioctl")
    _check(XRAY_IOC_LAST == 0xD0E05804, "170hx_xray LAST ioctl")
    _check(
        XRAY_CAP_A100_CONTROLLER == 1 << 6,
        "170hx_xray explicit 20b5 controller capability",
    )
    _check(decode_crc_status(0x55)["overall"] == "present_no_errors", "CRC status")
    _check(
        0x0082000C not in DEFAULT_CAPTURE_OFFSETS,
        "FUSEWDATA excluded",
    )
    _check(
        REGISTER_BY_NAME["I1500_DATA"]["access_effect"] == "read_to_advance"
        and not REGISTER_BY_NAME["I1500_DATA"]["capture_safe"]
        and 0x009A3CBC not in DEFAULT_CAPTURE_OFFSETS,
        "stateful I1500 DATA read catalogued but excluded from ordinary capture",
    )
    _check(
        GA100_SHADOW_MANIFEST_SUMMARY == {
            "name_count": 771,
            "unique_address_count": 770,
            "kind_counts": {"core": 15, "options": 672, "status": 84},
            "represented_name_count": 771,
            "missing_names": [],
        },
        "complete generated GA100 shadow manifest inventory",
    )
    _check(
        len(registers_for_profile("core")) == 211
        and len(registers_for_profile("fuse-reference")) == 254
        and len(registers_for_profile("shadow-full")) == 878
        and len(registers_for_profile("hbm-static")) == 340
        and len(registers_for_profile("hbm-i1500-latches")) == 460
        and len(registers_for_profile("all-safe")) == 1084,
        "register profile boundaries and optional full-shadow surface",
    )
    _check(
        REGISTER_BY_NAME["NV_FUSE_OPT_NVLINK_DISABLE"]["address"]
        == REGISTER_BY_NAME["OPT_NVLINK_DISABLE"]["address"],
        "generated/header aliases resolve to one canonical register address",
    )
    parsed_allowlist, duplicate_allowlist = parse_kernel_read_allowlist_text(
        "static const u32 xray_read_offsets[] = { 0x1, 0x2\n};\n"
    )
    _check(
        parsed_allowlist == [1, 2] and not duplicate_allowlist,
        "kernel/Python allowlist parser",
    )
    provenance_census = registry_provenance_census()
    _check(
        provenance_census["catalogued_unique_addresses"] == 1109
        and provenance_census["safe_unique_addresses"] == 1084
        and provenance_census["stateful_or_unsafe_entries"] == 25
        and provenance_census["safe_with_fields"] == 92
        and provenance_census["safe_raw_word_only"] == 992
        and len(provenance_census["missing_interpretation_confidence"]) == 463
        and not provenance_census["missing_identity_confidence"]
        and not provenance_census["missing_source_anchor"]
        and len(provenance_census["generic_source_anchor"]) == 56
        and not provenance_census["completeness"]["specific_source_anchor_complete"]
        and not provenance_census["planning_enabled_entries"],
        "register provenance/confidence census",
    )
    conflicting_header = parse_header_text(
        "#define NV_FUSE_FUSECTRL 0x00820000 /* RW-4R */\n"
        "#define NV_FUSE_FUSECTRL_CMD 2:0 /* -WIVF */\n",
        source="synthetic-conflict", sha256="0" * 64,
    )
    _check(
        len(registry_provenance_census(conflicting_header)[
            "direct_header_comparison"
        ]["field_conflicts"]) == 1,
        "direct-header field conflict detection without allowlist promotion",
    )
    _check(not hasattr(BoundGpcProbeBackend, "write32"), "gpcprobe backend has no write")
    _check(not hasattr(SysfsResourceBackend, "write32"), "sysfs backend has no write")
    _check(not hasattr(XrayHelperDevice, "write32"), "170hx_xray helper has no generic write")
    _check(
        not any("Set" in name or "Create" in name for name in NVML_QUERY_SYMBOLS),
        "NVML query-only symbol surface",
    )
    iff_provenance = _helper_software_iff_provenance({
        "protocol_valid": True,
        "status": 0,
        "entry": {
            "en_sw_override": "0x00000000",
            "iff_sw_fusing": "0x00000000",
            "iff_record": "0x01fb01e8",
        },
        "exit": {
            "en_sw_override": "0x00000000",
            "iff_sw_fusing": "0x00000000",
            "iff_record": "0x01fb01e8",
        },
    })
    _check(
        iff_provenance["software_iff_redirection_excluded"]
        and iff_provenance["controller_iff_interval_observation"]["start"] == 488
        and iff_provenance["controller_iff_interval_observation"]["last"] == 507,
        "nonzero stable controller-IFF interval does not imply software redirection",
    )
    active_iff = _helper_software_iff_provenance({
        "protocol_valid": True,
        "status": 0,
        "entry": {
            "en_sw_override": "0x00000001",
            "iff_sw_fusing": "0x00000000",
            "iff_record": "0x01fb01e8",
        },
        "exit": {
            "en_sw_override": "0x00000001",
            "iff_sw_fusing": "0x00000000",
            "iff_record": "0x01fb01e8",
        },
    })
    _check(
        not active_iff["software_iff_redirection_excluded"],
        "active software override blocks locked-controller certification",
    )
    topology_fixture = {}
    for name, value in (
        ("GPC_DEFECTIVE", 0), ("OPT_GPC_DISABLE", 0),
        ("OPT_GPC_DISABLE_CP", 0), ("CTRL_OPT_GPC", 0),
        ("STATUS_OPT_GPC", 0),
    ):
        topology_fixture[name] = {"status": "ok", "raw": hex32(value)}
    for gpc in range(8):
        fixture_values = {
            "TPC_GPC%d_DEFECTIVE" % gpc: (1 if gpc == 0 else 0),
            "OPT_TPC_GPC%d_DISABLE" % gpc: 0x01,
            "OPT_TPC_GPC%d_RECONFIG" % gpc: 0x04,
            "CTRL_OPT_TPC_GPC%d" % gpc: 0,
            "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc: 0x04,
            "DISABLE_EXTEND_TPC_GPC%d" % gpc: 0,
            "STATUS_OPT_TPC_GPC%d" % gpc: 0x01,
        }
        for name, value in fixture_values.items():
            topology_fixture[name] = {"status": "ok", "raw": hex32(value)}
    topology = analyze_tpc_layers(topology_fixture)
    _check(
        topology["state"] == "complete"
        and all(item["base_formula_matches"] for item in topology["per_gpc"])
        and topology["sm_count_if_two_sms_per_tpc"]["defect_ceiling_sms"] == 126
        and topology["sm_count_if_two_sms_per_tpc"]["effective_sms"] == 112,
        "TPC defect/disable/RECONFIG/control/override/status layer analysis",
    )
    memory_fixture = {}
    for name, value in (
        ("FBP_DEFECTIVE", 0x840), ("OPT_FBP_DISABLE", 0x852),
        ("OPT_FBP_DISABLE_CP", 0), ("CTRL_OPT_FBP", 0),
        ("STATUS_OPT_FBP", 0x852),
        ("FBPA_DEFECTIVE", _expand_fbp_pairs(0x840)),
        ("OPT_FBPA_DISABLE", _expand_fbp_pairs(0x852)),
        ("OPT_FBPA_DISABLE_CP", 0), ("CTRL_OPT_FBPA", 0),
        ("STATUS_OPT_FBPA", _expand_fbp_pairs(0x852)),
        ("FBIO_DEFECTIVE", _expand_fbp_pairs(0x840)),
        ("OPT_FBIO_DISABLE", _expand_fbp_pairs(0x852)),
        ("OPT_FBIO_DISABLE_CP", 0), ("CTRL_OPT_FBIO", 0),
        ("STATUS_OPT_FBIO", _expand_fbp_pairs(0x852)),
        ("ROP_L2_DEFECTIVE", _expand_fbp_pairs(0x840)),
        ("OPT_ROP_L2_DISABLE", _expand_fbp_pairs(0x852)),
        ("OPT_ROP_L2_DISABLE_CP", 0),
    ):
        memory_fixture[name] = {"status": "ok", "raw": hex32(value)}
    memory = analyze_memory_layers(memory_fixture)
    _check(
        memory["state"] == "complete"
        and memory["capacity_projection"]["current_effective_gib"] == 64
        and memory["capacity_projection"]["defect_preserving_target_gib"] == 80
        and memory["capacity_projection"]["architecture_maximum_gib"] == 96
        and memory["capacity_projection"]["lowest_per_plane_scalar_is_joint_bound"] is False,
        "HBM effective/disable/defect/architecture layer analysis",
    )
    memory_mismatch = dict(memory_fixture)
    memory_mismatch["FBPA_DEFECTIVE"] = {
        "status": "ok", "raw": hex32(_expand_fbp_pairs(0x840) ^ 1),
    }
    _check(
        analyze_memory_layers(memory_mismatch)["capacity_projection"][
            "target_admissible"
        ] is False,
        "HBM companion-plane mismatch fails exact capacity admission",
    )
    _check(parse_bdf("0000:05:00.0")["bus"] == 5, "BDF parser")
    parsed_device = build_parser().parse_args([
        "capture", "--device", "0x20b0", "--output", "-"
    ])
    _check(parsed_device.device == ["0x20b0"],
           "explicit device ID does not retain default scope")
    root_card, root_metadata = _select_card_capture({
        "schema": SCHEMA_VERSION,
        "command": "capture",
        "cards": [{
            "card": {"domain_bdf": "0000:01:00.0"},
            "physical_rows": {"passes": [[0] * MAX_FUSE_ROWS]},
            "rir_rows": {"passes": [[0] * XRAY_RIR_ROWS]},
        }],
    }, "0")
    _check(
        root_card["card"]["domain_bdf"] == "0000:01:00.0"
        and root_metadata["root_capture"]["command"] == "capture",
        "root cards capture selection",
    )
    text_rows = _parse_text_rows(
        "[INFO]\n[000] 0\n[001] 1\n", allow_log_messages=True
    )
    _check(text_rows[0][0] == 0 and text_rows[0][1] == 1,
           "zero-padded text row labels")
    _self_test_dump_formats()
    _self_test_header_parser()
    _self_test_decoder()
    _self_test_rir_and_manifest()
    _self_test_coherence()
    _self_test_helper_protocol()
    _self_test_native_capture_attestation()
    result = base_document("self-test")
    result["status"] = "PASS"
    result["checks"] = [
        "20b5/20c2/2080 read-attestation identities with 20c2-only default scope",
        "six ordinal levels plus zero-write glitch branch, explicit plan level and legacy aliases",
        "ioctl ABI constants",
        "170hx_xray ABI-6 fixed-width structure sizes and ioctl constants",
        "strict helper response invariants, recomputed coherence and MAIN-before-RIR stop",
        "offline native-capture envelope validation and tamper rejection",
        "explicit device scope and root cards artifact ingestion",
        "CRC_STATUS decoder",
        "FUSEWDATA excluded from allowlist",
        "stateful I1500 DATA read excluded and kernel/Python registry audit parser",
        "register provenance/confidence census and direct-header conflict detection",
        "771-name generated shadow inventory, alias integrity and profile boundaries",
        "live backends expose no write32 primitive",
        "optional NVML surface binds query/cleanup symbols only",
        "stable nonzero controller-IFF interval versus software-redirection predicates",
        "TPC defect/disable/RECONFIG/control/override/status layer analysis",
        "HBM 64/80/96 layered capacity and strict companion-plane closure",
        "BDF parser",
        "zero-padded retained text-row parser",
        "strict multi-format dump parsing and sparse/full-image export gates",
        "direct-header register/alias parser",
        "direct-header alias-piece decode without invented assembly",
        "FF replay, logical catalog and exact IFF record decoder",
        "complete SET_FIELD space/address decoder and final-capacity rescan",
        "conventional-only RIR sense clone, manifest integrity/tamper rejection, fail-closed planner",
        "separate public/cross-checked planner models and comparison-only plan import",
        "legacy exact-physical analysis cannot self-certify current controller view",
        "controller/RWL/IFF context coherence gate",
    ]
    emit_json(result, args.output)
    return 0


def _command_full_report_mode(args, report_mode):
    """Delegate one explicit report mode to the separately tested engine."""
    script = Path(__file__).resolve().parent / "tools" / "170hx_full_report.py"
    spec = importlib.util.spec_from_file_location("xray_full_report_cli", str(script))
    if spec is None or spec.loader is None:
        raise AuditError("cannot load full-report orchestrator: %s" % script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    remainder = list(args.report_args)
    if remainder[:1] == ["--"]:
        remainder = remainder[1:]
    if any(str(value).split("=", 1)[0] == "--report-mode"
           for value in remainder):
        raise AuditError(
            "report mode is selected by the XRAY subcommand; do not pass "
            "--report-mode through report_args"
        )
    return module.main(
        [args.output_dir, "--report-mode", report_mode] + remainder
    )


def command_full_report(args):
    """Produce the fast inventory/read/dump/plan report without workloads."""
    return _command_full_report_mode(args, "report")


def command_full_report_bench(args):
    """Produce the full report plus diagnostics and ArchBench workloads."""
    return _command_full_report_mode(args, "report+bench")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "170HX-XRAY: GA100 observer with six ordinal levels plus a zero-write glitch branch, FF/IFF/RIR analyzer, "
            "and fail-closed planner"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    live = subparsers.add_parser(
        "capture", aliases=("live",),
        help="capture through an explicit access mode (legacy alias: live)",
    )
    live.add_argument(
        "--access", choices=ACCESS_MODES, default="semantic-read",
        help=(
            "passive, bar-read, zero-device-write glitch-read, semantic-read "
            "(default), plan, volatile-write, or otp-write; non-observation "
            "levels downgrade because capture has no write primitive"
        ),
    )
    live.add_argument(
        "--owner-busy", action="store_true",
        help="declare active workloads; controller transactions must downgrade to passive",
    )
    live.add_argument(
        "--controller-helper", default=DEFAULT_XRAY_HELPER,
        help="atomic 170hx_xray helper path; XRAY never builds or loads it automatically",
    )
    live.add_argument(
        "--allow-bound-controller", action="store_true",
        help=(
            "request controller reads while a driver owns the GPU; helper must have "
            "been explicitly loaded with allow_bound_controller=1 (high risk)"
        ),
    )
    live.add_argument("--bdf", action="append", default=[], help="full PCI BDF; repeatable")
    live.add_argument(
        "--all-targets", action="store_true",
        help=(
            "select every present supported GA100 identity (20c2/20b5/2080) and "
            "acknowledge a sequential multi-card controller attempt; that sequence "
            "stops after the first uncertified transaction"
        ),
    )
    live.add_argument(
        "--device", action="append", default=[],
        help=(
            "allowed supported GA100 PCI device ID; repeatable (default: 0x20c2, "
            "or all 20c2/20b5/2080 with --all-targets/exact --bdf)"
        ),
    )
    live.add_argument(
        "--backend",
        choices=("auto", "xray-helper", "gpcprobe", "sysfs", "metadata"),
        default="auto",
    )
    live.add_argument("--passes", type=int, default=3)
    live.add_argument(
        "--register-profile", choices=REGISTER_PROFILES, default="core",
        help=(
            "cumulative pure-read inventory: core (default), fuse-reference, "
            "shadow-full, hbm-static, hbm-i1500-latches, or all-safe; DATA read-to-advance "
            "ports are excluded from every ordinary profile"
        ),
    )
    live.add_argument(
        "--fuseless-start", type=int, default=None,
        help="optional explicit FF/IFF interval start for immediate offline decode",
    )
    live.add_argument(
        "--fuseless-end", type=int, default=None,
        help="optional explicit FF/IFF interval end for immediate offline decode",
    )
    live.add_argument(
        "--bounds-provenance",
        default="caller-supplied; authoritative GA100 descriptor absent",
        help="provenance label stored with optional FF/IFF bounds",
    )
    live.add_argument("--sysfs-root", default=DEFAULT_SYSFS_ROOT)
    live.add_argument("--gpcprobe", default=DEFAULT_GPCPROBE)
    live.add_argument(
        "--nvml", action="store_true",
        help="force query-only NVML (driver queries are on by default)",
    )
    live.add_argument(
        "--nvml-library", default=None,
        help="optional explicit libnvidia-ml path/name (query symbols only)",
    )
    live.add_argument(
        "--smi-features", action="store_true",
        help="force query-only nvidia-smi (driver queries are on by default)",
    )
    live.add_argument(
        "--no-driver-queries", action="store_true",
        help="disable NVML/nvidia-smi and use OS/direct-read observations only",
    )
    live.add_argument("--nvidia-smi", default="nvidia-smi")
    live.add_argument("--query-timeout", type=positive_finite_float, default=15.0)
    live.add_argument(
        "--invoked-source-sha256", default=None,
        help="caller-supplied SHA-256 of streamed source; checked against __file__ when readable",
    )
    live.add_argument("--output", default="-")
    live.set_defaults(handler=command_live)

    analyze = subparsers.add_parser("analyze-rows", help="decode externally captured rows offline")
    analyze.add_argument("input", help="JSON/text row capture, or - for stdin")
    analyze.add_argument(
        "--input-format", choices=DUMP_INPUT_FORMATS, default="auto",
        help="strict input grammar; raw-u32le must be selected explicitly",
    )
    analyze.add_argument(
        "--card", default=None,
        help="card/pci/bdf/index selector when a JSON artifact contains multiple cards",
    )
    analyze.add_argument("--fuseless-start", type=int, required=True)
    analyze.add_argument("--fuseless-end", type=int, required=True)
    analyze.add_argument(
        "--sparse-policy", choices=("reject", "zero-fill", "preserve"), required=True,
        help="explicit treatment of missing rows",
    )
    analyze.add_argument(
        "--row-view", choices=(
            "locked_controller", "physical", "software_redirected", "unknown"
        ), required=True,
        help=(
            "provenance classification; 'physical' is a legacy alias whose active "
            "IFF interval is still a state-qualified controller view"
        ),
    )
    analyze.add_argument("--provenance", default=None)
    analyze.add_argument(
        "--header", default=None,
        help="GA100 register specification for exhaustive conventional alias-piece decode",
    )
    analyze.add_argument(
        "--rir-input", default=None,
        help="optional JSON/JSONL/indexed-text/raw-u32le RIR input",
    )
    analyze.add_argument(
        "--rir-input-format", choices=DUMP_INPUT_FORMATS, default="auto",
        help="strict RIR grammar; raw-u32le means exactly eight packed words",
    )
    analyze.add_argument("--rir-card", default=None, help="RIR card selector; defaults to --card")
    analyze.add_argument("--rir-provenance", default=None)
    analyze.add_argument("--output", default="-")
    analyze.set_defaults(handler=command_analyze_rows)

    dump_formats = subparsers.add_parser(
        "dump-formats", help="describe supported strict dump import/export profiles"
    )
    dump_formats.add_argument("--output", default="-")
    dump_formats.set_defaults(handler=command_dump_formats)

    convert = subparsers.add_parser(
        "convert-dump",
        help="convert MAIN/RIR evidence between explicit public-tool formats offline",
    )
    convert.add_argument("input", help="source dump, or - for stdin")
    convert.add_argument(
        "--input-format", choices=DUMP_INPUT_FORMATS, default="auto",
        help="strict source grammar; raw-u32le must be selected explicitly",
    )
    convert.add_argument(
        "--output-format", choices=DUMP_OUTPUT_FORMATS, required=True,
    )
    convert.add_argument("--card", default=None, help="exact card selector for multi-card JSON")
    convert.add_argument("--pass-index", type=int, default=1, help="one-based MAIN pass")
    convert.add_argument(
        "--rir-input", default=None,
        help="optional separate RIR JSON/JSONL/indexed-text/raw-u32le input",
    )
    convert.add_argument(
        "--rir-input-format", choices=DUMP_INPUT_FORMATS, default="auto",
    )
    convert.add_argument("--fuseless-start", type=int, default=None)
    convert.add_argument("--fuseless-end", type=int, default=None)
    convert.add_argument("--sku", choices=("8gb", "10gb"), default="8gb")
    convert.add_argument("--bdf", default=None, help="explicit canonical BDF override")
    convert.add_argument(
        "--allow-unsafe-projection", action="store_true",
        help=(
            "acknowledge serialization into an actionable/public-burner-shaped "
            "target; grants no planning, validation, or write authority"
        ),
    )
    convert.add_argument(
        "--row-view", choices=(
            "locked_controller", "physical", "software_redirected", "unknown"
        ), default="unknown",
    )
    convert.add_argument("--provenance", default="offline format conversion; row view unverified")
    convert.add_argument("--output", default="-")
    convert.set_defaults(handler=command_convert_dump)

    plan = subparsers.add_parser(
        "plan", help="build a non-executable, evidence-gated per-card goal plan"
    )
    plan.add_argument("input", help="XRAY capture/analyze JSON, or - for stdin")
    plan.add_argument(
        "--access", choices=("plan",), default="plan",
        help="explicit offline planning level; performs no live device access",
    )
    plan.add_argument(
        "--card", default=None,
        help="optional exact capture index, domain BDF, UUID, or reported name",
    )
    plan.add_argument(
        "--goal", action="append", choices=sorted(GOALS), default=[],
        help="goal to assess; repeatable (default: all)",
    )
    plan.add_argument(
        "--planning-model", choices=PLANNING_MODELS, default="evidence-only",
        help=(
            "keep raw evidence only (default), describe dated public-tool claims, "
            "emit XRAY's cross-checked non-authoritative theory, or compare both"
        ),
    )
    plan.add_argument(
        "--reference-plan", action="append", default=[],
        help=(
            "external plan/artifact to inventory by hash and declared schema; "
            "repeatable and never imported as write authority"
        ),
    )
    plan.add_argument("--output", default="-")
    plan.set_defaults(handler=command_plan)

    apply_cmd = subparsers.add_parser(
        "apply", help="validate a hash-bound plan/manifest; dry-run only in this release"
    )
    apply_cmd.add_argument("manifest", help="XRAY 170hx-xray-plan-v1 JSON")
    apply_cmd.add_argument(
        "--access", choices=("volatile-write", "read-write"),
        default="volatile-write",
        help="must be volatile-write (legacy alias: read-write); never authorizes mutation",
    )
    apply_cmd.add_argument(
        "--execute", action="store_true",
        help="request real execution (always refused: no write backend in this release)",
    )
    apply_cmd.add_argument("--output", default="-")
    apply_cmd.set_defaults(handler=command_apply)

    full_write = subparsers.add_parser(
        "full-write",
        help=(
            "inspect legacy readiness or run the separately gated experimental "
            "VFI blank-FF append v3 backend"
        ),
    )
    full_write.add_argument(
        "manifest",
        help="legacy XRAY plan-v1 or VFI append sealed-manifest-v3 JSON",
    )
    full_write.add_argument(
        "--access", choices=("otp-write", "full-write"), default="otp-write",
        help="must be otp-write (full-write is a compatibility alias)",
    )
    full_write.add_argument(
        "--execute", action="store_true",
        help=(
            "v1: confirmation-only refusal; v3: enter the experimental live "
            "executor after receipt and exact interactive confirmations"
        ),
    )
    full_write.add_argument(
        "--backend", choices=("auto", "ga100-vfi-ff-append-v1"),
        default="auto",
    )
    full_write.add_argument(
        "--write-strategy",
        choices=("public-direct-compat", "evidence-led-guarded"),
        default=None,
        help=(
            "explicit v3 transaction lifecycle; mandatory with --execute, "
            "never inferred from the manifest"
        ),
    )
    full_write.add_argument("--bdf", default=None, help="exact v3 target BDF")
    full_write.add_argument(
        "--profile", default=None, help="exact v3 profile ID",
    )
    full_write.add_argument(
        "--receipt", default=None,
        help="v3 experimental review receipt bound to the manifest",
    )
    full_write.add_argument(
        "--journal", default=None,
        help="new create-only v3 journal path; filename must start with BDF",
    )
    full_write.add_argument(
        "--held-state-capture", default=None,
        help=(
            "fresh absolute native XRAY JSON from the exact current DSOV=0 "
            "window; mandatory only for v3 --execute"
        ),
    )
    full_write.add_argument(
        "--sysfs-root", default=DEFAULT_SYSFS_ROOT,
        help=(
            "PCI sysfs root for v3 preflight/testing; irreversible execution "
            "requires the canonical /sys/bus/pci/devices root"
        ),
    )
    full_write.add_argument(
        "--live-preflight", action="store_true",
        help="run exact live gates and two 512-row comparisons but no setup/program",
    )
    full_write.add_argument(
        "--test-row-only", action="store_true",
        help="program only the first sealed operation, then require cold replan",
    )
    full_write.add_argument("--output", default="-")
    full_write.set_defaults(handler=command_full_write)

    full_dump = subparsers.add_parser(
        "full-dump",
        help=(
            "build a private, manifest-sealed bundle of explicitly permitted "
            "observations, every supported dump export, and all planner models"
        ),
    )
    full_dump.add_argument(
        "output_dir",
        help="new directory to create; an existing path is always refused",
    )
    full_dump.add_argument(
        "--source", action="append", default=[],
        help="existing capture/dump to import and package; repeatable",
    )
    full_dump.add_argument(
        "--input-format", choices=DUMP_INPUT_FORMATS, default="auto",
        help="strict grammar shared by imported MAIN sources",
    )
    full_dump.add_argument(
        "--rir-input", default=None,
        help="optional separate RIR source used for every compatible selector",
    )
    full_dump.add_argument(
        "--rir-input-format", choices=DUMP_INPUT_FORMATS, default="auto",
    )
    full_dump.add_argument(
        "--no-live-capture", action="store_true",
        help="package only --source evidence; perform no device discovery/access",
    )
    full_dump.add_argument(
        "--max-access", choices=FULL_DUMP_OBSERVATION_LEVELS, default="passive",
        help=(
            "explicit ceiling for live observation orchestration (default: passive); "
            "ordinary lower levels are captured separately; glitch-read is an "
            "isolated transient capture, never a prefix ceiling"
        ),
    )
    full_dump.add_argument(
        "--level", action="append", choices=FULL_DUMP_OBSERVATION_LEVELS,
        default=[],
        help=(
            "capture this exact observation level instead of --max-access; "
            "repeatable for ordinary levels, while glitch-read must be used alone"
        ),
    )
    full_dump.add_argument(
        "--owner-busy", action="store_true",
        help="declare active workloads; semantic controller transactions fail closed",
    )
    full_dump.add_argument(
        "--controller-helper", default=DEFAULT_XRAY_HELPER,
        help=(
            "helper-serialized controller path (default: /dev/170hx_xray); "
            "never built or loaded"
        ),
    )
    full_dump.add_argument(
        "--allow-bound-controller", action="store_true",
        help="request the separately gated high-risk bound-controller observation path",
    )
    full_dump.add_argument("--bdf", action="append", default=[])
    full_dump.add_argument("--all-targets", action="store_true")
    full_dump.add_argument("--device", action="append", default=[])
    full_dump.add_argument(
        "--backend",
        choices=("auto", "xray-helper", "gpcprobe", "sysfs", "metadata"),
        default="auto",
    )
    full_dump.add_argument("--passes", type=int, default=3)
    full_dump.add_argument(
        "--register-profile", choices=REGISTER_PROFILES, default="all-safe",
    )
    full_dump.add_argument("--sysfs-root", default=DEFAULT_SYSFS_ROOT)
    full_dump.add_argument("--gpcprobe", default=DEFAULT_GPCPROBE)
    full_dump.add_argument("--nvml", action="store_true")
    full_dump.add_argument("--nvml-library", default=None)
    full_dump.add_argument("--smi-features", action="store_true")
    full_dump.add_argument("--no-driver-queries", action="store_true")
    full_dump.add_argument("--nvidia-smi", default="nvidia-smi")
    full_dump.add_argument(
        "--query-timeout", type=positive_finite_float, default=15.0
    )
    full_dump.add_argument("--invoked-source-sha256", default=None)
    full_dump.add_argument("--fuseless-start", type=int, default=None)
    full_dump.add_argument("--fuseless-end", type=int, default=None)
    full_dump.add_argument(
        "--bounds-provenance",
        default="caller-supplied; authoritative GA100 descriptor absent",
    )
    full_dump.add_argument("--sku", choices=("8gb", "10gb"), default="8gb")
    full_dump.add_argument(
        "--row-view", choices=(
            "locked_controller", "physical", "software_redirected", "unknown"
        ), default="unknown",
    )
    full_dump.add_argument(
        "--provenance", default="full-dump packaging; row view unverified",
    )
    full_dump.add_argument(
        "--include-unsafe-projections", action="store_true",
        help=(
            "serialize burner-shaped public formats; this is an export-only "
            "acknowledgement and grants no planning/write authority"
        ),
    )
    full_dump.add_argument(
        "--goal", action="append", choices=sorted(GOALS), default=[],
        help="goal to include in every planner model; repeatable (default: all)",
    )
    full_dump.add_argument(
        "--reference-plan", action="append", default=[],
        help="external comparison plan to hash/inventory; repeatable and untrusted",
    )
    full_dump.add_argument(
        "--candidate-planner", action="append", default=[],
        help="planner source to bind by path/hash without importing or executing it",
    )
    full_dump.add_argument(
        "--bind-workspace-candidate", action="store_true",
        help="bind the adjacent workspace candidate planner by hash when present",
    )
    full_dump.set_defaults(handler=command_full_dump)

    verify_bundle = subparsers.add_parser(
        "verify-full-dump",
        help="verify a full-dump file set, SHA-256 manifest, index, and private modes",
    )
    verify_bundle.add_argument("bundle")
    verify_bundle.add_argument("--output", default="-")
    verify_bundle.set_defaults(handler=command_verify_full_dump)

    helper_info = subparsers.add_parser(
        "helper-info", help="report gpcprobe and integrated atomic-helper capabilities"
    )
    helper_info.add_argument("--gpcprobe", default=DEFAULT_GPCPROBE)
    helper_info.add_argument("--controller-helper", default=DEFAULT_XRAY_HELPER)
    helper_info.add_argument("--output", default="-")
    helper_info.set_defaults(handler=command_helper_info)

    registry_audit = subparsers.add_parser(
        "registry-audit",
        help="verify exact equality of Python safe-read registry and kernel READ32 allowlist",
    )
    registry_audit.add_argument(
        "--kernel-source",
        default=str(Path(__file__).resolve().parent / "kernel" / "170hx_xray.c"),
    )
    registry_audit.add_argument(
        "--header", default=None,
        help=(
            "optional compatible direct register header for provenance/field "
            "comparison; it never expands the live allowlist"
        ),
    )
    registry_audit.add_argument("--output", default="-")
    registry_audit.set_defaults(handler=command_registry_audit)

    schema = subparsers.add_parser("schema", help="emit tool and direct-header field schema")
    schema.add_argument("--header", default=None, help="GA100 register specification to parse")
    schema.add_argument("--output", default="-")
    schema.set_defaults(handler=command_schema)

    self_test = subparsers.add_parser("self-test", help="run offline safety/decoder tests")
    self_test.add_argument("--output", default="-")
    self_test.set_defaults(handler=command_self_test)

    full_report = subparsers.add_parser(
        "full-report",
        help=(
            "produce 170HX-XRAY_REPORT_<timestamp>.md plus structured inventory, "
            "read/dump/plan evidence; workload benchmarks are refused"
        ),
    )
    full_report.add_argument("output_dir", help="new private report directory")
    full_report.add_argument(
        "report_args", nargs=argparse.REMAINDER,
        help=(
            "full-report options (run tools/170hx_full_report.py --help for the "
            "complete validated option list)"
        ),
    )
    full_report.set_defaults(handler=command_full_report)
    full_report_bench = subparsers.add_parser(
        "full-report-bench", aliases=("full-report+bench",),
        help=(
            "produce the same report plus diagnostics, ArchBench, transient "
            "power-limit control and sampled power envelopes"
        ),
    )
    full_report_bench.add_argument(
        "output_dir", help="new private report directory",
    )
    full_report_bench.add_argument(
        "report_args", nargs=argparse.REMAINDER,
        help=(
            "report+bench options (run tools/170hx_full_report.py --help for "
            "the complete validated option list)"
        ),
    )
    full_report_bench.set_defaults(handler=command_full_report_bench)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (AuditError, OSError, UnicodeError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
