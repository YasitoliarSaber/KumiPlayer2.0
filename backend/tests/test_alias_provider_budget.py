import httpx
import pytest

from app.scrape.anilist_client import AniListClient, AniListRateLimitError


def test_429_records_shared_cooldown_without_sleep(monkeypatch):
    from app.scrape import provider_budget
    monkeypatch.setattr(provider_budget, "_budgets", {})
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "60"})
    monkeypatch.setattr("app.scrape.anilist_client.time.sleep", lambda _s: pytest.fail("429 slept instead of deferring"))
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        with AniListClient(rate_limit=3, _http_client=http) as first:
            with pytest.raises(AniListRateLimitError):
                first.search_anime("fixture")
        with AniListClient(rate_limit=3, _http_client=http) as second:
            with pytest.raises(AniListRateLimitError):
                second.search_anime("another")
    assert len(calls) == 1


def test_cancelled_provider_budget_never_requests(monkeypatch):
    from app.scrape import provider_budget
    monkeypatch.setattr(provider_budget, "_budgets", {})
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("cancelled request"))) as http:
        with AniListClient(rate_limit=3, _http_client=http, should_cancel=lambda: True) as client:
            with pytest.raises(Exception, match="cancel"):
                client.search_anime("fixture")
