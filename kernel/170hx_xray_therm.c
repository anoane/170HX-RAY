// SPDX-License-Identifier: GPL-2.0
/*
 * 170hx_xray_therm - constrained GA100 thermal observation helper.
 *
 * This module deliberately exports no caller-selected MMIO operation.  Its
 * only explicit MMIO writes belong to a requested, journaled thermal
 * snapshot, whose complete write alphabet is:
 *
 *   0x0002085c - one snapshot trigger write
 *   0x00020840 - selector writes and the mandatory selector restoration
 *
 * The read-to-advance FBPA DATA ports are skipped unless semantic mode also
 * explicitly requests HBM_DATA; issued reads are journaled by mask.  TS_AUTO
 * is raw observation metadata only.  It is never written here.
 */

#include <linux/bitops.h>
#include <linux/build_bug.h>
#include <linux/capability.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/pci_regs.h>
#include <linux/pm_runtime.h>
#include <linux/random.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#include "170hx_xray_therm_uapi.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("170HX-XRAY contributors");
MODULE_DESCRIPTION("Constrained GA100 raw thermal capture helper");
MODULE_VERSION("1.5.1");

#define XRAY_THERM_VENDOR_NVIDIA 0x10de
#define XRAY_THERM_DEVICE_A100_PCIE 0x20b5
#define XRAY_THERM_DEVICE_CMP170HX 0x20c2
#define XRAY_THERM_DEVICE_GA100_GLITCH 0x2080
#define XRAY_THERM_BAR_MIN_LENGTH 0x00a00000ULL

#define XRAY_THERM_CURRENT_SELECTOR 0x000206d4U
#define XRAY_THERM_CURRENT_TEMP 0x000206e0U
#define XRAY_THERM_CURRENT_OFFSET_TEMP 0x000206e8U
#define XRAY_THERM_CURRENT_RAW_CODE 0x000206ecU

#define XRAY_THERM_SNAPSHOT_SELECTOR 0x00020840U
#define XRAY_THERM_SNAPSHOT_TEMP 0x00020844U
#define XRAY_THERM_SNAPSHOT_OFFSET_TEMP 0x00020848U
#define XRAY_THERM_SNAPSHOT_TRIGGER 0x0002085cU
#define XRAY_THERM_TRIGGER_BUSY BIT(0)
#define XRAY_THERM_TRIGGER_POLLS 1000U

#define XRAY_THERM_GPC_ANALOG_BASE 0x0010c084U
#define XRAY_THERM_GPC_ANALOG_STRIDE 0x00000100U
#define XRAY_THERM_GPC_TSENSE 0x00000000U
#define XRAY_THERM_GPC_ADJUSTMENT 0x00000004U
#define XRAY_THERM_GPC_RAW_OVERRIDE 0x0000006cU
#define XRAY_THERM_GPC_TS_AUTO 0x00000070U
#define XRAY_THERM_TS_AUTO_BASE \
	(XRAY_THERM_GPC_ANALOG_BASE + XRAY_THERM_GPC_TS_AUTO)
#define XRAY_THERM_TS_AUTO_STRIDE XRAY_THERM_GPC_ANALOG_STRIDE
/* Raw-output metadata only.  GA100's polling-enable field is bit 15. */
#define XRAY_THERM_TS_AUTO_POLLING BIT(15)

#define XRAY_THERM_FBPA_BASE 0x00900000U
#define XRAY_THERM_FBPA_STRIDE 0x00004000U
#define XRAY_THERM_FBPA_CFG1 0x00000204U
#define XRAY_THERM_FBPA_TEST_MACRO 0x00003c9cU
#define XRAY_THERM_FBPA_INSTR 0x00003cb4U
#define XRAY_THERM_FBPA_MODE 0x00003cb8U
#define XRAY_THERM_FBPA_DATA 0x00003cbcU
#define XRAY_THERM_FBPA_SHWIR 0x00003cc0U
#define XRAY_THERM_FBPA_SHWDR 0x00003cc4U
#define XRAY_THERM_FBPA_STATUS 0x00003cc8U

#define XRAY_THERM_SENTINEL0 0x009a0164U
#define XRAY_THERM_SENTINEL1 0x009a0974U
#define XRAY_THERM_OPT_TS_DIS_MASK 0x00821004U
#define XRAY_THERM_STATUS_OPT_FBIO 0x00820c14U
#define XRAY_THERM_STATUS_OPT_GPC 0x00820c1cU

/* Fixed context and alias registers.  A raw word here is not, by itself,
 * evidence of an additional physical temperature sensor. */
