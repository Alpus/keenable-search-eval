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

1. Open `.gateway/coderabbit-setup.md`. Use its exact repository names, connection names and HTTPS URLs; do not copy another run's account settings.
2. [Install the GitHub App](https://docs.coderabbit.ai/platforms/github-com) for those repositories. Verify they appear in CodeRabbit's Connected repositories page; selecting all repositories also grants access to future ones.
3. In Connections, add each listed server as **Custom → MCP → Read-only**. Set both names to the listed connection name, Server URL to its listed URL, Transport to **Streamable HTTP**, Authentication to **API token**, and Auth header to **Authorization**.
4. Enter `Bearer ` followed by the corresponding token from local `.env`. Turn **Add this to the Base Scope** off. Set Prompt Guidance to: `Search public documentation when external facts are needed to review this change. For every search and fetch call, set review_url to https://github.com/{owner}/{repo}/pull/{pr}.`
5. Click **Discover tools**, select `search` and `fetch`, then save. Both tools must be discovered successfully for each connection; an entered URL alone is not verification.
6. In Scopes, create a named scope containing exactly the generated repositories and both connections. Verify its saved repository count, two connections and enabled state. Keep unrelated connections out of this scope and preserve unrelated account settings.
7. Verify that the account permits full reviews and MCP, and has enough existing quota. The runner disables automatic PR reviews and sends one explicit review command per attempt; an initial “Auto reviews are disabled” comment is expected.
8. Return to the waiting terminal and type `ready` only after saving and checking those settings. Confirm the existing budget when prompted; this is an operator attestation, not remote verification.

If editing an existing URL cannot discover tools, inspect whether CodeRabbit is still checking the old saved connection. This occurred on 2026-09-30; creating a replacement with the same permissions worked. Before any run starts, update `profiles.<profile>.connection` in the input configs and rerun preparation so its checklist and qualification match.

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

### Run one already prepared experiment

These commands do not provision CodeRabbit, repositories or a tunnel. Use the resolved config, existing source inputs and qualified protocol from preparation.

1. Confirm no runner is already executing this run ID. Check `docker compose ps -a` and `runs/<run_id>/state.json`.
2. For a fresh Martian run, record the verified existing quota: `./eval allow CONFIG --confirm-existing-quota --existing-model-credit-usd AMOUNT`. Replace `CONFIG` and `AMOUNT`; never invent a balance.
3. Check prerequisites with `./eval doctor CONFIG`. It must report `live_ready: true`; this local check does not prove provider balances or CodeRabbit access.
4. Run `./eval run CONFIG`. It collects reviews, grades completed attempts and writes the report automatically.
5. Inspect `runs/<run_id>/report.md`, `metrics.csv` and `state.json`. A running process or partial report is not a completed experiment.

### Resume or handoff

1. Read `runs/<run_id>/config.json`, `effective-inputs.json` and `state.json`, plus `.gateway/coderabbit-setup.md`. Check `docker compose ps -a` before doing anything that could start another runner.
2. If the runner is still active, inspect its logs with `docker logs CONTAINER`. Do not launch a second copy or rebuild its gateway while it is running.
3. If it stopped, preserve `.env`, configs, `runs/`, `validation/`, `.gateway/` and the original tunnel. Repeat the original command only after resolving the reported prerequisite; do not change frozen inputs.
4. If state reports an unknown external write, a changed tunnel or a stopped judge call, keep the evidence and inspect that specific failure. Do not delete state, issue another review command or raise a budget to make it pass.
5. Verify completion from terminal attempt states, complete grading and the report. Record the exact run ID, command and remaining issue when handing off.

The default presets already contain pinned source inputs and qualification evidence. Selecting new PRs is separate preparation: freeze and verify their base/head trees, retain original gold and answer-source exclusions, and qualify the new manifest before launch. A new dataset is not enabled merely by changing a task count.

## Public repository and replay

The harness and recorded DevDex and Martian evidence can be reused directly. `./eval reproduce` needs no account, keys or test repositories.

Fresh hosted reviews use your own account and generated private repositories; making my execution repositories public would not grant your account the same scope, model, quota or clean review history. I reuse a test repository only to resume its exact attempt.

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
