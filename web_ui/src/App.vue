<script setup lang="ts">
import { ref, onMounted, computed, watch, nextTick } from 'vue'
import {
  MiuixButton,
  MiuixSwitchPreference,
  MiuixSlider,
  MiuixSnackbarHost,
  showSnackbar
} from 'miuix-vue'

// 鉴权状态
const isAuthenticated = ref(false)
const passwordInput = ref('')
const authChecking = ref(true)
const loginError = ref('')

// 业务数据
interface BotStatus {
  bot: {
    qq: number
    admin_qq: number
    model: string
    is_sleeping: boolean
    uptime: number
    start_time_str: string
  }
  groups: Array<{
    group_id: number
    group_name: string
    config: Record<string, any>
    last_msg_ago: number
    last_bot_reply_ago: number
    msg_count_since: number
    interject_prob: number
    interject_why: string
  }>
  configs: Record<string, Record<string, any>>
  memories: Record<string, Record<string, {
    nickname: string
    favor: number
    traits: string
    notes: string[]
    updated_at: number
  }>>
  logs: string[]
}

const status = ref<BotStatus | null>(null)
const selectedGroupId = ref<number | null>(null)
const showLogs = ref(false)
const showMemories = ref(false)
const autoScroll = ref(true)
const logContainer = ref<HTMLElement | null>(null)

// 当前选中的群
const currentGroup = computed(() => {
  if (!status.value?.groups) return null
  return status.value.groups.find(g => g.group_id === selectedGroupId.value) || status.value.groups[0] || null
})

const currentConfig = computed(() => {
  if (!status.value?.configs || !currentGroup.value) return {}
  return status.value.configs[String(currentGroup.value.group_id)] || currentGroup.value.config || {}
})

// 滑块本地值与滑动状态解耦（拖动过程中不发请求，松手才提交）
const localProb = ref(0.35)
let isSliding = false

watch(currentConfig, (cfg) => {
  if (!isSliding) {
    localProb.value = cfg.interject_prob ?? 0.35
  }
}, { immediate: true })

function onSliderInput(val: number) {
  isSliding = true
  localProb.value = Number(val.toFixed(2))
}

function onSliderCommit() {
  if (isSliding) {
    isSliding = false
    saveConfig('interject_prob', localProb.value)
  }
}

// 选中的群的记忆成员
const currentMemories = computed(() => {
  if (!status.value?.memories || !currentGroup.value) return []
  const groupMems = status.value.memories[String(currentGroup.value.group_id)] || {}
  return Object.entries(groupMems).map(([qq, m]) => ({
    qq,
    nickname: m.nickname || '群友',
    favor: m.favor ?? 0,
    traits: m.traits || '',
    notes: m.notes || []
  })).sort((a, b) => b.favor - a.favor)
})

// 时间格式化
function formatUptime(secs: number): string {
  if (!secs) return '刚刚'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return h > 0 ? `${h}小时${m}分` : `${m}分钟`
}

// 检查鉴权
async function checkAuth() {
  authChecking.value = true
  const params = new URLSearchParams(window.location.search)
  const urlKey = params.get('key') || params.get('token')

  if (urlKey) {
    await doLoginWithKey(urlKey)
    return
  }

  try {
    const res = await fetch('/api/status')
    if (res.ok) {
      status.value = await res.json()
      isAuthenticated.value = true
      if (status.value?.groups?.length && !selectedGroupId.value) {
        selectedGroupId.value = status.value.groups[0].group_id
      }
    } else {
      isAuthenticated.value = false
    }
  } catch {
    isAuthenticated.value = false
  } finally {
    authChecking.value = false
  }
}

async function doLoginWithKey(key: string) {
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: key.trim() })
    })
    const data = await res.json()
    if (res.ok && data.status === 'ok') {
      isAuthenticated.value = true
      loginError.value = ''
      await refreshData()
    } else {
      loginError.value = data.error || '密钥错误'
      isAuthenticated.value = false
    }
  } catch (e: any) {
    loginError.value = '连接异常: ' + e.message
    isAuthenticated.value = false
  } finally {
    authChecking.value = false
  }
}