#define XRAY_THERM_SYS_SENSOR0 0x00020008U
#define XRAY_THERM_SYS_SENSOR1 0x0002000cU
#define XRAY_THERM_SYS_SENSOR2 0x00020010U
#define XRAY_THERM_SYS_SENSOR3 0x00020014U
#define XRAY_THERM_SYS_SENSOR4 0x0002001cU
#define XRAY_THERM_SYS_SENSOR6 0x000200f0U
#define XRAY_THERM_SYS_SENSOR7 0x000200f4U
#define XRAY_THERM_DEDICATED_OVERT_MAX 0x00020448U
#define XRAY_THERM_I2CS_SENSOR00 0x00020400U
#define XRAY_THERM_I2CS_SENSOR11 0x00020444U
#define XRAY_THERM_I2CS_SENSOR13 0x0002044cU
#define XRAY_THERM_I2CS_SENSOR14 0x00020450U
#define XRAY_THERM_SENSOR8 0x000204a0U
#define XRAY_THERM_SENSOR9 0x000204a4U
#define XRAY_THERM_STATISTICS_TEMP_AVG 0x00020510U
#define XRAY_THERM_STATISTICS_CTRL 0x00020514U
#define XRAY_THERM_GPC_TSENSE_HS_OFFSET_INDEX 0x000206ccU
#define XRAY_THERM_GPC_TSENSE_OVERRIDE_INDEX 0x000206d0U
#define XRAY_THERM_GPC_TSENSE_OVERRIDE 0x000206d8U
#define XRAY_THERM_GPC_TSENSE_GLOBAL_OVERRIDE 0x000206dcU
#define XRAY_THERM_GPC_TSENSE_HS_OFFSET 0x000206e4U
#define XRAY_THERM_SCI_SMBUS_CMD00 0x00118b80U
#define XRAY_THERM_SCI_OVERT_TS_SM 0x00118b90U
#define XRAY_THERM_SCI_OVERT_TS_ADJ 0x00118b94U
#define XRAY_THERM_SCI_OVERT_THRESH 0x00118b98U
#define XRAY_THERM_SCI_OVERT_FUSECTRL 0x00118b9cU
#define XRAY_THERM_SCI_OVERT_FUSE 0x00118ba0U
#define XRAY_THERM_SCI_OVERT_TEMP_COEFF_NOM 0x00118ba4U
#define XRAY_THERM_SCI_OVERT_TEMP_COEFF_FINAL 0x00118ba8U
#define XRAY_THERM_SCI_OVERT_TS_ADC 0x00118bacU
#define XRAY_THERM_SCI_OVERT_TEMPERATURE 0x00118bb0U

static const u32 xray_therm_aggregate_offsets[XRAY_THERM_AGGREGATE_COUNT] = {
	0x00020460U, 0x00020464U, 0x00020468U, 0x0002046cU,
	0x00020470U, 0x00020474U, 0x00020478U, 0x0002047cU,
};

static const u32
	xray_therm_snapshot_aggregate_offsets[XRAY_THERM_SNAPSHOT_AGG_COUNT] = {
		0x0002084cU,
		0x00020850U,
		0x00020854U,
		0x00020858U,
	};

static const u32 xray_therm_sentinel_offsets[XRAY_THERM_SENTINEL_COUNT] = {
	XRAY_THERM_SENTINEL0,
	XRAY_THERM_SENTINEL1,
};

static bool allow_bound_snapshot;
module_param(allow_bound_snapshot, bool, 0400);
MODULE_PARM_DESC(
	allow_bound_snapshot,
	"permit an explicitly flagged selector snapshot while a driver owns "
	"the GPU (unsafe, default false)");

struct xray_therm_card {
	struct pci_dev *pdev;
	struct xray_therm_identity mapped_identity;
	void __iomem *bar0;
	resource_size_t bar0_start;
	resource_size_t bar0_length;
	resource_size_t bar0_mapped_length;
	unsigned long bar0_flags;
	struct mutex lock;
};

static struct xray_therm_card xray_therm_cards[XRAY_THERM_MAX_CARDS];
static unsigned int xray_therm_ncards;
static u64 xray_therm_generation;
static atomic_t xray_therm_opened = ATOMIC_INIT(0);

static bool xray_therm_supported_device(u16 device)
{
	return device == XRAY_THERM_DEVICE_A100_PCIE ||
	       device == XRAY_THERM_DEVICE_CMP170HX ||
	       device == XRAY_THERM_DEVICE_GA100_GLITCH;
}

static void xray_therm_identity_from_pdev(struct pci_dev *pdev,
					  struct xray_therm_identity *identity)
{
	memset(identity, 0, sizeof(*identity));
	identity->generation = xray_therm_generation;
	identity->domain = pci_domain_nr(pdev->bus);
	identity->bus = pdev->bus->number;
	identity->devfn = pdev->devfn;
	identity->vendor = pdev->vendor;
	identity->device = pdev->device;
	identity->subvendor = pdev->subsystem_vendor;
	identity->subdevice = pdev->subsystem_device;
	identity->revision = pdev->revision;
}

/* Compare the complete frozen endpoint identity to both live config space and
 * the pci_dev topology/cache.  device_lock must be held. */
static int
xray_therm_attest_identity(struct pci_dev *pdev,
			   const struct xray_therm_identity *expected)
{
	u32 live_id, live_subsystem, live_class_revision;
	u16 live_vendor, live_device, live_subvendor, live_subdevice;
	u8 live_revision;

	if (pci_read_config_dword(pdev, PCI_VENDOR_ID, &live_id) ||
	    pci_read_config_dword(pdev, PCI_CLASS_REVISION,
				  &live_class_revision) ||
	    pci_read_config_dword(pdev, PCI_SUBSYSTEM_VENDOR_ID,
				  &live_subsystem))
		return -EIO;
	live_vendor = live_id & 0xffffU;
	live_device = live_id >> 16;
	live_subvendor = live_subsystem & 0xffffU;
	live_subdevice = live_subsystem >> 16;
	live_revision = live_class_revision & 0xffU;

	if (expected->generation != xray_therm_generation ||
	    expected->domain != pci_domain_nr(pdev->bus) ||
	    expected->bus != pdev->bus->number ||
	    expected->devfn != pdev->devfn ||
	    expected->vendor != pdev->vendor ||
	    expected->device != pdev->device ||
	    expected->subvendor != pdev->subsystem_vendor ||
	    expected->subdevice != pdev->subsystem_device ||
	    expected->revision != pdev->revision ||
	    live_vendor != expected->vendor ||
	    live_device != expected->device ||
	    live_subvendor != expected->subvendor ||
	    live_subdevice != expected->subdevice ||
	    live_revision != expected->revision ||
	    (live_class_revision >> 24) != PCI_BASE_CLASS_DISPLAY)
		return -ESTALE;
	return 0;
}

static bool xray_therm_identity_equal(const struct xray_therm_identity *a,
				      const struct xray_therm_identity *b)
{
	return !memcmp(a, b, sizeof(*a));
}

