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
import collections
import datetime
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
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
      <img class="bot-avatar" src="https://q1.qlogo.cn/g?b=qq&nk=__BOT_QQ__&s=100" alt="Bot Avatar" onerror="this.src='https://dummyimage.com/100x100/2563eb/ffffff?text=FISH'">
      <div class="title-box">
        <h1>大肥鱼 AI 控制台</h1>
        <p>基座: DeepSeek-V3 · 实体 QQ: __BOT_QQ__ · 双 Bot 协同中</p>
      </div>
    </div>
    <div class="status-badges" id="headerBadges">
      <span class="badge badge-success" id="badgeConn">● 正在连接...</span>
      <span class="badge badge-primary" id="badgeMode">模式: 加载中</span>
      <span class="badge badge-info" id="badgeSleep">作息: 检测中</span>
    </div>
  </header>

  <!-- 顶部状态卡片 -->
  <div class="grid-top">
    <div class="card">
      <div class="card-title">白名单群聊数量</div>
      <div class="card-value" id="valGroupCount">4 个群</div>
      <div class="card-sub" id="valGroupSub">加载中…</div>
    </div>
    <div class="card">
      <div class="card-title">已建立群友档案</div>
      <div class="card-value" id="valUserCount">-- 人</div>
      <div class="card-sub">按群严格隔离 · 动态好感度评估</div>
    </div>
    <div class="card">
      <div class="card-title">免 @ 追问配额机制</div>
      <div class="card-value">5分钟 / 5次</div>
      <div class="card-sub">新 @ 立即重置配额 · 连续追问窗口</div>
    </div>
    <div class="card">
      <div class="card-title">服务运行时间</div>
      <div class="card-value" id="valUptime">--</div>
      <div class="card-sub" id="valStartTime">启动时间: --</div>
    </div>
  </div>

  <!-- 群聊冷热场雷达 -->
  <div class="section-title">群聊活跃度与冷热场插话</div>
  <div class="group-grid" id="groupGrid">
    <!-- 动态填入群卡片 -->
  </div>

  <!-- 群友档案与好感度排行榜 -->
  <div class="section-title">群友记忆与好感度</div>
  <div class="tabs" id="groupTabs">
    <!-- 动态填入选项卡 -->
  </div>
  <div class="memories-grid" id="memoriesGrid">
    <!-- 动态填入群友卡片 -->
  </div>

  <!-- 实时运行日志 -->
  <div class="section-title">实时交互日志</div>
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
        badgeMode.innerText = "技术伙伴模式 (/root)";
      } else {
        badgeMode.className = "badge badge-primary";
        badgeMode.innerText = "闲聊模式 (/user)";
      }

      const badgeSleep = document.getElementById('badgeSleep');
      if (data.bot.is_sleeping) {
        if (data.bot.sleep_override) {
          badgeSleep.className = "badge badge-warning";
          badgeSleep.innerText = "主人唤醒中 (30分钟)";
        } else {
          badgeSleep.className = "badge badge-slate";
          badgeSleep.innerText = "休眠中 (01:00~07:00)";
        }
      } else {
        badgeSleep.className = "badge badge-success";
        badgeSleep.innerText = "清醒中";
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
        const isMuted = !!cfg.muted;
        const probPct = Math.round((cfg.interject_prob ?? 0.10) * 100);

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
                  ${isRoot ? 'root 模式 (点切闲聊)' : '闲聊模式 (点切 root)'}
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
                <span class="control-label">群功能总闸 (/off 关)</span>
                <div class="switch-pill ${isMuted ? 'active-off' : 'active-on'}"
                  onclick="setGroupConfig('${g.group_id}', 'muted', ${!isMuted})">
                  <span class="opt-on">在线</span>
                  <span class="opt-off">已停</span>
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
              主人私聊直通
              <button class="btn-mode-toggle ${isPrivRoot ? 'btn-mode-root' : 'btn-mode-user'}"
                onclick="setGroupConfig('private', 'mode', '${isPrivRoot ? 'user' : 'root'}')">
                ${isPrivRoot ? 'root 模式 (点切闲聊)' : '闲聊模式 (点切 root)'}
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
                  ${isMaster ? '' : ''}${u.nickname || '神秘群友'}
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
    <div class="logo">鱼</div>
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

# 会话与防爆破安全存储
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard_auth")
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dashboard_sessions.json")
SESSION_LIFETIME = 30 * 86400          # 30 天持久化免密免验证

def load_sessions() -> Dict[str, float]:
    """从磁盘加载持久化会话，杜绝服务重启/重载丢会话"""
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                return {k: v for k, v in data.items() if v > now}
        except Exception:
            pass
    return {}

def save_sessions():
    """将会话持久化保存至磁盘"""
    try:
        now = time.time()
        valid = {k: v for k, v in ACTIVE_SESSIONS.items() if v > now}
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(valid, f)
    except Exception:
        pass

ACTIVE_SESSIONS: Dict[str, float] = load_sessions()
LOGIN_FAILURES: Dict[str, list[float]] = collections.defaultdict(list)  # ip -> fail timestamps
LOCKED_IPS: Dict[str, float] = {}      # ip -> unlock_timestamp

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """使用 PBKDF2-HMAC-SHA256 进行密码加盐散列，防彩虹表破解"""
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return salt, key.hex()

def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """常量时间哈希校验，抵御时序侧信道攻击"""
    _, calc_hash = hash_password(password, salt)
    return hmac.compare_digest(calc_hash, expected_hash)

def get_auth_credentials() -> tuple[str, str, str]:
    """
    读取并维护认证凭据（user, salt, hash）。
    若检测到旧版明文格式，自动就地升级为加盐散列持久化。
    """
    env_auth = os.environ.get("DASHBOARD_AUTH")
    if env_auth and ":" in env_auth:
        parts = env_auth.split(":")
        if len(parts) == 3:
            return parts[0].strip(), parts[1].strip(), parts[2].strip()
        elif len(parts) == 2:
            salt, h = hash_password(parts[1].strip())
            return parts[0].strip(), salt, h

    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                line = f.readline().strip()
                parts = line.split(":")
                if len(parts) == 3:
                    return parts[0].strip(), parts[1].strip(), parts[2].strip()
                elif len(parts) == 2:
                    # 旧明文密码会自动升级为 PBKDF2 加盐哈希
                    user, plain_pwd = parts[0].strip(), parts[1].strip()
                    salt, h = hash_password(plain_pwd)
                    with open(AUTH_FILE, "w", encoding="utf-8") as wf:
                        wf.write(f"{user}:{salt}:{h}\n")
                    return user, salt, h
        except Exception:
            pass

    # 默认兜底
    salt, h = hash_password("admin")
    return "admin", salt, h

def get_client_ip(request: web.Request) -> str:
    """提取真实客户端 IP，支持 Cloudflare Tunnel 与反向代理链"""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.remote or "unknown"

def check_rate_limit(ip: str) -> Optional[int]:
    """检查 IP 是否正处于密码爆破封禁期，若封禁返回剩余锁定秒数"""
    now = time.time()
    if ip in LOCKED_IPS:
        remaining = int(LOCKED_IPS[ip] - now)
        if remaining > 0:
            return remaining
        else:
            del LOCKED_IPS[ip]
            LOGIN_FAILURES[ip] = []
    return None

def record_login_attempt(ip: str, success: bool):
    """记录登录结果，60 秒内连续失败 5 次则锁定该 IP 15 分钟"""
    now = time.time()
    if success:
        LOGIN_FAILURES.pop(ip, None)
        LOCKED_IPS.pop(ip, None)
        return

    recent = [t for t in LOGIN_FAILURES[ip] if now - t < 60]
    recent.append(now)
    LOGIN_FAILURES[ip] = recent
    if len(recent) >= 5:
        LOCKED_IPS[ip] = now + 900  # 封禁 15 分钟

def create_session() -> str:
    """生成高熵随机会话令牌并登记内存与磁盘"""
    token = secrets.token_urlsafe(32)
    ACTIVE_SESSIONS[token] = time.time() + SESSION_LIFETIME
    save_sessions()
    return token

def validate_session(token: Optional[str]) -> bool:
    """校验随机会话有效性并自动滑动续期"""
    if not token:
        return False
    if token not in ACTIVE_SESSIONS:
        # 兼容重启或外部初始化的 sessions 文件
        disk_sessions = load_sessions()
        ACTIVE_SESSIONS.update(disk_sessions)
    if token not in ACTIVE_SESSIONS:
        return False
    now = time.time()
    if now > ACTIVE_SESSIONS[token]:
        del ACTIVE_SESSIONS[token]
        save_sessions()
        return False
    # 滑动续期
    ACTIVE_SESSIONS[token] = now + SESSION_LIFETIME
    return True

def revoke_session(token: Optional[str]):
    """立即销毁并废弃会话"""
    if token and token in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[token]
        save_sessions()

def set_safe_cookie(resp: web.Response, session_token: str):
    """下发具备 HttpOnly 保护的随机会话 Cookie，杜绝 XSS 读取与密码暴露"""
    resp.set_cookie(
        "dashboard_session",
        session_token,
        max_age=SESSION_LIFETIME,
        path="/",
        httponly=True,  # 强制禁止客户端 JavaScript 访问，防凭据窃取
        samesite="Lax"
    )

def check_authenticated(request: web.Request) -> bool:
    """多通道严格鉴权：支持 Cloudflare Zero Trust 云端直通 / 服务端随机 Session / HTTP Basic"""
    # 0. 检查 Cloudflare Zero Trust (Access) 认证放行标识
    cf_email = request.headers.get("Cf-Access-Authenticated-User-Email")
    if cf_email and cf_email.strip():
        return True

    # 1. 检查客户端 HttpOnly Cookie 随机会话
    cookie_token = request.cookies.get("dashboard_session")
    if validate_session(cookie_token):
        return True

    # 2. 检查 HTTP Basic Header (供自动化脚本与 API 调试)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            raw_creds = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in raw_creds:
                u, p = raw_creds.split(":", 1)
                valid_user, salt, expected_hash = get_auth_credentials()
                if hmac.compare_digest(u, valid_user) and verify_password(p, salt, expected_hash):
                    return True
        except Exception:
            pass

    return False

DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")

@web.middleware
async def session_auth_middleware(request: web.Request, handler):
    """现代化安全无感会话拦截器"""
    # 静态前端资源与登录/注销接口直接放行
    if request.path.startswith("/assets/") or request.path in ("/api/login", "/api/logout"):
        return await handler(request)

    # 访问根页面：若是 URL 带 ?key= 显式直登，校验后自动 302 净化地址栏并种 Cookie
    if request.path == "/" and request.method in ("GET", "HEAD"):
        q_key = request.query.get("key") or request.query.get("token")
        if q_key:
            client_ip = get_client_ip(request)
            remaining_ban = check_rate_limit(client_ip)
            if not remaining_ban:
                _, salt, expected_hash = get_auth_credentials()
                if verify_password(q_key, salt, expected_hash):
                    record_login_attempt(client_ip, True)
                    token = create_session()
                    # 302 重定向到洁净根路径，抹除 URL 上的明文密钥与历史记录
                    redirect_resp = web.HTTPFound("/")
                    set_safe_cookie(redirect_resp, token)
                    return redirect_resp
                else:
                    record_login_attempt(client_ip, False)
        return await handler(request)

    # 后台管理 API 严格校验会话有效性
    if check_authenticated(request):
        response = await handler(request)
        # 若来自 Cloudflare Access 云端认证通过但本地无 Session Cookie，自动无感补发
        cf_email = request.headers.get("Cf-Access-Authenticated-User-Email")
        if cf_email and isinstance(response, web.Response) and not request.cookies.get("dashboard_session"):
            token = create_session()
            set_safe_cookie(response, token)
        return response

    # 未认证的 API 轮询直接返回 401 JSON，严禁弹出浏览器底层密码弹窗
    return web.json_response({"error": "unauthorized", "message": "控制台已锁定，请先完成身份验证"}, status=401)

async def create_web_app(get_status_func: Callable[[], Dict[str, Any]], update_config_func: Optional[Callable[[Optional[int], str, Any], bool]] = None) -> web.Application:
    """构建 aiohttp web 实例，支持加固鉴权与配置热写入"""
    app = web.Application(middlewares=[session_auth_middleware])

    # 静态资源在请求时按需解析：前端重新构建后不必重启服务也能立刻生效
    async def handle_asset(request: web.Request):
        rel = request.match_info.get("path", "")
        assets_root = os.path.realpath(os.path.join(DIST_DIR, "assets"))
        target = os.path.realpath(os.path.join(assets_root, rel))
        # 路径穿越防护：解析后必须仍在 assets 目录内
        if not target.startswith(assets_root + os.sep) or not os.path.isfile(target):
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def handle_login(request: web.Request):
        client_ip = get_client_ip(request)

        # 检查是否处于爆破封禁期
        ban_seconds = check_rate_limit(client_ip)
        if ban_seconds:
            return web.json_response(
                {"error": f"尝试过于频繁，IP 已被锁定，请等待 {ban_seconds} 秒后再试"},
                status=429
            )

        try:
            body = await request.json()
            pwd = body.get("password", "").strip()
            _, salt, expected_hash = get_auth_credentials()

            if pwd and verify_password(pwd, salt, expected_hash):
                record_login_attempt(client_ip, True)
                token = create_session()
                resp = web.json_response({"status": "ok"})
                set_safe_cookie(resp, token)
                return resp

            record_login_attempt(client_ip, False)
            return web.json_response({"error": "密钥错误，请重新输入"}, status=403)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_logout(request: web.Request):
        cookie_token = request.cookies.get("dashboard_session")
        revoke_session(cookie_token)
        resp = web.json_response({"status": "ok"})
        resp.del_cookie("dashboard_session", path="/")
        return resp

    async def handle_index(request: web.Request):
        dist_index = os.path.join(DIST_DIR, "index.html")
        if os.path.exists(dist_index):
            with open(dist_index, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html", charset="utf-8")
        # 页面里的机器人 QQ 从环境变量取，别把账号写进公开仓库
        page = INDEX_HTML.replace("__BOT_QQ__", os.environ.get("BOT_QQ", "1000000000"))
        return web.Response(text=page, content_type="text/html", charset="utf-8")

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
    app.router.add_get("/assets/{path:.*}", handle_asset)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/group/config", handle_update_config)
    return app

async def start_web_server(get_status_func: Callable[[], Dict[str, Any]], update_config_func: Optional[Callable[[Optional[int], str, Any], bool]] = None, port: int = 8088):
    """启动 Web 仪表盘"""
    app = await create_web_app(get_status_func, update_config_func)
    runner = web.AppRunner(app)
    await runner.setup()
    # 只监听本机，外网走 Cloudflare 隧道进来；没必要把面板暴露在整个局域网
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner
