"""
GitHub API 调用与卡片格式化。

卡片内容对齐 SuInk/Diana 的 GitHub 解析行为（用 Python 重新实现）：
- 仓库：名称 + 归档/Fork 标记 + 简介 + 星标/Fork/开放 Issue 数 + 语言 +
  许可证 + 话题 + 最近推送日期；
- Issue：状态（区分「不计划处理」）+ 作者 + 评论数 + 创建日期 + 标签 + 正文摘要；
- PR：走 pulls 接口，额外给出合并/草稿状态与改动规模（文件数、增删行）；
- Release：只认官方 API 返回的数据，并核对返回地址与请求的仓库、版本一致。

设计说明：
- 只读查询（仓库 / Issue / PR / Release / Commit / Star）都走 host.http_request；
- 订阅与设置的持久化在 gparse/store.py，不放在这里。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

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
        raise GitHubError(
            "仓库不存在或无权限访问",
            "确认 owner/repo 拼写，私有仓库需要有效 Token",
        )
    if status != 200:
        raise GitHubError(
            f"GitHub 返回 {status}",
            "可能是触发了匿名限流，配置 GitHub Token 后重试",
        )
    data = common.json_of(res)
    license_info = data.get("license") or {}
    topics = [str(item) for item in (data.get("topics") or [])]
    return {
        "kind": "repo",
        "full_name": data.get("full_name") or f"{owner}/{repo}",
        "description": common.truncate_runes(
            data.get("description") or "", common.DESCRIPTION_MAX
        ),
        "language": data.get("language") or "",
        "stars": data.get("stargazers_count") or 0,
        "forks": data.get("forks_count") or 0,
        "open_issues": data.get("open_issues_count") or 0,
        "archived": bool(data.get("archived")),
        "fork": bool(data.get("fork")),
        "pushed_at": data.get("pushed_at") or "",
        "topics": common.limit_strings(topics, common.TOPIC_MAX),
        "license": common.first_non_empty(
            license_info.get("spdx_id"), license_info.get("name")
        ),
        "html_url": data.get("html_url") or "",
    }


async def fetch_issue(
    host: Any, owner: str, repo: str, number: int, token: str
) -> dict[str, Any]:
    """取 Issue 详情。"""
    res = await _get(host, f"/repos/{owner}/{repo}/issues/{number}", token)
    status = common.status_of(res)
    if status != 200:
        raise GitHubError("没有找到 #%d" % number, "确认编号是否存在，私有仓库需要 Token")
    data = common.json_of(res)
    return {
        "kind": "issue",
        "number": number,
        "title": data.get("title") or "",
        "state": data.get("state") or "",
        "state_reason": data.get("state_reason") or "",
        "user": (data.get("user") or {}).get("login") or "",
        "comments": int(data.get("comments") or 0),
        "created_at": data.get("created_at") or "",
        "labels": [
            str(item.get("name") or "")
            for item in (data.get("labels") or [])
            if isinstance(item, dict)
        ],
        "body": data.get("body") or "",
        "html_url": data.get("html_url") or "",
    }


async def fetch_pull(
    host: Any, owner: str, repo: str, number: int, token: str
) -> dict[str, Any]:
    """
    取 PR 详情。

    走 pulls 接口而不是 issues：只有 pulls 才给得出合并状态、草稿标记、
    改动文件数与增删行数。
    """
    res = await _get(host, f"/repos/{owner}/{repo}/pulls/{number}", token)
    status = common.status_of(res)
    if status != 200:
        raise GitHubError("没有找到 #%d" % number, "确认编号是否存在，私有仓库需要 Token")
    data = common.json_of(res)
    return {
        "kind": "pr",
        "number": number,
        "title": data.get("title") or "",
        "state": data.get("state") or "",
        "merged": bool(data.get("merged")),
        "draft": bool(data.get("draft")),
        "user": (data.get("user") or {}).get("login") or "",
        "comments": int(data.get("comments") or 0),
        "created_at": data.get("created_at") or "",
        "labels": [
            str(item.get("name") or "")
            for item in (data.get("labels") or [])
            if isinstance(item, dict)
        ],
        "body": data.get("body") or "",
        "changed_files": int(data.get("changed_files") or 0),
        "additions": int(data.get("additions") or 0),
        "deletions": int(data.get("deletions") or 0),
        "head_ref": (data.get("head") or {}).get("ref") or "",
        "base_ref": (data.get("base") or {}).get("ref") or "",
        "html_url": data.get("html_url") or "",
    }


async def fetch_release(
    host: Any, owner: str, repo: str, token: str, *, tag: str = "", latest: bool = True
) -> dict[str, Any]:
    """
    读一个 Release，并对返回内容做核验。

    与 Diana 的 github_release_source 同一套判断：
    - 只能读公开 Release 地址，不带凭据、不带查询参数；
    - 草稿不算发布；指定版本时预发布也算；
    - 返回的发布地址必须与请求的仓库、版本对得上，对不上就是无效数据；
    - HTTP 失败只说明这次没读到，不能当成「该版本不存在」的证据。
    """
    if latest:
        path = f"/repos/{owner}/{repo}/releases/latest"
    else:
        path = f"/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
    res = await _get(host, path, token)
    status = common.status_of(res)
    if status != 200:
        raise GitHubError(
            f"读取 GitHub Release 失败（HTTP {status}），"
            "这不能证明该版本不存在",
            "可能是限流或网络问题，稍后再试",
        )
    data = common.json_of(res)
    release_tag = str(data.get("tag_name") or "").strip()
    published_at = str(data.get("published_at") or "").strip()
    html_url = str(data.get("html_url") or "").strip()

    if not release_tag:
        raise GitHubError("Release 数据不完整（没有版本号）", "")
    if not published_at:
        raise GitHubError("Release 数据不完整（没有发布时间）", "")
    if data.get("draft"):
        raise GitHubError("这个 Release 还是草稿，没有正式发布", "")
    if latest and data.get("prerelease"):
        raise GitHubError("仓库最新的正式版还没发布（当前最新的是预发布）", "")

    # 核对返回地址：必须与请求的仓库、版本一致，防止拿到别的仓库的发布。
    expected_repo = f"{owner}/{repo}".lower()
    if not _release_url_matches(html_url, expected_repo, release_tag):
        raise GitHubError("Release 返回的地址与请求的仓库或版本不一致，已忽略", "")

    return {
        "kind": "release",
        "tag": release_tag,
        "name": str(data.get("name") or release_tag),
        "html_url": html_url,
        "published_at": published_at,
        "prerelease": bool(data.get("prerelease")),
        "body": str(data.get("body") or ""),
    }


def _release_url_matches(html_url: str, expected_repo: str, tag: str) -> bool:
    """
    核对 Release 的发布页地址。

    要求：域名是 github.com，路径是 /{owner}/{repo}/releases/tag/{版本}，
    仓库与版本都要和请求一致。
    """
    raw = str(html_url or "").strip()
    if not raw:
        return False
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    if parsed.netloc.lower() != "github.com":
        return False
    segments = [item for item in parsed.path.split("/") if item]
    if len(segments) < 5:
        return False
    if segments[2].lower() != "releases" or segments[3].lower() != "tag":
        return False
    repo_path = f"{segments[0]}/{segments[1]}".lower()
    if repo_path != expected_repo:
        return False
    from urllib.parse import unquote

    return unquote("/".join(segments[4:])) == tag


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
    data = common.json_list_of(res)
    if not data:
        return None
    commit = data[0].get("commit") or {}
    return {
        "sha": data[0].get("sha") or "",
        "message": (commit.get("message") or "").split("\n")[0],
        "author": (commit.get("author") or {}).get("name") or "",
        "html_url": data[0].get("html_url") or "",
    }


async def fetch_latest_issues(
    host: Any, owner: str, repo: str, token: str
) -> list[dict[str, Any]]:
    """取最近的 Issue/PR，用于发现订阅后的新项目活动。"""
    res = await _get(
        host,
        f"/repos/{owner}/{repo}/issues?state=all&sort=created&direction=desc&per_page=30",
        token,
    )
    if common.status_of(res) != 200:
        return []
    items = []
    for data in common.json_list_of(res):
        item = {
            "number": int(data.get("number") or 0),
            "kind": "pr" if "pull_request" in data else "issue",
            "title": data.get("title") or "",
            "user": (data.get("user") or {}).get("login") or "",
            "html_url": data.get("html_url") or "",
            "created_at": data.get("created_at") or "",
        }
        if item["number"]:
            items.append(item)
    return items


async def fetch_commits(
    host: Any, owner: str, repo: str, token: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    """取最近的提交列表（新到旧）。"""
    res = await _get(
        host, f"/repos/{owner}/{repo}/commits?per_page={max(1, min(int(limit), 100))}", token
    )
    if common.status_of(res) != 200:
        return []
    items = []
    for data in common.json_list_of(res):
        commit = data.get("commit") or {}
        author = commit.get("author") or {}
        items.append(
            {
                "sha": str(data.get("sha") or "")[:7],
                "full_sha": str(data.get("sha") or ""),
                "message": (commit.get("message") or "").split("\n")[0],
                "author": str(author.get("name") or ""),
                "time": str(commit.get("author") or {}).get("date") or "",
                "html_url": data.get("html_url") or "",
            }
        )
    return items


async def fetch_pulls(
    host: Any, owner: str, repo: str, token: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    """取最近的 PR 列表（新到旧）。"""
    res = await _get(
        host,
        f"/repos/{owner}/{repo}/pulls?state=all&sort=created&direction=desc&per_page={max(1, min(int(limit), 100))}",
        token,
    )
    if common.status_of(res) != 200:
        return []
    items = []
    for data in common.json_list_of(res):
        items.append(
            {
                "number": int(data.get("number") or 0),
                "title": data.get("title") or "",
                "state": data.get("state") or "",
                "merged": bool(data.get("merged_at") or data.get("merged")),
                "draft": bool(data.get("draft")),
                "user": (data.get("user") or {}).get("login") or "",
                "time": data.get("created_at") or "",
                "head_ref": (data.get("head") or {}).get("ref") or "",
                "base_ref": (data.get("base") or {}).get("ref") or "",
                "html_url": data.get("html_url") or "",
            }
        )
    return items


async def fetch_releases(
    host: Any, owner: str, repo: str, token: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    """取最近的 Release 列表（新到旧）。"""
    res = await _get(
        host, f"/repos/{owner}/{repo}/releases?per_page={max(1, min(int(limit), 100))}", token
    )
    if common.status_of(res) != 200:
        return []
    items = []
    for data in common.json_list_of(res):
        if data.get("draft"):
            continue
        items.append(
            {
                "tag": data.get("tag_name") or "",
                "name": data.get("name") or data.get("tag_name") or "",
                "time": data.get("published_at") or "",
                "prerelease": bool(data.get("prerelease")),
                "html_url": data.get("html_url") or "",
            }
        )
    return items


async def fetch_star_events(
    host: Any, owner: str, repo: str, token: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    """
    取最近的 Star 事件。

    用 WatchEvent 判断有没有人 star，stargazers_count 只用来显示数字——
    拿数字当触发器再去列表里倒推是谁，反推不准。
    """
    res = await _get(
        host, f"/repos/{owner}/{repo}/events?per_page={max(1, min(int(limit), 100))}", token
    )
    if common.status_of(res) != 200:
        return []
    items = []
    for data in common.json_list_of(res):
        if str(data.get("type") or "") != "WatchEvent":
            continue
        payload = data.get("payload") or {}
        if str(payload.get("action") or "").lower() != "started":
            continue
        login = str((data.get("actor") or {}).get("login") or "")
        if not login:
            continue
        items.append(
            {
                "id": str(data.get("id") or ""),
                "login": login,
                "url": (data.get("actor") or {}).get("html_url") or "",
                "time": data.get("created_at") or "",
            }
        )
    return items


async def fetch_stargazers_count(
    host: Any, owner: str, repo: str, token: str
) -> int:
    """取仓库当前 Star 总数。"""
    res = await _get(host, f"/repos/{owner}/{repo}", token)
    if common.status_of(res) != 200:
        return 0
    return int(common.json_of(res).get("stargazers_count") or 0)


async def fetch_repo_privacy(
    host: Any, owner: str, repo: str, token: str
) -> tuple[str, bool]:
    """取仓库简介与是否私有，一次请求两样都有。"""
    res = await _get(host, f"/repos/{owner}/{repo}", token)
    if common.status_of(res) != 200:
        return "", False
    data = common.json_of(res)
    return str(data.get("description") or "").strip(), bool(data.get("private"))


async def fetch_account(host: Any, token: str) -> str:
    """
    用一条凭据查当前账号名。

    用来验证凭据能不能用——匿名调用 /user 会返回 401，
    带对 Token 才会返回账号信息。
    """
    res = await _get(host, "/user", token)
    status = common.status_of(res)
    if status == 401:
        raise GitHubError("凭据无效或已过期", "重新生成 Token 后再试")
    if status == 403:
        raise GitHubError("凭据权限不足或触发了限流", "稍后再试，或换一个 Token")
    if status != 200:
        raise GitHubError(f"GitHub 返回 {status}", "稍后再试")
    return str(common.json_of(res).get("login") or "")


def render_template(template: str, values: dict[str, str]) -> str:
    """
    逐行替换占位符。

    某一行里出现过占位符、而全部占位符都替换成空串时，这一行连同它的静态前缀
    一起删除——作者缺失时不能留下一行光秃秃的「作者：」。没有占位符的静态行原样保留。
    """
    lines = []
    for line in str(template or "").split("\n"):
        replaced = line
        saw_placeholder = False
        saw_value = False
        for key, value in values.items():
            placeholder = "{" + key + "}"
            if placeholder not in replaced:
                continue
            saw_placeholder = True
            if str(value or "").strip():
                saw_value = True
            replaced = replaced.replace(placeholder, str(value or ""))
        if saw_placeholder and not saw_value:
            continue
        if saw_placeholder:
            while " ·  · " in replaced:
                replaced = replaced.replace(" ·  · ", " · ")
            replaced = replaced.strip(" \t·|-—、,，")
        if not replaced.strip() and saw_placeholder:
            continue
        lines.append(replaced.strip())
    return "\n".join(lines)


def format_repo_card(r: dict[str, Any]) -> str:
    """拼仓库描述卡片。"""
    title = f"【GitHub】{r['full_name']}"
    if r.get("archived"):
        title += "（已归档）"
    if r.get("fork"):
        title += "（Fork）"
    lines = [title]
    if r.get("description"):
        lines.append(r["description"])
    stats = [
        f"★ {r.get('stars', 0)}",
        f"Fork {r.get('forks', 0)}",
        f"开放 Issue/PR {r.get('open_issues', 0)}",
    ]
    if r.get("language"):
        stats.append(r["language"])
    if r.get("license"):
        stats.append(r["license"])
    lines.append(" · ".join(stats))
    topics = r.get("topics") or []
    if topics:
        lines.append("话题：" + "、".join(topics))
    day = common.github_day(r.get("pushed_at") or "")
    if day:
        lines.append(f"最近推送：{day}")
    if r.get("html_url"):
        lines.append(r["html_url"])
    return "\n".join(lines)


def _issue_state_text(state: str, state_reason: str) -> str:
    """Issue 状态文案，关闭时区分「不计划处理」。"""
    if state != "closed":
        return "开放"
    if state_reason == "not_planned":
        return "已关闭（不计划处理）"
    return "已关闭"


def _pull_state_text(pull: dict[str, Any]) -> str:
    """PR 状态文案：合并 > 草稿 > 关闭 > 开放。"""
    if pull.get("merged"):
        return "已合并"
    if pull.get("draft"):
        return "草稿"
    if pull.get("state") == "closed":
        return "已关闭"
    return "开放"


def _thread_text(
    label: str,
    full_name: str,
    item: dict[str, Any],
    state_text: str,
    extra: str = "",
) -> list[str]:
    """Issue / PR 卡片正文。"""
    lines = [
        f"【GitHub {label}】{full_name}#{item.get('number', 0)} {str(item.get('title') or '').strip()}"
    ]
    meta = [state_text]
    if item.get("user"):
        meta.append(f"作者 {item['user']}")
    meta.append(f"{int(item.get('comments') or 0)} 条评论")
    day = common.github_day(item.get("created_at") or "")
    if day:
        meta.append(day)
    lines.append(" · ".join(meta))
    if extra:
        lines.append(extra)
    labels = common.limit_strings(item.get("labels") or [], common.LABEL_MAX)
    if labels:
        lines.append("标签：" + "、".join(labels))
    body = common.truncate_runes(
        common.compact_whitespace(item.get("body") or ""), common.BODY_MAX
    )
    if body:
        lines.append(body)
    if item.get("html_url"):
        lines.append(item["html_url"])
    return lines


def format_issue_card(i: dict[str, Any], full_name: str = "") -> str:
    """拼 Issue 描述卡片。"""
    name = full_name or i.get("full_name") or ""
    lines = _thread_text(
        "Issue", name, i, _issue_state_text(i.get("state", ""), i.get("state_reason", ""))
    )
    return "\n".join(lines)


def format_pull_card(p: dict[str, Any], full_name: str = "") -> str:
    """拼 PR 描述卡片（含改动规模）。"""
    name = full_name or p.get("full_name") or ""
    extra = ""
    head = p.get("head_ref") or ""
    base = p.get("base_ref") or ""
    if head or base:
        extra = (
            f"{head} → {base} · {int(p.get('changed_files') or 0)} 个文件 "
            f"+{int(p.get('additions') or 0)} −{int(p.get('deletions') or 0)}"
        )
    lines = _thread_text("PR", name, p, _pull_state_text(p))
    if extra:
        lines.insert(2, extra)
    return "\n".join(lines)


def format_release_card(r: dict[str, Any]) -> str:
    """拼 Release 描述卡片。"""
    lines = [f"【GitHub Release】{r.get('tag', '')}"]
    name = str(r.get("name") or "").strip()
    if name and name != r.get("tag"):
        lines.append(name)
    lines.append(f"发布时间：{r.get('published_at', '')}")
    lines.append(f"预发布：{'是' if r.get('prerelease') else '否'}")
    if r.get("html_url"):
        lines.append(r["html_url"])
    body = common.truncate_runes(
        common.compact_whitespace(r.get("body") or ""), common.BODY_MAX
    )
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)
