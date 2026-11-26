#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native-LP64 userspace binding for the constrained XRAY thermal helper."""

from __future__ import print_function

import ctypes
import fcntl
import os


ABI_VERSION = 5
MAX_CARDS = 16
AGGREGATE_COUNT = 8
TS_AUTO_COUNT = 8
FBPA_COUNT = 24
GPC_COUNT = 8
BJT_COUNT = 3
GPC_BJT_COUNT = 24
SENTINEL_COUNT = 2
SNAPSHOT_AGGREGATE_COUNT = 4
MAPPED_BAR0_LENGTH = 0x00A00000

CAP_PURE_CAPTURE = 1 << 0
CAP_SEMANTIC_SNAPSHOT = 1 << 1
CAP_BOUND_PURE_CAPTURE = 1 << 2
CAP_BOUND_SNAPSHOT = 1 << 3
CAP_STATEFUL_HBM_DATA = 1 << 4
CAP_PASSIVE_SELECTOR_OMISSION = 1 << 5
CAP_FRESH_PREWRITE_LIVENESS = 1 << 6
KNOWN_CAPABILITIES = (
    CAP_PURE_CAPTURE | CAP_SEMANTIC_SNAPSHOT | CAP_BOUND_PURE_CAPTURE |
    CAP_BOUND_SNAPSHOT | CAP_STATEFUL_HBM_DATA |
    CAP_PASSIVE_SELECTOR_OMISSION | CAP_FRESH_PREWRITE_LIVENESS
)
REQUIRED_CAPABILITIES = KNOWN_CAPABILITIES & ~CAP_BOUND_SNAPSHOT
SUPPORTED_DEVICE_IDS = frozenset((0x20B5, 0x20C2, 0x2080))

CAPTURE_F_SNAPSHOT = 1 << 0
CAPTURE_F_ALLOW_BOUND = 1 << 1
CAPTURE_F_HBM_DATA = 1 << 2

IOC_INFO = 0x85A05401
IOC_CAPTURE = 0xC7005402


class ThermError(RuntimeError):
    pass


class Identity(ctypes.Structure):
    _fields_ = [
        ("generation", ctypes.c_uint64),
        ("domain", ctypes.c_uint16),
        ("bus", ctypes.c_uint8),
        ("devfn", ctypes.c_uint8),
        ("vendor", ctypes.c_uint16),
        ("device", ctypes.c_uint16),
        ("subvendor", ctypes.c_uint16),
        ("subdevice", ctypes.c_uint16),
        ("revision", ctypes.c_uint8),
        ("reserved0", ctypes.c_uint8 * 7),
    ]


class CardInfo(ctypes.Structure):
    _fields_ = [
        ("identity", Identity),
        ("bar0_start", ctypes.c_uint64),
        ("bar0_length", ctypes.c_uint64),
        ("mapped_length", ctypes.c_uint64),
        ("resource_flags", ctypes.c_uint64),
        ("driver_bound", ctypes.c_uint32),
        ("current_state", ctypes.c_uint32),
        ("runtime_status", ctypes.c_uint32),
        ("reserved0", ctypes.c_uint32 * 3),
    ]


