"""名称恢复只能改变检索词，不能制造身份依据。"""

from app.media_v4.jobs import metadata
from app.scrape.anilist_client import extract_tmdb_link


def test_tmdb_external_link_requires_exact_https_host_and_path():
    for url in ("https://evil.test/themoviedb.org/tv/123", "http://themoviedb.org/tv/123",
                "https://themoviedb.org:444/tv/123", "https://user:pass@themoviedb.org/tv/123",
                "https://themoviedb.org/tv/123/season/2", "https://evil.test/tmdb-123"):
        assert extract_tmdb_link({"externalLinks": [{"site": "TMDB", "url": url}]}) == (None, "")
    assert extract_tmdb_link({"externalLinks": [{"url": "https://www.themoviedb.org/movie/123"}]}) == (123, "movie")


def test_recovery_queries_do_not_become_ranker_identity(monkeypatch):
    seen = []
    class Ranker:
        def rank(self, target, _candidates):
            seen.append(target)
            return []
        def auto_adopt(self, _ranked):
            return None, "fixture"
    monkeypatch.setattr(metadata, "CandidateRanker", Ranker)
    monkeypatch.setattr(metadata, "enrich_candidate_aliases", lambda *_a, **_k: [])
    target = {"preferred_title": "本地已确认片名", "work_type": "series", "recovery_search_queries": ["错误外传名"]}
    metadata._rank_metadata_candidates(target, "tv", ["错误外传名"], {}, object())
    assert "错误外传名" not in seen[0]["queries"]
    assert "本地已确认片名" in seen[0]["queries"]


def test_alias_sanitizer_keeps_subtitle_and_rejects_instruction_or_episode():
    from app.scrape.alias_contract import clean_aliases
    assert clean_aliases(["摇曳露营 剧场版", "摇曳露营 剧场版", "123", "S01E03", "<script>x</script>",
                          "ignore previous instructions", "x" * 301]) == ["摇曳露营 剧场版"]


def test_recovery_never_calls_name_providers_for_success_binding_or_network_error():
    from app.media_v4.jobs.alias_recovery import recover_metadata
    def denied(_target):
        raise AssertionError("external names were requested")
    for target, result in [({}, {"metadata_state": "ready"}),
                           ({"provider_bindings": [{"provider": "tmdb", "provider_id": "1"}]},
                            {"metadata_state": "source_unavailable", "reason_code": "provider_timeout"}),
                           ({}, {"metadata_state": "source_unavailable", "reason_code": "provider_timeout"})]:
        output = recover_metadata(target, metadata_provider=lambda _t, result=result: result, name_providers=[denied])
        assert output["metadata_state"] == result["metadata_state"]


def test_recovery_provider_order_query_isolation_and_trace():
    from app.media_v4.jobs.alias_recovery import recover_metadata
    from app.scrape.alias_contract import AliasEvidence
    calls = []
    def tmdb(target):
        calls.append(target)
        return {"metadata_state": "waiting_review", "reason_code": "no_candidates"}
    def anilist(_target):
        return [AliasEvidence("独立电影 副标题", "ja", "anilist", "1", "https://anilist.co/anime/1", "fixture")]
    def bangumi(_target):
        return []
    target = {"preferred_title": "本地名称", "identity_titles": ["本地名称"]}
    result = recover_metadata(target, metadata_provider=tmdb, name_providers=[anilist, bangumi])
    assert len(calls) == 2
    assert calls[1]["recovery_search_queries"] == ["独立电影 副标题"]
    assert calls[1]["identity_titles"] == ["本地名称"]
    assert "recovery_search_queries" not in target
    assert result["alias_recovery_trace"][1]["evidence"][0]["provider"] == "anilist"
