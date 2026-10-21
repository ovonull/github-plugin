"""
插件本地状态：订阅列表、每个仓库的设置、轮询游标与运行记录。

约定：
- 状态文件放在宿主的数据目录（data/plugins/{插件标识}/）下，不放在插件包目录里。
  插件升级时宿主会删掉旧版本的包目录，放在包里的文件会跟着一起没；
- 写入用「先写 .tmp 再改名」的原子方式，中途断电不会留下半截 JSON；
- 读到损坏内容逐项校验、丢弃无效项，不能因此起不来；
- 时间按东八区算，不依赖容器时区（容器里常是 UTC）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import quote


PLUGIN_ID = "cn.ucbk.github"

# 状态文件版本。结构变了就往上加，读取时按版本分支处理。
STATE_VERSION = 2

# 每个仓库可单独设置的项。键与面板上填的一致，取值是字符串。
REPO_OPTION_KEYS = {
    "label": "显示名",
    "mask": "私库打码",
    "star": "监控 Star 变化",
    "notify": "推送通知",
    "limit": "每类条数上限",
    "cred": "使用的凭据",
}

# 布尔型设置项里，「开」和「关」都认。
_TRUE_WORDS = {"1", "on", "yes", "true", "开", "启用", "是"}
_FALSE_WORDS = {"0", "off", "no", "false", "关", "关闭", "否"}


def state_dir() -> str:
    """
    算出插件自己的数据目录。

    宿主把插件数据放在 data/plugins/ 下（环境变量 PLUGIN_DATA_DIR 可改），
    子进程的工作目录就是后端根目录，所以相对路径直接可用。
    目录建不出来时退回插件包目录——功能会受影响，但不会起不来。
    """
    base = os.environ.get("PLUGIN_DATA_DIR", "").strip() or os.path.join("data", "plugins")
    target = os.path.join(base, PLUGIN_ID)
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return target
    except OSError:
        return os.path.dirname(os.path.abspath(__file__))


def _now() -> str:
    """当前时间（东八区），格式 2026-10-21 15:04:05。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "repos": {}, "log": []}


def _default_entry(owner: str, repo: str) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "targets": [],
        "options": {},
        "marks": {},
        "failures": {},
    }


def parse_bool(value: Any, default: bool = False) -> bool:
    """把面板或指令里的值读成布尔。"""
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


