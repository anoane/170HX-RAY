// SPDX-License-Identifier: GPL-2.0
/*
 * 170hx_xray - bounded GA100/CMP 170HX observation helper.
 *
 * The BAR discovery and small ioctl-device shape descend from the GPL-2.0
 * matstatman/gpcprobe module (commit c68e7fc05df6fba27e2ee8c5caad82bc70adf03b).
 * This implementation fixes its 64-bit BAR test and adds full-domain identity,
 * verified BAR length, CAP_SYS_RAWIO, exclusive open, per-card serialization,
 * allowlisted readl, and helper-serialized/journaled MAIN_FF and RIR reads.
 *
 * Deliberately absent: generic writel, FUSEWDATA access, CMD_WRITE, re-sense,
 * software overrides, PCI-command changes, runtime-PM changes, and OTP writes.
 */

#include <linux/capability.h>
#include <linux/build_bug.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/pci_regs.h>
#include <linux/pm_runtime.h>
#include <linux/random.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "170hx_xray_uapi.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("170HX-XRAY contributors; gpcprobe lineage credited above");
MODULE_DESCRIPTION(
	"Bounded GA100 FF/RIR observation helper; no programming surface");
MODULE_VERSION("4.2.0");

#define XRAY_VENDOR_NVIDIA 0x10de
#define XRAY_DEVICE_A100_PCIE 0x20b5
#define XRAY_DEVICE_CMP170HX 0x20c2
#define XRAY_DEVICE_GA100_GLITCH 0x2080
#define XRAY_BAR_MIN_LENGTH 0x00a00000ULL

#define XRAY_PMC_BOOT0 0x00000000
#define XRAY_FUSECTRL 0x00820000
#define XRAY_FUSEADDR 0x00820004
#define XRAY_FUSERDATA 0x00820008
#define XRAY_EN_SW_OVERRIDE 0x00820040
#define XRAY_IFF_RECORD 0x00820050
#define XRAY_DEBUGCTRL 0x00820060
#define XRAY_CRC_STATUS 0x00820070
#define XRAY_ENABLE_PROGRAM_STATUS 0x00820078
#define XRAY_DISABLE_PROGRAM_STATUS 0x0082007c
#define XRAY_BYPASS_FUSES_STATUS 0x00820080
#define XRAY_DISABLE_SW_STATUS 0x00820084
#define XRAY_IFF_SW_FUSING 0x00820088
#define XRAY_DEBUGCTRL_PRIV_MASK 0x008200e0
#define XRAY_FUSEWDATA_PRIV_MASK 0x008200f4
#define XRAY_FUSECTRL_PRIV_MASK 0x008200f8
#define XRAY_OPT_PRIV_MASK 0x008200fc

#define XRAY_CTRL_CMD_MASK 0x00000003U
#define XRAY_CTRL_STATE_MASK 0x001f0000U
#define XRAY_CTRL_STATE_IDLE 0x00040000U
#define XRAY_CTRL_CMD_READ 0x00000001U
#define XRAY_CTRL_IDLE_VALUE 0xe0040000U
#define XRAY_DEBUG_RWL 0x00000002U
#define XRAY_ADDR_MASK 0x000001ffU
#define XRAY_CRC_CLEAN 0x00000055U
#define XRAY_POLL_TIMEOUT_MS 250U

static bool allow_bound_controller;
module_param(allow_bound_controller, bool, 0400);
MODULE_PARM_DESC(
	allow_bound_controller,
	"permit explicitly flagged controller reads while another driver owns "
	"the GPU (unsafe, default false)");

static bool allow_bound_read32 = true;
module_param(allow_bound_read32, bool, 0400);
MODULE_PARM_DESC(
	allow_bound_read32,
	"permit kernel-allowlisted readl while another driver owns the GPU (default true)");

static bool allow_a100_controller;
module_param(allow_a100_controller, bool, 0400);
MODULE_PARM_DESC(
	allow_a100_controller,
	"permit MAIN/RIR controller transactions on post-conversion 20b5 "
	"(unvalidated, default false; READ32 remains available)");

struct xray_card {
	struct pci_dev *pdev;
	u16 mapped_vendor;
	u16 mapped_device;
	void __iomem *bar0;
	resource_size_t bar0_start;
	resource_size_t bar0_length;
	resource_size_t bar0_mapped_length;
	unsigned long bar0_flags;
	struct mutex lock;
	struct xray_dump *last;
	bool last_valid;
	bool poisoned;
};

static struct xray_card xray_cards[XRAY_MAX_CARDS];
static unsigned int xray_ncards;
static u64 xray_generation;
static atomic_t xray_opened = ATOMIC_INIT(0);

/* Exact readl allowlist used by the Python observer. Controller command/data
 * ports that could mutate state (notably FUSEWDATA) are intentionally absent. */
