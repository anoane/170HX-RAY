#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GA100 temperature census through a constrained thermal helper.

The default ``bar-read`` mode issues only 32-bit MMIO reads.  It records the
documented aggregate thermal readouts and FBPA context while deliberately
skipping every stateful FBPA DATA read-to-advance port.  Current and snapshot
selectors are recorded as raw context, but all seven selector-dependent
payload ports are unconditionally omitted because firmware can race their
selectors.  The probe also records the TS-disable and GPC-disable population
masks without claiming an observed endpoint set. ``semantic-read`` additionally triggers one
global thermal snapshot and walks all 8 x 3 architecturally addressable
GPC/BJT endpoints through the dedicated helper only.  That mode writes only the snapshot trigger and snapshot
    selector, restores the selector on every helper path, requires the valid
    endpoint set to match the pre-snapshot boot-static population-mask
    observation (the helper ABI has no exit reread), and never changes GPC
autopoll configuration.  FBPA DATA may be observed only through a separate,
explicit semantic-read acknowledgement.  The dedicated ``170hx_xray_therm`` helper is
preferred; the older domain-less ``gpcprobe`` ABI remains an explicit read-only
fallback and is rejected for ``semantic-read`` because its public ABI has no
write ioctl.

Readout channels are deliberately not called physical sensors.  Aggregate,
offset, maximum, and repeated FBPA views can be derived or aliased.
"""

from __future__ import print_function

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time


SCHEMA = "170hx-xray-ga100-temperature-census-v8"
VERSION = "1.7.0"
XRAY_THERM_CAPTURE_F_SNAPSHOT = 1 << 0
XRAY_THERM_CAPTURE_F_HBM_DATA = 1 << 2
GP_IOC_INFO = 0x00004701
GP_IOC_READ32 = 0xC00C4702
GP_INFO_SIZE = 4 + 16 * 4
SUPPORTED_IDS = frozenset((0x20B5, 0x20C2, 0x2080))
BDF_RE = re.compile(
    r"^(?P<domain>[0-9a-fA-F]{4,8}):(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<device>[0-1][0-9a-fA-F])\.(?P<function>[0-7])$"
)

NV_THERM_TEMP_SENSOR_TSENSE = 0x00020460
NV_THERM_TEMP_SENSOR_OFFSET_TSENSE = 0x00020464
NV_THERM_TEMP_SENSOR_CONSTANT = 0x00020468
NV_THERM_TEMP_SENSOR_MAX = 0x0002046C
NV_THERM_TEMP_SENSOR_GPU_MAX = 0x00020470
NV_THERM_TEMP_SENSOR_GPU_AVG = 0x00020474
NV_THERM_TEMP_SENSOR_GPU_OFFSET_MAX = 0x00020478
NV_THERM_TEMP_SENSOR_GPU_OFFSET_AVG = 0x0002047C

NV_THERM_GPC_TSENSE_INDEX = 0x000206D4
NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR = 0x000206E0
NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR = 0x000206E8
NV_THERM_GPC_TSENSE_RAW_CODE = 0x000206EC

# Fixed read-only context.  These registers are deliberately not exposed as
# a generic MMIO primitive.  Several are aliases, configuration, or raw ADC
# views of the same thermal complexes rather than independent sensors.
NV_THERM_SENSOR_0 = 0x00020008
NV_THERM_SENSOR_1 = 0x0002000C
NV_THERM_SENSOR_2 = 0x00020010
NV_THERM_SENSOR_3 = 0x00020014
NV_THERM_SENSOR_4 = 0x0002001C
NV_THERM_SENSOR_6 = 0x000200F0
NV_THERM_SENSOR_7 = 0x000200F4
NV_THERM_I2CS_SENSOR_00 = 0x00020400
NV_THERM_I2CS_SENSOR_11 = 0x00020444
NV_THERM_SENSOR_DEDICATED_OVERT_MAX = 0x00020448
NV_THERM_I2CS_SENSOR_13 = 0x0002044C
NV_THERM_I2CS_SENSOR_14 = 0x00020450
NV_THERM_SENSOR_8 = 0x000204A0
NV_THERM_SENSOR_9 = 0x000204A4
NV_THERM_STATISTICS_TEMP_AVG = 0x00020510
NV_THERM_STATISTICS_CTRL = 0x00020514
NV_PGC6_SCI_GPU_SMBUS_CMD00 = 0x00118B80
NV_PGC6_SCI_FS_OVERT_TS_SM = 0x00118B90
NV_PGC6_SCI_FS_OVERT_TS_ADJ = 0x00118B94
NV_PGC6_SCI_FS_OVERT_THRESH = 0x00118B98
NV_PGC6_SCI_FS_OVERT_FUSECTRL = 0x00118B9C
NV_PGC6_SCI_FS_OVERT_FUSE = 0x00118BA0
NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_NOM = 0x00118BA4
NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_FINAL = 0x00118BA8
NV_PGC6_SCI_FS_OVERT_TS_ADC = 0x00118BAC
NV_PGC6_SCI_FS_OVERT_TEMPERATURE = 0x00118BB0

NV_THERM_GPC_TSENSE_HS_OFFSET_INDEX = 0x000206CC
NV_THERM_GPC_TSENSE_OVERRIDE_INDEX = 0x000206D0
NV_THERM_GPC_TSENSE_OVERRIDE = 0x000206D8
NV_THERM_GPC_TSENSE_GLOBAL_OVERRIDE = 0x000206DC
NV_THERM_GPC_TSENSE_HS_OFFSET = 0x000206E4

PCHIPLET_TSENSE_BASE = 0x0010C000
PCHIPLET_TSENSE_STRIDE = 0x100
PCHIPLET_TSENSE_RAW = 0x084
PCHIPLET_TSENSE_ADJUST = 0x088
PCHIPLET_TSENSE_RAW_OVERRIDE = 0x0F0

NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT = 0x00020840
NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR_SNAPSHOT = 0x00020844
NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR_SNAPSHOT = 0x00020848
NV_THERM_TEMP_SENSOR_GPU_MAX_SNAPSHOT = 0x0002084C
NV_THERM_TEMP_SENSOR_GPU_AVG_SNAPSHOT = 0x00020850
NV_THERM_TEMP_SENSOR_GPU_OFFSET_MAX_SNAPSHOT = 0x00020854
NV_THERM_TEMP_SENSOR_GPU_OFFSET_AVG_SNAPSHOT = 0x00020858
NV_THERM_TSENSE_GLOBAL_SNAPSHOT = 0x0002085C

PCHIPLET_TS_AUTO_BASE = 0x0010C0F4
PCHIPLET_TS_AUTO_STRIDE = 0x100
PCHIPLET_TS_AUTO_POLLING = 1 << 15
TSENSE_SELECTOR_ALLOWED_MASK = (0xF << 16) | 0xF | (1 << 30)

TEMPERATURE_WRITE_ALLOWLIST = frozenset((
    NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT,
    NV_THERM_TSENSE_GLOBAL_SNAPSHOT,
))

FBPA_UC_BASE = 0x00900000
FBPA_UC_STRIDE = 0x4000
FBPA_UC_COUNT = 24
FBPA_UC_CFG1 = 0x0204
FBPA_UC_TEST_MACRO = 0x3C9C
FBPA_UC_INSTR = 0x3CB4
FBPA_UC_MODE = 0x3CB8
FBPA_UC_WDR = 0x3CBC
FBPA_UC_SHWIR = 0x3CC0
FBPA_UC_SHWDR = 0x3CC4
FBPA_UC_STATUS = 0x3CC8
FBPA_NUM_ACTIVE = 0x009A0164
FBPA_TRAINING = 0x009A0974
NV_FUSE_OPT_TS_DIS_MASK = 0x00821004
NV_FUSE_STATUS_OPT_GPC = 0x00820C1C
NV_FUSE_STATUS_OPT_FBIO = 0x00820C14

STACKS = (
    ("A", (0, 2)), ("B", (1, 4)), ("C", (3, 5)),
    ("D", (7, 9)), ("E", (6, 11)), ("F", (8, 10)),
)
PUBLIC_HBMMAP_LABELS_BY_FIRMWARE_ORDINAL = ("A", "E", "B", "C", "F", "D")
I1500_BRIDGE_STATE_NAMES = {
    0x00: "idle", 0x01: "write-instr", 0x02: "wir-wait",
    0x03: "wir-update", 0x04: "write-shift", 0x05: "write-update",
    0x06: "write-done", 0x07: "write-wait-idle",
    0x08: "write-wait-req", 0x09: "read-capture",
    0x0A: "read-capture-wait", 0x0B: "read-shift",
    0x0C: "read-done", 0x0D: "read-wait-idle",
    0x0E: "read-wait-req", 0x0F: "wait-idle", 0x10: "branch",
}

AGGREGATES = (
    ("tsense", NV_THERM_TEMP_SENSOR_TSENSE, "three-state", "provider"),
    ("offset_tsense", NV_THERM_TEMP_SENSOR_OFFSET_TSENSE,
     "three-state", "derived-offset"),
    ("constant", NV_THERM_TEMP_SENSOR_CONSTANT,
     "three-state", "software-constant-not-physical"),
    ("logical_max", NV_THERM_TEMP_SENSOR_MAX,
     "three-state", "derived-maximum"),
    ("gpu_max", NV_THERM_TEMP_SENSOR_GPU_MAX, "valid-bit", "derived-maximum"),
    ("gpu_average", NV_THERM_TEMP_SENSOR_GPU_AVG,
     "valid-bit", "derived-average"),
    ("gpu_offset_max", NV_THERM_TEMP_SENSOR_GPU_OFFSET_MAX,
     "valid-bit", "derived-offset-maximum"),
    ("gpu_offset_average", NV_THERM_TEMP_SENSOR_GPU_OFFSET_AVG,
     "valid-bit", "derived-offset-average"),
)

SNAPSHOT_AGGREGATES = (
    ("gpu_max", NV_THERM_TEMP_SENSOR_GPU_MAX_SNAPSHOT),
    ("gpu_average", NV_THERM_TEMP_SENSOR_GPU_AVG_SNAPSHOT),
    ("gpu_offset_max", NV_THERM_TEMP_SENSOR_GPU_OFFSET_MAX_SNAPSHOT),
    ("gpu_offset_average", NV_THERM_TEMP_SENSOR_GPU_OFFSET_AVG_SNAPSHOT),
)

FIXED_CONTEXT_REGISTERS = (
    ("nv_therm_sensor_0", NV_THERM_SENSOR_0, "logical-hardware-endpoint"),
    ("nv_therm_sensor_1", NV_THERM_SENSOR_1, "configuration"),
    ("nv_therm_sensor_2", NV_THERM_SENSOR_2, "configuration"),
    ("nv_therm_sensor_3", NV_THERM_SENSOR_3, "configuration"),
    ("nv_therm_sensor_4", NV_THERM_SENSOR_4, "configuration"),
    ("nv_therm_sensor_6", NV_THERM_SENSOR_6, "configuration"),
    ("nv_therm_sensor_7", NV_THERM_SENSOR_7, "mux-view"),
    ("nv_therm_i2cs_sensor_00", NV_THERM_I2CS_SENSOR_00,
     "alias"),
    ("nv_therm_i2cs_sensor_11", NV_THERM_I2CS_SENSOR_11,
     "alias"),
    ("nv_therm_sensor_dedicated_overt_max",
     NV_THERM_SENSOR_DEDICATED_OVERT_MAX, "aggregate"),
    ("nv_therm_i2cs_sensor_13", NV_THERM_I2CS_SENSOR_13,
     "alias"),
    ("nv_therm_i2cs_sensor_14", NV_THERM_I2CS_SENSOR_14, "alias"),
    ("nv_therm_sensor_8", NV_THERM_SENSOR_8,
     "configuration"),
    ("nv_therm_sensor_9", NV_THERM_SENSOR_9, "configuration"),
    ("nv_therm_statistics_temp_avg", NV_THERM_STATISTICS_TEMP_AVG,
     "derived-statistic"),
    ("nv_therm_statistics_ctrl", NV_THERM_STATISTICS_CTRL,
     "configuration"),
    ("nv_pgc6_sci_gpu_smbus_cmd00",
     NV_PGC6_SCI_GPU_SMBUS_CMD00, "alias"),
    ("nv_therm_gpc_tsense_hs_offset_index",
     NV_THERM_GPC_TSENSE_HS_OFFSET_INDEX,
     "configuration"),
    ("nv_therm_gpc_tsense_override_index",
     NV_THERM_GPC_TSENSE_OVERRIDE_INDEX,
     "configuration"),
    ("nv_therm_gpc_tsense_override", NV_THERM_GPC_TSENSE_OVERRIDE,
     "configuration"),
    ("nv_therm_gpc_tsense_global_override",
     NV_THERM_GPC_TSENSE_GLOBAL_OVERRIDE, "configuration"),
    ("nv_therm_gpc_tsense_hs_offset", NV_THERM_GPC_TSENSE_HS_OFFSET,
     "configuration"),
)

GPU_SCI_CONTEXT_REGISTERS = (
    ("nv_pgc6_sci_fs_overt_ts_sm", NV_PGC6_SCI_FS_OVERT_TS_SM,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_ts_adj", NV_PGC6_SCI_FS_OVERT_TS_ADJ,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_thresh", NV_PGC6_SCI_FS_OVERT_THRESH,
     "threshold"),
    ("nv_pgc6_sci_fs_overt_fusectrl", NV_PGC6_SCI_FS_OVERT_FUSECTRL,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_fuse", NV_PGC6_SCI_FS_OVERT_FUSE,
     "configuration"),
    ("nv_pgc6_sci_fs_overt_temp_coeff_nom",
     NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_NOM, "configuration"),
    ("nv_pgc6_sci_fs_overt_temp_coeff_final",
     NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_FINAL, "configuration"),
    ("nv_pgc6_sci_fs_overt_ts_adc", NV_PGC6_SCI_FS_OVERT_TS_ADC,
     "mux-view"),
    ("nv_pgc6_sci_fs_overt_temperature",
     NV_PGC6_SCI_FS_OVERT_TEMPERATURE, "logical-hardware-endpoint"),
)


class ProbeError(RuntimeError):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def source_component_records(backend, driver_queries_enabled,
                             loaded_identities=None):
    """Hash every Python component that can affect this capture."""
    directory = Path(__file__).resolve().parent
    loaded = {
        "temperature_probe": True,
        "xray_therm": backend == "xray-therm",
        "nvml_temperature": bool(driver_queries_enabled),
    }
    records = {}
    loaded_identities = loaded_identities or {}
    for name, filename in (
            ("temperature_probe", "temperature_probe.py"),
            ("xray_therm", "xray_therm.py"),
            ("nvml_temperature", "nvml_temperature.py")):
        path = directory / filename
        loaded_identity = loaded_identities.get(name)
        digest = None
        if isinstance(loaded_identity, dict):
            digest = loaded_identity.get("sha256")
        elif path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()
        records[name] = {
            "filename": filename,
            "path": str(path),
            "sha256": digest,
            "loaded": loaded[name],
            "identity_scope": (
                "compiled-byte-buffer" if isinstance(loaded_identity, dict)
                else "current-entry-script-file" if name == "temperature_probe"
                else "current-unloaded-file"
            ),
        }
    return records


def normalize_bdf(value):
    match = BDF_RE.fullmatch(str(value).strip())
    if match is None:
        raise ProbeError("invalid PCI BDF: %r" % value)
    domain = int(match.group("domain"), 16)
    if domain > 0xFFFF:
        raise ProbeError("PCI domain exceeds Linux 16-bit sysfs form: %r" % value)
    return "%04x:%02x:%02x.%x" % (
        domain, int(match.group("bus"), 16),
        int(match.group("device"), 16), int(match.group("function"), 16),
    )


def read_int(path, base=0):
    try:
        return int(Path(path).read_text(encoding="ascii").strip(), base)
    except (OSError, UnicodeError, ValueError):
        return None


def sign_extend_14(value):
    value &= 0x3FFF
    return value - 0x4000 if value & 0x2000 else value


def decode_sfp95(raw, state_kind="valid-bit"):
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    if badf or all_ones:
        return {
            "raw": "0x%08x" % raw,
            "state_encoding": state_kind,
            "state": "unavailable-badf" if badf else "unavailable-all-ones",
            "state_value": None,
            "source": None,
            "payload_temperature_c": None,
            "temperature_c": None,
            "measurement_authoritative": False,
            "live": False,
            "warning": None,
            "badf": badf,
            "all_ones": all_ones,
            "available": False,
        }
    if state_kind == "three-state":
        state_value = (raw >> 29) & 0x3
        state = {0: "invalid", 1: "valid", 2: "shadow"}.get(
            state_value, "reserved"
        )
        # GA100 RM rejects INVALID, but accepts SHADOW with a warning and
        # returns its SFP9.5 payload.  Preserve that payload without promoting
        # it to authoritative/live telemetry.
        payload_present = state_value in (1, 2)
        authoritative = state_value == 1
    else:
        state_value = (raw >> 30) & 0x1
        state = "valid" if state_value else "invalid"
        payload_present = bool(state_value)
        authoritative = bool(state_value)
    payload_temperature = (
        sign_extend_14(raw >> 3) / 32.0 if payload_present else None
    )
    return {
        "raw": "0x%08x" % raw,
        "state_encoding": state_kind,
        "state": state,
        "state_value": state_value,
        "source": "software-override" if raw & (1 << 31) else "hardware-logic",
        "payload_temperature_c": payload_temperature,
        "temperature_c": payload_temperature if authoritative else None,
        "measurement_authoritative": authoritative,
        "live": authoritative,
        "warning": (
            "shadow-state-payload-preserved-but-not-authoritative"
            if state_kind == "three-state" and state_value == 2 else None
        ),
        "badf": False,
        "all_ones": False,
        "available": True,
    }


def skipped_sfp95_read(state_kind="valid-bit"):
    return {
        "raw": None, "state_encoding": state_kind,
        "state": "not-read-owner-race-policy", "state_value": None,
        "source": None, "payload_temperature_c": None,
        "temperature_c": None, "measurement_authoritative": False,
        "live": False, "warning": None, "badf": False,
        "all_ones": False, "available": False, "read_skipped": True,
        "skip_reason": "selector-dependent-read-unconditionally-omitted-owner-race",
    }


def unbound_sfp95_read(raw, state_kind="valid-bit"):
    return {
        "raw": None, "observed_raw": "0x%08x" % raw,
        "state_encoding": state_kind, "state": "unbound-invalid-selector",
        "state_value": None, "source": None,
        "payload_temperature_c": None, "temperature_c": None,
        "measurement_authoritative": False, "live": False,
        "warning": None,
        "badf": False, "all_ones": False, "available": False,
        "read_skipped": False, "unbound": True,
        "unavailable_reason": "selector-not-semantically-valid",
    }


def selector_semantics(raw):
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    aperture_available = not (badf or all_ones)
    gpc = (raw >> 16) & 0xF
    bjt = raw & 0xF
    in_range = (gpc <= 7 and bjt <= 2) if aperture_available else None
    reserved_zero = (
        not bool(raw & (~TSENSE_SELECTOR_ALLOWED_MASK & 0xFFFFFFFF))
        if aperture_available else None
    )
    semantically_valid = bool(
        aperture_available and in_range and reserved_zero
    )
    return {
        "aperture_available": aperture_available,
        "selector_available": aperture_available,
        "selector_badf": badf,
        "selector_all_ones": all_ones,
        "selector_fields_in_range": in_range,
        "selector_reserved_bits_zero": reserved_zero,
        "selector_semantically_valid": semantically_valid,
        "gpc": gpc if semantically_valid else None,
        "bjt": bjt if semantically_valid else None,
    }


def liveness_report(tsense, num_active_fbps):
    tsense_badf = (tsense & 0xFFFF0000) == 0xBADF0000
    tsense_all_ones = tsense == 0xFFFFFFFF
    tsense_state = (tsense >> 29) & 0x3
    tsense_affirmative = (
        not tsense_badf and not tsense_all_ones and tsense_state in (1, 2)
    )
    active_badf = (num_active_fbps & 0xFFFF0000) == 0xBADF0000
    active_all_ones = num_active_fbps == 0xFFFFFFFF
    active_count = num_active_fbps & 0x1F
    reserved_zero = (num_active_fbps & ~0x1F) == 0
    active_affirmative = (
        not active_badf and not active_all_ones and reserved_zero and
        1 <= active_count <= 12
    )
    return {
        "tsense_raw": "0x%08x" % tsense,
        "tsense_state": tsense_state,
        "tsense_state_name": {
            0: "invalid", 1: "valid", 2: "shadow", 3: "reserved"
        }[tsense_state],
        "tsense_affirmative": tsense_affirmative,
        "num_active_fbps_raw": "0x%08x" % num_active_fbps,
        "num_active_fbps_count": active_count,
        "num_active_fbps_reserved_zero": reserved_zero,
        "num_active_fbps_in_range": 1 <= active_count <= 12,
        "num_active_fbps_affirmative": active_affirmative,
        "two_signal_corroborated": tsense_affirmative and active_affirmative,
        "policy": (
            "TSENSE state must be VALID/SHADOW and NUM_ACTIVE_FBPS must have "
            "reserved bits zero with count 1..12"
        ),
    }


def decode_hbm(raw, context_confirmed=False, aperture_responded=True,
               enabled=True, data_observed=True):
    usable = bool(aperture_responded and enabled)
    if raw is None or not data_observed:
        return {
            "raw": None, "valid": False, "latch_valid": False,
            "temperature_context_confirmed": bool(context_confirmed),
            "aperture_responded": bool(aperture_responded),
            "enabled": enabled, "fbpa_live": usable,
            "temperature_c": None,
            "invalid_bit": None, "badf": False, "all_ones": False,
            "data_observed": False,
            "availability": "not-read-stateful-read-to-advance-port",
        }
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    latch_valid = (usable and not badf and not all_ones and
                   not bool(raw & (1 << 31)))
    valid = latch_valid and bool(context_confirmed)
    return {
        "raw": "0x%08x" % raw,
        "valid": valid,
        "latch_valid": latch_valid,
        "temperature_context_confirmed": bool(context_confirmed),
        "aperture_responded": bool(aperture_responded),
        "enabled": enabled,
        "fbpa_live": usable,
        "temperature_c": ((raw >> 24) & 0x7F) if valid else None,
        "invalid_bit": bool(raw & (1 << 31)),
        "badf": badf,
        "all_ones": all_ones,
        "data_observed": True,
        "availability": (
            "unavailable-badf" if badf else
            "unavailable-all-ones" if all_ones else
            "observed"
        ),
    }


def decode_gpu_sci_mini_tsense(raw):
    """Decode SCI MINI_TSENSE without inventing a validity/source bit."""
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    payload = (raw >> 8) & 0x1FF
    signed = payload - 0x200 if payload & 0x100 else payload
    return {
        "name": "nv_pgc6_sci_fs_overt_temperature",
        "offset": "0x%08x" % NV_PGC6_SCI_FS_OVERT_TEMPERATURE,
        "raw": "0x%08x" % raw,
        "encoding": "signed-9-bit-in-bits-16-8-integer-celsius",
        "payload_temperature_c": None if badf or all_ones else signed,
        "temperature_c": None,
        "measurement_authoritative": False,
        "badf": badf,
        "all_ones": all_ones,
        "architected_valid_bit": None,
        "architected_state_bits": None,
        "architected_source_bit": None,
        "availability": (
            "unavailable-badf" if badf else
            "unavailable-all-ones" if all_ones else
            "payload-observed-no-architected-validity-bit"
        ),
        "logical_endpoint_family": "gpu-sci-mini-tsense",
        "relation_to_gpc_bjt": "separate-register-family",
        "physical_diode_independence": "unproven",
        "interpretation": (
            "separate GPU_SCI MINI_TSENSE logical endpoint/register family; "
            "physical-diode independence from the GPC/BJT matrix is unproven, "
            "the signed payload is preserved separately but is not promoted "
            "to an authoritative temperature because this word has no validity "
            "or freshness bit"
        ),
    }


def decode_gpu_sci_ts_adc(raw):
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    return {
        "name": "nv_pgc6_sci_fs_overt_ts_adc",
        "offset": "0x%08x" % NV_PGC6_SCI_FS_OVERT_TS_ADC,
        "raw": "0x%08x" % raw,
        "badf": badf,
        "all_ones": all_ones,
        "raw_adc_code": None if badf or all_ones else raw & 0x3FFF,
        "override_value": None if badf or all_ones else (raw >> 16) & 0x3FFF,
        "override_enabled": None if badf or all_ones else bool(raw & (1 << 31)),
        "temperature_scale": None,
        "readout_kind": "raw-adc-and-override-context-not-temperature",
        "interpretation": (
            "context for GPU_SCI MINI_TSENSE; no Celsius conversion is "
            "asserted for the 14-bit ADC code"
        ),
    }


def _signed_bits(value, width):
    sign = 1 << (width - 1)
    mask = (1 << width) - 1
    value &= mask
    return value - (1 << width) if value & sign else value


def decode_confirmed_context_fields(name, raw):
    """Return only header-confirmed mirrors; never infer undocumented units."""
    if (raw & 0xFFFF0000) == 0xBADF0000 or raw == 0xFFFFFFFF:
        return None
    if name == "nv_therm_sensor_0":
        select = (raw >> 15) & 0x3
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
        scale = (raw >> 24) & 0x3
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
                    "tsense": bool(raw & (1 << 0)),
                    "offset_tsense": bool(raw & (1 << 1)),
                    "gpu_max": bool(raw & (1 << 4)),
                    "gpu_average": bool(raw & (1 << 5)),
                    "gpu_offset_max": bool(raw & (1 << 6)),
                    "gpu_offset_average": bool(raw & (1 << 7)),
                },
                "temperature_c": _signed_bits((raw >> 17) & 0x3FFF, 14) / 32.0}
    i2cs_fields = {
        "nv_therm_i2cs_sensor_00": ("rlts", False),
        "nv_therm_i2cs_sensor_11": ("riet", False),
        "nv_therm_i2cs_sensor_13": ("unclamped_temperature", True),
        "nv_therm_i2cs_sensor_14": ("negative_temperature_offset", False),
    }
    if name in i2cs_fields:
        symbolic_field, signed = i2cs_fields[name]
        value = (_signed_bits(raw & 0xFF, 8) if signed else raw & 0xFF)
        return {"byte_value": value, "signed": signed,
                "symbolic_field": symbolic_field, "symbolic_value": value,
                "runtime_relevance": "uncertain-legacy-alias"}
    if name == "nv_therm_sensor_8":
        return {"offset_c": _signed_bits((raw >> 3) & 0x3FFF, 14) / 32.0}
    if name == "nv_therm_sensor_9":
        selector = (raw >> 28) & 0xF
        selector_names = ("tsense", "offset_tsense", "constant", "max",
                          "gpu_max", "gpu_average", "gpu_offset_max",
                          "gpu_offset_average")
        return {"source_flags": {
                    "tsense": bool(raw & (1 << 0)),
                    "offset_tsense": bool(raw & (1 << 1)),
                    "constant": bool(raw & (1 << 2)),
                    "gpu_max": bool(raw & (1 << 4)),
                    "gpu_average": bool(raw & (1 << 5)),
                    "gpu_offset_max": bool(raw & (1 << 6)),
                    "gpu_offset_average": bool(raw & (1 << 7)),
                }, "i2cs_selector": selector,
                "i2cs_selector_name": (selector_names[selector]
                                        if selector < 8 else "reserved"),
                "source_mask_raw": "0x%03x" % (raw & 0x3FF),
                "source_bits": [bool(raw & (1 << bit)) for bit in range(10)],
                "unnamed_source_bit_indices": [3, 8, 9]}
    if name == "nv_therm_statistics_temp_avg":
        return {"data": raw & 0xFF, "reset": bool(raw & (1 << 8))}
    if name == "nv_therm_statistics_ctrl":
        return {"ba_enabled": bool(raw & 1),
                "temperature_enabled": bool(raw & 2)}
    if name == "nv_pgc6_sci_gpu_smbus_cmd00":
        return {"current_byte": raw & 0xFF,
                "signed_offset": _signed_bits((raw >> 8) & 0xFF, 8),
                "read_data_selection": "offset-temperature" if raw & (1 << 28)
                else "offset", "units": None}
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
        return {"value": _signed_bits(raw & 0x3FFF, 14),
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
        return {"temperature_threshold_signed": _signed_bits(
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
                "adjustment_a_signed": _signed_bits((raw >> 11) & 0x3FF, 10),
                "adjustment_b_signed": _signed_bits((raw >> 21) & 0x3FF, 10)}
    if name == "nv_pgc6_sci_fs_overt_temp_coeff_nom":
        return {"coefficient_a": raw & 0x3FF,
                "coefficient_b": (raw >> 16) & 0x7FF}
    if name == "nv_pgc6_sci_fs_overt_temp_coeff_final":
        return {"coefficient_a_signed": _signed_bits(raw & 0xFFF, 12),
                "coefficient_b_signed": _signed_bits(
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


def decode_hbm_test_macro(raw):
    badf = (raw & 0xFFFF0000) == 0xBADF0000
    all_ones = raw == 0xFFFFFFFF
    unavailable = badf or all_ones
    return {"badf": badf, "all_ones": all_ones,
            "dram_reset_disabled": None if unavailable else bool(raw & 1),
            "raw_temperature_sideband": None if unavailable else (raw >> 1) & 0x7,
            "temperature_c": None,
            "dram_wrst_reset_disabled": None if unavailable else bool(raw & (1 << 4)),
            "read_i1500_register": None if unavailable else bool(raw & (1 << 5)),
            "debug_bus_wso_select": None if unavailable else (raw >> 6) & 0x7,
            "debug_bus_group0_select": None if unavailable else (raw >> 9) & 0x1F,
            "debug_bus_group1_select": None if unavailable else (raw >> 14) & 0x1F,
            "debug_bus_group2_select": None if unavailable else (raw >> 19) & 0x1F,
            "debug_bus_reset_select": None if unavailable else bool(raw & (1 << 24)),
            "wso_mux_override": None if unavailable else bool(raw & (1 << 25)),
            "wso_mux_select": None if unavailable else (raw >> 26) & 0xF}


def raw_context_register(name, offset, kind, raw, unavailable_reason=None):
    if raw is None:
        return {
            "name": name, "offset": "0x%08x" % offset, "raw": None,
            "badf": False, "all_ones": False,
            "suspicious_unavailable_pattern": False,
            "available": False, "read_skipped": True,
            "skip_reason": unavailable_reason or "not-read",
            "readout_kind": kind,
            "independent_physical_sensor": False,
            "decoded_temperature_c": None, "decoded": None,
            "interpretation": (
                "fixed selector-dependent view was unconditionally omitted "
                "because firmware can race its selector; no zero value is fabricated"
            ),
        }
    all_ones = raw == 0xFFFFFFFF
    return {
        "name": name,
        "offset": "0x%08x" % offset,
        "raw": "0x%08x" % raw,
        "badf": (raw & 0xFFFF0000) == 0xBADF0000,
        "all_ones": all_ones,
        "suspicious_unavailable_pattern": (
            all_ones or (raw & 0xFFFF0000) == 0xBADF0000
        ),
        "available": not (
            all_ones or (raw & 0xFFFF0000) == 0xBADF0000
        ),
        "read_skipped": False,
        "skip_reason": None,
        "readout_kind": kind,
        "independent_physical_sensor": False,
        "decoded_temperature_c": None,
        "decoded": decode_confirmed_context_fields(name, raw),
        "interpretation": (
            "register-backed logical endpoint counted once in the logical "
            "census; independence as a physical diode is unproven and no "
            "undocumented temperature scale is inferred"
            if kind == "logical-hardware-endpoint" else
            "fixed read-only raw/control/alias context; not counted as an "
            "independent sensor and no undocumented temperature scale inferred"
        ),
    }


class GpcProbe(object):
    def __init__(self, path):
        self.path = str(Path(path))
        self.fd = os.open(
            self.path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        )
        self.reads_issued = 0
        self.cards = self._info()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _info(self):
        payload = bytearray(GP_INFO_SIZE)
        fcntl.ioctl(self.fd, GP_IOC_INFO, payload, True)
        words = struct.unpack("=17I", payload)
        count = words[0]
        if not 1 <= count <= 16:
            raise ProbeError("gpcprobe returned an invalid card count")
        cards = []
        active_words = words[1:1 + count]
        if (any(encoded & 0xFFFF0000 for encoded in active_words) or
                len(set(active_words)) != len(active_words) or
                any(words[1 + count:])):
            raise ProbeError("gpcprobe returned a malformed INFO frame")
        for index, encoded in enumerate(active_words):
            bus = (encoded >> 8) & 0xFF
            devfn = encoded & 0xFF
            cards.append({
                "probe_index": index,
                "bus": bus,
                "device": devfn >> 3,
                "function": devfn & 0x7,
            })
        return cards

    def read32(self, index, offset):
        payload = bytearray(struct.pack("=III", index, offset, 0))
        fcntl.ioctl(self.fd, GP_IOC_READ32, payload, True)
        out_index, out_offset, value = struct.unpack("=III", payload)
        if out_index != index or out_offset != offset:
            raise ProbeError("gpcprobe READ32 response identity mismatch")
        self.reads_issued += 1
        return value

def sysfs_cards(sysfs_root="/sys"):
    cards = []
    root = Path(sysfs_root) / "bus" / "pci" / "devices"
    for path in sorted(root.glob("????:??:??.?")):
        try:
            bdf = normalize_bdf(path.name)
        except ProbeError:
            continue
        vendor = read_int(path / "vendor", 0)
        device = read_int(path / "device", 0)
        class_code = read_int(path / "class", 0)
        subvendor = read_int(path / "subsystem_vendor", 0)
        subdevice = read_int(path / "subsystem_device", 0)
        revision = read_int(path / "revision", 0)
        if (vendor != 0x10DE or device not in SUPPORTED_IDS or
                class_code is None or (class_code >> 16) != 0x03):
            continue
        match = BDF_RE.fullmatch(bdf)
        cards.append({
            "bdf": bdf,
            "domain": int(match.group("domain"), 16),
            "bus": int(match.group("bus"), 16),
            "device": int(match.group("device"), 16),
            "function": int(match.group("function"), 16),
            "vendor_id": "0x%04x" % vendor,
            "device_id": "0x%04x" % device,
            "subsystem_vendor_id": (
                "0x%04x" % subvendor if subvendor is not None else None
            ),
            "subsystem_device_id": (
                "0x%04x" % subdevice if subdevice is not None else None
            ),
            "revision": (
                "0x%02x" % revision if revision is not None else None
            ),
            "driver": (path / "driver").resolve().name
            if (path / "driver").exists() else None,
        })
    return cards


def bind_probe_indices(probe_cards, pci_cards, require_complete=False):
    result = []
    for probe in probe_cards:
        matches = [
            card for card in pci_cards
            if (card["bus"], card["device"], card["function"]) ==
               (probe["bus"], probe["device"], probe["function"])
        ]
        if len(matches) > 1:
            raise ProbeError(
                "gpcprobe ABI omits PCI domain; bus/devfn is ambiguous for %02x:%02x.%x"
                % (probe["bus"], probe["device"], probe["function"])
            )
        if not matches:
            raise ProbeError(
                "gpcprobe INFO entry has no matching supported sysfs device: "
                "%02x:%02x.%x" %
                (probe["bus"], probe["device"], probe["function"])
            )
        record = dict(matches[0])
        record["probe_index"] = probe["probe_index"]
        result.append(record)
    if require_complete:
        matched = {record["bdf"] for record in result}
        expected = {card["bdf"] for card in pci_cards}
        if matched != expected:
            raise ProbeError(
                "gpcprobe --all-targets coverage mismatch: missing=%s extra=%s" %
                (sorted(expected - matched), sorted(matched - expected))
            )
    return result


def _load_sibling_module(filename, module_name):
    path = Path(__file__).resolve().with_name(filename)
    if not path.is_file():
        raise ProbeError("required sibling module is absent: %s" % path)
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ProbeError("cannot load sibling module: %s" % path)
    module = importlib.util.module_from_spec(spec)
    try:
        source = path.read_bytes()
        code = compile(source, str(path), "exec")
    except (OSError, SyntaxError, ValueError) as error:
        raise ProbeError("cannot compile sibling module %s: %s" % (path, error))
    exec(code, module.__dict__)
    module.__xray_source_identity__ = {
        "path": str(path),
        "sha256": hashlib.sha256(source).hexdigest(),
        "identity_scope": "compiled-byte-buffer",
    }
    return module


def resolve_probe_backend(requested_backend, requested_device):
    backend = requested_backend
    device = requested_device
    if backend == "auto":
        if device != "auto":
            backend = (
                "gpcprobe" if Path(device).name == "gpcprobe"
                else "xray-therm"
            )
        elif Path("/dev/170hx_xray_therm").exists():
            backend, device = "xray-therm", "/dev/170hx_xray_therm"
        elif Path("/dev/gpcprobe").exists():
            backend, device = "gpcprobe", "/dev/gpcprobe"
        else:
            raise ProbeError(
                "auto backend found neither /dev/170hx_xray_therm nor /dev/gpcprobe"
            )
    elif device == "auto":
        device = (
            "/dev/170hx_xray_therm" if backend == "xray-therm"
            else "/dev/gpcprobe"
        )
    return backend, device


def bind_therm_cards(helper_cards, pci_cards, require_complete=False):
    """Bind the dedicated helper's full-domain identity to sysfs identity."""
    by_bdf = {card["bdf"]: card for card in pci_cards}
    result = []
    for helper_card in helper_cards:
        helper_identity = helper_card.get("identity", {})
        bdf = helper_identity.get("bdf")
        sysfs = by_bdf.get(bdf)
        if sysfs is None:
            raise ProbeError(
                "dedicated helper INFO entry has no matching supported sysfs "
                "device: %s" % bdf
            )
        comparisons = (
            ("vendor_id", "vendor_id"),
            ("device_id", "device_id"),
            ("subsystem_vendor_id", "subsystem_vendor_id"),
            ("subsystem_device_id", "subsystem_device_id"),
            ("revision", "revision"),
        )
        for helper_key, sysfs_key in comparisons:
            if helper_identity.get(helper_key) != sysfs.get(sysfs_key):
                raise ProbeError(
                    "%s helper/sysfs identity mismatch for %s" % (bdf, helper_key)
                )
        record = dict(sysfs)
        record["probe_index"] = helper_card["helper_index"]
        record["helper_identity"] = dict(helper_identity)
        result.append((record, helper_card))
    if require_complete:
        matched = {record["bdf"] for record, _helper in result}
        expected = set(by_bdf)
        if matched != expected:
            raise ProbeError(
                "dedicated helper --all-targets coverage mismatch: "
                "missing=%s extra=%s" %
                (sorted(expected - matched), sorted(matched - expected))
            )
    return result


