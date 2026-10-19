"""
公共工具：GitHub 链接识别、出网封装、文本格式化。

约定：
- 所有出网都走 host.http_request，域名受清单白名单约束；
- 解析失败统一抛 GitHubError，带上给用户看的原因，不向宿主抛裸异常。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


API_BASE = "https://api.github.com"

# GitHub 链接识别：仓库、Issue、PR、Release、Commit。
GH_REPO = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:\.git)?(?:/|$)", re.IGNORECASE
)
GH_ISSUE = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:issues|pull)/(\d+)",
    re.IGNORECASE,
)
GH_RELEASE = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/releases?(?:/|$)",
    re.IGNORECASE,
)


class GitHubError(Exception):
    """解析失败，message 给用户看，hint 是可选建议。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def find_github_target(text: str) -> dict[str, Any] | None:
    """
    从一段文本里找出第一个 GitHub 链接，并归类为仓库 / Issue / PR。

    Returns:
        {"kind": "issue"|"pr"|"repo", "owner": ..., "repo": ..., "number": int?}
        没找到时返回 None。
    """
    content = str(text or "")
    m = GH_ISSUE.search(content)
    if m:
        return {
            "kind": "pull" if "/pull/" in m.group(0).lower() else "issue",
            "owner": m.group(1),
            "repo": m.group(2),
            "number": int(m.group(3)),
        }
    m = GH_REPO.search(content)
    if m:
        return {"kind": "repo", "owner": m.group(1), "repo": m.group(2)}
    return None


def status_of(result: dict[str, Any]) -> int:
    """从出网结果里读 HTTP 状态码。"""
    try:
        return int(result.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def json_of(result: dict[str, Any]) -> dict[str, Any]:
    """从出网结果里取 JSON，取不到时回退到解析 text。"""
    payload = result.get("json")
    if isinstance(payload, dict):
        return payload
    text = str(result.get("text") or "")
    if not text:
        return {}
    try:
        import json

        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def headers_of(result: dict[str, Any]) -> dict[str, str]:
    """取响应头，统一转小写键。"""
    raw = result.get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def clip(text: str, limit: int) -> str:
    """截断成单行可读文本。"""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def host_of(url: str) -> str:
    """取链接域名（小写）。"""
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def api_headers(token: str) -> dict[str, str]:
    """GitHub API 请求头；带 Token 时额度更高。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucbk-github-plugin/1.0.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
