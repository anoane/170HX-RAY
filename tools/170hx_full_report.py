#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build one fail-closed, evidence-indexed 170HX-XRAY Markdown report.

This orchestrates existing readers, planners and diagnostics.  It implements no
BAR/MMIO or OTP primitive.  Every component is completed, failed, skipped,
not-requested, or not-yet-supported; missing work is never silently counted as
success.
"""

import argparse
from collections import Counter
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys


VERSION = "0.6.0"
SCHEMA = "170hx-xray-full-report-v1"
REPORT_MODES = ("report", "report+bench")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHBENCH_SUBMODULE_ROOT = (
    PROJECT_ROOT / "third_party" / "archbench_cuda13_5_v0_2_0"
)
ARCHBENCH_SUBMODULE_LOCK = PROJECT_ROOT / "docs" / "ARCHBENCH_SUBMODULE_LOCK.json"
BENCHMARK_ONLY_OPTIONS = frozenset((
    "--benchmarks", "--set-power-limit", "--power-limit-watts",
    "--power-sample-interval-ms", "--bandwidth-bytes",
    "--bandwidth-iterations", "--p2p-min-bytes", "--p2p-max-bytes",
    "--p2p-iterations", "--gds-scratch", "--gds-size", "--gds-io-size",
    "--media-suite", "--media-suite-sha256", "--temperature-probe-device",
    "--temperature-probe-backend", "--temperature-access",
    "--temperature-acknowledge-selector-write",
    "--temperature-acknowledge-bound-snapshot",
    "--temperature-acknowledge-hbm-data-read-to-advance",
    "--temperature-allow-active-workload", "--archbench",
    "--archbench-root", "--archbench-binary", "--archbench-case-timeout",
    "--archbench-telemetry-interval", "--benchmark-timeout",
))
POWER_SUMMARY_SCHEMA = "170hx-xray-benchmark-power-summary-v1"
POWER_EVIDENCE_SCHEMA = "170hx-xray-benchmark-power-evidence-v1"
POWER_AVERAGE_METHOD = "arithmetic-mean-of-successful-periodic-samples"
FEATURE_CENSUS_SCHEMA = "170hx-xray-ga100-feature-census-v1"
OFA_PROBE_SCHEMA = "170hx-xray-ofa-api-probe-v1"
FEATURE_COMMAND_FIELDS = {
    "identity": ("pci.bus_id", "uuid", "name"),
    "ecc": (
        "ecc.mode.current", "ecc.mode.pending",
        "ecc.errors.corrected.volatile.total",
        "ecc.errors.uncorrected.volatile.total",
        "ecc.errors.corrected.aggregate.total",
        "ecc.errors.uncorrected.aggregate.total",
    ),
    "mig_mode": ("mig.mode.current", "mig.mode.pending"),
    "sessions": (
        "encoder.stats.sessionCount", "encoder.stats.averageFps",
        "encoder.stats.averageLatency", "decoder.stats.sessionCount",
        "decoder.stats.averageFps", "decoder.stats.averageLatency",
    ),
}
FEATURE_COMMAND_KEYS = (
    "identity", "full_text", "full_xml", "ecc", "mig_mode", "sessions",
    "mig_gpu_profiles", "mig_compute_profiles", "nvlink",
)
GA100_IDS = frozenset(("20c2", "20b5", "2080"))
BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
MAX_OUTPUT = 32 * 1024 * 1024
MANIFEST_PROFILES = (
    "specimen-80gb-hbm",
    "specimen-more-sm",
    "xray-common-permanent-topology",
    "specimen-pcie-gen4-isolated",
    "specimen-a100-20b5-identity",
    "specimen-sys-pipe-to-defect-experimental",
    "specimen-media-mig-to-defect-experimental",
)
FULL_REPORT_FUSELESS_START = 222
FULL_REPORT_FUSELESS_END = 511
FULL_REPORT_BOUNDS_PROVENANCE = (
    "empirical retained GA100/CMP population bound; not an official GA100 "
    "physical descriptor"
)
FULL_REPORT_EXPORT_PROVENANCE = (
    "170HX-XRAY full-report complete exporter request; source-specific "
    "certification is retained; serialization grants no write authority"
)
ROW_EXPORT_FORMATS = (
    "xray-json", "xray-jsonl", "indexed-text", "raw-u32le", "ffcalc-json",
    "xpatrik-json", "ga100-fuse-re-json", "dstoro-projection-json",
    "dstoro-jsonl",
)
RIR_EXPORT_KINDS = (
    "rir-raw-u32le", "rir-indexed-text", "rir-json", "rir-jsonl",
)
REGISTER_EXPORT_KINDS = (
    "register-json", "register-jsonl", "register-text",
    "register-address-value-u32le",
)
OBSERVATION_TRANSPORT_KINDS = {
    "json": "live-capture",
    "jsonl": "live-capture-jsonl",
    "text": "live-capture-text",
    "binary_register_records": "live-capture-register-u32le",
}
CHECKLIST_PLANNING_MODELS = (
    "evidence-only", "public-model", "cross-checked-theory", "compare",
)
CANDIDATE_EXPORT_FILENAMES = {
    "json": "candidate-plan.json",
    "jsonl": "candidate-plan.jsonl",
    "text": "candidate-plan.txt",
    "canonical-json-bin": "candidate-plan.canonical-json.bin",
}
CANDIDATE_TEXT_BEGIN = "----- BEGIN CANONICAL CANDIDATE PLAN JSON -----"
CANDIDATE_TEXT_END = "----- END CANONICAL CANDIDATE PLAN JSON -----"
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
CANDIDATE_PROFILE_COUNT = len(CANDIDATE_PROFILES)
COMPONENT_STATUSES = frozenset((
    "completed", "failed", "skipped", "not-requested",
    "not-yet-supported",
))
DRIVER_PATCH_MARKERS = (
    ("gpu-unlock", re.compile(r"(?i)(?<![a-z0-9])gpu[-_ ]unlock(?![a-z0-9])")),
    ("cmpunlocker", re.compile(r"(?i)(?<![a-z0-9])cmp[-_ ]?unlocker(?![a-z0-9])")),
    ("anemone", re.compile(r"(?i)(?<![a-z0-9])anemone(?![a-z0-9])")),
    ("170hx-patch", re.compile(
        r"(?i)(?<![a-z0-9])170hx[-_ ](?:unlock|patch)(?![a-z0-9])"
    )),
    ("faceb13d", re.compile(r"(?i)(?<![a-z0-9])faceb13d(?![a-z0-9])")),
)
ARCHBENCH_EXPECTED_FAMILIES = {
    "atomic": 12, "bit": 3, "blas": 10, "collective": 7,
    "convert": 17, "fft": 18, "gemm": 25, "integer": 36,
    "integer_width": 1, "launch": 2, "memory": 3, "packed": 4,
    "quant": 14, "quant_gemm": 3, "scalar": 4, "sfu": 12,
    "sparse": 2, "tensor": 35, "transfer": 5, "workload": 22,
}
ARCHBENCH_SELECTED_CASES = 235
ARCHBENCH_TERMINAL_STATUSES = frozenset((
    "ok", "validation_failed", "unsupported", "not_implemented",
    "not_validated", "missing_binary", "architecture_mismatch",
    "memory_budget", "out_of_memory", "timeout", "error", "interrupted",
    "compile_failed", "missing_dependency", "probe_only",
    "measurement_only",
))
ARCHBENCH_NONFATAL_STATUSES = frozenset((
    "ok", "measurement_only", "unsupported", "memory_budget",
))
ARCHBENCH_VALIDATED = frozenset((
    "passed", "pass", "passed_sampled", "passed_full",
))
ARCHBENCH_STABLE_POLICY = {
    "samples": 11, "max-samples": 31, "target-cv": 0.03,
    "warmup": 10, "warmup-ms": 200, "target-ms": 50,
    "repeats": 0, "max-repeats": 10000, "memory-fraction": 0.7,
    "memory-limit-mib": 0,
}


class ReportError(RuntimeError):
    pass


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def token(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


def evidence_age_hours(value, reference=None):
    if not isinstance(value, str) or not value:
        raise ReportError("evidence timestamp is absent")
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError("evidence timestamp is malformed: %s" % value) from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ReportError("evidence timestamp lacks a timezone")
    age = ((reference or now_utc()) - moment.astimezone(datetime.timezone.utc))
    hours = age.total_seconds() / 3600.0
    if hours < -0.1:
        raise ReportError("evidence timestamp is unexpectedly in the future")
    return max(0.0, hours)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def create_dir(path, parents=False):
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=parents, exist_ok=False)
    except FileExistsError as exc:
        raise ReportError("path already exists: %s" % path) from exc
    os.chmod(str(path), 0o700)
    return path


def write_new(path, payload, binary=False):
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        data = payload if binary else payload.encode("utf-8")
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise ReportError("short write: %s" % path)
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def seal_private_tree(root):
    """Reject links/special files and enforce private modes recursively."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ReportError("report root is not a real directory: %s" % root)
    entries = sorted(root.rglob("*"), key=lambda value: value.as_posix())
    directories = 1
    files = 0
    for path in entries:
        if path.is_symlink():
            raise ReportError("symlink in report: %s" % path)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            os.chmod(str(path), 0o700)
            directories += 1
        elif stat.S_ISREG(mode):
            os.chmod(str(path), 0o600)
            files += 1
        else:
            raise ReportError("non-regular object in report: %s" % path)
    os.chmod(str(root), 0o700)
    for path in [root] + entries:
        if path.is_symlink():
            raise ReportError("symlink appeared while sealing report: %s" % path)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            expected = 0o700
        elif stat.S_ISREG(mode):
            expected = 0o600
        else:
            raise ReportError("non-regular object in sealed report: %s" % path)
        observed = stat.S_IMODE(mode)
        if observed != expected:
            raise ReportError(
                "private mode mismatch for %s: %04o != %04o" %
                (path, observed, expected)
            )
    return {"directories": directories, "regular_files": files}


def copy_regular_new(source, target):
    source = Path(source)
    target = Path(target)
    if source.is_symlink() or not source.is_file():
        raise ReportError("evidence input is not a regular file: %s" % source)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    output_fd = os.open(str(target), flags, 0o600)
    input_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        input_flags |= os.O_NOFOLLOW
    input_fd = os.open(str(source), input_flags)
    try:
        while True:
            block = os.read(input_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                count = os.write(output_fd, view)
                if count <= 0:
                    raise ReportError("short evidence copy: %s" % target)
                view = view[count:]
        os.fsync(output_fd)
    finally:
        os.close(input_fd)
        os.close(output_fd)
    return target


def copy_tree_new(source, destination):
    """Copy a verified evidence tree without following links or overwriting."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source.is_symlink() or not source.is_dir():
        raise ReportError("imported bundle is not a real directory: %s" % source)
    for child, parent in ((destination, source), (source, destination)):
        try:
            child.relative_to(parent)
        except ValueError:
            continue
        raise ReportError("imported bundle and report destination must not nest")
    entries = sorted(source.rglob("*"), key=lambda value: value.as_posix())
    for entry in entries:
        if entry.is_symlink():
            raise ReportError("symlink in imported bundle: %s" % entry)
        if not entry.is_dir() and not entry.is_file():
            raise ReportError("non-regular imported bundle object: %s" % entry)
    create_dir(destination)
    for entry in entries:
        relative = entry.relative_to(source)
        target = destination / relative
        if entry.is_dir():
            target.mkdir(mode=0o700, exist_ok=False)
            os.chmod(str(target), 0o700)
            continue
        copy_regular_new(entry, target)
    return destination


def _strict_object(label):
    def build(pairs):
        value = {}
        for key, item_value in pairs:
            if key in value:
                raise ReportError("duplicate JSON key in %s: %s" % (label, key))
            value[key] = item_value
        return value
    return build


def strict_json(path):
    def reject(value):
        raise ReportError("non-finite JSON in %s: %s" % (path, value))
    with Path(path).open("r", encoding="utf-8") as source:
        return json.load(
            source, parse_constant=reject,
            object_pairs_hook=_strict_object(path),
        )


def _finite_number(value, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    if positive and float(value) <= 0:
        return False
    if minimum is not None and float(value) < float(minimum):
        return False
    return True


def _number_equal(left, right, tolerance=1.0e-9):
    return (_finite_number(left) and _finite_number(right) and
            abs(float(left) - float(right)) <= tolerance)


def _power_limit_equal(left, right):
    return (_finite_number(left, positive=True) and
            _finite_number(right, positive=True) and
            abs(float(left) - float(right)) <= 0.5)


def _feature_bdf(value):
    """Normalize nvidia-smi's 8-digit domain without accepting aliases."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(?P<domain>[0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}):"
        r"(?P<bus>[0-9A-Fa-f]{2}):(?P<device>[0-9A-Fa-f]{2})\."
        r"(?P<function>[0-7])", value,
    )
    if match is None:
        return None
    domain = match.group("domain").lower()
    if len(domain) == 8:
        if domain[:4] != "0000":
            return None
        domain = domain[4:]
    return "%s:%s:%s.%s" % (
        domain, match.group("bus").lower(),
        match.group("device").lower(), match.group("function"),
    )