class CapturedRegisterProbe(object):
    """Read-only register image built from one atomic helper CAPTURE ioctl."""
    def __init__(self, capture):
        self.regs = {}
        for (_name, offset, _state_kind, _kind), value in zip(
                AGGREGATES, capture.aggregate):
            self.regs[offset] = int(value)
        for gpc, value in enumerate(capture.ts_auto_entry):
            self.regs[
                PCHIPLET_TS_AUTO_BASE + gpc * PCHIPLET_TS_AUTO_STRIDE
            ] = int(value)
        fixed = capture.fixed_context
        for field, offset in (
                ("sys_sensor0", NV_THERM_SENSOR_0),
                ("sys_sensor1", NV_THERM_SENSOR_1),
                ("sys_sensor2", NV_THERM_SENSOR_2),
                ("sys_sensor3", NV_THERM_SENSOR_3),
                ("sys_sensor4", NV_THERM_SENSOR_4),
                ("sys_sensor6", NV_THERM_SENSOR_6),
                ("sys_sensor7", NV_THERM_SENSOR_7),
                ("dedicated_overt_max", NV_THERM_SENSOR_DEDICATED_OVERT_MAX),
                ("i2cs_sensor00", NV_THERM_I2CS_SENSOR_00),
                ("i2cs_sensor11", NV_THERM_I2CS_SENSOR_11),
                ("i2cs_sensor13", NV_THERM_I2CS_SENSOR_13),
                ("i2cs_sensor14", NV_THERM_I2CS_SENSOR_14),
                ("sensor8", NV_THERM_SENSOR_8),
                ("sensor9", NV_THERM_SENSOR_9),
                ("statistics_temp_avg", NV_THERM_STATISTICS_TEMP_AVG),
                ("statistics_ctrl", NV_THERM_STATISTICS_CTRL),
                ("gpc_tsense_hs_offset_index", NV_THERM_GPC_TSENSE_HS_OFFSET_INDEX),
                ("gpc_tsense_override_index", NV_THERM_GPC_TSENSE_OVERRIDE_INDEX),
                ("gpc_tsense_override", NV_THERM_GPC_TSENSE_OVERRIDE),
                ("gpc_tsense_global_override", NV_THERM_GPC_TSENSE_GLOBAL_OVERRIDE),
                ("gpc_tsense_hs_offset", NV_THERM_GPC_TSENSE_HS_OFFSET),
                ("sci_smbus_cmd00", NV_PGC6_SCI_GPU_SMBUS_CMD00),
                ("sci_overt_ts_sm", NV_PGC6_SCI_FS_OVERT_TS_SM),
                ("sci_overt_ts_adj", NV_PGC6_SCI_FS_OVERT_TS_ADJ),
                ("sci_overt_thresh", NV_PGC6_SCI_FS_OVERT_THRESH),
                ("sci_overt_fusectrl", NV_PGC6_SCI_FS_OVERT_FUSECTRL),
                ("sci_overt_fuse", NV_PGC6_SCI_FS_OVERT_FUSE),
                ("sci_overt_temp_coeff_nom", NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_NOM),
                ("sci_overt_temp_coeff_final", NV_PGC6_SCI_FS_OVERT_TEMP_COEFF_FINAL),
                ("sci_overt_ts_adc", NV_PGC6_SCI_FS_OVERT_TS_ADC),
                ("sci_overt_temperature", NV_PGC6_SCI_FS_OVERT_TEMPERATURE)):
            self.regs[offset] = int(getattr(fixed, field))
        for gpc, sample in enumerate(capture.gpc_analog):
            base = PCHIPLET_TSENSE_BASE + gpc * PCHIPLET_TSENSE_STRIDE
            self.regs[base + PCHIPLET_TSENSE_RAW] = int(sample.tsense)
            self.regs[base + PCHIPLET_TSENSE_ADJUST] = int(sample.adjustment)
            self.regs[base + PCHIPLET_TSENSE_RAW_OVERRIDE] = int(sample.raw_override)
            self.regs[base + 0x0F4] = int(sample.ts_auto)
        self.regs[NV_THERM_GPC_TSENSE_INDEX] = int(capture.current_selector)
        self.regs[NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR] = int(
            capture.current_temperature
        )
        self.regs[NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR] = int(
            capture.current_offset_temperature
        )
        self.regs[NV_THERM_GPC_TSENSE_RAW_CODE] = int(capture.current_raw_code)
        self.regs[NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT] = int(
            capture.snapshot_as_found_selector_before
        )
        self.regs[NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR_SNAPSHOT] = int(
            capture.snapshot_as_found_temperature
        )
        self.regs[NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR_SNAPSHOT] = int(
            capture.snapshot_as_found_offset_temperature
        )
        for fbpa, sample in enumerate(capture.fbpa):
            base = FBPA_UC_BASE + fbpa * FBPA_UC_STRIDE
            for relative, value in (
                (FBPA_UC_CFG1, sample.cfg1),
                (FBPA_UC_TEST_MACRO, sample.test_macro),
                (FBPA_UC_INSTR, sample.instr),
                (FBPA_UC_MODE, sample.mode),
                (FBPA_UC_WDR, sample.data),
                (FBPA_UC_SHWIR, sample.shwir),
                (FBPA_UC_SHWDR, sample.shwdr),
                (FBPA_UC_STATUS, sample.status),
            ):
                self.regs[base + relative] = int(value)
        self.regs[FBPA_NUM_ACTIVE] = int(capture.sentinels_entry[0])
        self.regs[FBPA_TRAINING] = int(capture.sentinels_entry[1])
        self.regs[NV_FUSE_OPT_TS_DIS_MASK] = int(capture.opt_ts_dis_mask)
        self.regs[NV_FUSE_STATUS_OPT_GPC] = int(capture.status_opt_gpc)
        self.regs[NV_FUSE_STATUS_OPT_FBIO] = int(capture.status_opt_fbio)

    def read32(self, index, offset):
        del index
        if offset not in self.regs:
            raise ProbeError(
                "fixed helper capture lacks requested offset 0x%08x" % offset
            )
        return self.regs[offset]


