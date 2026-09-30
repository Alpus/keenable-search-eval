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

## Search-focused extension

`martian-search11-001` contains 11 manually screened, potentially documentation-dependent PRs from the 50-task offline benchmark, each reviewed once in four modes. This is a targeted subset, not proof that external search is necessary. The unchanged Core profile contains 47 reference issues.

| Search | Matched Core issues | Unmatched findings (benchmark FP) | Core F2 |
|---|---:|---:|---:|
| Native | 28/47 | 15 | 60.6% |
| None | 30/47 | 20 | 63.0% |
| Keenable MCP | 30/47 | 33 | 59.8% |
| Exa Auto MCP | 28/47 | 23 | 58.6% |

The original scorer treats unmatched findings as false positives; they are not necessarily incorrect. Core excludes style/speculative gold and follows the original matched-excluded handling. Precision is TP/(TP+FP), recall is TP/(TP+FN), and F2 is 5TP/(5TP+4FN+FP). Counts and unrounded values are in [the CSV](../results/martian-search11-metrics.csv).

### What the evidence supports

- All 11 Keenable and 11 Exa reviews called their assigned search provider. Keenable made 42 searches and 48 fetch attempts (10 failed fetches); Exa made 35 searches and 48 fetch attempts (8 failed fetches), plus two rejected calls. No search failed or remained unfinished. Both arms used the same page reader, not the providers' Fetch products. Native calls are unobservable.
- Discourse Graphite 4: Keenable retrieved MDN documentation on messaging and framing and reported corresponding origin-validation and framing-policy issues. Other arms found these too. Relevant retrieval is observed; a causal quality gain is not established.
- Sentry 93824: all arms found the process-type and shutdown-loop issues. Native also found the inconsistent metric tag. Keenable's extra findings included two documentation-example comments, an unused helper and a plausible repeated cluster-memory query. These examples are exploratory source inspection, not a blind audit or replacement score.
- Unmatched findings can be useful: Discourse's raw-HTML path bypasses normal rendering, supporting further investigation of the reported sanitization concern. This was also reported by native and Exa; exploitability was not established by this review.

### Exploratory source audit

All 33 unmatched Keenable comments were inspected against frozen source. This was a non-blind audit, not an exploit test or a replacement scorer.

- **Supported additional issues:** Discourse exposes a derived website field without the original privacy restriction; Cal.com refreshes Salesforce tokens unnecessarily and can leave deleted credentials visible in one settings view; Keycloak accepts malformed sequence lengths and truncates oversized integers. Many also appear in other arms.
- **Not 33 independent bugs:** the upload-limit message overlaps gold, negative-offset comments repeat an existing failure, two integer findings overlap, and documentation/test/cleanup suggestions do not demonstrate production failures. Some conditional deployment and security risks remain unverified.
- **Paired recall:** compared with no search, Keenable gains four reference matches and loses four. Its gains include upload limits, ERB syntax, framing policy and datetime arithmetic. Three are directly visible in local code; relevant framing documentation was retrieved, but causation is not established.
- **Gold is imperfect:** one Cal.com defect maps to two gold entries, and Keycloak 36882 already documents exit code 4 and uses an established CLI exit helper. The original scoring is retained; a missed reference is not automatically a real missed bug.

### Finding-level retrieval evidence

These are observed content overlaps, not causal counts. One review per mode cannot establish how many gains or losses were caused by search.

| Observation | Saved retrieval | Interpretation |
|---|---|---|
| Keenable gains the framing-header reference match over no search | Discourse Graphite 4 retrieved framing documentation and search excerpts explicitly warning about ALLOWALL. | Documentation supports this additional match; necessity is not established. |
| Keenable gains the upload-limit reference match | Discourse Graphite 1 retrieved upstream commits, including “FIX: don't hardcode maximum file size”. | Retrieval included a relevant existing fix, not just general documentation. Local code also exposes the mismatch. |
| Keenable misses the legacy CSS ordinal issue found without search | Discourse Graphite 5 search returned a mixin using `$int + 1` for legacy ordinal properties and `$int` for modern order. | The relevant distinction was available but not reported. This is not evidence that search caused the miss. |
| Additional iframe-source check | Graphite 4 fetched MDN postMessage documentation discussing message source. | Relevant documentation for a hardening suggestion, not a demonstrated extra exploit. |
| Additional Redis rollout type mismatch | Sentry Greptile 2 search returned Sentry's Redis troubleshooting page describing WRONGTYPE after a key-type change. | Strong content overlap with a source-supported conditional rollout concern. This later operational documentation is not an independent reproduction of this PR. |
| Additional timeout and ASN.1 validation findings | Cal.com and Keycloak fetched existing benchmark reviews describing these issues. | Answer exposure overlaps findings; useful observations cannot be presented as clean independent discoveries. |

An additional successful benchmark-copy fetch was identified for Sentry Greptile 1 (`AI-Code-Review-Evals/claude_code-sentry/pull/2`). At least four tasks therefore fetched benchmark copies; specific review-answer overlap remains established for Cal.com and Keycloak. The earlier three examples were a lower bound.

### Limitations and recovery

- Public benchmark copies appeared in search results. Successful Keenable fetches for Cal.com 11059 and Keycloak 33832 returned other agents’ review text overlapping the reported timeout, integer-truncation, sequence-validation and test-quality findings. Sentry 93824 copies were fetched too. Answer exposure is confirmed; reliance on that text is not proven. Known-URL exclusions did not cover every public mirror.
- Two Sentry Greptile tasks share a head and overlapping issues. The 11 tasks are not 11 independent samples. Two source bases use merge-base reconstructions, and original gold retains documented questionable labels. These limitations prevent a clean claim of general search superiority.
- Ten initial commands were rejected by CodeRabbit's hourly chat limit. They were retried once on the same PRs after checking absence of reviews. Two successful zero-finding reviews then updated existing walkthrough comments; the old collector incorrectly required a new comment creation time. Saved trusted, same-SHA completion evidence within the original deadlines recovered them without additional reviews.
- All 44 reviews and grading artifacts are complete. Token-accounted judge cost was $6.870475 within the $13 cap; this is API usage accounting, not an invoice. The measured runtime and correction receipts are preserved in `assets/martian-search11.tar.gz`. Offline replay rebuilds scores from saved responses without new model calls.