def _feature_command(record, key, uuid, smi_path, smi_sha256):
    """Validate retained command bytes and their binding to one exact UUID."""
    expected_keys = {
        "argv", "cwd", "started_utc", "dry_run", "environment_overrides",
        "executable", "executable_sha256", "trust_boundary", "status",
        "returncode", "duration_seconds", "stdout", "stderr",
        "stdout_sha256", "stderr_sha256",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise ReportError("feature command %s shape mismatch" % key)
    argv = record.get("argv")
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(value, str) for value in argv) or
            record.get("executable") != argv[0] or
            not isinstance(record.get("dry_run"), bool) or
            record.get("environment_overrides") != {} or
            record.get("cwd") is not None or
            not isinstance(record.get("trust_boundary"), str) or
            not record["trust_boundary"] or
            record.get("status") not in ("completed", "timeout") or
            isinstance(record.get("returncode"), bool) or
            (record.get("returncode") is not None and
             not isinstance(record.get("returncode"), int)) or
            not _finite_number(record.get("duration_seconds"), minimum=0)):
        raise ReportError("feature command %s provenance mismatch" % key)
    queried = FEATURE_COMMAND_FIELDS.get(key)
    expected_argv = {
        "identity": [
            smi_path, "-i", uuid,
            "--query-gpu=%s" % ",".join(FEATURE_COMMAND_FIELDS["identity"]),
            "--format=csv,noheader,nounits",
        ],
        "full_text": [smi_path, "-i", uuid, "-q"],
        "full_xml": [smi_path, "-i", uuid, "-q", "-x"],
        "ecc": [
            smi_path, "-i", uuid, "--query-gpu=%s" % ",".join(queried),
            "--format=csv,noheader,nounits",
        ] if key == "ecc" else None,
        "mig_mode": [
            smi_path, "-i", uuid, "--query-gpu=%s" % ",".join(queried),
            "--format=csv,noheader,nounits",
        ] if key == "mig_mode" else None,
        "sessions": [
            smi_path, "-i", uuid, "--query-gpu=%s" % ",".join(queried),
            "--format=csv,noheader,nounits",
        ] if key == "sessions" else None,
        "mig_gpu_profiles": [smi_path, "mig", "-i", uuid, "-lgip"],
        "mig_compute_profiles": [smi_path, "mig", "-i", uuid, "-lgci"],
        "nvlink": [smi_path, "nvlink", "-i", uuid, "-s"],
    }[key]
    if argv != expected_argv:
        raise ReportError("feature command %s argv mismatch" % key)
    for digest_key in ("executable_sha256", "stdout_sha256", "stderr_sha256"):
        digest = record.get(digest_key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReportError("feature command %s hash is malformed" % key)
    if record.get("executable_sha256") != smi_sha256:
        raise ReportError("feature command %s executable hash mismatch" % key)
    for stream in ("stdout", "stderr"):
        payload = record.get(stream)
        if (not isinstance(payload, str) or
                hashlib.sha256(payload.encode("utf-8")).hexdigest() !=
                record.get(stream + "_sha256")):
            raise ReportError("feature command %s %s hash mismatch" % (key, stream))
    return record.get("returncode") == 0


def _feature_csv(record, fields, label):
    if record.get("returncode") != 0:
        return None
    rows = [line.strip() for line in record.get("stdout", "").splitlines()
            if line.strip()]
    if len(rows) != 1:
        raise ReportError("feature %s CSV row count mismatch" % label)
    values = [value.strip() for value in rows[0].split(",")]
    if len(values) != len(fields):
        raise ReportError("feature %s CSV field count mismatch" % label)
    return dict(zip(fields, values))


def _feature_status(success):
    return "observed" if success else "unsupported-or-query-failed"


def _nvlink_query_status(record, success):
    if not success:
        return "unsupported-or-query-failed"
    stdout = record.get("stdout", "").lower()
    if ("does not have or support nvlink" in stdout or
            "does not support nvlink" in stdout):
        return "unsupported-reported-by-nvidia-smi"
    return "observed-see-raw-evidence"


def _media_functional_rows(document, selected_bdfs):
    """Return explicit per-card functional states; absence means not run."""
    rows = {
        bdf: {
            "nvenc_functional": "not-run",
            "nvdec_functional": "not-run",
            "nvjpeg_functional": "not-run",
        }
        for bdf in selected_bdfs
    }
    media = document.get("media")
    if media is None:
        return rows
    if not isinstance(media, dict) or not isinstance(media.get("status"), str):
        raise ReportError("diagnostics media component shape mismatch")
    results = media.get("results")
    if results is None:
        status = media.get("status")
        if status not in ("skipped", "planned", "failed"):
            raise ReportError("diagnostics media result inventory is absent")
        marker = "skipped" if status == "skipped" else status
        for row in rows.values():
            for key in tuple(row):
                row[key] = marker
        return rows
    if not isinstance(results, list):
        raise ReportError("diagnostics media results are not an array")
    seen = set()
    allowed = {"positive", "negative", "failed", "planned", "skipped"}
    for result in results:
        if not isinstance(result, dict):
            raise ReportError("diagnostics media result is not an object")
        bdf = result.get("bdf")
        if bdf not in rows or bdf in seen:
            raise ReportError("diagnostics media BDF is unknown or duplicated")
        seen.add(bdf)
        probes = result.get("probes")
        nvenc = result.get("nvenc")
        if not isinstance(probes, dict) or not isinstance(nvenc, dict):
            raise ReportError("diagnostics media probe inventory is absent")
        values = {
            "nvenc_functional": nvenc.get("status"),
            "nvdec_functional": (
                "%s/caps, %s/decode" % (
                    (probes.get("nvdec_caps") or {}).get("status"),
                    (probes.get("nvdec_decode") or {}).get("status"),
                )
            ),
            "nvjpeg_functional": (probes.get("nvjpg") or {}).get("status"),
        }
        if values["nvenc_functional"] not in allowed or values[
                "nvjpeg_functional"] not in allowed:
            raise ReportError("diagnostics media verdict is invalid")
        caps = (probes.get("nvdec_caps") or {}).get("status")
        decode = (probes.get("nvdec_decode") or {}).get("status")
        if caps not in allowed or decode not in allowed:
            raise ReportError("diagnostics NVDEC verdict is invalid")
        rows[bdf].update(values)
    return rows


def diagnostics_feature_rows(document, selected_bdfs):
    """Independently validate and summarize the query-only feature census."""
    if not isinstance(document, dict):
        raise ReportError("diagnostics document is not an object")
    census = document.get("feature_inventory")
    expected_census_keys = {
        "schema", "status", "query_only", "mutations_requested",
        "nvidia_smi", "ofa_api_probe", "cards", "interpretation",
        "validation_errors",
    }
    if not isinstance(census, dict) or set(census) != expected_census_keys:
        raise ReportError("feature census shape mismatch")
    interpretation = {
        "capability_queries_are_not_benchmarks": True,
        "functional_media_requires_external_suite": True,
        "mig_configuration_performed": False,
        "ecc_injection_performed": False,
        "ofa_functional_test": "not-yet-supported",
    }
    if (census.get("schema") != FEATURE_CENSUS_SCHEMA or
            census.get("status") != "completed" or
            census.get("query_only") is not True or
            census.get("mutations_requested") is not False or
            census.get("interpretation") != interpretation or
            census.get("validation_errors") != []):
        raise ReportError("feature census contract did not pass")
    smi = census.get("nvidia_smi")
    if (not isinstance(smi, dict) or set(smi) != {"path", "sha256"} or
            not isinstance(smi.get("path"), str) or not smi["path"].startswith("/") or
            not isinstance(smi.get("sha256"), str) or
            re.fullmatch(r"[0-9a-f]{64}", smi["sha256"]) is None):
        raise ReportError("feature census nvidia-smi provenance is malformed")
    ofa = census.get("ofa_api_probe")
    expected_ofa_keys = {
        "schema", "query_only", "creates_cuda_context",
        "creates_ofa_session", "candidates", "selected_candidate",
        "resolved_library_path", "resolved_library_sha256", "load_errors",
        "symbol", "return_code", "maximum_api_version", "status",
        "interpretation",
    }
    if not isinstance(ofa, dict) or set(ofa) != expected_ofa_keys:
        raise ReportError("OFA API probe shape mismatch")
    ofa_statuses = {
        "library-unavailable", "symbol-unavailable", "query-failed",
        "query-rejected", "api-version-observed",
    }
    if (ofa.get("schema") != OFA_PROBE_SCHEMA or
            ofa.get("query_only") is not True or
            ofa.get("creates_cuda_context") is not False or
            ofa.get("creates_ofa_session") is not False or
            ofa.get("symbol") != "NvOFGetMaxSupportedApiVersion" or
            ofa.get("status") not in ofa_statuses or
            ofa.get("interpretation") !=
            "host userspace API query only; not bound to a GPU and not functional OFA evidence"):
        raise ReportError("OFA API probe contract mismatch")
    if ofa.get("status") == "api-version-observed":
        if (ofa.get("return_code") != 0 or
                isinstance(ofa.get("maximum_api_version"), bool) or
                not isinstance(ofa.get("maximum_api_version"), int) or
                ofa["maximum_api_version"] <= 0 or
                not isinstance(ofa.get("resolved_library_sha256"), str) or
                re.fullmatch(r"[0-9a-f]{64}", ofa["resolved_library_sha256"]) is None):
            raise ReportError("OFA observed API version evidence is malformed")
    functional = _media_functional_rows(document, selected_bdfs)
    cards = census.get("cards")
    if (not isinstance(cards, list) or len(cards) != len(selected_bdfs)):
        raise ReportError("feature census card count mismatch")
    rows = []
    seen = set()
    for card in cards:
        expected_card_keys = {
            "bdf", "uuid", "status", "identity", "xml_identity",
            "commands", "features",
        }
        if not isinstance(card, dict) or set(card) != expected_card_keys:
            raise ReportError("feature census card shape mismatch")
        bdf = card.get("bdf")
        uuid = card.get("uuid")
        if (bdf not in selected_bdfs or bdf in seen or
                not isinstance(uuid, str) or not uuid.startswith("GPU-") or
                card.get("status") != "completed"):
            raise ReportError("feature census card identity/status mismatch")
        seen.add(bdf)
        identity = card.get("identity")
        if (not isinstance(identity, dict) or
                set(identity) != set(FEATURE_COMMAND_FIELDS["identity"]) or
                _feature_bdf(identity.get("pci.bus_id")) != bdf or
                identity.get("uuid") != uuid or
                not isinstance(identity.get("name"), str)):
            raise ReportError("feature census identity parse mismatch")
        xml_identity = card.get("xml_identity")
        if xml_identity != {"bdf": bdf, "uuid": uuid}:
            raise ReportError("feature census XML identity mismatch")
        commands = card.get("commands")
        if not isinstance(commands, dict) or set(commands) != set(FEATURE_COMMAND_KEYS):
            raise ReportError("feature command inventory mismatch")
        command_success = {
            key: _feature_command(
                commands[key], key, uuid, smi["path"], smi["sha256"]
            )
            for key in FEATURE_COMMAND_KEYS
        }
        if not all(command_success[key] for key in ("identity", "full_text", "full_xml")):
            raise ReportError("required feature identity command failed")
        features = card.get("features")
        if not isinstance(features, dict) or set(features) != {
                "ecc", "mig", "nvlink", "nvenc_nvdec_sessions", "nvjpeg", "ofa"}:
            raise ReportError("feature observation inventory mismatch")
        ecc_fields = _feature_csv(
            commands["ecc"], FEATURE_COMMAND_FIELDS["ecc"], "ECC"
        )
        ecc = features.get("ecc")
        if (not isinstance(ecc, dict) or set(ecc) != {
                "status", "fields", "command_key", "injection_performed"} or
                ecc.get("status") != _feature_status(command_success["ecc"]) or
                ecc.get("fields") != ecc_fields or ecc.get("command_key") != "ecc" or
                ecc.get("injection_performed") is not False):
            raise ReportError("feature ECC mirror mismatch")
        mig_fields = _feature_csv(
            commands["mig_mode"], FEATURE_COMMAND_FIELDS["mig_mode"], "MIG"
        )
        mig = features.get("mig")
        if (not isinstance(mig, dict) or set(mig) != {
                "status", "fields", "gpu_instance_profiles_status",
                "compute_instance_profiles_status", "command_keys",
                "configuration_performed"} or
                mig.get("status") != _feature_status(command_success["mig_mode"]) or
                mig.get("fields") != mig_fields or
                mig.get("gpu_instance_profiles_status") !=
                _feature_status(command_success["mig_gpu_profiles"]) or
                mig.get("compute_instance_profiles_status") !=
                _feature_status(command_success["mig_compute_profiles"]) or
                mig.get("command_keys") != [
                    "mig_mode", "mig_gpu_profiles", "mig_compute_profiles"] or
                mig.get("configuration_performed") is not False):
            raise ReportError("feature MIG mirror mismatch")
        sessions_fields = _feature_csv(
            commands["sessions"], FEATURE_COMMAND_FIELDS["sessions"], "sessions"
        )
        sessions = features.get("nvenc_nvdec_sessions")
        if (not isinstance(sessions, dict) or
                sessions.get("status") != _feature_status(command_success["sessions"]) or
                sessions.get("fields") != sessions_fields or
                sessions.get("command_key") != "sessions"):
            raise ReportError("feature media-session mirror mismatch")
        nvlink = features.get("nvlink")
        if (not isinstance(nvlink, dict) or
                nvlink != {"status": _feature_status(command_success["nvlink"]),
                           "command_key": "nvlink"}):
            raise ReportError("feature NVLink mirror mismatch")
        if features.get("nvjpeg") != {
                "status": "not-exposed-by-nvidia-smi",
                "reason": "functional NVJPEG evidence requires the external media suite"}:
            raise ReportError("feature NVJPEG boundary mismatch")
        if features.get("ofa") != {
                "status": "api-query-not-device-bound",
                "global_probe_status": ofa.get("status"),
                "reason": "maximum API version is a host-library query; functional OFA work remains not-yet-supported"}:
            raise ReportError("feature OFA boundary mismatch")
        rows.append({
            "bdf": bdf, "uuid": uuid, "census_status": card["status"],
            "ecc_status": ecc["status"],
            "ecc_current": (ecc_fields or {}).get("ecc.mode.current"),
            "ecc_pending": (ecc_fields or {}).get("ecc.mode.pending"),
            "ecc_injection": "not-run",
            "mig_status": mig["status"],
            "mig_current": (mig_fields or {}).get("mig.mode.current"),
            "mig_pending": (mig_fields or {}).get("mig.mode.pending"),
            "mig_gpu_profiles": mig["gpu_instance_profiles_status"],
            "mig_compute_profiles": mig["compute_instance_profiles_status"],
            "mig_configuration": "not-run",
            "nvlink_query": _nvlink_query_status(
                commands["nvlink"], command_success["nvlink"]
            ),
            "media_session_query": sessions["status"],
            "nvjpeg_inventory": features["nvjpeg"]["status"],
            "ofa_api_query": ofa["status"],
            "ofa_max_api_version": ofa.get("maximum_api_version"),
            "ofa_functional": "not-yet-supported",
            **functional[bdf],
        })
    if seen != set(selected_bdfs):
        raise ReportError("feature census BDF coverage mismatch")
    return sorted(rows, key=lambda row: row["bdf"])


def diagnostics_power_rows(document, selected_bdfs, expected_limit):
    """Independently validate diagnostics benchmark power evidence.

    This intentionally does not trust the diagnostics producer's status or
    aggregates.  It recomputes every draw statistic and coverage mirror before
    returning rows suitable for the Markdown report.
    """
    if not isinstance(document, dict) or document.get("status") != "completed":
        raise ReportError("diagnostics document is not completed")
    summary = document.get("benchmark_power_evidence")
    expected_summary_keys = {
        "schema", "status", "requested_limit_watts",
        "sampling_interval_ms_requested", "requested_families",
        "executed_families", "executed_target_bdfs", "scope_count",
        "scopes", "validation_errors", "scope_policy",
    }
    if not isinstance(summary, dict) or set(summary) != expected_summary_keys:
        raise ReportError("diagnostics power summary shape mismatch")
    if summary.get("schema") != POWER_SUMMARY_SCHEMA:
        raise ReportError("diagnostics power summary schema mismatch")
    if summary.get("status") != "pass" or summary.get("validation_errors") != []:
        raise ReportError("diagnostics power summary did not pass")
    if not _number_equal(summary.get("requested_limit_watts"), expected_limit):
        raise ReportError("diagnostics requested power limit mismatch")
    interval_ms = summary.get("sampling_interval_ms_requested")
    if (isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or
            interval_ms <= 0):
        raise ReportError("diagnostics power sampling interval is invalid")
    requested_families = summary.get("requested_families")
    if requested_families != ["bandwidth", "p2p", "gds", "media"]:
        raise ReportError("diagnostics full-mode family inventory mismatch")
    scopes = summary.get("scopes")
    if (not isinstance(scopes, list) or
            summary.get("scope_count") != len(scopes)):
        raise ReportError("diagnostics power scope count mismatch")
    selected = set(selected_bdfs)
    family_positions = {family: 0 for family in requested_families}
    executed_families = set()
    executed_targets = {}
    rows = []
    for scope_position, scope in enumerate(scopes):
        expected_scope_keys = {
            "family", "scope_index", "component_path", "component_status",
            "benchmark_executed", "target_bdfs", "evidence",
            "validation_errors",
        }
        if not isinstance(scope, dict) or set(scope) != expected_scope_keys:
            raise ReportError(
                "diagnostics power scope %d shape mismatch" % scope_position
            )
        family = scope.get("family")
        if family not in family_positions:
            raise ReportError("diagnostics power scope has unknown family")
        if scope.get("scope_index") != family_positions[family]:
            raise ReportError("diagnostics power scope index mismatch")
        family_positions[family] += 1
        if (not isinstance(scope.get("component_path"), str) or
                not scope["component_path"] or
                not isinstance(scope.get("component_status"), str) or
                not scope["component_status"]):
            raise ReportError("diagnostics power scope provenance is malformed")
        if not isinstance(scope.get("benchmark_executed"), bool):
            raise ReportError("diagnostics benchmark-executed marker is not boolean")
        target_bdfs = scope.get("target_bdfs")
        if (not isinstance(target_bdfs, list) or len(target_bdfs) != len(set(target_bdfs)) or
                any(not isinstance(bdf, str) or bdf not in selected
                    for bdf in target_bdfs)):
            raise ReportError("diagnostics power targets are invalid")
        if scope.get("validation_errors") != []:
            raise ReportError("diagnostics power scope contains validation errors")
        if not scope["benchmark_executed"]:
            continue
        executed_families.add(family)
        executed_targets.setdefault(family, set()).update(target_bdfs)
        evidence = scope.get("evidence")
        expected_evidence_keys = {
            "schema", "family", "status", "requested_limit_watts",
            "sampling_interval_ms_requested", "sampling_provenance", "cards",
        }
        if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
            raise ReportError("executed benchmark power evidence shape mismatch")
        if (evidence.get("schema") != POWER_EVIDENCE_SCHEMA or
                evidence.get("family") != family or
                evidence.get("status") != "pass" or
                not _number_equal(evidence.get("requested_limit_watts"),
                                  expected_limit) or
                evidence.get("sampling_interval_ms_requested") != interval_ms):
            raise ReportError("executed benchmark power evidence contract mismatch")
        provenance = evidence.get("sampling_provenance")
        expected_provenance_keys = {
            "backend", "executable", "executable_sha256", "query_fields",
            "query_argv_template", "control_commands_in_global_journal",
            "periodic_queries_in_global_journal",
        }
        if (not isinstance(provenance, dict) or
                set(provenance) != expected_provenance_keys or
                provenance.get("backend") != "nvidia-smi-periodic-query" or
                provenance.get("query_fields") != ["power.draw", "power.limit"] or
                provenance.get("control_commands_in_global_journal") is not True or
                provenance.get("periodic_queries_in_global_journal") is not False or
                not isinstance(provenance.get("executable"), str) or
                not provenance["executable"] or
                not isinstance(provenance.get("executable_sha256"), str) or
                re.fullmatch(r"[0-9a-f]{64}", provenance["executable_sha256"]) is None or
                not isinstance(provenance.get("query_argv_template"), list)):
            raise ReportError("benchmark power sampler provenance is malformed")
        cards = evidence.get("cards")
        if (not isinstance(cards, list) or len(cards) != len(target_bdfs) or
                {card.get("bdf") for card in cards if isinstance(card, dict)} !=
                set(target_bdfs)):
            raise ReportError("benchmark power evidence card coverage mismatch")
        for card in cards:
            required_card_keys = {
                "bdf", "uuid", "nvidia_smi_index", "original_limit_watts",
                "observed_limit_before_watts",
                "observed_limit_after_enforce_watts",
                "observed_limit_post_benchmark_watts",
                "observed_limit_after_restore_watts",
                "limit_enforced_during_benchmark", "restore_attempted",
                "restore_succeeded", "sample_count", "sample_failures",
                "power_draw_w_min", "power_draw_w_max", "power_draw_w_avg",
                "power_draw_average_method", "sampling_span_seconds", "samples",
                "pre_query_command", "set_command",
                "enforce_verification_query_command",
                "post_benchmark_query_command", "restore_command",
                "restore_verification_query_command", "error",
            }
            if not isinstance(card, dict) or set(card) != required_card_keys:
                raise ReportError("benchmark power card evidence shape mismatch")
            bdf = card.get("bdf")
            if (not isinstance(card.get("uuid"), str) or not card["uuid"] or
                    isinstance(card.get("nvidia_smi_index"), bool) or
                    not isinstance(card.get("nvidia_smi_index"), int) or
                    card["nvidia_smi_index"] < 0):
                raise ReportError("benchmark power card identity is malformed")
            original = card.get("original_limit_watts")
            if (not _finite_number(original, positive=True) or
                    not _number_equal(original,
                                      card.get("observed_limit_before_watts")) or
                    not _power_limit_equal(
                        card.get("observed_limit_after_enforce_watts"),
                        expected_limit) or
                    not _power_limit_equal(
                        card.get("observed_limit_post_benchmark_watts"),
                        expected_limit) or
                    not _power_limit_equal(
                        card.get("observed_limit_after_restore_watts"), original)):
                raise ReportError("benchmark power limit lifecycle mismatch")
            if (card.get("limit_enforced_during_benchmark") is not True or
                    card.get("restore_attempted") is not True or
                    card.get("restore_succeeded") is not True or
                    card.get("error") is not None):
                raise ReportError("benchmark power limit was not proved/restored")
            count = card.get("sample_count")
            failures = card.get("sample_failures")
            samples = card.get("samples")
            if (isinstance(count, bool) or not isinstance(count, int) or count < 1 or
                    isinstance(failures, bool) or not isinstance(failures, int) or
                    failures != 0 or not isinstance(samples, list) or
                    len(samples) != count):
                raise ReportError("benchmark power sample counters are invalid")
            draws = []
            offsets = []
            for sample in samples:
                if (not isinstance(sample, dict) or set(sample) != {
                        "offset_seconds", "power_draw_watts",
                        "observed_limit_watts", "query_duration_seconds"}):
                    raise ReportError("benchmark power sample shape mismatch")
                if (not _finite_number(sample.get("offset_seconds"), minimum=0) or
                        not _finite_number(sample.get("power_draw_watts"), minimum=0) or
                        not _power_limit_equal(sample.get("observed_limit_watts"),
                                               expected_limit) or
                        not _finite_number(sample.get("query_duration_seconds"),
                                           minimum=0)):
                    raise ReportError("benchmark power sample value is invalid")
                offsets.append(float(sample["offset_seconds"]))
                draws.append(float(sample["power_draw_watts"]))
            if offsets != sorted(offsets):
                raise ReportError("benchmark power sample offsets are not monotonic")
            minimum = min(draws)
            maximum = max(draws)
            average = sum(draws) / len(draws)
            span = offsets[-1] - offsets[0]
            if (not _number_equal(card.get("power_draw_w_min"), minimum) or
                    not _number_equal(card.get("power_draw_w_max"), maximum) or
                    not _number_equal(card.get("power_draw_w_avg"), average) or
                    card.get("power_draw_average_method") != POWER_AVERAGE_METHOD or
                    not _number_equal(card.get("sampling_span_seconds"), span)):
                raise ReportError("benchmark power aggregates do not match samples")
            for command_key in (
                    "pre_query_command", "set_command",
                    "enforce_verification_query_command",
                    "post_benchmark_query_command", "restore_command",
                    "restore_verification_query_command"):
                if not isinstance(card.get(command_key), dict):
                    raise ReportError("benchmark power command provenance is absent")
            rows.append({
                "family": family,
                "scope": scope["component_path"],
                "bdf": bdf,
                "used_limit_watts": float(expected_limit),
                "original_limit_watts": float(original),
                "restored_limit_watts": float(
                    card["observed_limit_after_restore_watts"]
                ),
                "minimum_watts": minimum,
                "maximum_watts": maximum,
                "average_watts": average,
                "average_method": POWER_AVERAGE_METHOD,
                "valid_sample_count": count,
                "failed_sample_count": failures,
                "interval_seconds": interval_ms / 1000.0,
                "sampling_span_seconds": span,
                "provenance": "%s %s" % (
                    provenance["executable"],
                    provenance["executable_sha256"],
                ),
            })
    if summary.get("executed_families") != sorted(executed_families):
        raise ReportError("diagnostics executed-family mirror mismatch")
    expected_targets = {
        family: sorted(values)
        for family, values in sorted(executed_targets.items())
    }
    if summary.get("executed_target_bdfs") != expected_targets:
        raise ReportError("diagnostics executed-target mirror mismatch")
    return rows


def telemetry_power_summary(path):
    """Strictly summarize ArchBench whole-worker NVML telemetry.

    The mean is sample-weighted, not kernel energy and not a guaranteed
    instantaneous peak. Per-case groups preserve the sampler's case labels.
    """
    path = Path(path)
    if not path.is_file():
        raise ReportError("telemetry file is absent: %s" % path)
    groups = {"__all__": []}
    monotonic = []
    failed_samples = 0
    lines = 0
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            lines += 1

            def reject(value):
                raise ReportError("non-finite telemetry value: %s" % value)

            try:
                sample = json.loads(
                    line, parse_constant=reject,
                    object_pairs_hook=_strict_object(
                        "%s line %d" % (path, line_number)
                    ),
                )
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ReportError(
                    "invalid telemetry JSON at %s line %d: %s" %
                    (path, line_number, exc)
                ) from exc
            if not isinstance(sample, dict):
                raise ReportError(
                    "telemetry sample is not an object at %s line %d" %
                    (path, line_number)
                )
            power_mw = sample.get("power_mw")
            if (isinstance(power_mw, bool) or
                    not isinstance(power_mw, int) or power_mw < 0):
                failed_samples += 1
            else:
                watts = power_mw / 1000.0
                groups["__all__"].append(watts)
                case_id = sample.get("case_id")
                if not isinstance(case_id, str) or not case_id:
                    case_id = "<missing-case-id>"
                groups.setdefault(case_id, []).append(watts)
            stamp = sample.get("monotonic_ns")
            if (isinstance(stamp, int) and not isinstance(stamp, bool)
                    and stamp >= 0):
                monotonic.append(stamp)
    if not lines:
        raise ReportError("telemetry contains no samples: %s" % path)
    if not groups["__all__"]:
        raise ReportError("telemetry contains no valid power samples: %s" % path)

    def statistics(values):
        return {
            "minimum_watts": min(values),
            "maximum_watts": max(values),
            "average_watts": sum(values) / len(values),
            "valid_sample_count": len(values),
            "average_method": "sample-weighted arithmetic mean",
        }

    deltas = [
        (right - left) / 1_000_000_000.0
        for left, right in zip(monotonic, monotonic[1:])
        if right > left
    ]
    return {
        "scope": "whole ArchBench worker including setup, validation and timing",
        "statistics": statistics(groups.pop("__all__")),
        "failed_sample_count": failed_samples,
        "line_count": lines,
        "observed_interval_seconds": ({
            "minimum": min(deltas), "maximum": max(deltas),
            "average": sum(deltas) / len(deltas), "count": len(deltas),
        } if deltas else None),
        "per_case": {
            name: statistics(values) for name, values in sorted(groups.items())
        },
        "provenance": {
            "source": "ArchBench NvmlSampler telemetry.jsonl",
            "path": path.name,
            "sha256": sha256_file(path),
            "nvml_measurement": "nvmlDeviceGetPowerUsage",
        },
        "caveat": (
            "sampled whole-worker power is not kernel energy and may miss "
            "short-lived instantaneous peaks"
        ),
    }


def _strict_jsonl(path, label):
    rows = []
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReportError("ArchBench %s is absent or not regular" % label)
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue

            def reject(value):
                raise ReportError(
                    "non-finite ArchBench %s value at line %d: %s" %
                    (label, line_number, value)
                )

            try:
                value = json.loads(
                    line, parse_constant=reject,
                    object_pairs_hook=_strict_object(
                        "%s line %d" % (path, line_number)
                    ),
                )
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ReportError(
                    "invalid ArchBench %s JSON at line %d: %s" %
                    (label, line_number, exc)
                ) from exc
            if not isinstance(value, dict):
                raise ReportError(
                    "ArchBench %s line %d is not an object" %
                    (label, line_number)
                )
            rows.append(value)
    if not rows:
        raise ReportError("ArchBench %s contains no objects" % label)
    return rows


def _archbench_uuid(value, label):
    if not isinstance(value, str):
        raise ReportError("ArchBench %s UUID is absent" % label)
    normalized = value.removeprefix("GPU-").replace("-", "").lower()
    if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
        raise ReportError("ArchBench %s UUID is malformed" % label)
    return normalized


def _archbench_case(value, label):
    required = {"id", "family", "params", "provider", "tags"}
    if not isinstance(value, dict) or set(value) != required:
        raise ReportError("ArchBench %s case shape mismatch" % label)
    family = value.get("family")
    params = value.get("params")
    provider = value.get("provider")
    tags = value.get("tags")
    if (not isinstance(family, str) or
            re.fullmatch(r"[a-z][a-z0-9_]*", family) is None or
            not isinstance(params, dict) or provider != "core" or
            not isinstance(tags, list) or
            any(not isinstance(tag, str) for tag in tags)):
        raise ReportError("ArchBench %s case content is invalid" % label)
    for key, item_value in params.items():
        if (not isinstance(key, str) or
                re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", key) is None or
                type(item_value) not in (str, bool, int, float) or
                (type(item_value) is float and
                 not math.isfinite(item_value))):
            raise ReportError("ArchBench %s case params are invalid" % label)
    canonical = json.dumps(
        {"family": family, "params": params, "provider": provider},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    expected_id = "%s.%s" % (
        family, hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    )
    if value.get("id") != expected_id:
        raise ReportError("ArchBench %s case ID/content mismatch" % label)
    return expected_id


def _archbench_count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError("ArchBench %s count is invalid" % label)
    return value


def _archbench_count_map(value, label):
    if not isinstance(value, dict) or any(
            not isinstance(key, str) or
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for key, count in value.items()):
        raise ReportError("ArchBench %s count map is invalid" % label)
    return dict(value)


def validate_archbench_run(directory, expected_bdf, expected_uuid,
                           expected_name, expected_binary, expected_matrix,
                           expected_timeout):
    """Independently validate one exact 235-case ArchBench acceptance run."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ReportError("ArchBench run directory is absent or not regular")
    required = {
        "run": directory / "run.json",
        "summary": directory / "summary.json",
        "coverage": directory / "coverage.json",
        "plan": directory / "plan.jsonl",
        "results": directory / "results.jsonl",
    }
    for label, path in required.items():
        if path.is_symlink() or not path.is_file():
            raise ReportError("ArchBench %s artifact is absent" % label)
    run = strict_json(required["run"])
    summary = strict_json(required["summary"])
    coverage = strict_json(required["coverage"])
    if not all(isinstance(value, dict) for value in (run, summary, coverage)):
        raise ReportError("ArchBench JSON artifact root is not an object")
    plan = _strict_jsonl(required["plan"], "plan")
    results = _strict_jsonl(required["results"], "results")

    if run.get("schema_version") != 1:
        raise ReportError("ArchBench run schema mismatch")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ReportError("ArchBench run ID is absent")
    if run.get("measurement_preset") != "stable":
        raise ReportError("ArchBench measurement preset is not stable")
    selected = _archbench_count(run.get("selected_cases"), "run selected")
    if selected != ARCHBENCH_SELECTED_CASES:
        raise ReportError(
            "ArchBench selected count is %d, expected %d" %
            (selected, ARCHBENCH_SELECTED_CASES)
        )

    expected_binary = Path(expected_binary).resolve()
    expected_matrix = Path(expected_matrix).resolve()
    if (not expected_binary.is_file() or expected_binary.is_symlink() or
            not expected_matrix.is_file() or expected_matrix.is_symlink()):
        raise ReportError("ArchBench requested binary/matrix is not regular")
    identity = run.get("identity")
    device = run.get("device")
    selection_source = run.get("selection_source")
    if not all(isinstance(value, dict)
               for value in (identity, device, selection_source)):
        raise ReportError("ArchBench run identity is incomplete")
    if Path(str(run.get("binary", ""))).resolve() != expected_binary:
        raise ReportError("ArchBench binary path identity mismatch")
    binary_sha256 = sha256_file(expected_binary)
    if identity.get("binary_sha256") != binary_sha256:
        raise ReportError("ArchBench binary hash identity mismatch")
    matrix_sha256 = sha256_file(expected_matrix)
    if (selection_source.get("kind") != "json_matrix" or
            Path(str(selection_source.get("path", ""))).resolve() !=
            expected_matrix or
            selection_source.get("sha256") != matrix_sha256 or
            selection_source.get("filters") != {} or
            selection_source.get("software_pairs") is not True or
            selection_source.get("shard_basis") != "operation_id"):
        raise ReportError("ArchBench matrix/selection-source identity mismatch")

    wanted_uuid = _archbench_uuid(expected_uuid, "requested")
    if (_archbench_uuid(run.get("requested_gpu_uuid"), "CLI requested") !=
            wanted_uuid or
            _archbench_uuid(identity.get("gpu_uuid"), "identity") !=
            wanted_uuid or
            _archbench_uuid(device.get("uuid_hex"), "device") != wanted_uuid):
        raise ReportError("ArchBench GPU UUID identity mismatch")
    if device.get("device_name") != expected_name:
        raise ReportError("ArchBench GPU name identity mismatch")
    if not BDF_RE.fullmatch(expected_bdf):
        raise ReportError("ArchBench expected BDF is invalid")
    domain, bus, slot_function = expected_bdf.split(":")
    slot, function = slot_function.split(".")
    try:
        observed_bdf = (
            int(str(device.get("pci_domain")), 10),
            int(str(device.get("pci_bus")), 10),
            int(str(device.get("pci_device")), 10),
        )
    except (TypeError, ValueError) as exc:
        raise ReportError("ArchBench device PCI identity is malformed") from exc
    expected_bdf_tuple = (int(domain, 16), int(bus, 16), int(slot, 16))
    if observed_bdf != expected_bdf_tuple or int(function, 10) != 0:
        raise ReportError("ArchBench device PCI identity mismatch")
    if device.get("binary_target") != "sm_80":
        raise ReportError("ArchBench worker target is not sm_80")

    policy = identity.get("policy")
    if not isinstance(policy, dict) or set(policy) != (
            set(ARCHBENCH_STABLE_POLICY) | {"device"}):
        raise ReportError("ArchBench stable policy shape mismatch")
    for key, expected_value in ARCHBENCH_STABLE_POLICY.items():
        observed = policy.get(key)
        if (isinstance(expected_value, float) and
                not _number_equal(observed, expected_value)):
            raise ReportError("ArchBench stable policy mismatch: %s" % key)
        if not isinstance(expected_value, float) and observed != expected_value:
            raise ReportError("ArchBench stable policy mismatch: %s" % key)
    if (isinstance(policy.get("device"), bool) or
            not isinstance(policy.get("device"), int) or
            policy["device"] < 0 or
            not _number_equal(identity.get("timeout_seconds"),
                              expected_timeout)):
        raise ReportError("ArchBench device/timeout policy mismatch")

    plan_by_id = {}
    plan_families = Counter()
    for index, case in enumerate(plan):
        case_id = _archbench_case(case, "plan[%d]" % index)
        if case_id in plan_by_id:
            raise ReportError("ArchBench plan contains duplicate case ID")
        plan_by_id[case_id] = case
        plan_families[case["family"]] += 1
    if len(plan_by_id) != selected:
        raise ReportError("ArchBench plan count differs from selected count")
    if dict(sorted(plan_families.items())) != ARCHBENCH_EXPECTED_FAMILIES:
        raise ReportError("ArchBench acceptance family/count matrix mismatch")
    selection_sha256 = hashlib.sha256(
        "\n".join(case["id"] for case in plan).encode("utf-8")
    ).hexdigest()
    if identity.get("selection_sha256") != selection_sha256:
        raise ReportError("ArchBench plan selection fingerprint mismatch")

    software = identity.get("software")
    if not isinstance(software, dict):
        raise ReportError("ArchBench software identity is absent")
    source_sha256 = device.get("source_sha256")
    build_sha256 = device.get("build_flags_sha256")
    if (not isinstance(source_sha256, str) or
            re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or
            not isinstance(build_sha256, str) or
            re.fullmatch(r"[0-9a-f]{64}", build_sha256) is None or
            software.get("source_sha256") != source_sha256 or
            software.get("build_flags_sha256") != build_sha256):
        raise ReportError("ArchBench source/build identity mismatch")

    result_by_id = {}
    statuses = Counter()
    family_outcomes = {}
    validated_measured = 0
    with_positive_samples = 0
    for index, row in enumerate(results):
        case = row.get("case")
        case_id = _archbench_case(case, "result[%d]" % index)
        if case_id in result_by_id:
            raise ReportError("ArchBench results contain duplicate case ID")
        if case_id not in plan_by_id or case != plan_by_id[case_id]:
            raise ReportError("ArchBench result differs from its plan")
        if row.get("run_id") != run_id:
            raise ReportError("ArchBench result run ID mismatch")
        status = row.get("status")
        if status not in ARCHBENCH_TERMINAL_STATUSES:
            raise ReportError("ArchBench result status is invalid")
        samples = row.get("samples_ms")
        if (not isinstance(samples, list) or any(
                type(value) not in (int, float) or
                not math.isfinite(float(value)) or float(value) <= 0
                for value in samples)):
            raise ReportError("ArchBench result samples are invalid")
        validation = row.get("validation")
        is_validated = (
            status == "ok" and validation in ARCHBENCH_VALIDATED and
            bool(samples)
        )
        if status == "ok" and not is_validated:
            raise ReportError("ArchBench ok result is not validated/measured")
        if (status == "measurement_only" and
                (validation != "not_applicable" or not samples)):
            raise ReportError("ArchBench measurement_only result is invalid")
        metadata = row.get("metadata")
        if (not isinstance(metadata, dict) or
                _archbench_uuid(metadata.get("uuid_hex"), "result") !=
                wanted_uuid or
                metadata.get("source_sha256") != source_sha256 or
                metadata.get("build_flags_sha256") != build_sha256):
            raise ReportError("ArchBench result device/source identity mismatch")
        result_by_id[case_id] = row
        statuses[status] += 1
        family = case["family"]
        family_outcomes.setdefault(family, Counter())[status] += 1
        if samples:
            with_positive_samples += 1
        if is_validated:
            validated_measured += 1
            family_outcomes[family]["validated_measured"] += 1
    if set(result_by_id) != set(plan_by_id):
        raise ReportError("ArchBench selected/recorded case IDs differ")
    fatal = sorted(set(statuses) - ARCHBENCH_NONFATAL_STATUSES)
    if fatal:
        raise ReportError(
            "ArchBench run contains fatal outcome statuses: %s" %
            ", ".join(fatal)
        )

    status_counts = dict(sorted(statuses.items()))
    family_counts = {
        family: dict(sorted(counts.items()))
        for family, counts in sorted(family_outcomes.items())
    }
    if coverage.get("run") != run:
        raise ReportError("ArchBench coverage/run identity mirror mismatch")
    for label, document in (("summary", summary), ("coverage", coverage)):
        if (_archbench_count(document.get("selected"), label + " selected") !=
                selected or
                _archbench_count(document.get("recorded"), label + " recorded") !=
                len(result_by_id) or
                _archbench_count(document.get("missing"), label + " missing") !=
                0 or
                _archbench_count(document.get("validated_measured"),
                                 label + " validated") != validated_measured):
            raise ReportError("ArchBench %s aggregate mismatch" % label)
    if summary.get("run_id") != run_id or summary.get("stopped") is not False:
        raise ReportError("ArchBench summary completion identity mismatch")
    if (_archbench_count_map(summary.get("status_counts"),
                             "summary statuses") != status_counts or
            _archbench_count_map(coverage.get("statuses"),
                                 "coverage statuses") != status_counts or
            coverage.get("families") != family_counts):
        raise ReportError("ArchBench status/family coverage mismatch")
    for key in ("implementation_counts", "measurement_quality_counts"):
        counts = _archbench_count_map(coverage.get(key), "coverage " + key)
        if sum(counts.values()) != selected:
            raise ReportError("ArchBench %s does not cover every case" % key)

    return {
        "schema": "170hx-xray-archbench-acceptance-validation-v1",
        "validated": True,
        "selected": selected,
        "recorded": len(result_by_id),
        "missing": 0,
        "all_selected_recorded": True,
        "all_selected_numerically_validated_measured": (
            validated_measured == selected
        ),
        "numerically_validated_measured": validated_measured,
        "records_with_positive_samples": with_positive_samples,
        "status_counts": status_counts,
        "selected_family_counts": dict(sorted(plan_families.items())),
        "family_outcomes": family_counts,
        "implementation_counts": coverage["implementation_counts"],
        "measurement_quality_counts": coverage["measurement_quality_counts"],
        "pairing_counts": coverage.get("pairing_counts"),
        "pairing_unique_operation_counts": coverage.get(
            "pairing_unique_operation_counts"
        ),
        "identity": {
            "bdf": expected_bdf, "uuid": expected_uuid,
            "name": expected_name, "run_id": run_id,
            "binary": str(expected_binary),
            "binary_sha256": binary_sha256,
            "matrix": str(expected_matrix),
            "matrix_sha256": matrix_sha256,
            "selection_sha256": selection_sha256,
            "source_sha256": source_sha256,
            "build_flags_sha256": build_sha256,
            "measurement_preset": "stable",
            "timeout_seconds": float(expected_timeout),
        },
        "artifact_sha256": {
            label: sha256_file(path) for label, path in required.items()
        },
        "interpretation": (
            "selected and recorded are coverage facts; only "
            "numerically_validated_measured counts validated numerical "
            "measurements, while unsupported, memory-budget and "
            "measurement-only outcomes remain distinct"
        ),
    }


def artifact(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def command_text(argv):
    return " ".join(shlex.quote(str(value)) for value in argv)


class Runner(object):
    def __init__(self, cwd, timeout=60.0):
        self.cwd = Path(cwd)
        self.timeout = float(timeout)

    def run(self, argv, cwd=None, timeout=None):
        argv = [str(value) for value in argv]
        started = now_utc().isoformat()
        try:
            result = subprocess.run(
                argv, cwd=str(cwd or self.cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=float(timeout or self.timeout),
            )
            return {
                "argv": argv, "command": command_text(argv),
                "started_utc": started, "finished_utc": now_utc().isoformat(),
                "returncode": result.returncode,
                "stdout": result.stdout[:MAX_OUTPUT],
                "stderr": result.stderr[:MAX_OUTPUT],
                "truncated": (len(result.stdout) > MAX_OUTPUT or
                              len(result.stderr) > MAX_OUTPUT),
                "timed_out": False,
            }
        except FileNotFoundError as exc:
            return {
                "argv": argv, "command": command_text(argv),
                "started_utc": started, "finished_utc": now_utc().isoformat(),
                "returncode": None, "stdout": b"",
                "stderr": str(exc).encode("utf-8", "replace"),
                "truncated": False, "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "argv": argv, "command": command_text(argv),
                "started_utc": started, "finished_utc": now_utc().isoformat(),
                "returncode": None, "stdout": (exc.stdout or b"")[:MAX_OUTPUT],
                "stderr": (exc.stderr or b"")[:MAX_OUTPUT],
                "truncated": False, "timed_out": True,
            }


def save_command(result, directory, stem, root):
    out = write_new(directory / (stem + ".stdout.txt"), result["stdout"], True)
    err = write_new(directory / (stem + ".stderr.txt"), result["stderr"], True)
    meta = {key: value for key, value in result.items()
            if key not in ("stdout", "stderr")}
    meta["stdout"] = artifact(out, root)
    meta["stderr"] = artifact(err, root)
    meta_path = write_new(
        directory / (stem + ".command.json"),
        json.dumps(meta, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    return {
        "status": "completed" if result["returncode"] == 0 else "failed",
        "command": result["command"], "returncode": result["returncode"],
        "timed_out": result["timed_out"], "truncated": result["truncated"],
        "stdout": artifact(out, root), "stderr": artifact(err, root),
        "metadata": artifact(meta_path, root),
    }


def item(identifier, status, reason, **extra):
    if status not in COMPONENT_STATUSES:
        raise ReportError("invalid component status: %s" % status)
    value = {"id": identifier, "status": status, "reason": reason}
    value.update(extra)
    return value


def bdf_arg(value):
    value = str(value).lower()
    if not BDF_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid full PCI BDF: %s" % value)
    return value


def positive(value):
    try:
        value = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return value


def positive_integer(value):
    """Parse a positive integer while accepting canonical integral floats."""
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError("must be a positive integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("not a number") from exc
    if (not math.isfinite(number) or number <= 0 or
            not number.is_integer()):
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(number)


def discover_bdfs(sysfs_root):
    found = []
    root = Path(sysfs_root)
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir(), key=lambda value: value.name):
        if not BDF_RE.fullmatch(path.name.lower()):
            continue
        try:
            vendor = (path / "vendor").read_text(encoding="ascii").strip().lower()
            device = (path / "device").read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError):
            continue
        if vendor == "0x10de" and device.removeprefix("0x") in GA100_IDS:
            found.append(path.name.lower())
    return found


def append_bdfs(argv, bdfs):
    for bdf in bdfs:
        argv.extend(("--bdf", bdf))


def resolve_program(name):
    discovered = shutil.which(name)
    if discovered:
        return discovered
    for root in ("/usr/bin", "/usr/sbin", "/bin", "/sbin",
                 "/usr/local/bin", "/usr/local/sbin"):
        candidate = Path(root) / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    for candidate in sorted(Path("/usr/local").glob("cuda*/bin/%s" % name)):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return name


def host_commands(smi, sudo_read_inventory=False):
    # All shell fragments are fixed constants; no user text is interpolated.
    dmidecode = resolve_program("dmidecode")
    modinfo = resolve_program("modinfo")
    ldconfig = resolve_program("ldconfig")
    dmi = [dmidecode, "-t", "baseboard", "-t", "bios", "-t", "memory"]
    if sudo_read_inventory:
        dmi = [resolve_program("sudo"), "-n"] + dmi
    quoted_modinfo = shlex.quote(modinfo)
    return (
        ("uname", ["uname", "-a"]),
        ("os-release", ["sh", "-c", "test -r /etc/os-release && cat /etc/os-release"]),
        ("hostname-platform", ["hostnamectl"]),
        ("virtualization", [
            "sh", "-c",
            "systemd-detect-virt; xray_virt_status=$?; "
            "test $xray_virt_status -eq 1 && exit 0; exit $xray_virt_status",
        ]),
        ("cpu", ["lscpu", "-J"]),
        ("numa", ["numactl", "--hardware"]),
        ("memory", ["free", "-b"]),
        ("meminfo", ["sh", "-c", "cat /proc/meminfo"]),
        ("dmi", dmi),
        ("pci-full", ["lspci", "-Dnnvvxxxx"]),
        ("pci-tree", ["lspci", "-Dtv"]),
        ("block", ["lsblk", "-J", "-O", "-b"]),
        ("nvme", [resolve_program("nvme"), "list", "-v", "-o", "json"]),
        ("mounts", ["findmnt", "-J"]),
        ("kernel-command-line", ["sh", "-c", "cat /proc/cmdline"]),
        ("loaded-modules", ["lsmod"]),
        ("kernel-taint", ["sh", "-c", "cat /proc/sys/kernel/tainted"]),
        ("secure-boot", ["mokutil", "--sb-state"]),
        ("dkms", [resolve_program("dkms"), "status"]),
        ("nvidia-module", [modinfo, "nvidia"]),
        ("nvidia-module-hash", [
            "sh", "-c",
            "xray_module_path=$(%s -n nvidia 2>/dev/null) && " % quoted_modinfo +
            "test -f \"$xray_module_path\" && sha256sum \"$xray_module_path\"",
        ]),
        ("nvidia-driver", ["sh", "-c", "cat /proc/driver/nvidia/version"]),
        ("nvidia-params", ["sh", "-c", "cat /proc/driver/nvidia/params"]),
        ("nvidia-proc-gpus", [
            "sh", "-c",
            "for xray_gpu_info in /proc/driver/nvidia/gpus/*/information; do "
            "test -r \"$xray_gpu_info\" && cat \"$xray_gpu_info\"; done",
        ]),
        ("nvidia-packages-deb", [
            "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n",
            "nvidia*", "cuda*", "libcuda*",
        ]),
        ("nvidia-packages-rpm", ["rpm", "-qa", "nvidia*", "cuda*"]),
        ("cuda", [resolve_program("nvcc"), "--version"]),
        ("cuda-version-json", [
            "sh", "-c",
            "for xray_cuda_json in /usr/local/cuda/version.json "
            "/usr/local/cuda-*/version.json; do "
            "test -r \"$xray_cuda_json\" && cat \"$xray_cuda_json\"; done",
        ]),
        ("cuda-driver-tool", [smi, "--query-gpu=pci.bus_id,driver_version,name,uuid,vbios_version,inforom.img,pstate,clocks.current.graphics,clocks.current.sm,clocks.current.memory,power.limit,memory.total,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max", "--format=csv"]),
        ("libraries", [ldconfig, "-p"]),
        ("nvidia-smi", [smi]),
        ("nvidia-smi-q", [smi, "-q"]),
        ("nvidia-smi-xml", [smi, "-q", "-x"]),
        ("nvidia-topology", [smi, "topo", "-m"]),
        ("nvidia-p2p-read", [smi, "topo", "-p2p", "r"]),
        ("nvidia-p2p-write", [smi, "topo", "-p2p", "w"]),
        ("nvidia-p2p-atomics", [smi, "topo", "-p2p", "a"]),
        ("nvidia-nvlink", [smi, "nvlink", "-s"]),
        ("nvidia-mig-gpu", [smi, "mig", "-lgip"]),
        ("nvidia-mig-compute", [smi, "mig", "-lgci"]),
    )


def power_query(runner, smi, bdfs):
    result = runner.run([
        smi, "--query-gpu=pci.bus_id,power.limit,power.min_limit,power.max_limit",
        "--format=csv,noheader,nounits",
    ])
    values = {}
    if result["returncode"] == 0:
        for line in result["stdout"].decode("utf-8", "replace").splitlines():
            fields = [part.strip() for part in line.split(",")]
            if len(fields) != 4:
                continue
            bdf = fields[0].lower().removeprefix("0000")
            if bdf.startswith(":"):
                bdf = "0000" + bdf
            try:
                values[bdf] = {
                    "limit_watts": float(fields[1]),
                    "minimum_watts": float(fields[2]),
                    "maximum_watts": float(fields[3]),
                }
            except ValueError:
                values[bdf] = {"raw": fields[1:]}
    return result, {bdf: values.get(bdf) for bdf in bdfs}


def change_limits(runner, smi, targets):
    records = []
    for bdf, watts in targets:
        result = runner.run([smi, "-i", bdf, "-pl", str(watts)])
        records.append({
            "bdf": bdf, "watts": watts, "returncode": result["returncode"],
            "stderr": result["stderr"].decode("utf-8", "replace"),
        })
    return records


def complete_row_export_arguments(row_view):
    """Return the explicit, non-authorizing complete exporter request."""
    return [
        "--fuseless-start", str(FULL_REPORT_FUSELESS_START),
        "--fuseless-end", str(FULL_REPORT_FUSELESS_END),
        "--bounds-provenance", FULL_REPORT_BOUNDS_PROVENANCE,
        "--row-view", row_view,
        "--provenance", FULL_REPORT_EXPORT_PROVENANCE,
        "--include-unsafe-projections",
    ]


def _strict_json_payload(payload, label):
    def reject(value):
        raise ReportError("non-finite JSON in %s: %s" % (label, value))
    try:
        value = json.loads(
            payload, parse_constant=reject,
            object_pairs_hook=_strict_object(label),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReportError("invalid JSON in %s: %s" % (label, exc)) from exc
    if not isinstance(value, dict):
        raise ReportError("candidate transport is not an object: %s" % label)
    return value


def candidate_transport_document(path, output_format):
    """Independently decode one lossless candidate-plan transport."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8" if output_format == "text" else "ascii")
    except UnicodeError as exc:
        raise ReportError(
            "candidate %s transport is not valid text: %s" %
            (output_format, exc)
        ) from exc
    if output_format == "text":
        begin_marker = CANDIDATE_TEXT_BEGIN + "\n"
        end_marker = "\n" + CANDIDATE_TEXT_END
        begin = text.find(begin_marker)
        end = text.rfind(end_marker)
        if begin < 0 or end <= begin:
            raise ReportError("candidate text transport lacks canonical markers")
        text = text[begin + len(begin_marker):end]
    elif output_format == "jsonl":
        lines = text.splitlines()
        if len(lines) != 1:
            raise ReportError("candidate JSONL transport must contain one record")
        text = lines[0]
    elif output_format not in ("json", "canonical-json-bin"):
        raise ReportError("unknown candidate transport: %s" % output_format)
    return _strict_json_payload(text, "candidate-%s" % output_format)


def full_dump_product_coverage(index, requested_access_scope="ordinary"):
    """Prove complete products for eligible specimens in one access scope."""
    if requested_access_scope not in ("ordinary", "glitch-read", "all"):
        raise ReportError(
            "unknown full-dump coverage scope: %s" % requested_access_scope
        )
    issues = []
    request = index.get("request") if isinstance(index, dict) else None
    if not isinstance(request, dict):
        request = {}
        issues.append("request metadata is absent")
    if request.get("include_unsafe_projections") is not True:
        issues.append("public-shaped projection acknowledgement is absent")
    if request.get("fuseless_bounds") != [
            FULL_REPORT_FUSELESS_START, FULL_REPORT_FUSELESS_END]:
        issues.append("empirical S/E exporter bounds differ from 222..511")
    if request.get("planning_models") != list(CHECKLIST_PLANNING_MODELS):
        issues.append("checklist planning-model request is incomplete")

    artifacts = index.get("artifacts") if isinstance(index, dict) else None
    artifact_by_path = {
        value.get("path"): value for value in artifacts or []
        if isinstance(value, dict) and isinstance(value.get("path"), str)
    }
    observations = (
        index.get("observations", []) if isinstance(index, dict) else []
    )
    if not isinstance(observations, list):
        observations = []
        issues.append("observation inventory is not a list")

    def in_scope(observation):
        access = observation.get("requested_access")
        if requested_access_scope == "all":
            return True
        if requested_access_scope == "glitch-read":
            return access == "glitch-read"
        return access != "glitch-read"

    scoped_observations = {
        value.get("id"): value for value in observations
        if isinstance(value, dict) and in_scope(value)
        and isinstance(value.get("id"), str)
    }
    observation_transport_sets = 0
    for observation in scoped_observations.values():
        if observation.get("status") != "complete":
            continue
        transports = observation.get("transports")
        valid = isinstance(transports, dict)
        if valid:
            for name, kind in OBSERVATION_TRANSPORT_KINDS.items():
                path = transports.get(name)
                if (not isinstance(path, str) or
                        artifact_by_path.get(path, {}).get("kind") != kind):
                    valid = False
                    break
        if not valid:
            issues.append(
                "observation %s lacks all four capture transports" %
                observation.get("id", "unknown")
            )
        else:
            observation_transport_sets += 1

    all_specimens = index.get("specimens") if isinstance(index, dict) else None
    if not isinstance(all_specimens, list):
        all_specimens = []
        issues.append("specimen inventory is not a list")
    specimens = []
    for specimen in all_specimens:
        if not isinstance(specimen, dict):
            issues.append("malformed specimen record")
            continue
        observation_id = specimen.get("observation")
        if requested_access_scope == "all":
            specimens.append(specimen)
        elif observation_id is None:
            if requested_access_scope == "ordinary":
                specimens.append(specimen)
        elif observation_id in scoped_observations:
            specimens.append(specimen)
    checklist_complete = 0
    complete_main_passes = 0
    eligible_specimens = 0
    complete_rir_sets = 0
    complete_register_sets = 0
    for specimen in specimens:
        specimen_id = specimen.get("id", "unknown")
        main = specimen.get("main")
        complete_passes = [
            value for value in main.get("passes", [])
            if isinstance(value, dict) and value.get("complete") is True
        ] if isinstance(main, dict) else []
        if not complete_passes:
            continue
        eligible_specimens += 1
        complete_main_passes += len(complete_passes)

        planning = specimen.get("planning")
        models = []
        for value in planning or []:
            if not isinstance(value, dict):
                continue
            model = value.get("model")
            transports = value.get("transports")
            valid = (
                value.get("status") == "generated_non_executable"
                and isinstance(transports, dict)
                and isinstance(value.get("apply_dry_run"), str)
                and isinstance(value.get("otp_readiness"), str)
            )
            expected_kinds = {
                "json": "plan-%s" % model,
                "jsonl": "plan-jsonl-%s" % model,
                "text": "plan-text-%s" % model,
                "binary_canonical_json": (
                    "plan-canonical-json-bin-%s" % model
                ),
            }
            if valid:
                for name, kind in expected_kinds.items():
                    path = transports.get(name)
                    if (not isinstance(path, str) or
                            artifact_by_path.get(path, {}).get("kind") != kind):
                        valid = False
                        break
            if valid and artifact_by_path.get(
                    value["apply_dry_run"], {}).get("kind") != (
                        "apply-dry-run-%s" % model):
                valid = False
            if valid and artifact_by_path.get(
                    value["otp_readiness"], {}).get("kind") != (
                        "otp-readiness-%s" % model):
                valid = False
            if valid:
                models.append(model)
        if models != list(CHECKLIST_PLANNING_MODELS):
            issues.append(
                "%s checklist planning products are incomplete" % specimen_id
            )
        else:
            checklist_complete += 1

        for main_pass in complete_passes:
            outputs = main_pass.get("outputs")
            exported = [
                value.get("format") for value in outputs or []
                if isinstance(value, dict) and value.get("status") == "exported"
                and isinstance(value.get("path"), str)
                and artifact_by_path.get(value["path"], {}).get("kind") ==
                "main-%s" % value.get("format")
            ]
            if exported != list(ROW_EXPORT_FORMATS):
                issues.append(
                    "%s MAIN pass %s lacks row exporters: %s" % (
                        specimen_id, main_pass.get("pass"),
                        ", ".join(sorted(
                            set(ROW_EXPORT_FORMATS) - set(exported)
                        )),
                    )
                )
        registers = specimen.get("register_exports")
        register_paths = (
            registers.get("formats", []) if isinstance(registers, dict) else []
        )
        register_kinds = [
            artifact_by_path.get(path, {}).get("kind")
            for path in register_paths if isinstance(path, str)
        ]
        if (not isinstance(registers, dict) or
                registers.get("available") is not True or
                register_kinds != list(REGISTER_EXPORT_KINDS)):
            issues.append("%s lacks all four register transports" % specimen_id)
        else:
            complete_register_sets += 1
        rir = specimen.get("rir")
        if not isinstance(rir, dict) or rir.get("available") is not True:
            issues.append("%s lacks an exact RIR export" % specimen_id)
        else:
            rir_complete = True
            for rir_pass in rir.get("passes", []):
                paths = rir_pass.get("outputs", []) if isinstance(rir_pass, dict) else []
                kinds = [
                    artifact_by_path.get(path, {}).get("kind") for path in paths
                ]
                if kinds != list(RIR_EXPORT_KINDS):
                    rir_complete = False
            if not rir.get("passes") or not rir_complete:
                issues.append("%s lacks all four RIR transports per pass" % specimen_id)
            else:
                complete_rir_sets += 1
    if complete_main_passes == 0:
        issues.append("no complete 512-row MAIN pass is available")
    return {
        "complete": not issues,
        "issues": issues,
        "expected_main_formats": list(ROW_EXPORT_FORMATS),
        "expected_checklist_models": list(CHECKLIST_PLANNING_MODELS),
        "requested_access_scope": requested_access_scope,
        "observation_transport_sets": observation_transport_sets,
        "specimens": len(specimens),
        "eligible_specimens": eligible_specimens,
        "specimens_with_complete_checklists": checklist_complete,
        "complete_main_passes": complete_main_passes,
        "complete_rir_sets": complete_rir_sets,
        "complete_register_sets": complete_register_sets,
        "empirical_fuseless_bounds": [
            FULL_REPORT_FUSELESS_START, FULL_REPORT_FUSELESS_END,
        ],
        "execution_authorized": False,
    }


def _full_dump_coverage_status(coverage):
    if coverage["complete"]:
        return "completed"
    if coverage["complete_main_passes"] == 0:
        return "skipped"
    return "failed"


def record_full_dump_coverage(index, ledger, source, scope="ordinary"):
    coverage = full_dump_product_coverage(index, scope)
    status = _full_dump_coverage_status(coverage)
    ledger.append(item(
        "read-plan-export-coverage",
        status,
        ("all checklist models and eligible row/RIR/register transports verified"
         if status == "completed" else
         "verified bundle has no eligible complete 512-row MAIN prerequisite"
         if status == "skipped" else
         "an eligible complete MAIN exists but required products are missing"),
        source=source, coverage=coverage, execution_authorized=False,
    ))
    return coverage


def record_skipped_full_dump_coverage(ledger, reason):
    ledger.append(item(
        "read-plan-export-coverage", "skipped", reason,
        expected_main_formats=list(ROW_EXPORT_FORMATS),
        expected_checklist_models=list(CHECKLIST_PLANNING_MODELS),
        execution_authorized=False,
    ))


def record_glitch_export_coverage(ledger, status, reason, coverage=None):
    ledger.append(item(
        "glitch-read-export-coverage", status, reason,
        coverage=coverage,
        expected_main_formats=list(ROW_EXPORT_FORMATS),
        execution_authorized=False,
    ))


def promote_semantic_capture(args, bundle, index, bdfs, report_root):
    """Bind one verified complete semantic observation to Layer 2 planning."""
    if args.physical_capture:
        return {"status": "explicit-input-retained", "promoted": False}
    candidates = [
        value for value in index.get("observations", [])
        if isinstance(value, dict)
        and value.get("requested_access") == "semantic-read"
        and value.get("status") == "complete"
        and isinstance(value.get("artifact"), str)
    ]
    if len(candidates) != 1:
        return {
            "status": "unavailable", "promoted": False,
            "reason": "verified bundle does not contain exactly one complete semantic observation",
        }
    candidate = (Path(bundle) / candidates[0]["artifact"]).resolve()
    try:
        candidate.relative_to(Path(bundle).resolve())
    except ValueError as exc:
        raise ReportError("semantic capture escaped verified bundle") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ReportError("semantic capture is absent or non-regular")
    document = strict_json(candidate)
    cards = document.get("cards")
    observed = []
    for card in cards if isinstance(cards, list) else []:
        identity = card.get("card") if isinstance(card, dict) else None
        bdf = identity.get("domain_bdf") if isinstance(identity, dict) else None
        if isinstance(bdf, str):
            observed.append(bdf.lower())
    if len(observed) != len(set(observed)) or set(observed) != set(bdfs):
        raise ReportError("semantic capture BDF set differs from report selection")
    args.physical_capture = [str(candidate)]
    return {
        "status": "verified-live-semantic-promoted", "promoted": True,
        "artifact": artifact(candidate, report_root),
    }


def record_planning_layer_coverage(ledger):
    by_id = {value.get("id"): value for value in ledger if isinstance(value, dict)}
    layers = [
        ("goal-checklist", by_id.get("read-plan-export-coverage")),
        ("candidate", by_id.get("exact-current-permanent-plans")),
        ("sealed-append-compiler", by_id.get(
            "guarded-experimental-write-manifests"
        )),
    ]
    details = []
    for name, value in layers:
        details.append({
            "layer": name,
            "status": value.get("status") if isinstance(value, dict) else "missing",
            "component": value.get("id") if isinstance(value, dict) else None,
        })
    statuses = [value["status"] for value in details]
    if statuses == ["completed", "completed", "completed"]:
        status = "completed"
        reason = "all three planning layers completed and remain non-authorizing"
    elif "failed" in statuses or "missing" in statuses:
        status = "failed"
        reason = "one or more required planning layers failed or were not recorded"
    else:
        status = "skipped"
        reason = "one or more planning layers lacked an exact-current prerequisite"
    ledger.append(item(
        "three-planning-layer-coverage", status, reason,
        layers=details, execution_authorized=False,
    ))


def run_glitch_dump(args, project, output, runner, bdfs, ledger):
    if not args.include_glitch_read:
        ledger.append(item(
            "glitch-read-dump-plan", "not-requested",
            "separate transient 2080-state branch was not requested",
            levels_requested=["glitch-read"], levels_completed=[],
            levels_not_requested={
                "glitch-read": "transient branch was not requested"
            },
        ))
        record_glitch_export_coverage(
            ledger, "not-requested", "transient branch was not requested"
        )
        return
    logs = create_dir(output / "glitch-command")
    glitch = output / "glitch-read-bundle"
    glitch_argv = [
        sys.executable, str(project / "170HX-XRAY.py"), "full-dump",
        str(glitch), "--level", "glitch-read", "--register-profile", "all-safe",
        "--controller-helper", args.controller_helper,
    ]
    glitch_argv.extend(complete_row_export_arguments("unknown"))
    append_bdfs(glitch_argv, bdfs)
    result = runner.run(glitch_argv, timeout=args.long_timeout)
    entry = item(
        "glitch-read-dump-plan",
        "completed" if result["returncode"] == 0 else "failed",
        "transient 2080-state captured" if result["returncode"] == 0
        else "no valid requested glitch-state specimen was captured",
        levels_requested=["glitch-read"],
        levels_completed=["glitch-read"] if result["returncode"] == 0 else [],
        command_evidence=save_command(result, logs, "glitch-read", output),
    )
    coverage = None
    if (glitch / "index.json").is_file() and (glitch / "SHA256SUMS").is_file():
        verify = runner.run([
            sys.executable, str(project / "170HX-XRAY.py"),
            "verify-full-dump", str(glitch), "--output", "-",
        ], timeout=args.long_timeout)
        entry["verification"] = save_command(
            verify, logs, "verify-glitch", output
        )
        if verify["returncode"] != 0:
            entry["status"] = "failed"
            entry["reason"] = "independent glitch bundle verification failed"
        else:
            coverage = full_dump_product_coverage(
                strict_json(glitch / "index.json"), "glitch-read"
            )
            entry["product_coverage"] = coverage
    elif result["returncode"] == 0:
        entry["status"] = "failed"
        entry["reason"] = "glitch full-dump returned success without index/manifest"
    ledger.append(entry)
    if coverage is not None:
        coverage_status = _full_dump_coverage_status(coverage)
        record_glitch_export_coverage(
            ledger,
            coverage_status,
            ("all eligible glitch-state row transports verified"
             if coverage_status == "completed" else
             "verified glitch-state bundle has no complete eligible MAIN prerequisite"
             if coverage_status == "skipped" else
             "an eligible glitch-state MAIN exists but required products are missing"),
            coverage,
        )
    else:
        record_glitch_export_coverage(
            ledger, "failed" if entry["status"] == "failed" else "skipped",
            "no independently verified glitch-state bundle is available",
        )


def run_imported_full_dump(args, project, output, runner, bdfs, ledger):
    requested_glitch = bool(getattr(args, "include_glitch_read", False))
    source = Path(args.read_bundle).resolve()
    logs = create_dir(output / "read-command")
    source_verify = runner.run([
        sys.executable, str(project / "170HX-XRAY.py"),
        "verify-full-dump", str(source), "--output", "-",
    ], timeout=args.long_timeout)
    entry = item(
        "ordinary-read-dump-plan",
        "completed" if source_verify["returncode"] == 0 else "failed",
        "verified full-dump imported as immutable report evidence"
        if source_verify["returncode"] == 0 else
        "external full-dump verification failed before import",
        levels_requested=list(args.requested_read_levels),
        levels_completed=[], levels_skipped={},
        imported_source=str(source),
        source_verification=save_command(
            source_verify, logs, "verify-import-source", output
        ),
    )
    if source_verify["returncode"] != 0:
        ledger.append(entry)
        record_skipped_full_dump_coverage(
            ledger, "imported bundle failed verification"
        )
        ledger.append(item(
            "glitch-read-dump-plan",
            "skipped" if requested_glitch else "not-requested",
            "imported bundle failed verification",
            levels_requested=["glitch-read"], levels_completed=[],
        ))
        record_glitch_export_coverage(
            ledger, "failed" if requested_glitch else "not-requested",
            "imported bundle failed verification",
        )
        return
    destination = copy_tree_new(source, output / "read-dump-plan-bundle")
    copied_verify = runner.run([
        sys.executable, str(project / "170HX-XRAY.py"),
        "verify-full-dump", str(destination), "--output", "-",
    ], timeout=args.long_timeout)
    entry["verification"] = save_command(
        copied_verify, logs, "verify-import-copy", output
    )
    if copied_verify["returncode"] != 0:
        entry["status"] = "failed"
        entry["reason"] = "relocated imported full-dump verification failed"
        ledger.append(entry)
        record_skipped_full_dump_coverage(
            ledger, "relocated imported bundle failed verification"
        )
        ledger.append(item(
            "glitch-read-dump-plan",
            "skipped" if requested_glitch else "not-requested",
            "relocated imported bundle failed verification",
            levels_requested=["glitch-read"], levels_completed=[],
        ))
        record_glitch_export_coverage(
            ledger, "failed" if requested_glitch else "not-requested",
            "relocated imported bundle failed verification",
        )
        return
    index = strict_json(destination / "index.json")
    if (index.get("command") != "full-dump" or
            not isinstance(index.get("completion"), dict) or
            index["completion"].get("status") not in ("COMPLETE", "PARTIAL") or
            index["completion"].get("otp_program_commands") != 0 or
            index["completion"].get("programming_writes_performed") != 0):
        entry["status"] = "failed"
        entry["reason"] = "imported full-dump completion/safety contract mismatch"
        ledger.append(entry)
        record_skipped_full_dump_coverage(
            ledger, "imported completion contract was rejected"
        )
        ledger.append(item(
            "glitch-read-dump-plan",
            "skipped" if requested_glitch else "not-requested",
            "imported completion contract was rejected",
            levels_requested=["glitch-read"], levels_completed=[],
        ))
        record_glitch_export_coverage(
            ledger, "failed" if requested_glitch else "not-requested",
            "imported completion contract was rejected",
        )
        return
    entry["imported_bundle_completion"] = dict(index["completion"])
    product_coverage = full_dump_product_coverage(index)
    entry["complete_product_set"] = product_coverage["complete"]
    entry["product_coverage"] = product_coverage
    if index["completion"]["status"] == "PARTIAL":
        entry["reason"] = (
            "verified capture levels imported; the source bundle explicitly "
            "marks unavailable derived formats/plans as PARTIAL"
        )
    observations = index.get("observations")
    if not isinstance(observations, list):
        observations = []
    complete_levels = []
    semantic_capture = None
    for observation in observations:
        if (not isinstance(observation, dict) or
                observation.get("status") != "complete"):
            continue
        level = observation.get("requested_access")
        if level in ("passive", "bar-read", "semantic-read", "glitch-read"):
            complete_levels.append(level)
        if level == "semantic-read" and isinstance(observation.get("artifact"), str):
            candidate = (destination / observation["artifact"]).resolve()
            try:
                candidate.relative_to(destination.resolve())
            except ValueError as exc:
                raise ReportError("semantic capture escaped imported bundle") from exc
            if not candidate.is_file() or candidate.is_symlink():
                raise ReportError("imported semantic capture is absent/non-regular")
            semantic_capture = candidate
    requested = list(args.requested_read_levels)
    entry["levels_completed"] = [
        level for level in requested if level in complete_levels
    ]
    entry["levels_skipped"] = {
        level: "verified imported bundle lacks a complete observation"
        for level in requested if level not in complete_levels
    }
    entry["bundle_index"] = artifact(destination / "index.json", output)
    entry["bundle_manifest"] = artifact(destination / "SHA256SUMS", output)
    if semantic_capture is not None:
        capture_document = strict_json(semantic_capture)
        capture_cards = capture_document.get("cards")
        capture_bdfs = []
        if isinstance(capture_cards, list):
            for card in capture_cards:
                identity = card.get("card") if isinstance(card, dict) else None
                bdf = identity.get("domain_bdf") if isinstance(identity, dict) else None
                if isinstance(bdf, str):
                    capture_bdfs.append(bdf.lower())
        if (len(capture_bdfs) != len(set(capture_bdfs)) or
                set(capture_bdfs) != set(bdfs)):
            raise ReportError("imported semantic-capture BDF set mismatch")
        entry["candidate_capture_generated_utc"] = capture_document.get(
            "generated_utc"
        )
        try:
            capture_age = evidence_age_hours(capture_document.get("generated_utc"))
            entry["candidate_capture_age_hours"] = capture_age
            entry["candidate_max_age_hours"] = args.candidate_max_age_hours
            fresh = capture_age <= args.candidate_max_age_hours
        except ReportError as exc:
            entry["candidate_capture_age_error"] = str(exc)
            fresh = False
        if fresh:
            entry["candidate_input_status"] = "fresh-same-capture-evidence"
        elif args.allow_historical_candidate:
            entry["candidate_input_status"] = "historical-explicitly-allowed"
        else:
            entry["candidate_input_status"] = "historical-or-undated-not-promoted"
        if (not args.physical_capture and
                (fresh or args.allow_historical_candidate)):
            args.physical_capture = [str(semantic_capture)]
            entry["candidate_input"] = artifact(semantic_capture, output)
    ledger.append(entry)
    record_full_dump_coverage(index, ledger, "verified-import")
    if "glitch-read" in complete_levels:
        ledger.append(item(
            "glitch-read-dump-plan", "completed",
            "verified imported bundle contains a complete glitch-read observation",
            levels_requested=["glitch-read"], levels_completed=["glitch-read"],
        ))
    else:
        ledger.append(item(
            "glitch-read-dump-plan",
            "skipped" if requested_glitch else "not-requested",
            ("requested imported bundle contains no complete glitch-read observation"
             if requested_glitch else
             "glitch-read was not requested and the imported bundle contains none"),
            levels_requested=["glitch-read"], levels_completed=[],
            levels_skipped=({
                "glitch-read": "verified import contains no complete observation"
            } if requested_glitch else {}),
            levels_not_requested=({} if requested_glitch else {
                "glitch-read": "transient branch was not requested"
            }),
        ))
    if "glitch-read" in complete_levels:
        glitch_coverage = full_dump_product_coverage(index, "glitch-read")
        glitch_status = _full_dump_coverage_status(glitch_coverage)
        record_glitch_export_coverage(
            ledger, glitch_status,
            ("verified imported glitch-state products are complete"
             if glitch_status == "completed" else
             "verified imported glitch-state capture has no complete MAIN prerequisite"
             if glitch_status == "skipped" else
             "an eligible imported glitch-state MAIN exists but required products are missing"),
            glitch_coverage,
        )
    else:
        record_glitch_export_coverage(
            ledger, "skipped" if requested_glitch else "not-requested",
            "imported bundle has no complete glitch-state observation",
        )


def run_full_dump(args, project, output, runner, bdfs, ledger):
    requested_levels = list(
        getattr(args, "requested_read_levels", args.read_level)
    )
    skipped_levels = dict(getattr(args, "skipped_read_levels", {}))
    if args.read_bundle:
        run_imported_full_dump(
            args, project, output, runner, bdfs, ledger
        )
        return
    if args.no_live_capture:
        ledger.append(item(
            "ordinary-read-dump-plan", "skipped", "--no-live-capture selected",
            levels_requested=requested_levels, levels_completed=[],
            levels_skipped={
                level: "--no-live-capture selected" for level in requested_levels
            },
        ))
        ledger.append(item(
            "glitch-read-dump-plan",
            "skipped" if args.include_glitch_read else "not-requested",
            ("requested transient capture unavailable because live capture is disabled"
             if args.include_glitch_read else
             "transient branch was not requested"),
            levels_requested=["glitch-read"], levels_completed=[],
        ))
        record_glitch_export_coverage(
            ledger, "skipped" if args.include_glitch_read else "not-requested",
            "live capture is disabled",
        )
        record_skipped_full_dump_coverage(
            ledger, "live capture was disabled and no verified bundle was imported"
        )
        return
    if not args.read_level:
        ledger.append(item(
            "ordinary-read-dump-plan", "skipped",
            "all requested ordinary levels were excluded by the access policy",
            levels_requested=requested_levels, levels_completed=[],
            levels_skipped=skipped_levels,
        ))
        record_skipped_full_dump_coverage(
            ledger, "all ordinary capture levels were excluded by access policy"
        )
        run_glitch_dump(args, project, output, runner, bdfs, ledger)
        return
    bundle = output / "read-dump-plan-bundle"
    argv = [
        sys.executable, str(project / "170HX-XRAY.py"), "full-dump",
        str(bundle), "--register-profile", "all-safe",
        "--controller-helper", args.controller_helper,
        "--candidate-planner", str(project / "tools" / "170hx_candidate_plan.py"),
    ]
    argv.extend(complete_row_export_arguments("locked_controller"))
    for level in args.read_level:
        argv.extend(("--level", level))
    append_bdfs(argv, bdfs)
    if args.owner_busy:
        argv.append("--owner-busy")
    if args.allow_bound_controller:
        argv.append("--allow-bound-controller")
    result = runner.run(argv, timeout=args.long_timeout)
    logs = create_dir(output / "read-command")
    entry = item(
        "ordinary-read-dump-plan",
        "completed" if result["returncode"] == 0 else "failed",
        "full-dump bundle created" if result["returncode"] == 0
        else "full-dump command failed",
        levels_requested=requested_levels,
        levels_completed=list(args.read_level) if result["returncode"] == 0 else [],
        levels_skipped=skipped_levels,
        command_evidence=save_command(result, logs, "full-dump", output),
    )
    verified_index = None
    if (bundle / "index.json").is_file() and (bundle / "SHA256SUMS").is_file():
        verify = runner.run([
            sys.executable, str(project / "170HX-XRAY.py"),
            "verify-full-dump", str(bundle), "--output", "-",
        ], timeout=args.long_timeout)
        entry["verification"] = save_command(verify, logs, "verify", output)
        if verify["returncode"] != 0:
            entry["status"] = "failed"
            entry["reason"] = "independent bundle verification failed"
        else:
            verified_index = strict_json(bundle / "index.json")
            entry["product_coverage"] = full_dump_product_coverage(
                verified_index
            )
            try:
                entry["candidate_input_promotion"] = promote_semantic_capture(
                    args, bundle, verified_index, bdfs, output
                )
            except (OSError, UnicodeError, json.JSONDecodeError,
                    ReportError) as exc:
                entry["candidate_input_promotion"] = {
                    "status": "rejected", "promoted": False,
                    "reason": str(exc),
                }
                if "semantic-read" in args.read_level:
                    entry["status"] = "failed"
                    entry["reason"] = (
                        "verified semantic capture was not eligible for "
                        "candidate planning: %s" % exc
                    )
    elif result["returncode"] == 0:
        entry["status"] = "failed"
        entry["reason"] = "full-dump returned success without an index/manifest"
    ledger.append(entry)
    if verified_index is not None:
        record_full_dump_coverage(verified_index, ledger, "live-full-dump")
    else:
        record_skipped_full_dump_coverage(
            ledger, "no independently verified live full-dump index is available"
        )
    run_glitch_dump(args, project, output, runner, bdfs, ledger)


def _candidate_words(values, count, label):
    if not isinstance(values, list) or len(values) != count:
        raise ReportError("%s must contain exactly %d words" % (label, count))
    words = []
    for index, value in enumerate(values):
        if isinstance(value, bool):
            raise ReportError("%s[%d] is boolean" % (label, index))
        try:
            word = int(value, 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as exc:
            raise ReportError("%s[%d] is not an integer" % (label, index)) from exc
        if not 0 <= word <= 0xFFFFFFFF:
            raise ReportError("%s[%d] is outside u32" % (label, index))
        words.append(word)
    return words


def _candidate_packed_sha256(words):
    return hashlib.sha256(struct.pack("<%dI" % len(words), *words)).hexdigest()


def export_candidate_proposed_images(document, destination, report_root):
    """Create and independently replay four neutral row transports/profile."""
    root = create_dir(destination)
    entries = []
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list) or not cards:
        raise ReportError("candidate proposed-image export has no cards")
    for card in cards:
        bdf = card.get("bdf") if isinstance(card, dict) else None
        if not isinstance(bdf, str) or not BDF_RE.fullmatch(bdf):
            raise ReportError("candidate proposed-image BDF is invalid")
        card_root = create_dir(
            root / bdf.replace(":", "-").replace(".", "-")
        )
        profiles = card.get("profiles")
        profile_ids = [
            value.get("model_id") if isinstance(value, dict) else None
            for value in profiles or []
        ]
        if (not isinstance(profiles, list) or
                profile_ids != list(CANDIDATE_PROFILES)):
            raise ReportError("candidate proposed-image profile inventory is incomplete")
        for profile in profiles:
            profile_id = profile.get("model_id") if isinstance(profile, dict) else None
            if (not isinstance(profile_id, str) or
                    re.fullmatch(r"[A-Za-z0-9_.-]+", profile_id) is None):
                raise ReportError("candidate profile ID is unsafe")
            profile_root = create_dir(card_root / profile_id)
            words = _candidate_words(
                profile.get("proposed_complete_image_rows"), 512,
                "%s/%s proposed MAIN" % (bdf, profile_id),
            )
            digest = _candidate_packed_sha256(words)
            claimed = profile.get("proposed_complete_image_u32le_sha256")
            if claimed != digest:
                raise ReportError(
                    "%s/%s proposed MAIN digest mismatch" % (bdf, profile_id)
                )
            rows = ["0x%08x" % value for value in words]
            expected_json = {
                "schema": "170hx-xray-proposed-main-v1",
                "bdf": bdf, "profile_id": profile_id,
                "rows": rows, "u32le_sha256": digest,
                "execution_authorized": False,
            }
            paths = {
                "json": write_new(
                    profile_root / "proposed-main.json",
                    json.dumps(
                        expected_json, indent=2, sort_keys=True,
                        allow_nan=False,
                    ) + "\n",
                ),
                "jsonl": write_new(
                    profile_root / "proposed-main.jsonl",
                    "".join(
                        json.dumps(
                            {"row": index, "value": value},
                            sort_keys=True, separators=(",", ":"),
                            allow_nan=False,
                        ) + "\n"
                        for index, value in enumerate(rows)
                    ),
                ),
                "indexed-text": write_new(
                    profile_root / "proposed-main.indexed.txt",
                    "".join(
                        "[%03d] %s\n" % (index, value)
                        for index, value in enumerate(rows)
                    ),
                ),
                "raw-u32le": write_new(
                    profile_root / "proposed-main.raw-u32le.bin",
                    struct.pack("<512I", *words), True,
                ),
            }
            if strict_json(paths["json"]) != expected_json:
                raise ReportError("proposed MAIN JSON replay mismatch")
            jsonl_words = []
            for ordinal, line in enumerate(
                    paths["jsonl"].read_text(encoding="ascii").splitlines()):
                record = _strict_json_payload(
                    line, "%s/%s proposed JSONL" % (bdf, profile_id)
                )
                if record.get("row") != ordinal:
                    raise ReportError("proposed MAIN JSONL order mismatch")
                jsonl_words.append(int(record.get("value"), 16))
            indexed_words = []
            for ordinal, line in enumerate(
                    paths["indexed-text"].read_text(
                        encoding="ascii"
                    ).splitlines()):
                match = re.fullmatch(r"\[(\d{3})\] (0x[0-9a-f]{8})", line)
                if match is None or int(match.group(1)) != ordinal:
                    raise ReportError("proposed MAIN indexed-text order mismatch")
                indexed_words.append(int(match.group(2), 16))
            raw = paths["raw-u32le"].read_bytes()
            if len(raw) != 2048:
                raise ReportError("proposed MAIN raw-u32le length mismatch")
            raw_words = list(struct.unpack("<512I", raw))
            if (jsonl_words != words or indexed_words != words or
                    raw_words != words or _candidate_packed_sha256(raw_words) != digest):
                raise ReportError(
                    "%s/%s proposed MAIN transport replay mismatch" %
                    (bdf, profile_id)
                )
            entries.append({
                "bdf": bdf, "profile_id": profile_id,
                "proposed_main_u32le_sha256": digest,
                "formats": {
                    name: artifact(path, report_root)
                    for name, path in sorted(paths.items())
                },
                "execution_authorized": False,
            })
    return entries


def candidate_target_summary(document, bdfs):
    """Extract and cross-check specimen-specific capacity ceilings."""
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list) or len(cards) != len(bdfs):
        raise ReportError("candidate target card count mismatch")
    by_bdf = {}
    for card in cards:
        if not isinstance(card, dict) or card.get("bdf") in by_bdf:
            raise ReportError("candidate target identity is invalid or duplicated")
        by_bdf[card.get("bdf")] = card
    if set(by_bdf) != set(bdfs):
        raise ReportError("candidate target BDF set mismatch")
    rows = []
    expected_routes = {
        "path-a-a100-identity-vbios-software-sm",
        "path-b-retain-cmp-software-geometry-gen4",
    }
    for bdf in bdfs:
        card = by_bdf[bdf]
        projection = card.get("specimen_ceiling_projection")
        if not isinstance(projection, dict):
            raise ReportError("candidate specimen ceiling is absent: %s" % bdf)
        memory = projection.get("memory")
        compute = projection.get("compute")
        pipe = projection.get("system_pipe")
        if not all(isinstance(value, dict) for value in (memory, compute, pipe)):
            raise ReportError("candidate specimen ceiling planes are absent: %s" % bdf)
        values = {
            "current_hbm_gib": memory.get("current_effective_hbm_gib"),
            "target_hbm_gib": memory.get("defect_preserving_target_hbm_gib"),
            "usable_fbps": memory.get("projected_usable_fbps"),
            "current_sm": compute.get("current_effective_sm"),
            "target_sm": compute.get("defect_preserving_target_sm"),
            "usable_tpcs": compute.get("projected_usable_tpcs"),
            "system_pipes": pipe.get("projected_nondefective_bits"),
        }
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in values.values()):
            raise ReportError("candidate ceiling contains a non-integer: %s" % bdf)
        if (values["target_hbm_gib"] not in (64, 80, 96) or
                values["target_hbm_gib"] != values["usable_fbps"] * 8 or
                not 0 <= values["current_hbm_gib"] <= values["target_hbm_gib"] or
                values["target_sm"] != values["usable_tpcs"] * 2 or
                not 0 <= values["current_sm"] <= values["target_sm"] <= 128 or
                not 0 <= values["system_pipes"] <= 8):
            raise ReportError("candidate capacity arithmetic is incoherent: %s" % bdf)
        routes = card.get("dual_path_plans")
        if (not isinstance(routes, list) or
                {route.get("id") for route in routes if isinstance(route, dict)} !=
                expected_routes or
                any(route.get("execution_authorized") is not False
                    for route in routes if isinstance(route, dict))):
            raise ReportError("candidate dual-route inventory mismatch: %s" % bdf)
        profiles = card.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ReportError("candidate profile inventory is absent: %s" % bdf)
        rows.append({
            "bdf": bdf,
            **values,
            "memory_confidence_percent": memory.get("confidence_percent"),
            "compute_confidence_percent": compute.get("confidence_percent"),
            "system_pipe_confidence_percent": pipe.get("confidence_percent"),
            "route_ids": sorted(expected_routes),
            "research_profile_count": len(profiles),
        })
    return rows


