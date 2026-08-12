# -*- coding: utf-8 -*-
"""NFO 生成器测试"""

import pytest
from pathlib import Path

from app.scrape.nfo import (
    generate_tvshow_nfo,
    generate_movie_nfo,
    generate_episode_nfo,
    write_nfo,
)


# ============================================================
# tvshow.nfo 测试
# ============================================================

class TestTvshowNfo:
    """测试 tvshow.nfo 生成"""

    def test_basic_fields(self):
        """基础字段"""
        nfo = generate_tvshow_nfo(title="CLANNAD", year=2007)
        assert "<title>CLANNAD</title>" in nfo
        assert "<year>2007</year>" in nfo
        assert '<?xml version="1.0"' in nfo
        assert "<tvshow>" in nfo

    def test_all_fields(self):
        """所有字段"""
        nfo = generate_tvshow_nfo(
            title="CLANNAD",
            original_title="CLANNAD",
            year=2007,
            plot="A story about...",
            tmdb_id=12189,
            season=1,
            rating=8.5,
            genres=["Animation", "Drama"],
            studios=["Kyoto Animation"],
            premiered="2007-10-04",
            runtime=24,
            cast=[{"name": "中村悠一", "role": "银古", "profile_path": "https://image.tmdb.org/t/p/w185/a.jpg"}],
        )
        assert "<originaltitle>CLANNAD</originaltitle>" in nfo
        assert "<plot>A story about...</plot>" in nfo
        assert "<tmdbid>12189</tmdbid>" in nfo
        assert "<season>1</season>" in nfo
        assert "<rating>8.5</rating>" in nfo
        assert "<genre>Animation</genre>" in nfo
        assert "<genre>Drama</genre>" in nfo
        assert "<studio>Kyoto Animation</studio>" in nfo
        assert "<premiered>2007-10-04</premiered>" in nfo
        assert "<runtime>24</runtime>" in nfo
        assert "<name>中村悠一</name>" in nfo
        assert "<role>银古</role>" in nfo
        assert "<thumb>https://image.tmdb.org/t/p/w185/a.jpg</thumb>" in nfo

    def test_xml_escape(self):
        """XML 转义"""
        nfo = generate_tvshow_nfo(
            title="Title & <Test>",
            plot='Quote "test" here',
        )
        assert "Title &amp; &lt;Test&gt;" in nfo
        assert "Quote &quot;test&quot; here" in nfo  # quote 也会被转义

    def test_empty_optional_fields(self):
        """空可选字段不生成标签"""
        nfo = generate_tvshow_nfo(title="Test")
        assert "<originaltitle>" not in nfo
        assert "<plot>" not in nfo
        assert "<tmdbid>" not in nfo
        assert "<rating>" not in nfo
        assert "<genre>" not in nfo
        assert "<studio>" not in nfo
        assert "<premiered>" not in nfo
        assert "<runtime>" not in nfo

    def test_multiple_genres(self):
        """多个 genre"""
        nfo = generate_tvshow_nfo(title="Test", genres=["Action", "Comedy", "Drama"])
        assert "<genre>Action</genre>" in nfo
        assert "<genre>Comedy</genre>" in nfo
        assert "<genre>Drama</genre>" in nfo

    def test_multiple_studios(self):
        """多个 studio"""
        nfo = generate_tvshow_nfo(title="Test", studios=["Studio A", "Studio B"])
        assert "<studio>Studio A</studio>" in nfo
        assert "<studio>Studio B</studio>" in nfo


# ============================================================
# movie.nfo 测试
# ============================================================

class TestMovieNfo:
    """测试 movie.nfo 生成"""

    def test_basic_fields(self):
        """基础字段"""
        nfo = generate_movie_nfo(title="Paprika", year=2006)
        assert "<title>Paprika</title>" in nfo
        assert "<year>2006</year>" in nfo
        assert "<movie>" in nfo

    def test_all_fields(self):
        """所有字段"""
        nfo = generate_movie_nfo(
            title="Paprika",
            original_title="パプリカ",
            year=2006,
            plot="A device...",
            tmdb_id=974,
            rating=8.0,
            genres=["Animation", "Sci-Fi"],
            studios=["Madhouse"],
            releasedate="2006-09-02",
            runtime=90,
        )
        assert "<originaltitle>パプリカ</originaltitle>" in nfo
        assert "<rating>8.0</rating>" in nfo
        assert "<genre>Animation</genre>" in nfo
        assert "<studio>Madhouse</studio>" in nfo
        assert "<releasedate>2006-09-02</releasedate>" in nfo
        assert "<runtime>90</runtime>" in nfo

    def test_year_as_releasedate_fallback(self):
        """无 releasedate 时用 year 作为 fallback"""
        nfo = generate_movie_nfo(title="Test", year=2020)
        assert "<releasedate>2020</releasedate>" in nfo


# ============================================================
# episode NFO 测试
# ============================================================

class TestEpisodeNfo:
    """测试 episode NFO 生成"""

    def test_basic_fields(self):
        """基础字段"""
        nfo = generate_episode_nfo(
            title="The Revival",
            season=1,
            episode=1,
        )
        assert "<title>The Revival</title>" in nfo
        assert "<season>1</season>" in nfo
        assert "<episode>1</episode>" in nfo
        assert "<episodedetails>" in nfo

    def test_all_fields(self):
        """所有字段"""
        nfo = generate_episode_nfo(
            title="The Revival",
            season=1,
            episode=1,
            plot="In this episode...",
            runtime=24,
            aired="2012-04-22",
            tmdb_id=123456,
            thumb="/path/to/thumb.jpg",
        )
        assert "<plot>In this episode...</plot>" in nfo
        assert "<runtime>24</runtime>" in nfo
        assert "<aired>2012-04-22</aired>" in nfo
        assert "<tmdbid>123456</tmdbid>" in nfo
        assert "<thumb>/path/to/thumb.jpg</thumb>" in nfo


# ============================================================
# write_nfo 测试
# ============================================================

class TestWriteNfo:
    """测试 NFO 写入"""

    def test_write_tvshow_nfo(self, tmp_path):
        """写入 tvshow.nfo"""
        content = generate_tvshow_nfo(title="Test")
        result = write_nfo(str(tmp_path), "tvshow.nfo", content)
        assert Path(result).exists()
        assert Path(result).read_text(encoding="utf-8") == content

    def test_write_movie_nfo(self, tmp_path):
        """写入 movie.nfo"""
        content = generate_movie_nfo(title="Test")
        result = write_nfo(str(tmp_path), "movie.nfo", content)
        assert Path(result).exists()

    def test_write_episode_nfo(self, tmp_path):
        """写入 episode NFO"""
        content = generate_episode_nfo(title="Test", season=1, episode=1)
        result = write_nfo(str(tmp_path), "S01E01.nfo", content)
        assert Path(result).exists()

    def test_reject_invalid_filename(self, tmp_path):
        """拒绝非法文件名"""
        with pytest.raises(ValueError, match="不允许"):
            write_nfo(str(tmp_path), "evil.nfo", "content")

    def test_reject_path_traversal(self, tmp_path):
        """拒绝路径遍历"""
        with pytest.raises(ValueError, match="不允许"):
            write_nfo(str(tmp_path), "../../../evil.nfo", "content")

    def test_creates_parent_dirs(self, tmp_path):
        """自动创建父目录"""
        content = generate_tvshow_nfo(title="Test")
        result = write_nfo(str(tmp_path / "sub" / "dir"), "tvshow.nfo", content)
        assert Path(result).exists()