def population_report(opt_ts_dis_mask, status_opt_gpc, endpoints=None):
    opt_ts_dis_reserved_zero = (opt_ts_dis_mask & ~0x00FFFFFF) == 0
    mask_reads_available = opt_ts_dis_reserved_zero and not any(
        (value & 0xFFFF0000) == 0xBADF0000 or value == 0xFFFFFFFF
        for value in (opt_ts_dis_mask, status_opt_gpc)
    )
    expected = [] if mask_reads_available else None
    if expected is not None:
        for gpc in range(8):
            if status_opt_gpc & (1 << gpc):
                continue
            for bjt in range(3):
                if not (opt_ts_dis_mask & (1 << (gpc * 3 + bjt))):
                    expected.append((gpc, bjt))
    observed = None
    matches = None
    if endpoints is not None:
        observed = sorted(
            (item["gpc"], item["bjt"]) for item in endpoints
            if item["temperature"]["state"] == "valid"
        )
        matches = observed == expected if expected is not None else None
    return {
        "opt_ts_dis_mask": "0x%08x" % opt_ts_dis_mask,
        "status_opt_gpc": "0x%08x" % status_opt_gpc,
        "opt_ts_dis_reserved_zero": opt_ts_dis_reserved_zero,
        "source_offsets": {
            "opt_ts_dis_mask": "0x%08x" % NV_FUSE_OPT_TS_DIS_MASK,
            "status_opt_gpc": "0x%08x" % NV_FUSE_STATUS_OPT_GPC,
        },
        "mask_reads_available": mask_reads_available,
        "expected_enabled_endpoints": (
            [{"gpc": gpc, "bjt": bjt} for gpc, bjt in expected]
            if expected is not None else None
        ),
        "expected_enabled_count": (
            len(expected) if expected is not None else None
        ),
        "observed_valid_endpoints": (
            [{"gpc": gpc, "bjt": bjt} for gpc, bjt in observed]
            if observed is not None else None
        ),
        "observed_valid_count": len(observed) if observed is not None else None,
        "observed_matches_population_masks": matches,
        "rule": (
            "endpoint expected only when STATUS_OPT_GPC[gpc]=0 and "
            "OPT_TS_DIS_MASK[3*gpc+bjt]=0"
        ),
    }


