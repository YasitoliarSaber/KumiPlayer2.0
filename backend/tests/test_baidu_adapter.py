# -*- coding: utf-8 -*-
"""百度适配器测试（tree 格式）"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.sources.baidu import BaiduAdapter


def _write_temp(content: str) -> str:
    """写入临时文件"""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    f.write(content)
    f.close()
    return f.name


def test_basic_tree_parse():
    """基本 tree 格式解析"""
    content = """\
├── 新番
│   ├── test.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.source == "baidu"
    assert snapshot.file_count == 1
    assert snapshot.video_count == 1
    assert snapshot.files[0].relative_path == "新番/test.mkv"
    Path(path).unlink()


def test_nested_tree():
    """嵌套目录解析"""
    content = """\
├── 刮削好的动画
│   ├── 石纪元 (2019)
│   │   ├── Season 1
│   │   │   ├── S01E01.mkv
│   │   │   ├── S01E02.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 2
    assert snapshot.files[0].relative_path == "刮削好的动画/石纪元 (2019)/Season 1/S01E01.mkv"
    assert snapshot.files[1].relative_path == "刮削好的动画/石纪元 (2019)/Season 1/S01E02.mkv"
    Path(path).unlink()


def test_windows_tree_parse():
    """百度/Windows tree 常见的单横线分支也必须识别"""
    content = """\
├─动画
│  ├─露营
│  │  ├─摇曳露营 - S01E01.mkv
│  │  └─摇曳露营 - S01E02.mkv
└─动画电影
   └─红猪 (1992).mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 3
    assert snapshot.video_count == 3
    paths = [f.relative_path for f in snapshot.files]
    assert "动画/露营/摇曳露营 - S01E01.mkv" in paths
    assert "动画/露营/摇曳露营 - S01E02.mkv" in paths
    assert "动画电影/红猪 (1992).mkv" in paths
    Path(path).unlink()


def test_mixed_files():
    """混合文件类型"""
    content = """\
├── 动画
│   ├── test.mkv
│   ├── test.ass
│   ├── test.nfo
│   ├── poster.jpg
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 4
    assert snapshot.video_count == 1
    hints = {f.name: f.resource_hint for f in snapshot.files}
    assert hints["test.mkv"] == "video"
    assert hints["test.ass"] == "subtitle"
    assert hints["test.nfo"] == "nfo"
    assert hints["poster.jpg"] == "image"
    Path(path).unlink()


def test_xml_and_torrent_are_kept_as_files():
    """XML / torrent 是真实百度树中的文件，来源层不能无声丢弃"""
    content = """\
├── 刮削好的动画
│   ├── tvshow.xml
│   ├── source.torrent
│   ├── test.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 3
    hints = {f.name: f.resource_hint for f in snapshot.files}
    assert hints["tvshow.xml"] == "text"
    assert hints["source.torrent"] == "other"
    assert hints["test.mkv"] == "video"
    Path(path).unlink()


def test_reject_malformed_tree_prefix():
    """只接受标准 tree 缩进，避免把异常缩进误解析成路径"""
    content = """\
├── 动画
│ │ ├── bad.mkv
│   ├── good.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 1
    assert snapshot.files[0].relative_path == "动画/good.mkv"
    Path(path).unlink()


def test_system_file_skip():
    """系统文件跳过"""
    content = """\
├── 动画
│   ├── thumbs.db
│   ├── test.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 1
    assert snapshot.files[0].name == "test.mkv"
    Path(path).unlink()


def test_source_id_and_namespace():
    """source_id = baidu, mirror_namespace = baidu"""
    adapter = BaiduAdapter()
    assert adapter.source_id == "baidu"
    assert adapter.mirror_namespace == "baidu"


def test_real_path():
    """real_path 正确拼接"""
    content = """\
├── 新番
│   ├── test.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    real = snapshot.files[0].real_path
    assert "BaiduNetdisk" in real
    assert "test.mkv" in real
    Path(path).unlink()


def test_real_path_uses_animation_subroot():
    """百度目录树相对 01动画 导出时，real_path 必须拼到 01动画 下。"""
    content = """\
├── 动画电影
│   ├── 吹响吧！上低音号 剧场版：想要传达的旋律 (2017)
│   │   ├── 吹响吧！上低音号 剧场版：想要传达的旋律 (2017) 2160p.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, r"H:\百度网盘\01动画")
    real = snapshot.files[0].real_path
    assert real == r"H:\百度网盘\01动画\动画电影\吹响吧！上低音号 剧场版：想要传达的旋律 (2017)\吹响吧！上低音号 剧场版：想要传达的旋律 (2017) 2160p.mkv"
    Path(path).unlink()


def test_virtual_root():
    """virtual_root = 第一层目录"""
    content = """\
├── 新番
│   ├── test.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.files[0].virtual_root == "新番"
    Path(path).unlink()


def test_real_baidu_tree():
    """解析真实百度目录树样本"""
    content = """\
├── 新番
│   ├── [Sakurato] Steel Ball Run：JoJo no Kimyou na Bouken [01][HEVC-10bit 1080p AAC][CHS&CHT].mkv
├── 刮削好的动画
│   ├── 石纪元 (2019) {tmdbid-86031} [4K]
│   │   ├── Season 1
│   │   │   ├── 石纪元 - S01E01 - 石之世界.mkv
│   │   │   ├── 石纪元 - S01E02 - 石之世界的王者.mkv
│   │   ├── Specials
│   │   │   ├── 石纪元 - S00E03 - 龙水.mkv
"""
    path = _write_temp(content)
    adapter = BaiduAdapter()
    snapshot = adapter.parse(path, "D:/BaiduNetdisk")
    assert snapshot.file_count == 4
    assert snapshot.video_count == 4

    paths = [f.relative_path for f in snapshot.files]
    assert "新番/[Sakurato] Steel Ball Run：JoJo no Kimyou na Bouken [01][HEVC-10bit 1080p AAC][CHS&CHT].mkv" in paths
    assert "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Season 1/石纪元 - S01E01 - 石之世界.mkv" in paths
    assert "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Specials/石纪元 - S00E03 - 龙水.mkv" in paths
    Path(path).unlink()


if __name__ == "__main__":
    tests = [
        test_basic_tree_parse,
        test_nested_tree,
        test_windows_tree_parse,
        test_mixed_files,
        test_xml_and_torrent_are_kept_as_files,
        test_reject_malformed_tree_prefix,
        test_system_file_skip,
        test_source_id_and_namespace,
        test_real_path,
        test_real_path_uses_animation_subroot,
        test_virtual_root,
        test_real_baidu_tree,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