class Store:
    """订阅与设置的状态存储。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(state_dir(), "state.json")
        self._cache: dict[str, Any] | None = None

    # ---------- 读写 ----------

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        data = _default_state()
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = self._migrate(loaded)
            except (ValueError, OSError):
                data = _default_state()
        self._cache = data
        return data

    def _migrate(self, loaded: dict[str, Any]) -> dict[str, Any]:
        """把旧结构（平铺的仓库条目）并入新结构。"""
        repos = loaded.get("repos")
        if isinstance(repos, dict):
            data = _default_state()
            data["version"] = STATE_VERSION
            for key, entry in repos.items():
                if not isinstance(entry, dict):
                    continue
                owner = str(entry.get("owner") or key.split("/")[0] or "")
                repo = str(entry.get("repo") or key.split("/")[-1] or "")
                if not owner or not repo:
                    continue
                fresh = _default_entry(owner, repo)
                fresh["targets"] = [t for t in (entry.get("targets") or []) if isinstance(t, dict)]
                fresh["options"] = {
                    str(k): str(v)
                    for k, v in (entry.get("options") or {}).items()
                    if isinstance(entry.get("options"), dict)
                }
                fresh["marks"] = {
                    "release": str((entry.get("marks") or {}).get("release") or entry.get("last_release") or ""),
                    "commit": str((entry.get("marks") or {}).get("commit") or entry.get("last_commit") or ""),
                    "issue": int((entry.get("marks") or {}).get("issue") or entry.get("last_issue") or 0),
                    "star": str((entry.get("marks") or {}).get("star") or ""),
                }
                data["repos"][fresh["owner"] + "/" + fresh["repo"].lower()] = fresh
            log = loaded.get("log")
            if isinstance(log, list):
                data["log"] = [item for item in log if isinstance(item, dict)][-200:]
            return data
        return _default_state()

    def _save(self) -> None:
        data = self._cache or _default_state()
        tmp = f"{self.path}.tmp"
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def key(owner: str, repo: str) -> str:
        return f"{owner}/{repo}".lower()

    # ---------- 订阅 ----------

    def subscribe(self, owner: str, repo: str, target: dict[str, Any]) -> bool:
        """添加一个订阅目标；返回这个仓库是不是新加的。"""
        data = self._load()
        key = self.key(owner, repo)
        entry = data["repos"].get(key)
        is_new = entry is None
        if entry is None:
            entry = _default_entry(owner, repo)
            data["repos"][key] = entry
        for item in entry["targets"]:
            if item.get("type") == target.get("type") and str(item.get("id")) == str(
                target.get("id")
            ):
                return is_new
        entry["targets"].append({"type": target.get("type"), "id": target.get("id")})
        self._save()
        return is_new

    def unsubscribe(self, owner: str, repo: str, target: dict[str, Any]) -> bool:
        """移除一个订阅目标；目标清空后删掉整个仓库订阅。返回是否还存在该仓库。"""
        data = self._load()
        key = self.key(owner, repo)
        entry = data["repos"].get(key)
        if not entry:
            return False
        entry["targets"] = [
            item
            for item in entry["targets"]
            if not (
                item.get("type") == target.get("type")
                and str(item.get("id")) == str(target.get("id"))
            )
        ]
        if not entry["targets"]:
            del data["repos"][key]
        self._save()
        return key in data["repos"]

    def list_for(self, target: dict[str, Any]) -> list[dict[str, Any]]:
        """列出某个会话（群 / 私聊）订阅的仓库。"""
        data = self._load()
        out = []
        for entry in data["repos"].values():
            if any(
                item.get("type") == target.get("type")
                and str(item.get("id")) == str(target.get("id"))
                for item in entry["targets"]
            ):
                out.append(entry)
        return out

    def all(self) -> list[dict[str, Any]]:
        """列出全部订阅（轮询用）。"""
        return list(self._load()["repos"].values())

    def get(self, owner: str, repo: str) -> dict[str, Any] | None:
        """取一个仓库的条目。"""
        return self._load()["repos"].get(self.key(owner, repo))

    # ---------- 每仓库设置 ----------

    def option(self, owner: str, repo: str, key: str, default: str = "") -> str:
        """读一个仓库级设置。"""
        entry = self.get(owner, repo)
        if not entry:
            return default
        return str((entry.get("options") or {}).get(key) or default)
    def option_bool(self, owner: str, repo: str, key: str, default: bool = False) -> bool:
        """读一个仓库级布尔设置。"""
        value = self.option(owner, repo, key, "")
        if not value:
            return default
        return parse_bool(value, default)

    def set_option(self, owner: str, repo: str, key: str, value: str) -> bool:
        """写一个仓库级设置。返回这个仓库有没有被订阅。"""
        data = self._load()
        entry = data["repos"].get(self.key(owner, repo))
        if not entry:
            return False
        options = entry.setdefault("options", {})
        options[key] = str(value)
        self._save()
        return True

    # ---------- 轮询游标 ----------

    def marks(self, owner: str, repo: str) -> dict[str, Any]:
        """取轮询游标。"""
        entry = self.get(owner, repo)
        if not entry:
            return {}
        return dict(entry.get("marks") or {})

    def update_marks(self, owner: str, repo: str, **marks: Any) -> None:
        """更新轮询游标。"""
        data = self._load()
        entry = data["repos"].get(self.key(owner, repo))
        if not entry:
            return
        current = entry.setdefault("marks", {})
        for key, value in marks.items():
            if value in (None, "", 0):
                continue
            current[key] = value
        self._save()

    # ---------- 失败与恢复 ----------

    def note_failure(self, owner: str, repo: str, message: str, threshold: int) -> bool:
        """
        记一次检查失败，判断这次要不要在群里说一声。

        连续失败到阈值才报一次，一轮故障只报一次；恢复后清零，
        下次再坏会重新计。阈值不大于 0 表示不报。
        """
        data = self._load()
        entry = data["repos"].get(self.key(owner, repo))
        if not entry:
            return False
        state = entry.setdefault("failures", {})
        state["count"] = int(state.get("count") or 0) + 1
        state["last_error"] = str(message or "")
        state["last_at"] = _now()
        alerted = bool(state.get("alerted"))
        if threshold <= 0 or state["count"] < threshold or alerted:
            self._save()
            return False
        state["alerted"] = True
        self._save()
        return True

    def clear_failure(self, owner: str, repo: str) -> bool:
        """检查成功时清掉失败状态；返回这次是不是刚从故障里恢复。"""
        data = self._load()
        entry = data["repos"].get(self.key(owner, repo))
        if not entry:
            return False
        state = entry.setdefault("failures", {})
        was_alerted = bool(state.get("alerted"))
        state["count"] = 0
        state["last_error"] = ""
        state["alerted"] = False
        self._save()
        return was_alerted

    # ---------- 运行记录 ----------

    def add_log(self, kind: str, repo: str, text: str) -> None:
        """追加一条运行记录。"""
        data = self._load()
        records = data.setdefault("log", [])
        records.append(
            {"time": _now(), "kind": str(kind or ""), "repo": str(repo or ""), "text": str(text or "")}
        )
        if len(records) > 200:
            del records[:-200]
        self._save()

    def logs(self, limit: int = 20) -> list[dict[str, Any]]:
        """取最近的运行记录（最新的在前）。"""
        records = list(self._load().get("log") or [])
        records.reverse()
        return records[: max(1, int(limit or 20))]
