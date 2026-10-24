# QQ DeepSeek Realistic Bot (大肥鱼拟真群聊机器人)

> 基于 OneBot 11 (NapCat / LLOneBot) 与 DeepSeek-V3 的高性能、去人机味、低成本拟真 QQ 群聊智能助手。

针对当前群聊机器人普遍存在的 **Token 成本指数级爆炸、上下文串台认错人、AI 报幕腔/油腻人设、频繁触发腾讯风控** 等落地痛点而设计。支持秒级多模态识图与大肥鱼 11 大场景表情包精准联动。

---

## 🌟 核心特性与架构亮点

### 1. 单轮未读轻量会话（Token 消耗暴降 90%~95%）
* **拒绝无脑滑动窗口**：彻底抛弃将全群最近几十条长篇记录硬拼进上下文的传统做法，解决群聊冷清时上下文缓存失效导致的 Token 费用指数级爆炸。
* **精准聚焦，绝不串台**：针对每一次触发仅将当前发言群友的最新输入（若为 QQ 引用回复，自动抓取并拼接原消息上下文）组装成单轮会话，单次交互 Token 恒定在 200~300 tokens。
* **独立好感度与长期记忆**：用户画像、好感度增减与性格印象交由独立持久化 JSON 维护，后台异步评估落地，前台秒回零阻塞。

### 2. 双模态视觉转录链路（低成本秒级识图）
* DeepSeek-V3 纯文本模型无法直接接收图片。本方案引入轻量异步视觉转录链路：
* 拦截消息中的图片链接，通过本地或云端高速视觉端点（如 Gemini 3.8 Flash / Qwen-VL）在 0.5s~1s 内提炼 20 字以内画面与文字要点（如 `“表情包配字‘这期拉了’，蓝发少女委屈撇嘴形象”`）；
* 将其伪装注入 DeepSeek 提示词，实现以极低成本让纯文本模型看懂群友表情包、梗图与报错截图。

### 3. 拟真打字体验与标点智能分段
* 模拟真人手感：回复按标点符号（`[!！?？~。]`）或换行符智能切分为最多两段短消息。
* 首句带 `@` 秒回，次句按字数计算延迟（0.6s~1.2s）连续发出，杜绝大段死板小作文。
* `/root` 生产模式或代码块输出时自动跳过分段，保持代码排版完整单条输出。

### 4. 彻底去人机味与避坑流水线
* **消灭无意义括号**：严格清理二次元提示词容易引发的句尾模式坍塌（如无休止的“（）”、“（”），代码层硬正则拦截。
* **消灭报幕腔**：无 `【指令汇报】`、`【记忆总结】` 等死板客服报幕，全面采用真人自然的随性回复。
* **戳一戳自然反应**：群友戳一戳随手吐槽（如 `?戳我干嘛`、`手痒是吧`），绝不油腻。

### 5. 单群滑动窗口硬限频（防腾讯风控）
* 腾讯对单群频繁发信风控极严。内置 60 秒滑动窗口速率保护，单群 60s 内最多发 8 条；
* 遇到群友多人群聊轰炸触发阈值时，自动进入冷静期并调侃“让本鲸宝喘口气”，彻底免疫封号与群禁言。

### 6. 大肥鱼 11 大神级场景表情包联动
* 接入精选大肥鱼表情包仓库（[shirachino/astrbot-deepseek-meme-pack](https://github.com/shirachino/astrbot-deepseek-meme-pack)）。
* 根据语义精准触发：
  * **二次元/米家梗**（`_genshin`）：原神、鸣潮、崩铁、感觉不如...
  * **理财/股市**（`_stock` / `_green`）：股票、基金、A股、跌了、亏麻、韭菜
  * **猖狂嘲笑**（`_laugh`）：哈哈、笑死、吃瘪、绷不住
  * **职场打工**（`work`）：上班、加班、打工、老板、周一
  * **睡觉晚安**（`sleep`）：困了、熬夜、失眠、晚安
  * **要饭红包**（`givemoney`）：v我50、红包、乞讨
  * **以及** `angry`（炸毛）、`fool`（逆天/草）、`baka`（笨蛋/嫌弃）、`happy`（好耶/喜欢）、`confused`（疑惑）。
* **防截断安全保障**：使用 OneBot 11 原生结构化图片消息独立下发，绝不拼在文本尾部，杜绝 base64 截断乱码。

### 7. 管理员双运行模式（一键无缝热切换）
* **/user 拟真群友模式**：大肥鱼口吻，随性互怼、拌嘴打趣。
* **/root 生产维护模式**：专为开发者设立，退出所有娱乐口癖，转变为技术专家助理，解除字数限制，提供严谨实用的排障与代码交付。

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

## 💡 管理员常用指令

* `/root`：切换至严肃技术开发助理模式（长回答、高准确率、无拟人演戏）。
* `/user`：恢复大肥鱼群聊拟真口吻。
* `/new`：重置当前会话上下文。
* `/stop`：中止当前正在排队或思考中的大模型任务。
* `/compress`：精炼压缩历史记忆。
* `给 123456 加 10 点好感度` / `把 123456 好感度设为 80`：动态调节指定群友的好感度档案。

---

## 📄 License
MIT License.
Created by life.
