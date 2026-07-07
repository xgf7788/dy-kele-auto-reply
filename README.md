# 抖音来客 IM 自动回复机器人

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个基于 Python + Playwright 的**抖音来客商家版 IM 自动回复机器人**。支持同时监控多个店铺消息，检测到新消息后调用自定义 HTTP API 获取回复内容并自动发送。

> **免责声明**：本工具仅供学习与研究使用。使用者需自行承担使用风险，并遵守抖音平台规则及相关法律法规。

## 功能特性

- **多店铺支持** — 同时监控多个店铺（5+）的 IM 消息
- **实时消息检测** — 轮询 + DOM 监听，秒级发现新消息
- **自定义回复 API** — 可接入任意大模型、聊天机器人或规则引擎
- **消息去重** — 防止重复处理同一条消息
- **持久化存储** — SQLite 记录消息历史
- **浏览器自动化** — 基于 Playwright（Chromium）
- **异步高并发** — 用户级队列与锁，处理效率高
- **内置健康检查** — HTTP 状态页 + JSON 接口

## 后续规划

- 支持更多电商平台：小红书、京东、淘宝等
- 配套 AI 客服后台：对话管理、知识库配置、数据统计、人机协作

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                   抖音来客自动回复机器人                      │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ 店铺管理器   │  │ 消息调度器   │  │   API 客户端        │  │
│  │ StoreMgr    │  │ Dispatcher  │  │   API Client        │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                    │             │
│  ┌──────▼────────────────▼────────────────────▼──────┐      │
│  │              Playwright 浏览器池                   │      │
│  │  ┌──────┐ ┌──────┐ ┌──────┐    ┌──────┐          │      │
│  │  │店铺 1│ │店铺 2│ │店铺 3│... │店铺 N│          │      │
│  │  └──────┘ └──────┘ └──────┘    └──────┘          │      │
│  └────────────────────────────────────────────────────┘      │
│                           │                                  │
└───────────────────────────┼──────────────────────────────────┘
                            │
                 ┌──────────▼──────────┐
                 │      抖音来客后台     │
                 │     （IM 消息系统）    │
                 └─────────────────────┘
```

## 安装步骤

### 1. 克隆仓库

```bash
git clone <repository-url>
cd dy_kele_auto_reply
```

### 2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate     # Windows
```

### 3. 安装依赖

```bash
pip install -r requirements.txt

# 安装 Playwright Chromium 浏览器
playwright install chromium
```

### 4. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，按需修改日志级别、数据库地址等
```

### 5. 配置店铺账号

```bash
cp config/accounts.yaml.example config/accounts.yaml
# 编辑 config/accounts.yaml，添加你的店铺信息
```

> **安全提示**：请勿将 `config/accounts.yaml`、`.env` 或任何包含真实 Cookie/手机号的文件提交到版本控制。相关文件已在 `.gitignore` 中排除。

## 配置文件说明

### accounts.yaml

```yaml
stores:
  # Cookie 登录（推荐用于自动化）
  - store_id: "store_001"
    name: "店铺A"
    login_type: "cookie"
    # 方式1：直接填写 cookie 字符串（不建议写入版本控制）
    # cookies: "sessionid=xxx;sid_guard=yyy;"
    # 方式2：从环境变量读取（推荐）
    cookies_from_env: "COOKIES_STORE_001"
    api_endpoint: "http://localhost:8000/api/chat/reply"
    api_timeout: 10
    reply_delay: 1.0
    enabled: true
    headless: true

  # 手机验证码登录
  - store_id: "store_002"
    name: "店铺B"
    login_type: "phone"
    phone_from_env: "PHONE_STORE_002"
    api_endpoint: "http://localhost:8000/api/chat/reply"
    api_timeout: 10
    reply_delay: 1.0
    enabled: false
    headless: false  # 必须可见才能输入验证码

  # 二维码登录
  - store_id: "store_003"
    name: "店铺C"
    login_type: "qrcode"
    api_endpoint: "http://localhost:8000/api/chat/reply"
    enabled: false
    headless: false  # 二维码登录需要可见浏览器
```

敏感字段建议通过环境变量传入：

| 配置项 | 环境变量示例 |
|--------|-------------|
| `cookies_from_env` | `COOKIES_STORE_001` |
| `phone_from_env` | `PHONE_STORE_002` |
| `api_key_from_env` | `API_KEY_STORE_001` |

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `LOG_LEVEL` | INFO | 日志级别 |
| `MAX_BROWSERS` | 10 | 最大浏览器实例数 |
| `HANDLER_WORKERS` | 5 | 消息处理工作线程数 |
| `DB_URL` | sqlite:///storage/messages.db | 数据库连接地址 |
| `MESSAGE_POLL_INTERVAL` | 0.5 | 消息轮询间隔（秒） |
| `STATUS_CHECK_INTERVAL` | 300.0 | 店铺健康检查间隔（秒） |

## 回复 API 接入

机器人会将每条新消息以 JSON 格式 POST 到你配置的 `api_endpoint`。

### 请求格式

```http
POST /api/chat/reply
Content-Type: application/json

