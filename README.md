# QQ 群聊机器人

一个住在 QQ 群里的机器人。底下接 OneBot 11（NapCat、LLOneBot 都行），模型走 OpenAI 兼容接口，默认 DeepSeek。

它不装客服。被 @ 会答，没人叫也会看气氛插一句；能看懂图和表情包，记得住群里谁是谁。

## 跑之前

- Python 3.10 以上
- 一个已经登录的 NapCat（或其他 OneBot 11 实现），开着 WebSocket
- 一个模型接口。默认配置指向 SiliconFlow 的 DeepSeek-V3，换别家只要兼容 OpenAI 格式就行
- 可选：Gemini key。不填也能跑，只是它看不懂图片内容

## 装

```bash
git clone https://github.com/lifefuck/qq-deepseek-realistic-bot.git
cd qq-deepseek-realistic-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 表情包单独一个仓库，放在项目根目录下
git clone --depth 1 https://github.com/shirachino/astrbot-deepseek-meme-pack.git meme-pack
```

## 配

```bash
cp .env.example .env
```

然后改 `.env`：

```dotenv
# NapCat 的 WebSocket 地址
NAPCAT_WS_URL=ws://127.0.0.1:3001

# 机器人自己的号，和管它的那个号
BOT_QQ=1000000000
ADMIN_QQ=1000000001

# 只在这些群里工作，逗号分隔
WHITELIST_GROUPS=123456789

DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_API_URL=https://api.siliconflow.cn/v1/chat/completions
DEEPSEEK_MODEL=deepseek-ai/DeepSeek-V3

# 不填就跳过识图
GEMINI_API_KEY=
```

`LLM_MAX_CONCURRENCY` 控制同时跑几个请求，机器吃不消就调小。

## 起

```bash
python3 bot.py
```

面板跟着起来，地址是 `http://127.0.0.1:8088`。它只监听本机，外网要自己套反代，别直接把端口放出去。

想用 systemd 托管，`deploy/systemd/` 里有现成的 unit，把路径改成你的就行。

## 指令

| 指令 | 谁能用 | 干什么 |
| :--- | :--- | :--- |
| `/memory` | 所有人 | 看本群的人员档案，合并转发成聊天记录 |
| `/new` | 管理员 | 清掉当前群或私聊的上下文 |
| `/new all` | 管理员 | 清掉所有群的上下文 |
| `/stop` | 管理员 | 停止正在生成的回复。群里发只停这个群，私聊发才是全局停 |
| `/on` | 管理员 | 恢复本群全部功能 |
| `/off` | 管理员 | 关掉本群全部功能，只认主人 |

戳一戳也算一次互动，之后 5 分钟内不用反复 @ 它，最多连着答 5 次。

同一个人 2 分钟内连着 @ 它 5 次，会被冷却 60 秒。这是防刷屏，不是 bug。

## 面板

按群单独调：接话意愿、作息、戳一戳、静音。还能看群友档案和实时日志。

配置写在 `group_configs.json`，密码哈希在 `.dashboard_auth`。这两个都不进版本库。

## 一些取舍

**不做拟真延迟。** 模型吐完就发，不模拟"正在输入"、不加气泡间隔。群里要的是快。

**不输出动作旁白。** `（甩尾巴）`、`*叹气*` 这类会被剥掉，只留说的话。

**短句优先。** 闲聊十来个字，真讲正事才铺开。

**群里不带长篇历史。** 默认只喂当前这句，避免接错人，也省 token。管理员私聊才给完整时间线。

## 目录

```
bot.py                入口，连接、调度、记忆
handlers/             群聊、私聊、管理指令
services/             模型调用、发送、视觉、情绪
meme_manager.py       梗库三层：核心梗、检索、轮换
media_resolver.py     抖音 / 小红书 / B站短链解析
web_dashboard.py      面板后端
web_ui/               面板前端（Vue 3）
docs/                 机制说明
scripts/              自检与工具脚本
```

## 改之前

```bash
./scripts/install_hooks.sh
```

装个 pre-commit，跑语法检查、循环导入检查、冒烟测试。不装也能提交，就是容易把东西改坏。

## 作者

life
