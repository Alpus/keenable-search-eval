"""Prepare live accounts and local inputs without changing benchmark execution."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

from search_eval.core import (
    ROOT,
    EvalError,
    atomic_json,
    digest,
    read_config,
    record_operator_allowance,
    schedule,
    suite_module,
    verify_allowance,
)
from search_eval.github import GitHub, ensure_repo, token_from_environment
from search_eval.runner import routing_store


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def initialize(paths, root=ROOT):
    configs = [read_config(root / p) for p in paths]
    if [c.suite for c in configs] != ["devdex_docs", "martian", "martian"]:
        raise EvalError(
            "Expected DevDex, scored Martian and Martian controls configs, in that order"
        )
    if [c.role for c in configs] != ["scored", "scored", "control"]:
        raise EvalError("Expected scored, scored and control roles")
    if len({c.run_id for c in configs}) != 3:
        raise EvalError("Each experiment needs a distinct run_id")
    required = ["GITHUB_TOKEN", "KEENABLE_API_KEY", "EXA_API_KEY", "ANTHROPIC_API_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise EvalError("Fill .env first: " + ", ".join(missing))
    additions = {}
    if not os.getenv("MARTIAN_API_KEY"):
        additions["MARTIAN_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    for config in configs:
        for profile in config.profiles.values():
            if not os.getenv(profile.token_env) and profile.token_env not in additions:
                additions[profile.token_env] = secrets.token_urlsafe(32)
    if additions:
        # Append to the mounted file itself; never replace its inode or existing values.
        with (root / ".env").open("a") as stream:
            for key, value in additions.items():
                stream.write(f"\n{key}={value}\n")
        os.environ.update(additions)
    (root / ".env").chmod(0o600)
    for config in configs:
        routing_store(config, root)
    urls = {
        c.public_mcp_url.rstrip("/")
        for c in configs[1:]
        if c.public_mcp_url and c.public_mcp_url != "https://your-public-mcp.example"
    }
    if os.getenv("PUBLIC_MCP_URL"):
        urls.add(os.environ["PUBLIC_MCP_URL"].rstrip("/"))
    if len(urls) > 1:
        raise EvalError("Martian configs and PUBLIC_MCP_URL must use the same endpoint")
    url = next(iter(urls), "")
    if url:
        validate_url(url)
    return url


def validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise EvalError("PUBLIC_MCP_URL must be an HTTPS origin without credentials or a path")


def resolve_configs(paths, owner, url, root=ROOT):
    validate_url(url)
    resolved = []
    for source in paths:
        config = read_config(root / source)
        values = config.model_dump()
        if config.github_owner == "your-github-login":
            values["github_owner"] = owner
        if config.suite == "martian":
            values["public_mcp_url"] = url
        config = type(config).model_validate(values)
        state = read_json(root / "runs" / config.run_id / "state.json", None)
        if state and state["config"] != config.model_dump():
            raise EvalError(
                f"Frozen inputs changed for {config.run_id}. Restore the original URL/config "
                "or use new run IDs. A restarted temporary tunnel cannot resume an old run."
            )
        target = Path("validation/prepared") / f"{config.run_id}.yaml"
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        (root / target).write_text(yaml.safe_dump(config.model_dump(), sort_keys=False))
        resolved.append((target, config))
    return resolved


def prepare_repositories(resolved, api, root=ROOT):
    path = root / "validation/platform-repositories.json"
    ledger = read_json(path, {})
    selected = []
    for _, config in resolved:
        if config.suite != "martian":
            continue
        tasks = suite_module(config.suite).load_tasks(root / config.manifest)
        for planned in schedule(config, tasks):
            name = f"se-{planned['id']}"
            full_name = f"{config.github_owner}/{name}"
            row = ledger.setdefault(full_name, {"id": planned["id"]})
            repo = ensure_repo(
                api,
                config.github_owner,
                name,
                f"search-eval:{planned['id']}",
                lambda: atomic_json(path, ledger),
                row,
            )
            selected.append({"name": repo["full_name"], "id": repo["id"]})
    return selected


def platform_checklist(resolved, repos, root=ROOT):
    connections = {}
    for _, config in resolved:
        if config.suite != "martian":
            continue
        for name, profile in config.profiles.items():
            value = {
                "name": profile.connection,
                "url": config.public_mcp_url + "/mcp/" + profile.endpoint,
                "token_env": profile.token_env,
                "token_sha256": digest(os.environ[profile.token_env].encode()),
            }
            if name in connections and connections[name] != value:
                raise EvalError("All configs must agree on shared MCP connections")
            connections[name] = value
    binding = digest({"repos": repos, "connections": connections})
    lines = [
        "# CodeRabbit account setup (local only)",
        "",
        "These are operator steps, not remotely verified by the runner.",
        "",
        "1. Install/authorize the CodeRabbit GitHub App for the repositories below.",
        "   https://github.com/apps/coderabbitai/installations/new",
        "2. In CodeRabbit Connections, add or update the following MCP servers.",
        "   Use Streamable HTTP, bearer authentication, and enable search/fetch tools.",
        "   Read token values from your local .env. Never publish them.",
        "",
    ]
    for c in connections.values():
        lines.append(f"- `{c['name']}`: `{c['url']}`. Bearer token: `{c['token_env']}` in `.env`.")
    lines += [
        "",
        "3. Create/update a named Review scope with these connections and ONLY these repositories.",
        "   Ensure the test repositories inherit no unrelated connections from Base Scope.",
        "   Preserve settings for unrelated repositories. Do not change scopes during a run.",
        "4. Confirm full PR reviews and MCP are enabled and enough existing review quota is available.",
        "   Do not enable paid overages or buy credits through this script.",
        "",
        "Docs: https://docs.coderabbit.ai/connections/mcp-servers",
        "Scopes: https://docs.coderabbit.ai/connections/scopes-review",
        "",
        "## Repositories",
        "",
    ]
    lines += [f"- https://github.com/{r['name']}" for r in repos]
    path = root / ".gateway/coderabbit-setup.md"
    path.write_text("\n".join(lines) + "\n")
    return binding


def confirm_platform(binding, root=ROOT, ask=input):
    path = root / ".gateway/platform-confirmation.json"
    if read_json(path, {}).get("binding") == binding:
        return
    print(
        "Complete .gateway/coderabbit-setup.md in CodeRabbit. No supported API provisions these settings."
    )
    if (
        ask("Type ready after saving the exact connections and repository scope: ").strip()
        != "ready"
    ):
        raise EvalError("Platform setup not confirmed. Repeat ./eval run-all when ready")
    atomic_json(
        path, {"binding": binding, "method": "operator_attestation", "remote_verified": False}
    )


def approve_quota(resolved, root=ROOT, ask=input):
    missing = []
    for _, config in resolved:
        tasks = suite_module(config.suite).load_tasks(root / config.manifest)
        try:
            verify_allowance(config, root, len(schedule(config, tasks)))
        except (EvalError, ValueError, TypeError):
            missing.append(config)
    if not missing:
        return
    cap = sum(c.suite_options.get("max_judge_usd", 0) for _, c in resolved)
    print(
        f"Martian judge caps total ${cap:g}. DevDex model usage is additional and has no local dollar cap."
    )
    print(
        "Check service access, prepaid credit and account spending controls. This script cannot read balances."
    )
    credit = float(ask(f"USD reserved for Martian AFTER DevDex (at least {cap:g}): "))
    import math

    if not math.isfinite(credit) or credit < cap:
        raise EvalError("Reserved credit does not cover the combined Martian judge caps")
    if (
        ask(
            "Confirm sufficient existing quota for ALL three experiments and no paid overage (yes): "
        ).strip()
        != "yes"
    ):
        raise EvalError("Quota not approved; no experiment has started")
    for config in missing:
        record_operator_allowance(config, root, credit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "provision"])
    parser.add_argument("--public-url")
    parser.add_argument("configs", nargs=3, type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    try:
        if args.command == "init":
            print(initialize(args.configs))
            return 0
        api = GitHub(token_from_environment())
        owner = api.call("GET", "/user")["login"]
        resolved = resolve_configs(args.configs, owner, args.public_url)
        repos = prepare_repositories(resolved, api)
        binding = platform_checklist(resolved, repos)
        confirm_platform(binding)
        approve_quota(resolved)
        (ROOT / ".gateway/prepared-configs.txt").write_text(
            "".join(str(path) + "\n" for path, _ in resolved)
        )
        print("Platform preparation complete. Remote CodeRabbit settings are operator-attested.")
        return 0
    except (EvalError, OSError, ValueError, EOFError) as exc:
        print(
            f"Preparation stopped: {exc}. Repeat ./eval run-all after resolving it.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
