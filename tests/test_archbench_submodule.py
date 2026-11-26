#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lock the ArchBench submodule URL, gitlink, version, and license boundary."""

import configparser
import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATH = "third_party/archbench_cuda13_5_v0_2_0"
URL = "https://github.com/anoane/archbench.git"
COMMIT = "e0852fa34933c605fca780180d23c13af2927265"
TREE = "acc45376cb9c6c5faa85333da0aa6365d94dc7c1"


class ArchBenchSubmoduleTests(unittest.TestCase):
    def lock(self):
        return json.loads(
            (ROOT / "docs" / "ARCHBENCH_SUBMODULE_LOCK.json").read_text(
                encoding="utf-8"
            )
        )

    def test_metadata_and_gitmodules_are_exact(self):
        lock = self.lock()
        self.assertEqual(lock["schema"], "170hx-xray-submodule-lock-v1")
        self.assertEqual(lock["name"], "ArchBench")
        self.assertEqual(lock["version"], "0.2.0")
        self.assertEqual(lock["path"], PATH)
        self.assertEqual(lock["url"], URL)
        self.assertEqual(lock["commit"], COMMIT)
        self.assertEqual(lock["tree"], TREE)
        self.assertEqual(lock["license_expression"], "AGPL-3.0-or-later")
        self.assertEqual(lock["expected_tracked_files"], 104)
        self.assertEqual(lock["validation"], {
            "host_tests": "134/134 passed",
            "independent_alu_transitions": 532480,
            "cuda_disabled_ctest": "3/3 passed",
        })

        parser = configparser.ConfigParser()
        parser.read(ROOT / ".gitmodules", encoding="utf-8")
        section = 'submodule "%s"' % PATH
        self.assertEqual(parser.get(section, "path"), PATH)
        self.assertEqual(parser.get(section, "url"), URL)
        self.assertFalse(parser.has_option(section, "branch"))

    def test_parent_gitlink_matches_lock_when_git_metadata_is_available(self):
        if not (ROOT / ".git").exists():
            self.skipTest("parent Git metadata is unavailable")
        completed = subprocess.run(
            ["git", "ls-files", "-s", "--", PATH], cwd=str(ROOT),
            check=True, capture_output=True, text=True,
        )
        fields = completed.stdout.strip().split()
        self.assertGreaterEqual(len(fields), 4)
        self.assertEqual(fields[0], "160000")
        self.assertEqual(fields[1], COMMIT)

    def test_initialized_submodule_matches_commit_and_tree(self):
        submodule = ROOT / PATH
        if not (submodule / ".git").exists():
            if (submodule / "bench").exists():
                self.fail(
                    "ArchBench source exists without verifiable submodule metadata"
                )
            self.skipTest("ArchBench submodule is not initialized")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(submodule), check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=str(submodule),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head, COMMIT)
        self.assertEqual(tree, TREE)

        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(submodule), check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        self.assertEqual(len([value for value in tracked if value]), 104)
        self.assertTrue((submodule / "LICENSE").is_file())
        self.assertFalse((submodule / "LICENSES" / "MIT.txt").exists())
        metadata = (submodule / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('license = {text = "AGPL-3.0-or-later"}', metadata)
        dual_spdx = subprocess.run(
            ["git", "grep", "-n", "SPDX-License-Identifier:.*MIT"],
            cwd=str(submodule), check=False, capture_output=True, text=True,
        )
        self.assertEqual(dual_spdx.returncode, 1)
        self.assertEqual(dual_spdx.stdout, "")


if __name__ == "__main__":
    unittest.main()
