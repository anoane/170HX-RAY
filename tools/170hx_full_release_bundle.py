#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build or verify a hardware-inert GA100 read-and-plan release bundle.

The builder never discovers a GPU, opens BAR space, executes a candidate
planner, or invokes an OTP backend.  It accepts only already-created evidence:
one verified 170HX-XRAY full-dump bundle, one exact-current four-card candidate,
and the complete current four-card/seven-profile sealed-v2 manifest matrix.

The immutable 4.2 demonstration report used an older eight-profile matrix. Its
inventory is retained as an explicitly historical constant only; current
bundles neither relabel it nor admit its global-IOCTRL profile for execution.

Absent observation levels are represented as unavailable.  In particular, a
normal 20c2 bundle never fabricates a transient 2080/glitch-read specimen.
"""

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys


VERSION = "0.4.0"
SCHEMA = "170hx-xray-complete-release-bundle-v1"
CANDIDATE_SCHEMA = "170hx-xray-candidate-plan-v1"
READ_LEVELS = ("passive", "bar-read", "semantic-read", "glitch-read")
CANDIDATE_PROFILES = (
    "specimen-80gb-hbm",
    "specimen-more-sm",
    "xray-common-permanent-topology",
    "specimen-nvlink-global",
    "specimen-pcie-gen4-isolated",
    "specimen-pcie-gen4-a100-exact",
    "specimen-a100-20b5-identity",
    "specimen-sys-pipe-to-defect-experimental",
    "specimen-media-mig-to-defect-experimental",
    "public-corrected-cmpglitch-intent",
    "public-corrected-dstoro-intent",
    "xray-best-evidence-a100-conversion",
    "review-ss0-iff",
    "review-ss1-iff",
    "review-dp-conventional",
    "review-ecc-conventional",
    "review-nvenc-unsupported",
    "review-nvlink-per-link-unsupported",
)
CURRENT_MANIFEST_PROFILES = (
    "specimen-80gb-hbm",
    "specimen-more-sm",
    "xray-common-permanent-topology",
    "specimen-pcie-gen4-isolated",
    "specimen-a100-20b5-identity",
    "specimen-sys-pipe-to-defect-experimental",
    "specimen-media-mig-to-defect-experimental",
)
HISTORICAL_4_2_REPORT_MANIFEST_PROFILES = (
    "specimen-80gb-hbm",
    "specimen-more-sm",
    "xray-common-permanent-topology",
    "specimen-nvlink-global",
    "specimen-pcie-gen4-isolated",
    "specimen-a100-20b5-identity",
    "specimen-sys-pipe-to-defect-experimental",
    "specimen-media-mig-to-defect-experimental",
)
# Compatibility name used throughout this module means the current policy.
MANIFEST_PROFILES = CURRENT_MANIFEST_PROFILES
IMPLEMENTATION_DEPENDENCY_SCHEMA = (
    "170hx-xray-complete-release-implementation-dependencies-v1"
)
IMPLEMENTATION_DEPENDENCY_PATHS = {
    "frontend": "implementation/170HX-XRAY.py",
    "backend": "implementation/otp_vfi_append.py",
    "candidate-planner": "implementation/tools/170hx_candidate_plan.py",
    "manifest-compiler": "implementation/tools/170hx_vfi_append_manifest.py",
    "candidate-validator": "implementation/tools/170hx_otp_executor_spec.py",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


class BundleError(RuntimeError):
    pass


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def reject_json_constant(value):
    raise BundleError("non-finite JSON constant is forbidden: %s" % value)


def strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", reject_json_constant)
    return _JSON_LOADS(*args, **kwargs)


def strict_json_dumps(*args, **kwargs):
    kwargs.setdefault("allow_nan", False)
    return _JSON_DUMPS(*args, **kwargs)


def _repo_root():
    here = Path(__file__).resolve()
    embedded = here.parent
    if ((embedded / "170HX-XRAY.py").is_file() and
            (embedded / "otp_vfi_append.py").is_file() and
            (embedded / "tools/170hx_candidate_plan.py").is_file()):
        return embedded
    return here.parents[1]


def _project_dir(root):
    if (root / "170HX-XRAY.py").is_file():
        return root
    candidate = root / "170HX-XRAY"
    if candidate.is_dir():
        return candidate
    raise BundleError("170HX-XRAY project directory is absent")


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise BundleError("cannot load module: %s" % path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _modules(root):
    project = _project_dir(root)
    frontend = project / "170HX-XRAY.py"
    if not frontend.is_file():
        raise BundleError("project frontend is absent")
    backend = project / "otp_vfi_append.py"
    if not backend.is_file():
        raise BundleError("v2 manifest backend is absent")
    return (
        frontend,
        _load_module(frontend, "full_release_frontend"),
        _load_module(backend, "full_release_backend"),
    )


def _implementation_source_paths(root, frontend_path):
    """Return the exact source set needed for self-contained verification."""
    root = Path(root).resolve()
    frontend_path = Path(frontend_path).resolve()
    sources = {
        "frontend": frontend_path,
        "backend": frontend_path.parent / "otp_vfi_append.py",
        "candidate-planner": root / "tools" / "170hx_candidate_plan.py",
        "manifest-compiler": root / "tools" / "170hx_vfi_append_manifest.py",
        "candidate-validator": root / "tools" / "170hx_otp_executor_spec.py",
    }
    for role, path in sources.items():
        try:
            info = path.lstat()
        except OSError as exc:
            raise BundleError("implementation dependency is absent: %s" % role) from exc
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise BundleError("implementation dependency is not a regular file: %s" % role)
    return sources


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def canonical_sha256(value):
    return hashlib.sha256(strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")).hexdigest()


def packed_sha256(words):
    return hashlib.sha256(b"".join(
        struct.pack("<I", value) for value in words
    )).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def strict_json(path):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise BundleError("JSON input is not a regular file: %s" % path)
    try:
        return strict_json_loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BundleError("non-finite JSON token: %s" % value)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("invalid JSON %s: %s" % (path, exc)) from exc


def _literal_assignment(path, name):
    """Read one top-level string constant without executing the source."""
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name
                   for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise BundleError("%s has no literal %s assignment" % (path, name))


def _safe_bundle_path(bundle, relative, label, kind=None):
    """Resolve an index path inside the bundle without symlink traversal."""
    root = Path(bundle).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise BundleError("%s is not a safe relative path" % label)
    path = Path(relative)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BundleError("%s is not a safe relative path" % label)
    current = root
    for part in path.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise BundleError("%s is absent: %s" % (label, current)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise BundleError("%s traverses a symlink" % label)
    try:
        current.resolve().relative_to(root)
    except ValueError as exc:
        raise BundleError("%s escapes the bundle" % label) from exc
    if kind == "file" and not current.is_file():
        raise BundleError("%s is not a regular file" % label)
    if kind == "directory" and not current.is_dir():
        raise BundleError("%s is not a directory" % label)
    return current


def _u32(value, label):
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise BundleError("%s is not an integer" % label) from exc
    if isinstance(value, bool) or not 0 <= parsed <= 0xFFFFFFFF:
        raise BundleError("%s is outside u32" % label)
    return parsed


def _new_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700)
    return path


def _ensure_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_bytes(path, payload):
    path = Path(path)
    _ensure_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _write_json(path, value):
    _write_bytes(path, (
        strict_json_dumps(value, sort_keys=True, indent=2, ensure_ascii=True,
                   allow_nan=False) + "\n"
    ).encode("ascii"))


def _copy_file(source, destination):
    source = Path(source)
    info = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise BundleError("refusing non-regular source object: %s" % source)
    with source.open("rb") as stream:
        _write_bytes(destination, stream.read())


def _copy_tree(source, destination):
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise BundleError("source bundle is not a real directory")
    _ensure_dir(destination)
    for item in sorted(source.rglob("*"), key=lambda path: path.as_posix()):
        relative = item.relative_to(source)
        target = destination / relative
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BundleError("source bundle contains a symlink: %s" % relative)
        if stat.S_ISDIR(info.st_mode):
            _ensure_dir(target)
        elif stat.S_ISREG(info.st_mode):
            _copy_file(item, target)
        else:
            raise BundleError("source bundle contains a special object: %s" % relative)


def _normalize_full_dump(path):
    path = Path(path).resolve()
    if (path / "index.json").is_file():
        return path
    if (path / "bundle" / "index.json").is_file():
        return path / "bundle"
    raise BundleError("full-dump input has no index.json")


def validate_full_dump(path, frontend):
    source = _normalize_full_dump(path)
    report = frontend.verify_full_dump_bundle(source)
    if report.get("valid") is not True:
        raise BundleError("source full-dump failed verification: %s" %
                          "; ".join(report.get("errors", [])))
    return source, report, strict_json(source / "index.json")


def validate_candidate(path, root, frontend_path):
    path = Path(path).resolve()
    document = strict_json(path)
    if document.get("schema") != CANDIDATE_SCHEMA:
        raise BundleError("candidate schema mismatch")
    if document.get("executable") is not False or document.get(
            "execution_authorized") is not False:
        raise BundleError("candidate is not explicitly non-executable")
    claimed = document.get("report_content_sha256_excluding_self")
    unsigned = copy.deepcopy(document)
    unsigned.pop("report_content_sha256_excluding_self", None)
    if claimed != canonical_sha256(unsigned):
        raise BundleError("candidate self-digest mismatch")
    planner_path = root / "tools" / "170hx_candidate_plan.py"
    tool = document.get("tool", {})
    if (tool.get("name") != "170hx_candidate_plan" or
            tool.get("version") != _literal_assignment(
                planner_path, "SCRIPT_VERSION") or
            tool.get("sha256") != sha256_file(planner_path)):
        raise BundleError("candidate binds a stale planner source")
    decoder = document.get("sources", {}).get("current_decoder", {})
    if (decoder.get("version") != _literal_assignment(
            frontend_path, "TOOL_VERSION") or
            decoder.get("sha256") != sha256_file(frontend_path)):
        raise BundleError("candidate binds a stale decoder source")
    if document.get("selected_profiles") != list(CANDIDATE_PROFILES):
        raise BundleError("candidate profile inventory/order mismatch")
    cards = document.get("cards")
    if not isinstance(cards, list) or len(cards) != 4:
        raise BundleError("candidate must contain exactly four cards")
    seen = set()
    profiles_by_key = {}
    for card_index, card in enumerate(cards):
        bdf = card.get("bdf")
        if not isinstance(bdf, str) or not BDF_RE.fullmatch(bdf) or bdf in seen:
            raise BundleError("candidate card BDF set is invalid")
        seen.add(bdf)
        baseline = [_u32(value, "baseline row") for value in
                    card.get("baseline", {}).get("main_rows", [])]
        if len(baseline) != 512:
            raise BundleError("%s baseline is not 512 rows" % bdf)
        if packed_sha256(baseline) != card.get("baseline", {}).get(
                "complete_main_u32le_sha256"):
            raise BundleError("%s baseline digest mismatch" % bdf)
        profiles = card.get("profiles")
        if not isinstance(profiles, list) or [
                value.get("model_id") for value in profiles
        ] != list(CANDIDATE_PROFILES):
            raise BundleError("%s candidate profile inventory mismatch" % bdf)
        for profile in profiles:
            profile_id = profile["model_id"]
            if profile.get("executable") is not False or profile.get(
                    "execution_authorized") is not False:
                raise BundleError("%s/%s is not inert" % (bdf, profile_id))
            replay = list(baseline)
            for ordinal, operation in enumerate(profile.get("operations", [])):
                if operation.get("index") != ordinal:
                    raise BundleError("%s/%s operation order mismatch" %
                                      (bdf, profile_id))
                row = operation.get("row")
                if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < 512:
                    raise BundleError("%s/%s operation row invalid" %
                                      (bdf, profile_id))
                expected_old = _u32(operation.get("expected_physical_old_word"),
                                    "expected old word")
                if replay[row] != expected_old:
                    raise BundleError("%s/%s replay old-word mismatch" %
                                      (bdf, profile_id))
                replay[row] = _u32(operation.get("proposed_physical_word"),
                                   "proposed word")
            proposed = [_u32(value, "proposed row") for value in
                        profile.get("proposed_complete_image_rows", [])]
            if replay != proposed or len(proposed) != 512:
                raise BundleError("%s/%s proposed image replay mismatch" %
                                  (bdf, profile_id))
            if packed_sha256(proposed) != profile.get(
                    "proposed_complete_image_u32le_sha256"):
                raise BundleError("%s/%s proposed image digest mismatch" %
                                  (bdf, profile_id))
            profiles_by_key[(bdf, profile_id)] = (profile, proposed)
    return path, document, profiles_by_key


def _capture_relocations(source_bundle, candidate):
    """Bind candidate capture names to exact files already in read-bundle."""
    source_bundle = Path(source_bundle).resolve()
    mappings = {}
    for card in candidate["cards"]:
        capture = card.get("physical_capture")
        if not isinstance(capture, dict):
            raise BundleError("candidate physical-capture binding is absent")
        named = capture.get("path")
        digest = capture.get("sha256")
        if (not isinstance(named, str) or not named or "\\" in named or
                not isinstance(digest, str) or not HEX64.fullmatch(digest)):
            raise BundleError("candidate physical-capture binding is malformed")
        named_path = Path(named)
        candidates = []
        if named_path.is_absolute():
            try:
                candidates.append(named_path.relative_to(source_bundle))
            except ValueError:
                pass
        else:
            candidates.append(named_path)
            bundle_positions = [
                index for index, part in enumerate(named_path.parts)
                if part == "bundle"
            ]
            candidates.extend(
                Path(*named_path.parts[index + 1:])
                for index in bundle_positions
                if index + 1 < len(named_path.parts)
            )
        resolved = []
        seen = set()
        for relative in candidates:
            if (relative.is_absolute() or not relative.parts or
                    any(part in ("", ".", "..") for part in relative.parts)):
                continue
            key = relative.as_posix()
            if key in seen:
                continue
            seen.add(key)
            path = source_bundle / relative
            try:
                info = path.lstat()
            except OSError:
                continue
            if (not path.is_symlink() and stat.S_ISREG(info.st_mode) and
                    sha256_file(path) == digest):
                resolved.append(relative)
        if len(resolved) != 1:
            raise BundleError(
                "candidate capture does not resolve uniquely inside source full-dump: %s" %
                named
            )
        entry = {
            "source_path": named,
            "bundled_path": "read-bundle/%s" % resolved[0].as_posix(),
            "sha256": digest,
        }
        previous = mappings.get(named)
        if previous is not None and previous != entry:
            raise BundleError("candidate capture path has conflicting bindings")
        mappings[named] = entry
    return [mappings[key] for key in sorted(mappings)]


def _manifest_paths(path):
    source = Path(path)
    if source.is_symlink():
        raise BundleError("manifest-set root must not be a symlink")
    path = source.resolve()
    directory = path / "manifests" if (path / "manifests").is_dir() else path
    if directory.is_symlink() or not directory.is_dir():
        raise BundleError("manifest-set directory is invalid")
    paths = sorted(directory.glob("*.json"), key=lambda item: item.name)
    expected_count = 4 * len(MANIFEST_PROFILES)
    if len(paths) != expected_count:
        raise BundleError(
            "manifest matrix must contain exactly %d JSON files" % expected_count
        )
    return paths


def validate_manifest_checksums(path, paths):
    """Require one canonical, complete checksum binding for the matrix."""
    root = Path(path).resolve()
    checksum_path = root / "SHA256SUMS"
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise BundleError("manifest-set SHA256SUMS is absent or invalid")
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BundleError("cannot read manifest-set SHA256SUMS: %s" % exc) from exc
    expected = {
        "manifests/%s" % item.name: sha256_file(item)
        for item in paths
    }
    parsed = {}
    line_re = re.compile(r"^([0-9a-f]{64})  (manifests/[A-Za-z0-9_.-]+\.json)$")
    for ordinal, line in enumerate(lines, 1):
        match = line_re.fullmatch(line)
        if match is None:
            raise BundleError(
                "manifest-set SHA256SUMS line %d is not canonical" % ordinal
            )
        digest, relative = match.groups()
        if relative in parsed:
            raise BundleError("duplicate manifest checksum path: %s" % relative)
        parsed[relative] = digest
    if parsed != expected:
        raise BundleError("manifest-set SHA256SUMS does not bind the exact matrix")
    return checksum_path, sha256_file(checksum_path)


def validate_manifests(path, backend, candidate_path, candidate,
                       profiles_by_key):
    candidate_sha = sha256_file(candidate_path)
    expected = {
        (card["bdf"], profile) for card in candidate["cards"]
        for profile in MANIFEST_PROFILES
    }
    validated = {}
    source_paths = {}
    manifest_paths = _manifest_paths(path)
    checksum_path, checksum_sha256 = validate_manifest_checksums(
        path, manifest_paths
    )
    for manifest_path in manifest_paths:
        raw = strict_json(manifest_path)
        binding = raw.get("binding", {})
        key = (binding.get("bdf"), binding.get("profile_id"))
        if key not in expected or key in validated:
            raise BundleError("duplicate/unexpected manifest binding: %r" % (key,))
        try:
            result = backend.load_and_validate_manifest(
                manifest_path, key[0], key[1]
            )
        except Exception as exc:
            raise BundleError("manifest validation failed for %s: %s" %
                              (manifest_path, exc)) from exc
        source = raw.get("candidate_source", {})
        if source.get("file_sha256") != candidate_sha or source.get(
                "report_content_sha256_excluding_self") != candidate.get(
                    "report_content_sha256_excluding_self"):
            raise BundleError("manifest candidate binding mismatch: %s" % manifest_path)
        profile, proposed = profiles_by_key[key]
        if raw.get("binding", {}).get(
                "proposed_main_u32le_sha256") != packed_sha256(proposed):
            raise BundleError("manifest proposed-image binding mismatch")
        validated[key] = result
        source_paths[key] = manifest_path
    if set(validated) != expected:
        raise BundleError("manifest matrix is incomplete")
    return validated, source_paths, checksum_path, checksum_sha256


def read_coverage(index):
    observations = index.get("observations", [])
    live = {}
    for item in observations if isinstance(observations, list) else []:
        if isinstance(item, dict) and item.get("origin") == "live":
            live.setdefault(item.get("requested_access"), []).append(item)
    result = []
    for level in READ_LEVELS:
        matches = live.get(level, [])
        available = bool(matches)
        if available:
            reason = "verified source bundle contains a live %s observation" % level
        elif level == "glitch-read":
            reason = (
                "no independently qualified live 10de:2080/DSOV=0 glitch-state "
                "observation was supplied; no state was fabricated"
            )
        else:
            reason = "verified source bundle contains no live %s observation" % level
        result.append({
            "level": level,
            "available": available,
            "observation_ids": [item.get("id") for item in matches],
            "reason": reason,
            "fabricated": False,
        })
    return {
        "schema": "170hx-xray-read-level-coverage-v1",
        "levels": result,
        "complete": all(item["available"] for item in result),
        "ordinary_levels_complete": all(
            item["available"] for item in result if item["level"] != "glitch-read"
        ),
        "unavailable_levels": [item["level"] for item in result
                               if not item["available"]],
        "fabricated_observations": 0,
    }


def _slug_bdf(bdf):
    return bdf.replace(":", "-").replace(".", "-")


def _emit_plan(root, bdf, profile, proposed):
    profile_id = profile["model_id"]
    base = root / "planning" / "profiles" / _slug_bdf(bdf) / profile_id
    _ensure_dir(base)
    plan_document = {
        "schema": "170hx-xray-candidate-profile-export-v1",
        "bdf": bdf,
        "profile_id": profile_id,
        "executable": False,
        "execution_authorized": False,
        "profile": profile,
    }
    _write_json(base / "plan.json", plan_document)
    _write_bytes(base / "plan.jsonl", (
        strict_json_dumps(plan_document, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("ascii"))
    rows = ["0x%08x" % value for value in proposed]
    _write_json(base / "proposed-main.json", {
        "schema": "170hx-xray-proposed-main-v1",
        "bdf": bdf,
        "profile_id": profile_id,
        "rows": rows,
        "u32le_sha256": packed_sha256(proposed),
    })
    _write_bytes(base / "proposed-main.jsonl", "".join(
        strict_json_dumps({"row": index, "value": value}, sort_keys=True,
                   separators=(",", ":")) + "\n"
        for index, value in enumerate(rows)
    ).encode("ascii"))
    _write_bytes(base / "proposed-main.indexed.txt", "".join(
        "[%03d] %s\n" % (index, value)
        for index, value in enumerate(rows)
    ).encode("ascii"))
    _write_bytes(base / "proposed-main.raw-u32le.bin", b"".join(
        struct.pack("<I", value) for value in proposed
    ))
    return {
        "bdf": bdf,
        "profile_id": profile_id,
        "directory": base.relative_to(root).as_posix(),
        "proposed_main_u32le_sha256": packed_sha256(proposed),
        "formats": ["json", "jsonl", "indexed-text", "raw-u32le"],
        "execution_authorized": False,
    }


def _manifest_text(root):
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BundleError("output contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o700)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, 0o600)
            relative = path.relative_to(root).as_posix()
            if relative != "SHA256SUMS":
                entries.append((relative, sha256_file(path)))
        else:
            raise BundleError("output contains a special object")
    return "".join("%s  %s\n" % (digest, path)
                   for path, digest in entries)


def build(full_dump, candidate, manifest_set, output, root=None):
    root = Path(root or _repo_root()).resolve()
    frontend_path, frontend, backend = _modules(root)
    source_bundle, source_report, source_index = validate_full_dump(
        full_dump, frontend
    )
    candidate_path, candidate_document, profile_map = validate_candidate(
        candidate, root, frontend_path
    )
    (manifest_results, manifest_sources, manifest_checksum_path,
     manifest_checksum_sha256) = validate_manifests(
        manifest_set, backend, candidate_path, candidate_document, profile_map
    )
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise BundleError("output already exists: %s" % output)
    _ensure_dir(output.parent)
    _new_dir(output)
    _copy_tree(source_bundle, output / "read-bundle")
    coverage = read_coverage(source_index)
    _write_json(output / "coverage" / "read-levels.json", coverage)
    _write_json(output / "validation" / "source-full-dump.json", source_report)
    _copy_file(candidate_path, output / "planning" / "candidate.json")
    plan_entries = []
    for card in candidate_document["cards"]:
        for profile in card["profiles"]:
            selected, proposed = profile_map[(card["bdf"], profile["model_id"])]
            plan_entries.append(_emit_plan(output, card["bdf"], selected, proposed))
    manifest_entries = []
    for key in sorted(manifest_results):
        source = manifest_sources[key]
        target = output / "planning" / "sealed-v2-manifests" / source.name
        _copy_file(source, target)
        manifest_entries.append({
            "bdf": key[0], "profile_id": key[1],
            "path": target.relative_to(output).as_posix(),
            "validated": True, "execution_authorized": False,
        })
    implementation_copy = output / "implementation" / Path(__file__).name
    _copy_file(Path(__file__).resolve(), implementation_copy)
    dependency_entries = []
    for role, source in sorted(
            _implementation_source_paths(root, frontend_path).items()):
        relative = IMPLEMENTATION_DEPENDENCY_PATHS[role]
        target = output / relative
        _copy_file(source, target)
        dependency_entries.append({
            "role": role,
            "path": relative,
            "sha256": sha256_file(source),
        })
    capture_relocations = _capture_relocations(
        source_bundle, candidate_document
    )
    index = {
        "schema": SCHEMA,
        "tool": {"name": Path(__file__).name, "version": VERSION,
                 "sha256": sha256_file(Path(__file__).resolve()),
                 "copied_path": implementation_copy.relative_to(output).as_posix()},
        "implementation_dependencies": {
            "schema": IMPLEMENTATION_DEPENDENCY_SCHEMA,
            "root": "implementation",
            "self_contained_verify": True,
            "files": dependency_entries,
            "capture_relocations": capture_relocations,
        },
        "source_full_dump": {
            "path": str(source_bundle),
            "index_sha256": sha256_file(source_bundle / "index.json"),
            "manifest_sha256": sha256_file(source_bundle / "SHA256SUMS"),
            "verified": True,
            "copied_directory": "read-bundle",
        },
        "read_level_coverage": coverage,
        "candidate": {
            "source_path": str(candidate_path),
            "source_sha256": sha256_file(candidate_path),
            "content_sha256": candidate_document[
                "report_content_sha256_excluding_self"],
            "copied_path": "planning/candidate.json",
            "cards": 4, "profiles_per_card": len(CANDIDATE_PROFILES),
            "execution_authorized": False,
        },
        "plan_exports": plan_entries,
        "sealed_v2_manifests": manifest_entries,
        "sealed_v2_manifest_set": {
            "source_checksum_path": str(manifest_checksum_path),
            "source_checksum_sha256": manifest_checksum_sha256,
            "verified": True,
        },
        "counts": {
            "cards": 4,
            "candidate_profiles": len(plan_entries),
            "sealed_v2_manifests": len(manifest_entries),
            "read_levels_available": sum(
                1 for item in coverage["levels"] if item["available"]),
            "read_levels_unavailable": len(coverage["unavailable_levels"]),
        },
        "safety": {
            "hardware_access_performed": False,
            "candidate_source_executed": False,
            "otp_backend_invoked": False,
            "programming_writes_performed": 0,
            "fabricated_observations": 0,
            "manifest_presence_is_not_execution_authority": True,
        },
        "relocation_boundary": (
            "verification is self-contained and replays the sealed documents "
            "against checksum-bound embedded implementations and relocated exact "
            "evidence; original absolute paths remain provenance strings and the "
            "copied artifacts do not grant execution authority"
        ),
    }
    _write_json(output / "index.json", index)
    _write_bytes(output / "SHA256SUMS", _manifest_text(output).encode("ascii"))
    result = verify(output, root=root)
    if result["valid"] is not True:
        raise BundleError("new bundle failed self-verification: %s" %
                          "; ".join(result["errors"]))
    return result


def _parse_manifest(path):
    entries = {}
    for ordinal, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00\r\n]+)", line)
        if match is None:
            raise BundleError("malformed SHA256SUMS line %d" % ordinal)
        digest, relative = match.groups()
        parts = Path(relative).parts
        if (not parts or relative.startswith(("/", "\\")) or "\\" in relative
                or "." in parts or ".." in parts or relative in entries):
            raise BundleError("unsafe/duplicate SHA256SUMS path")
        entries[relative] = digest
    if list(entries) != sorted(entries):
        raise BundleError("SHA256SUMS paths are not sorted")
    return entries


def _validate_implementation_dependencies(bundle, index):
    """Validate all executable support bytes before importing any of them."""
    binding = index.get("implementation_dependencies")
    if (not isinstance(binding, dict) or set(binding) != {
            "schema", "root", "self_contained_verify", "files",
            "capture_relocations",
    } or binding.get("schema") != IMPLEMENTATION_DEPENDENCY_SCHEMA or
            binding.get("root") != "implementation" or
            binding.get("self_contained_verify") is not True):
        raise BundleError("implementation dependency binding is malformed")
    dependency_root = _safe_bundle_path(
        bundle, binding["root"], "implementation dependency root", "directory"
    )

    candidate_binding = index.get("candidate")
    if not isinstance(candidate_binding, dict):
        raise BundleError("candidate index binding is absent")
    candidate_path = _safe_bundle_path(
        bundle, candidate_binding.get("copied_path"),
        "candidate copied_path", "file",
    )
    candidate = strict_json(candidate_path)
    candidate_tool = candidate.get("tool")
    decoder = candidate.get("sources", {}).get("current_decoder")
    if not isinstance(candidate_tool, dict) or not isinstance(decoder, dict):
        raise BundleError("candidate implementation bindings are absent")
    expected_hashes = {
        "frontend": decoder.get("sha256"),
        "candidate-planner": candidate_tool.get("sha256"),
    }

    manifest_items = index.get("sealed_v2_manifests")
    expected_manifest_count = 4 * len(MANIFEST_PROFILES)
    if (not isinstance(manifest_items, list) or
            len(manifest_items) != expected_manifest_count):
        raise BundleError(
            "sealed manifest inventory is not 4x%d" % len(MANIFEST_PROFILES)
        )
    manifest_hash_sets = {
        "backend": set(),
        "manifest-compiler": set(),
        "candidate-validator": set(),
    }
    manifest_keys = {
        "backend": "backend_file_sha256",
        "manifest-compiler": "compiler_file_sha256",
        "candidate-validator": "old_candidate_validator_file_sha256",
    }
    for ordinal, item in enumerate(manifest_items):
        if not isinstance(item, dict):
            raise BundleError("sealed manifest %d is not an object" % ordinal)
        path = _safe_bundle_path(
            bundle, item.get("path"),
            "sealed manifest %d dependency binding" % ordinal, "file",
        )
        implementation = strict_json(path).get("implementation_binding")
        if not isinstance(implementation, dict):
            raise BundleError("sealed manifest implementation binding is absent")
        for role, key in manifest_keys.items():
            manifest_hash_sets[role].add(implementation.get(key))
    for role, values in manifest_hash_sets.items():
        if len(values) != 1:
            raise BundleError("sealed manifests disagree on %s bytes" % role)
        expected_hashes[role] = next(iter(values))
    if any(not isinstance(value, str) or not HEX64.fullmatch(value)
           for value in expected_hashes.values()):
        raise BundleError("embedded dependency digest binding is malformed")

    files = binding.get("files")
    if not isinstance(files, list) or len(files) != len(
            IMPLEMENTATION_DEPENDENCY_PATHS):
        raise BundleError("implementation dependency inventory is incomplete")
    dependency_paths = {}
    for ordinal, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"role", "path", "sha256"}:
            raise BundleError("implementation dependency %d is malformed" % ordinal)
        role = item.get("role")
        if role not in IMPLEMENTATION_DEPENDENCY_PATHS or role in dependency_paths:
            raise BundleError("duplicate/unexpected implementation dependency role")
        if item.get("path") != IMPLEMENTATION_DEPENDENCY_PATHS[role]:
            raise BundleError("implementation dependency path mismatch: %s" % role)
        path = _safe_bundle_path(
            bundle, item["path"], "implementation dependency %s" % role, "file"
        )
        actual = sha256_file(path)
        if item.get("sha256") != actual or actual != expected_hashes[role]:
            raise BundleError("implementation dependency binding mismatch: %s" % role)
        dependency_paths[role] = path
    if set(dependency_paths) != set(IMPLEMENTATION_DEPENDENCY_PATHS):
        raise BundleError("implementation dependency role set is incomplete")

    relocations = binding.get("capture_relocations")
    if not isinstance(relocations, list) or not relocations:
        raise BundleError("capture relocation inventory is absent")
    capture_map = {}
    for ordinal, item in enumerate(relocations):
        if not isinstance(item, dict) or set(item) != {
                "source_path", "bundled_path", "sha256",
        }:
            raise BundleError("capture relocation %d is malformed" % ordinal)
        source_path = item.get("source_path")
        if (not isinstance(source_path, str) or not source_path or
                source_path in capture_map):
            raise BundleError("capture relocation source set is invalid")
        bundled_path = item.get("bundled_path")
        if not isinstance(bundled_path, str) or not bundled_path.startswith(
                "read-bundle/"):
            raise BundleError("capture relocation target is outside read-bundle")
        path = _safe_bundle_path(
            bundle, bundled_path, "capture relocation %d" % ordinal, "file"
        )
        if (not isinstance(item.get("sha256"), str) or
                not HEX64.fullmatch(item["sha256"]) or
                sha256_file(path) != item["sha256"]):
            raise BundleError("capture relocation digest mismatch")
        capture_map[source_path] = (path, item["sha256"])

    candidate_capture_map = {}
    cards = candidate.get("cards")
    if not isinstance(cards, list):
        raise BundleError("candidate card inventory is absent")
    for card in cards:
        capture = card.get("physical_capture") if isinstance(card, dict) else None
        if not isinstance(capture, dict):
            raise BundleError("candidate physical-capture binding is absent")
        source_path = capture.get("path")
        digest = capture.get("sha256")
        previous = candidate_capture_map.get(source_path)
        if previous is not None and previous != digest:
            raise BundleError("candidate physical-capture binding conflicts")
        candidate_capture_map[source_path] = digest
    if set(candidate_capture_map) != set(capture_map) or any(
            candidate_capture_map[name] != capture_map[name][1]
            for name in candidate_capture_map):
        raise BundleError("capture relocation set does not exactly bind candidate")
    return dependency_root, dependency_paths, capture_map


def _relocated_source_file(bundle, source_binding, original, digest, label):
    """Map one sealed absolute source-bundle path into copied read-bundle."""
    source_root = Path(str(source_binding.get("path", "")))
    original_path = Path(str(original))
    if (not source_root.is_absolute() or not original_path.is_absolute() or
            any(part in ("", ".", "..") for part in original_path.parts)):
        raise BundleError("%s is not a canonical absolute source path" % label)
    try:
        relative = original_path.relative_to(source_root)
    except ValueError as exc:
        raise BundleError("%s is outside the sealed source full-dump" % label) from exc
    copied = source_binding.get("copied_directory")
    if not isinstance(copied, str):
        raise BundleError("source full-dump copy binding is malformed")
    path = _safe_bundle_path(
        bundle, "%s/%s" % (copied, relative.as_posix()), label, "file"
    )
    if sha256_file(path) != digest:
        raise BundleError("%s digest mismatch" % label)
    return path


def _reconstruct_relocated_manifest(raw_manifest, compiler, candidate_path,
                                    bundle, source_binding, capture_map):
    """Replay a sealed manifest using only checksum-bound bundled evidence."""
    implementation = raw_manifest.get("implementation_binding", {})
    compiler_path = Path(compiler.__file__).resolve()
    if sha256_file(compiler_path) != implementation.get("compiler_file_sha256"):
        raise BundleError("embedded compiler differs from manifest binding")
    source = raw_manifest.get("candidate_source", {})
    if sha256_file(candidate_path) != source.get("file_sha256"):
        raise BundleError("relocated candidate differs from manifest binding")
    cold = raw_manifest.get("cold_baseline_attestation", {})
    main_path = _relocated_source_file(
        bundle, source_binding, cold.get("main_path"),
        cold.get("main_file_sha256"), "relocated cold MAIN",
    )
    rir_path = _relocated_source_file(
        bundle, source_binding, cold.get("rir_path"),
        cold.get("rir_file_sha256"), "relocated cold RIR",
    )

    original_resolver = compiler._resolve_bound_file

    def resolve_capture(_candidate_path, named_path):
        entry = capture_map.get(str(named_path))
        if entry is None:
            raise BundleError("compiler requested an unbound capture relocation")
        path, digest = entry
        if sha256_file(path) != digest:
            raise BundleError("relocated capture changed during reconstruction")
        return path

    compiler._resolve_bound_file = resolve_capture
    try:
        binding = raw_manifest.get("binding", {})
        rebuilt = compiler.build_manifest(
            candidate_path, binding.get("bdf"), binding.get("profile_id"),
            main_path, rir_path,
            generated_utc=raw_manifest.get("generated_utc"),
        )
    finally:
        compiler._resolve_bound_file = original_resolver

    # Paths are provenance strings in the sealed document.  The replay inputs
    # above were independently mapped and hash-checked inside the copied bundle;
    # restore only those path strings before the byte-for-byte semantic compare.
    rebuilt["candidate_source"]["path"] = source.get("path")
    rebuilt["cold_baseline_attestation"]["main_path"] = cold.get("main_path")
    rebuilt["cold_baseline_attestation"]["rir_path"] = cold.get("rir_path")
    rebuilt["exact_current_provenance"]["capture_path"] = raw_manifest.get(
        "exact_current_provenance", {}).get("capture_path")
    rebuilt.pop("content_sha256_excluding_self", None)
    rebuilt["content_sha256_excluding_self"] = canonical_sha256(rebuilt)
    if strict_json_dumps(
            rebuilt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ) != strict_json_dumps(
            raw_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ):
        raise BundleError(
            "manifest differs from relocated deterministic compiler reconstruction"
        )


def _verify_semantic_bundle(bundle, root):
    """Replay every copied semantic binding after file checksums pass."""
    bundle = Path(bundle).resolve()
    index = strict_json(bundle / "index.json")
    if index.get("schema") != SCHEMA:
        raise BundleError("index schema mismatch")

    tool = index.get("tool")
    if not isinstance(tool, dict):
        raise BundleError("packager tool binding is absent")
    implementation = _safe_bundle_path(
        bundle, tool.get("copied_path"), "packager copied_path", "file"
    )
    current_tool_sha = sha256_file(Path(__file__).resolve())
    if (tool.get("name") != Path(__file__).name or
            tool.get("version") != VERSION or
            tool.get("sha256") != current_tool_sha or
            sha256_file(implementation) != current_tool_sha):
        raise BundleError("packager implementation binding mismatch")

    (dependency_root, dependency_paths,
     capture_map) = _validate_implementation_dependencies(bundle, index)
    frontend_path, frontend, backend = _modules(dependency_root)
    if (frontend_path != dependency_paths["frontend"] or
            Path(backend.__file__).resolve() != dependency_paths["backend"]):
        raise BundleError("embedded implementation resolution mismatch")
    compiler = _load_module(
        dependency_paths["manifest-compiler"],
        "full_release_relocated_manifest_compiler",
    )

    source_binding = index.get("source_full_dump")
    if not isinstance(source_binding, dict):
        raise BundleError("source full-dump binding is absent")
    read_bundle = _safe_bundle_path(
        bundle, source_binding.get("copied_directory"),
        "source full-dump copied_directory", "directory",
    )
    if (sha256_file(read_bundle / "index.json") !=
            source_binding.get("index_sha256") or
            sha256_file(read_bundle / "SHA256SUMS") !=
            source_binding.get("manifest_sha256") or
            source_binding.get("verified") is not True):
        raise BundleError("source full-dump binding mismatch")
    nested = frontend.verify_full_dump_bundle(read_bundle)
    if nested.get("valid") is not True:
        raise BundleError("nested read bundle failed verification")
    nested_index = strict_json(read_bundle / "index.json")

    coverage_path = _safe_bundle_path(
        bundle, "coverage/read-levels.json", "read-level coverage", "file"
    )
    coverage = strict_json(coverage_path)
    if coverage != index.get("read_level_coverage"):
        raise BundleError("read-level coverage file/index mismatch")
    if coverage != read_coverage(nested_index):
        raise BundleError("read-level coverage does not derive from source bundle")
    if ([item.get("level") for item in coverage.get("levels", [])] !=
            list(READ_LEVELS) or coverage.get("fabricated_observations") != 0):
        raise BundleError("read-level coverage inventory mismatch")

    candidate_binding = index.get("candidate")
    if not isinstance(candidate_binding, dict):
        raise BundleError("candidate index binding is absent")
    candidate_path = _safe_bundle_path(
        bundle, candidate_binding.get("copied_path"),
        "candidate copied_path", "file",
    )
    candidate_path, candidate, profile_map = validate_candidate(
        candidate_path, dependency_root, frontend_path
    )
    candidate_sha = sha256_file(candidate_path)
    if (candidate_binding.get("source_sha256") != candidate_sha or
            candidate_binding.get("content_sha256") !=
            candidate.get("report_content_sha256_excluding_self") or
            candidate_binding.get("cards") != 4 or
            candidate_binding.get("profiles_per_card") !=
            len(CANDIDATE_PROFILES) or
            candidate_binding.get("execution_authorized") is not False):
        raise BundleError("copied candidate index binding mismatch")

    expected_plan_keys = set(profile_map)
    plan_entries = index.get("plan_exports")
    expected_plan_count = 4 * len(CANDIDATE_PROFILES)
    if not isinstance(plan_entries, list) or len(plan_entries) != expected_plan_count:
        raise BundleError(
            "plan export inventory is not 4x%d" % len(CANDIDATE_PROFILES)
        )
    observed_plan_keys = set()
    for ordinal, item in enumerate(plan_entries):
        if not isinstance(item, dict):
            raise BundleError("plan export %d is not an object" % ordinal)
        key = (item.get("bdf"), item.get("profile_id"))
        if key not in expected_plan_keys or key in observed_plan_keys:
            raise BundleError("duplicate/unexpected plan export binding: %r" % (key,))
        observed_plan_keys.add(key)
        profile, proposed = profile_map[key]
        expected_directory = (
            "planning/profiles/%s/%s" % (_slug_bdf(key[0]), key[1])
        )
        base = _safe_bundle_path(
            bundle, item.get("directory"),
            "plan export %s/%s directory" % key, "directory",
        )
        if item.get("directory") != expected_directory:
            raise BundleError("plan export directory does not match its binding")
        if (item.get("formats") !=
                ["json", "jsonl", "indexed-text", "raw-u32le"] or
                item.get("execution_authorized") is not False or
                item.get("proposed_main_u32le_sha256") != packed_sha256(proposed)):
            raise BundleError("plan export metadata mismatch for %s/%s" % key)

        plan_document = strict_json(base / "plan.json")
        expected_plan = {
            "schema": "170hx-xray-candidate-profile-export-v1",
            "bdf": key[0],
            "profile_id": key[1],
            "executable": False,
            "execution_authorized": False,
            "profile": profile,
        }
        if plan_document != expected_plan:
            raise BundleError("plan.json differs from candidate profile for %s/%s" % key)
        plan_jsonl_lines = (base / "plan.jsonl").read_text(
            encoding="ascii").splitlines()
        if len(plan_jsonl_lines) != 1 or strict_json_loads(
                plan_jsonl_lines[0], object_pairs_hook=_pairs) != expected_plan:
            raise BundleError("plan.jsonl differs from candidate profile for %s/%s" % key)

        raw = (base / "proposed-main.raw-u32le.bin").read_bytes()
        if len(raw) != 2048:
            raise BundleError("raw plan image length mismatch for %s/%s" % key)
        words = list(struct.unpack("<512I", raw))
        if words != proposed or packed_sha256(words) != item.get(
                "proposed_main_u32le_sha256"):
            raise BundleError("raw plan image differs from candidate for %s/%s" % key)
        proposed_document = strict_json(base / "proposed-main.json")
        expected_proposed_document = {
            "schema": "170hx-xray-proposed-main-v1",
            "bdf": key[0],
            "profile_id": key[1],
            "rows": ["0x%08x" % value for value in proposed],
            "u32le_sha256": packed_sha256(proposed),
        }
        if proposed_document != expected_proposed_document:
            raise BundleError("proposed-main.json differs from candidate for %s/%s" % key)
        jsonl_words = []
        for row_index, line in enumerate((base / "proposed-main.jsonl").read_text(
                encoding="ascii").splitlines()):
            record = strict_json_loads(line, object_pairs_hook=_pairs)
            if record.get("row") != row_index:
                raise BundleError("JSONL plan row order mismatch for %s/%s" % key)
            jsonl_words.append(_u32(record.get("value"), "JSONL plan row"))
        if jsonl_words != proposed:
            raise BundleError("JSONL plan image differs from candidate for %s/%s" % key)
        text_words = []
        for row_index, line in enumerate((base / "proposed-main.indexed.txt").read_text(
                encoding="ascii").splitlines()):
            match = re.fullmatch(r"\[(\d{3})\] (0x[0-9a-f]{8})", line)
            if match is None or int(match.group(1)) != row_index:
                raise BundleError("indexed plan row grammar/order mismatch for %s/%s" % key)
            text_words.append(int(match.group(2), 16))
        if text_words != proposed:
            raise BundleError("indexed plan image differs from candidate for %s/%s" % key)
    if observed_plan_keys != expected_plan_keys:
        raise BundleError("plan export matrix is incomplete")

    expected_manifest_keys = {
        (card["bdf"], profile_id) for card in candidate["cards"]
        for profile_id in MANIFEST_PROFILES
    }
    manifest_entries = index.get("sealed_v2_manifests")
    expected_manifest_count = 4 * len(MANIFEST_PROFILES)
    if (not isinstance(manifest_entries, list) or
            len(manifest_entries) != expected_manifest_count):
        raise BundleError(
            "sealed manifest inventory is not 4x%d" % len(MANIFEST_PROFILES)
        )
    observed_manifest_keys = set()
    for ordinal, item in enumerate(manifest_entries):
        if not isinstance(item, dict):
            raise BundleError("sealed manifest %d is not an object" % ordinal)
        key = (item.get("bdf"), item.get("profile_id"))
        if key not in expected_manifest_keys or key in observed_manifest_keys:
            raise BundleError(
                "duplicate/unexpected sealed manifest binding: %r" % (key,)
            )
        observed_manifest_keys.add(key)
        expected_relative = "planning/sealed-v2-manifests/%s__%s.json" % (
            _slug_bdf(key[0]), key[1],
        )
        manifest_path = _safe_bundle_path(
            bundle, item.get("path"),
            "sealed manifest %s/%s path" % key, "file",
        )
        if (item.get("path") != expected_relative or
                item.get("validated") is not True or
                item.get("execution_authorized") is not False):
            raise BundleError("sealed manifest index metadata mismatch for %s/%s" % key)
        raw_manifest = strict_json(manifest_path)
        original_reconstructor = backend._runtime_reconstruct_manifest

        def reconstruct_relocated(manifest):
            return _reconstruct_relocated_manifest(
                manifest, compiler, candidate_path, bundle,
                source_binding, capture_map,
            )

        backend._runtime_reconstruct_manifest = reconstruct_relocated
        try:
            validated = backend.load_and_validate_manifest(
                manifest_path, key[0], key[1]
            )
        except Exception as exc:
            raise BundleError(
                "sealed manifest backend validation failed for %s/%s: %s" %
                (key[0], key[1], exc)
            ) from exc
        finally:
            backend._runtime_reconstruct_manifest = original_reconstructor
        source = raw_manifest.get("candidate_source", {})
        _profile, proposed = profile_map[key]
        if (source.get("file_sha256") != candidate_sha or
                source.get("report_content_sha256_excluding_self") !=
                candidate.get("report_content_sha256_excluding_self") or
                raw_manifest.get("binding", {}).get(
                    "proposed_main_u32le_sha256") != packed_sha256(proposed) or
                validated.get("proposed") != proposed):
            raise BundleError("sealed manifest candidate binding mismatch for %s/%s" % key)
    if observed_manifest_keys != expected_manifest_keys:
        raise BundleError("sealed manifest matrix is incomplete")

    counts = index.get("counts")
    if not isinstance(counts, dict) or (
            counts.get("cards"), counts.get("candidate_profiles"),
            counts.get("sealed_v2_manifests")) != (
                4,
                4 * len(CANDIDATE_PROFILES),
                4 * len(MANIFEST_PROFILES),
            ):
        raise BundleError("index count summary mismatch")
    if counts.get("read_levels_available") != sum(
            1 for item in coverage["levels"] if item["available"]):
        raise BundleError("read-level available count mismatch")
    if counts.get("read_levels_unavailable") != len(
            coverage["unavailable_levels"]):
        raise BundleError("read-level unavailable count mismatch")
    safety = index.get("safety")
    if not isinstance(safety, dict) or (
            safety.get("hardware_access_performed") is not False or
            safety.get("candidate_source_executed") is not False or
            safety.get("otp_backend_invoked") is not False or
            safety.get("programming_writes_performed") != 0 or
            safety.get("fabricated_observations") != 0 or
            safety.get("manifest_presence_is_not_execution_authority") is not True):
        raise BundleError("bundle safety declaration mismatch")


def verify(bundle, root=None):
    root = Path(root or _repo_root()).resolve()
    requested_bundle = Path(bundle)
    if requested_bundle.is_symlink():
        return {"schema": SCHEMA + "-verification", "valid": False,
                "errors": ["bundle root must not be a symlink"]}
    bundle = requested_bundle.resolve()
    errors = []
    if bundle.is_symlink() or not bundle.is_dir():
        return {"schema": SCHEMA + "-verification", "valid": False,
                "errors": ["bundle is not a real directory"]}
    try:
        entries = _parse_manifest(bundle / "SHA256SUMS")
    except (BundleError, OSError, UnicodeError) as exc:
        return {"schema": SCHEMA + "-verification", "valid": False,
                "errors": [str(exc)]}
    actual = set()
    for path in bundle.rglob("*"):
        relative = path.relative_to(bundle).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            errors.append("symlink present: %s" % relative)
        elif stat.S_ISDIR(info.st_mode):
            if stat.S_IMODE(info.st_mode) != 0o700:
                errors.append("directory mode is not 0700: %s" % relative)
        elif stat.S_ISREG(info.st_mode):
            if relative != "SHA256SUMS":
                actual.add(relative)
            if stat.S_IMODE(info.st_mode) != 0o600:
                errors.append("file mode is not 0600: %s" % relative)
        else:
            errors.append("special object present: %s" % relative)
    if actual != set(entries):
        errors.append("manifest file-set mismatch")
    for relative, expected in entries.items():
        path = bundle / relative
        if path.is_file() and not path.is_symlink() and sha256_file(path) != expected:
            errors.append("digest mismatch: %s" % relative)
    try:
        _verify_semantic_bundle(bundle, root)
    except (BundleError, OSError, UnicodeError, ValueError, KeyError,
            json.JSONDecodeError) as exc:
        errors.append("semantic verification failed: %s" % exc)
    return {
        "schema": SCHEMA + "-verification",
        "bundle": str(bundle),
        "valid": not errors,
        "errors": errors,
        "manifest_entries": len(entries),
        "hardware_access_performed": False,
        "otp_backend_invoked": False,
    }


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--full-dump-bundle", required=True)
    build_parser.add_argument("--candidate", required=True)
    build_parser.add_argument("--manifest-set", required=True)
    build_parser.add_argument("--output", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle", required=True)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build(args.full_dump_bundle, args.candidate,
                           args.manifest_set, args.output)
        else:
            result = verify(args.bundle)
        print(strict_json_dumps(result, sort_keys=True, indent=2))
        return 0 if result.get("valid") else 2
    except (BundleError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