static const u32 xray_read_offsets[] = {
	0x00000000, 0x00001720, 0x00001724, 0x00001728, 0x0000172c, 0x0000a800,
	0x00021c14, 0x0002241c, 0x00022430, 0x00022434, 0x00022438, 0x0002243c,
	0x0002244c, 0x00022454, 0x00022458, 0x0002246c, 0x00022470, 0x00100800,
	0x00100b88, 0x00100b8c, 0x00100b90, 0x00100b98, 0x00100ce0, 0x00100ec0,
	0x0010a7c0, 0x0010a7c4, 0x0010be00, 0x0010be40, 0x0010be80, 0x0010bec0,
	0x0010bf80, 0x0010bfc0, 0x00111240, 0x00118234, 0x00120060, 0x001200a0,
	0x001211fc, 0x00407000, 0x00407010, 0x00407020, 0x00407024, 0x00407054,
	0x00409664, 0x00409668, 0x00504204, 0x00820000, 0x00820004, 0x00820008,
	0x00820040, 0x00820050, 0x00820060, 0x00820068, 0x0082006c, 0x00820070,
	0x00820078, 0x0082007c, 0x00820080, 0x00820084, 0x00820088, 0x008200d0,
	0x008200d8, 0x008200dc, 0x008200e0, 0x008200f4, 0x008200f8, 0x008200fc,
	0x00820100, 0x00820104, 0x00820108, 0x0082010c, 0x00820110, 0x00820114,
	0x00820118, 0x0082011c, 0x00820120, 0x00820124, 0x00820128, 0x0082012c,
	0x00820130, 0x00820134, 0x00820138, 0x0082013c, 0x00820140, 0x00820144,
	0x00820148, 0x0082014c, 0x00820150, 0x00820154, 0x00820158, 0x0082015c,
	0x00820160, 0x00820164, 0x00820168, 0x0082016c, 0x00820170, 0x00820174,
	0x00820178, 0x0082017c, 0x00820180, 0x00820184, 0x00820188, 0x0082018c,
	0x00820190, 0x00820194, 0x00820198, 0x0082019c, 0x008201a0, 0x008201a4,
	0x008201a8, 0x008201ac, 0x008201b0, 0x008201b4, 0x008201b8, 0x008201bc,
	0x008201c0, 0x008201c4, 0x008201c8, 0x008201cc, 0x008201d0, 0x008201d4,
	0x008201d8, 0x008201dc, 0x008201e0, 0x008201e4, 0x008201e8, 0x008201ec,
	0x008201f0, 0x008201f4, 0x008201f8, 0x008201fc, 0x00820200, 0x00820204,
	0x00820208, 0x0082020c, 0x00820210, 0x00820214, 0x00820218, 0x0082021c,
	0x00820220, 0x00820224, 0x00820228, 0x0082022c, 0x00820230, 0x00820234,
	0x00820238, 0x0082023c, 0x00820240, 0x00820244, 0x00820248, 0x0082024c,
	0x00820250, 0x00820254, 0x00820258, 0x0082025c, 0x00820260, 0x00820264,
	0x00820268, 0x0082026c, 0x00820270, 0x00820274, 0x00820278, 0x0082027c,
	0x00820280, 0x00820284, 0x00820288, 0x0082028c, 0x00820290, 0x00820294,
	0x00820298, 0x0082029c, 0x008202a0, 0x008202a4, 0x008202a8, 0x008202ac,
	0x008202b0, 0x008202b4, 0x008202b8, 0x008202bc, 0x008202c0, 0x008202c4,
	0x008202c8, 0x008202cc, 0x008202d0, 0x008202d4, 0x008202d8, 0x008202dc,
	0x008202e0, 0x008202e4, 0x008202e8, 0x008202ec, 0x008202f0, 0x008202f4,
	0x008202f8, 0x008202fc, 0x00820300, 0x00820304, 0x00820308, 0x0082030c,
	0x00820310, 0x00820318, 0x00820320, 0x00820324, 0x00820328, 0x0082032c,
	0x00820330, 0x00820334, 0x00820338, 0x0082033c, 0x00820340, 0x00820344,
	0x00820348, 0x0082034c, 0x00820350, 0x00820354, 0x0082035c, 0x00820364,
	0x00820368, 0x0082036c, 0x00820370, 0x00820374, 0x00820378, 0x0082037c,
	0x00820380, 0x00820384, 0x00820388, 0x0082038c, 0x00820390, 0x00820394,
	0x00820398, 0x0082039c, 0x008203a0, 0x008203a4, 0x008203a8, 0x008203ac,
	0x008203b0, 0x008203b4, 0x008203bc, 0x008203c0, 0x008203c4, 0x008203c8,
	0x008203cc, 0x008203d0, 0x008203d4, 0x008203d8, 0x008203dc, 0x008203f0,
	0x008203f4, 0x008203f8, 0x008203fc, 0x00820400, 0x00820404, 0x00820408,
	0x0082040c, 0x00820410, 0x00820414, 0x00820418, 0x0082041c, 0x00820420,
	0x00820424, 0x00820428, 0x0082042c, 0x00820430, 0x00820434, 0x00820438,
	0x0082043c, 0x00820440, 0x00820444, 0x00820448, 0x0082044c, 0x00820450,
	0x00820454, 0x00820458, 0x0082045c, 0x00820460, 0x00820464, 0x00820468,
	0x0082046c, 0x00820470, 0x00820474, 0x00820478, 0x0082047c, 0x00820480,
	0x00820484, 0x00820488, 0x0082048c, 0x00820490, 0x00820494, 0x0082049c,
	0x008204a0, 0x008204a4, 0x008204a8, 0x008204ac, 0x008204b0, 0x008204b4,
	0x008204b8, 0x008204bc, 0x008204c0, 0x008204c4, 0x008204c8, 0x008204cc,
	0x008204d0, 0x008204d4, 0x008204d8, 0x008204dc, 0x008204e0, 0x008204e8,
	0x008204ec, 0x008204f0, 0x008204f4, 0x008204f8, 0x008204fc, 0x00820500,
	0x00820504, 0x00820508, 0x0082050c, 0x00820510, 0x00820514, 0x00820518,
	0x0082051c, 0x00820520, 0x00820524, 0x00820528, 0x0082052c, 0x00820530,
	0x00820534, 0x00820538, 0x0082053c, 0x00820540, 0x00820544, 0x00820548,
	0x0082054c, 0x00820550, 0x00820558, 0x0082055c, 0x00820560, 0x00820564,
	0x00820568, 0x0082056c, 0x00820570, 0x00820574, 0x00820578, 0x0082057c,
	0x00820580, 0x00820584, 0x00820588, 0x0082058c, 0x00820590, 0x00820594,
	0x00820598, 0x0082059c, 0x008205a0, 0x008205a4, 0x008205a8, 0x008205ac,
	0x008205b0, 0x008205b4, 0x008205b8, 0x008205bc, 0x008205c0, 0x008205c4,
	0x008205c8, 0x008205cc, 0x008205d0, 0x008205d4, 0x008205d8, 0x008205dc,
	0x008205e0, 0x008205e4, 0x008205e8, 0x008205ec, 0x008205f0, 0x008205f4,
	0x008205f8, 0x008205fc, 0x00820600, 0x00820604, 0x00820608, 0x0082060c,
	0x00820610, 0x00820614, 0x00820618, 0x0082061c, 0x00820620, 0x00820624,
	0x00820628, 0x00820630, 0x00820634, 0x00820638, 0x0082063c, 0x00820640,
	0x00820644, 0x00820648, 0x0082064c, 0x00820650, 0x00820654, 0x00820658,
	0x0082065c, 0x00820660, 0x00820664, 0x00820668, 0x0082066c, 0x00820670,
	0x00820674, 0x00820678, 0x0082067c, 0x00820680, 0x00820684, 0x00820688,
	0x0082068c, 0x00820690, 0x00820694, 0x00820698, 0x0082069c, 0x008206a0,
	0x008206a4, 0x008206a8, 0x008206ac, 0x008206b0, 0x008206b4, 0x008206b8,
	0x008206cc, 0x008206d0, 0x008206d4, 0x008206d8, 0x008206f4, 0x008206f8,
	0x008206fc, 0x00820700, 0x00820704, 0x00820708, 0x0082070c, 0x00820710,
	0x00820718, 0x0082071c, 0x00820720, 0x00820724, 0x00820728, 0x0082072c,
	0x00820730, 0x00820734, 0x00820738, 0x0082073c, 0x00820740, 0x00820744,
	0x00820748, 0x0082074c, 0x00820750, 0x00820754, 0x00820758, 0x0082075c,
	0x00820760, 0x00820764, 0x00820768, 0x0082076c, 0x00820770, 0x00820774,
	0x00820778, 0x0082077c, 0x00820788, 0x0082078c, 0x00820790, 0x00820794,
	0x00820798, 0x0082079c, 0x008207a0, 0x008207a4, 0x008207c0, 0x008207c4,
	0x008207c8, 0x008207cc, 0x008207d0, 0x008207d4, 0x008207d8, 0x008207dc,
	0x008207e0, 0x008207e4, 0x008207e8, 0x008207ec, 0x008207f0, 0x008207f4,
	0x008207f8, 0x008207fc, 0x00820800, 0x00820814, 0x00820818, 0x0082081c,
	0x00820820, 0x0082082c, 0x00820830, 0x00820834, 0x00820838, 0x0082083c,
	0x00820840, 0x00820844, 0x00820848, 0x0082084c, 0x00820850, 0x00820854,
	0x00820938, 0x00820968, 0x008209b8, 0x00820a40, 0x00820a44, 0x00820a48,
	0x00820a4c, 0x00820a50, 0x00820a54, 0x00820a58, 0x00820a5c, 0x00820b20,
	0x00820b24, 0x00820b28, 0x00820b2c, 0x00820b30, 0x00820b34, 0x00820b38,
	0x00820b3c, 0x00820b40, 0x00820b44, 0x00820b48, 0x00820b4c, 0x00820b50,
	0x00820b54, 0x00820b58, 0x00820b5c, 0x00820b60, 0x00820b64, 0x00820b68,
	0x00820b6c, 0x00820b7c, 0x00820b80, 0x00820b84, 0x00820b88, 0x00820b8c,
	0x00820b90, 0x00820b94, 0x00820b98, 0x00820b9c, 0x00820bd0, 0x00820bd4,
	0x00820bd8, 0x00820bdc, 0x00820be0, 0x00820be4, 0x00820be8, 0x00820bec,
	0x00820bf0, 0x00820bf4, 0x00820bf8, 0x00820bfc, 0x00820c00, 0x00820c04,
	0x00820c08, 0x00820c0c, 0x00820c10, 0x00820c14, 0x00820c18, 0x00820c1c,
	0x00820c20, 0x00820c24, 0x00820c28, 0x00820c2c, 0x00820c30, 0x00820c34,
	0x00820c38, 0x00820c3c, 0x00820c40, 0x00820c44, 0x00820c48, 0x00820c4c,
	0x00820c50, 0x00820c54, 0x00820c58, 0x00820c5c, 0x00820c60, 0x00820c64,
	0x00820c68, 0x00820c6c, 0x00820c70, 0x00820c74, 0x00820c78, 0x00820c7c,
	0x00820c80, 0x00820c84, 0x00820c88, 0x00820c8c, 0x00820c90, 0x00820c94,
	0x00820c98, 0x00820c9c, 0x00820ca0, 0x00820ca4, 0x00820ca8, 0x00820cac,
	0x00820cb0, 0x00820cb4, 0x00820d38, 0x00820d68, 0x00820d6c, 0x00820d70,
	0x00820d74, 0x00820d78, 0x00820d7c, 0x00820d80, 0x00820d84, 0x00820d88,
	0x00820d8c, 0x00820d90, 0x00820d94, 0x00820d98, 0x00820d9c, 0x00820da0,
	0x00820da4, 0x00820da8, 0x00820dac, 0x00820db8, 0x00820dbc, 0x00820dc0,
	0x00820dc4, 0x00820dc8, 0x00820dcc, 0x00820dd0, 0x00820dd4, 0x00820dd8,
	0x00820ddc, 0x00820de0, 0x00820de4, 0x00820de8, 0x00820dec, 0x00820df0,
	0x00820df4, 0x00820df8, 0x00820e00, 0x00820e04, 0x00820e08, 0x00820e0c,
	0x00820e10, 0x00820e14, 0x00820e18, 0x00820e1c, 0x00820e20, 0x00820e30,
	0x00820e34, 0x00820e38, 0x00821000, 0x00821004, 0x00821008, 0x0082100c,
	0x00821010, 0x00821014, 0x00821018, 0x0082101c, 0x00821020, 0x00821024,
	0x00821028, 0x0082102c, 0x00821030, 0x00821034, 0x00821038, 0x00821040,
	0x00821044, 0x00821048, 0x0082104c, 0x00821050, 0x00821054, 0x00821058,
	0x0082105c, 0x00821060, 0x00821064, 0x00821068, 0x0082106c, 0x00821070,
	0x00821074, 0x00821078, 0x0082107c, 0x00821080, 0x00821084, 0x00821088,
	0x0082108c, 0x00821090, 0x00821094, 0x00821098, 0x0082109c, 0x008210a0,
	0x008210a4, 0x008210a8, 0x008210ac, 0x008210b0, 0x008210b4, 0x008210b8,
	0x008210bc, 0x008210d0, 0x008210d4, 0x008210d8, 0x008210dc, 0x008210e0,
	0x008210e4, 0x008210e8, 0x008210ec, 0x008210f0, 0x008210f4, 0x008210f8,
	0x00821100, 0x00821104, 0x00821108, 0x00822800, 0x00822804, 0x00822808,
	0x0082280c, 0x00822810, 0x00822814, 0x00822818, 0x0082281c, 0x00823800,
	0x00823804, 0x00823808, 0x0082380c, 0x00823810, 0x00823814, 0x00823818,
	0x0082381c, 0x00823820, 0x00823824, 0x00823828, 0x0082382c, 0x00824100,
	0x00824104, 0x00824108, 0x0082410c, 0x00824110, 0x00824114, 0x00824118,
	0x0082411c, 0x00824120, 0x00824124, 0x00824128, 0x0082412c, 0x00824130,
	0x00824134, 0x00824138, 0x0082413c, 0x00824140, 0x00824144, 0x00824148,
	0x0082414c, 0x00824150, 0x00824154, 0x00824158, 0x0082415c, 0x00824160,
	0x00824164, 0x00824168, 0x0082416c, 0x00824170, 0x00824174, 0x00824178,
	0x0082417c, 0x00824180, 0x00824184, 0x00824188, 0x0082418c, 0x00824190,
	0x00824194, 0x00824198, 0x0082419c, 0x008241a0, 0x008241a4, 0x008241a8,
	0x008241ac, 0x008241b0, 0x008241b4, 0x008241b8, 0x008241bc, 0x008241c0,
	0x008241c4, 0x008241c8, 0x008241cc, 0x008241d0, 0x008241d4, 0x008241d8,
	0x008241dc, 0x008241e0, 0x008241e4, 0x008241e8, 0x008241ec, 0x008241f0,
	0x008241f4, 0x008241f8, 0x008241fc, 0x00824200, 0x00824204, 0x00824208,
	0x0082420c, 0x00824210, 0x00824214, 0x00824218, 0x0082421c, 0x00824220,
	0x00824224, 0x00824228, 0x0082422c, 0x00824230, 0x00824234, 0x00824238,
	0x0082423c, 0x00824240, 0x00824244, 0x00824248, 0x0082424c, 0x00824250,
	0x00824254, 0x00824258, 0x0082425c, 0x00824260, 0x00824264, 0x00824268,
	0x0082426c, 0x00824270, 0x00824274, 0x00824278, 0x0082427c, 0x00824280,
	0x00824284, 0x00824288, 0x0082428c, 0x00824290, 0x00824294, 0x00824298,
	0x0082429c, 0x008242a0, 0x008242a4, 0x008242a8, 0x008242ac, 0x008242b0,
	0x008242b4, 0x008242b8, 0x008242bc, 0x008242c0, 0x008242c4, 0x008242c8,
	0x008242cc, 0x008242d0, 0x008242d4, 0x008242d8, 0x008242dc, 0x008242e0,
	0x008242e4, 0x008242e8, 0x008242ec, 0x008242f0, 0x008242f4, 0x008242f8,
	0x008242fc, 0x00824304, 0x00824308, 0x0082430c, 0x00824310, 0x00824314,
	0x00824318, 0x0082431c, 0x00824320, 0x00824324, 0x00824328, 0x0082432c,
	0x00824330, 0x00824334, 0x00824338, 0x0082433c, 0x00824340, 0x00825c00,
	0x00900200, 0x00900204, 0x0090020c, 0x00903cb4, 0x00903cb8, 0x00903cc0,
	0x00903cc4, 0x00903cc8, 0x00904200, 0x00904204, 0x0090420c, 0x00907cb4,
	0x00907cb8, 0x00907cc0, 0x00907cc4, 0x00907cc8, 0x00908200, 0x00908204,
	0x0090820c, 0x0090bcb4, 0x0090bcb8, 0x0090bcc0, 0x0090bcc4, 0x0090bcc8,
	0x0090c200, 0x0090c204, 0x0090c20c, 0x0090fcb4, 0x0090fcb8, 0x0090fcc0,
	0x0090fcc4, 0x0090fcc8, 0x00910200, 0x00910204, 0x0091020c, 0x00913cb4,
	0x00913cb8, 0x00913cc0, 0x00913cc4, 0x00913cc8, 0x00914200, 0x00914204,
	0x0091420c, 0x00917cb4, 0x00917cb8, 0x00917cc0, 0x00917cc4, 0x00917cc8,
	0x00918200, 0x00918204, 0x0091820c, 0x0091bcb4, 0x0091bcb8, 0x0091bcc0,
	0x0091bcc4, 0x0091bcc8, 0x0091c200, 0x0091c204, 0x0091c20c, 0x0091fcb4,
	0x0091fcb8, 0x0091fcc0, 0x0091fcc4, 0x0091fcc8, 0x00920200, 0x00920204,
	0x0092020c, 0x00923cb4, 0x00923cb8, 0x00923cc0, 0x00923cc4, 0x00923cc8,
	0x00924200, 0x00924204, 0x0092420c, 0x00927cb4, 0x00927cb8, 0x00927cc0,
	0x00927cc4, 0x00927cc8, 0x00928200, 0x00928204, 0x0092820c, 0x0092bcb4,
	0x0092bcb8, 0x0092bcc0, 0x0092bcc4, 0x0092bcc8, 0x0092c200, 0x0092c204,
	0x0092c20c, 0x0092fcb4, 0x0092fcb8, 0x0092fcc0, 0x0092fcc4, 0x0092fcc8,
	0x00930200, 0x00930204, 0x0093020c, 0x00933cb4, 0x00933cb8, 0x00933cc0,
	0x00933cc4, 0x00933cc8, 0x00934200, 0x00934204, 0x0093420c, 0x00937cb4,
	0x00937cb8, 0x00937cc0, 0x00937cc4, 0x00937cc8, 0x00938200, 0x00938204,
	0x0093820c, 0x0093bcb4, 0x0093bcb8, 0x0093bcc0, 0x0093bcc4, 0x0093bcc8,
	0x0093c200, 0x0093c204, 0x0093c20c, 0x0093fcb4, 0x0093fcb8, 0x0093fcc0,
	0x0093fcc4, 0x0093fcc8, 0x00940200, 0x00940204, 0x0094020c, 0x00943cb4,
	0x00943cb8, 0x00943cc0, 0x00943cc4, 0x00943cc8, 0x00944200, 0x00944204,
	0x0094420c, 0x00947cb4, 0x00947cb8, 0x00947cc0, 0x00947cc4, 0x00947cc8,
	0x00948200, 0x00948204, 0x0094820c, 0x0094bcb4, 0x0094bcb8, 0x0094bcc0,
	0x0094bcc4, 0x0094bcc8, 0x0094c200, 0x0094c204, 0x0094c20c, 0x0094fcb4,
	0x0094fcb8, 0x0094fcc0, 0x0094fcc4, 0x0094fcc8, 0x00950200, 0x00950204,
	0x0095020c, 0x00953cb4, 0x00953cb8, 0x00953cc0, 0x00953cc4, 0x00953cc8,
	0x00954200, 0x00954204, 0x0095420c, 0x00957cb4, 0x00957cb8, 0x00957cc0,
	0x00957cc4, 0x00957cc8, 0x00958200, 0x00958204, 0x0095820c, 0x0095bcb4,
	0x0095bcb8, 0x0095bcc0, 0x0095bcc4, 0x0095bcc8, 0x0095c200, 0x0095c204,
	0x0095c20c, 0x0095fcb4, 0x0095fcb8, 0x0095fcc0, 0x0095fcc4, 0x0095fcc8,
	0x0098bc90, 0x0098bc98, 0x009a0164, 0x009a0200, 0x009a0204, 0x009a020c,
	0x009a0300, 0x009a0304, 0x009a0320, 0x009a0334, 0x009a0338, 0x009a038c,
	0x009a0470, 0x009a0838, 0x009a083c, 0x009a0974, 0x009a3c7c, 0x009a3cb4,
	0x009a3cb8, 0x009a3cc0, 0x009a3cc4, 0x009a3cc8,
};

