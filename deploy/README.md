# 生产部署与双 Bot 协同配置

本目录提供生产环境所需的 systemd 守护单元配置与 TRSS-Yunzai（云崽）整合文件。

## 文件目录

- `systemd/`
  - `qq-deepseek-bot.service`：AI 群聊机器人的 systemd 用户态服务配置。
  - `redis-local.service`：云崽使用的本地 Redis 服务配置。
  - `trss-yunzai.service`：TRSS-Yunzai 云崽服务配置。
- `yunzai/`
  - `help.js`：9 大模块聚合帮助面板配置文件（覆盖原神、星铁、绝区零、鸣潮、异环/塔吉多、逍遥图鉴、点歌/小游戏、锅巴管理面板）。替换到 `plugins/miao-plugin/config/help.js` 即可生效。

## Systemd 托管步骤（Linux 用户态）

将 service 文件复制到 `~/.config/systemd/user/` 目录下：

```bash
mkdir -p ~/.config/systemd/user

# 复制单元文件
cp deploy/systemd/*.service ~/.config/systemd/user/

# 重载 systemd 用户配置
systemctl --user daemon-reload

# 设置开机自启并启动
systemctl --user enable --now redis-local.service
systemctl --user enable --now trss-yunzai.service
systemctl --user enable --now qq-deepseek-bot.service

# 查看运行状态
systemctl --user status qq-deepseek-bot.service
```

> **提示**：为确保用户登出后服务依然常驻，建议开启 systemd 驻留：
> ```bash
> loginctl enable-linger $USER
> ```

## TRSS-Yunzai 帮助面板配置

将 `deploy/yunzai/help.js` 覆盖到喵喵插件的配置目录下：
```bash
cp deploy/yunzai/help.js /path/to/trss-yunzai/plugins/miao-plugin/config/help.js
```
重启云崽服务后，群内发送 `#帮助` 或 `#菜单` 即可展示 9 栏游戏帮助图。
