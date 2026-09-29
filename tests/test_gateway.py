import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from search_eval.core import shared_runtime_identity
from search_eval.gateway import Gateway, create_app, digest
from search_eval.providers import ProviderError


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "routing.json"
    data = {
        "profiles": {
            "keenable": {
                "provider": "keenable",
                "endpoint": "search-a",
                "connection_token_sha256": digest("one"),
            },
            "exa": {
                "provider": "exa",
                "endpoint": "search-b",
                "connection_token_sha256": digest("two"),
            },
        },
        "attempts": {
            "a": {
                "review_url": "https://github.com/org/repo/pull/1",
                "allowed_profiles": ["keenable", "exa"],
                "authorized": True,
                "status": "running",
                "expected_gateway_runtime_sha256": shared_runtime_identity(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "profile": "keenable",
                "token_sha256": digest("dev"),
                "max_search_calls": 2,
                "max_fetch_calls": 2,
                "answer_patterns": ["*answers*"],
            }
        },
    }
    path.write_text(json.dumps(data))
    calls = []

    async def search(q, p):
        calls.append((q, p))
        return {
            "results": [
                {"title": "docs", "url": "https://example.com/docs", "excerpt": "ok"},
                {"title": "answer", "url": "https://example.com/answers", "excerpt": "x"},
            ],
            "raw": {"results": []},
            "parameters": {"query": q},
            "excerpt_fields": ["snippet"],
        }

    gateway = Gateway(path, tmp_path / "traces", search_fn=search)
    return gateway, data, calls


@asynccontextmanager
async def client_for(app):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, incoming.get, outgoing.put))
    await incoming.put({"type": "lifespan.startup"})
    assert (await outgoing.get())["type"] == "lifespan.startup.complete"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client
    await incoming.put({"type": "lifespan.shutdown"})
    assert (await outgoing.get())["type"] == "lifespan.shutdown.complete"
    await task