// 手动输入密码登录
async function handleLogin() {
  if (!passwordInput.value.trim()) {
    loginError.value = '请输入管理员密钥'
    return
  }
  await doLoginWithKey(passwordInput.value)
}

// 退出并清除会话
async function handleLogout() {
  try {
    await fetch('/api/logout', { method: 'POST' })
  } catch {}
  document.cookie = 'dashboard_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT;'
  isAuthenticated.value = false
  status.value = null
  passwordInput.value = ''
  showSnackbar({ message: '已锁定控制台', duration: 1500 })
}

// 刷新状态
async function refreshData() {
  try {
    const res = await fetch('/api/status')
    if (res.status === 401) {
      isAuthenticated.value = false
      return
    }
    if (res.ok) {
      status.value = await res.json()
      if (status.value?.groups?.length && !selectedGroupId.value) {
        selectedGroupId.value = status.value.groups[0].group_id
      }
      if (autoScroll.value && showLogs.value) {
        nextTick(scrollToBottom)
      }
    }
  } catch (e: any) {
    showSnackbar({ message: '刷新失败: ' + e.message, duration: 2000 })
  }
}

// 修改配置项
async function saveConfig(key: string, value: any) {
  if (!currentGroup.value) return
  try {
    const gid = currentGroup.value.group_id
    const res = await fetch('/api/group/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: gid, key, value })
    })
    const data = await res.json()
    if (res.ok && data.status === 'ok') {
      showSnackbar({ message: '已保存', duration: 1200 })
      if (status.value) {
        const gidStr = String(gid)
        if (status.value.configs[gidStr]) status.value.configs[gidStr][key] = value
        if (currentGroup.value.config) currentGroup.value.config[key] = value
      }
    } else {
      showSnackbar({ message: '保存失败: ' + (data.error || '未知原因'), duration: 2500 })
    }
  } catch (e: any) {
    showSnackbar({ message: '网络错误: ' + e.message, duration: 2500 })
  }
}

function scrollToBottom() {
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight
  }
}

onMounted(() => {
  checkAuth()
  setInterval(() => {
    if (isAuthenticated.value) refreshData()
  }, 6000)
})
</script>

