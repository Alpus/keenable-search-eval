import asyncio
import socket

import httpx
import pytest

from search_eval.providers import (
    ProviderError,
    PublicResolver,
    normalize,
    public_url,
    search_provider,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://[::1]/",
        "https://u:p@example.com",
        "http://169.254.169.254/",
        "http://localhost",
        "http://example.local",
        "https://example.com:9999/",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(ProviderError):
        public_url(url)


def test_normalization():
    raw = {
        "results": [
            {"title": str(i), "url": f"https://example.com/{i}", "snippet": "x" * 1100}
            for i in range(15)
        ]
    }
    raw["results"].insert(1, {"url": "https://example.com/0#fragment", "snippet": "duplicate"})
    result, fields = normalize(raw, "keenable")
    assert len(result) == 10 and result[1]["title"] == "1"
    assert all(len(r["excerpt"]) == 1000 for r in result)
    assert fields == ["snippet"] * 10
    assert (
        normalize({"results": [{"url": "https://example.com", "text": "must not use"}]}, "exa")[0][
            0
        ]["excerpt"]
        == ""
    )


async def test_dns_resolution_pinned_and_mixed_private_rejected(monkeypatch):
    loop = asyncio.get_running_loop()

    async def public(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", public)
    addresses = await PublicResolver().resolve("example.com", 443)
    assert addresses[0]["host"] == "93.184.216.34"
    assert addresses[0]["flags"] == socket.AI_NUMERICHOST

    async def mixed(*args, **kwargs):
        return await public() + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", mixed)
    with pytest.raises(ProviderError, match="unsafe_dns"):
        await PublicResolver().resolve("example.com", 443)


async def test_provider_requests_and_error_redaction(monkeypatch):
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com",
                        "title": "a",
                        "snippet": "text",
                        "highlights": ["h"],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await search_provider(
            "q", {"provider": "keenable", "tier": "public", "mode": "pro"}, client
        )
        assert str(seen[0].url).endswith("/v1/search/public")
        assert seen[0].headers["x-keenable-title"]
        assert result["results"][0]["excerpt"] == "text"
        monkeypatch.setenv("EXA_API_KEY", "private-secret")
        result = await search_provider(
            "q", {"provider": "exa", "tier": "authenticated", "mode": "auto"}, client
        )
        assert result["results"][0]["excerpt"] == "h"
        assert seen[1].headers["x-api-key"] == "private-secret"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="private-secret"))
    ) as client:
        with pytest.raises(ProviderError, match="provider_http_500") as e:
            await search_provider(
                "q", {"provider": "exa", "tier": "authenticated", "mode": "auto"}, client
            )
        assert "private-secret" not in str(e.value)


async def test_reader_redirect_and_limits(monkeypatch):
    from search_eval import providers as p

    calls = []

    class Response:
        def __init__(self, status=200, headers=None, body=b"hello"):
            self.status, self.headers, self.body = (
                status,
                headers or {"Content-Type": "text/plain"},
                body,
            )
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, n):
            yield self.body

    responses = []

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return responses.pop(0)

    monkeypatch.setattr(p.aiohttp, "ClientSession", Session)
    responses.append(Response(302, {"Location": "http://127.0.0.1/secret"}))
    with pytest.raises(ProviderError, match="unsafe_url"):
        await p.read_page("https://example.com", p.Limits())
    assert len(calls) == 1
    responses.append(Response(body=b"12345"))
    with pytest.raises(ProviderError, match="byte_limit"):
        await p.read_page("https://example.com", p.Limits(max_bytes=4))
    responses.append(Response(headers={"Content-Type": "application/pdf"}))
    with pytest.raises(ProviderError, match="unsupported"):
        await p.read_page("https://example.com", p.Limits())
    responses.append(
        Response(headers={"Content-Type": "text/html"}, body=b"<p>Hi</p><script>SECRET</script>")
    )
    assert "SECRET" not in (await p.read_page("https://example.com", p.Limits()))["content"]
    with pytest.raises(ProviderError, match="answer_page_blocked"):
        await p.read_page("https://example.com/answers", p.Limits(), blocked=lambda u: True)


async def test_explicit_provider_tier_never_falls_back_or_upgrades(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
        with pytest.raises(ProviderError, match="missing_provider_credential"):
            await search_provider(
                "q", {"provider": "keenable", "mode": "pro", "tier": "authenticated"}, client
            )
        with pytest.raises(ProviderError, match="explicit_provider_tier_required"):
            await search_provider("q", {"provider": "keenable", "mode": "pro"}, client)
        assert not seen
        monkeypatch.setenv("KEENABLE_API_KEY", "present-but-unused")
        result = await search_provider(
            "q", {"provider": "keenable", "mode": "realtime", "tier": "public"}, client
        )
        assert (
            result["endpoint"].endswith("/public")
            and result["tier"] == "public"
            and result["mode"] == "realtime"
        )
        assert "x-api-key" not in seen[-1].headers
        keyed = await search_provider(
            "q", {"provider": "keenable", "mode": "pro", "tier": "authenticated"}, client
        )
        assert keyed["endpoint"].endswith("/v1/search") and keyed["tier"] == "authenticated"
        assert seen[-1].headers["x-api-key"] == "present-but-unused"
