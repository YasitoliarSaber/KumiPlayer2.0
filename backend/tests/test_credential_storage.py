import json

from app.core import config as config_module
from app.core.config import AppConfig, invalidate_config_cache, load_config, save_config


class FakeCredentialStore:
    available = True

    def __init__(self):
        self.values: dict[str, str] = {}

    def read(self, name: str) -> str:
        return self.values.get(name, "")

    def write(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def enable_fake_store(monkeypatch, tmp_path):
    store = FakeCredentialStore()
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", store)
    monkeypatch.setattr(config_module, "_credential_storage_enabled", lambda: True)
    invalidate_config_cache()
    return store, config_file


def test_sensitive_config_is_stored_outside_json_and_restored(monkeypatch, tmp_path):
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(
        tmdb_bearer_token="tmdb-secret-value",
        bangumi_access_token="bangumi-secret-value",
    ))

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["tmdb_bearer_token"] == ""
    assert saved["bangumi_access_token"] == ""
    assert store.values["tmdb_bearer_token"] == "tmdb-secret-value"

    invalidate_config_cache()
    loaded = load_config(force_reload=True)
    assert loaded.tmdb_bearer_token == "tmdb-secret-value"
    assert loaded.bangumi_access_token == "bangumi-secret-value"


def test_legacy_plaintext_credentials_are_migrated_on_read(monkeypatch, tmp_path):
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"setup_completed": True, "tmdb_bearer_token": "legacy-secret"}),
        encoding="utf-8",
    )

    loaded = load_config(force_reload=True)

    assert loaded.tmdb_bearer_token == "legacy-secret"
    assert store.values["tmdb_bearer_token"] == "legacy-secret"
    assert json.loads(config_file.read_text(encoding="utf-8"))["tmdb_bearer_token"] == ""


def test_clearing_credential_removes_secure_copy(monkeypatch, tmp_path):
    store, _ = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(bangumi_access_token="saved-token"))
    save_config(AppConfig(bangumi_access_token=""))

    assert "bangumi_access_token" not in store.values


def test_credential_manager_failure_keeps_legacy_value_available(monkeypatch, tmp_path):
    _, config_file = enable_fake_store(monkeypatch, tmp_path)

    class FailingStore(FakeCredentialStore):
        def write(self, name: str, value: str) -> None:
            from app.core.credential_store import CredentialStoreError

            raise CredentialStoreError("temporary failure")

    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", FailingStore())
    config_file.write_text(
        json.dumps({"setup_completed": True, "tmdb_bearer_token": "legacy-secret"}),
        encoding="utf-8",
    )

    loaded = load_config(force_reload=True)

    assert loaded.tmdb_bearer_token == "legacy-secret"
    assert json.loads(config_file.read_text(encoding="utf-8"))["tmdb_bearer_token"] == "legacy-secret"


# ============================================================
# OL-3：Validated Candidate + Secure Credential Atomic Commit 回归
# ============================================================


class FailingWriteStore(FakeCredentialStore):
    def __init__(self, fail_key: str = "", seed: dict | None = None):
        super().__init__()
        self.fail_key = fail_key
        if seed:
            self.values.update(seed)
    def write(self, name: str, value: str) -> None:
        if self.fail_key and name == self.fail_key:
            from app.core.credential_store import CredentialStoreError

            raise CredentialStoreError("temporary write failure")
        super().write(name, value)



class FailingReadStore(FakeCredentialStore):
    def read(self, name: str) -> str:
        from app.core.credential_store import CredentialStoreError

        raise CredentialStoreError("temporary read failure")


def test_ol3_credential_read_failure_does_not_delete_on_unrelated_save(monkeypatch, tmp_path):
    """ROOT-7：凭据 read 失败时保存无关配置 → 保存中止，原 OpenList 凭据必须仍存在。

    旧实现存在危险路径：read 失败 → 字段未 hydrate → 通用持久化把空值解释成
    delete。现在 read 失败必须直接中止保存（0 mutation），不得把「存储暂时
    不可读」当作「清除凭据」，也不得把明文凭据写进 config.json。
    """
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass"))

    # 凭据管理器暂时不可读
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", FailingReadStore())
    invalidate_config_cache()
    cfg = load_config(force_reload=True)
    cfg.openlist_cache_ttl_minutes = 720
    try:
        save_config(cfg)
    except Exception:
        pass  # 预期抛出 CredentialStoreError（保存中止）

    # 原 OpenList 凭据必须仍存在（不可把 read 失败解释成 delete）
    real_store = store
    assert real_store.values.get("openlist_username") == "ol-user"
    assert real_store.values.get("openlist_password") == "ol-pass"
    # config.json 不得写入任何明文凭据（payload 未到达写盘阶段）
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved.get("openlist_username", "") == ""
    assert saved.get("openlist_password", "") == ""

def test_ol3_credential_write_failure_preserves_previous_values(monkeypatch, tmp_path):
    """凭据 write 失败 → 已写入的字段回滚，配置不被破坏（0 mutation）。

    同时变更 username 与 password（username 先写成功、password 写失败），
    验证 rollback 真实生效：username 必须回滚为旧值，password 保持旧值。
    """
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass"))

    failing = FailingWriteStore(fail_key="openlist_password", seed=dict(store.values))
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", failing)
    invalidate_config_cache()
    cfg = load_config(force_reload=True)
    cfg.openlist_username = "new-user"   # 先于 password 写入 → 需要被回滚
    cfg.openlist_password = "new-pass"   # 写入失败
    try:
        save_config(cfg)
    except Exception:
        pass  # 预期抛出 CredentialStoreError

    # 之前已写入的 username 应回滚为旧值，password 保持旧值
    assert failing.values.get("openlist_username") == "ol-user"
    assert failing.values.get("openlist_password") == "ol-pass"
    # 配置 JSON 未被写坏（原子写未执行或失败）
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved.get("openlist_password", "") == ""
    assert saved.get("openlist_username", "") == ""


def test_ol3_json_write_failure_rolls_back_credentials(monkeypatch, tmp_path):
    """JSON 写失败 → 凭据恢复旧值（preflight + compensation rollback）。"""
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass"))


    def failing_write(path, payload):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(config_module, "write_json_atomic", failing_write)
    invalidate_config_cache()
    cfg = load_config(force_reload=True)
    cfg.openlist_password = "new-pass"
    try:
        save_config(cfg)
    except OSError:
        pass  # 预期抛出

    # 凭据必须恢复旧值
    assert store.values.get("openlist_username") == "ol-user"
    assert store.values.get("openlist_password") == "ol-pass"
