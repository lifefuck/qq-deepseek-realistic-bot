# QQ DeepSeek Realistic Bot (大肥鱼拟真群聊机器人)

> 基于 OneBot 11 (NapCat / LLOneBot) 与 DeepSeek-V3 的高性能、去人机味、低成本拟真 QQ 群聊智能助手。

针对当前群聊机器人普遍存在的 **Token 成本指数级爆炸、上下文串台认错人、AI 报幕腔/油腻人设、与其他 AI 死循环互掐、频繁触发腾讯风控** 等落地痛点而设计。支持秒级多模态识图与大肥鱼 11 大场景表情包精准联动，并可与云崽（TRSS-Yunzai）无缝共存协同。

---

## 📚 详细文档导航

为了方便深度复刻与架构二次开发，核心技术与提示词规范已模块化归档于 `docs/` 目录：

* 📐 **[系统架构与工程设计指南 (docs/architecture.md)](docs/architecture.md)**
  * 十大工程机制、WebSocket 双向通信拓扑、视觉提炼链路、滑动窗口限频与静默熔断防禁言。
* 🎭 **[完整人设系统提示词与指令全景 (docs/persona-and-prompts.md)](docs/persona-and-prompts.md)**
  * 1:1 开箱即用 Prompt、13 组真实群聊语料 Few-Shot、AI 动态好感度评估规则、云崽协同指路、AI 限流拌嘴机制与全套控制指令。

---

## 🌟 核心特性与架构亮点

### 1. 单轮未读轻量会话（Token 消耗暴降 90%~95%）
* **拒绝无脑滑动窗口**：彻底抛弃将全群最近几十条长篇记录硬拼进上下文的传统做法，解决群聊冷清时上下文缓存失效导致的 Token 费用指数级爆炸。
* **精准聚焦，绝不串台**：针对每一次触发仅将当前发言群友的最新输入（若为 QQ 引用回复，自动抓取并拼接原消息上下文）组装成单轮会话，单次交互 Token 恒定在 200~300 tokens。
* **AI 动态好感度与长期记忆**：用户画像、好感度增减交由大模型后台异步深度理解语境动态评估（吵架对线扣分、善意互动加分），前台秒回零阻塞。

### 2. 双模态视觉转录链路（低成本秒级识图）
* DeepSeek-V3 纯文本模型无法直接接收图片。本方案引入轻量异步视觉转录链路：
* 拦截消息中的图片链接，通过本地或云端高速视觉端点（如 Gemini Flash / Qwen-VL）在 0.5s~1s 内提炼 20 字以内画面与文字要点（如 `“表情包配字‘这期拉了’，蓝发少女委屈撇嘴形象”`）；
* 将其伪装注入 DeepSeek 提示词，实现以极低成本让纯文本模型看懂群友表情包、梗图与报错截图。

### 3. 拟真打字体验与标点智能分段
* 模拟真人手感：回复按标点符号（`[!！?？~。]`）或换行符智能切分为最多两段短消息。
* 首句带 `@` 秒回，次句按字数计算延迟（0.6s~1.2s）连续发出，杜绝大段死板小作文。
* `/root` 生产模式或代码块输出时自动跳过分段，保持代码排版完整单条输出。

### 4. 彻底去人机味与避坑流水线
* **消灭无意义括号**：严格清理二次元提示词容易引发的句尾模式坍塌（如无休止的“（）”、“（”），代码层硬正则拦截。
* **消灭报幕腔**：无 `【指令汇报】`、`【记忆总结】` 等死板客服报幕，全面采用真人自然的随性回复。
* **戳一戳自然反应**：双击头像触发动态吐槽与 OneBot 动作回戳，绝不油腻。

### 5. 单群滑动窗口硬限频与全状态静默熔断（防封号禁言 + 零浪费 Token）
* **单群发信硬限频**：内置 60 秒滑动窗口速率保护，单群 60s 内最多发 8 条；遇到群聊刷屏触发阈值时，自动进入冷静期并调侃，彻底免疫封号。
* **主动与被动防禁言熔断（杜绝 Token 空转）**：
  * **主动监听 OneBot11 通知**：实时捕获 `group_ban`（全员禁言 / 自身被禁言）及 `group_decrease`，禁言期内所有艾特、关键词和戳一戳在入口处**前置短路拦截**，绝对不向大模型发起任何请求；
  * **解禁毫秒级恢复**：收到 `lift_ban` 自动清除静默状态，平稳无缝恢复聊天；
  * **被动容错熔断**：发信若被腾讯网关拦截（返回禁言/1200），自动触发该群 60 秒免调用大模型熔断保护。

