/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_170HX_XRAY_THERM_H
#define _UAPI_170HX_XRAY_THERM_H

#if defined(__linux__) || defined(__KERNEL__)
#include <linux/ioctl.h>
#include <linux/types.h>
#else
/* Let the hardware-inert ABI lock test run on any native LP64 host while
 * reproducing Linux's generic ioctl encoding exactly. */
#include <stdint.h>
typedef uint8_t __u8;
typedef uint16_t __u16;
typedef uint32_t __u32;
typedef uint64_t __u64;
typedef int32_t __s32;
#define _IOC_NRBITS 8U
#define _IOC_TYPEBITS 8U
#define _IOC_SIZEBITS 14U
#define _IOC_NRSHIFT 0U
#define _IOC_TYPESHIFT (_IOC_NRSHIFT + _IOC_NRBITS)
#define _IOC_SIZESHIFT (_IOC_TYPESHIFT + _IOC_TYPEBITS)
#define _IOC_DIRSHIFT (_IOC_SIZESHIFT + _IOC_SIZEBITS)
#define _IOC_READ 2U
#define _IOC_WRITE 1U
#define _IOC(dir, type, nr, size)                                \
	(((dir) << _IOC_DIRSHIFT) | ((type) << _IOC_TYPESHIFT) | \
	 ((nr) << _IOC_NRSHIFT) | ((size) << _IOC_SIZESHIFT))
#define _IOR(type, nr, data_type) \
	_IOC(_IOC_READ, (type), (nr), sizeof(data_type))
#define _IOWR(type, nr, data_type) \
	_IOC(_IOC_READ | _IOC_WRITE, (type), (nr), sizeof(data_type))
#endif

#define XRAY_THERM_ABI_VERSION 5U
#define XRAY_THERM_MAX_CARDS 16U
#define XRAY_THERM_AGGREGATE_COUNT 8U
#define XRAY_THERM_TS_AUTO_COUNT 8U
#define XRAY_THERM_FBPA_COUNT 24U
#define XRAY_THERM_GPC_COUNT 8U
#define XRAY_THERM_BJT_COUNT 3U
#define XRAY_THERM_GPC_BJT_COUNT 24U
#define XRAY_THERM_SENTINEL_COUNT 2U
#define XRAY_THERM_SNAPSHOT_AGG_COUNT 4U

/* ABI 5 is intentionally native LP64 only.  There is no compat_ioctl. */
#define XRAY_THERM_CAP_PURE_CAPTURE (1U << 0)
#define XRAY_THERM_CAP_SEMANTIC_SNAPSHOT (1U << 1)
#define XRAY_THERM_CAP_BOUND_PURE_CAPTURE (1U << 2)
#define XRAY_THERM_CAP_BOUND_SNAPSHOT (1U << 3)
#define XRAY_THERM_CAP_STATEFUL_HBM_DATA (1U << 4)
#define XRAY_THERM_CAP_PASSIVE_SELECTOR_OMISSION (1U << 5)
#define XRAY_THERM_CAP_FRESH_PREWRITE_LIVENESS (1U << 6)

/* Take the optional selector-driven semantic snapshot after the pure reads. */
#define XRAY_THERM_CAPTURE_F_SNAPSHOT (1U << 0)
/* A bound snapshot additionally requires allow_bound_snapshot=1 at module
 * load.  This is rejected unless SNAPSHOT is also set.  device_lock cannot
 * serialize proprietary firmware, so a bound snapshot can still race it; the
 * selector readbacks are detection, not prevention. */
#define XRAY_THERM_CAPTURE_F_ALLOW_BOUND (1U << 1)
/* Explicitly acknowledge the stateful, read-to-advance FBPA DATA reads.  This
 * is accepted only together with SNAPSHOT and is never part of pure mode. */
#define XRAY_THERM_CAPTURE_F_HBM_DATA (1U << 2)
#define XRAY_THERM_CAPTURE_REQUIRES_WRITE(flags)     \
	(((flags) & (XRAY_THERM_CAPTURE_F_SNAPSHOT | \
		     XRAY_THERM_CAPTURE_F_HBM_DATA)) != 0U)

