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
    """只有显式 cleared_keys 才删除凭据（REWORK：空值默认 KEEP）。"""
    store, _ = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(bangumi_access_token="saved-token"))
    # 无 cleared_keys：空值 = KEEP，不删除
    save_config(AppConfig(bangumi_access_token=""))
    assert store.values.get("bangumi_access_token") == "saved-token"
    # 显式 cleared_keys：才执行 DELETE
    save_config(AppConfig(bangumi_access_token=""), cleared_keys={"bangumi_access_token"})
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

# ============================================================
# REWORK：Credential Store 恢复后的 KEEP 语义与安全存储隔离
# ============================================================


class PartialFailStore(FakeCredentialStore):
    """hydrate 中途对指定字段 read 失败一次，之后恢复（模拟 transient failure）。"""

    def __init__(self, fail_key: str, seed: dict | None = None):
        super().__init__()
        self.fail_key = fail_key
        self.failed = False
        if seed:
            self.values.update(seed)

    def read(self, name: str) -> str:
        if name == self.fail_key and not self.failed:
            self.failed = True
            from app.core.credential_store import CredentialStoreError

            raise CredentialStoreError("temporary read failure")
        return super().read(name)


def _seed_all_credentials(store) -> None:
    store.values.update({
        "tmdb_bearer_token": "tmdb-secret",
        "deepseek_api_key": "deepseek-secret",
        "bangumi_access_token": "bangumi-secret",
        "openlist_username": "ol-user",
        "openlist_password": "ol-pass",
    })


def test_rework_stale_cache_after_recovery_keeps_all_credentials(monkeypatch, tmp_path):
    """事故链：hydrate 中途 read failure → stale blank cache → CM 恢复 →
    OpenList local-only 保存（空凭据）→ 所有 secure credential 必须保持不变。

    关键：保存走的是跟踪 store（记录全部 write/delete），断言：
    - 保存动作对凭据**零写入、零删除**（空值 = KEEP）；
    - 保存后所有凭据值（在保存 store 与 seed store 中）完全不变。
    """
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    _seed_all_credentials(store)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass",
                          tmdb_bearer_token="tmdb-secret",
                          deepseek_api_key="deepseek-secret",
                          bangumi_access_token="bangumi-secret"))

    # 保存路径使用跟踪 store（记录 write/delete 调用；seed 真实值）
    writes: list[str] = []
    deletes: list[str] = []

    class TrackingStore(FakeCredentialStore):
        def write(self, name: str, value: str) -> None:
            writes.append(name)
            super().write(name, value)

        def delete(self, name: str) -> None:
            deletes.append(name)
            super().delete(name)

    tracking = TrackingStore()
    tracking.values.update(store.values)
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", tracking)
    invalidate_config_cache()

    # 模拟 hydrate 中途 read failure（bangumi_access_token 读取失败一次）
    # → 缓存的 config 中该字段留空（stale blank cache）
    partial = PartialFailStore("bangumi_access_token")
    partial.values.update(tracking.values)
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", partial)
    invalidate_config_cache()
    try:
        stale_cfg = load_config(force_reload=True)  # hydrate 中途失败 → 部分字段留空
    except Exception:
        pass  # load_config 内部捕获 CredentialStoreError
    assert stale_cfg.bangumi_access_token == ""  # 确认 stale blank 已产生

    # Credential Store 恢复（用回 tracking store，全部真实值仍在）
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", tracking)
    # 用 stale 缓存对象（bangumi_access_token 为空）做 OpenList local-only 保存
    stale_cfg.openlist_cache_ttl_minutes = 720
    save_config(stale_cfg)

    # 关键断言：保存动作对凭据零写入、零删除（空值 = KEEP，绝不隐式 CLEAR）
    assert writes == []
    assert deletes == []
    # 保存 store 中所有凭据完全不变
    assert tracking.values.get("openlist_username") == "ol-user"
    assert tracking.values.get("openlist_password") == "ol-pass"
    assert tracking.values.get("tmdb_bearer_token") == "tmdb-secret"
    assert tracking.values.get("deepseek_api_key") == "deepseek-secret"
    assert tracking.values.get("bangumi_access_token") == "bangumi-secret"
    # seed store 同样不变（原始凭据未被删除）
    assert store.values.get("bangumi_access_token") == "bangumi-secret"

