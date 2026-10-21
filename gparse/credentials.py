"""
GitHub 凭据管理：多个账号、默认凭据、每个仓库单独绑定。

对应 SuInk/Diana 的「GitHub 凭据」那块：每条凭据有自己的名字和 Token，
仓库可以在「仓库管理」里各自选用；没指定的仓库用默认凭据。

凭据放在宿主给插件的数据目录下，不写进代码、不进日志；
面板上的 GitHub Token 字段作为默认凭据，与这里的凭据列表合并使用。
"""

from __future__ import annotations

import json
import os
from typing import Any

from gparse import store as store_mod


CRED_FILE = "credentials.json"


def _path() -> str:
    return os.path.join(store_mod.state_dir(), CRED_FILE)


def _load() -> dict[str, Any]:
    """读出凭据表。格式：{"default": "<名称>", "items": {"<名称>": "<token>"}}。"""
    path = _path()
    data: dict[str, Any] = {"default": "", "items": {}}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (ValueError, OSError):
        return data
    if not isinstance(loaded, dict):
        return data
    items = loaded.get("items")
    if isinstance(items, dict):
        data["items"] = {
            str(name): str(token)
            for name, token in items.items()
            if str(name).strip() and str(token).strip()
        }
    data["default"] = str(loaded.get("default") or "")
    return data


def _save(data: dict[str, Any]) -> None:
    path = _path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def names() -> list[str]:
    """列出所有凭据的名字。"""
    return list(_load()["items"].keys())


def get(name: str) -> str:
    """按名字取 Token；取不到返回空串。"""
    if not name:
        return ""
    return str(_load()["items"].get(str(name).strip()) or "")


def put(name: str, token: str) -> bool:
    """新增或覆盖一条凭据。返回是否成功。"""
    key = str(name or "").strip()
    value = str(token or "").strip()
    if not key or not value:
        return False
    data = _load()
    data["items"][key] = value
    if not data.get("default"):
        data["default"] = key
    _save(data)
    return True


def remove(name: str) -> bool:
    """删掉一条凭据。返回是否真的删掉了。"""
    key = str(name or "").strip()
    data = _load()
    if key not in data["items"]:
        return False
    del data["items"][key]
    if data.get("default") == key:
        data["default"] = next(iter(data["items"]), "")
    _save(data)
    return True


def default_name() -> str:
    """默认凭据的名字。"""
    return str(_load().get("default") or "")


def set_default(name: str) -> bool:
    """把某条凭据设为默认。"""
    key = str(name or "").strip()
    data = _load()
    if key not in data["items"]:
        return False
    data["default"] = key
    _save(data)
    return True


def resolve(repo_credential: str, panel_token: str) -> str:
    """
    算出这个仓库该用哪个 Token。

    优先用仓库自己绑定的凭据，没绑定就用默认凭据，
    默认凭据也没有时退回面板上填的那个 GitHub Token。
    """
    if repo_credential:
        token = get(repo_credential)
        if token:
            return token
    fallback = default_name()
    if fallback:
        token = get(fallback)
        if token:
            return token
    return str(panel_token or "").strip()