static bool xray_offset_allowed(u32 offset)
{
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(xray_read_offsets); i++)
		if (xray_read_offsets[i] == offset)
			return true;
	return false;
}

/* device_lock must be held.  Config dword 0 is the host-visible PCI identity;
 * do not treat mutable struct pci_dev cache fields as independent evidence.
 * The mapped identity also prevents an endpoint that changed identity after
 * module load from inheriting the old BAR mapping and module generation. */
static int xray_attest_pci_identity(struct pci_dev *pdev, u16 expected_vendor,
				    u16 expected_device)
{
	u32 live_id;
	u16 live_vendor, live_device;

	if (pci_read_config_dword(pdev, PCI_VENDOR_ID, &live_id))
		return -EIO;
	live_vendor = live_id & 0xffffU;
	live_device = live_id >> 16;
	if (live_vendor != pdev->vendor || live_device != pdev->device ||
	    live_vendor != expected_vendor || live_device != expected_device)
		return -ESTALE;
	return 0;
}

static int xray_attest_card_identity(struct xray_card *card)
{
	return xray_attest_pci_identity(card->pdev, card->mapped_vendor,
					card->mapped_device);
}

static void xray_fill_identity(struct xray_card *card, struct xray_identity *id)
{
	struct pci_dev *pdev = card->pdev;

	memset(id, 0, sizeof(*id));
	id->generation = xray_generation;
	id->domain = pci_domain_nr(pdev->bus);
	id->bus = pdev->bus->number;
	id->devfn = pdev->devfn;
	id->vendor = pdev->vendor;
	id->device = pdev->device;
	id->subvendor = pdev->subsystem_vendor;
	id->subdevice = pdev->subsystem_device;
	id->revision = pdev->revision;
}

