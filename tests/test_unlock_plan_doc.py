# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lock UNLOCK_PLAN.md to the planner/compiler/runtime contracts."""

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "UNLOCK_PLAN.md"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized(value):
    if isinstance(value, dict):
        return {str(key): normalized(value[key]) for key in sorted(value)}
    if isinstance(value, (set, frozenset)):
        return [normalized(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value


def planning_contract():
    frontend = load_module("xray_unlock_doc_frontend", ROOT / "170HX-XRAY.py")
    candidate = load_module(
        "xray_unlock_doc_candidate", ROOT / "tools" / "170hx_candidate_plan.py"
    )
    compiler = load_module(
        "xray_unlock_doc_compiler", ROOT / "tools" / "170hx_vfi_append_manifest.py"
    )
    backend = load_module(
        "xray_unlock_doc_backend", ROOT / "otp_vfi_append.py"
    )
    catalog = []
    for item in frontend.LOGICAL_FIELD_CATALOG:
        if item.get("plane") in ("IFF-candidate", "conventional") or item.get(
            "certainty", item.get("certainty_percent", 0)
        ) == 0:
            # Sanitized editions intentionally replace only attribution prose;
            # the shared contract hashes semantic coordinates/policy instead.
            catalog.append({
                key: value for key, value in item.items()
                if key not in ("sources", "source_class")
            })
    return normalized({
        "schema": "170hx-xray-unlock-plan-doc-contract-v1",
        "versions": {
            "frontend": frontend.TOOL_VERSION,
            "candidate": candidate.SCRIPT_VERSION,
            "compiler": compiler.TOOL_VERSION,
            "backend_id": backend.BACKEND_ID,
        },
        "goal_ids": sorted(frontend.GOALS),
        "planning_models": frontend.PLANNING_MODELS,
        "dump_input_formats": frontend.DUMP_INPUT_FORMATS,
        "dump_output_formats": frontend.DUMP_OUTPUT_FORMATS,
        "candidate_physical_input_formats": candidate.PHYSICAL_INPUT_FORMATS,
        "candidate_live_input_formats": candidate.LIVE_INPUT_FORMATS,
        "candidate_output_formats": candidate.PLAN_OUTPUT_FORMATS,
        "research_profiles": candidate.PROFILE_SPECS,
        "profile_aliases": candidate.PROFILE_ALIASES,
        "candidate_field_specs": candidate.FIELD_SPECS,
        "candidate_pcie_bit_specs": candidate.PCIE_BIT_SPECS,
        "compiler_profiles": compiler.APPROVED_PROFILES,
        "compiler_field_models": compiler.APPROVED_FIELD_MODELS,
        "runtime_profiles": backend.RUNTIME_PROFILE_POLICY,
        "runtime_field_policy": backend.RUNTIME_FIELD_POLICY,
        "non_executable_catalog": catalog,
    })


def contract_sha256():
    payload = json.dumps(
        planning_contract(), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class UnlockPlanDocumentationTests(unittest.TestCase):
    def test_contract_digest_and_inventory(self):
        text = DOC.read_text(encoding="utf-8")
        match = re.search(
            r"<!-- XRAY-PLAN-CONTRACT-SHA256: ([0-9a-f]{64}) -->", text
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), contract_sha256())

        contract = planning_contract()
        appendix = re.search(
            r"<!-- XRAY-PLAN-CONTRACT-JSON:BEGIN -->\s*"
            r"```json\s*(.*?)\s*```\s*"
            r"<!-- XRAY-PLAN-CONTRACT-JSON:END -->",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(appendix)
        self.assertEqual(json.loads(appendix.group(1)), contract)
        self.assertEqual(
            appendix.group(1),
            json.dumps(contract, sort_keys=True, indent=2, ensure_ascii=True),
        )
        required_tokens = set(contract["goal_ids"])
        required_tokens.update(contract["dump_input_formats"])
        required_tokens.update(contract["dump_output_formats"])
        required_tokens.update(contract["candidate_physical_input_formats"])
        required_tokens.update(contract["candidate_live_input_formats"])
        required_tokens.update(contract["candidate_output_formats"])
        required_tokens.update(contract["candidate_field_specs"])
        required_tokens.update(contract["candidate_pcie_bit_specs"])
        required_tokens.update(contract["compiler_profiles"])
        required_tokens.update(contract["runtime_profiles"])
        required_tokens.update(
            profile["id"] for profile in contract["research_profiles"]
        )
        required_tokens.update(
            stage["name"]
            for profile in contract["research_profiles"]
            for stage in profile["stages"]
        )
        missing = sorted(token for token in required_tokens if token not in text)
        self.assertEqual(missing, [])

    def test_compiler_and_runtime_independent_allowlists_match(self):
        contract = planning_contract()
        self.assertEqual(
            set(contract["compiler_profiles"]), set(contract["runtime_profiles"])
        )
        for profile_id in contract["compiler_profiles"]:
            compiler = contract["compiler_profiles"][profile_id]
            runtime = contract["runtime_profiles"][profile_id]
            self.assertEqual(compiler["fields"], runtime[0])
            self.assertEqual(compiler["stages"], runtime[1])
        self.assertEqual(
            set(contract["compiler_field_models"]),
            set(contract["runtime_field_policy"]),
        )

    def test_candidate_review_and_execution_counts_are_separate(self):
        contract = planning_contract()
        profiles = {
            item["id"]: item for item in contract["research_profiles"]
        }
        review = {
            profile_id for profile_id, profile in profiles.items()
            if profile.get("review_only")
        }
        self.assertEqual(len(profiles), 18)
        self.assertEqual(len(review), 6)
        self.assertEqual(len(contract["compiler_profiles"]), 7)
        self.assertTrue(review.isdisjoint(contract["compiler_profiles"]))
        self.assertTrue(review.isdisjoint(contract["runtime_profiles"]))
        self.assertIn("specimen-nvlink-global", profiles)
        self.assertNotIn("specimen-nvlink-global", contract["compiler_profiles"])
        self.assertNotIn("OPT_NVLINK_DISABLE", contract["compiler_field_models"])
        self.assertNotIn("OPT_NVLINK_DISABLE", contract["runtime_field_policy"])

    def test_sanitized_and_full_operational_contracts_are_equal(self):
        sibling_name = (
            "170HX-XRAY-SANITIZED"
            if ROOT.name == "170HX-XRAY" else "170HX-XRAY"
        )
        sibling_test = ROOT.parent / sibling_name / "tests" / Path(__file__).name
        if not sibling_test.is_file():
            self.skipTest("sibling edition is not present")
        sibling = load_module(
            "xray_unlock_doc_sibling_%s" % sibling_name.replace("-", "_"),
            sibling_test,
        )
        self.assertEqual(planning_contract(), sibling.planning_contract())

    def test_release_bundle_uses_current_policy_and_labels_history(self):
        candidate = load_module(
            "xray_unlock_doc_release_candidate",
            ROOT / "tools" / "170hx_candidate_plan.py",
        )
        compiler = load_module(
            "xray_unlock_doc_release_compiler",
            ROOT / "tools" / "170hx_vfi_append_manifest.py",
        )
        release = load_module(
            "xray_unlock_doc_release_bundle",
            ROOT / "tools" / "170hx_full_release_bundle.py",
        )
        self.assertEqual(
            release.CANDIDATE_PROFILES,
            tuple(item["id"] for item in candidate.PROFILE_SPECS),
        )
        self.assertEqual(
            release.CURRENT_MANIFEST_PROFILES,
            tuple(compiler.APPROVED_PROFILES),
        )
        self.assertEqual(
            release.MANIFEST_PROFILES, release.CURRENT_MANIFEST_PROFILES
        )
        self.assertEqual(len(release.CURRENT_MANIFEST_PROFILES), 7)
        self.assertNotIn(
            "specimen-nvlink-global", release.CURRENT_MANIFEST_PROFILES
        )
        self.assertEqual(len(release.HISTORICAL_4_2_REPORT_MANIFEST_PROFILES), 8)
        self.assertIn(
            "specimen-nvlink-global",
            release.HISTORICAL_4_2_REPORT_MANIFEST_PROFILES,
        )


if __name__ == "__main__":
    unittest.main()
