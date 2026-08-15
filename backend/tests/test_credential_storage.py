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
    """REWORK P0-2：空值保存 ≠ 删除（KEEP）；只有显式 cleared_keys 才 CLEAR。"""
    store, _ = enable_fake_store(monkeypatch, tmp_path)
    save_config(AppConfig(bangumi_access_token="saved-token"))
    # 空值未显式清除 → KEEP（凭据读取失败/未加载绝不能触发删除）
    save_config(AppConfig(bangumi_access_token=""))
    assert store.values["bangumi_access_token"] == "saved-token"
    # 用户显式清除（退出）→ CLEAR
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
