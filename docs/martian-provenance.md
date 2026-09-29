# Benchmark provenance

Martian: [Code Review Benchmark](https://github.com/withmartian/code-review-benchmark), commit `e616e849755441da38f18bf3adba2c9583b03803`. DevDex: commit `24e60473887d33960bf155a9e73affcd07d288a3`. Original licenses and unchanged agent/scorer/data files are preserved in the checksummed assets.

`data/martian-inventory.json` accounts for all 50 published PRs. The pilot selects ten eligible reconstructed tasks before provider results, with fixed seed and source allocation. Two authored development fixtures and two targeted fixed-code controls remain separate. The full source has 173 total, 158 Core and 139 Strict gold comments; these are not the pilot denominators.

The benchmark asset retains metadata screening, source-selection/patch evidence, original gold, one published evaluation and its dashboard. Regression tests reconcile selection, exact Git blobs/symlinks/modes and original score arithmetic. Gold and future-fix metadata never enter agent-visible inputs. Titles come from source metadata and bodies are neutral, a disclosed difference from historical review context. Scoring preserves original first-page comment projection while collection retains all pages.

Pre-run screening classified eight pilot cases as repository-local and two as plausibly documentation-dependent. This is not evidence that search helps. Known answer/fix URLs are blocked in MCP; native filtering and unknown copies remain limitations.

DevDex uses five development and thirty scored public documentation questions. Original URL normalization and deterministic scoring are unchanged. Some source URLs were unavailable during the original audit; tasks were not replaced after outcomes. The replay asset retains every measured episode and the original runtime. It does not claim deterministic new model calls.
