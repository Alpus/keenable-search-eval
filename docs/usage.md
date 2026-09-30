# Commands and methodology

Install Docker with Compose 2.24+ and use a POSIX shell (WSL on Windows). No host Python, Node, uv or historical Git commits are needed. Run commands from the cloned repository.

## Reproduce and verify

```sh
./eval reproduce
./eval check
```

The first build downloads pinned images/packages. Replay and tests then run without network or credentials.

Replay restores both historical runtimes and all scored observations under `runs/reproduced/`. Completed controls appear in `martian-controls-002-completed/`; their original evidence remains in `martian-controls-002/`.

Report, CSV, PNG, state and tool-call hashes must match. This reproduces scoring of recorded answers; fresh model/search calls can produce different answers.

## Fresh runs

1. Run `./eval setup` to create `.env` without overwriting existing settings.
2. Fill `GITHUB_TOKEN`, `KEENABLE_API_KEY`, `EXA_API_KEY` and `ANTHROPIC_API_KEY` in `.env`.
3. Check GitHub token permissions: private repository creation, contents/PR writes, Actions administration and review/status reads.
4. Ensure the pinned Claude models are available and the provider accounts have credits. Account registration and CodeRabbit installation are separate prerequisites; a key alone does not grant access.
5. Run `./eval run-all` and complete the CodeRabbit dashboard steps below when prompted.

`run-all` performs preparation and execution in one command:

1. Preserve `.env`, generate missing MCP bearer tokens and use the Anthropic key for Martian unless `MARTIAN_API_KEY` is set separately.
2. Start the gateway and a pinned Cloudflare temporary HTTPS tunnel. An optional `PUBLIC_MCP_URL` in `.env` uses your existing tunnel instead.
3. Read your GitHub login, write resolved configs under `validation/prepared/`, create the 48 deterministic private test repositories and disable their GitHub Actions. No source is pushed and no review is triggered during this preparation.
4. Write `.gateway/coderabbit-setup.md` with exact connection URLs and repository names. Pause for the dashboard steps below, then record your explicit quota confirmation.
5. Check all three configs before any model calls. Execute DevDex, scored Martian and Martian controls sequentially, grading and reporting each. Stop at the first failure.

Repeat the same command after fixing a prerequisite or interruption. Existing tokens and matching repositories are reused.

Completed attempts are preserved; unknown reviews are never blindly retried. Configs, source, model settings, data and scorer cannot change during resume.

An independent experiment needs new run IDs. Do not reuse another person's test repositories.

### Unavoidable CodeRabbit dashboard steps