def fbio_population_report(status_opt_fbio):
    available = (
        status_opt_fbio != 0xFFFFFFFF and
        (status_opt_fbio & 0xFFFF0000) != 0xBADF0000
    )
    enabled = ([index for index in range(FBPA_UC_COUNT)
                if not status_opt_fbio & (1 << index)]
               if available else None)
    disabled = ([index for index in range(FBPA_UC_COUNT)
                 if status_opt_fbio & (1 << index)]
                if available else None)
    return {
        "status_opt_fbio": "0x%08x" % status_opt_fbio,
        "source_offset": "0x%08x" % NV_FUSE_STATUS_OPT_FBIO,
        "available": available,
        "enabled_fbpa_indices": enabled,
        "disabled_fbpa_indices": disabled,
        "fbpa_enabled": (
            [not bool(status_opt_fbio & (1 << fbpa))
             for fbpa in range(FBPA_UC_COUNT)] if available else None
        ),
        "rule": (
            "FBPA n is enabled only when STATUS_OPT_FBIO bit n is clear"
        ),
    }


def selector_dependent_skip_count(mask):
    mask = int(mask)
    return ((3 if mask & 1 else 0) + (1 if mask & 2 else 0) +
            (1 if mask & 4 else 0) + (2 if mask & 8 else 0))