<template>
  <div class="miuix-root">
    <MiuixSnackbarHost />

    <!-- 1. 加载等待中 -->
    <div v-if="authChecking" class="loading-wrap">
      <div class="spinner"></div>
    </div>

    <!-- 2. 纯净密码锁屏验证界面 -->
    <div v-else-if="!isAuthenticated" class="auth-wrap">
      <div class="auth-card">
        <div class="auth-top">
          <div class="lock-icon">🔒</div>
          <h2 class="auth-title">大肥鱼控制台</h2>
          <p class="auth-desc">输入访问密钥解锁管理权限</p>
        </div>

        <div class="auth-form">
          <input
            v-model="passwordInput"
            type="password"
            placeholder="输入访问密钥"
            class="auth-input"
            autofocus
            @keyup.enter="handleLogin"
          />
          <div v-if="loginError" class="auth-err">{{ loginError }}</div>
          <MiuixButton type="primary" class="auth-btn" @click="handleLogin">
            解锁控制台
          </MiuixButton>
        </div>
      </div>
    </div>

    <!-- 3. 已认证：纯净直观设置列表 -->
    <div v-else class="main-wrap">
      <!-- 顶栏 -->
      <header class="app-bar">
        <div class="bar-left">
          <h1 class="bar-title">大肥鱼控制台</h1>
          <span class="status-dot-badge">
            <span class="dot"></span>
            运行 {{ formatUptime(status?.bot.uptime || 0) }}
          </span>
          <span v-if="status?.bot.is_sleeping" class="sleep-badge">
            🌙 休眠中
          </span>
        </div>

        <div class="bar-right">
          <button class="action-btn" @click="refreshData" title="刷新状态">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.19"/></svg>
          </button>
          <button class="action-btn" @click="handleLogout" title="锁定并退出">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          </button>
        </div>
      </header>

      <!-- 群聊切换胶囊条 -->
      <nav class="group-nav-bar">
        <button
          v-for="g in status?.groups"
          :key="g.group_id"
          class="group-nav-item"
          :class="{ active: selectedGroupId === g.group_id }"
          @click="selectedGroupId = g.group_id"
        >
          <span class="nav-name">{{ g.group_name }}</span>
          <span class="nav-id">{{ g.group_id }}</span>
        </button>
      </nav>

      <!-- 核心控制列表 -->
      <main v-if="currentGroup" class="content-body">
        <!-- 分组 1：响应行为 -->
        <section class="pref-group">
          <div class="pref-group-title">响应行为</div>
          <div class="pref-card">
            <MiuixSwitchPreference
              title="智能插话"
              summary="群友热聊时根据气氛与概率主动参与对话"
              :modelValue="currentConfig.enable_interject ?? true"
              @update:modelValue="(val: boolean) => saveConfig('enable_interject', val)"
            />

            <div class="pref-divider"></div>

            <!-- 滑块专属内嵌区域：左右内边距完整，绝对防溢出 -->
            <div class="slider-item">
              <div class="slider-head">
                <div class="slider-text-wrap">
                  <div class="slider-title">主动插话概率</div>
                  <div class="slider-desc">群活跃冷场时动态调节</div>
                </div>
                <div class="slider-badge">
                  {{ Math.round(localProb * 100) }}%
                </div>
              </div>
              <div class="slider-track-wrap">
                <MiuixSlider
                  :modelValue="localProb"
                  :min="0"
                  :max="1"
                  :step="0.05"
                  @update:modelValue="onSliderInput"
                  @change-end="onSliderCommit"
                  @pointerup="onSliderCommit"
                  @touchend="onSliderCommit"
                />
              </div>
            </div>

            <div class="pref-divider"></div>

            <MiuixSwitchPreference
              title="完整猫娘人设模式"
              summary="启用深度人设、好感度判定与个性化语气回复"
              :modelValue="currentConfig.mode === 'user'"
              @update:modelValue="(val: boolean) => saveConfig('mode', val ? 'user' : 'default')"
            />
          </div>
        </section>

        <!-- 分组 2：功能与交互开关 -->
        <section class="pref-group">
          <div class="pref-group-title">功能与交互</div>
          <div class="pref-card">
            <MiuixSwitchPreference
              title="遵循夜间作息"
              summary="凌晨时段进入睡眠状态，避免深夜惊扰群友"
              :modelValue="currentConfig.enable_sleep ?? true"
              @update:modelValue="(val: boolean) => saveConfig('enable_sleep', val)"
            />
            <div class="pref-divider"></div>

            <MiuixSwitchPreference
              title="戳一戳互动"
              summary="被群友戳一戳时做出表情立绘与动态文字反馈"
              :modelValue="currentConfig.enable_poke ?? true"
              @update:modelValue="(val: boolean) => saveConfig('enable_poke', val)"
            />
            <div class="pref-divider"></div>

            <MiuixSwitchPreference
              title="互动小游戏"
              summary="支持钓鱼、猜数字、角色抽卡与好感度试炼指令"
              :modelValue="currentConfig.enable_games ?? true"
              @update:modelValue="(val: boolean) => saveConfig('enable_games', val)"
            />
            <div class="pref-divider"></div>

            <MiuixSwitchPreference
              title="云崽/喵喵插件指令让路"
              summary="自动识别游戏查询指令，防止 AI 抢答与冲突"
              :modelValue="currentConfig.enable_yunzai ?? true"
              @update:modelValue="(val: boolean) => saveConfig('enable_yunzai', val)"
            />
          </div>
        </section>

        <!-- 分组 3：本群群友记忆 (点击展开收起) -->
        <section class="pref-group">
          <div class="pref-group-title header-clickable" @click="showMemories = !showMemories">
            <span>本群群友档案 ({{ currentMemories.length }})</span>
            <span class="fold-arrow">{{ showMemories ? '收起 ▲' : '展开 ▼' }}</span>
          </div>

          <div v-if="showMemories" class="pref-card memories-box">
            <div v-if="currentMemories.length === 0" class="empty-hint">
              本群暂未建立群友记忆档案
            </div>
            <div v-else class="memory-list">
              <div v-for="user in currentMemories" :key="user.qq" class="memory-item">
                <div class="mem-row-top">
                  <div class="mem-user-info">
                    <span class="mem-name">{{ user.nickname }}</span>
                    <span class="mem-qq">({{ user.qq }})</span>
                    <span v-if="user.traits" class="mem-tag">{{ user.traits }}</span>
                  </div>
                  <div class="mem-favor-tag" :class="user.favor >= 80 ? 'high' : 'normal'">
                    好感 {{ user.favor }}
                  </div>
                </div>

                <div class="favor-track">
                  <div
                    class="favor-bar"
                    :class="user.favor >= 80 ? 'high' : 'normal'"
                    :style="{ width: `${Math.min(100, Math.max(0, user.favor))}%` }"
                  ></div>
                </div>

                <div v-if="user.notes && user.notes.length > 0" class="mem-notes">
                  <span v-for="(n, i) in user.notes.slice(0, 4)" :key="i" class="note-pill">
                    {{ n }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- 分组 4：实时日志 (点击展开收起) -->
        <section class="pref-group">
          <div class="pref-group-title header-clickable" @click="showLogs = !showLogs">
            <span>实时运行日志</span>
            <span class="fold-arrow">{{ showLogs ? '收起 ▲' : '展开 ▼' }}</span>
          </div>

          <div v-if="showLogs" class="pref-card log-card">
            <div class="log-bar">
              <label class="auto-scroll-check">
                <input type="checkbox" v-model="autoScroll" />
                <span>自动置底</span>
              </label>
              <button class="mini-btn" @click="refreshData">刷新</button>
            </div>
            <div class="log-view" ref="logContainer">
              <div v-for="(line, idx) in status?.logs || []" :key="idx" class="log-line">
                {{ line }}
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
/* 根视窗 */
.miuix-root {
  min-height: 100vh;
  background-color: #f6f7f9;
  color: #1a1a1a;
  display: flex;
  justify-content: center;
}

.loading-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 80vh;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid rgba(0, 125, 255, 0.15);
  border-top-color: #007dff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 锁屏密码验证界面 */
.auth-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  padding: 20px;
}

