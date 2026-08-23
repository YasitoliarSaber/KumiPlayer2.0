"""重复卡片根因修复回归（2026-08-23 实库数据）。

根因：durable pipeline 的 canonical 身份粒度错误——
1. main_series 使用 unit 级 canonical（``unit:{unit_id}``），「编号季目录」布局
   （``作品名.S1-S3+剧场版/1.作品名.[S1].年份``）把同一系列拆成多个 MediaUnit，
   每季一张卡（Re:Zero 6 张、辉夜 5 张、刀剑神域 4 张…）；
2. standalone 使用文件级 sub canonical（``unit:{uid}:sub:{digest}``），外传 TV
   系列每一集一张卡（Gun Gale Online 25 张）、同一电影双版本两张卡
   （剧场版：序列之争）。

修复：canonical 派生（discovery）与投影层（library index / media_libraries）
统一按系列/作品键收敛（``series:{键}`` / ``standalone:{键}:{年份}``），
人工绑定 / tracking 绑定身份不受影响。
"""

from app.library.identity import (
    effective_library_identity,
    series_identity_from_titles,
    standalone_identity_from_titles,
)

# ---------------------------------------------------------------------------
# 身份键归一化
# ---------------------------------------------------------------------------

class TestIdentityFromTitles:
    def test_series_identity_normalizes_whitespace_and_case(self):
        assert series_identity_from_titles(" 我推的孩子. ", "") == series_identity_from_titles("我推的孩子", "")

    def test_series_identity_falls_back_to_series_group(self):
        assert series_identity_from_titles("", "刀剑神域") == "series:刀剑神域"

    def test_series_identity_empty_when_no_titles(self):
        assert series_identity_from_titles("", "") == ""

    def test_standalone_identity_includes_year(self):
        a = standalone_identity_from_titles("剧场版：序列之争", "", 2017)
        b = standalone_identity_from_titles("剧场版：序列之争", "", None)
        assert a != b
        assert a == "standalone:剧场版：序列之争:2017"

    def test_same_title_different_years_are_distinct_movies(self):
        assert (
            standalone_identity_from_titles("作品A", "", 2020)
            != standalone_identity_from_titles("作品A", "", 2021)
        )


# ---------------------------------------------------------------------------
# 投影层有效身份收敛
# ---------------------------------------------------------------------------

class TestEffectiveLibraryIdentity:
    def test_auto_unit_canonical_merges_same_series(self):
        """同一系列被拆成两个 unit（不同 canonical）→ 收敛到同一系列身份。"""
        common = dict(card_type="main_series", work_title="Re：从零开始的异世界生活", series_group="")
        a = effective_library_identity(canonical="unit:" + "a" * 32, year=2020, **common)
        b = effective_library_identity(canonical="unit:" + "b" * 32, year=2024, **common)
        assert a == b == "series:re：从零开始的异世界生活"

    def test_auto_sub_canonical_merges_standalone_files(self):
        """外传系列每集的文件级 sub canonical → 同一 standalone 身份。"""
        common = dict(
            card_type="standalone",
            work_title="外传：Gun Gale Online",
            series_group="外传：Gun Gale Online",
            year=None,
        )
        a = effective_library_identity(canonical=f"unit:{'a' * 32}:sub:{'1' * 12}", **common)
        b = effective_library_identity(canonical=f"unit:{'a' * 32}:sub:{'2' * 12}", **common)
        assert a == b == "standalone:外传：gun gale online:"

    def test_manual_canonical_preserved(self):
        """人工绑定 / tracking 绑定（非自动格式）不参与收敛。"""
        assert (
            effective_library_identity(
                card_type="main_series", work_title="作品A", series_group="作品A",
                year=None, canonical="work_manual_binding",
            )
            == "work_manual_binding"
        )

    def test_non_hex_unit_canonical_preserved(self):
        """历史测试/编辑流使用的 unit:unit-xxx:main 形态保持原样。"""
        assert (
            effective_library_identity(
                card_type="main_series", work_title="作品", series_group="SAME",
                year=None, canonical="unit:unit-cp2-a:main",
            )
            == "unit:unit-cp2-a:main"
        )

    def test_empty_canonical_returns_empty(self):
        assert effective_library_identity(
            card_type="main_series", work_title="作品", series_group="作品",
            year=None, canonical="",
        ) == ""

    def test_no_title_auto_canonical_kept(self):
        assert effective_library_identity(
            card_type="main_series", work_title="", series_group="",
            year=None, canonical="unit:" + "a" * 32,
        ) == "unit:" + "a" * 32

    def test_series_identity_idempotent(self):
        identity = "series:刀剑神域"
        assert (
            effective_library_identity(
                card_type="main_series", work_title="刀剑神域", series_group="刀剑神域",
                year=None, canonical=identity,
            )
            == identity
        )