def helper_read_contract(capture):
    writes = int(capture.writes_issued)
    reads = int(capture.reads_issued)
    skip = selector_dependent_skip_count(
        capture.selector_dependent_skipped_mask
    )
    policy_mask_ok = int(capture.selector_dependent_skipped_mask) == 0xF
    hbm_requested = bool(int(capture.flags) & XRAY_THERM_CAPTURE_F_HBM_DATA)
    if writes == 0:
        snapshot_requested = bool(int(capture.flags) & 1)
        hbm_extra = 24 if hbm_requested else 0
        completed_context = 255 + hbm_extra
        dead_selector = 246 + hbm_extra
        fresh_liveness_refusal = 248 + hbm_extra
        if not snapshot_requested:
            valid = reads == 255 and not hbm_requested and policy_mask_ok
            description = "255 fixed reads with seven selector-dependent reads omitted"
            trigger_polls = 0
            fixed_reads = 255
        else:
            valid = reads in (
                completed_context, dead_selector, fresh_liveness_refusal
            ) and policy_mask_ok
            description = (
                "semantic pre-write refusal: %d reads after complete context "
                "or cached-liveness refusal, %d after dead-selector refusal, "
                "or %d after fresh-liveness refusal" %
                (completed_context, dead_selector, fresh_liveness_refusal)
            )
            trigger_polls = None
            fixed_reads = None
        return {
            "valid": valid, "fixed_reads": fixed_reads,
            "skip_deduction": skip, "trigger_poll_reads": trigger_polls,
            "hbm_data_requested": hbm_requested,
            "description": description,
        }
    if (writes == 26 and int(capture.status) == 0 and
            bool(capture.snapshot_completed)):
        fixed = 409 if hbm_requested else 385
        polls = reads - fixed
        return {
            "valid": policy_mask_ok and 1 <= polls <= 1000,
            "fixed_reads": fixed,
            "skip_deduction": skip, "trigger_poll_reads": polls,
            "hbm_data_requested": hbm_requested,
            "description": (
                "%d selector-safe fixed reads plus 1..1000 "
                "trigger-poll reads" % fixed
            ),
        }
    if (writes == 2 and int(capture.status) == -110 and
            not bool(capture.trigger_self_cleared)):
        timeout_reads = 1285 if hbm_requested else 1261
        return {
            "valid": policy_mask_ok and reads == timeout_reads,
            "fixed_reads": timeout_reads - 1000,
            "skip_deduction": skip, "trigger_poll_reads": 1000,
            "hbm_data_requested": hbm_requested,
            "description": (
                "%d selector-safe fixed reads plus 1000 trigger-poll reads "
                "ending in timeout" % (timeout_reads - 1000)
            ),
        }
    return {
        "valid": None, "fixed_reads": None, "skip_deduction": skip,
        "trigger_poll_reads": None,
        "hbm_data_requested": hbm_requested,
        "description": (
            "failed semantic journal retained without a success read-count "
            "claim; interrupted/race/cleanup paths have stage-dependent counts"
        ),
    }


def decode_helper_snapshot(capture, bound_snapshot_acknowledged=False):
    writes_issued = int(capture.writes_issued)
    reads_issued = int(capture.reads_issued)
    read_contract = helper_read_contract(capture)
    trigger_poll_reads = read_contract["trigger_poll_reads"]
    trigger_count = 1 if writes_issued >= 1 else 0
    restore_write_count = 1 if writes_issued >= 2 else 0
    selector_program_count = max(
        0, writes_issued - trigger_count - restore_write_count
    )
    snapshot_completed = bool(capture.snapshot_completed)
    selector_entry_raw = int(capture.snapshot_selector_entry)
    selector_exit_raw = int(capture.snapshot_selector_exit)
    selector_entry_semantics = selector_semantics(selector_entry_raw)
    selector_exit_semantics = selector_semantics(selector_exit_raw)
    fresh_prewrite_liveness = liveness_report(
        int(capture.prewrite_tsense), int(capture.prewrite_active_fbps)
    )
    samples = []
    sequence_ok = snapshot_completed and selector_program_count == 24
    race = None
    for index, sample in enumerate(capture.gpc_bjt):
        gpc, bjt = divmod(index, 3)
        expected = (gpc << 16) | bjt
        before, after = int(sample.selector_before), int(sample.selector_after)
        reached = snapshot_completed or index < selector_program_count
        failed_here = (
            int(capture.failed_gpc) == gpc and
            int(capture.failed_bjt) == bjt
        )
        if reached and (int(sample.selector) != expected or before != expected or
                        after != expected):
            sequence_ok = False
            if race is None:
                race = {
                    "gpc": gpc, "bjt": bjt,
                    "expected": "0x%08x" % expected,
                    "recorded": "0x%08x" % int(sample.selector),
                    "before": "0x%08x" % before,
                    "after": "0x%08x" % after,
                }
        samples.append({
            "gpc": gpc,
            "bjt": bjt,
            "selector": "0x%08x" % expected,
            "selector_before": "0x%08x" % before,
            "selector_after": "0x%08x" % after,
            "temperature": decode_sfp95(int(sample.temperature)),
            "offset_temperature": decode_sfp95(
                int(sample.offset_temperature)
            ),
            "endpoint_kind": "architecturally-addressable-gpc-bjt",
            "physical_population": (
                "cross-checked-against-pre-snapshot-boot-static-population-masks"
            ),
            "journal_state": (
                "complete" if snapshot_completed else
                "partial-at-failure" if reached and failed_here else
                "reached-before-failure" if reached else
                "unreached-or-unproven"
            ),
        })
    aggregates = []
    for (name, offset), raw in zip(
            SNAPSHOT_AGGREGATES, capture.snapshot_aggregate):
        decoded = decode_sfp95(int(raw))
        decoded.update({
            "name": name, "offset": "0x%08x" % offset,
            "journal_valid": snapshot_completed,
        })
        aggregates.append(decoded)
    status_ok = (
        int(capture.status) == 0 and bool(capture.pure_completed) and
        snapshot_completed and bool(capture.selector_restored) and
        selector_entry_raw == selector_exit_raw and
        not bool(capture.selector_race) and bool(capture.autopoll_unchanged) and
        list(capture.ts_auto_entry) == list(capture.ts_auto_exit) and
        bool(capture.sentinels_unchanged) and
        list(capture.sentinels_entry) == list(capture.sentinels_exit) and
        bool(capture.trigger_self_cleared) and sequence_ok and
        writes_issued == 26 and read_contract["valid"]
        and selector_entry_semantics["selector_semantically_valid"]
        and selector_exit_semantics["selector_semantically_valid"]
        and fresh_prewrite_liveness["two_signal_corroborated"]
    )
    expected_hbm_mask = (
        0xFFFFFF if read_contract["hbm_data_requested"] else 0
    )
    if int(capture.hbm_data_read_mask) != expected_hbm_mask:
        status_ok = False
    if (bool(capture.driver_bound_at_entry) and
            not bound_snapshot_acknowledged):
        status_ok = False
    population = population_report(
        int(capture.opt_ts_dis_mask), int(capture.status_opt_gpc),
        samples if snapshot_completed else None,
    )
    if (snapshot_completed and
            population["observed_matches_population_masks"] is not True):
        status_ok = False
    population_proven = (
        snapshot_completed and
        population["observed_matches_population_masks"] is True
    )
    for sample in samples:
        sample["physical_population"] = (
            "cross-checked-against-pre-snapshot-boot-static-population-masks"
            if population_proven else
            "not-proven-population-mismatch-or-incomplete"
        )
    if bool(capture.selector_race) and race is None:
        race = {"helper_flag": True}
    return {
        "access": "semantic-read",
        "status": "pass" if status_ok else "failed",
        "error": None if status_ok else (
            "dedicated helper capture or population invariant failed"
        ),
        "backend": "xray-therm-fixed-capture",
        "helper_status": int(capture.status),
        "pure_completed": bool(capture.pure_completed),
        "snapshot_completed": snapshot_completed,
        "driver_bound_at_entry": bool(capture.driver_bound_at_entry),
        "bound_snapshot_acknowledged": bool(bound_snapshot_acknowledged),
        "reads_issued": reads_issued,
        "trigger_poll_reads": trigger_poll_reads,
        "read_count_contract": read_contract["description"],
        "selector_dependent_skipped_mask": "0x%08x" % int(
            capture.selector_dependent_skipped_mask
        ),
        "selector_dependent_omit_count": read_contract["skip_deduction"],
        "hbm_data_requested": read_contract["hbm_data_requested"],
        "hbm_data_read_mask": "0x%08x" % int(capture.hbm_data_read_mask),
        "fresh_prewrite_liveness": fresh_prewrite_liveness,
        "fresh_prewrite_liveness_scope": (
            "after-saved-selector-validation-immediately-before-first-write"
        ),
        "writes_issued": writes_issued,
        "trigger_count": trigger_count,
        "selector_program_count": selector_program_count,
        "restore_write_count": restore_write_count,
        "write_offsets_observed": (
            (["0x%08x" % NV_THERM_TSENSE_GLOBAL_SNAPSHOT]
             if trigger_count else []) +
            ["0x%08x" % NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT] *
            max(0, writes_issued - trigger_count)
        ),
        "write_offsets_directly_logged": False,
        "write_offsets_source": (
            "reconstructed-from-fixed-kernel-write-alphabet-and-exact-counter; "
            "not a per-write hardware log"
        ),
        "write_surface": {
            "snapshot_trigger": "0x%08x" % NV_THERM_TSENSE_GLOBAL_SNAPSHOT,
            "snapshot_selector": "0x%08x" % NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT,
            "gpc_autopoll": "never-written",
            "allowlist_only": True,
        },
        "trigger_readback": None,
        "trigger_self_cleared": bool(capture.trigger_self_cleared),
        "selector_entry": "0x%08x" % selector_entry_raw,
        "selector_exit": "0x%08x" % selector_exit_raw,
        "selector_entry_semantics": selector_entry_semantics,
        "selector_exit_semantics": selector_exit_semantics,
        "selector_restored": bool(capture.selector_restored),
        "selector_race": bool(capture.selector_race),
        "failed_gpc": (
            None if int(capture.failed_gpc) == 0xFFFFFFFF
            else int(capture.failed_gpc)
        ),
        "failed_bjt": (
            None if int(capture.failed_bjt) == 0xFFFFFFFF
            else int(capture.failed_bjt)
        ),
        "concurrent_selector_race": race,
        "autopoll_untouched": bool(capture.autopoll_unchanged),
        "autopoll_entry": ["0x%08x" % int(v) for v in capture.ts_auto_entry],
        "autopoll_exit": ["0x%08x" % int(v) for v in capture.ts_auto_exit],
        "sentinels_unchanged": bool(capture.sentinels_unchanged),
        "sentinels_entry": [
            "0x%08x" % int(v) for v in capture.sentinels_entry
        ],
        "sentinels_exit": [
            "0x%08x" % int(v) for v in capture.sentinels_exit
        ],
        "snapshot_aggregate_readouts": aggregates,
        "gpc_bjt_endpoints": samples,
        "valid_endpoint_count": sum(
            item["temperature"]["state"] == "valid" for item in samples
        ),
        "population": population,
        "procedure": (
            "one global trigger; enumerate 8x3 snapshot selector; mandatory "
            "kernel restoration; no GPC TS_AUTO writes"
        ),
    }


