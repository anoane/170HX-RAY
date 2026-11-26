/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_170HX_XRAY_H
#define _UAPI_170HX_XRAY_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define XRAY_ABI_VERSION 6U
#define XRAY_MAX_CARDS 16U
#define XRAY_MAX_PASSES 2U
#define XRAY_MAIN_ROWS 512U
#define XRAY_RIR_ROWS 8U

/* ABI 6 is intentionally native 64-bit only.  The module does not register a
 * compat_ioctl handler; its source locks the LP64 sizes and critical offsets. */

#define XRAY_CAP_READ32 (1U << 0)
#define XRAY_CAP_DUMP_MAIN (1U << 1)
#define XRAY_CAP_DUMP_RIR (1U << 2)
#define XRAY_CAP_BOUND_READ32 (1U << 3)
#define XRAY_CAP_BOUND_CONTROLLER (1U << 4)
#define XRAY_CAP_LAST_JOURNAL (1U << 5)
#define XRAY_CAP_A100_CONTROLLER (1U << 6)

#define XRAY_DUMP_MAIN 1U
#define XRAY_DUMP_RIR 2U

/* Refused unless the module was explicitly loaded with
 * allow_bound_controller=1. Never set this merely because a GPU is busy. */
#define XRAY_DUMP_F_ALLOW_BOUND (1U << 0)
/* Require the software-IFF redirection path to be disabled before MAIN_FF
 * capture: EN_SW_OVERRIDE=0 and IFF_SW_FUSING=0.  IFF_RECORD itself is not
 * required to be zero.  On the observed GA100s it is nonzero with both
 * software controls inactive and can corroborate the live active physical-IFF
 * interval (for example START=488/LAST=507); it is not permanent storage or
 * sole physical-boundary authority. */
#define XRAY_DUMP_F_NO_SW_IFF (1U << 1)
/* Require CRC_STATUS H0/H1 == PRESENT_NO_ERRORS (0x55). */
#define XRAY_DUMP_F_CLEAN_CRC (1U << 2)

struct xray_identity {
	__u64 generation;
	__u16 domain;
	__u8 bus;
	__u8 devfn;
	__u16 vendor;
	__u16 device;
	__u16 subvendor;
	__u16 subdevice;
	__u8 revision;
	__u8 reserved0[7];
};

struct xray_card_info {
	struct xray_identity identity;
	__u32 class_code;
	__u16 pci_command;
	__u8 driver_bound;
	__u8 runtime_status;
	__u64 bar0_start;
	__u64 bar0_length;
	char driver[32];
};

struct xray_info {
	__u32 abi_version;
	__u32 struct_size;
	__u32 capabilities;
	__u32 count;
	__u32 max_cards;
	__u32 reserved0;
	__u64 module_generation;
	struct xray_card_info cards[XRAY_MAX_CARDS];
};

struct xray_reg32 {
	__u32 abi_version;
	__u32 struct_size;
	struct xray_identity identity;
	__u32 offset;
	__u32 value;
};

struct xray_controller_snapshot {
	__u32 pmc_boot0;
	__u32 fusectrl;
	__u32 fuseaddr;
	__u32 en_sw_override;
	__u32 iff_record;
	__u32 debugctrl;
	__u32 crc_status;
	__u32 enable_fuse_program_status;
	__u32 disable_fuse_program_status;
	__u32 bypass_fuses_status;
	__u32 disable_sw_override_status;
	__u32 iff_sw_fusing;
	__u32 debugctrl_priv_mask;
	__u32 fusewdata_priv_mask;
	__u32 fusectrl_priv_mask;
	__u32 opt_priv_mask;
};

struct xray_dump {
	__u32 abi_version;
	__u32 struct_size;
	struct xray_identity identity;
	__u32 kind;
	__u32 flags;
	__u32 requested_passes;
	__u32 row_count;
	__u32 completed_passes;
	__u32 coherent;
	__s32 status;
	__u32 failed_pass;
	__u32 failed_row;
	__u32 writes_issued;
	/* Number of CMD_READ commands issued, including a command that later
	 * timed out or was interrupted before FUSERDATA could be returned. */
	__u32 reads_issued;
	__u32 address_restored;
	__u32 debug_restored;
	__u32 driver_bound_at_entry;
	struct xray_controller_snapshot entry;
	struct xray_controller_snapshot exit;
	__u32 words[XRAY_MAX_PASSES][XRAY_MAIN_ROWS];
};

#define XRAY_IOC_MAGIC 'X'
#define XRAY_IOC_INFO _IOR(XRAY_IOC_MAGIC, 0x01, struct xray_info)
#define XRAY_IOC_READ32 _IOWR(XRAY_IOC_MAGIC, 0x02, struct xray_reg32)
#define XRAY_IOC_DUMP _IOWR(XRAY_IOC_MAGIC, 0x03, struct xray_dump)
#define XRAY_IOC_LAST _IOWR(XRAY_IOC_MAGIC, 0x04, struct xray_dump)

#endif /* _UAPI_170HX_XRAY_H */
