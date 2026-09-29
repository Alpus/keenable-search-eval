# Commands and methodology

Install Docker with Compose 2.24+ and use a POSIX shell (WSL on Windows). No host Python, Node, uv or historical Git commits are needed. Run commands from the cloned repository.

## Reproduce and verify

```sh
./eval reproduce
./eval check
```

The first build downloads pinned images/packages. Replay and tests then run without network or credentials. Replay restores the historical DevDex runtime and observations into `runs/reproduced/devdex_docs-pilot-001/`. Report, CSV, PNG, state and tool-call hashes must match. This reproduces scoring of recorded answers; fresh model/search calls can produce different answers.

## New experiment

```sh
./eval setup
# Fill .env, inspect the YAML, check service access and existing quota.
./eval config configs/devdex_docs.yaml
./eval allow configs/devdex_docs.yaml --confirm-existing-quota
./eval run configs/devdex_docs.yaml
```

`setup` creates a private `.env` and never overwrites it. DevDex needs `KEENABLE_API_KEY`, `EXA_API_KEY` and `ANTHROPIC_API_KEY`, with access to the pinned original `claude-opus-4-8` model.

`allow` records your explicit confirmation of access, sufficient existing quota and disabled paid overage for this config, valid for 24 hours. It does not query balances, buy credit or prove access remotely. DevDex caps episodes/tool calls, not model dollars; configure account spending controls before approval. Local approvals are ignored by Git and never inherited by another user.

`run` builds the current image, starts the gateway, waits for health, checks prerequisites and executes/resumes the schedule. Failed preflight stops before paid calls or PR creation. Release qualification records bind tested code/protocol to original live development evidence. Protocol changes need new qualification. Repeat the same command to resume; use a new `run_id` for an independent experiment. Changed config/data/code/scorer cannot resume old state.

## CodeRabbit setup

Use `configs/martian.yaml` (40 scored reviews) or `configs/martian-controls.yaml` (8 controls). The main comparison has not run.

1. Add `GITHUB_TOKEN` and `MARTIAN_API_KEY` (Anthropic key) to `.env`; set `github_owner` in YAML. GitHub access must cover creation of private repos, PR writes and review/status reads.
2. Install CodeRabbit with full review and MCP access to the intended test repositories. Check review quota. These external account permissions cannot be inferred from API keys.
3. Expose localhost:8766 through an HTTPS tunnel and set `public_mcp_url`. Keep the tunnel reachable throughout the run.
4. Set random `MCP_SEARCH_A_TOKEN` and `MCP_SEARCH_B_TOKEN` values. Register scoped CodeRabbit connections `eval-search-a` and `eval-search-b` at `<public_mcp_url>/mcp/search-a` and `/mcp/search-b` using those bearer tokens. Keep unrelated/base scopes empty.
5. Confirm the YAML's SHA-bound completion contract for your account. Different completion contracts or new search combinations need a development smoke and requalification.
6. Check the configured judge cap and actual prepaid balance. Run `./eval allow configs/martian.yaml --confirm-existing-quota --existing-model-credit-usd AMOUNT`, using that checked balance, then `./eval run configs/martian.yaml`.

No command purchases credits or installs a paid plan. Martian preserves dated Opus 4.5, temperature 0, plain JSON and one total attempt per call. Its USD cap reserves $2.60 before each call and settles from returned usage; uncertain outcomes retain reservations. CodeRabbit's internal model and sampling remain unknown.

## Controls

| Command | Behavior |
|---|---|
| `./eval help` | List commands |
| `./eval config CONFIG` | Show resolved inputs/defaults, models, limits and scorer identity |
| `./eval plan CONFIG` | Save a preview without API calls or creating resumable run state |
| `./eval doctor CONFIG` | Show missing local prerequisites without API calls |
| `./eval status` | Show containers and saved attempt counts |
| `./eval report RUN_ID` | Rebuild a current-runtime report; use `reproduce` for the historical pilot |
| `./eval stop` | Stop local containers; retain all evidence |

Hosted calls can continue after local stop. Unknown outcomes remain unresolved and are never blindly retried. Do not erase run state to retry uncertain work. Approval does not bypass source, protocol or completion checks.

## Metrics and limits

DevDex uses 30 fixed public documentation questions, one repeat, Keenable Pro and Exa Auto with the same original agent. Recall@10 and MRR@10 score reference URLs in the agent's submitted sources, not answer-text correctness. Original failure scoring and >10% dead-run suppression are preserved. Actual result: Exa 10/30, Keenable 9/30; median episode 20.3s versus 17.2s. Different adaptive queries/modes limit general speed claims.

Martian compares native search, no configured search, Keenable MCP and Exa MCP on ten reconstructed PRs. Original Core F2/P/R/TP/FP/FN are primary, Strict/F1 secondary. Controls/development/blind audit remain separate. Missing grading suppresses headlines. These public tasks may be in model training; reconstructed PR context differs from the historical leaderboard. See [provenance](martian-provenance.md).

YAML controls supported model settings, search combinations, limits, dataset, scorer revision, schedule seed and repeats. `effective-inputs.json` freezes resolved values before execution. The order seed does not make inference deterministic. Null sampling remains provider-owned unknown. Requested and returned model IDs are separate. Repeats are scored independently, never best-of or pooled; suites are never averaged.
