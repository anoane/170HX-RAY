/* SPDX-License-Identifier: GPL-2.0 */
/* Hardware-inert host test for the native LP64 ABI exported by the helper. */
#include <stddef.h>
#include <stdio.h>

#include "170hx_xray_uapi.h"

_Static_assert(XRAY_ABI_VERSION == 6U, "unexpected ABI version");

_Static_assert(sizeof(struct xray_identity) == 32,
	       "xray_identity layout drift");
_Static_assert(sizeof(struct xray_card_info) == 88,
	       "xray_card_info layout drift");
_Static_assert(sizeof(struct xray_info) == 1440, "xray_info layout drift");
_Static_assert(sizeof(struct xray_reg32) == 48, "xray_reg32 layout drift");
_Static_assert(sizeof(struct xray_controller_snapshot) == 64,
	       "xray_controller_snapshot layout drift");
_Static_assert(sizeof(struct xray_dump) == 4320, "xray_dump layout drift");

_Static_assert(offsetof(struct xray_info, cards) == 32,
	       "xray_info.cards offset drift");
_Static_assert(offsetof(struct xray_reg32, offset) == 40,
	       "xray_reg32.offset drift");
_Static_assert(offsetof(struct xray_dump, entry) == 96,
	       "xray_dump.entry offset drift");
_Static_assert(offsetof(struct xray_dump, words) == 224,
	       "xray_dump.words offset drift");

_Static_assert(XRAY_IOC_INFO == 0x85a05801UL, "XRAY_IOC_INFO encoding drift");
_Static_assert(XRAY_IOC_READ32 == 0xc0305802UL,
	       "XRAY_IOC_READ32 encoding drift");
_Static_assert(XRAY_IOC_DUMP == 0xd0e05803UL, "XRAY_IOC_DUMP encoding drift");
_Static_assert(XRAY_IOC_LAST == 0xd0e05804UL, "XRAY_IOC_LAST encoding drift");

int main(void)
{
	puts("170hx_xray ABI 6 LP64 layout and ioctl encodings: PASS");
	return 0;
}
