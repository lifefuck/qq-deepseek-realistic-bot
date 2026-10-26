# QQ DeepSeek Realistic Bot

基于 OneBot 11（如 NapCat）和 DeepSeek-V3 的 QQ 群聊机器人。
主要设计目标是像个正常群友一样在群里聊天，回复不带客服报幕腔，按标点自动分句，支持表情包和连续免 @ 追问，同时自带后台 Web 面板用于分群管理。

## 特性

- **连续追问**：群友 @ 一次或戳一戳后，自动开启 5 分钟追问窗口，期间不用反复 @ 机器人。
- **打字分段**：回复按逗号或换行自动切成两段短消息发送，带短暂延迟，避免一次性砸出大段死板文字；代码块和技术模式保持单条输出。
- **模式切换**：
  - `/user`：日常摸鱼人设，嘴贫、会发表情包。
  - `/root`：纯技术助手模式，去掉客套话，利落解答问题与写代码。
- **频控与防封**：单群 60 秒硬限频；检测到群禁言或自身被禁言时自动静默，不向 API 盲目发请求。
- **云崽共存**：自动过滤游戏查询前缀（如 `#`、`面板` 等），游戏指令交由云崽处理，AI 不抢答。
- **链接解析**：群友发抖音、B 站、小红书短链时自动抓取视频标题和简介，带着上下文回复。
- **Web 控制台 (`:8088`)**：本地网页面板，可分群调整插话概率、开关夜间睡眠、查看群友记忆与实时日志。

## 文档

更多深入配置已拆分到 `docs/` 目录：
- [系统机制说明 (docs/architecture.md)](docs/architecture.md)
- [提示词与人设配置 (docs/persona-and-prompts.md)](docs/persona-and-prompts.md)

## 部署运行

### 1. 安装依赖

```bash
git clone https://github.com/lifefuck/qq-deepseek-realistic-bot.git
cd qq-deepseek-realistic-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 下载配套表情包
git clone --depth 1 https://github.com/shirachino/astrbot-deepseek-meme-pack.git meme-pack
```

### 2. 配置文件

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

编辑 `.env` 填入配置：

```dotenv
# OneBot 11 WebSocket 地址（以 NapCat 为例）
NAPCAT_WS_URL=ws://127.0.0.1:3001

# 机器人 QQ 与最高管理员 QQ
BOT_QQ=1000000000
ADMIN_QQ=1000000001

# 启用的白名单群号，多个用逗号隔开
WHITELIST_GROUPS=123456789,987654321

# DeepSeek 接口配置
DEEPSEEK_API_KEY=sk-xxxxxx
DEEPSEEK_API_URL=https://api.siliconflow.cn/v1/chat/completions
DEEPSEEK_MODEL=deepseek-ai/DeepSeek-V3
```

### 3. 运行

```bash
# 直接运行
python3 bot.py

# 启动后可浏览器访问 http://IP:8088 打开管理后台
```

生产环境推荐使用 systemd 托管，参考配置：

```ini
[Unit]
Description=QQ DeepSeek Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/lmxy/projects/qq-deepseek-realistic-bot
ExecStart=/home/lmxy/projects/qq-deepseek-realistic-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

## 常用指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `[戳一戳]` | 全员 | 触发戳一戳互动，开启 5 分钟追问窗口 |
| `/memory` | 全员 | 查看个人在机器人这里的印象画像 |
| `/root` | 管理员 | 切换为技术伙伴模式（无字数限制、不分段） |
| `/user` | 管理员 | 切回日常闲聊人设 |
| `/new` | 管理员 | 清空当前群或私聊的会话上下文 |
| `/new all` | 管理员 | 清空所有群上下文 |
| `/stop` | 管理员 | 停止当前生成并清空等待队列 |
| `/compress` | 管理员 | 手动压缩当前群上下文 |
| `/b [条数]` | 管理员 | 总结最近群聊记录，默认 50 条 |

## 作者

- life
