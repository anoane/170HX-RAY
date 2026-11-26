#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct, query-only NVML temperature census.

This module uses only the Python standard library.  It preserves every NVML
return code and never converts an unsupported value to zero.  The API calls
below are getters, although driver/firmware implementation side effects remain
opaque to userspace.
"""

from __future__ import print_function

import ctypes
import ctypes.util
import time


NVML_SUCCESS = 0
NVML_ERROR_ALREADY_INITIALIZED = 5
NVML_MAX_THERMAL_SENSORS_PER_GPU = 3
NVML_THERMAL_TARGET_ALL = 15

RETURN_NAMES = {
    0: "success",
    1: "uninitialized",
    2: "invalid-argument",
    3: "not-supported",
    4: "no-permission",
    5: "already-initialized",
    6: "not-found",
    7: "insufficient-size",
    8: "insufficient-power",
    9: "driver-not-loaded",
    10: "timeout",
    11: "irq-issue",
    12: "library-not-found",
    13: "function-not-found",
    14: "corrupted-inforom",
    15: "gpu-is-lost",
    16: "reset-required",
    17: "operating-system",
    18: "library-rm-version-mismatch",
    19: "in-use",
    20: "memory",
    21: "no-data",
    22: "vgpu-ecc-not-supported",
    23: "insufficient-resources",
    24: "frequency-not-supported",
    25: "argument-version-mismatch",
    26: "deprecated",
    27: "not-ready",
    28: "gpu-not-found",
    29: "invalid-state",
    30: "reset-type-not-supported",
    999: "unknown",
}

VALUE_TYPE_NAMES = {
    0: "double",
    1: "unsigned-int",
    2: "unsigned-long",
    3: "unsigned-long-long",
    4: "signed-long-long",
    5: "signed-int",
    6: "unsigned-short",
}

FIELD_SPECS = (
    (82, "memory_temperature_c"),
    (193, "shutdown_threshold_c"),
    (194, "slowdown_threshold_c"),
    (195, "memory_max_threshold_c"),
    (196, "gpu_max_threshold_c"),
    (308, "gpu_temperature_subdegree_c"),
)

TEMPERATURE_SENSORS = ((0, "gpu"), (1, "gpu_max"))
THRESHOLDS = (
    (0, "shutdown"),
    (1, "slowdown"),
    (2, "memory_max"),
    (3, "gpu_max"),
    (4, "acoustic_min"),
    (5, "acoustic_current"),
    (6, "acoustic_max"),
    (7, "gps_current"),
)

THERMAL_CONTROLLER_NAMES = {
    0: "none",
    1: "gpu-internal",
    2: "adm1032",
    3: "adt7461",
    4: "max6649",
    5: "max1617",
    6: "lm99",
    7: "lm89",
    8: "lm64",
    9: "g781",
    10: "adt7473",
    11: "sbmax6649",
    12: "vbios-event",
    13: "os",
    14: "nvsyscon-canoas",
    15: "nvsyscon-e551",
    16: "max6649r",
    17: "adt7473s",
    -1: "unknown",
}

THERMAL_TARGET_NAMES = {
    0: "none",
    1: "gpu",
    2: "memory",
    4: "power-supply",
    8: "board",
    9: "vcd-board",
    10: "vcd-inlet",
    11: "vcd-outlet",
    15: "all",
    -1: "unknown",
}


class NvmlValue(ctypes.Union):
    _fields_ = [
        ("dVal", ctypes.c_double),
        ("siVal", ctypes.c_int),
        ("uiVal", ctypes.c_uint),
        ("ulVal", ctypes.c_ulong),
        ("ullVal", ctypes.c_ulonglong),
        ("sllVal", ctypes.c_longlong),
        ("usVal", ctypes.c_ushort),
    ]


class NvmlFieldValue(ctypes.Structure):
    _fields_ = [
        ("fieldId", ctypes.c_uint),
        ("scopeId", ctypes.c_uint),
        ("timestamp", ctypes.c_longlong),
        ("latencyUsec", ctypes.c_longlong),
        ("valueType", ctypes.c_int),
        ("nvmlReturn", ctypes.c_int),
        ("value", NvmlValue),
    ]


class NvmlTemperature(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint),
        ("sensorType", ctypes.c_int),
        ("temperature", ctypes.c_int),
    ]


class NvmlThermalSensor(ctypes.Structure):
    _fields_ = [
        ("controller", ctypes.c_int),
        ("defaultMinTemp", ctypes.c_int),
        ("defaultMaxTemp", ctypes.c_int),
        ("currentTemp", ctypes.c_int),
        ("target", ctypes.c_int),
    ]


class NvmlGpuThermalSettings(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint),
        ("sensor", NvmlThermalSensor * NVML_MAX_THERMAL_SENSORS_PER_GPU),
    ]


class NvmlMarginTemperature(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint),
        ("marginTemperature", ctypes.c_int),
    ]


EXPECTED_LAYOUT = {
    "value": (NvmlValue, 8),
    "field_value": (NvmlFieldValue, 40),
    "temperature_v1": (NvmlTemperature, 12),
    "thermal_sensor": (NvmlThermalSensor, 20),
    "thermal_settings": (NvmlGpuThermalSettings, 64),
    "margin_temperature_v1": (NvmlMarginTemperature, 8),
}


def assert_layout():
    mismatches = []
    for name, (kind, expected) in EXPECTED_LAYOUT.items():
        actual = ctypes.sizeof(kind)
        if actual != expected:
            mismatches.append("%s=%d expected=%d" % (name, actual, expected))
    offsets = {
        "field_value.timestamp": (NvmlFieldValue.timestamp.offset, 8),
        "field_value.latency": (NvmlFieldValue.latencyUsec.offset, 16),
        "field_value.value_type": (NvmlFieldValue.valueType.offset, 24),
        "field_value.return": (NvmlFieldValue.nvmlReturn.offset, 28),
        "field_value.value": (NvmlFieldValue.value.offset, 32),
        "thermal_settings.sensor": (NvmlGpuThermalSettings.sensor.offset, 4),
    }
    for name, (actual, expected) in offsets.items():
        if actual != expected:
            mismatches.append("%s=%d expected=%d" % (name, actual, expected))
    if mismatches:
        raise RuntimeError("NVML ctypes ABI layout mismatch: %s" % ", ".join(mismatches))


def _versioned_size(struct_type, version=1):
    return ctypes.sizeof(struct_type) | (int(version) << 24)


def _return(code, library=None):
    code = int(code)
    record = {"code": code, "name": RETURN_NAMES.get(code, "unrecognized")}
    if library is not None and hasattr(library, "nvmlErrorString"):
        try:
            value = library.nvmlErrorString(code)
            if value:
                record["message"] = value.decode("utf-8", "replace")
        except (AttributeError, TypeError, ValueError):
            pass
    return record


def _configure(library, name, argtypes, required=False):
    function = getattr(library, name, None)
    if function is None:
        if required:
            raise RuntimeError("required NVML symbol is absent: %s" % name)
        return None
    function.argtypes = argtypes
    function.restype = ctypes.c_int
    return function


def _load_library():
    candidates = ["libnvidia-ml.so.1", "nvml.dll", "libnvidia-ml.dylib"]
    located = ctypes.util.find_library("nvidia-ml")
    if located and located not in candidates:
        candidates.append(located)
    errors = []
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate), candidate, errors
        except OSError as error:
            errors.append({"candidate": candidate, "error": str(error)})
    return None, None, errors


def _field_value(item):
    kind = int(item.valueType)
    if kind == 0:
        return float(item.value.dVal)
    if kind == 1:
        return int(item.value.uiVal)
    if kind == 2:
        return int(item.value.ulVal)
    if kind == 3:
        return int(item.value.ullVal)
    if kind == 4:
        return int(item.value.sllVal)
    if kind == 5:
        return int(item.value.siVal)
    if kind == 6:
        return int(item.value.usVal)
    return None


def _query_fields(library, function, handle):
    values = (NvmlFieldValue * len(FIELD_SPECS))()
    for item, (field_id, _name) in zip(values, FIELD_SPECS):
        item.fieldId = field_id
        item.scopeId = 0
    started = time.monotonic_ns()
    outer = function(handle, len(values), values)
    elapsed_us = (time.monotonic_ns() - started) // 1000
    outer_code = int(outer)
    records = []
    for item, (field_id, name) in zip(values, FIELD_SPECS):
        item_return = int(item.nvmlReturn)
        # The array is zero-initialized, so item.nvmlReturn == SUCCESS is not
        # evidence that NVML populated an item when the enclosing call failed.
        # Preserve the raw word for audit, but make the outer failure the
        # effective return and suppress every payload field.
        effective_return = item_return if outer_code == NVML_SUCCESS else outer_code
        populated = outer_code == NVML_SUCCESS and item_return == NVML_SUCCESS
        records.append({
            "field_id": field_id,
            "observed_field_id": int(item.fieldId),
            "name": name,
            "scope_id": int(item.scopeId),
            "return": _return(effective_return, library),
            "raw_item_return": _return(item_return, library),
            "outer_call_succeeded": outer_code == NVML_SUCCESS,
            "value_type": ({
                "code": int(item.valueType),
                "name": VALUE_TYPE_NAMES.get(
                    int(item.valueType), "unrecognized"
                ),
            } if populated else None),
            "value": _field_value(item) if populated else None,
            "timestamp_epoch_us": int(item.timestamp) if populated else None,
            "latency_us": int(item.latencyUsec) if populated else None,
        })
    return {
        "outer_return": _return(outer_code, library),
        "call_elapsed_us": elapsed_us,
        "fields": records,
    }


def _query_temperature_v(library, function, handle):
    records = []
    for sensor_type, name in TEMPERATURE_SENSORS:
        value = NvmlTemperature()
        value.version = _versioned_size(NvmlTemperature)
        value.sensorType = sensor_type
        started = time.monotonic_ns()
        code = function(handle, ctypes.byref(value))
        records.append({
            "sensor_type": sensor_type,
            "name": name,
            "return": _return(code, library),
            "temperature_c": int(value.temperature) if code == 0 else None,
            "call_elapsed_us": (time.monotonic_ns() - started) // 1000,
            "structure_version": "0x%08x" % value.version,
        })
    return records


def _query_thermal_settings(library, function, handle):
    value = NvmlGpuThermalSettings()
    started = time.monotonic_ns()
    code = function(
        handle, NVML_THERMAL_TARGET_ALL, ctypes.byref(value)
    )
    elapsed_us = (time.monotonic_ns() - started) // 1000
    count = int(value.count)
    malformed = count > NVML_MAX_THERMAL_SENSORS_PER_GPU
    sensors = []
    if code == 0 and not malformed:
        for index in range(count):
            item = value.sensor[index]
            sensors.append({
                "index": index,
                "controller": {
                    "code": int(item.controller),
                    "name": THERMAL_CONTROLLER_NAMES.get(
                        int(item.controller), "unrecognized"
                    ),
                },
                "target": {
                    "code": int(item.target),
                    "name": THERMAL_TARGET_NAMES.get(
                        int(item.target), "unrecognized"
                    ),
                },
                "default_minimum_c": int(item.defaultMinTemp),
                "default_maximum_c": int(item.defaultMaxTemp),
                "current_temperature_c": int(item.currentTemp),
            })
    return {
        "sensor_index_requested": NVML_THERMAL_TARGET_ALL,
        "return": _return(code, library),
        "reported_count": count if code == 0 else None,
        "maximum_count": NVML_MAX_THERMAL_SENSORS_PER_GPU,
        "malformed_count": malformed if code == 0 else False,
        "sensors": sensors,
        "call_elapsed_us": elapsed_us,
    }


def _query_margin(library, function, handle):
    value = NvmlMarginTemperature()
    value.version = _versioned_size(NvmlMarginTemperature)
    started = time.monotonic_ns()
    code = function(handle, ctypes.byref(value))
    return {
        "return": _return(code, library),
        "margin_temperature_c": (
            int(value.marginTemperature) if code == 0 else None
        ),
        "call_elapsed_us": (time.monotonic_ns() - started) // 1000,
        "structure_version": "0x%08x" % value.version,
    }


def _query_thresholds(library, function, handle):
    records = []
    for threshold_type, name in THRESHOLDS:
        value = ctypes.c_uint()
        started = time.monotonic_ns()
        code = function(handle, threshold_type, ctypes.byref(value))
        records.append({
            "threshold_type": threshold_type,
            "name": name,
            "return": _return(code, library),
            "temperature_c": int(value.value) if code == 0 else None,
            "call_elapsed_us": (time.monotonic_ns() - started) // 1000,
        })
    return records


def capture(bdfs):
    """Return direct NVML temperature data keyed by normalized PCI BDF."""
    library, library_name, load_errors = _load_library()
    if library is None:
        return {
            "status": "unavailable",
            "library": None,
            "library_load_errors": load_errors,
            "cards": {},
        }
    try:
        init = _configure(library, "nvmlInit_v2", [], required=True)
        shutdown = _configure(library, "nvmlShutdown", [], required=True)
        handle_lookup = _configure(
            library, "nvmlDeviceGetHandleByPciBusId_v2",
            [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)], required=True,
        )
        fields = _configure(
            library, "nvmlDeviceGetFieldValues",
            [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(NvmlFieldValue)],
        )
        temperature_v = _configure(
            library, "nvmlDeviceGetTemperatureV",
            [ctypes.c_void_p, ctypes.POINTER(NvmlTemperature)],
        )
        thermal_settings = _configure(
            library, "nvmlDeviceGetThermalSettings",
            [ctypes.c_void_p, ctypes.c_uint,
             ctypes.POINTER(NvmlGpuThermalSettings)],
        )
        margin = _configure(
            library, "nvmlDeviceGetMarginTemperature",
            [ctypes.c_void_p, ctypes.POINTER(NvmlMarginTemperature)],
        )
        thresholds = _configure(
            library, "nvmlDeviceGetTemperatureThreshold",
            [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)],
        )
        if hasattr(library, "nvmlErrorString"):
            library.nvmlErrorString.argtypes = [ctypes.c_int]
            library.nvmlErrorString.restype = ctypes.c_char_p
    except (AttributeError, RuntimeError, TypeError) as error:
        return {
            "status": "unavailable",
            "library": library_name,
            "configuration_error": str(error),
            "library_load_errors": load_errors,
            "cards": {},
        }

    init_code = init()
    if init_code not in (NVML_SUCCESS, NVML_ERROR_ALREADY_INITIALIZED):
        return {
            "status": "unavailable",
            "library": library_name,
            "initialization_return": _return(init_code, library),
            "library_load_errors": load_errors,
            "cards": {},
        }

    cards = {}
    handle_lookup_failed = False
    try:
        for bdf in bdfs:
            handle = ctypes.c_void_p()
            started = time.monotonic_ns()
            handle_code = handle_lookup(
                str(bdf).encode("ascii"), ctypes.byref(handle)
            )
            record = {
                "bdf": str(bdf),
                "handle_lookup_return": _return(handle_code, library),
                "handle_lookup_elapsed_us": (
                    time.monotonic_ns() - started
                ) // 1000,
            }
            if handle_code == 0:
                record["field_values"] = (
                    _query_fields(library, fields, handle)
                    if fields is not None else {"status": "symbol-absent"}
                )
                record["temperature_v"] = (
                    _query_temperature_v(library, temperature_v, handle)
                    if temperature_v is not None else {"status": "symbol-absent"}
                )
                record["thermal_settings"] = (
                    _query_thermal_settings(library, thermal_settings, handle)
                    if thermal_settings is not None else {"status": "symbol-absent"}
                )
                record["margin_temperature"] = (
                    _query_margin(library, margin, handle)
                    if margin is not None else {"status": "symbol-absent"}
                )
                record["temperature_thresholds"] = (
                    _query_thresholds(library, thresholds, handle)
                    if thresholds is not None else {"status": "symbol-absent"}
                )
            else:
                handle_lookup_failed = True
            cards[str(bdf)] = record
    finally:
        shutdown_record = (
            _return(shutdown(), library)
            if init_code == NVML_SUCCESS else {
                "code": None,
                "name": "not-called-library-was-already-initialized",
            }
        )

    return {
        "status": "partial" if handle_lookup_failed else "ok",
        "library": library_name,
        "library_load_errors": load_errors,
        "initialization_return": _return(init_code, library),
        "shutdown_return": shutdown_record,
        "structure_sizes": {
            "field_value": ctypes.sizeof(NvmlFieldValue),
            "temperature_v1": ctypes.sizeof(NvmlTemperature),
            "thermal_settings": ctypes.sizeof(NvmlGpuThermalSettings),
            "margin_temperature_v1": ctypes.sizeof(NvmlMarginTemperature),
        },
        "function_availability": {
            "field_values": fields is not None,
            "temperature_v": temperature_v is not None,
            "thermal_settings": thermal_settings is not None,
            "margin_temperature": margin is not None,
            "temperature_thresholds": thresholds is not None,
        },
        "cards": cards,
        "interpretation": {
            "query_only_public_api": True,
            "driver_firmware_internal_side_effects": "opaque",
            "unsupported_values": "null-with-exact-return-code",
            "thermal_settings_slots_are_not_gpc_bjt_endpoints": True,
        },
    }


__all__ = [
    "capture", "NvmlFieldValue", "NvmlTemperature",
    "NvmlGpuThermalSettings", "NvmlMarginTemperature", "assert_layout",
]


assert_layout()
