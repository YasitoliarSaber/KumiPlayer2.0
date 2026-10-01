"""电影/剧集别名的不同 API 合同与无消费字段请求回归。"""

import httpx
import pytest

from app.media_v4.jobs.metadata import _extract_aliases
from app.scrape.tmdb_client import TMDBClient


def test_movie_titles_and_translations_preserve_independent_subtitle():
    detail = {
        "alternative_titles": {"titles": [{"title": "摇曳露营 剧场版"}]},
        "translations": {"translations": [{"data": {"title": "Yuru Camp Movie"}}]},
    }
    assert _extract_aliases(detail, "movie") == ["摇曳露营 剧场版", "Yuru Camp Movie"]


def test_tv_results_and_translations_deduplicate_without_changing_identity():
    detail = {
        "alternative_titles": {"results": [{"title": "房间露营"}]},
        "translations": {"translations": [{"data": {"name": "房间露营"}}]},
    }
    assert _extract_aliases(detail, "tv") == ["房间露营"]


@pytest.mark.parametrize("method,args,required", [
    ("get_tv_detail", (1,), {"images", "external_ids", "alternative_titles", "translations", "content_ratings"}),
    ("get_movie_detail", (1,), {"images", "external_ids", "alternative_titles", "translations", "release_dates"}),
    ("get_tv_season_detail", (1, 1), {"images", "external_ids", "translations"}),
    ("get_tv_episode_detail", (1, 1, 1), {"images", "external_ids", "translations"}),
])
def test_detail_request_excludes_people_but_keeps_required_metadata(method, args, required):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"id": 1})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with TMDBClient(bearer_token="fixture", rate_limit=0, _http_client=http) as client:
            getattr(client, method)(*args)
    assert len(calls) == 1
    appended = set(calls[0].url.params["append_to_response"].split(","))
    assert appended == required
