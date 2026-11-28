#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline adversarial tests for the experimental GA100 VFI append backend.

The suite deliberately never opens PCI sysfs or BAR0.  The integration fixture
is compiled from the checked-in exact-current Level-3 four-card evidence, while
all controller transactions use an in-memory BAR model.  A failure in this
file is a release blocker for the experimental backend; it is not evidence that
real OTP programming is safe.
"""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = PACKAGE_ROOT.parent
BACKEND_PATH = PACKAGE_ROOT / "otp_vfi_append.py"
MAIN_PATH = PACKAGE_ROOT / "170HX-XRAY.py"
COMPILER_PATH = PACKAGE_ROOT / "tools" / "170hx_vfi_append_manifest.py"
CANDIDATE_PLANNER_PATH = PACKAGE_ROOT / "tools" / "170hx_candidate_plan.py"
RELEASE_BUNDLE_PATH = PACKAGE_ROOT / "tools" / "170hx_full_release_bundle.py"
CANDIDATE = (
    ROOT / "research-evidence" /
    "170hx-ray-specimen-plans-exact-current-level3-native-v3.7.0-r2-2026-11-23.json"
)
CROSS_DATE_CANDIDATE = (
    ROOT / "research-evidence" / "170hx-ray-specimen-plans-2026-11-23.json"
)
EVIDENCE_ROOT = (
    ROOT / "research-evidence" /
    "170hx-ray-ai-ws-full-dump-v3.6.1-2026-11-23" / "bundle" /
    "specimens" / "specimen-009" /
    "exports"
)
COLD_MAIN = EVIDENCE_ROOT / "main" / "pass-001" / "raw-u32le.bin"
COLD_RIR = EVIDENCE_ROOT / "rir" / "pass-001" / "raw-u32le.bin"
NATIVE_CAPTURE = (
    ROOT / "research-evidence" /
    "170hx-ray-ai-ws-full-dump-v3.6.1-2026-11-23" / "bundle" /
    "observations" / "observation-003" / "capture.json"
)
BDF = "0000:01:00.0"
PROFILE = "specimen-80gb-hbm"
EXTERNAL_EVIDENCE_AVAILABLE = all(path.is_file() for path in (
    CANDIDATE, CROSS_DATE_CANDIDATE, COLD_MAIN, COLD_RIR, NATIVE_CAPTURE,
))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKEND = load_module("xray_vfi_append_test_target", BACKEND_PATH)
COMPILER = load_module("xray_vfi_append_compiler_test_target", COMPILER_PATH)
CANDIDATE_PLANNER = load_module(
    "xray_vfi_append_candidate_planner_test_target", CANDIDATE_PLANNER_PATH
)
RELEASE_BUNDLE = load_module(
    "xray_full_release_bundle_test_target", RELEASE_BUNDLE_PATH
)


def canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def reseal(document):
    result = copy.deepcopy(document)
    result.pop("content_sha256_excluding_self", None)
    result["content_sha256_excluding_self"] = hashlib.sha256(
        canonical_bytes(result)
    ).hexdigest()
    return result


def write_json(directory, name, value):
    path = Path(directory) / name
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def compile_manifest():
    return COMPILER.build_manifest(
        CANDIDATE, BDF, PROFILE, COLD_MAIN, COLD_RIR,
        generated_utc="2026-11-23T00:00:00+00:00",
    )


def validated_manifest(directory):
    path = write_json(directory, "manifest.json", compile_manifest())
    return path, BACKEND.load_and_validate_manifest(path, BDF, PROFILE)


def session_validated_fixture():
    """Synthetic already-validated shape for controller state-machine tests.

    Compiler/manifest integration uses separately retained hardware evidence
    when that research workspace is present.  The controller state machine does
    not need card data, so keep it independently runnable with a deterministic
    valid FF/IFF envelope and two blank-row append operations.
    """
    baseline = [0] * 512
    baseline[508] = 0x20 | 0x1D | ((0x1234 >> 2) << 6)
    baseline[509] = 0xA5A5A5A5
    baseline[510] = 0xFFFFFFFF
    baseline[511] = 0x20
    rir = [0] * 8
    proposed = list(baseline)
    operations = []
    sources = (
        (224, (0x0001 << 16) | (8 << 6) | 1, "synthetic-memory"),
        (225, (0x0002 << 16) | (39 << 6) | 2, "synthetic-activation"),
    )
    for index, (row, word, stage) in enumerate(sources):
        proposed[row] = word
        operations.append({
            "index": index,
            "row": row,
            "word": word,
            "word_hex": "0x%08x" % word,
            "expected_prefix_compare_u32le_sha256":
                BACKEND._packed_sha256(proposed[:488]),
            "stage": stage,
        })
    identity = {
        "domain_bdf": BDF,
        "vendor": "0x10de",
        "device": "0x20c2",
        "subsystem_vendor": "0x10de",
        "subsystem_device": "0x1585",
        "revision": "0xa1",
    }
    return {
        "manifest": {},
        "path": "offline-session-fixture",
        "file_sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "bdf": BDF,
        "profile_id": PROFILE,
        "commit_policy": BACKEND._expected_commit_policy(PROFILE),
        "identity": identity,
        "baseline": baseline,
        "proposed": proposed,
        "rir": rir,
        "operations": operations,
        "compare_range": [0, 487],
        "iff_boundary": 508,
        "residual_research_risks": [],
    }


class FakeBar(object):
    """Small GA100 fuse-controller model; no real file or device is touched."""

    def __init__(self, rows, module=BACKEND):
        self.m = module
        self.rows = list(rows)
        self.address = 0
        self.wdata = 0
        self.read_data = 0
        self.read_log = []
        self.write_log = []
        self.command_log = []
        self.ctrl_queue = []
        self.address_read_override = None
        self.program_readback_override = None
        self.regs = {
            self.m.FUSECTRL: self.m.IDLE_VALUE,
            self.m.GFW_BOOT: 0x000000FF,
            self.m.DSOV_STATUS: 0,
            self.m.CRC_STATUS: 0x55,
            self.m.FUSECTRL_PLM: 0xFFFFFFFF,
            self.m.OPT_PRIV_MASK: 0xFFFFFFFF,
            self.m.FUSEDEBUG: 0,
            self.m.SWIFF: 0,
            self.m.IFF_CTRL: 0,
            self.m.LOADER_QUIESCENT: 0,
            self.m.SENTINEL_ALIAS: 0,
            self.m.FPF_SENTINEL: 0,
            self.m.EN_SW_OVERRIDE: 0,
            self.m.EFP_STATUS: 0,
            self.m.PROGRAM_DISABLE_STATUS: 0,
            self.m.BYPASS_FUSES_STATUS: 0,
            self.m.SBFE_SHADOW: 1,
            self.m.WDATA_WR_SECURE: 1,
            self.m.FUSEWDATA_PLM: 0xFFFFFFFF,
            self.m.RESENSE_DISABLE: 0,
        }
        self.sense_resense_disable_value = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read32(self, offset):
        if offset == self.m.FUSECTRL and self.ctrl_queue:
            value = self.ctrl_queue.pop(0)
        elif offset == self.m.FUSEADDR:
            value = (self.address if self.address_read_override is None else
                     self.address_read_override)
        elif offset == self.m.FUSERDATA:
            value = self.read_data
        else:
            value = self.regs.get(offset, 0)
        value &= 0xFFFFFFFF
        self.read_log.append((offset, value))
        return value

    def write32(self, offset, value):
        value &= 0xFFFFFFFF
        self.write_log.append((offset, value))
        if offset == self.m.FUSEADDR:
            self.address = value
            return
        if offset == self.m.FUSEWDATA:
            self.wdata = value
            return
        if offset == self.m.FUSECTRL:
            self.command_log.append((value, self.address, self.wdata))
            if value == self.m.CMD_READ:
                if self.program_readback_override is None:
                    self.read_data = self.rows[self.address]
                else:
                    self.read_data = self.program_readback_override
                self.regs[self.m.FUSECTRL] = self.m.IDLE_VALUE
                return
            if value == self.m.CMD_SENSE:
                self.regs[self.m.EFP_STATUS] = 1
                self.regs[self.m.RESENSE_DISABLE] = (
                    self.sense_resense_disable_value
                )
                self.regs[self.m.FUSECTRL] = self.m.IDLE_VALUE
                return
            if value == self.m.CMD_PROGRAM:
                # The sealed backend may only target a zero row, so OR and
                # assignment are equivalent here while OR models OTP polarity.
                self.rows[self.address] |= self.wdata
                self.regs[self.m.FUSECTRL] = self.m.IDLE_VALUE
                return
        self.regs[offset] = value


class MemoryJournal(object):
    """Captures session events without weakening DurableJournal tests."""

    def __init__(self):
        self.records = []

    def append(self, state, event, **details):
        record = {"state": state, "event": event, "details": details}
        self.records.append(record)
        return record


class CompilerPolicyUnitTests(unittest.TestCase):
    def test_release_manifest_filename_contract_is_canonical(self):
        key = ("0000:01:00.0", "specimen-more-sm")
        self.assertEqual(
            RELEASE_BUNDLE._canonical_manifest_filename(key),
            "0000-01-00-0__specimen-more-sm.json",
        )
        self.assertEqual(
            RELEASE_BUNDLE._require_canonical_manifest_filename(
                Path("0000-01-00-0__specimen-more-sm.json"), key
            ),
            "0000-01-00-0__specimen-more-sm.json",
        )
        with self.assertRaisesRegex(
            RELEASE_BUNDLE.BundleError, "filename is not canonical"
        ):
            RELEASE_BUNDLE._require_canonical_manifest_filename(
                Path("0000-01-00-0--specimen-more-sm.json"), key
            )
        source = RELEASE_BUNDLE_PATH.read_text(encoding="utf-8")
        self.assertIn("manifest filename is not canonical", source)
        self.assertNotIn(
            '/ "sealed-v3-manifests" / source.name', source
        )

    """Compiler gates that must run even without private retained evidence."""

    def test_compiler_aliases_resolve_or_are_explicitly_rejected(self):
        self.assertEqual(
            BACKEND.MANIFEST_SCHEMA,
            "170hx-xray-otp-sealed-manifest-v3",
        )
        self.assertTrue(all(
            target in COMPILER.APPROVED_PROFILES
            for target in COMPILER.PROFILE_ALIASES.values()
        ))
        for rejected in (
            "ray-best-evidence-a100-conversion",
            "xray-best-evidence-a100-conversion",
        ):
            with self.subTest(rejected=rejected):
                self.assertIn(rejected, COMPILER.REJECTED_PROFILE_ALIASES)
                with self.assertRaisesRegex(
                        COMPILER.CompileError, "separate eligible sessions"):
                    COMPILER.build_manifest(
                        Path("unused-candidate.json"), BDF, rejected,
                        Path("unused-main.bin"), Path("unused-rir.bin"),
                    )

    def test_compiler_refuses_dynamic_ff_row_above_runtime_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = write_json(directory, "candidate.json", {})
            synthetic = {
                "operation_rows": [BACKEND.COMPARE_LAST + 1],
            }
            with mock.patch.object(
                    COMPILER.OLD_SPEC, "validate_candidate_profile",
                    return_value=synthetic):
                with self.assertRaisesRegex(
                        COMPILER.CompileError, "runtime execution domain"):
                    COMPILER.build_manifest(
                        candidate_path, BDF, PROFILE,
                        Path(directory) / "unused-main.bin",
                        Path(directory) / "unused-rir.bin",
                    )

    def test_legacy_atomic_claim_cannot_be_normalized_away(self):
        with self.assertRaisesRegex(
                COMPILER.CompileError, "claiming hardware atomicity"):
            COMPILER._normalize_legacy_capture({
                "same_atomic_helper_transaction_per_pass": True,
                "hardware_atomic_snapshot": True,
            })
        normalized = COMPILER._normalize_legacy_capture({
            "same_atomic_helper_transaction_per_pass": True,
        })
        self.assertTrue(
            normalized[
                "two_passes_in_one_helper_serialized_ioctl_transaction"
            ]
        )
        self.assertFalse(normalized["hardware_atomic_snapshot"])

    def test_serialized_helper_certification_is_fail_closed(self):
        current = "two_passes_in_one_helper_serialized_ioctl_transaction"
        self.assertFalse(BACKEND._serialized_helper_certification({
            current: True,
        }))
        self.assertFalse(BACKEND._serialized_helper_certification({
            current: True,
            "hardware_atomic_snapshot": True,
        }))
        self.assertTrue(BACKEND._serialized_helper_certification({
            current: True,
            "hardware_atomic_snapshot": False,
        }))
        self.assertFalse(BACKEND._serialized_helper_certification({
            "same_atomic_helper_transaction_per_pass": True,
            "hardware_atomic_snapshot": True,
        }))

    def test_live_policy_field_set_is_exact(self):
        policy = copy.deepcopy(BACKEND.EXPECTED_LIVE_POLICY)
        BACKEND._require_exact_policy(
            policy, BACKEND.EXPECTED_LIVE_POLICY, "live_policy"
        )
        policy["unreviewed_extension"] = True
        with self.assertRaisesRegex(
                BACKEND.VfiAppendError, "field set differs"):
            BACKEND._require_exact_policy(
                policy, BACKEND.EXPECTED_LIVE_POLICY, "live_policy"
            )

    def test_v3_dispatcher_has_no_current_v2_status_or_gate_labels(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn("sealed-manifest-v2 is retired", source)
        self.assertIn('"mode_gated_v3_actions"', source)
        self.assertIn('"OFFLINE_V3_VALIDATED"', source)
        self.assertIn("manifest-only-v3", source)
        for stale in (
            "v2 execution requires",
            '"mode_gated_v2_actions"',
            '"OFFLINE_V2_VALIDATED"',
            "manifest-only-v2",
            "v2: experimental hash-sealed",
        ):
            self.assertNotIn(stale, source)

        compiler_source = COMPILER_PATH.read_text(encoding="utf-8")
        self.assertIn('"status": "v3_manifest_written"', compiler_source)
        self.assertIn(
            '"status": "valid_vfi_append_v3_manifest"', compiler_source
        )
        self.assertNotIn("v2_manifest_written", compiler_source)
        self.assertNotIn("valid_vfi_append_v2_manifest", compiler_source)

        release_source = RELEASE_BUNDLE_PATH.read_text(encoding="utf-8")
        self.assertIn('"sealed_v3_manifests"', release_source)
        self.assertIn('"sealed_v3_manifest_set"', release_source)
        self.assertNotIn("sealed_v2_manifests", release_source)
        self.assertNotIn("sealed_v2_manifest_set", release_source)


@unittest.skipUnless(
    EXTERNAL_EVIDENCE_AVAILABLE,
    "retained hardware/candidate evidence is outside the source release",
)
class ManifestValidationTests(unittest.TestCase):
    def test_every_admitted_profile_compiles_and_excludes_nvlink_targets(self):
        profiles = list(COMPILER.APPROVED_PROFILES)
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "all-admitted-candidate.json"
            report = CANDIDATE_PLANNER.build_report(
                ROOT, None, None, PACKAGE_ROOT / "170HX-XRAY.py",
                selected_profile_ids=profiles,
                physical_capture_paths=[NATIVE_CAPTURE],
                physical_card_selectors=[BDF],
                context_mode="same-capture-level3",
            )
            CANDIDATE_PLANNER.write_new_private_json(candidate_path, report)
            for profile_id in profiles:
                with self.subTest(profile_id=profile_id):
                    manifest = COMPILER.build_manifest(
                        candidate_path, BDF, profile_id, COLD_MAIN, COLD_RIR,
                        generated_utc="2026-11-23T00:00:00+00:00",
                    )
                    fields = {
                        item["field"]
                        for item in manifest["semantic_proofs"]["field_targets"]
                    }
                    operation_fields = {
                        field
                        for item in manifest["operations"]
                        for field in item["fields_intentionally_changed"]
                    }
                    self.assertNotIn("OPT_NVLINK_DISABLE", fields)
                    self.assertNotIn("OPT_NVLINK_DISABLE", operation_fields)

    def test_nvlink_global_candidate_is_categorically_non_executable(self):
        self.assertNotIn("OPT_NVLINK_DISABLE", COMPILER.APPROVED_FIELD_MODELS)
        self.assertNotIn("OPT_NVLINK_DISABLE", BACKEND.RUNTIME_FIELD_POLICY)
        self.assertNotIn("specimen-nvlink-global", COMPILER.APPROVED_PROFILES)
        self.assertNotIn(
            "specimen-nvlink-global", BACKEND.RUNTIME_PROFILE_POLICY
        )
        with self.assertRaisesRegex(
                COMPILER.CompileError,
                "profile is not in the execution-approved compiler policy"):
            COMPILER.build_manifest(
                CANDIDATE, BDF, "specimen-nvlink-global",
                COLD_MAIN, COLD_RIR,
                generated_utc="2026-11-23T00:00:00+00:00",
            )

    def test_exact_current_manifest_compiles_and_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            path, validated = validated_manifest(directory)
            self.assertEqual(validated["bdf"], BDF)
            self.assertEqual(validated["profile_id"], PROFILE)
            self.assertGreater(len(validated["operations"]), 0)
            self.assertEqual(path.stat().st_size > 0, True)

    def test_native_compiler_accepts_only_legacy_abi5_and_current_abi6(self):
        candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        card = next(item for item in candidate["cards"]
                    if item["bdf"] == BDF)
        capture_path = COMPILER._resolve_bound_file(
            CANDIDATE, card["physical_capture"]["path"]
        )
        capture = COMPILER._normalize_legacy_capture(
            json.loads(capture_path.read_text(encoding="utf-8"))
        )
        capture_card = next(
            item for item in capture["cards"]
            if item["card"]["domain_bdf"] == BDF
        )
        words = list(struct.unpack("<512I", COLD_MAIN.read_bytes()))
        identity = capture_card["card"]["xray_helper_identity"]
        original = capture_card["physical_rows"]["transaction"]
        for helper_abi in (5, 6):
            transaction = copy.deepcopy(original)
            transaction["helper_abi"] = helper_abi
            COMPILER._validate_capture_transaction(
                transaction, "main_ff", 512, words, identity
            )
        for helper_abi in (4, 7):
            transaction = copy.deepcopy(original)
            transaction["helper_abi"] = helper_abi
            with self.assertRaisesRegex(
                    COMPILER.CompileError,
                    "metadata/counter/identity contract failed"):
                COMPILER._validate_capture_transaction(
                    transaction, "main_ff", 512, words, identity
                )

    def test_current_abi6_retains_every_native_transaction_gate(self):
        candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        card = next(item for item in candidate["cards"]
                    if item["bdf"] == BDF)
        capture_path = COMPILER._resolve_bound_file(
            CANDIDATE, card["physical_capture"]["path"]
        )
        capture = COMPILER._normalize_legacy_capture(
            json.loads(capture_path.read_text(encoding="utf-8"))
        )
        capture_card = next(
            item for item in capture["cards"]
            if item["card"]["domain_bdf"] == BDF
        )
        words = list(struct.unpack("<512I", COLD_MAIN.read_bytes()))
        identity = capture_card["card"]["xray_helper_identity"]
        original = copy.deepcopy(
            capture_card["physical_rows"]["transaction"]
        )
        original["helper_abi"] = 6
        mutations = {
            "kind": "rir",
            "status": 1,
            "status_text": "not-ok",
            "transport_error": "injected",
            "protocol_errors": ["injected"],
            "failed_pass": 0,
            "failed_row": 0,
            "recovered_from_last": True,
            "driver_bound_at_entry": True,
            "otp_commands": 1,
            "row_count": 511,
            "requested_passes": 1,
            "completed_passes": 1,
            "cmd_read_commands_issued": 1023,
            "completed_data_reads_lower_bound": 1023,
            "operational_writes_issued": 2048,
            "helper_struct_size": 4319,
            "flags": "0x00000004",
            "response_kind": 2,
            "identity": dict(identity, revision="0xff"),
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                transaction = copy.deepcopy(original)
                transaction[field] = value
                with self.assertRaises(COMPILER.CompileError):
                    COMPILER._validate_capture_transaction(
                        transaction, "main_ff", 512, words, identity
                    )
        for field in (
                "protocol_valid", "certified_by_helper",
                "kernel_reported_coherent", "coherent", "counts_complete",
                "address_restored", "debug_restored"):
            with self.subTest(field=field):
                transaction = copy.deepcopy(original)
                transaction[field] = False
                with self.assertRaises(COMPILER.CompileError):
                    COMPILER._validate_capture_transaction(
                        transaction, "main_ff", 512, words, identity
                    )

    def test_cross_date_level2_candidate_cannot_compile_executable_v3(self):
        with self.assertRaises(COMPILER.CompileError):
            COMPILER.build_manifest(
                CROSS_DATE_CANDIDATE, BDF, PROFILE, COLD_MAIN, COLD_RIR,
                generated_utc="2026-11-23T00:00:00+00:00",
            )

    def assert_mutation_rejected(self, mutate):
        document = compile_manifest()
        mutate(document)
        document = reseal(document)
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(directory, "mutated.json", document)
            with self.assertRaises(BACKEND.VfiAppendError):
                BACKEND.load_and_validate_manifest(path, BDF, PROFILE)

    def test_zero_candidate_file_hash_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["candidate_source"].__setitem__("file_sha256", "0" * 64)
        )

    def test_zero_candidate_content_hash_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["candidate_source"].__setitem__(
                "report_content_sha256_excluding_self", "0" * 64
            )
        )

    def test_zero_implementation_hashes_rejected(self):
        def mutate(document):
            for key in list(document["implementation_binding"]):
                document["implementation_binding"][key] = "0" * 64
        self.assert_mutation_rejected(mutate)

    def test_zero_cold_attestation_hashes_rejected(self):
        def mutate(document):
            document["cold_baseline_attestation"]["main_file_sha256"] = "0" * 64
            document["cold_baseline_attestation"]["rir_file_sha256"] = "0" * 64
        self.assert_mutation_rejected(mutate)

    def test_zero_comparison_hashes_rejected(self):
        def mutate(document):
            document["binding"]["baseline_compare_0_487_u32le_sha256"] = "0" * 64
            document["binding"]["proposed_compare_0_487_u32le_sha256"] = "0" * 64
        self.assert_mutation_rejected(mutate)

    def test_empty_field_target_proof_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["semantic_proofs"].__setitem__("field_targets", [])
        )

    def test_deleted_live_policy_rejected(self):
        self.assert_mutation_rejected(lambda d: d.pop("live_policy"))

    def test_unknown_or_extra_setup_variant_mapping_rejected(self):
        def mutate(document):
            document["live_policy"][
                "setup_variants_by_initial_efp_status"
            ]["2"] = "attacker-variant"
        self.assert_mutation_rejected(mutate)

    def test_commit_policy_mismatch_rejected(self):
        self.assert_mutation_rejected(
            lambda document: document["commit_policy"].__setitem__(
                "minimum_commit_group_operations", 2
            )
        )

    def test_deleted_authorization_policy_rejected(self):
        self.assert_mutation_rejected(lambda d: d.pop("authorization_policy"))

    def test_downgraded_capture_context_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["binding"]["identity"].__setitem__(
                "context_mode", "cross-date-level2"
            )
        )

    def test_false_same_native_capture_binding_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["binding"]["identity"].__setitem__(
                "same_native_capture_artifact", False
            )
        )

    def test_capture_context_error_or_nonpass_status_rejected(self):
        def mutate(document):
            provenance = document["exact_current_provenance"]
            provenance["controller_context_status"] = "FAIL"
            provenance["controller_context_errors"] = ["synthetic test error"]
        self.assert_mutation_rejected(mutate)

    def test_native_main_rir_artifact_split_rejected(self):
        self.assert_mutation_rejected(
            lambda d: d["exact_current_provenance"].__setitem__(
                "rir_native_artifact_sha256", "1" * 64
            )
        )

    def test_claimed_logical_old_must_be_recomputed_from_baseline(self):
        self.assert_mutation_rejected(
            lambda d: d["operations"][0].__setitem__(
                "logical_block_old", "0xffff"
            )
        )

    def test_operation_cannot_drop_its_declared_field_changes(self):
        self.assert_mutation_rejected(
            lambda d: d["operations"][0].__setitem__(
                "fields_intentionally_changed", []
            )
        )

    def test_field_target_values_are_replayed_not_trusted(self):
        def mutate(document):
            target = document["semantic_proofs"]["field_targets"][0]
            target["current"] = "0x0"
            target["target"] = "0x0"
            target["final"] = "0x0"
            target["defect"] = "0x0"
            target["target_reached"] = True
            target["all_observed_defects_remain_disabled"] = True
        self.assert_mutation_rejected(mutate)

    def test_missing_defect_preservation_boolean_rejected(self):
        def mutate(document):
            document["semantic_proofs"]["field_targets"][0].pop(
                "all_observed_defects_remain_disabled"
            )
        self.assert_mutation_rejected(mutate)

    def test_arbitrary_field_name_rejected_by_independent_runtime_policy(self):
        def mutate(document):
            old = document["semantic_proofs"]["field_targets"][0]["field"]
            document["semantic_proofs"]["field_targets"][0]["field"] = \
                "OPT_ATTACKER_DEFINED"
            document["exact_current_provenance"][
                "field_coordinate_replay_model"
            ][0]["field"] = "OPT_ATTACKER_DEFINED"
            for operation in document["operations"]:
                operation["fields_intentionally_changed"] = [
                    "OPT_ATTACKER_DEFINED" if item == old else item
                    for item in operation["fields_intentionally_changed"]
                ]
        self.assert_mutation_rejected(mutate)

    def test_arbitrary_coordinate_rejected_by_independent_runtime_policy(self):
        self.assert_mutation_rejected(
            lambda document: document["exact_current_provenance"][
                "field_coordinate_replay_model"
            ][0]["components_lsb_first"][0].__setitem__("bit_in_block", 4)
        )

    def test_unknown_profile_rejected_even_when_hand_resealed(self):
        document = compile_manifest()
        document["binding"]["profile_id"] = "specimen-attacker-profile"
        document = reseal(document)
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(directory, "bad-profile.json", document)
            with self.assertRaises(BACKEND.VfiAppendError):
                BACKEND.load_and_validate_manifest(path, BDF)

    def test_arbitrary_stage_rejected_by_independent_runtime_policy(self):
        def mutate(document):
            old = document["semantic_proofs"]["declared_stage_order"][0]
            document["semantic_proofs"]["declared_stage_order"][0] = \
                "attacker-stage"
            for operation in document["operations"]:
                if operation["stage"] == old:
                    operation["stage"] = "attacker-stage"
        self.assert_mutation_rejected(mutate)

    def test_compiler_runtime_reconstruction_parity_is_exact(self):
        document = compile_manifest()
        # This function re-opens every source and canonical-compares the full
        # rebuilt document; a non-exception is exact compile/runtime parity.
        BACKEND._runtime_reconstruct_manifest(document)

    def test_compiler_and_runtime_common_topology_policies_are_identical(self):
        profile = "xray-common-permanent-topology"
        compiler = COMPILER.APPROVED_PROFILES[profile]
        runtime_fields, runtime_stages = BACKEND.RUNTIME_PROFILE_POLICY[profile]
        self.assertEqual(set(runtime_fields), set(compiler["fields"]))
        self.assertEqual(tuple(runtime_stages), tuple(compiler["stages"]))
        self.assertEqual(
            tuple(runtime_stages),
            (
                "memory-and-shared-prerequisites", "compute-prerequisites",
                "memory-activation", "compute-activation",
            ),
        )

    def test_stale_gen3_record_rejected_even_when_resealed(self):
        def mutate(document):
            operation = document["operations"][0]
            operation["program_word"] = "0x058009c2"
            operation["chain"] = 2
            operation["block"] = 39
            operation["logical_block_old"] = "0x0782"
            operation["logical_block_new"] = "0x0580"
            operation["fields_intentionally_changed"] = [
                "OPT_DISABLE_GEN3_SPEED"
            ]
            row = operation["row"]
            document["images"]["proposed_main_rows"][row] = "0x058009c2"
        self.assert_mutation_rejected(mutate)

    def test_duplicate_json_key_rejected_before_semantic_use(self):
        payload = (
            '{"schema":"%s","schema":"%s"}' %
            (BACKEND.MANIFEST_SCHEMA, BACKEND.MANIFEST_SCHEMA)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(payload, encoding="utf-8")
            with self.assertRaises(BACKEND.VfiAppendError):
                BACKEND.load_and_validate_manifest(path)

    def test_nonfinite_json_rejected_before_semantic_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nan.json"
            path.write_text('{"schema":NaN}', encoding="utf-8")
            with self.assertRaises(BACKEND.VfiAppendError):
                BACKEND.load_and_validate_manifest(path)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.validated = session_validated_fixture()

    def session(self, bar=None, journal=None, authorization_nonce=None,
                ownership_check=lambda: True,
                write_strategy=BACKEND.STRATEGY_EVIDENCE_GUARDED):
        if bar is None:
            bar = FakeBar(self.validated["baseline"])
        return bar, BACKEND.VfiAppendSession(
            bar, self.validated, journal=journal,
            sleeper=lambda _duration: None,
            ownership_check=ownership_check,
            authorization_nonce=authorization_nonce,
            held_baseline=self.validated["baseline"],
            write_strategy=write_strategy,
        )

    def continuation_authorizer(self):
        receipt = {"content_sha256": "3" * 64}
        contract = BACKEND.confirmation_contract(self.validated)
        answers = iter([
            contract["answer"], contract["manifest_file_sha256"],
            contract["manifest_content_sha256"], contract["bdf"],
            contract["profile_id"], contract["write_strategy"],
            contract["risk_phrase"],
            receipt["content_sha256"],
        ])
        authorization = BACKEND.perform_runtime_confirmations(
            self.validated, receipt,
            input_func=lambda _prompt: next(answers),
            stdin_isatty=True, stderr_isatty=True,
        )
        return BACKEND.make_continuation_authorizer(
            authorization,
            input_func=lambda _prompt: BACKEND.test_row_continuation_phrase(
                self.validated["operations"][0]
            ),
        )

    def test_live_preflight_reads_two_complete_passes_and_never_sets_up(self):
        bar, session = self.session()
        report = session.run(execute=False)
        self.assertEqual(report["status"], "LIVE_PREFLIGHT_PASS_NO_OTP")
        self.assertEqual(session.controller_read_commands, 1024)
        self.assertEqual(session.program_commands, 0)
        commands = [item[0] for item in bar.command_log]
        self.assertEqual(commands, [BACKEND.CMD_READ] * 1024)
        self.assertEqual(bar.regs[BACKEND.EN_SW_OVERRIDE], 0)
        self.assertEqual(bar.regs[BACKEND.EFP_STATUS], 0)

    def test_full_fake_execution_uses_one_cmd2_per_sealed_row_and_no_gpio(self):
        bar, session = self.session()
        report = session.run(
            execute=True, continue_after_test=lambda _operation: True
        )
        self.assertEqual(
            report["status"], "SESSION_ROWS_VERIFIED_COLD_VERIFY_REQUIRED"
        )
        program_commands = [entry for entry in bar.command_log
                            if entry[0] == BACKEND.CMD_PROGRAM]
        self.assertEqual(len(program_commands), len(self.validated["operations"]))
        self.assertEqual(session.program_commands, len(program_commands))
        self.assertEqual(report["armed_gates"]["setup_variant"],
                         "enable-override-sense-arm")
        allowed_write_offsets = {
            BACKEND.FUSEADDR, BACKEND.FUSEWDATA, BACKEND.FUSECTRL,
            BACKEND.EN_SW_OVERRIDE, BACKEND.SBFE_SHADOW,
            BACKEND.WDATA_WR_SECURE,
        }
        self.assertTrue(
            all(offset in allowed_write_offsets for offset, _value in bar.write_log)
        )
        for operation in self.validated["operations"]:
            self.assertEqual(bar.rows[operation["row"]], operation["word"])

    def test_initial_efp_one_uses_sealed_skip_sense_variant(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.EFP_STATUS] = 1
        journal = MemoryJournal()
        _bar, session = self.session(bar, journal=journal)
        report = session.run(execute=True, test_row_only=True)
        self.assertEqual(report["armed_gates"]["setup_variant"],
                         "enable-override-skip-sense-arm")
        self.assertFalse(any(command == BACKEND.CMD_SENSE
                             for command, _row, _word in bar.command_log))
        self.assertIn("sense-skipped-already-complete",
                      [record["event"] for record in journal.records])

    def test_efp_recovery_sense_refuses_resense_disable_without_writing_it(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.RESENSE_DISABLE] = 1
        _bar, session = self.session(bar)
        with self.assertRaisesRegex(BACKEND.VfiAppendError,
                                    "RESENSE_DISABLE"):
            session.run(execute=True, test_row_only=True)
        self.assertFalse(any(offset == BACKEND.RESENSE_DISABLE
                             for offset, _value in bar.write_log))
        self.assertFalse(any(command == BACKEND.CMD_SENSE
                             for command, _row, _word in bar.command_log))

    def test_post_sense_requires_one_shot_resense_disable_transition(self):
        bar = FakeBar(self.validated["baseline"])
        bar.sense_resense_disable_value = 0
        _bar, session = self.session(bar)
        with self.assertRaisesRegex(BACKEND.VfiAppendError,
                                    "one-shot RESENSE_DISABLE"):
            session.run(execute=True, test_row_only=True)
        self.assertTrue(any(command == BACKEND.CMD_SENSE
                            for command, _row, _word in bar.command_log))
        self.assertFalse(any(offset == BACKEND.RESENSE_DISABLE
                             for offset, _value in bar.write_log))

    def test_public_direct_strategy_senses_before_en_and_never_writes_timing(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.EFP_STATUS] = 1
        journal = MemoryJournal()
        _bar, session = self.session(
            bar, journal=journal,
            write_strategy=BACKEND.STRATEGY_PUBLIC_DIRECT,
        )
        report = session.run(execute=True, test_row_only=True)
        self.assertEqual(report["write_strategy"],
                         BACKEND.STRATEGY_PUBLIC_DIRECT)
        self.assertEqual(report["armed_gates"]["setup_variant"],
                         "sense-enable-override-arm")
        sense_index = bar.write_log.index((BACKEND.FUSECTRL, BACKEND.CMD_SENSE))
        en_index = bar.write_log.index((BACKEND.EN_SW_OVERRIDE, 1))
        self.assertLess(sense_index, en_index)
        timing = {
            BACKEND.FUSETIME_RD1, BACKEND.FUSETIME_RD2,
            BACKEND.FUSETIME_PGM1, BACKEND.FUSETIME_PGM2,
            BACKEND.FUSETIME_RD3,
        }
        self.assertFalse(any(offset in timing for offset, _value in bar.write_log))
        events = [record["event"] for record in journal.records]
        self.assertLess(events.index("sense-verified"), events.index("en-latched"))

    def test_transaction_traces_cover_both_strategies_without_execution(self):
        for strategy in BACKEND.WRITE_STRATEGIES:
            trace = BACKEND.transaction_trace(self.validated, strategy)
            self.assertEqual(trace["strategy"], strategy)
            self.assertFalse(trace["execution_performed"])
            self.assertFalse(trace["timing_registers"]["writes_permitted"])
            self.assertEqual(len(trace["row_operations"]),
                             len(self.validated["operations"]))

    def test_two_step_strategy_is_trace_only_and_expands_each_row(self):
        strategy = BACKEND.STRATEGY_INERT_THEN_ARM_TRACE
        self.assertIn(strategy, BACKEND.TRANSACTION_TRACE_STRATEGIES)
        self.assertNotIn(strategy, BACKEND.WRITE_STRATEGIES)
        trace = BACKEND.transaction_trace(self.validated, strategy)
        self.assertFalse(trace["execution_performed"])
        self.assertFalse(trace["execution_eligible"])
        loop = "\n".join(trace["program_loop"])
        self.assertIn("program chain-zero inert word exactly once", loop)
        self.assertIn("program only the sealed one-hot chain delta", loop)
        self.assertNotIn("FUSEWDATA <- sealed complete FF word", loop)
        self.assertEqual(
            len(trace["two_step_row_operations"]),
            len(self.validated["operations"]),
        )
        for source, expanded in zip(
                self.validated["operations"],
                trace["two_step_row_operations"]):
            word = source["word"]
            self.assertEqual(
                expanded["step_a_inert_word"],
                "0x%08x" % (word & ~0x1F),
            )
            self.assertEqual(expanded["step_b_armed_word"], "0x%08x" % word)
            self.assertTrue(expanded["chain_is_supported_one_hot"])
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.validate_write_strategy(strategy)

    def test_every_trace_discloses_unclosed_userspace_ownership_race(self):
        for strategy in BACKEND.TRANSACTION_TRACE_STRATEGIES:
            trace = BACKEND.transaction_trace(self.validated, strategy)
            ownership = trace["ownership_model"]
            self.assertEqual(ownership["transport"], "userspace resource0 mmap")
            self.assertFalse(ownership["kernel_owned_irreversible_transaction"])
            self.assertFalse(ownership["bind_remove_toctou_closed"])
        phrase = BACKEND.confirmation_contract(
            self.validated,
            BACKEND.STRATEGY_EVIDENCE_GUARDED,
        )["risk_phrase"]
        self.assertIn("USERSPACE OWNERSHIP RACE UNRESOLVED", phrase)

    def test_run_live_execution_refuses_implicit_strategy_before_device_access(self):
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "explicitly selected"):
            BACKEND.run_live(
                self.validated, execute=True,
                sysfs_root=Path(self.directory.name) / "must-not-open",
            )

    def test_armed_plane_suffix_change_refuses_before_first_program(self):
        for changed_row in (0, 488, 489, 511):
            with self.subTest(changed_row=changed_row):
                class PlaneChangingBar(FakeBar):
                    def write32(self, offset, value):
                        super().write32(offset, value)
                        if offset == BACKEND.EN_SW_OVERRIDE and value == 1:
                            self.rows[changed_row] ^= 0x00010000

                bar = PlaneChangingBar(self.validated["baseline"])
                _bar, session = self.session(bar)
                with self.assertRaisesRegex(
                        BACKEND.VfiAppendError,
                        "complete expected 512-row image"):
                    session.run(execute=True, test_row_only=True)
                self.assertEqual(session.program_commands, 0)
                self.assertEqual(
                    session.runtime_baseline[changed_row],
                    self.validated["baseline"][changed_row],
                )

    def test_identity_profile_forbids_test_row_stop_and_has_no_pause(self):
        validated = copy.deepcopy(self.validated)
        validated["profile_id"] = "specimen-a100-20b5-identity"
        validated["commit_policy"] = BACKEND._expected_commit_policy(
            validated["profile_id"]
        )
        rejected_bar = FakeBar(validated["baseline"])
        rejected = BACKEND.VfiAppendSession(
            rejected_bar, validated, sleeper=lambda _duration: None,
            ownership_check=lambda: True,
            held_baseline=validated["baseline"],
        )
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "forbids"):
            rejected.run(execute=True, test_row_only=True)
        self.assertEqual(rejected.program_commands, 0)
        self.assertEqual(rejected_bar.write_log, [])

        bar = FakeBar(validated["baseline"])
        session = BACKEND.VfiAppendSession(
            bar, validated, sleeper=lambda _duration: None,
            ownership_check=lambda: True,
            held_baseline=validated["baseline"],
        )
        report = session.run(execute=True, continue_after_test=None)
        self.assertEqual(report["commit_policy"][
            "minimum_commit_group_operations"], 2)
        programs = [index for index, entry in enumerate(bar.command_log)
                    if entry[0] == BACKEND.CMD_PROGRAM]
        self.assertGreaterEqual(len(programs), 2)
        reads_between = [entry for entry in
                         bar.command_log[programs[0] + 1:programs[1]]
                         if entry[0] == BACKEND.CMD_READ]
        self.assertGreaterEqual(len(reads_between), 1024)

    def test_attempt_tokens_are_unique_and_span_each_exact_lifecycle(self):
        journal = MemoryJournal()
        authorizer = self.continuation_authorizer()
        bar, session = self.session(
            journal=journal,
            authorization_nonce=authorizer.authorization_nonce,
        )
        with mock.patch.object(BACKEND.os, "isatty", return_value=True):
            session.run(execute=True, continue_after_test=authorizer)
        lifecycle_names = [
            "attempt-committed", "program-command-boundary",
            "program-command-issued", "program-command-idle", "row-verified",
            "prefix-image-verified",
        ]
        groups = []
        for operation in self.validated["operations"]:
            events = [record for record in journal.records
                      if record["details"].get("row") == operation["row"] and
                      record["event"] in lifecycle_names]
            self.assertEqual([item["event"] for item in events], lifecycle_names)
            tokens = {item["details"].get("attempt_token") for item in events}
            self.assertEqual(len(tokens), 1)
            token = next(iter(tokens))
            self.assertRegex(token, r"^[0-9a-f]{32}$")
            groups.append(token)
        self.assertEqual(len(groups), len(set(groups)))

    def test_second_attempt_has_durable_continuation_event_between_rows(self):
        journal = MemoryJournal()
        authorizer = self.continuation_authorizer()
        _bar, session = self.session(
            journal=journal,
            authorization_nonce=authorizer.authorization_nonce,
        )
        with mock.patch.object(BACKEND.os, "isatty", return_value=True):
            session.run(execute=True, continue_after_test=authorizer)
        names = [record["event"] for record in journal.records]
        first_verified = names.index("row-verified")
        continuation = names.index("continuation-authorized")
        second_attempt = names.index("attempt-committed", first_verified + 1)
        self.assertLess(first_verified, continuation)
        self.assertLess(continuation, second_attempt)
        details = journal.records[continuation]["details"]
        self.assertEqual(details.get("after_row"),
                         self.validated["operations"][0]["row"])
        self.assertRegex(details.get("authorization_nonce", ""),
                         r"^[0-9a-f]{32}$")

    def test_missing_continuation_authorization_refuses_before_setup_or_pulse(self):
        bar, session = self.session()
        with self.assertRaises(BACKEND.VfiAppendError):
            session.run(execute=True, continue_after_test=None)
        self.assertEqual(session.program_commands, 0)
        self.assertEqual(bar.regs[BACKEND.EN_SW_OVERRIDE], 0)
        self.assertFalse(any(command == BACKEND.CMD_SENSE
                             for command, _row, _word in bar.command_log))

    def test_initial_en_one_refuses_before_any_operational_write(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.EN_SW_OVERRIDE] = 1
        _bar, session = self.session(bar)
        with self.assertRaises(BACKEND.VfiAppendError):
            session.preflight()
        self.assertEqual(bar.write_log, [])

    def test_bypass_fuses_nonzero_refuses_before_any_operational_write(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.BYPASS_FUSES_STATUS] = 1
        _bar, session = self.session(bar)
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "BYPASS"):
            session.preflight()
        self.assertEqual(bar.write_log, [])

    def test_setup_requires_completed_preflight_and_is_one_shot(self):
        bar, session = self.session()
        with self.assertRaises(BACKEND.VfiAppendError):
            session.sense_and_arm()
        self.assertEqual(bar.write_log, [])
        session.preflight()
        session.sense_and_arm()
        writes = list(bar.write_log)
        with self.assertRaises(BACKEND.VfiAppendError):
            session.sense_and_arm()
        self.assertEqual(bar.write_log, writes)

    def test_preflight_is_one_shot(self):
        bar, session = self.session()
        session.preflight()
        writes = list(bar.write_log)
        with self.assertRaises(BACKEND.VfiAppendError):
            session.preflight()
        self.assertEqual(bar.write_log, writes)

    def test_program_requires_armed_state(self):
        bar, session = self.session()
        with self.assertRaises(BACKEND.VfiAppendError):
            session.program_one(self.validated["operations"][0])
        self.assertFalse(any(command == BACKEND.CMD_PROGRAM
                             for command, _row, _word in bar.command_log))

    def test_program_operation_is_one_attempt_only(self):
        bar, session = self.session()
        session.preflight()
        session.sense_and_arm()
        session.finalize_armed_runtime_baseline()
        operation = self.validated["operations"][0]
        session.program_one(operation)
        first_count = len([entry for entry in bar.command_log
                           if entry[0] == BACKEND.CMD_PROGRAM])
        with self.assertRaises(BACKEND.VfiAppendError):
            session.program_one(operation)
        second_count = len([entry for entry in bar.command_log
                            if entry[0] == BACKEND.CMD_PROGRAM])
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)

    def test_fusectrl_bit2_is_not_idle_and_poison_is_final_access(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.FUSECTRL] = BACKEND.IDLE_VALUE | 4
        _bar, session = self.session(bar)
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.gate_snapshot()
        self.assertTrue(session.poisoned)
        reads = len(bar.read_log)
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.gate_snapshot()
        self.assertEqual(len(bar.read_log), reads)

    def test_idle_requires_all_exact_done_bits(self):
        self.assertFalse(BACKEND._is_idle(0x00040000))
        self.assertFalse(BACKEND._is_idle(0x60040000))
        self.assertTrue(BACKEND._is_idle(BACKEND.IDLE_VALUE))

    def test_ownership_is_reattested_immediately_before_every_pulse(self):
        calls = []
        bar, session = self.session(
            ownership_check=lambda: calls.append(len(bar.command_log)) or True
        )
        session.run(execute=True, test_row_only=True)
        self.assertGreaterEqual(len(calls), 3)
        program_positions = [index for index, entry in enumerate(bar.command_log)
                             if entry[0] == BACKEND.CMD_PROGRAM]
        self.assertEqual(len(program_positions), 1)

    def test_mismatched_continuation_nonce_refuses_before_setup_or_pulse(self):
        journal = MemoryJournal()
        authorizer = self.continuation_authorizer()
        bar, session = self.session(
            journal=journal, authorization_nonce="f" * 32
        )
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "nonce"):
            session.run(execute=True, continue_after_test=authorizer)
        self.assertEqual(session.program_commands, 0)
        self.assertEqual(bar.regs[BACKEND.EN_SW_OVERRIDE], 0)

    def test_gate_badf_poison_is_last_mmio_access(self):
        bar = FakeBar(self.validated["baseline"])
        bar.regs[BACKEND.DSOV_STATUS] = 0xBADF0000
        _bar, session = self.session(bar)
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.gate_snapshot()
        self.assertEqual(bar.read_log[-1][0], BACKEND.DSOV_STATUS)
        reads = len(bar.read_log)
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.read_row(0)
        self.assertEqual(len(bar.read_log), reads)

    def test_fuseaddr_tagged_readback_poison_is_last_mmio_access(self):
        bar = FakeBar(self.validated["baseline"])
        bar.address_read_override = 0xBADF0000
        _bar, session = self.session(bar)
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.read_row(0)
        self.assertEqual(bar.read_log[-1][0], BACKEND.FUSEADDR)
        self.assertTrue(session.poisoned)

    def test_unknown_controller_state_stops_without_polling_again(self):
        bar = FakeBar(self.validated["baseline"])
        _bar, session = self.session(bar)
        bar.ctrl_queue = [BACKEND.IDLE_VALUE, 0x12345678, BACKEND.IDLE_VALUE]
        with self.assertRaises(BACKEND.VfiAppendPoison):
            session.read_row(0)
        ctrl_reads = [entry for entry in bar.read_log
                      if entry[0] == BACKEND.FUSECTRL]
        self.assertEqual(ctrl_reads[-1][1], 0x12345678)
        self.assertTrue(session.poisoned)

    def test_row_data_with_badf_prefix_is_data_not_transport_poison(self):
        bar = FakeBar(self.validated["baseline"])
        bar.rows[0] = 0xBADF1234
        _bar, session = self.session(bar)
        self.assertEqual(session.read_row(0), 0xBADF1234)
        self.assertFalse(session.poisoned)


@unittest.skipUnless(
    EXTERNAL_EVIDENCE_AVAILABLE,
    "manifest receipt integration needs separately retained hardware evidence",
)
class ReceiptAuthorizationJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest_path, self.validated = validated_manifest(
            self.directory.name
        )

    def make_receipt(self):
        baseline_digest = self.validated["manifest"]["binding"][
            "baseline_compare_0_487_u32le_sha256"
        ]
        answers = iter([
            BDF,
            PROFILE,
            self.validated["content_sha256"],
            baseline_digest,
            str(len(self.validated["operations"])),
            "I REVIEWED EXPERIMENTAL GA100 OTP %d ROWS" %
            len(self.validated["operations"]),
        ])
        receipt = COMPILER.collect_review_receipt(
            self.validated, input_func=lambda _prompt: next(answers),
            generated_utc="2026-11-23T00:00:00+00:00",
        )
        path = write_json(self.directory.name, "receipt.json", receipt)
        return path, BACKEND.load_and_validate_receipt(path, self.validated)

    def test_receipt_and_runtime_confirmation_bind_every_exact_value(self):
        _path, receipt = self.make_receipt()
        contract = BACKEND.confirmation_contract(self.validated)
        answers = iter([
            contract["answer"], contract["manifest_file_sha256"],
            contract["manifest_content_sha256"], contract["bdf"],
            contract["profile_id"], contract["write_strategy"],
            contract["risk_phrase"],
            receipt["content_sha256"],
        ])
        token = BACKEND.perform_runtime_confirmations(
            self.validated, receipt,
            input_func=lambda _prompt: next(answers),
            stdin_isatty=True, stderr_isatty=True,
        )
        self.assertIsNotNone(token)

    def test_receipt_operation_count_boolean_is_rejected(self):
        path, _receipt = self.make_receipt()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["operation_count"] = True
        path = write_json(self.directory.name, "bad-receipt.json", reseal(document))
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.load_and_validate_receipt(path, self.validated)

    def test_journal_hash_chain_detects_detail_tampering(self):
        journal_path = Path(self.directory.name) / (
            BACKEND._journal_name_prefix(BDF) + "hash-chain.jsonl"
        )
        journal = BACKEND.DurableJournal(
            journal_path, self.validated,
            "12345678-1234-1234-1234-123456789abc",
            hashlib.sha256(BACKEND_PATH.read_bytes()).hexdigest(),
        )
        journal.append("CREATED", "session-created")
        journal.append("REFUSED", "session-terminal", reason="offline test")
        journal.close()
        self.assertEqual(
            BACKEND.verify_journal(journal_path)["terminal_state"], "REFUSED"
        )
        records = journal_path.read_text(encoding="utf-8").splitlines()
        first = json.loads(records[0])
        first["details"]["tampered"] = True
        records[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
        journal_path.write_text("\n".join(records) + "\n", encoding="utf-8")
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.verify_journal(journal_path)

    def test_exact_bdf_device_lock_excludes_second_owner(self):
        lock_root = Path(self.directory.name) / "locks"
        lock_root.mkdir(mode=0o700)
        with BACKEND.DeviceLock(lock_root, BDF):
            with self.assertRaises(BACKEND.VfiAppendError):
                with BACKEND.DeviceLock(lock_root, BDF):
                    pass

    def test_state_directory_rejects_nonprivate_mode_and_symlink_leaf(self):
        unsafe = Path(self.directory.name) / "unsafe-state"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "0700"):
            BACKEND.DeviceLock(unsafe, BDF)
        safe = Path(self.directory.name) / "safe-state"
        safe.mkdir(mode=0o700)
        alias = Path(self.directory.name) / "state-alias"
        alias.symlink_to(safe, target_is_directory=True)
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.DeviceLock(alias, BDF)

    def test_rechained_continuation_nonce_cannot_detach_from_runtime_auth(self):
        validated = session_validated_fixture()
        journal_path = Path(self.directory.name) / (
            BACKEND._journal_name_prefix(BDF) + "lifecycle.jsonl"
        )
        journal = BACKEND.DurableJournal(
            journal_path, validated,
            "12345678-1234-1234-1234-123456789abc",
            hashlib.sha256(BACKEND_PATH.read_bytes()).hexdigest(),
        )
        journal.append("CREATED", "session-created")
        journal.append("IDENTITY_BOUND", "identity-bound")
        receipt = {"content_sha256": "3" * 64}
        contract = BACKEND.confirmation_contract(validated)
        answers = iter([
            contract["answer"], contract["manifest_file_sha256"],
            contract["manifest_content_sha256"], contract["bdf"],
            contract["profile_id"], contract["write_strategy"],
            contract["risk_phrase"],
            receipt["content_sha256"],
        ])
        authorization = BACKEND.perform_runtime_confirmations(
            validated, receipt, input_func=lambda _prompt: next(answers),
            stdin_isatty=True, stderr_isatty=True,
        )
        authorizer = BACKEND.make_continuation_authorizer(
            authorization,
            input_func=lambda _prompt: BACKEND.test_row_continuation_phrase(
                validated["operations"][0]
            ),
        )
        journal.append(
            "RUNTIME_AUTHORIZED", "runtime-authorized",
            authorization_nonce=authorization.nonce,
        )
        bar = FakeBar(validated["baseline"])
        session = BACKEND.VfiAppendSession(
            bar, validated, journal=journal, sleeper=lambda _duration: None,
            ownership_check=lambda: True,
            authorization_nonce=authorization.nonce,
            held_baseline=validated["baseline"],
        )
        with mock.patch.object(BACKEND.os, "isatty", return_value=True):
            session.run(execute=True, continue_after_test=authorizer)
        journal.append("PENDING_COLD_VERIFY", "session-terminal")
        journal.close()
        self.assertEqual(
            BACKEND.verify_journal(journal_path, validated)["terminal_state"],
            "PENDING_COLD_VERIFY",
        )

        records = [json.loads(line) for line in
                   journal_path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            if record["event"] == "continuation-authorized":
                record["details"]["authorization_nonce"] = "f" * 32
        previous = "0" * 64
        for record in records:
            record["previous_event_sha256"] = previous
            record.pop("event_sha256", None)
            record["event_sha256"] = hashlib.sha256(
                canonical_bytes(record)
            ).hexdigest()
            previous = record["event_sha256"]
        journal_path.write_text(
            "".join(json.dumps(record, sort_keys=True,
                               separators=(",", ":")) + "\n"
                    for record in records),
            encoding="utf-8",
        )
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.verify_journal(journal_path, validated)

    def test_forged_public_authorization_object_is_rejected_before_sysfs(self):
        receipt_path, receipt = self.make_receipt()
        forged = object.__new__(BACKEND.RuntimeAuthorization)
        forged.manifest_content_sha256 = self.validated["content_sha256"]
        forged.receipt_content_sha256 = receipt["content_sha256"]
        forged.bdf = BDF
        forged.profile_id = PROFILE
        journal = Path(self.directory.name) / "0000-01-00-0-forged.jsonl"
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "authorization"):
            BACKEND.run_live(
                self.validated,
                sysfs_root=Path(self.directory.name) / "missing-sysfs",
                execute=True,
                journal_path=journal,
                receipt_path=receipt_path,
                authorization=forged,
                write_strategy=BACKEND.STRATEGY_EVIDENCE_GUARDED,
            )

    def test_run_live_rejects_mismatched_continuation_nonce_before_state(self):
        receipt_path, receipt = self.make_receipt()

        def token():
            contract = BACKEND.confirmation_contract(self.validated)
            answers = iter([
                contract["answer"], contract["manifest_file_sha256"],
                contract["manifest_content_sha256"], contract["bdf"],
                contract["profile_id"], contract["write_strategy"],
                contract["risk_phrase"],
                receipt["content_sha256"],
            ])
            return BACKEND.perform_runtime_confirmations(
                self.validated, receipt,
                input_func=lambda _prompt: next(answers),
                stdin_isatty=True, stderr_isatty=True,
            )

        current = token()
        different = token()
        continuation = BACKEND.make_continuation_authorizer(
            different, input_func=lambda _prompt: "decline"
        )
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "continuation"):
            BACKEND.run_live(
                self.validated, execute=True,
                journal_path=Path(self.directory.name) / (
                    BACKEND._journal_name_prefix(BDF) + "nonce.jsonl"
                ),
                receipt_path=receipt_path,
                authorization=current,
                continue_after_test=continuation,
                write_strategy=BACKEND.STRATEGY_EVIDENCE_GUARDED,
            )

    def test_setup_failure_is_quarantined_and_never_labeled_refused(self):
        receipt_path, receipt = self.make_receipt()
        contract = BACKEND.confirmation_contract(self.validated)
        answers = iter([
            contract["answer"], contract["manifest_file_sha256"],
            contract["manifest_content_sha256"], contract["bdf"],
            contract["profile_id"], contract["write_strategy"],
            contract["risk_phrase"],
            receipt["content_sha256"],
        ])
        authorization = BACKEND.perform_runtime_confirmations(
            self.validated, receipt,
            input_func=lambda _prompt: next(answers),
            stdin_isatty=True, stderr_isatty=True,
        )
        continuation = BACKEND.make_continuation_authorizer(
            authorization, input_func=lambda _prompt: "decline"
        )

        class SetupFailBar(FakeBar):
            def write32(self, offset, value):
                if offset == BACKEND.EN_SW_OVERRIDE:
                    raise OSError("synthetic EN write failure")
                return super().write32(offset, value)

        state_root = Path(self.directory.name) / "canonical-state"
        state_root.mkdir(mode=0o700)
        boot_id = Path(self.directory.name) / "boot-id"
        boot_id.write_text(
            "12345678-1234-1234-1234-123456789abc\n", encoding="ascii"
        )
        journal_path = state_root / (
            BACKEND._journal_name_prefix(BDF) + "setup-failure.jsonl"
        )
        held_path = write_json(
            self.directory.name, "held-state-native.json", {"test": True}
        ).resolve()
        held_state = {
            "path": str(held_path), "file_sha256": "a" * 64,
            "runtime_baseline": list(self.validated["baseline"]),
            "rir": list(self.validated["rir"]),
            "held_suffix_u32le_sha256": BACKEND._packed_sha256(
                self.validated["baseline"][488:]
            ),
        }
        live = {
            "identity": {
                "domain_bdf": BDF, "vendor": 0x10DE, "device": 0x20C2,
                "subsystem_vendor": 0x10DE, "subsystem_device": 0x1585,
                "revision": 0xA1,
            },
            "resource0": "/offline/fake-resource0",
            "resolved_device_path": "/offline/fake-device",
            "bar0_size": BACKEND.MAP_SIZE,
            "pci_command": "0x0002",
            "driver_unbound": True,
            "driver_override": BACKEND.DRIVER_OVERRIDE_SENTINEL,
            "device_inode": [1, 2],
        }
        bar = SetupFailBar(self.validated["baseline"])
        with mock.patch.object(BACKEND, "CANONICAL_STATE_DIR", str(state_root)), \
                mock.patch.object(BACKEND, "CANONICAL_BOOT_ID_PATH", str(boot_id)), \
                mock.patch.object(BACKEND, "inspect_live_identity",
                                  return_value=live), \
                mock.patch.object(BACKEND, "validate_held_state_capture",
                                  return_value=held_state):
            with self.assertRaises(BACKEND.VfiAppendPoison):
                BACKEND.run_live(
                    self.validated,
                    sysfs_root="/sys/bus/pci/devices",
                    execute=True,
                    journal_path=journal_path,
                    receipt_path=receipt_path,
                    authorization=authorization,
                    continue_after_test=continuation,
                    held_state_capture_path=held_path,
                    bar_factory=lambda _path: bar,
                    write_strategy=BACKEND.STRATEGY_EVIDENCE_GUARDED,
                )
        records = [json.loads(line) for line in
                   journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["state"], "QUARANTINED")
        self.assertNotEqual(records[-1]["state"], "REFUSED")
        self.assertEqual(
            BACKEND.verify_journal(journal_path, self.validated)["terminal_state"],
            "QUARANTINED",
        )


@unittest.skipUnless(
    EXTERNAL_EVIDENCE_AVAILABLE,
    "cold-verifier integration needs separately retained hardware evidence",
)
class ColdVerifierContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest_path, self.validated = validated_manifest(
            self.directory.name
        )
        self.journal_path = self._make_pending_journal()
        self.journal_status = BACKEND.verify_journal(
            self.journal_path, self.validated
        )
        expected = list(self.validated["baseline"])
        for operation in self.validated["operations"]:
            expected[operation["row"]] = operation["word"]
        self.expected = expected
        self.capture_path = self._write_native_capture(
            "post-cold-capture.json", expected, self.validated["rir"]
        )
        self.held_capture_path = self._write_native_capture(
            "held-state-capture.json", self.validated["baseline"],
            self.validated["rir"]
        )
        self.post_boot = Path(self.directory.name) / "post-boot-id"
        self.post_boot.write_text(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="ascii"
        )
        self.attestation = self._attestation_document()
        self.attestation_path = write_json(
            self.directory.name, "power-attestation.json", self.attestation
        )

    def _native_identity_document(self):
        source = self.validated["identity"]
        expected = self.validated["post_cold_expectations"]
        result = {
            key: source[key] for key in (
                "domain_bdf", "vendor", "device", "subsystem_vendor",
                "subsystem_device", "revision",
            )
        }
        result.update({key: expected[key]
                       for key in ("domain_bdf", "vendor", "device")})
        return result

    def _native_transaction(self, kind, words):
        encoded = ["0x%08x" % word for word in words]
        count = len(words)
        snapshot = {
            "fusectrl": "0x%08x" % BACKEND.IDLE_VALUE,
            "en_sw_override": "0x00000000",
            "iff_sw_fusing": "0x00000000",
            "debugctrl": "0x00000000",
            "disable_sw_override_status": "0x00000000",
            "bypass_fuses_status": "0x00000000",
        }
        pass_hash = BACKEND._packed_sha256(words)
        return {
            "kind": kind,
            "certified_by_helper": True,
            "protocol_valid": True,
            "coherent": True,
            "kernel_reported_coherent": True,
            "counts_complete": True,
            "driver_bound_at_entry": False,
            "requested_passes": 2,
            "completed_passes": 2,
            "row_count": count,
            "cmd_read_commands_issued": count * 2,
            "completed_data_reads_lower_bound": count * 2,
            "otp_commands": 0,
            "status": 0,
            "status_text": "ok",
            "failed_pass": None,
            "failed_row": None,
            "transport_error": None,
            "protocol_errors": [],
            "recovered_from_last": False,
            "address_restored": True,
            "debug_restored": True,
            "helper_abi": 5,
            "identity": self._native_identity_document(),
            "entry": snapshot,
            "exit": copy.deepcopy(snapshot),
            "passes": [encoded, list(encoded)],
            "pass_hashes": [pass_hash, pass_hash],
        }

    def _write_native_capture(self, name, main, rir):
        identity = self._native_identity_document()
        main_tx = self._native_transaction("main_ff", main)
        rir_tx = self._native_transaction("rir", rir)
        document = {
            "schema": "170hx-xray-v1",
            "command": "capture",
            "status": "captured",
            "tool": {
                "name": "170HX-XRAY", "version": "test-native-1",
                "sha256": "a" * 64,
            },
            "access": {
                "canonical_requested_access": "semantic-read",
                "effective_access": "semantic-read",
                "counts_complete": True,
                "controller_snapshot_performed": True,
                "otp_program_commands": 0,
                "write_level_requested": False,
            },
            "safety": {
                "has_live_write_primitive": False,
                "otp_execution_supported": False,
                "volatile_write_execution_supported": False,
            },
            "target_scope": {
                "all_targets_acknowledged": True,
                "controller_sequence_stopped": False,
                "skipped_bdfs": [],
                "resolved_bdfs": [BDF],
            },
            "cards": [{
                "card": dict(identity, driver=None,
                             xray_helper_identity=copy.deepcopy(identity)),
                "access": {
                    "canonical_requested_access": "semantic-read",
                    "effective_access": "semantic-read",
                    "counts_complete": True,
                    "controller_transaction_error": None,
                },
                "physical_rows": {
                    "available": True, "coherent": True,
                    "certification": {
                        "available": True, "driver_unbound": True,
                        "full_domain_identity_bound": True,
                        "same_atomic_helper_transaction_per_pass": True,
                        "software_iff_redirection_excluded": True,
                    },
                    "rows": main_tx["passes"][0],
                    "passes": main_tx["passes"],
                    "pass_hashes": main_tx["pass_hashes"],
                    "transaction": main_tx,
                },
                "rir_rows": {
                    "available": True, "coherent": True,
                    "certification": {
                        "available": True, "driver_unbound": True,
                        "full_domain_identity_bound": True,
                        "same_atomic_helper_transaction_per_pass": True,
                        "debug_rwl_restored": True,
                    },
                    "rows": rir_tx["passes"][0],
                    "passes": rir_tx["passes"],
                    "pass_hashes": rir_tx["pass_hashes"],
                    "transaction": rir_tx,
                },
            }],
        }
        return write_json(self.directory.name, name, document)

    def _make_pending_journal(self, test_row_only=False):
        path = Path(self.directory.name) / (
            BACKEND._journal_name_prefix(BDF) +
            ("cold-test-row.jsonl" if test_row_only else "cold-test.jsonl")
        )
        journal = BACKEND.DurableJournal(
            path, self.validated,
            "12345678-1234-1234-1234-123456789abc",
            hashlib.sha256(BACKEND_PATH.read_bytes()).hexdigest(),
        )
        journal.append("CREATED", "session-created")
        journal.append("IDENTITY_BOUND", "identity-bound")
        receipt = {"content_sha256": "3" * 64}
        contract = BACKEND.confirmation_contract(self.validated)
        answers = iter([
            contract["answer"], contract["manifest_file_sha256"],
            contract["manifest_content_sha256"], contract["bdf"],
            contract["profile_id"], contract["write_strategy"],
            contract["risk_phrase"],
            receipt["content_sha256"],
        ])
        authorization = BACKEND.perform_runtime_confirmations(
            self.validated, receipt,
            input_func=lambda _prompt: next(answers),
            stdin_isatty=True, stderr_isatty=True,
        )
        authorizer = BACKEND.make_continuation_authorizer(
            authorization,
            input_func=lambda _prompt: BACKEND.test_row_continuation_phrase(
                self.validated["operations"][0]
            ),
        )
        journal.append(
            "RUNTIME_AUTHORIZED", "runtime-authorized",
            authorization_nonce=authorization.nonce,
        )
        bar = FakeBar(self.validated["baseline"])
        session = BACKEND.VfiAppendSession(
            bar, self.validated, journal=journal,
            sleeper=lambda _duration: None,
            ownership_check=lambda: True,
            authorization_nonce=authorization.nonce,
            held_baseline=self.validated["baseline"],
        )
        if test_row_only:
            session.run(execute=True, test_row_only=True)
            terminal = "TEST_ROW_PENDING_COLD_VERIFY"
        else:
            with mock.patch.object(BACKEND.os, "isatty", return_value=True):
                session.run(execute=True, continue_after_test=authorizer)
            terminal = "PENDING_COLD_VERIFY"
        journal.append(terminal, "session-terminal")
        journal.close()
        return path

    def _attestation_document(self):
        identity = self._native_identity_document()
        return {
            "schema": BACKEND.COLD_ATTESTATION_SCHEMA,
            "operator_confirmed": True,
            "method": "complete-slot-and-aux-power-removal",
            "slot_power_removed": True,
            "aux_power_removed": True,
            "complete_power_loss_measured": True,
            "manifest_content_sha256": self.validated["content_sha256"],
            "journal_last_event_sha256":
                self.journal_status["last_event_sha256"],
            "bdf": BDF,
            "powered_off_utc": "2026-11-23T00:01:00+00:00",
            "powered_on_utc": "2026-11-23T00:02:00+00:00",
            "operator": "offline-test-operator",
            "host_remained_powered": False,
            "post_cold_identity": {
                key: identity[key] for key in (
                    "domain_bdf", "vendor", "device", "subsystem_vendor",
                    "subsystem_device", "revision",
                )
            },
        }

    def verify(self, attestation_path=None, capture_path=None, post_boot=None):
        return BACKEND.verify_cold_persistence(
            self.validated, self.journal_path,
            capture_path or self.capture_path,
            attestation_path or self.attestation_path,
            post_boot_id_path=post_boot or self.post_boot,
        )

    def test_exact_two_pass_main_rir_and_power_identity_close_persistence(self):
        receipt = self.verify()
        self.assertEqual(receipt["status"], "COLD_PERSISTENCE_VERIFIED")
        self.assertEqual(
            receipt["verified_rows"],
            [operation["row"] for operation in self.validated["operations"]],
        )
        self.assertFalse(receipt["planner_noop_verified"])
        self.assertFalse(receipt["feature_validation_complete"])
        receipt_path = write_json(
            self.directory.name,
            self.journal_path.name + ".cold.json",
            receipt,
        )
        loaded = BACKEND.load_and_validate_cold_receipt(
            receipt_path, self.validated, self.journal_path
        )
        self.assertEqual(loaded["receipt"]["status"],
                         "COLD_PERSISTENCE_VERIFIED")
        BACKEND.assert_no_unresolved_journals(
            self.directory.name, self.validated,
            Path(self.directory.name) /
            (BACKEND._journal_name_prefix(BDF) + "next-session.jsonl"),
        )
        proposed = copy.deepcopy(self.validated)
        proposed["content_sha256"] = "d" * 64
        proposed["file_sha256"] = "e" * 64
        BACKEND.assert_no_unresolved_journals(
            self.directory.name, proposed,
            Path(self.directory.name) /
            (BACKEND._journal_name_prefix(BDF) + "replacement.jsonl"),
        )

    def test_current_abi6_native_cold_capture_is_accepted(self):
        document = json.loads(
            self.capture_path.read_text(encoding="utf-8")
        )
        for name in ("physical_rows", "rir_rows"):
            document["cards"][0][name]["transaction"]["helper_abi"] = 6
        path = write_json(
            self.directory.name, "post-cold-capture-abi6.json", document
        )
        self.assertEqual(
            self.verify(capture_path=path)["status"],
            "COLD_PERSISTENCE_VERIFIED",
        )

    def test_unknown_native_helper_abi_is_rejected(self):
        document = json.loads(
            self.capture_path.read_text(encoding="utf-8")
        )
        for name in ("physical_rows", "rir_rows"):
            document["cards"][0][name]["transaction"]["helper_abi"] = 7
        path = write_json(
            self.directory.name, "post-cold-capture-abi7.json", document
        )
        with self.assertRaisesRegex(
                BACKEND.VfiAppendError, "exact native certified"):
            self.verify(capture_path=path)

    def test_held_state_capture_binds_full_main_suffix_and_live_rir(self):
        held = BACKEND.validate_held_state_capture(
            self.held_capture_path, self.validated
        )
        self.assertEqual(held["runtime_baseline"], self.validated["baseline"])
        self.assertEqual(held["rir"], self.validated["rir"])
        self.assertEqual(
            held["held_suffix_u32le_sha256"],
            BACKEND._packed_sha256(self.validated["baseline"][488:]),
        )

    def test_held_state_capture_requires_dsov_zero_in_both_transactions(self):
        document = json.loads(
            self.held_capture_path.read_text(encoding="utf-8")
        )
        for name in ("physical_rows", "rir_rows"):
            transaction = document["cards"][0][name]["transaction"]
            transaction["entry"]["disable_sw_override_status"] = "0x00000001"
            transaction["exit"]["disable_sw_override_status"] = "0x00000001"
        path = write_json(self.directory.name, "held-dsov-one.json", document)
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "DSOV"):
            BACKEND.validate_held_state_capture(path, self.validated)

    def test_minimal_self_hashed_cold_receipt_cannot_close_journal(self):
        minimal = reseal({
            "schema": BACKEND.COLD_RECEIPT_SCHEMA,
            "status": "COLD_PERSISTENCE_VERIFIED",
            "feature_validation_complete": False,
        })
        path = write_json(self.directory.name, "minimal-cold.json", minimal)
        with self.assertRaises(BACKEND.VfiAppendError):
            BACKEND.load_and_validate_cold_receipt(
                path, self.validated, self.journal_path
            )

    def test_bdf_prefixed_nonjournal_extension_blocks_new_execution(self):
        hidden = Path(self.directory.name) / (
            BACKEND._journal_name_prefix(BDF) + "hidden.txt"
        )
        hidden.write_text("not a journal\n", encoding="utf-8")
        with self.assertRaisesRegex(BACKEND.VfiAppendError, "grammar"):
            BACKEND.assert_no_unresolved_journals(
                self.directory.name, self.validated,
                Path(self.directory.name) /
                (BACKEND._journal_name_prefix(BDF) + "next.jsonl"),
            )

    def test_one_bit_main_divergence_rejected(self):
        altered = list(self.expected)
        altered[self.validated["operations"][0]["row"]] ^= 1
        bad = self._write_native_capture(
            "bad-main-capture.json", altered, self.validated["rir"]
        )
        with self.assertRaises(BACKEND.VfiAppendError):
            self.verify(capture_path=bad)

    def test_test_row_terminal_accepts_exact_one_operation_prefix_only(self):
        prefix_journal = self._make_pending_journal(test_row_only=True)
        prefix_status = BACKEND.verify_journal(
            prefix_journal, self.validated
        )
        expected = list(self.validated["baseline"])
        operation = self.validated["operations"][0]
        expected[operation["row"]] = operation["word"]
        capture = self._write_native_capture(
            "prefix-native-capture.json", expected, self.validated["rir"]
        )
        attestation = copy.deepcopy(self.attestation)
        attestation["journal_last_event_sha256"] = prefix_status[
            "last_event_sha256"
        ]
        attestation_path = write_json(
            self.directory.name, "prefix-power.json", attestation
        )
        receipt = BACKEND.verify_cold_persistence(
            self.validated, prefix_journal, capture, attestation_path,
            post_boot_id_path=self.post_boot,
        )
        self.assertEqual(receipt["verified_rows"], [operation["row"]])

    def test_rir_divergence_rejected(self):
        altered = list(self.validated["rir"])
        altered[0] ^= 1
        bad = self._write_native_capture(
            "bad-rir-capture.json", self.expected, altered
        )
        with self.assertRaises(BACKEND.VfiAppendError):
            self.verify(capture_path=bad)

    def test_unmeasured_power_loss_rejected(self):
        document = copy.deepcopy(self.attestation)
        document["complete_power_loss_measured"] = False
        path = write_json(self.directory.name, "bad-power.json", document)
        with self.assertRaises(BACKEND.VfiAppendError):
            self.verify(attestation_path=path)

    def test_post_cold_identity_mismatch_rejected(self):
        document = copy.deepcopy(self.attestation)
        document["post_cold_identity"]["device"] = "0x20b5"
        path = write_json(self.directory.name, "bad-identity.json", document)
        with self.assertRaises(BACKEND.VfiAppendError):
            self.verify(attestation_path=path)

    def test_unchanged_boot_id_requires_measured_slot_only_attestation(self):
        same_boot = Path(self.directory.name) / "same-boot-id"
        same_boot.write_text(
            "12345678-1234-1234-1234-123456789abc\n", encoding="ascii"
        )
        with self.assertRaises(BACKEND.VfiAppendError):
            self.verify(post_boot=same_boot)


if __name__ == "__main__":
    unittest.main()
