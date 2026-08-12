from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_import_review_exposes_a_real_editable_repair_form_for_every_video_issue():
    """目录树识别异常不能只给“忽略/已处理”两个不可逆选项。

    导入工作台属于 MediaManagementPage：docs/PROJECT.md 第 8 节页面职责表把“导入、确认、
    镜像、刮削和维护”归给它，SettingsPage 只负责“账户、主题、元数据、播放和支持”。
    SettingsPage 的分区清单另由 test_settings_outline_navigation_ui.py 以 page.index() 锁定，
    把导入 UI 搬回设置页会同时打破那条契约，因此这里断言真实落点。
    """
    page = (ROOT / "src" / "pages" / "MediaManagementPage.tsx").read_text(encoding="utf-8")

    assert "item.resource_type === 'video'" in page
    assert "作品名称" in page
    assert "分组" in page
    assert "季度" in page
    assert "集数" in page
    assert "保存处理结果" in page
    assert "group_type" in page
    assert "season_number" in page
    assert "episode_number" in page


def test_import_family_and_local_import_scope_are_bound_to_real_parse_requests():
    """导入入口的动画/影视、完结/追更选择必须传入后端，不能只是展示控件。

    family / importScope 由 mediaWorkflow store 持有而非页面局部 useState：预设回填
    (setFamily(result.preset.import_family)) 依赖跨步骤共享状态，降级成局部 state 会破坏它。
    """
    page = (ROOT / "src" / "pages" / "MediaManagementPage.tsx").read_text(encoding="utf-8")
    store = (ROOT / "src" / "stores" / "mediaWorkflow.ts").read_text(encoding="utf-8")
    sources_api = (ROOT / "src" / "api" / "sources.ts").read_text(encoding="utf-8")

    assert "setFamily(event.target.value as MediaWorkflowFamily)" in page
    assert "sourcesApi.scanLocal(path, family, family === 'anime' ? importScope : '')" in page

    # parse 调用是多行的，按调用区间取切片断言实参，避免锁死缩进。
    parse_start = page.index("sourcesApi.parse(")
    parse_call = page[parse_start : page.index(");", parse_start) + 2]
    assert "family," in parse_call
    assert "importScope" in parse_call
    assert "setFamily" in store
    assert "setImportScope" in store
    assert "import_family: importFamily" in sources_api
    assert "import_scope: importScope" in sources_api