static bool xray_identity_matches(struct xray_card *card,
				  const struct xray_identity *expected)
{
	struct xray_identity actual;

	xray_fill_identity(card, &actual);
	return actual.generation == expected->generation &&
	       actual.domain == expected->domain &&
	       actual.bus == expected->bus && actual.devfn == expected->devfn &&
	       actual.vendor == expected->vendor &&
	       actual.device == expected->device &&
	       actual.subvendor == expected->subvendor &&
	       actual.subdevice == expected->subdevice &&
	       actual.revision == expected->revision;
}

static struct xray_card *xray_find_card(const struct xray_identity *identity)
{
	unsigned int i;

	for (i = 0; i < xray_ncards; i++)
		if (xray_identity_matches(&xray_cards[i], identity))
			return &xray_cards[i];
	return NULL;
}

static bool xray_driver_bound(struct xray_card *card)
{
	return READ_ONCE(card->pdev->driver) != NULL;
}

static u32 xray_rd(struct xray_card *card, u32 offset)
{
	return readl(card->bar0 + offset);
}

static void xray_wr(struct xray_card *card, u32 offset, u32 value,
		    struct xray_dump *journal)
{
	writel(value, card->bar0 + offset);
	/* Flush the posted write through the same BAR. */
	(void)readl(card->bar0 + offset);
	journal->writes_issued++;
}

static void xray_snapshot(struct xray_card *card,
			  struct xray_controller_snapshot *snapshot)
{
	snapshot->pmc_boot0 = xray_rd(card, XRAY_PMC_BOOT0);
	snapshot->fusectrl = xray_rd(card, XRAY_FUSECTRL);
	snapshot->fuseaddr = xray_rd(card, XRAY_FUSEADDR);
	snapshot->en_sw_override = xray_rd(card, XRAY_EN_SW_OVERRIDE);
	snapshot->iff_record = xray_rd(card, XRAY_IFF_RECORD);
	snapshot->debugctrl = xray_rd(card, XRAY_DEBUGCTRL);
	snapshot->crc_status = xray_rd(card, XRAY_CRC_STATUS);
	snapshot->enable_fuse_program_status =
		xray_rd(card, XRAY_ENABLE_PROGRAM_STATUS);
	snapshot->disable_fuse_program_status =
		xray_rd(card, XRAY_DISABLE_PROGRAM_STATUS);
	snapshot->bypass_fuses_status = xray_rd(card, XRAY_BYPASS_FUSES_STATUS);
	snapshot->disable_sw_override_status =
		xray_rd(card, XRAY_DISABLE_SW_STATUS);
	snapshot->iff_sw_fusing = xray_rd(card, XRAY_IFF_SW_FUSING);
	snapshot->debugctrl_priv_mask = xray_rd(card, XRAY_DEBUGCTRL_PRIV_MASK);
	snapshot->fusewdata_priv_mask = xray_rd(card, XRAY_FUSEWDATA_PRIV_MASK);
	snapshot->fusectrl_priv_mask = xray_rd(card, XRAY_FUSECTRL_PRIV_MASK);
	snapshot->opt_priv_mask = xray_rd(card, XRAY_OPT_PRIV_MASK);
}

