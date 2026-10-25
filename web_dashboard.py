"""
大肥鱼 AI 控制台 · 轻量现代浅色 Web 仪表盘
原生基于 aiohttp 实现，零外部依赖，自适应手机与 PC 端。
"""
import collections
import datetime
import html
import logging
import time
from typing import Callable, Deque, Dict, Any
from aiohttp import web

recent_web_logs: Deque[str] = collections.deque(maxlen=60)

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
      padding: 14px;
      border-radius: 8px;
      height: 280px;
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
      <img class="bot-avatar" src="https://q1.qlogo.cn/g?b=qq&nk=1000000000&s=100" alt="Bot Avatar" onerror="this.src='https://dummyimage.com/100x100/2563eb/ffffff?text=FISH'">
      <div class="title-box">
        <h1>🐟 大肥鱼 AI 控制台</h1>
        <p>基座: DeepSeek-V3 · 实体 QQ: 1000000000 · 双 Bot 协同中</p>
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
      <div class="card-sub">123456789 / 123456788 / 123456787 / 123456786</div>
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
    let currentTab = "123456789";
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
    }

    function renderGroups(data) {
      const container = document.getElementById('groupGrid');
      if (!data.groups) return;

      let html = "";
      for (const g of data.groups) {
        let badgeClass = "badge-slate";
        if (g.interject_why.includes("热场")) badgeClass = "badge-success";
        else if (g.interject_why.includes("冷场")) badgeClass = "badge-info";

        html += `
          <div class="group-card">
            <div class="group-card-header">
              <span class="group-id">群号: ${g.group_id}</span>
              <span class="badge ${badgeClass}">${g.interject_why} (权重 ${(g.interject_prob * 100).toFixed(0)}%)</span>
            </div>
            <div class="group-stat-row">
              <span class="group-stat-label">群内最新消息</span>
              <span class="group-stat-val">${formatRelative(g.last_msg_ago)}</span>
            </div>
            <div class="group-stat-row">
              <span class="group-stat-label">大肥鱼上次发言</span>
              <span class="group-stat-val">${formatRelative(g.last_bot_reply_ago)}</span>
            </div>
            <div class="group-stat-row">
              <span class="group-stat-label">静默连续积攒</span>
              <span class="group-stat-val">${g.msg_count_since} 句群聊</span>
            </div>
          </div>
        `;
      }
      container.innerHTML = html;
    }

    function renderTabs(data) {
      const tabsContainer = document.getElementById('groupTabs');
      const groups = Object.keys(data.memories || {});
      if (groups.length === 0) return;

      if (!groups.includes(currentTab)) {
        currentTab = groups[0];
      }

      let html = "";
      for (const g of groups) {
        const label = g === "private" ? "私聊专属记忆" : `群 ${g}`;
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

async def create_web_app(get_status_func: Callable[[], Dict[str, Any]]) -> web.Application:
    """构建 aiohttp web 实例"""
    app = web.Application()

    async def handle_index(request: web.Request):
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")

    async def handle_status(request: web.Request):
        try:
            data = get_status_func()
            data["logs"] = list(recent_web_logs)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    return app

async def start_web_server(get_status_func: Callable[[], Dict[str, Any]], port: int = 8088):
    """启动 Web 仪表盘"""
    app = await create_web_app(get_status_func)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
