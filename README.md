# GitHub 订阅与解析插件（cn.ucbk.github）

一个面向 NapCat / OneBot 宿主的 QQ GitHub 插件：贴 GitHub 链接就解析回复，也能订阅仓库、有更新主动推送到群。

## 功能特性

- 贴 GitHub 仓库 / Issue / PR / Release 链接，机器人直接回解析卡片
- 订阅仓库：`gh sub owner/repo`，有新 Release、新提交、新 Issue/PR 或 Star 变化时推送到订阅所在的群或私聊
- 取消订阅：`gh unsub owner/repo`；查看订阅：`gh list`
- 每个仓库可单独设置：`gh conf owner/repo 键 值`（显示名、监控项、私库打码、使用的凭据…）
- 多个 GitHub 账号：`gh cred list/add/rm/default/check`，仓库可各自选用
- 运行记录：`gh log` 查看最近几轮检查与推送
- 支持同时订阅多个仓库、多个会话（群 / 私聊）独立接收
- 可选填 GitHub Token 提高 API 限流额度；不填也能用（匿名 60 次/小时）
- 订阅与凭据存在宿主的数据目录下，插件升级不会丢

## 面板上能设置什么

宿主的面板按插件清单里的 `config_schema` 渲染成一张表单，能设的项有：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 生效范围 | 群聊与私聊 | 可改为仅群聊或仅私聊 |
| 指令前缀 | gh | 订阅相关命令的前缀 |
| GitHub Token | 空 | 可选，提高 API 限流额度 |
| 订阅轮询间隔 | 300 秒 | 检查订阅更新的间隔 |
| 每次轮询最多推送 | 5 条 | 防止单轮推送过多刷屏 |
| 每类动态展示条数 | 10 | 一次检查里 Commit / PR / Issue / Release 各自最多列几条，超出的会注明还剩多少条 |
| 仓库检查超时 | 20 秒 | 单次 GitHub 请求的最长等待（宿主上限 30 秒） |
| 连续失败几次才报 | 3 | 连续失败到这个次数才在群里说一声，一轮故障只报一次，恢复后再说一声好了；填 0 表示不报 |
| 发送错误通知 | 开 | 订阅坏了要不要在群里提一句 |
| 监控 Star 变化 | 关 | 有没有人给仓库加 Star |
| 监控新提交 / 新 PR / 新 Issue / 新 Release | 全开 | 分别控制监控哪几类动态 |
| 推送模板 | 空 | 可用 `{repository}` `{summary}` `{body}`；单独一行写 `[diana-msg]` 表示从那里分成下一条消息发送 |
| 私有仓库打码 | 开 | 私有仓库不写仓库路径、账号名和链接 |

> 面板上的数字项如果显示 0，那表示「没填」，插件会用自己的默认值 —— 不会真的按 0 跑。

## 每个仓库单独设置

`gh conf owner/repo 键 值`，不带值时是查看：

| 键 | 说明 |
| --- | --- |
| `label` | 显示名，推送标题里用它代替 `owner/repo` |
| `mask` | 私库打码，覆盖面板上的同名开关 |
| `star` | 监控 Star 变化 |
| `notify` | 推送通知 |
| `limit` | 每类条数上限 |
| `cred` | 这个仓库用哪条凭据 |

## 凭据管理

```
gh cred list                列出凭据名字（不显示 Token）
gh cred add 工作 <Token>     新增或覆盖一条凭据
gh cred rm 工作              删掉一条
gh cred default 工作         设成默认凭据
gh cred check 工作           拿这条凭据查一次账号，验证能不能用
```

仓库用哪条凭据由 `gh conf owner/repo cred 名称` 指定；没指定就用默认凭据，默认也没有就用面板上填的 GitHub Token。

## 解析规则

链接识别与卡片内容的判定规则，与 SuInk/Diana 内置的 GitHub 解析行为保持一致（用 Python 重新实现，未复制其源码）：

| 场景 | 行为 |
| --- | --- |
| `github.com/owner/repo` | 解析仓库卡片 |
| `github.com/owner/repo.git` | 剥掉 `.git` 后当仓库（克隆地址） |
| `github.com/owner/repo/tree/main/docs` | 当仓库 |
| `github.com/owner/repo/issues/12` | 解析 Issue（编号必须是正整数） |
| `github.com/owner/repo/pull/34/files` | 解析 PR |
| `github.com/owner/repo/releases`、`/releases/latest` | 解析最新正式 Release |
| `github.com/owner/repo/releases/tag/v1.2.3` | 解析指定版本 Release |
| `github.com/settings/profile`、`/topics/golang` 等保留页 | 不认（不是用户仓库） |
| `example.com/owner/repo`、`u.jd.com/...` | 不认 |

