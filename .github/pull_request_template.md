## Outcome and scope

<!-- What changes? Which files, schemas, ABIs, registers and access levels? -->

## Evidence and confidence

<!-- Source URL/revision/date, specimen, raw evidence, certainty, contradictions. -->

## Device effects

- [ ] Offline only
- [ ] Query-only driver APIs
- [ ] Allowlisted BAR reads
- [ ] Semantic controller reads/restoration
- [ ] Benchmark workloads/transient power control
- [ ] Volatile state
- [ ] Experimental irreversible path (explain separate authorization)

## Validation

- [ ] Normal full suite and self-test
- [ ] Sanitized full suite, parity and binary-safe boundary scan
- [ ] CPython 3.9 and 3.15; 3.14 when available
- [ ] Strict JSON/format/mutation/property tests
- [ ] Kernel builds, ABI tests and OTP-spec tests
- [ ] Lint/type/security tools run or individually waived
- [ ] Relevant live levels/benchmarks run with exact power evidence, or skipped with reason
- [ ] Full report generated and verified with explicit skips
- [ ] README, USAGE, models and credits synchronized
- [ ] Release hashes regenerated only after freeze

Commands, versions, outputs and artifact hashes:

```text

```

## Remaining limits and recovery boundary

<!-- What was not tested? What external hardware/state is missing? -->

I have read [PR.md](../PR.md). A passing software suite is not electrical fuse
safety or proof that unmarked hardware is healthy.
