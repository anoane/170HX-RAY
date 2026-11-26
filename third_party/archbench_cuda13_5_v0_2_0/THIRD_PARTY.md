# ArchBench third-party source

This subtree is an unmodified, source-only snapshot of **ArchBench 0.2.0**
(CUDA 13.5 edition), supplied on 2026-11-25. ArchBench is distributed under
the MIT License by the ArchBench contributors. Its original [`LICENSE`](LICENSE)
and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) are retained verbatim.
Those terms, not the surrounding 170HX-XRAY AGPL license, govern this subtree.

The vendored set contains the build files, CLI, Python package, C++/CUDA source,
headers, configurations, examples, documentation, tools, and tests needed to
build, inspect, and validate ArchBench. Generated reports, historical validation
evidence, archives, bytecode/cache files, macOS metadata, inactive upstream CI
configuration, and the source-package manifest that described those omitted
artifacts are intentionally excluded.

ArchBench does not vendor CUDA, cuBLAS, cuBLASLt, cuFFT, cuSPARSE, CUB, CUTLASS,
NCCL, Nsight Compute, or Compute Sanitizer. See its original notices and README
for their separate licenses and build/runtime requirements. The Python package
has no third-party Python runtime dependencies.

Vendoring audit: all 98 copied upstream files matched the supplied SHA-256
source manifest before copying. The independent host ALU reference passed
532,480 transitions. On macOS, 131 of 132 Python host tests passed; the sole
failure compares `/var/...` with its canonical `/private/var/...` spelling.
The vendored upstream source was not patched to hide that platform-specific
test mismatch.
