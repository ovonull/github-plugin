"""
公共工具：GitHub 链接识别、出网封装、文本格式化。

识别规则（对齐 SuInk/Diana 的 GitHub 解析行为，用 Python 重新实现）：
- 只有 github.com 域下的 owner/repo、Issue、PR、Release 链接会被识别；
- github.com 根路径下的保留页面（settings、topics、collections、explore…）
  不是用户仓库，一律不认；
- owner / repo 只允许字母、数字、点、下划线、连字符；
- 仓库名后面的 .git 是克隆地址后缀，剥掉；
- /issues/N 与 /pull/N 里的 N 必须是正整数才算 Issue / PR，
  只到 /issues 这一层仍然按仓库处理；
- /releases、/releases/latest、/releases/tag/<版本> 单独走 Release 通道。

出网约定：
- 所有出网都走 host.http_request，域名受清单白名单约束；
- 解析失败统一抛 GitHubError，带上给用户看的原因，不向宿主抛裸异常。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit


API_BASE = "https://api.github.com"

# github.com 根路径下这些页面不属于某个用户或组织，不能当成 owner/repo。
# 与 Diana 的 githubReservedOwners 保持一致。
RESERVED_OWNERS = frozenset(
    {
        "about",
        "apps",
        "collections",
        "contact",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "login",
        "marketplace",
        "new",
        "notifications",
        "orgs",
        "pricing",
        "pulls",
        "search",
        "security",
        "settings",
        "sponsors",
        "topics",
        "trending",
        "users",
    }
)

# GitHub 的用户名与仓库名只允许这些字符。
GH_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# 从一段聊天文本里捞 URL 形态的片段。
_URL_TOKEN = re.compile(r"https?://[^\s<>\"'）)】》，。；、！？]+", re.IGNORECASE)
# 没有 scheme 的裸写法（群里很常见）单独再捞一次。
_BARE_TOKEN = re.compile(
    r"(?<![\w.@/-])github\.com/[^\s<>\"'）)】》，。；、！？]+", re.IGNORECASE
)

# 链接末尾常跟着中文标点或英文句点，识别前先削掉。
_TRAILING = "。，、；：！？）】》」』.,;:!?)]}>\"'"

# 正文与描述的截断上限（字符数），与 Diana 一致。
BODY_MAX = 600
DESCRIPTION_MAX = 300
# 标签与话题最多展示几个。
LABEL_MAX = 6
TOPIC_MAX = 6


class GitHubError(Exception):
    """解析失败，message 给用户看，hint 是可选建议。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _clean_token(token: str) -> str:
    """削掉链接末尾的标点。"""
    return str(token or "").strip().rstrip(_TRAILING)


def _iter_candidates(text: str) -> list[str]:
    """列出文本里可能的 GitHub 链接（先带 scheme 的，再裸的）。"""
    content = str(text or "")
    seen: list[str] = []
    for pattern in (_URL_TOKEN, _BARE_TOKEN):
        for match in pattern.finditer(content):
            token = _clean_token(match.group(0))
            if token and token not in seen:
                seen.append(token)
    return seen