{
  "store_id": "store_001",
  "store_name": "店铺A",
  "conversation_id": "conv_123456",
  "user_id": "user_789",
  "user_name": "用户昵称",
  "message_id": "msg_abc123",
  "message": "用户发送的消息内容",
  "timestamp": 1704067200,
  "context": {
    "history": [
      {"role": "user", "content": "你好"},
      {"role": "assistant", "content": "您好，有什么可以帮您？"}
    ]
  }
}
```

### 响应格式

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "reply": "这是回复给用户的消息",
    "type": "text",
    "delay": 1,
    "end_conversation": false
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | `0` 表示成功 |
| `data.reply` | string | 回复内容 |
| `data.delay` | int | 延迟发送秒数 |
| `data.end_conversation` | bool | 是否结束对话 |

你可以用 FastAPI、Flask、Node.js 或任何后端实现该接口。

## 使用方法

### 启动所有店铺

```bash
python main.py
```

### 只启动单个店铺

```bash
python main.py --store store_001
# 或按店铺名称
python main.py --store "店铺A"
```

### 自定义健康检查端口

```bash
python main.py --store store_001 --port 8900
```

健康检查接口：

- `http://localhost:8899/` — HTML 状态页
- `http://localhost:8899/health` 或 `/api/health` — JSON 健康检查
- `http://localhost:8899/stats` 或 `/api/stats` — JSON 统计信息

### 首次登录（Cookie 方式）

1. 在浏览器中登录抖音来客后台
2. 打开开发者工具（F12）
3. 复制 Cookie 字符串
4. 设置为环境变量，或临时粘贴到 `config/accounts.yaml` 的 `cookies` 字段
5. 重启机器人

### 首次登录（二维码方式）

1. 设置 `login_type: "qrcode"` 和 `headless: false`
2. 运行程序，扫描浏览器中显示的二维码
3. 登录成功后保存 Cookie，供后续 headless 模式使用

### 登录辅助脚本

```bash
python login.py
```

该交互式脚本支持二维码、手机号或手动输入 Cookie 登录，并自动生成 `config/accounts.yaml` 配置项。

## 项目结构

```
dy_kele_auto_reply/
├── config/
│   ├── __init__.py
│   ├── settings.py              # 配置管理
│   └── accounts.yaml.example    # 店铺配置示例
├── core/
│   ├── __init__.py
│   ├── store_manager.py         # 店铺与浏览器管理
│   ├── browser_pool.py          # 浏览器池（遗留）
│   ├── message_listener.py      # 消息监听协调器
│   ├── message_handler.py       # 消息处理与回复
│   ├── api_client.py            # HTTP API 客户端
│   ├── health_server.py         # 内置健康检查服务
│   └── listener/                # 监听子模块
│       ├── conversation.py
│       ├── dedup.py
│       ├── extraction.py
│       └── page_utils.py
├── models/
│   ├── __init__.py
│   └── message.py               # 数据模型
├── utils/
│   ├── __init__.py
│   ├── constants.py
│   ├── helpers.py
│   └── logger.py                # 日志配置
├── storage/
│   ├── __init__.py
│   └── message_db.py            # SQLite 消息存储
├── main.py                      # 入口文件
├── login.py                     # 交互式登录工具
├── save_cookies.py              # Cookie 提取工具
├── requirements.txt
├── pyproject.toml
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## 重要说明

### Cookie 有效期

- Cookie 通常会过期，需要定期更新
- 建议每周检查并更新一次
- 可通过健康检查接口监控店铺是否在线

### 风控与合规

1. **频率控制**：不要过于频繁发送消息，建议间隔 1–2 秒
2. **内容规范**：确保回复内容符合平台规范
3. **人工介入**：复杂问题建议转人工客服
4. **可见浏览器**：部分登录方式需要 `headless: false`

### 页面选择器维护

抖音来客页面结构可能变化，以下文件中的 CSS 选择器可能需要更新：

- `core/store_manager.py` — 登录状态检查
- `core/listener/extraction.py` — 消息提取
- `core/message_handler.py` — 输入框与发送按钮

## 隐私与安全

- **切勿提交真实凭证**。仓库已排除 `config/accounts.yaml`、`.env`、Cookie、数据库、日志和调试截图。
- 推荐通过 `cookies_from_env`、`phone_from_env`、`api_key_from_env` 从环境变量读取敏感信息。
- 分享截图或日志前请检查 `storage/` 目录，调试图片可能包含用户聊天内容。

## 常见问题

### Cookie 登录失败

1. 检查 Cookie 是否过期
2. 确保格式正确：`key=value; key2=value2`
3. 尝试二维码登录

### 消息无法提取

1. 检查页面是否完全加载
2. 更新 CSS 选择器以匹配当前页面结构
3. 开启 `headless: false` 观察浏览器行为

### API 调用失败

1. 检查 API 端点是否可访问
2. 检查 API 超时设置
3. 查看 `storage/logs/error.log` 获取详细错误

## 参与贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/my-feature`
3. 提交修改，并尽量补充测试
4. 运行 `python -m py_compile main.py` 检查语法
5. 提交 Pull Request

请勿在 PR 中包含真实凭证、用户数据或调试截图。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。

## 免责声明

本工具仅供学习与研究使用。使用者需自行承担全部使用风险，并遵守抖音平台规则、用户隐私保护及相关法律法规。作者不对因使用本工具导致的任何损失或法律责任负责。

使用本软件即表示你同意：

- 遵守抖音来客服务协议和社区规范
- 保护用户隐私与数据安全
- 不将本工具用于垃圾消息、骚扰或其他违法用途
