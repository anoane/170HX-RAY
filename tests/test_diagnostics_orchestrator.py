#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline safety and selection tests for the diagnostic orchestrator."""

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "diagnostics" / "run_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("xray_diag_test", str(SCRIPT))
DIAG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAG)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def device_record(bdf, index, name="NVIDIA CMP 170HX"):
    return {
        "cuda_index": index, "pci_bdf": bdf, "name": name,
        "compute_capability": "8.0", "sm_count": 104,
        "vram_bytes": 64 << 30, "memory_bus_width_bits": 4096,
        "memory_clock_khz": 1215000, "l2_bytes": 40 << 20,
    }


def valid_telemetry(scope="per-dtype-bracketing-window", sample_count=1,
                    sampling_span_seconds=0.01):
    return {
        "scope": scope,
        "sampling_span_semantics": (
            "synthetic-sum-of-disjoint-window-sample-spans"
            if scope == "aggregate-of-per-dtype-bracketing-windows" else
            "last-relative-sample-offset-within-bracketing-window"
        ),
        "temperature_sensor": "nvml-temperature-gpu",
        "nvml_available": True, "status": "ready",
        "sample_count": sample_count,
        "sampling_span_seconds": sampling_span_seconds,
        "valid_sample_counts": {
            "power": sample_count, "sm_clock": sample_count,
            "memory_clock": sample_count,
            "temperature": sample_count, "utilization": sample_count,
        },
        "query_failure_counts": {
            "power": 0, "sm_clock": 0, "memory_clock": 0,
            "temperature": 0, "utilization": 0,
        },
        "power_w_avg": 100.0, "power_w_max": 101.0,
        "sm_mhz_avg": 1000.0, "sm_mhz_max": 1010.0,
        "memory_mhz_avg": 1200.0, "memory_mhz_max": 1210.0,
        "temperature_c_avg": 49.0, "temperature_c_max": 50.0,
        "gpu_util_percent_avg": 90.0, "gpu_util_percent_max": 91.0,
        "memory_util_percent_avg": 80.0,
        "memory_util_percent_max": 81.0,
    }


def valid_bandwidth_document(bdf, index, byte_count=4096, iterations=1):
    results = []
    milliseconds = 1.0
    for dtype, type_spec in DIAG.BANDWIDTH_TYPE_SPECS.items():
        (logical_bits, storage_bytes, logical_per_storage,
         signedness, packing) = type_spec
        adjusted = byte_count - byte_count % storage_bytes
        results.append({
            "dtype": dtype, "logical_element_bits": logical_bits,
            "storage_unit_bytes": storage_bytes,
            "logical_elements_per_storage_unit": logical_per_storage,
            "logical_elements": (
                adjusted // storage_bytes * logical_per_storage
            ),
            "signedness": signedness, "packing": packing,
            "bytes": adjusted, "iterations": iterations,
            "milliseconds": milliseconds,
            "traffic_definition": "one read plus one write",
            "gbps": 2.0 * adjusted * iterations / (milliseconds * 1.0e6),
            "rate_unit": "decimal-GB/s",
            "rate_scope": "physical-storage-bytes",
            "verified": True,
            "verification_scope": "full-buffer-host-scan",
            "telemetry": valid_telemetry(),
        })
    return {
        "schema": "170hx-xray-cuda-bandwidth-v3",
        "operation": "global-memory-copy-kernel",
        "measurement_scope": "typed-load-store-memory-traffic-no-arithmetic",
        "device": device_record(bdf, index), "results": results,
        "telemetry": valid_telemetry(
            "aggregate-of-per-dtype-bracketing-windows",
            sample_count=len(DIAG.BANDWIDTH_TYPE_SPECS),
            sampling_span_seconds=0.01 * len(DIAG.BANDWIDTH_TYPE_SPECS),
        ),
        "integer_note": "packed int4; no arithmetic",
        "float8_note": "not tested",
    }


def valid_p2p_document(first, second, sizes=(4096, 8192), iterations=1):
    def direction(source, destination):
        return {
            "from": {
                "cuda_index": source["cuda"]["cuda_index"],
                "pci_bdf": source["bdf"],
            },
            "to": {
                "cuda_index": destination["cuda"]["cuda_index"],
                "pci_bdf": destination["bdf"],
            },
            "destination_can_access_source": True,
            "peer_access_enabled": True,
            "results": [{
                "bytes": size, "iterations": iterations,
                "direct": {
                    "available": True, "latency_ms": 1.0,
                    "gbps": size / 1.0e6, "rate_unit": "decimal-GB/s",
                    "full_buffer_verified": True,
                },
                "host_staged": {
                    "path": "D2H-then-H2D-pinned", "latency_ms": 2.0,
                    "gbps": size / 2.0e6, "rate_unit": "decimal-GB/s",
                    "full_buffer_verified": True,
                },
            } for size in sizes],
            "all_copies_verified": True,
        }
    return {
        "schema": "170hx-xray-p2p-v2",
        "source": device_record(first["bdf"], first["cuda"]["cuda_index"]),
        "destination": device_record(
            second["bdf"], second["cuda"]["cuda_index"]),
        "peer_access_capability": {
            "source_can_access_destination": True,
            "destination_can_access_source": True,
        },
        "directions": [direction(first, second), direction(second, first)],
        "verification_scope": "full-buffer-host-scan-outside-timed-region",
        "all_copies_verified": True,
        "payload_bandwidth_note": "payload counted once",
    }