DRIVER_CENSUS_CONTRACT = {
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


def _fallback_query(raw, value_type, unit, timestamp, latency_us):
    token = str(raw).strip()
    try:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("non-finite")
        status = "success"
    except (TypeError, ValueError):
        value = None
        status = "not-available" if token.lower() in (
            "n/a", "na", "not supported", "[not supported]", ""
        ) else "parse-failed"
    return {
        "return_status": status,
        "raw": token,
        "value": value,
        "value_type": value_type,
        "unit": unit,
        "timestamp_utc": timestamp,
        "latency_us": latency_us,
        "latency_scope": "whole-nvidia-smi-command",
        "canonical_nvml_api": False,
    }


def nvidia_smi_snapshot():
    fields = (
        "index,pci.bus_id,name,temperature.gpu,temperature.memory,"
        "utilization.gpu,power.draw"
    )
    started_utc = utc_now()
    started = time.monotonic_ns()
    try:
        process = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + fields,
             "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "status": "unavailable", "error": str(error), "cards": {},
            "backend": "nvidia-smi-query-gpu", "canonical": False,
            "started_utc": started_utc,
            "latency_us": (time.monotonic_ns() - started) // 1000,
        }
    latency_us = (time.monotonic_ns() - started) // 1000
    if process.returncode != 0:
        return {
            "status": "failed", "returncode": process.returncode,
            "stderr": process.stderr.strip(), "cards": {},
            "backend": "nvidia-smi-query-gpu", "canonical": False,
            "started_utc": started_utc, "latency_us": latency_us,
        }
    cards = {}
    keys = ("index", "bdf", "name", "gpu_temperature_c",
            "memory_temperature_c", "utilization_percent", "power_w")
    for line in process.stdout.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(keys):
            continue
        row = dict(zip(keys, cells))
        try:
            bdf = normalize_bdf(row["bdf"])
        except ProbeError:
            continue
        query_specs = (
            ("gpu_temperature_c", "temperature", "celsius"),
            ("memory_temperature_c", "temperature", "celsius"),
            ("utilization_percent", "percentage", "percent"),
            ("power_w", "power", "watt"),
        )
        row["queries"] = {}
        for key, value_type, unit in query_specs:
            query = _fallback_query(
                row[key], value_type, unit, started_utc, latency_us
            )
            row["queries"][key] = query
            row[key] = query["value"]
        try:
            row["index"] = int(row["index"])
        except ValueError:
            row["index"] = None
        cards[bdf] = row
    return {
        "status": "ok", "cards": cards,
        "backend": "nvidia-smi-query-gpu", "canonical": False,
        "started_utc": started_utc, "latency_us": latency_us,
    }


def hwmon_snapshot(bdf, sysfs_root="/sys"):
    records = []
    for device in sorted((Path(sysfs_root) / "class" / "hwmon").glob("hwmon*")):
        try:
            resolved = str((device / "device").resolve(strict=True)).lower()
        except OSError:
            continue
        if bdf.lower() not in resolved:
            continue
        name = None
        try:
            name = (device / "name").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            pass
        channels = []
        for input_path in sorted(device.glob("temp*_input")):
            stem = input_path.name[:-6]
            value = read_int(input_path, 10)
            label = None
            try:
                label = (device / (stem + "label")).read_text(
                    encoding="utf-8"
                ).strip()
            except (OSError, UnicodeError):
                pass
            channels.append({
                "input": input_path.name, "label": label,
                "temperature_c": value / 1000.0 if value is not None else None,
                "raw_millicelsius": value,
            })
        records.append({"hwmon": device.name, "name": name, "channels": channels})
    return records


