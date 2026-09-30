# Benchmark provenance

Martian: [Code Review Benchmark](https://github.com/withmartian/code-review-benchmark), commit `e616e849755441da38f18bf3adba2c9583b03803`. DevDex: commit `24e60473887d33960bf155a9e73affcd07d288a3`. Original licenses and unchanged agent/scorer/data files are preserved in the checksummed assets.

`data/martian-inventory.json` accounts for all 50 published PRs. The pilot selects ten eligible reconstructed tasks before provider results, with fixed seed and source allocation. Two authored development fixtures and two targeted fixed-code controls remain separate. The full source has 173 total, 158 Core and 139 Strict gold comments; these are not the pilot denominators.

The benchmark asset retains metadata screening, source-selection/patch evidence, original gold, one published evaluation and its dashboard. Regression tests reconcile selection, exact Git blobs/symlinks/modes and original score arithmetic. Gold and future-fix metadata never enter agent-visible inputs. Titles come from source metadata and bodies are neutral, a disclosed difference from historical review context. Scoring preserves original first-page comment projection while collection retains all pages.

Pre-run screening classified eight pilot cases as repository-local and two as plausibly documentation-dependent. This is not evidence that search helps. Known answer/fix URLs are blocked in MCP; native filtering and unknown copies remain limitations.

DevDex uses five development and thirty scored public documentation questions. Original URL normalization and deterministic scoring are unchanged. Some source URLs were unavailable during the original audit; tasks were not replaced after outcomes. The replay asset retains every measured episode and the original runtime. It does not claim deterministic new model calls.

## Fixed-issue controls

Two targeted repairs were reviewed in all four modes, giving eight controls outside the scored cohort. The original judge checks whether an extracted finding still matches the repaired issue. Other findings can be valid; their count is not a false-positive count.

| Search | Repaired issues still flagged (of 2) ↓ | Extracted findings: Sentry / Cal.com |
|---|---:|---:|
| Native | 0 | 2 / 8 |
| None | 1 | 4 / 6 |
| Keenable MCP | 0 | 3 / 11 |
| Exa MCP | 0 | 2 / 12 |

All eight controls were graded. One no-search review re-flagged the repaired Sentry issue. Two controls per mode cannot establish a general false-positive rate or provider advantage.

Control grading initially stopped because the next $2.60 reservation exceeded its $3 cap. An approved transfer changed the allocation to $13 main plus $5 controls, keeping the total $18 ceiling. Exact saved request occurrences were replayed locally; 208 never-issued requests completed the remaining four controls through the unchanged dated Opus 4.5 pipeline. No review was repeated. The original stop, inputs and successful responses remain unchanged; completion evidence and the derived control report are stored separately.

Token-accounted judge cost: $3.941665 main plus $1.323620 controls, totaling $5.265285. Completion added $0.921190 within existing credits. These are usage-based calculations, not a billing invoice. Saved requests/responses, budget scope and replay hashes are retained in the archive.

## Search attribution and finding audit

Every MCP review used its assigned search provider: 20/20 main and 4/4 controls. A successful call means the gateway returned results; it does not prove relevance or influence on a finding.

| Cohort / provider | Successful searches | Fetch attempts | Failed fetches | Saved cap rejections |
|---|---:|---:|---:|---:|
| Main / Keenable | 39 | 43 | 4 | 0 |
| Main / Exa | 36 | 52 | 2 | 5 |
| Controls / Keenable | 9 | 9 | 2 | 0 |
| Controls / Exa | 5 | 15 | 1 | 0 |

Both providers use the same gateway page reader. Five Exa fetch requests in the main cohort reached its per-review cap. No search failed or remained unfinished. Native CodeRabbit searches and internal retrieval are unobservable. Missing rejection files cannot establish zero rejected calls: 22 unscoped gateway rejections match no review identity in these runs and remain unassigned. All 48 review receipts report included allowance; no actual overage is evidenced.

The separate 20-finding source audit found 13 source-supported introduced findings, two test-fixture limitations, four contradicted/style/preexisting findings and one unresolved finding. Duplicates and related defects are retained. Root saw partial mappings before review; the independent reviewer saw candidate product names in an incidental status listing, but no item mappings or native scores. This diagnostic is not fully blind, does not estimate precision and does not change native scores.

Raw traces, the attribution script and its 2,182-check receipt, locked verdicts, source references and root corrections are retained in the Martian archive. The attribution receipt describes the original control snapshot before grading completion; its search evidence is unchanged.
