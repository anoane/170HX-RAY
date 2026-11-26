#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline integrity tests for standalone planner evidence."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
TOOL_PATH = ROOT / "tools" / "170hx_candidate_plan.py"
NATIVE_CAPTURE = (
    WORKSPACE / "research-evidence" /
    "170hx-ray-ai-ws-full-dump-v3.6.1-2026-11-23" / "bundle" /
    "observations" / "observation-003" / "capture.json"
)
LEGACY_LIVE = (
    WORKSPACE / "research-evidence" /
    "170hx-ray-ai-ws-live-3.4.0-2026-11-23" /
    "level2-all-safe-exact.json"
)
EXTERNAL_EVIDENCE_AVAILABLE = NATIVE_CAPTURE.is_file() and LEGACY_LIVE.is_file()


def load_module():
    spec = importlib.util.spec_from_file_location(
        "xray_candidate_bundle_test", TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_module()


class CandidateBundleTests(unittest.TestCase):
    @unittest.skipUnless(
        EXTERNAL_EVIDENCE_AVAILABLE,
        "retained hardware evidence is intentionally outside the source release",
    )
    def test_declared_formats_are_enforced_before_legacy_normalization(self):
        xray = TOOL.load_python_module(
            ROOT / "170HX-XRAY.py", "xray_candidate_format_test_decoder"
        )
        with self.assertRaisesRegex(TOOL.PlanFailure, "does not match"):
            TOOL.validate_physical_capture(
                xray, NATIVE_CAPTURE, "0000:01:00.0",
                declared_input_format="xray-native-level3-json",
            )
        with self.assertRaisesRegex(TOOL.PlanFailure, "does not match"):
            TOOL.validate_live_capture(
                LEGACY_LIVE, xray, declared_input_format="xray-level2-json"
            )
        physical = TOOL.validate_physical_capture(
            xray, NATIVE_CAPTURE, "0000:01:00.0",
            declared_input_format="auto",
        )
        self.assertEqual(
            physical["input_format"],
            {
                "declared": "auto",
                "detected": "legacy-ray-native-level3-json",
                "legacy_normalization_applied": True,
            },
        )

    @unittest.skipUnless(
        EXTERNAL_EVIDENCE_AVAILABLE,
        "retained hardware evidence is intentionally outside the source release",
    )
    def test_cli_output_format_export_all_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "candidate.txt"
            exports = root / "all"
            self.assertEqual(TOOL.main([
                "--root", str(WORKSPACE),
                "--physical-capture", str(NATIVE_CAPTURE),
                "--physical-card", "0000:01:00.0",
                "--same-capture-context",
                "--profile", "review-ss0-iff",
                "--output", str(primary),
                "--output-format", "text",
                "--export-all-dir", str(exports),
            ]), 0)
            report = TOOL.deserialize_plan_export(primary.read_bytes(), "text")
            self.assertEqual(
                report["cards"][0]["physical_capture"]["input_format"]["detected"],
                "legacy-ray-native-level3-json",
            )
            self.assertEqual(len(list(exports.iterdir())), 4)
            collision = root / "collision"
            with self.assertRaisesRegex(TOOL.PlanFailure, "collide"):
                TOOL.main([
                    "--root", str(WORKSPACE),
                    "--physical-dir", "unused",
                    "--same-capture-context",
                    "--output", str(collision / "candidate-plan.json"),
                    "--export-all-dir", str(collision),
                ])

    @unittest.skipUnless(
        EXTERNAL_EVIDENCE_AVAILABLE,
        "retained hardware evidence is intentionally outside the source release",
    )
    def test_cli_rejects_explicit_current_label_for_legacy_physical_input(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TOOL.PlanFailure, "does not match"):
                TOOL.main([
                    "--root", str(WORKSPACE),
                    "--physical-capture", str(NATIVE_CAPTURE),
                    "--physical-card", "0000:01:00.0",
                    "--physical-input-format", "xray-native-level3-json",
                    "--same-capture-context",
                    "--profile", "review-ss0-iff",
                    "--output", str(Path(directory) / "unused.json"),
                ])

    def test_plan_export_formats_are_lossless_and_not_otp_images(self):
        document = {
            "schema": TOOL.SCHEMA,
            "execution_authorized": False,
            "executable": False,
            "nested": {"z": 2, "a": [1, "two"]},
        }
        self.assertEqual(
            TOOL.PLAN_OUTPUT_FORMATS,
            ("json", "jsonl", "text", "canonical-json-bin"),
        )
        for output_format in TOOL.PLAN_OUTPUT_FORMATS:
            with self.subTest(output_format=output_format):
                payload = TOOL.serialize_plan_export(document, output_format)
                self.assertEqual(
                    TOOL.deserialize_plan_export(payload, output_format), document
                )
        binary = TOOL.serialize_plan_export(document, "canonical-json-bin")
        self.assertTrue(binary.startswith(b"{"))
        self.assertNotIn(b"OTP", binary)

    def test_export_all_is_create_only_and_complete(self):
        document = {
            "schema": TOOL.SCHEMA,
            "execution_authorized": False,
            "executable": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            export_dir = Path(directory) / "exports"
            paths = TOOL.write_all_plan_exports(export_dir, document)
            self.assertEqual(set(paths), set(TOOL.PLAN_OUTPUT_FORMATS))
            self.assertEqual(export_dir.stat().st_mode & 0o777, 0o700)
            for output_format, path in paths.items():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    TOOL.deserialize_plan_export(path.read_bytes(), output_format),
                    document,
                )
            with self.assertRaises(TOOL.PlanFailure):
                TOOL.write_all_plan_exports(export_dir, document)

    def test_export_all_rejects_symlink_directory(self):
        document = {"schema": TOOL.SCHEMA}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(TOOL.PlanFailure, "real directory"):
                TOOL.write_all_plan_exports(alias, document)

    def test_plan_export_parsers_reject_ambiguous_containers(self):
        duplicate = (
            '{"schema":"%s","duplicate":1,"duplicate":2}' % TOOL.SCHEMA
        ).encode("ascii")
        for output_format in ("json", "canonical-json-bin"):
            with self.subTest(output_format=output_format):
                with self.assertRaisesRegex(TOOL.PlanFailure, "duplicate"):
                    TOOL.deserialize_plan_export(duplicate, output_format)
        one = TOOL.serialize_plan_export({"schema": TOOL.SCHEMA}, "jsonl")
        with self.assertRaisesRegex(TOOL.PlanFailure, "exactly one record"):
            TOOL.deserialize_plan_export(one + one, "jsonl")
        with self.assertRaisesRegex(TOOL.PlanFailure, "markers"):
            TOOL.deserialize_plan_export(b"not a marked plan", "text")
        duplicate_text = (
            "heading\n%s\n%s\n%s\n" % (
                TOOL.PLAN_TEXT_BEGIN, duplicate.decode("ascii"),
                TOOL.PLAN_TEXT_END,
            )
        ).encode("utf-8")
        with self.assertRaisesRegex(TOOL.PlanFailure, "duplicate"):
            TOOL.deserialize_plan_export(duplicate_text, "text")

    def test_cli_rejects_live_format_with_same_capture_context(self):
        with self.assertRaisesRegex(TOOL.PlanFailure, "inapplicable"):
            TOOL.main([
                "--physical-dir", "unused",
                "--same-capture-context",
                "--live-input-format", "xray-level2-json",
                "--output", "unused.json",
            ])

    def test_input_formats_and_review_only_profiles_are_explicit(self):
        self.assertEqual(
            TOOL.PHYSICAL_INPUT_FORMATS,
            ("auto", "xray-native-level3-json"),
        )
        self.assertEqual(
            TOOL.LIVE_INPUT_FORMATS, ("auto", "xray-level2-json")
        )
        review_ids = {
            "review-ss0-iff", "review-ss1-iff",
            "review-dp-conventional", "review-ecc-conventional",
            "review-nvenc-unsupported",
            "review-nvlink-per-link-unsupported",
        }
        profiles = {item["id"]: item for item in TOOL.PROFILE_SPECS}
        self.assertTrue(review_ids.issubset(profiles))
        for profile_id in review_ids:
            profile = profiles[profile_id]
            self.assertEqual(profile["stages"], [])
            self.assertEqual(
                profile["review_only"]["operation_count_required"], 0
            )
            self.assertFalse(profile["review_only"]["compiler_eligible"])
            self.assertFalse(profile["review_only"]["runtime_eligible"])

        nvenc = profiles["review-nvenc-unsupported"]["review_only"][
            "subjects"
        ][0]
        self.assertEqual(nvenc["option_control_defect_status_width_bits"], 2)
        self.assertEqual(nvenc["instance_count_register_width_bits"], 5)
        self.assertTrue(nvenc["ptop_instance_count_is_inventory_only"])
        self.assertEqual(
            nvenc["direct_registers"],
            [
                "OPT_NVENC_DISABLE", "OPT_NVENC_DISABLE_CP",
                "OPT_NVENC_DEFECTIVE", "CTRL_OPT_NVENC",
                "STATUS_OPT_NVENC", "PTOP_SCAL_NUM_NVENCS",
            ],
        )

    def test_bundled_public_sources_and_audit_are_hash_bound(self):
        bundled = [ROOT / relative for relative in
                   TOOL.BUNDLED_PUBLIC_SOURCE_PATHS.values()]
        bundled.append(ROOT / "evidence/public/public-plan-audit.json")
        if not all(path.is_file() for path in bundled):
            self.skipTest(
                "third-party public-source fixtures are not redistributed"
            )
        public_hashes = {}
        for name, relative in TOOL.BUNDLED_PUBLIC_SOURCE_PATHS.items():
            path = ROOT / relative
            self.assertTrue(path.is_file())
            digest = TOOL.sha256_file(path)
            self.assertEqual(
                digest, TOOL.PUBLIC_SOURCE_METADATA[name]["expected_sha256"]
            )
            public_hashes[name] = {"sha256": digest}
        with tempfile.TemporaryDirectory() as directory:
            result = TOOL.validate_public_faithful_audit(
                Path(directory), public_hashes
            )
        self.assertEqual(result["path"], "evidence/public/public-plan-audit.json")
        self.assertTrue(result["all_models_pass"])
        self.assertTrue(result["lineage_hashes_match"])

    def test_hash_only_public_lineage_is_complete_and_non_authorizing(self):
        path, document = TOOL.load_bundled_public_lineage(ROOT)
        self.assertEqual(
            path, ROOT / "evidence/public/public-source-lineage.json"
        )
        self.assertFalse(document["raw_sources_redistributed"])
        self.assertFalse(document["execution_authorized"])
        self.assertEqual(set(document["sources"]),
                         set(TOOL.PUBLIC_SOURCE_METADATA))
        public_hashes = {
            name: {"sha256": source["source_sha256"]}
            for name, source in document["sources"].items()
        }
        with tempfile.TemporaryDirectory() as directory:
            result = TOOL.validate_public_faithful_audit(
                Path(directory), public_hashes
            )
        self.assertTrue(result["all_models_pass"])
        self.assertTrue(result["lineage_hashes_match"])


if __name__ == "__main__":
    unittest.main()
