# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline safety/provenance tests for the XRAY registry and helper boundary."""

import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "170HX-XRAY.py"
KERNEL = ROOT / "kernel" / "170hx_xray.c"
UAPI = ROOT / "kernel" / "170hx_xray_uapi.h"


def load_xray():
    spec = importlib.util.spec_from_file_location("xray_registry_test_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegistryAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xray = load_xray()
        cls.kernel_text = KERNEL.read_text(encoding="utf-8")
        cls.uapi_text = UAPI.read_text(encoding="utf-8")

    def test_read_target_identity_scope(self):
        self.assertEqual(
            self.xray.GA100_READ_TARGET_DEVICE_IDS,
            frozenset((0x20B5, 0x20C2, 0x2080)),
        )
        self.assertEqual(self.xray.DEFAULT_DEVICE_IDS, frozenset((0x20C2,)))
        for device in self.xray.GA100_READ_TARGET_DEVICE_IDS:
            identity = {
                "domain_bdf": "0000:01:00.0",
                "vendor": "0x10de",
                "device": "0x%04x" % device,
                "subsystem_vendor": "0x10de",
                "subsystem_device": "0x1585",
                "revision": "0xa1",
            }
            result = self.xray._strict_identity_status(identity)
            self.assertTrue(result["valid"], (device, result))
            native_identity = dict(identity, generation="0x1")
            errors = []
            parsed = self.xray._capture_identity_tuple(
                native_identity, "test", errors
            )
            self.assertIsNotNone(parsed)
            self.assertEqual(errors, [])
        rejected = self.xray._strict_identity_status({
            "domain_bdf": "0000:01:00.0",
            "vendor": "0x10de",
            "device": "0x20b0",
            "subsystem_vendor": "0x10de",
            "subsystem_device": "0x1585",
            "revision": "0xa1",
        })
        self.assertFalse(rejected["valid"])

    def test_positive_float_cli_type_rejects_nonfinite_values(self):
        for value in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(self.xray.argparse.ArgumentTypeError):
                    self.xray.positive_finite_float(value)
        self.assertEqual(self.xray.positive_finite_float("0.25"), 0.25)

    def test_kernel_identity_scope_matches_userspace(self):
        definitions = {
            name: int(value, 16)
            for name, value in re.findall(
                r"^#define\s+(XRAY_DEVICE_[A-Z0-9_]+)\s+(0x[0-9a-fA-F]+)",
                self.kernel_text,
                re.MULTILINE,
            )
        }
        self.assertEqual(
            set(definitions.values()), self.xray.GA100_READ_TARGET_DEVICE_IDS
        )
        for name in definitions:
            self.assertGreaterEqual(self.kernel_text.count("pdev->device != " + name), 1)

    def test_python_kernel_read_allowlists_are_identical(self):
        offsets, duplicates = self.xray.parse_kernel_read_allowlist_text(
            self.kernel_text
        )
        self.assertFalse(duplicates)
        self.assertEqual(set(offsets), set(self.xray.SAFE_READ_OFFSETS))
        self.assertEqual(len(offsets), len(set(offsets)))

    def test_registry_metadata_census_is_explicit(self):
        census = self.xray.registry_provenance_census()
        self.assertEqual(census["catalogued_unique_addresses"], 1093)
        self.assertEqual(census["safe_unique_addresses"], 1068)
        self.assertEqual(census["stateful_or_unsafe_entries"], 25)
        # Three newly decoded records are still the same allowlisted reads:
        # the DP selector and the SS0/SS1 feature-override words.  Their exact
        # bit ranges are present in the pinned GA100 register descriptor, so
        # moving them from raw-word-only to decoded changes only this census.
        self.assertEqual(census["safe_with_fields"], 92)
        self.assertEqual(census["safe_raw_word_only"], 976)
        self.assertEqual(len(census["missing_interpretation_confidence"]), 463)
        self.assertEqual(census["missing_identity_confidence"], [])
        self.assertEqual(census["missing_source_anchor"], [])
        self.assertEqual(len(census["generic_source_anchor"]), 56)
        self.assertEqual(
            sum(census["source_anchor_class_counts"].values()),
            census["catalogued_entries"],
        )
        self.assertEqual(census["planning_enabled_entries"], [])
        self.assertTrue(census["completeness"]["address_policy_metadata_complete"])
        self.assertTrue(census["completeness"]["identity_confidence_complete"])
        self.assertFalse(census["completeness"]["specific_source_anchor_complete"])
        self.assertFalse(census["completeness"]["interpretation_confidence_complete"])
        self.assertFalse(census["completeness"]["safe_field_decode_complete"])
        self.assertFalse(census["completeness"]["planning_authority_complete"])
        self.assertFalse(census["direct_header_comparison"]["available"])

    def test_nvlink_widths_and_nvenc_inventory_registers_are_exact(self):
        expected = {
            "OPT_NVLINK_DISABLE": (0x00820684, "DATA", 2, 0),
            "NVLINK_DEFECTIVE": (0x0082068C, "DATA", 2, 0),
            "STATUS_OPT_NVLINK": (0x00820DB8, "DATA", 15, 0),
            "CTRL_OPT_NVLINK": (0x008209B8, "DATA", 15, 0),
            "PTOP_SCAL_NUM_NVENCS": (0x0002244C, "VALUE", 4, 0),
            "CTRL_OPT_NVENC": (0x00820968, "DATA", 1, 0),
        }
        for name, (address, field_name, msb, lsb) in expected.items():
            with self.subTest(name=name):
                register = self.xray.REGISTER_BY_NAME[name]
                self.assertEqual(register["address"], address)
                self.assertEqual(
                    register["fields"],
                    [{"name": field_name, "msb": msb, "lsb": lsb}],
                )
                self.assertTrue(register["capture_safe"])
                self.assertFalse(register["planning_allowed"])
                self.assertIn(address, self.xray.SAFE_READ_OFFSETS)

        media = self.xray.GOALS["nvdec_nvjpg"]["registers"]
        self.assertIn("PTOP_SCAL_NUM_NVENCS", media)
        self.assertIn("CTRL_OPT_NVENC", media)

    def test_registry_addresses_and_access_effects_are_bounded(self):
        for register in self.xray.CURATED_REGISTERS:
            self.assertEqual(register["address"] & 3, 0, register["name"])
            self.assertLess(register["address"], self.xray.XRAY_BAR0_MIN_LENGTH)
            if register["access_effect"] != "pure_read":
                self.assertFalse(register["capture_safe"], register["name"])
                self.assertNotIn(register["address"], self.xray.SAFE_READ_OFFSETS)
        self.assertNotIn(0x0082000C, self.xray.SAFE_READ_OFFSETS)

    def test_live_all_safe_badf_availability_does_not_block_controller_context(self):
        unavailable = {
            "GPC0_TPC0_TEX_LOD_DBG", "SKED_ACTIVITY", "SKED_CG",
            "SKED_HWW_ESR", "SKED_HWW_ESR_EN", "SKED_PM",
        }
        for name in unavailable:
            register = self.xray.REGISTER_BY_NAME[name]
            self.assertEqual(register["badf"], "availability", name)
            self.assertEqual(
                self.xray.classify_mmio_value(0xBADF1201, register),
                "unavailable_expected",
            )

        sentinels = {
            name: {"status": "ok", "value": "0x00000000"}
            for name in self.xray.SENTINEL_NAMES
        }
        passes = []
        for number in (1, 2):
            passes.append({
                "number": number,
                "before_sentinels": sentinels,
                "after_sentinels": sentinels,
                "registers": {
                    name: {
                        "status": "unavailable_expected",
                        "value": "0xbadf1201",
                        "u32": 0xBADF1201,
                    }
                    for name in unavailable
                },
            })
        summary = self.xray.summarize_passes(passes)
        self.assertTrue(summary["coherent"], summary)
        self.assertEqual(summary["read_errors"], [])

        unexpected = self.xray.REGISTER_BY_NAME["FUSECTRL"]
        self.assertEqual(unexpected["badf"], "error")
        self.assertEqual(
            self.xray.classify_mmio_value(0xBADF1201, unexpected),
            "badf_response",
        )

    def test_header_comparison_detects_field_conflict_without_granting_access(self):
        good = self.xray.parse_header_text(
            "#define NV_FUSE_FUSECTRL 0x00820000 /* RW-4R */\n"
            "#define NV_FUSE_FUSECTRL_CMD 1:0 /* -WIVF */\n"
            "#define NV_FUSE_UNLISTED 0x0082f000 /* R--4R */\n",
            source="synthetic",
            sha256="0" * 64,
        )
        good_census = self.xray.registry_provenance_census(good)
        comparison = good_census["direct_header_comparison"]
        self.assertEqual(comparison["field_conflicts"], [])
        self.assertEqual(
            len(comparison["confirmed_curated_fields"])
            + len(comparison["curated_fields_not_confirmed_by_exact_name"]),
            sum(len(item["fields"]) for item in self.xray.SAFE_READ_REGISTERS),
        )
        self.assertIn("0x0082f000", comparison["header_addresses_not_allowlisted"])
        self.assertNotIn(0x0082F000, self.xray.SAFE_READ_OFFSETS)

        bad = self.xray.parse_header_text(
            "#define NV_FUSE_FUSECTRL 0x00820000 /* RW-4R */\n"
            "#define NV_FUSE_FUSECTRL_CMD 2:0 /* -WIVF */\n",
            source="synthetic-conflict",
            sha256="1" * 64,
        )
        conflicts = self.xray.registry_provenance_census(bad)[
            "direct_header_comparison"
        ]["field_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field"], "CMD")

    def test_kernel_exposes_no_programming_ioctl_or_generic_write(self):
        ioctl_names = re.findall(
            r"^#define\s+(XRAY_IOC_[A-Z0-9_]+)\s+_IO",
            self.uapi_text,
            re.MULTILINE,
        )
        self.assertEqual(
            set(ioctl_names),
            {"XRAY_IOC_INFO", "XRAY_IOC_READ32", "XRAY_IOC_DUMP", "XRAY_IOC_LAST"},
        )
        self.assertNotRegex(self.kernel_text, r"\.compat_ioctl\s*=")
        self.assertNotRegex(self.kernel_text, r"#define\s+XRAY_FUSEWDATA\s")
        self.assertNotIn("XRAY_CTRL_CMD_WRITE", self.kernel_text)
        self.assertIn("module_param(allow_a100_controller, bool, 0400);", self.kernel_text)
        self.assertRegex(
            self.kernel_text,
            r"card->pdev->device == XRAY_DEVICE_A100_PCIE\s*&&\s*"
            r"!allow_a100_controller",
        )
        write_targets = set(re.findall(
            r"xray_wr\(card,\s*(XRAY_[A-Z0-9_]+)", self.kernel_text
        ))
        self.assertEqual(
            write_targets, {"XRAY_FUSEADDR", "XRAY_FUSECTRL", "XRAY_DEBUGCTRL"}
        )

    def test_abi_and_access_ladder_are_mechanically_locked(self):
        self.assertEqual(
            self.xray.ACCESS_LEVELS,
            (
                "passive", "bar-read", "glitch-read", "semantic-read", "plan",
                "volatile-write", "otp-write",
            ),
        )
        self.assertEqual(self.xray.canonical_access_mode("read-write"), "volatile-write")
        self.assertEqual(self.xray.canonical_access_mode("full-write"), "otp-write")
        self.assertEqual(self.xray.XRAY_HELPER_ABI, 6)
        self.assertEqual(self.xray.XRAY_IOC_INFO, 0x85A05801)
        self.assertEqual(self.xray.XRAY_IOC_READ32, 0xC0305802)
        self.assertEqual(self.xray.XRAY_IOC_DUMP, 0xD0E05803)
        self.assertEqual(self.xray.XRAY_IOC_LAST, 0xD0E05804)
        self.assertEqual(self.xray.XRAY_CAP_A100_CONTROLLER, 1 << 6)
        self.assertRegex(
            self.uapi_text,
            r"#define\s+XRAY_CAP_A100_CONTROLLER\s+\(1U\s*<<\s*6\)",
        )
        self.assertIn(
            "info->capabilities |= XRAY_CAP_A100_CONTROLLER;", self.kernel_text
        )
        for structure, expected_size in (
            ("xray_identity", 32),
            ("xray_card_info", 88),
            ("xray_info", 1440),
            ("xray_reg32", 48),
            ("xray_controller_snapshot", 64),
            ("xray_dump", 4320),
        ):
            self.assertIn(
                "BUILD_BUG_ON(sizeof(struct %s) != %d);" % (structure, expected_size),
                self.kernel_text,
            )
        self.assertFalse(hasattr(self.xray.BoundGpcProbeBackend, "write32"))
        self.assertFalse(hasattr(self.xray.SysfsResourceBackend, "write32"))
        self.assertFalse(hasattr(self.xray.XrayHelperDevice, "write32"))

        helper = object.__new__(self.xray.XrayHelperDevice)
        helper.path = "/dev/test"
        helper.abi_version = self.xray.XRAY_HELPER_ABI
        helper.module_generation = 1
        helper.cards = []
        helper.capabilities = self.xray.XRAY_CAP_READ32
        self.assertFalse(
            helper.info_document()["capability_flags"]["a100_20b5_controller"]
        )
        helper.capabilities |= self.xray.XRAY_CAP_A100_CONTROLLER
        self.assertTrue(
            helper.info_document()["capability_flags"]["a100_20b5_controller"]
        )

    def test_every_access_name_fails_closed_without_a_live_backend(self):
        for requested in self.xray.ACCESS_LEVELS:
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "capture",
                    "--backend", "metadata", "--no-driver-queries",
                    "--access", requested, "--output", "-",
                ],
                check=False, capture_output=True, text=True, timeout=30,
            )
            artifact = json.loads(completed.stdout)
            if requested == "glitch-read" and artifact["status"] == (
                    "glitch_phenotype_not_qualified"):
                self.assertEqual(completed.returncode, 2, completed.stderr)
            else:
                self.assertEqual(completed.returncode, 0, completed.stderr)
            access = artifact["access"]
            self.assertEqual(access["requested_access"], requested)
            self.assertEqual(access["effective_access"], "passive")
            self.assertEqual(access["direct_user_mmio_writes"], 0)
            self.assertEqual(access["controller_read_commands_issued"], 0)
            self.assertEqual(access["otp_program_commands"], 0)


if __name__ == "__main__":
    unittest.main()