async def rpc(client, route, token, method, params=None):
    return await client.post(
        "/mcp/" + route,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


async def test_real_mcp_initialize_list_call_and_auth(setup):
    g, data, calls = setup
    async with client_for(create_app(g)) as c:
        r = await rpc(c, "search-a", "wrong", "tools/list")
        assert r.status_code == 401 and not calls
        r = await rpc(
            c,
            "search-a",
            "one",
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
        )
        assert r.status_code == 200 and "serverInfo" in r.json()["result"]
        a = (await rpc(c, "search-a", "one", "tools/list")).json()["result"]["tools"]
        b = (await rpc(c, "search-b", "two", "tools/list")).json()["result"]["tools"]
        assert a == b
        assert "review_url" in a[0]["inputSchema"]["properties"]
        dev = (await rpc(c, "devdex/", "dev", "tools/list")).json()["result"]["tools"]
        assert "review_url" not in dev[0]["inputSchema"]["properties"]
        r = await rpc(
            c,
            "search-a",
            "one",
            "tools/call",
            {
                "name": "search",
                "arguments": {"query": "docs", "review_url": data["attempts"]["a"]["review_url"]},
            },
        )
        assert not r.json()["result"].get("isError")
        assert len(calls) == 1
        assert "answers" not in json.dumps(r.json())
        r = await rpc(
            c, "devdex", "dev", "tools/call", {"name": "search", "arguments": {"query": "docs"}}
        )
        assert not r.json()["result"].get("isError") and len(calls) == 2


async def test_context_caps_restart_and_late_calls(setup):
    g, data, calls = setup
    auth = g.authenticate("search-a", "one")
    url = data["attempts"]["a"]["review_url"]
    with pytest.raises(ProviderError, match="unknown_review"):
        await g.execute("search", "q", "https://github.com/other", auth)
    assert not calls
    await g.execute("search", "q", url, auth)
    other = Gateway(g.routing, g.traces, search_fn=g.search_fn)
    await other.execute("search", "q", url, auth)
    with pytest.raises(ProviderError, match="call_cap"):
        await other.execute("search", "q", url, auth)
    assert len(calls) == 2
    data["attempts"]["a"]["status"] = "completed"
    data["attempts"]["b"] = {**data["attempts"]["a"], "review_url": url + "2", "status": "running"}
    g.routing.write_text(json.dumps(data))
    with pytest.raises(ProviderError, match="disallowed"):
        await g.execute("search", "late", url, auth)
    assert len(calls) == 2


async def test_disallowed_and_mapping_mutation(setup):
    g, data, calls = setup
    url = data["attempts"]["a"]["review_url"]
    auth = g.authenticate("search-a", "one")
    await g.execute("search", "q", url, auth)
    data["attempts"]["a"]["max_search_calls"] = 99
    g.routing.write_text(json.dumps(data))
    with pytest.raises(ProviderError, match="changed_attempt"):
        await g.execute("search", "q", url, auth)
    data["attempts"]["a"]["allowed_profiles"] = ["exa"]
    g.routing.write_text(json.dumps(data))
    with pytest.raises(ProviderError, match="disallowed"):
        await g.execute("search", "q", url, auth)
    assert len(calls) == 1


async def test_trace_redacts_keys_and_persistent_reservations(setup, monkeypatch):
    g, data, _ = setup
    monkeypatch.setenv("EXA_API_KEY", "VERYSECRET")

    async def search(q, p):
        return {"results": [], "raw": {"echo": "VERYSECRET", "token": "hidden"}}

    g.search_fn = search
    await g.execute(
        "search", "q", data["attempts"]["a"]["review_url"], g.authenticate("search-a", "one")
    )
    text = (g.traces / (digest("a") + ".jsonl")).read_text()
    assert "VERYSECRET" not in text and "hidden" not in text
    assert len(text.splitlines()) == 2


async def test_concurrent_cap_and_two_profile_attribution(setup):
    g, data, calls = setup
    url = data["attempts"]["a"]["review_url"]
    outcomes = await asyncio.gather(
        *[
            g.execute("search", "q", url, g.authenticate(route, token))
            for route, token in [("search-a", "one"), ("search-b", "two"), ("search-a", "one")]
        ],
        return_exceptions=True,
    )
    assert len(calls) == 2
    assert sum(isinstance(o, ProviderError) for o in outcomes) == 1
    rows = [
        json.loads(line) for line in (g.traces / (digest("a") + ".jsonl")).read_text().splitlines()
    ]
    assert {r["profile"] for r in rows} == {"keenable", "exa"}
    assert {r["attempt_id"] for r in rows} == {"a"}


@pytest.mark.parametrize("expiry", [None, "2000-01-01T00:00:00Z", "2999-01-01T00:00:00", "bad"])
async def test_expired_or_missing_expiry_has_zero_outbound(setup, expiry):
    g, data, calls = setup
    if expiry is None:
        data["attempts"]["a"].pop("expires_at")
    else:
        data["attempts"]["a"]["expires_at"] = expiry
    g.routing.write_text(json.dumps(data))
    with pytest.raises(ProviderError, match="expiry"):
        await g.execute(
            "search", "q", data["attempts"]["a"]["review_url"], g.authenticate("search-a", "one")
        )
    with pytest.raises(ProviderError, match="expiry"):
        g.authenticate("devdex", "dev")
    assert not calls
    assert not (g.traces / (digest("a") + ".jsonl")).exists()
    rejected = [
        json.loads(line)
        for line in (g.traces / (digest("a") + ".rejections.jsonl")).read_text().splitlines()
    ]
    assert rejected[0]["error"] == "attempt_expired_or_missing_expiry"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/ORG/REPO/pull/1/",
        "https://GitHub.com/Org/Repo/pull/1/files?x=1#diff",
        "https://github.com/org/repo/PULL/01",
    ],
)
async def test_review_variants_attribute_same_attempt(setup, url):
    g, _, calls = setup
    await g.execute("search", "docs", url, g.authenticate("search-a", "one"))
    assert len(calls) == 1
    rows = [
        json.loads(line) for line in (g.traces / (digest("a") + ".jsonl")).read_text().splitlines()
    ]
    assert rows[0]["attempt_id"] == "a"


async def test_rejections_safe_and_separate_from_outbound_calls(setup):
    g, data, calls = setup
    data["attempts"]["a"]["configuration"] = "keenable"
    g.routing.write_text(json.dumps(data))
    async with client_for(create_app(g)) as client:
        assert (await rpc(client, "search-a", "DO-NOT-LOG-TOKEN", "tools/list")).status_code == 401
    anonymous = (g.traces / "rejections.jsonl").read_text()
    assert "DO-NOT-LOG-TOKEN" not in anonymous
    assert json.loads(anonymous)["attempt_id"] is None
    with pytest.raises(ProviderError, match="unsafe_url"):
        await g.execute(
            "fetch",
            "https://user:password@example.com/path?token=SECRET",
            data["attempts"]["a"]["review_url"],
            g.authenticate("search-a", "one"),
        )
    rejection = json.loads((g.traces / (digest("a") + ".rejections.jsonl")).read_text())
    assert rejection["attempt_id"] == "a" and rejection["error"] == "unsafe_url"
    assert rejection["url"] == "https://example.com/path"
    assert "password" not in json.dumps(rejection) and "SECRET" not in json.dumps(rejection)
    assert not calls and not (g.traces / (digest("a") + ".jsonl")).exists()


async def test_startup_records_actual_reader_settings(setup, monkeypatch):
    g, _, _ = setup
    monkeypatch.setenv("SEARCH_EVAL_READER_MAX_BYTES", "12345")
    current = Gateway(g.routing, g.traces)
    async with client_for(create_app(current)):
        evidence = json.loads((g.traces / "gateway-config.json").read_text())
    assert evidence["reader"]["max_bytes"] == 12345
    assert evidence["provider_transport"]["timeout_seconds"] == 20