def compile_candidate_manifests(project, output, runner, candidate_path,
                                bdfs, ledger, timeout):
    logs = create_dir(output / "manifest-command")
    baselines = create_dir(output / "candidate-baselines")
    manifest_set = create_dir(output / "sealed-v2-manifests")
    manifests = create_dir(manifest_set / "manifests")
    candidate = strict_json(candidate_path)
    cards = candidate.get("cards")
    if not isinstance(cards, list):
        raise ReportError("candidate cards is not a list")
    by_bdf = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("bdf"), str):
            raise ReportError("candidate card identity is invalid")
        bdf = card["bdf"].lower()
        if bdf in by_bdf:
            raise ReportError("duplicate candidate BDF: %s" % bdf)
        by_bdf[bdf] = card
    if set(by_bdf) != set(bdfs):
        raise ReportError("candidate BDF inventory differs from selected report BDFs")
    compiler = project / "tools" / "170hx_vfi_append_manifest.py"
    commands = []
    outputs = []
    for bdf in bdfs:
        card = by_bdf[bdf]
        baseline = card.get("baseline")
        if not isinstance(baseline, dict):
            raise ReportError("candidate baseline absent for %s" % bdf)
        main = _candidate_words(
            baseline.get("main_rows"), 512, "%s MAIN" % bdf
        )
        rir = _candidate_words(
            baseline.get("rir_rows"), 8, "%s RIR" % bdf
        )
        safe_bdf = bdf.replace(":", "-").replace(".", "-")
        main_path = write_new(
            baselines / (safe_bdf + ".main.u32le"),
            struct.pack("<512I", *main), True,
        )
        rir_path = write_new(
            baselines / (safe_bdf + ".rir.u32le"),
            struct.pack("<8I", *rir), True,
        )
        available = {
            value.get("model_id") for value in card.get("profiles", [])
            if isinstance(value, dict)
        }
        missing = set(MANIFEST_PROFILES) - available
        if missing:
            raise ReportError(
                "candidate %s lacks manifest profiles: %s" %
                (bdf, ", ".join(sorted(missing)))
            )
        for profile in MANIFEST_PROFILES:
            path = manifests / (safe_bdf + "__" + profile + ".json")
            compile_result = runner.run([
                sys.executable, str(compiler), "compile",
                "--candidate", str(candidate_path), "--bdf", bdf,
                "--profile", profile, "--cold-main", str(main_path),
                "--cold-rir", str(rir_path), "--output", str(path),
            ], timeout=timeout)
            stem = safe_bdf + "__" + profile
            compile_record = save_command(
                compile_result, logs, "compile-" + stem, output
            )
            verify_record = None
            if compile_result["returncode"] == 0 and path.is_file():
                verify_result = runner.run([
                    sys.executable, str(compiler), "verify",
                    "--manifest", str(path),
                ], timeout=timeout)
                verify_record = save_command(
                    verify_result, logs, "verify-" + stem, output
                )
            commands.append({
                "bdf": bdf, "profile": profile,
                "compile": compile_record, "verify": verify_record,
            })
            if (compile_result["returncode"] == 0 and path.is_file() and
                    verify_record is not None and
                    verify_record["status"] == "completed"):
                outputs.append(path)
    expected = len(bdfs) * len(MANIFEST_PROFILES)
    complete = len(outputs) == expected
    checksum_path = None
    if complete:
        checksum = "".join(
            "%s  manifests/%s\n" % (sha256_file(path), path.name)
            for path in sorted(outputs, key=lambda value: value.name)
        )
        checksum_path = write_new(manifest_set / "SHA256SUMS", checksum)
    ledger.append(item(
        "guarded-experimental-write-manifests",
        "completed" if complete else "failed",
        "complete compile-and-verify matrix; execution remains unauthorized"
        if complete else "one or more sealed manifests failed compilation/verification",
        profiles=list(MANIFEST_PROFILES), cards=len(bdfs),
        expected_manifest_count=expected, verified_manifest_count=len(outputs),
        execution_authorized=False, commands=commands,
        checksum=(artifact(checksum_path, output) if checksum_path else None),
    ))