def test_rework_resolver_reads_recovered_store_when_cache_blank(monkeypatch, tmp_path):
    """CM hydrate 失败 → 恢复 → cached 为空但 resolver 必须回源读到真实凭据。

    关键：调用 resolver 前**不重新 hydrate**——直接保留 stale blank 缓存，
    让 resolver 真正走「cached 空 + store 已恢复 → 回源读取」分支。
    """
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    _seed_all_credentials(store)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass"))

    # 模拟 hydrate 失败：load 时 store read 失败 → cache 中凭据为空
    partial = PartialFailStore("openlist_username", seed=dict(store.values))
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", partial)
    invalidate_config_cache()
    try:
        load_config(force_reload=True)
    except Exception:
        pass
    # cache 现在是 stale blank（openlist_username 未 hydrate）
    cached = load_config()
    assert cached.openlist_username == ""

    # Credential Store 恢复（partial 已自愈）——注意：**不 invalidate、不重新
    # hydrate**，缓存仍是 stale blank；resolver 必须直接回源读到真实凭据
    username, password, state = config_module.resolve_openlist_credentials()
    assert state == "found"
    assert username == "ol-user"
    assert password == "ol-pass"

def test_rework_resolver_unavailable_never_mutates(monkeypatch, tmp_path):
    """resolver read failure → unavailable，绝不猜 missing、绝不 mutation。"""
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    _seed_all_credentials(store)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass"))

    class AlwaysFailStore(FakeCredentialStore):
        def read(self, name: str) -> str:
            from app.core.credential_store import CredentialStoreError

            raise CredentialStoreError("unavailable")

    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", AlwaysFailStore())
    invalidate_config_cache()
    try:
        load_config(force_reload=True)
    except Exception:
        pass

    username, password, state = config_module.resolve_openlist_credentials()
    assert state == "unavailable"
    assert username == "" and password == ""
    # 存储未被写入或删除任何值
    assert store.values.get("openlist_username") == "ol-user"
    assert store.values.get("openlist_password") == "ol-pass"
    assert store.values.get("tmdb_bearer_token") == "tmdb-secret"


def test_rework_openlist_save_isolated_to_openlist_credentials(monkeypatch, tmp_path):
    """显式更新 OpenList password → 只允许 OpenList secure credential SET，
    其他安全凭据字段不 write、不 delete、值完全不变。"""
    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    _seed_all_credentials(store)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass",
                          tmdb_bearer_token="tmdb-secret",
                          deepseek_api_key="deepseek-secret",
                          bangumi_access_token="bangumi-secret"))

    writes: list[str] = []
    deletes: list[str] = []

    class TrackingStore(FakeCredentialStore):
        def write(self, name: str, value: str) -> None:
            writes.append(name)
            super().write(name, value)

        def delete(self, name: str) -> None:
            deletes.append(name)
            super().delete(name)

    tracking = TrackingStore()
    tracking.values.update(store.values)
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", tracking)
    invalidate_config_cache()
    cfg = load_config(force_reload=True)
    cfg.openlist_password = "new-pass"
    save_config(cfg)

    # OpenList password 被 SET，其余字段零写入、零删除
    assert tracking.values.get("openlist_password") == "new-pass"
    assert writes == ["openlist_password"]
    assert deletes == []
    assert tracking.values.get("tmdb_bearer_token") == "tmdb-secret"
    assert tracking.values.get("deepseek_api_key") == "deepseek-secret"
    assert tracking.values.get("bangumi_access_token") == "bangumi-secret"
    assert tracking.values.get("openlist_username") == "ol-user"


def test_rework_clear_token_isolated_to_target_credential(monkeypatch, tmp_path):
    """端点级：DELETE /token（cleared_keys）只删除 bangumi_access_token，
    其他 secure credential 不 write、不 delete、值完全不变（跨字段隔离）。"""

    store, config_file = enable_fake_store(monkeypatch, tmp_path)
    _seed_all_credentials(store)
    save_config(AppConfig(openlist_username="ol-user", openlist_password="ol-pass",
                          tmdb_bearer_token="tmdb-secret",
                          deepseek_api_key="deepseek-secret",
                          bangumi_access_token="bangumi-secret"))

    writes: list[str] = []
    deletes: list[str] = []

    class TrackingStore(FakeCredentialStore):
        def write(self, name: str, value: str) -> None:
            writes.append(name)
            super().write(name, value)

        def delete(self, name: str) -> None:
            deletes.append(name)
            super().delete(name)

    tracking = TrackingStore()
    tracking.values.update(store.values)
    monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", tracking)
    invalidate_config_cache()

    # 直接调用 save_config 的 cleared_keys 路径（等价于 DELETE /token 端点）
    current = load_config(force_reload=True)
    current.bangumi_access_token = ""
    save_config(current, cleared_keys={"bangumi_access_token"})

    # 只有 bangumi_access_token 被删除；其余字段零写入零删除
    assert deletes == ["bangumi_access_token"]
    assert writes == []
    assert "bangumi_access_token" not in tracking.values
    assert tracking.values.get("tmdb_bearer_token") == "tmdb-secret"
    assert tracking.values.get("deepseek_api_key") == "deepseek-secret"
    assert tracking.values.get("openlist_username") == "ol-user"
    assert tracking.values.get("openlist_password") == "ol-pass"

