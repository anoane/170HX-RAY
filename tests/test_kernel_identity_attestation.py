#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static, hardware-inert checks for the kernel helper's PCI identity gate."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
KERNEL = ROOT / "kernel" / "170hx_xray.c"
UAPI = ROOT / "kernel" / "170hx_xray_uapi.h"
UAPI_ABI_TEST = ROOT / "kernel" / "170hx_xray_uapi_abi_test.c"
PEER = WORKSPACE / (
    "170HX-XRAY-SANITIZED" if ROOT.name == "170HX-XRAY" else "170HX-XRAY"
)


def function_body(source, name):
    # Linux clang-format may place the function name on the line after its
    # return type.  Exclude punctuation so this cannot walk across a prototype
    # or object definition while accepting either kernel layout.
    match = re.search(
        r"^static\b[A-Za-z0-9_\s*]+?\b" + re.escape(name) + r"\s*\(",
        source,
        re.M,
    )
    if not match:
        raise AssertionError("function not found: %s" % name)
    opening = source.find("{", match.end())
    if opening < 0:
        raise AssertionError("function has no body: %s" % name)
    depth = 0
    for offset in range(opening, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : offset]
    raise AssertionError("unterminated function: %s" % name)


class KernelIdentityAttestationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = KERNEL.read_text(encoding="utf-8")
        cls.uapi = UAPI.read_text(encoding="utf-8")

    def test_live_config_dword_is_compared_to_cached_and_frozen_identity(self):
        body = function_body(self.source, "xray_attest_pci_identity")
        self.assertIn(
            "pci_read_config_dword(pdev, PCI_VENDOR_ID, &live_id)", body
        )
        self.assertIn("live_vendor = live_id & 0xffffU", body)
        self.assertIn("live_device = live_id >> 16", body)
        self.assertIn("live_vendor != pdev->vendor", body)
        self.assertIn("live_device != pdev->device", body)
        self.assertIn("live_vendor != expected_vendor", body)
        self.assertIn("live_device != expected_device", body)
        self.assertIn("return -EIO", body)
        self.assertIn("return -ESTALE", body)
        self.assertIn("u16 mapped_vendor", self.source)
        self.assertIn("u16 mapped_device", self.source)

    def test_mapping_attests_before_resources_and_after_iomap(self):
        body = function_body(self.source, "xray_map_card")
        calls = [
            match.start()
            for match in re.finditer(r"xray_attest_pci_identity\s*\(", body)
        ]
        self.assertEqual(len(calls), 2)
        resource = body.index("pci_resource_start")
        iomap = body.index("pci_iomap_range")
        accept = body.index("card->pdev = pci_dev_get")
        self.assertLess(calls[0], resource)
        self.assertLess(iomap, calls[1])
        self.assertLess(calls[1], accept)

    def test_common_state_validator_rechecks_live_identity(self):
        body = function_body(self.source, "xray_validate_device_state")
        presence = body.index("pci_device_is_present")
        attestation = body.index("xray_attest_card_identity")
        command = body.index("pci_read_config_word")
        self.assertLess(presence, attestation)
        self.assertLess(attestation, command)

    def test_read32_has_immediate_entry_and_exit_attestation(self):
        body = function_body(self.source, "xray_ioctl_read32")
        general = body.index("xray_validate_live_device")
        entry = body.index("xray_attest_card_identity", general)
        mmio = body.index("request.value = xray_rd", entry)
        exit_check = body.index("xray_validate_device_state", mmio)
        self.assertLess(general, entry)
        self.assertLess(entry, mmio)
        self.assertLess(mmio, exit_check)

    def test_dump_has_immediate_entry_and_exit_attestation(self):
        body = function_body(self.source, "xray_ioctl_dump")
        general = body.index("xray_validate_live_device")
        entry = body.index("xray_attest_card_identity", general)
        transaction = body.index("xray_run_dump", entry)
        exit_check = body.index("xray_validate_device_state", transaction)
        poison = body.index("card->poisoned = true", exit_check)
        self.assertLess(general, entry)
        self.assertLess(entry, transaction)
        self.assertLess(transaction, exit_check)
        self.assertLess(exit_check, poison)

    def test_info_attests_before_reporting_cached_identity(self):
        body = function_body(self.source, "xray_ioctl_info")
        self.assertLess(
            body.index("xray_attest_card_identity"), body.index("xray_fill_identity")
        )

    def test_controller_idle_requires_the_complete_evidenced_value(self):
        self.assertRegex(
            self.source,
            r"(?m)^#define\s+XRAY_CTRL_IDLE_VALUE\s+0xe0040000U$",
        )
        body = function_body(self.source, "xray_controller_idle")
        self.assertRegex(
            body,
            r"return\s+fusectrl\s*==\s*XRAY_CTRL_IDLE_VALUE\s*;",
        )
        self.assertNotIn("XRAY_CTRL_STATE_MASK", body)
        self.assertNotIn("XRAY_CTRL_CMD_MASK", body)

    def test_helper_write_alphabet_excludes_programming(self):
        call_offsets = set(
            re.findall(
                r"xray_wr\s*\(\s*card\s*,\s*(XRAY_[A-Z0-9_]+)", self.source
            )
        )
        self.assertEqual(
            call_offsets, {"XRAY_FUSEADDR", "XRAY_FUSECTRL", "XRAY_DEBUGCTRL"}
        )
        self.assertNotRegex(self.source, r"(?m)^#define\s+XRAY_FUSEWDATA\s")
        self.assertIn(
            "xray_wr(card, XRAY_FUSECTRL, XRAY_CTRL_CMD_READ, dump)", self.source
        )
        self.assertNotRegex(self.source, r"(?m)^\s*\.compat_ioctl\s*=")

    def test_ioctl_concurrency_and_module_lifetime_contract(self):
        for name in ("xray_ioctl_read32", "xray_ioctl_dump", "xray_ioctl_last"):
            body = function_body(self.source, name)
            self.assertIn("mutex_lock_interruptible(&card->lock)", body)
            self.assertIn("mutex_unlock(&card->lock)", body)
        for name in ("xray_ioctl_read32", "xray_ioctl_dump"):
            body = function_body(self.source, name)
            self.assertLess(
                body.index("mutex_lock_interruptible(&card->lock)"),
                body.index("device_trylock(&card->pdev->dev)"),
            )
            self.assertIn("pci_request_region", body)
            self.assertIn("pci_release_region", body)
        open_body = function_body(self.source, "xray_open")
        release_body = function_body(self.source, "xray_release")
        self.assertIn("atomic_cmpxchg(&xray_opened, 0, 1)", open_body)
        self.assertIn("atomic_set(&xray_opened, 0)", release_body)
        self.assertIn(".owner = THIS_MODULE", self.source)
        exit_body = function_body(self.source, "xray_exit")
        self.assertLess(
            exit_body.index("misc_deregister"), exit_body.index("xray_unmap_cards")
        )

    def test_transaction_fault_unwind_order_is_lifo(self):
        read_body = function_body(self.source, "xray_ioctl_read32")
        read_steps = [
            read_body.index("request.value = xray_rd"),
            read_body.index("release_region:"),
            read_body.index("pci_release_region"),
            read_body.index("xray_put_runtime_ref"),
            read_body.index("device_unlock"),
            read_body.index("mutex_unlock"),
            read_body.index("copy_to_user"),
        ]
        self.assertEqual(read_steps, sorted(read_steps))

        dump_body = function_body(self.source, "xray_ioctl_dump")
        dump_steps = [
            dump_body.index("xray_run_dump"),
            dump_body.index("release_region:"),
            dump_body.index("pci_release_region"),
            dump_body.index("xray_put_runtime_ref"),
            dump_body.index("device_unlock"),
            dump_body.index("mutex_unlock"),
            dump_body.index("copy_to_user"),
        ]
        self.assertEqual(dump_steps, sorted(dump_steps))

    def test_interrupted_controller_cleanup_is_bounded_and_uninterruptible(self):
        wait_body = function_body(self.source, "xray_wait_idle_cleanup")
        self.assertNotIn("signal_pending", wait_body)
        self.assertIn("XRAY_POLL_TIMEOUT_MS", wait_body)
        restore_body = function_body(self.source, "xray_restore")
        self.assertRegex(
            restore_body,
            r"if\s*\(xray_wait_idle_cleanup\(card\)\)\s*\{[\s\S]*?"
            r"return\s+-EUCLEAN\s*;\s*\}",
        )
        self.assertLess(
            restore_body.index("xray_wait_idle_cleanup"),
            restore_body.index("xray_wr(card, XRAY_FUSEADDR"),
        )

    def test_every_uncertain_controller_read_path_poison_quarantines(self):
        body = function_body(self.source, "xray_run_dump")
        rir_latch = re.search(
            r"if\s*\(xray_rd\(card, XRAY_DEBUGCTRL\)[\s\S]*?"
            r"ret\s*=\s*-EIO\s*;[\s\S]*?"
            r"force_poison\s*=\s*true\s*;[\s\S]*?goto\s+restore\s*;",
            body,
        )
        self.assertIsNotNone(rir_latch)
        read_failure = re.search(
            r"ret\s*=\s*xray_read_one\([\s\S]*?"
            r"if\s*\(ret\)\s*\{[\s\S]*?"
            r"force_poison\s*=\s*true\s*;[\s\S]*?"
            r"goto\s+failed_row\s*;",
            body,
        )
        self.assertIsNotNone(read_failure)
        incoherent = re.search(
            r"if\s*\(!dump->coherent\)\s*\{[\s\S]*?"
            r"ret\s*=\s*-EILSEQ\s*;[\s\S]*?"
            r"force_poison\s*=\s*true\s*;",
            body,
        )
        self.assertIsNotNone(incoherent)
        self.assertIn("module reload does not prove cold recovery", self.source)

    def test_abi6_identity_and_config_offset_are_compile_time_locked(self):
        self.assertRegex(
            self.uapi, r"(?m)^#define\s+XRAY_ABI_VERSION\s+6U$"
        )
        self.assertEqual(len(re.findall(r"^#define XRAY_IOC_", self.uapi, re.M)), 5)
        self.assertIn("BUILD_BUG_ON(PCI_VENDOR_ID != 0x00)", self.source)
        self.assertIn('MODULE_VERSION("4.2.0")', self.source)
        self.assertIn(
            "READ_ONCE(card->pdev->dev.power.runtime_status)", self.source
        )

    @unittest.skipUnless(
        PEER.is_dir(),
        "peer edition is intentionally absent from a standalone source release",
    )
    def test_normal_and_sanitized_kernel_abi_sources_match(self):
        self.assertTrue(PEER.is_dir())
        self.assertEqual(
            KERNEL.read_bytes(), (PEER / "kernel" / "170hx_xray.c").read_bytes()
        )
        self.assertEqual(
            UAPI.read_bytes(), (PEER / "kernel" / "170hx_xray_uapi.h").read_bytes()
        )
        self.assertEqual(
            UAPI_ABI_TEST.read_bytes(),
            (PEER / "kernel" / "170hx_xray_uapi_abi_test.c").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