def run_candidates(args, project, output, runner, bdfs, ledger):
    if not args.physical_capture:
        ledger.append(item(
            "exact-current-permanent-plans", "skipped",
            "no same-session native Level-3 capture supplied; stale data is not promoted to a burn plan",
        ))
        ledger.append(item(
            "guarded-experimental-write-manifests", "skipped",
            "no exact-current candidate exists to seal",
            execution_authorized=False,
        ))
        return
    if len(args.physical_capture) not in (1, len(bdfs)):
        ledger.append(item(
            "exact-current-permanent-plans", "failed",
            "supply one multi-card capture or one capture per selected BDF",
        ))
        ledger.append(item(
            "guarded-experimental-write-manifests", "skipped",
            "candidate input cardinality was rejected",
            execution_authorized=False,
        ))
        return
    inputs = create_dir(output / "candidate-inputs")
    copied_by_source = {}
    copied_artifacts = []
    copied_inputs = []
    for raw in args.physical_capture:
        source = Path(raw).resolve()
        key = str(source)
        copied = copied_by_source.get(key)
        if copied is None:
            copied = inputs / ("capture-%03d.json" % (len(copied_by_source) + 1))
            copy_regular_new(source, copied)
            copied_by_source[key] = copied
            copied_artifacts.append(artifact(copied, output))
        copied_inputs.append(str(copied))
    captures = (copied_inputs * len(bdfs)
                if len(copied_inputs) == 1 else copied_inputs)
    destination = output / "exact-current-candidate-plans.json"
    export_all = output / "candidate-plan-exports"
    argv = [
        sys.executable, str(project / "tools" / "170hx_candidate_plan.py"),
        "--root", str(project.parent), "--same-capture-context",
        "--xray", str(project / "170HX-XRAY.py"), "--output", str(destination),
        "--output-format", "json", "--export-all-dir", str(export_all),
    ]
    for capture, bdf in zip(captures, bdfs):
        argv.extend(("--physical-capture", capture, "--physical-card", bdf))
    result = runner.run(argv, timeout=args.long_timeout)
    logs = create_dir(output / "candidate-command")
    entry = item(
        "exact-current-permanent-plans",
        "completed" if result["returncode"] == 0 else "failed",
        "all current profiles generated from same-session evidence"
        if result["returncode"] == 0 else "candidate evidence was rejected",
        command_evidence=save_command(result, logs, "candidate", output),
        copied_capture_inputs=copied_artifacts,
        levels_requested=["semantic-read"], levels_completed=[],
        levels_skipped={},
    )
    if destination.is_symlink():
        entry["status"] = "failed"
        entry["reason"] = "candidate planner returned a symlink instead of an output file"
        entry["levels_skipped"] = {
            "semantic-read": "candidate evidence was rejected"
        }
    elif destination.is_file():
        entry["artifact"] = artifact(destination, output)
        try:
            candidate_document = strict_json(destination)
            entry["specimen_targets"] = candidate_target_summary(
                candidate_document, bdfs
            )
            selected_profiles = candidate_document.get("selected_profiles")
            if (not isinstance(selected_profiles, list) or
                    selected_profiles != list(CANDIDATE_PROFILES) or
                    any(value["research_profile_count"] !=
                        CANDIDATE_PROFILE_COUNT
                        for value in entry["specimen_targets"])):
                raise ReportError(
                    "candidate does not contain the exact ordered set of %d profiles per card" %
                    CANDIDATE_PROFILE_COUNT
                )
            transports = {}
            for output_format, filename in sorted(
                    CANDIDATE_EXPORT_FILENAMES.items()):
                transport_path = export_all / filename
                if transport_path.is_symlink() or not transport_path.is_file():
                    raise ReportError(
                        "candidate %s transport is absent" % output_format
                    )
                if candidate_transport_document(
                        transport_path, output_format) != candidate_document:
                    raise ReportError(
                        "candidate %s transport differs semantically" %
                        output_format
                    )
                transports[output_format] = artifact(transport_path, output)
            entry["transports"] = transports
            entry["candidate_profile_count"] = CANDIDATE_PROFILE_COUNT
            entry["transport_formats"] = sorted(CANDIDATE_EXPORT_FILENAMES)
            proposed_exports = export_candidate_proposed_images(
                candidate_document, output / "candidate-proposed-images",
                output,
            )
            expected_proposed = len(bdfs) * CANDIDATE_PROFILE_COUNT
            if len(proposed_exports) != expected_proposed:
                raise ReportError(
                    "candidate proposed-image export matrix is incomplete"
                )
            entry["proposed_image_exports"] = proposed_exports
            entry["proposed_image_export_count"] = len(proposed_exports)
            entry["proposed_image_formats"] = [
                "json", "jsonl", "indexed-text", "raw-u32le",
            ]
            capture_ages = []
            for card in candidate_document.get("cards", []):
                physical = card.get("physical_capture") if isinstance(card, dict) else None
                generated = physical.get("generated_utc") if isinstance(physical, dict) else None
                capture_ages.append(evidence_age_hours(generated))
            entry["candidate_capture_age_hours_max"] = max(capture_ages)
            entry["candidate_max_age_hours"] = args.candidate_max_age_hours
            historical = max(capture_ages) > args.candidate_max_age_hours
            entry["candidate_freshness"] = (
                "historical-explicitly-allowed" if historical and
                args.allow_historical_candidate else
                "fresh-same-capture-evidence" if not historical else
                "rejected-historical"
            )
            if historical and not args.allow_historical_candidate:
                entry["status"] = "failed"
                entry["reason"] = (
                    "same-capture evidence exceeds --candidate-max-age-hours; "
                    "historical data was not promoted as exact-current"
                )
            elif historical:
                entry["reason"] = (
                    "historical same-capture profiles generated under explicit "
                    "--allow-historical-candidate; not exact-current"
                )
            if entry["status"] == "completed":
                entry["levels_completed"] = ["semantic-read"]
            else:
                entry["levels_skipped"] = {
                    "semantic-read": "candidate evidence was rejected"
                }
        except (OSError, UnicodeError, json.JSONDecodeError, ReportError) as exc:
            entry["status"] = "failed"
            entry["reason"] = "candidate target arithmetic rejected: %s" % exc
            entry["levels_skipped"] = {
                "semantic-read": "candidate evidence was rejected"
            }
    elif result["returncode"] == 0:
        entry["status"] = "failed"
        entry["reason"] = "candidate planner returned success without an output file"
        entry["levels_skipped"] = {
            "semantic-read": "candidate evidence was rejected"
        }
    ledger.append(entry)
    if (result["returncode"] == 0 and destination.is_file() and
            entry["status"] == "completed"):
        try:
            compile_candidate_manifests(
                project, output, runner, destination, bdfs, ledger,
                args.long_timeout,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ReportError) as exc:
            ledger.append(item(
                "guarded-experimental-write-manifests", "failed",
                "manifest matrix rejected: %s" % exc,
                execution_authorized=False,
            ))
    else:
        ledger.append(item(
            "guarded-experimental-write-manifests", "skipped",
            "candidate generation failed",
            execution_authorized=False,
        ))


