# Architecture

One sequential runner executes a frozen configuration. Two suite adapters call original benchmark code. An MCP gateway supplies search and page reads. Saved evidence produces reports without new model calls. There is no database, queue or web application.

```text
eval                         Docker command entry point
configs/                     Three experiment presets
data/                        Frozen manifests and agent protocol
src/search_eval/
  cli.py                     Command parsing
  core.py                    Config, scheduling and immutable state
  effective_inputs.py        Resolved inputs and original-protocol constraints
  runner.py                  Preflight, attempts and resume
  github.py                  Private repositories, PRs and review collection
  gateway.py                 MCP routing, attribution and call limits
  providers.py               Search adapters and shared public-page reader
  devdex_worker.py           Original DevDex agent subprocess
  grading.py                 Recorded Martian judge calls and cost reservations
  report.py                  Offline metrics, comparisons, audit and charts
  suites/martian.py          Original Martian pipeline adapter
  suites/devdex.py           Original DevDex URL scorer adapter
scripts/                     Frozen replay and independent DevDex audit
assets/                      Checksummed benchmark dependencies and measured replay
results/                     Measured CSV and chart, plus release verification
tests/                       Focused regression tests
Dockerfile / compose.yaml    Build targets and the two live services
```

Flow: YAML → resolved input snapshot → schedule → CodeRabbit or DevDex → gateway → provider. Saved reviews/answers → original scorer → report. Native search and MCP profiles are independent configuration fields; an additional combination does not require a new executor.

The gateway image contains no gold, dataset or GitHub/model credentials. Its runtime identity covers shared source and dependencies. The runner owns gold and scoring. The worker sees only the permitted question. Each run saves its config, manifest, source/scorer hashes, requested model/settings, actual response evidence and artifacts. Resume cannot silently change these inputs.

`assets/benchmarks.tar.gz` contains unchanged upstream files required by execution and parity tests, plus source-selection fixtures. `assets/devdex-pilot.tar.gz` contains the completed pilot's original runtime, dependencies and observations. The latter intentionally includes the historical reference files covered by its fingerprint. These are reproducibility dependencies, not development transcripts. See [asset provenance](../assets/README.md).

Docker expands these archives inside images. Generated runs, local account records, gateway data and credentials are ignored by Git. `./eval reproduce` uses the historical image; fresh experiments use the current runner. Their identities remain distinct.

Native extraction, deduplication, matching and scoring stay upstream. Wrappers own transport, recorded evidence and the accepted single-attempt policy. The pairwise judge queue starts before the upstream call timer. Shared readiness and budget validation serve both preflight and live execution. New benchmark behavior belongs in a suite adapter; do not introduce a general executor framework.
