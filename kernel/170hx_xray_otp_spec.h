/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Hardware-inert OTP executor state-machine specification.
 *
 * This is intentionally not part of the 170hx_xray kernel module or UAPI.  It
 * contains no MMIO address, write primitive, GPIO operation, ioctl, or device
 * handle.  Its only purpose is to make safety-state transitions executable in
 * a host test.  A real backend is a separate, currently blocked design review.
 */
#ifndef _170HX_XRAY_OTP_SPEC_H
#define _170HX_XRAY_OTP_SPEC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define XRAY_OTP_SPEC_VERSION 1U
#define XRAY_OTP_REAL_HW_SUPPORTED 0U

/* Defining this can never turn the specification into a writer. */
#if defined(XRAY_OTP_ENABLE_REAL_HW)
#error "No qualified real-hardware OTP ABI exists"
#endif

/* Fault injection is host-test-only and cannot be enabled in a kernel build. */
#if defined(__KERNEL__) && defined(XRAY_OTP_SPEC_ENABLE_FAULT_INJECTION)
#error "OTP specification fault injection is forbidden in kernel builds"
#endif

enum xray_otp_spec_state {
	XRAY_OTP_NEW = 0,
	XRAY_OTP_PREFLIGHTED,
	XRAY_OTP_WATCHDOG_ARMED,
	XRAY_OTP_SOURCE_ASSERTED,
	XRAY_OTP_ROW_ARMED,
	XRAY_OTP_ATTEMPT_COMMITTED,
	XRAY_OTP_COMMAND_ISSUED,
	XRAY_OTP_COMMAND_COMPLETE,
	XRAY_OTP_SOURCE_LOW,
	XRAY_OTP_ROW_VERIFIED,
	XRAY_OTP_COMPLETE,
	XRAY_OTP_ABORTED,
	XRAY_OTP_QUARANTINED,
};

enum xray_otp_spec_event {
	XRAY_OTP_EV_PREFLIGHT = 0,
	XRAY_OTP_EV_ARM_WATCHDOG,
	XRAY_OTP_EV_ASSERT_SOURCE,
	XRAY_OTP_EV_ARM_ROW,
	XRAY_OTP_EV_COMMIT_ATTEMPT,
	XRAY_OTP_EV_ISSUE_COMMAND,
	XRAY_OTP_EV_COMMAND_COMPLETE,
	XRAY_OTP_EV_PROVE_SOURCE_LOW,
	XRAY_OTP_EV_VERIFY_ROW_AND_IMAGE,
	XRAY_OTP_EV_FINISH,
	XRAY_OTP_EV_ABORT,
};

enum xray_otp_spec_rc {
	XRAY_OTP_SPEC_OK = 0,
	XRAY_OTP_SPEC_ESTATE = -1,
	XRAY_OTP_SPEC_EBOUND = -2,
	XRAY_OTP_SPEC_ENOTBLANK = -3,
	XRAY_OTP_SPEC_ERETRY = -4,
	XRAY_OTP_SPEC_EFAULT = -5,
	XRAY_OTP_SPEC_EQUARANTINE = -6,
};

enum xray_otp_spec_fault {
	XRAY_OTP_FAULT_BEFORE_PREFLIGHT = 0,
	XRAY_OTP_FAULT_AFTER_SOURCE_ASSERT,
	XRAY_OTP_FAULT_AFTER_COMMAND_ISSUE,
	XRAY_OTP_FAULT_SOURCE_LOW_NOT_PROVEN,
	XRAY_OTP_FAULT_POSTVERIFY_MISMATCH,
};

struct xray_otp_spec_row {
	uint16_t row;
	uint32_t expected_old;
	uint32_t program_word;
};

struct xray_otp_spec_ctx {
	enum xray_otp_spec_state state;
	const struct xray_otp_spec_row *allowlist;
	size_t allowlist_count;
	size_t verified_count;
	uint16_t active_row;
	uint32_t active_word;
	uint32_t command_attempts;
	bool source_proven_low;
	bool watchdog_armed;
	bool outcome_uncertain;
	bool terminal;
};

static inline bool
xray_otp_spec_row_allowed(const struct xray_otp_spec_ctx *ctx, uint16_t row,
			  uint32_t word)
{
	size_t index;

	if (!ctx || !ctx->allowlist)
		return false;
	for (index = 0; index < ctx->allowlist_count; ++index) {
		const struct xray_otp_spec_row *entry = &ctx->allowlist[index];

		if (entry->row == row)
			return entry->expected_old == 0U &&
			       entry->program_word == word && word != 0U;
	}
	return false;
}

