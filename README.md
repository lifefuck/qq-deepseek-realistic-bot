# QQ DeepSeek Realistic Bot

基于 OneBot 11（如 NapCat）和 DeepSeek-V3 的 QQ 群聊机器人。
说话像普通群友：短句、接梗、不客套，按情绪配表情包，被 @ 一次后能连着聊几句。自带 Web 面板，按群管理。

## 特性

- **连续追问**：群友 @ 一次或戳一戳后，自动开启 5 分钟追问窗口，期间不用反复 @ 机器人。
- **打字分段**：回复按标点或换行切成两段发出，中间微停顿，不一次砸出一大坨；代码块和 root 模式整条发。
- **多模态视觉**：接入 Gemini 视觉前置识图，群友发表情包、梗图或截图能看懂画面细节并临场互动吐槽。
- **深层记忆与好感**：引入 DSH 思想与 fish_memory 引擎，按群与群友隔离记录画像与过往话题，支持 BM25 + 时间衰减深层召回。
- **模式切换**：
  - `/user`：日常摸鱼人设，嘴贫、会发表情包。
  - `/root`：纯技术助手模式，去掉客套话，利落解答问题与写代码。
- **冷热场权重**：群安静超过 10 分钟降权（0.25x），不硬凑；聊得热闹时提权（1.5x），跟着接梗。
- **动态频控防风控**：滑动 60 秒内严格控制群发言频次（最高 9 条），达到第 10 条触发 AI 自主生成的风控告警并自动闭麦冷却 60 秒；戳一戳特例放行不限频。
- **云崽共存**：自动过滤游戏查询前缀（如 `#`、`面板` 等），游戏指令交由云崽处理，AI 不抢答。
- **链接解析**：群友发抖音、B 站、小红书短链时自动抓取视频标题和简介，带着上下文回复。
- **表情包跟情绪走**：138 张图提前打好情绪和关键词，模型在句末标 `[MEME:情绪]`，按此选图，不全靠随机。
- **Web 控制台 (`:8088`)**：Miuix Vue 3 开发，构建产物内置。支持按群调插话概率、冷热场权重、夜间休眠、群友记忆与实时日志。

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
WorkingDirectory=%h/projects/qq-deepseek-realistic-bot
ExecStart=%h/projects/qq-deepseek-realistic-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

## 常用指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `[戳一戳]` | 全员 | 触发戳一戳互动，开启 5 分钟追问窗口 |
| `/memory` | 全员 | 群友档案，合并转发成聊天记录：群名片/QQ昵称、QQ 号、群身份、头衔、好感与印象 |
| `/root` | 管理员 | 切换为技术伙伴模式（无字数限制、不分段） |
| `/user` | 管理员 | 切回日常闲聊人设 |
| `/on` | 管理员 | 一键放开本群全部功能 |
| `/off` | 管理员 | 一键停掉本群全部功能，除主人外谁说话都不理 |
| `/new` | 管理员 | 清空当前群或私聊的会话上下文 |
| `/new all` | 管理员 | 清空所有群上下文 |
| `/stop` | 管理员 | 停止当前生成并清空等待队列 |

## 群玩法

| 指令 | 说明 |
| :--- | :--- |
| 被 @ 时带图 | 当斗图处理，直接从表情包库存反甩一张，同群 30 秒一次 |

不想接斗图就 `/off`，或者只在面板上调插话意愿。

作息：23:00~07:00 进入休眠，不主动插话，被 @ 只回短梦话。主人私聊或戳一下能叫醒 30 分钟。


## 提交前自检

```bash
./scripts/install_hooks.sh   # 装一次 pre-commit：
                             # 语法 + 模块循环导入 + 冒烟测试
```

## 作者

- life
