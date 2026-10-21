"""
GitHub 订阅与解析插件。

功能：
1. 群里或私聊里贴 github.com 链接（仓库 / Issue / PR / Release），机器人回一条解析卡片；
2. 发送「gh sub owner/repo」订阅仓库，「gh unsub owner/repo」取消，「gh list」查看；
3. 「gh conf owner/repo 键 值」单独设置某个仓库（显示名、监控项、脱敏等）；
4. 「gh log」查看最近的运行记录；
5. 后台按间隔轮询订阅仓库，有新 Release、新提交、新 Issue/PR 或 Star 变化时主动推送。

解析行为对齐 SuInk/Diana 的 GitHub 解析规则（用 Python 重新实现）：
- 保留 owner 黑名单、名字字符集校验、.git 后缀剥离；
- Issue/PR 走各自接口，PR 额外给出合并状态与改动规模；
- Release 只读官方 API，并核对返回地址与请求的仓库、版本一致。

通知行为同样对齐 Diana 的仓库订阅：
- 每类动态各自有条数上限，超出的注明还剩多少条未列出；
- 模板可用 {repository} {summary} {body}，单独一行写 [diana-msg] 表示从那里分成下一条；
- 连续失败到阈值才在群里说一声，一轮故障只报一次，恢复后再说一声好了。

约定：
- 出网一律走 host.http_request，域名受清单白名单约束；
- 解析失败回一条中文提示，不向宿主抛裸异常；
- 订阅列表存在宿主的数据目录下，插件升级不会丢。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_message

from gparse import common, github
from gparse import credentials
from gparse import store as store_mod
from gparse.common import GitHubError


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SUB_FILE = os.path.join(PLUGIN_DIR, "subscriptions.json")

DEFAULT_PREFIX = "gh"
DEFAULT_POLL = 300
DEFAULT_MAX_NOTIFY = 5
DEFAULT_ITEMS_PER_TYPE = 10
DEFAULT_CHECK_TIMEOUT = 20
DEFAULT_FAILURE_THRESHOLD = 3
MAX_TITLE_LENGTH = 80

# 通知模板里的分割标记：单独一行写它，就从那里分成下一条消息发送。
SPLIT_MARKER = "[diana-msg]"

# 后台轮询循环在宿主里只启动一次，用这个开关记住。
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
    """
    读一个数字型配置项。

    宿主对 number 类型的默认值是 0（未保存过的配置项在面板里就显示 0），
    所以这里的 0 和空串都按「没填」处理，直接用插件自己的默认值。
    否则填 0 会被夹到下限，轮询间隔会变成 30 秒、每次推送条数变成 1 条。
    """
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return max(low, min(value, high))


def _switch_config(host: Any, key: str, default: bool) -> bool:
    """
    读一个开关型配置项。

    宿主对 switch 类型的默认值也是 False，和「用户明确关掉」分不开。
    所以这里只在配置里确实存了值时才按它走，没存过就用插件自己的默认值。
    """
    raw = host.config.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return default
    return store_mod.parse_bool(text, default)


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
    token = _text_config(host, "github_token", "")

    # 先认 Release：它的路径最具体（/releases…），别被当成仓库链接。
    release = common.find_release_target(text)
    if release is not None:
        try:
            info = await github.fetch_release(
                host,
                release["owner"],
                release["repo"],
                token,
                tag=release["tag"],
                latest=release["mode"] == "latest",
            )
            await _safe_reply(
                host, event, github.format_release_card(info), "解析 GitHub Release"
            )
        except GitHubError as error:
            await _safe_reply(
                host,
                event,
                f"解析失败：{error.message}" + (f"\n{error.hint}" if error.hint else ""),
                "Release 解析失败提示",
            )
        except PluginCallError as error:
            host.log(f"出网被拒或失败：{error}", "error")
            await _safe_reply(host, event, "GitHub 服务暂时不可用，请稍后再试。", "服务提示")
        return True

    target = common.find_github_target(text)
    if target is None:
        return False
    full_name = f"{target['owner']}/{target['repo']}"
    try:
        if target["kind"] == "pr":
            info = await github.fetch_pull(
                host, target["owner"], target["repo"], target["number"], token
            )
            await _safe_reply(
                host, event, github.format_pull_card(info, full_name), "解析 GitHub PR"
            )
        elif target["kind"] == "issue":
            info = await github.fetch_issue(
                host, target["owner"], target["repo"], target["number"], token
            )
            await _safe_reply(
                host, event, github.format_issue_card(info, full_name), "解析 GitHub Issue"
            )
        else:
            info = await github.fetch_repo(host, target["owner"], target["repo"], token)
            await _safe_reply(
                host, event, github.format_repo_card(info), "解析 GitHub 仓库"
            )
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
    """处理 gh sub / unsub / list / conf / log / help。不是 gh 指令返回 False。"""
    if not arg.startswith(prefix):
        return False
    rest = arg[len(prefix):].strip()
    if not rest:
        return False
    action, params = _parse_command(rest)
    action = action.lower()
    store = store_mod.Store()
    target = _target_of(event)

    if action in ("sub", "subscribe"):
        m = REPO_SPEC.match(params.strip())
        if not m:
            await _safe_reply(host, event, "用法：gh sub owner/repo", "指令提示")
            return True
        is_new = store.subscribe(m.group(1), m.group(2), target)
        # 立即校准一次标记，避免下一轮轮询把历史 Release/提交/Issue 当新消息。
        await _calibrate(host, store, m.group(1), m.group(2))
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
            options = e.get("options") or {}
            extra = "".join(
                f" · {store_mod.REPO_OPTION_KEYS.get(k, k)}={v}"
                for k, v in options.items()
                if str(v).strip()
            )
            lines.append(f"- {e['owner']}/{e['repo']}{extra}")
        await _safe_reply(host, event, "\n".join(lines), "订阅列表")
        return True

    if action in ("conf", "config", "set"):
        await _handle_conf(host, event, store, params)
        return True

    if action in ("cred", "credential", "token"):
        await _handle_cred(host, event, params)
        return True

    if action in ("log", "logs", "record"):
        records = store.logs(20)
        if not records:
            await _safe_reply(host, event, "还没有运行记录。", "运行记录为空")
            return True
        lines = ["最近的运行记录："]
        for item in records:
            lines.append(f"[{item['time']}] {item['kind']} {item['repo']} {item['text']}".rstrip())
        await _safe_reply(host, event, "\n".join(lines), "运行记录")
        return True

    if action in ("help", "h", "?"):
        await _safe_reply(host, event, _help_text(prefix), "帮助")
        return True

    return True


async def _handle_conf(
    host: Any, event: dict[str, Any], store: Any, params: str
) -> None:
    """处理 gh conf：查看或修改某个仓库的单独设置。"""
    parts = params.strip().split(None, 2)
    if not parts:
        await _safe_reply(
            host,
            event,
            "用法：gh conf owner/repo [键 值]\n可设置的键："
            + "、".join(f"{k}（{label}）" for k, label in store_mod.REPO_OPTION_KEYS.items()),
            "设置提示",
        )
        return
    m = REPO_SPEC.match(parts[0])
    if not m:
        await _safe_reply(host, event, "用法：gh conf owner/repo [键 值]", "设置提示")
        return
    owner, repo = m.group(1), m.group(2)
    if not store.get(owner, repo):
        await _safe_reply(
            host, event, f"还没有订阅 {owner}/{repo}，先发「gh sub {owner}/{repo}」。", "设置提示"
        )
        return

    if len(parts) < 3:
        options = store.get(owner, repo).get("options") or {}
        if not options:
            await _safe_reply(host, event, f"{owner}/{repo} 还没有单独设置。", "设置查询")
            return
        lines = [f"{owner}/{repo} 的单独设置："]
        for k, v in options.items():
            lines.append(f"- {store_mod.REPO_OPTION_KEYS.get(k, k)}：{v}")
        await _safe_reply(host, event, "\n".join(lines), "设置查询")
        return

    key, value = parts[1].strip().lower(), parts[2].strip()
    if key not in store_mod.REPO_OPTION_KEYS:
        await _safe_reply(
            host,
            event,
            "可设置的键："
            + "、".join(f"{k}（{label}）" for k, label in store_mod.REPO_OPTION_KEYS.items()),
            "设置提示",
        )
        return
    store.set_option(owner, repo, key, value)
    await _safe_reply(
        host,
        event,
        f"已设置 {owner}/{repo} 的{store_mod.REPO_OPTION_KEYS[key]}：{value}",
        "设置成功",
    )


async def _handle_cred(host: Any, event: dict[str, Any], params: str) -> None:
    """
    处理 gh cred：管理 GitHub 凭据。

    - gh cred list：列出凭据名字（不显示 Token）
    - gh cred add 名称 Token：新增或覆盖一条凭据
    - gh cred rm 名称：删掉一条凭据
    - gh cred default 名称：设成默认凭据
    - gh cred check 名称：拿这条凭据查一次账号，验证能不能用
    """
    parts = params.strip().split(None, 2)
    action = parts[0].lower() if parts else "list"
    arg = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""

    if action in ("list", "ls"):
        items = credentials.names()
        default = credentials.default_name()
        if not items:
            await _safe_reply(
                host,
                event,
                "还没有添加凭据。发送「gh cred add 名称 Token」添加，"
                "不添加也能用（匿名 60 次/小时）。",
                "凭据列表",
            )
            return True
        lines = ["已配置的凭据："]
        for name in items:
            lines.append(f"- {name}" + ("（默认）" if name == default else ""))
        await _safe_reply(host, event, "\n".join(lines), "凭据列表")
        return True

    if action in ("add", "put", "set"):
        name, token = arg, rest.strip()
        if not name or not token:
            await _safe_reply(
                host, event, "用法：gh cred add 名称 Token", "凭据提示"
            )
            return True
        if credentials.put(name, token):
            await _safe_reply(host, event, f"已保存凭据「{name}」。", "凭据保存")
        else:
            await _safe_reply(host, event, "用法：gh cred add 名称 Token", "凭据提示")
        return True

    if action in ("rm", "remove", "del", "delete"):
        if not arg:
            await _safe_reply(host, event, "用法：gh cred rm 名称", "凭据提示")
            return True
        if credentials.remove(arg):
            await _safe_reply(host, event, f"已删除凭据「{arg}」。", "凭据删除")
        else:
            await _safe_reply(host, event, f"没有名为「{arg}」的凭据。", "凭据删除")
        return True

    if action in ("default", "use"):
        if not arg:
            await _safe_reply(host, event, "用法：gh cred default 名称", "凭据提示")
            return True
        if credentials.set_default(arg):
            await _safe_reply(host, event, f"默认凭据改为「{arg}」。", "凭据默认")
        else:
            await _safe_reply(host, event, f"没有名为「{arg}」的凭据。", "凭据默认")
        return True

    if action in ("check", "test", "whoami"):
        token = credentials.get(arg) if arg else _text_config(host, "github_token", "")
        if not token:
            await _safe_reply(
                host, event, "没有可用的凭据。先添加一条，或在面板上填 GitHub Token。", "凭据检查"
            )
            return True
        try:
            account = await github.fetch_account(host, token)
        except GitHubError as error:
            await _safe_reply(
                host, event, f"凭据不可用：{error.message}", "凭据检查"
            )
            return True
        except PluginCallError as error:
            host.log(f"凭据检查出网失败：{error}", "warning")
            await _safe_reply(host, event, "GitHub 服务暂时不可用，请稍后再试。", "凭据检查")
            return True
        await _safe_reply(
            host, event, f"凭据可用，当前账号：{account or '（未提供账号名）'}", "凭据检查"
        )
        return True

    await _safe_reply(
        host,
        event,
        "用法：gh cred list | add 名称 Token | rm 名称 | default 名称 | check 名称",
        "凭据提示",
    )
    return True


async def _calibrate(host: Any, store: Any, owner: str, repo: str) -> None:
    """订阅时把当前 Release/Commit 标记设为最新，后续只推真正的新内容。"""
    token = _text_config(host, "github_token", "")
    try:
        rel = await github.fetch_latest_release(host, owner, repo, token)
        com = await github.fetch_latest_commit(host, owner, repo, token)
        issues = await github.fetch_latest_issues(host, owner, repo, token)
    except PluginCallError:
        return
    max_issue = max((int(it.get("number") or 0) for it in issues), default=0)
    store.update_marks(
        owner,
        repo,
        release=rel["tag"] if rel else "",
        commit=com["sha"] if com else "",
        issue=max_issue,
    )


def _help_text(prefix: str) -> str:
    return (
        "GitHub 插件用法：\n"
        f"- 贴 github.com 链接：自动解析仓库 / Issue / PR / Release\n"
        f"- {prefix} sub owner/repo：订阅仓库更新\n"
        f"- {prefix} unsub owner/repo：取消订阅\n"
        f"- {prefix} list：查看本会话订阅\n"
        f"- {prefix} conf owner/repo [键 值]：单独设置某个仓库\n"
        f"- {prefix} cred list/add/rm/default/check：管理 GitHub 凭据\n"
        f"- {prefix} log：查看最近的运行记录\n"
        f"- {prefix} help：显示本帮助"
    )


async def _poll_once(host: Any) -> None:
    """轮询一轮所有订阅，把新更新推送到对应会话。"""
    token = _text_config(host, "github_token", "")
    max_notify = _int_config(host, "max_notify_per_poll", DEFAULT_MAX_NOTIFY, 1, 50)
    per_type = _int_config(host, "items_per_type", DEFAULT_ITEMS_PER_TYPE, 1, 30)
    timeout = _int_config(host, "check_timeout", DEFAULT_CHECK_TIMEOUT, 5, 30)
    threshold = _int_config(host, "failure_threshold", DEFAULT_FAILURE_THRESHOLD, 0, 20)
    send_error = _switch_config(host, "send_error_notice", True)
    template = _text_config(host, "notify_template", "")
    store = store_mod.Store()
    entries = store.all()
    sent = 0
    for entry in entries:
        if sent >= max_notify:
            break
        owner, repo = entry["owner"], entry["repo"]
        full_name = f"{owner}/{repo}"
        # 每仓库可单独覆盖监控项；没设置时用面板上的全局开关。
        watch = {
            "commit": _repo_switch(store, entry, "watch_commit", "watch_commit", True),
            "pull": _repo_switch(store, entry, "watch_pull", "watch_pull", True),
            "issue": _repo_switch(store, entry, "watch_issue", "watch_issue", True),
            "release": _repo_switch(store, entry, "watch_release", "watch_release", True),
            "star": _repo_switch(store, entry, "watch_star", "watch_star", False),
        }
        # 私库打码：私有仓库默认不写仓库路径、账号名和链接，可给每个订阅设显示名。
        mask_private = _repo_switch(store, entry, "mask", "private_mask", True)
        display = store.option(owner, repo, "label", "") or full_name
        # 每个仓库可以用自己的凭据；没指定就用默认凭据，再没有就用面板上的 Token。
        repo_token = credentials.resolve(store.option(owner, repo, "cred", ""), token)
        try:
            updates, masked = await _collect_updates(
                host, store, entry, repo_token, watch, per_type, timeout, mask_private
            )
            # 私库打码时，通知标题里也不能出现仓库路径。
            display = "某个私有仓库" if masked else display
        except PluginCallError as error:
            host.log(f"轮询 {full_name} 失败：{error}", "warning")
            if send_error and store.note_failure(owner, repo, str(error), threshold):
                await _notify(
                    host,
                    entry,
                    f"【GitHub 订阅异常】{full_name}\n连续检查失败 {threshold} 次：{error}",
                )
            continue
        except Exception as error:  # 单个仓库出错不能拖垮整轮
            host.log(f"轮询 {full_name} 异常：{type(error).__name__}: {error}", "warning")
            continue

        if store.clear_failure(owner, repo):
            store.add_log("恢复", full_name, "检查恢复正常")
            if send_error:
                await _notify(host, entry, f"【GitHub 订阅恢复】{full_name}\n检查已恢复正常。")

        if not updates:
            continue

        body = "\n".join(updates)
        text = _render_notification(template, display, body)
        for chunk in _split_notification(text):
            for target in entry["targets"]:
                await _send_to_target(host, target, chunk)
                sent += 1
                if sent >= max_notify:
                    break
            if sent >= max_notify:
                break
        store.add_log("推送", full_name, f"{len(updates)} 条更新")
        # 轻量限速：匿名额度有限，轮询之间留点间隔。
        await asyncio.sleep(1.5)


def _repo_switch(
    store: Any, entry: dict[str, Any], repo_key: str, global_key: str, default: bool
) -> bool:
    """每仓库的开关优先，没设置时用面板上的全局开关。"""
    value = store.option(entry["owner"], entry["repo"], repo_key, "")
    if value:
        return store_mod.parse_bool(value, default)
    return default


async def _collect_updates(
    host: Any,
    store: Any,
    entry: dict[str, Any],
    token: str,
    watch: dict[str, bool],
    per_type: int,
    timeout: float,
    mask_private: bool = False,
) -> tuple[list[str], bool]:
    """
    查一个仓库的新动态，组装成事实清单。

    私库打码开启且仓库确实是私有时，不写仓库路径、账号名和链接，
    统一换成「某个私有仓库」。

    Returns:
        (事实清单, 这条是否被打了码)
    """
    owner, repo = entry["owner"], entry["repo"]
    full_name = f"{owner}/{repo}"
    marks = store.marks(owner, repo)
    lines: list[str] = []

    masked = False
    if mask_private:
        _desc, private = await github.fetch_repo_privacy(host, owner, repo, token)
        masked = private
    repo_label = "某个私有仓库" if masked else full_name

    def link(url: str) -> str:
        """私库打码时不给链接。"""
        return "" if masked else str(url or "")

    def who(name: str) -> str:
        """私库打码时不给账号名。"""
        return "某位成员" if masked else str(name or "")

    if watch["release"]:
        releases = await github.fetch_releases(host, owner, repo, token)
        last_tag = str(marks.get("release") or "")
        fresh = [item for item in releases if item["tag"] and item["tag"] != last_tag]
        for item in fresh[:per_type]:
            label = item["tag"]
            if item.get("prerelease"):
                label += "（预发布）"
            lines.append(
                f"Release {label}\n发布于 {common.github_day(item['time'])}\n{link(item['html_url'])}"
            )
        if len(fresh) > per_type:
            lines.append(f"（另有 {len(fresh) - per_type} 条 Release 未列出）")
        if fresh:
            store.update_marks(owner, repo, release=fresh[0]["tag"])

    if watch["commit"]:
        commits = await github.fetch_commits(host, owner, repo, token, limit=per_type)
        last_sha = str(marks.get("commit") or "")
        fresh = [item for item in commits if item["full_sha"] and item["full_sha"] != last_sha]
        for item in fresh[:per_type]:
            lines.append(
                f"Commit {item['sha']}\n{item['message']}\n"
                f"作者：{who(item['author'])}\n提交于 {common.github_day(item['time'])}\n"
                f"{link(item['html_url'])}"
            )
        if len(fresh) > per_type:
            lines.append(f"（另有 {len(fresh) - per_type} 条提交未列出）")
        if fresh:
            store.update_marks(owner, repo, commit=fresh[0]["full_sha"])

    if watch["pull"]:
        pulls = await github.fetch_pulls(host, owner, repo, token, limit=per_type)
        last_issue = int(marks.get("issue") or 0)
        fresh = [item for item in pulls if item["number"] > last_issue]
        for item in fresh[:per_type]:
            status = "已合并" if item.get("merged") else ("草稿" if item.get("draft") else "开放")
            branches = ""
            if item.get("head_ref") or item.get("base_ref"):
                branches = f"{item['head_ref']} → {item['base_ref']}"
            extra = f"\n{branches}" if branches else ""
            lines.append(
                f"PR #{item['number']}（{status}）\n{item['title']}\n"
                f"作者：{who(item['user'])}{extra}\n"
                f"{common.github_day(item['time'])} {link(item['html_url'])}"
            )
        if len(fresh) > per_type:
            lines.append(f"（另有 {len(fresh) - per_type} 条 PR 未列出）")
        if fresh:
            store.update_marks(owner, repo, issue=max(item["number"] for item in fresh))

    if watch["issue"]:
        issues = await github.fetch_latest_issues(host, owner, repo, token)
        last_issue = int(store.marks(owner, repo).get("issue") or 0)
        fresh = [
            item
            for item in issues
            if item["kind"] == "issue" and item["number"] > last_issue
        ]
        for item in fresh[:per_type]:
            lines.append(
                f"Issue #{item['number']}（开放）\n{item['title']}\n"
                f"作者：{who(item['user'])}\n"
                f"{common.github_day(item['created_at'])} {link(item['html_url'])}"
            )
        if len(fresh) > per_type:
            lines.append(f"（另有 {len(fresh) - per_type} 条 Issue 未列出）")
        if fresh:
            store.update_marks(owner, repo, issue=max(item["number"] for item in fresh))

    if watch["star"]:
        stars = await github.fetch_star_events(host, owner, repo, token)
        last_star = str(marks.get("star") or "")
        fresh = [item for item in stars if item["id"] and item["id"] != last_star]
        for item in fresh[:per_type]:
            lines.append(f"新增 Star：{who(item['login'])}\n{link(item['url'])}")
        if len(fresh) > per_type:
            lines.append(f"（另有 {len(fresh) - per_type} 位新增 Star 未列出）")
        if fresh:
            store.update_marks(owner, repo, star=fresh[0]["id"])

    return lines, masked


def _render_notification(template: str, repository: str, body: str) -> str:
    """用模板组装整条通知。"""
    if not template.strip():
        return f"GitHub 动态：{repository}\n{body}"
    return github.render_template(
        template, {"repository": repository, "summary": body, "body": body}
    )


def _split_notification(text: str) -> list[str]:
    """按分割标记把一条通知拆成几条。"""
    if SPLIT_MARKER not in text:
        return [text]
    return [part.strip() for part in text.split(SPLIT_MARKER) if part.strip()]


async def _notify(host: Any, entry: dict[str, Any], text: str) -> None:
    """把一条消息发到订阅的所有目标。"""
    for target in entry["targets"]:
        await _send_to_target(host, target, text)


async def _send_to_target(host: Any, target: dict[str, Any], text: str) -> None:
    """往一个目标（群或私聊）发一条消息。"""
    if target.get("type") == "group":
        await _safe_reply(
            host,
            {"message_type": "group", "group_id": target["id"]},
            text,
            "推送 GitHub 更新",
        )
    else:
        await _safe_reply(
            host,
            {"message_type": "private", "user_id": target["id"]},
            text,
            "推送 GitHub 更新",
        )


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
    global _poller_started
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
