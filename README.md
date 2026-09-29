# Developer Search Eval

Compare Keenable and Exa on developer-documentation retrieval, and test search configurations in CodeRabbit reviews. Install Docker with Compose, clone this repository, then run `./eval reproduce` to rebuild the measured results without API keys. Run `./eval check` to verify the implementation. [Commands and live setup](docs/usage.md) · [Architecture](docs/architecture.md).

For fresh calls, put your four API keys in `.env`, then run `./eval run-all`. It prepares private test repositories, generates MCP tokens, starts a tunnel, guides the unavoidable CodeRabbit dashboard setup and quota confirmation, then runs or resumes both benchmarks and controls. Reports are automatic. [First-run details](docs/usage.md#fresh-runs).

![DevDex pilot](results/results.png)

**Measured:** 30 documentation questions per provider, the same Claude Opus 4.8 agent, one repeat. Exa found the reference source on 10/30 tasks; Keenable on 9/30. Median episode: 20.3s versus 17.2s. This pilot does not show a Keenable quality advantage. The main CodeRabbit comparison is not yet complete.

Both suites retain their original scorers. DevDex measures reference-URL recall@10 and MRR@10. Martian measures Core F2, precision and recall. Timing and completion are separate. These are public-subset pilots, not official leaderboard submissions or proof of customer revenue impact.