卡片里会给出的信息：

- **仓库**：名称、归档 / Fork 标记、简介、星标数、Fork 数、开放 Issue/PR 数、语言、许可证、话题、最近推送日期
- **Issue**：标题、状态（区分「已关闭（不计划处理）」）、作者、评论数、创建日期、标签、正文摘要（截断 600 字）
- **PR**：标题、状态（已合并 / 草稿 / 已关闭）、作者、评论数、创建日期、标签，以及分支流向与改动规模（文件数、增删行）
- **Release**：版本号、名称、发布时间、是否预发布、发布页面、发布说明摘要

Release 的读取有额外校验：只认公开地址（https + github.com 本域 + 不带凭据与查询参数）；草稿不算发布；`latest` 不接受预发布；返回的发布地址必须与请求的仓库、版本一致，对不上就丢弃。**HTTP 失败只说明这次没读到，不会当成「该版本不存在」的结论。**

## 目录结构

```text
github-plugin/
├── plugin.py                 # 插件入口：消息处理、指令、后台轮询
├── manifest.json             # 插件清单、能力声明、域名白名单、配置 Schema
├── README.md                 # 使用说明
├── gparse/
│   ├── __init__.py           # 解析工具包
│   ├── common.py             # 链接识别、出网封装、文本格式化、错误类型
│   ├── github.py             # GitHub API 调用与卡片格式化
│   ├── store.py              # 订阅、每仓库设置、轮询游标、运行记录
│   └── credentials.py        # 多凭据管理与仓库绑定
└── tests/
    └── test_github_plugin.py # 离线回归测试（不联网、不需要宿主）
```

## 安装与启用

本插件通过 GitHub 仓库获取和更新，不需要打包，也不需要单独下载源码文件。

### 通过 Git 获取

在宿主的插件目录中执行：

```bash
cd /path/to/插件目录
git clone https://github.com/ovonull/github-plugin.git github-plugin
```

进入插件目录拉取更新：

```bash
cd /path/to/插件目录/github-plugin
git pull --ff-only
```

获取后确认目录结构如下：

```text
插件目录/
└── github-plugin/
    ├── plugin.py
    ├── manifest.json
    └── gparse/
```

重启宿主，在插件管理面板中：

1. 加载或发现 `GitHub 订阅与解析` 插件；
2. 审核 / 授权插件声明的能力（`msg.send`、`http.outbound`）；
3. 填写配置（可选 GitHub Token、轮询间隔、监控哪几类动态等）；
4. 手动启用插件。

## 权限与出网

| 能力 | 用途 | 风险说明 |
| --- | --- | --- |
| `msg.send` | 发送解析卡片与更新通知 | 中风险 |
| `http.outbound` | 请求 GitHub API | 高风险 |

插件只请求 `manifest.json` 中声明的域名：`api.github.com`、`github.com`、`uploads.github.com`。

## 使用方式

| 操作 | 指令 |
| --- | --- |
| 解析链接 | 直接粘贴 `github.com/owner/repo` 等链接 |
| 订阅仓库 | `gh sub owner/repo` |
| 取消订阅 | `gh unsub owner/repo` |
| 查看订阅 | `gh list` |
| 单独设置某个仓库 | `gh conf owner/repo [键 值]` |
| 管理凭据 | `gh cred list/add/rm/default/check` |
| 查看运行记录 | `gh log` |
| 帮助 | `gh help` |

## 故障排查

- **提示未获批访问外网**：在宿主面板单独批准 `http.outbound`。
- **匿名限流（403）**：GitHub 匿名 API 为 60 次/小时；配置 GitHub Token 或添加凭据后额度大幅提升。
- **订阅一直没动静**：确认插件已启用、轮询间隔不是太大；查看宿主日志里的 `GitHub 轮询` 记录，或在群里发 `gh log`。
- **解析失败**：确认 owner/repo 拼写；私有仓库必须填有效 Token。
- **贴了链接没反应**：只有 github.com 的仓库 / Issue / PR / Release 链接会触发解析；`settings`、`topics` 这类站内页面不认。
- **面板上的数字显示 0**：那是「没填」，插件会用自己的默认值，不会按 0 跑。
- **插件升级后订阅没了**：订阅存在宿主数据目录（`data/plugins/cn.ucbk.github/`），不在插件包目录里，正常不会丢；如果确实没了，检查 `PLUGIN_DATA_DIR` 环境变量有没有被改过。

## 本地自检

改完代码先跑一遍离线测试，不需要宿主、不联网：

```bash
python tests/test_github_plugin.py
```

## 版本信息

- 插件 ID：`cn.ucbk.github`
- 当前版本：`1.2.0`
- 许可证：MIT
- 作者：UCBK
- 项目主页：<https://www.ucbk.cn>
