# Reproducibility assets

All archives are verified against `SHA256SUMS` during Docker builds. Extracted source files are unchanged. Archives contain no credentials or development review transcripts.

| Archive | Purpose | Source |
|---|---|---|
| `benchmarks.tar.gz` | Required original agent/scorer code, gold, one published scoring reference and dataset-selection test fixtures | [Martian](https://github.com/withmartian/code-review-benchmark), commit `e616e849755441da38f18bf3adba2c9583b03803`; DevDex source/license retained under `vendor/devdex`, commit `24e60473887d33960bf155a9e73affcd07d288a3` |
| `devdex-pilot.tar.gz` | Original runtime, full fingerprinted dependencies, 60 recorded episodes and expected output hashes | Measured `devdex_docs-pilot-001`; runtime source hash `435597159ea48d7e2e6064b6b26e6e3aa16d67ddaa89f5bb05b1ee257a130bba` |
| `martian-pilot.tar.gz` | Original runtime and dependency lock, frozen inputs, all 40 scored reviews, eight controls, original and completed grading evidence, full judge requests/responses, and diagnostic audits | Measured `martian-pilot-002`; runtime commit `bb68840aff490a81090ae2af5a6200945946bfd0`; fingerprinted source/vendor hash `0fbfbb737a3a79e400ee4785abd9a6d8f1f50c8cb1f2dd351638f739d55ee175` |

Each archive retains the upstream license files. The Martian archive also contains a full file checksum manifest and source provenance. Its measured source comes from the frozen commit archive, never from the current application source. DevDex and Martian each use their own preserved source and locked dependencies.

`./eval reproduce` replays both pilots under Docker with `--network none`. It requires no credentials or Git history. It writes copies under `runs/reproduced/` and verifies report, CSV, PNG, state and tool-call hashes. Martian replay verifies 286 main-run files plus the completed control report and its evidence, including grading and HTTP records. The original SVG is retained and checksummed in the archive. Regenerated SVG embeds a fresh timestamp and random element IDs, so it is excluded from byte comparison; the PNG chart matches exactly. The first Docker build requires access to pinned base images and package dependencies. Replay rebuilds native scores and reports from saved observations; it does not repeat model calls or establish a new live result.

Controls are graded separately. The archive preserves the original control stop and a checksummed completion sidecar; replay validates exact raw responses with the unchanged native pipeline before deriving the completed report. The historical finding sample remains unchanged; a separate independent audit does not alter native scores. Attribution code and source-audit evidence live under `validation/` inside the archive. The benchmark code and outcomes are public-benchmark evidence. The execution repositories remain private.

To inspect a bundle directly: `tar -tzf assets/martian-pilot.tar.gz`. Docker does the required extraction automatically. Do not regenerate a measured bundle from current source or relabel its outputs as a new experiment.