static bool xray_controller_idle(u32 fusectrl)
{
	/* The three high completion indicators are part of the evidenced GA100
	 * idle contract.  Accepting CMD_NONE + STATE_IDLE alone would misclassify
	 * 0x00040000 as complete and could issue/restore around an unfinished
	 * controller transaction. */
	return fusectrl == XRAY_CTRL_IDLE_VALUE;
}

static int xray_wait_idle(struct xray_card *card)
{
	unsigned long deadline =
		jiffies + msecs_to_jiffies(XRAY_POLL_TIMEOUT_MS);

	do {
		u32 value = xray_rd(card, XRAY_FUSECTRL);

		if (xray_controller_idle(value))
			return 0;
		if (signal_pending(current))
			return -ERESTARTSYS;
		usleep_range(50, 100);
	} while (time_before(jiffies, deadline));
	return -ETIMEDOUT;
}

/* Cleanup must not abandon an in-flight controller command merely because the
 * caller received a signal.  This poll is still bounded, but deliberately does
 * not consult signal_pending(). */
static int xray_wait_idle_cleanup(struct xray_card *card)
{
	unsigned long deadline =
		jiffies + msecs_to_jiffies(XRAY_POLL_TIMEOUT_MS);

	do {
		if (xray_controller_idle(xray_rd(card, XRAY_FUSECTRL)))
			return 0;
		usleep_range(50, 100);
	} while (time_before(jiffies, deadline));
	return -ETIMEDOUT;
}