def _split_host_path(candidate: str) -> tuple[str, str] | None:
    """
    把候选链接拆成 (host, path)。

    域名必须是 github.com 或它的子域；github.com.evil.example 这种
    看着像的域名会被挡掉。
    """
    raw = str(candidate or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host != "github.com" and not host.endswith(".github.com"):
        return None
    return host, parsed.path


def _path_parts(path: str) -> list[str]:
    """路径按 / 拆开，去掉首尾空段。"""
    return [item for item in str(path or "").split("/") if item]


def parse_github_target(candidate: str) -> dict[str, Any] | None:
    """
    判断一个链接是不是仓库 / Issue / PR。

    Returns:
        {"kind": "repo"|"issue"|"pr", "owner": ..., "repo": ..., "number": int}
        不是 GitHub 资源链接时返回 None。
    """
    parts = _split_host_path(candidate)
    if parts is None:
        return None
    _host, path = parts
    segments = _path_parts(path)
    if len(segments) < 2:
        return None
    owner = segments[0]
    if owner.lower() in RESERVED_OWNERS:
        return None
    repo = segments[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not repo or not GH_NAME.match(owner) or not GH_NAME.match(repo):
        return None

    target: dict[str, Any] = {"kind": "repo", "owner": owner, "repo": repo}
    if len(segments) >= 4 and segments[2].lower() in ("issues", "pull"):
        number = segments[3]
        if number.isdigit() and int(number) > 0:
            target["number"] = int(number)
            target["kind"] = "pr" if segments[2].lower() == "pull" else "issue"
    return target


def find_github_target(text: str) -> dict[str, Any] | None:
    """
    从一段文本里找出第一个 GitHub 仓库 / Issue / PR 链接。

    Returns:
        {"kind": "issue"|"pr"|"repo", "owner": ..., "repo": ..., "number": int?}
        没找到时返回 None。
    """
    for candidate in _iter_candidates(text):
        target = parse_github_target(candidate)
        if target is not None:
            return target
    return None


def find_release_target(text: str) -> dict[str, Any] | None:
    """
    从一段文本里找出 GitHub Release 链接。

    Release 只能走公开地址：https + github.com 本域 + 路径里没有
    点号段与空段，且不带查询参数（带 ?page=2 的列表页不是具体版本）。

    Returns:
        {"owner": ..., "repo": ..., "mode": "latest"|"tag", "tag": str}
        没找到时返回 None。
    """
    for candidate in _iter_candidates(text):
        raw = str(candidate or "").strip()
        if "://" not in raw:
            raw = "https://" + raw
        try:
            parsed = urlsplit(raw)
        except ValueError:
            continue
        if parsed.scheme.lower() != "https":
            continue
        # Release 只认 github.com 本域，且不带端口：www 与其他子域一律不认。
        if parsed.netloc.lower() != "github.com":
            continue
        if parsed.query or parsed.username or parsed.password:
            continue
        # 路径按 / 拆开后，段内不能出现空段、. 或 ..。
        segments = str(parsed.path or "").strip("/").split("/") if parsed.path else []
        if len(segments) < 3:
            continue
        if any(item in ("", ".", "..") for item in segments):
            continue
        owner, repo, third = segments[0], segments[1], segments[2].lower()
        if third != "releases":
            continue
        if owner.lower() in RESERVED_OWNERS:
            continue
        if not GH_NAME.match(owner) or not GH_NAME.match(repo):
            continue
        if len(segments) == 3:
            return {"owner": owner, "repo": repo, "mode": "latest", "tag": ""}
        if len(segments) == 4 and segments[3].lower() == "latest":
            return {"owner": owner, "repo": repo, "mode": "latest", "tag": ""}
        if len(segments) >= 5 and segments[3].lower() == "tag":
            tag = "/".join(segments[4:])
            if not tag:
                continue
            return {"owner": owner, "repo": repo, "mode": "tag", "tag": tag}
    return None


def resource_key(target: dict[str, Any]) -> str:
    """
    资源标识：仓库是 github:owner/repo，Issue/PR 带上编号。

    与 Diana 的 resourceKey 一致，owner/repo 一律小写。
    """
    key = "github:" + f"{target.get('owner', '')}/{target.get('repo', '')}".lower()
    kind = target.get("kind")
    if kind == "issue":
        return f"{key}#issue-{int(target.get('number') or 0)}"
    if kind == "pr":
        return f"{key}#pull-{int(target.get('number') or 0)}"
    return key


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
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def clip(text: str, limit: int) -> str:
    """截断成单行可读文本。"""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def truncate_runes(text: str, limit: int) -> str:
    """
    按字符数截断，超出部分用省略号收尾。

    与 Diana 的 truncateRunes 同义：先去掉首尾空白，再按字符切。
    """
    value = str(text or "").strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + "…"


def compact_whitespace(text: str) -> str:
    """把连续空白压成单个空格（Issue 正文常带换行与缩进）。"""
    return " ".join(str(text or "").split())


def limit_strings(values: list[str], limit: int) -> list[str]:
    """去重后取前 limit 个（limit<=0 表示不限）。"""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in out:
            continue
        out.append(text)
    if limit > 0 and len(out) > limit:
        return out[:limit]
    return out


def first_non_empty(*values: Any) -> str:
    """取第一个非空值。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def github_day(timestamp: str) -> str:
    """取 RFC3339 时间戳的日期部分。"""
    text = str(timestamp or "")
    return text[:10] if len(text) >= 10 else ""


def api_headers(token: str) -> dict[str, str]:
    """GitHub API 请求头；带 Token 时额度更高。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucbk-github-plugin/1.1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
