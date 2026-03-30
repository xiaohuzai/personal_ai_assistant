# Personal AI Assistant

基于 [Claude Agent SDK](https://github.com/anthropics/claude-code) 的飞书个人 AI 助理。

飞书消息 → WebSocket 长连接 → Claude SDK → 工具调用（Bash、文件读写等）

## 功能

- 私聊 / 群聊（@Bot）均可触发
- 支持文本、富文本、图片、文件消息
- 打字机效果逐步输出回复
- 工具调用进度实时展示
- 需要用户确认时展示交互按钮（CHOICE_REQUEST）
- 会话持久化，重启后自动恢复上次对话

**斜杠指令：**

| 指令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/new` | 新建会话 |
| `/context` | 查看当前会话摘要 |
| `/compact` | 压缩会话上下文（节省 token） |
| `/sessions` | 查看历史会话并切换 |

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
- `ASSISTANT_CWD`：改为本机项目的绝对路径，如 `/home/yourname/personal_ai_assistant/workspace`

**选填：**
- `AGENT_OWNER`：你的名字，写入 system prompt（默认 `user`）

### 4. 配置飞书机器人

在[飞书开放平台](https://open.feishu.cn)完成以下配置：

1. **权限**：开通 `im:message`、`im:message.group_at_msg`、`contact:user.base:readonly`
2. **事件订阅**：添加 `接收消息` 事件（`im.message.receive_v1`），传输协议选**长连接**
3. **机器人**：在应用功能中启用机器人

### 5. 运行

```bash
uv run python -m src
```

## 项目结构

```
src/
├── main.py              # 入口
├── agent/
│   ├── assistant.py     # Claude SDK 封装（对话 / 工具调用）
│   └── session.py       # 会话管理（open_id → session_id 持久化）
└── feishu/
    ├── bot.py           # 飞书 WebSocket 消息处理
    ├── card.py          # 飞书卡片构建
    └── feishu_client.py # 飞书 API 客户端
workspace/               # Agent 工作目录（运行时生成，不提交到 git）
```
