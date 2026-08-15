"""Windows Credential Manager adapter for KumiPlayer API credentials."""

from __future__ import annotations

import os
from typing import Any


class CredentialStoreError(RuntimeError):
    pass


class WindowsCredentialStore:
    """Store opaque UTF-8 secrets as Windows generic credentials."""

    _PREFIX = "KumiPlayer"
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self._ctypes: Any = None
        self._credential_type: Any = None
        self._credential_pointer_type: Any = None
        self._cred_read: Any = None
        self._cred_write: Any = None
        self._cred_delete: Any = None
        self._cred_free: Any = None
        if self.available:
            self._initialize_windows_api()

    def _initialize_windows_api(self) -> None:
        import ctypes
        from ctypes import wintypes

        byte_pointer = ctypes.POINTER(wintypes.BYTE)

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", byte_pointer),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", wintypes.LPVOID),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        credential_pointer = ctypes.POINTER(Credential)
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_read = api.CredReadW
        cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(credential_pointer)]
        cred_read.restype = wintypes.BOOL
        cred_write = api.CredWriteW
        cred_write.argtypes = [credential_pointer, wintypes.DWORD]
        cred_write.restype = wintypes.BOOL
        cred_delete = api.CredDeleteW
        cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        cred_delete.restype = wintypes.BOOL
        cred_free = api.CredFree
        cred_free.argtypes = [wintypes.LPVOID]

        self._ctypes = ctypes
        self._credential_type = Credential
        self._credential_pointer_type = credential_pointer
        self._cred_read = cred_read
        self._cred_write = cred_write
        self._cred_delete = cred_delete
        self._cred_free = cred_free

    def _target(self, name: str) -> str:
        return f"{self._PREFIX}:{name}"

    def read(self, name: str) -> str:
        if not self.available:
            return ""
        credential_pointer = self._credential_pointer_type()
        if not self._cred_read(
            self._target(name),
            self._CRED_TYPE_GENERIC,
            0,
            self._ctypes.byref(credential_pointer),
        ):
            error = self._ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return ""
            raise CredentialStoreError(f"Windows Credential Manager 读取失败：{error}")
        try:
            credential = credential_pointer.contents
            if not credential.CredentialBlobSize:
                return ""
            blob = self._ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return blob.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise CredentialStoreError("Windows Credential Manager 中的凭据格式无效") from error
        finally:
            self._cred_free(credential_pointer)

    def read_state(self, name: str) -> str:
        """凭据三态：``found`` / ``not_found`` / ``unavailable``。

        与 ``read()`` 的区别：存储本身的故障（CredentialStoreError）不吞成
        “不存在”——调用方（如 Bangumi session 层）必须能区分
        「凭据未保存」与「凭据存储暂时不可读」，两者绝不能都表现为退出登录。
        """
        try:
            value = self.read(name)
        except CredentialStoreError:
            return "unavailable"
        return "found" if value else "not_found"
    def write(self, name: str, value: str) -> None:
        if not self.available:
            raise CredentialStoreError("当前系统不支持 Windows Credential Manager")
        blob = value.encode("utf-8")
        if len(blob) > 5_120:
            raise CredentialStoreError("凭据超过 Windows Credential Manager 的 5 KB 限制")
        blob_buffer = (self._ctypes.c_ubyte * max(1, len(blob)))()
        if blob:
            blob_buffer[: len(blob)] = blob
        credential = self._credential_type()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = self._target(name)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = self._ctypes.cast(blob_buffer, self._ctypes.POINTER(self._ctypes.c_ubyte))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "KumiPlayer"
        if not self._cred_write(self._ctypes.byref(credential), 0):
            error = self._ctypes.get_last_error()
            raise CredentialStoreError(f"Windows Credential Manager 写入失败：{error}")

    def delete(self, name: str) -> None:
        if not self.available:
            return
        if self._cred_delete(self._target(name), self._CRED_TYPE_GENERIC, 0):
            return
        error = self._ctypes.get_last_error()
        if error != self._ERROR_NOT_FOUND:
            raise CredentialStoreError(f"Windows Credential Manager 删除失败：{error}")


SECURE_CREDENTIAL_STORE = WindowsCredentialStore()
