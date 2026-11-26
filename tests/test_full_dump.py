#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline regression tests for full-dump packaging and verification."""

import importlib.util
import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "170HX-XRAY.py"
NATIVE_CAPTURE = (
    Path(__file__).resolve().parents[2]
    / "research-evidence"
    / "170hx-ray-ai-ws-level3-v3.5.0-2026-11-23"
    / "01-level3-a.json"
)
LEGACY_LAYOUT2_BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "research-evidence"
    / "170hx-ray-ai-ws-full-dump-v3.6.1-2026-11-23"
    / "bundle"
)
RETAINED_EVIDENCE_AVAILABLE = (
    NATIVE_CAPTURE.is_file() and LEGACY_LAYOUT2_BUNDLE.is_dir()
)
SPEC = importlib.util.spec_from_file_location("xray_full_dump_test_target", SCRIPT)
XRAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(XRAY)


def capture_fixture(main=None, rir=None):
    main = list(range(XRAY.MAX_FUSE_ROWS)) if main is None else list(main)
    rir = [0x100 + index for index in range(XRAY.XRAY_RIR_ROWS)] if rir is None else list(rir)
    card = {
        "card": {
            "domain_bdf": "0000:01:00.0",
            "vendor": "0x10de",
            "device": "0x20c2",
            "subsystem_vendor": "0x10de",
            "subsystem_device": "0x1482",
            "revision": "0xa1",
        },
        "capture": {
            "backend": "offline-test-fixture",
            "read_count": 1,
            "coherence": {"coherent": True},
        },
        "registers": {},
        "controller": {},
        "physical_rows": {
            "available": True,
            "coherent": True,
            "passes": [main, main],
            "rows": main,
            "pass_hashes": [
                XRAY._packed_u32_pass_sha256(main),
                XRAY._packed_u32_pass_sha256(main),
            ],
            "certification": {"certified": True},
        },
        "rir_rows": {
            "available": True,
            "coherent": True,
            "passes": [rir, rir],
            "rows": rir,
            "pass_hashes": [
                XRAY._packed_u32_pass_sha256(rir),
                XRAY._packed_u32_pass_sha256(rir),
            ],
        },
        "access": {
            "requested_access": "semantic-read",
            "effective_access": "semantic-read",
            "counts_complete": True,
            "direct_user_mmio_reads": 0,
            "direct_user_mmio_writes": 0,
            "helper_operational_mmio_writes": 0,
            "controller_read_commands_issued": 0,
            "completed_data_reads_lower_bound": 0,
            "pci_config_writes": 0,
            "runtime_pm_writes": 0,
            "otp_program_commands": 0,
        },
    }
    return {
        "schema": XRAY.SCHEMA_VERSION,
        "command": "capture",
        "access": {
            "requested_access": "semantic-read",
            "effective_access": "semantic-read",
            "counts_complete": True,
            "direct_user_mmio_reads": 0,
            "direct_user_mmio_writes": 0,
            "helper_operational_mmio_writes": 0,
            "controller_read_commands_issued": 0,
            "completed_data_reads_lower_bound": 0,
            "pci_config_writes": 0,
            "runtime_pm_writes": 0,
            "otp_program_commands": 0,
        },
        "cards": [card],
        "status": "captured",
    }


