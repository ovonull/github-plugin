"""
公共工具：GitHub 链接识别、出网封装、文本格式化。

约定：
- 所有出网都走 host.http_request，域名受清单白名单约束；
- 解析失败统一抛 GitHubError，带上给用户看的原因，不向宿主抛裸异常。
"""

from __future__ import annotations

import json
import re
from typing import Any


API_BASE = "https://api.github.com"

# GitHub 链接识别：仓库、Issue、PR。
# 仓库名后的边界要允许 / ? # 空白或结尾，才能兼容 "xxx.git 装一下" 这类带尾文的粘贴。
GH_REPO = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?=[/?#\s]|$)",
    re.IGNORECASE,
)
GH_ISSUE = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:issues|pull)/(\d+)",
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
            "kind": "pr" if "/pull/" in m.group(0).lower() else "issue",
            "owner": m.group(1),
            "repo": m.group(2),
            "number": int(m.group(3)),
        }
    m = GH_REPO.search(content)
    if m:
        # 正则里的 (?:\.git)? 已把克隆地址后缀剥离，这里拿到的是仓库名本身。
        return {"kind": "repo", "owner": m.group(1), "repo": m.group(2)}
    return None


def status_of(result: dict[str, Any]) -> int:
    """从出网结果里读 HTTP 状态码。"""
    try:
        return int(result.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def json_of(result: dict[str, Any]) -> dict[str, Any]:
    """从出网结果里取 JSON 对象，取不到时回退到解析 text。"""
    payload = result.get("json")
    if isinstance(payload, dict):
        return payload
    text = str(result.get("text") or "")
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def json_list_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    """从出网结果里取 JSON 数组，取不到时回退到解析 text。"""
    payload = result.get("json")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    text = str(result.get("text") or "")
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except ValueError:
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def clip(text: str, limit: int) -> str:
    """截断成单行可读文本。"""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


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
