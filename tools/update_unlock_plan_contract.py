#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate or verify UNLOCK_PLAN.md's exact code-contract appendix."""

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "UNLOCK_PLAN.md"
TEST_MODULE = ROOT / "tests" / "test_unlock_plan_doc.py"
BEGIN = "<!-- XRAY-PLAN-CONTRACT-JSON:BEGIN -->"
END = "<!-- XRAY-PLAN-CONTRACT-JSON:END -->"


def _contract_module():
    spec = importlib.util.spec_from_file_location(
        "xray_unlock_plan_contract_generator", str(TEST_MODULE)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_text(current):
    module = _contract_module()
    payload = json.dumps(
        module.planning_contract(), sort_keys=True, indent=2, ensure_ascii=True
    )
    digest = module.contract_sha256()
    current = re.sub(
        r"<!-- XRAY-PLAN-CONTRACT-SHA256: [0-9a-f]{64} -->",
        "<!-- XRAY-PLAN-CONTRACT-SHA256: %s -->" % digest,
        current,
        count=1,
    )
    appendix = "%s\n\n```json\n%s\n```\n\n%s" % (BEGIN, payload, END)
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL
    )
    if not pattern.search(current):
        raise RuntimeError("UNLOCK_PLAN.md is missing contract appendix markers")
    return pattern.sub(appendix, current, count=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="refuse drift without editing"
    )
    args = parser.parse_args(argv)
    current = DOC.read_text(encoding="utf-8")
    expected = _expected_text(current)
    if current == expected:
        return 0
    if args.check:
        print("UNLOCK_PLAN.md contract appendix is stale", file=sys.stderr)
        return 1
    DOC.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