static inline int xray_otp_spec_init(struct xray_otp_spec_ctx *ctx,
				     const struct xray_otp_spec_row *allowlist,
				     size_t allowlist_count)
{
	size_t index;

	if (!ctx || !allowlist || allowlist_count == 0U)
		return XRAY_OTP_SPEC_EBOUND;
	for (index = 0; index < allowlist_count; ++index) {
		if (allowlist[index].expected_old != 0U ||
		    allowlist[index].program_word == 0U)
			return XRAY_OTP_SPEC_EBOUND;
		if (index != 0U &&
		    allowlist[index].row != allowlist[index - 1U].row + 1U)
			return XRAY_OTP_SPEC_EBOUND;
	}
	*ctx = (struct xray_otp_spec_ctx){
		.state = XRAY_OTP_NEW,
		.allowlist = allowlist,
		.allowlist_count = allowlist_count,
		.source_proven_low = true,
	};
	return XRAY_OTP_SPEC_OK;
}

/*
 * Model one row at a time.  COMMIT_ATTEMPT consumes the only attempt before the
 * command boundary.  ABORT after that point quarantines the session.  The
 * caller must model unconditional source-low cleanup separately; there is no
 * transition out of a terminal state and no retry transition.
 */
static inline int xray_otp_spec_step(struct xray_otp_spec_ctx *ctx,
				     enum xray_otp_spec_event event,
				     uint16_t row, uint32_t word,
				     uint32_t observed_old)
{
	if (!ctx || ctx->terminal)
		return XRAY_OTP_SPEC_ESTATE;

	if (event == XRAY_OTP_EV_ABORT) {
		ctx->terminal = true;
		if (ctx->outcome_uncertain || !ctx->source_proven_low) {
			ctx->state = XRAY_OTP_QUARANTINED;
			return XRAY_OTP_SPEC_EQUARANTINE;
		}
		ctx->state = XRAY_OTP_ABORTED;
		return XRAY_OTP_SPEC_EFAULT;
	}

	switch (ctx->state) {
	case XRAY_OTP_NEW:
	case XRAY_OTP_ROW_VERIFIED:
		if (event != XRAY_OTP_EV_PREFLIGHT)
			return XRAY_OTP_SPEC_ESTATE;
		if (ctx->verified_count >= ctx->allowlist_count)
			return XRAY_OTP_SPEC_EBOUND;
		if (!xray_otp_spec_row_allowed(ctx, row, word) ||
		    ctx->allowlist[ctx->verified_count].row != row)
			return XRAY_OTP_SPEC_EBOUND;
		if (observed_old != 0U)
			return XRAY_OTP_SPEC_ENOTBLANK;
		ctx->active_row = row;
		ctx->active_word = word;
		ctx->command_attempts = 0U;
		ctx->state = XRAY_OTP_PREFLIGHTED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_PREFLIGHTED:
		if (event != XRAY_OTP_EV_ARM_WATCHDOG ||
		    !ctx->source_proven_low)
			return XRAY_OTP_SPEC_ESTATE;
		ctx->watchdog_armed = true;
		ctx->state = XRAY_OTP_WATCHDOG_ARMED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_WATCHDOG_ARMED:
		if (event != XRAY_OTP_EV_ASSERT_SOURCE || !ctx->watchdog_armed)
			return XRAY_OTP_SPEC_ESTATE;
		ctx->source_proven_low = false;
		ctx->state = XRAY_OTP_SOURCE_ASSERTED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_SOURCE_ASSERTED:
		if (event != XRAY_OTP_EV_ARM_ROW || row != ctx->active_row ||
		    word != ctx->active_word)
			return XRAY_OTP_SPEC_EBOUND;
		ctx->state = XRAY_OTP_ROW_ARMED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_ROW_ARMED:
		if (event != XRAY_OTP_EV_COMMIT_ATTEMPT)
			return XRAY_OTP_SPEC_ESTATE;
		if (ctx->command_attempts != 0U)
			return XRAY_OTP_SPEC_ERETRY;
		ctx->command_attempts = 1U;
		ctx->outcome_uncertain = true;
		ctx->state = XRAY_OTP_ATTEMPT_COMMITTED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_ATTEMPT_COMMITTED:
		if (event != XRAY_OTP_EV_ISSUE_COMMAND)
			return XRAY_OTP_SPEC_ERETRY;
		ctx->state = XRAY_OTP_COMMAND_ISSUED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_COMMAND_ISSUED:
		if (event != XRAY_OTP_EV_COMMAND_COMPLETE)
			return XRAY_OTP_SPEC_ERETRY;
		ctx->state = XRAY_OTP_COMMAND_COMPLETE;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_COMMAND_COMPLETE:
		if (event != XRAY_OTP_EV_PROVE_SOURCE_LOW)
			return XRAY_OTP_SPEC_ESTATE;
		ctx->source_proven_low = true;
		ctx->watchdog_armed = false;
		ctx->state = XRAY_OTP_SOURCE_LOW;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_SOURCE_LOW:
		if (event != XRAY_OTP_EV_VERIFY_ROW_AND_IMAGE ||
		    row != ctx->active_row || word != ctx->active_word ||
		    observed_old != ctx->active_word)
			return XRAY_OTP_SPEC_EFAULT;
		ctx->outcome_uncertain = false;
		ctx->verified_count++;
		ctx->state = XRAY_OTP_ROW_VERIFIED;
		return XRAY_OTP_SPEC_OK;
	case XRAY_OTP_COMPLETE:
	case XRAY_OTP_ABORTED:
	case XRAY_OTP_QUARANTINED:
		return XRAY_OTP_SPEC_ESTATE;
	default:
		return XRAY_OTP_SPEC_ESTATE;
	}
}

