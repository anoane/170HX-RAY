/* SPDX-License-Identifier: GPL-2.0 */
/* Hardware-inert host test for the dedicated thermal helper's native ABI. */
#include <stddef.h>
#include <stdio.h>

#include "170hx_xray_therm_uapi.h"

_Static_assert(XRAY_THERM_ABI_VERSION == 5U, "unexpected ABI version");
_Static_assert(sizeof(struct xray_therm_identity) == 32,
	       "identity layout drift");
_Static_assert(sizeof(struct xray_therm_card_info) == 88,
	       "card-info layout drift");
_Static_assert(sizeof(struct xray_therm_info) == 1440, "info layout drift");
_Static_assert(sizeof(struct xray_therm_fbpa_raw) == 32, "FBPA layout drift");
_Static_assert(sizeof(struct xray_therm_gpc_analog_raw) == 16,
	       "GPC analogue layout drift");
_Static_assert(sizeof(struct xray_therm_fixed_context_raw) == 124,
	       "fixed-context layout drift");
_Static_assert(sizeof(struct xray_therm_gpc_bjt_raw) == 20,
	       "GPC/BJT layout drift");
_Static_assert(sizeof(struct xray_therm_capture) == 1792,
	       "capture layout drift");

_Static_assert(offsetof(struct xray_therm_info, cards) == 32,
	       "info.cards offset drift");
_Static_assert(offsetof(struct xray_therm_capture, aggregate) == 96,
	       "capture.aggregate offset drift");
_Static_assert(offsetof(struct xray_therm_capture, fixed_context) == 248,
	       "capture.fixed_context offset drift");
_Static_assert(offsetof(struct xray_therm_capture, gpc_analog) == 372,
	       "capture.gpc_analog offset drift");
_Static_assert(offsetof(struct xray_therm_capture, fbpa) == 500,
	       "capture.fbpa offset drift");
_Static_assert(offsetof(struct xray_therm_capture, gpc_bjt) == 1268,
	       "capture.gpc_bjt offset drift");
_Static_assert(offsetof(struct xray_therm_capture, status_opt_fbio) == 1772,
	       "capture.status_opt_fbio offset drift");
_Static_assert(offsetof(struct xray_therm_capture,
			selector_dependent_skipped_mask) == 1776,
	       "capture.selector_dependent_skipped_mask offset drift");
_Static_assert(offsetof(struct xray_therm_capture, hbm_data_read_mask) == 1780,
	       "capture.hbm_data_read_mask offset drift");
_Static_assert(offsetof(struct xray_therm_capture, prewrite_tsense) == 1784,
	       "capture.prewrite_tsense offset drift");
_Static_assert(offsetof(struct xray_therm_capture, prewrite_active_fbps) ==
		       1788,
	       "capture.prewrite_active_fbps offset drift");

_Static_assert(XRAY_THERM_CAPTURE_F_HBM_DATA == (1U << 2),
	       "HBM DATA flag drift");
_Static_assert(!XRAY_THERM_CAPTURE_REQUIRES_WRITE(0U),
	       "pure capture must remain available on a read-only fd");
_Static_assert(XRAY_THERM_CAPTURE_REQUIRES_WRITE(XRAY_THERM_CAPTURE_F_SNAPSHOT),
	       "snapshot capture must require a write-opened fd");
_Static_assert(XRAY_THERM_CAPTURE_REQUIRES_WRITE(XRAY_THERM_CAPTURE_F_HBM_DATA),
	       "stateful HBM DATA capture must require a write-opened fd");
_Static_assert(XRAY_THERM_CAP_STATEFUL_HBM_DATA == (1U << 4),
	       "HBM DATA capability drift");
_Static_assert(XRAY_THERM_CAP_PASSIVE_SELECTOR_OMISSION == (1U << 5),
	       "passive-selector-omission capability drift");
_Static_assert(XRAY_THERM_CAP_FRESH_PREWRITE_LIVENESS == (1U << 6),
	       "fresh-prewrite-liveness capability drift");
_Static_assert((XRAY_THERM_SELECTOR_SKIP_CURRENT |
		XRAY_THERM_SELECTOR_SKIP_OVERRIDE |
		XRAY_THERM_SELECTOR_SKIP_HS_OFFSET |
		XRAY_THERM_SELECTOR_SKIP_SNAPSHOT) ==
		       XRAY_THERM_SELECTOR_SKIP_ALL,
	       "selector-dependent skip-mask drift");

/* Hardware-inert truth table for the exact pre-write liveness predicate. */
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0U, 0U),
	       "all-zero stub must fail closed");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0xbadf0000U, 0xbadf0000U),
	       "BADF BAR must fail closed");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0xffffffffU, 0xffffffffU),
	       "all-ones BAR must fail closed");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0U, 0xbadf0000U),
	       "one zero among dead words must fail closed");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x20000000U, 0U),
	       "TSENSE alone must not establish liveness");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0U, 8U),
	       "active-FBP count alone must not establish liveness");
_Static_assert(XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x20000000U, 8U),
	       "VALID TSENSE plus active FBP count must establish liveness");
_Static_assert(XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x40000000U, 12U),
	       "SHADOW TSENSE plus active FBP count must establish liveness");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x20000000U, 13U),
	       "out-of-range active-FBP count must fail closed");
_Static_assert(!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(0x20000000U, 0x10000008U),
	       "reserved NUM_ACTIVE bits must fail closed");

/* Hardware-inert truth table for the exact pre-write saved-selector gate. */
_Static_assert(XRAY_THERM_SELECTOR_IS_VALID(0U),
	       "GPC0/BJT0 selector must be valid");
_Static_assert(XRAY_THERM_SELECTOR_IS_VALID(0x00070002U),
	       "GPC7/BJT2 selector must be valid");
_Static_assert(XRAY_THERM_SELECTOR_IS_VALID(XRAY_THERM_SELECTOR_READINCR |
					    0x00070002U),
	       "READINCR is the only allowed non-field selector bit");
_Static_assert(!XRAY_THERM_SELECTOR_IS_VALID(0x00080000U),
	       "GPC8 selector must fail closed");
_Static_assert(!XRAY_THERM_SELECTOR_IS_VALID(0x00000003U),
	       "BJT3 selector must fail closed");
_Static_assert(!XRAY_THERM_SELECTOR_IS_VALID(0x00000100U),
	       "reserved selector bits must fail closed");
_Static_assert(!XRAY_THERM_SELECTOR_IS_VALID(0xbadf0000U),
	       "BADF selector must fail closed");
_Static_assert(!XRAY_THERM_SELECTOR_IS_VALID(0xffffffffU),
	       "all-ones selector must fail closed");

_Static_assert(XRAY_THERM_IOC_INFO == 0x85a05401UL,
	       "INFO ioctl encoding drift");
_Static_assert(XRAY_THERM_IOC_CAPTURE == 0xc7005402UL,
	       "CAPTURE ioctl encoding drift");

int main(void)
{
	puts("170hx_xray_therm ABI 5 LP64 layout, liveness, selector, and ioctl encodings: PASS");
	return 0;
}