# ---------------------------------------------------------------------------
# canonical 派生（discovery）
# ---------------------------------------------------------------------------

class TestDeriveCanonicalWorkId:
    def _item(self, **overrides):
        from app.import_plan.models import ImportPlanItem

        base = dict(
            source="pan115", provider_id="pan115", relative_path="动画/作品/01.mkv",
            real_path="K:/115/作品/01.mkv", resource_type="video",
            action="generate_strm", work_id="w", canonical_work_id="",
            work_title="Re：从零开始的异世界生活", original_title="", year=None,
            media_type="tv", show_type="anime_series", series_group="Re：从零开始的异世界生活",
            card_type="main_series", belongs_to_series="", relation_type="",
            group_type="season", season_number=1, episode_number=1,
            special_number=None, title="", target_dir="", target_strm_path="",
            confidence="high", needs_review=False, availability="available",
        )
        base.update(overrides)
        return ImportPlanItem(**base)

    def test_same_series_across_units_shares_canonical(self):
        """编号季目录布局：同一系列的不同 unit 派生出同一 canonical。"""
        from app.catalog.discovery import _derive_canonical_work_id

        s1 = _derive_canonical_work_id("u" * 32, self._item(season_number=1))
        s2 = _derive_canonical_work_id("v" * 32, self._item(season_number=2))
        assert s1 == s2 == "series:re：从零开始的异世界生活"

    def test_standalone_series_episodes_share_canonical(self):
        """外传 TV 系列的每一集共享同一 canonical（不再每集一张卡）。"""
        from app.catalog.discovery import _derive_canonical_work_id

        kwargs = dict(
            work_title="外传：Gun Gale Online", series_group="外传：Gun Gale Online",
            card_type="standalone", group_type="season",
        )
        ep1 = _derive_canonical_work_id("u" * 32, self._item(episode_number=1, **kwargs))
        ep2 = _derive_canonical_work_id("u" * 32, self._item(episode_number=2, **kwargs))
        assert ep1 == ep2 == "standalone:外传：gun gale online:"

    def test_standalone_movie_multi_file_shares_canonical(self):
        from app.catalog.discovery import _derive_canonical_work_id

        kwargs = dict(
            work_title="剧场版：序列之争", series_group="刀剑神域",
            card_type="standalone", group_type="movie", year=2017,
        )
        v1 = _derive_canonical_work_id("u" * 32, self._item(relative_path="动画/a/1.mkv", **kwargs))
        v2 = _derive_canonical_work_id("u" * 32, self._item(relative_path="动画/a/2.mkv", **kwargs))
        assert v1 == v2 == "standalone:剧场版：序列之争:2017"

    def test_no_title_falls_back_to_unit(self):
        from app.catalog.discovery import _derive_canonical_work_id

        item = self._item(work_title="", series_group="")
        assert _derive_canonical_work_id("u" * 32, item) == f"unit:{'u' * 32}"

    def test_prebound_canonical_wins(self):
        from app.catalog.discovery import _derive_canonical_work_id

        item = self._item(canonical_work_id="manual-binding")
        assert _derive_canonical_work_id("u" * 32, item) == "manual-binding"


# ---------------------------------------------------------------------------
# LibraryIndex 聚合（卡片合并）
# ---------------------------------------------------------------------------