class Info(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("count", ctypes.c_uint32),
        ("max_cards", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("reserved0", ctypes.c_uint32 * 3),
        ("cards", CardInfo * MAX_CARDS),
    ]


class FbpaRaw(ctypes.Structure):
    _fields_ = [
        ("cfg1", ctypes.c_uint32),
        ("test_macro", ctypes.c_uint32),
        ("instr", ctypes.c_uint32),
        ("mode", ctypes.c_uint32),
        ("data", ctypes.c_uint32),
        ("shwir", ctypes.c_uint32),
        ("shwdr", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
    ]


class GpcAnalogRaw(ctypes.Structure):
    _fields_ = [
        ("tsense", ctypes.c_uint32),
        ("adjustment", ctypes.c_uint32),
        ("raw_override", ctypes.c_uint32),
        ("ts_auto", ctypes.c_uint32),
    ]


class FixedContextRaw(ctypes.Structure):
    _fields_ = [
        ("sys_sensor0", ctypes.c_uint32),
        ("sys_sensor1", ctypes.c_uint32),
        ("sys_sensor2", ctypes.c_uint32),
        ("sys_sensor3", ctypes.c_uint32),
        ("sys_sensor4", ctypes.c_uint32),
        ("sys_sensor6", ctypes.c_uint32),
        ("sys_sensor7", ctypes.c_uint32),
        ("dedicated_overt_max", ctypes.c_uint32),
        ("i2cs_sensor00", ctypes.c_uint32),
        ("i2cs_sensor11", ctypes.c_uint32),
        ("i2cs_sensor13", ctypes.c_uint32),
        ("i2cs_sensor14", ctypes.c_uint32),
        ("sensor8", ctypes.c_uint32),
        ("sensor9", ctypes.c_uint32),
        ("statistics_temp_avg", ctypes.c_uint32),
        ("statistics_ctrl", ctypes.c_uint32),
        ("gpc_tsense_hs_offset_index", ctypes.c_uint32),
        ("gpc_tsense_override_index", ctypes.c_uint32),
        ("gpc_tsense_override", ctypes.c_uint32),
        ("gpc_tsense_global_override", ctypes.c_uint32),
        ("gpc_tsense_hs_offset", ctypes.c_uint32),
        ("sci_smbus_cmd00", ctypes.c_uint32),
        ("sci_overt_ts_sm", ctypes.c_uint32),
        ("sci_overt_ts_adj", ctypes.c_uint32),
        ("sci_overt_thresh", ctypes.c_uint32),
        ("sci_overt_fusectrl", ctypes.c_uint32),
        ("sci_overt_fuse", ctypes.c_uint32),
        ("sci_overt_temp_coeff_nom", ctypes.c_uint32),
        ("sci_overt_temp_coeff_final", ctypes.c_uint32),
        ("sci_overt_ts_adc", ctypes.c_uint32),
        ("sci_overt_temperature", ctypes.c_uint32),
    ]


class GpcBjtRaw(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("selector_before", ctypes.c_uint32),
        ("temperature", ctypes.c_uint32),
        ("offset_temperature", ctypes.c_uint32),
        ("selector_after", ctypes.c_uint32),
    ]


class Capture(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("identity", Identity),
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_int32),
        ("reads_issued", ctypes.c_uint32),
        ("writes_issued", ctypes.c_uint32),
        ("driver_bound_at_entry", ctypes.c_uint32),
        ("pure_completed", ctypes.c_uint32),
        ("snapshot_completed", ctypes.c_uint32),
        ("selector_restored", ctypes.c_uint32),
        ("selector_race", ctypes.c_uint32),
        ("autopoll_unchanged", ctypes.c_uint32),
        ("sentinels_unchanged", ctypes.c_uint32),
        ("trigger_self_cleared", ctypes.c_uint32),
        ("failed_gpc", ctypes.c_uint32),
        ("failed_bjt", ctypes.c_uint32),
        ("aggregate", ctypes.c_uint32 * AGGREGATE_COUNT),
        ("ts_auto_entry", ctypes.c_uint32 * TS_AUTO_COUNT),
        ("ts_auto_exit", ctypes.c_uint32 * TS_AUTO_COUNT),
        ("current_selector", ctypes.c_uint32),
        ("current_temperature", ctypes.c_uint32),
        ("current_offset_temperature", ctypes.c_uint32),
        ("current_raw_code", ctypes.c_uint32),
        ("snapshot_selector_entry", ctypes.c_uint32),
        ("snapshot_selector_exit", ctypes.c_uint32),
        ("snapshot_aggregate",
         ctypes.c_uint32 * SNAPSHOT_AGGREGATE_COUNT),
        ("sentinels_entry", ctypes.c_uint32 * SENTINEL_COUNT),
        ("sentinels_exit", ctypes.c_uint32 * SENTINEL_COUNT),
        ("fixed_context", FixedContextRaw),
        ("gpc_analog", GpcAnalogRaw * GPC_COUNT),
        ("fbpa", FbpaRaw * FBPA_COUNT),
        ("gpc_bjt", GpcBjtRaw * GPC_BJT_COUNT),
        ("opt_ts_dis_mask", ctypes.c_uint32),
        ("status_opt_gpc", ctypes.c_uint32),
        ("snapshot_as_found_selector_before", ctypes.c_uint32),
        ("snapshot_as_found_temperature", ctypes.c_uint32),
        ("snapshot_as_found_offset_temperature", ctypes.c_uint32),
        ("snapshot_as_found_selector_after", ctypes.c_uint32),
        ("status_opt_fbio", ctypes.c_uint32),
        ("selector_dependent_skipped_mask", ctypes.c_uint32),
        ("hbm_data_read_mask", ctypes.c_uint32),
        ("prewrite_tsense", ctypes.c_uint32),
        ("prewrite_active_fbps", ctypes.c_uint32),
    ]


EXPECTED_LAYOUT = {
    "identity": (Identity, 32),
    "card_info": (CardInfo, 88),
    "info": (Info, 1440),
    "fbpa": (FbpaRaw, 32),
    "gpc_analog": (GpcAnalogRaw, 16),
    "fixed_context": (FixedContextRaw, 124),
    "gpc_bjt": (GpcBjtRaw, 20),
    "capture": (Capture, 1792),
}


def assert_layout():
    mismatches = []
    for name, (kind, expected) in EXPECTED_LAYOUT.items():
        actual = ctypes.sizeof(kind)
        if actual != expected:
            mismatches.append("%s=%d expected=%d" % (name, actual, expected))
    offsets = {
        "info.cards": (Info.cards.offset, 32),
        "capture.aggregate": (Capture.aggregate.offset, 96),
        "capture.fixed_context": (Capture.fixed_context.offset, 248),
        "capture.gpc_analog": (Capture.gpc_analog.offset, 372),
        "capture.fbpa": (Capture.fbpa.offset, 500),
        "capture.gpc_bjt": (Capture.gpc_bjt.offset, 1268),
        "capture.opt_ts_dis_mask": (Capture.opt_ts_dis_mask.offset, 1748),
        "capture.snapshot_as_found_selector_before": (
            Capture.snapshot_as_found_selector_before.offset, 1756
        ),
        "capture.status_opt_fbio": (Capture.status_opt_fbio.offset, 1772),
        "capture.selector_dependent_skipped_mask": (
            Capture.selector_dependent_skipped_mask.offset, 1776
        ),
        "capture.hbm_data_read_mask": (
            Capture.hbm_data_read_mask.offset, 1780
        ),
        "capture.prewrite_tsense": (Capture.prewrite_tsense.offset, 1784),
        "capture.prewrite_active_fbps": (
            Capture.prewrite_active_fbps.offset, 1788
        ),
    }
    for name, (actual, expected) in offsets.items():
        if actual != expected:
            mismatches.append("%s=%d expected=%d" % (name, actual, expected))
    if mismatches:
        raise ThermError("thermal helper ABI layout mismatch: %s" % ", ".join(mismatches))


def bdf(identity):
    return "%04x:%02x:%02x.%x" % (
        int(identity.domain), int(identity.bus),
        int(identity.devfn) >> 3, int(identity.devfn) & 7,
    )


def identity_dict(identity):
    return {
        "generation": "0x%016x" % int(identity.generation),
        "bdf": bdf(identity),
        "domain": int(identity.domain),
        "bus": int(identity.bus),
        "device": int(identity.devfn) >> 3,
        "function": int(identity.devfn) & 7,
        "vendor_id": "0x%04x" % int(identity.vendor),
        "device_id": "0x%04x" % int(identity.device),
        "subsystem_vendor_id": "0x%04x" % int(identity.subvendor),
        "subsystem_device_id": "0x%04x" % int(identity.subdevice),
        "revision": "0x%02x" % int(identity.revision),
    }


def _ioctl_structure(fd, command, value):
    raw = bytearray(ctypes.string_at(ctypes.addressof(value), ctypes.sizeof(value)))
    fcntl.ioctl(fd, command, raw, True)
    return value.__class__.from_buffer_copy(raw)


def _all_zero(array):
    return all(int(value) == 0 for value in array)


def _selector_is_valid(raw):
    raw = int(raw) & 0xFFFFFFFF
    allowed = 0x400F000F
    return ((raw & ~allowed) == 0 and
            ((raw >> 16) & 0xF) <= 7 and (raw & 0xF) <= 2)


def _has_affirmative_liveness(tsense, active_fbps):
    tsense = int(tsense) & 0xFFFFFFFF
    active_fbps = int(active_fbps) & 0xFFFFFFFF
    return (((tsense >> 29) & 0x3) in (1, 2) and
            (tsense & 0xFFFF0000) != 0xBADF0000 and
            tsense != 0xFFFFFFFF and
            (active_fbps & ~0x1F) == 0 and
            1 <= (active_fbps & 0x1F) <= 12)


def _validate_info(info):
    if (int(info.abi_version) != ABI_VERSION or
            int(info.struct_size) != ctypes.sizeof(Info) or
            int(info.max_cards) != MAX_CARDS or
            not 1 <= int(info.count) <= MAX_CARDS):
        raise ThermError("thermal helper returned an incompatible INFO")
    capabilities = int(info.capabilities)
    if capabilities & ~KNOWN_CAPABILITIES:
        raise ThermError("thermal helper returned unknown INFO capabilities")
    if capabilities & REQUIRED_CAPABILITIES != REQUIRED_CAPABILITIES:
        raise ThermError("thermal helper lacks required ABI5 capabilities")
    if not _all_zero(info.reserved0):
        raise ThermError("thermal helper returned nonzero INFO reserved words")
    bdfs = set()
    generation = None
    for index in range(int(info.count)):
        card = info.cards[index]
        identity = card.identity
        identity_generation = int(identity.generation)
        identity_bdf = (
            int(identity.domain), int(identity.bus), int(identity.devfn)
        )
        if (not identity_generation or int(identity.vendor) != 0x10DE or
                int(identity.device) not in SUPPORTED_DEVICE_IDS or
                not _all_zero(identity.reserved0) or
                not _all_zero(card.reserved0) or
                int(card.driver_bound) not in (0, 1) or
                int(card.bar0_start) == 0 or
                int(card.bar0_length) < MAPPED_BAR0_LENGTH or
                int(card.mapped_length) != MAPPED_BAR0_LENGTH or
                identity_bdf in bdfs or
                (generation is not None and
                 identity_generation != generation)):
            raise ThermError("thermal helper returned malformed card INFO")
        if generation is None:
            generation = identity_generation
        bdfs.add(identity_bdf)
    zero_card = bytes(ctypes.sizeof(CardInfo))
    for index in range(int(info.count), MAX_CARDS):
        raw = ctypes.string_at(
            ctypes.addressof(info.cards[index]), ctypes.sizeof(CardInfo)
        )
        if raw != zero_card:
            raise ThermError("thermal helper returned nonzero unused card INFO")


def _validate_capture_response(request, result):
    if (int(result.abi_version) != ABI_VERSION or
            int(result.struct_size) != ctypes.sizeof(Capture) or
            int(result.flags) != int(request.flags)):
        raise ThermError("thermal helper returned incompatible capture metadata")
    categorical = (
        "driver_bound_at_entry", "pure_completed", "snapshot_completed",
        "selector_restored", "selector_race", "autopoll_unchanged",
        "sentinels_unchanged", "trigger_self_cleared",
    )
    if any(int(getattr(result, field)) not in (0, 1) for field in categorical):
        raise ThermError("thermal helper returned noncanonical capture flags")
    if int(result.status) > 0:
        raise ThermError("thermal helper returned a positive capture status")
    failed_gpc = int(result.failed_gpc)
    failed_bjt = int(result.failed_bjt)
    if not ((failed_gpc == 0xFFFFFFFF and failed_bjt == 0xFFFFFFFF) or
            (0 <= failed_gpc <= 7 and 0 <= failed_bjt <= 2)):
        raise ThermError("thermal helper returned invalid failed endpoint indices")
    if int(result.selector_dependent_skipped_mask) != 0xF:
        raise ThermError("thermal helper violated passive selector omission")
    hbm_mask = int(result.hbm_data_read_mask)
    if hbm_mask & ~0xFFFFFF:
        raise ThermError("thermal helper returned an invalid HBM DATA mask")
    if not int(request.flags) & CAPTURE_F_HBM_DATA and hbm_mask != 0:
        raise ThermError("thermal helper observed unacknowledged HBM DATA ports")
    for fbpa in range(FBPA_COUNT):
        if not hbm_mask & (1 << fbpa) and int(result.fbpa[fbpa].data) != 0:
            raise ThermError(
                "thermal helper populated an unobserved HBM DATA slot"
            )
    omitted = (
        result.current_temperature, result.current_offset_temperature,
        result.current_raw_code, result.snapshot_as_found_temperature,
        result.snapshot_as_found_offset_temperature,
        result.fixed_context.gpc_tsense_override,
        result.fixed_context.gpc_tsense_hs_offset,
    )
    if any(int(value) != 0 for value in omitted):
        raise ThermError("thermal helper populated unconditionally omitted payloads")
    if not int(request.flags) & CAPTURE_F_SNAPSHOT:
        semantic_words = (
            int(result.snapshot_selector_entry),
            int(result.snapshot_selector_exit),
            int(result.prewrite_tsense),
            int(result.prewrite_active_fbps),
        )
        semantic_arrays_zero = (
            all(int(value) == 0 for value in result.snapshot_aggregate) and
            all(bytes(item) == bytes(ctypes.sizeof(GpcBjtRaw))
                for item in result.gpc_bjt)
        )
        if (int(result.writes_issued) != 0 or
                int(result.snapshot_completed) != 0 or
                int(result.selector_restored) != 0 or
                int(result.selector_race) != 0 or
                int(result.trigger_self_cleared) != 0 or
                failed_gpc != 0xFFFFFFFF or failed_bjt != 0xFFFFFFFF or
                any(semantic_words) or not semantic_arrays_zero or hbm_mask != 0):
            raise ThermError("thermal helper pure capture contains semantic state")
        if int(result.status) == 0 and (
                int(result.pure_completed) != 1 or
                int(result.reads_issued) != 255 or
                int(result.autopoll_unchanged) != 1 or
                int(result.sentinels_unchanged) != 1):
            raise ThermError("thermal helper pure success journal is inconsistent")
    elif int(result.snapshot_completed) == 1:
        fixed_reads = 409 if int(request.flags) & CAPTURE_F_HBM_DATA else 385
        polls = int(result.reads_issued) - fixed_reads
        selector_entry = int(result.snapshot_selector_entry)
        selector_exit = int(result.snapshot_selector_exit)
        endpoint_journal_ok = True
        for index, sample in enumerate(result.gpc_bjt):
            gpc, bjt = divmod(index, BJT_COUNT)
            expected_selector = (gpc << 16) | bjt
            if (int(sample.selector) != expected_selector or
                    int(sample.selector_before) != expected_selector or
                    int(sample.selector_after) != expected_selector):
                endpoint_journal_ok = False
                break
        if (int(result.status) != 0 or int(result.pure_completed) != 1 or
                int(result.writes_issued) != 26 or
                not 1 <= polls <= 1000 or
                int(result.selector_restored) != 1 or
                int(result.selector_race) != 0 or
                int(result.autopoll_unchanged) != 1 or
                int(result.sentinels_unchanged) != 1 or
                int(result.trigger_self_cleared) != 1 or
                selector_entry != selector_exit or
                not _selector_is_valid(selector_entry) or
                not _has_affirmative_liveness(
                    result.prewrite_tsense, result.prewrite_active_fbps) or
                list(result.ts_auto_entry) != list(result.ts_auto_exit) or
                list(result.sentinels_entry) != list(result.sentinels_exit) or
                not endpoint_journal_ok or
                failed_gpc != 0xFFFFFFFF or failed_bjt != 0xFFFFFFFF or
                (int(request.flags) & CAPTURE_F_HBM_DATA and
                 hbm_mask != 0xFFFFFF) or
                (not int(request.flags) & CAPTURE_F_HBM_DATA and hbm_mask != 0)):
            raise ThermError("thermal helper completed snapshot journal is inconsistent")
    elif int(result.status) == 0:
        raise ThermError("thermal helper successful snapshot is incomplete")
    elif int(result.writes_issued) > 26:
        raise ThermError("thermal helper semantic write count is out of range")


class ThermHelper(object):
    def __init__(self, path, allow_snapshot=False):
        assert_layout()
        self.path = str(path)
        flags = os.O_RDWR if allow_snapshot else os.O_RDONLY
        self.fd = os.open(self.path, flags | getattr(os, "O_CLOEXEC", 0))
        try:
            self.info = _ioctl_structure(self.fd, IOC_INFO, Info())
            _validate_info(self.info)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def cards(self):
        records = []
        for index in range(int(self.info.count)):
            card = self.info.cards[index]
            records.append({
                "helper_index": index,
                "identity": identity_dict(card.identity),
                "raw_identity": card.identity,
                "bar0_start": "0x%016x" % int(card.bar0_start),
                "bar0_length": int(card.bar0_length),
                "mapped_length": int(card.mapped_length),
                "resource_flags": "0x%016x" % int(card.resource_flags),
                "driver_bound": bool(card.driver_bound),
                "current_state": int(card.current_state),
                "runtime_status": int(card.runtime_status),
            })
        return records

    def capture(self, card, snapshot=False, allow_bound=False, hbm_data=False):
        if allow_bound and not snapshot:
            raise ThermError("allow_bound is meaningful only with snapshot")
        if hbm_data and not snapshot:
            raise ThermError("hbm_data is meaningful only with snapshot")
        capabilities = int(self.info.capabilities)
        if not capabilities & CAP_PASSIVE_SELECTOR_OMISSION:
            raise ThermError(
                "helper does not advertise passive selector omission"
            )
        if not capabilities & CAP_FRESH_PREWRITE_LIVENESS:
            raise ThermError(
                "helper does not advertise fresh prewrite liveness"
            )
        if not capabilities & CAP_PURE_CAPTURE:
            raise ThermError("helper does not advertise pure capture")
        if (not snapshot and card.get("driver_bound") and
                not capabilities & CAP_BOUND_PURE_CAPTURE):
            raise ThermError(
                "helper does not advertise bound pure capture"
            )
        if snapshot and not capabilities & CAP_SEMANTIC_SNAPSHOT:
            raise ThermError("helper does not advertise semantic snapshot")
        if snapshot and allow_bound and not capabilities & CAP_BOUND_SNAPSHOT:
            raise ThermError("helper was not loaded with bound snapshot support")
        if hbm_data and not capabilities & CAP_STATEFUL_HBM_DATA:
            raise ThermError("helper does not advertise stateful HBM DATA reads")
        request = Capture()
        request.abi_version = ABI_VERSION
        request.struct_size = ctypes.sizeof(Capture)
        request.identity = card["raw_identity"]
        request_flags = 0
        if snapshot:
            request_flags |= CAPTURE_F_SNAPSHOT
        if allow_bound:
            request_flags |= CAPTURE_F_ALLOW_BOUND
        if hbm_data:
            request_flags |= CAPTURE_F_HBM_DATA
        request.flags = request_flags
        result = _ioctl_structure(self.fd, IOC_CAPTURE, request)
        if (result.abi_version != ABI_VERSION or
                result.struct_size != ctypes.sizeof(Capture)):
            raise ThermError("thermal helper returned incompatible CAPTURE")
        if int(getattr(result, "flags")) != request_flags:
            raise ThermError("thermal helper returned mismatched capture flags")
        requested_identity = ctypes.string_at(
            ctypes.addressof(request.identity), ctypes.sizeof(Identity)
        )
        returned_identity = ctypes.string_at(
            ctypes.addressof(result.identity), ctypes.sizeof(Identity)
        )
        if returned_identity != requested_identity:
            raise ThermError("thermal helper returned mismatched capture identity")
        _validate_capture_response(request, result)
        return result


assert_layout()

__all__ = [
    "ThermHelper", "ThermError", "Capture", "Info", "Identity",
    "FbpaRaw", "GpcAnalogRaw", "FixedContextRaw",
    "identity_dict", "bdf", "assert_layout", "IOC_INFO", "IOC_CAPTURE",
]
