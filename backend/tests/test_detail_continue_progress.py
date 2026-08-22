from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_bangumi_progress_retries_when_connection_recovers():
    """详情页必须订阅连接状态，不能只依赖只发布一次的首次连接 revision。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert (
        "const bangumiSessionStatus = useBangumiStore((state) => state.sessionStatus);"
        in page
    )
    assert "previousBangumiSessionStatusRef" in page
    assert "bangumiSessionStatus !== 'connected'" in page
    assert "previousStatus === 'connected'" in page
    assert "[bangumiSessionStatus, work?.work_id]" in page


def test_bangumi_auto_sync_only_runs_for_durable_pending_progress():
    """自动补同步只处理本地待上传记录，刷新结果时不得再次触发同步。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "pendingBangumiSyncEpisodeIds" in page
    assert "item?.completed && !item.bangumi_synced" in page
    assert "skipProgressSync?: boolean" in page
    assert "skipProgressSync: true" in page


def test_continue_playback_card_is_the_only_detail_progress_surface():
    """观看进度只能存在于继续播放卡片，剧集缩略图不得绘制进度。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "detail-continue-btn ${continuePercent > 0 ? 'has-progress' : ''}" in page
    assert "detail-action-progress" in page
    assert "--continue-progress" in page
    assert 'role="progressbar"' in page
    assert 'aria-label="观看进度"' in page
    assert "aria-valuemin={0}" in page
    assert "aria-valuemax={100}" in page
    assert "aria-valuenow={continuePercent}" in page
    assert "(${continuePercent}%)" not in page
    assert "continuePercent > 0 && (" in page
    assert "detail-continue-btn:hover .detail-continue-copy { opacity: 0" not in css

    episode_progress_start = css.index(".episode-item.has-progress::before {")
    episode_progress_end = css.index("}\n", episode_progress_start)
    episode_progress_rules = css[episode_progress_start:episode_progress_end]
    assert "content: none !important" in episode_progress_rules
    assert "var(--episode-progress" not in episode_progress_rules


def test_bangumi_match_is_a_quiet_badge_next_to_genre_tags():
    """Bangumi 状态属于作品元数据，不应在播放区形成第二个高亮操作块。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    tags_start = page.index('<div className="detail-hero-tags">')
    status_start = page.index('className={`detail-sync-status')
    plot_start = page.index('{work.plot && (')
    play_stack_start = page.index('<div className="detail-play-stack">')
    favorite_start = page.index('<button className={`detail-favorite-btn')

    assert tags_start < status_start < plot_start
    assert "detail-sync-status" not in page[play_stack_start:favorite_start]

    final_layer = css.split("/* 最终详情交互层：", 1)[1]
    badge_selector = ".detail-page.detail-classic-page .detail-hero-tags .detail-sync-status"
    assert badge_selector in final_layer
    badge_start = final_layer.index(badge_selector)
    badge_end = final_layer.index("}\n", badge_start)
    badge_rules = final_layer[badge_start:badge_end]
    assert "min-width: 0" in badge_rules
    assert "min-height: 26px !important" in badge_rules
    assert "background: rgb(20 27 36 / .34) !important" in badge_rules
    assert "color: rgb(255 255 255 / .78) !important" in badge_rules
    assert "box-shadow: none !important" in badge_rules
    assert "rgb(25 112 64 / .90)" not in final_layer

    matched_icon_selector = (
        ".detail-page.detail-classic-page .detail-hero-tags "
        ".detail-sync-status.matched svg"
    )
    assert matched_icon_selector in final_layer
    icon_start = final_layer.index(matched_icon_selector)
    icon_end = final_layer.index("}\n", icon_start)
    icon_rules = final_layer[icon_start:icon_end]
    assert "color: rgb(141 207 163 / .88)" in icon_rules


