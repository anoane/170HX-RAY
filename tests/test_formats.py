#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for strict 170HX-XRAY dump parsing and conversion."""

import argparse
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "170HX-XRAY.py"
NATIVE_CAPTURE = (
    Path(__file__).resolve().parents[2]
    / "research-evidence"
    / "170hx-ray-ai-ws-level3-v3.5.0-2026-11-23"
    / "01-level3-a.json"
)
SPEC = importlib.util.spec_from_file_location("xray_format_test_target", SCRIPT)
XRAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(XRAY)


def danylo_backup():
    main = list(range(XRAY.MAX_FUSE_ROWS))
    rir = [0x100 + index for index in range(XRAY.XRAY_RIR_ROWS)]
    return {
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
        "main": main,
        "rir": rir,
        "complete": True,
        "shadow_matches_main": True,
        "main_hash": XRAY._packed_u32_pass_sha256(main),
        "rir_hash": XRAY._packed_u32_pass_sha256(rir),
        "rir_observation": {"fresh_readback": True},
        "collector_sha256": "c" * 64,
    }


class FormatHardeningTests(unittest.TestCase):
    def write_json(self, directory, name, value):
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_full_report_frontends_select_disjoint_modes(self):
        parser = XRAY.build_parser()
        fast = parser.parse_args(["full-report", "fast-report"])
        self.assertIs(fast.handler, XRAY.command_full_report)
        benchmark = parser.parse_args(["full-report-bench", "bench-report"])
        self.assertIs(benchmark.handler, XRAY.command_full_report_bench)
        alias = parser.parse_args(["full-report+bench", "bench-alias"])
        self.assertIs(alias.handler, XRAY.command_full_report_bench)

    def conversion_args(self, input_path, output_path, **overrides):
        values = {
            "input": str(input_path),
            "input_format": "auto",
            "card": None,
            "pass_index": 1,
            "rir_input": None,
            "rir_input_format": "auto",
            "fuseless_start": None,
            "fuseless_end": None,
            "sku": "8gb",
            "bdf": None,
            "row_view": "unknown",
            "provenance": "format regression test",
            "output": str(output_path),
            "allow_unsafe_projection": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_numeric_alias_duplicates_fail_closed(self):
        with self.assertRaises(XRAY.AuditError):
            XRAY._parse_row_container({"1": 0x11, "0x1": 0x22}, "test")

    def test_literal_duplicate_json_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"rows":{"1":17,"1":34}}', encoding="utf-8"
            )
            with self.assertRaisesRegex(XRAY.AuditError, "duplicate key"):
                XRAY.load_row_passes(str(path))
        with self.assertRaisesRegex(XRAY.AuditError, "duplicate key"):
            XRAY._parse_jsonl_rows('{"row":1,"row":2,"value":3}\n')

    def test_nonfinite_json_is_rejected_on_input_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonfinite.json"
            path.write_text('{"unused":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(XRAY.AuditError, "non-finite"):
                XRAY.load_json_artifact(str(path))
        with self.assertRaises(ValueError):
            XRAY._strict_json_dumps({"unused": float("nan")})

    def test_legacy_identity_normalization_is_narrow_and_collision_safe(self):
        legacy = {
            "schema": "170hx-ray-v1",
            "tool": {"name": "170HX-RAY", "version": "3.6.1"},
            "ray_helper_identity": {"backend":
                "170hx-ray-helper-allowlisted-read32"},
            "source": "research-evidence/170hx-ray-capture.json",
            "note": "170HX-RAY is historical prose",
        }
        normalized = XRAY._normalize_legacy_xray_document(legacy)
        self.assertEqual(normalized["schema"], XRAY.SCHEMA_VERSION)
        self.assertEqual(normalized["tool"]["name"], XRAY.LEGACY_TOOL_NAME)
        self.assertEqual(normalized["tool"]["version"], "3.6.1")
        self.assertIn("xray_helper_identity", normalized)
        self.assertEqual(
            normalized["xray_helper_identity"]["backend"],
            "170hx-xray-helper-allowlisted-read32",
        )
        self.assertEqual(normalized["source"], legacy["source"])
        self.assertEqual(normalized["note"], legacy["note"])
        with self.assertRaisesRegex(XRAY.AuditError, "collides"):
            XRAY._normalize_legacy_xray_document({
                "ray_helper_identity": {}, "xray_helper_identity": {},
            })

    def test_nested_and_top_level_container_ambiguity_fail_closed(self):
        with self.assertRaises(XRAY.AuditError):
            XRAY._parse_row_container({"rows": [1], "main": [1]}, "test")
        with self.assertRaises(XRAY.AuditError):
            XRAY._reject_ambiguous_main_containers({"rows": [1], "ff_rows": [1]})
        with self.assertRaises(XRAY.AuditError):
            XRAY._reject_ambiguous_rir_containers({"rir": [1], "rir_rows": [1]})

    def test_portable_redundancy_is_cross_checked(self):
        portable = {
            "artifact_schema": "170hx-xray-portable-dump-v1",
            "source_dump": {"selected_pass": 1},
            "passes": [{"0": "0x1"}],
            "main_rows": {"0": "0x1"},
        }
        XRAY._reject_ambiguous_main_containers(portable)
        portable["main_rows"]["0"] = "0x2"
        with self.assertRaises(XRAY.AuditError):
            XRAY._reject_ambiguous_main_containers(portable)

    def test_indexed_text_is_strict(self):
        self.assertEqual(XRAY._parse_text_rows("[001] 0x11\n"), [{1: 0x11}])
        for malformed in (
            "[001] 0x11 trailing\n",
            "garbage\n[001] 0x11\n",
            "[INFO]\n[001] 0x11\n",
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(XRAY.AuditError):
                    XRAY._parse_text_rows(malformed)
        self.assertEqual(
            XRAY._parse_text_rows(
                "[INFO] acquisition starts\n[001] 0x11\n",
                allow_log_messages=True,
            ),
            [{1: 0x11}],
        )

    def test_empty_embedded_rir_is_controlled_error(self):
        with self.assertRaises(XRAY.AuditError):
            XRAY.load_embedded_rir({
                "source": "test",
                "sha256": "0" * 64,
                "selected_capture": {"rir_rows": {"passes": []}},
            })

    def test_claimed_danylo_bad_digest_is_not_imported(self):
        value = danylo_backup()
        profile = XRAY._dstoro_backup_profile(value)
        self.assertTrue(profile["content_integrity_valid"])
        value["main_hash"] = "0" * 64
        damaged = XRAY._dstoro_backup_profile(value)
        self.assertTrue(damaged["matches_profile"])
        self.assertFalse(damaged["content_integrity_valid"])
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "damaged.json", value)
            with self.assertRaisesRegex(XRAY.AuditError, "content-integrity"):
                XRAY.load_row_passes(str(path))
            with self.assertRaisesRegex(XRAY.AuditError, "content-integrity"):
                XRAY.load_rir_words(str(path))

    def test_malformed_ffcalc_option_words_fail_closed(self):
        for option_words in ([], {"x": True}, {" x": 1}, {"x": 1 << 32}):
            source = {"ff_rows": {"0": "0x1"}, "option_words": option_words}
            with self.subTest(option_words=option_words):
                with self.assertRaises(XRAY.AuditError):
                    XRAY._parse_ffcalc_option_words(source)

    def test_ffcalc_option_words_semantic_round_trip(self):
        source = {
            "sku": "8gb",
            "ff_rows": {
                str(index): XRAY.hex32(index)
                for index in range(XRAY.MAX_FUSE_ROWS)
            },
            "option_words": {
                "gpc_disable": "0x1",
                "pcie_magic_gen4_dis": "0x1",
                "future_unknown_word": "0xa5a5a5a5",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source_path = self.write_json(directory, "ffcalc.json", source)
            xray_path = Path(directory) / "portable.json"
            args = self.conversion_args(source_path, xray_path)
            args.output_format = "xray-json"
            self.assertEqual(XRAY.command_convert_dump(args), 0)

            result_path = Path(directory) / "roundtrip.json"
            args = self.conversion_args(
                xray_path,
                result_path,
                fuseless_start=0,
                fuseless_end=XRAY.MAX_FUSE_ROWS - 1,
                allow_unsafe_projection=True,
            )
            args.output_format = "ffcalc-json"
            self.assertEqual(XRAY.command_convert_dump(args), 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(
                result["option_words"],
                {
                    "future_unknown_word": "0xa5a5a5a5",
                    "gpc_disable": "0x00000001",
                    "pcie_magic_gen4_dis": "0x00000001",
                },
            )
            self.assertEqual(
                result["loss_report"]["omitted_ffcalc_option_words"], []
            )

    def test_raw_and_indexed_export_import_round_trip(self):
        source = {"rows": list(range(XRAY.MAX_FUSE_ROWS))}
        with tempfile.TemporaryDirectory() as directory:
            source_path = self.write_json(directory, "rows.json", source)
            for output_format, input_format, suffix in (
                ("indexed-text", "indexed-text", ".txt"),
                ("raw-u32le", "raw-u32le", ".bin"),
            ):
                with self.subTest(output_format=output_format):
                    output = Path(directory) / ("rows" + suffix)
                    args = self.conversion_args(source_path, output)
                    args.output_format = output_format
                    self.assertEqual(XRAY.command_convert_dump(args), 0)
                    loaded = XRAY.load_row_passes(
                        str(output), input_format=input_format
                    )
                    self.assertEqual(
                        loaded["passes"][0],
                        {index: index for index in range(XRAY.MAX_FUSE_ROWS)},
                    )

    def test_every_declared_output_reimports_its_main_rows(self):
        source = danylo_backup()
        expected = {index: index for index in range(XRAY.MAX_FUSE_ROWS)}
        cases = (
            ("xray-json", "auto", ".json"),
            ("indexed-text", "indexed-text", ".txt"),
            ("raw-u32le", "raw-u32le", ".bin"),
            ("ffcalc-json", "auto", ".json"),
            ("xpatrik-json", "auto", ".json"),
            ("ga100-fuse-re-json", "auto", ".json"),
            ("dstoro-jsonl", "jsonl", ".jsonl"),
        )
        with tempfile.TemporaryDirectory() as directory:
            source_path = self.write_json(directory, "backup.json", source)
            for ordinal, (output_format, input_format, suffix) in enumerate(cases):
                with self.subTest(output_format=output_format):
                    output = Path(directory) / ("out-%d%s" % (ordinal, suffix))
                    args = self.conversion_args(
                        source_path,
                        output,
                        fuseless_start=0,
                        fuseless_end=XRAY.MAX_FUSE_ROWS - 1,
                        allow_unsafe_projection=True,
                    )
                    args.output_format = output_format
                    self.assertEqual(XRAY.command_convert_dump(args), 0)
                    loaded = XRAY.load_row_passes(
                        str(output), input_format=input_format
                    )
                    self.assertEqual(loaded["passes"][0], expected)

            refused = Path(directory) / "unbound-dstoro-projection.json"
            args = self.conversion_args(
                source_path,
                refused,
                fuseless_start=0,
                fuseless_end=XRAY.MAX_FUSE_ROWS - 1,
                allow_unsafe_projection=True,
            )
            args.output_format = "dstoro-projection-json"
            with self.assertRaisesRegex(
                XRAY.AuditError, "certified same-artifact"
            ):
                XRAY.command_convert_dump(args)

            # Retained card captures are deliberately excluded from the source
            # release.  Exercise this additional native-attestation round trip
            # only when the separate research-evidence workspace is present;
            # all format/gating checks above remain self-contained.
            if NATIVE_CAPTURE.is_file():
                native_output = Path(directory) / "native-dstoro-projection.json"
                args = self.conversion_args(
                    NATIVE_CAPTURE,
                    native_output,
                    card="0",
                    fuseless_start=222,
                    fuseless_end=511,
                    allow_unsafe_projection=True,
                )
                args.output_format = "dstoro-projection-json"
                self.assertEqual(XRAY.command_convert_dump(args), 0)
                native_source = XRAY.load_row_passes(
                    str(NATIVE_CAPTURE), "0", "json"
                )
                native_loaded = XRAY.load_row_passes(
                    str(native_output), input_format="json"
                )
                self.assertEqual(
                    native_loaded["passes"][0], native_source["passes"][0]
                )

    def test_deterministic_row_parser_properties(self):
        rng = random.Random(0x170)
        for case in range(200):
            indexes = rng.sample(range(XRAY.MAX_FUSE_ROWS), rng.randint(1, 32))
            expected = {index: rng.getrandbits(32) for index in indexes}
            mapping = {
                (hex(index) if position % 2 else str(index)): value
                for position, (index, value) in enumerate(expected.items())
            }
            with self.subTest(case=case):
                self.assertEqual(
                    XRAY._parse_row_container(mapping, "property mapping"), expected
                )
        for index in range(64):
            with self.subTest(alias=index):
                with self.assertRaises(XRAY.AuditError):
                    XRAY._parse_row_container(
                        {str(index): 1, hex(index): 2}, "property alias"
                    )


if __name__ == "__main__":
    unittest.main()
