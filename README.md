# QQ 群聊机器人

基于 OneBot 11（NapCat、LLOneBot 等）和 DeepSeek 的 QQ 机器人，带一个本地 Web 面板。

群聊和私聊都能用。被 @、被回复或被戳一戳时会回复；开了主动接话后，也会在话题合适时插几句。支持识图、解析抖音/小红书/B站短链、按情绪选表情包，记忆按群和群友分开存。

## 环境要求

- Python 3.10 及以上
- 一个运行中的 NapCat 或其他 OneBot 11 实现，WebSocket 已开启
- 一个 OpenAI 兼容的模型接口。默认用 SiliconFlow 的 DeepSeek-V3
- 可选：Gemini API key。不填会跳过图片理解，图片按普通消息处理

## 安装

```bash
git clone https://github.com/lifefuck/qq-deepseek-realistic-bot.git
cd qq-deepseek-realistic-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 表情包是另一个仓库，放在项目根目录下
git clone --depth 1 https://github.com/shirachino/astrbot-deepseek-meme-pack.git meme-pack
```

## 配置

```bash
cp .env.example .env
```

| 变量 | 说明 |
| :--- | :--- |
| `NAPCAT_WS_URL` | OneBot 11 的 WebSocket 地址 |
| `BOT_QQ` | 机器人自己的 QQ 号 |
| `ADMIN_QQ` | 管理员 QQ 号，可用管理指令 |
| `WHITELIST_GROUPS` | 启用的群号，英文逗号分隔 |
| `DEEPSEEK_API_KEY` | 模型接口的 key |
| `DEEPSEEK_API_URL` | 接口地址，兼容 OpenAI 格式即可 |
| `DEEPSEEK_MODEL` | 模型名 |
| `GEMINI_API_KEY` | 识图用，可留空 |
| `GEMINI_MODEL` | 识图模型名 |
| `LLM_MAX_CONCURRENCY` | 同时进行的模型请求数，按机器性能调整 |

## 启动

```bash
python3 bot.py
```

面板随服务启动，地址 `http://127.0.0.1:8088`。默认只监听本机，需要外网访问的话在前面加反向代理。

systemd 的 unit 文件在 `deploy/systemd/`，把路径改成自己的即可。

## 指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `/memory` | 所有人 | 查看本群的人员档案，以合并转发形式发出 |
| `/new` | 管理员 | 清空当前群或私聊的上下文 |
| `/new all` | 管理员 | 清空所有群的上下文 |
| `/stop` | 管理员 | 停止正在生成的回复。在群里发只停本群，在私聊发停全部 |
| `/on` | 管理员 | 恢复本群的全部功能 |
| `/off` | 管理员 | 关闭本群的全部功能，只响应管理员 |

被 @ 或戳一戳后，5 分钟内不用重复 @，最多连续回复 5 次。同一个人 2 分钟内 @ 超过 5 次会冷却 60 秒。

## 面板

可以按群调整接话概率、作息、戳一戳、静音，查看群友档案和运行日志。

群配置保存在 `group_configs.json`，面板密码哈希在 `.dashboard_auth`。这两个文件都在 `.gitignore` 里，不会提交。

## 行为说明

- 不模拟打字延迟，模型返回后直接发送，只在必要时按标点分段
- 括号里的动作描写（如 `（甩尾巴）`）会被过滤，只保留文字内容
- 群聊默认只把当前这条消息交给模型，不带长历史；管理员私聊会带完整时间线
- 回复长度按场景变化：闲聊偏短，讨论具体问题时正常展开
- 夜间 23:00 到次日 07:00 进入休眠，不主动插话，被 @ 时只回很短的一句

## 目录结构

```
bot.py                入口：连接、调度、记忆
handlers/             群聊、私聊、管理指令
services/             模型调用、消息发送、识图、情绪
meme_manager.py       梗库：核心梗、检索、轮换
media_resolver.py     抖音 / 小红书 / B站短链解析
web_dashboard.py      面板后端
web_ui/               面板前端（Vue 3）
docs/                 机制与提示词说明
scripts/              自检和工具脚本
```

## 开发

```bash
./scripts/install_hooks.sh
```

安装 pre-commit 钩子，提交前会跑语法检查、循环导入检查和冒烟测试。

## 作者

life
