#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed tests for zero-write glitch observation and target scope."""

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "170HX-XRAY.py"
SPEC = importlib.util.spec_from_file_location("xray_glitch_read_test_target", SCRIPT)
XRAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(XRAY)


def stable(raw):
    return {"status": "ok", "raw": XRAY.hex32(raw)}


class GlitchReadLayoutTests(unittest.TestCase):
    def phenotype(self, device="0x2080", dsov=0):
        return {
            "card": {"device": device, "domain_bdf": "0000:01:00.0"},
            "capture": {"coherence": {"coherent": True}},
            "registers": {
                "DISABLE_SW_OVERRIDE_STATUS": stable(dsov),
                "EN_SW_OVERRIDE": stable(0),
                "IFF_SW_FUSING": stable(0),
                "DEBUGCTRL": stable(0),
                "FUSECTRL": stable(0xE0040000),
                "ENABLE_FUSE_PROGRAM_STATUS": stable(1),
                "DISABLE_FUSE_PROGRAM_STATUS": stable(0),
                "BYPASS_FUSES_STATUS": stable(0),
                "CRC_STATUS": stable(0x55),
            },
        }

    def test_glitch_qualification_requires_identity_dsov_and_context(self):
        qualified = XRAY.qualify_zero_write_glitch_observation(self.phenotype())
        self.assertTrue(qualified["qualified"])
        self.assertEqual(qualified["operational_device_mmio_writes"], 0)
        self.assertFalse(qualified["otp_eligibility_claimed"])
        self.assertFalse(
            XRAY.qualify_zero_write_glitch_observation(
                self.phenotype(dsov=1)
            )["qualified"]
        )
        self.assertFalse(
            XRAY.qualify_zero_write_glitch_observation(
                self.phenotype(device="0x20c2")
            )["qualified"]
        )

    def test_glitch_capture_requires_opaque_driver_queries_off(self):
        args = XRAY.build_parser().parse_args([
            "capture", "--access", "glitch-read", "--backend", "metadata",
        ])
        with self.assertRaisesRegex(XRAY.AuditError, "--no-driver-queries"):
            XRAY.command_live(args)

    def test_glitch_full_dump_isolated_from_semantic_prefix(self):
        ordinary = argparse.Namespace(max_access="semantic-read", level=[])
        self.assertEqual(
            XRAY._full_dump_requested_levels(ordinary),
            ["passive", "bar-read", "semantic-read"],
        )
        glitch = argparse.Namespace(max_access="glitch-read", level=[])
        self.assertEqual(XRAY._full_dump_requested_levels(glitch), ["glitch-read"])
        mixed = argparse.Namespace(
            max_access="passive", level=["bar-read", "glitch-read"]
        )
        with self.assertRaisesRegex(XRAY.AuditError, "captured alone"):
            XRAY._full_dump_requested_levels(mixed)

    def test_unknown_census_accounts_for_every_catalog_entry(self):
        census = XRAY.unknown_register_census({}, "all-safe")
        self.assertEqual(census["catalogued_entries"], len(XRAY.CURATED_REGISTERS))
        self.assertEqual(census["stateful_ports_read"], 0)
        self.assertEqual(census["uncatalogued_offsets_read"], 0)
        self.assertEqual(
            census["counts_by_disposition"]["not_read_stateful_or_unsafe"],
            len(XRAY.CURATED_REGISTERS) - len(XRAY.SAFE_READ_REGISTERS),
        )

    def _pci_function(self, root, bdf, device):
        path = root / bdf
        (path / "power").mkdir(parents=True)
        values = {
            "vendor": "0x10de\n", "device": "0x%04x\n" % device,
            "class": "0x030200\n", "subsystem_vendor": "0x10de\n",
            "subsystem_device": "0x1585\n", "revision": "0xa1\n",
            "power/runtime_status": "active\n", "power_state": "D0\n",
        }
        for relative, value in values.items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="ascii")

    def test_all_targets_and_exact_bdf_cover_all_supported_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pci"
            for index, device in enumerate((0x20C2, 0x20B5, 0x2080), 1):
                self._pci_function(root, "0000:%02x:00.0" % index, device)
            output = Path(directory) / "all.json"
            args = XRAY.build_parser().parse_args([
                "capture", "--access", "passive", "--backend", "metadata",
                "--no-driver-queries", "--all-targets", "--sysfs-root", str(root),
                "--output", str(output),
            ])
            self.assertEqual(XRAY.command_live(args), 0)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                {card["card"]["device"] for card in artifact["cards"]},
                {"0x20b5", "0x20c2", "0x2080"},
            )

            exact = Path(directory) / "exact.json"
            args = XRAY.build_parser().parse_args([
                "capture", "--access", "passive", "--backend", "metadata",
                "--no-driver-queries", "--bdf", "0000:02:00.0",
                "--sysfs-root", str(root), "--output", str(exact),
            ])
            self.assertEqual(XRAY.command_live(args), 0)
            selected = json.loads(exact.read_text(encoding="utf-8"))["cards"]
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["card"]["device"], "0x20b5")


if __name__ == "__main__":
    unittest.main()
