"""
GitHub API 调用与订阅存储。

设计说明：
- 只读查询（仓库 / Issue / PR / Release / Commit）都走 host.http_request；
- 订阅用本地 JSON 文件持久化（原子写：先写 .tmp 再改名），不依赖宿主存储；
- 轮询比较「上次见到的 tag / commit sha / issue 号」，有变化才推送。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from gparse import common
from gparse.common import GitHubError


async def _get(host: Any, path: str, token: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """对 GitHub API 发一次 GET，返回解析后的响应包。"""
    url = f"{common.API_BASE}{path}"
    return await host.http_request(
        "GET", url, headers=common.api_headers(token), timeout=timeout
    )


async def fetch_repo(host: Any, owner: str, repo: str, token: str) -> dict[str, Any]:
    """取仓库基本信息，组装成描述卡片用的字典。"""
    res = await _get(host, f"/repos/{owner}/{repo}", token)
    status = common.status_of(res)
    if status == 404:
        raise GitHubError("仓库不存在或无权限访问", "确认 owner/repo 拼写，私有仓库需要有效 Token")
    if status != 200:
        raise GitHubError(f"GitHub 返回 {status}", "可能是触发了匿名限流，配置 GitHub Token 后重试")
    data = common.json_of(res)
    return {
        "kind": "repo",
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "description": data.get("description") or "",
        "language": data.get("language") or "",
        "stars": data.get("stargazers_count") or 0,
        "forks": data.get("forks_count") or 0,
        "open_issues": data.get("open_issues_count") or 0,
        "archived": bool(data.get("archived")),
        "pushed_at": data.get("pushed_at") or "",
        "html_url": data.get("html_url") or "",
    }


async def fetch_issue(
    host: Any, owner: str, repo: str, number: int, token: str
) -> dict[str, Any]:
    """取 Issue 或 PR 详情（PR 接口会带 pull_request 字段）。"""
    res = await _get(host, f"/repos/{owner}/{repo}/issues/{number}", token)
    status = common.status_of(res)
    if status != 200:
        raise GitHubError(f"没有找到 #{number}", "确认编号是否存在，私有仓库需要 Token")
    data = common.json_of(res)
    is_pr = bool(data.get("pull_request"))
    return {
        "kind": "pr" if is_pr else "issue",
        "number": number,
        "title": data.get("title") or "",
        "state": data.get("state") or "",
        "user": (data.get("user") or {}).get("login") or "",
        "body": data.get("body") or "",
        "html_url": data.get("html_url") or "",
    }


async def fetch_latest_release(
    host: Any, owner: str, repo: str, token: str
) -> dict[str, Any] | None:
    """取最新 Release（无 Release 时返回 None，不报错）。"""
    res = await _get(host, f"/repos/{owner}/{repo}/releases/latest", token)
    if common.status_of(res) in (404, 204):
        return None
    data = common.json_of(res)
    if not data:
        return None
    return {
        "tag": data.get("tag_name") or "",
        "name": data.get("name") or data.get("tag_name") or "",
        "html_url": data.get("html_url") or "",
        "published_at": data.get("published_at") or "",
    }


async def fetch_latest_commit(
    host: Any, owner: str, repo: str, token: str
) -> dict[str, Any] | None:
    """取最新一次提交。"""
    res = await _get(host, f"/repos/{owner}/{repo}/commits?per_page=1", token)
    if common.status_of(res) != 200:
        return None
    data = common.json_of(res)
    if not isinstance(data, list) or not data:
        return None
    commit = data[0].get("commit") or {}
    return {
        "sha": data[0].get("sha") or "",
        "message": (commit.get("message") or "").split("\n")[0],
        "author": (commit.get("author") or {}).get("name") or "",
        "html_url": data[0].get("html_url") or "",
    }


def format_repo_card(r: dict[str, Any]) -> str:
    """拼仓库描述卡片（对齐视频解析插件的风格）。"""
    lines = [f"【GitHub】{r['full_name']}"]
    if r["description"]:
        lines.append(r["description"])
    meta = []
    if r["language"]:
        meta.append(r["language"])
    if r["stars"]:
        meta.append(f"★ {r['stars']}")
    if r["forks"]:
        meta.append(f"⑂ {r['forks']}")
    if meta:
        lines.append(" · ".join(meta))
    if r["archived"]:
        lines.append("⚠ 仓库已归档，不再更新")
    if r["html_url"]:
        lines.append(r["html_url"])
    return "\n".join(lines)


def format_issue_card(i: dict[str, Any]) -> str:
    """拼 Issue / PR 描述卡片。"""
    tag = "PR" if i["kind"] == "pr" else "Issue"
    lines = [f"【GitHub {tag}】{i['full_name'] if 'full_name' in i else ''}#{i['number']}"]
    lines = [f"【GitHub {tag}】#{i['number']} {i['title']}"]
    meta = [i["state"]]
    if i["user"]:
        meta.append(f"@{i['user']}")
    lines.append(" · ".join(meta))
    if i["html_url"]:
        lines.append(i["html_url"])
    return "\n".join(lines)


class SubscriptionStore:
    """订阅列表的本地持久化存储（JSON，原子写）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        data: dict[str, Any] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
            except (ValueError, OSError):
                data = {}
        self._cache = data
        return data

    def _save(self) -> None:
        data = self._cache or {}
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def key(self, owner: str, repo: str) -> str:
        return f"{owner}/{repo}".lower()

    def subscribe(self, owner: str, repo: str, target: dict[str, Any]) -> bool:
        """添加一个订阅目标；已存在则追加目标，返回是否为新仓库。"""
        data = self._load()
        k = self.key(owner, repo)
        entry = data.get(k)
        is_new = entry is None
        if entry is None:
            entry = {
                "owner": owner,
                "repo": repo,
                "targets": [],
                "last_release": "",
                "last_commit": "",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            data[k] = entry
        for t in entry["targets"]:
            if t.get("type") == target.get("type") and str(t.get("id")) == str(
                target.get("id")
            ):
                return is_new
        entry["targets"].append(target)
        self._save()
        return is_new

    def unsubscribe(self, owner: str, repo: str, target: dict[str, Any]) -> bool:
        """移除一个订阅目标；目标清空后删掉整个仓库订阅。返回是否还存在该仓库。"""
        data = self._load()
        k = self.key(owner, repo)
        entry = data.get(k)
        if not entry:
            return False
        entry["targets"] = [
            t
            for t in entry["targets"]
            if not (t.get("type") == target.get("type") and str(t.get("id")) == str(target.get("id")))
        ]
        if not entry["targets"]:
            del data[k]
        self._save()
        return k in data

    def list_for(self, target: dict[str, Any]) -> list[dict[str, Any]]:
        """列出某个会话（群/私聊）订阅的仓库。"""
        data = self._load()
        out = []
        for entry in data.values():
            if any(
                t.get("type") == target.get("type")
                and str(t.get("id")) == str(target.get("id"))
                for t in entry["targets"]
            ):
                out.append(entry)
        return out

    def all(self) -> list[dict[str, Any]]:
        """列出全部订阅（轮询用）。"""
        return list(self._load().values())

    def update_marks(self, owner: str, repo: str, release: str, commit: str) -> None:
        """轮询后更新该仓库的上次见到标记。"""
        data = self._load()
        k = self.key(owner, repo)
        entry = data.get(k)
        if not entry:
            return
        if release:
            entry["last_release"] = release
        if commit:
            entry["last_commit"] = commit
        self._save()