static inline int xray_otp_spec_finish(struct xray_otp_spec_ctx *ctx)
{
	if (!ctx || ctx->terminal || ctx->state != XRAY_OTP_ROW_VERIFIED ||
	    ctx->verified_count != ctx->allowlist_count ||
	    !ctx->source_proven_low || ctx->outcome_uncertain)
		return XRAY_OTP_SPEC_ESTATE;
	ctx->state = XRAY_OTP_COMPLETE;
	ctx->terminal = true;
	return XRAY_OTP_SPEC_OK;
}

/*
 * Independent cleanup can still prove the source low after the logical session
 * is quarantined.  It never clears quarantine or uncertainty and never permits
 * a retry; it records only the electrical-safe observation in this model.
 */
static inline int
xray_otp_spec_emergency_source_low(struct xray_otp_spec_ctx *ctx,
				   bool independently_proven)
{
	if (!ctx)
		return XRAY_OTP_SPEC_ESTATE;
	if (!independently_proven) {
		ctx->source_proven_low = false;
		ctx->terminal = true;
		ctx->state = XRAY_OTP_QUARANTINED;
		return XRAY_OTP_SPEC_EQUARANTINE;
	}
	ctx->source_proven_low = true;
	ctx->watchdog_armed = false;
	return XRAY_OTP_SPEC_OK;
}

/* Compile-time-disabled, host-only fault hook.  It mutates only this struct. */
static inline int xray_otp_spec_inject_fault(struct xray_otp_spec_ctx *ctx,
					     enum xray_otp_spec_fault fault)
{
#if defined(XRAY_OTP_SPEC_ENABLE_FAULT_INJECTION)
	if (!ctx || ctx->terminal)
		return XRAY_OTP_SPEC_ESTATE;
	ctx->terminal = true;
	if (fault == XRAY_OTP_FAULT_BEFORE_PREFLIGHT &&
	    ctx->source_proven_low && !ctx->outcome_uncertain) {
		ctx->state = XRAY_OTP_ABORTED;
		return XRAY_OTP_SPEC_EFAULT;
	}
	ctx->state = XRAY_OTP_QUARANTINED;
	if (fault == XRAY_OTP_FAULT_AFTER_COMMAND_ISSUE ||
	    fault == XRAY_OTP_FAULT_POSTVERIFY_MISMATCH)
		ctx->outcome_uncertain = true;
	if (fault == XRAY_OTP_FAULT_SOURCE_LOW_NOT_PROVEN)
		ctx->source_proven_low = false;
	return XRAY_OTP_SPEC_EQUARANTINE;
#else
	(void)ctx;
	(void)fault;
	return XRAY_OTP_SPEC_ESTATE;
#endif
}

#endif /* _170HX_XRAY_OTP_SPEC_H */
