#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hardware-inert tests for the full-report orchestrator."""

import importlib.util
import copy
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "170hx_full_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("xray_full_report_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_module()


class FakeRunner(object):
    def __init__(self):
        self.calls = []

    def run(self, argv, cwd=None, timeout=None):
        del cwd, timeout
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        stdout = b"fixture\n"
        if any(value.startswith("--query-gpu=pci.bus_id,power.limit")
               for value in argv):
            stdout = b"00000000:01:00.0, 150.0, 100.0, 250.0\n"
        return {
            "argv": argv,
            "command": TOOL.command_text(argv),
            "started_utc": "2026-11-26T00:00:00+00:00",
            "finished_utc": "2026-11-26T00:00:01+00:00",
            "returncode": 0,
            "stdout": stdout,
            "stderr": b"",
            "truncated": False,
            "timed_out": False,
        }


class FailingRunner(FakeRunner):
    def run(self, argv, cwd=None, timeout=None):
        result = super().run(argv, cwd=cwd, timeout=timeout)
        result["returncode"] = 2
        return result


class FullReportTests(unittest.TestCase):
    def arguments(self, output):
        args = TOOL.build_parser().parse_args([
            str(output), "--bdf", "0000:01:00.0", "--no-live-capture",
        ])
        args.read_level = ["passive", "bar-read", "semantic-read"]
        return args

    def archbench_fixture(self, root):
        root = Path(root)
        run_dir = root / "run"
        run_dir.mkdir()
        binary = root / "archbench_sm80"
        binary.write_bytes(b"fixture-sm80-binary\n")
        matrix = root / "acceptance.json"
        matrix.write_text(
            json.dumps({"schema_version": 1, "cases": []}) + "\n",
            encoding="utf-8",
        )
        uuid = "GPU-11111111-2222-3333-4444-555555555555"
        uuid_hex = "11111111222233334444555555555555"
        source_sha = "1" * 64
        build_sha = "2" * 64
        run_id = "fixture-run-id"
        plan = []
        for family, count in TOOL.ARCHBENCH_EXPECTED_FAMILIES.items():
            for ordinal in range(count):
                params = {"fixture": ordinal}
                canonical = json.dumps(
                    {"family": family, "params": params, "provider": "core"},
                    sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
                case_id = "%s.%s" % (
                    family,
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20],
                )
                plan.append({
                    "id": case_id, "family": family, "params": params,
                    "provider": "core", "tags": [],
                })
        selection_sha = hashlib.sha256(
            "\n".join(value["id"] for value in plan).encode("utf-8")
        ).hexdigest()
        policy = dict(TOOL.ARCHBENCH_STABLE_POLICY)
        policy["device"] = 0
        device = {
            "uuid_hex": uuid_hex, "device_name": "NVIDIA CMP 170HX",
            "pci_domain": "0", "pci_bus": "1", "pci_device": "0",
            "binary_target": "sm_80", "source_sha256": source_sha,
            "build_flags_sha256": build_sha,
        }
        run = {
            "schema_version": 1, "run_id": run_id,
            "measurement_preset": "stable",
            "selected_cases": TOOL.ARCHBENCH_SELECTED_CASES,
            "binary": str(binary.resolve()), "requested_gpu_uuid": uuid,
            "device": device,
            "identity": {
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "gpu_uuid": uuid_hex, "policy": policy,
                "selection_sha256": selection_sha, "timeout_seconds": 600.0,
                "software": {
                    "source_sha256": source_sha,
                    "build_flags_sha256": build_sha,
                },
            },
            "selection_source": {
                "kind": "json_matrix", "path": str(matrix.resolve()),
                "sha256": hashlib.sha256(matrix.read_bytes()).hexdigest(),
                "filters": {}, "software_pairs": True,
                "shard_basis": "operation_id",
                "profile_argument_ignored": True,
            },
        }
        results = []
        statuses = []
        for index, case in enumerate(plan):
            if index < 225:
                status, validation, samples = "ok", "passed", [1.0]
            elif index < 233:
                status, validation, samples = "unsupported", "not_run", []
            else:
                status = "measurement_only"
                validation, samples = "not_applicable", [1.0]
            statuses.append(status)
            results.append({
                "schema_version": 1, "case": case, "run_id": run_id,
                "status": status, "validation": validation,
                "samples_ms": samples,
                "metadata": {
                    "uuid_hex": uuid_hex, "source_sha256": source_sha,
                    "build_flags_sha256": build_sha,
                },
            })
        status_counts = dict(sorted(Counter(statuses).items()))
        family_outcomes = {}
        for row in results:
            family = row["case"]["family"]
            counts = family_outcomes.setdefault(family, Counter())
            counts[row["status"]] += 1
            if row["status"] == "ok":
                counts["validated_measured"] += 1
        family_outcomes = {
            family: dict(sorted(counts.items()))
            for family, counts in sorted(family_outcomes.items())
        }
        summary = {
            "run_id": run_id, "selected": 235, "recorded": 235,
            "missing": 0, "validated_measured": 225,
            "status_counts": status_counts, "stopped": False,
        }
        coverage = {
            "run": run, "selected": 235, "recorded": 235, "missing": 0,
            "validated_measured": 225, "statuses": status_counts,
            "families": family_outcomes,
            "implementation_counts": {"fixture": 235},
            "measurement_quality_counts": {
                "stable": 227, "not_measured": 8,
            },
            "pairing_counts": {}, "pairing_unique_operation_counts": {},
        }
        for name, value in (
                ("run.json", run), ("summary.json", summary),
                ("coverage.json", coverage)):
            (run_dir / name).write_text(
                json.dumps(value, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        (run_dir / "plan.jsonl").write_text(
            "".join(json.dumps(value, sort_keys=True, allow_nan=False) + "\n"
                    for value in plan), encoding="utf-8",
        )
        (run_dir / "results.jsonl").write_text(
            "".join(json.dumps(value, sort_keys=True, allow_nan=False) + "\n"
                    for value in results), encoding="utf-8",
        )
        return {
            "run_dir": run_dir, "binary": binary, "matrix": matrix,
            "bdf": "0000:01:00.0", "uuid": uuid,
            "name": "NVIDIA CMP 170HX", "run": run,
            "summary": summary, "coverage": coverage,
        }

    def diagnostics_power_document(self):
        query = {"argv": ["nvidia-smi"], "returncode": 0}
        evidence = {
            "schema": TOOL.POWER_EVIDENCE_SCHEMA,
            "family": "bandwidth",
            "status": "pass",
            "requested_limit_watts": 150.0,
            "sampling_interval_ms_requested": 100,
            "sampling_provenance": {
                "backend": "nvidia-smi-periodic-query",
                "executable": "/usr/bin/nvidia-smi",
                "executable_sha256": "a" * 64,
                "query_fields": ["power.draw", "power.limit"],
                "query_argv_template": ["/usr/bin/nvidia-smi", "-i", "<exact-uuid>"],
                "control_commands_in_global_journal": True,
                "periodic_queries_in_global_journal": False,
            },
            "cards": [{
                "bdf": "0000:01:00.0", "uuid": "GPU-fixture",
                "nvidia_smi_index": 0,
                "original_limit_watts": 250.0,
                "observed_limit_before_watts": 250.0,
                "observed_limit_after_enforce_watts": 150.0,
                "observed_limit_post_benchmark_watts": 150.0,
                "observed_limit_after_restore_watts": 250.0,
                "limit_enforced_during_benchmark": True,
                "restore_attempted": True, "restore_succeeded": True,
                "sample_count": 2, "sample_failures": 0,
                "power_draw_w_min": 100.0, "power_draw_w_max": 140.0,
                "power_draw_w_avg": 120.0,
                "power_draw_average_method": TOOL.POWER_AVERAGE_METHOD,
                "sampling_span_seconds": 0.1,
                "samples": [
                    {"offset_seconds": 0.0, "power_draw_watts": 100.0,
                     "observed_limit_watts": 150.0,
                     "query_duration_seconds": 0.01},
                    {"offset_seconds": 0.1, "power_draw_watts": 140.0,
                     "observed_limit_watts": 150.0,
                     "query_duration_seconds": 0.01},
                ],
                "pre_query_command": query, "set_command": query,
                "enforce_verification_query_command": query,
                "post_benchmark_query_command": query,
                "restore_command": query,
                "restore_verification_query_command": query,
                "error": None,
            }],
        }
        scope = {
            "family": "bandwidth", "scope_index": 0,
            "component_path": "bandwidth[0]", "component_status": "pass",
            "benchmark_executed": True,
            "target_bdfs": ["0000:01:00.0"], "evidence": evidence,
            "validation_errors": [],
        }
        return {
            "status": "completed",
            "benchmark_power_evidence": {
                "schema": TOOL.POWER_SUMMARY_SCHEMA, "status": "pass",
                "requested_limit_watts": 150.0,
                "sampling_interval_ms_requested": 100,
                "requested_families": ["bandwidth", "p2p", "gds", "media"],
                "executed_families": ["bandwidth"],
                "executed_target_bdfs": {
                    "bandwidth": ["0000:01:00.0"],
                },
                "scope_count": 1, "scopes": [scope],
                "validation_errors": [],
                "scope_policy": "fixture benchmark scopes only",
            },
        }

    def feature_command(self, uuid, tail, stdout, returncode=0, raw_argv=None):
        argv = (list(raw_argv) if raw_argv is not None else
                ["/usr/bin/nvidia-smi", "-i", uuid] + list(tail))
        return {
            "argv": argv, "cwd": None,
            "started_utc": "2026-11-26T00:00:00+00:00",
            "dry_run": False, "environment_overrides": {},
            "executable": "/usr/bin/nvidia-smi",
            "executable_sha256": "a" * 64,
            "trust_boundary": "fixture executable with caller privileges",
            "status": "completed", "returncode": returncode,
            "duration_seconds": 0.01, "stdout": stdout, "stderr": "",
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        }

    def diagnostics_feature_document(self):
        bdf = "0000:01:00.0"
        uuid = "GPU-fixture"
        fields = TOOL.FEATURE_COMMAND_FIELDS
        commands = {
            "identity": self.feature_command(
                uuid,
                ["--query-gpu=" + ",".join(fields["identity"]),
                 "--format=csv,noheader,nounits"],
                "00000000:01:00.0, GPU-fixture, NVIDIA CMP 170HX\n",
            ),
            "full_text": self.feature_command(uuid, ["-q"], "full query\n"),
            "full_xml": self.feature_command(uuid, ["-q", "-x"], "<xml/>\n"),
            "ecc": self.feature_command(
                uuid,
                ["--query-gpu=" + ",".join(fields["ecc"]),
                 "--format=csv,noheader,nounits"],
                "Enabled, Enabled, 0, 0, 0, 0\n",
            ),
            "mig_mode": self.feature_command(
                uuid,
                ["--query-gpu=" + ",".join(fields["mig_mode"]),
                 "--format=csv,noheader,nounits"],
                "Disabled, Disabled\n",
            ),
            "sessions": self.feature_command(
                uuid,
                ["--query-gpu=" + ",".join(fields["sessions"]),
                 "--format=csv,noheader,nounits"],
                "query unsupported\n", returncode=2,
            ),
            "mig_gpu_profiles": self.feature_command(
                uuid, [], "profile table\n", raw_argv=[
                    "/usr/bin/nvidia-smi", "mig", "-i", uuid, "-lgip",
                ]
            ),
            "mig_compute_profiles": self.feature_command(
                uuid, [], "unsupported\n", returncode=2, raw_argv=[
                    "/usr/bin/nvidia-smi", "mig", "-i", uuid, "-lgci",
                ]
            ),
            "nvlink": self.feature_command(
                uuid, [], "Device does not support Nvlink\n", raw_argv=[
                    "/usr/bin/nvidia-smi", "nvlink", "-i", uuid, "-s",
                ]
            ),
        }
        ecc_fields = dict(zip(fields["ecc"], [
            "Enabled", "Enabled", "0", "0", "0", "0",
        ]))
        mig_fields = dict(zip(fields["mig_mode"], ["Disabled", "Disabled"]))
        ofa = {
            "schema": TOOL.OFA_PROBE_SCHEMA, "query_only": True,
            "creates_cuda_context": False, "creates_ofa_session": False,
            "candidates": ["libnvidia-opticalflow.so.1"],
            "selected_candidate": "libnvidia-opticalflow.so.1",
            "resolved_library_path": "/usr/lib/libnvidia-opticalflow.so.1",
            "resolved_library_sha256": "b" * 64,
            "load_errors": [], "symbol": "NvOFGetMaxSupportedApiVersion",
            "return_code": 0, "maximum_api_version": 80,
            "status": "api-version-observed",
            "interpretation": (
                "host userspace API query only; not bound to a GPU and not "
                "functional OFA evidence"
            ),
        }
        return {
            "feature_inventory": {
                "schema": TOOL.FEATURE_CENSUS_SCHEMA, "status": "completed",
                "query_only": True, "mutations_requested": False,
                "nvidia_smi": {"path": "/usr/bin/nvidia-smi",
                               "sha256": "a" * 64},
                "ofa_api_probe": ofa,
                "cards": [{
                    "bdf": bdf, "uuid": uuid, "status": "completed",
                    "identity": {"pci.bus_id": "00000000:01:00.0",
                                 "uuid": uuid, "name": "NVIDIA CMP 170HX"},
                    "xml_identity": {"bdf": bdf, "uuid": uuid},
                    "commands": commands,
                    "features": {
                        "ecc": {"status": "observed", "fields": ecc_fields,
                                "command_key": "ecc",
                                "injection_performed": False},
                        "mig": {
                            "status": "observed", "fields": mig_fields,
                            "gpu_instance_profiles_status": "observed",
                            "compute_instance_profiles_status":
                                "unsupported-or-query-failed",
                            "command_keys": ["mig_mode", "mig_gpu_profiles",
                                             "mig_compute_profiles"],
                            "configuration_performed": False,
                        },
                        "nvlink": {"status": "observed",
                                   "command_key": "nvlink"},
                        "nvenc_nvdec_sessions": {
                            "status": "unsupported-or-query-failed",
                            "fields": None, "command_key": "sessions",
                            "interpretation": (
                                "session counters are inventory, not proof that "
                                "a new encode or decode operation succeeds"
                            ),
                        },
                        "nvjpeg": {
                            "status": "not-exposed-by-nvidia-smi",
                            "reason": (
                                "functional NVJPEG evidence requires the "
                                "external media suite"
                            ),
                        },
                        "ofa": {
                            "status": "api-query-not-device-bound",
                            "global_probe_status": "api-version-observed",
                            "reason": (
                                "maximum API version is a host-library query; "
                                "functional OFA work remains not-yet-supported"
                            ),
                        },
                    },
                }],
                "interpretation": {
                    "capability_queries_are_not_benchmarks": True,
                    "functional_media_requires_external_suite": True,
                    "mig_configuration_performed": False,
                    "ecc_injection_performed": False,
                    "ofa_functional_test": "not-yet-supported",
                },
                "validation_errors": [],
            },
        }

    def candidate_document(self):
        routes = [
            {"id": "path-a-a100-identity-vbios-software-sm",
             "execution_authorized": False},
            {"id": "path-b-retain-cmp-software-geometry-gen4",
             "execution_authorized": False},
        ]
        return {"cards": [{
            "bdf": "0000:01:00.0", "profiles": [{"model_id": "fixture"}],
            "dual_path_plans": routes,
            "specimen_ceiling_projection": {
                "memory": {
                    "current_effective_hbm_gib": 64,
                    "defect_preserving_target_hbm_gib": 80,
                    "projected_usable_fbps": 10,
                    "confidence_percent": 96,
                },
                "compute": {
                    "current_effective_sm": 74,
                    "defect_preserving_target_sm": 126,
                    "projected_usable_tpcs": 63,
                    "confidence_percent": 96,
                },
                "system_pipe": {
                    "projected_nondefective_bits": 8,
                    "confidence_percent": 65,
                },
            },
        }]}

    def route_layout_fixture(self, root, exact_current=True, bdfs=None):
        root = Path(root)
        report = root / "report"
        report.mkdir()
        bdfs = list(bdfs or ["0000:01:00.0"])
        words, rir = [0] * 512, [0] * 8
        digest = TOOL._candidate_packed_sha256(words)
        candidate = self.candidate_document()
        candidate.update({
            "schema": "170hx-xray-candidate-plan-v1",
            "selected_profiles": list(TOOL.CANDIDATE_PROFILES),
            "execution_authorized": False,
        })
        template = candidate["cards"][0]
        candidate["cards"] = []
        for bdf in bdfs:
            card = copy.deepcopy(template)
            card["bdf"] = bdf
            card["baseline"] = {"main_rows": words, "rir_rows": rir}
            card["profiles"] = [{
                "model_id": profile,
                "proposed_complete_image_rows": words,
                "proposed_complete_image_u32le_sha256": digest,
            } for profile in TOOL.CANDIDATE_PROFILES]
            candidate["cards"].append(card)
        candidate_path = report / "exact-current-candidate-plans.json"
        pretty = json.dumps(candidate, indent=2, sort_keys=True)
        canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        candidate_path.write_text(pretty + "\n", encoding="utf-8")
        exports = report / "candidate-plan-exports"
        exports.mkdir()
        (exports / "candidate-plan.json").write_text(pretty + "\n", encoding="utf-8")
        (exports / "candidate-plan.jsonl").write_text(canonical + "\n", encoding="ascii")
        (exports / "candidate-plan.txt").write_text(
            "%s\n%s\n%s\n" % (
                TOOL.CANDIDATE_TEXT_BEGIN, pretty, TOOL.CANDIDATE_TEXT_END,
            ), encoding="utf-8",
        )
        (exports / "candidate-plan.canonical-json.bin").write_bytes(
            canonical.encode("ascii")
        )
        proposed = TOOL.export_candidate_proposed_images(
            candidate, report / "candidate-proposed-images", report,
        )
        baseline_root = report / "candidate-baselines"
        baseline_root.mkdir()
        manifests_root = report / "sealed-v2-manifests" / "manifests"
        manifests_root.mkdir(parents=True)
        baseline_paths = {}
        manifest_paths = {}
        for bdf in bdfs:
            safe_bdf = bdf.replace(":", "-").replace(".", "-")
            main_path = baseline_root / (safe_bdf + ".main.u32le")
            rir_path = baseline_root / (safe_bdf + ".rir.u32le")
            main_path.write_bytes(TOOL.struct.pack("<512I", *words))
            rir_path.write_bytes(TOOL.struct.pack("<8I", *rir))
            baseline_paths[bdf] = {"main": main_path, "rir": rir_path}
            for profile in TOOL.MANIFEST_PROFILES:
                path = manifests_root / (safe_bdf + "__" + profile + ".json")
                path.write_text(json.dumps({
                    "candidate_source": {
                        "file_sha256": TOOL.sha256_file(candidate_path),
                    },
                    "binding": {
                        "bdf": bdf, "profile_id": profile,
                        "baseline_main_u32le_sha256":
                            TOOL.sha256_file(main_path),
                        "baseline_rir_u32le_sha256":
                            TOOL.sha256_file(rir_path),
                    },
                    "execution_authorized": False,
                    "hardware_backend_invoked": False,
                }) + "\n", encoding="utf-8")
                manifest_paths[(bdf, profile)] = path
        result = TOOL.build_route_aware_outputs(
            report, candidate_path, candidate, bdfs, proposed,
            {
                "complete": True,
                "baseline_paths": baseline_paths,
                "manifest_paths": manifest_paths,
                "checksum_path": None,
            }, exact_current=exact_current,
        )
        return report, result

    def test_route_layout_separates_options_routes_and_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            report, result = self.route_layout_fixture(directory)
            self.assertFalse(result["execution_authorized"])
            self.assertEqual(result["otp_program_commands"], 0)
            option = json.loads(
                (report / "option-matrix" / "option-matrix.json").read_text()
            )
            self.assertFalse(option["cumulative"])
            self.assertEqual(len(option["entries"]), TOOL.CANDIDATE_PROFILE_COUNT)
            proposed = list((report / "option-matrix" / "cards").glob(
                "*/*/*/proposed-main.*"
            ))
            self.assertEqual(len(proposed), TOOL.CANDIDATE_PROFILE_COUNT * 4)
            manifest_sums = (
                report / "option-matrix" / "SHA256SUMS"
            ).read_text().splitlines()
            self.assertEqual(len(manifest_sums), len(TOOL.MANIFEST_PROFILES))
            self.assertTrue(all(
                "current-baseline-sibling-manifests" in value
                for value in manifest_sums
            ))
            route_index = json.loads(
                (report / "routes" / "route-index.json").read_text()
            )
            self.assertEqual(route_index["route_ids"], [
                "retain-cmp-20c2", "convert-a100-20b5",
            ])
            self.assertEqual(route_index["selected_bdfs"], ["0000:01:00.0"])
            self.assertEqual(route_index["card_count"], 1)
            self.assertEqual(route_index["supported_packaging_epoch"],
                             "000-baseline-only")
            self.assertFalse(
                route_index["automatic_post_write_epoch_advancement_supported"]
            )
            self.assertFalse(route_index["option_matrix_cumulative"])
            self.assertFalse(route_index["execution_authorized"])
            for output_format, filename in TOOL.ROUTE_INDEX_EXPORT_FILENAMES.items():
                replay = TOOL.structured_transport_document(
                    report / "routes" / filename, output_format,
                    TOOL.ROUTE_INDEX_TEXT_BEGIN, TOOL.ROUTE_INDEX_TEXT_END,
                    "route-index",
                )
                self.assertEqual(replay, route_index)
            for route_id in route_index["route_ids"]:
                status = json.loads((
                    report / "routes" / route_id / "cards" /
                    "0000-01-00-0" / "route-status.json"
                ).read_text())
                self.assertEqual(status["current_authoritative_manifest_count"], 1)
                self.assertEqual(status["current_next_segment"],
                                 "010-common-topology")
                self.assertFalse(status["execution_authorized"])
                self.assertEqual(status["otp_program_commands"], 0)
                self.assertIn("final_vbios_policy", status)
                self.assertIn("specimen_ceiling_projection", status)
            cmp_status = json.loads((
                report / "routes" / "retain-cmp-20c2" / "cards" /
                "0000-01-00-0" / "route-status.json"
            ).read_text())
            self.assertNotIn("identity_profile", cmp_status)
            a100_status = json.loads((
                report / "routes" / "convert-a100-20b5" / "cards" /
                "0000-01-00-0" / "route-status.json"
            ).read_text())
            identity = next(
                value for value in a100_status["segments"]
                if value["segment_id"] == "030-identity-20b5-last"
            )
            self.assertFalse(identity["manifest_present"])
            self.assertIn("withheld", identity["state"])
            self.assertTrue((report / "routes" / "comparison" /
                             "route-comparison.json").is_file())
            self.assertTrue((report / "overlays" / "nvlink-rejected" /
                             "overlay.json").is_file())

    def test_route_layout_four_card_counts_and_historical_withholding(self):
        bdfs = [
            "0000:01:00.0", "0000:41:00.0",
            "0000:82:00.0", "0000:c1:00.0",
        ]
        with tempfile.TemporaryDirectory() as directory:
            report, _ = self.route_layout_fixture(
                directory, exact_current=False, bdfs=bdfs,
            )
            option = json.loads(
                (report / "option-matrix" / "option-matrix.json").read_text()
            )
            self.assertEqual(len(option["entries"]),
                             4 * TOOL.CANDIDATE_PROFILE_COUNT)
            self.assertEqual(len((report / "option-matrix" / "SHA256SUMS")
                                 .read_text().splitlines()),
                             4 * len(TOOL.MANIFEST_PROFILES))
            statuses = list((report / "routes").glob(
                "*-*-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]/cards/*/route-status.json"
            ))
            self.assertEqual(len(statuses), 8)
            withheld_otp = 0
            for path in statuses:
                status = json.loads(path.read_text())
                self.assertTrue(status["historical_review_only"])
                self.assertIsNone(status["current_next_segment"])
                self.assertEqual(status["current_authoritative_manifest_count"], 0)
                route_document = json.loads((
                    path.parents[2] / "route.json"
                ).read_text())
                self.assertIn("no manifest is a current next segment",
                              route_document["current_epoch_manifest_policy"])
                for segment in status["segments"]:
                    if segment["segment_id"] == "020-isolated-pcie":
                        withheld_otp += 1
                    if segment["segment_id"] == "030-identity-20b5-last":
                        withheld_otp += 1
            self.assertEqual(withheld_otp, 12)

    def test_route_layout_rejects_unsafe_bdf_and_artifact_binding(self):
        with self.assertRaisesRegex(TOOL.ReportError, "unsafe route-layout BDF"):
            TOOL._safe_bdf_name("../../escape")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.write_text("bound\n", encoding="utf-8")
            record = TOOL.artifact(payload, root)
            record["sha256"] = "0" * 64
            with self.assertRaisesRegex(TOOL.ReportError, "size/hash"):
                TOOL._bound_artifact_path(record, root, "tampered")
            with self.assertRaisesRegex(TOOL.ReportError, "escapes report"):
                TOOL._bound_artifact_path({
                    "path": "../payload", "bytes": payload.stat().st_size,
                    "sha256": TOOL.sha256_file(payload),
                }, root, "escape")

    def test_offline_report_records_every_skip_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            result = TOOL.build_report(
                self.arguments(output), project=ROOT, runner=FakeRunner()
            )
            self.assertEqual(result["status"], "complete-with-explicit-skips")
            reports = list(output.glob("170HX-XRAY_REPORT_*.md"))
            self.assertEqual(len(reports), 1)
            markdown = reports[0].read_text(encoding="utf-8")
            self.assertIn("glitch-read", markdown)
            self.assertIn("not-yet-supported", markdown)
            self.assertIn("nvidia-smi-q", markdown)
            document = json.loads((output / "report-index.json").read_text())
            statuses = {entry["id"]: entry["status"]
                        for entry in document["components"]}
            self.assertEqual(statuses["ordinary-read-dump-plan"], "skipped")
            self.assertEqual(statuses["glitch-read-dump-plan"],
                             "not-requested")
            self.assertEqual(statuses["benchmark-power-limit"],
                             "not-requested")
            self.assertEqual(statuses["diagnostics-benchmarks"],
                             "not-requested")
            self.assertEqual(statuses["archbench-acceptance-235"],
                             "not-requested")
            self.assertEqual(statuses["ecc-injection"], "not-yet-supported")
            self.assertEqual(document["driver_assessment"]["verdict"],
                             "unknown")
            self.assertEqual(document["driver_assessment"]["basis"],
                             "insufficient-attestation")
            self.assertEqual(document["report_mode"], "report")
            self.assertGreater(document["component_status_counts"]
                               ["not-requested"], 0)
            self.assertIn("NVIDIA driver patch assessment", markdown)
            self.assertIn("Verdict: **unknown**", markdown)
            self.assertEqual(document["power"]["before"]["0000:01:00.0"]
                             ["limit_watts"], 150.0)
            self.assertIn("report-index.json", (output / "SHA256SUMS").read_text())

    def test_existing_output_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            TOOL.build_report(
                self.arguments(output), project=ROOT, runner=FakeRunner()
            )
            with self.assertRaisesRegex(TOOL.ReportError, "already exists"):
                TOOL.build_report(
                    self.arguments(output), project=ROOT, runner=FakeRunner()
                )

    def test_owner_busy_exclusion_is_reported_as_skipped_level(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            args = self.arguments(output)
            args.no_live_capture = False
            args.read_level = []
            args.requested_read_levels = ["semantic-read"]
            args.skipped_read_levels = {
                "semantic-read": "owner busy refusal"
            }
            TOOL.build_report(args, project=ROOT, runner=FakeRunner())
            report = next(output.glob("170HX-XRAY_REPORT_*.md"))
            markdown = report.read_text(encoding="utf-8")
            self.assertIn("skipped: owner busy refusal", markdown)
            document = json.loads((output / "report-index.json").read_text())
            entry = next(value for value in document["components"]
                         if value["id"] == "ordinary-read-dump-plan")
            self.assertEqual(entry["levels_completed"], [])
            self.assertEqual(entry["levels_skipped"]["semantic-read"],
                             "owner busy refusal")

    def test_invalid_bdf_is_rejected(self):
        with self.assertRaises(SystemExit):
            TOOL.build_parser().parse_args(["out", "--bdf", "01:00.0"])

    def test_archbench_defaults_resolve_to_pinned_submodule(self):
        parsed = TOOL.build_parser().parse_args(["out"])
        expected = ROOT / "third_party" / "archbench_cuda13_5_v0_2_0"
        self.assertEqual(Path(parsed.archbench_root), expected)
        self.assertIsNone(parsed.archbench_binary)

    def test_archbench_missing_submodule_and_worker_have_actionable_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.arguments(root / "report")
            args.archbench = True
            args.archbench_root = str(root / "missing-submodule")
            args.archbench_binary = str(root / "missing-worker")
            ledger = []
            TOOL.run_archbench(args, root, FakeRunner(), [], ledger)
            self.assertIn("git submodule update --init --recursive",
                          ledger[0]["reason"])

            source = root / "source"
            (source / "configs").mkdir(parents=True)
            (source / "bench").write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "configs" / "acceptance.json").write_text(
                "{}\n", encoding="utf-8"
            )
            args.archbench_root = str(source)
            args.archbench_binary = None
            ledger = []
            TOOL.run_archbench(args, root, FakeRunner(), [], ledger)
            self.assertIn("./bench build --arch 80", ledger[0]["reason"])
            self.assertEqual(
                ledger[0]["worker"],
                str((source / "build" / "bin" / "archbench_sm80").resolve()),
            )

    def test_report_modes_are_disjoint_and_fail_closed(self):
        parser = TOOL.build_parser()
        fast_argv = ["out", "--report-mode", "report"]
        fast = parser.parse_args(fast_argv)
        TOOL.apply_report_mode(fast, fast_argv)
        self.assertFalse(fast.benchmarks)
        self.assertFalse(fast.archbench)
        self.assertFalse(fast.set_power_limit)

        rejected_argv = [
            "out", "--report-mode", "report", "--benchmarks",
        ]
        rejected = parser.parse_args(rejected_argv)
        with self.assertRaisesRegex(TOOL.ReportError, "report\\+bench"):
            TOOL.apply_report_mode(rejected, rejected_argv)

        benchmark_argv = ["out", "--report-mode", "report+bench"]
        benchmark = parser.parse_args(benchmark_argv)
        TOOL.apply_report_mode(benchmark, benchmark_argv)
        self.assertTrue(benchmark.benchmarks)
        self.assertTrue(benchmark.archbench)
        self.assertTrue(benchmark.set_power_limit)

    def test_full_dump_commands_request_every_row_exporter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ordinary"
            output.mkdir()
            args = self.arguments(root / "unused")
            args.no_live_capture = False
            args.read_bundle = None
            args.read_level = ["semantic-read"]
            args.requested_read_levels = ["semantic-read"]
            args.skipped_read_levels = {}
            args.include_glitch_read = False
            runner = FailingRunner()
            ledger = []
            TOOL.run_full_dump(
                args, ROOT, output, runner, ["0000:01:00.0"], ledger
            )
            command = runner.calls[0]
            self.assertIn("full-dump", command)
            self.assertIn("--include-unsafe-projections", command)
            self.assertEqual(
                command[command.index("--fuseless-start") + 1], "222"
            )
            self.assertEqual(
                command[command.index("--fuseless-end") + 1], "511"
            )
            self.assertEqual(
                command[command.index("--row-view") + 1],
                "locked_controller",
            )
            self.assertIn("not an official GA100", command[
                command.index("--bounds-provenance") + 1
            ])

            glitch_output = root / "glitch"
            glitch_output.mkdir()
            args.include_glitch_read = True
            glitch_runner = FailingRunner()
            glitch_ledger = []
            TOOL.run_glitch_dump(
                args, ROOT, glitch_output, glitch_runner,
                ["0000:01:00.0"], glitch_ledger,
            )
            glitch_command = glitch_runner.calls[0]
            self.assertIn("--include-unsafe-projections", glitch_command)
            self.assertEqual(
                glitch_command[glitch_command.index("--row-view") + 1],
                "unknown",
            )
            self.assertEqual(
                glitch_command[glitch_command.index("--fuseless-start") + 1],
                "222",
            )

    def test_live_semantic_full_dump_auto_promotes_candidate_input(self):
        class BundleRunner(FakeRunner):
            def run(inner_self, argv, cwd=None, timeout=None):
                result = super(BundleRunner, inner_self).run(
                    argv, cwd=cwd, timeout=timeout
                )
                argv = [str(value) for value in argv]
                if "full-dump" in argv:
                    bundle = Path(argv[argv.index("full-dump") + 1])
                    capture = (
                        bundle / "levels" / "semantic-read" /
                        "observation" / "capture.json"
                    )
                    capture.parent.mkdir(parents=True)
                    capture.write_text(json.dumps({
                        "generated_utc": TOOL.now_utc().isoformat(),
                        "cards": [{
                            "card": {"domain_bdf": "0000:01:00.0"},
                        }],
                    }), encoding="utf-8")
                    (bundle / "index.json").write_text(json.dumps({
                        "request": {
                            "include_unsafe_projections": True,
                            "fuseless_bounds": [222, 511],
                            "planning_models": list(
                                TOOL.CHECKLIST_PLANNING_MODELS
                            ),
                        },
                        "observations": [{
                            "id": "observation-001",
                            "requested_access": "semantic-read",
                            "status": "complete",
                            "artifact": (
                                "levels/semantic-read/observation/capture.json"
                            ),
                            "transports": {
                                "json": "a", "jsonl": "b", "text": "c",
                                "binary_register_records": "d",
                            },
                        }],
                        "specimens": [], "artifacts": [],
                    }), encoding="utf-8")
                    (bundle / "SHA256SUMS").write_text(
                        "fixture\n", encoding="utf-8"
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report"
            output.mkdir()
            args = self.arguments(root / "unused")
            args.no_live_capture = False
            args.read_bundle = None
            args.read_level = ["semantic-read"]
            args.requested_read_levels = ["semantic-read"]
            args.skipped_read_levels = {}
            args.include_glitch_read = False
            args.physical_capture = []
            ledger = []
            TOOL.run_full_dump(
                args, ROOT, output, BundleRunner(), ["0000:01:00.0"], ledger
            )
            self.assertEqual(len(args.physical_capture), 1)
            self.assertTrue(Path(args.physical_capture[0]).is_file())
            ordinary = next(
                value for value in ledger
                if value["id"] == "ordinary-read-dump-plan"
            )
            self.assertTrue(
                ordinary["candidate_input_promotion"]["promoted"]
            )

    def test_candidate_layer_requests_and_decodes_all_lossless_formats(self):
        document = {
            "schema": "170hx-xray-candidate-plan-v1",
            "cards": [], "execution_authorized": False,
        }
        canonical = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "json": root / "candidate-plan.json",
                "jsonl": root / "candidate-plan.jsonl",
                "text": root / "candidate-plan.txt",
                "canonical-json-bin": root / "candidate-plan.canonical-json.bin",
            }
            paths["json"].write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            paths["jsonl"].write_text(canonical + "\n", encoding="ascii")
            paths["text"].write_text(
                "header\n%s\n%s\n%s\n" % (
                    TOOL.CANDIDATE_TEXT_BEGIN,
                    json.dumps(document, indent=2, sort_keys=True),
                    TOOL.CANDIDATE_TEXT_END,
                ),
                encoding="utf-8",
            )
            paths["canonical-json-bin"].write_bytes(canonical.encode("ascii"))
            for output_format, path in paths.items():
                self.assertEqual(
                    TOOL.candidate_transport_document(path, output_format),
                    document,
                )

            capture = root / "capture.json"
            capture.write_text("{}\n", encoding="utf-8")
            output = root / "report"
            output.mkdir()
            args = self.arguments(root / "unused")
            args.physical_capture = [str(capture)]
            runner = FailingRunner()
            ledger = []
            TOOL.run_candidates(
                args, ROOT, output, runner, ["0000:01:00.0"], ledger
            )
            command = runner.calls[0]
            self.assertIn("--export-all-dir", command)
            self.assertEqual(
                command[command.index("--output-format") + 1], "json"
            )

    def test_full_product_and_three_layer_coverage_fail_closed(self):
        artifacts = []
        observation_transports = {}
        for name, kind in TOOL.OBSERVATION_TRANSPORT_KINDS.items():
            path = "observation/%s" % name
            artifacts.append({"path": path, "kind": kind})
            observation_transports[name] = path
        rir_outputs = []
        for kind in TOOL.RIR_EXPORT_KINDS:
            path = "rir/%s" % kind
            artifacts.append({"path": path, "kind": kind})
            rir_outputs.append(path)
        planning = []
        for model in TOOL.CHECKLIST_PLANNING_MODELS:
            transports = {}
            for name, kind_prefix in (
                    ("json", "plan-"),
                    ("jsonl", "plan-jsonl-"),
                    ("text", "plan-text-"),
                    ("binary_canonical_json", "plan-canonical-json-bin-")):
                path = "planning/%s/%s" % (model, name)
                artifacts.append({"path": path, "kind": kind_prefix + model})
                transports[name] = path
            apply_path = "planning/%s/apply" % model
            readiness_path = "planning/%s/readiness" % model
            artifacts.extend((
                {"path": apply_path, "kind": "apply-dry-run-" + model},
                {"path": readiness_path, "kind": "otp-readiness-" + model},
            ))
            planning.append({
                "model": model, "status": "generated_non_executable",
                "transports": transports,
                "apply_dry_run": apply_path,
                "otp_readiness": readiness_path,
            })
        main_outputs = []
        for output_format in TOOL.ROW_EXPORT_FORMATS:
            path = "main/%s" % output_format
            artifacts.append({"path": path, "kind": "main-" + output_format})
            main_outputs.append({
                "format": output_format, "status": "exported", "path": path,
            })
        register_paths = []
        for kind in TOOL.REGISTER_EXPORT_KINDS:
            path = "register/%s" % kind
            artifacts.append({"path": path, "kind": kind})
            register_paths.append(path)
        index = {
            "request": {
                "include_unsafe_projections": True,
                "fuseless_bounds": [222, 511],
                "planning_models": list(TOOL.CHECKLIST_PLANNING_MODELS),
            },
            "observations": [{
                "id": "observation-001", "requested_access": "semantic-read",
                "status": "complete", "transports": observation_transports,
            }],
            "artifacts": artifacts,
            "specimens": [{
                "id": "specimen-001", "observation": "observation-001",
                "planning": planning,
                "main": {"passes": [{
                    "pass": 1, "complete": True,
                    "outputs": main_outputs,
                }]},
                "register_exports": {
                    "available": True, "formats": register_paths,
                },
                "rir": {
                    "available": True,
                    "passes": [{"outputs": rir_outputs}],
                },
            }],
        }
        coverage = TOOL.full_dump_product_coverage(index)
        self.assertTrue(coverage["complete"], coverage)
        index["specimens"][0]["main"]["passes"][0]["outputs"].pop()
        incomplete = TOOL.full_dump_product_coverage(index)
        self.assertFalse(incomplete["complete"])
        self.assertEqual(TOOL._full_dump_coverage_status(incomplete), "failed")

        ledger = [
            TOOL.item("read-plan-export-coverage", "completed", "fixture"),
            TOOL.item(
                "exact-current-permanent-plans", "completed", "fixture"
            ),
            TOOL.item(
                "guarded-experimental-write-manifests", "completed", "fixture"
            ),
        ]
        TOOL.record_planning_layer_coverage(ledger)
        self.assertEqual(
            ledger[-1]["id"], "three-planning-layer-coverage"
        )
        self.assertEqual(ledger[-1]["status"], "completed")

    def test_candidate_proposed_images_export_every_profile_in_four_formats(self):
        words = [index for index in range(512)]
        digest = TOOL._candidate_packed_sha256(words)
        document = {"cards": [{
            "bdf": "0000:01:00.0",
            "profiles": [{
                "model_id": profile,
                "proposed_complete_image_rows": words,
                "proposed_complete_image_u32le_sha256": digest,
            } for profile in TOOL.CANDIDATE_PROFILES],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = TOOL.export_candidate_proposed_images(
                document, root / "exports", root,
            )
            self.assertEqual(len(entries), TOOL.CANDIDATE_PROFILE_COUNT)
            self.assertEqual(
                [value["profile_id"] for value in entries],
                list(TOOL.CANDIDATE_PROFILES),
            )
            for value in entries:
                self.assertEqual(
                    set(value["formats"]),
                    {"json", "jsonl", "indexed-text", "raw-u32le"},
                )
                for retained in value["formats"].values():
                    self.assertTrue((root / retained["path"]).is_file())
            malformed = copy.deepcopy(document)
            malformed["cards"][0]["profiles"].reverse()
            with self.assertRaisesRegex(
                    TOOL.ReportError, "profile inventory is incomplete"):
                TOOL.export_candidate_proposed_images(
                    malformed, root / "reordered", root,
                )

    def test_glitch_product_coverage_is_scoped_and_fails_after_eligible_main(self):
        class GlitchBundleRunner(FakeRunner):
            def run(inner_self, argv, cwd=None, timeout=None):
                result = super(GlitchBundleRunner, inner_self).run(
                    argv, cwd=cwd, timeout=timeout
                )
                command = [str(value) for value in argv]
                if "full-dump" in command:
                    bundle = Path(command[command.index("full-dump") + 1])
                    bundle.mkdir(parents=True)
                    (bundle / "index.json").write_text(json.dumps({
                        "request": {
                            "include_unsafe_projections": True,
                            "fuseless_bounds": [222, 511],
                            "planning_models": list(
                                TOOL.CHECKLIST_PLANNING_MODELS
                            ),
                        },
                        "observations": [{
                            "id": "glitch-observation",
                            "requested_access": "glitch-read",
                            "status": "complete", "transports": {},
                        }],
                        "artifacts": [],
                        "specimens": [{
                            "id": "glitch-specimen",
                            "observation": "glitch-observation",
                            "main": {"passes": [{
                                "pass": 1, "complete": True, "outputs": [],
                            }]},
                        }],
                    }), encoding="utf-8")
                    (bundle / "SHA256SUMS").write_text(
                        "fixture\n", encoding="utf-8"
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            output.mkdir()
            args = self.arguments(output)
            args.include_glitch_read = True
            ledger = []
            TOOL.run_glitch_dump(
                args, ROOT, output, GlitchBundleRunner(),
                ["0000:01:00.0"], ledger,
            )
            coverage = next(
                value for value in ledger
                if value["id"] == "glitch-read-export-coverage"
            )
            self.assertEqual(coverage["status"], "failed")
            self.assertEqual(
                coverage["coverage"]["requested_access_scope"],
                "glitch-read",
            )

    def test_candidate_success_without_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.json"
            capture.write_text("{}\n", encoding="utf-8")
            output = root / "report"
            output.mkdir()
            args = self.arguments(output)
            args.physical_capture = [str(capture)]
            ledger = []
            TOOL.run_candidates(
                args, ROOT, output, FakeRunner(), ["0000:01:00.0"], ledger
            )
            self.assertEqual(ledger[0]["status"], "failed")
            self.assertIn("without an output file", ledger[0]["reason"])
            self.assertEqual(ledger[1]["status"], "skipped")

    def test_candidate_rejects_wrong_selected_profile_inventory(self):
        class CandidateRunner(FakeRunner):
            def run(inner_self, argv, cwd=None, timeout=None):
                result = super(CandidateRunner, inner_self).run(
                    argv, cwd=cwd, timeout=timeout
                )
                command = [str(value) for value in argv]
                if "--same-capture-context" in command:
                    destination = Path(command[command.index("--output") + 1])
                    document = self.candidate_document()
                    document["cards"][0]["profiles"] = [
                        {"model_id": profile}
                        for profile in TOOL.CANDIDATE_PROFILES
                    ]
                    document["selected_profiles"] = list(
                        reversed(TOOL.CANDIDATE_PROFILES)
                    )
                    destination.write_text(
                        json.dumps(document), encoding="utf-8"
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.json"
            capture.write_text("{}\n", encoding="utf-8")
            output = root / "report"
            output.mkdir()
            args = self.arguments(output)
            args.physical_capture = [str(capture)]
            ledger = []
            TOOL.run_candidates(
                args, ROOT, output, CandidateRunner(),
                ["0000:01:00.0"], ledger,
            )
            self.assertEqual(ledger[0]["status"], "failed")
            self.assertIn("exact ordered set", ledger[0]["reason"])

    def test_timeout_arguments_are_positive_and_integral_where_required(self):
        parser = TOOL.build_parser()
        parsed = parser.parse_args([
            "out", "--long-timeout", "1800.0",
            "--archbench-case-timeout", "600.0",
        ])
        self.assertEqual(parsed.long_timeout, 1800)
        self.assertIs(type(parsed.long_timeout), int)
        self.assertEqual(parsed.archbench_case_timeout, 600)
        for option, value in (
                ("--long-timeout", "0"),
                ("--long-timeout", "1.5"),
                ("--long-timeout", "nan"),
                ("--archbench-case-timeout", "-1"),
                ("--command-timeout", "inf"),
                ("--benchmark-timeout", "0")):
            with self.subTest(option=option, value=value):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["out", option, value])

    def test_diagnostics_timeout_is_canonical_and_rejects_fractional_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            output.mkdir()
            args = self.arguments(output)
            args.benchmarks = True
            args.long_timeout = 1800.0
            runner = FakeRunner()
            ledger = []
            TOOL.run_diagnostics(
                args, ROOT, output, runner, ["0000:01:00.0"], ledger
            )
            command = runner.calls[0]
            index = command.index("--command-timeout")
            self.assertEqual(command[index + 1], "1800")

            args.long_timeout = 1.5
            runner = FakeRunner()
            with self.assertRaisesRegex(
                    TOOL.ReportError, "must be a positive integer"):
                TOOL.run_diagnostics(
                    args, ROOT, output, runner, ["0000:01:00.0"], []
                )
            self.assertEqual(runner.calls, [])

    def test_outer_manifest_hashes_nested_manifests_and_only_excludes_self(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "SHA256SUMS").write_text(
                "nested manifest\n", encoding="utf-8"
            )
            (root / "SHA256SUMS").write_text(
                "stale outer manifest\n", encoding="utf-8"
            )
            records = TOOL.sums(root).splitlines()
            paths = [record.split("  ", 1)[1] for record in records]
            self.assertEqual(paths, ["alpha.txt", "nested/SHA256SUMS"])
            self.assertEqual(
                records[1].split("  ", 1)[0],
                hashlib.sha256(
                    (nested / "SHA256SUMS").read_bytes()
                ).hexdigest(),
            )

            (nested / "SHA256SUMS").unlink()
            target = root / "outside.txt"
            target.write_text("outside\n", encoding="utf-8")
            try:
                (nested / "SHA256SUMS").symlink_to(target)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(TOOL.ReportError, "symlink"):
                TOOL.sums(root)
            (nested / "SHA256SUMS").unlink()
            (root / "SHA256SUMS").unlink()
            (root / "SHA256SUMS").symlink_to(target)
            with self.assertRaisesRegex(TOOL.ReportError, "symlink"):
                TOOL.sums(root)

    def test_private_tree_seal_recurses_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "report"
            nested = root / "archbench-style"
            nested.mkdir(parents=True, mode=0o755)
            payload = nested / "results.jsonl"
            payload.write_text("{}\n", encoding="utf-8")
            root.chmod(0o755)
            nested.chmod(0o755)
            payload.chmod(0o644)
            counts = TOOL.seal_private_tree(root)
            self.assertEqual(counts, {"directories": 2, "regular_files": 1})
            self.assertEqual(TOOL.stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(TOOL.stat.S_IMODE(nested.stat().st_mode), 0o700)
            self.assertEqual(TOOL.stat.S_IMODE(payload.stat().st_mode), 0o600)
            try:
                (nested / "escape").symlink_to(payload)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(TOOL.ReportError, "symlink"):
                TOOL.seal_private_tree(root)

    def test_component_status_vocabulary_is_closed(self):
        self.assertEqual(
            TOOL.item("fixture", "not-requested", "optional work absent")
            ["status"],
            "not-requested",
        )
        with self.assertRaisesRegex(TOOL.ReportError,
                                    "invalid component status"):
            TOOL.item("fixture", "not-run", "ambiguous status")

    def test_driver_patch_assessment_requires_positive_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "dkms.txt"
            evidence.write_text("nvidia/580.0, installed\n", encoding="utf-8")
            commands = {
                "dkms": {
                    "status": "completed", "returncode": 0,
                    "stdout": {
                        "path": evidence.name,
                        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                        "bytes": evidence.stat().st_size,
                    },
                },
            }
            assessment = TOOL.assess_nvidia_driver(commands, root)
            self.assertEqual(assessment["verdict"], "unknown")
            self.assertEqual(assessment["explicit_marker_matches"], [])
            evidence.write_text(
                "nvidia/580.0 plus anemone patch lineage\n", encoding="utf-8"
            )
            assessment = TOOL.assess_nvidia_driver(commands, root)
            self.assertEqual(assessment["verdict"], "patched")
            self.assertEqual(assessment["basis"], "explicit-marker")
            self.assertEqual(assessment["explicit_marker_matches"], [{
                "command_key": "dkms", "marker": "anemone",
            }])

    def test_archbench_telemetry_power_summary_is_exact_and_per_case(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.jsonl"
            samples = [
                {"monotonic_ns": 1_000_000_000, "case_id": "setup",
                 "power_mw": 100_000},
                {"monotonic_ns": 1_200_000_000, "case_id": "case-a",
                 "power_mw": 150_000},
                {"monotonic_ns": 1_400_000_000, "case_id": "case-a",
                 "power_mw": 200_000},
                {"monotonic_ns": 1_600_000_000, "case_id": "case-b",
                 "power_mw": None},
            ]
            path.write_text(
                "".join(json.dumps(value) + "\n" for value in samples),
                encoding="utf-8",
            )
            summary = TOOL.telemetry_power_summary(path)
            stats = summary["statistics"]
            self.assertEqual(stats["minimum_watts"], 100.0)
            self.assertEqual(stats["maximum_watts"], 200.0)
            self.assertEqual(stats["average_watts"], 150.0)
            self.assertEqual(stats["valid_sample_count"], 3)
            self.assertEqual(summary["failed_sample_count"], 1)
            self.assertEqual(summary["per_case"]["case-a"]["average_watts"], 175.0)
            self.assertAlmostEqual(
                summary["observed_interval_seconds"]["average"], 0.2
            )

    def validate_archbench_fixture(self, fixture):
        return TOOL.validate_archbench_run(
            fixture["run_dir"], fixture["bdf"], fixture["uuid"],
            fixture["name"], fixture["binary"], fixture["matrix"], 600,
        )

    def test_archbench_coverage_validator_accepts_recorded_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.archbench_fixture(directory)
            validated = self.validate_archbench_fixture(fixture)
            self.assertTrue(validated["validated"])
            self.assertEqual(validated["selected"], 235)
            self.assertEqual(validated["recorded"], 235)
            self.assertEqual(validated["numerically_validated_measured"], 225)
            self.assertEqual(validated["records_with_positive_samples"], 227)
            self.assertEqual(validated["status_counts"], {
                "measurement_only": 2, "ok": 225, "unsupported": 8,
            })
            self.assertFalse(
                validated["all_selected_numerically_validated_measured"]
            )
            self.assertEqual(validated["selected_family_counts"],
                             TOOL.ARCHBENCH_EXPECTED_FAMILIES)

    def test_archbench_coverage_validator_rejects_missing_and_duplicate_ids(self):
        for mutation, pattern in (
                ("missing", "selected/recorded case IDs differ"),
                ("duplicate", "duplicate case ID")):
            with self.subTest(mutation=mutation), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = self.archbench_fixture(directory)
                path = fixture["run_dir"] / "results.jsonl"
                lines = path.read_text(encoding="utf-8").splitlines(True)
                if mutation == "missing":
                    lines.pop()
                else:
                    lines.append(lines[0])
                path.write_text("".join(lines), encoding="utf-8")
                with self.assertRaisesRegex(TOOL.ReportError, pattern):
                    self.validate_archbench_fixture(fixture)

    def test_archbench_coverage_validator_rejects_status_mirror_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.archbench_fixture(directory)
            summary = copy.deepcopy(fixture["summary"])
            summary["status_counts"]["ok"] -= 1
            (fixture["run_dir"] / "summary.json").write_text(
                json.dumps(summary, allow_nan=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(TOOL.ReportError,
                                        "status/family coverage mismatch"):
                self.validate_archbench_fixture(fixture)

    def test_archbench_coverage_validator_rejects_identity_mismatches(self):
        for mutation, pattern in (
                ("uuid", "GPU UUID identity mismatch"),
                ("binary", "binary hash identity mismatch"),
                ("matrix", "matrix/selection-source identity mismatch"),
                ("source", "result device/source identity mismatch")):
            with self.subTest(mutation=mutation), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = self.archbench_fixture(directory)
                if mutation == "uuid":
                    run = copy.deepcopy(fixture["run"])
                    run["requested_gpu_uuid"] = (
                        "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                    )
                    coverage = copy.deepcopy(fixture["coverage"])
                    coverage["run"] = run
                    for name, value in (("run.json", run),
                                        ("coverage.json", coverage)):
                        (fixture["run_dir"] / name).write_text(
                            json.dumps(value, allow_nan=False), encoding="utf-8"
                        )
                elif mutation == "binary":
                    fixture["binary"].write_bytes(b"different binary\n")
                elif mutation == "matrix":
                    fixture["matrix"].write_text(
                        '{"schema_version":1,"cases":[]}\n ', encoding="utf-8"
                    )
                else:
                    path = fixture["run_dir"] / "results.jsonl"
                    lines = path.read_text(encoding="utf-8").splitlines()
                    first = json.loads(lines[0])
                    first["metadata"]["source_sha256"] = "3" * 64
                    lines[0] = json.dumps(first, allow_nan=False)
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(TOOL.ReportError, pattern):
                    self.validate_archbench_fixture(fixture)

    def test_archbench_telemetry_rejects_duplicate_and_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.jsonl"
            path.write_text(
                '{"case_id":"a","power_mw":1,"power_mw":2}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TOOL.ReportError, "duplicate JSON key"):
                TOOL.telemetry_power_summary(path)
            path.write_text(
                '{"case_id":"a","power_mw":NaN}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(TOOL.ReportError, "non-finite"):
                TOOL.telemetry_power_summary(path)

    def test_manifest_profile_matrix_matches_strict_compiler(self):
        path = ROOT / "tools" / "170hx_vfi_append_manifest.py"
        spec = importlib.util.spec_from_file_location(
            "xray_manifest_matrix_full_report_test", path
        )
        compiler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiler)
        self.assertEqual(set(TOOL.MANIFEST_PROFILES),
                         set(compiler.APPROVED_PROFILES))
        self.assertEqual(len(TOOL.MANIFEST_PROFILES),
                         len(set(TOOL.MANIFEST_PROFILES)))

    def test_diagnostics_power_summary_is_independently_recomputed(self):
        document = self.diagnostics_power_document()
        rows = TOOL.diagnostics_power_rows(
            document, ["0000:01:00.0"], 150.0
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["minimum_watts"], 100.0)
        self.assertEqual(rows[0]["maximum_watts"], 140.0)
        self.assertEqual(rows[0]["average_watts"], 120.0)
        self.assertEqual(rows[0]["interval_seconds"], 0.1)
        self.assertEqual(rows[0]["sampling_span_seconds"], 0.1)

    def test_diagnostics_power_summary_rejects_mutations(self):
        mutations = (
            ("aggregate", lambda value: value["benchmark_power_evidence"]
             ["scopes"][0]["evidence"]["cards"][0].__setitem__(
                 "power_draw_w_avg", 119.0)),
            ("restoration", lambda value: value["benchmark_power_evidence"]
             ["scopes"][0]["evidence"]["cards"][0].__setitem__(
                 "restore_succeeded", False)),
            ("boolean-count", lambda value: value["benchmark_power_evidence"]
             ["scopes"][0]["evidence"]["cards"][0].__setitem__(
                 "sample_count", True)),
            ("target-mirror", lambda value: value["benchmark_power_evidence"]
             ["executed_target_bdfs"].__setitem__("bandwidth", [])),
            ("summary-status", lambda value: value["benchmark_power_evidence"]
             .__setitem__("status", "failed")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = copy.deepcopy(self.diagnostics_power_document())
                mutate(document)
                with self.assertRaises(TOOL.ReportError):
                    TOOL.diagnostics_power_rows(
                        document, ["0000:01:00.0"], 150.0
                    )

    def test_feature_census_is_independently_validated_and_summarized(self):
        document = self.diagnostics_feature_document()
        rows = TOOL.diagnostics_feature_rows(document, ["0000:01:00.0"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ecc_current"], "Enabled")
        self.assertEqual(rows[0]["mig_current"], "Disabled")
        self.assertEqual(rows[0]["nvenc_functional"], "not-run")
        self.assertEqual(rows[0]["ofa_api_query"], "api-version-observed")
        self.assertEqual(rows[0]["ofa_functional"], "not-yet-supported")

    def test_feature_census_rejects_raw_and_decode_mutations(self):
        document = self.diagnostics_feature_document()
        document["feature_inventory"]["cards"][0]["commands"]["ecc"][
            "stdout"
        ] = "Disabled, Disabled, 0, 0, 0, 0\n"
        with self.assertRaisesRegex(TOOL.ReportError, "hash mismatch"):
            TOOL.diagnostics_feature_rows(document, ["0000:01:00.0"])
        document = self.diagnostics_feature_document()
        document["feature_inventory"]["cards"][0]["features"]["mig"][
            "configuration_performed"
        ] = True
        with self.assertRaisesRegex(TOOL.ReportError, "MIG mirror"):
            TOOL.diagnostics_feature_rows(document, ["0000:01:00.0"])

    def test_feature_rows_render_with_explicit_raw_evidence_boundary(self):
        rows = TOOL.diagnostics_feature_rows(
            self.diagnostics_feature_document(), ["0000:01:00.0"]
        )
        markdown = TOOL.render({
            "generated_utc": "2026-11-26T00:00:00+00:00",
            "status": "complete-with-explicit-skips",
            "selected_bdfs": ["0000:01:00.0"],
            "safety": {"sudo_read_inventory": False},
            "power": {
                "requested_limit_watts": 150.0,
                "restoration_status": "not-needed",
                "before": {"0000:01:00.0": {}},
                "after_set": {"0000:01:00.0": {}},
            },
            "components": [{
                "id": "diagnostics-benchmarks", "status": "completed",
                "reason": "fixture", "feature_inventory_rows": rows,
                "feature_inventory_artifact": {
                    "path": "diagnostics.json", "sha256": "a" * 64,
                    "bytes": 1,
                },
            }],
        }, Path("/tmp"), {})
        self.assertIn("MIG, ECC, media, OFA and NVLink census", markdown)
        self.assertIn("unsupported-reported-by-nvidia-smi", markdown)
        self.assertIn("not-exposed-by-nvidia-smi / not-run", markdown)
        self.assertIn("[`diagnostics.json`](diagnostics.json)", markdown)

    def test_archbench_validation_renders_selected_recorded_and_gaps(self):
        validation = {
            "validated": True, "selected": 235, "recorded": 235,
            "missing": 0, "numerically_validated_measured": 225,
            "records_with_positive_samples": 227,
            "status_counts": {
                "ok": 225, "unsupported": 8, "measurement_only": 2,
            },
            "selected_family_counts": TOOL.ARCHBENCH_EXPECTED_FAMILIES,
        }
        markdown = TOOL.render({
            "generated_utc": "2026-11-26T00:00:00+00:00",
            "status": "complete", "selected_bdfs": ["0000:01:00.0"],
            "safety": {"sudo_read_inventory": False},
            "power": {"requested_limit_watts": 150.0,
                      "restoration_status": "not-needed", "before": {},
                      "after_set": {}},
            "components": [{
                "id": "archbench-acceptance-235", "status": "completed",
                "reason": "fixture", "per_card": [{
                    "bdf": "0000:01:00.0",
                    "coverage_validation": validation,
                }],
            }],
        }, Path("/tmp"), {})
        self.assertIn("Selected and recorded are coverage facts", markdown)
        self.assertIn("| `0000:01:00.0` | True | 235 | 235 | 0 | 225 | 227 |", markdown)
        self.assertIn("unsupported", markdown)

    def test_candidate_ceiling_summary_checks_arithmetic_and_routes(self):
        document = self.candidate_document()
        rows = TOOL.candidate_target_summary(document, ["0000:01:00.0"])
        self.assertEqual(rows[0]["target_hbm_gib"], 80)
        self.assertEqual(rows[0]["target_sm"], 126)
        malformed = copy.deepcopy(document)
        malformed["cards"][0]["specimen_ceiling_projection"]["memory"][
            "projected_usable_fbps"
        ] = 12
        with self.assertRaisesRegex(TOOL.ReportError, "arithmetic"):
            TOOL.candidate_target_summary(malformed, ["0000:01:00.0"])
        malformed = copy.deepcopy(document)
        malformed["cards"][0]["dual_path_plans"].pop()
        with self.assertRaisesRegex(TOOL.ReportError, "dual-route"):
            TOOL.candidate_target_summary(malformed, ["0000:01:00.0"])

    def test_verified_full_dump_import_is_relocated_and_supplies_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            capture_path = source / "observations" / "semantic" / "capture.json"
            capture_path.parent.mkdir(parents=True)
            capture_path.write_text(json.dumps({
                "generated_utc": TOOL.now_utc().isoformat(),
                "cards": [{"card": {"domain_bdf": "0000:01:00.0"}}],
            }), encoding="utf-8")
            (source / "index.json").write_text(json.dumps({
                "command": "full-dump",
                "completion": {
                    "status": "PARTIAL", "otp_program_commands": 0,
                    "programming_writes_performed": 0,
                },
                "observations": [
                    {"requested_access": "passive", "status": "complete"},
                    {"requested_access": "bar-read", "status": "complete"},
                    {"requested_access": "semantic-read", "status": "complete",
                     "artifact": "observations/semantic/capture.json"},
                ],
            }), encoding="utf-8")
            (source / "SHA256SUMS").write_text("fixture\n", encoding="utf-8")
            output = root / "report"
            output.mkdir()
            args = SimpleNamespace(
                read_bundle=str(source), long_timeout=10.0,
                requested_read_levels=["passive", "bar-read", "semantic-read"],
                physical_capture=[], candidate_max_age_hours=24.0,
                allow_historical_candidate=False,
            )
            ledger = []
            TOOL.run_imported_full_dump(
                args, ROOT, output, FakeRunner(), ["0000:01:00.0"], ledger
            )
            self.assertEqual(ledger[0]["status"], "completed")
            self.assertEqual(
                ledger[0]["levels_completed"],
                ["passive", "bar-read", "semantic-read"],
            )
            self.assertTrue(Path(args.physical_capture[0]).is_file())
            self.assertTrue((output / "read-dump-plan-bundle" / "index.json").is_file())
            coverage = next(
                value for value in ledger
                if value["id"] == "read-plan-export-coverage"
            )
            self.assertEqual(coverage["status"], "skipped")
            self.assertFalse(ledger[0]["complete_product_set"])

    def test_import_copy_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "target.txt"
            target.write_text("fixture", encoding="utf-8")
            try:
                (source / "link").symlink_to(target)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(TOOL.ReportError, "symlink"):
                TOOL.copy_tree_new(source, root / "destination")

    def test_evidence_age_requires_timezone_and_rejects_future(self):
        reference = TOOL.now_utc()
        one_hour = reference - TOOL.datetime.timedelta(hours=1)
        self.assertAlmostEqual(
            TOOL.evidence_age_hours(one_hour.isoformat(), reference), 1.0
        )
        with self.assertRaisesRegex(TOOL.ReportError, "timezone"):
            TOOL.evidence_age_hours("2026-11-26T00:00:00", reference)
        future = reference + TOOL.datetime.timedelta(hours=1)
        with self.assertRaisesRegex(TOOL.ReportError, "future"):
            TOOL.evidence_age_hours(future.isoformat(), reference)


if __name__ == "__main__":
    unittest.main()