def run_diagnostics(args, project, output, runner, bdfs, ledger):
    if not args.benchmarks:
        ledger.append(item(
            "diagnostics-benchmarks", "not-requested",
            "--benchmarks not selected",
            families=[
                "typed storage bandwidth", "P2P direct/host", "GDS", "MIG",
                "ECC", "NVENC", "NVDEC", "NVJPEG", "OFA", "temperature",
            ],
        ))
        return
    try:
        diagnostic_timeout = positive_integer(args.long_timeout)
    except argparse.ArgumentTypeError as exc:
        raise ReportError(
            "diagnostics command timeout must be a positive integer"
        ) from exc
    destination = output / "diagnostics.json"
    work = create_dir(output / "diagnostics-work")
    argv = [
        sys.executable, str(project / "diagnostics" / "run_diagnostics.py"),
        "full", "--output", str(destination), "--work-dir", str(work),
        "--command-timeout", str(diagnostic_timeout),
        "--temperature-probe-device", args.temperature_probe_device,
        "--temperature-probe-backend", args.temperature_probe_backend,
        "--temperature-access", args.temperature_access,
        "--power-limit-watts", str(args.power_limit_watts),
        "--power-sample-interval-ms", str(args.power_sample_interval_ms),
    ]
    append_bdfs(argv, bdfs)
    for option, value in (
        ("--bandwidth-bytes", args.bandwidth_bytes),
        ("--bandwidth-iterations", args.bandwidth_iterations),
        ("--p2p-min-bytes", args.p2p_min_bytes),
        ("--p2p-max-bytes", args.p2p_max_bytes),
        ("--p2p-iterations", args.p2p_iterations),
        ("--gds-io-size", args.gds_io_size),
    ):
        if value is not None:
            argv.extend((option, str(value)))
    if args.gds_scratch:
        argv.extend(("--gds-scratch", args.gds_scratch))
    for size in args.gds_size:
        argv.extend(("--gds-size", str(size)))
    if args.media_suite:
        argv.extend(("--media-suite", args.media_suite))
        if args.media_suite_sha256:
            argv.extend(("--media-suite-sha256", args.media_suite_sha256))
    for enabled, flag in (
        (args.temperature_acknowledge_selector_write,
         "--temperature-acknowledge-selector-write"),
        (args.temperature_acknowledge_bound_snapshot,
         "--temperature-acknowledge-bound-snapshot"),
        (args.temperature_acknowledge_hbm_data_read_to_advance,
         "--temperature-acknowledge-hbm-data-read-to-advance"),
        (args.temperature_allow_active_workload,
         "--temperature-allow-active-workload"),
    ):
        if enabled:
            argv.append(flag)
    result = runner.run(argv, timeout=args.benchmark_timeout)
    logs = create_dir(output / "diagnostics-command")
    entry = item(
        "diagnostics-benchmarks",
        "completed" if result["returncode"] == 0 else "failed",
        "per-family JSON retained" if result["returncode"] == 0
        else "diagnostics returned nonzero; partial evidence retained",
        command_evidence=save_command(result, logs, "diagnostics", output),
        power_requirements=[
            "requested/enforced limit before and after", "draw min/max/average",
            "sample count/failures/interval", "provenance",
        ],
    )
    if destination.is_file():
        entry["artifact"] = artifact(destination, output)
        try:
            document = strict_json(destination)
            entry["reported_status"] = document.get("status")
            entry["feature_inventory_rows"] = diagnostics_feature_rows(
                document, bdfs
            )
            entry["feature_inventory_artifact"] = artifact(
                destination, output
            )
            entry["benchmark_power_rows"] = diagnostics_power_rows(
                document, bdfs, args.power_limit_watts
            )
            entry["power_evidence_present"] = True
        except (OSError, UnicodeError, json.JSONDecodeError, ReportError) as exc:
            entry["status"] = "failed"
            entry["reason"] += "; invalid diagnostics JSON: %s" % exc
    ledger.append(entry)