class FullDumpTests(unittest.TestCase):
    def write_json(self, directory, name, value):
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def parse_args(self, output, source, *extra):
        argv = [
            "full-dump", str(output), "--no-live-capture",
            "--source", str(source), "--fuseless-start", "0",
            "--fuseless-end", str(XRAY.MAX_FUSE_ROWS - 1),
            "--row-view", "locked_controller",
        ]
        argv.extend(extra)
        return XRAY.build_parser().parse_args(argv)

    @unittest.skipUnless(
        RETAINED_EVIDENCE_AVAILABLE,
        "retained hardware bundles are intentionally outside the source release",
    )
    def test_retained_layout2_bundle_remains_verifiable(self):
        self.assertTrue(LEGACY_LAYOUT2_BUNDLE.is_dir())
        report = XRAY.verify_full_dump_bundle(str(LEGACY_LAYOUT2_BUNDLE))
        self.assertTrue(report["valid"], report)

    @unittest.skipUnless(
        RETAINED_EVIDENCE_AVAILABLE,
        "retained hardware bundles are intentionally outside the source release",
    )
    def test_complete_bundle_exports_every_format_plan_level_and_rir(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(NATIVE_CAPTURE.is_file())
            source = NATIVE_CAPTURE
            output = Path(directory) / "bundle"
            args = self.parse_args(
                output, source, "--include-unsafe-projections"
            )
            self.assertEqual(XRAY.command_full_dump(args), 0)

            report = XRAY.verify_full_dump_bundle(str(output))
            self.assertTrue(report["valid"], report)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["completion"]["status"], "COMPLETE")
            self.assertEqual(index["completion"]["fabricated_rows"], 0)
            self.assertEqual(len(index["specimens"]), 1)
            specimen = index["specimens"][0]
            self.assertEqual(index["layout_version"], 3)
            self.assertEqual(specimen["level_directory"], "imported-001")
            level_prefix = "levels/imported-001/specimens/specimen-001/"
            self.assertTrue(specimen["rir"]["certified_same_specimen"])
            self.assertEqual(
                {item["model"] for item in specimen["planning"]},
                set(XRAY.PLANNING_MODELS),
            )
            for item in specimen["planning"]:
                self.assertEqual(item["status"], "generated_non_executable")
                self.assertIn("apply_dry_run", item)
                self.assertIn("otp_readiness", item)
                self.assertEqual(
                    set(item["transports"]),
                    {
                        "json", "jsonl", "text", "binary_canonical_json",
                        "binary_semantics",
                    },
                )
                for key in ("json", "jsonl", "text", "binary_canonical_json"):
                    self.assertTrue(item["transports"][key].startswith(level_prefix))
                    self.assertTrue((output / item["transports"][key]).is_file())

            self.assertTrue(specimen["register_exports"]["available"])
            self.assertEqual(len(specimen["register_exports"]["formats"]), 4)
            self.assertTrue(all(
                path.startswith(level_prefix)
                for path in specimen["register_exports"]["formats"]
            ))

            source_main = XRAY.load_row_passes(str(source), "0", "json")
            self.assertEqual(len(specimen["main"]["passes"]), 2)
            for pass_index, main_pass in enumerate(specimen["main"]["passes"]):
                expected = source_main["passes"][pass_index]
                outputs = main_pass["outputs"]
                self.assertEqual(
                    {item["format"] for item in outputs},
                    set(XRAY.DUMP_OUTPUT_FORMATS),
                )
                self.assertTrue(
                    all(item["status"] == "exported" for item in outputs)
                )
                for item in outputs:
                    input_format = {
                        "raw-u32le": "raw-u32le",
                        "indexed-text": "indexed-text",
                        "dstoro-jsonl": "jsonl",
                    }.get(item["format"], "auto")
                    loaded = XRAY.load_row_passes(
                        str(output / item["path"]), input_format=input_format
                    )
                    self.assertEqual(loaded["passes"][0], expected)

            source_rir = XRAY.load_rir_words(str(source), "0", "json")
            rir = specimen["rir"]
            self.assertTrue(rir["available"])
            self.assertEqual(rir["pass_count"], 2)
            for pass_entry in rir["passes"]:
                for relative in pass_entry["outputs"]:
                    path = output / relative
                    input_format = (
                        "raw-u32le" if path.suffix == ".bin" else
                        "jsonl" if path.suffix == ".jsonl" else
                        "indexed-text" if path.suffix == ".txt" else "auto"
                    )
                    loaded = XRAY.load_rir_words(str(path), input_format=input_format)
                    self.assertEqual(
                        loaded["words"],
                        source_rir["words"],
                    )

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for path in output.rglob("*"):
                mode = stat.S_IMODE(path.lstat().st_mode)
                self.assertEqual(mode, 0o700 if path.is_dir() else 0o600)

    def test_default_policy_does_not_emit_actionable_projections(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            self.assertEqual(
                XRAY.command_full_dump(self.parse_args(output, source)), 0
            )
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            outputs = index["specimens"][0]["main"]["passes"][0]["outputs"]
            by_format = {item["format"]: item for item in outputs}
            for output_format in XRAY._UNSAFE_PROJECTION_OUTPUTS:
                self.assertEqual(
                    by_format[output_format]["status"],
                    "not_requested_policy_gate",
                )
                self.assertIsNone(by_format[output_format]["path"])
            self.assertEqual(index["completion"]["status"], "COMPLETE")

    def test_existing_output_is_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            output.mkdir()
            marker = output / "keep"
            marker.write_text("unchanged", encoding="utf-8")
            with self.assertRaisesRegex(XRAY.AuditError, "refusing to overwrite"):
                XRAY.command_full_dump(self.parse_args(output, source))
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_tamper_and_manifest_malformed_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            self.assertEqual(
                XRAY.command_full_dump(self.parse_args(output, source)), 0
            )
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            rogue = output / "unindexed.txt"
            rogue.write_text("not indexed", encoding="utf-8")
            os.chmod(str(rogue), 0o600)
            manifest = output / "SHA256SUMS"
            manifest.write_text(XRAY._full_dump_manifest(output), encoding="utf-8")
            os.chmod(str(manifest), 0o600)
            rogue_report = XRAY.verify_full_dump_bundle(str(output))
            self.assertFalse(rogue_report["valid"])
            self.assertTrue(any(
                "index/manifest artifact-set mismatch" in error
                for error in rogue_report["errors"]
            ))
            raw = next(
                item for item in index["artifacts"]
                if item["kind"] == "main-raw-u32le"
            )
            path = output / raw["path"]
            path.write_bytes(path.read_bytes() + b"tamper")
            self.assertFalse(XRAY.verify_full_dump_bundle(str(output))["valid"])

            manifest.write_text("not a manifest\n", encoding="utf-8")
            with self.assertRaisesRegex(XRAY.AuditError, "malformed SHA256SUMS"):
                XRAY.verify_full_dump_bundle(str(output))

    def test_resealed_semantic_and_cross_format_tampering_is_rejected(self):
        def reseal(bundle, index):
            index_path = bundle / "index.json"
            index_path.write_text(
                json.dumps(index, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(str(index_path), 0o600)
            manifest = bundle / "SHA256SUMS"
            manifest.write_text(XRAY._full_dump_manifest(bundle), encoding="utf-8")
            os.chmod(str(manifest), 0o600)

        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            self.assertEqual(XRAY.command_full_dump(self.parse_args(output, source)), 0)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            index["completion"].update({
                "status": "GO_AUTHORITATIVE",
                "requested_failures": 999,
                "unavailable_records": 1,
            })
            index["request"]["max_access"] = "otp-write"
            index["request"]["writes_requested"] = True
            index["safety_boundary"] = "tampered authority claim"
            index["unavailable"] = [{
                "required_for_requested_bundle": True,
                "reason": "deliberate contradiction",
            }]
            reseal(output, index)
            report = XRAY.verify_full_dump_bundle(str(output))
            self.assertFalse(report["valid"])
            self.assertTrue(any(
                "completion status" in error or "writes_requested" in error
                for error in report["errors"]
            ))

        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            self.assertEqual(XRAY.command_full_dump(self.parse_args(output, source)), 0)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            raw_record = next(
                item for item in index["artifacts"]
                if item["kind"] == "main-raw-u32le"
            )
            raw_path = output / raw_record["path"]
            payload = bytearray(raw_path.read_bytes())
            payload[0] ^= 1
            raw_path.write_bytes(payload)
            os.chmod(str(raw_path), 0o600)
            raw_record["sha256"] = hashlib.sha256(payload).hexdigest()
            raw_record["size"] = len(payload)
            reseal(output, index)
            report = XRAY.verify_full_dump_bundle(str(output))
            self.assertFalse(report["valid"])
            self.assertTrue(any(
                "rows differ from canonical source" in error
                for error in report["errors"]
            ))

    def test_unbound_single_pass_rir_cannot_feed_unsafe_combined_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            fake_rir = Path(directory) / "fake-rir.bin"
            fake_rir.write_bytes(b"".join(
                int(0xDEAD0000 + index).to_bytes(4, "little")
                for index in range(XRAY.XRAY_RIR_ROWS)
            ))
            output = Path(directory) / "bundle"
            args = self.parse_args(
                output, source,
                "--rir-input", str(fake_rir),
                "--rir-input-format", "raw-u32le",
                "--include-unsafe-projections",
            )
            self.assertEqual(XRAY.command_full_dump(args), 2)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["completion"]["status"], "PARTIAL")
            specimen = index["specimens"][0]
            self.assertFalse(specimen["rir"]["coherent_repeated_passes"])
            self.assertFalse(specimen["rir"]["certified_same_specimen"])
            dstoro = [
                output_item
                for main_pass in specimen["main"]["passes"]
                for output_item in main_pass["outputs"]
                if output_item["format"] == "dstoro-projection-json"
            ]
            self.assertTrue(dstoro)
            self.assertTrue(all(item["status"] == "unavailable" for item in dstoro))
            self.assertTrue(XRAY.verify_full_dump_bundle(str(output))["valid"])

    def test_malformed_source_is_preserved_but_never_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "malformed.json"
            source.write_text('{"rows": [1,', encoding="utf-8")
            output = Path(directory) / "bundle"
            self.assertEqual(
                XRAY.command_full_dump(self.parse_args(output, source)), 2
            )
            report = XRAY.verify_full_dump_bundle(str(output))
            self.assertTrue(report["valid"], report)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["completion"]["status"], "PARTIAL")
            self.assertGreater(index["completion"]["requested_failures"], 0)
            self.assertFalse(index["specimens"][0]["main"]["available"])
            self.assertFalse(any(
                item["kind"] == "main-raw-u32le" for item in index["artifacts"]
            ))

    def test_fatal_finalization_failure_leaves_explicit_incomplete_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            output = Path(directory) / "bundle"
            original = XRAY._full_dump_manifest

            def injected_failure(_root):
                raise XRAY.AuditError("injected finalization failure")

            XRAY._full_dump_manifest = injected_failure
            try:
                with self.assertRaisesRegex(XRAY.AuditError, "injected"):
                    XRAY.command_full_dump(self.parse_args(output, source))
            finally:
                XRAY._full_dump_manifest = original
            failure = json.loads(
                (output / "failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failure["status"], "INCOMPLETE")
            self.assertFalse((output / "SHA256SUMS").exists())
            self.assertEqual(
                stat.S_IMODE((output / "failure.json").stat().st_mode), 0o600
            )

    def test_sparse_partial_bundle_keeps_unknown_rows_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "sparse.json", {"rows": {"7": 9}})
            output = Path(directory) / "bundle"
            self.assertEqual(
                XRAY.command_full_dump(self.parse_args(output, source)), 2
            )
            self.assertTrue(XRAY.verify_full_dump_bundle(str(output))["valid"])
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            outputs = index["specimens"][0]["main"]["passes"][0]["outputs"]
            exported = {item["format"] for item in outputs if item["status"] == "exported"}
            self.assertIn("xray-json", exported)
            self.assertIn("indexed-text", exported)
            self.assertNotIn("raw-u32le", exported)
            portable = next(
                item for item in outputs if item["format"] == "xray-json"
            )
            document = json.loads(
                (output / portable["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(document["main_rows"], {"7": "0x00000009"})
            self.assertEqual(document["source_dump"]["missing_rows"][0], 0)

    def test_candidate_binding_is_hash_only_and_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", capture_fixture())
            candidate = Path(directory) / "candidate.py"
            marker = Path(directory) / "must-not-exist"
            candidate.write_text(
                "from pathlib import Path\nPath(%r).write_text('executed')\n" % str(marker),
                encoding="utf-8",
            )
            output = Path(directory) / "bundle"
            args = self.parse_args(
                output, source, "--candidate-planner", str(candidate)
            )
            self.assertEqual(XRAY.command_full_dump(args), 0)
            self.assertFalse(marker.exists())
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            binding = index["bindings"]["candidate_planners"][0]
            self.assertFalse(binding["executed"])
            self.assertEqual(binding["sha256"], XRAY.sha256_file(str(candidate)))

    def test_native_rir_redundancy_is_cross_checked(self):
        value = capture_fixture()
        contradictory = list(value["cards"][0]["rir_rows"]["rows"])
        contradictory[0] ^= 1
        value["cards"][0]["rir_rows"]["rows"] = contradictory
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", value)
            with self.assertRaisesRegex(XRAY.AuditError, "disagree"):
                XRAY.load_rir_words(str(source), "0")

    def test_explicitly_unavailable_rir_preserves_source_reason(self):
        value = capture_fixture()
        value["cards"][0]["rir_rows"] = {
            "available": False,
            "reason": "controller transaction was not permitted at this level",
        }
        with tempfile.TemporaryDirectory() as directory:
            source = self.write_json(directory, "capture.json", value)
            with self.assertRaisesRegex(
                XRAY.AuditError,
                "explicitly unavailable: controller transaction was not permitted",
            ):
                XRAY.load_rir_words(str(source), "0")
            output = Path(directory) / "bundle"
            self.assertEqual(
                XRAY.command_full_dump(self.parse_args(output, source)), 2
            )
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            reasons = [
                item["reason"] for item in index["unavailable"]
                if item["category"] == "rir-export"
            ]
            self.assertEqual(len(reasons), 1)
            self.assertIn("controller transaction was not permitted", reasons[0])
            self.assertFalse(any(
                item["kind"].startswith("rir-") for item in index["artifacts"]
            ))

    def test_parser_defaults_to_passive_ceiling(self):
        parsed = XRAY.build_parser().parse_args(["full-dump", "new-bundle"])
        self.assertEqual(parsed.max_access, "passive")
        self.assertEqual(parsed.controller_helper, "/dev/170hx_xray")

    def test_live_orchestration_never_exceeds_explicit_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            calls = []
            original = XRAY.command_live

            def offline_capture(args):
                calls.append(args.access)
                value = capture_fixture()
                value["access"]["requested_access"] = args.access
                value["access"]["effective_access"] = args.access
                value["cards"][0]["access"]["requested_access"] = args.access
                value["cards"][0]["access"]["effective_access"] = args.access
                XRAY.emit_json(value, args.output)
                return 0

            XRAY.command_live = offline_capture
            try:
                args = XRAY.build_parser().parse_args([
                    "full-dump", str(output),
                    "--max-access", "bar-read",
                    "--no-driver-queries",
                ])
                self.assertEqual(XRAY.command_full_dump(args), 0)
            finally:
                XRAY.command_live = original
            self.assertEqual(calls, ["passive", "bar-read"])
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [item["requested_access"] for item in index["observations"]],
                ["passive", "bar-read"],
            )
            self.assertTrue(index["operational_effects"]["counts_complete"])
            self.assertEqual(index["completion"]["writes_performed"], 0)

    def test_live_operational_effects_are_aggregated_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            original = XRAY.command_live
            counters = {
                "passive": (0, 0, 0),
                "bar-read": (11, 0, 0),
                "semantic-read": (13, 2084, 1040),
            }

            def offline_capture(args):
                value = capture_fixture()
                reads, writes, commands = counters[args.access]
                value["access"].update({
                    "requested_access": args.access,
                    "effective_access": args.access,
                    "direct_user_mmio_reads": reads,
                    "direct_user_mmio_writes": writes,
                    "helper_operational_mmio_writes": writes,
                    "controller_read_commands_issued": commands,
                    "completed_data_reads_lower_bound": commands,
                })
                value["cards"][0]["access"]["requested_access"] = args.access
                value["cards"][0]["access"]["effective_access"] = args.access
                XRAY.emit_json(value, args.output)
                return 0

            XRAY.command_live = offline_capture
            try:
                args = XRAY.build_parser().parse_args([
                    "full-dump", str(output),
                    "--max-access", "semantic-read",
                    "--no-driver-queries",
                ])
                self.assertEqual(XRAY.command_full_dump(args), 0)
            finally:
                XRAY.command_live = original
            index_path = output / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["layout_version"], 3)
            effects = index["operational_effects"]
            self.assertEqual(effects["direct_user_mmio_reads"], 24)
            self.assertEqual(effects["direct_user_mmio_writes"], 2084)
            self.assertEqual(effects["controller_read_commands"], 1040)
            self.assertEqual(effects["otp_program_commands"], 0)
            self.assertTrue(effects["counts_complete"])
            self.assertEqual(index["completion"]["writes_performed"], 2084)
            self.assertEqual(
                index["completion"]["programming_writes_performed"], 0
            )
            self.assertEqual(index["safety"]["controller_read_commands"], 1040)
            self.assertTrue(XRAY.verify_full_dump_bundle(str(output))["valid"])

            index["completion"]["writes_performed"] = 0
            index_path.write_text(
                json.dumps(index, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = output / "SHA256SUMS"
            manifest.write_text(XRAY._full_dump_manifest(output), encoding="utf-8")
            os.chmod(str(manifest), 0o600)
            report = XRAY.verify_full_dump_bundle(str(output))
            self.assertFalse(report["valid"])
            self.assertIn(
                "layout-v3 completion counter mismatch: writes_performed",
                report["errors"],
            )

    def test_source_uses_python_39_grammar(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 9),
        )


if __name__ == "__main__":
    unittest.main()
