# Provenance boundary

## Workspace-only evidence

The README and credits name research artifacts that exist in the maintainers'
larger evidence workspace. Those artifacts include raw GPU captures, firmware
analysis, Discord exports, supplied archives, historical release bundles and a
large source lock. They are evidence identifiers, not files shipped in this
repository.

They are deliberately not redistributed here because the complete corpus is
large, contains material with heterogeneous licenses and provenance, and would
turn a source release into an implicit republication of third-party data. Raw
demonstration reports are also omitted because they contain host/card identity,
raw fuse rows and command evidence. Users can generate a new self-contained,
hash-manifested specimen report with `full-report` or `full-report-bench`.
Public upstream sources remain linked directly from the README and credits.

Named workspace artifacts must not be treated as independently downloadable
proof merely because they appear in documentation. A claim depending on one of
them remains bounded by its stated date, digest, specimen and certainty. Ask a
maintainer for the corresponding sealed artifact when reproducing that claim.