static struct xray_therm_card *
xray_therm_find_card(const struct xray_therm_identity *identity)
{
	unsigned int i;

	for (i = 0; i < xray_therm_ncards; i++)
		if (xray_therm_identity_equal(
			    &xray_therm_cards[i].mapped_identity, identity))
			return &xray_therm_cards[i];
	return NULL;
}

static bool xray_therm_driver_bound(struct xray_therm_card *card)
{
	return READ_ONCE(card->pdev->driver) != NULL;
}

/* Verify that BAR0 is still the same decoded memory BAR and resource that was
 * mapped at module initialization.  device_lock must be held. */
static int xray_therm_validate_bar(struct xray_therm_card *card)
{
	struct pci_dev *pdev = card->pdev;
	resource_size_t start = pci_resource_start(pdev, 0);
	resource_size_t length = pci_resource_len(pdev, 0);
	unsigned long flags = pci_resource_flags(pdev, 0);
	u32 lo, hi = 0, type;
	u64 decoded;

	if (!start || length < XRAY_THERM_BAR_MIN_LENGTH ||
	    !(flags & IORESOURCE_MEM) || (flags & IORESOURCE_UNSET))
		return -ENXIO;
	if (start != card->bar0_start || length != card->bar0_length ||
	    flags != card->bar0_flags ||
	    card->bar0_mapped_length != XRAY_THERM_BAR_MIN_LENGTH)
		return -ESTALE;
	if (pci_read_config_dword(pdev, PCI_BASE_ADDRESS_0, &lo))
		return -EIO;
	if (lo & PCI_BASE_ADDRESS_SPACE_IO)
		return -EINVAL;
	type = lo & PCI_BASE_ADDRESS_MEM_TYPE_MASK;
	if (type != PCI_BASE_ADDRESS_MEM_TYPE_32 &&
	    type != PCI_BASE_ADDRESS_MEM_TYPE_64)
		return -EINVAL;
	if (type == PCI_BASE_ADDRESS_MEM_TYPE_64 &&
	    pci_read_config_dword(pdev, PCI_BASE_ADDRESS_0 + 4, &hi))
		return -EIO;
	decoded = (lo & PCI_BASE_ADDRESS_MEM_MASK) | ((u64)hi << 32);
	return decoded == (u64)start ? 0 : -ESTALE;
}

/* device_lock must be held. */
static int xray_therm_validate_device_state(struct xray_therm_card *card)
{
	struct pci_dev *pdev = card->pdev;
	u16 command;
	int ret;

	if (!pci_device_is_present(pdev) || pci_channel_offline(pdev))
		return -ENODEV;
	ret = xray_therm_attest_identity(pdev, &card->mapped_identity);
	if (ret)
		return ret;
	if (pdev->current_state != PCI_D0)
		return -EHOSTDOWN;
	ret = pci_read_config_word(pdev, PCI_COMMAND, &command);
	if (ret)
		return ret;
	if (!(command & PCI_COMMAND_MEMORY))
		return -ENXIO;
	return xray_therm_validate_bar(card);
}

/* Pin only an already-active device; this helper must never wake a GPU. */
static int xray_therm_validate_live_device(struct xray_therm_card *card,
					   int *pm_ref)
{
	struct device *dev = &card->pdev->dev;
	int ret;

	*pm_ref = 0;
	ret = xray_therm_validate_device_state(card);
	if (ret)
		return ret;
	if (pm_runtime_enabled(dev)) {
		if (READ_ONCE(dev->power.runtime_status) != RPM_ACTIVE)
			return -EHOSTDOWN;
		ret = pm_runtime_get_if_active(dev);
		if (ret <= 0)
			return ret < 0 ? ret : -EHOSTDOWN;
		*pm_ref = ret;
		if (READ_ONCE(dev->power.runtime_status) != RPM_ACTIVE) {
			ret = -EHOSTDOWN;
			goto fail;
		}
	}
	ret = xray_therm_validate_device_state(card);
	if (!ret)
		return 0;
fail:
	if (*pm_ref > 0) {
		pm_runtime_put_noidle(dev);
		*pm_ref = 0;
	}
	return ret;
}

static void xray_therm_put_runtime_ref(struct xray_therm_card *card, int pm_ref)
{
	if (pm_ref > 0)
		pm_runtime_put_noidle(&card->pdev->dev);
}

static u32 xray_therm_read(struct xray_therm_card *card, u32 offset,
			   struct xray_therm_capture *capture)
{
	capture->reads_issued++;
	return readl(card->bar0 + offset);
}

/* Require two independent affirmative GA100 semantics before the first write.
 * Merely finding one word that is not BADF/all-ones accepts zero-filled stubs.
 * TSENSE must report VALID/SHADOW and NUM_ACTIVE_FBPS must be a reserved-zero
 * count 1..12.  The host ABI test exercises the exact same predicate. */
static bool
xray_therm_capture_has_liveness(const struct xray_therm_capture *capture)
{
	return XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(capture->aggregate[0],
						   capture->sentinels_entry[0]);
}

/* These are the only two write primitives in the module. */
static void xray_therm_write_trigger(struct xray_therm_card *card,
				     struct xray_therm_capture *capture)
{
	writel(1U, card->bar0 + XRAY_THERM_SNAPSHOT_TRIGGER);
	/* Same-BAR readback drains the posted write. */
	(void)readl(card->bar0 + XRAY_THERM_SNAPSHOT_TRIGGER);
	capture->writes_issued++;
	capture->reads_issued++;
}

static void xray_therm_write_selector(struct xray_therm_card *card, u32 value,
				      struct xray_therm_capture *capture)
{
	writel(value, card->bar0 + XRAY_THERM_SNAPSHOT_SELECTOR);
	/* Same-register readback drains the posted write. */
	(void)readl(card->bar0 + XRAY_THERM_SNAPSHOT_SELECTOR);
	capture->writes_issued++;
	capture->reads_issued++;
}