def uuid_map(runner, smi):
    result = runner.run([
        smi, "--query-gpu=pci.bus_id,uuid,name", "--format=csv,noheader,nounits",
    ])
    mapping = {}
    if result["returncode"] == 0:
        for line in result["stdout"].decode("utf-8", "replace").splitlines():
            fields = [part.strip() for part in line.split(",", 2)]
            if len(fields) != 3:
                continue
            bdf = fields[0].lower().removeprefix("0000")
            if bdf.startswith(":"):
                bdf = "0000" + bdf
            mapping[bdf] = {"uuid": fields[1], "name": fields[2]}
    return result, mapping


def archbench_source_binding(root):
    """Fail closed if the canonical ArchBench checkout differs from its lock."""
    canonical = ARCHBENCH_SUBMODULE_ROOT.resolve()
    if root != canonical:
        return {
            "kind": "explicit-custom-root",
            "root": str(root),
            "pin_verified": False,
            "reason": (
                "custom ArchBench roots are outside the repository submodule "
                "lock and require separate review"
            ),
        }
    try:
        lock = strict_json(ARCHBENCH_SUBMODULE_LOCK)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError("cannot load ArchBench submodule lock: %s" % exc) from exc
    required = ("path", "url", "commit", "tree", "expected_tracked_files")
    missing = [name for name in required if name not in lock]
    if missing:
        raise ReportError(
            "ArchBench submodule lock lacks: %s" % ", ".join(missing)
        )
    if lock["path"] != str(root.relative_to(PROJECT_ROOT)):
        raise ReportError("ArchBench submodule lock path does not match checkout")

    def git(*argv):
        completed = subprocess.run(
            ["git", "-C", str(root), *argv], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise ReportError(
                "cannot verify ArchBench submodule %s: %s" %
                (" ".join(argv), detail or "git returned nonzero")
            )
        return completed.stdout

    head = git("rev-parse", "HEAD").decode("ascii", "strict").strip()
    tree = git("rev-parse", "HEAD^{tree}").decode("ascii", "strict").strip()
    tracked = [value for value in git("ls-files", "-z").split(b"\0") if value]
    dirty = git("status", "--porcelain", "--untracked-files=no")
    expected_count = lock["expected_tracked_files"]
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise ReportError("ArchBench expected_tracked_files is not an integer")
    mismatches = []
    if head != lock["commit"]:
        mismatches.append("commit %s != %s" % (head, lock["commit"]))
    if tree != lock["tree"]:
        mismatches.append("tree %s != %s" % (tree, lock["tree"]))
    if len(tracked) != expected_count:
        mismatches.append(
            "tracked files %d != %d" % (len(tracked), expected_count)
        )
    if dirty:
        mismatches.append("tracked files are locally modified")
    if mismatches:
        raise ReportError(
            "ArchBench submodule does not match its reviewed lock: %s" %
            "; ".join(mismatches)
        )
    return {
        "kind": "pinned-submodule",
        "root": str(root),
        "pin_verified": True,
        "url": lock["url"],
        "commit": head,
        "tree": tree,
        "tracked_files": len(tracked),
        "lock": str(ARCHBENCH_SUBMODULE_LOCK.relative_to(PROJECT_ROOT)),
    }


def run_archbench(args, output, runner, bdfs, ledger):
    if not args.archbench:
        ledger.append(item(
            "archbench-acceptance-235", "not-requested",
            "--archbench not selected",
        ))
        return
    root = Path(args.archbench_root).resolve()
    bench = root / "bench"
    worker = (
        Path(args.archbench_binary).resolve() if args.archbench_binary
        else root / "build" / "bin" / "archbench_sm80"
    )
    matrix = root / "configs" / "acceptance.json"
    if not bench.is_file() or not matrix.is_file():
        ledger.append(item(
            "archbench-acceptance-235", "failed",
            "ArchBench source is absent or incomplete; initialize the pinned "
            "submodule with: git submodule update --init --recursive",
            bench=str(bench), worker=str(worker), matrix=str(matrix),
        ))
        return
    try:
        source_binding = archbench_source_binding(root)
    except (OSError, UnicodeError, UnicodeDecodeError, subprocess.SubprocessError,
            ReportError, ValueError) as exc:
        ledger.append(item(
            "archbench-acceptance-235", "failed",
            "ArchBench source pin verification failed: %s" % exc,
            bench=str(bench), worker=str(worker), matrix=str(matrix),
        ))
        return
    if not worker.is_file():
        ledger.append(item(
            "archbench-acceptance-235", "failed",
            "ArchBench SM80 worker is absent; build it in the submodule with: "
            "./bench build --arch 80",
            bench=str(bench), worker=str(worker), matrix=str(matrix),
        ))
        return
    query, mapping = uuid_map(runner, args.nvidia_smi)
    logs = create_dir(output / "archbench-command")
    query_record = save_command(query, logs, "uuid-map", output)
    per_card = []
    for bdf in bdfs:
        stem = bdf.replace(":", "-").replace(".", "-")
        gpu = mapping.get(bdf)
        if not gpu:
            per_card.append({"bdf": bdf, "status": "failed",
                             "reason": "UUID mapping unavailable"})
            continue
        pre_power_result, pre_power_map = power_query(
            runner, args.nvidia_smi, [bdf]
        )
        pre_power_evidence = save_command(
            pre_power_result, logs, "power-before-" + stem, output
        )
        destination = output / ("archbench-" + stem)
        argv = [
            str(bench), "run", "--matrix", str(matrix), "--quality", "stable",
            "--gpu-uuid", gpu["uuid"], "--binary", str(worker),
            "--output", str(destination), "--timeout",
            str(args.archbench_case_timeout), "--telemetry",
            "--telemetry-interval", str(args.archbench_telemetry_interval),
        ]
        result = runner.run(argv, cwd=root, timeout=args.benchmark_timeout)
        post_power_result, post_power_map = power_query(
            runner, args.nvidia_smi, [bdf]
        )
        post_power_evidence = save_command(
            post_power_result, logs, "power-after-" + stem, output
        )
        pre_power = pre_power_map.get(bdf)
        post_power = post_power_map.get(bdf)
        record = {
            "bdf": bdf, "uuid": gpu["uuid"], "name": gpu["name"],
            "status": "completed" if result["returncode"] == 0 else "failed",
            "command_evidence": save_command(
                result, logs, "run-" + stem,
                output,
            ),
            "power_evidence": {
                "requested_limit_watts": args.power_limit_watts,
                "limit_enforcement_requested": args.set_power_limit,
                "limit_before": pre_power,
                "limit_after": post_power,
                "before_query": pre_power_evidence,
                "after_query": post_power_evidence,
                "requested_sampling_interval_seconds":
                    args.archbench_telemetry_interval,
                "provenance": (
                    "NVML whole-worker samples emitted by the selected ArchBench "
                    "runner and nvidia-smi limit queries bound to this BDF/UUID"
                ),
            },
        }
        for name in (
                "run.json", "summary.json", "coverage.json", "plan.jsonl",
                "results.jsonl", "results.csv", "telemetry.jsonl"):
            path = destination / name
            if path.is_file():
                record[name] = artifact(path, output)
        try:
            validation = validate_archbench_run(
                destination, bdf, gpu["uuid"], gpu["name"], worker, matrix,
                args.archbench_case_timeout,
            )
            record["coverage_validation"] = validation
            if result["returncode"] == 0:
                record["reason"] = (
                    "%d selected and %d recorded; %d numerically validated "
                    "measurements; outcomes %s" % (
                        validation["selected"], validation["recorded"],
                        validation["numerically_validated_measured"],
                        json.dumps(validation["status_counts"],
                                   sort_keys=True, allow_nan=False),
                    )
                )
        except (OSError, UnicodeError, json.JSONDecodeError, ReportError) as exc:
            record["status"] = "failed"
            record["reason"] = "ArchBench coverage validation failed: %s" % exc
        telemetry = destination / "telemetry.jsonl"
        try:
            summary = telemetry_power_summary(telemetry)
            summary["provenance"].update({"bdf": bdf, "uuid": gpu["uuid"]})
            record["power_evidence"]["draw"] = summary
        except (OSError, UnicodeError, ReportError) as exc:
            record["status"] = "failed"
            record["reason"] = "power telemetry unavailable or invalid: %s" % exc
        if (not isinstance(pre_power, dict) or
                pre_power.get("limit_watts") is None or
                not isinstance(post_power, dict) or
                post_power.get("limit_watts") is None):
            record["status"] = "failed"
            record["reason"] = "power-limit query before/after benchmark failed"
        elif abs(pre_power["limit_watts"] - post_power["limit_watts"]) > 0.51:
            record["status"] = "failed"
            record["reason"] = "power limit changed during benchmark"
        elif (args.set_power_limit and
              abs(pre_power["limit_watts"] - args.power_limit_watts) > 0.51):
            record["status"] = "failed"
            record["reason"] = "requested benchmark power limit was not enforced"
        per_card.append(record)
    status = ("completed" if per_card and
              all(value["status"] == "completed" for value in per_card)
              else "failed")
    ledger.append(item(
        "archbench-acceptance-235", status,
        ("every card has a validated 235-selected/235-recorded acceptance "
         "inventory with per-outcome measurement counts") if status == "completed"
        else "one or more per-card acceptance runs failed",
        scope=(
            "broad 20-family initial correctness/capability gate for principal "
            "floating, integer, low-precision, quantized, real and complex paths; "
            "not the 404,129-case catalogue or sustained peak proof"
        ),
        requested_power_limit_watts=args.power_limit_watts,
        source_binding=source_binding, gpu_query=query_record, per_card=per_card,
    ))


def esc(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def stdout_text(record, output):
    try:
        return (output / record["stdout"]["path"]).read_text(
            encoding="utf-8", errors="replace"
        )
    except (KeyError, OSError):
        return ""


def assess_nvidia_driver(commands, output):
    """Classify only explicit patch evidence; absence never proves stock."""
    evidence_keys = (
        "dkms", "nvidia-module", "nvidia-module-hash", "nvidia-driver",
        "nvidia-params", "nvidia-proc-gpus", "nvidia-packages-deb",
        "nvidia-packages-rpm",
    )
    evidence = []
    matches = []
    for key in evidence_keys:
        record = commands.get(key)
        if not isinstance(record, dict):
            continue
        stdout = record.get("stdout")
        evidence.append({
            "command_key": key,
            "status": record.get("status"),
            "returncode": record.get("returncode"),
            "stdout": stdout if isinstance(stdout, dict) else None,
        })
        text = stdout_text(record, output)
        for marker, pattern in DRIVER_PATCH_MARKERS:
            if pattern.search(text):
                matches.append({"command_key": key, "marker": marker})
    matches = sorted(
        {("%s\0%s" % (value["command_key"], value["marker"])): value
         for value in matches}.values(),
        key=lambda value: (value["command_key"], value["marker"]),
    )
    if matches:
        verdict = "patched"
        reason = (
            "one or more explicit, allowlisted patch-project markers occur "
            "in retained NVIDIA module/package evidence"
        )
        basis = "explicit-marker"
    else:
        verdict = "unknown"
        reason = (
            "no explicit patch marker was observed; absence is not proof of a "
            "stock driver, and no trusted vendor/package byte baseline was "
            "supplied for the loaded module"
        )
        basis = "insufficient-attestation"
    return {
        "schema": "170hx-xray-nvidia-driver-assessment-v1",
        "verdict": verdict,
        "possible_verdicts": ["patched", "stock", "unknown"],
        "basis": basis,
        "reason": reason,
        "explicit_marker_matches": matches,
        "evidence": evidence,
        "stock_verdict_policy": (
            "stock requires a trusted vendor/package baseline, integrity "
            "verification of the exact on-disk module, and binding evidence "
            "for the loaded module; marker absence alone is insufficient"
        ),
        "trust_boundary": (
            "text markers can establish an explicit patch lineage but cannot "
            "prove that every loaded NVIDIA component has the same lineage"
        ),
    }


def render(document, output, commands):
    driver = document.get("driver_assessment")
    if not isinstance(driver, dict):
        driver = {
            "verdict": "unknown", "basis": "assessment-absent",
            "reason": "no structured driver assessment was supplied",
            "stock_verdict_policy": "marker absence is not stock proof",
            "trust_boundary": "no assessment evidence",
            "explicit_marker_matches": [],
        }
    lines = [
        "# 170HX-XRAY full report", "",
        "Generated: `%s`  " % document["generated_utc"],
        "Overall status: **%s**" % document["status"], "",
        "Skipped, not-requested, failed, and not-yet-supported work is explicit; none is counted as completed.",
        "", "## Scope and safety", "",
        "- Report mode: `%s`" % document.get("report_mode", "report"),
        "- Selected GA100 BDFs: %s" % ", ".join(
            "`%s`" % value for value in document["selected_bdfs"]
        ),
        "- OTP commands, VBIOS writes and GPU resets by this report engine: **0**",
        "- Privileged read-only host inventory requested: `%s`" %
        document["safety"].get("sudo_read_inventory"),
        "- Benchmark power control: %s" % (
            "requested at `%s W`" % document["power"]["requested_limit_watts"]
            if document["power"].get("set_requested") else "not requested"
        ),
        "- Power restoration: `%s`" % document["power"]["restoration_status"],
        "", "## NVIDIA driver patch assessment", "",
        "- Verdict: **%s**" % esc(driver.get("verdict")),
        "- Basis: `%s`" % esc(driver.get("basis")),
        "- Reason: %s" % esc(driver.get("reason")),
        "- Stock-proof policy: %s" % esc(driver.get("stock_verdict_policy")),
        "- Trust boundary: %s" % esc(driver.get("trust_boundary")),
        "- Explicit marker matches: `%s`" % esc(
            json.dumps(driver.get("explicit_marker_matches", []),
                       sort_keys=True, allow_nan=False)
        ),
        "", "## Coverage ledger", "",
        "| Component | Status | Reason |", "|---|---:|---|",
    ]
    for entry in document["components"]:
        lines.append("| `%s` | **%s** | %s |" % (
            esc(entry["id"]), esc(entry["status"]), esc(entry["reason"]),
        ))
    levels = {}
    for entry in document["components"]:
        for level in entry.get("levels_requested", []):
            if level in entry.get("levels_completed", []):
                levels[level] = "completed"
            elif level in entry.get("levels_skipped", {}):
                levels[level] = "skipped: " + entry["levels_skipped"][level]
            elif level in entry.get("levels_not_requested", {}):
                levels[level] = (
                    "not-requested: " + entry["levels_not_requested"][level]
                )
            else:
                levels[level] = entry["status"]
    lines.extend([
        "", "## Read, dump, and plan levels", "",
        "| Level | Result |", "|---|---|",
    ])
    for level in ("passive", "bar-read", "semantic-read", "glitch-read"):
        lines.append("| `%s` | %s |" % (
            level, levels.get(level, "not-requested")
        ))
    lines.extend([
        "",
        "Every completed full-dump level exports the complete XRAY JSON plus all supported JSON/JSONL/indexed-text/raw-u32le and explicitly acknowledged public-compatible formats. Exact-current append candidates require same-session native Level-3 evidence.",
        "", "## Permanent route models", "",
        "1. **Retain CMP identity/VBIOS:** stage memory and compute prerequisites, activate topology, then optional global NVLink, defect-gated SYS_PIPE/media and isolated PCIe gates. Keep device ID `20c2`; apply the CMP driver/EFI memory geometry and PCIe policy needed after reset. This route normally exposes fuse-enabled SMs under the CMP VBIOS, while Gen4 and full memory geometry remain software-policy work.",
        "2. **Convert to A100 identity/VBIOS:** perform and cold-verify the common topology stages first, prepare a recoverable A100 VBIOS migration, and place the `20b5` identity records last as their own commit domain. After the A100 VBIOS is proven, apply the required software SM/topology policy; A100 identity alone does not manufacture healthy SMs, HBM, media engines, NVLink routing, ECC, or MIG sufficiency.",
        "",
        "Both routes are candidate models. Their ordered sealed-v2 manifests are unauthorized review artifacts, not an electrically qualified instruction to pulse OTP.",
        "", "### Specimen-specific theoretical ceilings", "",
        "These are defect-preserving arithmetic ceilings, not promises that a die, VBIOS, firmware or driver will operate at the projected state.",
        "", "| BDF | Current / target HBM GiB | Usable FBP | Current / target SM | Usable TPC | Nondefective SYS_PIPE bits | Confidence HBM / SM / pipe |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    target_rows = []
    for component in document["components"]:
        if component.get("id") == "exact-current-permanent-plans":
            target_rows.extend(component.get("specimen_targets", []))
    if target_rows:
        for row in target_rows:
            lines.append(
                "| `%s` | %s / %s | %s | %s / %s | %s | %s | %s%% / %s%% / %s%% |" % (
                    esc(row.get("bdf")), esc(row.get("current_hbm_gib")),
                    esc(row.get("target_hbm_gib")), esc(row.get("usable_fbps")),
                    esc(row.get("current_sm")), esc(row.get("target_sm")),
                    esc(row.get("usable_tpcs")), esc(row.get("system_pipes")),
                    esc(row.get("memory_confidence_percent")),
                    esc(row.get("compute_confidence_percent")),
                    esc(row.get("system_pipe_confidence_percent")),
                )
            )
    else:
        lines.append("| No exact-current Level-3 candidate supplied | - | - | - | - | - | - |")
    lines.extend([
        "", "## Benchmark power evidence", "",
        "Each executed family must retain requested/enforced limits, draw minimum/maximum/average, sample count/failures/interval and provenance. Missing evidence makes that component fail.",
        "", "| BDF | Before | Allowed min | Allowed max | During campaign |",
        "|---|---:|---:|---:|---:|",
    ])
    before = document["power"]["before"]
    during = document["power"]["after_set"]
    for bdf in document["selected_bdfs"]:
        old = before.get(bdf) or {}
        new = during.get(bdf) or {}
        lines.append("| `%s` | %s | %s | %s | %s |" % (
            bdf, old.get("limit_watts"), old.get("minimum_watts"),
            old.get("maximum_watts"), new.get("limit_watts"),
        ))
    lines.extend([
        "", "### Executed benchmark power envelopes", "",
        "| Family | BDF | Used limit W | Draw min W | Draw max W | Draw avg W | Valid / failed samples | Requested interval s | Sample span s | Provenance |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    power_rows = []
    for component in document["components"]:
        if component.get("id") == "archbench-acceptance-235":
            for card in component.get("per_card", []):
                evidence = card.get("power_evidence", {})
                draw = evidence.get("draw", {})
                stats = draw.get("statistics", {})
                limit = evidence.get("limit_before") or {}
                interval = draw.get("observed_interval_seconds") or {}
                power_rows.append((
                    "ArchBench acceptance-235", card.get("bdf"),
                    limit.get("limit_watts"), stats.get("minimum_watts"),
                    stats.get("maximum_watts"), stats.get("average_watts"),
                    "%s / %s" % (stats.get("valid_sample_count"),
                                   draw.get("failed_sample_count")),
                    interval.get("average"),
                    None,
                    draw.get("provenance", {}).get("source"),
                ))
        for row in component.get("benchmark_power_rows", []):
            power_rows.append((
                row.get("family"), row.get("bdf"), row.get("used_limit_watts"),
                row.get("minimum_watts"), row.get("maximum_watts"),
                row.get("average_watts"),
                "%s / %s" % (row.get("valid_sample_count"),
                               row.get("failed_sample_count")),
                row.get("interval_seconds"), row.get("sampling_span_seconds"),
                row.get("provenance"),
            ))
    if power_rows:
        for row in power_rows:
            lines.append("| %s | `%s` | %s | %s | %s | %s | %s | %s | %s | %s |" %
                         tuple(esc(value) for value in row))
    else:
        lines.append("| No benchmark executed | - | - | - | - | - | - | - | - | - |")
    feature_rows = []
    feature_artifact = None
    for component in document["components"]:
        if component.get("id") == "diagnostics-benchmarks":
            feature_rows.extend(component.get("feature_inventory_rows", []))
            feature_artifact = component.get("feature_inventory_artifact")
    lines.extend([
        "", "## MIG, ECC, media, OFA and NVLink census", "",
        (
            "Query-only inventory and functional media results are deliberately "
            "separate. `observed` means that the named query completed; it is not "
            "proof that an engine accepts work. `negative`, `skipped`, `not-run`, "
            "`unsupported-or-query-failed`, and `not-yet-supported` remain distinct."
        ),
        "",
    ])
    if feature_artifact:
        lines.extend([
            "Raw commands, stdout/stderr bytes, hashes, full `nvidia-smi -q` text "
            "and XML: [`%s`](%s)." % (
                esc(Path(feature_artifact["path"]).name),
                esc(feature_artifact["path"]),
            ),
            "",
        ])
    lines.extend([
        "| BDF | ECC current / pending | ECC injection | MIG current / pending | MIG GI / CI profiles | MIG configuration | NVLink query | NVENC/NVDEC session query | Functional NVENC | Functional NVDEC caps/decode | NVJPEG inventory / functional | OFA API / max | Functional OFA |",
        "|---|---|---:|---|---|---:|---|---|---|---|---|---|---|",
    ])
    if feature_rows:
        for row in feature_rows:
            lines.append(
                "| `%s` | %s / %s (%s) | %s | %s / %s (%s) | %s / %s | %s | %s | %s | %s | %s | %s / %s | %s / %s | %s |" % tuple(
                    esc(value) for value in (
                        row.get("bdf"), row.get("ecc_current"),
                        row.get("ecc_pending"), row.get("ecc_status"),
                        row.get("ecc_injection"), row.get("mig_current"),
                        row.get("mig_pending"), row.get("mig_status"),
                        row.get("mig_gpu_profiles"),
                        row.get("mig_compute_profiles"),
                        row.get("mig_configuration"), row.get("nvlink_query"),
                        row.get("media_session_query"),
                        row.get("nvenc_functional"),
                        row.get("nvdec_functional"),
                        row.get("nvjpeg_inventory"),
                        row.get("nvjpeg_functional"), row.get("ofa_api_query"),
                        row.get("ofa_max_api_version"),
                        row.get("ofa_functional"),
                    )
                )
            )
    else:
        lines.append(
            "| No validated feature census | - | not-run | - | - | not-run | "
            "not-run | not-run | not-run | not-run | not-run | not-run / - | "
            "not-yet-supported |"
        )
    lines.extend([
        "", "## Feature boundary", "",
        "- MIG/SYS_PIPE: experimental defect-preserving FF plan; enumeration is measured, not assumed.",
        "- NVDEC: five-bit defect-preserving FF plan.",
        "- NVJPG/OFA: bits 146 and 371 only as an inseparable, zero-defect pair; individual attribution is unknown.",
        "- NVENC: direct read/dump coverage exists; no defensible GA100 physical fuse coordinate is known.",
        "- ECC: state/read coverage exists; destructive injection and conventional-fuse execution are unsupported.",
        "", "## ArchBench option A", "",
        "`configs/acceptance.json` has 192 historical requests and expands to 235 distinct selected cases with software counterparts. Selected and recorded are coverage facts, not claims that all 235 produced validated numerical measurements. Outcomes such as `unsupported`, `memory_budget`, and `measurement_only` remain separate. This is a strong initial gate across 20 arithmetic families and principal floating, integer, low-precision, quantized, real and complex operations. It is not the 404,129-case catalogue, exhaustive bit-pattern coverage, sustained peak proof, or a replacement for repeated stability tests.",
        "", "| BDF | Validation | Selected | Recorded | Missing | Numerically validated measured | Records with samples | Outcome counts | Family counts |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ])
    archbench_cards = []
    for component in document["components"]:
        if component.get("id") == "archbench-acceptance-235":
            archbench_cards.extend(component.get("per_card", []))
    if archbench_cards:
        for card in archbench_cards:
            validation = card.get("coverage_validation") or {}
            lines.append(
                "| `%s` | %s | %s | %s | %s | %s | %s | `%s` | `%s` |" %
                tuple(esc(value) for value in (
                    card.get("bdf"), validation.get("validated", False),
                    validation.get("selected", "-"),
                    validation.get("recorded", "-"),
                    validation.get("missing", "-"),
                    validation.get("numerically_validated_measured", "-"),
                    validation.get("records_with_positive_samples", "-"),
                    json.dumps(validation.get("status_counts", {}),
                               sort_keys=True, allow_nan=False),
                    json.dumps(validation.get("selected_family_counts", {}),
                               sort_keys=True, allow_nan=False),
                ))
            )
    else:
        lines.append("| No ArchBench run requested | false | - | - | - | - | - | `{}` | `{}` |")
    lines.extend(["", "## Complete NVIDIA query evidence", ""])
    for name in (
        "nvidia-smi", "nvidia-smi-q", "nvidia-smi-xml", "nvidia-topology",
        "nvidia-nvlink", "nvidia-mig-gpu", "nvidia-mig-compute",
    ):
        record = commands.get(name)
        lines.extend(["### %s" % name, ""])
        if record is None:
            lines.append("Unavailable.")
        else:
            lines.extend([
                "Command: `%s`" % esc(record["command"]), "", "```text",
                stdout_text(record, output).rstrip(), "```",
            ])
        lines.append("")
    lines.extend([
        "## Host evidence files", "",
        "| Evidence | Status | SHA-256 |", "|---|---:|---|",
    ])
    for name, record in sorted(commands.items()):
        lines.append("| [%s](%s) | %s | `%s` |" % (
            esc(name), record["stdout"]["path"], record["status"],
            record["stdout"]["sha256"],
        ))
    lines.extend([
        "", "## Explicit limitations", "",
        "- Absence of a converted `20b5`, transient `2080`, GDS filesystem, codec engine, MIG mode or ECC mode is reported; it is never inferred.",
        "- Physical defect masks are ceilings, not health proof. HBM, SM, PCIe, NVLink, media and MIG need post-change functional validation.",
        "- This report performs no OTP pulse, VBIOS flash, GPU reset, MIG reconfiguration or ECC injection.",
        "", "## Integrity", "",
        "Structured evidence: [`report-index.json`](report-index.json). File hashes: [`SHA256SUMS`](SHA256SUMS).", "",
    ])
    return "\n".join(lines)


def sums(root):
    root = Path(root).resolve()
    self_manifest = root / "SHA256SUMS"
    lines = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise ReportError("symlink in report: %s" % path)
        if not path.is_file() or path == self_manifest:
            continue
        lines.append("%s  %s\n" % (
            sha256_file(path), path.relative_to(root).as_posix()
        ))
    return "".join(lines)


def build_report(args, project=None, runner=None):
    project = Path(project or Path(__file__).resolve().parents[1]).resolve()
    output = create_dir(Path(args.output_dir).resolve())
    runner = runner or Runner(project.parent, args.command_timeout)
    generated = now_utc()
    bdfs = list(dict.fromkeys(args.bdf or discover_bdfs(args.sysfs_root)))
    if not bdfs:
        raise ReportError("no supported GA100 BDF selected or discovered")
    system = create_dir(output / "system")
    commands = {}
    for name, argv in host_commands(
            args.nvidia_smi, args.sudo_read_inventory):
        commands[name] = save_command(
            runner.run(argv), system, name, output
        )
    driver_assessment = assess_nvidia_driver(commands, output)
    ledger = [item(
        "host-inventory", "completed" if commands["nvidia-smi"]["status"] ==
        "completed" else "failed",
        "full raw command outputs retained; optional unavailable commands are individually marked",
    ), item(
        "nvidia-driver-patch-assessment", "completed",
        driver_assessment["reason"],
        verdict=driver_assessment["verdict"],
        basis=driver_assessment["basis"],
    )]
    power_dir = create_dir(output / "power-command")
    before_result, before = power_query(runner, args.nvidia_smi, bdfs)
    power = {
        "requested_limit_watts": args.power_limit_watts,
        "set_requested": args.set_power_limit, "before": before,
        "before_query": save_command(before_result, power_dir, "before", output),
        "set_operations": [], "after_set": {}, "restore_operations": [],
        "after_restore": {}, "restoration_status": "not-needed",
    }
    try:
        if args.set_power_limit:
            power["set_operations"] = change_limits(
                runner, args.nvidia_smi,
                [(bdf, args.power_limit_watts) for bdf in bdfs],
            )
            after_result, power["after_set"] = power_query(
                runner, args.nvidia_smi, bdfs
            )
            power["after_set_query"] = save_command(
                after_result, power_dir, "after-set", output
            )
            bad = [entry for entry in power["set_operations"]
                   if entry["returncode"] != 0]
            mismatch = [bdf for bdf in bdfs if not isinstance(
                power["after_set"].get(bdf), dict) or
                power["after_set"][bdf].get("limit_watts") is None or
                abs(power["after_set"][bdf]["limit_watts"] -
                    args.power_limit_watts) > 0.51]
            ledger.append(item(
                "benchmark-power-limit",
                "completed" if not bad and not mismatch else "failed",
                "requested limit applied and re-read" if not bad and not mismatch
                else "power-limit application/re-read failed",
                failed=bad, mismatched_bdfs=mismatch,
            ))
        else:
            power["after_set"] = before
            ledger.append(item(
                "benchmark-power-limit", "not-requested",
                "observed only; --set-power-limit not selected",
            ))
        run_full_dump(args, project, output, runner, bdfs, ledger)
        run_candidates(args, project, output, runner, bdfs, ledger)
        record_planning_layer_coverage(ledger)
        run_diagnostics(args, project, output, runner, bdfs, ledger)
        run_archbench(args, output, runner, bdfs, ledger)
        campaign_result, power["after_campaign"] = power_query(
            runner, args.nvidia_smi, bdfs
        )
        power["after_campaign_query"] = save_command(
            campaign_result, power_dir, "after-campaign", output
        )
    finally:
        if args.set_power_limit:
            targets = []
            for bdf, reading in before.items():
                if isinstance(reading, dict) and reading.get("limit_watts") is not None:
                    targets.append((bdf, reading["limit_watts"]))
            power["restore_operations"] = change_limits(
                runner, args.nvidia_smi, targets
            )
            restored_result, power["after_restore"] = power_query(
                runner, args.nvidia_smi, bdfs
            )
            power["after_restore_query"] = save_command(
                restored_result, power_dir, "after-restore", output
            )
            mismatch = [bdf for bdf in bdfs if isinstance(before.get(bdf), dict)
                        and before[bdf].get("limit_watts") is not None and
                        (not isinstance(power["after_restore"].get(bdf), dict)
                         or power["after_restore"][bdf].get("limit_watts") is None
                         or abs(power["after_restore"][bdf]["limit_watts"] -
                                before[bdf]["limit_watts"]) > 0.51)]
            power["restoration_status"] = "completed" if not mismatch else "failed"
            power["restoration_mismatches"] = mismatch
    ledger.extend((
        item("ecc-injection", "not-yet-supported",
             "ECC state is inventoried; destructive injection is not implemented"),
        item("nvenc-permanent-coordinate", "not-yet-supported",
             "direct planes are readable but no defensible GA100 physical coordinate is known"),
        item("otp-vbios-reset", "skipped",
             "report generation is non-irreversible observation and benchmarking"),
    ))
    failed = [entry for entry in ledger if entry["status"] == "failed"]
    skipped = [entry for entry in ledger if entry["status"] in
               ("skipped", "not-requested", "not-yet-supported")]
    status_counts = {
        value: sum(entry["status"] == value for entry in ledger)
        for value in sorted(COMPONENT_STATUSES)
    }
    status = "failed" if failed else (
        "complete-with-explicit-skips" if skipped else "complete"
    )
    document = {
        "schema": SCHEMA, "generated_utc": generated.isoformat(),
        "report_mode": args.report_mode,
        "status": status, "selected_bdfs": bdfs, "components": ledger,
        "component_status_counts": status_counts,
        "driver_assessment": driver_assessment, "power": power,
        "tool": {"name": "170hx_full_report", "version": VERSION,
                 "python": platform.python_version(),
                 "sha256": sha256_file(Path(__file__).resolve())},
        "safety": {"otp_commands": 0, "vbios_writes": 0, "gpu_resets": 0,
                   "generic_user_shell": False,
                   "sudo_read_inventory": args.sudo_read_inventory,
                   "recursive_private_modes": {
                       "directories": "0700", "regular_files": "0600",
                       "sealed_before_outer_manifest": True,
                   }},
        "commands": commands,
    }
    index_path = write_new(
        output / "report-index.json",
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    report_path = write_new(
        output / ("170HX-XRAY_REPORT_%s.md" % token(generated)),
        render(document, output, commands) + "\n",
    )
    seal_private_tree(output)
    sum_path = write_new(output / "SHA256SUMS", sums(output))
    seal_private_tree(output)
    return {
        "status": status, "output_dir": str(output),
        "report": artifact(report_path, output), "index": artifact(index_path, output),
        "manifest": artifact(sum_path, output), "components": len(ledger),
        "failed": len(failed), "skipped_or_unsupported": len(skipped),
    }


def build_parser():
    archbench = str(ARCHBENCH_SUBMODULE_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", help="new private report directory")
    parser.add_argument(
        "--report-mode", choices=REPORT_MODES, default="report",
        help=(
            "report is the fast inventory/read/dump/plan path and refuses all "
            "benchmark options; report+bench enables diagnostics, ArchBench, "
            "150-W-by-default power control and sampled power envelopes"
        ),
    )
    parser.add_argument("--bdf", action="append", type=bdf_arg, default=[])
    parser.add_argument("--sysfs-root", default="/sys/bus/pci/devices")
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument(
        "--sudo-read-inventory", action="store_true",
        help=(
            "use non-interactive sudo only for read-only DMI inventory; no "
            "GPU setter or generic user command is enabled"
        ),
    )
    parser.add_argument("--controller-helper", default="/dev/170hx_xray")
    parser.add_argument(
        "--read-level", action="append",
        choices=("passive", "bar-read", "semantic-read"), default=[],
        help="repeatable; default is all three ordinary levels",
    )
    parser.add_argument("--include-glitch-read", action="store_true")
    parser.add_argument(
        "--read-bundle",
        help=(
            "verified prior full-dump directory to import into this report; "
            "requires --no-live-capture and can supply its complete semantic "
            "observation to the exact-current candidate planner"
        ),
    )
    parser.add_argument("--no-live-capture", action="store_true")
    parser.add_argument("--owner-busy", action="store_true")
    parser.add_argument("--allow-bound-controller", action="store_true")
    parser.add_argument("--physical-capture", action="append", default=[])
    parser.add_argument(
        "--candidate-max-age-hours", type=positive, default=24.0,
        help="maximum capture age promoted as exact-current (default: 24)",
    )
    parser.add_argument(
        "--allow-historical-candidate", action="store_true",
        help=(
            "explicitly permit an older internally same-capture plan; the "
            "report labels it historical and never calls it exact-current"
        ),
    )
    parser.add_argument("--benchmarks", action="store_true")
    parser.add_argument("--power-limit-watts", type=positive, default=150.0)
    parser.add_argument("--set-power-limit", action="store_true")
    parser.add_argument("--power-sample-interval-ms", type=int, default=100)
    parser.add_argument("--bandwidth-bytes", default=None)
    parser.add_argument("--bandwidth-iterations", type=int, default=None)
    parser.add_argument("--p2p-min-bytes", default=None)
    parser.add_argument("--p2p-max-bytes", default=None)
    parser.add_argument("--p2p-iterations", type=int, default=None)
    parser.add_argument("--gds-scratch", default=None)
    parser.add_argument("--gds-size", action="append", default=[])
    parser.add_argument("--gds-io-size", default=None)
    parser.add_argument("--media-suite", default=None)
    parser.add_argument("--media-suite-sha256", default=None)
    parser.add_argument("--temperature-probe-device", default="/dev/170hx_xray_therm")
    parser.add_argument(
        "--temperature-probe-backend", choices=("auto", "xray-therm", "gpcprobe"),
        default="auto",
    )
    parser.add_argument(
        "--temperature-access", choices=("bar-read", "semantic-read"),
        default="bar-read",
    )
    parser.add_argument("--temperature-acknowledge-selector-write", action="store_true")
    parser.add_argument("--temperature-acknowledge-bound-snapshot", action="store_true")
    parser.add_argument("--temperature-acknowledge-hbm-data-read-to-advance", action="store_true")
    parser.add_argument("--temperature-allow-active-workload", action="store_true")
    parser.add_argument("--archbench", action="store_true")
    parser.add_argument(
        "--archbench-root", default=archbench,
        help="ArchBench source root (default: the initialized pinned submodule)",
    )
    parser.add_argument(
        "--archbench-binary", default=None,
        help=(
            "ArchBench worker (default: build/bin/archbench_sm80 below the "
            "selected --archbench-root)"
        ),
    )
    parser.add_argument(
        "--archbench-case-timeout", type=positive_integer, default=600
    )
    parser.add_argument(
        "--archbench-telemetry-interval", type=positive, default=0.2
    )
    parser.add_argument("--command-timeout", type=positive, default=60.0)
    parser.add_argument("--long-timeout", type=positive_integer, default=1800)
    parser.add_argument("--benchmark-timeout", type=positive, default=86400.0)
    return parser


def apply_report_mode(args, raw_argv):
    """Bind the fast and benchmark report contracts before any host access."""
    supplied = []
    for token_value in raw_argv:
        token_text = str(token_value)
        option = token_text.split("=", 1)[0]
        if option in BENCHMARK_ONLY_OPTIONS:
            supplied.append(option)
    if args.report_mode == "report":
        if supplied:
            raise ReportError(
                "fast --report-mode report refuses benchmark option(s): %s; "
                "use --report-mode report+bench" %
                ", ".join(sorted(set(supplied)))
            )
        args.benchmarks = False
        args.archbench = False
        args.set_power_limit = False
    else:
        args.benchmarks = True
        args.archbench = True
        args.set_power_limit = True
    return args


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    try:
        apply_report_mode(args, raw_argv)
    except ReportError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if args.read_bundle and not args.no_live_capture:
        sys.stderr.write("error: --read-bundle requires --no-live-capture\n")
        return 2
    if not args.read_level:
        args.read_level = ["passive", "bar-read", "semantic-read"]
    args.requested_read_levels = list(args.read_level)
    args.skipped_read_levels = {}
    if args.owner_busy and "semantic-read" in args.read_level:
        args.skipped_read_levels["semantic-read"] = (
            "--owner-busy selected; controller transactions were refused"
        )
        args.read_level = [value for value in args.read_level
                           if value != "semantic-read"]
    try:
        result = build_report(args)
    except (ReportError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
