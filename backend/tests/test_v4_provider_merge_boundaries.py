from itertools import permutations

from app.media_v4.domain.models import ResolvedMediaGraph, ResolvedWork
from app.media_v4.resolution.candidates import WorkCandidate, merge_map_from_candidates


def _graph(*titles):
    return ResolvedMediaGraph(works=tuple(
        ResolvedWork(work_key=str(i), preferred_title=title, year=None, media_type="tv")
        for i, title in enumerate(titles)
    ))


def _candidate(key, title, aliases=()):
    return WorkCandidate(key, "tmdb", "1", "tv", title, None, "search", "high",
                         status="confirmed", aliases=aliases)


def test_shared_series_id_does_not_merge_different_local_titles():
    graph = _graph("化物语", "伤物语", "猫物语")
    candidates = {w.work_key: [_candidate(w.work_key, "物语系列")] for w in graph.works}
    assert merge_map_from_candidates(graph, candidates) == {}


def test_explicit_aliases_preserve_cross_language_merge():
    graph = _graph("辉夜大小姐想让我告白", "Kaguya-sama")
    candidates = {w.work_key: [_candidate(w.work_key, "辉夜大小姐想让我告白", ("Kaguya-sama",))]
                  for w in graph.works}
    assert len(merge_map_from_candidates(graph, candidates)) == 1


def test_alias_bridges_cannot_merge_unrelated_endpoints_and_are_order_independent():
    graph = _graph("Alpha", "Beta", "Gamma")
    candidates = {
        "0": [_candidate("0", "Alpha", ("Beta",))],
        "1": [_candidate("1", "Beta", ("Gamma",))],
        "2": [_candidate("2", "Gamma")],
    }
    results = []
    for works in permutations(graph.works):
        result = merge_map_from_candidates(ResolvedMediaGraph(works=works), candidates)
        assert len(result) == 1
        results.append(result)
    assert all(result == results[0] for result in results)


def test_equal_local_titles_still_merge():
    graph = _graph("Show", "Show")
    candidates = {w.work_key: [_candidate(w.work_key, "Show")] for w in graph.works}
    assert len(merge_map_from_candidates(graph, candidates)) == 1


def test_verified_translation_can_bridge_candidates_without_downloaded_aliases():
    graph = _graph("摇曳露营", "Yuru Camp")
    candidates = {w.work_key: [_candidate(w.work_key, w.preferred_title)] for w in graph.works}
    assert len(merge_map_from_candidates(graph, candidates)) == 1


def test_matching_each_local_name_to_shared_id_is_not_translation_evidence():
    graph = _graph("Alpha", "Gamma")
    candidates = {w.work_key: [_candidate(w.work_key, w.preferred_title)] for w in graph.works}
    assert merge_map_from_candidates(graph, candidates) == {}


def test_no_candidates_do_not_trigger_pairwise_grouping(monkeypatch):
    from app.media_v4.resolution import candidates as module

    graph = _graph(*(f"Work {index}" for index in range(4000)))
    pair_operations = 0

    def counted_frozenset(values):
        nonlocal pair_operations
        pair_operations += 1
        return frozenset(values)

    monkeypatch.setattr(module, "frozenset", counted_frozenset, raising=False)
    assert merge_map_from_candidates(graph, {}) == {}
    assert pair_operations == 0


def test_shared_identity_group_does_not_rescan_every_member_for_each_pair(monkeypatch):
    """同一身份组内的判据只能随“这一对”的候选量增长，不能逐对重扫整组成员。

    组内两两检查本身是 k² 的（证据只对单条候选成立），但逐对重扫整组成员会把成本
    变成 k³：k=1000 时实测 45s 对 3.7s。这里用规范化调用次数锁定重扫不会回来。
    """

    from app.media_v4.resolution import candidates as module

    member_count = 130
    graph = _graph(*(f"作品{index}" for index in range(member_count)))
    # 所有作品共享同一个在线身份，且各自命中自己的标题（仍不构成译名证据）。
    candidates = {
        work.work_key: [_candidate(work.work_key, work.preferred_title)]
        for work in graph.works
    }
    calls = 0
    original = module._normalize_title

    def counting_normalize_title(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(module, "_normalize_title", counting_normalize_title)

    assert merge_map_from_candidates(graph, candidates) == {}

    pairs = member_count * (member_count - 1) // 2
    # 每对只需：两个本地标题 2 次 + 双方候选的 title/original_title/aliases 3×2 次。
    # 逐对重扫成员的实现是 O(k) 倍（本夹具约 782 次/对），必然超出这个宽松上界。
    assert calls <= pairs * 12, f"规范化调用 {calls} 次，超出 {pairs * 12} 的每对常数上界"