def feature_command(argv, stdout="", returncode=0):
    stderr = "" if returncode == 0 else "unsupported"
    return {
        "argv": list(argv), "cwd": None,
        "started_utc": "2026-11-26T12:00:00+00:00", "dry_run": False,
        "environment_overrides": {}, "executable": argv[0],
        "executable_sha256": "a" * 64,
        "trust_boundary": "fixture executable identity only",
        "status": "completed", "returncode": returncode,
        "duration_seconds": 0.01,
        "stdout": stdout, "stderr": stderr,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def valid_feature_inventory(card, smi="/usr/bin/nvidia-smi", ofa=None):
    uuid = card["nvidia_smi"]["uuid"]
    bdf = card["bdf"]
    outputs = {
        "identity": "00000000:01:00.0, %s, NVIDIA CMP 170HX\n" % uuid,
        "full_text": "NVIDIA-SMI full query fixture\n",
        "full_xml": (
            "<nvidia_smi_log><gpu><uuid>%s</uuid><pci>"
            "<pci_bus_id>00000000:01:00.0</pci_bus_id>"
            "</pci></gpu></nvidia_smi_log>" % uuid
        ),
        "ecc": "Enabled, Enabled, 0, 0, 0, 0\n",
        "mig_mode": "Disabled, Disabled\n",
        "sessions": "0, 0, 0, 0, 0, 0\n",
        "mig_gpu_profiles": "No MIG profiles supported\n",
        "mig_compute_profiles": "No MIG profiles supported\n",
        "nvlink": "GPU 0: no active links\n",
    }
    argv = {
        "identity": DIAG._feature_query_argv(
            smi, uuid, DIAG.FEATURE_COMMAND_FIELDS["identity"]
        ),
        "full_text": [smi, "-i", uuid, "-q"],
        "full_xml": [smi, "-i", uuid, "-q", "-x"],
        "ecc": DIAG._feature_query_argv(
            smi, uuid, DIAG.FEATURE_COMMAND_FIELDS["ecc"]
        ),
        "mig_mode": DIAG._feature_query_argv(
            smi, uuid, DIAG.FEATURE_COMMAND_FIELDS["mig_mode"]
        ),
        "sessions": DIAG._feature_query_argv(
            smi, uuid, DIAG.FEATURE_COMMAND_FIELDS["sessions"]
        ),
        "mig_gpu_profiles": [smi, "mig", "-i", uuid, "-lgip"],
        "mig_compute_profiles": [smi, "mig", "-i", uuid, "-lgci"],
        "nvlink": [smi, "nvlink", "-i", uuid, "-s"],
    }
    commands = {
        key: feature_command(argv[key], outputs[key]) for key in argv
    }
    identity = DIAG._feature_csv(
        commands["identity"], DIAG.FEATURE_COMMAND_FIELDS["identity"]
    )["fields"]
    ofa = ofa or DIAG.probe_ofa_api(
        candidates=["missing-fixture-library"],
        loader=lambda _candidate: (_ for _ in ()).throw(OSError("missing")),
    )
    return {
        "schema": DIAG.FEATURE_CENSUS_SCHEMA, "status": "completed",
        "query_only": True, "mutations_requested": False,
        "nvidia_smi": {"path": smi, "sha256": "a" * 64},
        "ofa_api_probe": ofa,
        "cards": [{
            "bdf": bdf, "uuid": uuid, "status": "completed",
            "identity": identity,
            "xml_identity": {"bdf": bdf, "uuid": uuid},
            "commands": commands,
            "features": DIAG._feature_observations(commands, ofa),
        }],
        "interpretation": {
            "capability_queries_are_not_benchmarks": True,
            "functional_media_requires_external_suite": True,
            "mig_configuration_performed": False,
            "ecc_injection_performed": False,
            "ofa_functional_test": "not-yet-supported",
        },
        "validation_errors": [],
    }


def valid_direct_nvml_record(bdf):
    success = {"code": 0, "name": "success"}
    fields = []
    for field_id, name, value_type, value in (
            (82, "memory_temperature_c", 1, 47),
            (193, "shutdown_threshold_c", 5, 98),
            (194, "slowdown_threshold_c", 5, 95),
            (195, "memory_max_threshold_c", 5, 95),
            (196, "gpu_max_threshold_c", 5, 85),
            (308, "gpu_temperature_subdegree_c", 0, 32.25)):
        fields.append({
            "field_id": field_id, "observed_field_id": field_id,
            "name": name, "scope_id": 0,
            "return": dict(success), "raw_item_return": dict(success),
            "outer_call_succeeded": True,
            "value_type": {
                "code": value_type,
                "name": DIAG.NVML_VALUE_TYPE_NAMES[value_type],
            }, "value": value,
            "timestamp_epoch_us": 1, "latency_us": 0,
        })
    return {
        "bdf": bdf, "handle_lookup_return": dict(success),
        "handle_lookup_elapsed_us": 0,
        "field_values": {"outer_return": dict(success),
                         "call_elapsed_us": 0, "fields": fields},
        "temperature_v": [
            {"sensor_type": 0, "name": "gpu", "return": dict(success),
             "temperature_c": 32, "call_elapsed_us": 0,
             "structure_version": "0x0100000c"},
            {"sensor_type": 1, "name": "gpu_max", "return": dict(success),
             "temperature_c": 34, "call_elapsed_us": 0,
             "structure_version": "0x0100000c"},
        ],
        "thermal_settings": {
            "sensor_index_requested": 15,
            "return": dict(success), "reported_count": 1,
            "maximum_count": 3, "malformed_count": False,
            "call_elapsed_us": 0,
            "sensors": [{
                "index": 0,
                "controller": {"code": 1, "name": "gpu-internal"},
                "target": {"code": 1, "name": "gpu"},
                "default_minimum_c": 0,
                "default_maximum_c": 127, "current_temperature_c": 32,
            }],
        },
        "margin_temperature": {
            "return": dict(success), "margin_temperature_c": 53,
            "call_elapsed_us": 0, "structure_version": "0x01000008",
        },
        "temperature_thresholds": [
            {"threshold_type": index, "name": name, "return": dict(success),
             "temperature_c": 90, "call_elapsed_us": 0}
            for index, name in enumerate((
                "shutdown", "slowdown", "memory_max", "gpu_max",
                "acoustic_min", "acoustic_current", "acoustic_max",
                "gps_current",
            ))
        ],
    }


def valid_fallback_record(bdf, timestamp, latency_us):
    match = DIAG.BDF_RE.fullmatch(bdf)
    if match is None:
        raise AssertionError("fixture BDF is malformed: %r" % bdf)
    nvidia_smi_bdf = "%08X:%02X:%02X.%X" % (
        int(match.group("domain"), 16), int(match.group("bus"), 16),
        int(match.group("device"), 16), int(match.group("function"), 16),
    )
    specifications = {
        "gpu_temperature_c": ("32", "temperature", "celsius", 32.0),
        "memory_temperature_c": ("N/A", "temperature", "celsius", None),
        "utilization_percent": ("0", "percentage", "percent", 0.0),
        "power_w": ("100", "power", "watt", 100.0),
    }
    record = {
        "index": 0, "bdf": nvidia_smi_bdf,
        "name": "CMP 170HX", "queries": {},
    }
    for key, (raw, value_type, unit, value) in specifications.items():
        record[key] = value
        record["queries"][key] = {
            "return_status": "success" if value is not None else "not-available",
            "raw": raw, "value": value, "value_type": value_type,
            "unit": unit, "timestamp_utc": timestamp,
            "latency_us": latency_us,
            "latency_scope": "whole-nvidia-smi-command",
            "canonical_nvml_api": False,
        }
    return record


def valid_temperature_document(bdfs, access="bar-read"):
    readout = {
        "raw": "0x00000000", "state": "invalid", "state_value": 0,
        "state_encoding": "valid-bit", "source": "hardware-logic",
        "payload_temperature_c": None, "temperature_c": None,
        "measurement_authoritative": False, "live": False,
        "warning": None, "badf": False, "all_ones": False,
        "available": True,
    }
    def context_row(specification, raw=0):
        name, offset, kind = specification
        if name in (
                "nv_therm_gpc_tsense_override",
                "nv_therm_gpc_tsense_hs_offset"):
            return {
                "name": name, "offset": "0x%08x" % offset,
                "raw": None, "badf": False, "all_ones": False,
                "suspicious_unavailable_pattern": False,
                "available": False, "read_skipped": True,
                "skip_reason":
                "selector-dependent-read-unconditionally-omitted-owner-race",
                "readout_kind": kind,
                "independent_physical_sensor": False,
                "decoded_temperature_c": None, "decoded": None,
                "interpretation": (
                    "fixed selector-dependent view was unconditionally omitted "
                    "because firmware can race its selector; no zero value is "
                    "fabricated"
                ),
            }
        return {
            "name": name, "offset": "0x%08x" % offset,
            "raw": "0x%08x" % raw, "badf": False,
            "all_ones": False, "suspicious_unavailable_pattern": False,
            "available": True, "read_skipped": False, "skip_reason": None,
            "readout_kind": kind,
            "independent_physical_sensor": False,
            "decoded_temperature_c": None,
            "decoded": DIAG._expected_thermal_context_decode(name, raw),
            "interpretation": (
                "register-backed logical endpoint counted once in the logical "
                "census; independence as a physical diode is unproven and no "
                "undocumented temperature scale is inferred"
                if kind == "logical-hardware-endpoint" else
                "fixed read-only raw/control/alias context; not counted as an "
                "independent sensor and no undocumented temperature scale inferred"
            ),
        }
    records = []
    skipped_readout = {
        "raw": None, "state_encoding": "valid-bit",
        "state": "not-read-owner-race-policy", "state_value": None,
        "source": None, "payload_temperature_c": None,
        "temperature_c": None, "measurement_authoritative": False,
        "live": False, "warning": None, "badf": False,
        "all_ones": False, "available": False, "read_skipped": True,
        "skip_reason":
        "selector-dependent-read-unconditionally-omitted-owner-race",
    }
    fallback_started = "2026-11-24T00:00:00+00:00"
    fallback_latency = 100
    for probe_index, bdf in enumerate(bdfs):
        match = DIAG.BDF_RE.fullmatch(bdf)
        fbpas = []
        for fbpa in range(24):
            fbpas.append({
                "fbpa": fbpa, "raw": None, "valid": False,
                "latch_valid": False, "temperature_context_confirmed": True,
                "aperture_responded": True, "enabled": True,
                "fbpa_live": True, "live": True,
                "cfg1": "0x00000000", "invalid_bit": None,
                "data_observed": False,
                "availability": "not-read-stateful-read-to-advance-port",
                "test_macro": "0x00000011",
                "test_macro_decoded": {
                    "badf": False, "all_ones": False,
                    "dram_reset_disabled": True,
                    "raw_temperature_sideband": 0, "temperature_c": None,
                    "dram_wrst_reset_disabled": True,
                    "read_i1500_register": False,
                    "debug_bus_wso_select": 0,
                    "debug_bus_group0_select": 0,
                    "debug_bus_group1_select": 0,
                    "debug_bus_group2_select": 0,
                    "debug_bus_reset_select": False,
                    "wso_mux_override": False,
                    "wso_mux_select": 0,
                },
                "test_macro_temperature_context_ready": True,
                "badf": False,
                "all_ones": False,
                "temperature_c": None, "instruction": "0x0000000f",
                "mode": "0x00000008",
                "shadow_instruction": "0x000000f0",
                "shadow_instruction_opcode": "0x0f",
                "shadow_instruction_is_temperature": True,
                "shadow_instruction_corroborates_temperature_empirically": True,
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
                    "shadow_instruction_role":
                    "empirical-latch-corroboration",
                },
                "shadow_data": "0x00000000", "status": "0x00000000",
                "status_bridge": {
                    "raw": "0x00000000", "available": True,
                    "badf": False, "all_ones": False,
                    "reserved_bits_zero": True,
                    "state_value": 0, "state_name": "idle", "idle": True,
                    "transaction_active_or_nonidle": False,
                    "coherence_signal": "idle-observed",
                    "temperature_gate_applied": False,
                    "gate_reason": (
                        "exact bridge state is recorded, but STATUS follows the "
                        "read-to-advance DATA access and may race; official getter "
                        "does not poll IDLE, so non-IDLE downgrades coherence but "
                        "does not alone reject the payload"
                    ),
                },
                "interpretation": (
                    "IEEE1500 DATA is a read-to-advance port and is null unless "
                    "explicitly acknowledged; TEST_MACRO is raw sideband context "
                    "with no Celsius conversion; decoded as temperature only when "
                    "enabled/responding, INSTR=0x0f, MODE=0x08, SHWIR is exactly "
                    "0xf0, and TEST_MACRO proves reset-safe non-read mode; exact "
                    "STATUS bridge state is separately classified because the "
                    "post-DATA observation may race; this probe never initiates a "
                    "fresh HBM transaction; distinct physical sensor not established"
                ),
                **{
                    key: "0x%08x" % (
                        DIAG.TEMPERATURE_FBPA_BASE +
                        fbpa * DIAG.TEMPERATURE_FBPA_STRIDE + relative
                    )
                    for key, relative in DIAG.TEMPERATURE_FBPA_REGISTERS.items()
                },
            })
        stacks = []
        for ordinal, ((name, fbp_pair), public_label) in enumerate(zip(
                DIAG.TEMPERATURE_STACKS,
                DIAG.TEMPERATURE_PUBLIC_HBMMAP_LABELS)):
            members = []
            for fbp in fbp_pair:
                members.extend((2 * fbp, 2 * fbp + 1))
            stacks.append({
                "stack": name, "fbps": list(fbp_pair), "fbpas": members,
                "firmware_site_ordinal": ordinal,
                "xray_sequential_label": name,
                "public_hbmmap_label": public_label,
                "label_authority": "presentation-only-membership-authoritative",
                "site_present": True,
                "selected_fbpa": members[0],
                "selected_fbpa_valid": False,
                "selected_temperature_c": None,
                "selected_aperture_responded": True,
                "enabled_fbpas": members,
                "responding_fbpas": members,
                "usable_fbpas": members,
                "live_fbpas": members,
                "valid_temperatures_c": [],
                "unique_temperatures_c": [],
                "all_valid_views_equal": False,
                "interpretation": (
                    "exact GA100 PMU table order; selected_fbpa is the first "
                    "enabled firmware-selected routing candidate/source; passive "
                    "measurement context remains conditional and non-atomic; other "
                    "views remain diagnostic and no physical sensor count is inferred"
                ),
            })
        population = {
            "opt_ts_dis_mask": "0x00000000",
            "status_opt_gpc": "0x000000ff",
            "source_offsets": {
                "opt_ts_dis_mask": "0x%08x" % DIAG.TEMPERATURE_OPT_TS_DIS_MASK,
                "status_opt_gpc": "0x%08x" % DIAG.TEMPERATURE_STATUS_OPT_GPC,
            },
            "mask_reads_available": True,
            "opt_ts_dis_reserved_zero": True,
            "expected_enabled_endpoints": [],
            "expected_enabled_count": 0,
            "observed_valid_endpoints": None,
            "observed_valid_count": None,
            "observed_matches_population_masks": None,
            "rule": DIAG.TEMPERATURE_POPULATION_RULE,
        }
        aggregate_readouts = [dict(
            readout, name=name, offset="0x%08x" % offset,
            state_encoding=encoding, readout_kind=kind,
        ) for name, offset, encoding, kind in DIAG.TEMPERATURE_AGGREGATES]
        aggregate_readouts[0].update({
            "raw": "0x20000000", "state": "valid", "state_value": 1,
            "payload_temperature_c": 0.0, "temperature_c": 0.0,
            "measurement_authoritative": True, "live": True,
        })
        liveness = {
            "tsense_raw": "0x20000000", "tsense_state": 1,
            "tsense_state_name": "valid", "tsense_affirmative": True,
            "num_active_fbps_raw": "0x00000008",
            "num_active_fbps_count": 8,
            "num_active_fbps_reserved_zero": True,
            "num_active_fbps_in_range": True,
            "num_active_fbps_affirmative": True,
            "two_signal_corroborated": True,
            "policy": (
                "TSENSE state must be VALID/SHADOW and NUM_ACTIVE_FBPS must "
                "have reserved bits zero with count 1..12"
            ),
        }
        record = {
            "identity": {
                "bdf": bdf,
                "domain": int(match.group("domain"), 16),
                "bus": int(match.group("bus"), 16),
                "device": int(match.group("device"), 16),
                "function": int(match.group("function"), 16),
                "vendor_id": "0x10de", "device_id": "0x20c2",
                "subsystem_vendor_id": "0x10de",
                "subsystem_device_id": "0x1530", "revision": "0xa1",
                "driver": "nvidia", "probe_index": probe_index,
            },
            "driver_temperature_census": {
                "canonical_direct_nvml": None,
                "fallback_nvidia_smi": valid_fallback_record(
                    bdf, fallback_started, fallback_latency
                ),
            },
            "hwmon": [],
            "pure": {
                "access": "bar-read", "writes_issued": 0,
                "status": "conditional", "backend": "legacy-gpcprobe",
                "error": None,
                "evidence_authority": "conditional-non-authoritative",
                "transaction_journal_available": False,
                "postflight_identity_verified": False,
                "dead_bar_sentinel_check": "passed",
                "reads_issued": 244,
                "read_count_contract": (
                    "244 unique reads with seven selector-dependent reads omitted"
                ),
                "selector_dependent_skipped_mask": "0x0000000f",
                "selector_dependent_read_policy":
                "unconditionally-omitted-owner-race",
                "selector_dependent_reads_unconditionally_omitted": True,
                "prewrite_liveness": liveness,
                "hbm_data_read_mask": "0x00000000",
                "hbm_data_ports_observed": 0,
                "aggregate_readouts": aggregate_readouts,
                "fixed_context_registers": [
                    context_row(specification)
                    for specification in DIAG.TEMPERATURE_FIXED_CONTEXT
                ],
                "gpu_sci": {
                    "mini_tsense": {
                        "name": "nv_pgc6_sci_fs_overt_temperature",
                        "offset": "0x00118bb0", "raw": "0x00000000",
                        "encoding": "signed-9-bit-in-bits-16-8-integer-celsius",
                        "payload_temperature_c": 0, "temperature_c": None,
                        "measurement_authoritative": False, "badf": False,
                        "all_ones": False,
                        "architected_valid_bit": None,
                        "architected_state_bits": None,
                        "architected_source_bit": None,
                        "availability": "payload-observed-no-architected-validity-bit",
                        "logical_endpoint_family": "gpu-sci-mini-tsense",
                        "relation_to_gpc_bjt": "separate-register-family",
                        "physical_diode_independence": "unproven",
                        "interpretation": (
                            "separate GPU_SCI MINI_TSENSE logical endpoint/"
                            "register family; physical-diode independence from "
                            "the GPC/BJT matrix is unproven, the signed payload "
                            "is preserved separately but is not promoted to an "
                            "authoritative temperature because this word has no "
                            "validity or freshness bit"
                        ),
                    },
                    "adc_context": {
                        "name": "nv_pgc6_sci_fs_overt_ts_adc",
                        "offset": "0x00118bac", "raw": "0x00000000",
                        "badf": False, "all_ones": False, "raw_adc_code": 0,
                        "override_value": 0, "override_enabled": False,
                        "temperature_scale": None,
                        "readout_kind":
                        "raw-adc-and-override-context-not-temperature",
                        "interpretation": (
                            "context for GPU_SCI MINI_TSENSE; no Celsius conversion is "
                            "asserted for the 14-bit ADC code"
                        ),
                    },
                    "register_block": [
                        context_row(specification)
                        for specification in DIAG.TEMPERATURE_GPU_SCI_CONTEXT
                    ],
                    "register_block_range": "0x00118b90..0x00118bb0",
                    "register_count": 9,
                },
                "gpc_analog_context": [{
                    "gpc": gpc, "base": "0x%08x" % (0x0010C000 + gpc * 0x100),
                    "registers": [context_row((
                        name, 0x0010C000 + gpc * 0x100 + relative, kind
                    )) for name, relative, kind in DIAG.TEMPERATURE_GPC_ANALOG_REGISTERS],
                    "independent_sensor_count": 0,
                    "interpretation":
                    DIAG.TEMPERATURE_GPC_ANALOG_INTERPRETATION,
                } for gpc in range(8)],
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
                "gpc_autopoll_controls": [{
                    "gpc": gpc,
                    "offset": "0x%08x" % (
                        DIAG.TEMPERATURE_TS_AUTO_BASE +
                        gpc * DIAG.TEMPERATURE_TS_AUTO_STRIDE
                    ),
                    "raw": "0x00000000", "auto_polling": False,
                    "auto_polling_bit": 15, "available": True,
                    "all_ones": False,
                    "sample_delay": 0, "polling_interval": 0,
                } for gpc in range(8)],
                "fbpa_readout_channels": fbpas,
                "hbm_stack_groups": stacks,
                "fbio_population": {
                    "status_opt_fbio": "0x00000000",
                    "source_offset": "0x%08x" %
                    DIAG.TEMPERATURE_STATUS_OPT_FBIO,
                    "available": True,
                    "enabled_fbpa_indices": list(range(24)),
                    "disabled_fbpa_indices": [],
                    "fbpa_enabled": [True] * 24,
                    "rule": (
                        "FBPA n is enabled only when STATUS_OPT_FBIO bit n is clear"
                    ),
                },
                "population": copy.deepcopy(population),
                "currently_selected_gpc_readout": {
                    "selector": "0x00000000",
                    "selector_available": True,
                    "aperture_available": True,
                    "selector_badf": False, "selector_all_ones": False,
                    "selector_fields_in_range": True,
                    "selector_reserved_bits_zero": True,
                    "selector_semantically_valid": True,
                    "read_increment_enabled": False,
                    "dependent_reads_skipped": True,
                    "gpc": 0, "bjt": 0, "raw_adc": None,
                    "observed_raw_adc_unbound": None,
                    "temperature": dict(skipped_readout),
                    "offset_temperature": dict(skipped_readout),
                    "warning": (
                        "selector was observed, but dependent payload ports were "
                        "unconditionally omitted because their owner can race"
                    ),
                },
                "snapshot_as_found": {
                    "selector": "0x00000000",
                    "selector_available": True,
                    "aperture_available": True,
                    "selector_badf": False, "selector_all_ones": False,
                    "selector_fields_in_range": True,
                    "selector_reserved_bits_zero": True,
                    "selector_semantically_valid": True,
                    "gpc": 0, "bjt": 0,
                    "read_increment_enabled": False,
                    "dependent_reads_skipped": True,
                    "temperature": dict(skipped_readout),
                    "offset_temperature": dict(skipped_readout),
                    "warning": (
                        "no trigger was issued and selector-dependent snapshot "
                        "payload ports were unconditionally omitted because their "
                        "owner can race"
                    ),
                },
                "hbm_transaction_policy": (
                    "DATA read-to-advance ports are skipped unless semantic mode "
                    "has an explicit HBM-DATA acknowledgement; require INSTR=0x0f, "
                    "MODE=0x08, exact SHWIR=0xf0, and reset-safe TEST_MACRO context "
                    "before decoding observed DATA; the collector never initiates "
                    "an IEEE1500 temperature transaction"
                ),
                "sentinels": {
                    "fbpa_num_active": "0x00000008",
                    "fbpa_training": "0x00000000",
                },
            },
        }
        if access == "semantic-read":
            record["global_snapshot"] = {
                "access": "semantic-read", "status": "pass",
                "writes_issued": 26, "trigger_count": 1,
                "selector_program_count": 24, "restore_write_count": 1,
                "selector_entry": "0x00000000",
                "selector_exit": "0x00000000",
                "selector_entry_semantics":
                    DIAG._temperature_selector_semantics(0),
                "selector_exit_semantics":
                    DIAG._temperature_selector_semantics(0),
                "selector_restored": True,
                "autopoll_untouched": True, "sentinels_unchanged": True,
                "autopoll_entry": ["0x00000000"] * 8,
                "autopoll_exit": ["0x00000000"] * 8,
                "sentinels_entry": ["0x00000008", "0x00000000"],
                "sentinels_exit": ["0x00000008", "0x00000000"],
                "trigger_readback": "0x00000000",
                "concurrent_selector_race": None,
                "write_offsets_observed": (
                    ["0x%08x" % DIAG.TEMPERATURE_SNAPSHOT_TRIGGER] +
                    ["0x%08x" % DIAG.TEMPERATURE_SNAPSHOT_SELECTOR] * 25
                ),
                "write_surface": {
                    "snapshot_trigger": "0x%08x" %
                    DIAG.TEMPERATURE_SNAPSHOT_TRIGGER,
                    "snapshot_selector": "0x%08x" %
                    DIAG.TEMPERATURE_SNAPSHOT_SELECTOR,
                    "gpc_autopoll": "never-written", "allowlist_only": True,
                },
                "gpc_bjt_endpoints": [{
                    "gpc": gpc, "bjt": bjt,
                    "selector": "0x%08x" % ((gpc << 16) | bjt),
                    "temperature": dict(readout),
                    "offset_temperature": dict(readout),
                    "journal_state": "complete",
                    "endpoint_kind": "architecturally-addressable-gpc-bjt",
                    "physical_population": (
                        "cross-checked-against-pre-snapshot-boot-static-"
                        "population-masks"
                    ),
                } for gpc in range(8) for bjt in range(3)],
                "snapshot_aggregate_readouts": [dict(
                    readout, name=name, offset="0x%08x" % offset,
                    journal_valid=True,
                ) for name, offset in DIAG.TEMPERATURE_SNAPSHOT_AGGREGATES],
                "valid_endpoint_count": 0,
                "population": dict(copy.deepcopy(population),
                    observed_valid_endpoints=[], observed_valid_count=0,
                    observed_matches_population_masks=True),
                "population_masks_unchanged": True,
                "population_entry": {
                    "opt_ts_dis_mask": "0x00000000",
                    "status_opt_gpc": "0x000000ff",
                },
                "population_exit": {
                    "opt_ts_dis_mask": "0x00000000",
                    "status_opt_gpc": "0x000000ff",
                },
            }
        records.append(record)
    return {
        "schema": DIAG.TEMPERATURE_SCHEMA,
        "version": DIAG.TEMPERATURE_VERSION,
        "status": "pass", "captured_utc": "2026-11-24T00:00:00+00:00",
        "access": access,
        "probe_backend": "gpcprobe",
        "probe_device": "/dev/gpcprobe",
        "bound_snapshot_acknowledged": False,
        "selector_write_acknowledged": access == "semantic-read",
        "hbm_data_read_to_advance_acknowledged": False,
        "active_workload_override": False,
        "max_utilization_percent": 5.0,
        "source_components": {
            name: {
                "filename": filename,
                "path": str(ROOT / "diagnostics" / filename),
                "sha256": DIAG.sha256_file(ROOT / "diagnostics" / filename),
                "loaded": (
                    name == "temperature_probe" or
                    name == "nvml_temperature"
                ),
                "identity_scope": (
                    "current-entry-script-file" if name == "temperature_probe"
                    else "compiled-byte-buffer" if name == "nvml_temperature"
                    else "current-unloaded-file"
                ),
            }
            for name, filename in (
                ("temperature_probe", "temperature_probe.py"),
                ("xray_therm", "xray_therm.py"),
                ("nvml_temperature", "nvml_temperature.py"),
            )
        },
        "driver_query_status": {
            "direct_nvml": "unavailable",
            "nvidia_smi_fallback": "ok",
        },
        "driver_temperature_census": {
            "contract": copy.deepcopy(DIAG.TEMPERATURE_DRIVER_CENSUS_CONTRACT),
            "direct_nvml": {
                "status": "unavailable", "library": None,
                "library_load_errors": [{
                    "candidate": "libnvidia-ml.so.1",
                    "error": "library unavailable in fixture",
                }],
            },
            "fallback_command": {
                "status": "ok", "backend": "nvidia-smi-query-gpu",
                "canonical": False, "started_utc": fallback_started,
                "latency_us": fallback_latency,
            },
        },
        "cards": records,
        "interpretation": copy.deepcopy(DIAG.TEMPERATURE_INTERPRETATION),
    }