.auth-card {
  width: 100%;
  max-width: 380px;
  background: #ffffff;
  border-radius: 24px;
  padding: 36px 26px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04);
  text-align: center;
}

.lock-icon {
  font-size: 34px;
  margin-bottom: 12px;
}

.auth-title {
  font-size: 20px;
  font-weight: 700;
  margin-bottom: 6px;
  color: #1a1a1a;
}

.auth-desc {
  font-size: 13px;
  color: #8c8c8c;
  margin-bottom: 24px;
}

.auth-input {
  width: 100%;
  padding: 13px 16px;
  border-radius: 14px;
  border: 1px solid #e5e5e5;
  background: #f8f9fa;
  font-size: 15px;
  margin-bottom: 14px;
  outline: none;
  transition: all 0.2s;
  box-sizing: border-box;
}

.auth-input:focus {
  border-color: #007dff;
  background: #ffffff;
}

.auth-err {
  color: #ff4d4f;
  font-size: 12px;
  margin-bottom: 12px;
}

.auth-btn {
  width: 100%;
  height: 44px;
  font-weight: 600;
  border-radius: 12px;
}

/* 主面板容器 */
.main-wrap {
  width: 100%;
  max-width: 740px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 顶栏 */
.app-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 4px;
}

.bar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.bar-title {
  font-size: 18px;
  font-weight: 700;
}

.status-dot-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #595959;
  background: #ffffff;
  padding: 3px 10px;
  border-radius: 9999px;
  border: 1px solid rgba(0, 0, 0, 0.05);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #00b578;
}

.sleep-badge {
  font-size: 12px;
  color: #fa8c16;
  background: #fff7e6;
  padding: 3px 8px;
  border-radius: 9999px;
}

.bar-right {
  display: flex;
  gap: 6px;
}

