# 迁移到 AstrBot 的方案

写于 2026-11-15。目标：NapCat 不动，把「功能」交给 AstrBot，「聊天」暂时留在这套自研 bot 上，分批切换、每批都能回滚。

## 0. 先分清谁是谁

```
QQ 服务器
   ↓
NapCat（协议端，登录 QQ、收发消息，不会思考）
   ↓ WebSocket
   ├── qq-deepseek-bot（本仓库，负责聊天）
   ├── 云崽 TRSS-Yunzai（负责游戏指令）
   └── AstrBot（要加的那个，负责任意插件）
```

AstrBot 本身也靠 NapCat 才能连 QQ（它的 `aiocqhttp` 适配器就是干这个的）。所以这次迁移**不需要、也不应该**动 NapCat。

## 1. 要分三批，别一次切完

| 批次 | 内容 | 耗时 | 风险 | 回滚 |
|---|---|---|---|---|
| A | 新号 + 第二个 NapCat + AstrBot 容器，只装功能插件 | 半天 | 低 | 停容器即可 |
| B | 功能逐个接管，本仓库对应指令下线 | 1~2 天 | 低 | 把指令重新启用 |
| C | 聊天本体迁移（提示词、语料、记忆档案） | 3~5 天 | 中 | 保持两套并行跑一周再切 |

A、B 两批做完，你就同时有了「会聊天的」和「功能全的」。C 是可选项——做完实验再决定。

## 2. 批次 A：把 AstrBot 跑起来

### 2.1 准备一个新 QQ 号

必须用第二个号，理由：一个 NapCat 实例只能挂一个 QQ 登录态；同号两个框架会抢同一句话。

### 2.2 起第二个 NapCat 实例

在跑 NapCat 的那台机器（当前是 `10.0.0.2`）上再起一份，和现有实例隔离：

- 独立数据目录（不要复用原来的 `napcat/config`，登录态会互相覆盖）
- 独立端口：WebSocket 用 `3002`（现有的是 `3001`）
- 用新号扫码登录

### 2.3 起 AstrBot 容器

官方 compose（来自 `AstrBotDevs/AstrBot` 的 `compose.yml`），放到 `~/projects/astrbot/docker-compose.yml`：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: always
    security_opt:
      - no-new-privileges:true
    ports:
      - "6285:6185"   # WebUI（避开 6185 冲突就换宿主机端口）
      - "6299:6199"   # OneBot v11 WebSocket
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - ./data:/AstrBot/data
      - /etc/localtime:/etc/localtime:ro
```

```bash
docker compose up -d && docker compose logs -f
```

### 2.4 配平台适配器

WebUI（`http://<宿主机>:6285`）→ 配置 → 消息平台 → 新增 `aiocqhttp`：

- 反向 WS 模式下 AstrBot 监听 `ws://0.0.0.0:6199/ws`，在 NapCat 里加一个 WebSocket 客户端指过去
- 或用正向 WS：AstrBot 连 `ws://10.0.0.2:3002`
- 记住默认账号密码是 `astrbot`/`astrbot`，第一件事是改掉

### 2.5 配模型

WebUI → 服务提供商 → 新增 OpenAI 兼容：

- Base URL 用你现在这套的接口地址（`DEEPSEEK_API_KEY` 那家）
- 模型填你现在用的那个 flash 模型
- Key 从 `~/.config/qq-deepseek-bot.env` 里抄，不要写进仓库

### 2.6 装插件

WebUI → 插件市场，搜关键词直接装。首批建议（都是市场里现成的）：

| 用途 | 关键词 |
|---|---|
| QQ 空间 | `qzone`（QzoneUltra / qzone_tools，需 NapCat > 4.17.55，你现在是 4.18.41） |
| 点歌 | `点歌` `music` |
| 抽卡/签到 | `抽卡` `签到` |
| 图片/表情 | `图片` `表情` `meme` |
| 查询类 | `天气` `翻译` `百科` |

装完逐个在测试群里试，确认**没有**在普通聊天里自动接话（这是同号并存唯一的坑，用新号就不存在）。

## 3. 批次 B：功能接管对照表

| 现在这四个谁在管 | 迁到 AstrBot 后 |
|---|---|
| 云崽的游戏指令（原神/星铁/米游社） | 可以继续留云崽，也可以找 AstrBot 的同类插件替换 |
| 本仓库的表情包/斗图 | 换 AstrBot 表情包插件，或保留（我们这套的选择逻辑是自己调的） |
| 本仓库的短链解析（抖音/小红书/B站） | AstrBot 有解析类插件，装了就把本仓库那段关掉 |
| 本仓库的 `/logs`、`/memary` 运维指令 | 用 WebUI 的日志页替代，不迁 |

每接管一项，就在 `group_configs.json` 里把本仓库对应的开关关掉，避免两边都回。

## 4. 批次 C：聊天本体要不要迁

这是唯一需要你拍板的部分。迁的话，要处理三样东西：

| 资产 | 位置 | 迁移办法 |
|---|---|---|
| 人设提示词 | `prompts.py` 的 `SYSTEM_PROMPT`（3355 字节） | 已导出成 `exports/persona.txt`，直接粘进 AstrBot 的人格设置 |
| 95 位群友档案 + 好感度 | `user_memories.json` | 写个 AstrBot 插件，读这个 JSON 后按群注入 prompt；好感度是半衰期衰减逻辑，得一起搬 |
| 640 组群表达样例 | `data/style_fewshot.json` | 同上，插件里抽 3~5 条注入 |
| 插话时机打分 | `services/interject_planner.py` | **没有等价物，只能写成插件**，这是最难的一块 |
| 反模板闸、分段、表情选择 | `services/anti_template.py`、`messaging.py` | AstrBot 有回复分段设置，但复读闸没有，得插件化 |

结论：C 批不是「配置一下」，是「把我们这套的核心写成 2000 行左右的 AstrBot 插件」。做完的好处是插件市场和 WebUI 白拿；不做的坏处是聊天继续靠我维护。

## 5. 验收清单

批次 A 完成的标准：

- [ ] 新号能在群里说话，AstrBot 日志里有消息进出
- [ ] WebUI 能改配置、能看日志
- [ ] 装 2 个以上插件并正常工作
- [ ] 现有这套 bot 和云崽**行为完全没变**（同一个群，两边都在跑）
- [ ] 关掉 AstrBot 容器，其它两个不受影响

批次 C 完成的标准：

- [ ] 同一批消息，AstrBot 的回复不比现在差（并排贴出来比对，至少 20 组）
- [ ] 群友档案、好感度、语料能正确注入
- [ ] 连续跑一周没有重复回复、没有抢话

## 6. 回滚

- 本仓库的 systemd 服务 `qq-deepseek-bot` 不动，随时 `systemctl --user start` 切回
- 迁移前的完整备份：`~/backups/qq-deepseek-bot-20261115-2057.tar.gz`（198M，含代码、data、.git、meme-pack）
- Git 历史快照：`~/backups/qq-deepseek-bot-20261115-2100.bundle`
- AstrBot 的数据全在 `./data` 卷里，删容器不丢配置

## 7. 已知风险

1. **空间/账号类插件走 Cookie，不走官方接口**，自动发说说、批量点赞属于风控高危动作。用新号、控制频率、别开每日自动发。
2. **同号两个框架会抢话**。本方案用第二个号规避；如果坚持同号，必须在 AstrBot 侧关掉对话管线，只留功能插件。
3. **插件质量参差**。1329 个插件里大量是个人作品，装之前看一眼最后提交时间，一年没更新的别装。
4. **两套并存期间日志会分裂**，排查问题时先确认是哪套在回。
