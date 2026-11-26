#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for the constrained GA100 temperature probe."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "diagnostics" / "temperature_probe.py"
SPEC = importlib.util.spec_from_file_location("xray_temperature_test", str(SCRIPT))
TEMP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TEMP)
THERM_SPEC = importlib.util.spec_from_file_location(
    "xray_therm_test", str(ROOT / "diagnostics" / "xray_therm.py")
)
THERM = importlib.util.module_from_spec(THERM_SPEC)
THERM_SPEC.loader.exec_module(THERM)
NVML_SPEC = importlib.util.spec_from_file_location(
    "xray_nvml_temperature_test",
    str(ROOT / "diagnostics" / "nvml_temperature.py"),
)
NVML = importlib.util.module_from_spec(NVML_SPEC)
NVML_SPEC.loader.exec_module(NVML)


def valid_sfp(celsius):
    fixed = int(round(celsius * 32.0)) & 0x3FFF
    return (1 << 30) | (fixed << 3)


class FakeProbe(object):
    def __init__(self):
        self.regs = {}
        self.writes = []
        self.write_attempts = []
        self.read_offsets = []
        self.fail_read_offset = None
        self.fail_read_once = False
        self.failed_read = False
        self.fail_restore_value = None
        self.race_once = False
        self.raced = False
        self.selector = 0x000F000F
        self.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT] = self.selector
        self.regs[TEMP.NV_THERM_TSENSE_GLOBAL_SNAPSHOT] = 0
        self.regs[TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR_SNAPSHOT] = valid_sfp(42)
        self.regs[TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR_SNAPSHOT] = valid_sfp(41)
        self.regs[TEMP.FBPA_NUM_ACTIVE] = 8
        self.regs[TEMP.FBPA_TRAINING] = 0x12345678
        for _, offset, _, _ in TEMP.AGGREGATES:
            self.regs[offset] = valid_sfp(40)
        for _, offset in TEMP.SNAPSHOT_AGGREGATES:
            self.regs[offset] = valid_sfp(39)
        self.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX] = 0
        self.regs[TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR] = valid_sfp(40)
        self.regs[TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR] = valid_sfp(39)
        self.regs[TEMP.NV_THERM_GPC_TSENSE_RAW_CODE] = 0x123
        for gpc in range(8):
            self.regs[
                TEMP.PCHIPLET_TS_AUTO_BASE + gpc * TEMP.PCHIPLET_TS_AUTO_STRIDE
            ] = 0
        for fbpa in range(TEMP.FBPA_UC_COUNT):
            base = TEMP.FBPA_UC_BASE + fbpa * TEMP.FBPA_UC_STRIDE
            self.regs[base + TEMP.FBPA_UC_CFG1] = 1
            self.regs[base + TEMP.FBPA_UC_TEST_MACRO] = 0x11
            self.regs[base + TEMP.FBPA_UC_INSTR] = 0x0F
            self.regs[base + TEMP.FBPA_UC_MODE] = 0x08
            self.regs[base + TEMP.FBPA_UC_WDR] = 42 << 24
            self.regs[base + TEMP.FBPA_UC_SHWIR] = 0x0F << 4
            self.regs[base + TEMP.FBPA_UC_SHWDR] = 0
            self.regs[base + TEMP.FBPA_UC_STATUS] = 0

    def read32(self, index, offset):
        del index
        self.read_offsets.append(offset)
        if (offset == self.fail_read_offset and not self.failed_read and
                (self.fail_read_once or self.fail_read_offset is not None)):
            self.failed_read = True
            raise OSError("injected read fault")
        if (offset == TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT and
                self.race_once and not self.raced and self.writes):
            self.raced = True
            return self.selector ^ 1
        return self.regs.get(offset, 0)

    def write32(self, index, offset, value):
        del index
        self.write_attempts.append((offset, value))
        if (offset == TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT and
                value == self.fail_restore_value):
            raise OSError("injected restore fault")
        self.writes.append((offset, value))
        if offset == TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT:
            self.selector = value
            self.regs[offset] = value
        elif offset == TEMP.NV_THERM_TSENSE_GLOBAL_SNAPSHOT:
            # Hardware self-clears the trigger.
            self.regs[offset] = 0