def capture_pure_reads(probe, card, hbm_data_read_mask=0,
                       selector_dependent_skipped_mask=None,
                       legacy_backend=False):
    index = card["probe_index"]
    register_cache = {}
    reads_before = getattr(probe, "reads_issued", None)

    def _read(offset):
        if offset not in register_cache:
            register_cache[offset] = probe.read32(index, offset)
        return register_cache[offset]

    aggregates = []
    for name, offset, state_kind, kind in AGGREGATES:
        decoded = decode_sfp95(_read(offset), state_kind)
        decoded.update({
            "name": name, "offset": "0x%08x" % offset,
            "readout_kind": kind,
        })
        aggregates.append(decoded)

    auto = []
    for gpc in range(8):
        offset = PCHIPLET_TS_AUTO_BASE + gpc * PCHIPLET_TS_AUTO_STRIDE
        raw = _read(offset)
        available = ((raw & 0xFFFF0000) != 0xBADF0000 and
                     raw != 0xFFFFFFFF)
        auto.append({
            "gpc": gpc, "offset": "0x%08x" % offset,
            "raw": "0x%08x" % raw,
            "all_ones": raw == 0xFFFFFFFF,
            "available": available,
            "auto_polling": (
                bool(raw & PCHIPLET_TS_AUTO_POLLING) if available else None
            ),
            "auto_polling_bit": 15,
            "sample_delay": raw & 0x3FFF if available else None,
            "polling_interval": (raw >> 16) & 0xFFFF if available else None,
        })

    context_registers = []
    reported_skip_mask = (
        0xF if selector_dependent_skipped_mask is None
        else int(selector_dependent_skipped_mask)
    )
    for name, offset, kind in FIXED_CONTEXT_REGISTERS:
        skip_bit = 0
        if name == "nv_therm_gpc_tsense_override":
            skip_bit = 1 << 1
        elif name == "nv_therm_gpc_tsense_hs_offset":
            skip_bit = 1 << 2
        if skip_bit:
            context_registers.append(raw_context_register(
                name, offset, kind, None,
                "selector-dependent-read-unconditionally-omitted-owner-race",
            ))
            continue
        raw = _read(offset)
        context_registers.append(raw_context_register(
            name, offset, kind, raw
        ))
    gpu_sci_registers = [
        raw_context_register(name, offset, kind, _read(offset))
        for name, offset, kind in GPU_SCI_CONTEXT_REGISTERS
    ]
    gpu_sci = {
        "mini_tsense": decode_gpu_sci_mini_tsense(
            _read(NV_PGC6_SCI_FS_OVERT_TEMPERATURE)
        ),
        "adc_context": decode_gpu_sci_ts_adc(
            _read(NV_PGC6_SCI_FS_OVERT_TS_ADC)
        ),
        "register_block": gpu_sci_registers,
        "register_block_range": "0x00118b90..0x00118bb0",
        "register_count": 9,
    }
    gpc_analog = []
    for gpc in range(8):
        base = PCHIPLET_TSENSE_BASE + gpc * PCHIPLET_TSENSE_STRIDE
        registers = []
        for name, relative, kind in (
                ("tsense", PCHIPLET_TSENSE_RAW, "mux-view"),
                ("adjustment", PCHIPLET_TSENSE_ADJUST, "configuration"),
                ("raw_override", PCHIPLET_TSENSE_RAW_OVERRIDE,
                 "configuration"),
                ("ts_auto", 0x0F4, "configuration")):
            registers.append(raw_context_register(
                name, base + relative, kind,
                _read(base + relative),
            ))
        gpc_analog.append({
            "gpc": gpc,
            "base": "0x%08x" % base,
            "registers": registers,
            "independent_sensor_count": 0,
            "interpretation": (
                "four views of one GPC thermal analogue complex; endpoints "
                "are counted by the 8x3 GPC/BJT matrix, not by these words"
            ),
        })

    status_opt_fbio = _read(NV_FUSE_STATUS_OPT_FBIO)
    fbio_population = fbio_population_report(status_opt_fbio)
    fbpas = []
    for fbpa in range(FBPA_UC_COUNT):
        base = FBPA_UC_BASE + fbpa * FBPA_UC_STRIDE
        cfg1 = _read(base + FBPA_UC_CFG1)
        test_macro = _read(base + FBPA_UC_TEST_MACRO)
        instruction = _read(base + FBPA_UC_INSTR)
        mode = _read(base + FBPA_UC_MODE)
        data_observed = bool(int(hbm_data_read_mask) & (1 << fbpa))
        # DATA is a read-to-advance port.  Absence is represented by null and
        # must never be replaced with the helper's zero-filled ABI slot.
        wdr = _read(base + FBPA_UC_WDR) if data_observed else None
        shadow_instruction = _read(base + FBPA_UC_SHWIR)
        shadow_data = _read(base + FBPA_UC_SHWDR)
        status = _read(base + FBPA_UC_STATUS)
        aperture_responded = (
            (cfg1 & 0xFFFF0000) != 0xBADF0000 and cfg1 != 0xFFFFFFFF
        )
        enabled = (
            fbio_population["fbpa_enabled"][fbpa]
            if fbio_population["fbpa_enabled"] is not None else None
        )
        live = bool(aperture_responded and enabled)
        shadow_opcode = (shadow_instruction >> 4) & 0xFF
        test_macro_decoded = decode_hbm_test_macro(test_macro)
        test_macro_ready = (
            not test_macro_decoded["badf"] and
            not test_macro_decoded["all_ones"] and
            (test_macro & (~0x0000000E & 0xFFFFFFFF)) == 0x00000011
        )
        temperature_context = (
            instruction == 0x0000000F and mode == 0x00000008 and
            shadow_instruction == 0x000000F0 and test_macro_ready
        )
        decoded = decode_hbm(
            wdr, temperature_context, aperture_responded, enabled,
            data_observed=data_observed
        )
        status_badf = (status & 0xFFFF0000) == 0xBADF0000
        status_all_ones = status == 0xFFFFFFFF
        bridge_state = status & 0x1F
        status_reserved_zero = (status & ~0x1F) == 0
        bridge_available = not (
            status_badf or status_all_ones or not status_reserved_zero
        )
        decoded.update({
            "fbpa": fbpa, "cfg1": "0x%08x" % cfg1,
            "aperture_responded": aperture_responded,
            "enabled": enabled, "live": live,
            "test_macro": "0x%08x" % test_macro,
            "test_macro_decoded": test_macro_decoded,
            "test_macro_temperature_context_ready": test_macro_ready,
            "instruction": "0x%08x" % instruction,
            "mode": "0x%08x" % mode,
            "shadow_instruction": "0x%08x" % shadow_instruction,
            "shadow_instruction_opcode": "0x%02x" % shadow_opcode,
            "shadow_instruction_is_temperature":
                shadow_instruction == 0x000000F0,
            "shadow_instruction_corroborates_temperature_empirically":
                shadow_instruction == 0x000000F0,
            "temperature_context_requirements": {
                "instruction_equals": "0x0000000f",
                "mode_equals": "0x00000008",
                "shadow_instruction_equals": "0x000000f0",
                "test_macro_available": True,
                "test_macro_dram_reset_disabled": True,
                "test_macro_dram_wrst_reset_disabled": True,
                "test_macro_read_i1500_register": False,
                "test_macro_writable_bits_equal": "0x00000011",
                "test_macro_temperature_sideband_mask": "0x0000000e",
                "shadow_instruction_role": "empirical-latch-corroboration",
            },
            "shadow_data": "0x%08x" % shadow_data,
            "status": "0x%08x" % status,
            "status_bridge": {
                "raw": "0x%08x" % status,
                "available": bridge_available,
                "badf": status_badf,
                "all_ones": status_all_ones,
                "reserved_bits_zero": status_reserved_zero,
                "state_value": bridge_state if bridge_available else None,
                "state_name": (
                    I1500_BRIDGE_STATE_NAMES.get(bridge_state, "reserved")
                    if bridge_available else None
                ),
                "idle": bridge_state == 0 if bridge_available else None,
                "transaction_active_or_nonidle": (
                    bridge_state != 0 if bridge_available else None
                ),
                "coherence_signal": (
                    "idle-observed" if bridge_available and bridge_state == 0
                    else "nonidle-observed-possible-race" if bridge_available
                    else "unavailable-reserved-bits"
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
            },
            "cfg1_offset": "0x%08x" % (base + FBPA_UC_CFG1),
            "test_macro_offset": "0x%08x" % (base + FBPA_UC_TEST_MACRO),
            "instruction_offset": "0x%08x" % (base + FBPA_UC_INSTR),
            "mode_offset": "0x%08x" % (base + FBPA_UC_MODE),
            "data_observed": data_observed,
            "wdr_offset": "0x%08x" % (base + FBPA_UC_WDR),
            "shadow_instruction_offset": "0x%08x" % (base + FBPA_UC_SHWIR),
            "shadow_data_offset": "0x%08x" % (base + FBPA_UC_SHWDR),
            "status_offset": "0x%08x" % (base + FBPA_UC_STATUS),
            "interpretation": (
                "IEEE1500 DATA is a read-to-advance port and is null unless "
                "explicitly acknowledged; TEST_MACRO is raw sideband context "
                "with no Celsius conversion; decoded as temperature only "
                "when enabled/responding, INSTR=0x0f, MODE=0x08, SHWIR is "
                "exactly 0xf0, and TEST_MACRO proves reset-safe non-read mode; "
                "exact STATUS bridge state is separately classified because "
                "the post-DATA observation may race; this "
                "probe never initiates a fresh HBM transaction; distinct physical "
                "sensor not established"
            ),
        })
        fbpas.append(decoded)

    stacks = []
    for ordinal, (name, fbps) in enumerate(STACKS):
        members = []
        for fbp in fbps:
            members.extend((2 * fbp, 2 * fbp + 1))
        valid_temps = [
            fbpas[item]["temperature_c"] for item in members
            if fbpas[item]["live"] and fbpas[item]["valid"]
        ]
        enabled_fbpas = [item for item in members
                         if fbpas[item]["enabled"] is True]
        responding_fbpas = [item for item in members
                            if fbpas[item]["aperture_responded"]]
        usable_fbpas = [item for item in members if fbpas[item]["live"]]
        selected_fbpa = enabled_fbpas[0] if enabled_fbpas else None
        selected = fbpas[selected_fbpa] if selected_fbpa is not None else None
        stacks.append({
            "stack": name, "fbps": list(fbps), "fbpas": members,
            "firmware_site_ordinal": ordinal,
            "xray_sequential_label": name,
            "public_hbmmap_label": (
                PUBLIC_HBMMAP_LABELS_BY_FIRMWARE_ORDINAL[ordinal]
            ),
            "label_authority": "presentation-only-membership-authoritative",
            "site_present": (selected_fbpa is not None
                             if fbio_population["available"] else None),
            "selected_fbpa": selected_fbpa,
            "selected_fbpa_valid": (
                selected["valid"] if selected is not None else None
            ),
            "selected_temperature_c": (
                selected["temperature_c"] if selected is not None else None
            ),
            "selected_aperture_responded": (
                selected["aperture_responded"] if selected is not None else None
            ),
            "enabled_fbpas": enabled_fbpas,
            "responding_fbpas": responding_fbpas,
            "usable_fbpas": usable_fbpas,
            "live_fbpas": usable_fbpas,
            "valid_temperatures_c": valid_temps,
            "unique_temperatures_c": sorted(set(valid_temps)),
            "all_valid_views_equal": bool(valid_temps) and len(set(valid_temps)) == 1,
            "interpretation": (
                "exact GA100 PMU table order; selected_fbpa is the first enabled "
                "firmware-selected routing candidate/source; passive measurement "
                "context remains conditional and non-atomic; other views remain "
                "diagnostic and no physical sensor count is inferred"
            ),
        })

    current_index = _read(NV_THERM_GPC_TSENSE_INDEX)
    snapshot_index = _read(NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT)
    current_selector = selector_semantics(current_index)
    snapshot_selector = selector_semantics(snapshot_index)
    current_read_increment = bool(current_index & (1 << 30))
    snapshot_read_increment = bool(snapshot_index & (1 << 30))
    current_reads_skipped = True
    snapshot_reads_skipped = True
    if current_reads_skipped:
        current_temperature = skipped_sfp95_read()
        current_offset_temperature = skipped_sfp95_read()
        current_raw_adc = None
        current_observed_raw_adc = None
    else:
        current_temp_raw = _read(NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR)
        current_offset_raw = _read(
            NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR)
        current_adc_raw = _read(NV_THERM_GPC_TSENSE_RAW_CODE)
        if current_selector["selector_semantically_valid"]:
            current_temperature = decode_sfp95(current_temp_raw)
            current_offset_temperature = decode_sfp95(current_offset_raw)
            current_raw_adc = "0x%08x" % current_adc_raw
            current_observed_raw_adc = None
        else:
            current_temperature = unbound_sfp95_read(current_temp_raw)
            current_offset_temperature = unbound_sfp95_read(current_offset_raw)
            current_raw_adc = None
            current_observed_raw_adc = "0x%08x" % current_adc_raw
    if snapshot_reads_skipped:
        snapshot_temperature = skipped_sfp95_read()
        snapshot_offset_temperature = skipped_sfp95_read()
    else:
        snapshot_temp_raw = _read(
            NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR_SNAPSHOT)
        snapshot_offset_raw = _read(
            NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR_SNAPSHOT)
        if snapshot_selector["selector_semantically_valid"]:
            snapshot_temperature = decode_sfp95(snapshot_temp_raw)
            snapshot_offset_temperature = decode_sfp95(snapshot_offset_raw)
        else:
            snapshot_temperature = unbound_sfp95_read(snapshot_temp_raw)
            snapshot_offset_temperature = unbound_sfp95_read(snapshot_offset_raw)
    opt_ts_dis_mask = _read(NV_FUSE_OPT_TS_DIS_MASK)
    status_opt_gpc = _read(NV_FUSE_STATUS_OPT_GPC)
    num_active_fbps = _read(FBPA_NUM_ACTIVE)
    fbpa_training = _read(FBPA_TRAINING)
    liveness = liveness_report(
        _read(NV_THERM_TEMP_SENSOR_TSENSE), num_active_fbps
    )
    result = {
        "access": "bar-read",
        "writes_issued": 0,
        "hbm_data_read_mask": "0x%08x" % int(hbm_data_read_mask),
        "hbm_data_ports_observed": bin(int(hbm_data_read_mask)).count("1"),
        "selector_dependent_skipped_mask": "0x%08x" % reported_skip_mask,
        "selector_dependent_read_policy":
            "unconditionally-omitted-owner-race",
        "selector_dependent_reads_unconditionally_omitted": True,
        "prewrite_liveness": liveness,
        "aggregate_readouts": aggregates,
        "fixed_context_registers": context_registers,
        "gpu_sci": gpu_sci,
        "gpc_analog_context": gpc_analog,
        "logical_hardware_backed_endpoint_census": {
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
        },
        "gpc_autopoll_controls": auto,
        "currently_selected_gpc_readout": {
            "selector": "0x%08x" % current_index,
            **current_selector,
            "read_increment_enabled": current_read_increment,
            "dependent_reads_skipped": current_reads_skipped,
            "temperature": current_temperature,
            "offset_temperature": current_offset_temperature,
            "raw_adc": current_raw_adc,
            "observed_raw_adc_unbound": current_observed_raw_adc,
            "warning": (
                "selector was observed, but dependent payload ports were "
                "unconditionally omitted because their owner can race"
            ),
        },
        "snapshot_as_found": {
            "selector": "0x%08x" % snapshot_index,
            **snapshot_selector,
            "read_increment_enabled": snapshot_read_increment,
            "dependent_reads_skipped": snapshot_reads_skipped,
            "temperature": snapshot_temperature,
            "offset_temperature": snapshot_offset_temperature,
            "warning": (
                "no trigger was issued and selector-dependent snapshot payload "
                "ports were unconditionally omitted because their owner can race"
            ),
        },
        "fbpa_readout_channels": fbpas,
        "hbm_stack_groups": stacks,
        "fbio_population": fbio_population,
        "hbm_transaction_policy": (
            "DATA read-to-advance ports are skipped unless semantic mode has "
            "an explicit HBM-DATA acknowledgement; require INSTR=0x0f, "
            "MODE=0x08, exact SHWIR=0xf0, and reset-safe TEST_MACRO context "
            "before decoding observed DATA; "
            "the collector never initiates an IEEE1500 temperature transaction"
        ),
        "sentinels": {
            "fbpa_num_active": "0x%08x" % num_active_fbps,
            "fbpa_training": "0x%08x" % fbpa_training,
        },
        "population": population_report(opt_ts_dis_mask, status_opt_gpc),
    }
    if legacy_backend:
        reads_after = getattr(probe, "reads_issued", None)
        observed_reads = (
            reads_after - reads_before
            if isinstance(reads_before, int) and isinstance(reads_after, int)
            else len(register_cache)
        )
        # The legacy path de-duplicates repeated SCI/TS_AUTO views in its
        # userspace cache; schema v8/helper ABI5 adds STATUS_OPT_FBIO and
        # unconditionally omits all selector-dependent payload ports.
        expected_reads = 244
        dead_bar = not liveness["two_signal_corroborated"]
        result.update({
            "status": (
                "failed" if dead_bar or observed_reads != expected_reads
                else "conditional"
            ),
            "error": (
                "legacy BAR lacks affirmative two-signal liveness" if dead_bar else
                "legacy unique MMIO read-count mismatch" if
                observed_reads != expected_reads else None
            ),
            "backend": "legacy-gpcprobe",
            "evidence_authority": "conditional-non-authoritative",
            "transaction_journal_available": False,
            "postflight_identity_verified": False,
            "reads_issued": observed_reads,
            "read_count_contract": (
                "244 unique reads with seven selector-dependent reads omitted"
            ),
            "dead_bar_sentinel_check": "failed" if dead_bar else "passed",
        })
    return result


def decode_helper_pure(capture, card):
    helper_status = int(capture.status)
    snapshot_requested = bool(
        int(capture.flags) & XRAY_THERM_CAPTURE_F_SNAPSHOT
    )
    fresh_prewrite_liveness = (
        liveness_report(
            int(capture.prewrite_tsense), int(capture.prewrite_active_fbps)
        ) if snapshot_requested else None
    )
    if not bool(capture.pure_completed):
        return {
            "access": "bar-read",
            "status": "failed",
            "error": (
                "dedicated helper did not complete the pure capture; "
                "zero-filled payload is intentionally not decoded"
            ),
            "backend": "xray-therm-fixed-capture",
            "helper_status": helper_status,
            "pure_completed": False,
            "reads_issued": int(capture.reads_issued),
            "writes_issued": 0,
            "transaction_total_writes": int(capture.writes_issued),
            "selector_dependent_skipped_mask": "0x%08x" % int(
                capture.selector_dependent_skipped_mask
            ),
            "hbm_data_read_mask": "0x%08x" % int(
                capture.hbm_data_read_mask
            ),
            "fresh_prewrite_liveness": fresh_prewrite_liveness,
            "driver_bound_at_entry": bool(capture.driver_bound_at_entry),
            "autopoll_unchanged": bool(capture.autopoll_unchanged),
            "sentinels_unchanged": bool(capture.sentinels_unchanged),
            "journal_scope": "minimal-no-register-decode",
            "identity": card.get("bdf"),
        }
    read_contract = helper_read_contract(capture)
    hbm_requested = read_contract["hbm_data_requested"]
    expected_hbm_mask = 0xFFFFFF if hbm_requested else 0
    mask_contract_ok = (
        int(capture.hbm_data_read_mask) == expected_hbm_mask and
        int(capture.selector_dependent_skipped_mask) == 0xF
    )
    pure_ok = (
        helper_status == 0 and
        bool(capture.autopoll_unchanged) and
        bool(capture.sentinels_unchanged) and read_contract["valid"] and
        mask_contract_ok
    )
    if snapshot_requested:
        pure_ok = (
            pure_ok and
            fresh_prewrite_liveness["two_signal_corroborated"]
        )
    elif (int(capture.prewrite_tsense) != 0 or
          int(capture.prewrite_active_fbps) != 0):
        pure_ok = False
    pure = capture_pure_reads(
        CapturedRegisterProbe(capture), card,
        hbm_data_read_mask=int(capture.hbm_data_read_mask),
        selector_dependent_skipped_mask=int(
            capture.selector_dependent_skipped_mask
        ),
    )
    if not pure["prewrite_liveness"]["two_signal_corroborated"]:
        pure_ok = False
    pure.update({
        "status": "pass" if pure_ok else "failed",
        "error": None if pure_ok else (
            "dedicated helper transaction failed; partial journal retained"
        ),
        "backend": "xray-therm-fixed-capture",
        "helper_status": helper_status,
        "pure_completed": bool(capture.pure_completed),
        "reads_issued": int(capture.reads_issued),
        "read_count_contract": read_contract["description"],
        "trigger_poll_reads": read_contract["trigger_poll_reads"],
        "selector_dependent_omit_count": read_contract["skip_deduction"],
        "hbm_data_requested": hbm_requested,
        "fresh_prewrite_liveness": fresh_prewrite_liveness,
        "fresh_prewrite_liveness_scope": (
            "after-saved-selector-validation-immediately-before-first-write"
            if snapshot_requested else "not-requested-pure-capture"
        ),
        "transaction_total_writes": int(capture.writes_issued),
        "driver_bound_at_entry": bool(capture.driver_bound_at_entry),
        "autopoll_unchanged": bool(capture.autopoll_unchanged),
        "sentinels_unchanged": bool(capture.sentinels_unchanged),
        "fbpa_context_atomic": False,
        "fbpa_context_warning": (
            "seven fixed FBPA context fields plus optional acknowledged DATA "
            "are sequential and can race their firmware owner; context "
            "correlation is not atomic"
        ),
        "population": population_report(
            int(capture.opt_ts_dis_mask), int(capture.status_opt_gpc)
        ),
    })
    pure["currently_selected_gpc_readout"]["coherent_tuple"] = False
    pure["snapshot_as_found"].update({
        "selector_before": "0x%08x" % int(
            capture.snapshot_as_found_selector_before
        ),
        "selector_after": "0x%08x" % int(
            capture.snapshot_as_found_selector_after
        ),
        "selector_stable": (
            int(capture.snapshot_as_found_selector_before) ==
            int(capture.snapshot_as_found_selector_after)
        ),
        "selector_stability_source": "userspace-recomputed-before-equals-after",
        "coherent_tuple": False,
        "stability_scope": (
            "selector-before equals selector-after only; dependent temperature "
            "and offset payload ports were unconditionally omitted"
        ),
    })
    return pure


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="auto")
    parser.add_argument(
        "--probe-backend", choices=("auto", "xray-therm", "gpcprobe"),
        default="auto",
    )
    parser.add_argument("--sysfs-root", default="/sys")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all-targets", action="store_true")
    selection.add_argument("--bdf", action="append", type=normalize_bdf)
    parser.add_argument(
        "--access", choices=("bar-read", "semantic-read"), default="bar-read"
    )
    parser.add_argument("--acknowledge-selector-write", action="store_true")
    parser.add_argument("--acknowledge-bound-snapshot", action="store_true")
    parser.add_argument(
        "--acknowledge-hbm-data-read-to-advance", action="store_true",
        help=(
            "semantic-read only: explicitly permit all 24 stateful FBPA "
            "IEEE1500 DATA read-to-advance accesses"
        ),
    )
    parser.add_argument("--allow-active-workload", action="store_true")
    parser.add_argument("--max-utilization", type=float, default=5.0)
    parser.add_argument(
        "--no-driver-queries", action="store_true",
        help=(
            "disable the non-canonical nvidia-smi fallback; semantic-read then "
            "requires --allow-active-workload because utilization is unknown"
        ),
    )
    parser.add_argument("--output")
    return parser.parse_args(argv)


