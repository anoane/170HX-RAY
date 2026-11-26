#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed GA100 OTP manifest compiler and hardware-inert executor model.

This program has deliberately *no* MMIO, ioctl, PCI-resource, GPIO, sysfs-write,
or subprocess backend.  It can independently validate a XRAY candidate-plan
artifact, bind one exact profile to one exact BDF and complete MAIN/RIR images,
emit a sealed non-executable manifest, collect an interactive review receipt,
and exercise the proposed one-attempt state machine against an in-memory model.

It must not be described as a fuse writer.  The real programming-source,
electrical timing, privilege transition, resense/reset, and recovery contracts
are not qualified on CMP 170HX.  A future hardware implementation must use a
new, separately reviewed kernel ABI; generic write32 is forbidden.
"""

from __future__ import print_function

import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid


TOOL_VERSION = "0.2.0"
CANDIDATE_SCHEMA = "170hx-xray-candidate-plan-v1"
LEGACY_CANDIDATE_SCHEMA = "170hx-ray-candidate-plan-v1"
MANIFEST_SCHEMA = "170hx-xray-otp-sealed-manifest-v1"
RECEIPT_SCHEMA = "170hx-xray-otp-review-receipt-v1"
JOURNAL_SCHEMA = "170hx-xray-otp-simulation-journal-v1"
MAIN_ROWS = 512
RIR_ROWS = 8
FUSELESS_START = 222
FUSELESS_END = 511
MIN_RECOVERY_MARGIN = 8
BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]$")

# These are design-time interlocks, not command-line defaults.  There is no
# code path that changes either value.
REAL_HARDWARE_BACKEND_COMPILED = False
GENERIC_WRITE32_PRESENT = False


class SpecFailure(RuntimeError):
    """A fail-closed validation or state-machine refusal."""


class InjectedFault(SpecFailure):
    """A deliberate, hardware-inert simulator fault."""


_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps


def reject_json_constant(value):
    raise SpecFailure("non-finite JSON constant is forbidden: %s" % value)


def strict_json_loads(*args, **kwargs):
    kwargs.setdefault("parse_constant", reject_json_constant)
    return _JSON_LOADS(*args, **kwargs)


def strict_json_dumps(*args, **kwargs):
    kwargs.setdefault("allow_nan", False)
    return _JSON_DUMPS(*args, **kwargs)


def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpecFailure("duplicate JSON object key: %r" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        return strict_json_loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (OSError, ValueError) as exc:
        raise SpecFailure("cannot load strict JSON %s: %s" % (path, exc)) from exc


def canonical_json_bytes(value):
    return strict_json_dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise SpecFailure("cannot hash %s: %s" % (path, exc)) from exc


def canonical_sha256(value):
    return sha256_bytes(canonical_json_bytes(value))


def packed_u32_sha256(words):
    return sha256_bytes(b"".join(struct.pack("<I", word) for word in words))


def parse_u32(value, label):
    if isinstance(value, bool):
        raise SpecFailure("%s is boolean, not u32" % label)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError as exc:
            raise SpecFailure("%s is not an integer: %r" % (label, value)) from exc
    else:
        raise SpecFailure("%s is not an integer: %r" % (label, value))
    if not 0 <= parsed <= 0xFFFFFFFF:
        raise SpecFailure("%s is outside u32 range" % label)
    return parsed


def hex32(value):
    return "0x%08x" % (value & 0xFFFFFFFF)


def require(condition, message):
    if not condition:
        raise SpecFailure(message)


def normalize_bdf(value):
    normalized = str(value).lower()
    require(BDF_RE.match(normalized) is not None, "BDF must be exact dddd:bb:dd.f")
    return normalized


def parse_words(values, count, label):
    require(isinstance(values, list), "%s must be an array" % label)
    require(len(values) == count, "%s must contain exactly %d rows" % (label, count))
    return [parse_u32(value, "%s[%d]" % (label, index))
            for index, value in enumerate(values)]


def read_u32le_image(path, count, label):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SpecFailure("cannot read %s: %s" % (path, exc)) from exc
    require(len(data) == count * 4,
            "%s must be exactly %d bytes, got %d" % (label, count * 4, len(data)))
    return list(struct.unpack("<%dI" % count, data))


def verify_self_digest(document, field="content_sha256_excluding_self"):
    require(isinstance(document, dict), "document must be an object")
    claimed = document.get(field)
    require(isinstance(claimed, str) and len(claimed) == 64,
            "missing or malformed %s" % field)
    unsigned = copy.deepcopy(document)
    del unsigned[field]
    actual = canonical_sha256(unsigned)
    require(actual == claimed,
            "%s mismatch: claimed %s actual %s" % (field, claimed, actual))
    return actual


def verify_candidate_self_digest(document):
    return verify_self_digest(document, "report_content_sha256_excluding_self")


def _normalize_legacy_candidate(value):
    """Normalize only the retired RAY envelope/profile vocabulary."""
    if isinstance(value, list):
        return [_normalize_legacy_candidate(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = {
                "ray_helper_identity": "xray_helper_identity",
                "planner_catalog_matches_selected_ray_catalog":
                    "planner_catalog_matches_selected_xray_catalog",
                "planning_override_of_ray_planning_allowed_false":
                    "planning_override_of_xray_planning_allowed_false",
            }.get(key, key)
            if normalized_key in result:
                raise SpecFailure(
                    "legacy normalization key collision at %r" % normalized_key
                )
            result[normalized_key] = _normalize_legacy_candidate(item)
        return result
    if isinstance(value, str):
        exact = {
            LEGACY_CANDIDATE_SCHEMA: CANDIDATE_SCHEMA,
            "ray-common-permanent-topology":
                "xray-common-permanent-topology",
            "ray-best-evidence-a100-conversion":
                "xray-best-evidence-a100-conversion",
            "ray-best-evidence": "xray-best-evidence",
            "ray-best-evidence-segment": "xray-best-evidence-segment",
        }
        return exact.get(value, value)
    return value


def canonical_profile_id(profile_id):
    return {
        "ray-common-permanent-topology":
            "xray-common-permanent-topology",
        "ray-best-evidence-a100-conversion":
            "xray-best-evidence-a100-conversion",
    }.get(profile_id, profile_id)


def decode_ff_word(word):
    """Decode the structural FF record envelope, not its field semantics."""
    return {
        "data": (word >> 16) & 0xFFFF,
        "block": (word >> 6) & 0x3FF,
        "chain": word & 0x3F,
    }


def select_card_and_profile(candidate, bdf, profile_id):
    require(candidate.get("schema") == CANDIDATE_SCHEMA,
            "unsupported candidate schema")
    require(candidate.get("executable") is False,
            "candidate must remain explicitly non-executable")
    require(candidate.get("execution_authorized") is False,
            "candidate must remain explicitly unauthorized")
    cards = candidate.get("cards")
    require(isinstance(cards, list), "candidate cards must be an array")
    matching_cards = [card for card in cards
                      if isinstance(card, dict) and str(card.get("bdf", "")).lower() == bdf]
    require(len(matching_cards) == 1,
            "exact BDF must select exactly one card; got %d" % len(matching_cards))
    card = matching_cards[0]
    profiles = card.get("profiles")
    require(isinstance(profiles, list), "card profiles must be an array")
    matching_profiles = [profile for profile in profiles
                         if isinstance(profile, dict) and profile.get("model_id") == profile_id]
    require(len(matching_profiles) == 1,
            "exact profile must select exactly one model; got %d" % len(matching_profiles))
    return card, matching_profiles[0]


def validate_operation(op, index, baseline, iff_boundary, quarantined):
    require(isinstance(op, dict), "operation[%d] must be an object" % index)
    require(parse_u32(op.get("index"), "operation[%d].index" % index) == index,
            "operation indexes must be contiguous and ordered")
    row = parse_u32(op.get("row"), "operation[%d].row" % index)
    word = parse_u32(op.get("proposed_physical_word"),
                     "operation[%d].proposed_physical_word" % index)
    expected_old = parse_u32(op.get("expected_physical_old_word"),
                             "operation[%d].expected_physical_old_word" % index)
    require(FUSELESS_START <= row <= FUSELESS_END, "operation row outside FF/IFF interval")
    require(row < iff_boundary, "operation row overlaps active IFF or suffix")
    require(row not in quarantined, "operation row is quarantined")
    require(expected_old == 0, "new FF row must declare exact blank old word")
    require(baseline[row] == 0, "new FF row was not blank in complete baseline")
    require(word != 0, "zero/no-op OTP records are forbidden")
    require((expected_old & ~word) == 0, "physical transition is not 0-to-1-only")

    decoded = decode_ff_word(word)
    chain = parse_u32(op.get("chain"), "operation[%d].chain" % index)
    block = parse_u32(op.get("block"), "operation[%d].block" % index)
    logical_new = parse_u32(op.get("logical_block_new"),
                            "operation[%d].logical_block_new" % index)
    require(chain in (1, 2), "FF chain must be 1 or 2")
    require(decoded["chain"] == chain, "operation chain does not match physical word")
    require(decoded["block"] == block, "operation block does not match physical word")
    require(decoded["data"] == logical_new,
            "operation logical data does not match physical word")
    require(op.get("executable") is False, "operation must remain non-executable")
    immediate = op.get("immediate_verification_required", {})
    require(immediate.get("read_back_exact_target_row") is True,
            "operation lacks exact-row immediate verification")
    require(immediate.get("redecode_complete_image") is True,
            "operation lacks complete-image immediate verification")
    require(immediate.get("retry_on_partial_or_mismatch") is False,
            "operation must forbid retry on mismatch")
    return row, word, decoded


def validate_recovery(profile, baseline, proposed, iff_boundary, quarantined, op_rows):
    recovery = profile.get("recovery")
    require(isinstance(recovery, dict), "recovery plan is mandatory")
    records = recovery.get("records")
    require(isinstance(records, list) and records,
            "at least one append-only recovery record is mandatory")
    require(parse_u32(recovery.get("record_count"), "recovery.record_count") == len(records),
            "recovery record_count mismatch")
    rows = []
    sealed_records = []
    for index, record in enumerate(records):
        require(isinstance(record, dict), "recovery record must be an object")
        row = parse_u32(record.get("row"), "recovery.records[%d].row" % index)
        word = parse_u32(record.get("proposed_physical_word"),
                         "recovery.records[%d].proposed_physical_word" % index)
        old = parse_u32(record.get("expected_physical_old_word"),
                        "recovery.records[%d].expected_physical_old_word" % index)
        require(old == 0, "recovery target must declare exact blank old word")
        require(row < iff_boundary and FUSELESS_START <= row <= FUSELESS_END,
                "recovery row outside available FF interval")
        require(row not in quarantined and row not in op_rows,
                "recovery row overlaps allowlist/quarantine")
        require(baseline[row] == 0 and proposed[row] == 0,
                "recovery reserve row is not blank in baseline/proposed image")
        require(word != 0 and (old & ~word) == 0,
                "recovery physical transition is not blank 0-to-1")
        decoded = decode_ff_word(word)
        chain = parse_u32(record.get("chain"), "recovery chain")
        block = parse_u32(record.get("block"), "recovery block")
        restored = parse_u32(record.get("logical_block_restored"), "recovery restore")
        require(decoded == {"data": restored, "block": block, "chain": chain},
                "recovery record envelope mismatch")
        require(record.get("executable") is False,
                "recovery record must remain non-executable")
        rows.append(row)
        sealed_records.append({
            "row": row,
            "word": hex32(word),
            "chain": chain,
            "block": block,
            "logical_restore": "0x%04x" % restored,
        })
    require(len(rows) == len(set(rows)), "duplicate recovery rows")
    require(rows == list(range(rows[0], rows[0] + len(rows))),
            "recovery reserve must be contiguous and ordered")
    require(not op_rows or rows[0] == max(op_rows) + 1,
            "recovery reserve must immediately follow operation allowlist")
    remaining = parse_u32(recovery.get("remaining_unquarantined_rows"),
                          "recovery.remaining_unquarantined_rows")
    required_margin = parse_u32(recovery.get("required_additional_margin_rows"),
                                "recovery.required_additional_margin_rows")
    require(required_margin >= MIN_RECOVERY_MARGIN,
            "recovery additional margin below policy minimum")
    require(remaining >= required_margin, "recovery reserve leaves insufficient margin")
    require(recovery.get("margin_satisfied") is True,
            "candidate does not attest recovery margin")
    return rows, sealed_records


def validate_candidate_profile(candidate, bdf, profile_id):
    candidate_digest = verify_candidate_self_digest(candidate)
    source_was_legacy = candidate.get("schema") == LEGACY_CANDIDATE_SCHEMA
    candidate = _normalize_legacy_candidate(candidate)
    profile_id = canonical_profile_id(profile_id)
    card, profile = select_card_and_profile(candidate, bdf, profile_id)
    baseline_obj = card.get("baseline")
    require(isinstance(baseline_obj, dict), "missing baseline")
    baseline = parse_words(baseline_obj.get("main_rows"), MAIN_ROWS,
                           "baseline.main_rows")
    rir = parse_words(baseline_obj.get("rir_rows"), RIR_ROWS, "baseline.rir_rows")
    baseline_sha = packed_u32_sha256(baseline)
    rir_sha = packed_u32_sha256(rir)
    require(baseline_sha == baseline_obj.get("complete_main_u32le_sha256"),
            "baseline MAIN hash mismatch")
    require(rir_sha == baseline_obj.get("complete_rir_u32le_sha256"),
            "baseline RIR hash mismatch")
    require(profile.get("baseline_complete_image_u32le_sha256") == baseline_sha,
            "profile is not bound to selected complete baseline")
    require(profile.get("executable") is False and
            profile.get("execution_authorized") is False,
            "profile must remain non-executable and unauthorized")
    require(profile.get("completeness_state") == "prefix-analyzed",
            "profile lacks complete prefix analysis")

    operations = profile.get("operations")
    require(isinstance(operations, list) and operations,
            "profile must contain a nonempty ordered operation list")
    require(parse_u32(profile.get("operation_count"), "profile.operation_count") == len(operations),
            "operation_count mismatch")
    quarantined = set(parse_u32(row, "quarantined row")
                      for row in profile.get("quarantined_rows", []))
    baseline_boundary = baseline_obj.get("capacity", {}).get("iff_boundary_B")
    iff_boundary = parse_u32(baseline_boundary, "baseline iff boundary")
    require(FUSELESS_START < iff_boundary <= FUSELESS_END,
            "invalid IFF boundary")

    image = list(baseline)
    op_rows = []
    sealed_ops = []
    prefixes = profile.get("prefixes")
    require(isinstance(prefixes, list) and len(prefixes) == len(operations) + 1,
            "prefix list must cover baseline plus every operation")
    require(prefixes[0].get("prefix_operation_count") == 0,
            "first prefix is not baseline")
    require(prefixes[0].get("complete_image_u32le_sha256") == baseline_sha,
            "baseline prefix hash mismatch")
    require(prefixes[0].get("structural_pass") is True,
            "baseline prefix did not structurally pass")
    active_iff_signature = baseline_obj.get("active_iff_signature")
    require(isinstance(active_iff_signature, str) and len(active_iff_signature) == 64,
            "baseline active-IFF signature missing")
    require(prefixes[0].get("active_iff_signature") == active_iff_signature,
            "baseline prefix active-IFF signature mismatch")

    for index, op in enumerate(operations):
        row, word, decoded = validate_operation(
            op, index, baseline, iff_boundary, quarantined
        )
        require(row not in op_rows, "operation rows must be unique")
        if index:
            require(row == op_rows[-1] + 1,
                    "operation allowlist must be contiguous and ordered")
        image[row] = word
        digest = packed_u32_sha256(image)
        prefix = prefixes[index + 1]
        require(prefix.get("prefix_operation_count") == index + 1,
                "prefix operation count mismatch")
        require(prefix.get("complete_image_u32le_sha256") == digest,
                "prefix complete-image hash mismatch at operation %d" % index)
        require(op.get("prefix_complete_image_u32le_sha256") == digest,
                "operation prefix hash mismatch at operation %d" % index)
        require(prefix.get("structural_pass") is True and
                prefix.get("ff_structurally_complete") is True and
                prefix.get("active_iff_unchanged") is True,
                "prefix structural/IFF invariant failed at operation %d" % index)
        require(prefix.get("active_iff_signature") == active_iff_signature,
                "prefix active-IFF signature changed at operation %d" % index)
        require(prefix.get("bootability") == "not-established-by-offline-prefix-analysis",
                "offline prefix must not claim bootability")
        op_rows.append(row)
        sealed_ops.append({
            "index": index,
            "row": row,
            "expected_old_word": "0x00000000",
            "program_word": hex32(word),
            "chain": decoded["chain"],
            "block": decoded["block"],
            "logical_data": "0x%04x" % decoded["data"],
            "stage": op.get("stage"),
            "expected_prefix_main_u32le_sha256": digest,
            "one_attempt_only": True,
            "retry_forbidden_after_command_issue": True,
        })

    proposed = parse_words(profile.get("proposed_complete_image_rows"), MAIN_ROWS,
                           "profile.proposed_complete_image_rows")
    require(proposed == image, "proposed complete image is not operation reconstruction")
    changed_rows = [index for index, pair in enumerate(zip(baseline, proposed))
                    if pair[0] != pair[1]]
    require(changed_rows == op_rows,
            "complete proposed image changes rows outside exact operation allowlist")
    proposed_sha = packed_u32_sha256(proposed)
    require(proposed_sha == profile.get("proposed_complete_image_u32le_sha256"),
            "proposed complete-image hash mismatch")
    transition = profile.get("physical_transition_proof", {})
    for key in (
        "all_operations_are_zero_to_programmed_word",
        "all_target_rows_were_zero",
        "logical_clear_is_last-record-wins_not_physical_erasure",
        "no_existing_physical_row_is_modified",
    ):
        require(transition.get(key) is True, "missing physical transition proof: %s" % key)

    recovery_rows, recovery_records = validate_recovery(
        profile, baseline, proposed, iff_boundary, quarantined, set(op_rows)
    )
    rir_proof = profile.get("rir_preservation_proof", {})
    require(rir_proof.get("physical_rir_words_changed") is False,
            "profile changes RIR")
    require(rir_proof.get("baseline_rir_u32le_sha256") == rir_sha and
            rir_proof.get("proposed_rir_u32le_sha256") == rir_sha,
            "RIR preservation hash mismatch")

    baseline_frontier = parse_u32(
        baseline_obj.get("capacity", {}).get("ff_frontier_F"),
        "baseline FF frontier",
    )
    allocation = profile.get("allocation_policy", {})
    require(op_rows[0] == baseline_frontier,
            "operation allowlist does not start at measured baseline frontier")
    require(parse_u32(allocation.get("first_operation_row"), "allocation first row") == op_rows[0]
            and parse_u32(allocation.get("last_operation_row"), "allocation last row") == op_rows[-1],
            "allocation policy does not bind exact operation endpoints")
    require(parse_u32(allocation.get("active_iff_boundary_B"), "allocation IFF boundary") == iff_boundary,
            "allocation policy IFF boundary mismatch")
    final_capacity = profile.get("final_capacity", {})
    require(final_capacity.get("entire_interval_known") is True and
            final_capacity.get("frontier_complete") is True and
            final_capacity.get("reserve_satisfied") is True and
            final_capacity.get("allocation_proof_complete") is True,
            "final capacity proof is incomplete")
    require(parse_u32(final_capacity.get("ff_frontier_F"), "final FF frontier") == op_rows[-1] + 1,
            "final FF frontier does not follow exact allowlist")

    return {
        "candidate_digest": candidate_digest,
        "profile_id": profile_id,
        "source_was_legacy": source_was_legacy,
        "card": card,
        "profile": profile,
        "baseline": baseline,
        "rir": rir,
        "proposed": proposed,
        "baseline_sha": baseline_sha,
        "rir_sha": rir_sha,
        "proposed_sha": proposed_sha,
        "iff_boundary": iff_boundary,
        "quarantined_rows": sorted(quarantined),
        "operation_rows": op_rows,
        "operations": sealed_ops,
        "recovery_rows": recovery_rows,
        "recovery_records": recovery_records,
    }


def fresh_attestation(validated, main_path=None, rir_path=None):
    if (main_path is None) != (rir_path is None):
        raise SpecFailure("fresh MAIN and RIR must be supplied together")
    if main_path is None:
        return {
            "provided": False,
            "exact_main_match": False,
            "exact_rir_match": False,
            "complete": False,
            "blocker": "no same-session fresh complete MAIN/RIR baseline supplied",
        }
    main = read_u32le_image(main_path, MAIN_ROWS, "fresh MAIN")
    rir = read_u32le_image(rir_path, RIR_ROWS, "fresh RIR")
    main_match = main == validated["baseline"]
    rir_match = rir == validated["rir"]
    require(main_match, "fresh MAIN differs byte-for-byte from sealed baseline")
    require(rir_match, "fresh RIR differs byte-for-byte from sealed baseline")
    return {
        "provided": True,
        "main_path": str(Path(main_path).resolve()),
        "rir_path": str(Path(rir_path).resolve()),
        # Hashes are derived from the exact bytes already parsed and compared,
        # avoiding a time-of-check/time-of-use second file read.
        "main_file_sha256": packed_u32_sha256(main),
        "rir_file_sha256": packed_u32_sha256(rir),
        "exact_main_match": True,
        "exact_rir_match": True,
        "complete": True,
        "blocker": None,
    }


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def build_manifest(candidate_path, bdf, profile_id, main_path=None, rir_path=None,
                   generated_utc=None):
    bdf = normalize_bdf(bdf)
    candidate_file_sha = sha256_file(candidate_path)
    candidate = load_json(candidate_path)
    require(sha256_file(candidate_path) == candidate_file_sha,
            "candidate file changed while it was being parsed")
    validated = validate_candidate_profile(candidate, bdf, profile_id)
    profile_id = validated["profile_id"]
    attestation = fresh_attestation(validated, main_path, rir_path)
    profile = validated["profile"]

    hard_blockers = [
        "no electrically qualified CMP 170HX programming-source path, voltage, polarity, or external source-low proof",
        "no per-board qualified controller clock and program-pulse timing contract",
        "no implemented and validated privilege/security-lowering contract",
        "no validated resense, fundamental-reset, cold-boot, and recovery lifecycle",
        "no real kernel OTP ABI exists; generic write32 is prohibited",
        "manifest digests provide integrity binding but no operator/device trust-root signature",
        "offline FF coordinates and prefixes do not establish electrical safety or bootability",
    ]
    if not attestation["complete"]:
        hard_blockers.insert(0, attestation["blocker"])
    for blocker in profile.get("blockers", []):
        if blocker not in hard_blockers:
            hard_blockers.append(blocker)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tool": {"name": "170hx_otp_executor_spec", "version": TOOL_VERSION},
        "generated_utc": generated_utc or utc_now(),
        "candidate_source": {
            "path": str(Path(candidate_path).resolve()),
            "file_sha256": candidate_file_sha,
            "report_content_sha256_excluding_self": validated["candidate_digest"],
            "schema": CANDIDATE_SCHEMA,
            "legacy_input_normalized": validated["source_was_legacy"],
        },
        "binding": {
            "bdf": bdf,
            "profile_id": profile_id,
            "model_type": profile.get("model_type"),
            "identity": copy.deepcopy(validated["card"].get("identity_binding")),
            "baseline_main_u32le_sha256": validated["baseline_sha"],
            "baseline_rir_u32le_sha256": validated["rir_sha"],
            "proposed_main_u32le_sha256": validated["proposed_sha"],
            "exact_complete_image_required_at_preflight": True,
        },
        "images": {
            "baseline_main_rows": [hex32(word) for word in validated["baseline"]],
            "baseline_rir_rows": [hex32(word) for word in validated["rir"]],
            "proposed_main_rows": [hex32(word) for word in validated["proposed"]],
        },
        "allocation": {
            "iff_boundary": validated["iff_boundary"],
            "exact_row_allowlist": validated["operation_rows"],
            "exact_row_allowlist_sha256": canonical_sha256(validated["operation_rows"]),
            "quarantined_rows": validated["quarantined_rows"],
            "recovery_reserve_rows": validated["recovery_rows"],
            "recovery_records": validated["recovery_records"],
            "canary_or_noop_rows_forbidden": True,
        },
        "operations": validated["operations"],
        "fresh_baseline_attestation": attestation,
        "required_future_preflight": [
            "exclusive device ownership and driver unbound",
            "exact PCI identity and generation token match",
            "two complete coherent fresh MAIN passes equal sealed baseline/prefix",
            "two complete coherent fresh RIR passes equal sealed RIR",
            "clean CRC, controller idle, software override disabled, target row blank",
            "exact BDF/profile/image/row allowlist match",
            "durable journal and independent source-low watchdog armed",
            "qualified source/timing/security/resense/reset profile digest match",
            "trusted detached signature over manifest and hardware-profile digests",
        ],
        "required_future_row_lifecycle": [
            "durably journal preflight and one-attempt token",
            "assert qualified source and independently measure asserted state",
            "arm exactly one allowlisted blank row and exact word",
            "issue exactly one command; after this point outcome is uncertain until verified",
            "poll bounded completion without retry",
            "unconditionally lower source and independently prove low",
            "read exact row and complete image; compare expected prefix",
            "quarantine session/card on timeout, mismatch, cleanup failure, or lost journal",
        ],
        "required_future_postverify": [
            "source independently proven low",
            "two complete coherent MAIN/RIR captures and CRC verification",
            "complete decode and exact prefix comparison",
            "separate qualified resense/reset lifecycle",
            "cold-boot capture and feature/stability validation are separate gates",
        ],
        "operator_confirmations": [
            "exact BDF",
            "exact profile_id",
            "manifest content digest",
            "baseline MAIN digest",
            "explicit acknowledgement that review does not enable writes",
        ],
        "safety": {
            "real_hardware_backend_compiled": REAL_HARDWARE_BACKEND_COMPILED,
            "generic_write32_present": GENERIC_WRITE32_PRESENT,
            "one_attempt_per_row": True,
            "retry_after_command_issue": False,
            "source_low_cleanup_mandatory": True,
            "uncertain_outcome_requires_quarantine": True,
            "kernel_fault_injection_can_reach_hardware": False,
        },
        "hard_blockers": hard_blockers,
        "executable": False,
        "execution_authorized": False,
        "hardware_backend_invoked": False,
    }
    manifest["content_sha256_excluding_self"] = canonical_sha256(manifest)
    return manifest


def verify_manifest(manifest):
    require(manifest.get("schema") == MANIFEST_SCHEMA, "unsupported manifest schema")
    digest = verify_self_digest(manifest)
    require(manifest.get("executable") is False, "manifest may not be executable")
    require(manifest.get("execution_authorized") is False,
            "manifest may not authorize execution")
    require(manifest.get("hardware_backend_invoked") is False,
            "manifest falsely claims hardware backend invocation")
    safety = manifest.get("safety", {})
    require(safety.get("real_hardware_backend_compiled") is False,
            "real hardware backend must be absent")
    require(safety.get("generic_write32_present") is False,
            "generic write32 must be absent")
    require(safety.get("retry_after_command_issue") is False,
            "retry after command issue must be forbidden")
    binding = manifest.get("binding", {})
    bdf = normalize_bdf(binding.get("bdf", ""))
    require(isinstance(binding.get("profile_id"), str) and binding["profile_id"],
            "manifest profile binding missing")
    images = manifest.get("images", {})
    baseline = parse_words(images.get("baseline_main_rows"), MAIN_ROWS,
                           "manifest baseline MAIN")
    rir = parse_words(images.get("baseline_rir_rows"), RIR_ROWS,
                      "manifest baseline RIR")
    proposed = parse_words(images.get("proposed_main_rows"), MAIN_ROWS,
                           "manifest proposed MAIN")
    require(packed_u32_sha256(baseline) == binding.get("baseline_main_u32le_sha256"),
            "manifest baseline MAIN binding mismatch")
    require(packed_u32_sha256(rir) == binding.get("baseline_rir_u32le_sha256"),
            "manifest baseline RIR binding mismatch")
    require(packed_u32_sha256(proposed) == binding.get("proposed_main_u32le_sha256"),
            "manifest proposed MAIN binding mismatch")
    allowlist = manifest.get("allocation", {}).get("exact_row_allowlist")
    require(isinstance(allowlist, list) and allowlist,
            "manifest exact row allowlist missing")
    require(canonical_sha256(allowlist) ==
            manifest["allocation"].get("exact_row_allowlist_sha256"),
            "manifest row allowlist digest mismatch")
    require(len(allowlist) == len(set(allowlist)), "manifest row allowlist has duplicates")
    require(allowlist == list(range(allowlist[0], allowlist[0] + len(allowlist))),
            "manifest row allowlist is not contiguous and ordered")
    quarantined = manifest["allocation"].get("quarantined_rows", [])
    recovery = manifest["allocation"].get("recovery_reserve_rows", [])
    require(set(allowlist).isdisjoint(quarantined),
            "manifest allowlist overlaps quarantined rows")
    require(set(allowlist).isdisjoint(recovery) and set(quarantined).isdisjoint(recovery),
            "manifest recovery reserve overlaps allowlist/quarantine")
    operations = manifest.get("operations")
    require(isinstance(operations, list) and len(operations) == len(allowlist),
            "manifest operations/allowlist mismatch")
    for index, op in enumerate(operations):
        require(op.get("index") == index and op.get("row") == allowlist[index],
                "manifest operation order does not match allowlist")
        row = allowlist[index]
        old = parse_u32(op.get("expected_old_word"), "manifest expected old")
        word = parse_u32(op.get("program_word"), "manifest program word")
        require(old == 0 and baseline[row] == 0 and proposed[row] == word,
                "manifest operation does not bind blank baseline to proposed image")
        require(op.get("one_attempt_only") is True and
                op.get("retry_forbidden_after_command_issue") is True,
                "manifest operation lacks one-attempt/no-retry interlock")
    changed_rows = [index for index, pair in enumerate(zip(baseline, proposed))
                    if pair[0] != pair[1]]
    require(changed_rows == allowlist,
            "manifest image diff is not exactly the sealed row allowlist")
    return {"digest": digest, "bdf": bdf, "baseline": baseline,
            "rir": rir, "proposed": proposed}


def atomic_write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%s" % uuid.uuid4().hex)
    payload = strict_json_dumps(value, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(temporary), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) provides create-only publication: an existing destination is
        # never silently replaced, even under a race between review processes.
        os.link(str(temporary), str(path))
        temporary.unlink()
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def collect_review_receipt(manifest, input_func=input, generated_utc=None):
    verified = verify_manifest(manifest)
    binding = manifest["binding"]
    digest = verified["digest"]
    expected = [
        ("Type exact BDF", binding["bdf"]),
        ("Type exact profile", binding["profile_id"]),
        ("Type manifest content digest", digest),
        ("Type baseline MAIN digest", binding["baseline_main_u32le_sha256"]),
        ("Type acknowledgement",
         "I UNDERSTAND THIS RECEIPT DOES NOT ENABLE HARDWARE WRITES"),
    ]
    confirmations = []
    for prompt, wanted in expected:
        got = input_func("%s [%s]: " % (prompt, wanted))
        require(got == wanted, "confirmation mismatch for %s" % prompt)
        confirmations.append({"prompt": prompt, "exact_match": True})
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "generated_utc": generated_utc or utc_now(),
        "manifest_content_sha256": digest,
        "binding": {
            "bdf": binding["bdf"],
            "profile_id": binding["profile_id"],
            "baseline_main_u32le_sha256": binding["baseline_main_u32le_sha256"],
        },
        "confirmations": confirmations,
        "review_complete": True,
        "execution_enabled": False,
        "hardware_backend_invoked": False,
    }
    receipt["content_sha256_excluding_self"] = canonical_sha256(receipt)
    return receipt


class DurableJournal(object):
    """Create-only, fsync-per-record, hash-chained simulation journal."""

    def __init__(self, path, manifest_digest, bdf, profile_id, session_id=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or uuid.uuid4().hex
        self.manifest_digest = manifest_digest
        self.bdf = bdf
        self.profile_id = profile_id
        self.sequence = 0
        self.previous = "0" * 64
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        self.fd = os.open(str(self.path), flags, 0o600)
        directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def append(self, state, event, row=None, word=None, detail=None):
        record = {
            "schema": JOURNAL_SCHEMA,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "utc": utc_now(),
            "manifest_content_sha256": self.manifest_digest,
            "bdf": self.bdf,
            "profile_id": self.profile_id,
            "state": state,
            "event": event,
            "row": row,
            "word": word,
            "detail": detail,
            "previous_event_sha256": self.previous,
        }
        record["event_sha256"] = canonical_sha256(record)
        payload = canonical_json_bytes(record) + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise SpecFailure("short/failed durable journal write")
            view = view[written:]
        os.fsync(self.fd)
        self.previous = record["event_sha256"]
        self.sequence += 1
        return record

    def close(self):
        if self.fd is not None:
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


FAULT_POINTS = (
    "none",
    "before-preflight",
    "after-preflight",
    "after-watchdog-arm",
    "after-source-assert",
    "after-row-arm",
    "after-attempt-commit-before-command",
    "after-command-issue-after-effect",
    "poll-timeout",
    "source-low-failure",
    "readback-mismatch",
    "full-image-mismatch",
)


class InMemoryExecutor(object):
    """Hardware-inert reference state machine with explicit fault boundaries."""

    def __init__(self, manifest, journal):
        verified = verify_manifest(manifest)
        self.manifest = manifest
        self.journal = journal
        self.image = list(verified["baseline"])
        self.rir = list(verified["rir"])
        self.source = "PROVEN_LOW"
        self.watchdog_armed = False
        self.command_attempts = {}
        self.terminal = False
        self.uncertain = False

    def _inject(self, selected, point):
        if selected == point:
            raise InjectedFault("injected at %s" % point)

    def _lower_source(self, fault):
        if self.source == "PROVEN_LOW":
            return
        self.journal.append("CLEANUP", "source-low-commanded")
        if fault == "source-low-failure":
            self.source = "UNKNOWN"
            self.journal.append("CLEANUP_FAILED", "source-low-not-proven")
            raise InjectedFault("source-low cleanup not proven")
        self.source = "PROVEN_LOW"
        self.watchdog_armed = False
        self.journal.append("SOURCE_LOW", "source-low-independently-proven")

    def run(self, fault="none"):
        require(fault in FAULT_POINTS, "unknown simulator fault point")
        operations = self.manifest["operations"]
        allowlist = self.manifest["allocation"]["exact_row_allowlist"]
        self.journal.append("CREATED", "session-created", detail={
            "hardware_backend": False,
            "fault_injection": fault,
            "exact_allowlist": allowlist,
        })
        try:
            self._inject(fault, "before-preflight")
            for op in operations:
                row = op["row"]
                word = parse_u32(op["program_word"], "simulator program word")
                expected_prefix = op["expected_prefix_main_u32le_sha256"]
                require(not self.terminal, "terminal session cannot continue")
                require(row in allowlist, "row outside exact manifest allowlist")
                require(self.image[row] == 0, "target row is not blank at preflight")
                require(self.command_attempts.get(row, 0) == 0,
                        "one-attempt token already consumed")
                self.journal.append("PREFLIGHTED", "fresh-complete-image-match",
                                    row=row, word=hex32(word), detail={
                                        "current_main_u32le_sha256": packed_u32_sha256(self.image),
                                        "target_blank": True,
                                    })
                self._inject(fault, "after-preflight")

                self.watchdog_armed = True
                self.journal.append("WATCHDOG_ARMED", "independent-source-low-watchdog-armed",
                                    row=row)
                self._inject(fault, "after-watchdog-arm")
                self.source = "ASSERTED"
                self.journal.append("SOURCE_ASSERTED", "source-asserted-and-measured",
                                    row=row)
                self._inject(fault, "after-source-assert")
                self.journal.append("ROW_ARMED", "exact-row-word-armed",
                                    row=row, word=hex32(word))
                self._inject(fault, "after-row-arm")

                # Durably consume the only attempt *before* the hardware command
                # boundary.  A crash in the following gap wastes the attempt
                # but cannot create an unsafe automatic retry.
                self.command_attempts[row] = 1
                self.uncertain = True
                self.journal.append("ATTEMPT_COMMITTED", "one-attempt-token-consumed",
                                    row=row, word=hex32(word))
                self._inject(fault, "after-attempt-commit-before-command")
                self.journal.append("COMMAND_ISSUED", "hardware-inert-command-modeled",
                                    row=row, word=hex32(word))
                self.image[row] |= word
                self._inject(fault, "after-command-issue-after-effect")
                self._inject(fault, "poll-timeout")
                self.journal.append("COMMAND_COMPLETE", "bounded-poll-complete",
                                    row=row, word=hex32(word))
                self._lower_source(fault)

                if fault == "readback-mismatch":
                    raise InjectedFault("exact-row readback mismatch")
                require(self.image[row] == word, "exact-row postverify mismatch")
                actual = packed_u32_sha256(self.image)
                if fault == "full-image-mismatch":
                    raise InjectedFault("complete-image postverify mismatch")
                require(actual == expected_prefix, "complete prefix postverify mismatch")
                self.uncertain = False
                self.journal.append("ROW_VERIFIED", "row-and-complete-prefix-match",
                                    row=row, word=hex32(word), detail={
                                        "main_u32le_sha256": actual,
                                        "source": self.source,
                                    })
            require(self.source == "PROVEN_LOW", "source is not proven low at completion")
            require(self.image == [parse_u32(word, "proposed image")
                                   for word in self.manifest["images"]["proposed_main_rows"]],
                    "simulator final image mismatch")
            self.terminal = True
            self.journal.append("COMPLETE", "simulation-complete", detail={
                "hardware_backend": False,
                "command_attempts": self.command_attempts,
                "final_main_u32le_sha256": packed_u32_sha256(self.image),
            })
            return "COMPLETE"
        except (InjectedFault, SpecFailure) as exc:
            cleanup_error = None
            try:
                self._lower_source(fault)
            except (InjectedFault, SpecFailure) as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            self.terminal = True
            terminal_state = "QUARANTINED" if self.uncertain or cleanup_error else "ABORTED"
            self.journal.append(terminal_state, "simulation-stopped-no-retry", detail={
                "reason": str(exc),
                "cleanup_error": cleanup_error,
                "uncertain_outcome": self.uncertain,
                "source": self.source,
                "command_attempts": self.command_attempts,
            })
            return terminal_state


def verify_journal(path, expected_manifest_digest=None):
    previous = "0" * 64
    records = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SpecFailure("cannot read journal: %s" % exc) from exc
    require(lines, "journal is empty")
    immutable = None
    for sequence, line in enumerate(lines):
        try:
            record = strict_json_loads(line, object_pairs_hook=reject_duplicate_json_keys)
        except ValueError as exc:
            raise SpecFailure("invalid journal JSON at line %d" % (sequence + 1)) from exc
        require(record.get("schema") == JOURNAL_SCHEMA, "journal schema mismatch")
        require(record.get("sequence") == sequence, "journal sequence discontinuity")
        require(record.get("previous_event_sha256") == previous,
                "journal hash chain discontinuity")
        claimed = record.get("event_sha256")
        unsigned = copy.deepcopy(record)
        del unsigned["event_sha256"]
        require(canonical_sha256(unsigned) == claimed, "journal event digest mismatch")
        if expected_manifest_digest is not None:
            require(record.get("manifest_content_sha256") == expected_manifest_digest,
                    "journal manifest binding mismatch")
        current_immutable = (
            record.get("session_id"), record.get("manifest_content_sha256"),
            record.get("bdf"), record.get("profile_id"),
        )
        if immutable is None:
            immutable = current_immutable
        else:
            require(current_immutable == immutable,
                    "journal session/BDF/profile binding changed")
        previous = claimed
        records.append(record)
    require(records[0].get("state") == "CREATED",
            "journal does not begin with CREATED")
    terminal_states = ("COMPLETE", "ABORTED", "QUARANTINED")
    require(records[-1].get("state") in terminal_states,
            "journal lacks terminal state")
    require(all(record.get("state") not in terminal_states for record in records[:-1]),
            "journal contains a non-final terminal state")

    attempt_counts = {}
    command_counts = {}
    armed_at = {}
    attempt_at = {}
    command_at = {}
    verified_at = {}
    words = {}
    source_asserted = False
    active_source_row = None
    for record in records:
        sequence = record["sequence"]
        state = record.get("state")
        row = record.get("row")
        word = record.get("word")
        if state == "SOURCE_ASSERTED":
            require(not source_asserted, "source asserted twice without proven low")
            require(row is not None, "source assertion lacks row binding")
            source_asserted = True
            active_source_row = row
        elif state == "ROW_ARMED":
            require(source_asserted and row == active_source_row,
                    "row armed without matching asserted source")
            require(row not in armed_at, "row armed more than once")
            armed_at[row] = sequence
            words[row] = word
        if record.get("state") == "ATTEMPT_COMMITTED":
            require(source_asserted and row == active_source_row,
                    "attempt committed without matching asserted source")
            require(row in armed_at and armed_at[row] < sequence,
                    "attempt committed before exact row was armed")
            require(words[row] == word, "attempt word differs from armed word")
            attempt_counts[row] = attempt_counts.get(row, 0) + 1
            attempt_at[row] = sequence
        if record.get("state") == "COMMAND_ISSUED":
            require(source_asserted and row == active_source_row,
                    "command issued without matching asserted source")
            require(row in attempt_at and attempt_at[row] < sequence,
                    "command issued before durable attempt commitment")
            require(words[row] == word, "command word differs from armed word")
            command_counts[row] = command_counts.get(row, 0) + 1
            command_at[row] = sequence
        elif state == "SOURCE_LOW":
            require(source_asserted, "source-low proof without prior assertion")
            source_asserted = False
            active_source_row = None
        elif state == "ROW_VERIFIED":
            require(not source_asserted, "row verified before source was proven low")
            require(row in command_at and command_at[row] < sequence,
                    "row verified without prior command issue")
            require(words[row] == word, "verified word differs from armed word")
            require(row not in verified_at, "row verified more than once")
            verified_at[row] = sequence
    require(all(count == 1 for count in command_counts.values()),
            "journal contains a retry/multiple command issue")
    require(all(count == 1 for count in attempt_counts.values()),
            "journal contains a reused/multiple attempt token")
    require(set(command_counts).issubset(attempt_counts),
            "journal contains command without durable attempt commitment")
    if records[-1].get("state") == "COMPLETE":
        require(not source_asserted, "COMPLETE while source remains asserted")
        require(set(attempt_counts) == set(command_counts) == set(verified_at),
                "COMPLETE without matching attempts, commands, and verification")
        final_sha = records[-1].get("detail", {}).get("final_main_u32le_sha256")
        require(isinstance(final_sha, str) and len(final_sha) == 64,
                "COMPLETE lacks final complete-image digest")
    elif records[-1].get("state") == "ABORTED":
        require(not source_asserted, "ABORTED without source-low proof")
    return {
        "records": len(records),
        "terminal_state": records[-1]["state"],
        "last_event_sha256": previous,
        "attempt_counts": attempt_counts,
        "command_counts": command_counts,
    }


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    compile_parser = sub.add_parser("compile", help="emit sealed non-executable manifest")
    compile_parser.add_argument("--candidate", required=True)
    compile_parser.add_argument("--bdf", required=True)
    compile_parser.add_argument("--profile", required=True)
    compile_parser.add_argument("--fresh-main-bin")
    compile_parser.add_argument("--fresh-rir-bin")
    compile_parser.add_argument("--output", required=True)

    verify_parser = sub.add_parser("verify", help="verify sealed manifest")
    verify_parser.add_argument("--manifest", required=True)

    review_parser = sub.add_parser("review", help="interactive review receipt; never enables writes")
    review_parser.add_argument("--manifest", required=True)
    review_parser.add_argument("--output", required=True)

    simulate_parser = sub.add_parser("simulate", help="hardware-inert state-machine simulation")
    simulate_parser.add_argument("--manifest", required=True)
    simulate_parser.add_argument("--journal", required=True)
    simulate_parser.add_argument("--fault", choices=FAULT_POINTS, default="none")

    journal_parser = sub.add_parser("verify-journal", help="verify durable journal chain")
    journal_parser.add_argument("--journal", required=True)
    journal_parser.add_argument("--manifest")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "compile":
        manifest = build_manifest(
            args.candidate, args.bdf, args.profile,
            args.fresh_main_bin, args.fresh_rir_bin,
        )
        atomic_write_json(args.output, manifest)
        print(strict_json_dumps({
            "status": "sealed_non_executable_manifest_written",
            "output": str(Path(args.output).resolve()),
            "content_sha256": manifest["content_sha256_excluding_self"],
            "hard_blocker_count": len(manifest["hard_blockers"]),
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    if args.command == "verify":
        manifest = load_json(args.manifest)
        verified = verify_manifest(manifest)
        print(strict_json_dumps({
            "status": "valid_non_executable_manifest",
            "content_sha256": verified["digest"],
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    if args.command == "review":
        require(sys.stdin.isatty(), "review requires an interactive TTY")
        receipt = collect_review_receipt(load_json(args.manifest))
        atomic_write_json(args.output, receipt)
        print(strict_json_dumps({
            "status": "review_receipt_written_no_execution_enabled",
            "output": str(Path(args.output).resolve()),
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0
    if args.command == "simulate":
        manifest = load_json(args.manifest)
        verified = verify_manifest(manifest)
        with DurableJournal(
            args.journal, verified["digest"], manifest["binding"]["bdf"],
            manifest["binding"]["profile_id"],
        ) as journal:
            state = InMemoryExecutor(manifest, journal).run(args.fault)
        result = verify_journal(args.journal, verified["digest"])
        print(strict_json_dumps({
            "status": state,
            "journal": result,
            "hardware_backend_invoked": False,
        }, sort_keys=True))
        return 0 if state == "COMPLETE" else 2
    if args.command == "verify-journal":
        digest = None
        if args.manifest:
            digest = verify_manifest(load_json(args.manifest))["digest"]
        print(strict_json_dumps(verify_journal(args.journal, digest), sort_keys=True))
        return 0
    raise SpecFailure("unreachable command")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SpecFailure as exc:
        print("refused: %s" % exc, file=sys.stderr)
        sys.exit(2)