def test_continue_playback_progress_is_readable_before_and_after_hover():
    """静态态保留播放提示，悬浮态居中显示剧集并用短标签显示进度。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "<strong>开始播放</strong>" in page
    assert 'className="detail-continue-hover-copy"' in page
    assert "`${continueEpisodeCode}${continueEpisodeTitle ? ` · ${continueEpisodeTitle}` : ''}`" in page
    assert "{`${continuePercent}%`}" in page
    assert "已播放 ${continuePercent}%" not in page

    expected_rest_surfaces = {
        "fluent": "rgb(13 25 32 / .34)",
        "cinema": "rgb(12 12 12 / .38)",
        # 纯白主题的详情播放按钮仍以冷中性暗层承托白色文字。
        "mica": "rgb(22 25 28 / .34)",
    }
    expected_hover_surfaces = {
        "fluent": "rgb(13 25 32 / .42)",
        "cinema": "rgb(12 12 12 / .46)",
        "mica": "rgb(22 25 28 / .42)",
    }
    expected_progress_surfaces = {
        "fluent": ("rgb(181 208 214 / .11)", "rgb(201 220 224 / .15)", "#d2e4e8"),
        "cinema": ("rgb(231 234 238 / .09)", "rgb(239 241 244 / .13)", "#e0e4e8"),
        # 纯白主题进度色阶保持低对比中性灰，避免暖色偏移。
        "mica": ("rgb(229 231 235 / .11)", "rgb(229 231 235 / .15)", "#e5e7eb"),
    }
    for theme in ("fluent", "cinema", "mica"):
        theme_selector = f":root[data-theme='{theme}'] .detail-classic-page"
        theme_start = css.index(theme_selector)
        theme_end = css.index("}\n", theme_start)
        theme_rules = css[theme_start:theme_end]
        assert f"--detail-play-rest-surface: {expected_rest_surfaces[theme]}" in theme_rules
        assert "--detail-play-rest-border:" in theme_rules
        assert "--detail-play-rest-ink: #fff" in theme_rules
        assert f"--detail-play-hover-surface: {expected_hover_surfaces[theme]}" in theme_rules
        assert "--detail-play-hover-ink: #fff" in theme_rules
        rest_progress, hover_progress, progress_ink = expected_progress_surfaces[theme]
        assert f"--detail-play-progress-rest-surface: {rest_progress}" in theme_rules
        assert "--detail-play-progress-rest-edge:" in theme_rules
        assert f"--detail-play-progress-hover-surface: {hover_progress}" in theme_rules
        assert "--detail-play-progress-hover-edge:" in theme_rules
        assert f"--detail-play-progress-fill: {progress_ink}" in theme_rules
        assert "--detail-play-progress-label-surface:" in theme_rules

        for rejected_progress_color in ("#ff654f", "rgb(255 101 79", "#0f6cbd", "#2563eb"):
            assert rejected_progress_color not in theme_rules

    final_layer = css.split("/* 最终详情交互层：", 1)[1]
    assert ".detail-page.detail-classic-page .detail-action-btn.primary.detail-continue-btn" in final_layer
    assert "background: var(--detail-play-rest-surface) !important" in final_layer
    assert "border-color: var(--detail-play-rest-border) !important" in final_layer
    assert "color: var(--detail-play-rest-ink) !important" in final_layer
    assert "backdrop-filter: blur(16px) saturate(1.04) !important" in final_layer
    assert "transition: transform 180ms var(--ease-out)" in final_layer
    hover_selector = (
        ".detail-page.detail-classic-page "
        ".detail-action-btn.primary.detail-continue-btn:is(:hover, :focus-visible)"
    )
    assert hover_selector in final_layer
    play_rule_start = final_layer.index(
        ".detail-page.detail-classic-page .detail-action-btn.primary.detail-continue-btn"
    )
    play_rule_end = final_layer.index("}\n", play_rule_start)
    play_rules = final_layer[play_rule_start:play_rule_end]
    assert "-webkit-backdrop-filter" not in play_rules
    assert "min-height: 64px !important" in play_rules
    assert "grid-template-columns: 20px minmax(0, 1fr) !important" in play_rules
    assert "column-gap: 12px !important" in play_rules
    hover_rule_start = final_layer.index(hover_selector)
    hover_rule_end = final_layer.index("}\n", hover_rule_start)
    hover_rules = final_layer[hover_rule_start:hover_rule_end]
    assert "backdrop-filter: blur(20px) saturate(1.08) !important" in hover_rules
    assert "-webkit-backdrop-filter" not in hover_rules
    assert ".detail-continue-hover-copy" in final_layer
    assert "justify-content: center" in final_layer
    copy_start = final_layer.index(
        ".detail-page.detail-classic-page .detail-continue-btn .detail-continue-copy {"
    )
    copy_end = final_layer.index("}\n", copy_start)
    copy_rules = final_layer[copy_start:copy_end]
    assert "gap: 3px !important" in copy_rules
    assert "min-width: 0" in copy_rules
    assert "text-align: left" in copy_rules

    strong_start = final_layer.index(
        ".detail-page.detail-classic-page .detail-continue-btn .detail-continue-copy strong {"
    )
    strong_end = final_layer.index("}\n", strong_start)
    strong_rules = final_layer[strong_start:strong_end]
    assert "color: rgb(255 255 255 / .98) !important" in strong_rules
    assert "font-size: 16px !important" in strong_rules
    assert "line-height: 1.15 !important" in strong_rules

    small_start = final_layer.index(
        ".detail-page.detail-classic-page .detail-continue-btn .detail-continue-copy small {"
    )
    small_end = final_layer.index("}\n", small_start)
    small_rules = final_layer[small_start:small_end]
    assert "color: rgb(255 255 255 / .78) !important" in small_rules
    assert "font-size: 13px !important" in small_rules
    assert "font-weight: 600 !important" in small_rules
    assert "line-height: 1.25 !important" in small_rules
    assert "text-overflow: ellipsis" in small_rules
    assert "var(--detail-ink)" not in small_rules

    hover_copy_start = final_layer.index(".detail-continue-hover-copy {")
    hover_copy_end = final_layer.index("}\n", hover_copy_start)
    hover_copy_rules = final_layer[hover_copy_start:hover_copy_end]
    assert "font-size: 13px" in hover_copy_rules
    assert ".detail-continue-btn:is(:hover, :focus-visible) .detail-continue-icon" in final_layer
    assert ".detail-continue-btn:is(:hover, :focus-visible) .detail-continue-copy" in final_layer
    assert ".detail-continue-btn:is(:hover, :focus-visible) .detail-continue-hover-copy" in final_layer
    assert ".detail-continue-btn.has-progress .detail-action-progress" in final_layer
    assert "inset: 0 auto 0 0 !important" in final_layer
    assert "width: var(--continue-progress, 0%) !important" in final_layer
    assert "height: auto !important" in final_layer
    assert "border-radius: inherit 0 0 inherit !important" in final_layer
    assert "var(--continue-progress, 0%)" in final_layer
    assert "var(--detail-play-progress-rest-surface)" in final_layer
    assert "var(--detail-play-progress-rest-edge)" in final_layer
    assert "var(--detail-play-progress-hover-surface)" in final_layer
    assert "var(--detail-play-progress-hover-edge)" in final_layer
    assert "width 260ms var(--ease-out)" in final_layer
    assert "height: 5px" not in final_layer
    assert "height: 6px" not in final_layer
    percent_start = final_layer.index("\n.detail-continue-percent {\n")
    percent_end = final_layer.index("}\n", percent_start)
    percent_rules = final_layer[percent_start:percent_end]
    assert "opacity: 0" in percent_rules
    assert "font-variant-numeric: tabular-nums" in percent_rules
    assert "font-size: 12px" in percent_rules
    assert "color: var(--detail-play-progress-fill)" in percent_rules
    assert "background: var(--detail-play-progress-label-surface)" in percent_rules
    assert ".detail-continue-btn:is(:hover, :focus-visible) .detail-continue-percent" in final_layer
    assert "background: var(--detail-play-progress-label-surface)" in final_layer
    assert "opacity: 1" in final_layer[hover_rule_start:]
    assert "translateY(-2px)" in final_layer


def test_continue_episode_keeps_completed_history_context():
    """历史集已完成时，详情页主按钮保留该集上下文，不得回退到全季第一集未观看。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    start = page.index("function resolveContinueEpisode(")
    end = page.index("\n}\n", start)
    body = page[start:end]

    # 历史集存在时无条件作为当前显示上下文（已完成也保留），不再寻找全季第一集未观看。
    assert "if (historyEpisode) return historyEpisode;" in body
    assert "watchedEpisodeIds.has(episode.episode_id)) || historyEpisode" not in body


def test_handle_play_empty_target_does_not_reset_to_first_episode():
    """空目标或刷新空档时主按钮不得静默回退到第 1 集，保留已有上下文。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    start = page.index("const handlePlay = async (episodeId?: string) => {")
    end = page.index("};", start)
    body = page[start:end]

    assert "episodeId || continueTarget?.episode_id || ''" in body
    assert "episodes[0]?.episode_id" not in body


def test_refresh_keeps_latest_history_episode_as_continue_target():
    """刷新中间态的 continueEpisodeId 来自最新历史集，用户播放/选择的集不被覆盖回第一集。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    start = page.index("const refreshPlaybackSnapshot = async () => {")
    end = page.index("return next;\n  };", start)
    body = page[start:end]

    assert "next.continueEpisodeId = payload.items?.[0]?.episode_id || '';" in body
    assert "next.continueEpisodeId = pendingIntent.episodeId;" in body
