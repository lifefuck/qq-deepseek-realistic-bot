"""
大肥鱼 AI 控制台 · 轻量现代浅色 Web 仪表盘
原生基于 aiohttp 实现，零外部依赖，自适应手机与 PC 端。
"""
import collections
import datetime
import html
import logging
import time
import base64
import hmac
import os
from typing import Callable, Deque, Dict, Any, Optional
from aiohttp import web

recent_web_logs: Deque[str] = collections.deque(maxlen=200)

class DequeLogHandler(logging.Handler):
    """拦截近期日志供 Web 仪表盘实时查看"""
    def emit(self, record):
        try:
            msg = self.format(record)
            recent_web_logs.append(msg)
        except Exception:
            pass

def setup_log_capture(target_logger: logging.Logger):
    """挂载内存日志捕获器"""
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    handler = DequeLogHandler()
    handler.setFormatter(formatter)
    target_logger.addHandler(handler)

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>大肥鱼 AI 控制台 · DeepSeek-V3</title>
  <style>
    :root {
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --card-border: #e2e8f0;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-light: #eff6ff;
      --success: #10b981;
      --success-light: #ecfdf5;
      --warning: #f59e0b;
      --warning-light: #fffbeb;
      --info: #0284c7;
      --info-light: #f0f9ff;
      --gold: #d97706;
      --gold-bg: #fef3c7;
      --radius: 12px;
      --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
      --shadow: 0 4px 6px -1px rgb(0 0 0 / 0.07), 0 2px 4px -2px rgb(0 0 0 / 0.05);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.5;
      padding: 24px 16px;
      max-width: 1280px;
      margin: 0 auto;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--card-border);
    }
    .header-left {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .bot-avatar {
      width: 52px;
      height: 52px;
      border-radius: 50%;
      border: 2px solid var(--card-border);
      box-shadow: var(--shadow-sm);
    }
    .title-box h1 {
      font-size: 20px;
      font-weight: 700;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .title-box p {
      font-size: 13px;
      color: var(--text-muted);
      margin-top: 2px;
    }
    .status-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid transparent;
    }
    .badge-success { background: var(--success-light); color: var(--success); border-color: #a7f3d0; }
    .badge-primary { background: var(--primary-light); color: var(--primary); border-color: #bfdbfe; }
    .badge-warning { background: var(--warning-light); color: var(--warning); border-color: #fde68a; }
    .badge-info { background: var(--info-light); color: var(--info); border-color: #bae6fd; }
    .badge-slate { background: #f1f5f9; color: #475569; border-color: #cbd5e1; }
    .badge-gold { background: var(--gold-bg); color: var(--gold); border-color: #fde68a; }

    .grid-top {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 18px 20px;
      box-shadow: var(--shadow-sm);
      transition: box-shadow 0.2s ease;
    }
    .card:hover {
      box-shadow: var(--shadow);
    }
    .card-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
    }
    .card-value {
      font-size: 24px;
      font-weight: 700;
      color: var(--text-main);
    }
    .card-sub {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 4px;
    }

    .section-title {
      font-size: 16px;
      font-weight: 700;
      color: var(--text-main);
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .group-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .group-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--shadow-sm);
    }
    .group-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    .group-id {
      font-weight: 700;
      font-size: 15px;
      color: var(--primary);
    }
    .group-stat-row {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      padding: 5px 0;
      border-bottom: 1px dashed #f1f5f9;
    }
    .group-stat-label { color: var(--text-muted); }
    .group-stat-val { font-weight: 600; color: var(--text-main); }

    /* 按群独立调节控件样式 */
    .controls-box {
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin: 8px 0;
    }
    .control-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 13px;
    }
    .control-label {
      font-weight: 600;
      color: var(--text-main);
    }
    .btn-mode-toggle {
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.15s ease;
    }
    .btn-mode-user {
      background: var(--primary-light);
      color: var(--primary);
      border-color: #bfdbfe;
    }
    .btn-mode-root {
      background: var(--warning-light);
      color: var(--warning);
      border-color: #fde68a;
    }
    .switch-pill {
      display: inline-flex;
      background: #e2e8f0;
      border-radius: 9999px;
      padding: 2px;
      cursor: pointer;
      user-select: none;
    }
    .switch-pill span {
      padding: 2px 10px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      color: #64748b;
      transition: all 0.15s ease;
    }
    .switch-pill.active-on .opt-on {
      background: #10b981;
      color: #ffffff;
      box-shadow: var(--shadow-sm);
    }
    .switch-pill.active-off .opt-off {
      background: #94a3b8;
      color: #ffffff;
      box-shadow: var(--shadow-sm);
    }
    .slider-box {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 140px;
    }
    .slider-box input[type=range] {
      flex: 1;
      accent-color: var(--primary);
      cursor: pointer;
    }
    .slider-val {
      font-size: 12px;
      font-weight: 700;
      color: var(--primary);
      min-width: 32px;
      text-align: right;
    }
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #0f172a;
      color: #ffffff;
      padding: 10px 18px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      box-shadow: var(--shadow);
      opacity: 0;
      transition: opacity 0.2s ease, transform 0.2s ease;
      transform: translateY(10px);
      pointer-events: none;
      z-index: 9999;
    }
    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }

    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }
    .tab-btn {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .tab-btn:hover {
      background: #f1f5f9;
      color: var(--text-main);
    }
    .tab-btn.active {
      background: var(--primary);
      color: #ffffff;
      border-color: var(--primary);
      box-shadow: var(--shadow-sm);
    }

    .memories-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .user-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .user-card.is-master {
      border: 1.5px solid #fcd34d;
      background: linear-gradient(180deg, #fffdfa 0%, #ffffff 100%);
    }
    .user-card-top {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .user-avatar {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: #e2e8f0;
      border: 1px solid var(--card-border);
    }
    .user-meta { flex: 1; min-width: 0; }
    .user-name {
      font-size: 14px;
      font-weight: 700;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .user-qq {
      font-size: 12px;
      color: var(--text-muted);
    }
    .favor-box {
      margin-top: 4px;
    }
    .favor-header {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 4px;
    }
    .favor-bar-bg {
      height: 8px;
      background: #f1f5f9;
      border-radius: 9999px;
      overflow: hidden;
    }
    .favor-bar-fill {
      height: 100%;
      border-radius: 9999px;
      transition: width 0.4s ease;
    }
    .user-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 4px;
    }
    .user-tag {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 6px;
      background: #f1f5f9;
      color: #475569;
    }

    .log-box {
      background: #ffffff;
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--shadow-sm);
    }
    .log-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
    }
    .log-content {
      background: #0f172a;
      color: #e2e8f0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 12px;
      line-height: 1.6;
      padding: 16px;
      border-radius: 8px;
      height: 520px;
      max-height: 70vh;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .log-time { color: #38bdf8; }
    .log-info { color: #a7f3d0; }
    .log-warn { color: #fde047; }
    .log-err { color: #f87171; }

    @media (max-width: 640px) {
      body { padding: 16px 12px; }
      .header-left { flex-direction: column; align-items: flex-start; gap: 8px; }
      .status-badges { justify-content: flex-start; }
    }
  </style>
</head>
<body>

  <header>
    <div class="header-left">
      <img class="bot-avatar" src="https://dummyimage.com/100x100/2563eb/ffffff?text=FISH" alt="Bot Avatar" onerror="this.src='https://dummyimage.com/100x100/2563eb/ffffff?text=FISH'">
      <div class="title-box">
        <h1>🐟 大肥鱼 AI 控制台</h1>
        <p>基座: DeepSeek-V3 · 实体已就绪 · 双 Bot 协同中</p>
      </div>
    </div>
    <div class="status-badges" id="headerBadges">
      <span class="badge badge-success" id="badgeConn">● 正在连接...</span>
      <span class="badge badge-primary" id="badgeMode">💬 模式: 加载中</span>
      <span class="badge badge-info" id="badgeSleep">☀️ 作息: 检测中</span>
    </div>
  </header>

  <!-- 顶部状态卡片 -->
  <div class="grid-top">
    <div class="card">
      <div class="card-title">白名单群聊数量</div>
      <div class="card-value" id="valGroupCount">4 个群</div>
      <div class="card-sub" id="valGroupSub">多群已载入</div>
    </div>
    <div class="card">
      <div class="card-title">已建立群友档案</div>
      <div class="card-value" id="valUserCount">-- 人</div>
      <div class="card-sub">按群严格隔离 · 动态好感度评估</div>
    </div>
    <div class="card">
      <div class="card-title">免 @ 追问配额机制</div>
      <div class="card-value">5分钟 / 5次</div>
      <div class="card-sub">新 @ 立即重置配额 · 智能抗死缠烂打</div>
    </div>
    <div class="card">
      <div class="card-title">服务运行时间</div>
      <div class="card-value" id="valUptime">--</div>
      <div class="card-sub" id="valStartTime">启动时间: --</div>
    </div>
  </div>

  <!-- 群聊冷热场雷达 -->
  <div class="section-title">📡 群聊活跃度与冷热场插话雷达</div>
  <div class="group-grid" id="groupGrid">
    <!-- 动态填入群卡片 -->
  </div>

  <!-- 群友档案与好感度排行榜 -->
  <div class="section-title">💖 群友个性化记忆与好感度档案</div>
  <div class="tabs" id="groupTabs">
    <!-- 动态填入选项卡 -->
  </div>
  <div class="memories-grid" id="memoriesGrid">
    <!-- 动态填入群友卡片 -->
  </div>

  <!-- 实时运行日志 -->
  <div class="section-title">📜 实时交互日志流</div>
  <div class="log-box">
    <div class="log-header">
      <span style="font-size: 13px; color: var(--text-muted);">自动同步最近 50 条后台交互与反模板削平动作</span>
      <label style="font-size: 12px; color: var(--text-muted); cursor: pointer;">
        <input type="checkbox" id="chkAutoScroll" checked> 自动滚到底部
      </label>
    </div>
    <div class="log-content" id="logBox">加载日志中...</div>
  </div>

  <script>
    let currentTab = "";
    let latestData = null;

    function formatRelative(secondsAgo) {
      if (!secondsAgo || secondsAgo < 0) return "从未";
      if (secondsAgo < 60) return Math.floor(secondsAgo) + " 秒前";
      if (secondsAgo < 3600) return Math.floor(secondsAgo / 60) + " 分钟前";
      if (secondsAgo < 86400) return Math.floor(secondsAgo / 3600) + " 小时前";
      return Math.floor(secondsAgo / 86400) + " 天前";
    }

    function formatUptime(sec) {
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      if (h > 0) return `${h}小时 ${m}分 ${s}秒`;
      if (m > 0) return `${m}分 ${s}秒`;
      return `${s}秒`;
    }

    async function fetchState() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        latestData = data;
        renderHeader(data);
        renderOverview(data);
        renderGroups(data);
        renderTabs(data);
        renderMemories(data);
        renderLogs(data.logs || []);
      } catch (e) {
        document.getElementById('badgeConn').className = "badge badge-warning";
        document.getElementById('badgeConn').innerText = "● 连接断开，重连中...";
      }
    }

    function renderHeader(data) {
      const badgeConn = document.getElementById('badgeConn');
      badgeConn.className = "badge badge-success";
      badgeConn.innerText = "● NapCat 在线 (QQ " + data.bot.qq + ")";

      const badgeMode = document.getElementById('badgeMode');
      if (data.bot.admin_mode === 'root') {
        badgeMode.className = "badge badge-warning";
        badgeMode.innerText = "🛠️ 技术伙伴模式 (/root)";
      } else {
        badgeMode.className = "badge badge-primary";
        badgeMode.innerText = "💬 闲聊陪伴模式 (/user)";
      }

      const badgeSleep = document.getElementById('badgeSleep');
      if (data.bot.is_sleeping) {
        if (data.bot.sleep_override) {
          badgeSleep.className = "badge badge-warning";
          badgeSleep.innerText = "⚡ 主人专属唤醒中 (30分钟)";
        } else {
          badgeSleep.className = "badge badge-slate";
          badgeSleep.innerText = "🌙 拟真睡眠中 (01:00~07:00)";
        }
      } else {
        badgeSleep.className = "badge badge-success";
        badgeSleep.innerText = "☀️ 白天精神满满";
      }
    }

    function renderOverview(data) {
      document.getElementById('valUptime').innerText = formatUptime(data.bot.uptime);
      document.getElementById('valStartTime').innerText = "开机时刻: " + data.bot.start_time_str;
      
      let totalUsers = 0;
      if (data.memories) {
        for (const k in data.memories) {
          totalUsers += Object.keys(data.memories[k]).length;
        }
      }
      document.getElementById('valUserCount').innerText = totalUsers + " 位群友";

      if (data.groups) {
        document.getElementById('valGroupCount').innerText = data.groups.length + " 个群";
        const sub = document.getElementById('valGroupSub');
        if (sub) {
          sub.innerText = data.groups.map(g => (g.group_name && !g.group_name.startsWith('群聊 ')) ? `${g.group_name} (${g.group_id})` : `${g.group_id}`).join(' / ');
        }
      }
    }

    let toastTimer = null;
    function showToast(msg) {
      let t = document.getElementById('toastBox');
      if (!t) {
        t = document.createElement('div');
        t.id = 'toastBox';
        t.className = 'toast';
        document.body.appendChild(t);
      }
      t.innerText = msg || "设置已保存并实时生效";
      t.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
    }

    async function setGroupConfig(groupId, key, val) {
      try {
        const res = await fetch('/api/group/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ group_id: groupId, key: key, value: val })
        });
        if (res.ok) {
          showToast("群 " + groupId + " 已更新 " + key);
          fetchState();
        } else {
          showToast("保存失败");
        }
      } catch (e) {
        showToast("网络请求错误");
      }
    }

    function renderGroups(data) {
      const container = document.getElementById('groupGrid');
      if (!data.groups) return;

      let html = "";
      for (const g of data.groups) {
        const cfg = g.config || {};
        const isRoot = cfg.mode === "root";
        const isInterjectOn = cfg.enable_interject !== false;
        const isSleepOn = cfg.enable_sleep !== false;
        const isPokeOn = cfg.enable_poke !== false;
        const isGamesOn = cfg.enable_games !== false;
        const isSearchOn = cfg.enable_search !== false;
        const probPct = Math.round((cfg.interject_prob || 0.35) * 100);

        let badgeClass = "badge-slate";
        if (g.interject_why.includes("热场")) badgeClass = "badge-success";
        else if (g.interject_why.includes("冷场")) badgeClass = "badge-info";

        const gName = g.group_name || `群 ${g.group_id}`;
        html += `
          <div class="group-card">
            <div class="group-card-header">
              <span class="group-id">
                ${gName} <span style="font-size:12px; color:var(--text-muted); font-weight:normal;">(${g.group_id})</span>
                <button class="btn-mode-toggle ${isRoot ? 'btn-mode-root' : 'btn-mode-user'}"
                  onclick="setGroupConfig('${g.group_id}', 'mode', '${isRoot ? 'user' : 'root'}')">
                  ${isRoot ? '🛠️ root模式 (点切闲聊)' : '💬 user闲聊 (点切root)'}
                </button>
              </span>
              <span class="badge ${badgeClass}">${g.interject_why}</span>
            </div>

            <!-- 独立调节控制区 -->
            <div class="controls-box">
              <div class="control-row">
                <span class="control-label">主动插话开关</span>
                <div class="switch-pill ${isInterjectOn ? 'active-on' : 'active-off'}"
                  onclick="setGroupConfig('${g.group_id}', 'enable_interject', ${!isInterjectOn})">
                  <span class="opt-on">开启</span>
                  <span class="opt-off">静音</span>
                </div>
              </div>

              <div class="control-row">
                <span class="control-label">插话意愿 (基准概率)</span>
                <div class="slider-box">
                  <input type="range" min="5" max="80" step="5" value="${probPct}"
                    onchange="setGroupConfig('${g.group_id}', 'interject_prob', this.value / 100)"
                    oninput="this.nextElementSibling.innerText = this.value + '%'">
                  <span class="slider-val">${probPct}%</span>
                </div>
              </div>

              <div class="control-row">
                <span class="control-label">01:00~07:00 睡眠作息</span>
                <div class="switch-pill ${isSleepOn ? 'active-on' : 'active-off'}"
                  onclick="setGroupConfig('${g.group_id}', 'enable_sleep', ${!isSleepOn})">
                  <span class="opt-on">入睡</span>
                  <span class="opt-off">全天</span>
                </div>
              </div>

              <div class="control-row">
                <span class="control-label">戳一戳互动与回戳</span>
                <div class="switch-pill ${isPokeOn ? 'active-on' : 'active-off'}"
                  onclick="setGroupConfig('${g.group_id}', 'enable_poke', ${!isPokeOn})">
                  <span class="opt-on">允许</span>
                  <span class="opt-off">忽略</span>
                </div>
              </div>

              <div class="control-row">
                <span class="control-label">多人文字群游戏 (炸弹/roll)</span>
                <div class="switch-pill ${isGamesOn ? 'active-on' : 'active-off'}"
                  onclick="setGroupConfig('${g.group_id}', 'enable_games', ${!isGamesOn})">
                  <span class="opt-on">开启</span>
                  <span class="opt-off">禁用</span>
                </div>
              </div>

              <div class="control-row">
                <span class="control-label">联网时效检索 (最新/热点)</span>
                <div class="switch-pill ${isSearchOn ? 'active-on' : 'active-off'}"
                  onclick="setGroupConfig('${g.group_id}', 'enable_search', ${!isSearchOn})">
                  <span class="opt-on">开启</span>
                  <span class="opt-off">离线</span>
                </div>
              </div>
            </div>

            <!-- 统计指标 -->
            <div style="margin-top:2px;">
              <div class="group-stat-row">
                <span class="group-stat-label">群内最新消息</span>
                <span class="group-stat-val">${formatRelative(g.last_msg_ago)}</span>
              </div>
              <div class="group-stat-row">
                <span class="group-stat-label">大肥鱼上次发言</span>
                <span class="group-stat-val">${formatRelative(g.last_bot_reply_ago)}</span>
              </div>
              <div class="group-stat-row">
                <span class="group-stat-label">连续未回积攒</span>
                <span class="group-stat-val">${g.msg_count_since} 句群聊</span>
              </div>
            </div>
          </div>
        `;
      }

      // 附加私聊专属卡片
      const privCfg = (data.configs && data.configs.private) || { mode: "user" };
      const isPrivRoot = privCfg.mode === "root";
      html += `
        <div class="group-card" style="border-color: #cbd5e1;">
          <div class="group-card-header">
            <span class="group-id" style="color: #475569;">
              🔒 主人私聊直通
              <button class="btn-mode-toggle ${isPrivRoot ? 'btn-mode-root' : 'btn-mode-user'}"
                onclick="setGroupConfig('private', 'mode', '${isPrivRoot ? 'user' : 'root'}')">
                ${isPrivRoot ? '🛠️ root模式 (点切闲聊)' : '💬 user闲聊 (点切root)'}
              </button>
            </span>
            <span class="badge badge-gold">主人专属通道</span>
          </div>
          <div class="controls-box">
            <div class="control-row">
              <span class="control-label">私聊默认工作模式</span>
              <span style="font-size:12px; font-weight:600; color:var(--text-main);">
                ${isPrivRoot ? '技术搭档 (代码/排障/无字数限制)' : '日常伴聊 (口头禅/傲娇/短句)'}
              </span>
            </div>
          </div>
          <div style="font-size:12px; color:var(--text-muted); margin-top:8px;">
            私聊支持使用 <code>/root</code> 或 <code>/user</code> 自由切换，拥有免睡眠直接唤醒特权。
          </div>
        </div>
      `;

      container.innerHTML = html;
    }

    function renderTabs(data) {
      const tabsContainer = document.getElementById('groupTabs');
      const groups = Object.keys(data.memories || {});
      if (groups.length === 0) return;

      if (!groups.includes(currentTab)) {
        currentTab = groups[0];
      }

      // 建立 group_id -> group_name 字典
      const nameMap = {};
      if (data.groups) {
        for (const item of data.groups) {
          nameMap[String(item.group_id)] = item.group_name;
        }
      }

      let html = "";
      for (const g of groups) {
        let label = g === "private" ? "私聊专属记忆" : `群 ${g}`;
        if (g !== "private" && nameMap[String(g)] && !nameMap[String(g)].startsWith("群聊 ")) {
          label = `${nameMap[String(g)]} (${g})`;
        }
        const active = g === currentTab ? "active" : "";
        html += `<button class="tab-btn ${active}" onclick="switchTab('${g}')">${label} (${Object.keys(data.memories[g]).length})</button>`;
      }
      tabsContainer.innerHTML = html;
    }

    function switchTab(gid) {
      currentTab = gid;
      if (latestData) {
        renderTabs(latestData);
        renderMemories(latestData);
      }
    }

    function renderMemories(data) {
      const container = document.getElementById('memoriesGrid');
      const map = data.memories ? data.memories[currentTab] : null;
      if (!map || Object.keys(map).length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">该群暂无建立群友记忆档案</div>';
        return;
      }

      // 转换为数组并按好感度降序
      const list = Object.entries(map).map(([uid, info]) => ({ uid, ...info }));
      list.sort((a, b) => (b.favor || 0) - (a.favor || 0));

      let html = "";
      for (const u of list) {
        const isMaster = String(u.uid) === String(data.bot.admin_qq);
        const favor = Math.max(0, Math.min(100, u.favor || 0));
        
        let barColor = "linear-gradient(90deg, #3b82f6, #2563eb)";
        if (isMaster) {
          barColor = "linear-gradient(90deg, #f59e0b, #eab308)";
        } else if (favor >= 75) {
          barColor = "linear-gradient(90deg, #10b981, #059669)";
        } else if (favor < 40) {
          barColor = "linear-gradient(90deg, #94a3b8, #64748b)";
        }

        const tags = (u.notes || []).map(t => `<span class="user-tag">${t}</span>`).join("");

        html += `
          <div class="user-card ${isMaster ? 'is-master' : ''}">
            <div class="user-card-top">
              <img class="user-avatar" src="https://q1.qlogo.cn/g?b=qq&nk=${u.uid}&s=100" onerror="this.src='https://dummyimage.com/100x100/cbd5e1/475569?text=Q'">
              <div class="user-meta">
                <div class="user-name">
                  ${isMaster ? '👑 ' : ''}${u.nickname || '神秘群友'}
                  ${isMaster ? '<span class="badge badge-gold" style="font-size:10px; padding:2px 6px;">专属主人</span>' : ''}
                </div>
                <div class="user-qq">QQ: ${u.uid}</div>
              </div>
            </div>
            
            <div class="favor-box">
              <div class="favor-header">
                <span style="color:var(--text-muted);">好感度</span>
                <span style="font-weight:700; color:${isMaster ? '#d97706' : '#2563eb'};">${favor} / 100</span>
              </div>
              <div class="favor-bar-bg">
                <div class="favor-bar-fill" style="width: ${favor}%; background: ${barColor};"></div>
              </div>
            </div>

            ${u.traits ? `<div style="font-size:12px; color:var(--text-muted);">特质: <span style="color:var(--text-main); font-weight:500;">${u.traits}</span></div>` : ''}

            <div class="user-tags">
              ${tags || '<span class="user-tag" style="color:#94a3b8;">暂无标签</span>'}
            </div>
          </div>
        `;
      }
      container.innerHTML = html;
    }

    function renderLogs(logs) {
      const box = document.getElementById('logBox');
      const chk = document.getElementById('chkAutoScroll');
      if (!logs || logs.length === 0) {
        box.innerText = "暂无日志";
        return;
      }

      const formatted = logs.map(line => {
        let esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        if (esc.includes("[ERROR]")) return `<span class="log-err">${esc}</span>`;
        if (esc.includes("[WARNING]")) return `<span class="log-warn">${esc}</span>`;
        if (esc.includes("[INFO]")) return `<span class="log-info">${esc}</span>`;
        return esc;
      }).join("\\n");

      box.innerHTML = formatted;
      if (chk.checked) {
        box.scrollTop = box.scrollHeight;
      }
    }

    fetchState();
    setInterval(fetchState, 3000);
  </script>
</body>
</html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>大肥鱼 AI 控制台 · 安全登录</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .login-card {
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 16px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.02);
      padding: 36px 32px;
      width: 100%;
      max-width: 380px;
      text-align: center;
    }
    .logo {
      font-size: 38px;
      margin-bottom: 12px;
    }
    h1 {
      font-size: 20px;
      font-weight: 700;
      color: #0f172a;
      margin-bottom: 6px;
    }
    p {
      font-size: 13px;
      color: #64748b;
      margin-bottom: 24px;
    }
    .input-group {
      margin-bottom: 18px;
      text-align: left;
    }
    label {
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: #475569;
      margin-bottom: 6px;
    }
    input {
      width: 100%;
      padding: 12px 14px;
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      font-size: 14px;
      outline: none;
      transition: all 0.2s;
    }
    input:focus {
      border-color: #0284c7;
      box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.15);
    }
    button {
      width: 100%;
      padding: 12px;
      background: #0284c7;
      color: #ffffff;
      border: none;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s;
    }
    button:hover {
      background: #0369a1;
    }
    .err-msg {
      margin-top: 14px;
      font-size: 13px;
      color: #ef4444;
      display: none;
    }
  </style>
</head>
<body>
  <div class="login-card">
    <div class="logo">🐟</div>
    <h1>大肥鱼 AI 控制台</h1>
    <p>请输入安全访问密钥以进入后台</p>
    <div class="input-group">
      <label for="pwd">访问密码</label>
      <input type="password" id="pwd" placeholder="输入密钥" autofocus>
    </div>
    <button id="btn" onclick="doLogin()">进入控制台</button>
    <div id="errMsg" class="err-msg">密码错误，请重新输入</div>
  </div>
  <script>
    document.getElementById('pwd').addEventListener('keydown', function(e) {
      if (e.key === 'Enter') doLogin();
    });
    async function doLogin() {
      const p = document.getElementById('pwd').value.trim();
      if (!p) return;
      const btn = document.getElementById('btn');
      btn.innerText = "正在验证...";
      btn.disabled = true;
      try {
        const res = await fetch('/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: p })
        });
        if (res.ok) {
          window.location.reload();
        } else {
          document.getElementById('errMsg').style.display = 'block';
          btn.innerText = "进入控制台";
          btn.disabled = false;
        }
      } catch (e) {
        document.getElementById('errMsg').innerText = "网络异常，请重试";
        document.getElementById('errMsg').style.display = 'block';
        btn.innerText = "进入控制台";
        btn.disabled = false;
      }
    }
  </script>
</body>
</html>
"""

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard_auth")

def get_auth_credentials() -> tuple[str, str]:
    """获取仪表盘认证凭据，优先从环境变量读取，其次读 .dashboard_auth 本地文件"""
    env_auth = os.environ.get("DASHBOARD_AUTH")
    if env_auth and ":" in env_auth:
        u, p = env_auth.split(":", 1)
        return u.strip(), p.strip()
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                line = f.readline().strip()
                if ":" in line:
                    u, p = line.split(":", 1)
                    return u.strip(), p.strip()
        except Exception:
            pass
    return "admin", "admin"

def check_authenticated(request: web.Request) -> bool:
    """多通道鉴权：支持 URL Key / Cookie Session / HTTP Basic Header"""
    valid_user, valid_pass = get_auth_credentials()
    
    # 1. 检查 URL 参数 ?key=... 或 ?token=...
    q_key = request.query.get("key") or request.query.get("token")
    if q_key and hmac.compare_digest(q_key, valid_pass):
        return True

    # 2. 检查 Cookie (30天持久会话)
    cookie_token = request.cookies.get("dashboard_session")
    if cookie_token and hmac.compare_digest(cookie_token, valid_pass):
        return True

    # 3. 检查 HTTP Basic Header (兼容脚本/命令行)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            raw_creds = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in raw_creds:
                u, p = raw_creds.split(":", 1)
                if hmac.compare_digest(u, valid_user) and hmac.compare_digest(p, valid_pass):
                    return True
        except Exception:
            pass

    return False

@web.middleware
async def session_auth_middleware(request: web.Request, handler):
    """现代化无感会话拦截器，彻底解决原生弹窗死循环问题"""
    # 登录接口必须放行
    if request.path == "/api/login":
        return await handler(request)

    if check_authenticated(request):
        response = await handler(request)
        # 如果是带 ?key= 访问，顺便帮用户种上 Cookie，下次无需输入
        q_key = request.query.get("key") or request.query.get("token")
        if q_key and isinstance(response, web.Response):
            response.set_cookie("dashboard_session", q_key, max_age=2592000, path="/", samesite="Lax")
        return response

    # 未认证情况：
    # 访问页面，直接渲染整洁漂亮的卡片登录表单
    if request.path == "/" and request.method == "GET":
        return web.Response(text=LOGIN_HTML, content_type="text/html", charset="utf-8")

    # 访问后台 API 轮询，返回 JSON 401（绝不发 WWW-Authenticate 头，避免浏览器弹原生框）
    return web.json_response({"error": "unauthorized", "message": "请先登录控制台"}, status=401)

async def create_web_app(get_status_func: Callable[[], Dict[str, Any]], update_config_func: Optional[Callable[[Optional[int], str, Any], bool]] = None) -> web.Application:
    """构建 aiohttp web 实例，支持查询与独立群配置写入"""
    app = web.Application(middlewares=[session_auth_middleware])

    async def handle_login(request: web.Request):
        try:
            body = await request.json()
            pwd = body.get("password", "").strip()
            _, valid_pass = get_auth_credentials()
            if pwd and hmac.compare_digest(pwd, valid_pass):
                resp = web.json_response({"status": "ok"})
                resp.set_cookie("dashboard_session", valid_pass, max_age=2592000, path="/", samesite="Lax")
                return resp
            return web.json_response({"error": "密码错误"}, status=403)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_index(request: web.Request):
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")

    async def handle_status(request: web.Request):
        try:
            data = get_status_func()
            data["logs"] = list(recent_web_logs)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_update_config(request: web.Request):
        if not update_config_func:
            return web.json_response({"error": "未提供配置更新回调"}, status=500)
        try:
            body = await request.json()
            raw_gid = body.get("group_id")
            key = body.get("key")
            val = body.get("value")

            if not key:
                return web.json_response({"error": "缺少 key 参数"}, status=400)

            gid = None
            if raw_gid and str(raw_gid).lower() != "private":
                try:
                    gid = int(raw_gid)
                except ValueError:
                    gid = None

            ok = update_config_func(gid, key, val)
            return web.json_response({"status": "ok" if ok else "fail"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_get("/", handle_index)
    app.router.add_post("/api/login", handle_login)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/group/config", handle_update_config)
    return app

async def start_web_server(get_status_func: Callable[[], Dict[str, Any]], update_config_func: Optional[Callable[[Optional[int], str, Any], bool]] = None, port: int = 8088):
    """启动 Web 仪表盘"""
    app = await create_web_app(get_status_func, update_config_func)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