async def test_real_martian_prefixes_block_pr_fix_dashboard_search_and_fetch(tmp_path, monkeypatch):
    from pathlib import Path

    from test_core import config

    from search_eval import core, runner

    manifest = json.loads((core.ROOT / "data/martian-pilot.json").read_text())
    (tmp_path / "data.json").write_text(json.dumps(manifest))
    cfg = config()
    task = {"id": "fixture", "role": "scored", "kind": "pr_review", "input": {}}
    state = core.prepare_state(cfg, [task], tmp_path / "runs/test-run", tmp_path)
    a = next(a for a in state["attempts"] if a["configuration"] == "keenable")
    a.update(
        review_url="https://github.com/org/repo/pull/1",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.delenv("SEARCH_EVAL_ROUTING", raising=False)
    monkeypatch.setenv("A", "fixture-token")
    route = runner.routing_store(cfg, tmp_path)
    runner.register_attempt(
        route, cfg, a, task, next(arm for arm in cfg.arms if arm.id == "keenable"), root=tmp_path
    )
    prefixes = manifest["answer_source_rules"]["url_prefixes"]
    urls = [
        next(p for p in prefixes if "/pull/" in p) + "/files",
        next(p for p in prefixes if "/commit/" in p) + "?diff=1",
        "https://codereview.withmartian.com/dashboard",
        "https://raw.githubusercontent.com/withmartian/code-review-benchmark/main/offline/golden_comments/grafana.json",
        "https://raw.github.com/withmartian/code-review-benchmark/main/offline/golden_comments/sentry.json",
        "https://api.github.com/repos/withmartian/code-review-benchmark/contents/offline/golden_comments/cal_dot_com.json",
        "https://codeload.github.com/withmartian/code-review-benchmark/tar.gz/main",
    ]
    urls = [u.upper() for u in urls]

    async def search(query, profile):
        return {"results": [{"url": u} for u in urls] + [{"url": "https://docs.python.org/3/"}]}

    async def reader(url, limits, blocked):
        raise AssertionError("Blocked answer must cause zero reader/network calls")

    monkeypatch.setattr(
        "search_eval.gateway.shared_runtime_identity",
        lambda: core.shared_runtime_identity(tmp_path),
    )
    g = Gateway(Path(route), tmp_path / "traces", search_fn=search, reader_fn=reader)
    auth = g.authenticate("search-a", "fixture-token")
    assert await g.execute("search", "q", a["review_url"], auth) == {
        "results": [{"url": "https://docs.python.org/3/"}]
    }
    for url in urls:
        with pytest.raises(ProviderError, match="answer_page_blocked"):
            await g.execute("fetch", url, a["review_url"], auth)


@pytest.mark.parametrize("expected", [None, "stale-runtime"])
async def test_missing_or_wrong_gateway_runtime_has_zero_outbound(setup, expected):
    gateway, data, calls = setup
    if expected is None:
        del data["attempts"]["a"]["expected_gateway_runtime_sha256"]
    else:
        data["attempts"]["a"]["expected_gateway_runtime_sha256"] = expected
    gateway.routing.write_text(json.dumps(data))
    auth = gateway.authenticate("search-a", "one")
    with pytest.raises(ProviderError, match="gateway_runtime_mismatch"):
        await gateway.execute("search", "query", data["attempts"]["a"]["review_url"], auth)
    assert calls == []
    assert not (gateway.traces / (digest("a") + ".jsonl")).exists()


async def test_gateway_startup_and_call_trace_share_observed_runtime(setup):
    gateway, data, _ = setup
    gateway.write_effective_config()
    config = json.loads((gateway.traces / "gateway-config.json").read_text())
    assert config["runtime_sha256"] == shared_runtime_identity()
    auth = gateway.authenticate("search-a", "one")
    await gateway.execute("search", "query", data["attempts"]["a"]["review_url"], auth)
    rows = [
        json.loads(row)
        for row in (gateway.traces / (digest("a") + ".jsonl")).read_text().splitlines()
    ]
    assert all(row["gateway_runtime_sha256"] == config["runtime_sha256"] for row in rows)


async def test_authenticated_health_makes_no_provider_calls(setup):
    gateway, _, calls = setup
    async with client_for(create_app(gateway)) as client:
        assert (await client.get("/mcp/search-a/health")).status_code == 401
        response = await client.get("/mcp/search-a/health", headers={"Authorization": "Bearer one"})
        assert response.status_code == 200
        assert response.json() == {
            "runtime_sha256": gateway.runtime_identity,
            "reader": vars(gateway.limits),
        }
    assert calls == []