static void xray_therm_read_context(struct xray_therm_card *card,
				    struct xray_therm_capture *capture,
				    u32 ts_auto[XRAY_THERM_TS_AUTO_COUNT],
				    u32 sentinels[XRAY_THERM_SENTINEL_COUNT])
{
	unsigned int i;

	for (i = 0; i < XRAY_THERM_TS_AUTO_COUNT; i++)
		ts_auto[i] = xray_therm_read(
			card,
			XRAY_THERM_TS_AUTO_BASE + i * XRAY_THERM_TS_AUTO_STRIDE,
			capture);
	for (i = 0; i < XRAY_THERM_SENTINEL_COUNT; i++)
		sentinels[i] = xray_therm_read(
			card, xray_therm_sentinel_offsets[i], capture);
}

static void xray_therm_read_fixed_context(struct xray_therm_card *card,
					  struct xray_therm_capture *capture)
{
	struct xray_therm_fixed_context_raw *context = &capture->fixed_context;

	context->sys_sensor0 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR0, capture);
	context->sys_sensor1 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR1, capture);
	context->sys_sensor2 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR2, capture);
	context->sys_sensor3 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR3, capture);
	context->sys_sensor4 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR4, capture);
	context->sys_sensor6 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR6, capture);
	context->sys_sensor7 =
		xray_therm_read(card, XRAY_THERM_SYS_SENSOR7, capture);
	context->dedicated_overt_max =
		xray_therm_read(card, XRAY_THERM_DEDICATED_OVERT_MAX, capture);
	context->i2cs_sensor00 =
		xray_therm_read(card, XRAY_THERM_I2CS_SENSOR00, capture);
	context->i2cs_sensor11 =
		xray_therm_read(card, XRAY_THERM_I2CS_SENSOR11, capture);
	context->i2cs_sensor13 =
		xray_therm_read(card, XRAY_THERM_I2CS_SENSOR13, capture);
	context->i2cs_sensor14 =
		xray_therm_read(card, XRAY_THERM_I2CS_SENSOR14, capture);
	context->sensor8 = xray_therm_read(card, XRAY_THERM_SENSOR8, capture);
	context->sensor9 = xray_therm_read(card, XRAY_THERM_SENSOR9, capture);
	context->statistics_temp_avg =
		xray_therm_read(card, XRAY_THERM_STATISTICS_TEMP_AVG, capture);
	context->statistics_ctrl =
		xray_therm_read(card, XRAY_THERM_STATISTICS_CTRL, capture);
	context->gpc_tsense_hs_offset_index = xray_therm_read(
		card, XRAY_THERM_GPC_TSENSE_HS_OFFSET_INDEX, capture);
	context->gpc_tsense_override_index = xray_therm_read(
		card, XRAY_THERM_GPC_TSENSE_OVERRIDE_INDEX, capture);
	/* Never read the selector-dependent override port.  Firmware can change
	 * READINCR after the selector observation and before a dependent read. */
	context->gpc_tsense_global_override = xray_therm_read(
		card, XRAY_THERM_GPC_TSENSE_GLOBAL_OVERRIDE, capture);
	/* The hotspot-offset port is omitted for the same owner-race reason. */
	context->sci_smbus_cmd00 =
		xray_therm_read(card, XRAY_THERM_SCI_SMBUS_CMD00, capture);
	context->sci_overt_ts_sm =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_TS_SM, capture);
	context->sci_overt_ts_adj =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_TS_ADJ, capture);
	context->sci_overt_thresh =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_THRESH, capture);
	context->sci_overt_fusectrl =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_FUSECTRL, capture);
	context->sci_overt_fuse =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_FUSE, capture);
	context->sci_overt_temp_coeff_nom = xray_therm_read(
		card, XRAY_THERM_SCI_OVERT_TEMP_COEFF_NOM, capture);
	context->sci_overt_temp_coeff_final = xray_therm_read(
		card, XRAY_THERM_SCI_OVERT_TEMP_COEFF_FINAL, capture);
	context->sci_overt_ts_adc =
		xray_therm_read(card, XRAY_THERM_SCI_OVERT_TS_ADC, capture);
	context->sci_overt_temperature = xray_therm_read(
		card, XRAY_THERM_SCI_OVERT_TEMPERATURE, capture);
}

static void xray_therm_capture_pure(struct xray_therm_card *card,
				    struct xray_therm_capture *capture)
{
	unsigned int i;

	capture->selector_dependent_skipped_mask = XRAY_THERM_SELECTOR_SKIP_ALL;

	for (i = 0; i < XRAY_THERM_AGGREGATE_COUNT; i++)
		capture->aggregate[i] = xray_therm_read(
			card, xray_therm_aggregate_offsets[i], capture);
	xray_therm_read_context(card, capture, capture->ts_auto_entry,
				capture->sentinels_entry);
	xray_therm_read_fixed_context(card, capture);
	capture->current_selector =
		xray_therm_read(card, XRAY_THERM_CURRENT_SELECTOR, capture);
	/* Current TEMP/OFFSET/RAW_CODE are selector-dependent and therefore
	 * omitted even when the observed selector has READINCR clear. */

	for (i = 0; i < XRAY_THERM_GPC_COUNT; i++) {
		u32 base = XRAY_THERM_GPC_ANALOG_BASE +
			   i * XRAY_THERM_GPC_ANALOG_STRIDE;
		struct xray_therm_gpc_analog_raw *sample =
			&capture->gpc_analog[i];

		sample->tsense = xray_therm_read(
			card, base + XRAY_THERM_GPC_TSENSE, capture);
		sample->adjustment = xray_therm_read(
			card, base + XRAY_THERM_GPC_ADJUSTMENT, capture);
		sample->raw_override = xray_therm_read(
			card, base + XRAY_THERM_GPC_RAW_OVERRIDE, capture);
		/* Reuse the entry context read: do not issue a duplicate MMIO read. */
		sample->ts_auto = capture->ts_auto_entry[i];
	}

