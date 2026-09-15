"""B6：115 / 百度目录树解析适配合同。

三件事：
1. 视频扩展名表补齐（旧链路识别 `.rmvb`，V4 曾经漏掉，会被当目录压栈并污染后面条目）；
2. 外壳根裁剪按 provider 决定：115 需要跳过导出外壳层，百度完全不需要，
   不能再用“任何一行命中 115 语法就整棵树裁一层”的粘性状态；
3. 正文 provider 探测：用户选错来源标签时，解析规则仍应按正文实际格式工作。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples"
_PAN115_SAMPLE = _SAMPLES / "根目录20260703203700_目录树.txt"


def test_rmvb_is_a_video_suffix_and_does_not_pollute_later_entries():
    """`.rmvb` 必须当视频；否则它会被压栈，后续同级条目凭空多一层祖先。"""

    from app.media_v4.sources.scanner import tree_media_relative_paths

    text = "\n".join([
        "├── 动画",
        "│   ├── Show",
        "│   │   ├── Show.S01E01.mkv",
        "│   │   ├── Show.S01E02.rmvb",
        "│   │   └── Show.S01E03.mkv",
    ])

    paths = tree_media_relative_paths(text, provider="baidu")

    assert paths == [
        "动画/Show/Show.S01E01.mkv",
        "动画/Show/Show.S01E02.rmvb",
        "动画/Show/Show.S01E03.mkv",
    ]


def test_shell_root_follows_content_grammar_not_the_source_label():
    """正文语法优先：选错来源标签也能解析正确；百度树永不被裁层。"""

    pan115_text = "\n".join([
        "|——根目录",
        "| |-动画",
        "| | |-Show",
        "| | | |-Show.S01E01.mkv",
    ])
    baidu_text = "\n".join([
        "├── 动画",
        "│   ├── Show",
        "│   │   └── Show.S01E01.mkv",
    ])

    from app.media_v4.sources.scanner import tree_media_relative_paths

    # 115 正文：外壳层 `根目录` 被裁掉，挂载语义要求的 `动画` 段保留；
    # 即使来源标签写成了百度也一样（这正是“用户选错来源”的真实场景）。
    assert tree_media_relative_paths(pan115_text, provider="pan115") == ["动画/Show/Show.S01E01.mkv"]
    assert tree_media_relative_paths(pan115_text, provider="baidu") == ["动画/Show/Show.S01E01.mkv"]

    # 百度正文：没有外壳层可裁，标签写成 115 也不能凭空裁掉第一段。
    assert tree_media_relative_paths(baidu_text, provider="baidu") == ["动画/Show/Show.S01E01.mkv"]
    assert tree_media_relative_paths(baidu_text, provider="pan115") == ["动画/Show/Show.S01E01.mkv"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("|——根目录\n| |-动画\n", "pan115"),
        ("动画\n├── Show\n", "baidu"),
        ("", "unknown"),
        ("随便写点东西\n", "unknown"),
    ],
)
def test_detect_tree_provider_reads_tree_grammar(text, expected):
    from app.media_v4.sources.scanner import detect_tree_provider

    assert detect_tree_provider(text) == expected


def test_detect_tree_provider_reports_ambiguous_mixed_grammar():
    from app.media_v4.sources.scanner import detect_tree_provider

    assert detect_tree_provider("|——根目录\n动画\n├── Show\n") == "ambiguous"


def test_real_pan115_sample_keeps_mount_semantics():
    """真实 115 导出：顶层段必须是 `动画`（挂载根/动画/... 语义）。"""

    if not _PAN115_SAMPLE.is_file():
        pytest.skip("本地脱敏样本不在仓库中（docs/ 未纳入版本控制）")

    from app.media_v4.sources.scanner import read_directory_tree_text, tree_media_relative_paths

    text = read_directory_tree_text(_PAN115_SAMPLE)
    paths = tree_media_relative_paths(text, provider="pan115")

    assert paths
    assert {path.split("/")[0] for path in paths} == {"动画"}