### 6. 云崽（TRSS-Yunzai）共存协同与 AI 对线限流
* **双 Bot 丝滑协同**：前置拦截游戏前缀（`#`、`*`、`!`、`十连`、`面板` 等），由云崽秒出高清渲染图，AI 保持静默；群友问及功能时，AI 懂行指路群友如何触发。
* **AI 对线乐子限流**：群里其他机器人挑衅时，首轮现场毒舌造乐子，次轮现场宣布胜利，随后代码底层掐断追问并进入冷战期，杜绝两个 AI 死循环互掐。

### 7. 大肥鱼 11 大神级场景表情包联动
* 接入精选大肥鱼表情包仓库（[shirachino/astrbot-deepseek-meme-pack](https://github.com/shirachino/astrbot-deepseek-meme-pack)）。
* 根据语义精准触发：二次元米家梗（`_genshin`）、理财股市（`_stock`）、嘲笑（`_laugh`）、打工加班（`work`）、晚安睡觉（`sleep`）、要饭红包（`givemoney`）等 11 大场景。

---

## 🛠️ 快速部署

### 1. 克隆仓库与安装依赖
```bash
git clone https://github.com/lifefuck/qq-deepseek-realistic-bot.git
cd qq-deepseek-realistic-bot

# 创建并激活 Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装必要依赖
pip install -r requirements.txt
```

### 2. 拉取专属表情包资源
```bash
# 克隆大肥鱼表情包库到项目目录中
git clone --depth 1 https://github.com/shirachino/astrbot-deepseek-meme-pack.git meme-pack
```

### 3. 配置环境变量
复制 `.env.example` 为 `.env` 并填入你的实际配置：
```bash
cp .env.example .env
vim .env
```

配置说明：
```dotenv
# OneBot 11 正向 WebSocket 地址 (如 NapCat 默认 3001)
NAPCAT_WS_URL=ws://127.0.0.1:3001

# 机器人自身的 QQ 号
BOT_QQ=1000000000

# 最高超级管理员 QQ (支持 /root 切换和好感度管理)
ADMIN_QQ=1000000001

# 允许响应的白名单群号 (逗号分隔)
WHITELIST_GROUPS=123456789,987654321

# DeepSeek 文本大模型配置
DEEPSEEK_API_KEY=sk-xxxxxx
DEEPSEEK_API_URL=https://api.siliconflow.cn/v1/chat/completions
DEEPSEEK_MODEL=deepseek-ai/DeepSeek-V3

# 视觉提炼模型配置 (支持 Gemini Flash / Qwen-VL 等兼容 OpenAI 接口的模型)
VISION_API_KEY=sk-xxxxxx
VISION_API_URL=http://127.0.0.1:3000/v1/chat/completions
VISION_MODEL=gemini-3.8-flash-high
```

### 4. 启动机器人
```bash
python3 bot.py
```

或使用 systemd 托管后台常驻：
```ini
[Unit]
Description=QQ DeepSeek Realistic Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/qq-deepseek-realistic-bot
ExecStart=/path/to/qq-deepseek-realistic-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

---

## 💡 常用控制指令速查

| 指令 | 权限要求 | 说明与效果 |
| :--- | :--- | :--- |
| **`/memory`** | 全员可用 | 查看群内群友的好感度卡片与画像印象 |
| **`[戳一戳]`** | 全员可用 | 双击头像触发动态吐槽与 OneBot 动作回戳 |
| **`/root`** | 专属管理员 | 一键进入工程生产模式（严肃严谨排障、无字数限制、不演戏） |
| **`/user`** | 专属管理员 | 一键切回日常老群友摸鱼模式 |
| **`/new`** | 专属管理员 | 重置当前群（或私聊）的上下文历史（保留长期好感度） |
| **`/new all`** | 专属管理员 | 一键清空所有群及私聊的上下文 |
| **`/stop`** | 专属管理员 | 紧急熔断当前所有思考任务与 Debounce 队列 |
| **`/compress`** | 专属管理员 | 提取 40 字前情提要压缩上下文，腾出 Token |
| **`/b [数量]`** | 专属管理员 | 基于滚动长池提炼群聊要点简报（默认 50 条，最大 200 条） |

> 更多自然语言好感度控制（如 `把 123456 的好感度设为 80`）请参阅 [docs/persona-and-prompts.md](docs/persona-and-prompts.md)。

---

## 📄 License
MIT License.
Created by life.
