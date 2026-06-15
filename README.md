# Personal AI Assistant

基于 [Claude Agent SDK](https://github.com/anthropics/claude-code) 的飞书个人 AI 助理。

```
飞书消息 → WebSocket 长连接 → Claude SDK → 工具调用（Bash、文件读写、lark-cli 等）
```

## 功能

### 对话能力
- 私聊 / 群聊（@Bot）均可触发
- 支持文本、富文本（post）、图片、文件消息
- 支持引用回复（自动注入被引用消息内容）
- 会话持久化，重启后自动恢复上次对话
- CHOICE_REQUEST：需确认时展示交互按钮，点击继续

### 进度展示
- **普通模式**：打字机效果逐步输出
- **富文本模式**（`/rich` 开启）：
  - 流式展示当前正在执行的工具、思考过程
  - 完成后折叠面板展示完整的 thinking / 工具调用记录
  - 正在输出时实时更新文字进度

### 飞书集成
- 通过 lark-cli 访问飞书空间（日历、文档、知识库、消息等）
- 启动时自动发起 lark-cli 账号授权（Device Flow，可点链接完成）
- 给原始消息加 emoji 表情：⏳ 处理中 → 随机完成 emoji

---

## 斜杠指令

| 指令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/new` / `/reset` | 新建会话（清空上下文） |
| `/context` | 查看当前会话摘要 |
| `/compact` | 压缩会话上下文（节省 token） |
| `/sessions` | 查看历史会话并切换 |
| `/save <名称>` | 给当前会话保存别名 |
| `/s <名称> <消息>` | 在指定别名会话内发消息（不切换当前会话） |
| `/models` | 查看可用模型并切换 |
| `/rich` | 切换富文本模式（折叠面板 开/关） |
| `/thread [on\|off]` | 群聊话题回复模式（开启后回复以「话题」形式展示） |
| `/turns [N]` | 查看或设置 max_turns（默认 20，范围 1–200） |
| `/effort [level]` | 查看或设置思考深度（low / medium / high / xhigh / max） |
| `/shell <命令>` | 直接执行 shell 命令并返回输出（超长输出以文件回传） |

---

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd personal_ai_assistant
```

### 2. 安装依赖

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv sync
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，按注释填入以下内容：

**必填：**
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`：飞书开放平台创建企业自建应用后获取
- `ANTHROPIC_API_KEY`（直连）或 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`（代理网关）
- `ASSISTANT_CWD`：Agent 工作目录绝对路径，如 `/home/yourname/personal_ai_assistant/workspace`

**选填：**
- `AGENT_OWNER`：你的名字，写入 system prompt（默认 `user`）
- `ANTHROPIC_AVAILABLE_MODELS`：逗号分隔的模型 ID，供 `/models` 指令使用

### 4. 配置飞书机器人

在[飞书开放平台](https://open.feishu.cn)完成以下配置：

**权限（API 权限 → 添加权限）：**
- `im:message` / `im:message.group_at_msg`
- `im:message.reaction:write`（表情回复）
- `contact:user.base:readonly`

**事件订阅（事件与回调 → 事件订阅）：**
- `im.message.receive_v1`（接收消息）
- `application.bot.menu_v6`（机器人菜单，可选）
- 传输协议选**长连接**

**应用功能：**
- 启用**机器人**
- 启用**互动卡片**——缺少此项则按钮/下拉框点击无效（报错 200340）

**机器人菜单（可选）：**

用户在私聊机器人时顶部会出现快捷菜单按钮。配置步骤：

1. 飞书开放平台 → 应用功能 → **机器人** → **机器人菜单** → 添加菜单项
2. 每个菜单项的「响应动作」选 **推送事件**，填入下表对应的 `event_key`
3. 确保「事件订阅」中已添加 `application.bot.menu_v6` 事件（见上方）
4. **发布新版本**后菜单才会对用户生效

| event_key | 菜单文字（建议） | 触发行为 |
|-----------|----------------|---------|
| `help` | 📋 指令手册 | 显示帮助卡片 |
| `new_session` | 🆕 新建对话 | 清空历史 |
| `sessions` | 📂 历史会话 | 会话切换卡片 |
| `models` | 🤖 切换模型 | 模型选择卡片 |
| `toggle_rich` | ✨ 富文本模式 | 切换折叠面板 |

### 5. 配置 lark-cli（可选，用于访问飞书空间）

lark-cli 让 Agent 可以操作飞书日历、文档、知识库等。

```bash
# 安装（需要 Node.js 16+）
npm install -g @larksuite/cli

# ⚠️ 必须与飞书机器人使用同一套 App ID / Secret
lark-cli config init --app-id <FEISHU_APP_ID> --app-secret-stdin --brand feishu

# 授权个人账号（每台新服务器执行一次）
lark-cli auth login
```

> 启动时 bot 会自动搜索 nvm/npm 路径并注入 PATH，也会自动发起 Device Flow 授权并把链接发给你。

### 6. 运行

```bash
uv run python -m src
```

---

## 项目结构

```
.
├── src/
│   ├── main.py              # 入口：加载 .env，初始化，启动 bot
│   ├── __main__.py          # python -m src 入口
│   ├── agent/
│   │   ├── assistant.py     # Claude SDK 封装（对话 / 工具调用 / lark-cli 配置）
│   │   ├── session.py       # 会话管理（open_id → session_id 持久化）
│   │   └── prefs.py         # 用户偏好持久化（rich_mode / turns / effort 等）
│   └── feishu/
│       ├── bot.py           # 飞书 WebSocket 消息处理 + 所有指令路由
│       ├── card.py          # 飞书卡片构建（进度 / 富文本 / 选择 / 会话 / 模型）
│       └── feishu_client.py # 飞书 API 客户端（消息 / 文件 / 表情 / 撤回等）
├── workspace/               # Agent 工作目录（不提交到 git）
│   ├── uploads/             # 用户上传的文件（自动保存）
│   ├── .sessions.json       # 会话映射持久化（自动生成）
│   ├── .user_prefs.json     # 用户偏好持久化（自动生成）
│   ├── .agent_env           # 运行时额外环境变量（重启后自动加载）
│   └── .claude/
│       ├── settings.json    # Claude Code 配置（模型 / 权限白名单）
│       ├── skills/          # 已安装的 Skill（如 tavily-search）
│       └── commands/        # 自定义 Slash Command
├── .env                     # 本地环境变量（不提交到 git）
├── .env.example             # 环境变量模板
└── pyproject.toml           # 项目依赖（uv 管理）
```

---

## 架构说明

单进程架构，无外部依赖（Redis / 数据库）：

```
飞书 WebSocket
    │
    ▼
bot.py（消息解析 / 指令路由 / 卡片渲染）
    │
    ├─ 普通消息 ──▶ assistant.py ──▶ Claude SDK（query / StreamEvent）
    │                   │
    │               on_event 回调（实时更新进度卡片）
    │
    ├─ 卡片点击 ──▶ 处理 stop / choice / switch_session / switch_model
    │
    └─ 菜单点击 ──▶ p2.application.bot.menu_v6（映射到对应指令）
```

**状态管理（内存 + 文件，替代 Redis）：**
- 消息去重：TTLCache（1h TTL）
- 待处理交互：TTLCache（10min TTL）
- 会话映射：`.sessions.json`
- 用户偏好：`.user_prefs.json`
