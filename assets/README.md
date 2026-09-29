# Reproducibility assets

Both archives are verified against `SHA256SUMS` during Docker builds. Extracted source files are unchanged. Archives contain no credentials or development review transcripts.

| Archive | Purpose | Source |
|---|---|---|
| `benchmarks.tar.gz` | Required original agent/scorer code, gold, one published scoring reference and dataset-selection test fixtures | [Martian](https://github.com/withmartian/code-review-benchmark), commit `e616e849755441da38f18bf3adba2c9583b03803`; DevDex source/license retained under `vendor/devdex`, commit `24e60473887d33960bf155a9e73affcd07d288a3` |
| `devdex-pilot.tar.gz` | Original runtime, full fingerprinted dependencies, 60 recorded episodes and expected output hashes | Measured `devdex_docs-pilot-001`; runtime source hash `435597159ea48d7e2e6064b6b26e6e3aa16d67ddaa89f5bb05b1ee257a130bba` |

Each archive retains the upstream license files. The replay has no dependency on Git history or remote model/provider services. It reconstructs a copy under `runs/reproduced/` and checks report, CSV, PNG, state and tool-call hashes. The first Docker build requires access to pinned base images and package dependencies.

To inspect a bundle directly: `tar -tzf assets/benchmarks.tar.gz`. Docker does the required extraction automatically. Do not regenerate the measured bundle from current source or relabel its outputs as a new experiment.