def _plan_item(relative_path: str, **overrides):
    from app.import_plan.models import ImportPlanItem

    strm = f"H:/mirror/115/{relative_path}".replace(".mkv", ".strm")
    base = dict(
        id="", source="pan115", provider_id="pan115",
        relative_path=relative_path, real_path=f"K:/115/{relative_path}",
        resource_type="video", action="generate_strm",
        work_id="w", canonical_work_id="",
        work_title="Re：从零开始的异世界生活", original_title="", year=2016,
        media_type="tv", show_type="anime_series",
        series_group="Re：从零开始的异世界生活",
        card_type="main_series", belongs_to_series="", relation_type="",
        group_type="season", season_number=1, episode_number=1,
        special_number=None, title="", target_dir=strm.rsplit("/", 1)[0],
        target_strm_path=strm,
        confidence="high", needs_review=False, availability="available",
    )
    base.update(overrides)
    return ImportPlanItem(**base)


class TestBuildLibraryIndexMergesDuplicateCards:
    def _build_plan(self, items):
        from app.import_plan.models import ImportPlan

        return ImportPlan(plan_id="p-dup", source="pan115", status="confirmed", items=items)

    def _scan_result(self, items):
        from app.library.scanner import MirrorFile, MirrorScanResult

        return MirrorScanResult(
            source="pan115",
            strm_files=[
                MirrorFile(source="pan115", strm_path=item.target_strm_path, exists=True)
                for item in items
            ],
        )

    def test_seasons_across_units_merge_into_one_card(self):
        """同一系列 S1/S2 分布在不同 unit（不同 auto canonical）→ 一张卡。"""
        from app.library.index import build_library_index

        items = [
            _plan_item(
                "动画/Re零/1.[S1].2016/01.mkv",
                canonical_work_id="unit:" + "a" * 32,
                season_number=1, episode_number=1,
            ),
            _plan_item(
                "动画/Re零/4.[S2].2020/01.mkv",
                canonical_work_id="unit:" + "b" * 32,
                season_number=2, episode_number=1,
            ),
        ]
        index = build_library_index(self._build_plan(items), scan_result=self._scan_result(items))
        assert len(index.works) == 1
        work = index.works[0]
        assert work.work_id == "series:re：从零开始的异世界生活"
        assert work.title == "Re：从零开始的异世界生活"
        assert len(work.episodes) == 2
        assert {e.season_number for e in work.episodes} == {1, 2}

    def test_standalone_episodes_merge_into_one_card(self):
        """外传系列多集文件级 canonical → 一张卡。"""
        from app.library.index import build_library_index

        items = [
            _plan_item(
                f"动画/刀剑/外传GunGale/第1季 {n:02d}.mkv",
                canonical_work_id=f"unit:{'a' * 32}:sub:{'0' + str(n) * 11}",
                work_title="外传：Gun Gale Online", series_group="外传：Gun Gale Online",
                card_type="standalone", group_type="season", season_number=1,
                episode_number=n, year=None,
            )
            for n in range(1, 4)
        ]
        index = build_library_index(self._build_plan(items), scan_result=self._scan_result(items))
        assert len(index.works) == 1
        assert index.works[0].work_id == "standalone:外传：gun gale online:"
        assert len(index.works[0].episodes) == 3

    def test_fragment_series_group_falls_back_to_work_title(self):
        """鬼灭之刃式编号季目录：series_group 碎片不得作为卡片标题。"""
        from app.library.index import build_library_index

        items = [
            _plan_item(
                "动画/鬼灭/1.立志篇.[S1].2019/01.mkv",
                canonical_work_id="unit:" + "a" * 32,
                work_title="鬼灭之刃系列", series_group="1.立志篇.",
                season_number=1, episode_number=1,
            ),
        ]
        index = build_library_index(self._build_plan(items), scan_result=self._scan_result(items))
        assert index.works[0].title == "鬼灭之刃系列"

    def test_manual_canonical_cards_stay_distinct(self):
        """人工绑定不同 canonical 的条目不因标题相同被合并。"""
        from app.library.index import build_library_index

        items = [
            _plan_item("动画/A/01.mkv", canonical_work_id="manual-a"),
            _plan_item("动画/B/01.mkv", canonical_work_id="manual-b"),
        ]
        index = build_library_index(self._build_plan(items), scan_result=self._scan_result(items))
        assert len(index.works) == 2