class TemperatureProbeTests(unittest.TestCase):
    def _valid_helper_info(self, count=1):
        info = THERM.Info()
        info.abi_version = THERM.ABI_VERSION
        info.struct_size = THERM.ctypes.sizeof(THERM.Info)
        info.max_cards = THERM.MAX_CARDS
        info.count = count
        info.capabilities = THERM.REQUIRED_CAPABILITIES
        for index in range(count):
            card = info.cards[index]
            card.identity.generation = 1
            card.identity.domain = 0
            card.identity.bus = index + 1
            card.identity.devfn = 0
            card.identity.vendor = 0x10DE
            card.identity.device = 0x20C2
            card.bar0_start = 0x100000000 + index * 0x1000000
            card.bar0_length = THERM.MAPPED_BAR0_LENGTH
            card.mapped_length = THERM.MAPPED_BAR0_LENGTH
            card.driver_bound = 1
        return info

    def _valid_helper_capture(self):
        capture = THERM.Capture()
        capture.status = 0
        capture.pure_completed = 1
        capture.snapshot_completed = 1
        capture.selector_restored = 1
        capture.autopoll_unchanged = 1
        capture.sentinels_unchanged = 1
        capture.trigger_self_cleared = 1
        capture.failed_gpc = 0xFFFFFFFF
        capture.failed_bjt = 0xFFFFFFFF
        capture.reads_issued = 386
        capture.writes_issued = 26
        capture.selector_dependent_skipped_mask = 0xF
        for index in range(8):
            capture.aggregate[index] = valid_sfp(40)
            capture.ts_auto_entry[index] = 0
            capture.ts_auto_exit[index] = 0
        capture.sentinels_entry[0] = 8
        capture.sentinels_exit[0] = 8
        capture.prewrite_tsense = valid_sfp(40)
        capture.prewrite_active_fbps = 8
        # ABI5 always omits these selector-dependent payload ports.
        capture.current_temperature = 0
        capture.current_offset_temperature = 0
        capture.snapshot_as_found_temperature = 0
        capture.snapshot_as_found_offset_temperature = 0
        for index in range(4):
            capture.snapshot_aggregate[index] = valid_sfp(42)
        for index in range(24):
            capture.fbpa[index].cfg1 = 1
            capture.fbpa[index].test_macro = 0x11
            capture.fbpa[index].instr = 0x0F
            capture.fbpa[index].mode = 0x08
            capture.fbpa[index].data = 0
            capture.fbpa[index].shwir = 0x0F << 4
            gpc, bjt = divmod(index, 3)
            selector = (gpc << 16) | bjt
            capture.gpc_bjt[index].selector = selector
            capture.gpc_bjt[index].selector_before = selector
            capture.gpc_bjt[index].selector_after = selector
            capture.gpc_bjt[index].temperature = valid_sfp(43)
            capture.gpc_bjt[index].offset_temperature = valid_sfp(42)
        return capture

    def test_dedicated_helper_layout_and_population_cross_check(self):
        THERM.assert_layout()
        self.assertEqual(THERM.IOC_INFO, 0x85A05401)
        self.assertEqual(THERM.ABI_VERSION, 5)
        self.assertEqual(THERM.IOC_CAPTURE, 0xC7005402)
        capture = self._valid_helper_capture()
        card = {"bdf": "0000:01:00.0", "probe_index": 0}
        pure = TEMP.decode_helper_pure(capture, card)
        self.assertTrue(pure["pure_completed"])
        self.assertEqual(pure["population"]["expected_enabled_count"], 24)
        snapshot = TEMP.decode_helper_snapshot(capture)
        self.assertEqual(snapshot["status"], "pass")
        self.assertTrue(snapshot["population"]["observed_matches_population_masks"])
        capture.status_opt_gpc = 1
        self.assertEqual(
            TEMP.decode_helper_snapshot(capture)["status"], "failed"
        )

    def test_dedicated_bound_snapshot_requires_explicit_ack(self):
        capture = self._valid_helper_capture()
        capture.driver_bound_at_entry = 1
        self.assertEqual(TEMP.decode_helper_snapshot(capture)["status"], "failed")
        self.assertEqual(
            TEMP.decode_helper_snapshot(
                capture, bound_snapshot_acknowledged=True
            )["status"],
            "pass",
        )

    def test_failed_helper_snapshot_retains_exact_partial_journal(self):
        capture = self._valid_helper_capture()
        capture.status = -11
        capture.snapshot_completed = 0
        capture.writes_issued = 5
        capture.failed_gpc = 0
        capture.failed_bjt = 2
        card = {"bdf": "0000:01:00.0", "probe_index": 0}
        pure = TEMP.decode_helper_pure(capture, card)
        self.assertEqual(pure["status"], "failed")
        self.assertEqual(pure["helper_status"], -11)
        snapshot = TEMP.decode_helper_snapshot(capture)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["trigger_count"], 1)
        self.assertEqual(snapshot["selector_program_count"], 3)
        self.assertEqual(snapshot["restore_write_count"], 1)
        self.assertEqual(len(snapshot["write_offsets_observed"]), 5)
        self.assertFalse(snapshot["write_offsets_directly_logged"])
        self.assertIn("exact-counter", snapshot["write_offsets_source"])
        self.assertIsNone(
            snapshot["population"]["observed_valid_endpoints"]
        )
        self.assertEqual(
            snapshot["gpc_bjt_endpoints"][2]["journal_state"],
            "partial-at-failure",
        )
        self.assertEqual(
            snapshot["gpc_bjt_endpoints"][3]["journal_state"],
            "unreached-or-unproven",
        )

    def test_incomplete_helper_pure_capture_never_decodes_zero_fill(self):
        capture = THERM.Capture()
        capture.status = -16
        card = {"bdf": "0000:01:00.0", "probe_index": 0}
        pure = TEMP.decode_helper_pure(capture, card)
        self.assertEqual(pure["status"], "failed")
        self.assertEqual(pure["journal_scope"], "minimal-no-register-decode")
        self.assertNotIn("aggregate_readouts", pure)
        self.assertNotIn("fbpa_readout_channels", pure)

    def test_helper_partial_write_reconstruction_boundaries(self):
        cases = (
            (2, 0, 1, False),
            (3, 1, 1, False),
            (26, 24, 1, False),
        )
        for writes, selectors, restores, self_cleared in cases:
            with self.subTest(writes=writes):
                capture = self._valid_helper_capture()
                capture.status = -110
                capture.snapshot_completed = 0
                capture.writes_issued = writes
                capture.trigger_self_cleared = int(self_cleared)
                capture.selector_restored = int(writes != 26)
                result = TEMP.decode_helper_snapshot(capture)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["trigger_count"], 1)
                self.assertEqual(result["selector_program_count"], selectors)
                self.assertEqual(result["restore_write_count"], restores)
                self.assertEqual(len(result["write_offsets_observed"]), writes)

    def test_abi5_refusal_and_timeout_read_contracts_are_exact(self):
        capture = self._valid_helper_capture()
        capture.flags = THERM.CAPTURE_F_SNAPSHOT
        capture.writes_issued = 0
        for reads in (255, 246, 248):
            capture.reads_issued = reads
            self.assertTrue(TEMP.helper_read_contract(capture)["valid"], reads)
        for reads in (245, 247, 249, 256):
            capture.reads_issued = reads
            self.assertFalse(TEMP.helper_read_contract(capture)["valid"], reads)
        capture.flags |= THERM.CAPTURE_F_HBM_DATA
        for reads in (279, 270, 272):
            capture.reads_issued = reads
            self.assertTrue(TEMP.helper_read_contract(capture)["valid"], reads)
        capture.writes_issued = 2
        capture.status = -110
        capture.trigger_self_cleared = 0
        capture.snapshot_completed = 0
        capture.reads_issued = 1285
        contract = TEMP.helper_read_contract(capture)
        self.assertTrue(contract["valid"])
        self.assertEqual(contract["trigger_poll_reads"], 1000)
        capture.flags = THERM.CAPTURE_F_SNAPSHOT
        capture.reads_issued = 1261
        self.assertTrue(TEMP.helper_read_contract(capture)["valid"])

    def test_nvml_outer_field_failure_cannot_fabricate_success(self):
        def fail_outer(_handle, _count, values):
            values[0].value.uiVal = 99
            values[0].timestamp = 123
            values[0].latencyUsec = 456
            return 999

        report = NVML._query_fields(object(), fail_outer, object())
        self.assertEqual(report["outer_return"]["code"], 999)
        for item in report["fields"]:
            self.assertEqual(item["observed_field_id"], item["field_id"])
            self.assertNotEqual(item["return"]["code"], 0)
            self.assertIsNone(item["value_type"])
            self.assertIsNone(item["value"])
            self.assertIsNone(item["timestamp_epoch_us"])
            self.assertIsNone(item["latency_us"])

    def test_nvml_native_abi_layout_is_locked(self):
        NVML.assert_layout()
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlValue), 8)
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlFieldValue), 40)
        self.assertEqual(NVML.NvmlFieldValue.value.offset, 32)
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlTemperature), 12)
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlThermalSensor), 20)
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlGpuThermalSettings), 64)
        self.assertEqual(NVML.ctypes.sizeof(NVML.NvmlMarginTemperature), 8)

    def test_nvml_thermal_sensor_temperatures_are_signed_int(self):
        fields = dict(NVML.NvmlThermalSensor._fields_)
        for name in ("defaultMinTemp", "defaultMaxTemp", "currentTemp"):
            self.assertIs(fields[name], NVML.ctypes.c_int)
        sensor = NVML.NvmlThermalSensor()
        sensor.defaultMinTemp = -40
        sensor.defaultMaxTemp = 125
        sensor.currentTemp = -1
        self.assertEqual(sensor.defaultMinTemp, -40)
        self.assertEqual(sensor.defaultMaxTemp, 125)
        self.assertEqual(sensor.currentTemp, -1)

    def test_nvml_current_return_names_include_release_620_codes(self):
        self.assertEqual(NVML._return(28)["name"], "gpu-not-found")
        self.assertEqual(NVML._return(29)["name"], "invalid-state")
        self.assertEqual(
            NVML._return(30)["name"], "reset-type-not-supported"
        )

    def test_sfp95_positive_negative_invalid_and_shadow(self):
        positive = TEMP.decode_sfp95(valid_sfp(42.5))
        self.assertEqual(positive["state"], "valid")
        self.assertEqual(positive["temperature_c"], 42.5)
        negative = TEMP.decode_sfp95(valid_sfp(-7.25))
        self.assertEqual(negative["temperature_c"], -7.25)
        invalid = TEMP.decode_sfp95(0)
        self.assertEqual(invalid["state"], "invalid")
        self.assertIsNone(invalid["temperature_c"])
        shadow = TEMP.decode_sfp95((2 << 29) | (int(55 * 32) << 3), "three-state")
        self.assertEqual(shadow["state"], "shadow")
        self.assertIsNone(shadow["temperature_c"])
        self.assertEqual(shadow["payload_temperature_c"], 55.0)
        self.assertFalse(shadow["measurement_authoritative"])
        self.assertFalse(shadow["live"])
        self.assertEqual(
            shadow["warning"],
            "shadow-state-payload-preserved-but-not-authoritative",
        )
        for encoding in ("valid-bit", "three-state"):
            badf = TEMP.decode_sfp95(0xBADF0000, encoding)
            self.assertTrue(badf["badf"])
            self.assertFalse(badf["available"])
            self.assertEqual(badf["state"], "unavailable-badf")
            self.assertIsNone(badf["temperature_c"])
            all_ones = TEMP.decode_sfp95(0xFFFFFFFF, encoding)
            self.assertTrue(all_ones["all_ones"])
            self.assertFalse(all_ones["available"])
            self.assertIsNone(all_ones["temperature_c"])

    def test_hbm_decode_requires_complete_live_transaction_context(self):
        raw = 57 << 24
        unconfirmed = TEMP.decode_hbm(raw, False)
        self.assertTrue(unconfirmed["latch_valid"])
        self.assertFalse(unconfirmed["valid"])
        self.assertIsNone(unconfirmed["temperature_c"])
        confirmed = TEMP.decode_hbm(raw, True)
        self.assertTrue(confirmed["valid"])
        self.assertEqual(confirmed["temperature_c"], 57)
        offline = TEMP.decode_hbm(raw, True, False)
        self.assertFalse(offline["latch_valid"])
        self.assertFalse(offline["valid"])
        self.assertIsNone(offline["temperature_c"])
        for bad in (0xBADF0000, 0x80000000 | (57 << 24)):
            decoded = TEMP.decode_hbm(bad, True)
            self.assertFalse(decoded["valid"])
            self.assertIsNone(decoded["temperature_c"])

    def test_gpu_sci_mini_tsense_has_signed_payload_but_no_validity_bit(self):
        positive = TEMP.decode_gpu_sci_mini_tsense(42 << 8)
        self.assertEqual(positive["payload_temperature_c"], 42)
        self.assertIsNone(positive["temperature_c"])
        self.assertFalse(positive["measurement_authoritative"])
        self.assertIsNone(positive["architected_valid_bit"])
        self.assertNotIn("valid", positive)
        negative = TEMP.decode_gpu_sci_mini_tsense(0x1F9 << 8)
        self.assertEqual(negative["payload_temperature_c"], -7)
        self.assertIsNone(negative["temperature_c"])
        badf = TEMP.decode_gpu_sci_mini_tsense(0xBADF0000)
        self.assertTrue(badf["badf"])
        self.assertIsNone(badf["payload_temperature_c"])
        self.assertIsNone(badf["temperature_c"])
        all_ones = TEMP.decode_gpu_sci_mini_tsense(0xFFFFFFFF)
        self.assertIsNone(all_ones["payload_temperature_c"])
        self.assertIsNone(all_ones["temperature_c"])

    def test_confirmed_context_and_test_macro_decoders_are_bounded(self):
        sensor0 = TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_0", 0x123 << 17 | 2 << 15 | 0x456
        )
        self.assertEqual(sensor0, {
            "temperature_code": 0x456, "select": 2,
            "select_name": "secure-override", "override_raw": 0x123,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_1", (1 << 31) | (0x54321 << 2) | 3), {
                "select_sw_a": True, "select_sw_b": True,
                "ts_stable_count": 0x54321, "reset": True,
            })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_2", (0x456 << 16) | 0x123), {
                "sw_a": 0x123, "sw_b": 0x456,
            })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_3", (0x321 << 16) | 0x234), {
                "hw_a": 0x234, "hw_b": 0x321,
            })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_4", (0x654 << 16) | 0x345), {
                "hw_a_nominal": 0x345, "hw_b_nominal": 0x654,
            })
        sensor9 = TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_9", (2 << 28) | (1 << 9) | (1 << 8) |
            (1 << 3) | (1 << 2)
        )
        self.assertTrue(sensor9["source_flags"]["constant"])
        self.assertEqual(sensor9["source_mask_raw"], "0x30c")
        self.assertEqual(sensor9["source_bits"], [
            False, False, True, True, False, False, False, False, True, True,
        ])
        self.assertEqual(sensor9["unnamed_source_bit_indices"], [3, 8, 9])
        sensor7 = TEMP.decode_confirmed_context_fields(
            "nv_therm_sensor_7", (1 << 16) | (1 << 15) | 0x321
        )
        self.assertEqual(sensor7["debug_adc_code"], 0x321)
        self.assertTrue(sensor7["adc_override_enabled"])
        self.assertTrue(sensor7["power_down"])
        index = TEMP.decode_confirmed_context_fields(
            "nv_therm_gpc_tsense_hs_offset_index",
            (1 << 31) | (1 << 30) | (7 << 16) | 2,
        )
        self.assertEqual(index, {
            "bjt": 2, "gpc": 7, "fields_in_range": True,
            "reserved_bits_zero": True, "read_increment_enabled": True,
            "write_increment_enabled": True,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_therm_gpc_tsense_global_override", (1 << 16) | 0x3FFF
        ), {"value": -1, "enabled": True})
        sci_sm = TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_ts_sm",
            1 | (9 << 8) | (1 << 13) | (17 << 16) | (6 << 21),
        )
        self.assertEqual(sci_sm, {
            "ts_clock_divider": 1, "stable_period": 9,
            "stable_scale": True, "polling_period": 17,
            "polling_scale": 6,
        })
        self.assertIs(type(sci_sm["ts_clock_divider"]), int)
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_ts_sm", 0x1B
        )["ts_clock_divider"], 0x1B)
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_ts_adj", (0x15555 << 11) | (5 << 8) | 0xA5
        ), {
            "bandgap_voltage": 0xA5,
            "bandgap_temperature_coefficient": 5,
            "configuration": 0x15555,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_thresh",
            (6 << 29) | (17 << 24) | (0x1FF << 8),
        ), {
            "temperature_threshold_signed": -1,
            "time_period": 17, "time_scale": 6,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_fusectrl",
            (1 << 31) | (1 << 11) | (3 << 8) | 0x44,
        ), {
            "presnap_bandgap_voltage": 0x44,
            "presnap_bandgap_temperature_coefficient": 3,
            "presnap_bandgap_valid": True,
            "hardware_auto_snapshot": True,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_fuse",
            (0x3FE << 21) | (0x3FF << 11) | (2 << 8) | 0x33,
        ), {
            "bandgap_voltage": 0x33,
            "bandgap_temperature_coefficient": 2,
            "adjustment_a_signed": -1,
            "adjustment_b_signed": -2,
        })
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_temp_coeff_nom", (0x345 << 16) | 0x1AA
        ), {"coefficient_a": 0x1AA, "coefficient_b": 0x345})
        self.assertEqual(TEMP.decode_confirmed_context_fields(
            "nv_pgc6_sci_fs_overt_temp_coeff_final",
            (0x1FFE << 16) | 0xFFF,
        ), {"coefficient_a_signed": -1, "coefficient_b_signed": -2})
        adjustment = TEMP.decode_confirmed_context_fields(
            "adjustment", 0xFFFFFFFF
        )
        # 0xffffffff is a reserved dead-aperture sentinel, never decoded.
        self.assertIsNone(adjustment)
        adjustment = TEMP.decode_confirmed_context_fields(
            "adjustment", 0x7FFFFFFF
        )
        self.assertEqual(adjustment["bandgap_voltage"], 0xFF)
        self.assertEqual(adjustment["ptat_source"], 0xF)
        self.assertTrue(adjustment["start_bandgap"])
        self.assertFalse(adjustment["stop_swapping_bjt"])
        macro = TEMP.decode_hbm_test_macro(0x1F7)
        self.assertTrue(macro["dram_reset_disabled"])
        self.assertTrue(macro["dram_wrst_reset_disabled"])
        self.assertIsNone(macro["temperature_c"])
        badf = TEMP.decode_hbm_test_macro(0xBADF0000)
        self.assertTrue(badf["badf"])
        self.assertIsNone(badf["raw_temperature_sideband"])
        all_ones = TEMP.decode_hbm_test_macro(0xFFFFFFFF)
        self.assertTrue(all_ones["all_ones"])
        self.assertIsNone(all_ones["raw_temperature_sideband"])

    def test_legacy_liveness_and_selector_dependent_reads_are_omitted(self):
        class ConstantProbe(object):
            def __init__(self, value):
                self.value = value
                self.reads_issued = 0
                self.offsets = []

            def read32(self, _index, offset):
                self.reads_issued += 1
                self.offsets.append(offset)
                return self.value

        for unavailable in (0xFFFFFFFF, 0xBADF0000):
            probe = ConstantProbe(unavailable)
            report = TEMP.capture_pure_reads(
                probe, {"probe_index": 0}, legacy_backend=True
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["dead_bar_sentinel_check"], "failed")
            self.assertNotIn(TEMP.FBPA_UC_BASE + TEMP.FBPA_UC_WDR,
                             probe.offsets)
        probe = ConstantProbe(0)
        report = TEMP.capture_pure_reads(
            probe, {"probe_index": 0}, legacy_backend=True
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(
            report["prewrite_liveness"]["two_signal_corroborated"]
        )

        class AffirmativeProbe(ConstantProbe):
            def read32(self, index, offset):
                del index
                self.reads_issued += 1
                self.offsets.append(offset)
                if offset == TEMP.NV_THERM_TEMP_SENSOR_TSENSE:
                    return 1 << 29
                if offset == TEMP.FBPA_NUM_ACTIVE:
                    return 8
                return 0

        probe = AffirmativeProbe(0)
        report = TEMP.capture_pure_reads(
            probe, {"probe_index": 0}, legacy_backend=True
        )
        self.assertEqual(report["status"], "conditional")
        self.assertEqual(
            report["selector_dependent_skipped_mask"], "0x0000000f"
        )
        self.assertEqual(
            report["selector_dependent_read_policy"],
            "unconditionally-omitted-owner-race",
        )
        self.assertEqual(report["reads_issued"], 244)
        for offset in (
                TEMP.NV_THERM_GPC_TSENSE_OVERRIDE,
                TEMP.NV_THERM_GPC_TSENSE_HS_OFFSET,
                TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR,
                TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR,
                TEMP.NV_THERM_GPC_TSENSE_RAW_CODE,
                TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_CURR_SNAPSHOT,
                TEMP.NV_THERM_TEMP_SENSOR_GPC_TSENSE_OFFSET_CURR_SNAPSHOT):
            self.assertNotIn(offset, probe.offsets)

    def test_hbm_data_ack_is_semantic_only(self):
        with self.assertRaisesRegex(TEMP.ProbeError, "valid only for semantic-read"):
            TEMP.main([
                "--probe-backend", "xray-therm", "--all-targets",
                "--acknowledge-hbm-data-read-to-advance",
            ])

    def test_helper_abi5_layout_and_read_count_boundaries(self):
        self.assertEqual(THERM.ctypes.sizeof(THERM.FixedContextRaw), 124)
        self.assertEqual(THERM.ctypes.sizeof(THERM.GpcAnalogRaw), 16)
        self.assertEqual(THERM.ctypes.sizeof(THERM.FbpaRaw), 32)
        self.assertEqual(THERM.ctypes.sizeof(THERM.Capture), 1792)
        self.assertEqual(THERM.Capture.fixed_context.offset, 248)
        self.assertEqual(THERM.Capture.gpc_analog.offset, 372)
        self.assertEqual(THERM.Capture.fbpa.offset, 500)
        self.assertEqual(THERM.Capture.status_opt_fbio.offset, 1772)
        self.assertEqual(
            THERM.Capture.selector_dependent_skipped_mask.offset, 1776
        )
        self.assertEqual(THERM.Capture.hbm_data_read_mask.offset, 1780)
        self.assertEqual(THERM.Capture.prewrite_tsense.offset, 1784)
        self.assertEqual(THERM.Capture.prewrite_active_fbps.offset, 1788)
        capture = self._valid_helper_capture()
        for reads in (386, 1385):
            capture.reads_issued = reads
            self.assertEqual(TEMP.decode_helper_snapshot(capture)["status"], "pass")
        for reads in (385, 1386):
            capture.reads_issued = reads
            self.assertEqual(TEMP.decode_helper_snapshot(capture)["status"], "failed")
        capture.flags = (
            THERM.CAPTURE_F_SNAPSHOT | THERM.CAPTURE_F_HBM_DATA
        )
        capture.hbm_data_read_mask = 0xFFFFFF
        capture.reads_issued = 410
        self.assertEqual(TEMP.decode_helper_snapshot(capture)["status"], "pass")
        capture.reads_issued = 409
        self.assertEqual(TEMP.decode_helper_snapshot(capture)["status"], "failed")

        pure_capture = self._valid_helper_capture()
        pure_capture.flags = 0
        pure_capture.writes_issued = 0
        pure_capture.reads_issued = 255
        pure_capture.snapshot_completed = 0
        pure_capture.prewrite_tsense = 0
        pure_capture.prewrite_active_fbps = 0
        pure = TEMP.decode_helper_pure(
            pure_capture, {"bdf": "0000:01:00.0", "probe_index": 0}
        )
        self.assertEqual(pure["status"], "pass")

    def test_helper_capture_rejects_returned_flags_and_identity_mismatch(self):
        helper = object.__new__(THERM.ThermHelper)
        helper.fd = 123
        helper.info = THERM.Info()
        identity = THERM.Identity()
        identity.generation = 7
        identity.vendor = 0x10DE
        identity.device = 0x20C2
        card = {"raw_identity": identity}

        helper.info.capabilities = THERM.CAP_PURE_CAPTURE
        with self.assertRaisesRegex(THERM.ThermError, "passive selector omission"):
            helper.capture(card)
        helper.info.capabilities = (
            THERM.CAP_PURE_CAPTURE | THERM.CAP_PASSIVE_SELECTOR_OMISSION
        )
        with self.assertRaisesRegex(THERM.ThermError, "fresh prewrite"):
            helper.capture(card)
        helper.info.capabilities |= THERM.CAP_FRESH_PREWRITE_LIVENESS
        card["driver_bound"] = True
        with self.assertRaisesRegex(THERM.ThermError, "bound pure capture"):
            helper.capture(card)
        card["driver_bound"] = False

        result = THERM.Capture()
        result.abi_version = THERM.ABI_VERSION
        result.struct_size = THERM.ctypes.sizeof(THERM.Capture)
        result.identity = identity
        result.flags = THERM.CAPTURE_F_SNAPSHOT
        with mock.patch.object(THERM, "_ioctl_structure", return_value=result):
            with self.assertRaisesRegex(THERM.ThermError, "mismatched capture flags"):
                helper.capture(card)

        result.flags = 0
        result.identity.device = 0x20B5
        with mock.patch.object(THERM, "_ioctl_structure", return_value=result):
            with self.assertRaisesRegex(
                    THERM.ThermError, "mismatched capture identity"):
                helper.capture(card)

    def test_helper_info_rejects_reserved_unknown_and_noncanonical_fields(self):
        info = self._valid_helper_info()
        THERM._validate_info(info)

        for mutate, pattern in (
                (lambda value: setattr(value, "count", 0),
                 "incompatible INFO"),
                (lambda value: setattr(value, "capabilities",
                                       int(value.capabilities) | (1 << 31)),
                 "unknown INFO capabilities"),
                (lambda value: value.reserved0.__setitem__(0, 1),
                 "reserved words"),
                (lambda value: value.cards[0].identity.reserved0.__setitem__(0, 1),
                 "malformed card INFO"),
                (lambda value: setattr(value.cards[0], "driver_bound", 2),
                 "malformed card INFO"),
                (lambda value: setattr(value.cards[0], "bar0_start", 0),
                 "malformed card INFO"),
                (lambda value: setattr(value.cards[0], "bar0_length",
                                       THERM.MAPPED_BAR0_LENGTH - 1),
                 "malformed card INFO"),
                (lambda value: setattr(value.cards[0], "mapped_length",
                                       THERM.MAPPED_BAR0_LENGTH + 4),
                 "malformed card INFO")):
            candidate = THERM.Info.from_buffer_copy(bytes(info))
            mutate(candidate)
            with self.assertRaisesRegex(THERM.ThermError, pattern):
                THERM._validate_info(candidate)

        two = self._valid_helper_info(2)
        for mutate in (
                lambda value: setattr(value.cards[1].identity, "generation", 2),
                lambda value: setattr(value.cards[1].identity, "bus", 1)):
            candidate = THERM.Info.from_buffer_copy(bytes(two))
            mutate(candidate)
            with self.assertRaisesRegex(THERM.ThermError, "malformed card INFO"):
                THERM._validate_info(candidate)

    def test_helper_opens_pure_readonly_and_semantic_readwrite(self):
        info = self._valid_helper_info()
        with mock.patch.object(THERM.os, "open", return_value=17) as opened, \
                mock.patch.object(THERM.os, "close"), \
                mock.patch.object(THERM, "_ioctl_structure", return_value=info):
            helper = THERM.ThermHelper("/dev/test", allow_snapshot=False)
            self.assertEqual(opened.call_args.args[1] & THERM.os.O_ACCMODE,
                             THERM.os.O_RDONLY)
            helper.close()
            helper = THERM.ThermHelper("/dev/test", allow_snapshot=True)
            self.assertEqual(opened.call_args.args[1] & THERM.os.O_ACCMODE,
                             THERM.os.O_RDWR)
            helper.close()

    def test_helper_capture_response_rejects_noncanonical_and_omitted_payloads(self):
        request = THERM.Capture()
        request.abi_version = THERM.ABI_VERSION
        request.struct_size = THERM.ctypes.sizeof(THERM.Capture)
        result = THERM.Capture()
        result.abi_version = THERM.ABI_VERSION
        result.struct_size = THERM.ctypes.sizeof(THERM.Capture)
        result.status = 0
        result.reads_issued = 255
        result.pure_completed = 1
        result.autopoll_unchanged = 1
        result.sentinels_unchanged = 1
        result.failed_gpc = 0xFFFFFFFF
        result.failed_bjt = 0xFFFFFFFF
        result.selector_dependent_skipped_mask = 0xF
        THERM._validate_capture_response(request, result)

        for mutate, pattern in (
                (lambda value: setattr(value, "pure_completed", 2),
                 "noncanonical capture flags"),
                (lambda value: setattr(value, "status", 1),
                 "positive capture status"),
                (lambda value: setattr(value, "failed_gpc", 8),
                 "invalid failed endpoint"),
                (lambda value: setattr(value, "selector_dependent_skipped_mask", 0),
                 "passive selector omission"),
                (lambda value: setattr(value, "current_temperature", 1),
                 "unconditionally omitted payloads"),
                (lambda value: setattr(value.fixed_context,
                                       "gpc_tsense_override", 1),
                 "unconditionally omitted payloads"),
                (lambda value: setattr(value, "hbm_data_read_mask", 1),
                 "unacknowledged HBM DATA"),
                (lambda value: setattr(value.fbpa[0], "data", 1),
                 "unobserved HBM DATA slot"),
                (lambda value: setattr(value, "snapshot_selector_entry", 1),
                 "pure capture contains semantic state")):
            candidate = THERM.Capture.from_buffer_copy(bytes(result))
            mutate(candidate)
            with self.assertRaisesRegex(THERM.ThermError, pattern):
                THERM._validate_capture_response(request, candidate)
        candidate = THERM.Capture.from_buffer_copy(bytes(result))
        candidate.snapshot_aggregate[0] = 1
        with self.assertRaisesRegex(
                THERM.ThermError, "pure capture contains semantic state"):
            THERM._validate_capture_response(request, candidate)

        semantic_request = THERM.Capture()
        semantic_request.abi_version = THERM.ABI_VERSION
        semantic_request.struct_size = THERM.ctypes.sizeof(THERM.Capture)
        semantic_request.flags = THERM.CAPTURE_F_SNAPSHOT
        semantic = self._valid_helper_capture()
        semantic.abi_version = THERM.ABI_VERSION
        semantic.struct_size = THERM.ctypes.sizeof(THERM.Capture)
        semantic.flags = THERM.CAPTURE_F_SNAPSHOT
        semantic.selector_dependent_skipped_mask = 0xF
        THERM._validate_capture_response(semantic_request, semantic)
        for mutate in (
                lambda value: setattr(value, "snapshot_selector_exit", 1),
                lambda value: setattr(value, "prewrite_active_fbps", 0),
                lambda value: value.ts_auto_exit.__setitem__(0, 1),
                lambda value: setattr(value.gpc_bjt[0], "selector_after", 1),
                lambda value: setattr(value, "reads_issued", 385)):
            candidate = THERM.Capture.from_buffer_copy(bytes(semantic))
            mutate(candidate)
            with self.assertRaisesRegex(
                    THERM.ThermError, "completed snapshot journal"):
                THERM._validate_capture_response(semantic_request, candidate)

    def test_pure_read_hbm_context_and_fixed_stack_mapping(self):
        self.assertEqual(TEMP.SCHEMA, "170hx-xray-ga100-temperature-census-v8")
        self.assertEqual(TEMP.VERSION, "1.7.0")
        probe = FakeProbe()
        base = TEMP.FBPA_UC_BASE + TEMP.FBPA_UC_STRIDE
        probe.regs[base + TEMP.FBPA_UC_MODE] = 0
        offline_base = TEMP.FBPA_UC_BASE + 2 * TEMP.FBPA_UC_STRIDE
        probe.regs[offline_base + TEMP.FBPA_UC_CFG1] = 0xBADF0000
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        self.assertEqual(report["writes_issued"], 0)
        self.assertEqual(len(report["fbpa_readout_channels"]), 24)
        self.assertIsNone(report["fbpa_readout_channels"][0]["raw"])
        self.assertFalse(report["fbpa_readout_channels"][0]["data_observed"])
        self.assertNotIn(
            TEMP.FBPA_UC_BASE + TEMP.FBPA_UC_WDR, probe.read_offsets
        )
        self.assertIsNone(report["fbpa_readout_channels"][1]["temperature_c"])
        self.assertFalse(report["fbpa_readout_channels"][2]["live"])
        self.assertIsNone(report["fbpa_readout_channels"][2]["temperature_c"])
        mapping = {
            item["stack"]: tuple(item["fbps"])
            for item in report["hbm_stack_groups"]
        }
        self.assertEqual(mapping, dict(TEMP.STACKS))
        self.assertEqual(
            [item["firmware_site_ordinal"]
             for item in report["hbm_stack_groups"]],
            list(range(6)),
        )
        self.assertEqual(
            [item["public_hbmmap_label"]
             for item in report["hbm_stack_groups"]],
            list(TEMP.PUBLIC_HBMMAP_LABELS_BY_FIRMWARE_ORDINAL),
        )
        first_fbpa = report["fbpa_readout_channels"][0]
        self.assertEqual(first_fbpa["cfg1_offset"], "0x00900204")
        self.assertEqual(first_fbpa["instruction_offset"], "0x00903cb4")
        self.assertEqual(first_fbpa["mode_offset"], "0x00903cb8")
        self.assertEqual(first_fbpa["wdr_offset"], "0x00903cbc")
        observed = TEMP.capture_pure_reads(
            FakeProbe(), {"probe_index": 0}, hbm_data_read_mask=0xFFFFFF
        )
        self.assertEqual(observed["fbpa_readout_channels"][0]["temperature_c"], 42)
        self.assertTrue(observed["fbpa_readout_channels"][0]["data_observed"])
        self.assertEqual(first_fbpa["shadow_instruction_offset"], "0x00903cc0")
        self.assertEqual(first_fbpa["shadow_data_offset"], "0x00903cc4")
        self.assertEqual(first_fbpa["status_offset"], "0x00903cc8")
        population = report["population"]
        self.assertEqual(population["expected_enabled_count"], 24)
        self.assertIsNone(population["observed_valid_endpoints"])
        self.assertIsNone(population["observed_matches_population_masks"])
        self.assertIn("never initiate", report["hbm_transaction_policy"])

    def test_fbio_mask_is_direct_per_fbpa_and_site_selects_first_enabled(self):
        probe = FakeProbe()
        probe.regs[TEMP.NV_FUSE_STATUS_OPT_FBIO] = 1 << 0
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        population = report["fbio_population"]
        self.assertFalse(population["fbpa_enabled"][0])
        self.assertTrue(population["fbpa_enabled"][1])
        self.assertEqual(population["disabled_fbpa_indices"], [0])
        self.assertEqual(report["hbm_stack_groups"][0]["selected_fbpa"], 1)
        self.assertTrue(report["fbpa_readout_channels"][0]["aperture_responded"])
        self.assertFalse(report["fbpa_readout_channels"][0]["enabled"])
        self.assertFalse(report["fbpa_readout_channels"][0]["live"])

        probe = FakeProbe()
        probe.regs[TEMP.NV_FUSE_STATUS_OPT_FBIO] = 1 << 1
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        self.assertTrue(report["fbio_population"]["fbpa_enabled"][0])
        self.assertFalse(report["fbio_population"]["fbpa_enabled"][1])
        self.assertEqual(report["hbm_stack_groups"][0]["selected_fbpa"], 0)

    def test_hbm_context_gate_rejects_extra_writable_bits_and_nonexact_shwir(self):
        probe = FakeProbe()
        report = TEMP.capture_pure_reads(
            probe, {"probe_index": 0}, hbm_data_read_mask=0xFFFFFF
        )
        self.assertTrue(report["fbpa_readout_channels"][0]["valid"])
        self.assertEqual(report["fbpa_readout_channels"][0]["temperature_c"], 42)

        base = TEMP.FBPA_UC_BASE
        probe = FakeProbe()
        probe.regs[base + TEMP.FBPA_UC_TEST_MACRO] = 0x51
        report = TEMP.capture_pure_reads(
            probe, {"probe_index": 0}, hbm_data_read_mask=0xFFFFFF
        )
        self.assertFalse(
            report["fbpa_readout_channels"][0][
                "test_macro_temperature_context_ready"
            ]
        )
        self.assertFalse(report["fbpa_readout_channels"][0]["valid"])

        probe = FakeProbe()
        probe.regs[base + TEMP.FBPA_UC_SHWIR] = 0x1F0
        report = TEMP.capture_pure_reads(
            probe, {"probe_index": 0}, hbm_data_read_mask=0xFFFFFF
        )
        self.assertFalse(report["fbpa_readout_channels"][0]["valid"])
        self.assertFalse(
            report["fbpa_readout_channels"][0][
                "shadow_instruction_corroborates_temperature_empirically"
            ]
        )

    def test_dead_selectors_never_bind_incidental_payloads_to_endpoints(self):
        probe = FakeProbe()
        probe.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX] = 0xBADF5040
        probe.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT] = 0xBADF5040
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        for item in (
                report["currently_selected_gpc_readout"],
                report["snapshot_as_found"]):
            self.assertFalse(item["selector_available"])
            self.assertTrue(item["selector_badf"])
            self.assertIsNone(item.get("gpc"))
            self.assertIsNone(item.get("bjt"))
            self.assertEqual(item["temperature"]["state"],
                             "not-read-owner-race-policy")
            self.assertIsNone(item["temperature"]["temperature_c"])

        probe = FakeProbe()
        probe.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX] = 0xFFFFFFFF
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        current = report["currently_selected_gpc_readout"]
        self.assertFalse(current["selector_available"])
        self.assertTrue(current["selector_all_ones"])
        self.assertTrue(current["dependent_reads_skipped"])
        self.assertIsNone(current["gpc"])
        self.assertIsNone(current["bjt"])

    def test_malformed_selector_fields_are_available_but_never_endpoint_bound(self):
        probe = FakeProbe()
        # GPC 8/BJT 3 is field-shaped but outside the GA100 8x3 matrix.
        probe.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX] = 0x00080003
        # Bit 20 is reserved even though GPC/BJT themselves decode in range.
        probe.regs[TEMP.NV_THERM_GPC_TSENSE_INDEX_SNAPSHOT] = 0x00100000
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        current = report["currently_selected_gpc_readout"]
        self.assertTrue(current["aperture_available"])
        self.assertFalse(current["selector_fields_in_range"])
        self.assertTrue(current["selector_reserved_bits_zero"])
        self.assertFalse(current["selector_semantically_valid"])
        self.assertIsNone(current["gpc"])
        self.assertIsNone(current["bjt"])
        self.assertEqual(current["temperature"]["state"],
                         "not-read-owner-race-policy")

        found = report["snapshot_as_found"]
        self.assertTrue(found["aperture_available"])
        self.assertTrue(found["selector_fields_in_range"])
        self.assertFalse(found["selector_reserved_bits_zero"])
        self.assertFalse(found["selector_semantically_valid"])
        self.assertIsNone(found["gpc"])
        self.assertIsNone(found["bjt"])
        self.assertEqual(found["temperature"]["state"],
                         "not-read-owner-race-policy")

    def test_population_badf_is_unavailable_and_never_decoded(self):
        report = TEMP.population_report(0xBADF201B, 0, [])
        self.assertFalse(report["mask_reads_available"])
        self.assertIsNone(report["expected_enabled_endpoints"])
        self.assertIsNone(report["expected_enabled_count"])
        self.assertEqual(report["observed_valid_endpoints"], [])
        self.assertIsNone(report["observed_matches_population_masks"])

    def test_autopoll_uses_bit15_and_not_public_bit17_bug(self):
        probe = FakeProbe()
        probe.regs[TEMP.PCHIPLET_TS_AUTO_BASE] = 1 << 15
        probe.regs[
            TEMP.PCHIPLET_TS_AUTO_BASE + TEMP.PCHIPLET_TS_AUTO_STRIDE
        ] = 1 << 17
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        controls = report["gpc_autopoll_controls"]
        self.assertTrue(controls[0]["auto_polling"])
        self.assertFalse(controls[1]["auto_polling"])
        self.assertTrue(all(item["auto_polling_bit"] == 15 for item in controls))
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("PCHIPLET_TS_AUTO_POLLING = 1 << 15", source)
        self.assertNotIn("PCHIPLET_TS_AUTO_POLLING = 1 << 17", source)

    def test_badf_autopoll_is_unavailable_and_never_decoded(self):
        probe = FakeProbe()
        probe.regs[TEMP.PCHIPLET_TS_AUTO_BASE] = 0xBADF8000
        report = TEMP.capture_pure_reads(probe, {"probe_index": 0})
        control = report["gpc_autopoll_controls"][0]
        self.assertFalse(control["available"])
        self.assertIsNone(control["auto_polling"])
        self.assertIsNone(control["sample_delay"])
        self.assertIsNone(control["polling_interval"])

    def test_opt_ts_disable_mask_rejects_reserved_upper_bits(self):
        report = TEMP.population_report(0x01000000, 0)
        self.assertFalse(report["opt_ts_dis_reserved_zero"])
        self.assertFalse(report["mask_reads_available"])
        self.assertIsNone(report["expected_enabled_endpoints"])

    def test_public_gpcprobe_binding_has_no_invented_write_ioctl(self):
        self.assertFalse(hasattr(TEMP, "GP_IOC_WRITE"))
        self.assertFalse(hasattr(TEMP.GpcProbe, "write32"))

    def test_legacy_gpcprobe_info_frame_is_fail_closed(self):
        def open_with_words(words):
            def ioctl(_fd, command, payload, _mutate):
                self.assertEqual(command, TEMP.GP_IOC_INFO)
                payload[:] = TEMP.struct.pack("=17I", *words)
            with mock.patch.object(TEMP.os, "open", return_value=17), \
                    mock.patch.object(TEMP.os, "close"), \
                    mock.patch.object(TEMP.fcntl, "ioctl", side_effect=ioctl):
                probe = TEMP.GpcProbe("/dev/gpcprobe")
                probe.fd = None
                return probe

        valid = [2, 0x0100, 0x0200] + [0] * 14
        probe = open_with_words(valid)
        self.assertEqual(
            [(card["bus"], card["device"], card["function"])
             for card in probe.cards],
            [(1, 0, 0), (2, 0, 0)],
        )
        for words in (
                [0] * 17,
                [1, 0x00010100] + [0] * 15,
                [2, 0x0100, 0x0100] + [0] * 14,
                [1, 0x0100, 0x0200] + [0] * 14):
            with self.assertRaisesRegex(TEMP.ProbeError, "invalid|malformed"):
                open_with_words(words)

    def test_selector_acknowledgement_is_rejected_for_bar_read(self):
        with self.assertRaisesRegex(TEMP.ProbeError, "valid only for semantic-read"):
            TEMP.main([
                "--all-targets", "--access", "bar-read",
                "--acknowledge-selector-write",
            ])

    def test_domain_ambiguity_fails_closed(self):
        probe_cards = [{"probe_index": 0, "bus": 1, "device": 0, "function": 0}]
        cards = [
            {"bdf": "0000:01:00.0", "domain": 0, "bus": 1,
             "device": 0, "function": 0},
            {"bdf": "0001:01:00.0", "domain": 1, "bus": 1,
             "device": 0, "function": 0},
        ]
        with self.assertRaisesRegex(TEMP.ProbeError, "omits PCI domain"):
            TEMP.bind_probe_indices(probe_cards, cards)
        bound = TEMP.bind_probe_indices(probe_cards, cards[:1])
        self.assertEqual(bound[0]["bdf"], "0000:01:00.0")
        with self.assertRaisesRegex(TEMP.ProbeError, "no matching"):
            TEMP.bind_probe_indices(
                [{"probe_index": 0, "bus": 2, "device": 0, "function": 0}],
                cards[:1],
            )
        with self.assertRaisesRegex(TEMP.ProbeError, "coverage mismatch"):
            TEMP.bind_probe_indices(
                probe_cards,
                [cards[0], {
                    "bdf": "0000:02:00.0", "domain": 0, "bus": 2,
                    "device": 0, "function": 0,
                }],
                require_complete=True,
            )

    def test_dedicated_helper_binding_requires_full_identity_and_all_coverage(self):
        sysfs = [{
            "bdf": "0000:01:00.0", "vendor_id": "0x10de",
            "device_id": "0x20c2", "subsystem_vendor_id": "0x10de",
            "subsystem_device_id": "0x0001", "revision": "0x01",
        }, {
            "bdf": "0000:02:00.0", "vendor_id": "0x10de",
            "device_id": "0x20c2", "subsystem_vendor_id": "0x10de",
            "subsystem_device_id": "0x0002", "revision": "0x01",
        }]
        helper = [{
            "helper_index": 0,
            "identity": dict(sysfs[0], generation="0x0000000000000001"),
        }]
        bound = TEMP.bind_therm_cards(helper, sysfs)
        self.assertEqual(bound[0][0]["bdf"], sysfs[0]["bdf"])
        with self.assertRaisesRegex(TEMP.ProbeError, "coverage mismatch"):
            TEMP.bind_therm_cards(helper, sysfs, require_complete=True)
        missing = [{
            "helper_index": 0,
            "identity": dict(helper[0]["identity"], bdf="0000:03:00.0"),
        }]
        with self.assertRaisesRegex(TEMP.ProbeError, "no matching"):
            TEMP.bind_therm_cards(missing, sysfs)

    def test_nvidia_smi_na_is_null_with_per_query_evidence(self):
        completed = mock.Mock(
            returncode=0, stderr="",
            stdout="0, 00000000:01:00.0, CMP 170HX, 45, N/A, 0, 92.5\n",
        )
        with mock.patch.object(TEMP.subprocess, "run", return_value=completed):
            report = TEMP.nvidia_smi_snapshot()
        row = report["cards"]["0000:01:00.0"]
        self.assertIsNone(row["memory_temperature_c"])
        query = row["queries"]["memory_temperature_c"]
        self.assertEqual(query["return_status"], "not-available")
        self.assertIsNone(query["value"])
        self.assertIsInstance(query["timestamp_utc"], str)
        self.assertIsInstance(query["latency_us"], int)
        self.assertFalse(query["canonical_nvml_api"])

    def test_semantic_gate_fails_closed_without_utilization(self):
        card = {
            "bdf": "0000:01:00.0", "domain": 0, "bus": 1,
            "device": 0, "function": 0, "probe_index": 0,
        }

        class ContextProbe(object):
            cards = [{"probe_index": 0, "bus": 1, "device": 0, "function": 0}]

            def __init__(self, *args, **kwargs):
                del args, kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args

            def close(self):
                pass

        argv = [
            "--probe-backend", "gpcprobe", "--device", "/dev/gpcprobe",
            "--bdf", card["bdf"], "--access", "semantic-read",
            "--acknowledge-selector-write", "--no-driver-queries",
        ]
        with mock.patch.object(TEMP, "GpcProbe", ContextProbe), \
                mock.patch.object(TEMP, "sysfs_cards", return_value=[card]):
            with self.assertRaisesRegex(TEMP.ProbeError, "requires xray-therm"):
                TEMP.main(argv)

    def test_semantic_override_is_explicit_and_emits_failed_snapshot_status(self):
        card = {
            "bdf": "0000:01:00.0", "domain": 0, "bus": 1,
            "device": 0, "function": 0, "probe_index": 0,
        }

        class ContextProbe(object):
            cards = [{"probe_index": 0, "bus": 1, "device": 0, "function": 0}]

            def __init__(self, *args, **kwargs):
                del args, kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args

            def close(self):
                pass

        argv = [
            "--probe-backend", "gpcprobe", "--device", "/dev/gpcprobe",
            "--bdf", card["bdf"], "--access", "semantic-read",
            "--acknowledge-selector-write", "--no-driver-queries",
            "--allow-active-workload",
        ]
        with mock.patch.object(TEMP, "GpcProbe", ContextProbe), \
                mock.patch.object(TEMP, "sysfs_cards", return_value=[card]), \
                mock.patch.object(TEMP, "hwmon_snapshot", return_value=[]), \
                mock.patch.object(TEMP, "capture_pure_reads", return_value={
                    "access": "bar-read", "writes_issued": 0,
                }):
            with self.assertRaisesRegex(TEMP.ProbeError, "requires xray-therm"):
                TEMP.main(argv)

    def test_output_is_private_create_only(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "temperature.json"
            TEMP.emit_document("{}\n", str(output))
            self.assertEqual(output.read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(TEMP.ProbeError, "overwrite"):
                TEMP.emit_document("{}\n", str(output))


if __name__ == "__main__":
    unittest.main()