static int xray_validate_bar(struct xray_card *card)
{
	struct pci_dev *pdev = card->pdev;
	resource_size_t start = pci_resource_start(pdev, 0);
	resource_size_t length = pci_resource_len(pdev, 0);
	unsigned long flags = pci_resource_flags(pdev, 0);
	u32 lo, hi = 0, type;
	u64 decoded;

	if (!start || length < XRAY_BAR_MIN_LENGTH ||
	    !(flags & IORESOURCE_MEM) || (flags & IORESOURCE_UNSET))
		return -ENXIO;
	if (start != card->bar0_start || length != card->bar0_length ||
	    flags != card->bar0_flags)
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

/* device_lock must be held.  This does not change runtime-PM accounting. */
static int xray_validate_device_state(struct xray_card *card)
{
	struct pci_dev *pdev = card->pdev;
	u16 command;
	int ret;

	if (!pci_device_is_present(pdev) || pci_channel_offline(pdev))
		return -ENODEV;
	ret = xray_attest_card_identity(card);
	if (ret)
		return ret;
	if (pdev->current_state != PCI_D0)
		return -EHOSTDOWN;
	ret = pci_read_config_word(pdev, PCI_COMMAND, &command);
	if (ret)
		return ret;
	if (!(command & PCI_COMMAND_MEMORY))
		return -ENXIO;
	return xray_validate_bar(card);
}

/* device_lock must be held.  A positive pm_ref means the caller must release
 * the non-waking runtime-PM reference with pm_runtime_put_noidle(). */
static int xray_validate_live_device(struct xray_card *card, int *pm_ref)
{
	struct pci_dev *pdev = card->pdev;
	int ret;

	*pm_ref = 0;
	ret = xray_validate_device_state(card);
	if (ret)
		return ret;
	if (pm_runtime_enabled(&pdev->dev)) {
		ret = pm_runtime_get_if_active(&pdev->dev);
		if (ret <= 0)
			return ret < 0 ? ret : -EHOSTDOWN;
		*pm_ref = ret;
	}
	/* Revalidate after pinning in case runtime state changed at the boundary. */
	ret = xray_validate_device_state(card);
	if (ret)
		goto fail;
	return 0;

fail:
	if (*pm_ref > 0) {
		pm_runtime_put_noidle(&pdev->dev);
		*pm_ref = 0;
	}
	return ret;
}

static void xray_put_runtime_ref(struct xray_card *card, int pm_ref)
{
	if (pm_ref > 0)
		pm_runtime_put_noidle(&card->pdev->dev);
}

static int xray_preflight(struct xray_card *card, struct xray_dump *dump)
{
	u16 command;

	if (card->poisoned)
		return -EUCLEAN;
	if (xray_driver_bound(card) &&
	    (!(dump->flags & XRAY_DUMP_F_ALLOW_BOUND) ||
	     !allow_bound_controller))
		return -EBUSY;
	if (pci_read_config_word(card->pdev, PCI_COMMAND, &command))
		return -EIO;
	if (!(command & PCI_COMMAND_MEMORY))
		return -ENXIO;

	xray_snapshot(card, &dump->entry);
	if (dump->entry.pmc_boot0 == 0 || dump->entry.pmc_boot0 == 0xffffffff)
		return -ENXIO;
	if (!xray_controller_idle(dump->entry.fusectrl))
		return -EBUSY;
	if (dump->entry.debugctrl != 0 || (dump->entry.iff_sw_fusing & 1))
		return -EBUSY;
	if ((dump->flags & XRAY_DUMP_F_CLEAN_CRC) &&
	    (dump->entry.crc_status & 0x77) != XRAY_CRC_CLEAN)
		return -EBADMSG;
	/* IFF_RECORD is not, by itself, evidence of a software-IFF redirect.  It
	 * is nonzero on observed GA100s with both software controls inactive and
	 * can corroborate their live active physical-IFF interval; it is not OTP
	 * or sole boundary authority.  Ampere's software-IFF loader first enables
	 * EN_SW_OVERRIDE, then programs IFF_RECORD and pulses IFF_SW_FUSING.  A
	 * physical-view capture therefore requires both software controls to be
	 * inactive while preserving/reporting the observed interval. */
	if (dump->kind == XRAY_DUMP_MAIN &&
	    (dump->flags & XRAY_DUMP_F_NO_SW_IFF) &&
	    ((dump->entry.en_sw_override & 1) ||
	     (dump->entry.iff_sw_fusing & 1)))
		return -EBUSY;
	return 0;
}

static int xray_read_one(struct xray_card *card, u32 row, u32 *value,
			 struct xray_dump *dump)
{
	int ret;
	u32 address;

	if (!xray_controller_idle(xray_rd(card, XRAY_FUSECTRL)))
		return -EBUSY;
	address = xray_rd(card, XRAY_FUSEADDR);
	address = (address & ~XRAY_ADDR_MASK) | (row & XRAY_ADDR_MASK);
	xray_wr(card, XRAY_FUSEADDR, address, dump);
	if (xray_rd(card, XRAY_FUSEADDR) != address)
		return -EIO;
	xray_wr(card, XRAY_FUSECTRL, XRAY_CTRL_CMD_READ, dump);
	/* Count command issuance, not only successfully returned data. */
	dump->reads_issued++;
	ret = xray_wait_idle(card);
	if (ret)
		return ret;
	*value = xray_rd(card, XRAY_FUSERDATA);
	return 0;
}

static int xray_restore(struct xray_card *card, struct xray_dump *dump,
			bool rwl_changed)
{
	int ret = 0;
	u32 debug;

	/* Never write restoration state over a command that may still be active. */
	if (xray_wait_idle_cleanup(card)) {
		dump->debug_restored = 0;
		dump->address_restored = 0;
		return -EUCLEAN;
	}

	if (rwl_changed) {
		debug = xray_rd(card, XRAY_DEBUGCTRL);
		debug = (debug & ~XRAY_DEBUG_RWL) |
			(dump->entry.debugctrl & XRAY_DEBUG_RWL);
		xray_wr(card, XRAY_DEBUGCTRL, debug, dump);
		dump->debug_restored = (xray_rd(card, XRAY_DEBUGCTRL) ==
					dump->entry.debugctrl);
		if (!dump->debug_restored)
			ret = -EUCLEAN;
	} else {
		dump->debug_restored = 1;
	}

	xray_wr(card, XRAY_FUSEADDR, dump->entry.fuseaddr, dump);
	dump->address_restored =
		(xray_rd(card, XRAY_FUSEADDR) == dump->entry.fuseaddr);
	if (!dump->address_restored)
		ret = -EUCLEAN;
	if (!xray_controller_idle(xray_rd(card, XRAY_FUSECTRL)))
		ret = -EUCLEAN;
	return ret;
}

static bool xray_critical_unchanged(const struct xray_dump *dump)
{
	const struct xray_controller_snapshot *a = &dump->entry;
	const struct xray_controller_snapshot *b = &dump->exit;

	return a->pmc_boot0 == b->pmc_boot0 &&
	       (((a->fusectrl ^ b->fusectrl) &
		 ~(XRAY_CTRL_CMD_MASK | XRAY_CTRL_STATE_MASK)) == 0) &&
	       a->fuseaddr == b->fuseaddr &&
	       a->en_sw_override == b->en_sw_override &&
	       a->iff_record == b->iff_record && a->debugctrl == b->debugctrl &&
	       a->crc_status == b->crc_status &&
	       a->enable_fuse_program_status == b->enable_fuse_program_status &&
	       a->disable_fuse_program_status ==
		       b->disable_fuse_program_status &&
	       a->bypass_fuses_status == b->bypass_fuses_status &&
	       a->disable_sw_override_status == b->disable_sw_override_status &&
	       a->iff_sw_fusing == b->iff_sw_fusing &&
	       a->debugctrl_priv_mask == b->debugctrl_priv_mask &&
	       a->fusewdata_priv_mask == b->fusewdata_priv_mask &&
	       a->fusectrl_priv_mask == b->fusectrl_priv_mask &&
	       a->opt_priv_mask == b->opt_priv_mask &&
	       xray_controller_idle(b->fusectrl);
}

static void xray_run_dump(struct xray_card *card, struct xray_dump *dump)
{
	u32 pass, row, rows;
	int ret, restore_ret;
	bool rwl_changed = false;
	bool transaction_started = false;
	bool force_poison = false;

	dump->status = 0;
	dump->failed_pass = U32_MAX;
	dump->failed_row = U32_MAX;
	dump->driver_bound_at_entry = xray_driver_bound(card);
	rows = dump->kind == XRAY_DUMP_MAIN ? XRAY_MAIN_ROWS : XRAY_RIR_ROWS;
	dump->row_count = rows;

	ret = xray_preflight(card, dump);
	if (ret)
		goto out;
	transaction_started = true;

	if (dump->kind == XRAY_DUMP_RIR) {
		xray_wr(card, XRAY_DEBUGCTRL,
			dump->entry.debugctrl | XRAY_DEBUG_RWL, dump);
		rwl_changed = true;
		if (xray_rd(card, XRAY_DEBUGCTRL) !=
		    (dump->entry.debugctrl | XRAY_DEBUG_RWL)) {
			ret = -EIO;
			force_poison = true;
			goto restore;
		}
	}

	for (pass = 0; pass < XRAY_MAX_PASSES; pass++) {
		for (row = 0; row < rows; row++) {
			if (signal_pending(current)) {
				ret = -ERESTARTSYS;
				goto failed_row;
			}
			if (xray_rd(card, XRAY_CRC_STATUS) !=
				    dump->entry.crc_status ||
			    xray_rd(card, XRAY_EN_SW_OVERRIDE) !=
				    dump->entry.en_sw_override ||
			    xray_rd(card, XRAY_IFF_RECORD) !=
				    dump->entry.iff_record ||
			    xray_rd(card, XRAY_IFF_SW_FUSING) !=
				    dump->entry.iff_sw_fusing ||
			    xray_rd(card, XRAY_DEBUGCTRL) !=
				    (dump->entry.debugctrl |
				     (dump->kind == XRAY_DUMP_RIR ?
					      XRAY_DEBUG_RWL :
					      0))) {
				ret = -EUCLEAN;
				force_poison = true;
				goto failed_row;
			}
			ret = xray_read_one(card, row, &dump->words[pass][row],
					    dump);
			if (ret) {
				/* A failed address latch, command, or completion leaves
				 * controller intent uncertain.  Quarantine every such
				 * failure, not only timeouts/busy returns. */
				force_poison = true;
				goto failed_row;
			}
		}
		dump->completed_passes++;
	}
	goto restore;

failed_row:
	dump->failed_pass = pass;
	dump->failed_row = row;
restore:
	restore_ret = xray_restore(card, dump, rwl_changed);
	xray_snapshot(card, &dump->exit);
	if (restore_ret || !xray_critical_unchanged(dump)) {
		ret = -EUCLEAN;
		force_poison = true;
	}
	if (!ret && dump->completed_passes == XRAY_MAX_PASSES) {
		dump->coherent = !memcmp(dump->words[0], dump->words[1],
					 rows * sizeof(u32));
		if (!dump->coherent) {
			ret = -EILSEQ;
			force_poison = true;
		}
	}
out:
	dump->status = ret;
	if (transaction_started && force_poison) {
		card->poisoned = true;
		pr_err("170hx_xray: %s transaction/read/restore/context "
		       "failure; card poisoned for this module instance; "
		       "module reload does not prove cold recovery\n",
		       pci_name(card->pdev));
	}
}

static long xray_ioctl_info(unsigned long arg)
{
	struct xray_info *info;
	unsigned int i;
	long ret = 0;

	info = kvzalloc(sizeof(*info), GFP_KERNEL);
	if (!info)
		return -ENOMEM;
	info->abi_version = XRAY_ABI_VERSION;
	info->struct_size = sizeof(*info);
	info->capabilities = XRAY_CAP_READ32 | XRAY_CAP_DUMP_MAIN |
			     XRAY_CAP_DUMP_RIR | XRAY_CAP_LAST_JOURNAL;
	if (allow_bound_read32)
		info->capabilities |= XRAY_CAP_BOUND_READ32;
	if (allow_bound_controller)
		info->capabilities |= XRAY_CAP_BOUND_CONTROLLER;
	if (allow_a100_controller)
		info->capabilities |= XRAY_CAP_A100_CONTROLLER;
	info->count = xray_ncards;
	info->max_cards = XRAY_MAX_CARDS;
	info->module_generation = xray_generation;
	for (i = 0; i < xray_ncards; i++) {
		struct xray_card *card = &xray_cards[i];
		struct xray_card_info *ci = &info->cards[i];

		if (!device_trylock(&card->pdev->dev)) {
			ret = -EBUSY;
			goto free;
		}
		ret = xray_attest_card_identity(card);
		if (ret) {
			device_unlock(&card->pdev->dev);
			goto free;
		}
		xray_fill_identity(card, &ci->identity);
		ci->class_code = card->pdev->class;
		if (pci_read_config_word(card->pdev, PCI_COMMAND,
					 &ci->pci_command)) {
			device_unlock(&card->pdev->dev);
			ret = -EIO;
			goto free;
		}
		ci->driver_bound = xray_driver_bound(card);
		ci->runtime_status =
			READ_ONCE(card->pdev->dev.power.runtime_status);
		ci->bar0_start = card->bar0_start;
		ci->bar0_length = card->bar0_length;
		if (card->pdev->driver)
			strscpy(ci->driver, card->pdev->driver->name,
				sizeof(ci->driver));
		device_unlock(&card->pdev->dev);
	}
	if (copy_to_user((void __user *)arg, info, sizeof(*info))) {
		ret = -EFAULT;
		goto free;
	}
free:
	kvfree(info);
	return ret;
}

static long xray_ioctl_read32(unsigned long arg)
{
	struct xray_reg32 request;
	struct xray_card *card;
	long ret = 0;
	int pm_ref = 0;
	bool device_locked = false;
	bool region_claimed = false;

	if (copy_from_user(&request, (void __user *)arg, sizeof(request)))
		return -EFAULT;
	if (request.abi_version != XRAY_ABI_VERSION ||
	    request.struct_size != sizeof(request))
		return -EPROTO;
	card = xray_find_card(&request.identity);
	if (!card)
		return -ENODEV;
	if ((request.offset & 3) || !xray_offset_allowed(request.offset) ||
	    request.offset > card->bar0_mapped_length - sizeof(u32))
		return -ERANGE;
	if (mutex_lock_interruptible(&card->lock))
		return -ERESTARTSYS;
	if (!device_trylock(&card->pdev->dev)) {
		ret = -EBUSY;
		goto unlock;
	}
	device_locked = true;
	if (!xray_identity_matches(card, &request.identity)) {
		ret = -ESTALE;
		goto unlock_device;
	}
	xray_fill_identity(card, &request.identity);
	ret = xray_validate_live_device(card, &pm_ref);
	if (ret)
		goto unlock_device;
	if (xray_driver_bound(card) && !allow_bound_read32) {
		ret = -EBUSY;
		goto put_pm;
	}
	if (!xray_driver_bound(card)) {
		ret = pci_request_region(card->pdev, 0, "170hx_xray-read32");
		if (ret)
			goto put_pm;
		region_claimed = true;
	}
	/* Transaction-entry boundary: close the interval between the general
	 * state validation/region claim and the actual MMIO read. */
	ret = xray_attest_card_identity(card);
	if (ret)
		goto release_region;
	request.value = xray_rd(card, request.offset);
	/* Transaction-exit boundary; xray_validate_device_state() re-attests
	 * config dword 0 before accepting the result. */
	ret = xray_validate_device_state(card);
release_region:
	if (region_claimed)
		pci_release_region(card->pdev, 0);
put_pm:
	xray_put_runtime_ref(card, pm_ref);
unlock_device:
	if (device_locked)
		device_unlock(&card->pdev->dev);
unlock:
	mutex_unlock(&card->lock);
	if (ret)
		return ret;
	return copy_to_user((void __user *)arg, &request, sizeof(request)) ?
		       -EFAULT :
		       0;
}

static long xray_ioctl_dump(unsigned long arg)
{
	struct xray_dump *request;
	struct xray_card *card;
	struct xray_identity identity;
	u32 kind, flags, requested_passes;
	long ret = 0;
	int pm_ref = 0;
	bool device_locked = false;
	bool region_claimed = false;
	bool poisoned_before;

	request = kvmalloc(sizeof(*request), GFP_KERNEL | __GFP_ZERO);
	if (!request)
		return -ENOMEM;
	if (copy_from_user(request, (void __user *)arg, sizeof(*request))) {
		ret = -EFAULT;
		goto free;
	}
	if (request->abi_version != XRAY_ABI_VERSION ||
	    request->struct_size != sizeof(*request) ||
	    request->requested_passes != XRAY_MAX_PASSES ||
	    (request->kind != XRAY_DUMP_MAIN &&
	     request->kind != XRAY_DUMP_RIR) ||
	    (request->flags &
	     ~(XRAY_DUMP_F_ALLOW_BOUND | XRAY_DUMP_F_NO_SW_IFF |
	       XRAY_DUMP_F_CLEAN_CRC))) {
		ret = -EINVAL;
		goto free;
	}

	/* Retain only validated request fields.  Every response/journal byte is
	 * initialized by the kernel, never inherited from the caller. */
	identity = request->identity;
	kind = request->kind;
	flags = request->flags;
	requested_passes = request->requested_passes;
	memset(request, 0, sizeof(*request));
	request->abi_version = XRAY_ABI_VERSION;
	request->struct_size = sizeof(*request);
	request->identity = identity;
	request->kind = kind;
	request->flags = flags;
	request->requested_passes = requested_passes;
	request->failed_pass = U32_MAX;
	request->failed_row = U32_MAX;

	card = xray_find_card(&identity);
	if (!card) {
		ret = -ENODEV;
		goto free;
	}
	if (mutex_lock_interruptible(&card->lock)) {
		ret = -ERESTARTSYS;
		goto free;
	}
	if (!device_trylock(&card->pdev->dev)) {
		ret = -EBUSY;
		goto unlock;
	}
	device_locked = true;
	if (!xray_identity_matches(card, &request->identity)) {
		ret = -ESTALE;
		goto unlock_device;
	}
	if (card->pdev->device == XRAY_DEVICE_A100_PCIE &&
	    !allow_a100_controller) {
		ret = -EOPNOTSUPP;
		goto unlock_device;
	}
	/* Remove caller-controlled reserved bytes from every returned/journaled
	 * identity after the semantic match succeeds. */
	xray_fill_identity(card, &request->identity);
	ret = xray_validate_live_device(card, &pm_ref);
	if (ret)
		goto unlock_device;
	if (!xray_driver_bound(card)) {
		ret = pci_request_region(card->pdev, 0,
					 "170hx_xray-controller");
		if (ret)
			goto put_pm;
		region_claimed = true;
	}
	/* Transaction-entry boundary immediately before any controller MMIO. */
	ret = xray_attest_card_identity(card);
	if (ret)
		goto release_region;
	poisoned_before = card->poisoned;
	xray_run_dump(card, request);
	/* Transaction-exit boundary; any identity/config failure poisons the
	 * mapped card and invalidates this dump. */
	if (xray_validate_device_state(card)) {
		request->status = -EUCLEAN;
		card->poisoned = true;
		pr_err("170hx_xray: %s postflight PCI/BAR validation failed; "
		       "card poisoned for this module instance; module reload "
		       "does not prove cold recovery\n",
		       pci_name(card->pdev));
	}
	/* Preserve the causal poison journal across later refused retries. */
	if (!poisoned_before || !card->last_valid) {
		memcpy(card->last, request, sizeof(*request));
		card->last_valid = true;
	}
release_region:
	if (region_claimed)
		pci_release_region(card->pdev, 0);
put_pm:
	xray_put_runtime_ref(card, pm_ref);
unlock_device:
	if (device_locked)
		device_unlock(&card->pdev->dev);
unlock:
	mutex_unlock(&card->lock);
	if (!ret && copy_to_user((void __user *)arg, request, sizeof(*request)))
		ret = -EFAULT;
free:
	kvfree(request);
	return ret;
}

static long xray_ioctl_last(unsigned long arg)
{
	struct xray_dump *request;
	struct xray_card *card;
	long ret = 0;

	request = kvmalloc(sizeof(*request), GFP_KERNEL | __GFP_ZERO);
	if (!request)
		return -ENOMEM;
	if (copy_from_user(request, (void __user *)arg, sizeof(*request))) {
		ret = -EFAULT;
		goto free;
	}
	if (request->abi_version != XRAY_ABI_VERSION ||
	    request->struct_size != sizeof(*request)) {
		ret = -EPROTO;
		goto free;
	}
	card = xray_find_card(&request->identity);
	if (!card) {
		ret = -ENODEV;
		goto free;
	}
	if (mutex_lock_interruptible(&card->lock)) {
		ret = -ERESTARTSYS;
		goto free;
	}
	if (!xray_identity_matches(card, &request->identity)) {
		ret = -ESTALE;
		goto unlock;
	}
	if (!card->last_valid) {
		ret = -ENODATA;
		goto unlock;
	}
	memcpy(request, card->last, sizeof(*request));
unlock:
	mutex_unlock(&card->lock);
	if (!ret && copy_to_user((void __user *)arg, request, sizeof(*request)))
		ret = -EFAULT;
free:
	kvfree(request);
	return ret;
}

static long xray_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	switch (cmd) {
	case XRAY_IOC_INFO:
		return xray_ioctl_info(arg);
	case XRAY_IOC_READ32:
		return xray_ioctl_read32(arg);
	case XRAY_IOC_DUMP:
		return xray_ioctl_dump(arg);
	case XRAY_IOC_LAST:
		return xray_ioctl_last(arg);
	default:
		return -ENOTTY;
	}
}