def emit_document(encoded, output_name):
    if not output_name:
        sys.stdout.write(encoded)
        return
    output = Path(output_name).resolve()
    if output.exists():
        raise ProbeError("refusing to overwrite output: %s" % output)
    if not output.parent.is_dir():
        raise ProbeError("output directory does not exist: %s" % output.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.tmp-" % output.name, dir=str(output.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(output))
        except FileExistsError as error:
            raise ProbeError("refusing to overwrite output: %s" % output) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv=None):
    args = parse_args(argv)
    if not math.isfinite(args.max_utilization) or not (
            0.0 <= args.max_utilization <= 100.0):
        raise ProbeError("--max-utilization must be finite and in [0, 100]")
    if args.access == "semantic-read" and not args.acknowledge_selector_write:
        raise ProbeError(
            "semantic-read requires --acknowledge-selector-write"
        )
    if args.acknowledge_selector_write and args.access != "semantic-read":
        raise ProbeError(
            "--acknowledge-selector-write is valid only for semantic-read"
        )
    if args.acknowledge_bound_snapshot and args.access != "semantic-read":
        raise ProbeError(
            "--acknowledge-bound-snapshot is valid only for semantic-read"
        )
    if (args.acknowledge_hbm_data_read_to_advance and
            args.access != "semantic-read"):
        raise ProbeError(
            "--acknowledge-hbm-data-read-to-advance is valid only for "
            "semantic-read"
        )
    backend, device = resolve_probe_backend(args.probe_backend, args.device)
    if args.access == "semantic-read" and backend != "xray-therm":
        raise ProbeError(
            "semantic-read requires xray-therm; public gpcprobe ABI is read-only"
        )
    if args.acknowledge_bound_snapshot and backend != "xray-therm":
        raise ProbeError(
            "--acknowledge-bound-snapshot requires --probe-backend xray-therm"
        )
    if (args.acknowledge_hbm_data_read_to_advance and
            backend != "xray-therm"):
        raise ProbeError(
            "--acknowledge-hbm-data-read-to-advance requires "
            "--probe-backend xray-therm"
        )
    pci_cards = sysfs_cards(args.sysfs_root)
    requested = set(args.bdf or ())

    helper = None
    probe = None
    helper_pairs = []
    loaded_component_identities = {}
    try:
        if backend == "xray-therm":
            therm = _load_sibling_module(
                "xray_therm.py", "xray_therm_runtime"
            )
            loaded_component_identities["xray_therm"] = (
                therm.__xray_source_identity__
            )
            helper = therm.ThermHelper(
                device, allow_snapshot=args.access == "semantic-read"
            )
            helper_pairs = bind_therm_cards(
                helper.cards(), pci_cards, require_complete=args.all_targets
            )
            cards = [pair[0] for pair in helper_pairs]
        else:
            probe = GpcProbe(device)
            cards = bind_probe_indices(
                probe.cards, pci_cards, require_complete=args.all_targets
            )

        selected = cards if args.all_targets else [
            card for card in cards if card["bdf"] in requested
        ]
        missing = sorted(requested - {card["bdf"] for card in selected})
        if missing:
            raise ProbeError("requested BDFs are unavailable: %s" % missing)
        if not selected:
            raise ProbeError("no supported GA100 targets were selected")

        if args.no_driver_queries:
            smi = {"status": "disabled", "cards": {}}
            direct = {"status": "disabled", "cards": {}}
        else:
            smi = nvidia_smi_snapshot()
            nvml = _load_sibling_module(
                "nvml_temperature.py", "nvml_temperature_runtime"
            )
            loaded_component_identities["nvml_temperature"] = (
                nvml.__xray_source_identity__
            )
            direct = nvml.capture([card["bdf"] for card in selected])

        if (args.access == "semantic-read" and not args.allow_active_workload and
                smi.get("status") != "ok"):
            raise ProbeError(
                "semantic-read workload gate has no successful driver query; "
                "use --allow-active-workload only after independent verification"
            )

        helper_by_bdf = {
            card["bdf"]: helper_card for card, helper_card in helper_pairs
        }
        records = []
        for card in selected:
            driver = smi.get("cards", {}).get(card["bdf"])
            utilization = driver.get("utilization_percent") if driver else None
            if args.access == "semantic-read" and not args.allow_active_workload:
                if utilization is None or not math.isfinite(utilization):
                    raise ProbeError(
                        "%s has no finite utilization sample for semantic-read; "
                        "use --allow-active-workload only after independent verification"
                        % card["bdf"]
                    )
                if utilization > args.max_utilization:
                    raise ProbeError(
                        "%s utilization %.1f%% exceeds semantic-read gate %.1f%%"
                        % (card["bdf"], utilization, args.max_utilization)
                    )
            direct_card = direct.get("cards", {}).get(card["bdf"])
            record = {
                "identity": card,
                "driver_temperature_census": {
                    "canonical_direct_nvml": direct_card,
                    "fallback_nvidia_smi": driver,
                },
                "hwmon": hwmon_snapshot(card["bdf"], args.sysfs_root),
            }
            if backend == "xray-therm":
                helper_card = helper_by_bdf[card["bdf"]]
                if (args.access == "semantic-read" and
                        helper_card.get("driver_bound") and
                        not args.acknowledge_bound_snapshot):
                    raise ProbeError(
                        "%s is driver-bound; semantic snapshot requires "
                        "--acknowledge-bound-snapshot and a helper loaded with "
                        "allow_bound_snapshot=1" % card["bdf"]
                    )
                capture = helper.capture(
                    helper_card,
                    snapshot=args.access == "semantic-read",
                    allow_bound=args.acknowledge_bound_snapshot,
                    hbm_data=args.acknowledge_hbm_data_read_to_advance,
                )
                record["pure"] = decode_helper_pure(capture, card)
                if args.access == "semantic-read":
                    record["global_snapshot"] = decode_helper_snapshot(
                        capture,
                        bound_snapshot_acknowledged=(
                            args.acknowledge_bound_snapshot
                        ),
                    )
            else:
                record["pure"] = capture_pure_reads(
                    probe, card, legacy_backend=True
                )
            records.append(record)
    finally:
        if helper is not None:
            helper.close()
        if probe is not None:
            probe.close()
    transaction_failed = any(
        record.get("pure", {}).get("status") == "failed" or
        record.get("global_snapshot", {}).get("status") == "failed"
        for record in records
    )
    document = {
        "schema": SCHEMA,
        "version": VERSION,
        "status": "failed" if transaction_failed else "pass",
        "captured_utc": utc_now(),
        "access": args.access,
        "probe_backend": backend,
        "probe_device": device,
        "bound_snapshot_acknowledged": bool(
            args.acknowledge_bound_snapshot
        ),
        "selector_write_acknowledged": bool(
            args.acknowledge_selector_write
        ),
        "hbm_data_read_to_advance_acknowledged": bool(
            args.acknowledge_hbm_data_read_to_advance
        ),
        "active_workload_override": bool(args.allow_active_workload),
        "max_utilization_percent": args.max_utilization,
        "source_components": source_component_records(
            backend, not args.no_driver_queries, loaded_component_identities
        ),
        "driver_query_status": {
            "direct_nvml": direct.get("status"),
            "nvidia_smi_fallback": smi.get("status"),
        },
        "driver_temperature_census": {
            "contract": DRIVER_CENSUS_CONTRACT,
            "direct_nvml": {
                key: value for key, value in direct.items() if key != "cards"
            },
            "fallback_command": {
                key: value for key, value in smi.items() if key != "cards"
            },
        },
        "cards": records,
        "interpretation": {
            "physical_sensor_count": "not inferred from readout-channel count",
            "gpc_address_space": "8 GPC x 3 BJT = 24 endpoints; population empirical",
            "hbm_address_space": (
                "24 FBPA latch channels grouped into 6 fixed FBP-pair views; "
                "temperature is exposed only under confirmed transaction context"
            ),
            "driver_query": (
                "direct NVML preserves per-call and per-field return codes; "
                "nvidia-smi is a non-canonical workload/fallback view; unsupported "
                "and N/A values remain null, never zero"
            ),
            "dedicated_helper": (
                "fixed capture ABI has no caller-selected MMIO primitive; legacy "
                "gpcprobe remains domain-less and is not the preferred backend"
            ),
        },
    }
    encoded = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    emit_document(encoded, args.output)
    return 2 if transaction_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ProbeError, OSError) as error:
        sys.stderr.write("temperature probe failed: %s\n" % error)
        sys.exit(2)
