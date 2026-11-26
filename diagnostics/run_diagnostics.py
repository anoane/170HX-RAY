#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-persistent CUDA/NVML diagnostics for CMP 170HX and A100.

The shipped orchestrator reads PCI/NVMe and query-only MIG/ECC/NVLink/media
inventory, compiles the adjacent CUDA helper, and can run temperature, HBM,
P2P, GDS and media capability tests.
Temperature mode prefers the fixed-capture 170hx_xray_therm helper, retains
gpcprobe as a legacy fallback, and uses the global-snapshot trigger/selector
only with explicit acknowledgement. The
remaining command surface never uses BAR0; no mode uses sysfs bind/unbind,
reset, sudo, or fuse interfaces.
Owner-selected CUDA binaries, compilers, GDS programs and media scripts are
unsandboxed trusted-code inputs running with the caller's privileges; a SHA-256
records their identity but does not prove their safety. GDS writes are permitted
only below an explicitly supplied scratch directory and are deleted in a
verified ``finally`` block.
"""

from __future__ import print_function

import argparse
import ast
import ctypes
import ctypes.util
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import xml.etree.ElementTree as ElementTree


SCHEMA = "170hx-xray-diagnostics-v1"
VERSION = "2.0.0"
FEATURE_CENSUS_SCHEMA = "170hx-xray-ga100-feature-census-v1"
OFA_PROBE_SCHEMA = "170hx-xray-ofa-api-probe-v1"
GA100_DEVICE_IDS = frozenset((0x20C2, 0x20B5))
GLITCH_OBSERVATION_ID = 0x2080
BDF_RE = re.compile(
    r"^(?P<domain>[0-9a-fA-F]{4,8}):(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<device>[0-1][0-9a-fA-F])\.(?P<function>[0-7])$"
)
NVIDIA_SMI_BDF_RE = re.compile(
    r"^(?P<domain>[0-9A-F]{8}):(?P<bus>[0-9A-F]{2}):"
    r"(?P<device>[0-1][0-9A-F])\.(?P<function>[0-7])$"
)
NVME_RE = re.compile(r"^nvme[0-9]+$")
POWER_OF_TWO_DEFAULTS = (1 << 20, 1 << 22, 1 << 24, 1 << 26,
                         1 << 28, 1 << 30)
MAX_CUDA_BUFFER_BYTES = 4 << 30
MAX_GDS_BYTES = 1 << 40
MAX_ITERATIONS = 10000
BANDWIDTH_TYPE_SPECS = {
    "float16": (16, 2, 1, "floating", "native-scalar"),
    "bfloat16": (16, 2, 1, "floating", "native-scalar"),
    "float32": (32, 4, 1, "floating", "native-scalar"),
    "float64": (64, 8, 1, "floating", "native-scalar"),
    "int4": (4, 1, 2, "signed", "two-nibbles-per-byte-low-then-high"),
    "uint4": (4, 1, 2, "unsigned", "two-nibbles-per-byte-low-then-high"),
    "int8": (8, 1, 1, "signed", "native-scalar"),
    "uint8": (8, 1, 1, "unsigned", "native-scalar"),
    "int16": (16, 2, 1, "signed", "native-scalar"),
    "uint16": (16, 2, 1, "unsigned", "native-scalar"),
    "int32": (32, 4, 1, "signed", "native-scalar"),
    "uint32": (32, 4, 1, "unsigned", "native-scalar"),
    "int64": (64, 8, 1, "signed", "native-scalar"),
    "uint64": (64, 8, 1, "unsigned", "native-scalar"),
}
TEMPERATURE_SCHEMA = "170hx-xray-ga100-temperature-census-v8"
TEMPERATURE_VERSION = "1.7.0"
TEMPERATURE_STACKS = (
    ("A", (0, 2)), ("B", (1, 4)), ("C", (3, 5)),
    ("D", (7, 9)), ("E", (6, 11)), ("F", (8, 10)),
)
TEMPERATURE_PUBLIC_HBMMAP_LABELS = ("A", "E", "B", "C", "F", "D")
TEMPERATURE_I1500_BRIDGE_STATES = {
    0x00: "idle", 0x01: "write-instr", 0x02: "wir-wait",
    0x03: "wir-update", 0x04: "write-shift", 0x05: "write-update",
    0x06: "write-done", 0x07: "write-wait-idle",
    0x08: "write-wait-req", 0x09: "read-capture",
    0x0A: "read-capture-wait", 0x0B: "read-shift",
    0x0C: "read-done", 0x0D: "read-wait-idle",
    0x0E: "read-wait-req", 0x0F: "wait-idle", 0x10: "branch",
}
TEMPERATURE_AGGREGATES = (
    ("tsense", 0x00020460, "three-state", "provider"),
    ("offset_tsense", 0x00020464, "three-state", "derived-offset"),
    ("constant", 0x00020468, "three-state",
     "software-constant-not-physical"),
    ("logical_max", 0x0002046C, "three-state", "derived-maximum"),
    ("gpu_max", 0x00020470, "valid-bit", "derived-maximum"),
    ("gpu_average", 0x00020474, "valid-bit", "derived-average"),
    ("gpu_offset_max", 0x00020478, "valid-bit",
     "derived-offset-maximum"),
    ("gpu_offset_average", 0x0002047C, "valid-bit",
     "derived-offset-average"),
)
TEMPERATURE_SNAPSHOT_AGGREGATES = (
    ("gpu_max", 0x0002084C),
    ("gpu_average", 0x00020850),
    ("gpu_offset_max", 0x00020854),
    ("gpu_offset_average", 0x00020858),
)
TEMPERATURE_SNAPSHOT_SELECTOR = 0x00020840
TEMPERATURE_SNAPSHOT_TRIGGER = 0x0002085C
TEMPERATURE_TS_AUTO_BASE = 0x0010C0F4
TEMPERATURE_TS_AUTO_STRIDE = 0x100
TEMPERATURE_OPT_TS_DIS_MASK = 0x00821004
TEMPERATURE_STATUS_OPT_GPC = 0x00820C1C
TEMPERATURE_STATUS_OPT_FBIO = 0x00820C14
TEMPERATURE_FBPA_BASE = 0x00900000
TEMPERATURE_FBPA_STRIDE = 0x4000
TEMPERATURE_FBPA_REGISTERS = {
    "cfg1_offset": 0x00000204,
    "test_macro_offset": 0x00003C9C,
    "instruction_offset": 0x00003CB4,
    "mode_offset": 0x00003CB8,
    "wdr_offset": 0x00003CBC,
    "shadow_instruction_offset": 0x00003CC0,
    "shadow_data_offset": 0x00003CC4,
    "status_offset": 0x00003CC8,
}
TEMPERATURE_FIXED_CONTEXT = (
    ("nv_therm_sensor_0", 0x00020008, "logical-hardware-endpoint"),
    ("nv_therm_sensor_1", 0x0002000C, "configuration"),
    ("nv_therm_sensor_2", 0x00020010, "configuration"),
    ("nv_therm_sensor_3", 0x00020014, "configuration"),
    ("nv_therm_sensor_4", 0x0002001C, "configuration"),
    ("nv_therm_sensor_6", 0x000200F0, "configuration"),
    ("nv_therm_sensor_7", 0x000200F4, "mux-view"),
    ("nv_therm_i2cs_sensor_00", 0x00020400, "alias"),
    ("nv_therm_i2cs_sensor_11", 0x00020444, "alias"),
    ("nv_therm_sensor_dedicated_overt_max", 0x00020448, "aggregate"),
    ("nv_therm_i2cs_sensor_13", 0x0002044C, "alias"),
    ("nv_therm_i2cs_sensor_14", 0x00020450, "alias"),
    ("nv_therm_sensor_8", 0x000204A0, "configuration"),
    ("nv_therm_sensor_9", 0x000204A4, "configuration"),
    ("nv_therm_statistics_temp_avg", 0x00020510, "derived-statistic"),
    ("nv_therm_statistics_ctrl", 0x00020514, "configuration"),
    ("nv_pgc6_sci_gpu_smbus_cmd00", 0x00118B80, "alias"),
    ("nv_therm_gpc_tsense_hs_offset_index", 0x000206CC, "configuration"),
    ("nv_therm_gpc_tsense_override_index", 0x000206D0, "configuration"),
    ("nv_therm_gpc_tsense_override", 0x000206D8, "configuration"),
    ("nv_therm_gpc_tsense_global_override", 0x000206DC, "configuration"),
    ("nv_therm_gpc_tsense_hs_offset", 0x000206E4, "configuration"),
)
TEMPERATURE_GPU_SCI_CONTEXT = (
    ("nv_pgc6_sci_fs_overt_ts_sm", 0x00118B90, "configuration"),
    ("nv_pgc6_sci_fs_overt_ts_adj", 0x00118B94, "configuration"),
    ("nv_pgc6_sci_fs_overt_thresh", 0x00118B98, "threshold"),
    ("nv_pgc6_sci_fs_overt_fusectrl", 0x00118B9C, "configuration"),
    ("nv_pgc6_sci_fs_overt_fuse", 0x00118BA0, "configuration"),
    ("nv_pgc6_sci_fs_overt_temp_coeff_nom", 0x00118BA4,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_temp_coeff_final", 0x00118BA8,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_ts_adc", 0x00118BAC, "mux-view"),
    ("nv_pgc6_sci_fs_overt_temperature", 0x00118BB0,
     "logical-hardware-endpoint"),
)
TEMPERATURE_GPC_ANALOG_REGISTERS = (
    ("tsense", 0x084, "mux-view"),
    ("adjustment", 0x088, "configuration"),
    ("raw_override", 0x0F0, "configuration"),
    ("ts_auto", 0x0F4, "configuration"),
)
TEMPERATURE_GPC_ANALOG_INTERPRETATION = (
    "four views of one GPC thermal analogue complex; endpoints are counted "
    "by the 8x3 GPC/BJT matrix, not by these words"
)
TEMPERATURE_POPULATION_RULE = (
    "endpoint expected only when STATUS_OPT_GPC[gpc]=0 and "
    "OPT_TS_DIS_MASK[3*gpc+bjt]=0"
)
TEMPERATURE_DRIVER_CENSUS_CONTRACT = {
    "canonical_direct_nvml": {
        "status": "implemented",
        "canonical": True,
        "requested_surfaces": [
            {"api": "FieldValues", "field_id": 82,
             "meaning": "memory-temperature"},
            {"api": "FieldValues", "field_id": 193,
             "meaning": "shutdown-threshold-ada-or-newer-where-supported"},
            {"api": "FieldValues", "field_id": 194,
             "meaning": "slowdown-threshold-ada-or-newer-where-supported"},
            {"api": "FieldValues", "field_id": 195,
             "meaning": "memory-max-threshold-ada-or-newer-where-supported"},
            {"api": "FieldValues", "field_id": 196,
             "meaning": "gpu-max-threshold-ada-or-newer-where-supported"},
            {"api": "FieldValues", "field_id": 308,
             "meaning": "subdegree-gpu-temperature"},
            {"api": "TemperatureV", "sensor": "GPU"},
            {"api": "TemperatureV", "sensor": "GPU_MAX"},
            {"api": "ThermalSettings", "maximum_channels": 3},
            {"api": "temperature-margin-and-thresholds",
             "support": "query-where-supported"},
        ],
        "unsupported_policy": "null-with-exact-return-code",
    },
    "fallback": {
        "backend": "nvidia-smi-query-gpu",
        "canonical": False,
        "warning": (
            "best-effort driver view; N/A remains null and is never coerced to zero"
        ),
    },
}
TEMPERATURE_INTERPRETATION = {
    "physical_sensor_count": "not inferred from readout-channel count",
    "gpc_address_space": "8 GPC x 3 BJT = 24 endpoints; population empirical",
    "hbm_address_space": (
        "24 FBPA latch channels grouped into 6 fixed FBP-pair views; "
        "temperature is exposed only under confirmed transaction context"
    ),
    "driver_query": (
        "direct NVML preserves per-call and per-field return codes; nvidia-smi is "
        "a non-canonical workload/fallback view; unsupported and N/A values remain "
        "null, never zero"
    ),
    "dedicated_helper": (
        "fixed capture ABI has no caller-selected MMIO primitive; legacy gpcprobe "
        "remains domain-less and is not the preferred backend"
    ),
}
NVML_VALUE_TYPE_NAMES = {
    0: "double", 1: "unsigned-int", 2: "unsigned-long",
    3: "unsigned-long-long", 4: "signed-long-long",
    5: "signed-int", 6: "unsigned-short",
}
NVML_RETURN_NAMES = {
    0: "success", 1: "uninitialized", 2: "invalid-argument",
    3: "not-supported", 4: "no-permission", 5: "already-initialized",
    6: "not-found", 7: "insufficient-size", 8: "insufficient-power",
    9: "driver-not-loaded", 10: "timeout", 11: "irq-issue",
    12: "library-not-found", 13: "function-not-found",
    14: "corrupted-inforom", 15: "gpu-is-lost", 16: "reset-required",
    17: "operating-system", 18: "library-rm-version-mismatch",
    19: "in-use", 20: "memory", 21: "no-data",
    22: "vgpu-ecc-not-supported", 23: "insufficient-resources",
    24: "frequency-not-supported", 25: "argument-version-mismatch",
    26: "deprecated", 27: "not-ready", 28: "gpu-not-found",
    29: "invalid-state", 30: "reset-type-not-supported", 999: "unknown",
}
NVML_THERMAL_CONTROLLER_NAMES = {
    0: "none", 1: "gpu-internal", 2: "adm1032", 3: "adt7461",
    4: "max6649", 5: "max1617", 6: "lm99", 7: "lm89", 8: "lm64",
    9: "g781", 10: "adt7473", 11: "sbmax6649", 12: "vbios-event",
    13: "os", 14: "nvsyscon-canoas", 15: "nvsyscon-e551",
    16: "max6649r", 17: "adt7473s", -1: "unknown",
}
NVML_THERMAL_TARGET_NAMES = {
    0: "none", 1: "gpu", 2: "memory", 4: "power-supply", 8: "board",
    9: "vcd-board", 10: "vcd-inlet", 11: "vcd-outlet", 15: "all",
    -1: "unknown",
}
TEMPERATURE_BOOLEAN_FIELDS = frozenset((
    "active_workload_override", "adc_override_enabled", "all_ones",
    "all_valid_views_equal", "allowlist_only", "aperture_available",
    "aperture_responded", "auto_polling", "autopoll_unchanged",
    "autopoll_untouched",
    "available", "ba_enabled", "badf", "bound_snapshot_acknowledged",
    "calibration_source_override", "canonical", "canonical_nvml_api",
    "coherent_tuple", "configuration_trigger", "constant",
    "control_source_override", "data_observed", "debug_bus_reset_select",
    "dependent_reads_skipped", "dram_reset_disabled",
    "dram_wrst_reset_disabled", "driver_bound_at_entry", "enabled",
    "fbpa_context_atomic", "fbpa_live", "field_values", "fields_in_range",
    "gpu_average",
    "gpu_max", "gpu_offset_average", "gpu_offset_max",
    "hbm_data_read_to_advance_acknowledged", "hbm_data_requested", "idle",
    "independent_physical_sensor", "invalid_bit",
    "hardware_auto_snapshot", "journal_valid", "latch_valid", "live",
    "loaded", "malformed_count",
    "margin_temperature", "mask_reads_available", "measurement_authoritative",
    "num_active_fbps_affirmative", "num_active_fbps_in_range",
    "num_active_fbps_reserved_zero", "observed_matches_population_masks",
    "opt_ts_dis_reserved_zero",
    "offset_tsense", "outer_call_succeeded", "override_enabled",
    "population_masks_unchanged", "postflight_identity_verified", "power_down",
    "power_on", "pure_completed", "query_only_public_api",
    "read_i1500_register",
    "read_increment_enabled", "read_skipped", "reset",
    "reserved_bits_zero",
    "selected_aperture_responded", "selected_fbpa_valid", "selector_all_ones",
    "select_sw_a", "select_sw_b",
    "selector_available", "selector_badf",
    "selector_dependent_reads_unconditionally_omitted",
    "selector_fields_in_range", "selector_race", "selector_reserved_bits_zero",
    "selector_restored", "selector_semantically_valid", "selector_stable",
    "selector_write_acknowledged", "sentinels_unchanged",
    "shadow_instruction_corroborates_temperature_empirically",
    "shadow_instruction_is_temperature", "signed", "site_present",
    "snapshot_completed", "stable_scale", "stream_raw",
    "suspicious_unavailable_pattern",
    "temperature_coefficient_source_override", "temperature_context_confirmed",
    "temperature_enabled", "temperature_gate_applied", "temperature_thresholds",
    "thermal_settings_slots_are_not_gpc_bjt_endpoints",
    "temperature_v", "test_macro_available", "test_macro_dram_reset_disabled",
    "test_macro_dram_wrst_reset_disabled", "test_macro_read_i1500_register",
    "test_macro_temperature_context_ready", "thermal_settings",
    "transaction_active_or_nonidle", "transaction_journal_available",
    "trigger_self_cleared", "tsense", "tsense_affirmative",
    "two_signal_corroborated", "valid", "write_offsets_directly_logged",
    "write_increment_enabled", "wso_mux_override",
    "presnap_bandgap_valid", "bandgap_mode_no_average", "bandgap_bypass",
    "bandgap_startup_bias_current", "ptat_bipolar_swap", "ptat_opamp_swap",
    "stop_swap", "bypass_ptat", "send_vbg", "average_vbg",
    "start_bandgap", "stop_swapping_bjt", "xtal_gate_invalidates_temperature",
))
FORBIDDEN_EXECUTABLES = frozenset((
    "sudo", "su", "doas", "reboot", "shutdown", "poweroff", "modprobe",
    "insmod", "rmmod", "devmem", "setpci", "nvflash", "nvflash64",
))


class DiagnosticError(RuntimeError):
    pass


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def _temperature_declared_schema_keys():
    """Return keys explicitly declared by the schema-v8 producer sources.

    This closes imported documents to the shipped producer vocabulary without
    duplicating several hundred leaf names in two independently drifting lists.
    Per-object validators below still enforce exact required/status-specific
    shapes; this guard rejects undeclared extensions at every nesting depth.
    """
    keys = set((
        "gpu_temperature_c", "memory_temperature_c", "utilization_percent",
        "power_w",
    ))
    directory = Path(__file__).resolve().parent
    for filename in ("temperature_probe.py", "nvml_temperature.py",
                     "xray_therm.py"):
        tree = ast.parse((directory / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Dict):
                candidates.extend(node.keys)
            elif isinstance(node, ast.Subscript):
                candidates.append(node.slice)
            for candidate in candidates:
                if (isinstance(candidate, ast.Constant) and
                        isinstance(candidate.value, str)):
                    keys.add(candidate.value)
    return frozenset(keys)


TEMPERATURE_DECLARED_SCHEMA_KEYS = _temperature_declared_schema_keys()


def _reject_json_constant(value):
    raise DiagnosticError("non-finite JSON constant is forbidden: %s" % value)


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiagnosticError("duplicate JSON object key: %r" % key)
        result[key] = value
    return result


def _strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", _reject_json_constant)
    kwargs.setdefault("object_pairs_hook", _json_object_without_duplicates)
    return _JSON_LOADS(*args, **kwargs)


def _strict_json_dumps(*args, **kwargs):
    kwargs.setdefault("allow_nan", False)
    return _JSON_DUMPS(*args, **kwargs)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def normalize_bdf(value):
    match = BDF_RE.fullmatch(str(value).strip())
    if match is None:
        raise DiagnosticError("invalid PCI BDF: %r" % value)
    domain = int(match.group("domain"), 16)
    if domain > 0xFFFF:
        raise DiagnosticError("PCI domain is outside 16-bit sysfs form: %s" % value)
    return "%04x:%02x:%02x.%x" % (
        domain, int(match.group("bus"), 16),
        int(match.group("device"), 16), int(match.group("function"), 16),
    )


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def read_hex(path):
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def read_numa_near(path, max_parents=4):
    candidate = Path(path)
    for _ in range(max_parents + 1):
        value = read_text(candidate / "numa_node")
        if value is not None:
            try:
                parsed = int(value, 10)
            except ValueError:
                parsed = -1
            return parsed if parsed >= 0 else None
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return None


def parse_link_speed(value):
    if not value:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*GT/s", value, re.I)
    if match is None:
        return None
    speed = float(match.group(1))
    table = ((2.5, 1), (5.0, 2), (8.0, 3), (16.0, 4),
             (32.0, 5), (64.0, 6))
    matched = min(table, key=lambda item: abs(item[0] - speed))
    return matched[1] if abs(matched[0] - speed) <= 0.05 else None


def parse_positive_size(value):
    match = re.fullmatch(r"([0-9]+)([kKmMgGtT]?)", str(value).strip())
    if match is None:
        raise argparse.ArgumentTypeError("invalid size: %s" % value)
    number = int(match.group(1))
    suffix = match.group(2).lower()
    scale = {"": 1, "k": 1 << 10, "m": 1 << 20,
             "g": 1 << 30, "t": 1 << 40}[suffix]
    result = number * scale
    if result <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return result


def is_power_of_two(value):
    return isinstance(value, int) and value > 0 and (value & (value - 1)) == 0


def find_executable(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve())
        path = Path(candidate)
        if path.is_file() and os.access(str(path), os.X_OK):
            return str(path.resolve())
    return None


class Runner(object):
    """No-shell runner; external executables remain unsandboxed trusted code."""

    def __init__(self, dry_run=False, timeout=600):
        self.dry_run = bool(dry_run)
        self.timeout = int(timeout)
        self.records = []

    def run(self, argv, cwd=None, env=None, timeout=None, read_only=False):
        if not isinstance(argv, (list, tuple)) or not argv:
            raise DiagnosticError("command must be a non-empty argv list")
        command = [str(item) for item in argv]
        executable_name = Path(command[0]).name
        if executable_name in FORBIDDEN_EXECUTABLES:
            raise DiagnosticError("forbidden diagnostic executable: %s" % executable_name)
        if any("/sys/bus/pci/drivers/" in item for item in command):
            raise DiagnosticError("driver bind/unbind path is forbidden")
        executable = find_executable((command[0],))
        record = {
            "argv": command,
            "cwd": None if cwd is None else str(Path(cwd).resolve()),
            "started_utc": utc_now(),
            "dry_run": self.dry_run,
            "environment_overrides": {
                str(key): (
                    str(value) if str(key) in
                    ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER")
                    else "<redacted>"
                )
                for key, value in (env or {}).items()
            },
            "executable": executable,
            "executable_sha256": (
                sha256_file(executable) if executable and Path(executable).is_file()
                else None
            ),
            "trust_boundary": (
                "unsandboxed executable with caller privileges; SHA-256 "
                "identifies bytes but does not establish safety"
            ),
        }
        if self.dry_run and not read_only:
            record.update({
                "status": "planned", "returncode": None, "duration_seconds": 0,
                "stdout": "", "stderr": "", "stdout_sha256": None,
                "stderr_sha256": None,
            })
            self.records.append(record)
            return record
        if executable is None:
            record.update({
                "status": "missing_executable", "returncode": 127,
                "duration_seconds": 0, "stdout": "",
                "stderr": "executable not found", "stdout_sha256": None,
                "stderr_sha256": hashlib.sha256(b"executable not found").hexdigest(),
            })
            self.records.append(record)
            return record
        command[0] = executable
        child_env = os.environ.copy()
        if env:
            child_env.update({str(key): str(val) for key, val in env.items()})
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=None if cwd is None else str(cwd), env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, shell=False,
                timeout=self.timeout if timeout is None else timeout,
                check=False,
            )
            stdout_raw = completed.stdout or b""
            stderr_raw = completed.stderr or b""
            record.update({
                "status": "completed", "returncode": completed.returncode,
                "duration_seconds": time.monotonic() - started,
                "stdout": stdout_raw.decode("utf-8", errors="replace"),
                "stderr": stderr_raw.decode("utf-8", errors="replace"),
                "stdout_sha256": hashlib.sha256(stdout_raw).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_raw).hexdigest(),
            })
        except subprocess.TimeoutExpired as exc:
            stdout_raw = exc.stdout or b""
            stderr_raw = exc.stderr or b""
            if isinstance(stdout_raw, str):
                stdout_raw = stdout_raw.encode("utf-8", errors="replace")
            if isinstance(stderr_raw, str):
                stderr_raw = stderr_raw.encode("utf-8", errors="replace")
            record.update({
                "status": "timeout", "returncode": None,
                "duration_seconds": time.monotonic() - started,
                "stdout": stdout_raw.decode("utf-8", errors="replace"),
                "stderr": stderr_raw.decode("utf-8", errors="replace"),
                "stdout_sha256": hashlib.sha256(stdout_raw).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_raw).hexdigest(),
            })
        self.records.append(record)
        return record


POWER_EVIDENCE_SCHEMA = "170hx-xray-benchmark-power-evidence-v1"
POWER_SUMMARY_SCHEMA = "170hx-xray-benchmark-power-summary-v1"
POWER_AVERAGE_METHOD = "arithmetic-mean-of-successful-periodic-samples"


def _parse_power_query_output(record):
    """Parse one nounits power.draw,power.limit row without accepting N/A."""
    if not isinstance(record, dict) or record.get("returncode") != 0:
        return None, None
    rows = [line.strip() for line in record.get("stdout", "").splitlines()
            if line.strip()]
    if len(rows) != 1:
        return None, None
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 2:
        return None, None
    try:
        draw = float(fields[0])
        limit = float(fields[1])
    except ValueError:
        return None, None
    if (not math.isfinite(draw) or draw < 0 or
            not math.isfinite(limit) or limit <= 0):
        return None, None
    return draw, limit


def _power_limit_matches(observed, requested):
    return (_is_finite_number(observed, positive=True) and
            _is_finite_number(requested, positive=True) and
            abs(float(observed) - float(requested)) <= 0.5)


class BenchmarkPowerEvidence(object):
    """Transient power-limit control plus periodic draw evidence.

    Control/query commands used at transaction boundaries are run through the
    normal command journal.  The sampling loop invokes the same attested
    nvidia-smi binary directly so it can run concurrently with the benchmark;
    its exact argv template, binary hash, interval, raw samples, and failures
    are retained in the evidence object.  Every successful prepare is paired
    with a best-effort restoration in ``finish``.
    """

    def __init__(self, family, cards, args, runner):
        self.family = str(family)
        self.cards = list(cards)
        self.args = args
        self.runner = runner
        self.requested = getattr(args, "power_limit_watts", None)
        self.interval_ms = getattr(args, "power_sample_interval_ms", 100)
        self.smi = find_executable(("nvidia-smi",))
        self.started_monotonic = None
        self.stop_event = threading.Event()
        self.thread = None
        self.prepared = False
        self.finished = False
        self.entries = []
        for card in self.cards:
            smi = card.get("nvidia_smi")
            self.entries.append({
                "bdf": card.get("bdf"),
                "uuid": smi.get("uuid") if isinstance(smi, dict) else None,
                "nvidia_smi_index": (
                    smi.get("nvidia_smi_index") if isinstance(smi, dict) else None
                ),
                "original_limit_watts": None,
                "observed_limit_before_watts": None,
                "observed_limit_after_enforce_watts": None,
                "observed_limit_post_benchmark_watts": None,
                "observed_limit_after_restore_watts": None,
                "limit_enforced_during_benchmark": False,
                "restore_attempted": False,
                "restore_succeeded": False,
                "sample_count": 0,
                "sample_failures": 0,
                "power_draw_w_min": None,
                "power_draw_w_max": None,
                "power_draw_w_avg": None,
                "power_draw_average_method": POWER_AVERAGE_METHOD,
                "sampling_span_seconds": 0.0,
                "samples": [],
                "pre_query_command": None,
                "set_command": None,
                "enforce_verification_query_command": None,
                "post_benchmark_query_command": None,
                "restore_command": None,
                "restore_verification_query_command": None,
                "error": None,
            })

    def _query_argv(self, entry):
        return [
            self.smi, "-i", entry["uuid"],
            "--query-gpu=power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ]

    def _journaled_query(self, entry):
        record = self.runner.run(
            self._query_argv(entry), timeout=self.args.command_timeout,
            read_only=True,
        )
        draw, limit = _parse_power_query_output(record)
        return record, draw, limit

    def _sample_once(self, entry):
        command = self._query_argv(entry)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, shell=False,
                timeout=max(1.0, self.interval_ms / 1000.0 * 5.0),
                check=False,
            )
            raw = completed.stdout or b""
            record = {
                "returncode": completed.returncode,
                "stdout": raw.decode("utf-8", errors="replace"),
            }
            draw, limit = _parse_power_query_output(record)
        except (OSError, subprocess.SubprocessError):
            draw, limit = None, None
        if draw is None or limit is None:
            entry["sample_failures"] += 1
            return
        offset = time.monotonic() - self.started_monotonic
        entry["samples"].append({
            "offset_seconds": offset,
            "power_draw_watts": draw,
            "observed_limit_watts": limit,
            "query_duration_seconds": time.monotonic() - started,
        })

    def _sample_loop(self):
        interval = self.interval_ms / 1000.0
        while not self.stop_event.is_set():
            loop_started = time.monotonic()
            for entry in self.entries:
                self._sample_once(entry)
            remaining = interval - (time.monotonic() - loop_started)
            if remaining > 0:
                self.stop_event.wait(remaining)

    def prepare(self):
        if self.prepared:
            raise DiagnosticError("power evidence transaction prepared twice")
        self.prepared = True
        if self.requested is None:
            return True
        if self.runner.dry_run:
            return True
        if self.smi is None:
            for entry in self.entries:
                entry["error"] = "nvidia-smi is unavailable"
            return False
        if any(not isinstance(entry.get("uuid"), str) or not entry["uuid"]
               for entry in self.entries):
            for entry in self.entries:
                if not entry.get("uuid"):
                    entry["error"] = "card lacks an exact nvidia-smi UUID"
            return False
        prepared_entries = []
        for entry in self.entries:
            record, _draw, limit = self._journaled_query(entry)
            entry["pre_query_command"] = record
            entry["observed_limit_before_watts"] = limit
            entry["original_limit_watts"] = limit
            if limit is None:
                entry["error"] = "power limit could not be queried before control"
                break
            entry["set_command"] = self.runner.run([
                self.smi, "-i", entry["uuid"],
                "--power-limit=%s" % format(float(self.requested), ".6g"),
            ], timeout=self.args.command_timeout)
            record, _draw, enforced = self._journaled_query(entry)
            entry["enforce_verification_query_command"] = record
            entry["observed_limit_after_enforce_watts"] = enforced
            prepared_entries.append(entry)
            if (entry["set_command"].get("returncode") != 0 or
                    not _power_limit_matches(enforced, self.requested)):
                entry["error"] = "requested power limit was not observed"
                break
        if (len(prepared_entries) != len(self.entries) or
                any(entry.get("error") for entry in self.entries)):
            self._restore_entries(prepared_entries)
            return False
        self.started_monotonic = time.monotonic()
        # Guarantee at least one in-window observation even when a benchmark
        # finishes faster than one periodic interval.
        for entry in self.entries:
            self._sample_once(entry)
        self.thread = threading.Thread(
            target=self._sample_loop,
            name="170hx-xray-power-%s" % self.family,
            daemon=True,
        )
        self.thread.start()
        return True

    def _restore_entries(self, entries):
        if self.runner.dry_run:
            return
        for entry in reversed(entries):
            original = entry.get("original_limit_watts")
            if not _is_finite_number(original, positive=True):
                continue
            entry["restore_attempted"] = True
            entry["restore_command"] = self.runner.run([
                self.smi, "-i", entry["uuid"],
                "--power-limit=%s" % format(float(original), ".6g"),
            ], timeout=self.args.command_timeout)
            record, _draw, restored = self._journaled_query(entry)
            entry["restore_verification_query_command"] = record
            entry["observed_limit_after_restore_watts"] = restored
            entry["restore_succeeded"] = (
                entry["restore_command"].get("returncode") == 0 and
                _power_limit_matches(restored, original)
            )

    def finish(self):
        if self.finished:
            return self.document()
        self.finished = True
        if self.requested is None or self.runner.dry_run:
            return self.document()
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join(timeout=max(2.0, self.interval_ms / 1000.0 * 10.0))
            if self.thread.is_alive():
                for entry in self.entries:
                    entry["error"] = "power sampler thread did not stop"
        for entry in self.entries:
            if entry.get("set_command") is None:
                continue
            record, _draw, post_limit = self._journaled_query(entry)
            entry["post_benchmark_query_command"] = record
            entry["observed_limit_post_benchmark_watts"] = post_limit
            values = [sample["power_draw_watts"] for sample in entry["samples"]]
            entry["sample_count"] = len(values)
            if values:
                entry["power_draw_w_min"] = min(values)
                entry["power_draw_w_max"] = max(values)
                entry["power_draw_w_avg"] = sum(values) / len(values)
                entry["sampling_span_seconds"] = (
                    entry["samples"][-1]["offset_seconds"] -
                    entry["samples"][0]["offset_seconds"]
                )
            entry["limit_enforced_during_benchmark"] = (
                _power_limit_matches(
                    entry.get("observed_limit_after_enforce_watts"),
                    self.requested,
                ) and _power_limit_matches(post_limit, self.requested) and
                all(_power_limit_matches(
                    sample.get("observed_limit_watts"), self.requested
                ) for sample in entry["samples"])
            )
        self._restore_entries([
            entry for entry in self.entries if entry.get("set_command") is not None
        ])
        return self.document()

    def document(self):
        if self.requested is None:
            status = "not-requested"
        elif self.runner.dry_run:
            status = "planned"
        elif any(entry.get("error") for entry in self.entries):
            status = "failed"
        elif all(
                entry.get("limit_enforced_during_benchmark") is True and
                entry.get("restore_succeeded") is True and
                entry.get("sample_count", 0) > 0 and
                entry.get("sample_failures") == 0
                for entry in self.entries):
            status = "pass"
        else:
            status = "failed"
        return {
            "schema": POWER_EVIDENCE_SCHEMA,
            "family": self.family,
            "status": status,
            "requested_limit_watts": self.requested,
            "sampling_interval_ms_requested": self.interval_ms,
            "sampling_provenance": {
                "backend": "nvidia-smi-periodic-query",
                "executable": self.smi,
                "executable_sha256": (
                    sha256_file(self.smi)
                    if self.smi and Path(self.smi).is_file() else None
                ),
                "query_fields": ["power.draw", "power.limit"],
                "query_argv_template": (
                    [self.smi, "-i", "<exact-uuid>",
                     "--query-gpu=power.draw,power.limit",
                     "--format=csv,noheader,nounits"]
                    if self.smi else None
                ),
                "control_commands_in_global_journal": True,
                "periodic_queries_in_global_journal": False,
            },
            "cards": self.entries,
        }


def parse_nvidia_smi_mapping(record):
    """Map BDFs to NVML/nvidia-smi indices.

    The nvidia-smi/NVML index is deliberately *not* called a CUDA index. CUDA
    defaults to ``CUDA_DEVICE_ORDER=FASTEST_FIRST`` and can therefore assign a
    different ordinal to the same BDF. The adjacent CUDA helper is the sole
    authority for CUDA runtime ordinals.
    """
    mapping = {}
    indices = {}
    if record.get("returncode") != 0:
        return mapping
    for line in record.get("stdout", "").splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            index = int(parts[0])
            bdf = normalize_bdf(parts[1])
        except (ValueError, DiagnosticError):
            continue
        if bdf in mapping:
            raise DiagnosticError("nvidia-smi returned duplicate BDF: %s" % bdf)
        if index in indices:
            raise DiagnosticError(
                "nvidia-smi returned duplicate index %d for %s and %s" %
                (index, indices[index], bdf)
            )
        indices[index] = bdf
        mapping[bdf] = {
            "nvidia_smi_index": index,
            "nvidia_smi_name": parts[2],
            "uuid": parts[3],
            "memory_total_mib": int(parts[4]) if parts[4].isdigit() else None,
        }
    return mapping


def inventory_ga100(sysfs_root, runner, requested_bdfs=None,
                    include_glitch_observation=False):
    root = Path(sysfs_root)
    devices_root = root / "bus" / "pci" / "devices"
    smi = find_executable(("nvidia-smi",))
    if smi:
        smi_record = runner.run([
            smi, "--query-gpu=index,pci.bus_id,name,uuid,memory.total",
            "--format=csv,noheader,nounits",
        ], read_only=True)
        cuda_mapping = parse_nvidia_smi_mapping(smi_record)
    else:
        cuda_mapping = {}
    requested = set(normalize_bdf(item) for item in (requested_bdfs or []))
    allowed = set(GA100_DEVICE_IDS)
    if include_glitch_observation:
        allowed.add(GLITCH_OBSERVATION_ID)
    cards = []
    if not devices_root.is_dir():
        raise DiagnosticError("PCI sysfs root is absent: %s" % devices_root)
    for path in sorted(devices_root.iterdir(), key=lambda item: item.name):
        try:
            bdf = normalize_bdf(path.name)
        except DiagnosticError:
            continue
        vendor = read_hex(path / "vendor")
        device = read_hex(path / "device")
        if vendor != 0x10DE or device not in allowed:
            continue
        if requested and bdf not in requested:
            continue
        current_speed = read_text(path / "current_link_speed")
        max_speed = read_text(path / "max_link_speed")
        current_width = read_text(path / "current_link_width")
        max_width = read_text(path / "max_link_width")
        driver = None
        try:
            driver_link = path / "driver"
            if driver_link.exists():
                driver = driver_link.resolve().name
        except OSError:
            pass
        smi_identity = cuda_mapping.get(bdf)
        subsystem_vendor = read_hex(path / "subsystem_vendor")
        subsystem_device = read_hex(path / "subsystem_device")
        try:
            pci_path = str(path.resolve(strict=True))
        except OSError:
            pci_path = str(path.resolve())
        card = {
            "bdf": bdf,
            "pci_path": pci_path,
            "numa_node": read_numa_near(pci_path),
            "vendor": "0x%04x" % vendor,
            "device": "0x%04x" % device,
            "subsystem_vendor": (
                None if subsystem_vendor is None else "0x%04x" % subsystem_vendor
            ),
            "subsystem_device": (
                None if subsystem_device is None else "0x%04x" % subsystem_device
            ),
            "driver": driver,
            "current_link_speed": current_speed,
            "current_link_gen": parse_link_speed(current_speed),
            "current_link_width": (
                int(current_width) if current_width and current_width.isdigit() else None
            ),
            "max_link_speed": max_speed,
            "max_link_gen": parse_link_speed(max_speed),
            "max_link_width": (
                int(max_width) if max_width and max_width.isdigit() else None
            ),
            # Keep the NVML/nvidia-smi and CUDA-runtime index namespaces
            # separate. ``cuda`` is populated only from ga100_diag inventory.
            "nvidia_smi": smi_identity,
            "nvidia_smi_visible": smi_identity is not None,
            "cuda": None,
            "diagnostic_eligible": device in GA100_DEVICE_IDS,
            "observation_only_identity": device == GLITCH_OBSERVATION_ID,
        }
        cards.append(card)
    missing = sorted(requested.difference(item["bdf"] for item in cards))
    if missing:
        raise DiagnosticError(
            "requested BDF is absent or not an allowed GA100 identity: %s"
            % ", ".join(missing)
        )
    return cards


def select_top_pair(cards):
    eligible = [
        item for item in cards
        if item.get("diagnostic_eligible") and isinstance(item.get("cuda"), dict)
    ]
    ranked = sorted(
        eligible,
        key=lambda item: (
            -(item.get("current_link_gen") or -1),
            -(item.get("current_link_width") or -1),
            -(item.get("max_link_gen") or -1),
            -(item.get("max_link_width") or -1),
            item["bdf"],
        ),
    )
    selected = ranked[:2]
    return {
        "selected_bdfs": [item["bdf"] for item in selected],
        "cards": selected,
        "ranking_order": [item["bdf"] for item in ranked],
        "policy": (
            "CUDA-mapped eligible 20c2/20b5 devices sorted by negotiated PCIe generation "
            "descending, negotiated width descending, advertised generation/width "
            "descending, then canonical BDF ascending"
        ),
        "pair_available": len(selected) == 2,
    }


FEATURE_COMMAND_FIELDS = {
    "identity": ("pci.bus_id", "uuid", "name"),
    "ecc": (
        "ecc.mode.current", "ecc.mode.pending",
        "ecc.errors.corrected.volatile.total",
        "ecc.errors.uncorrected.volatile.total",
        "ecc.errors.corrected.aggregate.total",
        "ecc.errors.uncorrected.aggregate.total",
    ),
    "mig_mode": ("mig.mode.current", "mig.mode.pending"),
    "sessions": (
        "encoder.stats.sessionCount", "encoder.stats.averageFps",
        "encoder.stats.averageLatency", "decoder.stats.sessionCount",
        "decoder.stats.averageFps", "decoder.stats.averageLatency",
    ),
}
FEATURE_COMMAND_KEYS = (
    "identity", "full_text", "full_xml", "ecc", "mig_mode", "sessions",
    "mig_gpu_profiles", "mig_compute_profiles", "nvlink",
)


def _ofa_loaded_path(library_name):
    """Resolve the loaded SONAME through procfs without invoking a shell."""
    try:
        lines = Path("/proc/self/maps").read_text(
            encoding="utf-8", errors="strict"
        ).splitlines()
    except (OSError, UnicodeError):
        return None
    basename = Path(str(library_name)).name
    matches = []
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith("/"):
            continue
        path = Path(fields[5])
        if path.name == basename or path.name.startswith("libnvidia-opticalflow.so"):
            try:
                matches.append(str(path.resolve(strict=True)))
            except OSError:
                continue
    return sorted(set(matches))[0] if matches else None


def probe_ofa_api(candidates=None, loader=None):
    """Query only the public OFA maximum-API-version export.

    This creates no CUDA context or OFA session. It therefore proves only that
    a userspace optical-flow API library answered the version query, never that
    a selected GPU exposes or can execute an OFA engine.
    """
    loader = loader or ctypes.CDLL
    if candidates is None:
        discovered = ctypes.util.find_library("nvidia-opticalflow")
        candidates = [discovered, "libnvidia-opticalflow.so.1",
                      "libnvidia-opticalflow.so"]
    ordered = []
    for candidate in candidates:
        if candidate and str(candidate) not in ordered:
            ordered.append(str(candidate))
    load_errors = []
    library = None
    selected = None
    for candidate in ordered:
        try:
            library = loader(candidate)
            selected = candidate
            break
        except (OSError, TypeError, ValueError) as exc:
            load_errors.append({"candidate": candidate, "error": str(exc)})
    base = {
        "schema": OFA_PROBE_SCHEMA,
        "query_only": True,
        "creates_cuda_context": False,
        "creates_ofa_session": False,
        "candidates": ordered,
        "selected_candidate": selected,
        "resolved_library_path": None,
        "resolved_library_sha256": None,
        "load_errors": load_errors,
        "symbol": "NvOFGetMaxSupportedApiVersion",
        "return_code": None,
        "maximum_api_version": None,
        "status": "library-unavailable",
        "interpretation": (
            "host userspace API query only; not bound to a GPU and not "
            "functional OFA evidence"
        ),
    }
    if library is None:
        return base
    try:
        query = getattr(library, "NvOFGetMaxSupportedApiVersion")
    except AttributeError:
        base["status"] = "symbol-unavailable"
        return base
    query.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    query.restype = ctypes.c_int
    version = ctypes.c_uint32(0)
    try:
        return_code = int(query(ctypes.byref(version)))
    except (OSError, TypeError, ValueError, ctypes.ArgumentError) as exc:
        base["status"] = "query-failed"
        base["load_errors"].append({"candidate": selected, "error": str(exc)})
        return base
    resolved = _ofa_loaded_path(selected)
    base.update({
        "resolved_library_path": resolved,
        "resolved_library_sha256": (
            sha256_file(resolved) if resolved and Path(resolved).is_file()
            else None
        ),
        "return_code": return_code,
        "maximum_api_version": int(version.value) if return_code == 0 else None,
        "status": "api-version-observed" if return_code == 0 else "query-rejected",
    })
    return base


def _feature_query_argv(smi, uuid, fields):
    return [
        smi, "-i", uuid, "--query-gpu=%s" % ",".join(fields),
        "--format=csv,noheader,nounits",
    ]


def _feature_csv(record, fields):
    if not isinstance(record, dict) or record.get("returncode") != 0:
        return {"status": "unsupported-or-query-failed", "fields": None}
    rows = [line.strip() for line in record.get("stdout", "").splitlines()
            if line.strip()]
    if len(rows) != 1:
        return {"status": "malformed", "fields": None}
    tokens = [token.strip() for token in rows[0].split(",")]
    if len(tokens) != len(fields):
        return {"status": "malformed", "fields": None}
    return {"status": "observed", "fields": dict(zip(fields, tokens))}


def _feature_observations(commands, ofa_probe):
    ecc = _feature_csv(commands.get("ecc"), FEATURE_COMMAND_FIELDS["ecc"])
    mig = _feature_csv(
        commands.get("mig_mode"), FEATURE_COMMAND_FIELDS["mig_mode"]
    )
    sessions = _feature_csv(
        commands.get("sessions"), FEATURE_COMMAND_FIELDS["sessions"]
    )

    def command_status(key):
        record = commands.get(key)
        return ("observed" if isinstance(record, dict) and
                record.get("returncode") == 0 else
                "unsupported-or-query-failed")

    return {
        "ecc": {
            "status": ecc["status"], "fields": ecc["fields"],
            "command_key": "ecc", "injection_performed": False,
        },
        "mig": {
            "status": mig["status"], "fields": mig["fields"],
            "gpu_instance_profiles_status": command_status("mig_gpu_profiles"),
            "compute_instance_profiles_status": command_status(
                "mig_compute_profiles"
            ),
            "command_keys": [
                "mig_mode", "mig_gpu_profiles", "mig_compute_profiles"
            ],
            "configuration_performed": False,
        },
        "nvlink": {
            "status": command_status("nvlink"),
            "command_key": "nvlink",
        },
        "nvenc_nvdec_sessions": {
            "status": sessions["status"], "fields": sessions["fields"],
            "command_key": "sessions",
            "interpretation": (
                "session counters are inventory, not proof that a new encode "
                "or decode operation succeeds"
            ),
        },
        "nvjpeg": {
            "status": "not-exposed-by-nvidia-smi",
            "reason": "functional NVJPEG evidence requires the external media suite",
        },
        "ofa": {
            "status": "api-query-not-device-bound",
            "global_probe_status": ofa_probe.get("status"),
            "reason": (
                "maximum API version is a host-library query; functional OFA "
                "work remains not-yet-supported"
            ),
        },
    }


def _xml_identity(record):
    if not isinstance(record, dict) or record.get("returncode") != 0:
        return None
    try:
        root = ElementTree.fromstring(record.get("stdout", ""))
    except ElementTree.ParseError:
        return None
    gpus = root.findall(".//gpu")
    if len(gpus) != 1:
        return None
    uuid = gpus[0].findtext("uuid")
    bdf = gpus[0].findtext("pci/pci_bus_id")
    try:
        bdf = normalize_bdf(bdf)
    except DiagnosticError:
        return None
    return {"bdf": bdf, "uuid": uuid}


def collect_feature_inventory(cards, args, runner):
    """Collect query-only MIG/ECC/link/media state for exact selected cards."""
    del args
    smi = find_executable(("nvidia-smi",))
    ofa = probe_ofa_api()
    records = []
    for card in cards:
        identity = card.get("nvidia_smi")
        uuid = identity.get("uuid") if isinstance(identity, dict) else None
        commands = {key: None for key in FEATURE_COMMAND_KEYS}
        if smi and isinstance(uuid, str) and uuid:
            commands.update({
                "identity": runner.run(_feature_query_argv(
                    smi, uuid, FEATURE_COMMAND_FIELDS["identity"]
                ), read_only=True),
                "full_text": runner.run(
                    [smi, "-i", uuid, "-q"], read_only=True
                ),
                "full_xml": runner.run(
                    [smi, "-i", uuid, "-q", "-x"], read_only=True
                ),
                "ecc": runner.run(_feature_query_argv(
                    smi, uuid, FEATURE_COMMAND_FIELDS["ecc"]
                ), read_only=True),
                "mig_mode": runner.run(_feature_query_argv(
                    smi, uuid, FEATURE_COMMAND_FIELDS["mig_mode"]
                ), read_only=True),
                "sessions": runner.run(_feature_query_argv(
                    smi, uuid, FEATURE_COMMAND_FIELDS["sessions"]
                ), read_only=True),
                "mig_gpu_profiles": runner.run(
                    [smi, "mig", "-i", uuid, "-lgip"], read_only=True
                ),
                "mig_compute_profiles": runner.run(
                    [smi, "mig", "-i", uuid, "-lgci"], read_only=True
                ),
                "nvlink": runner.run(
                    [smi, "nvlink", "-i", uuid, "-s"], read_only=True
                ),
            })
        parsed_identity = _feature_csv(
            commands.get("identity"), FEATURE_COMMAND_FIELDS["identity"]
        )
        identity_fields = parsed_identity.get("fields")
        identity_ok = False
        if isinstance(identity_fields, dict):
            try:
                observed_bdf = normalize_bdf(identity_fields["pci.bus_id"])
            except DiagnosticError:
                observed_bdf = None
            identity_ok = (
                observed_bdf == card.get("bdf") and
                identity_fields.get("uuid") == uuid
            )
        xml_identity = _xml_identity(commands.get("full_xml"))
        required_ok = bool(
            smi and identity_ok and
            isinstance(commands.get("full_text"), dict) and
            commands["full_text"].get("returncode") == 0 and
            xml_identity == {"bdf": card.get("bdf"), "uuid": uuid}
        )
        records.append({
            "bdf": card.get("bdf"), "uuid": uuid,
            "status": "completed" if required_ok else (
                "unavailable" if not smi or not uuid else "failed"
            ),
            "identity": identity_fields,
            "xml_identity": xml_identity,
            "commands": commands,
            "features": _feature_observations(commands, ofa),
        })
    status = (
        "unavailable" if not smi else
        "failed" if any(record["status"] == "failed" for record in records)
        else "completed"
    )
    return {
        "schema": FEATURE_CENSUS_SCHEMA,
        "status": status,
        "query_only": True,
        "mutations_requested": False,
        "nvidia_smi": {
            "path": smi,
            "sha256": sha256_file(smi) if smi and Path(smi).is_file() else None,
        },
        "ofa_api_probe": ofa,
        "cards": records,
        "interpretation": {
            "capability_queries_are_not_benchmarks": True,
            "functional_media_requires_external_suite": True,
            "mig_configuration_performed": False,
            "ecc_injection_performed": False,
            "ofa_functional_test": "not-yet-supported",
        },
        "validation_errors": [],
    }


def ensure_cuda_binary(args, runner, owned_work):
    source = Path(__file__).resolve().with_name("ga100_diag.cu")
    if args.cuda_binary:
        binary = Path(args.cuda_binary).resolve()
        if not binary.is_file() or not os.access(str(binary), os.X_OK):
            raise DiagnosticError("--cuda-binary is not executable: %s" % binary)
        return binary, {
            "source": str(source) if source.is_file() else None,
            "source_sha256": sha256_file(source) if source.is_file() else None,
            "binary": str(binary), "binary_sha256": sha256_file(binary),
            "build": "prebuilt",
        }
    if not source.is_file():
        raise DiagnosticError("CUDA diagnostic source is absent: %s" % source)
    nvcc = find_executable((
        args.nvcc, "nvcc", "/usr/local/cuda/bin/nvcc",
        "/usr/local/cuda-13.5/bin/nvcc",
    ))
    if nvcc is None:
        if args.dry_run:
            return None, {
                "source": str(source), "source_sha256": sha256_file(source),
                "binary": None, "binary_sha256": None,
                "build": "planned_but_nvcc_unavailable",
            }
        raise DiagnosticError("nvcc is unavailable; use --cuda-binary")
    binary = Path(owned_work) / "ga100_diag"
    command = [
        nvcc, "-O3", "-std=c++14", "-gencode", "arch=compute_80,code=sm_80",
        "-Xcompiler", "-pthread", str(source), "-lnvidia-ml", "-o", str(binary),
    ]
    build = runner.run(command, timeout=args.command_timeout)
    if not args.dry_run and build.get("returncode") != 0:
        raise DiagnosticError("CUDA diagnostic build failed")
    if not args.dry_run and (
        not binary.is_file() or not os.access(str(binary), os.X_OK)
    ):
        raise DiagnosticError("CUDA build did not create an executable")
    return binary, {
        "source": str(source), "source_sha256": sha256_file(source),
        "binary": str(binary),
        "binary_sha256": (
            sha256_file(binary) if binary.is_file() else None
        ),
        "build": build,
    }


def parse_json_stdout(record):
    if record.get("returncode") != 0:
        return None
    text = record.get("stdout", "").strip()
    if not text:
        return None
    try:
        value = _strict_json_loads(text)
    except (ValueError, DiagnosticError):
        return None
    return value if isinstance(value, dict) else None


def _is_int(value, minimum=None, maximum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _strict_equal(actual, expected):
    """Type-strict JSON equality (bool/int/float aliases are not equal)."""
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int):
        return _is_int(actual) and actual == expected
    if isinstance(expected, float):
        return (isinstance(actual, float) and math.isfinite(actual) and
                actual == expected)
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(actual) == set(expected) and
                all(_strict_equal(actual[key], value)
                    for key, value in expected.items()))
    if isinstance(expected, (list, tuple)):
        return (isinstance(actual, type(expected)) and
                len(actual) == len(expected) and
                all(_strict_equal(left, right)
                    for left, right in zip(actual, expected)))
    return actual == expected


def _missing_keys(record, required, label):
    if not isinstance(record, dict):
        return ["%s is not an object" % label]
    missing = sorted(set(required) - set(record))
    return (["%s is missing required keys: %s" %
             (label, ", ".join(missing))] if missing else [])


def _exact_keys(record, expected, label):
    if not isinstance(record, dict):
        return ["%s is not an object" % label]
    observed = set(record)
    expected = set(expected)
    if observed == expected:
        return []
    return ["%s object keys mismatch (missing=%s unexpected=%s)" % (
        label, ",".join(sorted(expected - observed)) or "none",
        ",".join(sorted(observed - expected)) or "none",
    )]


def validate_ofa_probe(document):
    errors = []
    label = "OFA API probe"
    expected_keys = {
        "schema", "query_only", "creates_cuda_context",
        "creates_ofa_session", "candidates", "selected_candidate",
        "resolved_library_path", "resolved_library_sha256", "load_errors",
        "symbol", "return_code", "maximum_api_version", "status",
        "interpretation",
    }
    errors.extend(_exact_keys(document, expected_keys, label))
    if not isinstance(document, dict):
        return errors
    if (document.get("schema") != OFA_PROBE_SCHEMA or
            document.get("query_only") is not True or
            document.get("creates_cuda_context") is not False or
            document.get("creates_ofa_session") is not False or
            document.get("symbol") != "NvOFGetMaxSupportedApiVersion" or
            document.get("interpretation") != (
                "host userspace API query only; not bound to a GPU and not "
                "functional OFA evidence"
            )):
        errors.append("OFA API probe contract mismatch")
    candidates = document.get("candidates")
    if (not isinstance(candidates, list) or
            any(not isinstance(value, str) or not value for value in candidates) or
            len(candidates) != len(set(candidates))):
        errors.append("OFA API candidates are malformed")
        candidates = []
    selected = document.get("selected_candidate")
    if selected is not None and selected not in candidates:
        errors.append("OFA selected candidate is not in the candidate list")
    load_errors = document.get("load_errors")
    if (not isinstance(load_errors, list) or any(
            not isinstance(item, dict) or set(item) != {"candidate", "error"} or
            not isinstance(item.get("candidate"), str) or
            not isinstance(item.get("error"), str)
            for item in load_errors)):
        errors.append("OFA load/query errors are malformed")
    resolved = document.get("resolved_library_path")
    digest = document.get("resolved_library_sha256")
    if resolved is None:
        if digest is not None:
            errors.append("OFA unresolved library has a fabricated hash")
    elif (not isinstance(resolved, str) or not resolved.startswith("/") or
          not isinstance(digest, str) or
          re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        errors.append("OFA resolved-library provenance is malformed")
    status = document.get("status")
    allowed = {
        "library-unavailable", "symbol-unavailable", "query-failed",
        "query-rejected", "api-version-observed",
    }
    if status not in allowed:
        errors.append("OFA API status is invalid")
    return_code = document.get("return_code")
    version = document.get("maximum_api_version")
    if status == "api-version-observed":
        if (not _is_int(return_code, 0, 0) or
                not _is_int(version, 1, 0xFFFFFFFF) or selected is None):
            errors.append("successful OFA API query lacks exact result")
    elif status == "query-rejected":
        if (not _is_int(return_code) or return_code == 0 or version is not None or
                selected is None):
            errors.append("rejected OFA API query result is malformed")
    elif return_code is not None or version is not None:
        errors.append("unperformed OFA API query fabricates a result")
    if status == "library-unavailable" and selected is not None:
        errors.append("unavailable OFA library has a selected candidate")
    if status != "library-unavailable" and selected is None:
        errors.append("loaded OFA status lacks a selected candidate")
    return errors


def _validate_feature_command(record, expected_argv, label):
    if record is None:
        return ["%s command is absent" % label]
    expected_keys = {
        "argv", "cwd", "started_utc", "dry_run", "environment_overrides",
        "executable", "executable_sha256", "trust_boundary", "status",
        "returncode", "duration_seconds", "stdout", "stderr",
        "stdout_sha256", "stderr_sha256",
    }
    errors = _exact_keys(record, expected_keys, label)
    if not isinstance(record, dict):
        return errors
    if record.get("argv") != expected_argv:
        errors.append("%s argv mismatch" % label)
    if (record.get("cwd") is not None or
            not isinstance(record.get("dry_run"), bool) or
            record.get("environment_overrides") != {} or
            record.get("executable") != expected_argv[0] or
            not isinstance(record.get("executable_sha256"), str) or
            re.fullmatch(r"[0-9a-f]{64}", record["executable_sha256"]) is None or
            not isinstance(record.get("trust_boundary"), str) or
            not record["trust_boundary"]):
        errors.append("%s execution provenance mismatch" % label)
    timestamp = record.get("started_utc")
    try:
        parsed = datetime.datetime.fromisoformat(timestamp)
        timestamp_valid = parsed.tzinfo is not None
    except (TypeError, ValueError):
        timestamp_valid = False
    if not timestamp_valid:
        errors.append("%s timestamp is malformed" % label)
    if (record.get("status") not in ("completed", "timeout") or
            (record.get("returncode") is not None and
             not _is_int(record.get("returncode"))) or
            not _is_finite_number(record.get("duration_seconds"), minimum=0) or
            not isinstance(record.get("stdout"), str) or
            not isinstance(record.get("stderr"), str)):
        errors.append("%s result shape is malformed" % label)
    for stream in ("stdout", "stderr"):
        value = record.get(stream)
        digest = record.get(stream + "_sha256")
        if (not isinstance(value, str) or not isinstance(digest, str) or
                digest != hashlib.sha256(value.encode("utf-8")).hexdigest()):
            errors.append("%s %s hash mismatch" % (label, stream))
    return errors


def validate_feature_inventory(document, cards, command_journal=None):
    errors = []
    expected_root = {
        "schema", "status", "query_only", "mutations_requested",
        "nvidia_smi", "ofa_api_probe", "cards", "interpretation",
        "validation_errors",
    }
    errors.extend(_exact_keys(document, expected_root, "feature census"))
    if not isinstance(document, dict):
        return errors
    if (document.get("schema") != FEATURE_CENSUS_SCHEMA or
            document.get("query_only") is not True or
            document.get("mutations_requested") is not False):
        errors.append("feature census contract mismatch")
    expected_interpretation = {
        "capability_queries_are_not_benchmarks": True,
        "functional_media_requires_external_suite": True,
        "mig_configuration_performed": False,
        "ecc_injection_performed": False,
        "ofa_functional_test": "not-yet-supported",
    }
    if not _strict_equal(document.get("interpretation"), expected_interpretation):
        errors.append("feature census interpretation mismatch")
    if document.get("validation_errors") != []:
        errors.append("feature census contains producer validation errors")
    smi = document.get("nvidia_smi")
    if not isinstance(smi, dict) or set(smi) != {"path", "sha256"}:
        errors.append("feature census nvidia-smi provenance is malformed")
        smi_path = None
    else:
        smi_path = smi.get("path")
        if smi_path is None:
            if smi.get("sha256") is not None:
                errors.append("absent nvidia-smi has a fabricated hash")
        elif (not isinstance(smi_path, str) or not smi_path.startswith("/") or
              not isinstance(smi.get("sha256"), str) or
              re.fullmatch(r"[0-9a-f]{64}", smi["sha256"]) is None):
            errors.append("feature census nvidia-smi provenance is malformed")
    errors.extend(validate_ofa_probe(document.get("ofa_api_probe")))
    expected_cards = {card.get("bdf"): card for card in cards}
    observed_cards = document.get("cards")
    if (not isinstance(observed_cards, list) or
            len(observed_cards) != len(expected_cards)):
        return errors + ["feature census card count mismatch"]
    seen = set()
    for index, record in enumerate(observed_cards):
        label = "feature census card %d" % index
        expected_card_keys = {
            "bdf", "uuid", "status", "identity", "xml_identity",
            "commands", "features",
        }
        errors.extend(_exact_keys(record, expected_card_keys, label))
        if not isinstance(record, dict):
            continue
        bdf = record.get("bdf")
        if bdf not in expected_cards or bdf in seen:
            errors.append("%s BDF is unknown or duplicated" % label)
            continue
        seen.add(bdf)
        expected_card = expected_cards[bdf]
        expected_smi = expected_card.get("nvidia_smi")
        expected_uuid = (
            expected_smi.get("uuid") if isinstance(expected_smi, dict) else None
        )
        if record.get("uuid") != expected_uuid:
            errors.append("%s UUID mismatch" % label)
        commands = record.get("commands")
        if (not isinstance(commands, dict) or
                set(commands) != set(FEATURE_COMMAND_KEYS)):
            errors.append("%s command inventory mismatch" % label)
            continue
        if smi_path is None or not expected_uuid:
            if any(value is not None for value in commands.values()):
                errors.append(
                    "%s fabricates commands without a queryable identity" % label
                )
            expected_status = "unavailable"
        else:
            expected_argv = {
                "identity": _feature_query_argv(
                    smi_path, expected_uuid, FEATURE_COMMAND_FIELDS["identity"]
                ),
                "full_text": [smi_path, "-i", expected_uuid, "-q"],
                "full_xml": [smi_path, "-i", expected_uuid, "-q", "-x"],
                "ecc": _feature_query_argv(
                    smi_path, expected_uuid, FEATURE_COMMAND_FIELDS["ecc"]
                ),
                "mig_mode": _feature_query_argv(
                    smi_path, expected_uuid, FEATURE_COMMAND_FIELDS["mig_mode"]
                ),
                "sessions": _feature_query_argv(
                    smi_path, expected_uuid, FEATURE_COMMAND_FIELDS["sessions"]
                ),
                "mig_gpu_profiles": [
                    smi_path, "mig", "-i", expected_uuid, "-lgip"
                ],
                "mig_compute_profiles": [
                    smi_path, "mig", "-i", expected_uuid, "-lgci"
                ],
                "nvlink": [smi_path, "nvlink", "-i", expected_uuid, "-s"],
            }
            for key in FEATURE_COMMAND_KEYS:
                errors.extend(_validate_feature_command(
                    commands.get(key), expected_argv[key], "%s %s" % (label, key)
                ))
                if isinstance(command_journal, list):
                    matches = sum(
                        _strict_equal(commands.get(key), item)
                        for item in command_journal
                    )
                    if matches != 1:
                        errors.append(
                            "%s %s is not uniquely bound to command journal" %
                            (label, key)
                        )
            identity = _feature_csv(
                commands.get("identity"), FEATURE_COMMAND_FIELDS["identity"]
            ).get("fields")
            if not _strict_equal(record.get("identity"), identity):
                errors.append("%s identity parse mirror mismatch" % label)
            identity_ok = False
            if isinstance(identity, dict):
                try:
                    observed_bdf = normalize_bdf(identity.get("pci.bus_id"))
                except DiagnosticError:
                    observed_bdf = None
                identity_ok = (
                    observed_bdf == bdf and identity.get("uuid") == expected_uuid
                )
            xml_identity = _xml_identity(commands.get("full_xml"))
            if not _strict_equal(record.get("xml_identity"), xml_identity):
                errors.append("%s XML identity mirror mismatch" % label)
            required_ok = bool(
                identity_ok and
                isinstance(commands.get("full_text"), dict) and
                commands["full_text"].get("returncode") == 0 and
                xml_identity == {"bdf": bdf, "uuid": expected_uuid}
            )
            expected_status = "completed" if required_ok else "failed"
        if record.get("status") != expected_status:
            errors.append("%s status mismatch" % label)
        expected_features = _feature_observations(
            commands, document.get("ofa_api_probe")
        )
        if not _strict_equal(record.get("features"), expected_features):
            errors.append("%s feature decode mirror mismatch" % label)
    expected_status = (
        "unavailable" if smi_path is None else
        "failed" if any(isinstance(item, dict) and item.get("status") == "failed"
                        for item in observed_cards) else "completed"
    )
    if document.get("status") != expected_status:
        errors.append("feature census root status mismatch")
    return errors


def _validate_temperature_boolean_types(value, path="temperature"):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = "%s.%s" % (path, key)
            if isinstance(child, bool) and key not in TEMPERATURE_BOOLEAN_FIELDS:
                errors.append("%s boolean aliases a non-boolean field" % child_path)
            errors.extend(_validate_temperature_boolean_types(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_validate_temperature_boolean_types(
                child, "%s[%d]" % (path, index)
            ))
    return errors


def _validate_temperature_declared_keys(value, path="temperature"):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = "%s.%s" % (path, key)
            if key not in TEMPERATURE_DECLARED_SCHEMA_KEYS:
                errors.append("%s is an undeclared schema-v8 key" % child_path)
            errors.extend(_validate_temperature_declared_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_validate_temperature_declared_keys(
                child, "%s[%d]" % (path, index)
            ))
    return errors


def _is_finite_number(value, positive=False, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    if not math.isfinite(numeric):
        return False
    if positive and numeric <= 0:
        return False
    if minimum is not None and numeric < minimum:
        return False
    if maximum is not None and numeric > maximum:
        return False
    return True


def _approximately_equal(actual, expected):
    return abs(float(actual) - float(expected)) <= max(
        1.0e-5, abs(float(expected)) * 2.0e-5
    )


def _power_command_matches(record, expected_argv, require_success=False):
    if not isinstance(record, dict) or record.get("argv") != expected_argv:
        return False
    if require_success:
        return (record.get("status") == "completed" and
                _is_int(record.get("returncode"), 0, 0))
    return (record.get("returncode") is None or
            _is_int(record.get("returncode")))


def validate_power_evidence(document, cards, args, family):
    """Fail closed on benchmark power-control and sampling claims."""
    errors = []
    if not isinstance(document, dict):
        return ["power evidence is not an object"]
    errors.extend(_exact_keys(document, {
        "schema", "family", "status", "requested_limit_watts",
        "sampling_interval_ms_requested", "sampling_provenance", "cards",
    }, "power evidence"))
    requested = getattr(args, "power_limit_watts", None)
    interval = getattr(args, "power_sample_interval_ms", 100)
    if document.get("schema") != POWER_EVIDENCE_SCHEMA:
        errors.append("power evidence schema mismatch")
    if document.get("family") != family:
        errors.append("power evidence family mismatch")
    if not _strict_equal(document.get("requested_limit_watts"), requested):
        errors.append("power evidence requested limit mismatch")
    if not _is_int(document.get("sampling_interval_ms_requested"), interval, interval):
        errors.append("power evidence sampling interval mismatch")
    provenance = document.get("sampling_provenance")
    if not isinstance(provenance, dict):
        errors.append("power evidence provenance is not an object")
    else:
        errors.extend(_exact_keys(provenance, {
            "backend", "executable", "executable_sha256", "query_fields",
            "query_argv_template", "control_commands_in_global_journal",
            "periodic_queries_in_global_journal",
        }, "power evidence provenance"))
        if (provenance.get("backend") != "nvidia-smi-periodic-query" or
                provenance.get("query_fields") != ["power.draw", "power.limit"] or
                provenance.get("control_commands_in_global_journal") is not True or
                provenance.get("periodic_queries_in_global_journal") is not False):
            errors.append("power evidence provenance contract mismatch")
        executable = provenance.get("executable")
        digest = provenance.get("executable_sha256")
        if executable is None:
            if digest is not None or provenance.get("query_argv_template") is not None:
                errors.append("absent power sampler has fabricated provenance")
        elif (not isinstance(executable, str) or not executable or
              not isinstance(digest, str) or
              re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            errors.append("power sampler executable provenance is malformed")
    entries = document.get("cards")
    if not isinstance(entries, list) or len(entries) != len(cards):
        return errors + ["power evidence card count mismatch"]
    expected = {card.get("bdf"): card for card in cards}
    seen = set()
    for position, entry in enumerate(entries):
        label = "power evidence card %d" % position
        if not isinstance(entry, dict):
            errors.append("%s is not an object" % label)
            continue
        errors.extend(_exact_keys(entry, {
            "bdf", "uuid", "nvidia_smi_index", "original_limit_watts",
            "observed_limit_before_watts", "observed_limit_after_enforce_watts",
            "observed_limit_post_benchmark_watts",
            "observed_limit_after_restore_watts",
            "limit_enforced_during_benchmark", "restore_attempted",
            "restore_succeeded", "sample_count", "sample_failures",
            "power_draw_w_min", "power_draw_w_max", "power_draw_w_avg",
            "power_draw_average_method",
            "sampling_span_seconds", "samples", "pre_query_command",
            "set_command", "enforce_verification_query_command",
            "post_benchmark_query_command", "restore_command",
            "restore_verification_query_command", "error",
        }, label))
        bdf = entry.get("bdf")
        if bdf not in expected or bdf in seen:
            errors.append("%s identity is unknown or duplicated" % label)
            continue
        seen.add(bdf)
        smi = expected[bdf].get("nvidia_smi")
        expected_uuid = smi.get("uuid") if isinstance(smi, dict) else None
        expected_index = smi.get("nvidia_smi_index") if isinstance(smi, dict) else None
        if (entry.get("uuid") != expected_uuid or
                not _strict_equal(entry.get("nvidia_smi_index"), expected_index)):
            errors.append("%s nvidia-smi identity mismatch" % label)
        if not all(isinstance(entry.get(key), bool) for key in (
                "limit_enforced_during_benchmark", "restore_attempted",
                "restore_succeeded")):
            errors.append("%s categorical fields are malformed" % label)
        if entry.get("power_draw_average_method") != POWER_AVERAGE_METHOD:
            errors.append("%s power average method is malformed" % label)
        executable = (
            provenance.get("executable") if isinstance(provenance, dict)
            else None
        )
        query_argv = (
            [executable, "-i", entry.get("uuid"),
             "--query-gpu=power.draw,power.limit",
             "--format=csv,noheader,nounits"]
            if isinstance(executable, str) and executable else None
        )
        if query_argv is not None:
            for command_key in (
                    "pre_query_command", "enforce_verification_query_command",
                    "post_benchmark_query_command",
                    "restore_verification_query_command"):
                command = entry.get(command_key)
                if (command is not None and
                        not _power_command_matches(command, query_argv)):
                    errors.append("%s %s provenance mismatch" %
                                  (label, command_key))
            set_argv = [
                executable, "-i", entry.get("uuid"),
                "--power-limit=%s" % format(float(requested), ".6g"),
            ] if _is_finite_number(requested, positive=True) else None
            original = entry.get("original_limit_watts")
            restore_argv = [
                executable, "-i", entry.get("uuid"),
                "--power-limit=%s" % format(float(original), ".6g"),
            ] if _is_finite_number(original, positive=True) else None
            if (entry.get("set_command") is not None and
                    (set_argv is None or not _power_command_matches(
                        entry["set_command"], set_argv))):
                errors.append("%s set-command provenance mismatch" % label)
            if (entry.get("restore_command") is not None and
                    (restore_argv is None or not _power_command_matches(
                        entry["restore_command"], restore_argv))):
                errors.append("%s restore-command provenance mismatch" % label)
        count = entry.get("sample_count")
        failures = entry.get("sample_failures")
        samples = entry.get("samples")
        if (not _is_int(count, 0) or not _is_int(failures, 0) or
                not isinstance(samples, list) or
                (_is_int(count, 0) and count != len(samples))):
            errors.append("%s sample counters are malformed" % label)
            continue
        draws = []
        prior_offset = None
        for sample_index, sample in enumerate(samples):
            sample_label = "%s sample %d" % (label, sample_index)
            if not isinstance(sample, dict):
                errors.append("%s is not an object" % sample_label)
                continue
            errors.extend(_exact_keys(sample, {
                "offset_seconds", "power_draw_watts", "observed_limit_watts",
                "query_duration_seconds",
            }, sample_label))
            offset = sample.get("offset_seconds")
            draw = sample.get("power_draw_watts")
            if (not _is_finite_number(offset, minimum=0) or
                    (prior_offset is not None and offset < prior_offset)):
                errors.append("%s offset is invalid" % sample_label)
            else:
                prior_offset = offset
            if not _is_finite_number(draw, minimum=0):
                errors.append("%s power draw is invalid" % sample_label)
            else:
                draws.append(float(draw))
            if not _is_finite_number(sample.get("observed_limit_watts"), positive=True):
                errors.append("%s observed limit is invalid" % sample_label)
            if not _is_finite_number(sample.get("query_duration_seconds"), minimum=0):
                errors.append("%s query duration is invalid" % sample_label)
        expected_min = min(draws) if draws else None
        expected_max = max(draws) if draws else None
        expected_avg = sum(draws) / len(draws) if draws else None
        if (not _strict_equal(entry.get("power_draw_w_min"), expected_min) or
                not _strict_equal(entry.get("power_draw_w_max"), expected_max) or
                not _strict_equal(entry.get("power_draw_w_avg"), expected_avg)):
            errors.append("%s power aggregates do not match samples" % label)
        expected_span = (
            float(samples[-1]["offset_seconds"]) -
            float(samples[0]["offset_seconds"])
            if len(samples) >= 2 and all(isinstance(item, dict) and
                                         _is_finite_number(item.get("offset_seconds"))
                                         for item in samples) else 0.0
        )
        if (not _is_finite_number(entry.get("sampling_span_seconds"), minimum=0) or
                not _approximately_equal(entry.get("sampling_span_seconds"),
                                         expected_span)):
            errors.append("%s sampling span mismatch" % label)
    status = document.get("status")
    if status not in ("pass", "failed", "planned", "not-requested"):
        errors.append("power evidence status is invalid")
    if requested is None and status != "not-requested":
        errors.append("unrequested power evidence status mismatch")
    if requested is not None and getattr(args, "dry_run", False) and status != "planned":
        errors.append("dry-run power evidence status mismatch")
    if status == "pass":
        for entry in entries:
            if (not isinstance(entry, dict) or
                    entry.get("limit_enforced_during_benchmark") is not True or
                    entry.get("restore_attempted") is not True or
                    entry.get("restore_succeeded") is not True or
                    not _is_int(entry.get("sample_count"), 1) or
                    entry.get("sample_failures") != 0 or
                    entry.get("error") is not None):
                errors.append("passing power evidence lacks complete proof")
                break
            executable = provenance.get("executable")
            query_argv = [
                executable, "-i", entry.get("uuid"),
                "--query-gpu=power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ]
            queries = {
                "pre_query_command": entry.get("observed_limit_before_watts"),
                "enforce_verification_query_command":
                    entry.get("observed_limit_after_enforce_watts"),
                "post_benchmark_query_command":
                    entry.get("observed_limit_post_benchmark_watts"),
                "restore_verification_query_command":
                    entry.get("observed_limit_after_restore_watts"),
            }
            for command_key, observed_limit in queries.items():
                command = entry.get(command_key)
                _draw, parsed_limit = _parse_power_query_output(command)
                if (not _power_command_matches(
                        command, query_argv, require_success=True) or
                        not _strict_equal(parsed_limit, observed_limit)):
                    errors.append(
                        "passing power evidence query proof mismatch: %s" %
                        command_key
                    )
            set_argv = [
                executable, "-i", entry.get("uuid"),
                "--power-limit=%s" % format(float(requested), ".6g"),
            ]
            restore_argv = [
                executable, "-i", entry.get("uuid"),
                "--power-limit=%s" % format(
                    float(entry.get("original_limit_watts")), ".6g"
                ),
            ]
            if (not _power_command_matches(
                    entry.get("set_command"), set_argv, require_success=True) or
                    not _power_command_matches(
                        entry.get("restore_command"), restore_argv,
                        require_success=True)):
                errors.append("passing power evidence control command mismatch")
            if not _strict_equal(
                    entry.get("original_limit_watts"),
                    entry.get("observed_limit_before_watts")):
                errors.append("power evidence original-limit proof mismatch")
            for key in (
                    "observed_limit_after_enforce_watts",
                    "observed_limit_post_benchmark_watts"):
                if not _power_limit_matches(entry.get(key), requested):
                    errors.append("passing power evidence limit mismatch")
            if not _power_limit_matches(
                    entry.get("observed_limit_after_restore_watts"),
                    entry.get("original_limit_watts")):
                errors.append("power evidence restoration mismatch")
    return errors


def _benchmark_power_scope_sources(report):
    """Yield benchmark scopes only; capability/NVML probes are excluded."""
    bandwidth = report.get("bandwidth")
    if isinstance(bandwidth, list):
        for index, item in enumerate(bandwidth):
            if not isinstance(item, dict):
                continue
            if "power_evidence" in item or "command" in item:
                yield (
                    "bandwidth", "bandwidth[%d]" % index, item,
                    "command" in item,
                    [item.get("bdf")] if item.get("bdf") else [],
                )
    p2p = report.get("p2p")
    if isinstance(p2p, dict) and (
            "power_evidence" in p2p or "command" in p2p):
        yield "p2p", "p2p", p2p, "command" in p2p, []
    gds = report.get("gds")
    if isinstance(gds, dict) and (
            "power_evidence" in gds or
            isinstance(gds.get("results"), list) and bool(gds["results"])):
        yield (
            "gds", "gds", gds,
            isinstance(gds.get("results"), list) and bool(gds["results"]), []
        )
    media = report.get("media")
    if isinstance(media, dict) and isinstance(media.get("results"), list):
        for index, item in enumerate(media["results"]):
            if not isinstance(item, dict):
                continue
            executed = "probes" in item or "nvenc" in item
            if "power_evidence" in item or executed:
                yield (
                    "media", "media.results[%d]" % index, item, executed,
                    [item.get("bdf")] if item.get("bdf") else [],
                )


def build_benchmark_power_summary(report, cards, args):
    """Build one closed, cross-family power-evidence coverage object."""
    requested_families = [
        family for family in ("bandwidth", "p2p", "gds", "media")
        if getattr(args, "mode", None) in (family, "full")
    ]
    inventory = {card.get("bdf"): card for card in cards}
    family_positions = {family: 0 for family in requested_families}
    scopes = []
    all_errors = []
    for family, component_path, component, executed, fallback_bdfs in (
            _benchmark_power_scope_sources(report)):
        evidence = component.get("power_evidence")
        evidence_cards = evidence.get("cards") if isinstance(evidence, dict) else None
        bdfs = (
            [entry.get("bdf") for entry in evidence_cards
             if isinstance(entry, dict)]
            if isinstance(evidence_cards, list) else list(fallback_bdfs)
        )
        bdfs = [bdf for bdf in bdfs if isinstance(bdf, str)]
        target_cards = [inventory[bdf] for bdf in bdfs if bdf in inventory]
        validation_errors = []
        if evidence is None:
            validation_errors.append(
                "executed or attempted benchmark scope lacks power evidence"
            )
        elif len(target_cards) != len(bdfs) or len(set(bdfs)) != len(bdfs):
            validation_errors.append(
                "power evidence targets are unknown or duplicated"
            )
        else:
            validation_errors.extend(validate_power_evidence(
                evidence, target_cards, args, family
            ))
        command_journal = report.get("commands")
        if isinstance(evidence_cards, list) and "commands" in report:
            if not isinstance(command_journal, list):
                validation_errors.append(
                    "global command journal is unavailable for power proof"
                )
            else:
                for card_entry in evidence_cards:
                    if not isinstance(card_entry, dict):
                        continue
                    for command_key in (
                            "pre_query_command", "set_command",
                            "enforce_verification_query_command",
                            "post_benchmark_query_command", "restore_command",
                            "restore_verification_query_command"):
                        command = card_entry.get(command_key)
                        if (command is not None and not any(
                                _strict_equal(command, journal_entry)
                                for journal_entry in command_journal)):
                            validation_errors.append(
                                "%s is not bound to the global command journal" %
                                command_key
                            )
        expected_status = (
            "not-requested"
            if getattr(args, "power_limit_watts", None) is None else
            "planned" if getattr(args, "dry_run", False) else "pass"
        )
        if (isinstance(evidence, dict) and
                evidence.get("status") != expected_status):
            validation_errors.append(
                "benchmark scope lacks the required power evidence status"
            )
        if component.get("power_evidence_validation_errors") != validation_errors:
            # Component-local validators are produced before this summary.  An
            # omitted/mismatched list would hide a failure in the nested view.
            validation_errors.append(
                "component-local power validation mirror mismatch"
            )
        scope_index = family_positions.setdefault(family, 0)
        family_positions[family] = scope_index + 1
        scope = {
            "family": family,
            "scope_index": scope_index,
            "component_path": component_path,
            "component_status": component.get("status"),
            "benchmark_executed": bool(executed),
            "target_bdfs": bdfs,
            "evidence": evidence,
            "validation_errors": validation_errors,
        }
        scopes.append(scope)
        all_errors.extend(
            "%s: %s" % (component_path, error)
            for error in validation_errors
        )
    executed_families = sorted({
        scope["family"] for scope in scopes if scope["benchmark_executed"]
    })
    executed_targets = {
        family: sorted({
            bdf for scope in scopes
            if scope["family"] == family and scope["benchmark_executed"]
            for bdf in scope["target_bdfs"]
        })
        for family in executed_families
    }
    if all_errors:
        status = "failed"
    elif getattr(args, "dry_run", False) and scopes:
        status = "planned"
    else:
        status = "pass"
    return {
        "schema": POWER_SUMMARY_SCHEMA,
        "status": status,
        "requested_limit_watts": getattr(args, "power_limit_watts", None),
        "sampling_interval_ms_requested": getattr(
            args, "power_sample_interval_ms", 100
        ),
        "requested_families": requested_families,
        "executed_families": executed_families,
        "executed_target_bdfs": executed_targets,
        "scope_count": len(scopes),
        "scopes": scopes,
        "validation_errors": all_errors,
        "scope_policy": (
            "only benchmark command scopes are covered; CUDA/NVML inventory, "
            "nvidia-smi census, gdscheck, and other capability probes are not "
            "mislabelled as benchmarks"
        ),
    }


def validate_benchmark_power_summary(document, report, cards, args):
    expected = build_benchmark_power_summary(report, cards, args)
    return [] if _strict_equal(document, expected) else [
        "top-level benchmark power evidence summary mismatch"
    ]


def validate_device_record(record, expected_bdf=None, expected_index=None):
    errors = []
    if not isinstance(record, dict):
        return ["device record is not an object"]
    index = record.get("cuda_index")
    if not _is_int(index, minimum=0):
        errors.append("cuda_index is not a non-negative integer")
    elif expected_index is not None and index != expected_index:
        errors.append("cuda_index does not match the requested ordinal")
    try:
        bdf = normalize_bdf(record.get("pci_bdf"))
    except DiagnosticError:
        bdf = None
        errors.append("pci_bdf is invalid")
    if expected_bdf is not None and bdf != expected_bdf:
        errors.append("pci_bdf does not match the requested device")
    if not isinstance(record.get("name"), str) or not record.get("name"):
        errors.append("name is missing")
    capability = record.get("compute_capability")
    if not isinstance(capability, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", capability):
        errors.append("compute_capability is malformed")
    for key in ("sm_count", "vram_bytes", "memory_bus_width_bits",
                "memory_clock_khz", "l2_bytes"):
        if not _is_int(record.get(key), minimum=1):
            errors.append("%s is not a positive integer" % key)
    return errors


def validate_cuda_inventory_document(document):
    errors = []
    if not isinstance(document, dict):
        return ["inventory document is not an object"]
    if document.get("schema") != "170hx-xray-cuda-inventory-v1":
        errors.append("inventory schema mismatch")
    devices = document.get("devices")
    if not isinstance(devices, list):
        return errors + ["devices is not an array"]
    seen_bdfs = set()
    seen_indices = set()
    for position, item in enumerate(devices):
        if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
            errors.append("devices[%d] lacks a record object" % position)
            continue
        record = item["record"]
        record_errors = validate_device_record(record)
        errors.extend("devices[%d]: %s" % (position, error)
                      for error in record_errors)
        try:
            bdf = normalize_bdf(record.get("pci_bdf"))
        except DiagnosticError:
            bdf = None
        index = record.get("cuda_index")
        if bdf is not None:
            if bdf in seen_bdfs:
                errors.append("duplicate CUDA BDF: %s" % bdf)
            seen_bdfs.add(bdf)
        if _is_int(index, minimum=0):
            if index in seen_indices:
                errors.append("duplicate CUDA ordinal: %d" % index)
            seen_indices.add(index)
    return errors


def validate_telemetry_document(telemetry, expected_scope):
    errors = []
    if not isinstance(telemetry, dict):
        return ["telemetry is not an object"]
    expected_semantics = {
        "per-dtype-bracketing-window": (
            "last-relative-sample-offset-within-bracketing-window"
        ),
        "aggregate-of-per-dtype-bracketing-windows": (
            "synthetic-sum-of-disjoint-window-sample-spans"
        ),
    }
    if telemetry.get("scope") != expected_scope:
        errors.append("telemetry.scope mismatch")
    if telemetry.get("temperature_sensor") != "nvml-temperature-gpu":
        errors.append("telemetry temperature sensor mismatch")
    if telemetry.get("sampling_span_semantics") != expected_semantics.get(
            expected_scope):
        errors.append("telemetry sampling-span semantics mismatch")
    if not isinstance(telemetry.get("nvml_available"), bool):
        errors.append("telemetry.nvml_available is not boolean")
    if not isinstance(telemetry.get("status"), str) or not telemetry["status"]:
        errors.append("telemetry.status is missing")
    sample_count = telemetry.get("sample_count")
    if not _is_int(sample_count, minimum=0):
        errors.append("telemetry.sample_count is invalid")
        sample_count = None
    elif telemetry.get("nvml_available") is True and sample_count < 1:
        errors.append("available NVML telemetry has no samples")
    elif telemetry.get("nvml_available") is False and sample_count != 0:
        errors.append("unavailable NVML telemetry has samples")
    sampling_span = telemetry.get("sampling_span_seconds")
    if not _is_finite_number(sampling_span, minimum=0):
        errors.append("telemetry sampling span is invalid")
    elif sample_count == 0 and sampling_span != 0:
        errors.append("empty telemetry has a nonzero sampling span")
    valid = telemetry.get("valid_sample_counts")
    failures = telemetry.get("query_failure_counts")
    count_keys = ("power", "sm_clock", "memory_clock", "temperature", "utilization")
    if (not isinstance(valid, dict) or not isinstance(failures, dict) or
            set(valid) != set(count_keys) or set(failures) != set(count_keys)):
        errors.append("telemetry sample-count objects are missing")
    elif sample_count is not None:
        for key in count_keys:
            good = valid.get(key)
            bad = failures.get(key)
            if not _is_int(good, minimum=0, maximum=sample_count):
                errors.append("telemetry valid count is invalid: %s" % key)
            if not _is_int(bad, minimum=0, maximum=sample_count):
                errors.append("telemetry failure count is invalid: %s" % key)
            if _is_int(good, minimum=0) and _is_int(bad, minimum=0) and good + bad != sample_count:
                errors.append("telemetry counts do not sum to sample_count: %s" % key)
    metric_bounds = {
        "power_w_avg": (0, None), "power_w_max": (0, None),
        "sm_mhz_avg": (0, None), "sm_mhz_max": (0, None),
        "memory_mhz_avg": (0, None), "memory_mhz_max": (0, None),
        "temperature_c_avg": (-273.15, 1000),
        "temperature_c_max": (-273.15, 1000),
        "gpu_util_percent_avg": (0, 100),
        "gpu_util_percent_max": (0, 100),
        "memory_util_percent_avg": (0, 100),
        "memory_util_percent_max": (0, 100),
    }
    for key, (minimum, maximum) in metric_bounds.items():
        value = telemetry.get(key)
        if value is not None and not _is_finite_number(
                value, minimum=minimum, maximum=maximum):
            errors.append("telemetry metric is invalid: %s" % key)
    if isinstance(valid, dict):
        count_to_metrics = {
            "power": ("power_w_avg", "power_w_max"),
            "sm_clock": ("sm_mhz_avg", "sm_mhz_max"),
            "memory_clock": ("memory_mhz_avg", "memory_mhz_max"),
            "temperature": ("temperature_c_avg", "temperature_c_max"),
            "utilization": (
                "gpu_util_percent_avg", "gpu_util_percent_max",
                "memory_util_percent_avg", "memory_util_percent_max",
            ),
        }
        for count_key, metric_keys in count_to_metrics.items():
            count = valid.get(count_key)
            if not _is_int(count, minimum=0):
                continue
            for metric_key in metric_keys:
                metric = telemetry.get(metric_key)
                if count == 0 and metric is not None:
                    errors.append(
                        "telemetry metric must be null without samples: %s" % metric_key
                    )
                if count > 0 and metric is None:
                    errors.append(
                        "telemetry metric is missing despite samples: %s" % metric_key
                    )
    for average_key, maximum_key in (
            ("power_w_avg", "power_w_max"),
            ("sm_mhz_avg", "sm_mhz_max"),
            ("memory_mhz_avg", "memory_mhz_max"),
            ("temperature_c_avg", "temperature_c_max"),
            ("gpu_util_percent_avg", "gpu_util_percent_max"),
            ("memory_util_percent_avg", "memory_util_percent_max")):
        average = telemetry.get(average_key)
        maximum = telemetry.get(maximum_key)
        if (_is_finite_number(average) and
                _is_finite_number(maximum) and maximum < average):
            errors.append("telemetry maximum is below average: %s" % maximum_key)
    return errors


def validate_bandwidth_document(document, card, args):
    errors = []
    if not isinstance(document, dict):
        return ["bandwidth document is not an object"]
    if document.get("schema") != "170hx-xray-cuda-bandwidth-v3":
        errors.append("bandwidth schema mismatch")
    if document.get("operation") != "global-memory-copy-kernel":
        errors.append("bandwidth operation mismatch")
    if document.get("measurement_scope") != (
            "typed-load-store-memory-traffic-no-arithmetic"):
        errors.append("bandwidth measurement scope mismatch")
    cuda = card.get("cuda") or {}
    errors.extend(validate_device_record(
        document.get("device"), expected_bdf=card["bdf"],
        expected_index=cuda.get("cuda_index"),
    ))
    results = document.get("results")
    expected_types = BANDWIDTH_TYPE_SPECS
    per_dtype_telemetry = []
    if not isinstance(results, list):
        errors.append("bandwidth results is not an array")
    else:
        seen = set()
        for position, result in enumerate(results):
            if not isinstance(result, dict):
                errors.append("bandwidth result %d is not an object" % position)
                continue
            dtype = result.get("dtype")
            type_spec = expected_types.get(dtype)
            if type_spec is None or dtype in seen:
                errors.append("bandwidth dtype is unknown or duplicated")
                continue
            seen.add(dtype)
            (logical_bits, storage_bytes, logical_per_storage,
             signedness, packing) = type_spec
            expected_bytes = (
                args.bandwidth_bytes - args.bandwidth_bytes % storage_bytes
            )
            expected_logical_elements = (
                expected_bytes // storage_bytes * logical_per_storage
            )
            if result.get("logical_element_bits") != logical_bits:
                errors.append("bandwidth logical width mismatch: %s" % dtype)
            if result.get("storage_unit_bytes") != storage_bytes:
                errors.append("bandwidth storage width mismatch: %s" % dtype)
            if (result.get("logical_elements_per_storage_unit") !=
                    logical_per_storage):
                errors.append("bandwidth packing ratio mismatch: %s" % dtype)
            if result.get("logical_elements") != expected_logical_elements:
                errors.append("bandwidth logical element count mismatch: %s" % dtype)
            if result.get("signedness") != signedness:
                errors.append("bandwidth signedness mismatch: %s" % dtype)
            if result.get("packing") != packing:
                errors.append("bandwidth packing description mismatch: %s" % dtype)
            if result.get("bytes") != expected_bytes:
                errors.append("bandwidth byte count mismatch: %s" % dtype)
            if result.get("iterations") != args.bandwidth_iterations:
                errors.append("bandwidth iteration count mismatch: %s" % dtype)
            if result.get("traffic_definition") != "one read plus one write":
                errors.append("bandwidth traffic definition mismatch: %s" % dtype)
            if result.get("rate_unit") != "decimal-GB/s":
                errors.append("bandwidth rate unit mismatch: %s" % dtype)
            if result.get("rate_scope") != "physical-storage-bytes":
                errors.append("bandwidth rate scope mismatch: %s" % dtype)
            milliseconds = result.get("milliseconds")
            gbps = result.get("gbps")
            if not _is_finite_number(milliseconds, positive=True):
                errors.append("bandwidth timing is invalid: %s" % dtype)
            if not _is_finite_number(gbps, positive=True):
                errors.append("bandwidth rate is invalid: %s" % dtype)
            if (_is_finite_number(milliseconds, positive=True) and
                    _is_finite_number(gbps, positive=True)):
                expected_gbps = (
                    2.0 * expected_bytes * args.bandwidth_iterations /
                    (float(milliseconds) * 1.0e6)
                )
                if not _approximately_equal(gbps, expected_gbps):
                    errors.append("bandwidth rate/timing mismatch: %s" % dtype)
            if result.get("verified") is not True:
                errors.append("bandwidth full-buffer verification failed: %s" % dtype)
            if result.get("verification_scope") != "full-buffer-host-scan":
                errors.append("bandwidth verification scope mismatch: %s" % dtype)
            telemetry = result.get("telemetry")
            errors.extend(validate_telemetry_document(
                telemetry, "per-dtype-bracketing-window"
            ))
            if isinstance(telemetry, dict):
                per_dtype_telemetry.append(telemetry)
        if seen != set(expected_types):
            errors.append("bandwidth dtype set is incomplete")
    root_telemetry = document.get("telemetry")
    errors.extend(validate_telemetry_document(
        root_telemetry, "aggregate-of-per-dtype-bracketing-windows"
    ))
    if (isinstance(root_telemetry, dict) and
            len(per_dtype_telemetry) == len(expected_types)):
        sample_counts = [
            item.get("sample_count") for item in per_dtype_telemetry
        ]
        if (all(_is_int(value, minimum=0) for value in sample_counts) and
                root_telemetry.get("sample_count") != sum(sample_counts)):
            errors.append("aggregate telemetry sample count mismatch")
        spans = [
            item.get("sampling_span_seconds") for item in per_dtype_telemetry
        ]
        if (all(_is_finite_number(value, minimum=0) for value in spans) and
                _is_finite_number(root_telemetry.get("sampling_span_seconds"),
                                  minimum=0) and
                not _approximately_equal(
                    root_telemetry["sampling_span_seconds"], sum(spans))):
            errors.append("aggregate telemetry sampling span mismatch")
        if any(item.get("nvml_available") is not
               root_telemetry.get("nvml_available")
               for item in per_dtype_telemetry):
            errors.append("aggregate telemetry NVML availability mismatch")
        if any(item.get("status") != root_telemetry.get("status")
               for item in per_dtype_telemetry):
            errors.append("aggregate telemetry status mismatch")
        for count_object in ("valid_sample_counts", "query_failure_counts"):
            root_counts = root_telemetry.get(count_object)
            for key in ("power", "sm_clock", "memory_clock",
                        "temperature", "utilization"):
                component_counts = [
                    item.get(count_object, {}).get(key)
                    if isinstance(item.get(count_object), dict) else None
                    for item in per_dtype_telemetry
                ]
                if (isinstance(root_counts, dict) and
                        all(_is_int(value, minimum=0)
                            for value in component_counts) and
                        root_counts.get(key) != sum(component_counts)):
                    errors.append("aggregate telemetry %s mismatch: %s" %
                                  (count_object, key))
        for count_key, average_key, maximum_key in (
                ("power", "power_w_avg", "power_w_max"),
                ("sm_clock", "sm_mhz_avg", "sm_mhz_max"),
                ("memory_clock", "memory_mhz_avg", "memory_mhz_max"),
                ("temperature", "temperature_c_avg", "temperature_c_max"),
                ("utilization", "gpu_util_percent_avg",
                 "gpu_util_percent_max"),
                ("utilization", "memory_util_percent_avg",
                 "memory_util_percent_max")):
            metric_counts = [
                item.get("valid_sample_counts", {}).get(count_key)
                if isinstance(item.get("valid_sample_counts"), dict) else None
                for item in per_dtype_telemetry
            ]
            averages = [item.get(average_key) for item in per_dtype_telemetry]
            maxima = [item.get(maximum_key) for item in per_dtype_telemetry]
            total = (sum(metric_counts)
                     if all(_is_int(value, minimum=0)
                            for value in metric_counts) else None)
            if (total and all(_is_finite_number(value)
                              for value in averages + maxima)):
                expected_average = sum(
                    value * count for value, count in zip(averages, metric_counts)
                ) / total
                if (not _is_finite_number(root_telemetry.get(average_key)) or
                        not _is_finite_number(
                            root_telemetry.get(maximum_key)) or
                        not _approximately_equal(
                            root_telemetry.get(average_key), expected_average) or
                        not _approximately_equal(
                            root_telemetry.get(maximum_key), max(maxima))):
                    errors.append("aggregate telemetry metric mismatch: %s" %
                                  average_key)
    return errors


def _expected_power_two_sizes(minimum, maximum):
    result = []
    value = minimum
    while value <= maximum:
        result.append(value)
        if value > maximum // 2:
            break
        value <<= 1
    return result


def _validate_copy_metrics(metrics, direct, label):
    errors = []
    if not isinstance(metrics, dict):
        return ["%s is not an object" % label]
    if direct and not isinstance(metrics.get("available"), bool):
        errors.append("%s.available is not boolean" % label)
        return errors
    available = metrics.get("available", True)
    if metrics.get("rate_unit") != "decimal-GB/s":
        errors.append("%s rate unit mismatch" % label)
    if not available:
        if any(metrics.get(key) is not None for key in
               ("latency_ms", "gbps", "full_buffer_verified")):
            errors.append("%s unavailable metrics must be null" % label)
        return errors
    if not _is_finite_number(metrics.get("latency_ms"), positive=True):
        errors.append("%s latency is invalid" % label)
    if not _is_finite_number(metrics.get("gbps"), positive=True):
        errors.append("%s rate is invalid" % label)
    if metrics.get("full_buffer_verified") is not True:
        errors.append("%s full-buffer verification failed" % label)
    return errors


def validate_p2p_document(document, first, second, args):
    errors = []
    if not isinstance(document, dict):
        return ["P2P document is not an object"]
    if document.get("schema") != "170hx-xray-p2p-v2":
        errors.append("P2P schema mismatch")
    errors.extend(validate_device_record(
        document.get("source"), first["bdf"], first["cuda"]["cuda_index"]))
    errors.extend(validate_device_record(
        document.get("destination"), second["bdf"], second["cuda"]["cuda_index"]))
    capability = document.get("peer_access_capability")
    if not isinstance(capability, dict) or not all(
            isinstance(capability.get(key), bool) for key in
            ("source_can_access_destination", "destination_can_access_source")):
        errors.append("P2P peer-access capability is malformed")
    expected_directions = (
        (first, second, "destination_can_access_source"),
        (second, first, "source_can_access_destination"),
    )
    directions = document.get("directions")
    expected_sizes = _expected_power_two_sizes(
        args.p2p_min_bytes, args.p2p_max_bytes)
    if not isinstance(directions, list) or len(directions) != 2:
        return errors + ["P2P directions must contain exactly two entries"]
    for position, (direction, expected) in enumerate(zip(directions, expected_directions)):
        source, destination, capability_key = expected
        label = "P2P direction %d" % position
        if not isinstance(direction, dict):
            errors.append("%s is not an object" % label)
            continue
        for endpoint_key, card in (("from", source), ("to", destination)):
            endpoint = direction.get(endpoint_key)
            if not isinstance(endpoint, dict):
                errors.append("%s.%s is malformed" % (label, endpoint_key))
                continue
            try:
                endpoint_bdf = normalize_bdf(endpoint.get("pci_bdf"))
            except DiagnosticError:
                endpoint_bdf = None
            if (endpoint_bdf != card["bdf"] or
                    endpoint.get("cuda_index") != card["cuda"]["cuda_index"]):
                errors.append("%s.%s identity mismatch" % (label, endpoint_key))
        expected_capability = capability.get(capability_key) if isinstance(capability, dict) else None
        if direction.get("destination_can_access_source") is not expected_capability:
            errors.append("%s capability direction mismatch" % label)
        if not isinstance(direction.get("peer_access_enabled"), bool):
            errors.append("%s peer_access_enabled is malformed" % label)
        elif direction.get("peer_access_enabled") is not expected_capability:
            errors.append("%s peer-access enable/capability mismatch" % label)
        results = direction.get("results")
        if not isinstance(results, list) or len(results) != len(expected_sizes):
            errors.append("%s result count mismatch" % label)
            continue
        seen_sizes = []
        for result in results:
            if not isinstance(result, dict):
                errors.append("%s contains a non-object result" % label)
                continue
            result_bytes = result.get("bytes")
            seen_sizes.append(result_bytes)
            if not _is_int(result_bytes, minimum=1):
                errors.append("%s byte count is invalid" % label)
            if result.get("iterations") != args.p2p_iterations:
                errors.append("%s iteration mismatch" % label)
            errors.extend(_validate_copy_metrics(
                result.get("direct"), True, label + ".direct"))
            errors.extend(_validate_copy_metrics(
                result.get("host_staged"), False, label + ".host_staged"))
            direct = result.get("direct")
            host = result.get("host_staged")
            if isinstance(direct, dict):
                if direct.get("available") is not direction.get("peer_access_enabled"):
                    errors.append("%s direct availability mismatch" % label)
                if (_is_int(result_bytes, minimum=1) and
                        direct.get("available") is True and all(
                        _is_finite_number(direct.get(key), positive=True)
                        for key in ("latency_ms", "gbps"))):
                    expected_gbps = result_bytes / (
                        float(direct["latency_ms"]) * 1.0e6)
                    if not _approximately_equal(direct["gbps"], expected_gbps):
                        errors.append("%s direct rate/timing mismatch" % label)
            if isinstance(host, dict):
                if host.get("path") != "D2H-then-H2D-pinned":
                    errors.append("%s host-staged path mismatch" % label)
                if (_is_int(result_bytes, minimum=1) and all(
                        _is_finite_number(host.get(key), positive=True)
                        for key in ("latency_ms", "gbps"))):
                    expected_gbps = result_bytes / (
                        float(host["latency_ms"]) * 1.0e6)
                    if not _approximately_equal(host["gbps"], expected_gbps):
                        errors.append("%s host-staged rate/timing mismatch" % label)
        if seen_sizes != expected_sizes:
            errors.append("%s size matrix mismatch" % label)
        if direction.get("all_copies_verified") is not True:
            errors.append("%s aggregate verification failed" % label)
    if document.get("all_copies_verified") is not True:
        errors.append("P2P aggregate verification failed")
    if document.get("verification_scope") != "full-buffer-host-scan-outside-timed-region":
        errors.append("P2P verification scope mismatch")
    return errors


def run_cuda_inventory(cards, binary, args, runner):
    if binary is None:
        return {
            "status": "planned", "command": None, "document": None,
            "mapping_comparisons": [
                {
                    "bdf": card["bdf"],
                    "nvidia_smi": card.get("nvidia_smi"),
                    "cuda_helper": None,
                    "indices_happen_to_match": False,
                    "bdf_mapped_by_both": False,
                }
                for card in cards
            ],
            "schema_expected": "170hx-xray-cuda-inventory-v1",
            "schema_actual": None,
            "duplicate_bdfs": [], "duplicate_cuda_indices": [],
            "missing_target_bdfs": sorted(
                card["bdf"] for card in cards
                if card.get("diagnostic_eligible")
            ),
            "index_namespace_note": (
                "cuda_index is a CUDA runtime ordinal; nvidia_smi_index is an "
                "NVML/nvidia-smi ordinal. They are not required to be equal."
            ),
            "reason": "CUDA helper is unavailable in dry-run",
        }
    record = runner.run(
        [str(binary), "inventory"], timeout=args.command_timeout,
        # Inventory is permitted during --dry-run when a prebuilt helper is
        # supplied. It allocates no benchmark buffers and performs no writes.
        read_only=True,
    )
    document = parse_json_stdout(record)
    document_errors = validate_cuda_inventory_document(document)
    helper_mapping = {}
    helper_indices = {}
    duplicate_bdfs = []
    duplicate_indices = []
    if isinstance(document, dict):
        for item in document.get("devices", []):
            device = item.get("record") if isinstance(item, dict) else None
            if not isinstance(device, dict):
                continue
            try:
                bdf = normalize_bdf(device.get("pci_bdf"))
            except DiagnosticError:
                continue
            index = device.get("cuda_index")
            if not isinstance(index, int) or index < 0:
                continue
            if bdf in helper_mapping:
                duplicate_bdfs.append(bdf)
            if index in helper_indices:
                duplicate_indices.append(index)
            helper_mapping[bdf] = device
            helper_indices[index] = bdf
    comparisons = []
    for card in cards:
        smi = card.get("nvidia_smi")
        helper = helper_mapping.get(card["bdf"])
        card["cuda"] = helper
        comparisons.append({
            "bdf": card["bdf"], "nvidia_smi": smi, "cuda_helper": helper,
            # Equality is informational only: the namespaces are independent.
            "indices_happen_to_match": (
                smi is not None and helper is not None and
                smi.get("nvidia_smi_index") == helper.get("cuda_index")
            ),
            "bdf_mapped_by_both": smi is not None and helper is not None,
        })
    missing_target_bdfs = sorted(
        card["bdf"] for card in cards
        if card.get("diagnostic_eligible") and card.get("cuda") is None
    )
    schema_actual = document.get("schema") if isinstance(document, dict) else None
    structurally_valid = (
        schema_actual == "170hx-xray-cuda-inventory-v1" and
        not duplicate_bdfs and not duplicate_indices and not document_errors
    )
    return {
        "status": "planned" if args.dry_run else (
            "pass" if record.get("returncode") == 0 and structurally_valid and
            not missing_target_bdfs
            else "failed"
        ),
        "command": record, "document": document,
        "mapping_comparisons": comparisons,
        "schema_expected": "170hx-xray-cuda-inventory-v1",
        "schema_actual": schema_actual,
        "duplicate_bdfs": sorted(set(duplicate_bdfs)),
        "duplicate_cuda_indices": sorted(set(duplicate_indices)),
        "validation_errors": document_errors,
        "missing_target_bdfs": missing_target_bdfs,
        "index_namespace_note": (
            "cuda_index is a CUDA runtime ordinal; nvidia_smi_index is an "
            "NVML/nvidia-smi ordinal. They are not required to be equal."
        ),
    }


def run_bandwidth(cards, binary, args, runner):
    results = []
    for card in cards:
        if not card.get("diagnostic_eligible"):
            results.append({
                "bdf": card["bdf"],
                "status": "skipped_observation_only_identity",
            })
            continue
        cuda = card.get("cuda")
        if not cuda:
            results.append({
                "bdf": card["bdf"], "status": "skipped_no_cuda_mapping",
            })
            continue
        power = BenchmarkPowerEvidence("bandwidth", [card], args, runner)
        if not power.prepare():
            evidence = power.document()
            results.append({
                "bdf": card["bdf"], "cuda_index": cuda["cuda_index"],
                "status": "failed", "reason": "power evidence preparation failed",
                "power_evidence": evidence,
                "power_evidence_validation_errors": validate_power_evidence(
                    evidence, [card], args, "bandwidth"
                ),
            })
            continue
        try:
            record = runner.run([
                str(binary), "bandwidth", "--device", str(cuda["cuda_index"]),
                "--bytes", str(args.bandwidth_bytes),
                "--iterations", str(args.bandwidth_iterations),
            ], timeout=args.command_timeout)
        finally:
            evidence = power.finish()
        power_errors = validate_power_evidence(
            evidence, [card], args, "bandwidth"
        )
        parsed = parse_json_stdout(record)
        validation_errors = validate_bandwidth_document(parsed, card, args)
        target_verified = False
        if isinstance(parsed, dict) and isinstance(parsed.get("device"), dict):
            try:
                target_verified = (
                    normalize_bdf(parsed["device"].get("pci_bdf")) == card["bdf"]
                )
            except DiagnosticError:
                pass
        results.append({
            "bdf": card["bdf"], "cuda_index": cuda["cuda_index"],
            "status": "planned" if args.dry_run else (
                "pass" if record.get("returncode") == 0 and target_verified and
                not validation_errors and not power_errors and
                evidence.get("status") in ("pass", "not-requested")
                else "failed"
            ),
            "target_bdf_verified": target_verified,
            "validation_errors": validation_errors,
            "power_evidence": evidence,
            "power_evidence_validation_errors": power_errors,
            "result": parsed, "command": record,
        })
    return results


def _parse_hex_u32(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-f]{8}", value):
        return None
    return int(value, 16)


def _temperature_selector_dependent_skip_count(mask):
    return ((3 if mask & 1 else 0) + (1 if mask & 2 else 0) +
            (1 if mask & 4 else 0) + (2 if mask & 8 else 0))


def _temperature_selector_semantics(raw):
    if raw is None:
        return None
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    aperture_available = not (badf or all_ones)
    gpc = (raw >> 16) & 0xF
    bjt = raw & 0xF
    fields_in_range = (gpc <= 7 and bjt <= 2) if aperture_available else None
    allowed = (0xF << 16) | 0xF | (1 << 30)
    reserved_zero = (
        not bool(raw & (~allowed & 0xFFFFFFFF))
        if aperture_available else None
    )
    semantically_valid = bool(
        aperture_available and fields_in_range and reserved_zero
    )
    return {
        "selector_available": aperture_available,
        "aperture_available": aperture_available,
        "selector_badf": badf,
        "selector_all_ones": all_ones,
        "selector_fields_in_range": fields_in_range,
        "selector_reserved_bits_zero": reserved_zero,
        "selector_semantically_valid": semantically_valid,
        "gpc": gpc if semantically_valid else None,
        "bjt": bjt if semantically_valid else None,
    }


def _temperature_liveness_report(tsense, num_active_fbps):
    if tsense is None or num_active_fbps is None:
        return None
    tsense_badf = (tsense & 0xFFFF0000) == 0xBADF0000
    tsense_all_ones = tsense == 0xFFFFFFFF
    state = (tsense >> 29) & 0x3
    tsense_affirmative = (
        not tsense_badf and not tsense_all_ones and state in (1, 2)
    )
    active_badf = (num_active_fbps & 0xFFFF0000) == 0xBADF0000
    active_all_ones = num_active_fbps == 0xFFFFFFFF
    count = num_active_fbps & 0x1F
    reserved_zero = (num_active_fbps & ~0x1F) == 0
    active_affirmative = (
        not active_badf and not active_all_ones and reserved_zero and
        1 <= count <= 12
    )
    return {
        "tsense_raw": "0x%08x" % tsense,
        "tsense_state": state,
        "tsense_state_name": {
            0: "invalid", 1: "valid", 2: "shadow", 3: "reserved"
        }[state],
        "tsense_affirmative": tsense_affirmative,
        "num_active_fbps_raw": "0x%08x" % num_active_fbps,
        "num_active_fbps_count": count,
        "num_active_fbps_reserved_zero": reserved_zero,
        "num_active_fbps_in_range": 1 <= count <= 12,
        "num_active_fbps_affirmative": active_affirmative,
        "two_signal_corroborated": tsense_affirmative and active_affirmative,
        "policy": (
            "TSENSE state must be VALID/SHADOW and NUM_ACTIVE_FBPS must have "
            "reserved bits zero with count 1..12"
        ),
    }


def _validate_temperature_readout(readout, label, expected_encoding=None,
                                  expected_name=None, expected_offset=None,
                                  expected_kind=None):
    errors = []
    if not isinstance(readout, dict):
        return ["%s is not an object" % label]
    readout_keys = {
        "raw", "state_encoding", "state", "state_value", "source",
        "payload_temperature_c", "temperature_c", "measurement_authoritative",
        "live", "warning", "badf", "all_ones", "available",
    }
    if expected_name is not None:
        readout_keys.add("name")
    if expected_offset is not None:
        readout_keys.add("offset")
    if expected_kind is not None:
        readout_keys.add("readout_kind")
    for optional in ("read_skipped", "skip_reason", "observed_raw", "unbound",
                     "unavailable_reason", "journal_valid"):
        if optional in readout:
            readout_keys.add(optional)
    errors.extend(_exact_keys(readout, readout_keys, label))
    observed_encoding = readout.get("state_encoding")
    if (expected_encoding is not None and
            observed_encoding != expected_encoding):
        errors.append("%s state encoding does not match register semantics" % label)
    encoding = expected_encoding or observed_encoding
    if expected_name is not None and readout.get("name") != expected_name:
        errors.append("%s register name mismatch" % label)
    if expected_offset is not None and readout.get("offset") != (
            "0x%08x" % expected_offset):
        errors.append("%s register offset mismatch" % label)
    if expected_kind is not None and readout.get("readout_kind") != expected_kind:
        errors.append("%s readout-kind mismatch" % label)
    raw = _parse_hex_u32(readout.get("raw"))
    if raw is None:
        observed_raw = _parse_hex_u32(readout.get("observed_raw"))
        if (readout.get("raw") is None and observed_raw is not None and
                readout.get("state") == "unbound-invalid-selector" and
                readout.get("state_value") is None and
                readout.get("source") is None and
                readout.get("payload_temperature_c") is None and
                readout.get("temperature_c") is None and
                readout.get("measurement_authoritative") is False and
                readout.get("live") is False and
                readout.get("warning") is None and
                readout.get("badf") is False and
                readout.get("all_ones") is False and
                readout.get("available") is False and
                readout.get("read_skipped") is False and
                readout.get("unbound") is True and
                readout.get("unavailable_reason") ==
                "selector-not-semantically-valid"):
            return errors
        if (readout.get("raw") is None and
                readout.get("state") == "not-read-owner-race-policy" and
                readout.get("state_value") is None and
                readout.get("source") is None and
                readout.get("payload_temperature_c") is None and
                readout.get("temperature_c") is None and
                readout.get("measurement_authoritative") is False and
                readout.get("live") is False and
                readout.get("warning") is None and
                readout.get("badf") is False and
                readout.get("all_ones") is False and
                readout.get("available") is False and
                readout.get("read_skipped") is True and
                readout.get("skip_reason") ==
                "selector-dependent-read-unconditionally-omitted-owner-race"):
            return errors
        errors.append("%s raw value is malformed/skipped payload invalid" % label)
        return errors
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    if badf or all_ones:
        if (readout.get("badf") is not badf or
                readout.get("all_ones") is not all_ones or
                readout.get("available") is not False or
                readout.get("state") != (
                    "unavailable-badf" if badf else "unavailable-all-ones") or
                readout.get("state_value") is not None or
                readout.get("source") is not None or
                readout.get("payload_temperature_c") is not None or
                readout.get("temperature_c") is not None or
                readout.get("measurement_authoritative") is not False or
                readout.get("live") is not False or
                readout.get("warning") is not None):
            errors.append("%s BADF word is decoded as usable telemetry" % label)
        return errors
    if (readout.get("badf") is not False or
            readout.get("all_ones") is not False or
            readout.get("available") is not True):
        errors.append("%s availability/BADF mirror mismatch" % label)
    if encoding == "three-state":
        state_value = (raw >> 29) & 0x3
        state = {0: "invalid", 1: "valid", 2: "shadow"}.get(
            state_value, "reserved"
        )
    elif encoding == "valid-bit":
        state_value = (raw >> 30) & 0x1
        state = "valid" if state_value else "invalid"
    else:
        errors.append("%s state encoding is unknown" % label)
        return errors
    source = "software-override" if raw & (1 << 31) else "hardware-logic"
    fixed = (raw >> 3) & 0x3FFF
    if fixed & 0x2000:
        fixed -= 0x4000
    payload_temperature = (
        fixed / 32.0 if state in ("valid", "shadow") else None
    )
    expected_temperature = payload_temperature if state == "valid" else None
    if not _is_int(readout.get("state_value"), state_value, state_value):
        errors.append("%s state value does not match raw" % label)
    if readout.get("state") != state:
        errors.append("%s state does not match raw" % label)
    if readout.get("source") != source:
        errors.append("%s source does not match raw" % label)
    actual_payload = readout.get("payload_temperature_c")
    actual_temperature = readout.get("temperature_c")
    if ((payload_temperature is not None and
         not isinstance(actual_payload, float)) or
            not _strict_equal(actual_payload, payload_temperature)):
        errors.append("%s payload temperature does not match raw/state" % label)
    if ((expected_temperature is not None and
         not isinstance(actual_temperature, float)) or
            not _strict_equal(actual_temperature, expected_temperature)):
        errors.append("%s temperature does not match raw/state" % label)
    authoritative = state == "valid"
    expected_warning = (
        "shadow-state-payload-preserved-but-not-authoritative"
        if encoding == "three-state" and state == "shadow" else None
    )
    if (readout.get("measurement_authoritative") is not authoritative or
            readout.get("live") is not authoritative or
            readout.get("warning") != expected_warning):
        errors.append("%s authority/shadow-warning mismatch" % label)
    if expected_temperature is not None and not _is_finite_number(
            expected_temperature, minimum=-256.0, maximum=255.96875):
        errors.append("%s temperature is outside SFP9.5 range" % label)
    return errors


def _signed_thermal_field(value, width):
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _expected_thermal_context_decode(name, raw):
    if (raw & 0xFFFF0000) == 0xBADF0000 or raw == 0xFFFFFFFF:
        return None
    if name == "nv_therm_sensor_0":
        select = (raw >> 15) & 3
        return {"temperature_code": raw & 0x3FFF, "select": select,
                "select_name": (
                    "real", "override", "secure-override", "reserved"
                )[select],
                "override_raw": (raw >> 17) & 0x3FFF}
    if name == "nv_therm_sensor_1":
        return {"select_sw_a": bool(raw & 1),
                "select_sw_b": bool(raw & 2),
                "ts_stable_count": (raw >> 2) & 0xFFFFF,
                "reset": bool(raw & (1 << 31))}
    if name == "nv_therm_sensor_2":
        return {"sw_a": raw & 0x3FF, "sw_b": (raw >> 16) & 0x7FF}
    if name == "nv_therm_sensor_3":
        return {"hw_a": raw & 0x3FF, "hw_b": (raw >> 16) & 0x7FF}
    if name == "nv_therm_sensor_4":
        return {"hw_a_nominal": raw & 0x3FF,
                "hw_b_nominal": (raw >> 16) & 0x7FF}
    if name == "nv_therm_sensor_6":
        scale = (raw >> 24) & 3
        return {"power_on": bool(raw & 1),
                "xtal_gate_invalidates_temperature": bool(raw & 2),
                "xtal_gate_status": "gated" if raw & 4 else "running",
                "polling_interval_period": (raw >> 16) & 0xFF,
                "polling_interval_scale": scale,
                "polling_interval_scale_us": (16, 256, 4096, 65536)[scale],
                "polling_status": "error" if raw & (1 << 31) else "valid"}
    if name == "nv_therm_sensor_7":
        return {"debug_adc_code": raw & 0x3FFF,
                "adc_override_enabled": bool(raw & (1 << 15)),
                "power_down": bool(raw & (1 << 16))}
    if name == "nv_therm_sensor_dedicated_overt_max":
        return {"source_flags": {
                    "tsense": bool(raw & 1), "offset_tsense": bool(raw & 2),
                    "gpu_max": bool(raw & (1 << 4)),
                    "gpu_average": bool(raw & (1 << 5)),
                    "gpu_offset_max": bool(raw & (1 << 6)),
                    "gpu_offset_average": bool(raw & (1 << 7))},
                "temperature_c": _signed_thermal_field(
                    (raw >> 17) & 0x3FFF, 14) / 32.0}
    i2cs_fields = {
        "nv_therm_i2cs_sensor_00": ("rlts", False),
        "nv_therm_i2cs_sensor_11": ("riet", False),
        "nv_therm_i2cs_sensor_13": ("unclamped_temperature", True),
        "nv_therm_i2cs_sensor_14": ("negative_temperature_offset", False),
    }
    if name in i2cs_fields:
        symbolic_field, signed = i2cs_fields[name]
        value = (_signed_thermal_field(raw & 0xFF, 8)
                 if signed else raw & 0xFF)
        return {"byte_value": value, "signed": signed,
                "symbolic_field": symbolic_field, "symbolic_value": value,
                "runtime_relevance": "uncertain-legacy-alias"}
    if name == "nv_therm_sensor_8":
        return {"offset_c": _signed_thermal_field((raw >> 3) & 0x3FFF, 14) / 32.0}
    if name == "nv_therm_sensor_9":
        selector = (raw >> 28) & 0xF
        names = ("tsense", "offset_tsense", "constant", "max", "gpu_max",
                 "gpu_average", "gpu_offset_max", "gpu_offset_average")
        return {"source_flags": {
                    "tsense": bool(raw & 1), "offset_tsense": bool(raw & 2),
                    "constant": bool(raw & 4), "gpu_max": bool(raw & (1 << 4)),
                    "gpu_average": bool(raw & (1 << 5)),
                    "gpu_offset_max": bool(raw & (1 << 6)),
                    "gpu_offset_average": bool(raw & (1 << 7))},
                "i2cs_selector": selector,
                "i2cs_selector_name": names[selector] if selector < 8 else "reserved",
                "source_mask_raw": "0x%03x" % (raw & 0x3FF),
                "source_bits": [bool(raw & (1 << bit)) for bit in range(10)],
                "unnamed_source_bit_indices": [3, 8, 9]}
    if name == "nv_therm_statistics_temp_avg":
        return {"data": raw & 0xFF, "reset": bool(raw & (1 << 8))}
    if name == "nv_therm_statistics_ctrl":
        return {"ba_enabled": bool(raw & 1), "temperature_enabled": bool(raw & 2)}
    if name == "nv_pgc6_sci_gpu_smbus_cmd00":
        return {"current_byte": raw & 0xFF,
                "signed_offset": _signed_thermal_field((raw >> 8) & 0xFF, 8),
                "read_data_selection": "offset-temperature" if raw & (1 << 28) else "offset",
                "units": None}
    if name in ("nv_therm_gpc_tsense_hs_offset_index",
                "nv_therm_gpc_tsense_override_index"):
        gpc = (raw >> 16) & 0xF
        bjt = raw & 0xF
        return {"bjt": bjt, "gpc": gpc,
                "fields_in_range": gpc <= 7 and bjt <= 2,
                "reserved_bits_zero": (raw & ~0xC00F000F) == 0,
                "read_increment_enabled": bool(raw & (1 << 30)),
                "write_increment_enabled": bool(raw & (1 << 31))}
    if name == "nv_therm_gpc_tsense_global_override":
        return {"value": _signed_thermal_field(raw & 0x3FFF, 14),
                "enabled": bool(raw & (1 << 16))}
    if name == "nv_pgc6_sci_fs_overt_ts_sm":
        return {"ts_clock_divider": raw & 0x1F,
                "stable_period": (raw >> 8) & 0xF,
                "stable_scale": bool(raw & (1 << 13)),
                "polling_period": (raw >> 16) & 0x1F,
                "polling_scale": (raw >> 21) & 0x7}
    if name == "nv_pgc6_sci_fs_overt_ts_adj":
        return {"bandgap_voltage": raw & 0xFF,
                "bandgap_temperature_coefficient": (raw >> 8) & 0x7,
                "configuration": (raw >> 11) & 0x1FFFFF}
    if name == "nv_pgc6_sci_fs_overt_thresh":
        return {"temperature_threshold_signed": _signed_thermal_field(
                    (raw >> 8) & 0x1FF, 9),
                "time_period": (raw >> 24) & 0x1F,
                "time_scale": (raw >> 29) & 0x7}
    if name == "nv_pgc6_sci_fs_overt_fusectrl":
        return {"presnap_bandgap_voltage": raw & 0xFF,
                "presnap_bandgap_temperature_coefficient": (raw >> 8) & 0x7,
                "presnap_bandgap_valid": bool(raw & (1 << 11)),
                "hardware_auto_snapshot": bool(raw & (1 << 31))}
    if name == "nv_pgc6_sci_fs_overt_fuse":
        return {"bandgap_voltage": raw & 0xFF,
                "bandgap_temperature_coefficient": (raw >> 8) & 0x7,
                "adjustment_a_signed": _signed_thermal_field(
                    (raw >> 11) & 0x3FF, 10),
                "adjustment_b_signed": _signed_thermal_field(
                    (raw >> 21) & 0x3FF, 10)}
    if name == "nv_pgc6_sci_fs_overt_temp_coeff_nom":
        return {"coefficient_a": raw & 0x3FF,
                "coefficient_b": (raw >> 16) & 0x7FF}
    if name == "nv_pgc6_sci_fs_overt_temp_coeff_final":
        return {"coefficient_a_signed": _signed_thermal_field(raw & 0xFFF, 12),
                "coefficient_b_signed": _signed_thermal_field(
                    (raw >> 16) & 0x1FFF, 13)}
    if name == "tsense":
        return {"adc_code": raw & 0x3FFF, "stream_raw": bool(raw & (1 << 16)),
                "bjt_selector": (raw >> 20) & 0xF,
                "power_down": bool(raw & (1 << 24)),
                "control_source_override": bool(raw & (1 << 28)),
                "calibration_source_override": bool(raw & (1 << 29)),
                "temperature_coefficient_source_override": bool(raw & (1 << 30)),
                "configuration_trigger": bool(raw & (1 << 31))}
    if name == "adjustment":
        return {"bandgap_voltage": raw & 0xFF,
                "bandgap_temperature_coefficient": (raw >> 8) & 0x7,
                "bandgap_mode_no_average": bool(raw & (1 << 11)),
                "vptat_bias_current": (raw >> 12) & 0x3,
                "bandgap_bypass": bool(raw & (1 << 14)),
                "bandgap_startup_bias_current": bool(raw & (1 << 15)),
                "ts_vref_output": (raw >> 16) & 0xF,
                "ptat_bipolar_swap": bool(raw & (1 << 20)),
                "ptat_opamp_swap": bool(raw & (1 << 21)),
                "ptat_source": (raw >> 22) & 0xF,
                "stop_swap": bool(raw & (1 << 26)),
                "bypass_ptat": bool(raw & (1 << 27)),
                "send_vbg": bool(raw & (1 << 28)),
                "average_vbg": bool(raw & (1 << 29)),
                "start_bandgap": bool(raw & (1 << 30)),
                "stop_swapping_bjt": bool(raw & (1 << 31))}
    if name == "raw_override":
        return {"value": raw & 0x3FFF, "enabled": bool(raw & (1 << 16))}
    if name == "ts_auto":
        return {"sample_delay": raw & 0x3FFF,
                "auto_polling": bool(raw & (1 << 15)),
                "polling_interval": (raw >> 16) & 0xFFFF}
    return None


def _validate_raw_thermal_context(record, specification, label):
    if not isinstance(record, dict):
        return ["%s is not an object" % label]
    name, offset, kind = specification
    errors = _exact_keys(record, {
        "name", "offset", "raw", "badf", "all_ones",
        "suspicious_unavailable_pattern", "available", "read_skipped",
        "skip_reason", "readout_kind", "independent_physical_sensor",
        "decoded_temperature_c", "decoded", "interpretation",
    }, label)
    raw = _parse_hex_u32(record.get("raw"))
    if record.get("name") != name:
        errors.append("%s register name mismatch" % label)
    if record.get("offset") != "0x%08x" % offset:
        errors.append("%s register offset mismatch" % label)
    if record.get("readout_kind") != kind:
        errors.append("%s register classification mismatch" % label)
    if record.get("independent_physical_sensor") is not False:
        errors.append("%s physical-sensor classification mismatch" % label)
    if record.get("decoded_temperature_c") is not None:
        errors.append("%s undocumented temperature conversion is non-null" % label)
    if raw is None:
        expected_interpretation = (
            "fixed selector-dependent view was unconditionally omitted "
            "because firmware can race its selector; no zero value is fabricated"
        )
        if (record.get("raw") is not None or record.get("badf") is not False or
                record.get("all_ones") is not False or
                record.get("suspicious_unavailable_pattern") is not False or
                record.get("available") is not False or
                record.get("read_skipped") is not True or
                record.get("skip_reason") !=
                "selector-dependent-read-unconditionally-omitted-owner-race" or
                record.get("decoded") is not None or
                record.get("interpretation") != expected_interpretation):
            errors.append("%s skipped dependent view is malformed" % label)
        return errors
    expected_badf = raw is not None and (raw & 0xFFFF0000) == 0xBADF0000
    expected_all_ones = raw == 0xFFFFFFFF if raw is not None else False
    if record.get("badf") is not expected_badf:
        errors.append("%s BADF decode mismatch" % label)
    if (record.get("all_ones") is not expected_all_ones or
            record.get("suspicious_unavailable_pattern") is not
            (expected_badf or expected_all_ones)):
        errors.append("%s all-ones/unavailable decode mismatch" % label)
    expected_available = not (expected_badf or expected_all_ones)
    if (record.get("available") is not expected_available or
            record.get("read_skipped") is not False or
            record.get("skip_reason") is not None):
        errors.append("%s availability/skip mirror mismatch" % label)
    if not _strict_equal(
            record.get("decoded"), _expected_thermal_context_decode(name, raw)):
        errors.append("%s decoded field mirror mismatch" % label)
    expected_interpretation = (
        "register-backed logical endpoint counted once in the logical census; "
        "independence as a physical diode is unproven and no undocumented "
        "temperature scale is inferred"
        if kind == "logical-hardware-endpoint" else
        "fixed read-only raw/control/alias context; not counted as an "
        "independent sensor and no undocumented temperature scale inferred"
    )
    if record.get("interpretation") != expected_interpretation:
        errors.append("%s interpretation/classification mismatch" % label)
    return errors


def _validate_gpu_sci(record, label):
    if not isinstance(record, dict):
        return ["%s GPU_SCI object is missing" % label]
    errors = _exact_keys(record, {
        "mini_tsense", "adc_context", "register_block",
        "register_block_range", "register_count",
    }, label)
    if (record.get("register_block_range") != "0x00118b90..0x00118bb0" or
            not _is_int(record.get("register_count"), 9, 9)):
        errors.append("%s GPU_SCI register-block metadata mismatch" % label)
    block = record.get("register_block")
    if not isinstance(block, list) or len(block) != 9:
        errors.append("%s GPU_SCI register-block count mismatch" % label)
    else:
        for index, specification in enumerate(TEMPERATURE_GPU_SCI_CONTEXT):
            errors.extend(_validate_raw_thermal_context(
                block[index], specification,
                "%s GPU_SCI register[%d]" % (label, index)))
    mini = record.get("mini_tsense")
    if not isinstance(mini, dict):
        errors.append("%s GPU_SCI MINI_TSENSE is missing" % label)
    else:
        errors.extend(_exact_keys(mini, {
            "name", "offset", "raw", "encoding", "payload_temperature_c",
            "temperature_c", "measurement_authoritative",
            "architected_valid_bit", "architected_state_bits",
            "architected_source_bit", "badf", "all_ones", "availability",
            "logical_endpoint_family", "relation_to_gpc_bjt",
            "physical_diode_independence",
            "interpretation",
        }, "%s GPU_SCI MINI_TSENSE" % label))
        raw = _parse_hex_u32(mini.get("raw"))
        badf = raw is not None and (raw & 0xFFFF0000) == 0xBADF0000
        all_ones = raw == 0xFFFFFFFF if raw is not None else False
        payload = ((raw >> 8) & 0x1FF) if raw is not None else None
        signed = (payload - 0x200 if payload is not None and payload & 0x100 else payload)
        if (mini.get("name") != "nv_pgc6_sci_fs_overt_temperature" or
                mini.get("offset") != "0x00118bb0"):
            errors.append("%s GPU_SCI MINI_TSENSE identity mismatch" % label)
        if mini.get("encoding") != "signed-9-bit-in-bits-16-8-integer-celsius":
            errors.append("%s GPU_SCI MINI_TSENSE encoding mismatch" % label)
        if (not _strict_equal(mini.get("payload_temperature_c"),
                              None if badf or all_ones else signed) or
                mini.get("temperature_c") is not None or
                mini.get("measurement_authoritative") is not False or
                mini.get("badf") is not badf or
                mini.get("all_ones") is not all_ones):
            errors.append("%s GPU_SCI MINI_TSENSE decode mismatch" % label)
        if any(mini.get(key) is not None for key in (
                "architected_valid_bit", "architected_state_bits",
                "architected_source_bit")):
            errors.append("%s GPU_SCI MINI_TSENSE invents validity/state/source" % label)
        if "valid" in mini or "state" in mini or "source" in mini:
            errors.append("%s GPU_SCI MINI_TSENSE exposes invented semantics" % label)
        expected_availability = ("unavailable-badf" if badf else
                                 "unavailable-all-ones" if all_ones else
                                 "payload-observed-no-architected-validity-bit")
        if (mini.get("availability") != expected_availability or
                mini.get("logical_endpoint_family") != "gpu-sci-mini-tsense" or
                mini.get("relation_to_gpc_bjt") != "separate-register-family" or
                mini.get("physical_diode_independence") != "unproven" or
                mini.get("interpretation") != (
                    "separate GPU_SCI MINI_TSENSE logical endpoint/register "
                    "family; physical-diode independence from the GPC/BJT "
                    "matrix is unproven, the signed payload is preserved "
                    "separately but is not promoted to an authoritative "
                    "temperature because this word has no validity or freshness "
                    "bit")):
            errors.append("%s GPU_SCI MINI_TSENSE classification mismatch" % label)
    adc = record.get("adc_context")
    if not isinstance(adc, dict):
        errors.append("%s GPU_SCI ADC context is missing" % label)
    else:
        errors.extend(_exact_keys(adc, {
            "name", "offset", "raw", "badf", "all_ones", "raw_adc_code",
            "override_value", "override_enabled", "temperature_scale",
            "readout_kind", "interpretation",
        }, "%s GPU_SCI ADC context" % label))
        raw = _parse_hex_u32(adc.get("raw"))
        badf = raw is not None and (raw & 0xFFFF0000) == 0xBADF0000
        all_ones = raw == 0xFFFFFFFF if raw is not None else False
        if (adc.get("name") != "nv_pgc6_sci_fs_overt_ts_adc" or
                adc.get("offset") != "0x00118bac"):
            errors.append("%s GPU_SCI ADC identity mismatch" % label)
        unavailable = badf or all_ones or raw is None
        if (adc.get("badf") is not badf or adc.get("all_ones") is not all_ones or
                not _strict_equal(adc.get("raw_adc_code"),
                                  None if unavailable else raw & 0x3FFF) or
                not _strict_equal(adc.get("override_value"),
                                  None if unavailable else (raw >> 16) & 0x3FFF) or
                adc.get("override_enabled") is not (None if unavailable else bool(raw & (1 << 31))) or
                adc.get("temperature_scale") is not None or
                adc.get("readout_kind") !=
                "raw-adc-and-override-context-not-temperature" or
                adc.get("interpretation") != (
                    "context for GPU_SCI MINI_TSENSE; no Celsius conversion is "
                    "asserted for the 14-bit ADC code"
                )):
            errors.append("%s GPU_SCI ADC decode mismatch" % label)
    if isinstance(block, list) and len(block) == 9:
        if (not isinstance(block[8], dict) or not isinstance(mini, dict) or
                block[8].get("raw") != mini.get("raw")):
            errors.append("%s GPU_SCI temperature duplicate mismatch" % label)
        if (not isinstance(block[7], dict) or not isinstance(adc, dict) or
                block[7].get("raw") != adc.get("raw")):
            errors.append("%s GPU_SCI ADC duplicate mismatch" % label)
    return errors


def _nvml_return_code(record):
    if not isinstance(record, dict):
        return None
    value = record.get("code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate_nvml_return(record, label):
    code = _nvml_return_code(record)
    allowed_keys = ({"code", "name"}, {"code", "name", "message"})
    if (not isinstance(record, dict) or set(record) not in allowed_keys or
            ("message" in record and
             (not isinstance(record.get("message"), str) or
              not record.get("message"))) or
            code not in NVML_RETURN_NAMES or record.get("name") !=
            NVML_RETURN_NAMES.get(code)):
        return ["%s return metadata is malformed" % label]
    return []


def _valid_nonnegative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_direct_nvml_card(record, expected_bdf, label,
                               function_availability=None):
    """Validate the direct ctypes census without inventing unsupported values."""
    errors = []
    if not isinstance(record, dict):
        return ["%s direct NVML record is missing" % label]
    if record.get("bdf") != expected_bdf:
        errors.append("%s direct NVML BDF mismatch" % label)
    errors.extend(_validate_nvml_return(
        record.get("handle_lookup_return"), "%s handle lookup" % label
    ))
    if not _valid_nonnegative_int(record.get("handle_lookup_elapsed_us")):
        errors.append("%s direct NVML handle latency is malformed" % label)
    handle_code = _nvml_return_code(record.get("handle_lookup_return"))
    if handle_code is None:
        errors.append("%s direct NVML handle return is malformed" % label)
        return errors
    surfaces = (
        "field_values", "temperature_v", "thermal_settings",
        "margin_temperature", "temperature_thresholds",
    )
    expected_record_keys = {
        "bdf", "handle_lookup_return", "handle_lookup_elapsed_us",
    }
    if handle_code == 0:
        expected_record_keys.update(surfaces)
    errors.extend(_exact_keys(record, expected_record_keys,
                              "%s direct NVML record" % label))
    if handle_code != 0:
        if any(key in record for key in surfaces):
            errors.append("%s failed handle lookup contains query payloads" % label)
        return errors

    if isinstance(function_availability, dict):
        availability_keys = {
            "field_values": "field_values",
            "temperature_v": "temperature_v",
            "thermal_settings": "thermal_settings",
            "margin_temperature": "margin_temperature",
            "temperature_thresholds": "temperature_thresholds",
        }
        for availability_key, surface_key in availability_keys.items():
            surface = record.get(surface_key)
            symbol_absent = (
                isinstance(surface, dict) and
                surface.get("status") == "symbol-absent"
            )
            if symbol_absent is function_availability.get(availability_key):
                errors.append(
                    "%s direct NVML availability/payload mismatch: %s" %
                    (label, availability_key)
                )

    fields = record.get("field_values")
    if isinstance(fields, dict) and fields.get("status") == "symbol-absent":
        errors.extend(_exact_keys(fields, {"status"},
                                  "%s FieldValues absence" % label))
    elif not isinstance(fields, dict):
        errors.append("%s direct NVML field census is malformed" % label)
    else:
        errors.extend(_exact_keys(fields, {"outer_return", "call_elapsed_us", "fields"},
                                  "%s FieldValues" % label))
        outer_code = _nvml_return_code(fields.get("outer_return"))
        values = fields.get("fields")
        errors.extend(_validate_nvml_return(
            fields.get("outer_return"), "%s FieldValues outer call" % label
        ))
        if not _valid_nonnegative_int(fields.get("call_elapsed_us")):
            errors.append("%s FieldValues call latency is malformed" % label)
        if outer_code is None or not isinstance(values, list):
            errors.append("%s direct NVML field return is malformed" % label)
        else:
            expected_fields = (
                (82, "memory_temperature_c", 1),
                (193, "shutdown_threshold_c", 5),
                (194, "slowdown_threshold_c", 5),
                (195, "memory_max_threshold_c", 5),
                (196, "gpu_max_threshold_c", 5),
                (308, "gpu_temperature_subdegree_c", 0),
            )
            expected_ids = tuple(item[0] for item in expected_fields)
            if tuple(item.get("field_id") for item in values
                     if isinstance(item, dict)) != expected_ids:
                errors.append("%s direct NVML field set/order mismatch" % label)
            for index, item in enumerate(values):
                item_label = "%s direct NVML field[%d]" % (label, index)
                if not isinstance(item, dict):
                    errors.append("%s is malformed" % item_label)
                    continue
                errors.extend(_exact_keys(item, {
                    "field_id", "observed_field_id", "name", "scope_id",
                    "return", "raw_item_return", "outer_call_succeeded",
                    "value_type", "value", "timestamp_epoch_us", "latency_us",
                }, item_label))
                if index >= len(expected_fields):
                    errors.append("%s is unexpected" % item_label)
                    continue
                expected_id, expected_name, expected_type = expected_fields[index]
                value_type = item.get("value_type")
                if (not _is_int(item.get("field_id"), expected_id, expected_id) or
                        not _is_int(item.get("observed_field_id"),
                                    expected_id, expected_id) or
                        item.get("name") != expected_name or
                        not _is_int(item.get("scope_id"), 0, 0)):
                    errors.append("%s identity metadata mismatch" % item_label)
                code = _nvml_return_code(item.get("return"))
                errors.extend(_validate_nvml_return(
                    item.get("return"), "%s effective" % item_label
                ))
                errors.extend(_validate_nvml_return(
                    item.get("raw_item_return"), "%s raw item" % item_label
                ))
                if code is None:
                    errors.append("%s return is malformed" % item_label)
                    continue
                if outer_code != 0 and code != outer_code:
                    errors.append("%s effective return does not match outer failure" % item_label)
                if item.get("outer_call_succeeded") is not (outer_code == 0):
                    errors.append("%s outer-success marker mismatch" % item_label)
                raw_item_code = _nvml_return_code(item.get("raw_item_return"))
                if outer_code == 0 and raw_item_code != code:
                    errors.append("%s raw/effective return mismatch" % item_label)
                if code == 0:
                    errors.extend(_exact_keys(
                        value_type, {"code", "name"},
                        "%s value type" % item_label
                    ))
                    if (not isinstance(value_type, dict) or
                            not _is_int(value_type.get("code"),
                                        expected_type, expected_type) or
                            value_type.get("name") !=
                            NVML_VALUE_TYPE_NAMES[expected_type]):
                        errors.append("%s successful type metadata mismatch" % item_label)
                    value = item.get("value")
                    if expected_type in (1, 5):
                        value_valid = isinstance(value, int) and not isinstance(value, bool)
                    else:
                        value_valid = (isinstance(value, float) and
                                       math.isfinite(value))
                    minimum = 0.0 if expected_type == 1 else -273.15
                    if (not value_valid or not _is_finite_number(
                            value, minimum=minimum, maximum=1000.0)):
                        errors.append("%s successful value is missing/implausible" % item_label)
                    for key in ("timestamp_epoch_us", "latency_us"):
                        value = item.get(key)
                        if not _valid_nonnegative_int(value):
                            errors.append("%s successful %s is invalid" % (item_label, key))
                elif any(item.get(key) is not None for key in (
                        "value_type", "value", "timestamp_epoch_us", "latency_us")):
                    errors.append("%s unsupported/failed payload is not null" % item_label)

    temperatures = record.get("temperature_v")
    if isinstance(temperatures, dict) and temperatures.get("status") == "symbol-absent":
        errors.extend(_exact_keys(temperatures, {"status"},
                                  "%s TemperatureV absence" % label))
    elif not isinstance(temperatures, list) or len(temperatures) != 2:
        errors.append("%s direct NVML TemperatureV census is malformed" % label)
    else:
        expected_temperatures = ((0, "gpu"), (1, "gpu_max"))
        for index, item in enumerate(temperatures):
            code = _nvml_return_code(item.get("return") if isinstance(item, dict) else None)
            expected_sensor_type, expected_sensor_name = expected_temperatures[index]
            errors.extend(_exact_keys(item, {
                "sensor_type", "name", "return", "temperature_c",
                "call_elapsed_us", "structure_version",
            }, "%s TemperatureV[%d]" % (label, index)))
            if (not isinstance(item, dict) or
                    not _is_int(item.get("sensor_type"),
                                expected_sensor_type, expected_sensor_type) or
                    item.get("name") != expected_sensor_name or
                    item.get("structure_version") != "0x0100000c"):
                errors.append("%s TemperatureV[%d] metadata mismatch" % (label, index))
                continue
            errors.extend(_validate_nvml_return(
                item.get("return"), "%s TemperatureV[%d]" % (label, index)
            ))
            if not _valid_nonnegative_int(item.get("call_elapsed_us")):
                errors.append("%s TemperatureV[%d] latency is malformed" % (label, index))
            if code is None:
                errors.append("%s TemperatureV[%d] return is malformed" % (label, index))
            elif code == 0 and (not isinstance(item.get("temperature_c"), int) or
                    isinstance(item.get("temperature_c"), bool) or
                    not _is_finite_number(item.get("temperature_c"),
                                          minimum=-273.15, maximum=1000.0)):
                errors.append("%s TemperatureV[%d] successful value is missing" % (label, index))
            elif code != 0 and item.get("temperature_c") is not None:
                errors.append("%s TemperatureV[%d] failed value is not null" % (label, index))

    settings = record.get("thermal_settings")
    if isinstance(settings, dict) and settings.get("status") == "symbol-absent":
        errors.extend(_exact_keys(settings, {"status"},
                                  "%s thermal-settings absence" % label))
    elif not isinstance(settings, dict):
        errors.append("%s direct NVML thermal settings are malformed" % label)
    else:
        errors.extend(_exact_keys(settings, {
            "sensor_index_requested", "return", "reported_count",
            "maximum_count", "malformed_count", "sensors", "call_elapsed_us",
        }, "%s thermal settings" % label))
        code = _nvml_return_code(settings.get("return"))
        sensors = settings.get("sensors")
        errors.extend(_validate_nvml_return(
            settings.get("return"), "%s thermal settings" % label
        ))
        if not _is_int(settings.get("sensor_index_requested"), 15, 15):
            errors.append("%s thermal-settings selector is malformed" % label)
        if not _valid_nonnegative_int(settings.get("call_elapsed_us")):
            errors.append("%s thermal-settings latency is malformed" % label)
        if code is None or not isinstance(sensors, list):
            errors.append("%s direct NVML thermal settings return is malformed" % label)
        elif code == 0:
            count = settings.get("reported_count")
            if (not isinstance(count, int) or isinstance(count, bool) or
                    not 0 <= count <= 3 or
                    not _is_int(settings.get("maximum_count"), 3, 3) or
                    settings.get("malformed_count") is not False or
                    len(sensors) != count):
                errors.append("%s successful thermal-settings count is malformed" % label)
            for index, sensor in enumerate(sensors):
                errors.extend(_exact_keys(sensor, {
                    "index", "controller", "target", "default_minimum_c",
                    "default_maximum_c", "current_temperature_c",
                }, "%s thermal sensor[%d]" % (label, index)))
                if (not isinstance(sensor, dict) or
                        not _is_int(sensor.get("index"), index, index) or
                        not isinstance(sensor.get("controller"), dict) or
                        not isinstance(sensor.get("target"), dict) or
                        not isinstance(sensor.get("controller", {}).get("code"), int) or
                        isinstance(sensor.get("controller", {}).get("code"), bool) or
                        not isinstance(sensor.get("target", {}).get("code"), int) or
                        isinstance(sensor.get("target", {}).get("code"), bool)):
                    errors.append("%s thermal sensor[%d] metadata is malformed" % (label, index))
                    continue
                controller = sensor["controller"]
                target = sensor["target"]
                errors.extend(_exact_keys(
                    controller, {"code", "name"},
                    "%s thermal sensor[%d] controller" % (label, index)))
                errors.extend(_exact_keys(
                    target, {"code", "name"},
                    "%s thermal sensor[%d] target" % (label, index)))
                if (controller["code"] not in NVML_THERMAL_CONTROLLER_NAMES or
                        controller.get("name") !=
                        NVML_THERMAL_CONTROLLER_NAMES.get(controller["code"])):
                    errors.append("%s thermal sensor[%d] controller name mismatch" % (label, index))
                if (target["code"] not in NVML_THERMAL_TARGET_NAMES or
                        target.get("name") !=
                        NVML_THERMAL_TARGET_NAMES.get(target["code"])):
                    errors.append("%s thermal sensor[%d] target name mismatch" % (label, index))
                temperatures_c = [
                    sensor.get("default_minimum_c"),
                    sensor.get("default_maximum_c"),
                    sensor.get("current_temperature_c"),
                ]
                if not all(isinstance(value, int) and not isinstance(value, bool) and
                           _is_finite_number(value, minimum=-273.15,
                                             maximum=1000.0)
                           for value in temperatures_c):
                    errors.append("%s thermal sensor[%d] value is malformed" % (label, index))
        elif (not _is_int(settings.get("maximum_count"), 3, 3) or
              settings.get("reported_count") is not None or
              settings.get("malformed_count") not in (False, None) or sensors):
            errors.append("%s failed thermal-settings payload is not null/empty" % label)

    margin = record.get("margin_temperature")
    if isinstance(margin, dict) and margin.get("status") == "symbol-absent":
        errors.extend(_exact_keys(margin, {"status"},
                                  "%s margin absence" % label))
    elif not isinstance(margin, dict):
        errors.append("%s direct NVML margin is malformed" % label)
    else:
        errors.extend(_exact_keys(margin, {
            "return", "margin_temperature_c", "call_elapsed_us",
            "structure_version",
        }, "%s margin" % label))
        code = _nvml_return_code(margin.get("return"))
        errors.extend(_validate_nvml_return(
            margin.get("return"), "%s margin" % label
        ))
        if not _valid_nonnegative_int(margin.get("call_elapsed_us")):
            errors.append("%s direct NVML margin latency is malformed" % label)
        if margin.get("structure_version") != "0x01000008":
            errors.append("%s direct NVML margin version is malformed" % label)
        if code is None:
            errors.append("%s direct NVML margin return is malformed" % label)
        elif code == 0 and (not isinstance(
                margin.get("margin_temperature_c"), int) or
                isinstance(margin.get("margin_temperature_c"), bool) or
                not _is_finite_number(margin.get("margin_temperature_c"),
                                      minimum=-1000.0, maximum=1000.0)):
            errors.append("%s successful margin payload is missing" % label)
        elif code != 0 and margin.get("margin_temperature_c") is not None:
            errors.append("%s failed margin payload is not null" % label)

    thresholds = record.get("temperature_thresholds")
    if isinstance(thresholds, dict) and thresholds.get("status") == "symbol-absent":
        errors.extend(_exact_keys(thresholds, {"status"},
                                  "%s threshold absence" % label))
    elif not isinstance(thresholds, list) or len(thresholds) != 8:
        errors.append("%s direct NVML threshold census is malformed" % label)
    else:
        expected_thresholds = (
            "shutdown", "slowdown", "memory_max", "gpu_max",
            "acoustic_min", "acoustic_current", "acoustic_max", "gps_current",
        )
        for index, item in enumerate(thresholds):
            code = _nvml_return_code(item.get("return") if isinstance(item, dict) else None)
            errors.extend(_exact_keys(item, {
                "threshold_type", "name", "return", "temperature_c",
                "call_elapsed_us",
            }, "%s threshold[%d]" % (label, index)))
            if (not isinstance(item, dict) or
                    not _is_int(item.get("threshold_type"), index, index) or
                    item.get("name") != expected_thresholds[index]):
                errors.append("%s threshold[%d] metadata mismatch" % (label, index))
                continue
            errors.extend(_validate_nvml_return(
                item.get("return"), "%s threshold[%d]" % (label, index)
            ))
            if not _valid_nonnegative_int(item.get("call_elapsed_us")):
                errors.append("%s threshold[%d] latency is malformed" % (label, index))
            if code is None:
                errors.append("%s threshold[%d] return is malformed" % (label, index))
            elif code == 0 and (not isinstance(item.get("temperature_c"), int) or
                    isinstance(item.get("temperature_c"), bool) or
                    not _is_finite_number(item.get("temperature_c"),
                                          minimum=0.0, maximum=1000.0)):
                errors.append("%s threshold[%d] successful value is missing" % (label, index))
            elif code != 0 and item.get("temperature_c") is not None:
                errors.append("%s threshold[%d] failed value is not null" % (label, index))
    return errors


def _valid_timezone_timestamp(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_fallback_temperature_card(record, expected_bdf, summary, label):
    errors = []
    if not isinstance(record, dict):
        return ["%s fallback record is missing" % label]
    errors.extend(_exact_keys(record, {
        "index", "bdf", "name", "gpu_temperature_c",
        "memory_temperature_c", "utilization_percent", "power_w", "queries",
    }, "%s fallback record" % label))
    raw_bdf = record.get("bdf")
    raw_bdf_match = (
        NVIDIA_SMI_BDF_RE.fullmatch(raw_bdf)
        if isinstance(raw_bdf, str) else None
    )
    try:
        observed_bdf = normalize_bdf(raw_bdf)
    except DiagnosticError:
        observed_bdf = None
    if raw_bdf_match is None or observed_bdf != expected_bdf:
        errors.append("%s fallback BDF mismatch" % label)
    index = record.get("index")
    if index is not None and not _valid_nonnegative_int(index):
        errors.append("%s fallback index is malformed" % label)
    if not isinstance(record.get("name"), str) or not record["name"]:
        errors.append("%s fallback device name is malformed" % label)
    queries = record.get("queries")
    query_specs = {
        "gpu_temperature_c": ("temperature", "celsius", -273.15, 1000.0),
        "memory_temperature_c": ("temperature", "celsius", -273.15, 1000.0),
        "utilization_percent": ("percentage", "percent", 0.0, 100.0),
        "power_w": ("power", "watt", 0.0, None),
    }
    if not isinstance(queries, dict) or set(queries) != set(query_specs):
        return errors + ["%s fallback query set is malformed" % label]
    started_utc = summary.get("started_utc") if isinstance(summary, dict) else None
    latency_us = summary.get("latency_us") if isinstance(summary, dict) else None
    unavailable = frozenset((
        "n/a", "na", "not supported", "[not supported]", "",
    ))
    for key, (value_type, unit, minimum, maximum) in query_specs.items():
        query = queries.get(key)
        query_label = "%s fallback %s" % (label, key)
        if not isinstance(query, dict):
            errors.append("%s is malformed" % query_label)
            continue
        errors.extend(_exact_keys(query, {
            "return_status", "raw", "value", "value_type", "unit",
            "timestamp_utc", "latency_us", "latency_scope",
            "canonical_nvml_api",
        }, query_label))
        raw = query.get("raw")
        if not isinstance(raw, str):
            errors.append("%s raw token is malformed" % query_label)
            continue
        try:
            parsed = float(raw.strip())
            if not math.isfinite(parsed):
                raise ValueError("non-finite")
            expected_status = "success"
            expected_value = parsed
        except ValueError:
            expected_status = (
                "not-available" if raw.strip().lower() in unavailable
                else "parse-failed"
            )
            expected_value = None
        if query.get("return_status") != expected_status:
            errors.append("%s return status does not match raw" % query_label)
        if (not _strict_equal(query.get("value"), expected_value) or
                not _strict_equal(record.get(key), expected_value)):
            errors.append("%s value/mirror does not match raw" % query_label)
        if (query.get("value_type") != value_type or
                query.get("unit") != unit or
                query.get("timestamp_utc") != started_utc or
                not _is_int(query.get("latency_us"), latency_us, latency_us) or
                query.get("latency_scope") != "whole-nvidia-smi-command" or
                query.get("canonical_nvml_api") is not False):
            errors.append("%s metadata mismatch" % query_label)
        if (expected_value is not None and
                not _is_finite_number(expected_value, minimum=minimum,
                                      maximum=maximum)):
            errors.append("%s value is outside bounds" % query_label)
    return errors


def _validate_hwmon_records(records, label):
    errors = []
    if not isinstance(records, list):
        return ["%s hwmon census is not an array" % label]
    seen_hwmon = set()
    for index, record in enumerate(records):
        record_label = "%s hwmon[%d]" % (label, index)
        if not isinstance(record, dict):
            errors.append("%s is malformed" % record_label)
            continue
        errors.extend(_exact_keys(
            record, {"hwmon", "name", "channels"}, record_label
        ))
        hwmon = record.get("hwmon")
        name = record.get("name")
        channels = record.get("channels")
        if (not isinstance(hwmon, str) or
                re.fullmatch(r"hwmon[0-9]+", hwmon) is None or
                hwmon in seen_hwmon):
            errors.append("%s identity is malformed/duplicate" % record_label)
        else:
            seen_hwmon.add(hwmon)
        if name is not None and not isinstance(name, str):
            errors.append("%s name is malformed" % record_label)
        if not isinstance(channels, list):
            errors.append("%s channels are malformed" % record_label)
            continue
        seen_inputs = set()
        for channel_index, channel in enumerate(channels):
            channel_label = "%s channel[%d]" % (record_label, channel_index)
            if not isinstance(channel, dict):
                errors.append("%s is malformed" % channel_label)
                continue
            errors.extend(_exact_keys(channel, {
                "input", "label", "raw_millicelsius", "temperature_c",
            }, channel_label))
            input_name = channel.get("input")
            if (not isinstance(input_name, str) or
                    re.fullmatch(r"temp[0-9]+_input", input_name) is None or
                    input_name in seen_inputs):
                errors.append("%s input is malformed/duplicate" % channel_label)
            else:
                seen_inputs.add(input_name)
            if (channel.get("label") is not None and
                    not isinstance(channel.get("label"), str)):
                errors.append("%s label is malformed" % channel_label)
            raw = channel.get("raw_millicelsius")
            temperature = channel.get("temperature_c")
            if raw is None:
                if temperature is not None:
                    errors.append("%s null raw value has a temperature" % channel_label)
            elif (not isinstance(raw, int) or isinstance(raw, bool) or
                  not isinstance(temperature, float) or
                  not _is_finite_number(temperature, minimum=-273.15,
                                        maximum=1000.0) or
                  temperature != raw / 1000.0):
                errors.append("%s raw/temperature decode mismatch" % channel_label)
    return errors


def _temperature_population(mask, disabled_gpcs):
    return [
        {"gpc": gpc, "bjt": bjt}
        for gpc in range(8) if not (disabled_gpcs & (1 << gpc))
        for bjt in range(3) if not (mask & (1 << (3 * gpc + bjt)))
    ]


def _validate_fbio_population(population, label):
    errors = []
    if not isinstance(population, dict):
        return ["%s FBIO population report is missing" % label], None
    errors.extend(_exact_keys(population, {
        "status_opt_fbio", "source_offset", "available",
        "enabled_fbpa_indices", "disabled_fbpa_indices", "fbpa_enabled", "rule",
    }, "%s FBIO population" % label))
    raw = _parse_hex_u32(population.get("status_opt_fbio"))
    available = (
        raw is not None and raw != 0xFFFFFFFF and
        (raw & 0xFFFF0000) != 0xBADF0000
    )
    enabled = ([index for index in range(24) if not raw & (1 << index)]
               if available else None)
    disabled = ([index for index in range(24) if raw & (1 << index)]
                if available else None)
    fbpa_enabled = ([not bool(raw & (1 << fbpa))
                     for fbpa in range(24)] if available else None)
    if (population.get("source_offset") !=
            "0x%08x" % TEMPERATURE_STATUS_OPT_FBIO or
            population.get("available") is not available or
            not _strict_equal(population.get("enabled_fbpa_indices"), enabled) or
            not _strict_equal(population.get("disabled_fbpa_indices"), disabled) or
            not _strict_equal(population.get("fbpa_enabled"), fbpa_enabled) or
            population.get("rule") != (
                "FBPA n is enabled only when STATUS_OPT_FBIO bit n is clear"
            )):
        errors.append("%s FBIO population decode mismatch" % label)
    return errors, fbpa_enabled


def _validate_temperature_population(population, label, observed=None):
    errors = []
    if not isinstance(population, dict):
        return ["%s population report is missing" % label], None
    errors.extend(_exact_keys(population, {
        "opt_ts_dis_mask", "status_opt_gpc", "source_offsets",
        "opt_ts_dis_reserved_zero", "mask_reads_available",
        "expected_enabled_endpoints", "expected_enabled_count",
        "observed_valid_endpoints", "observed_valid_count",
        "observed_matches_population_masks", "rule",
    }, "%s population" % label))
    mask = _parse_hex_u32(population.get("opt_ts_dis_mask"))
    disabled = _parse_hex_u32(population.get("status_opt_gpc"))
    mask_reserved_zero = mask is not None and (mask & ~0x00FFFFFF) == 0
    available = (
        mask is not None and disabled is not None and
        mask_reserved_zero and
        (mask & 0xFFFF0000) != 0xBADF0000 and
        (disabled & 0xFFFF0000) != 0xBADF0000 and
        mask != 0xFFFFFFFF and disabled != 0xFFFFFFFF
    )
    if (population.get("mask_reads_available") is not available or
            population.get("opt_ts_dis_reserved_zero") is not
            mask_reserved_zero):
        errors.append("%s population availability mismatch" % label)
    if population.get("source_offsets") != {
            "opt_ts_dis_mask": "0x%08x" % TEMPERATURE_OPT_TS_DIS_MASK,
            "status_opt_gpc": "0x%08x" % TEMPERATURE_STATUS_OPT_GPC,
    }:
        errors.append("%s population source offsets mismatch" % label)
    if population.get("rule") != TEMPERATURE_POPULATION_RULE:
        errors.append("%s population rule mismatch" % label)
    if not available:
        errors.append("%s population mask reads are unavailable" % label)
        if (population.get("expected_enabled_endpoints") is not None or
                population.get("expected_enabled_count") is not None or
                population.get("observed_matches_population_masks") is not None):
            errors.append("%s unavailable population fields are not null" % label)
        return errors, None
    expected = _temperature_population(mask, disabled)
    if (not _strict_equal(population.get("expected_enabled_endpoints"), expected) or
            not _is_int(population.get("expected_enabled_count"),
                        len(expected), len(expected))):
        errors.append("%s population mask decode is inconsistent" % label)
    if observed is None:
        if any(population.get(key) is not None for key in (
                "observed_valid_endpoints", "observed_valid_count",
                "observed_matches_population_masks")):
            errors.append("%s pure population report fabricates observations" % label)
    elif (not _strict_equal(population.get("observed_valid_endpoints"), observed) or
          not _is_int(population.get("observed_valid_count"),
                      len(observed), len(observed)) or
          population.get("observed_matches_population_masks") is not True or
          not _strict_equal(observed, expected)):
        errors.append("%s observed population does not match masks" % label)
    return errors, expected


def _validate_direct_nvml_summary(summary):
    """Validate the status-discriminated summary after its card map is stripped."""
    errors = []
    if not isinstance(summary, dict):
        return ["direct NVML summary is not an object"], None, None
    status = summary.get("status")
    if status not in ("ok", "partial", "unavailable", "disabled"):
        return ["direct NVML runtime status is invalid"], status, None
    if "cards" in summary:
        errors.append("direct NVML summary contains forbidden card payload")
    if status == "disabled":
        if set(summary) != {"status"}:
            errors.append("disabled direct NVML summary contains live-query fields")
        return errors, status, None
    library = summary.get("library")
    load_errors = summary.get("library_load_errors")
    if (library is not None and
            (not isinstance(library, str) or not library)):
        errors.append("direct NVML library identity is malformed")
    if (not isinstance(load_errors, list) or any(
            not isinstance(item, dict) or set(item) != {"candidate", "error"} or
            not isinstance(item.get("candidate"), str) or not item["candidate"] or
            not isinstance(item.get("error"), str) or not item["error"]
            for item in load_errors)):
        errors.append("direct NVML library-load errors are malformed")
    if status == "unavailable":
        variants = (
            {"status", "library", "library_load_errors"},
            {"status", "library", "library_load_errors", "configuration_error"},
            {"status", "library", "library_load_errors", "initialization_return"},
        )
        if set(summary) not in variants:
            errors.append("unavailable direct NVML summary shape is invalid")
        if set(summary) == {"status", "library", "library_load_errors"}:
            if library is not None or not load_errors:
                errors.append(
                    "NVML load failure requires no library and nonempty load errors"
                )
        elif "configuration_error" in summary:
            if (not isinstance(library, str) or not library or
                    not isinstance(summary.get("configuration_error"), str) or
                    not summary.get("configuration_error")):
                errors.append("direct NVML configuration error is malformed")
        elif "initialization_return" in summary:
            initialization = summary.get("initialization_return")
            if (not isinstance(library, str) or not library or
                    _validate_nvml_return(initialization,
                                      "direct NVML initialization") or
                    _nvml_return_code(initialization) in (0, 5)):
                errors.append("unavailable direct NVML initialization is malformed")
        return errors, status, None
    expected_keys = {
        "status", "library", "library_load_errors", "initialization_return",
        "shutdown_return", "structure_sizes", "function_availability",
        "interpretation",
    }
    if set(summary) != expected_keys:
        errors.append("successful direct NVML summary shape is invalid")
    if not isinstance(library, str) or not library:
        errors.append("direct NVML runtime metadata is malformed")
    if not _strict_equal(summary.get("structure_sizes"), {
            "field_value": 40, "temperature_v1": 12,
            "thermal_settings": 64, "margin_temperature_v1": 8,
    }):
        errors.append("direct NVML ctypes structure sizes mismatch")
    availability = summary.get("function_availability")
    availability_keys = {
        "field_values", "temperature_v", "thermal_settings",
        "margin_temperature", "temperature_thresholds",
    }
    if (not isinstance(availability, dict) or
            set(availability) != availability_keys or
            any(not isinstance(value, bool) for value in availability.values())):
        errors.append("direct NVML function availability is malformed")
        availability = None
    initialization = summary.get("initialization_return")
    shutdown = summary.get("shutdown_return")
    init_code = _nvml_return_code(initialization)
    if (init_code not in (0, 5) or
            _validate_nvml_return(initialization,
                                  "direct NVML initialization")):
        errors.append("direct NVML initialization return is malformed")
    if init_code == 0:
        if (_nvml_return_code(shutdown) != 0 or
                _validate_nvml_return(shutdown, "direct NVML shutdown")):
            errors.append("direct NVML owned shutdown return is malformed")
    elif init_code == 5 and not _strict_equal(shutdown, {
            "code": None,
            "name": "not-called-library-was-already-initialized",
    }):
        errors.append("direct NVML borrowed initialization shutdown marker is malformed")
    if not _strict_equal(summary.get("interpretation"), {
            "query_only_public_api": True,
            "driver_firmware_internal_side_effects": "opaque",
            "unsupported_values": "null-with-exact-return-code",
            "thermal_settings_slots_are_not_gpc_bjt_endpoints": True,
    }):
        errors.append("direct NVML interpretation metadata is malformed")
    return errors, status, availability


def _validate_fallback_summary(summary):
    """Validate a status-discriminated nvidia-smi summary without its card map."""
    errors = []
    if not isinstance(summary, dict):
        return ["fallback nvidia-smi summary is not an object"], None
    status = summary.get("status")
    if status not in ("ok", "failed", "unavailable", "disabled"):
        return ["fallback nvidia-smi runtime status is invalid"], status
    if "cards" in summary:
        errors.append("fallback nvidia-smi summary contains forbidden card payload")
    if status == "disabled":
        if set(summary) != {"status"}:
            errors.append("disabled fallback summary contains live-query fields")
        return errors, status
    required = {"status", "backend", "canonical", "started_utc", "latency_us"}
    if status == "failed":
        required.update(("returncode", "stderr"))
    elif status == "unavailable":
        required.add("error")
    if set(summary) != required:
        errors.append("fallback nvidia-smi status-specific summary shape is invalid")
    if (summary.get("backend") != "nvidia-smi-query-gpu" or
            summary.get("canonical") is not False or
            not _valid_timezone_timestamp(summary.get("started_utc")) or
            not _valid_nonnegative_int(summary.get("latency_us"))):
        errors.append("fallback nvidia-smi summary metadata is malformed")
    if status == "failed" and (
            not _is_int(summary.get("returncode")) or
            summary.get("returncode") == 0 or
            not isinstance(summary.get("stderr"), str)):
        errors.append("failed fallback nvidia-smi result is malformed")
    if status == "unavailable" and (
            not isinstance(summary.get("error"), str) or
            not summary.get("error")):
        errors.append("unavailable fallback nvidia-smi error is malformed")
    return errors, status


def validate_temperature_document(document, cards, args,
                                  expected_component_identity=None):
    errors = []
    if not isinstance(document, dict):
        return ["temperature document is not an object"]
    expected_document_keys = {
        "schema", "version", "status", "captured_utc", "access",
        "probe_backend", "probe_device", "bound_snapshot_acknowledged",
        "selector_write_acknowledged",
        "hbm_data_read_to_advance_acknowledged", "active_workload_override",
        "max_utilization_percent", "source_components", "driver_query_status",
        "driver_temperature_census", "cards", "interpretation",
    }
    if set(document) != expected_document_keys:
        errors.append("temperature document top-level shape is invalid")
    errors.extend(_validate_temperature_declared_keys(document))
    errors.extend(_validate_temperature_boolean_types(document))
    if document.get("schema") != TEMPERATURE_SCHEMA:
        errors.append("temperature schema mismatch")
    if document.get("version") != TEMPERATURE_VERSION:
        errors.append("temperature version mismatch")
    if not _valid_timezone_timestamp(document.get("captured_utc")):
        errors.append("temperature capture timestamp is invalid")
    if document.get("access") != args.temperature_access:
        errors.append("temperature access mismatch")
    if document.get("status") != "pass":
        errors.append("temperature probe status is not pass")
    backend = document.get("probe_backend")
    if backend not in ("xray-therm", "gpcprobe"):
        errors.append("temperature probe backend is invalid")
    if args.temperature_access == "semantic-read" and backend != "xray-therm":
        errors.append("semantic temperature access requires xray-therm backend")
    requested_backend = getattr(args, "temperature_probe_backend", "auto")
    if requested_backend != "auto" and backend != requested_backend:
        errors.append("temperature probe backend does not match request")
    requested_device = getattr(args, "temperature_probe_device", "auto")
    expected_device = requested_device if requested_device != "auto" else {
        "xray-therm": "/dev/170hx_xray_therm",
        "gpcprobe": "/dev/gpcprobe",
    }.get(backend)
    if document.get("probe_device") != expected_device:
        errors.append("temperature probe-device provenance mismatch")
    expected_bound_ack = bool(getattr(
        args, "temperature_acknowledge_bound_snapshot", False
    ))
    if document.get("bound_snapshot_acknowledged") is not expected_bound_ack:
        errors.append("bound-snapshot acknowledgement mismatch")
    expected_selector_ack = bool(getattr(
        args, "temperature_acknowledge_selector_write", False
    ))
    if document.get("selector_write_acknowledged") is not expected_selector_ack:
        errors.append("selector-write acknowledgement mismatch")
    expected_hbm_ack = bool(getattr(
        args, "temperature_acknowledge_hbm_data_read_to_advance", False
    ))
    if (document.get("hbm_data_read_to_advance_acknowledged") is not
            expected_hbm_ack):
        errors.append("HBM DATA read-to-advance acknowledgement mismatch")
    if args.temperature_access == "semantic-read":
        if not expected_selector_ack:
            errors.append(
                "semantic temperature access lacks selector-write acknowledgement"
            )
        if backend != "xray-therm":
            errors.append("semantic acknowledgements require xray-therm backend")
    elif expected_selector_ack or expected_bound_ack or expected_hbm_ack:
        errors.append("bar-read cannot carry semantic acknowledgements")
    if ((expected_bound_ack or expected_hbm_ack) and
            (args.temperature_access != "semantic-read" or
             backend != "xray-therm")):
        errors.append(
            "stateful temperature acknowledgements require semantic xray-therm"
        )
    if (backend == "gpcprobe" and
            (args.temperature_access != "bar-read" or expected_selector_ack or
             expected_bound_ack or expected_hbm_ack)):
        errors.append("legacy gpcprobe must remain unacknowledged bar-read")
    expected_workload_override = bool(getattr(
        args, "temperature_allow_active_workload", False
    ))
    if document.get("active_workload_override") is not expected_workload_override:
        errors.append("active-workload override mismatch")
    expected_max_utilization = float(getattr(
        args, "temperature_max_utilization", 5.0
    ))
    observed_max_utilization = document.get("max_utilization_percent")
    if (not isinstance(observed_max_utilization, float) or
            not math.isfinite(observed_max_utilization) or
            float(observed_max_utilization) != expected_max_utilization):
        errors.append("temperature workload threshold mismatch")
    if not _strict_equal(document.get("interpretation"),
                         TEMPERATURE_INTERPRETATION):
        errors.append("temperature interpretation contract mismatch")

    source_components = document.get("source_components")
    expected_loaded = {
        "temperature_probe": True,
        "xray_therm": backend == "xray-therm",
        "nvml_temperature": not bool(getattr(
            args, "temperature_no_driver_queries", False
        )),
    }
    component_filenames = {
        "temperature_probe": "temperature_probe.py",
        "xray_therm": "xray_therm.py",
        "nvml_temperature": "nvml_temperature.py",
    }
    if not isinstance(source_components, dict):
        errors.append("temperature source-component identity is missing")
    else:
        if set(source_components) != set(component_filenames):
            errors.append("temperature source-component set is invalid")
        directory = Path(__file__).resolve().parent
        for name, filename in component_filenames.items():
            component = source_components.get(name)
            path = directory / filename
            expected_scope = (
                "current-entry-script-file" if name == "temperature_probe"
                else "compiled-byte-buffer" if expected_loaded[name]
                else "current-unloaded-file"
            )
            if (not isinstance(component, dict) or
                    set(component) != {
                        "filename", "path", "sha256", "loaded",
                        "identity_scope",
                    } or
                    component.get("filename") != filename or
                    component.get("path") != str(path) or
                    component.get("loaded") is not expected_loaded[name] or
                    component.get("identity_scope") != expected_scope or
                    not path.is_file() or
                    component.get("sha256") != sha256_file(path)):
                errors.append("temperature component identity mismatch: %s" % name)
            if expected_component_identity is not None:
                before = expected_component_identity.get(name)
                if (not isinstance(before, dict) or
                        before.get("path") != str(path) or
                        before.get("sha256") != component.get("sha256")):
                    errors.append(
                        "temperature component changed during capture: %s" % name
                    )
    census = document.get("driver_temperature_census")
    if (not isinstance(census, dict) or
            set(census) != {"contract", "direct_nvml", "fallback_command"}):
        errors.append("driver temperature census shape is invalid")
    census_contract = census.get("contract") if isinstance(census, dict) else None
    if not _strict_equal(census_contract, TEMPERATURE_DRIVER_CENSUS_CONTRACT):
        errors.append("driver temperature census contract mismatch")
    direct_summary = census.get("direct_nvml") if isinstance(census, dict) else None
    direct_errors, direct_status, direct_availability = (
        _validate_direct_nvml_summary(direct_summary)
    )
    errors.extend(direct_errors)
    fallback_summary = (
        census.get("fallback_command") if isinstance(census, dict) else None
    )
    fallback_errors, fallback_status = _validate_fallback_summary(
        fallback_summary
    )
    errors.extend(fallback_errors)
    query_status = document.get("driver_query_status")
    if (not isinstance(query_status, dict) or
            set(query_status) != {"direct_nvml", "nvidia_smi_fallback"} or
            query_status.get("direct_nvml") != direct_status or
            query_status.get("nvidia_smi_fallback") != fallback_status or
            fallback_status not in (
                "ok", "failed", "unavailable", "disabled"
            )):
        errors.append("driver temperature query status is inconsistent")
    driver_queries_disabled = bool(getattr(
        args, "temperature_no_driver_queries", False
    ))
    if driver_queries_disabled:
        if direct_status != "disabled" or fallback_status != "disabled":
            errors.append("disabled driver-query mode has live query status")
    elif direct_status == "disabled" or fallback_status == "disabled":
        errors.append("enabled driver-query mode reports disabled query status")
    if (args.temperature_access == "semantic-read" and
            not expected_workload_override and
            (not isinstance(query_status, dict) or
             query_status.get("nvidia_smi_fallback") != "ok")):
        errors.append("semantic workload gate lacks a successful driver query")

    expected_bdfs = {card["bdf"] for card in cards}
    outer_cards_by_bdf = {card["bdf"]: card for card in cards}
    records = document.get("cards")
    if not isinstance(records, list):
        return errors + ["temperature cards is not an array"]
    seen_bdfs = set()
    seen_probe_indices = set()
    last_probe_index = -1
    helper_generations = set()
    for position, record in enumerate(records):
        label = "temperature cards[%d]" % position
        if not isinstance(record, dict):
            errors.append("%s is not an object" % label)
            continue
        expected_record_keys = {
            "identity", "driver_temperature_census", "hwmon", "pure",
        }
        if args.temperature_access == "semantic-read":
            expected_record_keys.add("global_snapshot")
        if set(record) != expected_record_keys:
            errors.append("%s object shape is invalid" % label)
        identity = record.get("identity")
        try:
            bdf = normalize_bdf(identity.get("bdf") if isinstance(identity, dict) else None)
        except DiagnosticError:
            bdf = None
            errors.append("%s identity BDF is invalid" % label)
        if (bdf is not None and isinstance(identity, dict) and
                identity.get("bdf") != bdf):
            errors.append("%s identity BDF is not canonical lowercase" % label)
        if bdf in seen_bdfs:
            errors.append("duplicate temperature BDF: %s" % bdf)
        if bdf is not None:
            seen_bdfs.add(bdf)
        outer_card = outer_cards_by_bdf.get(bdf, {})
        if isinstance(identity, dict):
            expected_identity_keys = {
                "bdf", "domain", "bus", "device", "function", "vendor_id",
                "device_id", "subsystem_vendor_id", "subsystem_device_id",
                "revision", "driver", "probe_index",
            }
            if backend == "xray-therm":
                expected_identity_keys.add("helper_identity")
            if set(identity) != expected_identity_keys:
                errors.append("%s identity shape is invalid" % label)
            bdf_match = BDF_RE.fullmatch(bdf) if bdf is not None else None
            expected_components = ({
                "domain": int(bdf_match.group("domain"), 16),
                "bus": int(bdf_match.group("bus"), 16),
                "device": int(bdf_match.group("device"), 16),
                "function": int(bdf_match.group("function"), 16),
            } if bdf_match else {})
            if any(not _is_int(identity.get(key), value, value)
                   for key, value in expected_components.items()):
                errors.append("%s identity BDF components are inconsistent" % label)
            if (re.fullmatch(r"0x[0-9a-f]{4}",
                             str(identity.get("vendor_id", ""))) is None or
                    re.fullmatch(r"0x[0-9a-f]{4}",
                                 str(identity.get("device_id", ""))) is None or
                    re.fullmatch(r"0x[0-9a-f]{4}", str(
                        identity.get("subsystem_vendor_id", ""))) is None or
                    re.fullmatch(r"0x[0-9a-f]{4}", str(
                        identity.get("subsystem_device_id", ""))) is None or
                    re.fullmatch(r"0x[0-9a-f]{2}",
                                 str(identity.get("revision", ""))) is None or
                    (identity.get("driver") is not None and
                     not isinstance(identity.get("driver"), str))):
                errors.append("%s identity PCI fields are not canonical" % label)
            probe_index = identity.get("probe_index")
            if (not _is_int(probe_index, 0) or
                    probe_index in seen_probe_indices or
                    probe_index <= last_probe_index or
                    probe_index != position):
                errors.append("%s probe index is malformed, duplicate, or unordered" % label)
            else:
                seen_probe_indices.add(probe_index)
                last_probe_index = probe_index
            for inner_key, outer_key in (
                    ("vendor_id", "vendor"), ("device_id", "device"),
                    ("subsystem_vendor_id", "subsystem_vendor"),
                    ("subsystem_device_id", "subsystem_device"),
                    ("driver", "driver")):
                if (outer_key in outer_card and
                        identity.get(inner_key) != outer_card.get(outer_key)):
                    errors.append("%s identity does not match selected card: %s" %
                                  (label, inner_key))
        if backend == "xray-therm" and (
                not isinstance(identity, dict) or
                identity.get("vendor_id") != "0x10de" or
                identity.get("device_id") not in (
                    "0x20c2", "0x20b5", "0x2080")):
            errors.append("%s dedicated-helper GA100 identity is invalid" % label)

        per_card_census = record.get("driver_temperature_census")
        if not isinstance(per_card_census, dict):
            errors.append("%s driver census is missing" % label)
        else:
            if set(per_card_census) != {
                    "canonical_direct_nvml", "fallback_nvidia_smi"}:
                errors.append("%s driver census shape is invalid" % label)
            direct_card = per_card_census.get("canonical_direct_nvml")
            fallback_card = per_card_census.get("fallback_nvidia_smi")
            if direct_status in ("ok", "partial"):
                errors.extend(_validate_direct_nvml_card(
                    direct_card, bdf, label, direct_availability
                ))
            elif direct_card is not None:
                errors.append(
                    "%s direct NVML payload exists while census is unavailable" % label
                )
            if driver_queries_disabled:
                if direct_card is not None or fallback_card is not None:
                    errors.append("%s disabled driver query has a payload" % label)
            elif fallback_status == "ok" and not isinstance(fallback_card, dict):
                errors.append("%s successful fallback query lacks a payload" % label)
            elif fallback_status == "ok":
                errors.extend(_validate_fallback_temperature_card(
                    fallback_card, bdf, fallback_summary, label
                ))
            elif fallback_card is not None:
                errors.append("%s failed/unavailable fallback has a payload" % label)
            if (args.temperature_access == "semantic-read" and
                    not expected_workload_override):
                utilization = (
                    fallback_card.get("utilization_percent")
                    if isinstance(fallback_card, dict) else None
                )
                if (isinstance(utilization, bool) or
                        not isinstance(utilization, (int, float)) or
                        not math.isfinite(utilization)):
                    errors.append("%s workload utilization is unavailable" % label)
                elif not 0.0 <= utilization <= 100.0:
                    errors.append("%s workload utilization is outside [0, 100]" % label)
                elif utilization > expected_max_utilization:
                    errors.append("%s workload utilization exceeds gate" % label)
        errors.extend(_validate_hwmon_records(record.get("hwmon"), label))
        pure = record.get("pure")
        if not isinstance(pure, dict):
            errors.append("%s pure read object is missing" % label)
            continue
        legacy_pure_keys = {
            "access", "writes_issued", "hbm_data_read_mask",
            "hbm_data_ports_observed", "selector_dependent_skipped_mask",
            "selector_dependent_read_policy",
            "selector_dependent_reads_unconditionally_omitted",
            "prewrite_liveness", "aggregate_readouts",
            "fixed_context_registers", "gpu_sci", "gpc_analog_context",
            "logical_hardware_backed_endpoint_census", "gpc_autopoll_controls",
            "currently_selected_gpc_readout", "snapshot_as_found",
            "fbpa_readout_channels", "hbm_stack_groups", "fbio_population",
            "hbm_transaction_policy", "sentinels", "population", "status",
            "error", "backend", "evidence_authority",
            "transaction_journal_available", "postflight_identity_verified",
            "dead_bar_sentinel_check", "reads_issued", "read_count_contract",
        }
        helper_pure_keys = {
            "access", "writes_issued", "hbm_data_read_mask",
            "hbm_data_ports_observed", "selector_dependent_skipped_mask",
            "selector_dependent_read_policy",
            "selector_dependent_reads_unconditionally_omitted",
            "prewrite_liveness", "aggregate_readouts",
            "fixed_context_registers", "gpu_sci", "gpc_analog_context",
            "logical_hardware_backed_endpoint_census", "gpc_autopoll_controls",
            "currently_selected_gpc_readout", "snapshot_as_found",
            "fbpa_readout_channels", "hbm_stack_groups", "fbio_population",
            "hbm_transaction_policy", "sentinels", "population", "status",
            "error", "backend", "helper_status", "reads_issued",
            "pure_completed", "autopoll_unchanged", "sentinels_unchanged",
            "transaction_total_writes", "trigger_poll_reads",
            "read_count_contract", "hbm_data_requested",
            "selector_dependent_omit_count", "driver_bound_at_entry",
            "fbpa_context_atomic", "fbpa_context_warning",
            "fresh_prewrite_liveness", "fresh_prewrite_liveness_scope",
        }
        errors.extend(_exact_keys(
            pure, helper_pure_keys if backend == "xray-therm" else legacy_pure_keys,
            "%s pure read" % label
        ))
        if (pure.get("access") != "bar-read" or
                not _is_int(pure.get("writes_issued"), 0, 0) or
                pure.get("error") is not None):
            errors.append("%s pure read is not zero-write" % label)
        selector_skip_mask = _parse_hex_u32(
            pure.get("selector_dependent_skipped_mask")
        )
        hbm_data_mask = _parse_hex_u32(pure.get("hbm_data_read_mask"))
        if (selector_skip_mask != 0xF or
                pure.get("selector_dependent_read_policy") !=
                "unconditionally-omitted-owner-race" or
                pure.get("selector_dependent_reads_unconditionally_omitted")
                is not True):
            errors.append("%s selector-dependent omission policy is malformed" % label)
            selector_skip_mask = 0
        hbm_ports_observed = pure.get("hbm_data_ports_observed")
        if (hbm_data_mask is None or hbm_data_mask & ~0xFFFFFF or
                not _is_int(hbm_ports_observed, 0, 24) or
                hbm_ports_observed != bin(hbm_data_mask or 0).count("1")):
            errors.append("%s HBM DATA read mask is malformed" % label)
            hbm_data_mask = 0
        expected_hbm_mask = 0xFFFFFF if expected_hbm_ack else 0
        if hbm_data_mask != expected_hbm_mask:
            errors.append("%s HBM DATA reads do not match acknowledgement" % label)
        skip_deduction = _temperature_selector_dependent_skip_count(
            selector_skip_mask
        )
        if backend == "xray-therm":
            if pure.get("backend") != "xray-therm-fixed-capture":
                errors.append("%s dedicated-helper backend marker is missing" % label)
            if (pure.get("status") != "pass" or
                    not _is_int(pure.get("helper_status"), 0, 0) or
                    pure.get("pure_completed") is not True):
                errors.append("%s dedicated-helper pure completion is not proven" % label)
            if (pure.get("autopoll_unchanged") is not True or
                    pure.get("sentinels_unchanged") is not True):
                errors.append("%s dedicated-helper pure context changed" % label)
            if (pure.get("fbpa_context_atomic") is not False or
                    pure.get("fbpa_context_warning") != (
                        "seven fixed FBPA context fields plus optional "
                        "acknowledged DATA are sequential and can race their "
                        "firmware owner; context correlation is not atomic")):
                errors.append("%s FBPA context atomicity claim mismatch" % label)
            expected_transaction_writes = (
                26 if args.temperature_access == "semantic-read" else 0
            )
            if not _is_int(
                    pure.get("transaction_total_writes"),
                    expected_transaction_writes, expected_transaction_writes):
                errors.append("%s dedicated-helper transaction write count mismatch" % label)
            reads_issued = pure.get("reads_issued")
            if args.temperature_access == "semantic-read":
                fixed_reads = 409 if expected_hbm_ack else 385
                expected_polls = (
                    reads_issued - fixed_reads
                    if isinstance(reads_issued, int) and
                    not isinstance(reads_issued, bool) else None
                )
                read_count_ok = (
                    _is_int(expected_polls, 1, 1000) and
                    _is_int(pure.get("trigger_poll_reads"), 1, 1000) and
                    pure.get("trigger_poll_reads") == expected_polls
                )
                expected_read_contract = (
                    "%d selector-safe fixed reads plus 1..1000 "
                    "trigger-poll reads" % fixed_reads
                )
            else:
                expected_polls = 0
                read_count_ok = (
                    isinstance(reads_issued, int) and
                    not isinstance(reads_issued, bool) and
                    reads_issued == 255 and
                    _is_int(pure.get("trigger_poll_reads"), 0, 0)
                )
                expected_read_contract = (
                    "255 fixed reads with seven selector-dependent reads omitted"
                )
            if (not read_count_ok or
                    pure.get("read_count_contract") != expected_read_contract or
                    not _is_int(pure.get("selector_dependent_omit_count"),
                                skip_deduction, skip_deduction) or
                    pure.get("hbm_data_requested") is not expected_hbm_ack):
                errors.append("%s dedicated-helper read count mismatch" % label)
            fresh_prewrite = pure.get("fresh_prewrite_liveness")
            if args.temperature_access == "semantic-read":
                fresh_tsense = _parse_hex_u32(
                    fresh_prewrite.get("tsense_raw")
                    if isinstance(fresh_prewrite, dict) else None
                )
                fresh_active = _parse_hex_u32(
                    fresh_prewrite.get("num_active_fbps_raw")
                    if isinstance(fresh_prewrite, dict) else None
                )
                expected_fresh = _temperature_liveness_report(
                    fresh_tsense, fresh_active
                )
                if (expected_fresh is None or
                        not _strict_equal(fresh_prewrite, expected_fresh) or
                        expected_fresh["two_signal_corroborated"] is not True or
                        pure.get("fresh_prewrite_liveness_scope") !=
                        "after-saved-selector-validation-immediately-before-first-write"):
                    errors.append("%s fresh prewrite liveness is not proven" % label)
            elif (fresh_prewrite is not None or
                  pure.get("fresh_prewrite_liveness_scope") !=
                  "not-requested-pure-capture"):
                errors.append("%s pure capture fabricates prewrite liveness" % label)
            if not isinstance(pure.get("driver_bound_at_entry"), bool):
                errors.append(
                    "%s dedicated-helper bound-entry marker is malformed" % label
                )
            helper_identity = identity.get("helper_identity") if isinstance(identity, dict) else None
            generation = (
                helper_identity.get("generation")
                if isinstance(helper_identity, dict) else None
            )
            generation_valid = (
                isinstance(generation, str) and
                re.fullmatch(r"0x[0-9a-f]{16}", generation) is not None and
                int(generation, 16) != 0
            )
            bdf_match = BDF_RE.fullmatch(bdf) if bdf is not None else None
            expected_components = ({
                "domain": int(bdf_match.group("domain"), 16),
                "bus": int(bdf_match.group("bus"), 16),
                "device": int(bdf_match.group("device"), 16),
                "function": int(bdf_match.group("function"), 16),
            } if bdf_match else {})
            helper_components_valid = (
                isinstance(helper_identity, dict) and
                all(_is_int(helper_identity.get(key), value, value)
                    for key, value in expected_components.items())
            )
            helper_identity_keys = {
                "generation", "bdf", "domain", "bus", "device", "function",
                "vendor_id", "device_id", "subsystem_vendor_id",
                "subsystem_device_id", "revision",
            }
            if (not isinstance(helper_identity, dict) or
                    set(helper_identity) != helper_identity_keys or
                    not generation_valid or not helper_components_valid or
                    helper_identity.get("bdf") != bdf or
                    helper_identity.get("vendor_id") != identity.get("vendor_id") or
                    helper_identity.get("device_id") != identity.get("device_id") or
                    helper_identity.get("subsystem_vendor_id") !=
                    identity.get("subsystem_vendor_id") or
                    helper_identity.get("subsystem_device_id") !=
                    identity.get("subsystem_device_id") or
                    helper_identity.get("revision") != identity.get("revision")):
                errors.append("%s dedicated-helper full identity is inconsistent" % label)
            elif generation_valid:
                helper_generations.add(generation)
        elif backend == "gpcprobe":
            expected_reads = 244
            if (pure.get("backend") != "legacy-gpcprobe" or
                    pure.get("status") != "conditional" or
                    pure.get("evidence_authority") !=
                    "conditional-non-authoritative" or
                    pure.get("transaction_journal_available") is not False or
                    pure.get("postflight_identity_verified") is not False or
                    pure.get("dead_bar_sentinel_check") != "passed" or
                    not _is_int(pure.get("reads_issued"),
                                expected_reads, expected_reads) or
                    pure.get("read_count_contract") !=
                    "244 unique reads with seven selector-dependent reads omitted"):
                errors.append("%s legacy read boundary is not preserved" % label)
        aggregates = pure.get("aggregate_readouts")
        if not isinstance(aggregates, list) or len(aggregates) != 8:
            errors.append("%s aggregate readout count mismatch" % label)
        else:
            for index, (readout, specification) in enumerate(zip(
                    aggregates, TEMPERATURE_AGGREGATES)):
                name, offset, encoding, kind = specification
                errors.extend(_validate_temperature_readout(
                    readout, "%s aggregate[%d]" % (label, index),
                    expected_encoding=encoding, expected_name=name,
                    expected_offset=offset, expected_kind=kind,
                ))

        sentinels = pure.get("sentinels")
        errors.extend(_exact_keys(
            sentinels, {"fbpa_num_active", "fbpa_training"},
            "%s sentinels" % label
        ))
        tsense_raw = (
            _parse_hex_u32(aggregates[0].get("raw"))
            if isinstance(aggregates, list) and len(aggregates) == 8 and
            isinstance(aggregates[0], dict) else None
        )
        active_fbps_raw = _parse_hex_u32(
            sentinels.get("fbpa_num_active")
            if isinstance(sentinels, dict) else None
        )
        training_raw = _parse_hex_u32(
            sentinels.get("fbpa_training")
            if isinstance(sentinels, dict) else None
        )
        expected_liveness = _temperature_liveness_report(
            tsense_raw, active_fbps_raw
        )
        if (training_raw is None or expected_liveness is None or
                not _strict_equal(pure.get("prewrite_liveness"),
                                  expected_liveness)):
            errors.append("%s prewrite liveness evidence mismatch" % label)
        elif (pure.get("status") in ("pass", "conditional") and
              expected_liveness["two_signal_corroborated"] is not True):
            errors.append("%s lacks affirmative two-signal liveness" % label)

        fixed_context = pure.get("fixed_context_registers")
        if (not isinstance(fixed_context, list) or
                len(fixed_context) != len(TEMPERATURE_FIXED_CONTEXT)):
            errors.append("%s fixed thermal context count mismatch" % label)
        else:
            for index, specification in enumerate(TEMPERATURE_FIXED_CONTEXT):
                errors.extend(_validate_raw_thermal_context(
                    fixed_context[index], specification,
                    "%s fixed context[%d]" % (label, index)))
            expected_skipped_context = {
                "nv_therm_gpc_tsense_override": bool(selector_skip_mask & 2),
                "nv_therm_gpc_tsense_hs_offset": bool(selector_skip_mask & 4),
            }
            for item in fixed_context:
                name = item.get("name") if isinstance(item, dict) else None
                expected_skip = expected_skipped_context.get(name, False)
                if isinstance(item, dict) and (
                        item.get("read_skipped") is not expected_skip or
                        (item.get("raw") is None) is not expected_skip):
                    errors.append("%s fixed-context omission-policy mismatch" % label)
        errors.extend(_validate_gpu_sci(pure.get("gpu_sci"), label))

        analog = pure.get("gpc_analog_context")
        if not isinstance(analog, list) or len(analog) != 8:
            errors.append("%s GPC analogue context count mismatch" % label)
        else:
            for gpc, row in enumerate(analog):
                base = 0x0010C000 + gpc * 0x100
                if (not isinstance(row, dict) or
                        not _is_int(row.get("gpc"), gpc, gpc) or
                        row.get("base") != "0x%08x" % base or
                        not _is_int(row.get("independent_sensor_count"), 0, 0)):
                    errors.append("%s GPC analogue context identity mismatch" % label)
                    continue
                errors.extend(_exact_keys(row, {
                    "gpc", "base", "registers", "independent_sensor_count",
                    "interpretation",
                }, "%s GPC analogue[%d]" % (label, gpc)))
                if (row.get("interpretation") !=
                        TEMPERATURE_GPC_ANALOG_INTERPRETATION):
                    errors.append(
                        "%s GPC analogue[%d] interpretation mismatch" %
                        (label, gpc)
                    )
                registers = row.get("registers")
                if not isinstance(registers, list) or len(registers) != 4:
                    errors.append("%s GPC analogue register count mismatch" % label)
                    continue
                for rindex, (name, relative, kind) in enumerate(
                        TEMPERATURE_GPC_ANALOG_REGISTERS):
                    errors.extend(_validate_raw_thermal_context(
                        registers[rindex], (name, base + relative, kind),
                        "%s GPC analogue[%d][%d]" % (label, gpc, rindex)))

        census = pure.get("logical_hardware_backed_endpoint_census")
        if not _strict_equal(census, {
                "maximum_logical_endpoints": 32,
                "components": {
                    "gpc_bjt_addressable_endpoints": 24,
                    "system_tsense_endpoint": 1,
                    "gpu_sci_mini_tsense_endpoint": 1,
                    "hbm_logical_site_maxima": 6,
                },
                "classification": "hardware-backed-logical-endpoints-not-diodes",
                "scope": (
                    "architectural maximum; aliases, mux views, latches, "
                    "aggregates, thresholds and configuration words excluded"
                ),
        }):
            errors.append("%s logical endpoint census mismatch" % label)

        auto = pure.get("gpc_autopoll_controls")
        if not isinstance(auto, list) or len(auto) != 8:
            errors.append("%s autopoll readout count mismatch" % label)
        else:
            for gpc, item in enumerate(auto):
                raw = _parse_hex_u32(item.get("raw") if isinstance(item, dict) else None)
                expected_offset = TEMPERATURE_TS_AUTO_BASE + gpc * TEMPERATURE_TS_AUTO_STRIDE
                if (not isinstance(item, dict) or
                        not _is_int(item.get("gpc"), gpc, gpc)):
                    errors.append("%s autopoll GPC order mismatch" % label)
                    continue
                errors.extend(_exact_keys(item, {
                    "gpc", "offset", "raw", "auto_polling",
                    "auto_polling_bit", "available", "all_ones",
                    "sample_delay", "polling_interval",
                }, "%s autopoll[%d]" % (label, gpc)))
                if item.get("offset") != "0x%08x" % expected_offset:
                    errors.append("%s autopoll offset mismatch" % label)
                if not _is_int(item.get("auto_polling_bit"), 15, 15):
                    errors.append("%s autopoll bit is not bit 15" % label)
                expected_available = (
                    raw is not None and (raw & 0xFFFF0000) != 0xBADF0000 and
                    raw != 0xFFFFFFFF
                )
                if (item.get("available") is not expected_available or
                        item.get("all_ones") is not (raw == 0xFFFFFFFF)):
                    errors.append("%s autopoll availability mismatch" % label)
                if expected_available:
                    if item.get("auto_polling") is not bool(raw & (1 << 15)):
                        errors.append("%s autopoll decode does not use bit 15" % label)
                    if not _is_int(item.get("sample_delay"),
                                   raw & 0x3FFF, raw & 0x3FFF):
                        errors.append("%s autopoll sample-delay mismatch" % label)
                    expected_interval = (raw >> 16) & 0xFFFF
                    if not _is_int(item.get("polling_interval"),
                                   expected_interval, expected_interval):
                        errors.append("%s autopoll interval mismatch" % label)
                elif any(item.get(key) is not None for key in (
                        "auto_polling", "sample_delay", "polling_interval")):
                    errors.append("%s unavailable autopoll fields are not null" % label)
                analog = pure.get("gpc_analog_context")
                if (isinstance(analog, list) and len(analog) == 8 and
                        isinstance(analog[gpc], dict) and
                        isinstance(analog[gpc].get("registers"), list) and
                        len(analog[gpc]["registers"]) == 4 and
                        isinstance(analog[gpc]["registers"][3], dict) and
                        analog[gpc]["registers"][3].get("raw") != item.get("raw")):
                    errors.append("%s GPC %d TS_AUTO duplicate mismatch" %
                                  (label, gpc))

        fbio_errors, fbpa_enabled = _validate_fbio_population(
            pure.get("fbio_population"), label
        )
        errors.extend(fbio_errors)
        fbpas = pure.get("fbpa_readout_channels")
        if not isinstance(fbpas, list) or len(fbpas) != 24:
            errors.append("%s FBPA channel count mismatch" % label)
        else:
            for fbpa, item in enumerate(fbpas):
                if (not isinstance(item, dict) or
                        not _is_int(item.get("fbpa"), fbpa, fbpa)):
                    errors.append("%s FBPA order mismatch" % label)
                    continue
                errors.extend(_exact_keys(item, {
                    "fbpa", "raw", "valid", "latch_valid",
                    "temperature_context_confirmed", "aperture_responded",
                    "enabled", "fbpa_live", "live", "cfg1", "invalid_bit",
                    "data_observed", "availability", "test_macro",
                    "test_macro_decoded", "test_macro_temperature_context_ready",
                    "badf", "all_ones", "temperature_c", "instruction", "mode",
                    "shadow_instruction", "shadow_instruction_opcode",
                    "shadow_instruction_is_temperature",
                    "shadow_instruction_corroborates_temperature_empirically",
                    "temperature_context_requirements", "shadow_data", "status",
                    "status_bridge", "cfg1_offset", "test_macro_offset",
                    "instruction_offset", "mode_offset", "wdr_offset",
                    "shadow_instruction_offset", "shadow_data_offset",
                    "status_offset", "interpretation",
                }, "%s FBPA %d" % (label, fbpa)))
                cfg1 = _parse_hex_u32(item.get("cfg1"))
                test_macro = _parse_hex_u32(item.get("test_macro"))
                instruction = _parse_hex_u32(item.get("instruction"))
                mode = _parse_hex_u32(item.get("mode"))
                shadow_instruction = _parse_hex_u32(
                    item.get("shadow_instruction"))
                shadow_data = _parse_hex_u32(item.get("shadow_data"))
                status = _parse_hex_u32(item.get("status"))
                raw = _parse_hex_u32(item.get("raw"))
                expected_data_observed = bool(hbm_data_mask & (1 << fbpa))
                raw_shape_ok = (
                    raw is not None if expected_data_observed
                    else item.get("raw") is None
                )
                if (item.get("data_observed") is not expected_data_observed or
                        not raw_shape_ok):
                    errors.append("%s FBPA %d DATA observation mask mismatch" %
                                  (label, fbpa))
                if any(value is None for value in (
                        cfg1, test_macro, instruction, mode, shadow_instruction,
                        shadow_data, status)):
                    errors.append("%s FBPA %d register word is malformed" %
                                  (label, fbpa))
                base = TEMPERATURE_FBPA_BASE + fbpa * TEMPERATURE_FBPA_STRIDE
                for field, relative in TEMPERATURE_FBPA_REGISTERS.items():
                    if item.get(field) != "0x%08x" % (base + relative):
                        errors.append("%s FBPA %d %s mismatch" %
                                      (label, fbpa, field))
                test_badf = (test_macro is not None and
                             (test_macro & 0xFFFF0000) == 0xBADF0000)
                test_all_ones = test_macro == 0xFFFFFFFF
                test_unavailable = test_badf or test_all_ones
                expected_test_macro = (None if test_macro is None else {
                    "badf": test_badf,
                    "all_ones": test_all_ones,
                    "dram_reset_disabled": None if test_unavailable else bool(test_macro & 1),
                    "raw_temperature_sideband": None if test_unavailable else (test_macro >> 1) & 0x7,
                    "temperature_c": None,
                    "dram_wrst_reset_disabled": None if test_unavailable else bool(test_macro & (1 << 4)),
                    "read_i1500_register": None if test_unavailable else bool(test_macro & (1 << 5)),
                    "debug_bus_wso_select": None if test_unavailable else (test_macro >> 6) & 0x7,
                    "debug_bus_group0_select": None if test_unavailable else (test_macro >> 9) & 0x1F,
                    "debug_bus_group1_select": None if test_unavailable else (test_macro >> 14) & 0x1F,
                    "debug_bus_group2_select": None if test_unavailable else (test_macro >> 19) & 0x1F,
                    "debug_bus_reset_select": None if test_unavailable else bool(test_macro & (1 << 24)),
                    "wso_mux_override": None if test_unavailable else bool(test_macro & (1 << 25)),
                    "wso_mux_select": None if test_unavailable else (test_macro >> 26) & 0xF,
                })
                if not _strict_equal(
                        item.get("test_macro_decoded"), expected_test_macro):
                    errors.append("%s FBPA %d TEST_MACRO decode mismatch" %
                                  (label, fbpa))
                expected_aperture = (
                    cfg1 is not None and
                    (cfg1 & 0xFFFF0000) != 0xBADF0000 and
                    cfg1 != 0xFFFFFFFF
                )
                expected_enabled = (
                    fbpa_enabled[fbpa] if fbpa_enabled is not None else None
                )
                expected_live = bool(expected_aperture and expected_enabled)
                if (item.get("aperture_responded") is not expected_aperture or
                        item.get("enabled") is not expected_enabled or
                        item.get("live") is not expected_live or
                        item.get("fbpa_live") is not expected_live):
                    errors.append("%s FBPA %d live-state decode mismatch" %
                                  (label, fbpa))
                opcode = ((shadow_instruction >> 4) & 0xFF
                          if shadow_instruction is not None else None)
                expected_test_ready = (
                    expected_test_macro is not None and not test_unavailable and
                    (test_macro & (~0x0000000E & 0xFFFFFFFF)) == 0x00000011
                )
                expected_context = (
                    instruction == 0x0F and mode == 0x08 and
                    shadow_instruction == 0x000000F0 and expected_test_ready
                )
                if (item.get("shadow_instruction_opcode") !=
                        ("0x%02x" % opcode if opcode is not None else None) or
                        item.get("shadow_instruction_is_temperature") is not
                        (shadow_instruction == 0x000000F0) or
                        item.get(
                            "shadow_instruction_corroborates_temperature_empirically"
                        ) is not (shadow_instruction == 0x000000F0)):
                    errors.append("%s FBPA %d SHWIR opcode mirror mismatch" %
                                  (label, fbpa))
                if not _strict_equal(
                        item.get("temperature_context_requirements"), {
                        "instruction_equals": "0x0000000f",
                        "mode_equals": "0x00000008",
                        "shadow_instruction_equals": "0x000000f0",
                        "test_macro_available": True,
                        "test_macro_dram_reset_disabled": True,
                        "test_macro_dram_wrst_reset_disabled": True,
                        "test_macro_read_i1500_register": False,
                        "test_macro_writable_bits_equal": "0x00000011",
                        "test_macro_temperature_sideband_mask": "0x0000000e",
                        "shadow_instruction_role":
                            "empirical-latch-corroboration",
                }):
                    errors.append("%s FBPA %d context requirements mismatch" %
                                  (label, fbpa))
                if item.get("temperature_context_confirmed") is not expected_context:
                    errors.append("%s FBPA %d context gate mismatch" % (label, fbpa))
                if (item.get("test_macro_temperature_context_ready") is not
                        expected_test_ready):
                    errors.append("%s FBPA %d TEST_MACRO context gate mismatch" %
                                  (label, fbpa))
                status_badf = (status is not None and
                               (status & 0xFFFF0000) == 0xBADF0000)
                status_all_ones = status == 0xFFFFFFFF
                status_reserved_zero = (
                    status is not None and (status & ~0x1F) == 0
                )
                bridge_available = not (
                    status_badf or status_all_ones or
                    not status_reserved_zero
                )
                bridge_state = status & 0x1F if status is not None else None
                expected_status_bridge = (None if status is None else {
                    "raw": "0x%08x" % status,
                    "available": bridge_available,
                    "badf": status_badf,
                    "all_ones": status_all_ones,
                    "reserved_bits_zero": status_reserved_zero,
                    "state_value": bridge_state if bridge_available else None,
                    "state_name": (
                        TEMPERATURE_I1500_BRIDGE_STATES.get(
                            bridge_state, "reserved")
                        if bridge_available else None
                    ),
                    "idle": bridge_state == 0 if bridge_available else None,
                    "transaction_active_or_nonidle": (
                        bridge_state != 0 if bridge_available else None
                    ),
                    "coherence_signal": (
                        "idle-observed"
                        if bridge_available and bridge_state == 0 else
                        "nonidle-observed-possible-race" if bridge_available else
                        "unavailable-reserved-bits"
                        if not status_reserved_zero and not status_badf and
                        not status_all_ones else "unavailable"
                    ),
                    "temperature_gate_applied": False,
                    "gate_reason": (
                        "exact bridge state is recorded, but STATUS follows the "
                        "read-to-advance DATA access and may race; official getter "
                        "does not poll IDLE, so non-IDLE downgrades coherence but "
                        "does not alone reject the payload"
                    ),
                })
                if not _strict_equal(item.get("status_bridge"),
                                     expected_status_bridge):
                    errors.append("%s FBPA %d bridge-status decode mismatch" %
                                  (label, fbpa))
                expected_badf = (
                    raw is not None and (raw & 0xFFFF0000) == 0xBADF0000
                )
                expected_all_ones = raw == 0xFFFFFFFF if raw is not None else False
                expected_invalid = (
                    bool(raw & (1 << 31)) if raw is not None else None
                )
                if (item.get("badf") is not expected_badf or
                        item.get("all_ones") is not expected_all_ones or
                        item.get("invalid_bit") is not expected_invalid):
                    errors.append("%s FBPA %d latch flag mirror mismatch" %
                                  (label, fbpa))
                expected_latch_valid = (
                    expected_data_observed and expected_live and raw is not None and
                    (raw & 0xFFFF0000) != 0xBADF0000 and
                    raw != 0xFFFFFFFF and
                    not bool(raw & (1 << 31))
                )
                expected_valid = expected_latch_valid and expected_context
                if item.get("latch_valid") is not expected_latch_valid:
                    errors.append("%s FBPA %d latch validity mismatch" % (label, fbpa))
                if item.get("valid") is not expected_valid:
                    errors.append("%s FBPA %d temperature validity mismatch" % (label, fbpa))
                expected_temperature = ((raw >> 24) & 0x7F) if expected_valid else None
                if ((expected_temperature is None and
                     item.get("temperature_c") is not None) or
                        (expected_temperature is not None and
                         not _is_int(item.get("temperature_c"),
                                     expected_temperature,
                                     expected_temperature))):
                    errors.append("%s FBPA %d temperature decode mismatch" % (label, fbpa))
                expected_availability = (
                    "not-read-stateful-read-to-advance-port"
                    if not expected_data_observed else
                    "unavailable-badf" if expected_badf else
                    "unavailable-all-ones" if expected_all_ones else
                    "observed"
                )
                if item.get("availability") != expected_availability:
                    errors.append("%s FBPA %d availability mismatch" % (label, fbpa))
                if item.get("interpretation") != (
                        "IEEE1500 DATA is a read-to-advance port and is null "
                        "unless explicitly acknowledged; TEST_MACRO is raw "
                        "sideband context with no Celsius conversion; decoded "
                        "as temperature only when enabled/responding, INSTR=0x0f, "
                        "MODE=0x08, SHWIR is exactly 0xf0, and TEST_MACRO proves "
                        "reset-safe non-read mode; exact STATUS bridge state is "
                        "separately classified because the post-DATA observation "
                        "may race; this probe never initiates a fresh HBM "
                        "transaction; distinct physical sensor not established"):
                    errors.append("%s FBPA %d interpretation mismatch" %
                                  (label, fbpa))

        current = pure.get("currently_selected_gpc_readout")
        if not isinstance(current, dict):
            errors.append("%s current GPC readout is missing" % label)
        else:
            current_keys = {
                "selector", "selector_available", "aperture_available",
                "selector_badf", "selector_all_ones", "selector_fields_in_range",
                "selector_reserved_bits_zero", "selector_semantically_valid",
                "gpc", "bjt", "read_increment_enabled",
                "dependent_reads_skipped", "temperature", "offset_temperature",
                "raw_adc", "observed_raw_adc_unbound", "warning",
            }
            if backend == "xray-therm":
                current_keys.add("coherent_tuple")
            errors.extend(_exact_keys(current, current_keys,
                                      "%s current GPC readout" % label))
            selector = _parse_hex_u32(current.get("selector"))
            expected_skip = True
            selector_state = _temperature_selector_semantics(selector)
            selector_valid = bool(
                selector_state and
                selector_state["selector_semantically_valid"]
            )
            if (selector is None or
                    any(not _strict_equal(current.get(key), value)
                        for key, value in selector_state.items()) or
                    current.get("read_increment_enabled") is not
                    bool(selector & (1 << 30)) or
                    current.get("dependent_reads_skipped") is not expected_skip):
                errors.append("%s current GPC selector decode mismatch" % label)
            if current.get("warning") != (
                    "selector was observed, but dependent payload ports were "
                    "unconditionally omitted because their owner can race"):
                errors.append("%s current GPC warning mismatch" % label)
            if backend == "xray-therm" and current.get("coherent_tuple") is not False:
                errors.append("%s current GPC tuple is falsely coherent" % label)
            raw_adc = _parse_hex_u32(current.get("raw_adc"))
            observed_unbound_adc = _parse_hex_u32(
                current.get("observed_raw_adc_unbound"))
            if (selector_valid and not expected_skip and raw_adc is None or
                    (not selector_valid or expected_skip) and
                    current.get("raw_adc") is not None or
                    (not selector_valid and not expected_skip and
                     observed_unbound_adc is None) or
                    (selector_valid or expected_skip) and
                    current.get("observed_raw_adc_unbound") is not None):
                errors.append("%s current GPC raw ADC availability mismatch" % label)
            errors.extend(_validate_temperature_readout(
                current.get("temperature"), "%s current GPC temperature" % label,
                expected_encoding="valid-bit",
            ))
            errors.extend(_validate_temperature_readout(
                current.get("offset_temperature"),
                "%s current GPC offset temperature" % label,
                expected_encoding="valid-bit",
            ))
        found = pure.get("snapshot_as_found")
        if not isinstance(found, dict):
            errors.append("%s snapshot-as-found is missing" % label)
        else:
            found_keys = {
                "selector", "selector_available", "aperture_available",
                "selector_badf", "selector_all_ones", "selector_fields_in_range",
                "selector_reserved_bits_zero", "selector_semantically_valid",
                "gpc", "bjt", "read_increment_enabled",
                "dependent_reads_skipped", "temperature", "offset_temperature",
                "warning",
            }
            if backend == "xray-therm":
                found_keys.update((
                    "selector_before", "selector_after", "selector_stable",
                    "selector_stability_source", "coherent_tuple",
                    "stability_scope",
                ))
            errors.extend(_exact_keys(found, found_keys,
                                      "%s snapshot-as-found" % label))
            selector = _parse_hex_u32(found.get("selector"))
            expected_skip = True
            selector_state = _temperature_selector_semantics(selector)
            if (selector is None or
                    any(not _strict_equal(found.get(key), value)
                        for key, value in selector_state.items()) or
                    found.get("read_increment_enabled") is not
                    bool(selector & (1 << 30)) or
                    found.get("dependent_reads_skipped") is not expected_skip):
                errors.append("%s snapshot-as-found selector decode mismatch" % label)
            if found.get("warning") != (
                    "no trigger was issued and selector-dependent snapshot "
                    "payload ports were unconditionally omitted because their "
                    "owner can race"):
                errors.append("%s snapshot-as-found warning mismatch" % label)
            errors.extend(_validate_temperature_readout(
                found.get("temperature"), "%s found snapshot temperature" % label,
                expected_encoding="valid-bit",
            ))
            errors.extend(_validate_temperature_readout(
                found.get("offset_temperature"),
                "%s found snapshot offset temperature" % label,
                expected_encoding="valid-bit",
            ))
            if backend == "xray-therm":
                before = _parse_hex_u32(found.get("selector_before"))
                after = _parse_hex_u32(found.get("selector_after"))
                stable = found.get("selector_stable")
                if before is None or after is None or not isinstance(stable, bool):
                    errors.append("%s snapshot-as-found journal is malformed" % label)
                elif stable is not (before == after):
                    errors.append("%s snapshot-as-found stability is inconsistent" % label)
                if (found.get("selector_stability_source") !=
                        "userspace-recomputed-before-equals-after"):
                    errors.append("%s snapshot stability provenance mismatch" % label)
                if (before != selector or after != selector or
                        found.get("selector") != found.get("selector_before")):
                    errors.append("%s snapshot-as-found selector mirrors conflict" % label)
                if found.get("coherent_tuple") is not False:
                    errors.append("%s snapshot-as-found tuple is falsely coherent" % label)
                if found.get("stability_scope") != (
                        "selector-before equals selector-after only; dependent "
                        "temperature and offset payload ports were "
                        "unconditionally omitted"):
                    errors.append("%s snapshot stability scope mismatch" % label)

        population_errors, _pure_population = _validate_temperature_population(
            pure.get("population"), "%s pure" % label
        )
        errors.extend(population_errors)
        if pure.get("hbm_transaction_policy") != (
                "DATA read-to-advance ports are skipped unless semantic mode "
                "has an explicit HBM-DATA acknowledgement; require INSTR=0x0f, "
                "MODE=0x08, exact SHWIR=0xf0, and reset-safe TEST_MACRO context "
                "before decoding observed DATA; the collector never initiates "
                "an IEEE1500 temperature transaction"):
            errors.append("%s HBM transaction policy mismatch" % label)

        stacks = pure.get("hbm_stack_groups")
        if not isinstance(stacks, list) or len(stacks) != 6:
            errors.append("%s HBM stack group count mismatch" % label)
        else:
            for ordinal, (stack, expected, public_label) in enumerate(zip(
                    stacks, TEMPERATURE_STACKS,
                    TEMPERATURE_PUBLIC_HBMMAP_LABELS)):
                if not isinstance(stack, dict):
                    errors.append("%s HBM stack entry malformed" % label)
                    continue
                errors.extend(_exact_keys(stack, {
                    "stack", "firmware_site_ordinal", "xray_sequential_label",
                    "public_hbmmap_label", "label_authority", "fbps", "fbpas",
                    "enabled_fbpas", "responding_fbpas", "usable_fbpas",
                    "live_fbpas", "site_present", "selected_fbpa",
                    "selected_fbpa_valid", "selected_temperature_c",
                    "selected_aperture_responded", "valid_temperatures_c",
                    "unique_temperatures_c", "all_valid_views_equal",
                    "interpretation",
                }, "%s HBM stack[%d]" % (label, ordinal)))
                name, expected_pair = expected
                fbp_value = stack.get("fbps")
                fbp_pair = (
                    tuple(fbp_value)
                    if isinstance(fbp_value, (list, tuple)) else None
                )
                if (stack.get("stack") != name or
                        not _strict_equal(fbp_value, list(expected_pair)) or
                        not _is_int(stack.get("firmware_site_ordinal"),
                                    ordinal, ordinal) or
                        stack.get("xray_sequential_label") != name or
                        stack.get("public_hbmmap_label") != public_label or
                        stack.get("label_authority") !=
                        "presentation-only-membership-authoritative"):
                    errors.append("%s HBM stack mapping mismatch" % label)
                    continue
                expected_fbpas = []
                for fbp in fbp_pair:
                    expected_fbpas.extend((2 * fbp, 2 * fbp + 1))
                if not _strict_equal(stack.get("fbpas"), expected_fbpas):
                    errors.append("%s HBM FBPA expansion mismatch" % label)
                enabled_fbpas = [index for index in expected_fbpas
                                 if isinstance(fbpas, list) and len(fbpas) == 24 and
                                 isinstance(fbpas[index], dict) and
                                 fbpas[index].get("enabled") is True]
                responding_fbpas = [index for index in expected_fbpas
                                    if isinstance(fbpas, list) and len(fbpas) == 24 and
                                    isinstance(fbpas[index], dict) and
                                    fbpas[index].get("aperture_responded") is True]
                live_fbpas = [index for index in expected_fbpas
                              if isinstance(fbpas, list) and len(fbpas) == 24 and
                              isinstance(fbpas[index], dict) and
                              fbpas[index].get("live") is True]
                valid_temperatures = [
                    fbpas[index].get("temperature_c") for index in expected_fbpas
                    if isinstance(fbpas, list) and len(fbpas) == 24 and
                    isinstance(fbpas[index], dict) and
                    fbpas[index].get("live") is True and
                    fbpas[index].get("valid") is True
                ]
                unique_temperatures = sorted(set(valid_temperatures))
                expected_site_present = (
                    bool(enabled_fbpas) if fbpa_enabled is not None else None
                )
                selected_fbpa = enabled_fbpas[0] if enabled_fbpas else None
                selected = (fbpas[selected_fbpa]
                            if selected_fbpa is not None and
                            isinstance(fbpas, list) and len(fbpas) == 24 else None)
                if (stack.get("site_present") is not expected_site_present or
                        not _strict_equal(stack.get("selected_fbpa"),
                                          selected_fbpa) or
                        stack.get("selected_fbpa_valid") is not (
                            selected.get("valid") if selected else None) or
                        not _strict_equal(
                            stack.get("selected_temperature_c"),
                            selected.get("temperature_c") if selected else None) or
                        stack.get("selected_aperture_responded") is not (
                            selected.get("aperture_responded")
                            if selected else None) or
                        not _strict_equal(stack.get("enabled_fbpas"),
                                          enabled_fbpas) or
                        not _strict_equal(stack.get("responding_fbpas"),
                                          responding_fbpas) or
                        not _strict_equal(stack.get("usable_fbpas"), live_fbpas) or
                        not _strict_equal(stack.get("live_fbpas"), live_fbpas) or
                        not _strict_equal(stack.get("valid_temperatures_c"),
                                          valid_temperatures) or
                        not _strict_equal(stack.get("unique_temperatures_c"),
                                          unique_temperatures) or
                        stack.get("all_valid_views_equal") is not
                        (bool(valid_temperatures) and
                         len(unique_temperatures) == 1)):
                    errors.append("%s HBM stack derived telemetry mismatch" % label)
                if stack.get("interpretation") != (
                        "exact GA100 PMU table order; selected_fbpa is the first "
                        "enabled firmware-selected routing candidate/source; "
                        "passive measurement context remains conditional and "
                        "non-atomic; other views remain diagnostic and no physical "
                        "sensor count is inferred"):
                    errors.append("%s HBM site selection semantics mismatch" % label)

        snapshot = record.get("global_snapshot")
        if args.temperature_access == "bar-read":
            if snapshot is not None:
                errors.append("%s bar-read unexpectedly contains active snapshot" % label)
            continue
        if not isinstance(snapshot, dict):
            errors.append("%s semantic snapshot is missing" % label)
            continue
        errors.extend(_exact_keys(snapshot, {
            "access", "status", "error", "backend", "helper_status",
            "writes_issued", "reads_issued", "trigger_poll_reads",
            "read_count_contract", "hbm_data_requested", "hbm_data_read_mask",
            "selector_dependent_skipped_mask", "selector_dependent_omit_count",
            "pure_completed", "snapshot_completed", "driver_bound_at_entry",
            "bound_snapshot_acknowledged", "selector_entry", "selector_exit",
            "selector_entry_semantics", "selector_exit_semantics",
            "selector_restored", "selector_race", "trigger_count",
            "trigger_self_cleared", "trigger_readback", "selector_program_count",
            "restore_write_count", "write_offsets_observed",
            "write_offsets_directly_logged", "write_offsets_source",
            "write_surface", "failed_gpc", "failed_bjt",
            "concurrent_selector_race", "autopoll_untouched", "autopoll_entry",
            "autopoll_exit", "sentinels_unchanged", "sentinels_entry",
            "sentinels_exit", "snapshot_aggregate_readouts",
            "gpc_bjt_endpoints", "valid_endpoint_count", "population",
            "procedure", "fresh_prewrite_liveness",
            "fresh_prewrite_liveness_scope",
        }, "%s semantic snapshot" % label))
        if snapshot.get("access") != "semantic-read" or snapshot.get("status") != "pass":
            errors.append("%s semantic snapshot did not pass" % label)
        if snapshot.get("error") is not None:
            errors.append("%s passing semantic snapshot has a fabricated error" % label)
        if snapshot.get("procedure") != (
                "one global trigger; enumerate 8x3 snapshot selector; mandatory "
                "kernel restoration; no GPC TS_AUTO writes"):
            errors.append("%s semantic snapshot procedure mismatch" % label)
        if backend == "xray-therm":
            if any(key in snapshot for key in (
                    "population_entry", "population_exit",
                    "population_masks_unchanged")):
                errors.append(
                    "%s dedicated helper fabricates population exit evidence" % label
                )
            if (snapshot.get("backend") != "xray-therm-fixed-capture" or
                    not _is_int(snapshot.get("helper_status"), 0, 0) or
                    snapshot.get("pure_completed") is not True or
                    snapshot.get("snapshot_completed") is not True or
                    snapshot.get("selector_race") is not False or
                    snapshot.get("trigger_self_cleared") is not True or
                    snapshot.get("failed_gpc") is not None or
                    snapshot.get("failed_bjt") is not None):
                errors.append("%s dedicated-helper snapshot journal is incomplete" % label)
            snapshot_reads = snapshot.get("reads_issued")
            fixed_reads = 409 if expected_hbm_ack else 385
            expected_polls = (snapshot_reads - fixed_reads
                              if isinstance(snapshot_reads, int) and
                              not isinstance(snapshot_reads, bool) else None)
            if (not _is_int(expected_polls, 1, 1000) or
                    not _is_int(snapshot.get("trigger_poll_reads"), 1, 1000) or
                    snapshot.get("trigger_poll_reads") != expected_polls or
                    snapshot.get("read_count_contract") !=
                    ("%d selector-safe fixed reads plus 1..1000 "
                     "trigger-poll reads" % fixed_reads) or
                    snapshot.get("selector_dependent_skipped_mask") !=
                    pure.get("selector_dependent_skipped_mask") or
                    not _is_int(snapshot.get("selector_dependent_omit_count"),
                                skip_deduction, skip_deduction) or
                    snapshot.get("hbm_data_requested") is not expected_hbm_ack or
                    snapshot.get("hbm_data_read_mask") !=
                    pure.get("hbm_data_read_mask")):
                errors.append("%s dedicated-helper snapshot read count mismatch" % label)
            for mirrored_key in (
                    "reads_issued", "trigger_poll_reads", "read_count_contract",
                    "hbm_data_requested", "hbm_data_read_mask",
                    "selector_dependent_skipped_mask",
                    "selector_dependent_omit_count"):
                if not _strict_equal(
                        snapshot.get(mirrored_key), pure.get(mirrored_key)):
                    errors.append(
                        "%s helper duplicate journal mismatch: %s" %
                        (label, mirrored_key)
                    )
            snapshot_bound = snapshot.get("driver_bound_at_entry")
            pure_bound = pure.get("driver_bound_at_entry")
            if (not isinstance(snapshot_bound, bool) or
                    not isinstance(pure_bound, bool) or
                    snapshot_bound is not pure_bound):
                errors.append("%s bound-entry journal is inconsistent" % label)
            if (snapshot_bound is True and
                    snapshot.get("bound_snapshot_acknowledged") is not True):
                errors.append("%s bound snapshot lacks explicit acknowledgement" % label)
            if (snapshot.get("bound_snapshot_acknowledged") is not
                    expected_bound_ack):
                errors.append("%s bound snapshot acknowledgement is inconsistent" % label)
            if (snapshot.get("write_offsets_directly_logged") is not False or
                    snapshot.get("write_offsets_source") !=
                    "reconstructed-from-fixed-kernel-write-alphabet-and-exact-"
                    "counter; not a per-write hardware log"):
                errors.append("%s helper write-offset provenance is invalid" % label)
            if (not _strict_equal(snapshot.get("fresh_prewrite_liveness"),
                                  pure.get("fresh_prewrite_liveness")) or
                    snapshot.get("fresh_prewrite_liveness_scope") !=
                    "after-saved-selector-validation-immediately-before-first-write"):
                errors.append("%s snapshot fresh-liveness journal mismatch" % label)
        if (not _is_int(snapshot.get("writes_issued"), 26, 26) or
                not _is_int(snapshot.get("trigger_count"), 1, 1) or
                not _is_int(snapshot.get("selector_program_count"), 24, 24) or
                not _is_int(snapshot.get("restore_write_count"), 1, 1)):
            errors.append("%s semantic write cardinality mismatch" % label)
        if (snapshot.get("selector_restored") is not True or
                snapshot.get("selector_entry") != snapshot.get("selector_exit")):
            errors.append("%s snapshot selector was not restored" % label)
        selector_entry = _parse_hex_u32(snapshot.get("selector_entry"))
        selector_exit = _parse_hex_u32(snapshot.get("selector_exit"))
        selector_entry_state = _temperature_selector_semantics(selector_entry)
        selector_exit_state = _temperature_selector_semantics(selector_exit)
        if (selector_entry is None or selector_exit is None or
                not selector_entry_state["selector_semantically_valid"] or
                not selector_exit_state["selector_semantically_valid"] or
                not _strict_equal(snapshot.get("selector_entry_semantics"),
                                  selector_entry_state) or
                not _strict_equal(snapshot.get("selector_exit_semantics"),
                                  selector_exit_state)):
            errors.append("%s snapshot selector journal is malformed" % label)
        if (snapshot.get("autopoll_untouched") is not True or
                snapshot.get("sentinels_unchanged") is not True):
            errors.append("%s semantic sentinels changed" % label)
        autopoll_entry = snapshot.get("autopoll_entry")
        autopoll_exit = snapshot.get("autopoll_exit")
        pure_autopoll = pure.get("gpc_autopoll_controls")
        expected_autopoll = (
            [item.get("raw") for item in pure_autopoll]
            if isinstance(pure_autopoll, list) and len(pure_autopoll) == 8 and
            all(isinstance(item, dict) for item in pure_autopoll) else None
        )
        if (not isinstance(autopoll_entry, list) or len(autopoll_entry) != 8 or
                autopoll_entry != autopoll_exit or
                any(_parse_hex_u32(value) is None for value in autopoll_entry) or
                autopoll_entry != expected_autopoll):
            errors.append("%s semantic autopoll journal mismatch" % label)
        sentinels_entry = snapshot.get("sentinels_entry")
        sentinels_exit = snapshot.get("sentinels_exit")
        expected_sentinels = pure.get("sentinels")
        expected_sentinel_values = (
            [expected_sentinels.get("fbpa_num_active"),
             expected_sentinels.get("fbpa_training")]
            if isinstance(expected_sentinels, dict) else None
        )
        if (not isinstance(sentinels_entry, list) or len(sentinels_entry) != 2 or
                sentinels_entry != sentinels_exit or
                any(_parse_hex_u32(value) is None for value in sentinels_entry) or
                sentinels_entry != expected_sentinel_values):
            errors.append("%s semantic sentinel journal mismatch" % label)
        trigger = _parse_hex_u32(snapshot.get("trigger_readback"))
        trigger_proven = snapshot.get("trigger_self_cleared") is True
        if backend == "xray-therm" and snapshot.get("trigger_readback") is not None:
            errors.append("%s dedicated-helper trigger readback must be null" % label)
        if not trigger_proven and (trigger is None or trigger & 1):
            errors.append("%s snapshot trigger did not self-clear" % label)
        if trigger is not None and trigger & 1:
            errors.append("%s snapshot trigger readback remains asserted" % label)
        if snapshot.get("concurrent_selector_race") is not None:
            errors.append("%s snapshot selector race was observed" % label)
        offsets = snapshot.get("write_offsets_observed")
        expected_offsets = (
            ["0x%08x" % TEMPERATURE_SNAPSHOT_TRIGGER] +
            ["0x%08x" % TEMPERATURE_SNAPSHOT_SELECTOR] * 25
        )
        if offsets != expected_offsets:
            errors.append("%s semantic write surface mismatch" % label)
        surface = snapshot.get("write_surface")
        errors.extend(_exact_keys(surface, {
            "snapshot_trigger", "snapshot_selector", "gpc_autopoll",
            "allowlist_only",
        }, "%s semantic write surface" % label))
        if (not isinstance(surface, dict) or
                surface.get("snapshot_trigger") !=
                "0x%08x" % TEMPERATURE_SNAPSHOT_TRIGGER or
                surface.get("snapshot_selector") !=
                "0x%08x" % TEMPERATURE_SNAPSHOT_SELECTOR or
                surface.get("gpc_autopoll") != "never-written" or
                surface.get("allowlist_only") is not True):
            errors.append("%s semantic write allowlist is not asserted" % label)
        endpoints = snapshot.get("gpc_bjt_endpoints")
        expected_endpoints = [(gpc, bjt) for gpc in range(8) for bjt in range(3)]
        actual_endpoints = []
        if isinstance(endpoints, list):
            for endpoint_index, endpoint in enumerate(endpoints):
                if isinstance(endpoint, dict):
                    errors.extend(_exact_keys(endpoint, {
                        "gpc", "bjt", "selector", "selector_before",
                        "selector_after", "temperature",
                        "offset_temperature", "endpoint_kind",
                        "physical_population", "journal_state",
                    }, "%s endpoint[%d]" % (label, endpoint_index)))
                    pair = (endpoint.get("gpc"), endpoint.get("bjt"))
                    actual_endpoints.append(pair)
                    pair_valid = (
                        _is_int(pair[0], 0, 7) and _is_int(pair[1], 0, 2)
                    )
                    if (not pair_valid or
                            endpoint.get("selector") != "0x%08x" % (
                                (pair[0] << 16) | pair[1]
                                if pair_valid else 0)):
                        errors.append("%s endpoint selector mismatch" % label)
                    if (endpoint.get("journal_state") != "complete" or
                            endpoint.get("endpoint_kind") !=
                            "architecturally-addressable-gpc-bjt"):
                        errors.append("%s endpoint journal metadata mismatch" % label)
                    errors.extend(_validate_temperature_readout(
                        endpoint.get("temperature"), "%s endpoint" % label,
                        expected_encoding="valid-bit",
                    ))
                    errors.extend(_validate_temperature_readout(
                        endpoint.get("offset_temperature"),
                        "%s endpoint offset" % label,
                        expected_encoding="valid-bit",
                    ))
        if actual_endpoints != expected_endpoints:
            errors.append("%s semantic endpoint matrix mismatch" % label)
        if backend == "xray-therm":
            for endpoint in endpoints if isinstance(endpoints, list) else ():
                if not isinstance(endpoint, dict):
                    errors.append("%s dedicated-helper endpoint is malformed" % label)
                    break
                expected_selector = endpoint.get("selector")
                if (endpoint.get("selector_before") != expected_selector or
                        endpoint.get("selector_after") != expected_selector):
                    errors.append("%s dedicated-helper endpoint selector race" % label)
                    break
        observed_population = [
            {"gpc": endpoint.get("gpc"), "bjt": endpoint.get("bjt")}
            for endpoint in endpoints if isinstance(endpoint, dict) and
            isinstance(endpoint.get("temperature"), dict) and
            endpoint["temperature"].get("state") == "valid"
        ] if isinstance(endpoints, list) else []
        population_errors, _snapshot_population = (
            _validate_temperature_population(
                snapshot.get("population"), "%s semantic" % label,
                observed_population,
            )
        )
        errors.extend(population_errors)
        if not _is_int(snapshot.get("valid_endpoint_count"),
                       len(observed_population), len(observed_population)):
            errors.append("%s semantic valid-endpoint count mismatch" % label)
        for endpoint in endpoints if isinstance(endpoints, list) else ():
            if (not isinstance(endpoint, dict) or
                    endpoint.get("physical_population") != (
                        "cross-checked-against-pre-snapshot-boot-static-"
                        "population-masks")):
                errors.append("%s endpoint population proof is missing" % label)
                break
        if backend == "gpcprobe":
            population = snapshot.get("population")
            expected_words = {
                "opt_ts_dis_mask": population.get("opt_ts_dis_mask")
                if isinstance(population, dict) else None,
                "status_opt_gpc": population.get("status_opt_gpc")
                if isinstance(population, dict) else None,
            }
            if (snapshot.get("population_masks_unchanged") is not True or
                    snapshot.get("population_entry") != expected_words or
                    snapshot.get("population_exit") != expected_words):
                errors.append("%s semantic population masks changed" % label)
        snapshot_aggregates = snapshot.get("snapshot_aggregate_readouts")
        if not isinstance(snapshot_aggregates, list) or len(snapshot_aggregates) != 4:
            errors.append("%s snapshot aggregate count mismatch" % label)
        else:
            for index, (aggregate, specification) in enumerate(zip(
                    snapshot_aggregates, TEMPERATURE_SNAPSHOT_AGGREGATES)):
                name, offset = specification
                errors.extend(_validate_temperature_readout(
                    aggregate, "%s snapshot aggregate[%d]" % (label, index),
                    expected_encoding="valid-bit", expected_name=name,
                    expected_offset=offset,
                ))
                if (not isinstance(aggregate, dict) or
                        aggregate.get("journal_valid") is not True):
                    errors.append("%s snapshot aggregate journal is not valid" % label)
    if direct_status in ("ok", "partial"):
        handle_codes = []
        for record in records if isinstance(records, list) else ():
            census_record = (record.get("driver_temperature_census")
                             if isinstance(record, dict) else None)
            direct_card = (census_record.get("canonical_direct_nvml")
                           if isinstance(census_record, dict) else None)
            handle_codes.append(_nvml_return_code(
                direct_card.get("handle_lookup_return")
                if isinstance(direct_card, dict) else None
            ))
        expected_direct_status = (
            "partial" if any(code != 0 for code in handle_codes) else "ok"
        )
        if (len(handle_codes) != len(expected_bdfs) or
                any(code is None for code in handle_codes) or
                direct_status != expected_direct_status):
            errors.append("direct NVML summary status does not match handle outcomes")
    if backend == "xray-therm" and len(helper_generations) != 1:
        errors.append("dedicated-helper generation is missing or inconsistent")
    if seen_bdfs != expected_bdfs:
        errors.append("temperature BDF set does not exactly match selected targets")
    return errors


def run_temperature(cards, args, runner):
    script = Path(__file__).resolve().with_name("temperature_probe.py")
    if not script.is_file():
        raise DiagnosticError("temperature probe script is absent: %s" % script)
    python = find_executable((args.python, sys.executable, "python3"))
    if python is None:
        raise DiagnosticError("Python interpreter is unavailable for temperature probe")
    if not cards:
        return {"status": "skipped", "reason": "no selected GA100 targets"}
    component_paths = {
        name: Path(__file__).resolve().with_name(filename)
        for name, filename in (
            ("temperature_probe", "temperature_probe.py"),
            ("xray_therm", "xray_therm.py"),
            ("nvml_temperature", "nvml_temperature.py"),
        )
    }
    component_identity = {
        name: {
            "path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None,
        }
        for name, path in component_paths.items()
    }
    command = [
        python, str(script), "--device", args.temperature_probe_device,
        "--probe-backend", getattr(args, "temperature_probe_backend", "auto"),
        "--sysfs-root", args.sysfs_root, "--access", args.temperature_access,
        "--max-utilization", str(args.temperature_max_utilization),
    ]
    for card in cards:
        command.extend(("--bdf", card["bdf"]))
    if args.temperature_acknowledge_selector_write:
        command.append("--acknowledge-selector-write")
    if getattr(args, "temperature_acknowledge_bound_snapshot", False):
        command.append("--acknowledge-bound-snapshot")
    if getattr(args, "temperature_acknowledge_hbm_data_read_to_advance", False):
        command.append("--acknowledge-hbm-data-read-to-advance")
    if args.temperature_allow_active_workload:
        command.append("--allow-active-workload")
    if args.temperature_no_driver_queries:
        command.append("--no-driver-queries")
    record = runner.run(command, timeout=args.command_timeout)
    parsed = None
    text = record.get("stdout", "").strip()
    if text:
        try:
            candidate = _strict_json_loads(text)
            if isinstance(candidate, dict):
                parsed = candidate
        except (ValueError, DiagnosticError):
            pass
    validation_errors = [] if args.dry_run else validate_temperature_document(
        parsed, cards, args, expected_component_identity=component_identity
    )
    actual_bdfs = set()
    if isinstance(parsed, dict) and isinstance(parsed.get("cards"), list):
        for item in parsed["cards"]:
            try:
                actual_bdfs.add(normalize_bdf(item.get("identity", {}).get("bdf")))
            except (AttributeError, DiagnosticError):
                pass
    expected_bdfs = {card["bdf"] for card in cards}
    return {
        "status": "planned" if args.dry_run else (
            "pass" if record.get("returncode") == 0 and not validation_errors
            else "failed"
        ),
        "access": args.temperature_access,
        "target_bdfs": sorted(expected_bdfs),
        "target_bdfs_verified": actual_bdfs == expected_bdfs,
        "temperature_probe": str(script),
        "temperature_probe_sha256": sha256_file(script),
        "temperature_components": component_identity,
        "validation_errors": validation_errors,
        "result": parsed,
        "command": record,
    }


def run_p2p(selection, binary, args, runner):
    if not selection["pair_available"]:
        return {
            "status": "skipped", "reason": "fewer than two eligible GA100 CUDA devices",
            "selection": selection,
        }
    first, second = selection["cards"]
    if not first.get("cuda") or not second.get("cuda"):
        return {
            "status": "planned" if args.dry_run else "failed",
            "reason": "verified CUDA runtime ordinal unavailable",
            "selection": selection,
        }
    source = first["cuda"]["cuda_index"]
    destination = second["cuda"]["cuda_index"]
    power = BenchmarkPowerEvidence("p2p", [first, second], args, runner)
    if not power.prepare():
        evidence = power.document()
        return {
            "status": "failed", "reason": "power evidence preparation failed",
            "selection": selection, "power_evidence": evidence,
            "power_evidence_validation_errors": validate_power_evidence(
                evidence, [first, second], args, "p2p"
            ),
        }
    try:
        record = runner.run([
            str(binary), "p2p", "--source", str(source),
            "--destination", str(destination), "--min", str(args.p2p_min_bytes),
            "--max", str(args.p2p_max_bytes),
            "--iterations", str(args.p2p_iterations),
        ], timeout=args.command_timeout)
    finally:
        evidence = power.finish()
    power_errors = validate_power_evidence(
        evidence, [first, second], args, "p2p"
    )
    parsed = parse_json_stdout(record)
    validation_errors = validate_p2p_document(parsed, first, second, args)
    targets_verified = False
    if isinstance(parsed, dict):
        source_record = parsed.get("source")
        destination_record = parsed.get("destination")
        if isinstance(source_record, dict) and isinstance(destination_record, dict):
            try:
                targets_verified = (
                    normalize_bdf(source_record.get("pci_bdf")) == first["bdf"] and
                    normalize_bdf(destination_record.get("pci_bdf")) == second["bdf"]
                )
            except DiagnosticError:
                pass
    return {
        "status": "planned" if args.dry_run else (
            "pass" if record.get("returncode") == 0 and targets_verified and
            not validation_errors and not power_errors and
            evidence.get("status") in ("pass", "not-requested")
            else "failed"
        ),
        "selection": selection, "command": record,
        "target_bdfs_verified": targets_verified,
        "validation_errors": validation_errors,
        "power_evidence": evidence,
        "power_evidence_validation_errors": power_errors,
        "result": parsed,
    }


def _link_values(device_path):
    path = Path(device_path)
    for candidate in (path, path.parent, path.parent.parent):
        if (candidate / "current_link_speed").is_file():
            current_speed = read_text(candidate / "current_link_speed")
            max_speed = read_text(candidate / "max_link_speed")
            current_width = read_text(candidate / "current_link_width")
            max_width = read_text(candidate / "max_link_width")
            return {
                "pci_path": str(candidate),
                "numa_node": read_numa_near(candidate),
                "bdf": candidate.name if BDF_RE.fullmatch(candidate.name) else None,
                "current_link_speed": current_speed,
                "current_link_gen": parse_link_speed(current_speed),
                "current_link_width": (
                    int(current_width) if current_width and current_width.isdigit() else None
                ),
                "max_link_speed": max_speed,
                "max_link_gen": parse_link_speed(max_speed),
                "max_link_width": (
                    int(max_width) if max_width and max_width.isdigit() else None
                ),
            }
    return {}


def inventory_nvme(sysfs_root):
    root = Path(sysfs_root) / "class" / "nvme"
    devices = []
    if not root.is_dir():
        return devices
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not NVME_RE.fullmatch(entry.name):
            continue
        try:
            device_path = (entry / "device").resolve(strict=True)
        except OSError:
            continue
        item = {"controller": entry.name, "device_path": str(device_path)}
        item.update(_link_values(device_path))
        devices.append(item)
    return devices


def _unescape_mount(value):
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def mount_for_path(path, mountinfo_path="/proc/self/mountinfo"):
    target = Path(path).resolve()
    candidates = []
    try:
        lines = Path(mountinfo_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DiagnosticError("cannot read mountinfo: %s" % exc)
    for line in lines:
        fields = line.split()
        if "-" not in fields or len(fields) < 10:
            continue
        separator = fields.index("-")
        if separator + 2 >= len(fields):
            continue
        mountpoint = Path(_unescape_mount(fields[4])).resolve()
        try:
            target.relative_to(mountpoint)
        except ValueError:
            continue
        candidates.append({
            "mountpoint": str(mountpoint),
            "source": _unescape_mount(fields[separator + 2]),
            "filesystem": fields[separator + 1],
            "major_minor": fields[2],
        })
    if not candidates:
        raise DiagnosticError("no mount covers scratch path %s" % target)
    return max(candidates, key=lambda item: len(item["mountpoint"]))


def nvme_controller_for_scratch(path, nvme_devices, sysfs_root,
                                mountinfo_path="/proc/self/mountinfo"):
    scratch = Path(path).resolve()
    if not scratch.is_dir() or not os.access(str(scratch), os.W_OK | os.X_OK):
        raise DiagnosticError("GDS scratch is not a writable directory: %s" % scratch)
    mount = mount_for_path(scratch, mountinfo_path=mountinfo_path)
    controller = None
    source_name = Path(mount["source"]).name
    sysroot = Path(sysfs_root)
    block_path = sysroot / "class" / "block" / source_name
    try:
        resolved = block_path.resolve(strict=True)
        for part in resolved.parts:
            if NVME_RE.fullmatch(part):
                controller = part
    except OSError:
        pass
    if controller is None:
        match = re.match(r"(nvme[0-9]+)n[0-9]+", source_name)
        if match:
            controller = match.group(1)
    device = next(
        (item for item in nvme_devices if item["controller"] == controller), None
    )
    return {
        "scratch": str(scratch), "mount": mount,
        "block_source": source_name, "controller": controller,
        "nvme": device,
    }


def select_fastest_scratch(mappings):
    usable = [item for item in mappings if item.get("nvme")]
    ranked = sorted(
        usable,
        key=lambda item: (
            -(item["nvme"].get("current_link_gen") or -1),
            -(item["nvme"].get("current_link_width") or -1),
            -(item["nvme"].get("max_link_gen") or -1),
            -(item["nvme"].get("max_link_width") or -1),
            item["scratch"],
        ),
    )
    return {
        "selected": ranked[0] if ranked else None,
        "ranking": [item["scratch"] for item in ranked],
        "policy": (
            "explicit writable scratch paths sorted by their NVMe controller's "
            "negotiated PCIe generation and width, advertised generation and width, "
            "then resolved scratch path"
        ),
    }


PCI_ROOT_RE = re.compile(r"^pci[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}$")


def _pci_root_name(path):
    if not path:
        return None
    roots = [part for part in Path(path).parts if PCI_ROOT_RE.fullmatch(part)]
    return roots[-1].lower() if roots else None


def _topology_relationship(gpu, nvme):
    gpu_path = Path(gpu.get("pci_path") or "/").parts
    nvme_path = Path(nvme.get("pci_path") or nvme.get("device_path") or "/").parts
    common_count = 0
    for gpu_part, nvme_part in zip(gpu_path, nvme_path):
        if gpu_part != nvme_part:
            break
        common_count += 1
    ancestry_distance = (
        len(gpu_path) + len(nvme_path) - 2 * common_count
        if gpu.get("pci_path") and (nvme.get("pci_path") or nvme.get("device_path"))
        else 1 << 30
    )
    gpu_root = _pci_root_name(gpu.get("pci_path"))
    nvme_root = _pci_root_name(
        nvme.get("pci_path") or nvme.get("device_path")
    )
    gpu_numa = gpu.get("numa_node")
    nvme_numa = nvme.get("numa_node")
    common_ancestor = str(Path(*gpu_path[:common_count])) if common_count else None
    return {
        "same_pci_root": bool(gpu_root and gpu_root == nvme_root),
        "gpu_pci_root": gpu_root,
        "nvme_pci_root": nvme_root,
        "same_numa_node": bool(
            _is_int(gpu_numa, minimum=0) and
            _is_int(nvme_numa, minimum=0) and gpu_numa == nvme_numa
        ),
        "gpu_numa_node": gpu_numa,
        "nvme_numa_node": nvme_numa,
        "sysfs_common_ancestor": common_ancestor,
        "sysfs_ancestry_distance": ancestry_distance,
    }


def _link_rank(item):
    return tuple(
        -(item.get(key) if _is_int(item.get(key), minimum=0) else -1)
        for key in (
            "current_link_gen", "current_link_width",
            "max_link_gen", "max_link_width",
        )
    )


def select_gds_pair(cards, mappings):
    gpus = [
        item for item in cards
        if item.get("diagnostic_eligible") and isinstance(item.get("cuda"), dict)
    ]
    scratch = [item for item in mappings if isinstance(item.get("nvme"), dict)]
    ranked = []
    for gpu in gpus:
        for mapping in scratch:
            nvme = mapping["nvme"]
            topology = _topology_relationship(gpu, nvme)
            sort_key = (
                _link_rank(gpu) + _link_rank(nvme) +
                (0 if topology["same_pci_root"] else 1,
                 0 if topology["same_numa_node"] else 1,
                 topology["sysfs_ancestry_distance"],
                 gpu["bdf"], mapping["scratch"])
            )
            ranked.append({
                "gpu": gpu,
                "scratch": mapping,
                "topology": topology,
                "gpu_link_rank": {
                    key: gpu.get(key) for key in (
                        "current_link_gen", "current_link_width",
                        "max_link_gen", "max_link_width",
                    )
                },
                "nvme_link_rank": {
                    key: nvme.get(key) for key in (
                        "current_link_gen", "current_link_width",
                        "max_link_gen", "max_link_width",
                    )
                },
                "sort_key": list(sort_key),
            })
    ranked.sort(key=lambda item: tuple(item["sort_key"]))
    return {
        "selected": ranked[0] if ranked else None,
        "candidates": ranked,
        "policy": (
            "joint GPU/NVMe pairs sorted first by GPU negotiated generation/width "
            "and advertised generation/width, then NVMe negotiated and advertised "
            "generation/width; ties prefer a shared PCI root, shared NUMA node, "
            "shortest resolved-sysfs ancestry distance, GPU BDF, then scratch path"
        ),
    }


def find_gds_tool(name):
    candidates = [name]
    for root in sorted(Path("/usr/local").glob("cuda*")):
        candidates.append(str(root / "gds" / "tools" / name))
    return find_executable(candidates)


GDS_RESULT_RE = re.compile(
    r"IoType:\s*(?P<io_type>\S+)\s+XferType:\s*(?P<xfer_type>\S+).*?"
    r"Throughput:\s*(?P<throughput>\S+)\s*"
    r"(?P<throughput_unit>GiB/(?:s|sec)|GB/(?:s|sec)).*?"
    r"Avg[._\s]*Latency:\s*(?P<latency>\S+)\s*"
    r"(?P<latency_unit>us|usec|usecs)",
    re.I,
)


def _finite_float_token(token):
    try:
        value = float(token)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def parse_gds_metrics(text, expected_io_type=None, require_verification=False):
    lines = str(text).splitlines()
    records = []
    malformed = []
    marker_lines = []
    for line_number, line in enumerate(lines):
        if line.strip() == "Verifying data":
            marker_lines.append(line_number)
        if "IoType:" not in line:
            continue
        match = GDS_RESULT_RE.search(line)
        if match is None:
            malformed.append({"line_number": line_number, "text": line})
            continue
        throughput = _finite_float_token(match.group("throughput"))
        latency = _finite_float_token(match.group("latency"))
        throughput_unit = match.group("throughput_unit")
        throughput_gib_s = None
        if throughput is not None:
            throughput_gib_s = (
                throughput if throughput_unit.lower().startswith("gib") else
                throughput * (1000.0 ** 3) / (1024.0 ** 3)
            )
        latency_status = (
            "invalid" if latency is None or latency < 0 else
            ("unmeasurable_zero" if latency == 0 else "measured")
        )
        records.append({
            "line_number": line_number,
            "io_type": match.group("io_type").upper(),
            "xfer_type": match.group("xfer_type").upper(),
            "throughput_reported_token": match.group("throughput"),
            "throughput_reported_unit": throughput_unit,
            "throughput_gib_s": throughput_gib_s,
            "throughput_valid": _is_finite_number(
                throughput_gib_s, positive=True
            ),
            "latency_reported_token": match.group("latency"),
            "latency_us": latency,
            "latency_valid": latency_status == "measured",
            "latency_status": latency_status,
        })
    expected = expected_io_type.upper() if expected_io_type else None
    primary_candidates = [
        record for record in records
        if expected is None or record["io_type"] == expected
    ]
    if expected == "WRITE" and marker_lines:
        primary_candidates = [
            record for record in primary_candidates
            if record["line_number"] < marker_lines[0]
        ]
    primary = primary_candidates[0] if primary_candidates else None
    verification_records = []
    if marker_lines:
        verification_records = [
            record for record in records
            if record["line_number"] > marker_lines[0] and
            record["io_type"] == "READ"
        ]
    verification_evidence_valid = bool(
        marker_lines and any(
            record["xfer_type"] == "GPUD" and
            record["throughput_valid"] and
            record["latency_status"] != "invalid"
            for record in verification_records
        )
    )
    transfer_valid = bool(
        primary and not malformed and
        primary["xfer_type"] == "GPUD" and
        primary["throughput_valid"] and
        primary["latency_status"] != "invalid" and
        (not require_verification or verification_evidence_valid)
    )
    metrics = {
        "expected_io_type": expected,
        "records": records,
        "malformed_result_lines": malformed,
        "verification_marker_present": bool(marker_lines),
        "verification_marker_lines": marker_lines,
        "verification_evidence_valid": verification_evidence_valid,
        "transfer_valid": transfer_valid,
    }
    if primary:
        metrics.update({
            "io_type": primary["io_type"],
            "xfer_type": primary["xfer_type"],
            "throughput_reported_value": _finite_float_token(
                primary["throughput_reported_token"]
            ),
            "throughput_reported_unit": primary["throughput_reported_unit"],
            "throughput_gib_s": primary["throughput_gib_s"],
            "throughput_valid": primary["throughput_valid"],
            "latency_us": primary["latency_us"],
            "latency_valid": primary["latency_valid"],
            "latency_status": primary["latency_status"],
        })
    return metrics


def validate_gds_metrics(metrics):
    return isinstance(metrics, dict) and metrics.get("transfer_valid") is True


def gds_size_argument(value):
    for suffix, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if value >= scale and value % scale == 0:
            return "%d%s" % (value // scale, suffix)
    return str(value)


def run_gds(cards, args, runner, sysfs_root, owned_roots):
    if not args.gds_scratch:
        return {"status": "skipped", "reason": "no explicit --gds-scratch supplied"}
    nvme = inventory_nvme(sysfs_root)
    mappings = [
        nvme_controller_for_scratch(path, nvme, sysfs_root)
        for path in args.gds_scratch
    ]
    scratch_selection = select_fastest_scratch(mappings)
    eligible_cards = [
        item for item in cards
        if item.get("diagnostic_eligible") and isinstance(item.get("cuda"), dict)
    ]
    joint_selection = select_gds_pair(cards, mappings)
    selected_pair = joint_selection["selected"]
    selected_gpu = selected_pair["gpu"] if selected_pair else None
    selected_scratch = selected_pair["scratch"] if selected_pair else None
    gdscheck = find_gds_tool("gdscheck.py") or find_gds_tool("gdscheck")
    gdsio = find_gds_tool("gdsio")
    module_active = False
    modules = read_text("/proc/modules") or ""
    module_active = any(
        line.split()[0] == "nvidia_fs" for line in modules.splitlines() if line
    )
    capability = {
        "nvidia_fs_active": module_active,
        "gdscheck": gdscheck,
        "gdsio": gdsio,
        "nvme_inventory": nvme,
        "scratch_selection": scratch_selection,
        "joint_gpu_nvme_selection": joint_selection,
        "gpu_selection": selected_gpu,
    }
    if not eligible_cards:
        return {"status": "skipped", "reason": "no eligible CUDA GA100", "capability": capability}
    if not isinstance(selected_scratch, dict):
        return {"status": "skipped", "reason": "scratch path is not mapped to NVMe", "capability": capability}
    if not module_active or not gdscheck or not gdsio:
        return {"status": "skipped", "reason": "GDS capability gate failed", "capability": capability}
    check = runner.run([gdscheck, "-p"], timeout=args.command_timeout)
    if not args.dry_run and check.get("returncode") != 0:
        return {
            "status": "skipped", "reason": "gdscheck -p failed",
            "capability": capability, "gdscheck_command": check,
        }
    # gdsio documents -d in the NVML/nvidia-smi device-index namespace,
    # unlike the CUDA helper and nvmedia scripts which take CUDA ordinals.
    smi = selected_gpu.get("nvidia_smi")
    if smi is None:
        return {
            "status": "skipped",
            "reason": "selected GA100 has no nvidia-smi index",
            "capability": capability,
        }
    gds_device_index = smi["nvidia_smi_index"]
    power = BenchmarkPowerEvidence("gds", [selected_gpu], args, runner)
    if not power.prepare():
        evidence = power.document()
        return {
            "status": "failed", "reason": "power evidence preparation failed",
            "capability": capability,
            "power_evidence": evidence,
            "power_evidence_validation_errors": validate_power_evidence(
                evidence, [selected_gpu], args, "gds"
            ),
        }
    scratch_value = selected_scratch.get("scratch")
    if not isinstance(scratch_value, str):
        return {
            "status": "skipped",
            "reason": "selected scratch path is missing or invalid",
            "capability": capability,
        }
    scratch = Path(scratch_value).resolve()
    owned = Path(tempfile.mkdtemp(
        prefix=".170hx-xray-gds-", dir=str(scratch)
    )).resolve()
    owned_roots.append(owned)
    results = []
    try:
        for size in args.gds_size:
            if not is_power_of_two(size):
                raise DiagnosticError("every --gds-size must be a power of two")
            target = (owned / ("gds-%d.bin" % size)).resolve()
            try:
                target.relative_to(owned)
            except ValueError:
                raise DiagnosticError("internal GDS target escaped owned directory")
            io_size = min(size, args.gds_io_size)
            write = runner.run([
                gdsio, "-f", str(target), "-d", str(gds_device_index),
                "-m", "0", "-w", "1", "-s", gds_size_argument(size),
                "-o", "0", "-i", gds_size_argument(io_size),
                "-x", "0", "-I", "1", "-V",
            ], timeout=args.command_timeout)
            read = None
            if args.dry_run or write.get("returncode") == 0:
                read = runner.run([
                    gdsio, "-f", str(target), "-d", str(gds_device_index),
                    "-m", "0", "-w", "1", "-s", gds_size_argument(size),
                    "-o", "0", "-i", gds_size_argument(io_size),
                    "-x", "0", "-I", "0", "-V",
                ], timeout=args.command_timeout)
            write_metrics = parse_gds_metrics(
                write.get("stdout", ""), expected_io_type="WRITE",
                require_verification=True,
            )
            read_metrics = (
                parse_gds_metrics(
                    read.get("stdout", ""), expected_io_type="READ",
                    require_verification=False,
                ) if read else {}
            )
            results.append({
                "bytes": size, "io_bytes": io_size,
                "write": write, "write_metrics": write_metrics,
                "read": read,
                "read_metrics": read_metrics,
                "write_metrics_valid": validate_gds_metrics(write_metrics),
                "read_metrics_valid": validate_gds_metrics(read_metrics),
                "verification_requested": True,
            })
    finally:
        evidence = power.finish()
        if owned.exists():
            # Refuse broad or substituted cleanup targets.
            if owned.parent != scratch or not owned.name.startswith(".170hx-xray-gds-"):
                raise DiagnosticError("refusing unsafe GDS cleanup target: %s" % owned)
            shutil.rmtree(str(owned))
    power_errors = validate_power_evidence(
        evidence, [selected_gpu], args, "gds"
    )
    return {
        "status": "planned" if args.dry_run else (
            "pass" if all(
                item["write"].get("returncode") == 0 and
                item["read"] and item["read"].get("returncode") == 0 and
                item["write_metrics_valid"] and item["read_metrics_valid"]
                for item in results
            ) and not power_errors and
            evidence.get("status") in ("pass", "not-requested") else "failed"
        ),
        "capability": capability, "gdscheck_command": check,
        "joint_gpu_nvme_selection": joint_selection,
        "gds_device_index": gds_device_index,
        "gds_device_index_namespace": "nvidia-smi/NVML",
        "owned_temporary_directory_removed": not owned.exists(),
        "power_evidence": evidence,
        "power_evidence_validation_errors": power_errors,
        "results": results,
    }


def safe_extract_tar(archive, destination, expected_sha256,
                     max_files=128, max_total_bytes=128 << 20):
    archive = Path(archive).resolve()
    destination = Path(destination).resolve()
    actual = sha256_file(archive)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        raise DiagnosticError("media archive requires an exact SHA-256")
    if actual.lower() != expected_sha256.lower():
        raise DiagnosticError("media archive SHA-256 mismatch")
    count = 0
    total = 0
    with tarfile.open(str(archive), mode="r:*") as bundle:
        members = bundle.getmembers()
        for member in members:
            count += 1
            total += max(0, member.size)
            if count > max_files or total > max_total_bytes:
                raise DiagnosticError("media archive exceeds extraction limits")
            if not (member.isdir() or member.isfile()):
                raise DiagnosticError(
                    "media archive contains a non-regular/non-directory member"
                )
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError:
                raise DiagnosticError("media archive path escapes destination")
            if Path(member.name).is_absolute():
                raise DiagnosticError("media archive contains an absolute path")
        # Extract manually instead of applying archive metadata. This keeps the
        # same safe behavior on Python 3.9 through 3.15 and never creates links,
        # devices, archive-selected owners, or archive-selected permissions.
        for member in members:
            target = (destination / member.name).resolve()
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise DiagnosticError("unable to read regular archive member")
            with source, open(str(target), "xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(str(target), 0o600)
            if target.stat().st_size != member.size:
                raise DiagnosticError("short media archive extraction")
    return {
        "path": str(archive), "sha256": actual,
        "file_count": count, "uncompressed_bytes": total,
    }


def _find_one(root, name):
    matches = sorted(Path(root).rglob(name))
    return matches[0] if len(matches) == 1 else None


def run_media(cards, args, runner, owned_roots):
    if not args.media_suite:
        return {"status": "skipped", "reason": "no --media-suite archive supplied"}
    extracted = Path(tempfile.mkdtemp(prefix="170hx-xray-media-"))
    owned_roots.append(extracted)
    active_power = None
    try:
        archive = safe_extract_tar(
            args.media_suite, extracted, args.media_suite_sha256,
        )
        scripts = {
            name: _find_one(extracted, name) for name in (
                "nvdec_probe.py", "nvdec_decode.py", "nvjpg_probe.py",
            )
        }
        if any(value is None for value in scripts.values()):
            raise DiagnosticError("media suite lacks a unique required probe script")
        python = find_executable((args.python, sys.executable, "python3"))
        if python is None:
            raise DiagnosticError("Python interpreter is unavailable for media suite")
        ffmpeg = find_executable((args.ffmpeg, "ffmpeg"))
        script_evidence = {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in scripts.items()
        }
        results = []
        for card in cards:
            if not card.get("diagnostic_eligible") or not card.get("cuda"):
                continue
            cuda_index = card["cuda"]["cuda_index"]
            smi = card.get("nvidia_smi")
            uuid = smi.get("uuid") if isinstance(smi, dict) else None
            if not uuid:
                results.append({
                    "bdf": card["bdf"], "cuda_index": cuda_index,
                    "status": "failed",
                    "reason": "media target lacks a UUID for exact CUDA visibility isolation",
                })
                continue
            isolated_env = {
                "CUDA_VISIBLE_DEVICES": uuid,
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            }
            card_dir = extracted / ("results-%s" % card["bdf"].replace(":", "-"))
            card_dir.mkdir(mode=0o700)
            power = BenchmarkPowerEvidence("media", [card], args, runner)
            active_power = power
            if not power.prepare():
                evidence = power.document()
                results.append({
                    "bdf": card["bdf"], "cuda_index": cuda_index,
                    "status": "failed",
                    "reason": "power evidence preparation failed",
                    "power_evidence": evidence,
                    "power_evidence_validation_errors": validate_power_evidence(
                        evidence, [card], args, "media"
                    ),
                })
                active_power = None
                continue
            probes = {}
            for key, script_name in (
                ("nvdec_caps", "nvdec_probe.py"),
                ("nvdec_decode", "nvdec_decode.py"),
                ("nvjpg", "nvjpg_probe.py"),
            ):
                output = card_dir / (key + ".json")
                command = [
                    python, str(scripts[script_name]), "--device", "0",
                ]
                # The attachment's caps/JPEG probes have JSON output flags;
                # its end-to-end NVDEC decoder intentionally reports by exit
                # status/stdout only.
                if script_name != "nvdec_decode.py":
                    command.extend(("--out", str(output)))
                record = runner.run(
                    command, cwd=str(scripts[script_name].parent),
                    env=isolated_env, timeout=args.command_timeout,
                )
                parsed = None
                if output.is_file():
                    try:
                        parsed = _strict_json_loads(
                            output.read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError, ValueError, DiagnosticError):
                        pass
                requires_json = script_name != "nvdec_decode.py"
                expected_engine = "NVDEC" if key == "nvdec_caps" else (
                    "NVJPG" if key == "nvjpg" else None
                )
                document_valid = (
                    not requires_json or (
                        isinstance(parsed, dict) and
                        parsed.get("engine") == expected_engine and
                        isinstance(parsed.get("verdict"), str) and
                        bool(parsed.get("verdict"))
                    )
                )
                command_completed = (
                    record.get("status") == "completed" and
                    isinstance(record.get("returncode"), int)
                )
                probe_status = (
                    "planned" if args.dry_run else
                    ("failed" if not command_completed or not document_valid else
                     ("positive" if record.get("returncode") == 0 else "negative"))
                )
                probes[key] = {
                    "command": record,
                    "result": parsed,
                    "status": probe_status,
                    "document_required": requires_json,
                    "document_valid": document_valid,
                }
            nvenc = {
                "status": "skipped", "reason": "ffmpeg with h264_nvenc unavailable",
            }
            if ffmpeg:
                encoders = runner.run([ffmpeg, "-hide_banner", "-encoders"])
                if "h264_nvenc" in encoders.get("stdout", "") or args.dry_run:
                    command = runner.run([
                        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
                        "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
                        "-frames:v", "30", "-c:v", "h264_nvenc", "-gpu",
                        "0", "-f", "null", "-",
                    ], env=isolated_env, timeout=args.command_timeout)
                    nvenc = {
                        "status": (
                            "planned" if args.dry_run else
                            ("failed" if command.get("status") != "completed" else
                             ("positive" if command.get("returncode") == 0 else
                              "negative"))
                        ),
                        "command": command,
                        "criterion": "30 synthetic frames accepted by h264_nvenc",
                    }
                else:
                    nvenc["encoder_inventory_command"] = encoders
            evidence = power.finish()
            power_errors = validate_power_evidence(
                evidence, [card], args, "media"
            )
            active_power = None
            results.append({
                "bdf": card["bdf"], "cuda_index": cuda_index,
                "status": (
                    "planned" if args.dry_run else
                    ("failed" if any(
                        item.get("status") == "failed" for item in probes.values()
                    ) or nvenc.get("status") == "failed" or power_errors or
                     evidence.get("status") not in ("pass", "not-requested")
                     else "completed")
                ),
                "isolated_visible_cuda_index": 0,
                "cuda_visible_devices": uuid,
                "probes": probes, "nvenc": nvenc,
                "power_evidence": evidence,
                "power_evidence_validation_errors": power_errors,
            })
        return {
            "status": "planned" if args.dry_run else (
                "failed" if any(item.get("status") == "failed" for item in results)
                else "completed"
            ),
            "archive": archive, "probe_scripts": script_evidence,
            "results": results,
            "interpretation": (
                "authoritative API/decode results are recorded verbatim; nvidia-smi "
                "decoder utilization alone is not treated as engine presence"
            ),
        }
    finally:
        if active_power is not None:
            active_power.finish()
        if extracted.exists():
            if not extracted.name.startswith("170hx-xray-media-") or extracted.parent != Path(tempfile.gettempdir()).resolve():
                raise DiagnosticError("refusing unsafe media cleanup target")
            shutil.rmtree(str(extracted))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=(
            "inventory", "temperature", "bandwidth", "p2p", "gds", "media", "full"
        ),
    )
    parser.add_argument("--output", default="-", help="new JSON file or - for stdout")
    parser.add_argument("--sysfs-root", default="/sys")
    parser.add_argument("--bdf", action="append", help="exact GA100 BDF; repeatable")
    parser.add_argument("--include-2080-observation", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=900)
    parser.add_argument("--work-dir", help="existing directory for temporary build files")
    parser.add_argument("--cuda-binary")
    parser.add_argument("--nvcc")
    parser.add_argument("--bandwidth-bytes", type=parse_positive_size, default=512 << 20)
    parser.add_argument("--bandwidth-iterations", type=int, default=50)
    parser.add_argument("--p2p-min-bytes", type=parse_positive_size, default=4 << 10)
    parser.add_argument("--p2p-max-bytes", type=parse_positive_size, default=1 << 30)
    parser.add_argument("--p2p-iterations", type=int, default=20)
    parser.add_argument(
        "--power-limit-watts", type=float, default=150.0,
        help=(
            "transient benchmark power limit; verified before/after each family "
            "and restored to the observed original value (default: 150)"
        ),
    )
    parser.add_argument(
        "--power-sample-interval-ms", type=int, default=100,
        help="periodic nvidia-smi power sampling interval (default: 100)",
    )
    parser.add_argument("--gds-scratch", action="append")
    parser.add_argument("--gds-size", type=parse_positive_size, action="append")
    parser.add_argument("--gds-io-size", type=parse_positive_size, default=1 << 20)
    parser.add_argument("--media-suite")
    parser.add_argument("--media-suite-sha256")
    parser.add_argument("--python")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--temperature-probe-device", default="auto")
    parser.add_argument(
        "--temperature-probe-backend",
        choices=("auto", "xray-therm", "gpcprobe"), default="auto",
    )
    parser.add_argument(
        "--temperature-access", choices=("bar-read", "semantic-read"),
        default="bar-read",
    )
    parser.add_argument(
        "--temperature-acknowledge-selector-write", action="store_true"
    )
    parser.add_argument(
        "--temperature-acknowledge-bound-snapshot", action="store_true"
    )
    parser.add_argument(
        "--temperature-acknowledge-hbm-data-read-to-advance",
        action="store_true",
    )
    parser.add_argument("--temperature-allow-active-workload", action="store_true")
    parser.add_argument("--temperature-max-utilization", type=float, default=5.0)
    parser.add_argument("--temperature-no-driver-queries", action="store_true")
    return parser


def validate_args(args):
    if args.command_timeout <= 0:
        raise DiagnosticError("--command-timeout must be positive")
    for name in ("bandwidth_iterations", "p2p_iterations"):
        value = getattr(args, name)
        if value <= 0 or value > MAX_ITERATIONS:
            raise DiagnosticError(
                "--%s must be in [1, %d]" %
                (name.replace("_", "-"), MAX_ITERATIONS)
            )
    if (not _is_finite_number(args.power_limit_watts, positive=True) or
            args.power_limit_watts > 1000):
        raise DiagnosticError("--power-limit-watts must be finite and in (0, 1000]")
    if not _is_int(args.power_sample_interval_ms, 10, 10000):
        raise DiagnosticError(
            "--power-sample-interval-ms must be in [10, 10000]"
        )
    if args.bandwidth_bytes < 4096 or args.bandwidth_bytes > MAX_CUDA_BUFFER_BYTES:
        raise DiagnosticError(
            "--bandwidth-bytes must be in [4096, %d]" % MAX_CUDA_BUFFER_BYTES
        )
    if not is_power_of_two(args.p2p_min_bytes) or not is_power_of_two(args.p2p_max_bytes):
        raise DiagnosticError("P2P min/max sizes must be powers of two")
    if args.p2p_max_bytes < args.p2p_min_bytes:
        raise DiagnosticError("P2P maximum is below minimum")
    if args.p2p_max_bytes > MAX_CUDA_BUFFER_BYTES:
        raise DiagnosticError(
            "--p2p-max-bytes exceeds %d" % MAX_CUDA_BUFFER_BYTES
        )
    if args.gds_size is None:
        args.gds_size = list(POWER_OF_TWO_DEFAULTS)
    if not all(is_power_of_two(item) for item in args.gds_size):
        raise DiagnosticError("every GDS size must be a power of two")
    if any(item > MAX_GDS_BYTES for item in args.gds_size):
        raise DiagnosticError("a GDS size exceeds %d" % MAX_GDS_BYTES)
    if args.gds_io_size > MAX_GDS_BYTES:
        raise DiagnosticError("--gds-io-size exceeds %d" % MAX_GDS_BYTES)
    if args.media_suite and not args.media_suite_sha256:
        raise DiagnosticError("--media-suite requires --media-suite-sha256")
    if (not isinstance(args.temperature_probe_device, str) or
            not args.temperature_probe_device.strip()):
        raise DiagnosticError("--temperature-probe-device must not be empty")
    if (not math.isfinite(args.temperature_max_utilization) or
            not 0.0 <= args.temperature_max_utilization <= 100.0):
        raise DiagnosticError(
            "--temperature-max-utilization must be finite and in [0, 100]"
        )
    temperature_requested = args.mode in ("temperature", "full")
    if (temperature_requested and args.temperature_access == "semantic-read" and
            not args.temperature_acknowledge_selector_write):
        raise DiagnosticError(
            "semantic temperature access requires "
            "--temperature-acknowledge-selector-write"
        )
    if (args.temperature_acknowledge_selector_write and
            args.temperature_access != "semantic-read"):
        raise DiagnosticError(
            "--temperature-acknowledge-selector-write requires semantic-read"
        )
    if (temperature_requested and args.temperature_access == "semantic-read" and
            args.temperature_probe_backend == "gpcprobe"):
        raise DiagnosticError(
            "semantic temperature access requires the xray-therm backend"
        )
    if (args.temperature_acknowledge_bound_snapshot and
            args.temperature_access != "semantic-read"):
        raise DiagnosticError(
            "--temperature-acknowledge-bound-snapshot requires semantic-read"
        )
    if (args.temperature_acknowledge_bound_snapshot and
            args.temperature_probe_backend == "gpcprobe"):
        raise DiagnosticError(
            "--temperature-acknowledge-bound-snapshot requires the xray-therm backend"
        )
    if (args.temperature_acknowledge_hbm_data_read_to_advance and
            args.temperature_access != "semantic-read"):
        raise DiagnosticError(
            "--temperature-acknowledge-hbm-data-read-to-advance requires "
            "semantic-read"
        )
    if (args.temperature_acknowledge_hbm_data_read_to_advance and
            args.temperature_probe_backend == "gpcprobe"):
        raise DiagnosticError(
            "--temperature-acknowledge-hbm-data-read-to-advance requires the "
            "xray-therm backend"
        )
    if (temperature_requested and args.temperature_access == "semantic-read" and
            args.temperature_no_driver_queries and
            not args.temperature_allow_active_workload):
        raise DiagnosticError(
            "semantic temperature access without driver queries requires "
            "--temperature-allow-active-workload"
        )
    if args.mode in ("gds", "full") and args.gds_scratch:
        for path in args.gds_scratch:
            if Path(path).is_symlink():
                raise DiagnosticError("GDS scratch may not be a symlink: %s" % path)


def emit_report(report, output):
    try:
        encoded = (
            _strict_json_dumps(report, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except ValueError as exc:
        raise DiagnosticError("report contains non-finite JSON data") from exc
    if output == "-":
        sys.stdout.buffer.write(encoded)
        return
    path = Path(output).resolve()
    if path.exists():
        raise DiagnosticError("refusing to overwrite output: %s" % path)
    if not path.parent.is_dir():
        raise DiagnosticError("output directory does not exist: %s" % path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.tmp-" % path.name, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
        except FileExistsError as exc:
            raise DiagnosticError("refusing to overwrite output: %s" % path) from exc
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def collect_component_failures(report):
    failures = []
    bandwidth = report.get("bandwidth")
    if isinstance(bandwidth, list):
        failures.extend(
            "bandwidth:%s" % item.get("bdf", "unknown")
            for item in bandwidth
            if isinstance(item, dict) and item.get("status") == "failed"
        )
    for name in ("feature_inventory", "temperature", "p2p", "gds", "media"):
        component = report.get(name)
        if isinstance(component, dict) and component.get("status") == "failed":
            failures.append(name)
    power = report.get("benchmark_power_evidence")
    if isinstance(power, dict) and power.get("status") == "failed":
        failures.append("benchmark_power_evidence")
    return failures


def main(argv=None):
    args = build_parser().parse_args(argv)
    validate_args(args)
    runner = Runner(dry_run=args.dry_run, timeout=args.command_timeout)
    owned_roots = []
    temporary_work = None
    if args.work_dir:
        work_parent = Path(args.work_dir).resolve()
        if not work_parent.is_dir() or not os.access(
                str(work_parent), os.W_OK | os.X_OK):
            raise DiagnosticError("--work-dir is not a directory")
        temporary_work = Path(tempfile.mkdtemp(
            prefix="170hx-xray-diag-", dir=str(work_parent)
        )).resolve()
    else:
        work_parent = Path(tempfile.gettempdir()).resolve()
        temporary_work = Path(
            tempfile.mkdtemp(prefix="170hx-xray-diag-", dir=str(work_parent))
        ).resolve()
    owned_roots.append(temporary_work)
    work = temporary_work
    started = utc_now()
    report = {
        "schema": SCHEMA,
        "tool": {"name": "170HX-XRAY diagnostics", "version": VERSION,
                 "script_sha256": sha256_file(Path(__file__).resolve())},
        "started_utc": started,
        "request": {
            "mode": args.mode, "dry_run": args.dry_run,
            "requested_bdfs": [normalize_bdf(item) for item in (args.bdf or [])],
            "benchmark_power": {
                "requested_limit_watts": args.power_limit_watts,
                "sampling_interval_ms": args.power_sample_interval_ms,
                "control_policy": (
                    "query-original-enforce-verify-sample-verify-restore-verify"
                ),
            },
            "temperature": {
                "access": args.temperature_access,
                "probe_backend": args.temperature_probe_backend,
                "probe_device": args.temperature_probe_device,
                "selector_write_acknowledged": (
                    args.temperature_acknowledge_selector_write
                ),
                "bound_snapshot_acknowledged": (
                    args.temperature_acknowledge_bound_snapshot
                ),
                "hbm_data_read_to_advance_acknowledged": (
                    args.temperature_acknowledge_hbm_data_read_to_advance
                ),
                "active_workload_override": args.temperature_allow_active_workload,
                "max_utilization_percent": args.temperature_max_utilization,
                "driver_queries_enabled": not args.temperature_no_driver_queries,
            },
        },
        "safety": {
            "bar0_access": args.mode in ("temperature", "full"),
            "bar0_access_scope": (
                "temperature MMIO reads plus acknowledged snapshot trigger/selector"
                if args.mode in ("temperature", "full") and
                args.temperature_access == "semantic-read" else
                ("temperature MMIO reads only" if args.mode in ("temperature", "full")
                 else "none")
            ),
            "fuse_access": False, "otp_commands": 0,
            "sudo_or_driver_binding": False, "gpu_reset": False,
            "persistent_gpu_configuration": False,
            "transient_power_limit_control_requested": (
                args.mode in ("bandwidth", "p2p", "gds", "media", "full")
            ),
            "power_limit_restore_required": (
                args.mode in ("bandwidth", "p2p", "gds", "media", "full")
            ),
            "gds_writes_restricted_to_owned_unique_temp": True,
            "feature_inventory_query_only": True,
            "mig_configuration": False,
            "ecc_injection": False,
            "ofa_session_creation": False,
        },
    }
    return_code = 0
    cards = []
    try:
        cards = inventory_ga100(
            args.sysfs_root, runner, requested_bdfs=args.bdf,
            include_glitch_observation=args.include_2080_observation,
        )
        feature_inventory = collect_feature_inventory(cards, args, runner)
        feature_errors = validate_feature_inventory(
            feature_inventory, cards, runner.records
        )
        if feature_errors:
            feature_inventory["validation_errors"] = feature_errors
            feature_inventory["status"] = "failed"
        report["feature_inventory"] = feature_inventory
        # Temperature-only operation intentionally has no CUDA dependency.
        # Other modes inventory through CUDA as well as nvidia-smi because the
        # ordinal namespaces can (and on mixed systems do) diverge.
        binary = None
        if args.mode == "temperature":
            cuda_inventory = {"status": "skipped", "reason": "temperature-only mode"}
            report["cuda_helper"] = {"status": "skipped", "inventory": cuda_inventory}
        else:
            binary, build = ensure_cuda_binary(args, runner, work)
            report["cuda_helper"] = build
            cuda_inventory = run_cuda_inventory(cards, binary, args, runner)
            report["cuda_helper"]["inventory"] = cuda_inventory
        selection = select_top_pair(cards)
        report["inventory"] = {
            "cards": cards, "top_pair_selection": selection,
            "eligible_count": sum(bool(item["diagnostic_eligible"]) for item in cards),
            "cuda_runtime_mapped_count": sum(
                item.get("cuda") is not None for item in cards
            ),
        }
        if (args.mode != "temperature" and not args.dry_run and
                cuda_inventory["status"] != "pass"):
            raise DiagnosticError(
                "CUDA runtime inventory did not map every selected GA100 BDF"
            )
        if args.mode in ("temperature", "full"):
            report["temperature"] = run_temperature(cards, args, runner)
        if args.mode in ("bandwidth", "full"):
            report["bandwidth"] = run_bandwidth(cards, binary, args, runner)
        if args.mode in ("p2p", "full"):
            report["p2p"] = run_p2p(selection, binary, args, runner)
        if args.mode in ("gds", "full"):
            report["gds"] = run_gds(
                cards, args, runner, args.sysfs_root, owned_roots,
            )
        if args.mode in ("media", "full"):
            report["media"] = run_media(cards, args, runner, owned_roots)
        report["commands"] = runner.records
        report["benchmark_power_evidence"] = build_benchmark_power_summary(
            report, cards, args
        )
        power_summary_errors = validate_benchmark_power_summary(
            report["benchmark_power_evidence"], report, cards, args
        )
        if power_summary_errors:
            report["benchmark_power_evidence"]["validation_errors"].extend(
                power_summary_errors
            )
            report["benchmark_power_evidence"]["status"] = "failed"
        report["component_failures"] = collect_component_failures(report)
        if report["component_failures"]:
            report["status"] = "failed"
            return_code = 2
        else:
            report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        return_code = 2
    finally:
        if "benchmark_power_evidence" not in report:
            report["benchmark_power_evidence"] = build_benchmark_power_summary(
                report, cards, args
            )
        report["finished_utc"] = utc_now()
        report["commands"] = runner.records
        report["owned_temporary_roots"] = [str(item) for item in owned_roots]
        if temporary_work and temporary_work.exists():
            if (temporary_work.parent != work_parent or
                    not temporary_work.name.startswith("170hx-xray-diag-")):
                raise DiagnosticError("refusing unsafe work cleanup target")
            shutil.rmtree(str(temporary_work))
        report["temporary_work_removed"] = (
            temporary_work is None or not temporary_work.exists()
        )
    emit_report(report, args.output)
    return return_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (DiagnosticError, OSError, ValueError, tarfile.TarError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
