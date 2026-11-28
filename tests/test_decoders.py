#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained public-vector tests for DEC-01 through DEC-04.

The normal edition additionally locks its model to retained source anchors and
private captures. This sanitized suite intentionally has no dependency on
those files or on the normal package.
"""

import importlib.util
import random
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "170HX-XRAY.py"


def load_xray():
    spec = importlib.util.spec_from_file_location(
        "xray_sanitized_decoder_test_target", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DecoderHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xray = load_xray()

    def test_public_model_exposes_fact_observation_unknown_boundaries(self):
        self.assertEqual(
            (self.xray.GA100_GPC_COUNT, self.xray.GA100_TPCS_PER_GPC,
             self.xray.GA100_SMS_PER_TPC),
            (8, 8, 2),
        )
        self.assertEqual(
            self.xray.IFF_LENGTHS,
            {0x00: 1, 0x1B: 3, 0x1C: 3, 0x1D: 2, 0x1E: 1, 0x1F: 1},
        )
        separation = self.xray.IFF_MACHINE_SPEC["evidence_separation"]
        self.assertTrue(separation["authoritative_facts"])
        self.assertTrue(separation["empirical_observations"])
        self.assertTrue(separation["unresolved"])
        self.assertFalse(
            self.xray.IFF_MACHINE_SPEC["shared_interval"]["official_ga100_bounds_known"]
        )
        poison = self.xray.decode_ga100_rir_words(
            [0, 0, 0, 0, 0, 0, 0, 0xFFFF0923]
        )
        self.assertEqual(
            [(item["slot"], item["absolute_bit_address"], item["forced_data"])
             for item in poison["enabled_records"]],
            [(14, 584, 1)],
        )

    def test_rir_linear_round_trip_exhaustive_and_duplicates_fail_closed(self):
        for address in range(1 << 14):
            for data in (0, 1):
                raw = (address << 2) | (data << 1) | 1
                decoded = self.xray.decode_ga100_rir_words([raw] + [0] * 7)
                if raw == 0xFFFF:
                    self.assertEqual(decoded["entries"][0]["state"], "invalidated_spent")
                    continue
                record = decoded["enabled_records"][0]
                self.assertEqual(record["absolute_bit_address"], address)
                self.assertEqual(record["forced_data"], data)

        address = 0x123
        zero = (address << 2) | 1
        one = (address << 2) | 3
        same = self.xray.decode_ga100_rir_words([zero | (zero << 16)] + [0] * 7)
        self.assertFalse(same["effective_replay_available"])
        self.assertTrue(same["duplicate_address_groups"][0]["values_consistent"])
        conflict = self.xray.decode_ga100_rir_words([zero | (one << 16)] + [0] * 7)
        self.assertFalse(conflict["effective_replay_available"])
        self.assertTrue(conflict["duplicate_address_groups"][0]["conflicting_values"])

    def test_rir_input_validation_and_nonblank_inactive_state(self):
        for bad in (None, [0] * 7, [0] * 9, [0] * 7 + [-1],
                    [0] * 7 + [1 << 32], [0] * 7 + [True]):
            with self.assertRaises(self.xray.AuditError):
                self.xray.decode_ga100_rir_words(bad)
        inactive = self.xray.decode_ga100_rir_words([2] + [0] * 7)
        self.assertEqual(inactive["entries"][0]["state"], "disabled_record")
        self.assertEqual((inactive["used_slots"], inactive["blank_slots"]), (1, 15))

    @staticmethod
    def reference_ff(rows, start, end):
        boundary = end + 1
        frontier = start
        effective = {}
        for index in range(start, end + 1):
            value = rows[index]
            if value == 0xFFFFFFFF:
                frontier = index + 1
                continue
            if value & 0x20:
                boundary = index
                break
            if value:
                frontier = index + 1
            chain = value & 0x1F
            if value and chain not in (0, 0x1F):
                effective[(chain, (value >> 6) & 0x3FF)] = (
                    (value >> 16) & 0xFFFF, index
                )
        return boundary, frontier, effective

    def test_ff_frontier_boundary_and_replay_differential(self):
        rng = random.Random(0x170DEC01)
        for _ in range(250):
            length = rng.randint(8, 96)
            boundary = rng.randint(1, length)
            rows = [0] * length
            for index in range(boundary):
                kind = rng.randrange(6)
                if kind == 0:
                    value = 0
                elif kind == 1:
                    value = 0xFFFFFFFF
                elif kind == 2:
                    value = (rng.randrange(1, 0x10000) << 16) | (rng.randrange(1024) << 6)
                elif kind == 3:
                    value = (rng.randrange(0x10000) << 16) | (rng.randrange(1024) << 6) | 0x1F
                else:
                    value = (
                        (rng.randrange(0x10000) << 16)
                        | (rng.randrange(1024) << 6)
                        | rng.randint(1, 30)
                    )
                rows[index] = value
            if boundary < length:
                rows[boundary:] = [0x20] * (length - boundary)
            expected_b, expected_f, expected_effective = self.reference_ff(
                rows, 0, length - 1
            )
            decoded = self.xray.decode_ff_iff(rows, 0, length - 1)
            self.assertTrue(decoded["structurally_complete"], decoded["errors"])
            self.assertEqual(
                decoded["boundary"]["iff_start"],
                expected_b if expected_b < length else None,
            )
            self.assertEqual(decoded["boundary"]["first_available_ff_row"], expected_f)
            observed = {
                (item["chain"], item["block"]): (int(item["data"], 0), item["row"])
                for item in decoded["effective_ff_records"]
            }
            self.assertEqual(observed, expected_effective)
            capacity = decoded["capacity_snapshot"]
            self.assertEqual(
                (capacity["iff_boundary_B"], capacity["ff_frontier_F"],
                 capacity["zero_gap_rows"]),
                (expected_b, expected_f, expected_b - expected_f),
            )
            self.assertTrue(capacity["physical_partition"]["partition_matches"])
            self.assertEqual(
                capacity["physical_partition"]["iff_side_span_rows_including_spent_suffix"],
                length - expected_b,
            )

    def test_iff_lengths_order_and_set_field_properties(self):
        set0 = 0x20 | 0x1E | (3 << 6)
        set1 = 0x20 | 0x1E | (3 << 6) | (1 << 18)
        rows = [
            set0,
            0x20 | 0x1D, 0xA5A5A5A5,
            0x20 | 0x1C, 0x0F0F0F0F, 0x10101010,
            0x20 | 0x1B, 0x11111111, 0x22222222,
            0x20, 0xFFFFFFFF, set1,
        ]
        decoded = self.xray.decode_ff_iff(rows, 0, len(rows) - 1)
        self.assertTrue(decoded["structurally_complete"])
        self.assertFalse(decoded["ga100_semantically_valid"])
        self.assertEqual(
            [item["length"] for item in decoded["iff_records"]],
            [1, 2, 3, 3, 1, 1, 1],
        )
        self.assertEqual(
            [item["physical_order_index"] for item in decoded["iff_records"]],
            list(range(7)),
        )
        self.assertEqual(
            [item["execution_order_index"] for item in decoded["iff_records"]],
            [0, 1, 2, 3, None, None, 4],
        )

        for space, info in self.xray.IFF_SET_FIELD_SPACES.items():
            offsets = [
                max(0, info["valid_min"] - info["base"]),
                min(0xFFC, info["valid_max"] - info["base"]),
            ]
            for offset in offsets:
                for field_spec in (1, 2, 3, 0x7F):
                    width = field_spec.bit_length() - 1
                    bit_index = 31 - width
                    word = (
                        0x20 | 0x1E | (field_spec << 6) | (bit_index << 13)
                        | (space << 18) | ((offset >> 2) << 22)
                    )
                    result = self.xray.decode_iff_operands(0x1E, [word])
                    self.assertEqual(result["field_width_bits"], width)
                    self.assertTrue(result["target_in_advertised_space_range"])
                    self.assertNotIn("malformed", result)

    def test_malformed_inputs_and_incomplete_capacity_fail_closed(self):
        for rows in (None, [], [True], [-1], [1 << 32]):
            with self.assertRaises(self.xray.AuditError):
                self.xray.decode_ff_iff(rows, 0, 0)
        for opcode, words in ((0x1D, [0x3D]), (0x1E, []), (1, [0x21]),
                              (0x1E, [0x3D]), (0x1E, [-1])):
            with self.assertRaises(self.xray.AuditError):
                self.xray.decode_iff_operands(opcode, words)
        with self.assertRaises(self.xray.AuditError):
            self.xray.scan_shared_ff_iff_capacity([0], 0, 0, reserve=-1)
        with self.assertRaises(self.xray.AuditError):
            self.xray.scan_shared_ff_iff_capacity([0], 0, 0, reserve=True)

        for rows, expected_text in (([0x21], "unknown IFF opcode"),
                                    ([0x3D], "overruns explicit end"),
                                    ([0x20, 0], "type bit 5 clear")):
            decoded = self.xray.decode_ff_iff(rows, 0, len(rows) - 1)
            self.assertFalse(decoded["structurally_complete"])
            self.assertIn(expected_text, decoded["structural_errors"][0])
        malformed_set = self.xray.decode_ff_iff([0x3E], 0, 0)
        self.assertTrue(malformed_set["structurally_complete"])
        self.assertFalse(malformed_set["ga100_semantically_valid"])

        rows = [1, 0, 0, 0, 0, 0, 0, 0x20, None, None]
        capacity = self.xray.scan_shared_ff_iff_capacity(rows, 0, 9, reserve=5)
        self.assertTrue(capacity["frontier_complete"])
        self.assertFalse(capacity["entire_interval_known"])
        self.assertTrue(capacity["observed_gap_meets_reserve"])
        self.assertIsNone(capacity["reserve_satisfied"])
        self.assertFalse(capacity["allocation_proof_complete"])

    def test_restricted_topology_formula_and_unresolved_control_state(self):
        disable_masks = [0xFF, 0xFF, 0x01, 0x00, 0x00, 0xFF, 0x01, 0x01]
        fixture = {
            name: {"status": "ok", "raw": "0x00000000"}
            for name in (
                "GPC_DEFECTIVE", "OPT_GPC_DISABLE", "OPT_GPC_DISABLE_CP",
                "CTRL_OPT_GPC", "STATUS_OPT_GPC",
            )
        }
        for gpc, disable in enumerate(disable_masks):
            values = {
                "TPC_GPC%d_DEFECTIVE" % gpc: 0,
                "OPT_TPC_GPC%d_DISABLE" % gpc: disable,
                "OPT_TPC_GPC%d_RECONFIG" % gpc: 0x04,
                "CTRL_OPT_TPC_GPC%d" % gpc: 0,
                "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc: 0x04,
                "DISABLE_EXTEND_TPC_GPC%d" % gpc: 0,
                "STATUS_OPT_TPC_GPC%d" % gpc: disable,
            }
            for name, value in values.items():
                fixture[name] = {"status": "ok", "raw": "0x%08x" % value}
        result = self.xray.analyze_tpc_layers(fixture)
        self.assertEqual(result["totals"]["effective_enabled_tpcs"], 37)
        self.assertEqual(result["sm_count_if_two_sms_per_tpc"]["effective_sms"], 74)
        self.assertEqual(
            (result["architecture_capacity"]["maximum_tpcs"],
             result["architecture_capacity"]["maximum_sms"]),
            (64, 128),
        )
        self.assertTrue(all(item["base_formula_matches"] for item in result["per_gpc"]))

        fixture["CTRL_OPT_TPC_GPC0"] = {"status": "ok", "raw": "0x00000001"}
        unresolved = self.xray.analyze_tpc_layers(fixture)
        self.assertFalse(unresolved["per_gpc"][0]["base_formula_assessable"])
        self.assertIsNone(unresolved["per_gpc"][0]["base_formula_matches"])

    def test_memory_capacity_layers_cover_64_80_96_and_reject_pair_mismatch(self):
        def expand(mask):
            value = 0
            for bit in range(12):
                if mask & (1 << bit):
                    value |= 3 << (2 * bit)
            return value

        def reading(value):
            return {"status": "ok", "raw": "0x%08x" % value}

        def fixture(defect_fbp, disable_fbp):
            defect_sub = expand(defect_fbp)
            disable_sub = expand(disable_fbp)
            values = {
                "FBP_DEFECTIVE": defect_fbp,
                "OPT_FBP_DISABLE": disable_fbp,
                "OPT_FBP_DISABLE_CP": 0,
                "CTRL_OPT_FBP": 0,
                "STATUS_OPT_FBP": disable_fbp,
                "FBPA_DEFECTIVE": defect_sub,
                "OPT_FBPA_DISABLE": disable_sub,
                "OPT_FBPA_DISABLE_CP": 0,
                "CTRL_OPT_FBPA": 0,
                "STATUS_OPT_FBPA": disable_sub,
                "FBIO_DEFECTIVE": defect_sub,
                "OPT_FBIO_DISABLE": disable_sub,
                "OPT_FBIO_DISABLE_CP": 0,
                "CTRL_OPT_FBIO": 0,
                "STATUS_OPT_FBIO": disable_sub,
                "ROP_L2_DEFECTIVE": defect_sub,
                "OPT_ROP_L2_DISABLE": disable_sub,
                "OPT_ROP_L2_DISABLE_CP": 0,
            }
            return {name: reading(value) for name, value in values.items()}

        for defect, expected_target in ((0x00F, 64), (0x840, 80), (0x000, 96)):
            current_disable = defect | 0x012
            result = self.xray.analyze_memory_layers(fixture(defect, current_disable))
            self.assertTrue(result["capacity_projection"]["target_admissible"])
            self.assertEqual(
                result["capacity_projection"]["defect_preserving_target_gib"],
                expected_target,
            )

        inconsistent = fixture(0x840, 0x852)
        inconsistent["FBPA_DEFECTIVE"] = reading(expand(0x840) ^ 1)
        result = self.xray.analyze_memory_layers(inconsistent)
        self.assertFalse(result["capacity_projection"]["target_admissible"])
        self.assertIsNone(
            result["capacity_projection"]["defect_preserving_target_gib"]
        )
        self.assertEqual(result["state"], "incomplete_or_inconsistent")

    def test_normalized_topology_rejects_unsupported_capacity_classes(self):
        def reading(value):
            return {"status": "ok", "raw": "0x%08x" % value}

        def expand(mask):
            return sum(
                (3 << (2 * bit)) for bit in range(12)
                if mask & (1 << bit)
            )

        def fixture(defect_fbp):
            defect_subpartitions = expand(defect_fbp)
            values = {
                "GPC_DEFECTIVE": 0, "OPT_GPC_DISABLE": 0,
                "STATUS_OPT_GPC": 0,
                "PES_DEFECTIVE": 0, "OPT_PES_DISABLE": 0,
                "FBP_DEFECTIVE": defect_fbp,
                "OPT_FBP_DISABLE": defect_fbp,
                "STATUS_OPT_FBP": defect_fbp,
                "FBPA_DEFECTIVE": defect_subpartitions,
                "OPT_FBPA_DISABLE": defect_subpartitions,
                "STATUS_OPT_FBPA": defect_subpartitions,
                "FBIO_DEFECTIVE": defect_subpartitions,
                "OPT_FBIO_DISABLE": defect_subpartitions,
                "STATUS_OPT_FBIO": defect_subpartitions,
                "ROP_L2_DEFECTIVE": defect_subpartitions,
                "OPT_ROP_L2_DISABLE": defect_subpartitions,
                "OPT_HALF_FBPA_ENABLE": 0,
                "CTRL_OPT_HALF_FBPA": 0,
                "STATUS_HALF_FBPA": 0,
            }
            for gpc in range(self.xray.GA100_GPC_COUNT):
                values.update({
                    "TPC_GPC%d_DEFECTIVE" % gpc: 0,
                    "OPT_TPC_GPC%d_DISABLE" % gpc: 0,
                    "STATUS_OPT_TPC_GPC%d" % gpc: 0,
                    "STATUS_OPT_PES_GPC[%d]" % gpc: 0,
                    "OPT_TPC_GPC%d_RECONFIG" % gpc: 0,
                    "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc: 0,
                    "CTRL_OPT_TPC_GPC%d" % gpc: 0,
                    "DISABLE_EXTEND_TPC_GPC%d" % gpc: 0,
                })
            return {name: reading(value) for name, value in values.items()}

        density = {"qualified": True, "gib_per_subpartition": 4}
        for defect, capacity in ((0x00F, 64), (0x003, 80), (0x000, 96)):
            result = self.xray.normalize_ga100_topology(
                fixture(defect), 4, density
            )
            self.assertTrue(result["memory"]["target_admissible"])
            self.assertEqual(
                result["memory"]["layers"]["defect"]["capacity_gib"],
                capacity,
            )
            self.assertEqual(
                result["memory"]["supported_exact_subpartition_counts"],
                [16, 20, 24],
            )
            self.assertEqual(
                result["memory"]["supported_exact_capacity_classes_gib"],
                [64, 80, 96],
            )

        for defect, diagnostic_capacity in ((0x007, 72), (0x001, 88)):
            result = self.xray.normalize_ga100_topology(
                fixture(defect), 4, density
            )
            self.assertEqual(
                result["memory"]["layers"]["defect"]["capacity_gib"],
                diagnostic_capacity,
            )
            self.assertFalse(result["memory"]["target_admissible"])
            self.assertEqual(result["state"], "incomplete_or_inconsistent")
            self.assertTrue(any(
                "supported exact capacity classes require 16, 20 or 24"
                in violation
                for violation in result["memory"]["invariant_violations"]
            ))

        scaled_density = {"qualified": True, "gib_per_subpartition": 5}
        scaled = self.xray.normalize_ga100_topology(
            fixture(0x003), 5, scaled_density
        )
        self.assertTrue(scaled["memory"]["target_admissible"])
        self.assertEqual(
            scaled["memory"]["layers"]["defect"]["capacity_gib"], 100
        )
        self.assertEqual(
            scaled["memory"]["supported_exact_capacity_classes_gib"],
            [80, 100, 120],
        )

    def test_compute_ceiling_gates_tpcs_by_gpc_layer(self):
        def reading(value):
            return {"status": "ok", "raw": "0x%08x" % value}

        values = {
            "GPC_DEFECTIVE": reading(0x01),
            "OPT_GPC_DISABLE": reading(0x03),
            "OPT_GPC_DISABLE_CP": reading(0),
            "CTRL_OPT_GPC": reading(0),
            "STATUS_OPT_GPC": reading(0x07),
        }
        for gpc in range(8):
            layer_values = {
                "TPC_GPC%d_DEFECTIVE" % gpc: 0,
                "OPT_TPC_GPC%d_DISABLE" % gpc: 0,
                "OPT_TPC_GPC%d_RECONFIG" % gpc: 0,
                "CTRL_OPT_TPC_GPC%d" % gpc: 0,
                "RECONFIG_OVERRIDE_TPC_GPC%d" % gpc: 0xFF,
                "DISABLE_EXTEND_TPC_GPC%d" % gpc: 0,
                "STATUS_OPT_TPC_GPC%d" % gpc: 0,
            }
            values.update({
                name: reading(value) for name, value in layer_values.items()
            })
        result = self.xray.analyze_tpc_layers(values)
        counts = result["sm_count_if_two_sms_per_tpc"]
        self.assertEqual(counts["defect_ceiling_sms"], 112)
        self.assertEqual(counts["disable_layer_sms"], 96)
        self.assertEqual(counts["effective_sms"], 80)


if __name__ == "__main__":
    unittest.main()