.action-btn {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  background: #ffffff;
  color: #595959;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  transition: transform 0.15s;
}

.action-btn:active {
  transform: scale(0.92);
}

/* 群切换导航条 (单层横滑胶囊) */
.group-nav-bar {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 2px 0 6px 0;
  -webkit-overflow-scrolling: touch;
}

.group-nav-bar::-webkit-scrollbar {
  display: none;
}

.group-nav-item {
  border: none;
  background: #ffffff;
  padding: 8px 16px;
  border-radius: 9999px;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 6px;
  color: #595959;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
  transition: all 0.2s cubic-bezier(0.2, 0.8, 0.2, 1);
}

.group-nav-item.active {
  background: #007dff;
  color: #ffffff;
  box-shadow: 0 3px 10px rgba(0, 125, 255, 0.22);
}

.nav-name {
  font-weight: 600;
}

.nav-id {
  font-size: 11px;
  opacity: 0.7;
}

/* 设置分组列表 (单层无毛刺圆角卡片) */
.content-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.pref-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pref-group-title {
  font-size: 13px;
  font-weight: 600;
  color: #8c8c8c;
  padding: 0 12px;
}

.header-clickable {
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.fold-arrow {
  font-size: 11px;
  color: #007dff;
}

.pref-card {
  background: #ffffff;
  border-radius: 20px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.pref-divider {
  height: 1px;
  background: rgba(0, 0, 0, 0.04);
  margin: 0 16px;
}

/* 滑块专属槽位与防溢出样式 */
.slider-item {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.slider-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.slider-title {
  font-size: 15px;
  font-weight: 500;
  color: #1a1a1a;
}

.slider-desc {
  font-size: 12px;
  color: #8c8c8c;
  margin-top: 1px;
}

.slider-badge {
  font-size: 13px;
  font-weight: 700;
  color: #007dff;
  background: #f0f7ff;
  padding: 3px 10px;
  border-radius: 9999px;
  min-width: 46px;
  text-align: center;
}

.slider-track-wrap {
  width: 100%;
  padding: 2px 4px;
  box-sizing: border-box;
}

/* 群记忆列表 */
.memories-box {
  padding: 14px 16px;
}

.empty-hint {
  text-align: center;
  color: #8c8c8c;
  font-size: 13px;
  padding: 16px 0;
}

.memory-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.memory-item {
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
}

.memory-item:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.mem-row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.mem-user-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
}

.mem-name {
  font-weight: 600;
}

.mem-qq {
  font-size: 12px;
  color: #8c8c8c;
}

.mem-tag {
  font-size: 11px;
  background: #f0f0f0;
  padding: 1px 6px;
  border-radius: 4px;
  color: #595959;
}

.mem-favor-tag {
  font-size: 12px;
  font-weight: 700;
}

.mem-favor-tag.high { color: #f43f5e; }
.mem-favor-tag.normal { color: #007dff; }

.favor-track {
  width: 100%;
  height: 5px;
  background: #f0f0f0;
  border-radius: 9999px;
  overflow: hidden;
  margin-bottom: 6px;
}

.favor-bar {
  height: 100%;
  border-radius: 9999px;
  transition: width 0.3s;
}

.favor-bar.high { background: #f43f5e; }
.favor-bar.normal { background: #007dff; }

.mem-notes {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.note-pill {
  font-size: 11px;
  color: #8c8c8c;
  background: #f6f7f9;
  padding: 2px 6px;
  border-radius: 4px;
}

/* 日志卡片 */
.log-card {
  padding: 14px;
  background: #1e1e1e;
  color: #d1d5db;
}

.log-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-size: 12px;
}

.auto-scroll-check {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}

.mini-btn {
  background: rgba(255, 255, 255, 0.1);
  border: none;
  color: #fff;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
}

.log-view {
  height: 280px;
  overflow-y: auto;
  font-family: monospace;
  font-size: 11px;
  line-height: 1.5;
  background: #141414;
  padding: 10px;
  border-radius: 10px;
}

.log-line {
  white-space: pre-wrap;
  word-break: break-all;
  color: #a3a3a3;
}
</style>
