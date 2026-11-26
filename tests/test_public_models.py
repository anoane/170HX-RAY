#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for dated public-model corrections and non-execution gates."""

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "170HX-XRAY.py"


def load_xray():
    spec = importlib.util.spec_from_file_location("xray_public_model_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xray = load_xray()

    def one_catalog_entry(self, name):
        matches = [
            item for item in self.xray.LOGICAL_FIELD_CATALOG
            if item["name"] == name
        ]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    def test_current_classifier_and_cooldown_claims_supersede_old_behavior(self):
        claims = self.xray.PUBLIC_MODEL_CLAIMS["persistent_dsov0"]
        joined = "\n".join(item["claim"] + " " + item["disposition"] for item in claims)
        self.assertIn("makes the EN_SW_OVERRIDE=1 classification write opt-in", joined)
        self.assertIn("10-second inter-shot cooldown", joined)
        self.assertIn("pre-7a3c7a3 every-shot EN_SW write", joined)
        self.assertNotIn("current public console's classifier writes EN_SW_OVERRIDE=1", joined)

    def test_ss0_ss1_iff_candidates_are_exact_but_non_executable(self):
        expected = {
            "FEATURE_OVERRIDE_SM_SPEED_SELECT": (
                0x0082381C, 0x88888888, [0x082381FD, 0x88888888], [484, 485]
            ),
            "FEATURE_OVERRIDE_SM_SPEED_SELECT_1": (
                0x00823820, 0x00000008, [0x0823823D, 0x00000008], [486, 487]
            ),
        }
        for name, (address, value, words, rows) in expected.items():
            item = self.one_catalog_entry(name)
            self.assertEqual(item["plane"], "IFF-candidate")
            self.assertEqual(item["target_address"], address)
            self.assertEqual(item["reported_value"], value)
            self.assertEqual(item["reported_words"], words)
            self.assertEqual(item["reported_rows"], rows)
            self.assertFalse(item["execution_supported"])
            self.assertFalse(item["planning_allowed"])
            self.assertIn("not a universal allocation", item["placement_boundary"])

    def test_conventional_dp_ecc_fields_remain_single_non_execution_candidates(self):
        expected = {
            "OPT_SM_SPEED_SELECT_DP": (26, 99),
            "OPT_SM_SPEED_SELECT_DP_CONTROL": (27, 65),
            "OPT_ECC_EN": (28, 99),
            "OPT_ECC_EN_CONTROL": (29, 99),
        }
        for name, (bit, certainty) in expected.items():
            item = self.one_catalog_entry(name)
            self.assertEqual(item["plane"], "conventional")
            self.assertEqual((item["primary_row"], item["redundant_row"]), (6, 7))
            self.assertEqual(item["bit"], bit)
            self.assertEqual(item["certainty_percent"], certainty)
            self.assertFalse(item["planning_allowed"])

    def test_public_recipe_claims_retain_scope_and_executor_boundary(self):
        ecc = "\n".join(
            item["claim"] + " " + item["disposition"]
            for item in self.xray.PUBLIC_MODEL_CLAIMS["ecc"]
        )
        speed = "\n".join(
            item["claim"] + " " + item["disposition"]
            for item in self.xray.PUBLIC_MODEL_CLAIMS["oc_frequencies"]
        )
        self.assertIn("row-6/7 bit 28", ecc)
        self.assertIn("cannot program conventional rows", ecc)
        self.assertIn("0x082381fd/0x88888888", speed)
        self.assertIn("0x0823823d/0x00000008", speed)
        self.assertIn("does not pass it to the FF-only OTP executor", speed)


if __name__ == "__main__":
    unittest.main()