/* Selector-dependent reads are omitted unconditionally.  A bound firmware
 * owner can change READINCR after any host-side check, so checking once and
 * then reading a dependent port is not a passive transaction. */
#define XRAY_THERM_SELECTOR_SKIP_CURRENT (1U << 0)
#define XRAY_THERM_SELECTOR_SKIP_OVERRIDE (1U << 1)
#define XRAY_THERM_SELECTOR_SKIP_HS_OFFSET (1U << 2)
#define XRAY_THERM_SELECTOR_SKIP_SNAPSHOT (1U << 3)
#define XRAY_THERM_SELECTOR_SKIP_ALL 0x0fU

/* Pre-write liveness must be affirmative rather than merely "not BADF".
 * GA100 TSENSE STATE 1/2 means VALID/SHADOW.  NUM_ACTIVE_FBPS is a five-bit
 * count and GA100 has at most twelve FBP partitions.  Constant-expression
 * macros let the host ABI test exercise the kernel's exact predicate. */
#define XRAY_THERM_TSENSE_STATE_SHIFT 29U
#define XRAY_THERM_TSENSE_STATE_MASK 0x3U
#define XRAY_THERM_TSENSE_STATE_VALID 0x1U
#define XRAY_THERM_TSENSE_STATE_SHADOW 0x2U
#define XRAY_THERM_ACTIVE_FBPS_MASK 0x1fU
#define XRAY_THERM_ACTIVE_FBPS_MAX 12U
#define XRAY_THERM_SELECTOR_GPC_SHIFT 16U
#define XRAY_THERM_SELECTOR_GPC_MASK 0x0fU
#define XRAY_THERM_SELECTOR_BJT_MASK 0x0fU
#define XRAY_THERM_SELECTOR_GPC_MAX 7U
#define XRAY_THERM_SELECTOR_BJT_MAX 2U
#define XRAY_THERM_SELECTOR_READINCR (1U << 30)
#define XRAY_THERM_SELECTOR_ALLOWED_MASK                                   \
	((XRAY_THERM_SELECTOR_GPC_MASK << XRAY_THERM_SELECTOR_GPC_SHIFT) | \
	 XRAY_THERM_SELECTOR_BJT_MASK | XRAY_THERM_SELECTOR_READINCR)
#define XRAY_THERM_WORD_IS_DEAD(value) \
	(((value) == 0xffffffffU) || (((value) & 0xffff0000U) == 0xbadf0000U))
#define XRAY_THERM_TSENSE_IS_AFFIRMATIVE(value)                              \
	(!XRAY_THERM_WORD_IS_DEAD(value) &&                                  \
	 ((((value) >> XRAY_THERM_TSENSE_STATE_SHIFT) &                      \
	   XRAY_THERM_TSENSE_STATE_MASK) == XRAY_THERM_TSENSE_STATE_VALID || \
	  (((value) >> XRAY_THERM_TSENSE_STATE_SHIFT) &                      \
	   XRAY_THERM_TSENSE_STATE_MASK) == XRAY_THERM_TSENSE_STATE_SHADOW))
#define XRAY_THERM_ACTIVE_FBPS_IS_AFFIRMATIVE(value)         \
	(!XRAY_THERM_WORD_IS_DEAD(value) &&                  \
	 (((value) & ~XRAY_THERM_ACTIVE_FBPS_MASK) == 0U) && \
	 (((value) & XRAY_THERM_ACTIVE_FBPS_MASK) >= 1U) &&  \
	 (((value) & XRAY_THERM_ACTIVE_FBPS_MASK) <=         \
	  XRAY_THERM_ACTIVE_FBPS_MAX))
#define XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(tsense, active_fbps) \
	(XRAY_THERM_TSENSE_IS_AFFIRMATIVE(tsense) &&             \
	 XRAY_THERM_ACTIVE_FBPS_IS_AFFIRMATIVE(active_fbps))
