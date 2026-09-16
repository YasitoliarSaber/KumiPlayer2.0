"""O6：重复发布产物必须是增量的——只补缺失的图片产物。

`retry_artifacts` 的公开承诺是"只重新下载**缺失**的图片产物"，但发布路径原先无条件
重新下载全部剧照/海报/背景图并重写全部 NFO：24 集作品点一次重试 ≈ 27 个 HTTPS 请求
+ 25 次写盘，而且正好落进"下载阶段没有心跳"的失联回收窗口（120s 后被误判为失败）。
"""

from __future__ import annotations

from types import SimpleNamespace


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-retry",
        root_id="root-retry",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="movie",
        group_type="movie",
    )
    return evidence, facts


def _download_counter(monkeypatch, module) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_download(url: str, path, *, client):
        calls.append((url, str(path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image-bytes")
        return "digest-" + path.name

    monkeypatch.setattr(module, "_download_artwork", fake_download)
    return calls


def _artwork_config():
    return SimpleNamespace(artwork_storage_mode="local", tmdb_timeout=5, proxy_url="")


def test_materialize_skips_artwork_that_is_already_published(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata_artifacts as module

    calls = _download_counter(monkeypatch, module)
    work_dir = tmp_path / "mirror" / "work"
    target = {
        "work_type": "series",
        "episodes": [
            {"episode_id": "e1", "local_season_number": 1, "local_episode_number": 1},
            {"episode_id": "e2", "local_season_number": 1, "local_episode_number": 2},
        ],
    }
    metadata = {
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/w500/fanart.jpg",
    }
    episode_metadata = {
        "e1": {"still_url": "https://image.tmdb.org/t/p/w500/s1.jpg"},
        "e2": {"still_url": "https://image.tmdb.org/t/p/w500/s2.jpg"},
    }

    first: list = []
    module._materialize_local_artwork(
        config=_artwork_config(), work_dir=work_dir, target=target, metadata=dict(metadata),
        episode_metadata={key: dict(value) for key, value in episode_metadata.items()},
        artifacts=first,
    )
    assert len(calls) == 4, "首轮：2 张剧照 + 海报 + 背景图"
    assert len(first) == 4

    # 第二轮：产物已发布且文件非空 → 一次下载都不该发生，digest 复用既有值。
    calls.clear()
    published = {(artifact_type, str(path)): digest for artifact_type, path, digest in first}
    second: list = []
    module._materialize_local_artwork(
        config=_artwork_config(), work_dir=work_dir, target=target, metadata=dict(metadata),
        episode_metadata={key: dict(value) for key, value in episode_metadata.items()},
        artifacts=second,
        published=published,
    )

    assert calls == [], "已发布产物不得重新下载"
    assert [(t, str(p), d) for t, p, d in second] == [(t, str(p), d) for t, p, d in first]


def test_materialize_redownloads_only_the_artifact_whose_file_is_gone(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata_artifacts as module

    calls = _download_counter(monkeypatch, module)
    work_dir = tmp_path / "mirror" / "work"
    target = {"work_type": "movie", "episodes": []}
    metadata = {
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/w500/fanart.jpg",
    }

    first: list = []
    module._materialize_local_artwork(
        config=_artwork_config(), work_dir=work_dir, target=target, metadata=dict(metadata),
        episode_metadata={}, artifacts=first,
    )
    assert len(calls) == 2
    poster_path = next(path for artifact_type, path, _digest in first if artifact_type == "poster")
    poster_path.unlink()

    calls.clear()
    published = {(artifact_type, str(path)): digest for artifact_type, path, digest in first}
    second: list = []
    module._materialize_local_artwork(
        config=_artwork_config(), work_dir=work_dir, target=target, metadata=dict(metadata),
        episode_metadata={}, artifacts=second, published=published,
    )

    assert [url for url, _path in calls] == ["https://image.tmdb.org/t/p/w500/poster.jpg"]


def test_republish_does_not_rewrite_unchanged_nfo_or_redownload_artwork(tmp_path, monkeypatch):
    """走完整发布路径：第二次发布应零下载、零 NFO 重写。"""

    from app.media_v4.jobs import metadata_artifacts as module
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "retry.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p")])
    revisions.confirm("rev-1")
    with database.connect() as conn:
        work_id = str(conn.execute(
            "SELECT work_id FROM revision_bindings WHERE revision_id = 'rev-1' LIMIT 1"
        ).fetchone()["work_id"])

    calls = _download_counter(monkeypatch, module)
    writes: list[str] = []
    real_write = module._write_atomic

    def counting_write(path, payload):
        writes.append(str(path))
        return real_write(path, payload)

    monkeypatch.setattr(module, "_write_atomic", counting_write)
    monkeypatch.setattr(module, "load_config", _artwork_config)

    target = {"work_type": "movie", "title": "Show", "year": 2024, "episodes": []}
    metadata = {
        "provider": "tmdb",
        "provider_id": "1",
        "title": "Show",
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/w500/fanart.jpg",
        "clearlogo_url": "https://image.tmdb.org/t/p/w500/logo.png",
    }
    mirror_root = tmp_path / "mirror"

    module.publish_metadata_artifacts(
        database, revision_id="rev-1", work_id=work_id, target=target,
        metadata=dict(metadata), mirror_root=mirror_root,
    )
    assert len(calls) == 3, "首轮：海报 + 背景图 + clearlogo"
    assert writes, "首轮必须写出 NFO"

    calls.clear()
    writes.clear()
    module.publish_metadata_artifacts(
        database, revision_id="rev-1", work_id=work_id, target=target,
        metadata=dict(metadata), mirror_root=mirror_root,
    )

    assert calls == [], "重复发布不得重新下载任何图片"
    assert writes == [], "内容未变的 NFO 不得重写"
