/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stdio.h>

#include "170hx_xray_otp_spec.h"

static void step_ok(struct xray_otp_spec_ctx *ctx,
		    enum xray_otp_spec_event event, uint16_t row, uint32_t word,
		    uint32_t observed)
{
	assert(xray_otp_spec_step(ctx, event, row, word, observed) ==
	       XRAY_OTP_SPEC_OK);
}

static void test_success(void)
{
	const struct xray_otp_spec_row rows[] = {
		{ 296U, 0U, 0x80070141U },
		{ 297U, 0U, 0x06010181U },
	};
	struct xray_otp_spec_ctx ctx;
	size_t index;

	assert(xray_otp_spec_init(&ctx, rows, 2U) == XRAY_OTP_SPEC_OK);
	for (index = 0U; index < 2U; ++index) {
		step_ok(&ctx, XRAY_OTP_EV_PREFLIGHT, rows[index].row,
			rows[index].program_word, 0U);
		step_ok(&ctx, XRAY_OTP_EV_ARM_WATCHDOG, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_ASSERT_SOURCE, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_ARM_ROW, rows[index].row,
			rows[index].program_word, 0U);
		step_ok(&ctx, XRAY_OTP_EV_COMMIT_ATTEMPT, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_ISSUE_COMMAND, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_COMMAND_COMPLETE, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_PROVE_SOURCE_LOW, 0U, 0U, 0U);
		step_ok(&ctx, XRAY_OTP_EV_VERIFY_ROW_AND_IMAGE, rows[index].row,
			rows[index].program_word, rows[index].program_word);
		assert(ctx.command_attempts == 1U);
	}
	assert(xray_otp_spec_finish(&ctx) == XRAY_OTP_SPEC_OK);
	assert(ctx.state == XRAY_OTP_COMPLETE && ctx.terminal);
}

static void test_refusals(void)
{
	const struct xray_otp_spec_row row = { 296U, 0U, 0x80070141U };
	const struct xray_otp_spec_row bad_gap[] = {
		{ 296U, 0U, 1U },
		{ 298U, 0U, 2U },
	};
	struct xray_otp_spec_ctx ctx;

	assert(xray_otp_spec_init(&ctx, bad_gap, 2U) == XRAY_OTP_SPEC_EBOUND);
	assert(xray_otp_spec_init(&ctx, &row, 1U) == XRAY_OTP_SPEC_OK);
	assert(xray_otp_spec_step(&ctx, XRAY_OTP_EV_PREFLIGHT, 297U,
				  row.program_word,
				  0U) == XRAY_OTP_SPEC_EBOUND);
	assert(xray_otp_spec_step(&ctx, XRAY_OTP_EV_PREFLIGHT, row.row,
				  row.program_word,
				  1U) == XRAY_OTP_SPEC_ENOTBLANK);
	step_ok(&ctx, XRAY_OTP_EV_PREFLIGHT, row.row, row.program_word, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ARM_WATCHDOG, 0U, 0U, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ASSERT_SOURCE, 0U, 0U, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ARM_ROW, row.row, row.program_word, 0U);
	step_ok(&ctx, XRAY_OTP_EV_COMMIT_ATTEMPT, 0U, 0U, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ISSUE_COMMAND, 0U, 0U, 0U);
	assert(xray_otp_spec_step(&ctx, XRAY_OTP_EV_ISSUE_COMMAND, 0U, 0U,
				  0U) == XRAY_OTP_SPEC_ERETRY);
	assert(xray_otp_spec_step(&ctx, XRAY_OTP_EV_ABORT, 0U, 0U, 0U) ==
	       XRAY_OTP_SPEC_EQUARANTINE);
	assert(ctx.state == XRAY_OTP_QUARANTINED && ctx.terminal);
	assert(xray_otp_spec_emergency_source_low(&ctx, true) ==
	       XRAY_OTP_SPEC_OK);
	assert(ctx.source_proven_low && ctx.state == XRAY_OTP_QUARANTINED);
}

static void test_fault_hook(void)
{
	const struct xray_otp_spec_row row = { 296U, 0U, 0x80070141U };
	struct xray_otp_spec_ctx ctx;

	assert(xray_otp_spec_init(&ctx, &row, 1U) == XRAY_OTP_SPEC_OK);
	assert(xray_otp_spec_inject_fault(&ctx,
					  XRAY_OTP_FAULT_BEFORE_PREFLIGHT) ==
	       XRAY_OTP_SPEC_EFAULT);
	assert(ctx.state == XRAY_OTP_ABORTED && ctx.terminal);
	assert(xray_otp_spec_init(&ctx, &row, 1U) == XRAY_OTP_SPEC_OK);
	step_ok(&ctx, XRAY_OTP_EV_PREFLIGHT, row.row, row.program_word, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ARM_WATCHDOG, 0U, 0U, 0U);
	step_ok(&ctx, XRAY_OTP_EV_ASSERT_SOURCE, 0U, 0U, 0U);
	assert(xray_otp_spec_inject_fault(&ctx,
					  XRAY_OTP_FAULT_AFTER_SOURCE_ASSERT) ==
	       XRAY_OTP_SPEC_EQUARANTINE);
	assert(ctx.state == XRAY_OTP_QUARANTINED && !ctx.source_proven_low);
	assert(xray_otp_spec_emergency_source_low(&ctx, true) ==
	       XRAY_OTP_SPEC_OK);
	assert(ctx.source_proven_low && ctx.state == XRAY_OTP_QUARANTINED);
}

int main(void)
{
	test_success();
	test_refusals();
	test_fault_hook();
	puts("170hx_xray_otp_spec_test: PASS (hardware backend absent)");
	return 0;
}
