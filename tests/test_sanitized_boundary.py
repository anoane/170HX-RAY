# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mechanical checks for the shareable tree's provenance boundary."""

import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "170HX-XRAY.py"


def _joined(*parts):
    """Join at runtime so the guarded tokens never enter Python constants."""
    return "".join(parts)


def load_xray():
    spec = importlib.util.spec_from_file_location("xray_sanitized_test_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SanitizedBoundaryTests(unittest.TestCase):
    def test_private_provenance_tokens_are_absent(self):
        # Keep the forbidden names split so this guard does not itself embed
        # any name that it is intended to reject, including in compiled
        # bytecode.  Adjacent literals and constant ``+`` expressions are not
        # sufficient here because CPython folds them before writing ``.pyc``.
        literal_tokens = (
            _joined("integ", "dev", "_", "gpu", "_drv"),
            _joined("integ", "dev", "-selected"),
            _joined("stage", "_rel"),
            _joined("gpu", "fuseaccessor"),
            _joined("ampere", "fuseaccessor"),
            _joined("fpf", "accessor"),
            _joined("rir", "accessor"),
            _joined("new", "fuse.cpp"),
            _joined("fuse", "factory"),
            _joined("iff", "encoder"),
            _joined("iff", "record"),
            _joined("maxwell", "ff"),
        )
        forbidden = (
            *(re.compile(re.escape(token), re.IGNORECASE)
              for token in literal_tokens),
            re.compile(r"dev_[a-z0-9_]+\.(?:h|cpp)(?::\d+)?", re.IGNORECASE),
        )
        forbidden_bytes = tuple(token.casefold().encode("ascii")
                                for token in literal_tokens)
        findings = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            folded = raw.lower()
            if any(token in folded for token in forbidden_bytes):
                findings.append("%s:binary-token" % path.relative_to(ROOT))
                # Text scanning would report the same literal a second time.
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if any(pattern.search(line) for pattern in forbidden):
                    findings.append(
                        "%s:%d" % (path.relative_to(ROOT), line_number)
                    )
        self.assertEqual(findings, [])

    def test_workspace_private_header_discovery_is_disabled(self):
        xray = load_xray()
        self.assertIsNone(xray.default_header_path())

    def test_bundled_tools_select_only_package_local_implementations(self):
        planner = load_module(
            ROOT / "tools" / "170hx_candidate_plan.py",
            "xray_sanitized_candidate_planner",
        )
        compiler = load_module(
            ROOT / "tools" / "170hx_vfi_append_manifest.py",
            "xray_sanitized_manifest_compiler",
        )
        self.assertEqual(planner.default_xray_path(ROOT).resolve(), SCRIPT.resolve())
        self.assertEqual(compiler.BACKEND_PATH.resolve(),
                         (ROOT / "otp_vfi_append.py").resolve())


if __name__ == "__main__":
    unittest.main()