class FakeRunner(object):
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.records = []

    def run(self, argv, **kwargs):
        record = {
            "argv": list(argv), "returncode": self.returncode,
            "stdout": self.stdout, "stderr": "", "status": "completed",
        }
        self.records.append(record)
        return record


class DiagnosticOrchestratorTests(unittest.TestCase):
    def test_ofa_probe_is_query_only_and_never_claims_device_function(self):
        class FakeSymbol(object):
            argtypes = None
            restype = None

            def __call__(self, pointer):
                ctypes_pointer = DIAG.ctypes.cast(
                    pointer, DIAG.ctypes.POINTER(DIAG.ctypes.c_uint32)
                )
                ctypes_pointer.contents.value = 0x00050000
                return 0

        class FakeLibrary(object):
            NvOFGetMaxSupportedApiVersion = FakeSymbol()

        result = DIAG.probe_ofa_api(
            candidates=["libfixture-opticalflow.so"],
            loader=lambda _candidate: FakeLibrary(),
        )
        self.assertEqual(result["status"], "api-version-observed")
        self.assertEqual(result["maximum_api_version"], 0x00050000)
        self.assertFalse(result["creates_cuda_context"])
        self.assertFalse(result["creates_ofa_session"])
        self.assertEqual(DIAG.validate_ofa_probe(result), [])
        malformed = copy.deepcopy(result)
        malformed["maximum_api_version"] = float(0x00050000)
        self.assertTrue(DIAG.validate_ofa_probe(malformed))

    def test_feature_inventory_strict_validation_and_journal_binding(self):
        card = {
            "bdf": "0000:01:00.0",
            "nvidia_smi": {
                "uuid": "GPU-fixture", "nvidia_smi_index": 0,
                "nvidia_smi_name": "NVIDIA CMP 170HX",
                "memory_total_mib": 65536,
            },
        }
        document = valid_feature_inventory(card)
        journal = list(document["cards"][0]["commands"].values())
        self.assertEqual(
            DIAG.validate_feature_inventory(document, [card], journal), []
        )
        mutations = (
            ("unknown-key", lambda value: value["cards"][0]["features"]
             ["ecc"].__setitem__("unexpected", 1)),
            ("bool-int", lambda value: value.__setitem__("query_only", 1)),
            ("target-uuid", lambda value: value["cards"][0].__setitem__(
                "uuid", "GPU-other")),
            ("command-argv", lambda value: value["cards"][0]["commands"]
             ["ecc"]["argv"].append("--invented")),
            ("duplicate-command-key", lambda value: value["cards"][0]
             ["commands"].__setitem__("invented", None)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                malformed = copy.deepcopy(document)
                mutate(malformed)
                self.assertTrue(DIAG.validate_feature_inventory(
                    malformed, [card], journal
                ))
        duplicated_journal = journal + [journal[0]]
        errors = DIAG.validate_feature_inventory(
            document, [card], duplicated_journal
        )
        self.assertTrue(any("uniquely bound" in item for item in errors))
        duplicated = copy.deepcopy(document)
        duplicated["cards"].append(copy.deepcopy(duplicated["cards"][0]))
        self.assertTrue(DIAG.validate_feature_inventory(
            duplicated, [card], journal
        ))

    def test_nonfinite_json_is_rejected_for_inputs_and_reports(self):
        self.assertIsNone(DIAG.parse_json_stdout({
            "returncode": 0, "stdout": '{"unused":NaN}',
        }))
        self.assertIsNone(DIAG.parse_json_stdout({
            "returncode": 0, "stdout": '{"duplicate":1,"duplicate":2}',
        }))
        with self.assertRaisesRegex(DIAG.DiagnosticError, "non-finite"):
            DIAG._strict_json_loads('{"unused":Infinity}')
        with self.assertRaisesRegex(DIAG.DiagnosticError, "duplicate"):
            DIAG._strict_json_loads('{"duplicate":1,"duplicate":2}')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with self.assertRaisesRegex(DIAG.DiagnosticError, "non-finite"):
                DIAG.emit_report({"unused": float("nan")}, str(output))
            self.assertFalse(output.exists())

    def test_cuda_helper_guards_every_bandwidth_denominator(self):
        source = (ROOT / "diagnostics" / "ga100_diag.cu").read_text(
            encoding="utf-8"
        )
        self.assertIn("static bool valid_timing_ms", source)
        self.assertIn("std::isfinite(milliseconds)", source)
        self.assertGreaterEqual(source.count("valid_timing_ms("), 4)
        self.assertIn("if (!all_timings_valid)", source)
        self.assertIn("cudaMemset(b, 0xc3, bytes)", source)
        self.assertIn("copy_kernel<<<blocks, threads>>>(b, a, count)", source)
        self.assertIn("cudaMemsetAsync(dst, pattern ^ 0xffU, bytes, stream)", source)
        self.assertIn("destination_can_access_source", source)
        self.assertIn("170hx-xray-p2p-v2", source)
        self.assertIn("verify_device_buffer", source)
        self.assertIn("full_buffer_verified", source)
        self.assertIn("load_device_record", source)
        self.assertIn("valid_sample_counts", source)
        self.assertIn("query_failure_counts", source)
        emitted = {
            match[0]: (
                int(match[1]), int(match[2]), match[3], match[4]
            )
            for match in re.findall(
                r'measure_dtype<[^>]+>\(\s*"([^"]+)",\s*'
                r'(\d+),\s*(\d+),\s*"([^"]+)",\s*"([^"]+)"',
                source,
            )
        }
        expected = {
            name: (spec[0], spec[2], spec[3], spec[4])
            for name, spec in DIAG.BANDWIDTH_TYPE_SPECS.items()
        }
        self.assertEqual(emitted, expected)
        self.assertIn("PackedInt4x2", source)
        self.assertIn("PackedUint4x2", source)
        self.assertIn("170hx-xray-cuda-bandwidth-v3", source)
        self.assertIn(
            "typed-load-store-memory-traffic-no-arithmetic", source
        )

    def test_cuda_copy_verifiers_are_bound_to_freshly_poisoned_destinations(self):
        source = (ROOT / "diagnostics" / "ga100_diag.cu").read_text(
            encoding="utf-8"
        )
        bandwidth = source[source.index("static int measure_dtype"):source.index(
            "static int bandwidth_command")]
        warmup = bandwidth.index("copy_kernel<<<blocks, threads>>>(b, a, count)")
        poison = bandwidth.index("cudaMemset(b, 0xc3, bytes)")
        timing = bandwidth.index("cudaEventRecord(begin)")
        timed_copy = bandwidth.index(
            "copy_kernel<<<blocks, threads>>>(b, a, count)", warmup + 1
        )
        verification = bandwidth.index(
            "verify_device_buffer(\n        b, bytes, 0x3c"
        )
        self.assertLess(warmup, poison)
        self.assertLess(poison, timing)
        self.assertLess(timing, timed_copy)
        self.assertLess(timed_copy, verification)

        p2p = source[source.index("static int measure_copy_direction"):source.index(
            "static void print_direction_json")]
        source_init = p2p.index("cudaMemset(src, pattern, maximum)")
        source_sync = p2p.index("cudaDeviceSynchronize()", source_init)
        destination_context = p2p.index("cudaSetDevice(destination)", source_sync)
        self.assertLess(source_init, source_sync)
        self.assertLess(source_sync, destination_context)
        direct_warmup = p2p.index("cudaMemcpyPeerAsync")
        direct_poison = p2p.index("cudaMemsetAsync", direct_warmup)
        direct_timing = p2p.index("cudaEventRecord(begin", direct_poison)
        direct_timed_copy = p2p.index("cudaMemcpyPeerAsync", direct_timing)
        direct_verify = p2p.index("verify_device_buffer", direct_timed_copy)
        self.assertLess(direct_warmup, direct_poison)
        self.assertLess(direct_poison, direct_timing)
        self.assertLess(direct_timing, direct_timed_copy)
        self.assertLess(direct_timed_copy, direct_verify)

        host_poison = p2p.index("std::memset(host, pattern ^ 0xffU, bytes)")
        destination_poison = p2p.index(
            "cudaMemset(dst, pattern ^ 0xffU, bytes)", host_poison
        )
        host_timing = p2p.index("auto host_begin", destination_poison)
        host_timed_d2h = p2p.index("cudaMemcpy(host, src", host_timing)
        host_timed_h2d = p2p.index("cudaMemcpy(dst, host", host_timed_d2h)
        host_verify = p2p.index("verify_device_buffer", host_timed_h2d)
        self.assertLess(host_poison, destination_poison)
        self.assertLess(destination_poison, host_timing)
        self.assertLess(host_timing, host_timed_d2h)
        self.assertLess(host_timed_d2h, host_timed_h2d)
        self.assertLess(host_timed_h2d, host_verify)

    def test_bandwidth_catalog_covers_packed_and_native_integer_widths(self):
        document = valid_bandwidth_document("0000:01:00.0", 3)
        by_name = {item["dtype"]: item for item in document["results"]}
        self.assertEqual(len(by_name), 14)
        self.assertEqual(
            set(by_name),
            {"float16", "bfloat16", "float32", "float64",
             "int4", "uint4", "int8", "uint8", "int16", "uint16",
             "int32", "uint32", "int64", "uint64"},
        )
        for name in ("int4", "uint4"):
            self.assertEqual(by_name[name]["logical_element_bits"], 4)
            self.assertEqual(by_name[name]["storage_unit_bytes"], 1)
            self.assertEqual(
                by_name[name]["logical_elements_per_storage_unit"], 2
            )
            self.assertEqual(by_name[name]["logical_elements"], 8192)
            self.assertEqual(
                by_name[name]["packing"],
                "two-nibbles-per-byte-low-then-high",
            )
        errors = DIAG.validate_bandwidth_document(
            document,
            {"bdf": "0000:01:00.0", "cuda": {"cuda_index": 3}},
            SimpleNamespace(bandwidth_bytes=4096, bandwidth_iterations=1),
        )
        self.assertEqual(errors, [])

    def test_bandwidth_validator_rejects_false_int4_geometry(self):
        document = valid_bandwidth_document("0000:01:00.0", 3)
        int4 = next(item for item in document["results"]
                    if item["dtype"] == "int4")
        int4["logical_elements_per_storage_unit"] = 1
        int4["logical_elements"] = 4096
        int4["packing"] = "native-scalar"
        errors = DIAG.validate_bandwidth_document(
            document,
            {"bdf": "0000:01:00.0", "cuda": {"cuda_index": 3}},
            SimpleNamespace(bandwidth_bytes=4096, bandwidth_iterations=1),
        )
        self.assertTrue(any("packing ratio mismatch: int4" in e for e in errors))
        self.assertTrue(any("logical element count mismatch: int4" in e
                            for e in errors))
        self.assertTrue(any("packing description mismatch: int4" in e
                            for e in errors))

    def test_bandwidth_v3_telemetry_contract_is_fail_closed(self):
        card = {"bdf": "0000:01:00.0", "cuda": {"cuda_index": 3}}
        args = SimpleNamespace(
            bandwidth_bytes=4096, bandwidth_iterations=1,
        )
        document = valid_bandwidth_document(card["bdf"], 3)
        self.assertEqual(
            DIAG.validate_bandwidth_document(document, card, args), []
        )
        malformed = copy.deepcopy(document)
        malformed["results"][0]["telemetry"]["scope"] = "timed-window"
        malformed["results"][1]["telemetry"]["temperature_sensor"] = "memory"
        malformed["results"][2]["telemetry"]["temperature_c_avg"] = None
        malformed["telemetry"]["sample_count"] -= 1
        malformed["telemetry"]["sampling_span_semantics"] = "concatenated"
        errors = DIAG.validate_bandwidth_document(malformed, card, args)
        self.assertTrue(any("scope mismatch" in item for item in errors))
        self.assertTrue(any("temperature sensor" in item for item in errors))
        self.assertTrue(any("missing despite samples" in item for item in errors))
        self.assertTrue(any("aggregate telemetry sample count" in item
                            for item in errors))
        self.assertTrue(any("sampling-span semantics" in item
                            for item in errors))

    def test_benchmark_power_evidence_is_complete_and_fail_closed(self):
        card = {
            "bdf": "0000:01:00.0",
            "nvidia_smi": {"uuid": "GPU-test", "nvidia_smi_index": 3},
        }
        args = SimpleNamespace(
            power_limit_watts=150.0, power_sample_interval_ms=100,
            dry_run=False,
        )
        query_argv = [
            "/usr/bin/nvidia-smi", "-i", "GPU-test",
            "--query-gpu=power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ]
        def power_record(argv, stdout=""):
            return {
                "argv": list(argv), "status": "completed", "returncode": 0,
                "stdout": stdout, "stderr": "",
            }
        pre_query = power_record(query_argv, "101.0, 200.0\n")
        enforce_query = power_record(query_argv, "101.0, 150.0\n")
        post_query = power_record(query_argv, "101.0, 150.0\n")
        restore_query = power_record(query_argv, "101.0, 200.0\n")
        set_command = power_record([
            "/usr/bin/nvidia-smi", "-i", "GPU-test", "--power-limit=150",
        ])
        restore_command = power_record([
            "/usr/bin/nvidia-smi", "-i", "GPU-test", "--power-limit=200",
        ])
        evidence = {
            "schema": DIAG.POWER_EVIDENCE_SCHEMA,
            "family": "p2p", "status": "pass",
            "requested_limit_watts": 150.0,
            "sampling_interval_ms_requested": 100,
            "sampling_provenance": {
                "backend": "nvidia-smi-periodic-query",
                "executable": "/usr/bin/nvidia-smi",
                "executable_sha256": "0" * 64,
                "query_fields": ["power.draw", "power.limit"],
                "query_argv_template": [
                    "/usr/bin/nvidia-smi", "-i", "<exact-uuid>",
                    "--query-gpu=power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                "control_commands_in_global_journal": True,
                "periodic_queries_in_global_journal": False,
            },
            "cards": [{
                "bdf": card["bdf"], "uuid": "GPU-test",
                "nvidia_smi_index": 3,
                "original_limit_watts": 200.0,
                "observed_limit_before_watts": 200.0,
                "observed_limit_after_enforce_watts": 150.0,
                "observed_limit_post_benchmark_watts": 150.0,
                "observed_limit_after_restore_watts": 200.0,
                "limit_enforced_during_benchmark": True,
                "restore_attempted": True, "restore_succeeded": True,
                "sample_count": 1, "sample_failures": 0,
                "power_draw_w_min": 101.0, "power_draw_w_max": 101.0,
                "power_draw_w_avg": 101.0,
                "power_draw_average_method": DIAG.POWER_AVERAGE_METHOD,
                "sampling_span_seconds": 0.0,
                "samples": [{
                    "offset_seconds": 0.0, "power_draw_watts": 101.0,
                    "observed_limit_watts": 150.0,
                    "query_duration_seconds": 0.01,
                }],
                "pre_query_command": pre_query,
                "set_command": set_command,
                "enforce_verification_query_command": enforce_query,
                "post_benchmark_query_command": post_query,
                "restore_command": restore_command,
                "restore_verification_query_command": restore_query,
                "error": None,
            }],
        }
        self.assertEqual(
            DIAG.validate_power_evidence(evidence, [card], args, "p2p"), []
        )
        malformed = copy.deepcopy(evidence)
        malformed["cards"][0]["power_draw_w_avg"] = 99.0
        malformed["cards"][0]["restore_succeeded"] = False
        errors = DIAG.validate_power_evidence(
            malformed, [card], args, "p2p"
        )
        self.assertTrue(any("aggregates" in item for item in errors))
        self.assertTrue(any("complete proof" in item for item in errors))
        parsed = DIAG.build_parser().parse_args(["bandwidth"])
        self.assertEqual(parsed.power_limit_watts, 150.0)
        self.assertEqual(parsed.power_sample_interval_ms, 100)

        summary_args = SimpleNamespace(
            mode="p2p", power_limit_watts=150.0,
            power_sample_interval_ms=100, dry_run=False,
        )
        report = {"commands": [
            pre_query, set_command, enforce_query, post_query,
            restore_command, restore_query,
        ], "p2p": {
            "status": "pass", "command": {},
            "power_evidence": evidence,
            "power_evidence_validation_errors": [],
        }}
        summary = DIAG.build_benchmark_power_summary(
            report, [card], summary_args
        )
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["executed_families"], ["p2p"])
        self.assertEqual(
            summary["executed_target_bdfs"], {"p2p": [card["bdf"]]}
        )
        self.assertEqual(
            DIAG.validate_benchmark_power_summary(
                summary, report, [card], summary_args
            ), []
        )
        report["p2p"].pop("power_evidence")
        report["p2p"].pop("power_evidence_validation_errors")
        missing = DIAG.build_benchmark_power_summary(
            report, [card], summary_args
        )
        self.assertEqual(missing["status"], "failed")
        self.assertTrue(any("lacks power evidence" in item
                            for item in missing["validation_errors"]))
        capability_only = {"gds": {
            "status": "skipped", "gdscheck_command": {}, "capability": {},
        }}
        capability_summary = DIAG.build_benchmark_power_summary(
            capability_only, [card], SimpleNamespace(
                mode="gds", power_limit_watts=150.0,
                power_sample_interval_ms=100, dry_run=False,
            )
        )
        self.assertEqual(capability_summary["scope_count"], 0)
        self.assertEqual(capability_summary["executed_families"], [])

    def test_benchmark_power_transaction_enforces_samples_and_restores(self):
        class PowerRunner(object):
            def __init__(self):
                self.dry_run = False
                self.limit = 200.0
                self.records = []

            def run(self, argv, **_kwargs):
                for token in argv:
                    if token.startswith("--power-limit="):
                        self.limit = float(token.split("=", 1)[1])
                stdout = (
                    "101.0, %.1f\n" % self.limit
                    if any("--query-gpu=power.draw,power.limit" == token
                           for token in argv) else ""
                )
                record = {
                    "argv": list(argv), "returncode": 0, "stdout": stdout,
                    "stderr": "", "status": "completed",
                }
                self.records.append(record)
                return record

        class NoopThread(object):
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def start(self):
                pass

            def join(self, timeout=None):
                del timeout

            def is_alive(self):
                return False

        card = {
            "bdf": "0000:01:00.0",
            "nvidia_smi": {"uuid": "GPU-test", "nvidia_smi_index": 3},
        }
        args = SimpleNamespace(
            power_limit_watts=150.0, power_sample_interval_ms=100,
            command_timeout=10, dry_run=False,
        )
        runner = PowerRunner()
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "nvidia-smi"
            executable.write_bytes(b"fixture")
            completed = SimpleNamespace(
                returncode=0, stdout=b"101.0, 150.0\n", stderr=b"",
            )
            with mock.patch.object(
                    DIAG, "find_executable", return_value=str(executable)), \
                    mock.patch.object(
                        DIAG.subprocess, "run", return_value=completed), \
                    mock.patch.object(DIAG.threading, "Thread", NoopThread):
                power = DIAG.BenchmarkPowerEvidence(
                    "bandwidth", [card], args, runner
                )
                self.assertTrue(power.prepare())
                evidence = power.finish()
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["cards"][0]["sample_count"], 1)
        self.assertEqual(evidence["cards"][0]["power_draw_w_min"], 101.0)
        self.assertEqual(evidence["cards"][0]["power_draw_w_max"], 101.0)
        self.assertEqual(evidence["cards"][0]["power_draw_w_avg"], 101.0)
        self.assertEqual(
            evidence["cards"][0]["observed_limit_after_restore_watts"], 200.0
        )
        self.assertEqual(runner.limit, 200.0)
        self.assertEqual(
            DIAG.validate_power_evidence(
                evidence, [card], args, "bandwidth"
            ), []
        )

    def test_copy_verification_model_rejects_noop_partial_and_mutated_copies(self):
        for expected in (0x3C, 0x5A, 0xA5):
            source = bytearray([expected] * 257)
            original_source = bytes(source)
            destination = bytearray([expected ^ 0xFF] * len(source))
            self.assertFalse(all(value == expected for value in destination))
            # A no-op leaves the independent poison intact.
            self.assertNotEqual(destination, source)
            # A partial write cannot satisfy the full-buffer scan.
            destination[:128] = source[:128]
            self.assertFalse(all(value == expected for value in destination))
            # Only the complete copy passes; any later mutation fails again.
            destination[:] = source
            self.assertTrue(all(value == expected for value in destination))
            destination[129] ^= 1
            self.assertFalse(all(value == expected for value in destination))
            self.assertEqual(bytes(source), original_source)

    def test_bdf_and_link_parsing(self):
        self.assertEqual(DIAG.normalize_bdf("00000000:41:00.0"), "0000:41:00.0")
        self.assertEqual(DIAG.parse_link_speed("16.0 GT/s PCIe"), 4)
        self.assertEqual(DIAG.parse_link_speed("32 GT/s"), 5)
        self.assertIsNone(DIAG.parse_link_speed("20 GT/s"))
        with self.assertRaises(DIAG.DiagnosticError):
            DIAG.normalize_bdf("41:00.0")

    def test_inventory_maps_only_ga100_and_exact_requested_bdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            devices = root / "bus" / "pci" / "devices"
            for bdf, device, speed, width in (
                ("0000:01:00.0", "0x20c2", "5.0 GT/s PCIe", "16"),
                ("0000:41:00.0", "0x20b5", "16.0 GT/s PCIe", "8"),
                ("0000:82:00.0", "0x2bb1", "32.0 GT/s PCIe", "16"),
            ):
                path = devices / bdf
                write(path / "vendor", "0x10de")
                write(path / "device", device)
                write(path / "subsystem_vendor", "0x10de")
                write(path / "subsystem_device", "0x1585")
                write(path / "current_link_speed", speed)
                write(path / "current_link_width", width)
                write(path / "max_link_speed", speed)
                write(path / "max_link_width", width)
            smi = (
                "0, 00000000:01:00.0, CMP 170HX, GPU-a, 65536\n"
                "1, 00000000:41:00.0, A100, GPU-b, 81920\n"
                "2, 00000000:82:00.0, RTX, GPU-c, 97887\n"
            )
            with mock.patch.object(DIAG, "find_executable", return_value="/bin/true"):
                cards = DIAG.inventory_ga100(root, FakeRunner(smi))
            self.assertEqual([item["bdf"] for item in cards],
                             ["0000:01:00.0", "0000:41:00.0"])
            self.assertEqual(
                cards[0]["nvidia_smi"]["nvidia_smi_index"], 0
            )
            self.assertIsNone(cards[0]["cuda"])
            self.assertEqual(cards[1]["current_link_gen"], 4)
            with mock.patch.object(DIAG, "find_executable", return_value="/bin/true"):
                selected = DIAG.inventory_ga100(
                    root, FakeRunner(smi), requested_bdfs=["0000:41:00.0"]
                )
            self.assertEqual(len(selected), 1)
            with mock.patch.object(DIAG, "find_executable", return_value="/bin/true"):
                with self.assertRaises(DIAG.DiagnosticError):
                    DIAG.inventory_ga100(
                        root, FakeRunner(smi), requested_bdfs=["0000:c1:00.0"]
                    )

    def test_cuda_runtime_ordinal_is_mapped_by_bdf_not_smi_index(self):
        cards = [{
            "bdf": "0000:01:00.0", "diagnostic_eligible": True,
            "nvidia_smi": {"nvidia_smi_index": 0}, "cuda": None,
        }]
        helper = json.dumps({
            "schema": "170hx-xray-cuda-inventory-v1",
            "devices": [{"record": device_record("0000:01:00.0", 3)}],
        })
        args = SimpleNamespace(dry_run=False, command_timeout=10)
        result = DIAG.run_cuda_inventory(
            cards, Path("/fake/ga100_diag"), args, FakeRunner(helper)
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(cards[0]["cuda"]["cuda_index"], 3)
        self.assertFalse(
            result["mapping_comparisons"][0]["indices_happen_to_match"]
        )
        command_runner = FakeRunner(json.dumps(
            valid_bandwidth_document("0000:01:00.0", 3)
        ))
        bandwidth = DIAG.run_bandwidth(
            cards, Path("/fake/ga100_diag"), SimpleNamespace(
                dry_run=False, command_timeout=10,
                bandwidth_bytes=4096, bandwidth_iterations=1,
            ), command_runner,
        )
        argv = command_runner.records[0]["argv"]
        self.assertEqual(argv[argv.index("--device") + 1], "3")
        self.assertEqual(bandwidth[0]["status"], "pass")
        self.assertTrue(bandwidth[0]["target_bdf_verified"])

    def test_p2p_verifies_result_bdfs(self):
        cards = [
            {"bdf": "0000:01:00.0", "diagnostic_eligible": True,
             "current_link_gen": 2, "current_link_width": 16,
             "max_link_gen": 4, "max_link_width": 16,
             "cuda": {"cuda_index": 1}},
            {"bdf": "0000:41:00.0", "diagnostic_eligible": True,
             "current_link_gen": 2, "current_link_width": 16,
             "max_link_gen": 4, "max_link_width": 16,
             "cuda": {"cuda_index": 2}},
        ]
        payload = json.dumps(valid_p2p_document(cards[0], cards[1]))
        result = DIAG.run_p2p(
            DIAG.select_top_pair(cards), Path("/fake/ga100_diag"),
            SimpleNamespace(dry_run=False, command_timeout=10,
                            p2p_min_bytes=4096, p2p_max_bytes=8192,
                            p2p_iterations=1),
            FakeRunner(payload),
        )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["target_bdfs_verified"])

    def test_top_pair_selection_is_negotiated_gen_then_width_then_bdf(self):
        cards = [
            {"bdf": "0000:82:00.0", "diagnostic_eligible": True,
             "current_link_gen": 4, "current_link_width": 8,
             "max_link_gen": 5, "max_link_width": 16,
             "cuda": {"cuda_index": 2}},
            {"bdf": "0000:41:00.0", "diagnostic_eligible": True,
             "current_link_gen": 4, "current_link_width": 16,
             "max_link_gen": 4, "max_link_width": 16,
             "cuda": {"cuda_index": 1}},
            {"bdf": "0000:01:00.0", "diagnostic_eligible": True,
             "current_link_gen": 4, "current_link_width": 16,
             "max_link_gen": 4, "max_link_width": 16,
             "cuda": {"cuda_index": 0}},
            {"bdf": "0000:c1:00.0", "diagnostic_eligible": True,
             "current_link_gen": 3, "current_link_width": 16,
             "max_link_gen": 5, "max_link_width": 16,
             "cuda": {"cuda_index": 3}},
        ]
        result = DIAG.select_top_pair(cards)
        self.assertEqual(result["selected_bdfs"],
                         ["0000:01:00.0", "0000:41:00.0"])
        self.assertIn("negotiated PCIe generation", result["policy"])

    def test_runner_refuses_privilege_and_driver_binding(self):
        runner = DIAG.Runner(dry_run=True)
        with self.assertRaises(DIAG.DiagnosticError):
            runner.run(["sudo", "true"])
        with self.assertRaises(DIAG.DiagnosticError):
            runner.run(["echo", "0000:01:00.0", "/sys/bus/pci/drivers/nvidia/bind"])
        record = runner.run(["true"])
        self.assertEqual(record["status"], "planned")
        self.assertIn("unsandboxed executable", record["trust_boundary"])

    def test_safe_media_tar_requires_hash_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.tar.gz"
            payload = b"print('ok')\n"
            with tarfile.open(str(good), "w:gz") as archive:
                info = tarfile.TarInfo("suite/nvdec_probe.py")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            digest = hashlib.sha256(good.read_bytes()).hexdigest()
            destination = root / "out"
            destination.mkdir()
            result = DIAG.safe_extract_tar(good, destination, digest)
            self.assertEqual(result["file_count"], 1)
            self.assertTrue((destination / "suite" / "nvdec_probe.py").is_file())
            with self.assertRaises(DIAG.DiagnosticError):
                DIAG.safe_extract_tar(good, destination, "0" * 64)
            bad = root / "bad.tar"
            with tarfile.open(str(bad), "w") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            with self.assertRaises(DIAG.DiagnosticError):
                DIAG.safe_extract_tar(
                    bad, destination, hashlib.sha256(bad.read_bytes()).hexdigest()
                )
            self.assertFalse((root / "escape").exists())

    def test_mount_selection_uses_longest_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "mnt" / "fast" / "scratch"
            scratch.mkdir(parents=True)
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                "1 0 8:1 / %s rw - ext4 /dev/sda1 rw\n"
                "2 1 259:1 / %s rw - xfs /dev/nvme0n1p1 rw\n"
                % (root / "mnt", root / "mnt" / "fast"),
                encoding="utf-8",
            )
            result = DIAG.mount_for_path(scratch, str(mountinfo))
            self.assertEqual(result["source"], "/dev/nvme0n1p1")

    def test_gds_metrics_and_power_of_two_validation(self):
        read_line = (
            "IoType: READ XferType: GPUD Threads: 1 "
            "Throughput: 12.5 GiB/sec, Avg_Latency: 87.25 usecs"
        )
        metrics = DIAG.parse_gds_metrics(read_line, expected_io_type="READ")
        self.assertTrue(DIAG.validate_gds_metrics(metrics))
        self.assertEqual(metrics["throughput_gib_s"], 12.5)
        self.assertEqual(metrics["latency_us"], 87.25)
        decimal = DIAG.parse_gds_metrics(
            read_line.replace("12.5 GiB/sec", "1 GB/sec"),
            expected_io_type="READ",
        )
        self.assertAlmostEqual(
            decimal["throughput_gib_s"], (1000.0 ** 3) / (1024.0 ** 3)
        )
        args = DIAG.build_parser().parse_args(["inventory", "--gds-size", "3M"])
        with self.assertRaises(DIAG.DiagnosticError):
            DIAG.validate_args(args)

    def test_gds_zero_latency_is_unmeasurable_but_verified_transfer_passes(self):
        write_output = (
            "IoType: WRITE XferType: GPUD Threads: 1 Throughput: 0.5 GiB/sec, "
            "Avg_Latency: 0.000000 usecs ops: 1\n"
            "Verifying data \n"
            "IoType: READ XferType: GPUD Threads: 1 Throughput: 0.4 GiB/sec, "
            "Avg_Latency: 0.000000 usecs ops: 1\n"
        )
        metrics = DIAG.parse_gds_metrics(
            write_output, expected_io_type="WRITE", require_verification=True,
        )
        self.assertTrue(metrics["transfer_valid"])
        self.assertTrue(metrics["throughput_valid"])
        self.assertFalse(metrics["latency_valid"])
        self.assertEqual(metrics["latency_status"], "unmeasurable_zero")
        self.assertTrue(metrics["verification_marker_present"])
        self.assertTrue(metrics["verification_evidence_valid"])

    def test_gds_parser_rejects_false_verification_cpu_and_bad_numbers(self):
        template = (
            "IoType: WRITE XferType: {write_path} Threads: 1 "
            "Throughput: {throughput} GiB/sec, Avg_Latency: {latency} usecs\n"
            "{marker}\n"
            "IoType: READ XferType: {read_path} Threads: 1 "
            "Throughput: 1.0 GiB/sec, Avg_Latency: 0.0 usecs\n"
        )
        cases = (
            {"write_path": "GPUD", "read_path": "GPUD", "throughput": "1.0",
             "latency": "0", "marker": "not really Verifying data"},
            {"write_path": "GPUD", "read_path": "CPU_GPU", "throughput": "1.0",
             "latency": "0", "marker": "Verifying data"},
            {"write_path": "CPU_GPU", "read_path": "GPUD", "throughput": "1.0",
             "latency": "0", "marker": "Verifying data"},
            {"write_path": "GPUD", "read_path": "GPUD", "throughput": "NaN",
             "latency": "0", "marker": "Verifying data"},
            {"write_path": "GPUD", "read_path": "GPUD", "throughput": "1.0",
             "latency": "-1", "marker": "Verifying data"},
        )
        for values in cases:
            metrics = DIAG.parse_gds_metrics(
                template.format(**values), expected_io_type="WRITE",
                require_verification=True,
            )
            self.assertFalse(metrics["transfer_valid"], values)
            # Invalid numeric tokens are retained as strings/null, never NaN JSON.
            DIAG._strict_json_dumps(metrics)
        malformed = DIAG.parse_gds_metrics(
            "IoType: WRITE XferType: GPUD broken output\nVerifying data\n",
            expected_io_type="WRITE", require_verification=True,
        )
        self.assertFalse(malformed["transfer_valid"])
        self.assertTrue(malformed["malformed_result_lines"])

    def test_run_gds_passes_verified_single_op_with_unmeasurable_latency(self):
        write_output = (
            "IoType: WRITE XferType: GPUD Threads: 1 Throughput: 0.5 GiB/sec, "
            "Avg_Latency: 0.0 usecs ops: 1\nVerifying data\n"
            "IoType: READ XferType: GPUD Threads: 1 Throughput: 0.4 GiB/sec, "
            "Avg_Latency: 0.0 usecs ops: 1\n"
        )
        read_output = (
            "IoType: READ XferType: GPUD Threads: 1 Throughput: 0.4 GiB/sec, "
            "Avg_Latency: 0.0 usecs ops: 1\n"
        )

        class GdsRunner(object):
            def run(self, argv, **kwargs):
                if "-p" in argv:
                    stdout = "GDS platform check passed"
                elif argv[argv.index("-I") + 1] == "1":
                    stdout = write_output
                else:
                    stdout = read_output
                return {
                    "argv": list(argv), "status": "completed",
                    "returncode": 0, "stdout": stdout, "stderr": "",
                }

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            card = {
                "bdf": "0000:41:00.0", "diagnostic_eligible": True,
                "current_link_gen": 4, "current_link_width": 16,
                "max_link_gen": 4, "max_link_width": 16,
                "pci_path": "/sys/devices/pci0000:40/0000:41:00.0",
                "numa_node": 1, "cuda": {"cuda_index": 1},
                "nvidia_smi": {"nvidia_smi_index": 1},
            }
            nvme = {
                "controller": "nvme0", "current_link_gen": 5,
                "current_link_width": 4, "max_link_gen": 5,
                "max_link_width": 4,
                "pci_path": "/sys/devices/pci0000:40/0000:42:00.0",
                "numa_node": 1,
            }
            mapping = {"scratch": str(scratch), "nvme": nvme}
            args = SimpleNamespace(
                gds_scratch=[str(scratch)], dry_run=False, command_timeout=10,
                gds_size=[4096], gds_io_size=4096,
            )
            owned_roots = []
            with mock.patch.object(DIAG, "inventory_nvme", return_value=[nvme]), \
                    mock.patch.object(
                        DIAG, "nvme_controller_for_scratch", return_value=mapping
                    ), mock.patch.object(
                        DIAG, "find_gds_tool", return_value="/bin/true"
                    ), mock.patch.object(
                        DIAG, "read_text", return_value="nvidia_fs 1 0 - Live 0x0"
                    ):
                result = DIAG.run_gds(
                    [card], args, GdsRunner(), "/sys", owned_roots,
                )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(
                result["results"][0]["write_metrics"]["latency_status"],
                "unmeasurable_zero",
            )
            self.assertFalse(
                result["results"][0]["write_metrics"]["latency_valid"]
            )
            self.assertTrue(result["owned_temporary_directory_removed"])
            self.assertTrue(owned_roots)
            self.assertFalse(owned_roots[0].exists())

    def test_joint_gds_selection_uses_topology_only_after_link_rank(self):
        common = {
            "diagnostic_eligible": True,
            "current_link_gen": 4, "current_link_width": 16,
            "max_link_gen": 4, "max_link_width": 16,
        }
        gpu_01 = dict(common, **{
            "bdf": "0000:01:00.0", "cuda": {"cuda_index": 0},
            "pci_path": "/sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0",
            "numa_node": 0,
        })
        gpu_41 = dict(common, **{
            "bdf": "0000:41:00.0", "cuda": {"cuda_index": 1},
            "pci_path": "/sys/devices/pci0000:40/0000:40:01.0/0000:41:00.0",
            "numa_node": 1,
        })
        gpu_slow = dict(common, **{
            "bdf": "0000:42:00.0", "cuda": {"cuda_index": 2},
            "current_link_gen": 3,
            "pci_path": "/sys/devices/pci0000:40/0000:40:02.0/0000:42:00.0",
            "numa_node": 1,
        })
        fast_nvme = {
            "scratch": "/mnt/fast", "nvme": {
                "controller": "nvme0", "current_link_gen": 5,
                "current_link_width": 4, "max_link_gen": 5,
                "max_link_width": 4,
                "pci_path": "/sys/devices/pci0000:40/0000:40:03.0/0000:43:00.0",
                "numa_node": 1,
            },
        }
        slow_nvme = {
            "scratch": "/mnt/slow", "nvme": {
                "controller": "nvme1", "current_link_gen": 4,
                "current_link_width": 4, "max_link_gen": 4,
                "max_link_width": 4,
                "pci_path": "/sys/devices/pci0000:00/0000:00:04.0/0000:04:00.0",
                "numa_node": 0,
            },
        }
        selection = DIAG.select_gds_pair(
            [gpu_01, gpu_41, gpu_slow], [slow_nvme, fast_nvme]
        )
        selected = selection["selected"]
        self.assertEqual(selected["gpu"]["bdf"], "0000:41:00.0")
        self.assertEqual(selected["scratch"]["scratch"], "/mnt/fast")
        self.assertTrue(selected["topology"]["same_pci_root"])
        self.assertTrue(selected["topology"]["same_numa_node"])
        self.assertIn("shared PCI root", selection["policy"])

    def test_partial_benchmark_documents_fail_closed(self):
        card = {
            "bdf": "0000:01:00.0", "diagnostic_eligible": True,
            "cuda": device_record("0000:01:00.0", 3),
        }
        args = SimpleNamespace(
            dry_run=False, command_timeout=10,
            bandwidth_bytes=4096, bandwidth_iterations=1,
        )
        result = DIAG.run_bandwidth(
            [card], Path("/fake/ga100_diag"), args,
            FakeRunner(json.dumps({
                "device": {"pci_bdf": "0000:01:00.0"},
            })),
        )
        self.assertEqual(result[0]["status"], "failed")
        self.assertTrue(result[0]["validation_errors"])

        second = {
            "bdf": "0000:41:00.0", "diagnostic_eligible": True,
            "current_link_gen": 4, "current_link_width": 16,
            "max_link_gen": 4, "max_link_width": 16,
            "cuda": device_record("0000:41:00.0", 4),
        }
        card.update({
            "current_link_gen": 4, "current_link_width": 16,
            "max_link_gen": 4, "max_link_width": 16,
        })
        p2p = DIAG.run_p2p(
            DIAG.select_top_pair([card, second]), Path("/fake/ga100_diag"),
            SimpleNamespace(
                dry_run=False, command_timeout=10,
                p2p_min_bytes=4096, p2p_max_bytes=8192,
                p2p_iterations=1,
            ),
            FakeRunner(json.dumps({
                "source": {"pci_bdf": card["bdf"]},
                "destination": {"pci_bdf": second["bdf"]},
            })),
        )
        self.assertEqual(p2p["status"], "failed")
        self.assertTrue(p2p["validation_errors"])

    def test_observation_only_identity_is_never_benchmarked(self):
        runner = FakeRunner(json.dumps({}))
        card = {
            "bdf": "0000:01:00.0", "device": "0x2080",
            "diagnostic_eligible": False, "observation_only_identity": True,
            "cuda": device_record("0000:01:00.0", 0),
        }
        result = DIAG.run_bandwidth(
            [card], Path("/fake/ga100_diag"), SimpleNamespace(
                dry_run=False, command_timeout=10,
                bandwidth_bytes=4096, bandwidth_iterations=1,
            ), runner,
        )
        self.assertEqual(result[0]["status"], "skipped_observation_only_identity")
        self.assertEqual(runner.records, [])
        self.assertFalse(DIAG.select_top_pair([card])["pair_available"])

    def test_temperature_mode_gates_semantic_writes_and_unknown_workload(self):
        args = DIAG.build_parser().parse_args([
            "temperature", "--temperature-access", "semantic-read",
        ])
        with self.assertRaisesRegex(
                DIAG.DiagnosticError, "acknowledge-selector-write"):
            DIAG.validate_args(args)
        args = DIAG.build_parser().parse_args([
            "temperature", "--temperature-access", "semantic-read",
            "--temperature-acknowledge-selector-write",
            "--temperature-no-driver-queries",
        ])
        with self.assertRaisesRegex(
                DIAG.DiagnosticError, "allow-active-workload"):
            DIAG.validate_args(args)
        args = DIAG.build_parser().parse_args([
            "temperature", "--temperature-access", "semantic-read",
            "--temperature-acknowledge-selector-write",
            "--temperature-no-driver-queries",
            "--temperature-allow-active-workload",
        ])
        DIAG.validate_args(args)
        bad = DIAG.build_parser().parse_args([
            "temperature", "--temperature-max-utilization", "nan",
        ])
        with self.assertRaisesRegex(DIAG.DiagnosticError, "finite"):
            DIAG.validate_args(bad)
        irrelevant_ack = DIAG.build_parser().parse_args([
            "temperature", "--temperature-acknowledge-selector-write",
        ])
        with self.assertRaisesRegex(
                DIAG.DiagnosticError, "requires semantic-read"):
            DIAG.validate_args(irrelevant_ack)

    def test_temperature_runner_targets_exact_bdf_set(self):
        cards = [
            {"bdf": "0000:01:00.0"}, {"bdf": "0000:41:00.0"},
        ]
        document = valid_temperature_document(
            [item["bdf"] for item in cards], "bar-read"
        )
        args = SimpleNamespace(
            python=None, temperature_probe_device="/dev/gpcprobe",
            temperature_probe_backend="gpcprobe",
            sysfs_root="/sys", temperature_access="bar-read",
            temperature_max_utilization=5.0,
            temperature_acknowledge_selector_write=False,
            temperature_acknowledge_bound_snapshot=False,
            temperature_allow_active_workload=False,
            temperature_no_driver_queries=False,
            command_timeout=10, dry_run=False,
        )
        runner = FakeRunner(json.dumps(document))
        with mock.patch.object(DIAG, "find_executable", return_value="/bin/true"):
            result = DIAG.run_temperature(cards, args, runner)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["target_bdfs_verified"])
        self.assertEqual(
            set(result["temperature_components"]),
            {"temperature_probe", "xray_therm", "nvml_temperature"},
        )
        self.assertTrue(all(
            item["sha256"] for item in
            result["temperature_components"].values()
        ))
        argv = runner.records[0]["argv"]
        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--bdf"],
            ["0000:01:00.0", "0000:41:00.0"],
        )

    def test_temperature_validator_rejects_bit17_hbm_and_bdf_mismatches(self):
        cards = [{"bdf": "0000:01:00.0"}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([cards[0]["bdf"]])
        control = document["cards"][0]["pure"]["gpc_autopoll_controls"][0]
        control["raw"] = "0x00020000"
        control["auto_polling"] = True
        hbm = document["cards"][0]["pure"]["fbpa_readout_channels"][0]
        hbm["mode"] = "0x00000000"
        hbm["temperature_context_confirmed"] = True
        hbm["temperature_c"] = 42
        offline = document["cards"][0]["pure"]["fbpa_readout_channels"][1]
        offline["cfg1"] = "0xbadf0000"
        offline["live"] = False
        offline["fbpa_live"] = False
        document["cards"][0]["identity"]["bdf"] = "0000:41:00.0"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("bit 15" in item for item in errors))
        self.assertTrue(any("context gate" in item for item in errors))
        self.assertTrue(any("temperature decode" in item for item in errors))
        self.assertTrue(any("BDF set" in item for item in errors))

    def test_temperature_validator_pins_v8_bdf_literals_and_exact_shapes(self):
        bdf = "0000:c1:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        baseline = valid_temperature_document([bdf])
        fallback = baseline["cards"][0]["driver_temperature_census"][
            "fallback_nvidia_smi"
        ]
        self.assertEqual(fallback["bdf"], "00000000:C1:00.0")
        self.assertEqual(
            DIAG.validate_temperature_document(baseline, cards, args), []
        )

        mutations = (
            ("wrong fallback identity", lambda doc: doc["cards"][0][
                "driver_temperature_census"]["fallback_nvidia_smi"].__setitem__(
                    "bdf", "00000000:C2:00.0"), "fallback BDF mismatch"),
            ("non-NVIDIA BDF spelling", lambda doc: doc["cards"][0][
                "driver_temperature_census"]["fallback_nvidia_smi"].__setitem__(
                    "bdf", "0000:c1:00.0"), "fallback BDF mismatch"),
            ("fallback extra field", lambda doc: doc["cards"][0][
                "driver_temperature_census"]["fallback_nvidia_smi"].__setitem__(
                    "unexpected", True), "fallback record object keys mismatch"),
            ("analogue literal", lambda doc: doc["cards"][0]["pure"][
                "gpc_analog_context"][0].__setitem__(
                    "interpretation", "close but not the schema literal"),
             "GPC analogue[0] interpretation mismatch"),
            ("analogue extra field", lambda doc: doc["cards"][0]["pure"][
                "gpc_analog_context"][0].__setitem__("unexpected", True),
             "GPC analogue[0] object keys mismatch"),
            ("population literal", lambda doc: doc["cards"][0]["pure"][
                "population"].__setitem__("rule", "different rule"),
             "pure population rule mismatch"),
            ("population extra field", lambda doc: doc["cards"][0]["pure"][
                "population"].__setitem__("unexpected", True),
             "pure population object keys mismatch"),
        )
        for description, mutate, expected in mutations:
            with self.subTest(description=description):
                document = copy.deepcopy(baseline)
                mutate(document)
                errors = DIAG.validate_temperature_document(
                    document, cards, args
                )
                self.assertTrue(
                    any(expected in item for item in errors), errors
                )

    def test_temperature_validator_rejects_restore_and_write_surface_faults(self):
        cards = [{"bdf": "0000:01:00.0"}]
        args = SimpleNamespace(temperature_access="semantic-read")
        document = valid_temperature_document(
            [cards[0]["bdf"]], "semantic-read"
        )
        snapshot = document["cards"][0]["global_snapshot"]
        snapshot["selector_restored"] = False
        snapshot["write_offsets_observed"][3] = "0x0010c0f4"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("not restored" in item for item in errors))
        self.assertTrue(any("write surface" in item for item in errors))

    def test_temperature_validator_rejects_legacy_semantic_backend(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="gpcprobe",
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_selector_write=True,
            temperature_allow_active_workload=False,
            temperature_max_utilization=5.0,
            temperature_no_driver_queries=False,
        )
        document = valid_temperature_document([bdf], "semantic-read")
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("requires xray-therm backend" in item
                            for item in errors))

    def test_temperature_validator_rejects_impossible_acknowledgements(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        document = valid_temperature_document([bdf])
        for name, document_key in (
                ("temperature_acknowledge_selector_write",
                 "selector_write_acknowledged"),
                ("temperature_acknowledge_bound_snapshot",
                 "bound_snapshot_acknowledged"),
                ("temperature_acknowledge_hbm_data_read_to_advance",
                 "hbm_data_read_to_advance_acknowledged")):
            args = SimpleNamespace(temperature_access="bar-read")
            setattr(args, name, True)
            mutated = copy.deepcopy(document)
            mutated[document_key] = True
            errors = DIAG.validate_temperature_document(mutated, cards, args)
            self.assertTrue(any(
                "acknowledgement" in item or "acknowledgments" in item
                for item in errors
            ), name)

        semantic_args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_selector_write=False,
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_hbm_data_read_to_advance=False,
        )
        semantic = valid_temperature_document([bdf], "semantic-read")
        semantic["probe_backend"] = "xray-therm"
        semantic["selector_write_acknowledged"] = False
        errors = DIAG.validate_temperature_document(
            semantic, cards, semantic_args
        )
        self.assertTrue(any("lacks selector-write" in item for item in errors))

    def test_temperature_validator_recomputes_sfp_and_exact_snapshot_matrix(self):
        cards = [{"bdf": "0000:01:00.0"}]
        args = SimpleNamespace(temperature_access="semantic-read")
        document = valid_temperature_document(
            [cards[0]["bdf"]], "semantic-read"
        )
        snapshot = document["cards"][0]["global_snapshot"]
        endpoint = snapshot["gpc_bjt_endpoints"][0]
        endpoint["temperature"].update({
            "state": "valid", "state_value": 1,
            "temperature_c": 42.0, "source": "software-override",
        })
        snapshot["gpc_bjt_endpoints"][0], snapshot["gpc_bjt_endpoints"][1] = (
            snapshot["gpc_bjt_endpoints"][1],
            snapshot["gpc_bjt_endpoints"][0],
        )
        snapshot["trigger_readback"] = "0x00000001"
        snapshot["concurrent_selector_race"] = {"observed": True}
        snapshot["snapshot_aggregate_readouts"].pop()
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("does not match raw" in item for item in errors))
        self.assertTrue(any("endpoint matrix" in item for item in errors))
        self.assertTrue(any("self-clear" in item for item in errors))
        self.assertTrue(any("race" in item for item in errors))
        self.assertTrue(any("aggregate count" in item for item in errors))

    def test_readout_validator_checks_metadata_before_raw_null_early_return(self):
        skipped = {
            "raw": None, "state_encoding": "three-state",
            "state": "not-read-owner-race-policy", "state_value": None,
            "source": None, "payload_temperature_c": None,
            "temperature_c": None, "measurement_authoritative": False,
            "live": False, "warning": None, "badf": False,
            "all_ones": False, "available": False,
            "read_skipped": True,
            "skip_reason":
            "selector-dependent-read-unconditionally-omitted-owner-race",
        }
        errors = DIAG._validate_temperature_readout(
            skipped, "mutation", expected_encoding="valid-bit",
            expected_name="expected", expected_offset=0x1234,
            expected_kind="mux-view",
        )
        self.assertTrue(any("state encoding" in item for item in errors))
        self.assertTrue(any("register name" in item for item in errors))
        self.assertTrue(any("register offset" in item for item in errors))
        self.assertTrue(any("readout-kind" in item for item in errors))

    def test_temperature_validator_binds_registers_hbm_and_stack_taxonomy(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        pure = document["cards"][0]["pure"]
        pure["aggregate_readouts"][0]["name"] = "gpu_max"
        pure["aggregate_readouts"][1]["offset"] = "0x00020460"
        pure["aggregate_readouts"][2]["state_encoding"] = "valid-bit"
        pure["currently_selected_gpc_readout"]["temperature"][
            "state_encoding"
        ] = "three-state"
        fbpa = pure["fbpa_readout_channels"][0]
        fbpa["cfg1"] = "0xbadf0000"
        fbpa["instruction_offset"] = "0x00000000"
        fbpa["shadow_instruction_opcode"] = "0x00"
        pure["fbpa_readout_channels"][1]["raw"] = "MUT"
        pure["hbm_stack_groups"][0]["firmware_site_ordinal"] = 5
        pure["hbm_stack_groups"][1]["public_hbmmap_label"] = "B"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("register name mismatch" in item for item in errors))
        self.assertTrue(any("register offset mismatch" in item for item in errors))
        self.assertTrue(any("register semantics" in item for item in errors))
        self.assertTrue(any("live-state decode" in item for item in errors))
        self.assertTrue(any("instruction_offset mismatch" in item for item in errors))
        self.assertTrue(any("SHWIR opcode mirror" in item for item in errors))
        self.assertTrue(any("DATA observation mask" in item for item in errors))
        self.assertTrue(any("HBM stack mapping" in item for item in errors))

    def test_temperature_validator_recomputes_direct_fbpa_mask_and_exact_gate(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        pure = document["cards"][0]["pure"]
        population = pure["fbio_population"]
        population.update({
            "status_opt_fbio": "0x00000002",
            # Deliberately encode the obsolete paired-FBPA interpretation.
            "enabled_fbpa_indices": [n for n in range(24) if n not in (2, 3)],
            "disabled_fbpa_indices": [2, 3],
            "fbpa_enabled": [n not in (2, 3) for n in range(24)],
        })
        fbpa = pure["fbpa_readout_channels"][0]
        fbpa["test_macro"] = "0x00000051"
        fbpa["status"] = "0x80000000"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("FBIO population decode" in item for item in errors))
        self.assertTrue(any("TEST_MACRO" in item for item in errors))
        self.assertTrue(any("bridge-status decode" in item for item in errors))

    def test_temperature_validator_rejects_malformed_observed_hbm_raw(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_selector_write=True,
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_hbm_data_read_to_advance=True,
        )
        document = valid_temperature_document([bdf], "semantic-read")
        document["probe_backend"] = "xray-therm"
        document["probe_device"] = "/dev/170hx_xray_therm"
        document["source_components"]["xray_therm"].update({
            "loaded": True, "identity_scope": "compiled-byte-buffer",
        })
        document["hbm_data_read_to_advance_acknowledged"] = True
        pure = document["cards"][0]["pure"]
        pure["hbm_data_read_mask"] = "0x00ffffff"
        pure["hbm_data_ports_observed"] = 24
        for item in pure["fbpa_readout_channels"]:
            item.update({
                "raw": "0x00000000", "data_observed": True,
                "invalid_bit": False, "latch_valid": True, "valid": True,
                "temperature_c": 0, "availability": "observed",
            })
        pure["fbpa_readout_channels"][0]["raw"] = "MUT"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("DATA observation mask" in item for item in errors))

    def test_temperature_validator_preserves_shadow_payload_non_authoritatively(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        readout = document["cards"][0]["pure"]["aggregate_readouts"][0]
        raw = (2 << 29) | (55 * 32 << 3)
        readout.update({
            "raw": "0x%08x" % raw,
            "state": "shadow", "state_value": 2,
            "payload_temperature_c": 55.0, "temperature_c": None,
            "measurement_authoritative": False, "live": False,
            "warning": "shadow-state-payload-preserved-but-not-authoritative",
        })
        document["cards"][0]["pure"]["prewrite_liveness"] = (
            DIAG._temperature_liveness_report(raw, 8)
        )
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        readout["payload_temperature_c"] = None
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("payload temperature" in item for item in errors))

    def test_temperature_validator_recomputes_two_signal_liveness(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        pure = document["cards"][0]["pure"]
        pure["sentinels"]["fbpa_num_active"] = "0x00000000"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("liveness evidence mismatch" in item
                            for item in errors))
        pure["prewrite_liveness"] = DIAG._temperature_liveness_report(
            0x20000000, 0
        )
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("lacks affirmative two-signal" in item
                            for item in errors))
        document = valid_temperature_document([bdf])
        population = document["cards"][0]["pure"]["population"]
        population["opt_ts_dis_mask"] = "0x01000000"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("population availability" in item or
                            "mask reads are unavailable" in item
                            for item in errors))

    def test_temperature_validator_returns_errors_for_malformed_containers(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        for description, mutate in (
                ("census contract", lambda doc: doc[
                    "driver_temperature_census"].__setitem__("contract", None)),
                ("GPU_SCI ADC row", lambda doc: doc["cards"][0]["pure"][
                    "gpu_sci"]["register_block"].__setitem__(7, None)),
                ("GPU_SCI temperature row", lambda doc: doc["cards"][0]["pure"][
                    "gpu_sci"]["register_block"].__setitem__(8, None)),
                ("GPC TS_AUTO row", lambda doc: doc["cards"][0]["pure"][
                    "gpc_analog_context"][0]["registers"].__setitem__(3, None)),
                ("HBM FBP membership", lambda doc: doc["cards"][0]["pure"][
                    "hbm_stack_groups"][0].__setitem__("fbps", None))):
            document = valid_temperature_document([bdf])
            mutate(document)
            errors = DIAG.validate_temperature_document(document, cards, args)
            self.assertTrue(errors, description)

        semantic_args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_selector_write=True,
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_hbm_data_read_to_advance=False,
        )
        for key in ("gpc_bjt_endpoints", "snapshot_aggregate_readouts"):
            document = valid_temperature_document([bdf], "semantic-read")
            document["probe_backend"] = "xray-therm"
            document["source_components"]["xray_therm"].update({
                "loaded": True, "identity_scope": "compiled-byte-buffer",
            })
            document["cards"][0]["global_snapshot"][key][0] = None
            errors = DIAG.validate_temperature_document(
                document, cards, semantic_args
            )
            self.assertTrue(errors, key)

    def test_temperature_validator_rejects_bool_integer_aliases(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        mutations = (
            lambda pure: pure["prewrite_liveness"].__setitem__(
                "tsense_state", True),
            lambda pure: pure["aggregate_readouts"][0].__setitem__(
                "state_value", True),
            lambda pure: pure["fbpa_readout_channels"][0].__setitem__(
                "fbpa", False),
            lambda pure: pure["hbm_stack_groups"][0].__setitem__(
                "firmware_site_ordinal", False),
            lambda pure: pure["hbm_stack_groups"][0]["fbps"].__setitem__(
                0, False),
            lambda pure: pure["fbpa_readout_channels"][0][
                "temperature_context_requirements"
            ].__setitem__("test_macro_available", 1),
        )
        for mutate in mutations:
            document = valid_temperature_document([bdf])
            mutate(document["cards"][0]["pure"])
            errors = DIAG.validate_temperature_document(document, cards, args)
            self.assertTrue(errors)

    def test_temperature_validator_binds_identity_to_selected_inventory(self):
        bdf = "0000:01:00.0"
        cards = [{
            "bdf": bdf, "vendor": "0x10de", "device": "0x20c2",
            "subsystem_vendor": "0x10de", "subsystem_device": "0x1530",
            "driver": "nvidia",
        }]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        document["cards"][0]["identity"]["subsystem_device_id"] = "0x0000"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("does not match selected card" in item
                            for item in errors))

    def test_temperature_validator_rejects_v5_context_conflicts(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        pure = document["cards"][0]["pure"]
        pure["fixed_context_registers"][0]["decoded"][
            "temperature_code"
        ] = 1
        pure["gpu_sci"]["mini_tsense"]["valid"] = True
        pure["gpu_sci"]["register_block"][8]["raw"] = "0x00000100"
        pure["gpc_analog_context"][0]["registers"][3]["raw"] = "0x00008000"
        pure["logical_hardware_backed_endpoint_census"][
            "maximum_logical_endpoints"
        ] = 55
        fbpa = pure["fbpa_readout_channels"][0]
        fbpa["test_macro"] = "0x00000002"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("decoded field mirror" in item for item in errors))
        self.assertTrue(any("invented semantics" in item for item in errors))
        self.assertTrue(any("temperature duplicate" in item for item in errors))
        self.assertTrue(any("TS_AUTO duplicate" in item for item in errors))
        self.assertTrue(any("logical endpoint census" in item for item in errors))
        self.assertTrue(any("TEST_MACRO decode" in item for item in errors))

    def test_temperature_validator_rejects_all_ones_and_fabricated_hbm_data(self):
        bdf = "0000:01:00.0"
        document = valid_temperature_document([bdf])
        pure = document["cards"][0]["pure"]
        pure["aggregate_readouts"][0]["raw"] = "0xffffffff"
        pure["gpu_sci"]["mini_tsense"]["raw"] = "0xffffffff"
        pure["gpc_autopoll_controls"][0]["raw"] = "0xffffffff"
        fbpa = pure["fbpa_readout_channels"][0]
        fbpa.update({
            "cfg1": "0xffffffff", "raw": "0x2a000000",
            "data_observed": True, "temperature_c": 42,
        })
        pure["hbm_stack_groups"][0]["valid_temperatures_c"] = [99]
        errors = DIAG.validate_temperature_document(
            document, [{"bdf": bdf}], SimpleNamespace(
                temperature_access="bar-read"
            )
        )
        self.assertTrue(any("usable telemetry" in item for item in errors))
        self.assertTrue(any("MINI_TSENSE decode" in item for item in errors))
        self.assertTrue(any("autopoll availability" in item for item in errors))
        self.assertTrue(any("DATA observation mask" in item for item in errors))
        self.assertTrue(any("derived telemetry" in item for item in errors))

    def test_dedicated_helper_read_count_contract_is_bounded_not_constant(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_selector_write=True,
            temperature_allow_active_workload=False,
            temperature_max_utilization=5.0,
            temperature_no_driver_queries=False,
        )
        document = valid_temperature_document([bdf], "semantic-read")
        document["probe_backend"] = "xray-therm"
        document["probe_device"] = "/dev/170hx_xray_therm"
        document["source_components"]["xray_therm"].update({
            "loaded": True, "identity_scope": "compiled-byte-buffer",
        })
        identity = document["cards"][0]["identity"]
        identity.update({
            "vendor_id": "0x10de", "device_id": "0x20c2",
            "subsystem_vendor_id": "0x10de",
            "subsystem_device_id": "0x1530", "revision": "0xa1",
        })
        identity["helper_identity"] = dict(identity)
        identity["helper_identity"]["generation"] = "0x0000000000000001"
        identity["helper_identity"].pop("driver", None)
        identity["helper_identity"].pop("probe_index", None)
        pure = document["cards"][0]["pure"]
        pure.update({
            "status": "pass", "backend": "xray-therm-fixed-capture",
            "helper_status": 0, "pure_completed": True,
            "autopoll_unchanged": True, "sentinels_unchanged": True,
            "transaction_total_writes": 26, "reads_issued": 1385,
            "trigger_poll_reads": 1000,
            "read_count_contract": (
                "385 selector-safe fixed reads plus 1..1000 "
                "trigger-poll reads"
            ),
            "selector_dependent_omit_count": 7,
            "hbm_data_requested": False,
            "fresh_prewrite_liveness": DIAG._temperature_liveness_report(
                0x20000000, 8
            ),
            "fresh_prewrite_liveness_scope": (
                "after-saved-selector-validation-immediately-before-first-write"
            ),
        })
        for legacy_key in (
                "dead_bar_sentinel_check", "evidence_authority",
                "postflight_identity_verified", "transaction_journal_available"):
            pure.pop(legacy_key, None)
        pure["snapshot_as_found"].update({
            "selector_before": "0x00000000", "selector_after": "0x00000000",
            "selector_stable": True,
            "selector_stability_source":
            "userspace-recomputed-before-equals-after",
            "coherent_tuple": False,
            "stability_scope": (
                "selector-before equals selector-after only; dependent "
                "temperature and offset payload ports were unconditionally omitted"
            ),
        })
        pure["currently_selected_gpc_readout"]["coherent_tuple"] = False
        snapshot = document["cards"][0]["global_snapshot"]
        for legacy_key in (
                "population_entry", "population_exit",
                "population_masks_unchanged"):
            snapshot.pop(legacy_key)
        snapshot.update({
            "backend": "xray-therm-fixed-capture", "helper_status": 0,
            "error": None,
            "pure_completed": True, "snapshot_completed": True,
            "selector_race": False, "trigger_self_cleared": True,
            "failed_gpc": None, "failed_bjt": None,
            "driver_bound_at_entry": False,
            "bound_snapshot_acknowledged": False,
            "write_offsets_directly_logged": False,
            "write_offsets_source": (
                "reconstructed-from-fixed-kernel-write-alphabet-and-exact-counter; "
                "not a per-write hardware log"
            ),
            "reads_issued": 1385, "trigger_poll_reads": 1000,
            "read_count_contract": (
                "385 selector-safe fixed reads plus 1..1000 "
                "trigger-poll reads"
            ),
            "selector_dependent_skipped_mask": "0x0000000f",
            "selector_dependent_omit_count": 7,
            "hbm_data_requested": False,
            "hbm_data_read_mask": "0x00000000",
            "fresh_prewrite_liveness": pure["fresh_prewrite_liveness"],
            "fresh_prewrite_liveness_scope": (
                "after-saved-selector-validation-immediately-before-first-write"
            ),
            "procedure": (
                "one global trigger; enumerate 8x3 snapshot selector; mandatory "
                "kernel restoration; no GPC TS_AUTO writes"
            ),
        })
        # Other helper-journal invariants are exercised by the complete-journal
        # test.  Here the range itself must not produce a read-count error.
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertFalse(any("read count mismatch" in item for item in errors))
        pure["reads_issued"] = 1386
        snapshot["reads_issued"] = 1386
        snapshot["trigger_poll_reads"] = 1001
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("read count mismatch" in item for item in errors))

    def test_dedicated_helper_pure_only_abi5_journal_is_exact(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="bar-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_selector_write=False,
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_hbm_data_read_to_advance=False,
            temperature_no_driver_queries=False,
        )
        document = valid_temperature_document([bdf])
        document["probe_backend"] = "xray-therm"
        document["probe_device"] = "/dev/170hx_xray_therm"
        document["source_components"]["xray_therm"].update({
            "loaded": True, "identity_scope": "compiled-byte-buffer",
        })
        identity = document["cards"][0]["identity"]
        identity["helper_identity"] = {
            key: value for key, value in identity.items()
            if key not in ("driver", "probe_index", "helper_identity")
        }
        identity["helper_identity"]["generation"] = "0x0000000000000001"
        pure = document["cards"][0]["pure"]
        for key in (
                "dead_bar_sentinel_check", "evidence_authority",
                "postflight_identity_verified", "transaction_journal_available"):
            pure.pop(key)
        pure.update({
            "backend": "xray-therm-fixed-capture", "status": "pass",
            "error": None, "helper_status": 0, "reads_issued": 255,
            "pure_completed": True, "autopoll_unchanged": True,
            "sentinels_unchanged": True, "transaction_total_writes": 0,
            "trigger_poll_reads": 0,
            "read_count_contract": (
                "255 fixed reads with seven selector-dependent reads omitted"
            ),
            "hbm_data_requested": False,
            "selector_dependent_omit_count": 7,
            "driver_bound_at_entry": False,
            "fbpa_context_atomic": False,
            "fbpa_context_warning": (
                "seven fixed FBPA context fields plus optional acknowledged "
                "DATA are sequential and can race their firmware owner; "
                "context correlation is not atomic"
            ),
            "fresh_prewrite_liveness": None,
            "fresh_prewrite_liveness_scope": "not-requested-pure-capture",
        })
        pure["currently_selected_gpc_readout"]["coherent_tuple"] = False
        pure["snapshot_as_found"].update({
            "selector_before": "0x00000000",
            "selector_after": "0x00000000",
            "selector_stable": True,
            "selector_stability_source":
                "userspace-recomputed-before-equals-after",
            "coherent_tuple": False,
            "stability_scope": (
                "selector-before equals selector-after only; dependent "
                "temperature and offset payload ports were unconditionally omitted"
            ),
        })
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        pure["reads_issued"] = 254
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("read count mismatch" in item for item in errors))

    def test_temperature_validator_rechecks_fallback_hwmon_and_nvml_shape(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        card = document["cards"][0]
        card["hwmon"] = [{
            "hwmon": "hwmon0", "name": "nvidia",
            "channels": [{
                "input": "temp1_input", "label": "GPU",
                "raw_millicelsius": 42000, "temperature_c": 42.0,
            }],
        }]
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        fallback = card["driver_temperature_census"]["fallback_nvidia_smi"]
        memory = fallback["queries"]["memory_temperature_c"]
        memory.update({"return_status": "success", "value": 0.0})
        fallback["memory_temperature_c"] = 0.0
        card["hwmon"][0]["channels"][0]["temperature_c"] = 42.1
        document["driver_temperature_census"]["fallback_command"][
            "latency_us"
        ] = 101
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("return status does not match raw" in item
                            for item in errors))
        self.assertTrue(any("raw/temperature decode mismatch" in item
                            for item in errors))
        self.assertTrue(any("metadata mismatch" in item for item in errors))

        direct_document = valid_temperature_document([bdf])
        direct_document["driver_query_status"]["direct_nvml"] = "ok"
        direct_document["driver_temperature_census"]["direct_nvml"] = {
            "status": "ok", "library": "libnvidia-ml.so.1",
            "library_load_errors": [],
            "initialization_return": {"code": 0, "name": "success"},
            "shutdown_return": {"code": 0, "name": "success"},
            "structure_sizes": {
                "field_value": 40, "temperature_v1": 12,
                "thermal_settings": 64, "margin_temperature_v1": 8,
            },
            "function_availability": {
                "field_values": True, "temperature_v": True,
                "thermal_settings": True, "margin_temperature": True,
                "temperature_thresholds": True,
            },
            "interpretation": {
                "query_only_public_api": True,
                "driver_firmware_internal_side_effects": "opaque",
                "unsupported_values": "null-with-exact-return-code",
                "thermal_settings_slots_are_not_gpc_bjt_endpoints": True,
            },
        }
        direct_document["cards"][0]["driver_temperature_census"][
            "canonical_direct_nvml"
        ] = valid_direct_nvml_record(bdf)
        self.assertEqual(
            DIAG.validate_temperature_document(direct_document, cards, args), []
        )
        sensor = direct_document["cards"][0]["driver_temperature_census"][
            "canonical_direct_nvml"
        ]["thermal_settings"]["sensors"][0]
        sensor["current_temperature_c"] = -1
        self.assertEqual(
            DIAG.validate_temperature_document(direct_document, cards, args), []
        )
        sensor["current_temperature_c"] = -274
        errors = DIAG.validate_temperature_document(
            direct_document, cards, args
        )
        self.assertTrue(any("thermal sensor[0] value" in item for item in errors))
        sensor["current_temperature_c"] = 32
        direct_card = direct_document["cards"][0]["driver_temperature_census"][
            "canonical_direct_nvml"
        ]
        direct_card["field_values"]["fields"][0]["value"] = -1
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("successful value is missing/implausible" in item
                            for item in errors))
        direct_card["field_values"]["fields"][0]["value"] = 47
        direct_card["field_values"]["fields"][0]["observed_field_id"] = 308
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("identity metadata mismatch" in item
                            for item in errors))
        direct_card["field_values"]["fields"][0]["observed_field_id"] = 82
        direct_card["temperature_thresholds"][0]["temperature_c"] = -1
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("threshold[0] successful value" in item
                            for item in errors))
        direct_card["temperature_thresholds"][0]["temperature_c"] = 90
        direct_card["handle_lookup_return"]["name"] = "bogus"
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("handle lookup return metadata" in item
                            for item in errors))
        direct_card["handle_lookup_return"]["name"] = "success"
        direct_card["temperature_thresholds"][0]["return"]["name"] = "bogus"
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("threshold[0] return metadata" in item
                            for item in errors))
        direct_card["temperature_thresholds"][0]["return"]["name"] = "success"
        direct_card["temperature_thresholds"][0]["return"] = {
            "code": 31, "name": "unrecognized",
        }
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("threshold[0] return metadata" in item
                            for item in errors))
        direct_card["temperature_thresholds"][0]["return"] = {
            "code": 0, "name": "success",
        }
        sensor["controller"] = {"code": 999, "name": "unrecognized"}
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("controller name mismatch" in item
                            for item in errors))
        sensor["controller"] = {"code": 1, "name": "gpu-internal"}
        sensor["target"] = {"code": 999, "name": "unrecognized"}
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("target name mismatch" in item for item in errors))
        sensor["target"] = {"code": 1, "name": "gpu"}
        direct_document["driver_temperature_census"]["direct_nvml"].pop(
            "shutdown_return"
        )
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("shutdown return" in item for item in errors))
        direct_document["driver_temperature_census"]["direct_nvml"][
            "shutdown_return"
        ] = {"code": 0, "name": "success"}
        direct_document["driver_temperature_census"]["direct_nvml"][
            "status"
        ] = "partial"
        direct_document["driver_query_status"]["direct_nvml"] = "partial"
        errors = DIAG.validate_temperature_document(direct_document, cards, args)
        self.assertTrue(any("summary status" in item for item in errors))
        direct_document["driver_temperature_census"]["direct_nvml"][
            "status"
        ] = "ok"
        direct_document["driver_query_status"]["direct_nvml"] = "ok"
        direct_document["driver_temperature_census"]["direct_nvml"][
            "function_availability"
        ]["field_values"] = False
        errors = DIAG.validate_temperature_document(
            direct_document, cards, args
        )
        self.assertTrue(any("availability/payload mismatch" in item
                            for item in errors))

    def test_temperature_summary_shapes_are_status_discriminated(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(temperature_access="bar-read")
        document = valid_temperature_document([bdf])
        document["driver_temperature_census"]["direct_nvml"][
            "initialization_return"
        ] = {"code": 0, "name": "success"}
        document["driver_temperature_census"]["fallback_command"]["error"] = "x"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("unavailable direct NVML" in item
                            for item in errors))
        self.assertTrue(any("status-specific summary shape" in item
                            for item in errors))

        impossible = valid_temperature_document([bdf])
        impossible["driver_temperature_census"]["direct_nvml"] = {
            "status": "unavailable", "library": None,
            "library_load_errors": [],
        }
        errors = DIAG.validate_temperature_document(impossible, cards, args)
        self.assertTrue(any("load failure" in item
                            for item in errors))
        impossible["driver_temperature_census"]["direct_nvml"] = {
            "status": "unavailable", "library": "libnvidia-ml.so.1",
            "library_load_errors": [{"candidate": "x", "error": "y"}],
        }
        errors = DIAG.validate_temperature_document(impossible, cards, args)
        self.assertTrue(any("load failure" in item
                            for item in errors))

        disabled = valid_temperature_document([bdf])
        disabled["driver_query_status"] = {
            "direct_nvml": "disabled", "nvidia_smi_fallback": "disabled",
        }
        disabled["driver_temperature_census"]["direct_nvml"] = {
            "status": "disabled",
        }
        disabled["driver_temperature_census"]["fallback_command"] = {
            "status": "disabled",
        }
        disabled["cards"][0]["driver_temperature_census"] = {
            "canonical_direct_nvml": None, "fallback_nvidia_smi": None,
        }
        disabled["source_components"]["nvml_temperature"].update({
            "loaded": False, "identity_scope": "current-unloaded-file",
        })
        disabled_args = SimpleNamespace(
            temperature_access="bar-read", temperature_no_driver_queries=True,
        )
        self.assertEqual(
            DIAG.validate_temperature_document(disabled, cards, disabled_args), []
        )
        disabled["driver_temperature_census"]["direct_nvml"]["library"] = "x"
        errors = DIAG.validate_temperature_document(
            disabled, cards, disabled_args
        )
        self.assertTrue(any("disabled direct NVML summary" in item
                            for item in errors))

    def test_temperature_validator_accepts_complete_dedicated_helper_journal(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="xray-therm",
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_selector_write=True,
            temperature_allow_active_workload=False,
            temperature_max_utilization=5.0,
            temperature_no_driver_queries=False,
        )
        document = valid_temperature_document([bdf], "semantic-read")
        document["probe_backend"] = "xray-therm"
        document["probe_device"] = "/dev/170hx_xray_therm"
        document["source_components"]["xray_therm"]["loaded"] = True
        document["source_components"]["xray_therm"][
            "identity_scope"
        ] = "compiled-byte-buffer"
        identity = document["cards"][0]["identity"]
        identity.update({
            "vendor_id": "0x10de", "device_id": "0x20c2",
            "subsystem_vendor_id": "0x10de",
            "subsystem_device_id": "0x1530", "revision": "0xa1",
        })
        identity["helper_identity"] = dict(identity)
        identity["helper_identity"]["generation"] = "0x0000000000000001"
        identity["helper_identity"].pop("helper_identity", None)
        identity["helper_identity"].pop("driver", None)
        identity["helper_identity"].pop("probe_index", None)
        population = {
            "opt_ts_dis_mask": "0x00000000",
            "status_opt_gpc": "0x000000ff",
            "source_offsets": {
                "opt_ts_dis_mask": "0x%08x" % DIAG.TEMPERATURE_OPT_TS_DIS_MASK,
                "status_opt_gpc": "0x%08x" % DIAG.TEMPERATURE_STATUS_OPT_GPC,
            },
            "mask_reads_available": True,
            "opt_ts_dis_reserved_zero": True,
            "expected_enabled_endpoints": [],
            "expected_enabled_count": 0,
            "observed_valid_endpoints": None,
            "observed_valid_count": None,
            "observed_matches_population_masks": None,
            "rule": DIAG.TEMPERATURE_POPULATION_RULE,
        }
        pure = document["cards"][0]["pure"]
        pure.update({
            "status": "pass", "backend": "xray-therm-fixed-capture",
            "helper_status": 0,
            "pure_completed": True, "autopoll_unchanged": True,
            "sentinels_unchanged": True, "transaction_total_writes": 26,
            "driver_bound_at_entry": False,
            "reads_issued": 386, "trigger_poll_reads": 1,
            "read_count_contract": (
                "385 selector-safe fixed reads plus 1..1000 "
                "trigger-poll reads"
            ),
            "selector_dependent_omit_count": 7,
            "hbm_data_requested": False,
            "fresh_prewrite_liveness": DIAG._temperature_liveness_report(
                0x20000000, 8
            ),
            "fresh_prewrite_liveness_scope": (
                "after-saved-selector-validation-immediately-before-first-write"
            ),
            "population": population,
            "fbpa_context_atomic": False,
            "fbpa_context_warning": (
                "seven fixed FBPA context fields plus optional acknowledged DATA "
                "are sequential and can race their firmware owner; context "
                "correlation is not atomic"
            ),
        })
        for legacy_key in (
                "dead_bar_sentinel_check", "evidence_authority",
                "postflight_identity_verified", "transaction_journal_available"):
            pure.pop(legacy_key, None)
        pure["snapshot_as_found"].update({
            "selector_before": "0x00000000",
            "selector_after": "0x00000000", "selector_stable": True,
            "selector_stability_source":
            "userspace-recomputed-before-equals-after",
            "coherent_tuple": False,
            "stability_scope": (
                "selector-before equals selector-after only; dependent "
                "temperature and offset payload ports were unconditionally omitted"
            ),
        })
        pure["currently_selected_gpc_readout"]["coherent_tuple"] = False
        snapshot = document["cards"][0]["global_snapshot"]
        for legacy_key in (
                "population_entry", "population_exit",
                "population_masks_unchanged"):
            snapshot.pop(legacy_key)
        snapshot.update({
            "backend": "xray-therm-fixed-capture", "helper_status": 0,
            "error": None,
            "pure_completed": True, "snapshot_completed": True,
            "selector_race": False, "trigger_self_cleared": True,
            "failed_gpc": None, "failed_bjt": None,
            "driver_bound_at_entry": False,
            "bound_snapshot_acknowledged": False,
            "trigger_readback": None,
            "reads_issued": 386, "trigger_poll_reads": 1,
            "read_count_contract": (
                "385 selector-safe fixed reads plus 1..1000 "
                "trigger-poll reads"
            ),
            "selector_dependent_skipped_mask": "0x0000000f",
            "selector_dependent_omit_count": 7,
            "hbm_data_requested": False,
            "hbm_data_read_mask": "0x00000000",
            "fresh_prewrite_liveness": pure["fresh_prewrite_liveness"],
            "fresh_prewrite_liveness_scope": (
                "after-saved-selector-validation-immediately-before-first-write"
            ),
            "procedure": (
                "one global trigger; enumerate 8x3 snapshot selector; mandatory "
                "kernel restoration; no GPC TS_AUTO writes"
            ),
            "write_offsets_directly_logged": False,
            "write_offsets_source": (
                "reconstructed-from-fixed-kernel-write-alphabet-and-exact-counter; "
                "not a per-write hardware log"
            ),
            "valid_endpoint_count": 0,
            "population": dict(population,
                observed_valid_endpoints=[], observed_valid_count=0,
                observed_matches_population_masks=True),
        })
        for endpoint in snapshot["gpc_bjt_endpoints"]:
            endpoint["selector_before"] = endpoint["selector"]
            endpoint["selector_after"] = endpoint["selector"]
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        hbm_document = copy.deepcopy(document)
        hbm_args = copy.copy(args)
        hbm_args.temperature_acknowledge_hbm_data_read_to_advance = True
        hbm_document["hbm_data_read_to_advance_acknowledged"] = True
        hbm_pure = hbm_document["cards"][0]["pure"]
        hbm_snapshot = hbm_document["cards"][0]["global_snapshot"]
        for journal in (hbm_pure, hbm_snapshot):
            journal["reads_issued"] = 410
            journal["trigger_poll_reads"] = 1
            journal["read_count_contract"] = (
                "409 selector-safe fixed reads plus 1..1000 trigger-poll reads"
            )
            journal["hbm_data_requested"] = True
            journal["hbm_data_read_mask"] = "0x00ffffff"
        hbm_pure["hbm_data_ports_observed"] = 24
        for channel in hbm_pure["fbpa_readout_channels"]:
            channel.update({
                "raw": "0x00000000", "data_observed": True,
                "invalid_bit": False, "latch_valid": True, "valid": True,
                "temperature_c": 0, "availability": "observed",
            })
        for stack in hbm_pure["hbm_stack_groups"]:
            stack.update({
                "selected_fbpa_valid": True, "selected_temperature_c": 0,
                "valid_temperatures_c": [0, 0, 0, 0],
                "unique_temperatures_c": [0],
                "all_valid_views_equal": True,
            })
        self.assertEqual(
            DIAG.validate_temperature_document(hbm_document, cards, hbm_args), []
        )
        hbm_pure["fbpa_readout_channels"][0]["temperature_c"] = 0.0
        errors = DIAG.validate_temperature_document(
            hbm_document, cards, hbm_args
        )
        self.assertTrue(any("temperature decode" in item for item in errors))
        hbm_pure["fbpa_readout_channels"][0]["temperature_c"] = 0
        hbm_pure["hbm_stack_groups"][0]["selected_temperature_c"] = 0.0
        errors = DIAG.validate_temperature_document(
            hbm_document, cards, hbm_args
        )
        self.assertTrue(any("derived telemetry" in item for item in errors))
        hbm_pure["hbm_stack_groups"][0]["selected_temperature_c"] = 0
        identity["device_id"] = "0x2080"
        identity["helper_identity"]["device_id"] = "0x2080"
        self.assertEqual(
            DIAG.validate_temperature_document(document, cards, args), []
        )
        identity["device_id"] = "0x20c2"
        identity["helper_identity"]["device_id"] = "0x20c2"
        snapshot["population_entry"] = {"fabricated": True}
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("fabricates population exit" in item
                            for item in errors))
        snapshot.pop("population_entry")
        numeric_mutations = (
            (pure, "writes_issued", False, "not zero-write"),
            (pure, "hbm_data_ports_observed", False, "HBM DATA read mask"),
            (pure, "helper_status", False, "pure completion"),
            (pure, "transaction_total_writes", False, "write count"),
            (pure, "trigger_poll_reads", True, "read count"),
            (snapshot, "helper_status", False, "snapshot journal"),
            (snapshot, "writes_issued", False, "write cardinality"),
            (snapshot, "trigger_count", True, "write cardinality"),
            (snapshot, "restore_write_count", True, "write cardinality"),
            (snapshot, "trigger_poll_reads", True, "read count"),
            (snapshot, "valid_endpoint_count", False, "valid-endpoint count"),
        )
        for target, key, malformed, expected_error in numeric_mutations:
            original = target[key]
            target[key] = malformed
            errors = DIAG.validate_temperature_document(document, cards, args)
            self.assertTrue(any(expected_error in item for item in errors), key)
            target[key] = original
        pure["driver_bound_at_entry"] = 0
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("bound-entry" in item for item in errors))
        pure["driver_bound_at_entry"] = False
        snapshot["driver_bound_at_entry"] = True
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("bound-entry journal" in item for item in errors))
        snapshot["driver_bound_at_entry"] = False
        identity["vendor_id"] = "0x1234"
        identity["helper_identity"]["vendor_id"] = "0x1234"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("GA100 identity" in item for item in errors))
        identity["vendor_id"] = "0x10de"
        identity["helper_identity"]["vendor_id"] = "0x10de"
        identity["helper_identity"]["generation"] = "0x0000000000000000"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("full identity" in item or "generation" in item
                            for item in errors))
        identity["helper_identity"]["generation"] = "0x0000000000000001"
        identity["bus"] = False
        identity["helper_identity"]["bus"] = False
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("BDF components" in item or "boolean aliases" in item
                            for item in errors))
        identity["bus"] = 1
        identity["helper_identity"]["bus"] = 1
        identity["probe_index"] = False
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("probe index" in item or "boolean aliases" in item
                            for item in errors))
        identity["probe_index"] = 0
        snapshot["trigger_readback"] = "MUT"
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("trigger readback must be null" in item
                            for item in errors))
        snapshot["trigger_readback"] = None
        original_fresh = copy.deepcopy(pure["fresh_prewrite_liveness"])
        invalid_fresh = DIAG._temperature_liveness_report(0, 0)
        pure["fresh_prewrite_liveness"] = invalid_fresh
        snapshot["fresh_prewrite_liveness"] = invalid_fresh
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("fresh prewrite liveness" in item for item in errors))
        pure["fresh_prewrite_liveness"] = original_fresh
        snapshot["fresh_prewrite_liveness"] = original_fresh
        pure["fbpa_context_atomic"] = True
        pure["currently_selected_gpc_readout"]["coherent_tuple"] = True
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("atomicity claim" in item for item in errors))
        self.assertTrue(any("falsely coherent" in item for item in errors))
        pure["fbpa_context_atomic"] = False
        pure["currently_selected_gpc_readout"]["coherent_tuple"] = False
        for malformed in (
                "0xbadf0000", "0xffffffff", "0x00100000",
                "0x00080000", "0x00000003"):
            raw_selector = int(malformed, 16)
            snapshot["selector_entry"] = malformed
            snapshot["selector_exit"] = malformed
            snapshot["selector_entry_semantics"] = (
                DIAG._temperature_selector_semantics(raw_selector)
            )
            snapshot["selector_exit_semantics"] = (
                DIAG._temperature_selector_semantics(raw_selector)
            )
            errors = DIAG.validate_temperature_document(document, cards, args)
            self.assertTrue(any("selector journal is malformed" in item
                                for item in errors))
        snapshot["selector_entry"] = "0x00000000"
        snapshot["selector_exit"] = "0x00000000"
        snapshot["selector_entry_semantics"] = (
            DIAG._temperature_selector_semantics(0)
        )
        snapshot["selector_exit_semantics"] = (
            DIAG._temperature_selector_semantics(0)
        )
        snapshot["population"]["observed_matches_population_masks"] = False
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("population" in item for item in errors))

    def test_temperature_validator_rechecks_version_workload_and_components(self):
        bdf = "0000:01:00.0"
        cards = [{"bdf": bdf}]
        args = SimpleNamespace(
            temperature_access="semantic-read",
            temperature_probe_backend="gpcprobe",
            temperature_acknowledge_bound_snapshot=False,
            temperature_acknowledge_selector_write=True,
            temperature_allow_active_workload=False,
            temperature_max_utilization=5.0,
            temperature_no_driver_queries=False,
        )
        document = valid_temperature_document([bdf], "semantic-read")
        document["version"] = "0.0.0"
        document["cards"][0]["driver_temperature_census"][
            "fallback_nvidia_smi"
        ]["utilization_percent"] = 99.0
        document["source_components"]["nvml_temperature"]["sha256"] = "0" * 64
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("version mismatch" in item for item in errors))
        self.assertTrue(any("utilization exceeds" in item for item in errors))
        self.assertTrue(any("component identity" in item for item in errors))
        stable = valid_temperature_document([bdf])
        expected_components = {
            name: {"path": component["path"], "sha256": component["sha256"]}
            for name, component in stable["source_components"].items()
        }
        expected_components["temperature_probe"]["sha256"] = "0" * 64
        errors = DIAG.validate_temperature_document(
            stable, cards, SimpleNamespace(temperature_access="bar-read"),
            expected_component_identity=expected_components,
        )
        self.assertTrue(any("changed during capture" in item for item in errors))
        document["cards"][0]["driver_temperature_census"][
            "fallback_nvidia_smi"
        ]["utilization_percent"] = -1.0
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("outside [0, 100]" in item for item in errors))
        args.temperature_no_driver_queries = True
        errors = DIAG.validate_temperature_document(document, cards, args)
        self.assertTrue(any("disabled driver-query mode" in item for item in errors))

    def test_direct_nvml_validator_rejects_missing_success_payloads(self):
        bdf = "0000:01:00.0"
        record = valid_direct_nvml_record(bdf)
        self.assertEqual(DIAG._validate_direct_nvml_card(record, bdf, "card"), [])
        for code, name in (
                (28, "gpu-not-found"), (29, "invalid-state"),
                (30, "reset-type-not-supported")):
            self.assertEqual(
                DIAG._validate_nvml_return(
                    {"code": code, "name": name}, "return"), []
            )
        malformed = copy.deepcopy(record)
        malformed["field_values"]["fields"][0]["value"] = None
        malformed["field_values"]["fields"][1]["value"] = 98.5
        malformed["field_values"]["fields"][2]["value_type"]["name"] = "wrong"
        malformed["field_values"]["call_elapsed_us"] = -1
        malformed["temperature_v"][0]["temperature_c"] = None
        malformed["temperature_v"][1]["call_elapsed_us"] = True
        malformed["thermal_settings"].update({
            "reported_count": 4, "malformed_count": True,
            "sensor_index_requested": 0,
        })
        malformed["thermal_settings"]["sensors"][0]["controller"][
            "name"
        ] = "wrong"
        malformed["temperature_thresholds"][0]["name"] = "wrong"
        errors = DIAG._validate_direct_nvml_card(malformed, bdf, "card")
        self.assertTrue(any("successful value" in item for item in errors))
        self.assertTrue(any("thermal-settings count" in item for item in errors))
        self.assertTrue(any("metadata mismatch" in item for item in errors))
        self.assertTrue(any("latency" in item for item in errors))
        self.assertTrue(any("selector" in item for item in errors))
        self.assertTrue(any("controller name" in item for item in errors))

    def test_gds_missing_smi_identity_returns_before_temp_creation(self):
        card = {
            "bdf": "0000:01:00.0", "diagnostic_eligible": True,
            "current_link_gen": 4, "current_link_width": 16,
            "cuda": device_record("0000:01:00.0", 0),
            "nvidia_smi": None,
        }
        selected = {
            "scratch": "/tmp", "nvme": {
                "current_link_gen": 5, "current_link_width": 4,
                "max_link_gen": 5, "max_link_width": 4,
            },
        }
        args = SimpleNamespace(
            gds_scratch=["/tmp"], dry_run=False, command_timeout=10,
            gds_size=[4096], gds_io_size=4096,
        )
        with mock.patch.object(DIAG, "inventory_nvme", return_value=[]), \
                mock.patch.object(
                    DIAG, "nvme_controller_for_scratch", return_value=selected
                ), mock.patch.object(
                    DIAG, "select_fastest_scratch",
                    return_value={"selected": selected, "ranking": ["/tmp"]}
                ), mock.patch.object(
                    DIAG, "find_gds_tool", return_value="/bin/true"
                ), mock.patch.object(
                    DIAG, "read_text", return_value="nvidia_fs 1 0 - Live 0x0"
                ), mock.patch.object(
                    DIAG.tempfile, "mkdtemp",
                    side_effect=AssertionError("temporary directory must not be created")
                ):
            result = DIAG.run_gds([card], args, FakeRunner(), "/sys", [])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("nvidia-smi index", result["reason"])

    def test_resource_caps_and_component_failure_aggregation(self):
        args = DIAG.build_parser().parse_args([
            "bandwidth", "--bandwidth-bytes", "8G",
        ])
        with self.assertRaisesRegex(DIAG.DiagnosticError, "bandwidth-bytes"):
            DIAG.validate_args(args)
        report = {
            "bandwidth": [
                {"bdf": "0000:01:00.0", "status": "pass"},
                {"bdf": "0000:41:00.0", "status": "failed"},
            ],
            "p2p": {"status": "failed"},
            "gds": {"status": "skipped"},
            "temperature": {"status": "failed"},
        }
        self.assertEqual(
            DIAG.collect_component_failures(report),
            ["bandwidth:0000:41:00.0", "temperature", "p2p"],
        )

    def test_dry_run_inventory_emits_private_atomic_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bus" / "pci" / "devices" / "0000:01:00.0"
            write(path / "vendor", "0x10de")
            write(path / "device", "0x20c2")
            write(path / "current_link_speed", "5.0 GT/s PCIe")
            write(path / "current_link_width", "16")
            write(path / "max_link_speed", "16.0 GT/s PCIe")
            write(path / "max_link_width", "16")
            sentinel = root / "existing-work-file"
            sentinel.write_text("preserve", encoding="utf-8")
            output = root / "result.json"
            with mock.patch.object(DIAG, "find_executable", return_value=None):
                rc = DIAG.main([
                    "inventory", "--dry-run", "--sysfs-root", str(root),
                    "--work-dir", str(root),
                    "--output", str(output),
                ])
            self.assertEqual(rc, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["tool"]["version"], "2.0.0")
            self.assertEqual(report["feature_inventory"]["status"], "unavailable")
            self.assertEqual(
                report["feature_inventory"]["interpretation"]
                ["ofa_functional_test"], "not-yet-supported"
            )
            self.assertFalse(report["safety"]["bar0_access"])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertTrue(report["temporary_work_removed"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(any(
                item.name.startswith("170hx-xray-diag-") for item in root.iterdir()
            ))
            self.assertFalse(any(
                item.name.startswith(".result.json.tmp-") for item in root.iterdir()
            ))


if __name__ == "__main__":
    unittest.main()
