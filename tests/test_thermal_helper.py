#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hardware-inert contract tests for the constrained GA100 thermal helper."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
KERNEL = ROOT / "kernel" / "170hx_xray_therm.c"
UAPI = ROOT / "kernel" / "170hx_xray_therm_uapi.h"
ABI_TEST = ROOT / "kernel" / "170hx_xray_therm_uapi_abi_test.c"
MAKEFILE = ROOT / "kernel" / "Makefile.therm"
PEER = WORKSPACE / (
    "170HX-XRAY-SANITIZED" if ROOT.name == "170HX-XRAY" else "170HX-XRAY"
)


def function_body(source: str, name: str) -> str:
    # Linux clang-format may place the function name on the line after its
    # return type.  Exclude punctuation so this cannot walk across a prototype
    # or object definition while accepting either kernel layout.
    match = re.search(
        r"^static\b[A-Za-z0-9_\s*]+?\b" + re.escape(name) + r"\s*\(",
        source,
        re.M,
    )
    if not match:
        raise AssertionError(f"function not found: {name}")
    opening = source.find("{", match.end())
    if opening < 0:
        raise AssertionError(f"function has no body: {name}")
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : offset]
    raise AssertionError(f"unterminated function: {name}")


class ThermalHelperContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = KERNEL.read_text(encoding="utf-8")
        cls.uapi = UAPI.read_text(encoding="utf-8")

    def test_surface_is_two_fixed_ioctls_and_not_generic_mmio(self):
        ioctls = re.findall(
            r"^#define\s+(XRAY_THERM_IOC_[A-Z0-9_]+)\s+\\?\s*_IO",
            self.uapi,
            re.M,
        )
        self.assertEqual(ioctls, ["XRAY_THERM_IOC_INFO", "XRAY_THERM_IOC_CAPTURE"])
        self.assertNotRegex(self.source, r"(?m)^\s*\.compat_ioctl\s*=")
        for forbidden in ("READ32", "WRITE32", "MMIO_READ", "MMIO_WRITE"):
            self.assertNotIn(forbidden, self.uapi)

    def test_write_alphabet_is_exact_and_never_ts_auto(self):
        writes = re.findall(r"writel\s*\(([^;]+)\);", self.source)
        self.assertEqual(len(writes), 2)
        self.assertIn("XRAY_THERM_SNAPSHOT_TRIGGER", writes[0])
        self.assertIn("XRAY_THERM_SNAPSHOT_SELECTOR", writes[1])
        self.assertNotIn("XRAY_THERM_TS_AUTO", "\n".join(writes))
        self.assertEqual(
            self.source.count("xray_therm_write_trigger(card, capture);"), 1
        )
        self.assertEqual(
            set(
                re.findall(
                    r"^#define\s+XRAY_THERM_SNAPSHOT_(SELECTOR|TRIGGER)\s+"
                    r"(0x[0-9a-f]+U)$",
                    self.source,
                    re.M,
                )
            ),
            {("SELECTOR", "0x00020840U"), ("TRIGGER", "0x0002085cU")},
        )

    def test_snapshot_has_8x3_race_checks_and_unconditional_restoration(self):
        body = function_body(self.source, "xray_therm_capture_snapshot")
        self.assertIn("gpc < XRAY_THERM_GPC_COUNT", body)
        self.assertIn("bjt < XRAY_THERM_BJT_COUNT", body)
        self.assertIn("sample->selector_before != selector", body)
        self.assertIn("sample->selector_after != selector", body)
        self.assertIn("XRAY_THERM_SELECTOR_IS_VALID(saved_selector)", body)
        self.assertLess(
            body.index("XRAY_THERM_SELECTOR_IS_VALID(saved_selector)"),
            body.index("xray_therm_write_trigger(card, capture)"),
        )
        self.assertIn("restore_selector:", body)
        restore = body.index("restore_selector:")
        self.assertIn(
            "xray_therm_write_selector(card, saved_selector, capture)", body[restore:]
        )
        self.assertIn("capture->selector_restored", body[restore:])
        self.assertIn("xray_therm_finish_context(card, capture)", body[restore:])
        self.assertNotIn("signal_pending", body[restore:])
        abi_test = ABI_TEST.read_text(encoding="utf-8")
        for token in (
            "XRAY_THERM_SELECTOR_IS_VALID(0U)",
            "XRAY_THERM_SELECTOR_IS_VALID(0x00070002U)",
            "XRAY_THERM_SELECTOR_IS_VALID(0x00080000U)",
            "XRAY_THERM_SELECTOR_IS_VALID(0x00000003U)",
        ):
            self.assertIn(token, abi_test)

    def test_correct_ts_auto_metadata_and_population_evidence(self):
        self.assertRegex(
            self.source,
            r"(?m)^#define\s+XRAY_THERM_TS_AUTO_POLLING\s+BIT\(15\)$",
        )
        self.assertNotIn("BIT(17)", self.source)
        self.assertIn("0x00821004U", self.source)
        self.assertIn("0x00820c14U", self.source)
        self.assertIn("0x00820c1cU", self.source)
        pure = function_body(self.source, "xray_therm_capture_pure")
        self.assertIn("capture->opt_ts_dis_mask", pure)
        self.assertIn("capture->status_opt_gpc", pure)
        self.assertIn("capture->selector_dependent_skipped_mask", pure)
        self.assertIn("capture->snapshot_as_found_selector_before", pure)
        self.assertIn("capture->snapshot_as_found_selector_after", pure)
        for forbidden in (
            "capture->current_temperature = xray_therm_read",
            "capture->current_offset_temperature = xray_therm_read",
            "capture->current_raw_code = xray_therm_read",
            "capture->snapshot_as_found_temperature = xray_therm_read",
            "capture->snapshot_as_found_offset_temperature = xray_therm_read",
        ):
            self.assertNotIn(forbidden, pure)
        fixed = function_body(self.source, "xray_therm_read_fixed_context")
        self.assertNotIn("context->gpc_tsense_override =", fixed)
        self.assertNotIn("context->gpc_tsense_hs_offset =", fixed)
        self.assertIn("capture->status_opt_fbio", pure)
        self.assertNotIn("capture->snapshot_as_found_stable", pure)

    def test_ids_privilege_permissions_and_bound_gate_are_exact(self):
        for value in ("0x20b5", "0x20c2", "0x2080"):
            self.assertIn(value, self.source)
        self.assertEqual(len(re.findall(r"0x20(?:b5|c2|80)", self.source)), 3)
        self.assertIn("capable(CAP_SYS_RAWIO)", self.source)
        self.assertIn(".mode = 0600", self.source)
        self.assertIn("atomic_cmpxchg(&xray_therm_opened, 0, 1)", self.source)
        self.assertRegex(
            self.source,
            r"module_param\(allow_bound_snapshot,\s*bool,\s*0400\)",
        )
        body = function_body(self.source, "xray_therm_ioctl_capture")
        self.assertIn("!allow_bound_snapshot", body)
        self.assertIn("XRAY_THERM_CAPTURE_F_ALLOW_BOUND", body)
        self.assertRegex(
            body,
            r"XRAY_THERM_CAPTURE_F_ALLOW_BOUND\s*\|[\s\S]*?"
            r"XRAY_THERM_CAPTURE_F_HBM_DATA\)\)[\s\S]*?"
            r"!\(capture->flags & XRAY_THERM_CAPTURE_F_SNAPSHOT\)",
        )

    def test_semantic_liveness_gate_precedes_every_write(self):
        body = function_body(self.source, "xray_therm_ioctl_capture")
        cached_liveness = body.index("xray_therm_capture_has_liveness(capture)")
        snapshot_call = body.index("xray_therm_capture_snapshot(card, capture)")
        self.assertLess(cached_liveness, snapshot_call)
        self.assertIn("ret = -ENODEV", body[cached_liveness:snapshot_call])
        gate = function_body(self.source, "xray_therm_capture_has_liveness")
        for token in ("capture->aggregate[0]", "capture->sentinels_entry[0]"):
            self.assertIn(token, gate)
        for token in ("capture->sentinels_entry[1]", "capture->opt_ts_dis_mask",
                      "capture->status_opt_gpc"):
            self.assertNotIn(token, gate)
        abi_test = ABI_TEST.read_text(encoding="utf-8")
        self.assertIn("XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0U, 0U)", abi_test)
        self.assertIn("XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x20000000U, 8U)",
                      abi_test)
        snapshot = function_body(self.source, "xray_therm_capture_snapshot")
        fresh_tsense_match = re.search(
            r"capture->prewrite_tsense\s*=\s*xray_therm_read", snapshot
        )
        fresh_active_match = re.search(
            r"capture->prewrite_active_fbps\s*=\s*xray_therm_read", snapshot
        )
        self.assertIsNotNone(fresh_tsense_match)
        self.assertIsNotNone(fresh_active_match)
        fresh_tsense = fresh_tsense_match.start()
        fresh_active = fresh_active_match.start()
        fresh_gate = snapshot.index("XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS")
        first_write = snapshot.index("xray_therm_write_trigger(card, capture)")
        self.assertLess(fresh_tsense, fresh_active)
        self.assertLess(fresh_active, fresh_gate)
        self.assertLess(fresh_gate, first_write)

    def test_transaction_locking_attestation_and_nonwaking_pm(self):
        body = function_body(self.source, "xray_therm_ioctl_capture")
        ordered = [
            body.index("mutex_lock_interruptible(&card->lock)"),
            body.index("device_trylock(&card->pdev->dev)"),
            body.index("xray_therm_validate_live_device"),
            body.index("xray_therm_validate_device_state(card)", body.index("region_claimed")),
            body.index("xray_therm_capture_pure"),
            body.rindex("xray_therm_validate_device_state(card)"),
            body.index("release_region:"),
            body.index("xray_therm_put_runtime_ref"),
            body.index("device_unlock"),
            body.index("mutex_unlock"),
            body.index("copy_to_user"),
        ]
        self.assertEqual(ordered, sorted(ordered))
        live = function_body(self.source, "xray_therm_validate_live_device")
        self.assertIn("pm_runtime_get_if_active", live)
        self.assertNotIn("pm_runtime_get_sync", live)
        self.assertIn("RPM_ACTIVE", live)
        state = function_body(self.source, "xray_therm_validate_device_state")
        self.assertIn("pdev->current_state != PCI_D0", state)
        self.assertIn("PCI_COMMAND_MEMORY", state)
        self.assertIn("xray_therm_attest_identity", state)
        self.assertIn("xray_therm_validate_bar", state)

    def test_full_identity_and_bar_type_are_live_attested(self):
        identity = function_body(self.source, "xray_therm_attest_identity")
        for token in (
            "PCI_VENDOR_ID",
            "PCI_CLASS_REVISION",
            "PCI_SUBSYSTEM_VENDOR_ID",
            "pci_domain_nr",
            "expected->bus",
            "expected->devfn",
            "expected->revision",
            "xray_therm_generation",
        ):
            self.assertIn(token, identity)
        bar = function_body(self.source, "xray_therm_validate_bar")
        for token in (
            "pci_resource_start",
            "pci_resource_len",
            "pci_resource_flags",
            "IORESOURCE_MEM",
            "IORESOURCE_UNSET",
            "PCI_BASE_ADDRESS_MEM_TYPE_32",
            "PCI_BASE_ADDRESS_MEM_TYPE_64",
            "decoded == (u64)start",
        ):
            self.assertIn(token, bar)

    def test_abi_and_build_contracts_are_locked(self):
        self.assertIn('MODULE_VERSION("1.5.1")', self.source)
        self.assertRegex(
            self.uapi, r"(?m)^#define\s+XRAY_THERM_ABI_VERSION\s+5U$"
        )
        self.assertIn("sizeof(struct xray_therm_capture) != 1792", self.source)
        self.assertIn("XRAY_THERM_CAP_FRESH_PREWRITE_LIVENESS", self.uapi)
        self.assertIn(
            "XRAY_THERM_SENTINEL1 + sizeof(u32) >", self.source
        )
        self.assertIn("0x85a05401UL", ABI_TEST.read_text(encoding="utf-8"))
        self.assertIn("0xc7005402UL", ABI_TEST.read_text(encoding="utf-8"))
        self.assertIn("M=$(CURDIR) modules", MAKEFILE.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        PEER.is_dir(),
        "peer edition is intentionally absent from a standalone source release",
    )
    def test_editions_are_byte_identical_and_private_tokens_absent(self):
        self.assertTrue(PEER.is_dir())
        for relative in (
            "diagnostics/ga100_diag.cu",
            "diagnostics/nvml_temperature.py",
            "diagnostics/run_diagnostics.py",
            "diagnostics/temperature_probe.py",
            "diagnostics/xray_therm.py",
            "kernel/170hx_xray_therm.c",
            "kernel/170hx_xray_therm_uapi.h",
            "kernel/170hx_xray_therm_uapi_abi_test.c",
            "kernel/Makefile.therm",
            "tests/test_diagnostics_orchestrator.py",
            "tests/test_temperature_probe.py",
            "tests/test_thermal_helper.py",
        ):
            mine = ROOT / relative
            peer = PEER / relative
            self.assertEqual(mine.read_bytes(), peer.read_bytes(), relative)


if __name__ == "__main__":
    unittest.main()