The [documented API](https://docs.coderabbit.ai/api) and [OpenAPI specification](https://docs.coderabbit.ai/openapi.json) do not expose saved-connection or Review-scope provisioning (checked 2026-09-29). `run-all` stops at this checkpoint on first use or when connection/repository identity changes. Follow the generated local checklist:

1. [Install the GitHub App](https://docs.coderabbit.ai/platforms/github-com) for the generated repositories. Selecting all repositories also covers future ones but grants broader access.
2. [Save two MCP connections](https://docs.coderabbit.ai/connections/mcp-servers), `eval-search-a` and `eval-search-b`, with the generated URLs and bearer tokens from `.env`. Enable their search and fetch tools.
3. [Create a named Review scope](https://docs.coderabbit.ai/connections/scopes-review) containing these connections and the generated repositories. Keep unrelated inherited connections out of these test repositories; preserve unrelated account settings. Confirm full reviews and MCP access.

Type `ready` in the terminal only after saving these settings. This is an operator attestation, not remote verification.

Live gateway health and actual review/tool receipts remain the execution evidence. No browser session is required after setup.

Temporary tunnels need no Cloudflare key or account, but their address can change after a restart. Keep the tunnel running through both Martian stages.

If it changes, update the CodeRabbit connections before any new run. Started runs retain their original URL and cannot silently migrate; use a stable HTTPS tunnel for resumable long experiments.

`./eval stop` stops the temporary tunnel too. [Cloudflare limitations](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).

### Quota and configuration

The command asks for confirmation of existing quota and disabled paid overage, plus the prepaid USD reserved for Martian **after** DevDex. The Martian judge caps total $18 ($13 main, $5 controls), including headroom for the $2.60 per-call reservation.

DevDex has episode/tool limits but no local dollar cap; use account spending controls and budget for it separately.

No command reads provider balances, buys credit, changes a subscription or proves entitlement from a key. Confirmations expire after 24 hours and are never included in the repository.

The presets pace reviews at 10 per hour. Set `limits.max_review_events_per_hour: null` only after verifying eligible [free trial PR overages](https://docs.coderabbit.ai/management/usage-based-addon#pr-reviews-during-your-trial). Hosted review latency remains separate.

For different run IDs, owner/organization, limits or search combinations, edit copies of the presets and pass all three paths:

```sh
./eval run-all configs/devdex-local.yaml configs/martian-local.yaml configs/controls-local.yaml
```

The original YAML files are never overwritten. `validation/prepared/` contains the exact resolved configs. Each run additionally saves `effective-inputs.json` with all defaults and identities before execution.

Protocol changes require a development smoke and requalification; changing the account alone does not.

Martian preserves dated Opus 4.5, temperature 0, plain JSON and one total attempt per call. Its USD cap reserves $2.60 before each judge call and settles from returned usage.

Uncertain outcomes retain reservations. CodeRabbit's internal model and sampling remain unknown.

For an individual already configured experiment, `./eval allow CONFIG --confirm-existing-quota` records quota approval (`--existing-model-credit-usd AMOUNT` is also required for Martian), then `./eval run CONFIG` executes it. These lower-level commands do not provision account settings or a tunnel. `./eval setup` only creates a private `.env` template and output directories without overwriting files.

## Public repository and replay

The harness and recorded DevDex and Martian evidence can be reused directly. `./eval reproduce` needs no account, keys or test repositories.

Fresh hosted reviews use your own account and generated private repositories; making our execution repositories public would not grant your account the same scope, model, quota or clean review history. We reuse a test repository only to resume its exact attempt.

## Controls

| Command | Behavior |
|---|---|
| `./eval help` | List commands |
| `./eval config CONFIG` | Show resolved inputs/defaults, models, limits and scorer identity |
| `./eval plan CONFIG` | Save a preview without API calls or creating resumable run state |
| `./eval doctor CONFIG` | Show missing local prerequisites without API calls |
| `./eval run-all [DEVDEX MARTIAN CONTROLS]` | Prepare accounts, then run/resume both benchmarks and controls |
| `./eval status` | Show containers and saved attempt counts |
| `./eval report RUN_ID` | Rebuild a current-runtime report; use `reproduce` for the historical pilot |
| `./eval stop` | Stop local containers; retain all evidence |

Hosted calls can continue after local stop. Unknown outcomes remain unresolved and are never blindly retried.

Do not erase run state to retry uncertain work. Approval does not bypass source, protocol or completion checks.

## Metrics and limits

DevDex uses 30 fixed public documentation questions, one repeat, Keenable Pro and Exa Auto with the same original agent. Recall@10 and MRR@10 score reference URLs in the agent's submitted sources, not answer-text correctness. Original failure scoring and >10% dead-run suppression are preserved.

Actual result: Exa 10/30, Keenable 9/30; median episode 20.3s versus 17.2s. Different adaptive queries/modes limit general speed claims.

Martian compares native search, no configured search, Keenable MCP and Exa MCP on ten reconstructed PRs. Original Core F2/P/R/TP/FP/FN are primary, Strict/F1 secondary. Controls/development/blind audit remain separate.

Missing grading suppresses headlines. These public tasks may be in model training; reconstructed PR context differs from the historical leaderboard. See [provenance](martian-provenance.md).

YAML controls supported model settings, search combinations, limits, dataset, scorer revision, schedule seed and repeats. `effective-inputs.json` freezes resolved values before execution.

The order seed does not make inference deterministic. Null sampling remains provider-owned unknown.

Requested and returned model IDs are separate. Repeats are scored independently, never best-of or pooled; suites are never averaged.

### Concurrent PR reviews

Fresh Martian and Martian control presets use `limits.max_concurrent_reviews: 10`.
Omitting this setting keeps the sequential default of `1`. DevDex always runs
sequentially.

The runner prepares and triggers one PR at a time, then polls the
confirmed reviews and fills free slots. Hosted reviews overlap while state and
gateway routing writes remain serialized under the existing workspace lock.

Concurrency and `limits.max_review_events_per_hour` are independent limits.
The presets retain the owner-wide cap of 10 review events per hour across saved
runs. A full quota blocks new starts while active reviews continue to be polled.

The ledger cannot observe reviews outside this workspace. Source preparation and
individual GitHub requests can still delay a polling pass.

Resume reads every confirmed active review before admitting new writes. It keeps
the saved trigger identity and deadline.

Pending reads keep their gateway route
until terminal evidence or the original expiry.

A transient read error stops new
starts and permits read-only recovery; an unknown write outcome requires explicit
reconciliation.

Neither case automatically sends another review request.

The concurrency setting is part of the run fingerprint and live acceptance
protocol. Changing it requires a new run identity and qualification.

Historical
configs, measured bundles and existing run state remain frozen.

Offline scheduler
tests establish overlap and admission behavior; they do not establish hosted
throughput or unchanged benchmark performance with parallel reviews.
