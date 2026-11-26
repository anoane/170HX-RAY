# Public-plan evidence boundary

This directory contains first-party XRAY metadata, not the raw third-party
plans it describes.

- `public-source-lineage.json` records canonical provenance and SHA-256 values.
  `raw_sources_redistributed: false` is a licensing and privacy boundary: the
  descriptor does not relicense or reproduce the referenced source bytes.
- `public-plan-audit.json` is the retained first-party audit result. It binds
  its models to the source hashes in the lineage descriptor and explicitly
  grants no execution authority.

When the raw sources are absent, candidate output labels their bindings
`hash_attestation_only: true`, `hash_verified: false`, and
`raw_source_present: false`. A matching hash establishes lineage only; it does
not validate the plan's conclusions or authorize hardware changes.

Copyright (C) 2026 Fabiano Anemone <fabiano.anemone@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
