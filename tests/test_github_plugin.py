"""
github-plugin 离线回归测试。

跑法（不需要宿主、不联网）：

    python tests/test_github_plugin.py

用例来源：SuInk/Diana 的 Go 测试（resolver_github_test.go、
github_release_source_test.go）按同样的判定条件重写，
外加本插件自己的订阅与轮询逻辑。
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from gparse import common, github  # noqa: E402
from gparse import store as store_mod  # noqa: E402


# 宿主 SDK 替身：插件入口会 import app.services.plugin_sdk。
def _install_sdk_stub() -> None:
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    services_pkg = types.ModuleType("app.services")
    services_pkg.__path__ = []
    sdk = types.ModuleType("app.services.plugin_sdk")

    class PluginCallError(Exception):
        pass

    sdk.PluginCallError = PluginCallError
    sdk.on_message = lambda fn: fn
    sys.modules["app"] = app_pkg
    sys.modules["app.services"] = services_pkg
    sys.modules["app.services.plugin_sdk"] = sdk


_install_sdk_stub()

import plugin  # noqa: E402


class FakeHost:
    """按插件调用方式返回预置响应，并记录请求。"""

    def __init__(self, responder=None, default=None):
        self.responder = responder
        self.default = default or {}
        self.config = {
            "reply_scope": "群聊与私聊",
            "trigger_prefix": "gh",
            "github_token": "",
            "poll_interval": 300,
            "max_notify_per_poll": 5,
        }
        self.bot_qq = 10000
        self.requests: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.sent: list[str] = []

    def log(self, message, level="info"):
        self.logs.append(str(message))

    def has(self, capability):
        return True

    async def http_request(self, method, url, *, headers=None, query=None, **kwargs):
        self.requests.append((method, url))
        if self.responder is not None:
            return self.responder(method, url)
        return {"status": 200, "json": self.default}

    async def send_group_msg(self, group_id, message):
        self.sent.append(str(message))

    async def send_private_msg(self, user_id, message):
        self.sent.append(str(message))


def json_response(key: str, payload: dict) -> dict:
    """拼出网关风格的响应包。"""

    def responder(method, url):
        return {"status": 200, "json": {key: {"code": "0", "result": json.dumps(payload)}}}

    return responder


def run(cases: list[tuple[str, bool, str]], title: str) -> int:
    print(f"--- {title}")
    failed = 0
    for name, ok, detail in cases:
        print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  {detail}"))
        if not ok:
            failed += 1
    return failed


def test_link_recognition() -> list[tuple[str, bool, str]]:
    """链接识别：对应 Diana 的 TestParseGitHubURL。"""
    cases: list[tuple[str, bool, str]] = []

    def expect_repo(raw: str, owner: str, repo: str) -> None:
        target = common.parse_github_target(raw)
        ok = bool(target) and target["kind"] == "repo" and target["owner"] == owner and target["repo"] == repo
        cases.append((f"仓库 {raw}", ok, str(target)))

    def expect_issue(raw: str, number: int) -> None:
        target = common.parse_github_target(raw)
        ok = bool(target) and target["kind"] == "issue" and target["number"] == number
        cases.append((f"Issue {raw}", ok, str(target)))

    def expect_pr(raw: str, number: int) -> None:
        target = common.parse_github_target(raw)
        ok = bool(target) and target["kind"] == "pr" and target["number"] == number
        cases.append((f"PR {raw}", ok, str(target)))

    def expect_none(raw: str) -> None:
        target = common.parse_github_target(raw)
        cases.append((f"不认 {raw}", target is None, str(target)))

    expect_repo("https://github.com/acme/widget", "acme", "widget")
    expect_repo("https://github.com/acme/widget.git", "acme", "widget")
    expect_repo("https://github.com/acme/widget/tree/main/docs", "acme", "widget")
    expect_repo("https://github.com/acme/widget/issues", "acme", "widget")
    expect_issue("https://github.com/acme/widget/issues/12", 12)
    expect_pr("https://github.com/acme/widget/pull/34/files", 34)
    expect_none("https://github.com/acme")
    expect_none("https://github.com/settings/profile")
    expect_none("https://github.com/topics/golang")
    expect_none("https://example.com/acme/widget")

    # 群里常见的写法：不带 scheme、后面跟着话。
    found = common.find_github_target("看看这个 https://github.com/ovonull/video-parser.git 装一下")
    cases.append(
        (
            "克隆地址带尾文仍认得出仓库",
            bool(found) and found["owner"] == "ovonull" and found["repo"] == "video-parser",
            str(found),
        )
    )
    found2 = common.find_github_target("github.com/ovonull/github-plugin")
    cases.append(
        (
            "裸写法也认得出仓库",
            bool(found2) and found2["repo"] == "github-plugin",
            str(found2),
        )
    )
    found3 = common.find_github_target("u.jd.com/abc 不是 GitHub")
    cases.append(("非 GitHub 链接不认", found3 is None, str(found3)))

    # 资源标识与 Diana 的 resourceKey 一致。
    key = common.resource_key({"kind": "pr", "owner": "Acme", "repo": "Widget", "number": 7})
    cases.append(("资源标识小写且带类型", key == "github:acme/widget#pull-7", key))
    key2 = common.resource_key({"kind": "repo", "owner": "acme", "repo": "widget"})
    cases.append(("仓库资源标识", key2 == "github:acme/widget", key2))
    return cases


def test_release_link() -> list[tuple[str, bool, str]]:
    """Release 链接判定：对应 Diana 的 TestGitHubReleaseURLMapping。"""
    cases: list[tuple[str, bool, str]] = []

    def expect(raw: str, mode: str, tag: str = "") -> None:
        target = common.find_release_target(raw)
        ok = bool(target) and target["mode"] == mode and target.get("tag", "") == tag
        cases.append((f"Release {raw}", ok, str(target)))

    def expect_none(raw: str) -> None:
        target = common.find_release_target(raw)
        cases.append((f"不认 Release {raw}", target is None, str(target)))

    expect("https://github.com/go-gitea/gitea/releases", "latest")
    expect("https://github.com/go-gitea/gitea/releases/latest", "latest")
    expect("https://github.com/go-gitea/gitea/releases/tag/v1.27.3", "tag", "v1.27.3")
    expect("https://github.com/acme/tool/releases/tag/pkg/v2", "tag", "pkg/v2")
    expect_none("https://github.com/acme/tool/releases?page=2")
    expect_none("https://github.com.evil.example/acme/tool/releases")
    expect_none("https://token@github.com/acme/tool/releases")
    expect_none("https://github.com/acme/tool/issues")
    expect_none("https://github.com/acme/../releases")
    return cases


def test_cards() -> list[tuple[str, bool, str]]:
    """卡片内容：对应 Diana 的 TestFormatGitHubRepo / Issue / Pull。"""
    cases: list[tuple[str, bool, str]] = []

    repo = {
        "kind": "repo",
        "full_name": "acme/widget",
        "description": "A small widget toolkit",
        "language": "Go",
        "stars": 1200,
        "forks": 34,
        "open_issues": 5,
        "archived": True,
        "fork": False,
        "pushed_at": "2026-09-30T08:00:00Z",
        "topics": ["cli", "toolkit"],
        "license": "MIT",
        "html_url": "https://github.com/acme/widget",
    }
    text = github.format_repo_card(repo)
    for want in (
        "【GitHub】acme/widget（已归档）",
        "A small widget toolkit",
        "★ 1200",
        "Fork 34",
        "开放 Issue/PR 5",
        "Go",
        "MIT",
        "话题：cli、toolkit",
        "最近推送：2026-09-30",
    ):
        cases.append((f"仓库卡片含 {want}", want in text, text))

    fork_repo = dict(repo, archived=False, fork=True)
    cases.append(
        (
            "Fork 仓库有标记",
            "（Fork）" in github.format_repo_card(fork_repo),
            github.format_repo_card(fork_repo),
        )
    )

    issue = {
        "kind": "issue",
        "number": 9,
        "title": "Crash on empty input",
        "state": "closed",
        "state_reason": "not_planned",
        "user": "alice",
        "comments": 3,
        "created_at": "2026-08-01T00:00:00Z",
        "labels": ["bug", "help wanted"],
        "body": "steps:\n\n1. run it\n2. boom",
        "html_url": "https://github.com/acme/widget/issues/9",
    }
    text = github.format_issue_card(issue, "acme/widget")
    for want in (
        "【GitHub Issue】acme/widget#9 Crash on empty input",
        "已关闭（不计划处理）",
        "作者 alice",
        "3 条评论",
        "标签：bug、help wanted",
        "steps: 1. run it 2. boom",
    ):
        cases.append((f"Issue 卡片含 {want}", want in text, text))

    pull = {
        "kind": "pr",
        "number": 10,
        "title": "Fix crash",
        "state": "closed",
        "merged": True,
        "draft": False,
        "user": "bob",
        "comments": 1,
        "created_at": "2026-09-01T00:00:00Z",
        "labels": [],
        "body": "",
        "changed_files": 3,
        "additions": 10,
        "deletions": 2,
        "head_ref": "fix-crash",
        "base_ref": "main",
        "html_url": "https://github.com/acme/widget/pull/10",
    }
    text = github.format_pull_card(pull, "acme/widget")
    for want in (
        "【GitHub PR】acme/widget#10 Fix crash",
        "已合并",
        "作者 bob",
        "fix-crash → main · 3 个文件 +10 −2",
    ):
        cases.append((f"PR 卡片含 {want}", want in text, text))

    draft_pull = dict(pull, merged=False, draft=True, state="open")
    cases.append(
        ("草稿 PR 标草稿", "草稿" in github.format_pull_card(draft_pull, "acme/widget"), "")
    )

    # 正文截断上限 600 字符。
    long_issue = dict(issue, body="长" * 2000)
    long_text = github.format_issue_card(long_issue, "acme/widget")
    cases.append(
        (
            "正文被截断",
            len(long_text) < 2000,
            f"{len(long_text)}",
        )
    )
    return cases


def test_release_fetch() -> list[tuple[str, bool, str]]:
    """Release 读取与校验：对应 Diana 的 TestGitHubReleaseFailuresAreNotNegativeEvidence。"""
    import asyncio

    cases: list[tuple[str, bool, str]] = []

    def release_payload(**overrides) -> dict:
        data = {
            "tag_name": "v1.27.3",
            "name": "Gitea 1.27.3",
            "html_url": "https://github.com/go-gitea/gitea/releases/tag/v1.27.3",
            "published_at": "2026-08-29T17:42:17Z",
            "body": "security and bug fixes",
            "draft": False,
            "prerelease": False,
        }
        data.update(overrides)
        return data

    def responder_for(status: int, payload: dict):
        def responder(method, url):
            return {"status": status, "json": payload if status == 200 else {}}

        return responder

    async def fetch(status: int, payload: dict, tag: str = "", latest: bool = True) -> str:
        host = FakeHost(responder=responder_for(status, payload))
        try:
            await github.fetch_release(
                host, "go-gitea", "gitea", "", tag=tag, latest=latest
            )
            return ""
        except common.GitHubError as error:
            return error.message

    # HTTP 失败不能被当成「该版本不存在」的证据。
    for status in (403, 404, 429, 500):
        message = asyncio.run(fetch(status, {}))
        cases.append(
            (
                f"HTTP {status} 不当成版本不存在",
                "不能证明" in message,
                message,
            )
        )

    # 记录不完整 / 不匹配的一律拒绝。
    cases.append(
        (
            "地址指向别的仓库要拒绝",
            "不一致" in asyncio.run(fetch(200, release_payload(html_url="https://github.com/another/repo/releases/tag/v1.27.3"))),
            "",
        )
    )
    cases.append(
        (
            "地址不是 GitHub 要拒绝",
            "不一致" in asyncio.run(fetch(200, release_payload(html_url="https://evil.example/release"))),
            "",
        )
    )
    cases.append(
        (
            "缺发布时间要拒绝",
            "不完整" in asyncio.run(fetch(200, release_payload(published_at=""))),
            "",
        )
    )
    cases.append(
        (
            "草稿不算发布",
            "草稿" in asyncio.run(fetch(200, release_payload(draft=True))),
            "",
        )
    )
    cases.append(
        (
            "latest 不接受预发布",
            "预发布" in asyncio.run(fetch(200, release_payload(prerelease=True))),
            "",
        )
    )
    cases.append(
        (
            "指定版本时预发布照读",
            asyncio.run(fetch(200, release_payload(prerelease=True), tag="v1.27.3", latest=False)) == "",
            "",
        )
    )
    return cases


def test_plugin_flow() -> list[tuple[str, bool, str]]:
    """插件入口：贴链接能回卡片，Release 与 PR 走各自的接口。"""
    import asyncio

    cases: list[tuple[str, bool, str]] = []
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 2,
        "self_id": 999,
        "message": "",
    }

    repo_payload = {
        "full_name": "acme/widget",
        "description": "A small widget toolkit",
        "language": "Go",
        "stargazers_count": 1200,
        "forks_count": 34,
        "open_issues_count": 5,
        "archived": False,
        "fork": False,
        "pushed_at": "2026-09-30T08:00:00Z",
        "topics": ["cli"],
        "license": {"spdx_id": "MIT", "name": "MIT License"},
        "html_url": "https://github.com/acme/widget",
    }

    def repo_responder(method, url):
        return {"status": 200, "json": repo_payload}

    host = FakeHost(responder=repo_responder)
    link_event = dict(event)
    link_event["message"] = "看看 https://github.com/acme/widget"
    asyncio.run(plugin._handle_link(host, link_event, link_event["message"]))
    body = "".join(host.sent)
    cases.append(("仓库链接回卡片", "acme/widget" in body and "★ 1200" in body, body))
    cases.append(
        (
            "仓库走 /repos 接口",
            any("/repos/acme/widget" in url for _m, url in host.requests),
            str(host.requests),
        )
    )

    # PR 必须走 pulls 接口。
    pull_payload = {
        "number": 10,
        "title": "Fix crash",
        "state": "closed",
        "merged": True,
        "draft": False,
        "user": {"login": "bob"},
        "comments": 1,
        "created_at": "2026-09-01T00:00:00Z",
        "labels": [],
        "body": "",
        "changed_files": 3,
        "additions": 10,
        "deletions": 2,
        "head": {"ref": "fix-crash"},
        "base": {"ref": "main"},
        "html_url": "https://github.com/acme/widget/pull/10",
    }

    def pull_responder(method, url):
        if "/pulls/" in url:
            return {"status": 200, "json": pull_payload}
        return {"status": 200, "json": {}}

    host2 = FakeHost(responder=pull_responder)
    pr_event = dict(event)
    pr_event["message"] = "https://github.com/acme/widget/pull/10"
    asyncio.run(plugin._handle_link(host2, pr_event, pr_event["message"]))
    body2 = "".join(host2.sent)
    cases.append(("PR 走 pulls 接口", any("/pulls/10" in url for _m, url in host2.requests), str(host2.requests)))
    cases.append(("PR 卡片含改动规模", "+10 −2" in body2, body2))

    # Release 链接回 Release 卡片。
    release_payload = {
        "tag_name": "v1.27.3",
        "name": "Gitea 1.27.3",
        "html_url": "https://github.com/go-gitea/gitea/releases/tag/v1.27.3",
        "published_at": "2026-08-29T17:42:17Z",
        "body": "security and bug fixes",
        "draft": False,
        "prerelease": False,
    }

    def release_responder(method, url):
        return {"status": 200, "json": release_payload}

    host3 = FakeHost(responder=release_responder)
    rel_event = dict(event)
    rel_event["message"] = "https://github.com/go-gitea/gitea/releases/latest"
    asyncio.run(plugin._handle_link(host3, rel_event, rel_event["message"]))
    body3 = "".join(host3.sent)
    cases.append(("Release 链接回 Release 卡片", "v1.27.3" in body3, body3))
    cases.append(
        (
            "Release 走 releases/latest 接口",
            any("/releases/latest" in url for _m, url in host3.requests),
            str(host3.requests),
        )
    )
    return cases


def test_subscription() -> list[tuple[str, bool, str]]:
    """订阅存储：原子写、标记更新、订阅/取消。"""
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        store = store_mod.Store(path)
        target = {"type": "group", "id": 1}
        is_new = store.subscribe("acme", "widget", target)
        cases.append(("新订阅标记为新", is_new is True, str(is_new)))
        again = store.subscribe("acme", "widget", target)
        cases.append(("重复订阅不新增条目", again is False and len(store.list_for(target)) == 1, str(store.list_for(target))))
        store.update_marks("acme", "widget", release="v1", commit="abc123", issue=42)
        reread = store_mod.Store(path)
        entry = reread.list_for(target)[0]
        marks = entry.get("marks") or {}
        cases.append(
            (
                "标记写进文件",
                marks.get("release") == "v1"
                and marks.get("commit") == "abc123"
                and marks.get("issue") == 42,
                str(entry),
            )
        )
        cases.append(("没有残留 tmp 文件", not os.path.exists(path + ".tmp"), ""))
        reread.unsubscribe("acme", "widget", target)
        cases.append(("取消订阅后清空", not reread.list_for(target), ""))
        cases.append(("仓库条目被移除", not reread.all(), str(reread.all())))
    return cases


def test_repo_options() -> list[tuple[str, bool, str]]:
    """每仓库单独设置：能写、能读、能持久化。"""
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        store = store_mod.Store(path)
        store.subscribe("acme", "widget", {"type": "group", "id": 1})
        cases.append(("能写仓库级设置", store.set_option("acme", "widget", "star", "开"), ""))
        cases.append(("读回仓库级设置", store.option("acme", "widget", "star") == "开", ""))
        cases.append(("布尔设置读成 True", store.option_bool("acme", "widget", "star") is True, ""))
        store.set_option("acme", "widget", "star", "关")
        cases.append(("布尔设置读成 False", store.option_bool("acme", "widget", "star") is False, ""))
        reread = store_mod.Store(path)
        cases.append(("设置持久化", reread.option("acme", "widget", "star") == "关", ""))
        cases.append(("没订阅的仓库写不了", store.set_option("acme", "nope", "star", "开") is False, ""))
        cases.append(("没订阅的仓库读默认", store.option("acme", "nope", "star", "缺省") == "缺省", ""))
    return cases


def test_failure_alert() -> list[tuple[str, bool, str]]:
    """失败告警：连续失败到阈值才报一次，恢复后清零。"""
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        store = store_mod.Store(path)
        store.subscribe("acme", "widget", {"type": "group", "id": 1})
        cases.append(("第 1 次失败不报", store.note_failure("acme", "widget", "超时", 3) is False, ""))
        cases.append(("第 2 次失败不报", store.note_failure("acme", "widget", "超时", 3) is False, ""))
        cases.append(("第 3 次失败才报", store.note_failure("acme", "widget", "超时", 3) is True, ""))
        cases.append(("同一轮故障不再报", store.note_failure("acme", "widget", "超时", 3) is False, ""))
        cases.append(("阈值 0 表示不报", store.note_failure("acme", "widget", "超时", 0) is False, ""))
        cases.append(("恢复时提示一次", store.clear_failure("acme", "widget") is True, ""))
        cases.append(("恢复后不再提示", store.clear_failure("acme", "widget") is False, ""))
        cases.append(("恢复后重新计数", store.note_failure("acme", "widget", "超时", 1) is True, ""))
    return cases


def test_notification_template() -> list[tuple[str, bool, str]]:
    """通知模板：占位符替换、空值整行删除、分割标记。"""
    cases: list[tuple[str, bool, str]] = []

    text = github.render_template(
        "GitHub 动态：{repository}\n{body}", {"repository": "acme/widget", "body": "PR #3"}
    )
    cases.append(("占位符替换", text == "GitHub 动态：acme/widget\nPR #3", repr(text)))

    text = github.render_template(
        "作者：{author}\n标题：{title}", {"author": "", "title": "修复崩溃"}
    )
    cases.append(("空占位符整行删除", text == "标题：修复崩溃", repr(text)))

    text = github.render_template("静态行\n{empty}", {"empty": ""})
    cases.append(("静态行原样保留", text == "静态行", repr(text)))

    # 分割标记：单独一行写它就从那里分成下一条。
    chunked = plugin._split_notification(
        "第一段\n[diana-msg]\n第二段"
    )
    cases.append(("按标记拆成两条", len(chunked) == 2 and chunked[0] == "第一段", str(chunked)))

    single = plugin._split_notification("只有一条")
    cases.append(("没有标记就一条", len(single) == 1, str(single)))

    # 默认模板
    default = plugin._render_notification("", "acme/widget", "PR #3")
    cases.append(("默认模板带仓库名", "acme/widget" in default and "PR #3" in default, default))
    return cases


def test_config_defaults() -> list[tuple[str, bool, str]]:
    """配置读值：0 和空串按「没填」处理，开关没存过用插件默认值。"""
    cases: list[tuple[str, bool, str]] = []

    host = FakeHost()
    host.config = {"poll_interval": 0, "max_notify_per_poll": 0}
    cases.append(
        (
            "轮询间隔 0 走默认值 300",
            plugin._int_config(host, "poll_interval", 300, 30, 3600) == 300,
            str(plugin._int_config(host, "poll_interval", 300, 30, 3600)),
        )
    )
    cases.append(
        (
            "推送条数 0 走默认值 5",
            plugin._int_config(host, "max_notify_per_poll", 5, 1, 50) == 5,
            str(plugin._int_config(host, "max_notify_per_poll", 5, 1, 50)),
        )
    )

    host.config = {"poll_interval": 60}
    cases.append(
        (
            "填了 60 就用 60",
            plugin._int_config(host, "poll_interval", 300, 30, 3600) == 60,
            str(plugin._int_config(host, "poll_interval", 300, 30, 3600)),
        )
    )

    host.config = {}
    cases.append(
        (
            "开关没存过用插件默认值",
            plugin._switch_config(host, "watch_star", False) is False,
            str(plugin._switch_config(host, "watch_star", False)),
        )
    )
    host.config = {"watch_star": True}
    cases.append(
        (
            "开关开了就是开",
            plugin._switch_config(host, "watch_star", False) is True,
            str(plugin._switch_config(host, "watch_star", False)),
        )
    )
    host.config = {"watch_star": False}
    cases.append(
        (
            "开关关了就是关",
            plugin._switch_config(host, "watch_star", True) is False,
            str(plugin._switch_config(host, "watch_star", True)),
        )
    )
    return cases


def test_manifest_config() -> list[tuple[str, bool, str]]:
    """清单里的配置项：数量不超上限、类型受支持、键名合法。"""
    import json as _json

    cases: list[tuple[str, bool, str]] = []
    manifest = _json.loads(
        (Path(r"D:\test\github-plugin\manifest.json")).read_text(encoding="utf-8")
    )
    schema = manifest["config_schema"]
    cases.append(("配置项不超过 50 条", len(schema) <= 50, str(len(schema))))
    allowed_types = {"text", "password", "number", "switch", "select"}
    bad_types = [item["key"] for item in schema if item["type"] not in allowed_types]
    cases.append(("类型都在宿主白名单内", not bad_types, str(bad_types)))
    bad_keys = [
        item["key"]
        for item in schema
        if not item["key"].replace("_", "").isalnum() or not item["key"][0].isalpha()
    ]
    cases.append(("键名符合宿主规则", not bad_keys, str(bad_keys)))
    keys = [item["key"] for item in schema]
    cases.append(("键名不重复", len(keys) == len(set(keys)), str(keys)))
    cases.append(
        (
            "面板上带每类条数设置",
            "items_per_type" in keys,
            str(keys),
        )
    )
    cases.append(("面板上带推送模板", "notify_template" in keys, str(keys)))
    cases.append(("面板上带失败阈值", "failure_threshold" in keys, str(keys)))
    return cases


def test_poll_flow() -> list[tuple[str, bool, str]]:
    """轮询一轮：新动态能推出去，条数上限生效，模板能改格式。"""
    import asyncio
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        original_init = store_mod.Store.__init__

        def patched_init(self, path=None):
            original_init(self, state_path)

        store_mod.Store.__init__ = patched_init
        try:
            store = store_mod.Store()
            store.subscribe("acme", "widget", {"type": "group", "id": 1})

            def responder(method, url):
                if "/releases" in url:
                    return {
                        "status": 200,
                        "json": [
                            {
                                "tag_name": "v2.0.0",
                                "name": "二点零",
                                "published_at": "2026-10-20T10:00:00Z",
                                "prerelease": False,
                                "draft": False,
                                "html_url": "https://github.com/acme/widget/releases/tag/v2.0.0",
                            },
                            {
                                "tag_name": "v1.9.0",
                                "name": "一点九",
                                "published_at": "2026-10-19T10:00:00Z",
                                "prerelease": False,
                                "draft": False,
                                "html_url": "https://github.com/acme/widget/releases/tag/v1.9.0",
                            },
                        ],
                    }
                if "/pulls" in url:
                    return {"status": 200, "json": []}
                if "/issues" in url:
                    return {"status": 200, "json": []}
                if "/commits" in url:
                    return {"status": 200, "json": []}
                if "/events" in url:
                    return {"status": 200, "json": []}
                return {"status": 200, "json": {}}

            host = FakeHost(responder=responder)
            host.config = {"items_per_type": 1, "watch_star": False}
            asyncio.run(plugin._poll_once(host))
            body = "".join(host.sent)
            cases.append(("新 Release 被推送", "v2.0.0" in body, body))
            cases.append(("推送带仓库名", "acme/widget" in body, body))
            cases.append(
                (
                    "每类条数上限生效（只推 1 条）",
                    "v1.9.0" not in body and "另有 1 条 Release 未列出" in body,
                    body,
                )
            )

            # 第二轮：游标已推进，同一个版本不再重复推。
            host.sent.clear()
            asyncio.run(plugin._poll_once(host))
            cases.append(("同一版本不重复推送", "v2.0.0" not in "".join(host.sent), "".join(host.sent)))
        finally:
            store_mod.Store.__init__ = original_init
    return cases


def test_conf_command() -> list[tuple[str, bool, str]]:
    """gh conf：能查、能改，改完能读回来。"""
    import asyncio
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 2,
        "self_id": 999,
        "message": "",
    }
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        original_init = store_mod.Store.__init__

        def patched_init(self, path=None):
            original_init(self, state_path)

        store_mod.Store.__init__ = patched_init
        try:
            store = store_mod.Store()
            store.subscribe("acme", "widget", {"type": "group", "id": 1})

            host = FakeHost()
            asyncio.run(
                plugin._handle_command(host, event, "gh", "gh conf acme/widget star 开")
            )
            body = "".join(host.sent)
            cases.append(("设置有回执", "Star" in body or "star" in body, body))
            # 插件内部新建了 Store，测试这份是旧缓存，重新读盘才准。
            fresh = store_mod.Store()
            cases.append(
                (
                    "设置真的写进去了",
                    fresh.option("acme", "widget", "star") == "开",
                    fresh.option("acme", "widget", "star"),
                )
            )

            host.sent.clear()
            asyncio.run(plugin._handle_command(host, event, "gh", "gh conf acme/widget"))
            body2 = "".join(host.sent)
            cases.append(("不带值时能查回设置", "开" in body2, body2))

            host.sent.clear()
            asyncio.run(
                plugin._handle_command(host, event, "gh", "gh conf acme/widget 乱填 关")
            )
            body3 = "".join(host.sent)
            cases.append(("非法键名被拒", "可设置的键" in body3, body3))

            host.sent.clear()
            asyncio.run(plugin._handle_command(host, event, "gh", "gh log"))
            body4 = "".join(host.sent)
            cases.append(("gh log 有回执", "运行记录" in body4, body4))
        finally:
            store_mod.Store.__init__ = original_init
    return cases


def test_credentials() -> list[tuple[str, bool, str]]:
    """凭据管理：增删改查、默认凭据、仓库绑定优先级。"""
    import os
    import tempfile

    from gparse import credentials

    cases: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        original = credentials._path
        credentials._path = lambda: os.path.join(tmp, "credentials.json")
        try:
            cases.append(("初始没有凭据", credentials.names() == [], str(credentials.names())))
            cases.append(("添加凭据", credentials.put("工作", "token-a") is True, ""))
            cases.append(("凭据能读回", credentials.get("工作") == "token-a", credentials.get("工作")))
            cases.append(("第一条自动成为默认", credentials.default_name() == "工作", credentials.default_name()))
            credentials.put("私人", "token-b")
            cases.append(("凭据名不重复", len(credentials.names()) == 2, str(credentials.names())))
            cases.append(("切换默认", credentials.set_default("私人") is True, ""))
            cases.append(("默认已切换", credentials.default_name() == "私人", credentials.default_name()))
            cases.append(("设不存在的默认被拒", credentials.set_default("没有") is False, ""))
            cases.append(("删掉不存在的被拒", credentials.remove("没有") is False, ""))
            cases.append(("删除凭据", credentials.remove("私人") is True, ""))
            cases.append(("删除后只剩一条", len(credentials.names()) == 1, str(credentials.names())))
            cases.append(("删掉默认后自动换一个", credentials.default_name() == "工作", credentials.default_name()))

            # 仓库绑定优先于默认凭据
            cases.append(
                (
                    "仓库绑定的凭据优先",
                    credentials.resolve("工作", "面板token") == "token-a",
                    credentials.resolve("工作", "面板token"),
                )
            )
            cases.append(
                (
                    "没绑定就用默认凭据",
                    credentials.resolve("", "面板token") == "token-a",
                    credentials.resolve("", "面板token"),
                )
            )
            # 一条凭据都不剩时，才轮到面板上填的那个 Token。
            credentials.remove("工作")
            cases.append(
                (
                    "没有凭据时用面板的 Token",
                    credentials.resolve("", "面板token") == "面板token",
                    credentials.resolve("", "面板token"),
                )
            )
            cases.append(
                (
                    "什么都不填就是空",
                    credentials.resolve("", "") == "",
                    credentials.resolve("", ""),
                )
            )
        finally:
            credentials._path = original
    return cases


def test_private_mask() -> list[tuple[str, bool, str]]:
    """私库打码：私有仓库不写路径、账号名和链接。"""
    import asyncio
    import os
    import tempfile

    cases: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        original_init = store_mod.Store.__init__

        def patched_init(self, path=None):
            original_init(self, state_path)

        store_mod.Store.__init__ = patched_init
        try:
            store = store_mod.Store()
            store.subscribe("acme", "secret", {"type": "group", "id": 1})

            def responder(method, url):
                if url.endswith("/repos/acme/secret"):
                    return {
                        "status": 200,
                        "json": {"description": "私有项目", "private": True},
                    }
                if "/releases" in url:
                    return {
                        "status": 200,
                        "json": [
                            {
                                "tag_name": "v1.0.0",
                                "name": "一",
                                "published_at": "2026-10-20T10:00:00Z",
                                "prerelease": False,
                                "draft": False,
                                "html_url": "https://github.com/acme/secret/releases/tag/v1.0.0",
                            }
                        ],
                    }
                return {"status": 200, "json": []}

            host = FakeHost(responder=responder)
            host.config = {"private_mask": True}
            asyncio.run(plugin._poll_once(host))
            body = "".join(host.sent)
            cases.append(("私库通知不含仓库路径", "acme/secret" not in body, body))
            cases.append(("私库通知不含链接", "github.com/acme/secret" not in body, body))
            cases.append(("私库通知仍说明发生了什么", "v1.0.0" in body, body))
        finally:
            store_mod.Store.__init__ = original_init
    return cases


def main() -> int:
    groups = [
        ("链接识别", test_link_recognition()),
        ("Release 链接", test_release_link()),
        ("卡片内容", test_cards()),
        ("Release 读取校验", test_release_fetch()),
        ("插件流程", test_plugin_flow()),
        ("订阅存储", test_subscription()),
        ("仓库级设置", test_repo_options()),
        ("失败告警", test_failure_alert()),
        ("通知模板", test_notification_template()),
        ("配置默认值", test_config_defaults()),
        ("清单配置项", test_manifest_config()),
        ("轮询流程", test_poll_flow()),
        ("gh conf 指令", test_conf_command()),
        ("凭据管理", test_credentials()),
        ("私库打码", test_private_mask()),
    ]
    failed = 0
    total = 0
    for title, cases in groups:
        failed += run(cases, title)
        total += len(cases)
    print()
    print(f"共 {total} 条断言，失败 {failed} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
