"""
GitHub 订阅与解析插件。

功能：
1. 群里或私聊里贴 github.com 链接（仓库 / Issue / PR），机器人回一条解析卡片；
2. 发送「gh sub owner/repo」订阅仓库，「gh unsub owner/repo」取消，「gh list」查看；
3. 后台按间隔轮询订阅仓库，有新 Release 或新提交时主动推送到订阅所在的群或私聊。

约定：
- 出网一律走 host.http_request，域名受清单白名单约束；
- 解析失败回一条中文提示，不向宿主抛裸异常；
- 订阅列表用本地 JSON 文件持久化，不依赖宿主存储。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_message

from gparse import common, github
from gparse.common import GitHubError


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SUB_FILE = os.path.join(PLUGIN_DIR, "subscriptions.json")

DEFAULT_PREFIX = "gh"
DEFAULT_POLL = 300
DEFAULT_MAX_NOTIFY = 5
MAX_TITLE_LENGTH = 80

# 模块级宿主句柄：后台轮询循环复用首次事件拿到的 host。
_host: Any = None
_poller_started = False

# 仓库坐标：owner/repo
REPO_SPEC = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")


def _text_config(host: Any, key: str, default: str) -> str:
    value = host.config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _choice(host: Any, key: str, default: str) -> str:
    return _text_config(host, key, default)


def _int_config(host: Any, key: str, default: int, low: int, high: int) -> int:
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _message_text(event: dict[str, Any]) -> str:
    """取出纯文本消息。"""
    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                parts.append(str((segment.get("data") or {}).get("text") or ""))
        return "".join(parts)
    return str(event.get("raw_message") or "")


def _target_of(event: dict[str, Any]) -> dict[str, Any]:
    """当前会话标识，用作订阅目标。"""
    if event.get("message_type") == "group":
        return {"type": "group", "id": event.get("group_id")}
    return {"type": "private", "id": event.get("user_id")}


async def _reply(host: Any, event: dict[str, Any], text: str) -> None:
    if event.get("message_type") == "group":
        await host.send_group_msg(event.get("group_id"), text)
    else:
        await host.send_private_msg(event.get("user_id"), text)


async def _safe_reply(host: Any, event: dict[str, Any], text: str, label: str) -> None:
    try:
        await _reply(host, event, text)
    except PluginCallError as error:
        host.log(f"{label}失败：{error}", "warning")


async def _handle_link(host: Any, event: dict[str, Any], text: str) -> bool:
    """识别 GitHub 链接并回解析卡片；不是 GitHub 链接返回 False。"""
    target = common.find_github_target(text)
    if target is None:
        return False
    token = _text_config(host, "github_token", "")
    try:
        if target["kind"] in ("issue", "pr"):
            info = await github.fetch_issue(
                host, target["owner"], target["repo"], target["number"], token
            )
            info["full_name"] = f"{target['owner']}/{target['repo']}"
            await _safe_reply(host, event, github.format_issue_card(info), "解析 GitHub")
        else:
            info = await github.fetch_repo(host, target["owner"], target["repo"], token)
            await _safe_reply(host, event, github.format_repo_card(info), "解析 GitHub")
    except GitHubError as error:
        await _safe_reply(
            host,
            event,
            f"解析失败：{error.message}" + (f"\n{error.hint}" if error.hint else ""),
            "解析失败提示",
        )
    except PluginCallError as error:
        host.log(f"出网被拒或失败：{error}", "error")
        await _safe_reply(host, event, "GitHub 服务暂时不可用，请稍后再试。", "服务提示")
    return True


def _parse_command(text: str) -> tuple[str, str]:
    """把首行指令拆成 (动作, 参数)。返回 ("", "") 表示不是指令。"""
    head = text.strip().lstrip("。.,!?！？~ 　")
    parts = head.split(None, 1)
    if len(parts) < 2:
        return parts[0] if parts else "", ""
    return parts[0], parts[1].strip()


async def _handle_command(
    host: Any, event: dict[str, Any], prefix: str, arg: str
) -> bool:
    """处理 gh sub / unsub / list / help。不是 gh 指令返回 False。"""
    if not arg.startswith(prefix):
        return False
    rest = arg[len(prefix):].strip()
    if not rest:
        return False
    action, params = _parse_command(rest)
    action = action.lower()
    store = github.SubscriptionStore(SUB_FILE)
    target = _target_of(event)

    if action in ("sub", "subscribe"):
        m = REPO_SPEC.match(params.strip())
        if not m:
            await _safe_reply(host, event, "用法：gh sub owner/repo", "指令提示")
            return True
        is_new = store.subscribe(m.group(1), m.group(2), target)
        # 立即校准一次标记，避免下一轮轮询把历史 Release 当新消息。
        await _calibrate(host, store, m.group(1), m.group(2), target)
        msg = f"已订阅 {m.group(1)}/{m.group(2)}" + ("" if is_new else "（追加到当前会话）")
        await _safe_reply(host, event, msg, "订阅成功")
        return True

    if action in ("unsub", "unsubscribe"):
        m = REPO_SPEC.match(params.strip())
        if not m:
            await _safe_reply(host, event, "用法：gh unsub owner/repo", "指令提示")
            return True
        still = store.unsubscribe(m.group(1), m.group(2), target)
        msg = (
            f"已取消本会话对 {m.group(1)}/{m.group(2)} 的订阅"
            + ("" if still else "（该仓库已无其他订阅，彻底移除）")
        )
        await _safe_reply(host, event, msg, "取消订阅")
        return True

    if action in ("list", "ls"):
        entries = store.list_for(target)
        if not entries:
            await _safe_reply(host, event, "当前会话还没有订阅任何仓库。", "列表为空")
            return True
        lines = ["当前会话订阅的仓库："]
        for e in entries:
            lines.append(f"- {e['owner']}/{e['repo']}")
        await _safe_reply(host, event, "\n".join(lines), "订阅列表")
        return True

    if action in ("help", "h", "?"):
        await _safe_reply(host, event, _help_text(prefix), "帮助")
        return True

    return True


async def _calibrate(
    host: Any, store: github.SubscriptionStore, owner: str, repo: str, target: dict[str, Any]
) -> None:
    """订阅时把当前 Release/Commit 标记设为最新，后续只推真正的新内容。"""
    token = _text_config(host, "github_token", "")
    try:
        rel = await github.fetch_latest_release(host, owner, repo, token)
        com = await github.fetch_latest_commit(host, owner, repo, token)
    except PluginCallError:
        return
    store.update_marks(
        owner, repo, rel["tag"] if rel else "", com["sha"] if com else ""
    )


def _help_text(prefix: str) -> str:
    return (
        "GitHub 插件用法：\n"
        f"- 贴 github.com 链接：自动解析仓库 / Issue / PR\n"
        f"- {prefix} sub owner/repo：订阅仓库更新\n"
        f"- {prefix} unsub owner/repo：取消订阅\n"
        f"- {prefix} list：查看本会话订阅\n"
        f"- {prefix} help：显示本帮助"
    )


async def _poll_once(host: Any) -> None:
    """轮询一轮所有订阅，把新更新推送到对应会话。"""
    token = _text_config(host, "github_token", "")
    max_notify = _int_config(host, "max_notify_per_poll", DEFAULT_MAX_NOTIFY, 1, 50)
    store = github.SubscriptionStore(SUB_FILE)
    entries = store.all()
    sent = 0
    for entry in entries:
        if sent >= max_notify:
            break
        owner, repo = entry["owner"], entry["repo"]
        try:
            rel = await github.fetch_latest_release(host, owner, repo, token)
            com = await github.fetch_latest_commit(host, owner, repo, token)
        except PluginCallError as error:
            host.log(f"轮询 {owner}/{repo} 失败：{error}", "warning")
            continue

        updates: list[str] = []
        if rel and rel["tag"] and rel["tag"] != entry.get("last_release"):
            updates.append(
                f"【新 Release】{owner}/{repo}\n{rel['name']} ({rel['tag']})\n{rel['html_url']}"
            )
        if com and com["sha"] and com["sha"] != entry.get("last_commit"):
            updates.append(
                f"【新提交】{owner}/{repo}\n{common.clip(com['message'], MAX_TITLE_LENGTH)}"
                f" — {com['author']}\n{com['html_url']}"
            )

        store.update_marks(
            owner, repo, rel["tag"] if rel else "", com["sha"] if com else ""
        )
        for text in updates:
            for t in entry["targets"]:
                if t["type"] == "group":
                    await _safe_reply(
                        host,
                        {"message_type": "group", "group_id": t["id"]},
                        text,
                        "推送 GitHub 更新",
                    )
                else:
                    await _safe_reply(
                        host,
                        {"message_type": "private", "user_id": t["id"]},
                        text,
                        "推送 GitHub 更新",
                    )
                sent += 1
                if sent >= max_notify:
                    break
        # 轻量限速：匿名额度有限，轮询之间留点间隔。
        await asyncio.sleep(1.5)


async def _poll_loop(host: Any) -> None:
    interval = _int_config(host, "poll_interval", DEFAULT_POLL, 30, 3600)
    while True:
        await asyncio.sleep(interval)
        try:
            await _poll_once(host)
        except Exception as error:  # 轮询自身不能把插件带崩
            host.log(f"GitHub 轮询异常：{type(error).__name__}: {error}", "error")


def _ensure_poller(host: Any) -> None:
    """首次处理事件时启动后台轮询循环（只启一次）。"""
    global _host, _poller_started
    _host = host
    if _poller_started:
        return
    _poller_started = True
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_poll_loop(host))
        host.log("GitHub 订阅轮询已启动", "info")
    except RuntimeError:
        host.log("无法启动后台轮询（当前环境无事件循环）", "warning")


@on_message
async def handle_github(event: dict[str, Any], host: Any) -> None:
    """收到消息时判断是 GitHub 链接还是订阅指令。"""
    global _host
    _host = host
    if event.get("post_type") != "message":
        return
    if str(event.get("user_id")) == str(event.get("self_id")):
        return

    scope = _choice(host, "reply_scope", "群聊与私聊")
    is_group = event.get("message_type") == "group"
    if (scope == "仅群聊" and not is_group) or (scope == "仅私聊" and is_group):
        return

    text = _message_text(event)
    if not text:
        return

    prefix = _text_config(host, "trigger_prefix", DEFAULT_PREFIX).strip() or DEFAULT_PREFIX

    if not host.has("msg.send"):
        host.log("缺少 msg.send 能力", "error")
        return
    if not host.has("http.outbound"):
        host.log("缺少 http.outbound 能力", "error")
        await _reply(host, event, "本机器人未获批「访问外网」能力，GitHub 功能不可用。")
        return

    _ensure_poller(host)

    # 先试指令，再试链接；都不是就当普通消息忽略。
    if await _handle_command(host, event, prefix, text):
        return
    await _handle_link(host, event, text)