#define XRAY_THERM_SELECTOR_IS_VALID(value)                                 \
	(!XRAY_THERM_WORD_IS_DEAD(value) &&                                 \
	 (((value) & ~XRAY_THERM_SELECTOR_ALLOWED_MASK) == 0U) &&           \
	 ((((value) >> XRAY_THERM_SELECTOR_GPC_SHIFT) &                     \
	   XRAY_THERM_SELECTOR_GPC_MASK) <= XRAY_THERM_SELECTOR_GPC_MAX) && \
	 (((value) & XRAY_THERM_SELECTOR_BJT_MASK) <=                       \
	  XRAY_THERM_SELECTOR_BJT_MAX))

struct xray_therm_identity {
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

struct xray_therm_card_info {
	struct xray_therm_identity identity;
	__u64 bar0_start;
	__u64 bar0_length;
	__u64 mapped_length;
	__u64 resource_flags;
	__u32 driver_bound;
	__u32 current_state;
	__u32 runtime_status;
	__u32 reserved0[3];
};

struct xray_therm_info {
	__u32 abi_version;
	__u32 struct_size;
	__u32 count;
	__u32 max_cards;
	__u32 capabilities;
	__u32 reserved0[3];
	struct xray_therm_card_info cards[XRAY_THERM_MAX_CARDS];
};

struct xray_therm_fbpa_raw {
	/* These are sequential register slots, not an atomic FBPA sample.
	 * Firmware can change state between fields.  data is read-to-advance and
	 * remains zero unless HBM_DATA was explicitly accepted; consult the mask. */
	__u32 cfg1;
	__u32 test_macro;
	__u32 instr;
	__u32 mode;
	__u32 data;
	__u32 shwir;
	__u32 shwdr;
	__u32 status;
};

/* Fixed, read-only views of each GPC thermal analogue block.  These are raw
 * register values, not four independent temperature sensors. */
struct xray_therm_gpc_analog_raw {
	__u32 tsense;
	__u32 adjustment;
	__u32 raw_override;
	__u32 ts_auto;
};

/* Fixed, read-only system/SCI thermal context.  Several words are aliases,
 * controls, calibration, or derived statistics; their presence in this
 * structure does not classify them as physical sensors. */
struct xray_therm_fixed_context_raw {
	__u32 sys_sensor0;
	__u32 sys_sensor1;
	__u32 sys_sensor2;
	__u32 sys_sensor3;
	__u32 sys_sensor4;
	__u32 sys_sensor6;
	__u32 sys_sensor7;
	__u32 dedicated_overt_max;
	__u32 i2cs_sensor00;
	__u32 i2cs_sensor11;
	__u32 i2cs_sensor13;
	__u32 i2cs_sensor14;
	__u32 sensor8;
	__u32 sensor9;
	__u32 statistics_temp_avg;
	__u32 statistics_ctrl;
	__u32 gpc_tsense_hs_offset_index;
	__u32 gpc_tsense_override_index;
	__u32 gpc_tsense_override;
	__u32 gpc_tsense_global_override;
	__u32 gpc_tsense_hs_offset;
	__u32 sci_smbus_cmd00;
	__u32 sci_overt_ts_sm;
	__u32 sci_overt_ts_adj;
	__u32 sci_overt_thresh;
	__u32 sci_overt_fusectrl;
	__u32 sci_overt_fuse;
	__u32 sci_overt_temp_coeff_nom;
	__u32 sci_overt_temp_coeff_final;
	__u32 sci_overt_ts_adc;
	__u32 sci_overt_temperature;
};

struct xray_therm_gpc_bjt_raw {
	__u32 selector;
	__u32 selector_before;
	__u32 temperature;
	__u32 offset_temperature;
	__u32 selector_after;
};

struct xray_therm_capture {
	__u32 abi_version;
	__u32 struct_size;
	struct xray_therm_identity identity;
	__u32 flags;
	__s32 status;
	__u32 reads_issued;
	__u32 writes_issued;
	__u32 driver_bound_at_entry;
	__u32 pure_completed;
	__u32 snapshot_completed;
	__u32 selector_restored;
	__u32 selector_race;
	__u32 autopoll_unchanged;
	__u32 sentinels_unchanged;
	__u32 trigger_self_cleared;
	__u32 failed_gpc;
	__u32 failed_bjt;
	__u32 aggregate[XRAY_THERM_AGGREGATE_COUNT];
	__u32 ts_auto_entry[XRAY_THERM_TS_AUTO_COUNT];
	__u32 ts_auto_exit[XRAY_THERM_TS_AUTO_COUNT];
	/* The selector is observed, but its three dependent ports are always
	 * omitted.  Their ABI slots stay zero and the skip mask is authoritative. */
	__u32 current_selector;
	__u32 current_temperature;
	__u32 current_offset_temperature;
	__u32 current_raw_code;
	__u32 snapshot_selector_entry;
	__u32 snapshot_selector_exit;
	__u32 snapshot_aggregate[XRAY_THERM_SNAPSHOT_AGG_COUNT];
	__u32 sentinels_entry[XRAY_THERM_SENTINEL_COUNT];
	__u32 sentinels_exit[XRAY_THERM_SENTINEL_COUNT];
	struct xray_therm_fixed_context_raw fixed_context;
	struct xray_therm_gpc_analog_raw gpc_analog[XRAY_THERM_GPC_COUNT];
	struct xray_therm_fbpa_raw fbpa[XRAY_THERM_FBPA_COUNT];
	struct xray_therm_gpc_bjt_raw gpc_bjt[XRAY_THERM_GPC_BJT_COUNT];
	/* Independent population evidence for interpreting the 8x3 snapshot. */
	__u32 opt_ts_dis_mask;
	__u32 status_opt_gpc;
	/* Pure-read snapshot selector as found.  The two dependent temperature
	 * ports are always omitted; userspace may compare the selector words only.
	 * snapshot_selector_entry/exit above are reserved for an explicitly
	 * requested semantic transaction and are not fabricated in pure mode. */
	__u32 snapshot_as_found_selector_before;
	__u32 snapshot_as_found_temperature;
	__u32 snapshot_as_found_offset_temperature;
	__u32 snapshot_as_found_selector_after;
	__u32 status_opt_fbio;
	/* ABI4: exact mask XRAY_THERM_SELECTOR_SKIP_ALL. */
	__u32 selector_dependent_skipped_mask;
	/* Bits 0..23 identify FBPA DATA ports actually read.  A zero data word
	 * with the corresponding bit clear means not observed, not value zero. */
	__u32 hbm_data_read_mask;
	/* ABI5: a second affirmative pair sampled after saved-selector validation
	 * and immediately before the first semantic write.  These remain zero in
	 * pure mode and on refusal before the fresh pre-write check. */
	__u32 prewrite_tsense;
	__u32 prewrite_active_fbps;
};

/* CAPTURE returning from ioctl is not evidence of a valid observation by
 * itself.  Userspace must require status == 0 and pure_completed == 1.
 * selector_dependent_skipped_mask must equal XRAY_THERM_SELECTOR_SKIP_ALL and
 * HBM data bits must exactly match the requested stateful tier.  If SNAPSHOT
 * was requested, require snapshot_completed, trigger_self_cleared,
 * selector_restored, autopoll_unchanged, and sentinels_unchanged all equal 1;
 * selector_race must be 0, failed_gpc/failed_bjt U32_MAX, the saved entry and
 * restored exit selectors must satisfy XRAY_THERM_SELECTOR_IS_VALID and match,
 * the fresh prewrite_tsense/prewrite_active_fbps pair must satisfy
 * XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS, writes must equal exactly 26, and reads
 * must be within the documented trigger-poll bound. */

#define XRAY_THERM_IOC_MAGIC 'T'
#define XRAY_THERM_IOC_INFO \
	_IOR(XRAY_THERM_IOC_MAGIC, 0x01, struct xray_therm_info)
#define XRAY_THERM_IOC_CAPTURE \
	_IOWR(XRAY_THERM_IOC_MAGIC, 0x02, struct xray_therm_capture)

#endif /* _UAPI_170HX_XRAY_THERM_H */