static int xray_open(struct inode *inode, struct file *file)
{
	int ret;

	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	if (atomic_cmpxchg(&xray_opened, 0, 1) != 0)
		return -EBUSY;
	ret = nonseekable_open(inode, file);
	if (ret)
		atomic_set(&xray_opened, 0);
	return ret;
}

static int xray_release(struct inode *inode, struct file *file)
{
	atomic_set(&xray_opened, 0);
	return 0;
}

static const struct file_operations xray_fops = {
	.owner = THIS_MODULE,
	.open = xray_open,
	.release = xray_release,
	.unlocked_ioctl = xray_ioctl,
	.llseek = noop_llseek,
};

static struct miscdevice xray_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "170hx_xray",
	.fops = &xray_fops,
	.mode = 0600,
};

static int xray_map_card(struct pci_dev *pdev)
{
	struct xray_card *card = &xray_cards[xray_ncards];
	resource_size_t start, length;
	unsigned long flags;
	u16 mapped_vendor, mapped_device;
	u32 lo, hi = 0, type;
	u64 decoded;
	int ret = 0;

	if (!device_trylock(&pdev->dev))
		return -EBUSY;
	ret = xray_attest_pci_identity(pdev, pdev->vendor, pdev->device);
	if (ret)
		goto unlock;
	mapped_vendor = pdev->vendor;
	mapped_device = pdev->device;
	start = pci_resource_start(pdev, 0);
	length = pci_resource_len(pdev, 0);
	flags = pci_resource_flags(pdev, 0);
	if (!start || length < XRAY_BAR_MIN_LENGTH ||
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

	card->bar0 = pci_iomap_range(pdev, 0, 0, XRAY_BAR_MIN_LENGTH);
	if (!card->bar0) {
		ret = -ENOMEM;
		goto unlock;
	}
	ret = xray_attest_pci_identity(pdev, mapped_vendor, mapped_device);
	if (ret) {
		pci_iounmap(pdev, card->bar0);
		card->bar0 = NULL;
		goto unlock;
	}
	card->pdev = pci_dev_get(pdev);
	card->mapped_vendor = mapped_vendor;
	card->mapped_device = mapped_device;
	card->bar0_start = start;
	card->bar0_length = length;
	card->bar0_mapped_length = XRAY_BAR_MIN_LENGTH;
	card->bar0_flags = flags;
	mutex_init(&card->lock);
	card->last = kvzalloc(sizeof(*card->last), GFP_KERNEL);
	if (!card->last) {
		pci_iounmap(pdev, card->bar0);
		pci_dev_put(card->pdev);
		memset(card, 0, sizeof(*card));
		ret = -ENOMEM;
		goto unlock;
	}
	xray_ncards++;
	pr_info("170hx_xray: mapped %s BAR0 %pa resource %pa aperture 0x%llx driver=%s\n",
		pci_name(pdev), &start, &length,
		(unsigned long long)XRAY_BAR_MIN_LENGTH,
		pdev->driver ? pdev->driver->name : "unbound");
unlock:
	device_unlock(&pdev->dev);
	return ret;
}

static void xray_unmap_cards(void)
{
	while (xray_ncards) {
		struct xray_card *card = &xray_cards[--xray_ncards];

		kvfree(card->last);
		pci_iounmap(card->pdev, card->bar0);
		pci_dev_put(card->pdev);
		memset(card, 0, sizeof(*card));
	}
}

static int __init xray_init(void)
{
	struct pci_dev *pdev = NULL;
	int ret;

	/* ABI 6 is native 64-bit only.  Keep its LP64 layout mechanically locked;
	 * no compat_ioctl is advertised. */
	BUILD_BUG_ON(sizeof(struct xray_identity) != 32);
	BUILD_BUG_ON(sizeof(struct xray_card_info) != 88);
	BUILD_BUG_ON(sizeof(struct xray_info) != 1440);
	BUILD_BUG_ON(sizeof(struct xray_reg32) != 48);
	BUILD_BUG_ON(sizeof(struct xray_controller_snapshot) != 64);
	BUILD_BUG_ON(sizeof(struct xray_dump) != 4320);
	BUILD_BUG_ON(PCI_VENDOR_ID != 0x00);
	BUILD_BUG_ON(offsetof(struct xray_info, cards) != 32);
	BUILD_BUG_ON(offsetof(struct xray_reg32, offset) != 40);
	BUILD_BUG_ON(offsetof(struct xray_dump, entry) != 96);
	BUILD_BUG_ON(offsetof(struct xray_dump, words) != 224);

	get_random_bytes(&xray_generation, sizeof(xray_generation));
	if (!xray_generation)
		xray_generation = 1;
	for_each_pci_dev(pdev) {
		if (pdev->vendor != XRAY_VENDOR_NVIDIA)
			continue;
		if (pdev->device != XRAY_DEVICE_A100_PCIE &&
		    pdev->device != XRAY_DEVICE_CMP170HX &&
		    pdev->device != XRAY_DEVICE_GA100_GLITCH)
			continue;
		if ((pdev->class >> 16) != PCI_BASE_CLASS_DISPLAY)
			continue;
		if (xray_ncards >= XRAY_MAX_CARDS) {
			pci_dev_put(pdev);
			pdev = NULL;
			break;
		}
		ret = xray_map_card(pdev);
		if (ret)
			pr_err("170hx_xray: %s mapping rejected: %d\n",
			       pci_name(pdev), ret);
	}
	if (!xray_ncards)
		return -ENODEV;
	ret = misc_register(&xray_misc);
	if (ret) {
		xray_unmap_cards();
		return ret;
	}
	pr_info("170hx_xray: ABI %u, %u card(s), controller while bound=%s, "
		"20b5 controller=%s; no OTP surface\n",
		XRAY_ABI_VERSION, xray_ncards,
		allow_bound_controller ? "enabled" : "disabled",
		allow_a100_controller ? "enabled" : "disabled");
	return 0;
}

static void __exit xray_exit(void)
{
	misc_deregister(&xray_misc);
	xray_unmap_cards();
}

module_init(xray_init);
module_exit(xray_exit);