	for (i = 0; i < XRAY_THERM_FBPA_COUNT; i++) {
		u32 base = XRAY_THERM_FBPA_BASE + i * XRAY_THERM_FBPA_STRIDE;
		struct xray_therm_fbpa_raw *sample = &capture->fbpa[i];

		sample->cfg1 = xray_therm_read(
			card, base + XRAY_THERM_FBPA_CFG1, capture);
		sample->test_macro = xray_therm_read(
			card, base + XRAY_THERM_FBPA_TEST_MACRO, capture);
		sample->instr = xray_therm_read(
			card, base + XRAY_THERM_FBPA_INSTR, capture);
		sample->mode = xray_therm_read(
			card, base + XRAY_THERM_FBPA_MODE, capture);
		if (capture->flags & XRAY_THERM_CAPTURE_F_HBM_DATA) {
			sample->data = xray_therm_read(
				card, base + XRAY_THERM_FBPA_DATA, capture);
			capture->hbm_data_read_mask |= BIT(i);
		}
		sample->shwir = xray_therm_read(
			card, base + XRAY_THERM_FBPA_SHWIR, capture);
		sample->shwdr = xray_therm_read(
			card, base + XRAY_THERM_FBPA_SHWDR, capture);
		sample->status = xray_therm_read(
			card, base + XRAY_THERM_FBPA_STATUS, capture);
	}
	capture->opt_ts_dis_mask =
		xray_therm_read(card, XRAY_THERM_OPT_TS_DIS_MASK, capture);
	capture->status_opt_fbio =
		xray_therm_read(card, XRAY_THERM_STATUS_OPT_FBIO, capture);
	capture->status_opt_gpc =
		xray_therm_read(card, XRAY_THERM_STATUS_OPT_GPC, capture);
	capture->snapshot_as_found_selector_before =
		xray_therm_read(card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
	/* Preserve both selector observations but never consume the as-found
	 * selector-dependent snapshot payload in the passive phase. */
	capture->snapshot_as_found_selector_after =
		xray_therm_read(card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
	capture->pure_completed = 1;
}

static int xray_therm_finish_context(struct xray_therm_card *card,
				     struct xray_therm_capture *capture)
{
	xray_therm_read_context(card, capture, capture->ts_auto_exit,
				capture->sentinels_exit);
	capture->autopoll_unchanged = !memcmp(capture->ts_auto_entry,
					      capture->ts_auto_exit,
					      sizeof(capture->ts_auto_entry));
	capture->sentinels_unchanged =
		!memcmp(capture->sentinels_entry, capture->sentinels_exit,
			sizeof(capture->sentinels_entry));
	return capture->autopoll_unchanged && capture->sentinels_unchanged ?
		       0 :
		       -EUCLEAN;
}

static int xray_therm_wait_trigger(struct xray_therm_card *card,
				   struct xray_therm_capture *capture)
{
	unsigned int poll;

	for (poll = 0; poll < XRAY_THERM_TRIGGER_POLLS; poll++) {
		if (!(xray_therm_read(card, XRAY_THERM_SNAPSHOT_TRIGGER,
				      capture) &
		      XRAY_THERM_TRIGGER_BUSY)) {
			capture->trigger_self_cleared = 1;
			return 0;
		}
		udelay(1);
	}
	return -ETIMEDOUT;
}

/* On every path after selector capture, restoration is attempted and verified
 * without consulting pending signals. */
static int xray_therm_capture_snapshot(struct xray_therm_card *card,
				       struct xray_therm_capture *capture)
{
	unsigned int gpc, bjt, index, i;
	u32 saved_selector;
	bool sequence_complete = false;
	int ret = 0;

	saved_selector =
		xray_therm_read(card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
	capture->snapshot_selector_entry = saved_selector;
	/* Never write after observing a dead, reserved, or out-of-range selector.
	 * Restoring an arbitrary non-BADF word would itself be unsafe. */
	if (!XRAY_THERM_SELECTOR_IS_VALID(saved_selector))
		return -ENODEV;

	/* Close the long passive-phase liveness window: re-sample two independent
	 * affirmative GA100 signals after selector validation and immediately
	 * before the first write.  Both values are journaled in ABI5. */
	capture->prewrite_tsense =
		xray_therm_read(card, xray_therm_aggregate_offsets[0], capture);
	capture->prewrite_active_fbps =
		xray_therm_read(card, xray_therm_sentinel_offsets[0], capture);
	if (!XRAY_THERM_HAS_AFFIRMATIVE_LIVENESS(capture->prewrite_tsense,
						 capture->prewrite_active_fbps))
		return -ENODEV;

	/* Exactly one trigger write per requested semantic snapshot. */
	xray_therm_write_trigger(card, capture);
	ret = xray_therm_wait_trigger(card, capture);
	if (ret)
		goto restore_selector;

	for (gpc = 0; gpc < XRAY_THERM_GPC_COUNT; gpc++) {
		for (bjt = 0; bjt < XRAY_THERM_BJT_COUNT; bjt++) {
			struct xray_therm_gpc_bjt_raw *sample;
			u32 selector = (gpc << 16) | bjt;

			if (signal_pending(current)) {
				ret = -ERESTARTSYS;
				capture->failed_gpc = gpc;
				capture->failed_bjt = bjt;
				goto restore_selector;
			}
			index = gpc * XRAY_THERM_BJT_COUNT + bjt;
			sample = &capture->gpc_bjt[index];
			sample->selector = selector;
			xray_therm_write_selector(card, selector, capture);
			sample->selector_before = xray_therm_read(
				card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
			if (sample->selector_before != selector) {
				capture->selector_race = 1;
				capture->failed_gpc = gpc;
				capture->failed_bjt = bjt;
				ret = -EAGAIN;
				goto restore_selector;
			}
			sample->temperature = xray_therm_read(
				card, XRAY_THERM_SNAPSHOT_TEMP, capture);
			sample->offset_temperature = xray_therm_read(
				card, XRAY_THERM_SNAPSHOT_OFFSET_TEMP, capture);
			sample->selector_after = xray_therm_read(
				card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
			if (sample->selector_after != selector) {
				capture->selector_race = 1;
				capture->failed_gpc = gpc;
				capture->failed_bjt = bjt;
				ret = -EAGAIN;
				goto restore_selector;
			}
		}
	}
	for (i = 0; i < XRAY_THERM_SNAPSHOT_AGG_COUNT; i++)
		capture->snapshot_aggregate[i] = xray_therm_read(
			card, xray_therm_snapshot_aggregate_offsets[i],
			capture);
	sequence_complete = true;

restore_selector:
	/* Cleanup is mandatory and uninterruptible once saved_selector exists. */
	xray_therm_write_selector(card, saved_selector, capture);
	capture->snapshot_selector_exit =
		xray_therm_read(card, XRAY_THERM_SNAPSHOT_SELECTOR, capture);
	capture->selector_restored = capture->snapshot_selector_exit ==
				     saved_selector;
	if (!capture->selector_restored)
		ret = -EUCLEAN;
	if (xray_therm_finish_context(card, capture))
		ret = -EUCLEAN;
	if (!ret && sequence_complete)
		capture->snapshot_completed = 1;
	return ret;
}

static int xray_therm_ioctl_info(void __user *argp)
{
	struct xray_therm_info *info;
	unsigned int i;
	int ret;

	info = kzalloc(sizeof(*info), GFP_KERNEL);
	if (!info)
		return -ENOMEM;
	info->abi_version = XRAY_THERM_ABI_VERSION;
	info->struct_size = sizeof(*info);
	info->max_cards = XRAY_THERM_MAX_CARDS;
	info->capabilities = XRAY_THERM_CAP_PURE_CAPTURE |
			     XRAY_THERM_CAP_SEMANTIC_SNAPSHOT |
			     XRAY_THERM_CAP_BOUND_PURE_CAPTURE |
			     XRAY_THERM_CAP_STATEFUL_HBM_DATA |
			     XRAY_THERM_CAP_PASSIVE_SELECTOR_OMISSION |
			     XRAY_THERM_CAP_FRESH_PREWRITE_LIVENESS;
	if (allow_bound_snapshot)
		info->capabilities |= XRAY_THERM_CAP_BOUND_SNAPSHOT;

	for (i = 0; i < xray_therm_ncards; i++) {
		struct xray_therm_card *card = &xray_therm_cards[i];
		struct xray_therm_card_info *card_info =
			&info->cards[info->count];

		ret = mutex_lock_interruptible(&card->lock);
		if (ret)
			goto out;
		if (!device_trylock(&card->pdev->dev)) {
			mutex_unlock(&card->lock);
			ret = -EBUSY;
			goto out;
		}
		ret = xray_therm_attest_identity(card->pdev,
						 &card->mapped_identity);
		if (!ret)
			ret = xray_therm_validate_bar(card);
		if (!ret) {
			card_info->identity = card->mapped_identity;
			card_info->bar0_start = card->bar0_start;
			card_info->bar0_length = card->bar0_length;
			card_info->mapped_length = card->bar0_mapped_length;
			card_info->resource_flags = card->bar0_flags;
			card_info->driver_bound = xray_therm_driver_bound(card);
			card_info->current_state =
				(__force __u32)card->pdev->current_state;
			card_info->runtime_status =
				READ_ONCE(card->pdev->dev.power.runtime_status);
			info->count++;
		}
		device_unlock(&card->pdev->dev);
		mutex_unlock(&card->lock);
		if (ret)
			goto out;
	}
	ret = copy_to_user(argp, info, sizeof(*info)) ? -EFAULT : 0;
out:
	kfree(info);
	return ret;
}

static int xray_therm_ioctl_capture(struct file *file, void __user *argp)
{
	struct xray_therm_capture *capture;
	struct xray_therm_identity requested_identity;
	struct xray_therm_card *card;
	u32 requested_flags;
	bool region_claimed = false;
	bool bound;
	int pm_ref = 0;
	int ret;

	capture = kmalloc(sizeof(*capture), GFP_KERNEL);
	if (!capture)
		return -ENOMEM;
	if (copy_from_user(capture, argp, sizeof(*capture))) {
		ret = -EFAULT;
		goto free_capture;
	}
	if (capture->abi_version != XRAY_THERM_ABI_VERSION ||
	    capture->struct_size != sizeof(*capture)) {
		ret = -EINVAL;
		goto free_capture;
	}
	if (capture->flags &
	    ~(XRAY_THERM_CAPTURE_F_SNAPSHOT | XRAY_THERM_CAPTURE_F_ALLOW_BOUND |
	      XRAY_THERM_CAPTURE_F_HBM_DATA)) {
		ret = -EINVAL;
		goto free_capture;
	}
	if ((capture->flags & (XRAY_THERM_CAPTURE_F_ALLOW_BOUND |
			       XRAY_THERM_CAPTURE_F_HBM_DATA)) &&
	    !(capture->flags & XRAY_THERM_CAPTURE_F_SNAPSHOT)) {
		ret = -EINVAL;
		goto free_capture;
	}
	/* MMIO-writing semantic transactions require an explicitly write-opened
	 * descriptor.  _IOWR metadata does not make the VFS enforce f_mode for a
	 * character-device ioctl, so keep this boundary inside the helper. */
	if (XRAY_THERM_CAPTURE_REQUIRES_WRITE(capture->flags) &&
	    !(file->f_mode & FMODE_WRITE)) {
		ret = -EBADF;
		goto free_capture;
	}
	requested_identity = capture->identity;
	requested_flags = capture->flags;
	card = xray_therm_find_card(&requested_identity);
	if (!card) {
		ret = -ENODEV;
		goto free_capture;
	}

	memset(capture, 0, sizeof(*capture));
	capture->abi_version = XRAY_THERM_ABI_VERSION;
	capture->struct_size = sizeof(*capture);
	capture->identity = requested_identity;
	capture->flags = requested_flags;
	capture->failed_gpc = U32_MAX;
	capture->failed_bjt = U32_MAX;

	ret = mutex_lock_interruptible(&card->lock);
	if (ret)
		goto free_capture;
	if (!device_trylock(&card->pdev->dev)) {
		ret = -EBUSY;
		goto unlock_mutex;
	}
	ret = xray_therm_validate_live_device(card, &pm_ref);
	if (ret)
		goto unlock_device;
	bound = xray_therm_driver_bound(card);
	capture->driver_bound_at_entry = bound;
	if ((capture->flags & XRAY_THERM_CAPTURE_F_SNAPSHOT) && bound &&
	    (!(capture->flags & XRAY_THERM_CAPTURE_F_ALLOW_BOUND) ||
	     !allow_bound_snapshot)) {
		ret = -EBUSY;
		goto release_pm;
	}
	if (!bound) {
		ret = pci_request_region(card->pdev, 0, "170hx_xray_therm");
		if (ret)
			goto release_pm;
		region_claimed = true;
	}
	/* Exact D0, memory-enable, identity, and BAR recheck after all transaction
	 * resources are held and immediately before the first MMIO read. */
	ret = xray_therm_validate_device_state(card);
	if (ret)
		goto release_region;

	xray_therm_capture_pure(card, capture);
	if (!xray_therm_capture_has_liveness(capture)) {
		/* Preserve the entry/exit audit even on refusal, but issue no semantic
		 * write and return the failure in the capture journal. */
		(void)xray_therm_finish_context(card, capture);
		ret = -ENODEV;
	} else if (capture->flags & XRAY_THERM_CAPTURE_F_SNAPSHOT)
		ret = xray_therm_capture_snapshot(card, capture);
	else
		ret = xray_therm_finish_context(card, capture);

	/* Exact postflight state/identity/BAR recheck while every lock is held. */
	if (xray_therm_validate_device_state(card))
		ret = -EUCLEAN;

release_region:
	if (region_claimed)
		pci_release_region(card->pdev, 0);
release_pm:
	xray_therm_put_runtime_ref(card, pm_ref);
unlock_device:
	device_unlock(&card->pdev->dev);
unlock_mutex:
	mutex_unlock(&card->lock);
	capture->status = ret;
	if (copy_to_user(argp, capture, sizeof(*capture)))
		ret = -EFAULT;
	else
		ret = 0;
	/* Operational/refusal status is part of the returned journal. */
free_capture:
	kfree(capture);
	return ret;
}

static long xray_therm_ioctl(struct file *file, unsigned int command,
			     unsigned long argument)
{
	void __user *argp = (void __user *)argument;

	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	if (_IOC_TYPE(command) != XRAY_THERM_IOC_MAGIC)
		return -ENOTTY;
	switch (command) {
	case XRAY_THERM_IOC_INFO:
		return xray_therm_ioctl_info(argp);
	case XRAY_THERM_IOC_CAPTURE:
		return xray_therm_ioctl_capture(file, argp);
	default:
		return -ENOTTY;
	}
}

static int xray_therm_open(struct inode *inode, struct file *file)
{
	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	if (atomic_cmpxchg(&xray_therm_opened, 0, 1))
		return -EBUSY;
	return 0;
}

static int xray_therm_release(struct inode *inode, struct file *file)
{
	atomic_set(&xray_therm_opened, 0);
	return 0;
}

static const struct file_operations xray_therm_fops = {
	.owner = THIS_MODULE,
	.open = xray_therm_open,
	.release = xray_therm_release,
	.unlocked_ioctl = xray_therm_ioctl,
	.llseek = noop_llseek,
};

static struct miscdevice xray_therm_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "170hx_xray_therm",
	.fops = &xray_therm_fops,
	.mode = 0600,
};

static int xray_therm_map_card(struct pci_dev *pdev)
{
	struct xray_therm_card *card = &xray_therm_cards[xray_therm_ncards];
	struct xray_therm_identity identity;
	resource_size_t start, length;
	unsigned long flags;
	u32 lo, hi = 0, type;
	u64 decoded;
	int ret;

	if (!device_trylock(&pdev->dev))
		return -EBUSY;
	xray_therm_identity_from_pdev(pdev, &identity);
	ret = xray_therm_attest_identity(pdev, &identity);
	if (ret)
		goto unlock;
	start = pci_resource_start(pdev, 0);
	length = pci_resource_len(pdev, 0);
	flags = pci_resource_flags(pdev, 0);
	if (!start || length < XRAY_THERM_BAR_MIN_LENGTH ||
	    !(flags & IORESOURCE_MEM) || (flags & IORESOURCE_UNSET)) {
		ret = -ENXIO;
		goto unlock;
	}
	if (pci_read_config_dword(pdev, PCI_BASE_ADDRESS_0, &lo)) {
		ret = -EIO;
		goto unlock;
	}
	if (lo & PCI_BASE_ADDRESS_SPACE_IO) {
		ret = -EINVAL;
		goto unlock;
	}
	type = lo & PCI_BASE_ADDRESS_MEM_TYPE_MASK;
	if (type != PCI_BASE_ADDRESS_MEM_TYPE_32 &&
	    type != PCI_BASE_ADDRESS_MEM_TYPE_64) {
		ret = -EINVAL;
		goto unlock;
	}
	if (type == PCI_BASE_ADDRESS_MEM_TYPE_64 &&
	    pci_read_config_dword(pdev, PCI_BASE_ADDRESS_0 + 4, &hi)) {
		ret = -EIO;
		goto unlock;
	}
	decoded = (lo & PCI_BASE_ADDRESS_MEM_MASK) | ((u64)hi << 32);
	if (decoded != (u64)start) {
		ret = -ESTALE;
		goto unlock;
	}

	card->bar0 = pci_iomap_range(pdev, 0, 0, XRAY_THERM_BAR_MIN_LENGTH);
	if (!card->bar0) {
		ret = -ENOMEM;
		goto unlock;
	}
	ret = xray_therm_attest_identity(pdev, &identity);
	if (ret) {
		pci_iounmap(pdev, card->bar0);
		card->bar0 = NULL;
		goto unlock;
	}
	card->pdev = pci_dev_get(pdev);
	card->mapped_identity = identity;
	card->bar0_start = start;
	card->bar0_length = length;
	card->bar0_mapped_length = XRAY_THERM_BAR_MIN_LENGTH;
	card->bar0_flags = flags;
	mutex_init(&card->lock);
	xray_therm_ncards++;
	pr_info("170hx_xray_therm: mapped %s BAR0 %pa resource %pa aperture 0x%llx driver=%s\n",
		pci_name(pdev), &start, &length,
		(unsigned long long)XRAY_THERM_BAR_MIN_LENGTH,
		pdev->driver ? pdev->driver->name : "unbound");
	ret = 0;
unlock:
	device_unlock(&pdev->dev);
	return ret;
}

static void xray_therm_unmap_cards(void)
{
	while (xray_therm_ncards) {
		struct xray_therm_card *card =
			&xray_therm_cards[--xray_therm_ncards];

		pci_iounmap(card->pdev, card->bar0);
		pci_dev_put(card->pdev);
		memset(card, 0, sizeof(*card));
	}
}

static int __init xray_therm_init(void)
{
	struct pci_dev *pdev = NULL;
	int ret;

	BUILD_BUG_ON(sizeof(struct xray_therm_identity) != 32);
	BUILD_BUG_ON(sizeof(struct xray_therm_card_info) != 88);
	BUILD_BUG_ON(sizeof(struct xray_therm_info) != 1440);
	BUILD_BUG_ON(sizeof(struct xray_therm_fbpa_raw) != 32);
	BUILD_BUG_ON(sizeof(struct xray_therm_gpc_analog_raw) != 16);
	BUILD_BUG_ON(sizeof(struct xray_therm_fixed_context_raw) != 124);
	BUILD_BUG_ON(sizeof(struct xray_therm_gpc_bjt_raw) != 20);
	BUILD_BUG_ON(sizeof(struct xray_therm_capture) != 1792);
	BUILD_BUG_ON(offsetof(struct xray_therm_info, cards) != 32);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, aggregate) != 96);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, fixed_context) != 248);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, gpc_analog) != 372);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, fbpa) != 500);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, gpc_bjt) != 1268);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, status_opt_fbio) !=
		     1772);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture,
			      selector_dependent_skipped_mask) != 1776);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, hbm_data_read_mask) !=
		     1780);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture, prewrite_tsense) !=
		     1784);
	BUILD_BUG_ON(offsetof(struct xray_therm_capture,
			      prewrite_active_fbps) != 1788);
	BUILD_BUG_ON(XRAY_THERM_FBPA_COUNT > 32);
	BUILD_BUG_ON(XRAY_THERM_SELECTOR_READINCR != BIT(30));
	BUILD_BUG_ON(XRAY_THERM_TS_AUTO_POLLING != BIT(15));
	BUILD_BUG_ON(XRAY_THERM_SENTINEL1 + sizeof(u32) >
		     XRAY_THERM_BAR_MIN_LENGTH);
	BUILD_BUG_ON(PCI_VENDOR_ID != 0x00);

	get_random_bytes(&xray_therm_generation, sizeof(xray_therm_generation));
	if (!xray_therm_generation)
		xray_therm_generation = 1;
	for_each_pci_dev(pdev) {
		if (pdev->vendor != XRAY_THERM_VENDOR_NVIDIA ||
		    !xray_therm_supported_device(pdev->device) ||
		    (pdev->class >> 16) != PCI_BASE_CLASS_DISPLAY)
			continue;
		if (xray_therm_ncards >= XRAY_THERM_MAX_CARDS) {
			pci_dev_put(pdev);
			pdev = NULL;
			break;
		}
		ret = xray_therm_map_card(pdev);
		if (ret)
			pr_err("170hx_xray_therm: %s mapping rejected: %d\n",
			       pci_name(pdev), ret);
	}
	if (!xray_therm_ncards)
		return -ENODEV;
	ret = misc_register(&xray_therm_misc);
	if (ret) {
		xray_therm_unmap_cards();
		return ret;
	}
	pr_info("170hx_xray_therm: ABI %u, %u card(s), bound snapshots=%s\n",
		XRAY_THERM_ABI_VERSION, xray_therm_ncards,
		allow_bound_snapshot ? "enabled" : "disabled");
	return 0;
}

static void __exit xray_therm_exit(void)
{
	misc_deregister(&xray_therm_misc);
	xray_therm_unmap_cards();
}

module_init(xray_therm_init);
module_exit(xray_therm_exit);
