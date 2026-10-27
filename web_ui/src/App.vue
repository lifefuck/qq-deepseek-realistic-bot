<script setup lang="ts">
import { ref, onMounted, computed, watch, nextTick } from 'vue'
import {
  MiuixButton,
  MiuixSwitchPreference,
  MiuixSlider,
  MiuixTabRow,
  MiuixSnackbarHost,
  showSnackbar
} from 'miuix-vue'

// ==================== 类型 ====================
interface UsageBucket {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  requests: number
  cost_cny: number
}
interface TodayBucket extends UsageBucket { date: string }
interface PeriodBucket { prompt_tokens: number; completion_tokens: number; total_tokens: number; cost_cny: number }
interface ModelBucket extends UsageBucket {}
interface GroupBucket { total_tokens: number; cost_cny: number; requests: number }
interface RateTier { input: number; output: number }
interface ModelPrice {
  name: string
  provider: string
  offpeak: RateTier
  peak: RateTier
}
interface TokenUsage {
  total: UsageBucket
  periods: { peak: PeriodBucket; offpeak: PeriodBucket }
  today: TodayBucket
  by_model: Record<string, ModelBucket>
  by_group: Record<string, GroupBucket>
  current_period: 'peak' | 'offpeak'
  pricing_table: Record<string, ModelPrice>
}
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
  token_usage?: TokenUsage
  logs: string[]
}

// ==================== 鉴权 ====================
const isAuthenticated = ref(false)
const passwordInput = ref('')
const authChecking = ref(true)
const loginError = ref('')

// ==================== 业务状态 ====================
const status = ref<BotStatus | null>(null)
const selectedGroupId = ref<number | null>(null)
const activeTab = ref(0)
const showLogs = ref(false)
const showMemories = ref(false)
const autoScroll = ref(true)
const logContainer = ref<HTMLElement | null>(null)

const currentGroup = computed(() => {
  if (!status.value?.groups?.length) return null
  return status.value.groups.find(g => g.group_id === selectedGroupId.value) || status.value.groups[0]
})

const currentConfig = computed(() => {
  if (!status.value?.configs || !currentGroup.value) return {}
  return status.value.configs[String(currentGroup.value.group_id)] || currentGroup.value.config || {}
})

// 开关乐观更新：点了立刻翻，不等接口回来
const pendingCfg = ref<Record<string, any>>({})
const cfg = computed(() => ({ ...(currentConfig.value || {}), ...pendingCfg.value }))
watch(selectedGroupId, () => { pendingCfg.value = {} })

const localProb = ref(0)
let isSliding = false

watch(() => cfg.value.interject_prob, (v) => {
  if (!isSliding) localProb.value = Number(v ?? 0.35)
}, { immediate: true })

function onSliderInput(val: number) {
  isSliding = true
  localProb.value = Math.round(val * 100) / 100
}

function onSliderCommit() {
  if (!isSliding) return
  isSliding = false
  if (Math.abs(localProb.value - Number(cfg.value.interject_prob ?? 0.35)) > 1e-9) {
    saveConfig('interject_prob', localProb.value)
  }
}

// ==================== 用量数据 ====================
const usage = computed<TokenUsage | null>(() => status.value?.token_usage || null)

const periodLabel = computed(() => usage.value?.current_period === 'peak' ? '峰时' : '谷时')

const groupNameMap = computed(() => {
  const m: Record<string, string> = {}
  for (const g of status.value?.groups || []) m[String(g.group_id)] = g.group_name
  m['private'] = '私聊'
  return m
})

// 模型用量按花费倒序
const modelRows = computed(() => {
  const t = usage.value
  if (!t) return []
  return Object.entries(t.by_model)
    .map(([key, v]) => ({
      key,
      price: t.pricing_table?.[key] || null,
      ...v
    }))
    .sort((a, b) => b.cost_cny - a.cost_cny)
})

// 群用量按花费倒序
const groupRows = computed(() => {
  const t = usage.value
  if (!t) return []
  const rows = Object.entries(t.by_group).map(([gid, v]) => ({
    gid,
    name: groupNameMap.value[gid] || gid,
    ...v
  }))
  rows.sort((a, b) => b.cost_cny - a.cost_cny)
  const max = rows.reduce((m, r) => Math.max(m, r.cost_cny), 0) || 1
  return rows.map(r => ({ ...r, ratio: Math.max(r.cost_cny / max, 0.02) }))
})

const peakRatio = computed(() => {
  const t = usage.value
  if (!t) return 0
  const sum = t.periods.peak.total_tokens + t.periods.offpeak.total_tokens
  return sum > 0 ? t.periods.peak.total_tokens / sum : 0
})

// ==================== 格式化 ====================
// 金额格式化：统一保留 2 位小数（不足 0.01 的极小值保留 4 位，普通金额统一 2 位杜绝长短不一）
function fmtMoney(v: number): string {
  if (!v || v === 0) return '¥0.00'
  if (v < 0.01) return '¥' + v.toFixed(4)
  return '¥' + v.toFixed(2)
}

function fmtTok(n: number): string {
  if (!n) return '0'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}

function fmtRate(r?: RateTier): string {
  if (!r) return '—'
  return `¥${r.input} / ¥${r.output}`
}

function fmtUptime(secs: number): string {
  if (!secs) return '刚刚'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`
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

// ==================== 请求 ====================
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

async function handleLogin() {
  if (!passwordInput.value.trim()) {
    loginError.value = '请输入管理员密钥'
    return
  }
  await doLoginWithKey(passwordInput.value)
}

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
      if (autoScroll.value && showLogs.value) nextTick(scrollToBottom)
    }
  } catch {}
}

async function saveConfig(key: string, value: any) {
  if (!currentGroup.value) return
  const gid = currentGroup.value.group_id
  pendingCfg.value[key] = value
  try {
    const res = await fetch('/api/group/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: gid, key, value })
    })
    const data = await res.json()
    if (res.ok && data.status === 'ok') {
      if (status.value) {
        const gidStr = String(gid)
        if (!status.value.configs[gidStr]) status.value.configs[gidStr] = {}
        status.value.configs[gidStr][key] = value
      }
      delete pendingCfg.value[key]
    } else {
      delete pendingCfg.value[key]
      showSnackbar({ message: '保存失败：' + (data.error || '未知原因'), duration: 2500 })
    }
  } catch (e: any) {
    delete pendingCfg.value[key]
    showSnackbar({ message: '网络错误：' + e.message, duration: 2500 })
  }
}

function scrollToBottom() {
  if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
}

onMounted(() => {
  checkAuth()
  setInterval(() => {
    if (isAuthenticated.value) refreshData()
  }, 8000)
})
</script>

<template>
  <div class="app-root">
    <MiuixSnackbarHost />

    <!-- 加载中 -->
    <div v-if="authChecking" class="center-screen">
      <div class="spinner"></div>
    </div>

    <!-- 锁屏 -->
    <div v-else-if="!isAuthenticated" class="center-screen">
      <div class="auth-card">
        <div class="auth-lock">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <rect x="4" y="10.5" width="16" height="10" rx="3" />
            <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
          </svg>
        </div>
        <div class="auth-title">大肥鱼控制台</div>
        <div class="auth-desc">输入访问密钥解锁</div>
        <input
          v-model="passwordInput"
          type="password"
          placeholder="访问密钥"
          class="auth-input"
          autofocus
          @keyup.enter="handleLogin"
        />
        <div v-if="loginError" class="auth-error">{{ loginError }}</div>
        <MiuixButton type="primary" class="auth-submit" @click="handleLogin">解锁</MiuixButton>
      </div>
    </div>

    <!-- 主界面 -->
    <div v-else class="page">
      <header class="top-bar">
        <div class="top-left">
          <div class="top-title">大肥鱼控制台</div>
          <div class="top-meta">
            <span class="live-dot"></span>
            <span>运行 {{ fmtUptime(status?.bot.uptime || 0) }}</span>
            <span v-if="status?.bot.is_sleeping" class="chip chip-warn">休眠中</span>
            <span class="chip chip-period" :class="usage?.current_period">
              {{ periodLabel }}计费
            </span>
          </div>
        </div>
        <div class="top-actions">
          <button class="icon-btn" title="刷新" @click="refreshData">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1" />
              <path d="M21 3.5V9h-5.5" />
            </svg>
          </button>
          <button class="icon-btn" title="锁定" @click="handleLogout">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <rect x="4" y="10.5" width="16" height="10" rx="3" />
              <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
            </svg>
          </button>
        </div>
      </header>

      <div class="tab-wrap">
        <div class="custom-tab-row">
          <button
            class="tab-item"
            :class="{ active: activeTab === 0 }"
            @click="activeTab = 0"
          >
            用量与花费
          </button>
          <button
            class="tab-item"
            :class="{ active: activeTab === 1 }"
            @click="activeTab = 1"
          >
            群设置
          </button>
        </div>
      </div>

      <!-- ==================== 用量与花费 ==================== -->
      <main v-if="activeTab === 0" class="body">
        <div v-if="!usage" class="empty-hint">暂无用量数据</div>

        <template v-else>
          <!-- 总额 -->
          <section class="block">
            <div class="block-title">花费总览</div>
            <div class="card">
              <div class="row row-spend">
                <div class="row-main">
                  <div class="row-label">累计花费</div>
                  <div class="row-sub">{{ usage.total.requests }} 次调用 · {{ fmtTok(usage.total.total_tokens) }} tokens</div>
                </div>
                <div class="spend-amount">{{ fmtMoney(usage.total.cost_cny) }}</div>
              </div>
              <div class="divider"></div>
              <div class="row">
                <div class="row-main"><div class="row-label">今日花费</div></div>
                <div class="row-value">{{ fmtMoney(usage.today.cost_cny) }}</div>
              </div>
              <div class="divider"></div>
              <div class="row">
                <div class="row-main"><div class="row-label">今日调用</div></div>
                <div class="row-value">{{ usage.today.requests }} 次 · {{ fmtTok(usage.today.total_tokens) }}</div>
              </div>
              <div class="divider"></div>
              <div class="row">
                <div class="row-main">
                  <div class="row-label">输入 / 输出</div>
                  <div class="row-sub">累计 token 结构</div>
                </div>
                <div class="row-value">{{ fmtTok(usage.total.prompt_tokens) }} / {{ fmtTok(usage.total.completion_tokens) }}</div>
              </div>
            </div>
          </section>

          <!-- 峰谷 -->
          <section class="block">
            <div class="block-title">
              峰谷计费分布
              <span class="block-note">当前按{{ periodLabel }}单价计费</span>
            </div>
            <div class="card">
              <div class="split-bar">
                <div class="split-peak" :style="{ width: (peakRatio * 100) + '%' }"></div>
              </div>
              <div class="split-legend">
                <div class="legend-item">
                  <span class="legend-dot peak"></span>
                  <span class="legend-name">峰时</span>
                  <span class="legend-val">{{ fmtMoney(usage.periods.peak.cost_cny) }}</span>
                </div>
                <div class="legend-item">
                  <span class="legend-dot offpeak"></span>
                  <span class="legend-name">谷时</span>
                  <span class="legend-val">{{ fmtMoney(usage.periods.offpeak.cost_cny) }}</span>
                </div>
              </div>
              <div class="divider"></div>
              <div class="row">
                <div class="row-main"><div class="row-label">峰时</div></div>
                <div class="row-value">{{ fmtTok(usage.periods.peak.total_tokens) }} tokens</div>
              </div>
              <div class="divider"></div>
              <div class="row">
                <div class="row-main"><div class="row-label">谷时</div></div>
                <div class="row-value">{{ fmtTok(usage.periods.offpeak.total_tokens) }} tokens</div>
              </div>
            </div>
          </section>

          <!-- 模型 -->
          <section class="block">
            <div class="block-title">模型明细</div>
            <div class="card">
              <div v-for="(m, idx) in modelRows" :key="m.key" class="model-item">
                <div v-if="idx > 0" class="divider"></div>
                <div class="model-head">
                  <div class="model-name-wrap">
                    <div class="model-name">{{ m.price?.name || m.key }}</div>
                    <div class="model-provider">{{ m.price?.provider || '未收录单价' }}</div>
                  </div>
                  <div class="model-cost">{{ fmtMoney(m.cost_cny) }}</div>
                </div>
                <div class="model-stats">
                  <span class="stat-pill">{{ m.requests }} 次</span>
                  <span class="stat-pill">{{ fmtTok(m.total_tokens) }} tokens</span>
                  <span class="stat-pill">{{ fmtTok(m.prompt_tokens) }} 入 / {{ fmtTok(m.completion_tokens) }} 出</span>
                </div>
                <div v-if="m.price" class="model-rates">
                  <div class="rate-pairs">
                    <div class="rate-line">
                      <span class="rate-tag" :class="{ active: usage.current_period === 'offpeak' }">谷</span>
                      <span class="rate-text">{{ fmtRate(m.price.offpeak) }}</span>
                    </div>
                    <div class="rate-line">
                      <span class="rate-tag peak" :class="{ active: usage.current_period === 'peak' }">峰</span>
                      <span class="rate-text">{{ fmtRate(m.price.peak) }}</span>
                    </div>
                  </div>
                  <div class="rate-unit">元/百万 token（入/出）</div>
                </div>
              </div>
              <div v-if="modelRows.length === 0" class="empty-hint">还没有模型调用记录</div>
            </div>
          </section>

          <!-- 群用量 -->
          <section class="block">
            <div class="block-title">各群花费排行</div>
            <div class="card">
              <div v-for="(g, idx) in groupRows" :key="g.gid" class="group-usage-item">
                <div v-if="idx > 0" class="divider"></div>
                <div class="gu-head">
                  <div class="gu-name">{{ g.name }}</div>
                  <div class="gu-cost">{{ fmtMoney(g.cost_cny) }}</div>
                </div>
                <div class="gu-track">
                  <div class="gu-bar" :style="{ width: (g.ratio * 100) + '%' }"></div>
                </div>
                <div class="gu-meta">{{ g.requests }} 次 · {{ fmtTok(g.total_tokens) }} tokens</div>
              </div>
              <div v-if="groupRows.length === 0" class="empty-hint">还没有群聊调用记录</div>
            </div>
          </section>
        </template>
      </main>

      <!-- ==================== 群设置 ==================== -->
      <main v-else class="body">
        <div class="group-nav">
          <button
            v-for="g in status?.groups"
            :key="g.group_id"
            class="nav-pill"
            :class="{ active: selectedGroupId === g.group_id }"
            @click="selectedGroupId = g.group_id"
          >
            <span class="nav-pill-name">{{ g.group_name }}</span>
            <span class="nav-pill-id">{{ g.group_id }}</span>
          </button>
        </div>

        <template v-if="currentGroup">
          <section class="block">
            <div class="block-title">响应行为</div>
            <div class="card">
              <MiuixSwitchPreference
                title="智能插话"
                summary="群友热聊时按气氛与概率主动参与"
                :modelValue="cfg.enable_interject ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_interject', v)"
              />
              <div class="divider"></div>

              <div class="slider-block">
                <div class="slider-head">
                  <div class="slider-text">
                    <div class="row-label">主动插话概率</div>
                    <div class="row-sub">群活跃冷场时动态调节</div>
                  </div>
                  <div class="slider-value">{{ Math.round(localProb * 100) }}%</div>
                </div>
                <div class="slider-track">
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

              <div class="divider"></div>
              <MiuixSwitchPreference
                title="完整猫娘人设"
                summary="深度人设、好感度判定与个性化语气"
                :modelValue="cfg.mode === 'user'"
                @update:modelValue="(v: boolean) => saveConfig('mode', v ? 'user' : 'default')"
              />
            </div>
          </section>

          <section class="block">
            <div class="block-title">功能开关</div>
            <div class="card">
              <MiuixSwitchPreference
                title="夜间作息"
                summary="凌晨时段进入睡眠，不惊扰群友"
                :modelValue="cfg.enable_sleep ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_sleep', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="戳一戳互动"
                summary="被戳时做出表情与文字反馈"
                :modelValue="cfg.enable_poke ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_poke', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="互动小游戏"
                summary="钓鱼、猜数字、抽卡与好感试炼"
                :modelValue="cfg.enable_games ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_games', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="插件指令让路"
                summary="识别游戏查询指令，避免 AI 抢答"
                :modelValue="cfg.enable_yunzai ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_yunzai', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="联网搜索"
                summary="需要实时信息时主动搜索"
                :modelValue="cfg.enable_search ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_search', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="B 站推送"
                summary="群友发链接时抓取内容参与讨论"
                :modelValue="cfg.enable_bilibili_push ?? false"
                @update:modelValue="(v: boolean) => saveConfig('enable_bilibili_push', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="整活模式"
                summary="允许梗图、玩梗与更放飞的发言"
                :modelValue="cfg.enable_fun ?? true"
                @update:modelValue="(v: boolean) => saveConfig('enable_fun', v)"
              />
              <div class="divider"></div>
              <MiuixSwitchPreference
                title="本群静默"
                summary="只读不发言，随时可恢复"
                :modelValue="cfg.muted ?? false"
                @update:modelValue="(v: boolean) => saveConfig('muted', v)"
              />
            </div>
          </section>

          <section class="block">
            <div class="block-title clickable" @click="showMemories = !showMemories">
              <span>本群群友档案 · {{ currentMemories.length }}</span>
              <span class="fold">{{ showMemories ? '收起' : '展开' }}</span>
            </div>
            <div v-if="showMemories" class="card card-pad">
              <div v-if="currentMemories.length === 0" class="empty-hint">本群还没有群友档案</div>
              <div v-for="(u, i) in currentMemories" :key="u.qq" class="mem-item" :class="{ 'not-first': i > 0 }">
                <div class="mem-top">
                  <div class="mem-id">
                    <span class="mem-name">{{ u.nickname }}</span>
                    <span class="mem-qq">{{ u.qq }}</span>
                    <span v-if="u.traits" class="mem-trait">{{ u.traits }}</span>
                  </div>
                  <div class="mem-favor" :class="{ high: u.favor >= 80 }">{{ u.favor }}</div>
                </div>
                <div class="favor-track">
                  <div class="favor-bar" :class="{ high: u.favor >= 80 }" :style="{ width: Math.min(100, Math.max(0, u.favor)) + '%' }"></div>
                </div>
                <div v-if="u.notes.length" class="mem-notes">
                  <span v-for="(n, ni) in u.notes.slice(0, 3)" :key="ni" class="note-pill">{{ n }}</span>
                </div>
              </div>
            </div>
          </section>

          <section class="block">
            <div class="block-title clickable" @click="showLogs = !showLogs">
              <span>运行日志</span>
              <span class="fold">{{ showLogs ? '收起' : '展开' }}</span>
            </div>
            <div v-if="showLogs" class="card log-card">
              <div class="log-bar">
                <label class="log-check">
                  <input type="checkbox" v-model="autoScroll" />
                  <span>自动置底</span>
                </label>
                <button class="mini-btn" @click="refreshData">刷新</button>
              </div>
              <div class="log-view" ref="logContainer">
                <div v-for="(line, idx) in status?.logs || []" :key="idx" class="log-line">{{ line }}</div>
              </div>
            </div>
          </section>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
/* ============ 基础 ============ */
.app-root {
  min-height: 100vh;
  background: #f5f6f7;
  color: #131313;
  display: flex;
  justify-content: center;
}

.center-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 100vh;
  padding: 20px;
}

.spinner {
  width: 30px;
  height: 30px;
  border: 3px solid rgba(52, 130, 255, 0.16);
  border-top-color: #3482ff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ============ 锁屏 ============ */
.auth-card {
  width: 100%;
  max-width: 340px;
  background: #fff;
  border-radius: 24px;
  padding: 32px 24px 26px;
  text-align: center;
}

.auth-lock {
  width: 52px;
  height: 52px;
  margin: 0 auto 14px;
  border-radius: 50%;
  background: #f2f5fa;
  color: #3482ff;
  display: flex;
  align-items: center;
  justify-content: center;
}

.auth-title {
  font-size: 19px;
  font-weight: 700;
  letter-spacing: 0.2px;
}

.auth-desc {
  font-size: 13px;
  color: #9096a3;
  margin: 6px 0 20px;
}

.auth-input {
  width: 100%;
  height: 46px;
  padding: 0 16px;
  border-radius: 14px;
  border: 1px solid #e8e9ec;
  background: #f7f8fa;
  font-size: 15px;
  outline: none;
  transition: border-color 0.18s, background 0.18s;
}

.auth-input:focus {
  border-color: #3482ff;
  background: #fff;
}

.auth-error {
  color: #e94634;
  font-size: 12px;
  margin-top: 10px;
}

.auth-submit {
  width: 100%;
  height: 46px;
  margin-top: 16px;
  font-weight: 600;
  border-radius: 14px;
}

/* ============ 页面骨架 ============ */
.page {
  width: 100%;
  max-width: 620px;
  padding: 14px 14px 40px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.top-bar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 2px 0;
}

.top-left {
  min-width: 0;
}

.top-title {
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 0.2px;
  white-space: nowrap;
}

.top-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
  font-size: 12px;
  color: #8b919e;
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.14);
}

.chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  line-height: 1.5;
  white-space: nowrap;
}

.chip-warn {
  color: #d97706;
  background: #fef3e2;
}

.chip-period.peak {
  color: #dc2626;
  background: #fdeceb;
}

.chip-period.offpeak {
  color: #0f8a4d;
  background: #e8f7ee;
}

.top-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.icon-btn {
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 50%;
  background: #fff;
  color: #555b66;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: transform 0.14s ease, background 0.14s ease;
}

.icon-btn:active {
  transform: scale(0.9);
  background: #eef0f3;
}

.tab-wrap {
  display: flex;
  justify-content: center;
}

.custom-tab-row {
  display: flex;
  background: #e8eaed;
  padding: 3px;
  border-radius: 999px;
  width: 100%;
  max-width: 320px;
}

.tab-item {
  flex: 1;
  border: none;
  background: transparent;
  padding: 7px 16px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  color: #717682;
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.2, 0, 0, 1);
  white-space: nowrap;
}

.tab-item.active {
  background: #fff;
  color: #131313;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
}

.body {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* ============ 分组 ============ */
.block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  color: #8b919e;
  padding: 0 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.block-note {
  font-size: 11px;
  font-weight: 400;
  color: #a8adb8;
  white-space: nowrap;
}

.block-title.clickable {
  cursor: pointer;
}

.fold {
  font-size: 12px;
  font-weight: 500;
  color: #3482ff;
  white-space: nowrap;
}

.card {
  background: #fff;
  border-radius: 20px;
  overflow: hidden;
}

.card-pad {
  padding: 4px 16px;
}

.divider {
  height: 1px;
  background: #f0f1f3;
  margin: 0 16px;
}

/* ============ 行 ============ */
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 13px 16px;
  min-height: 52px;
}

.row-main {
  min-width: 0;
  flex: 1;
}

.row-label {
  font-size: 15px;
  font-weight: 500;
  color: #131313;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-sub {
  font-size: 12px;
  color: #9096a3;
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-value {
  font-size: 14px;
  color: #4b515c;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

.row-spend {
  padding-top: 16px;
  padding-bottom: 16px;
}

.spend-amount {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.4px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

/* ============ 峰谷条 ============ */
.split-bar {
  height: 8px;
  border-radius: 999px;
  background: #eaf3ff;
  margin: 16px 16px 12px;
  overflow: hidden;
}

.split-peak {
  height: 100%;
  background: #f2913b;
  border-radius: 999px;
  transition: width 0.35s cubic-bezier(0.22, 0.9, 0.3, 1);
}

.split-legend {
  display: flex;
  gap: 22px;
  padding: 0 16px 14px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.legend-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

.legend-dot.peak { background: #f2913b; }
.legend-dot.offpeak { background: #7fb2f0; }

.legend-name {
  font-size: 12px;
  color: #8b919e;
  white-space: nowrap;
}

.legend-val {
  font-size: 12px;
  font-weight: 600;
  color: #4b515c;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* ============ 模型 ============ */
.model-item {
  padding: 14px 16px;
}

.model-item .divider {
  margin: 0 -16px 14px;
  width: calc(100% + 32px);
}

.model-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.model-name-wrap {
  min-width: 0;
}

.model-name {
  font-size: 15px;
  font-weight: 600;
  color: #131313;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-provider {
  font-size: 12px;
  color: #9096a3;
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.model-cost {
  font-size: 17px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

.model-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.stat-pill {
  font-size: 11px;
  color: #5b6473;
  background: #f4f6f8;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}

.model-rates {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #f2f3f5;
}

.rate-pairs {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rate-line {
  display: flex;
  align-items: center;
  gap: 5px;
}

.rate-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 5px;
  background: #eef4ff;
  color: #6d7c95;
  line-height: 1.6;
}

.rate-tag.peak {
  background: #fdf0e6;
  color: #b06a24;
}

.rate-tag.active {
  background: #3482ff;
  color: #fff;
}

.rate-text {
  font-size: 12px;
  color: #4b515c;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.rate-unit {
  font-size: 11px;
  color: #a8adb8;
  white-space: nowrap;
}

/* ============ 群用量 ============ */
.group-usage-item {
  padding: 13px 16px;
}

.group-usage-item .divider {
  margin: 0 -16px 13px;
  width: calc(100% + 32px);
}

.gu-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.gu-name {
  font-size: 14px;
  font-weight: 600;
  color: #131313;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.gu-cost {
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

.gu-track {
  height: 5px;
  border-radius: 999px;
  background: #f2f4f6;
  margin: 8px 0 6px;
  overflow: hidden;
}

.gu-bar {
  height: 100%;
  border-radius: 999px;
  background: #3482ff;
  transition: width 0.35s cubic-bezier(0.22, 0.9, 0.3, 1);
}

.gu-meta {
  font-size: 11px;
  color: #9096a3;
  white-space: nowrap;
}

/* ============ 群切换 ============ */
.group-nav {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 2px 2px 4px;
  scrollbar-width: none;
}

.group-nav::-webkit-scrollbar {
  display: none;
}

.nav-pill {
  display: flex;
  align-items: baseline;
  gap: 6px;
  border: none;
  background: #fff;
  padding: 8px 15px;
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.16s ease, color 0.16s ease;
}

.nav-pill-name {
  font-size: 13px;
  font-weight: 600;
  color: #3d434e;
}

.nav-pill-id {
  font-size: 10px;
  color: #a8adb8;
}

.nav-pill.active {
  background: #3482ff;
}

.nav-pill.active .nav-pill-name {
  color: #fff;
}

.nav-pill.active .nav-pill-id {
  color: rgba(255, 255, 255, 0.72);
}

/* ============ 滑块 ============ */
.slider-block {
  padding: 14px 16px;
}

.slider-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.slider-text {
  min-width: 0;
}

.slider-value {
  font-size: 14px;
  font-weight: 700;
  color: #3482ff;
  background: #eef4ff;
  padding: 3px 10px;
  border-radius: 999px;
  min-width: 50px;
  text-align: center;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

.slider-track {
  margin-top: 10px;
  padding: 0 2px;
}

/* ============ 记忆 ============ */
.mem-item {
  padding: 12px 0;
}

.mem-item.not-first {
  border-top: 1px solid #f0f1f3;
}

.mem-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 7px;
}

.mem-id {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.mem-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mem-qq {
  font-size: 11px;
  color: #a8adb8;
  flex-shrink: 0;
}

.mem-trait {
  font-size: 10px;
  color: #6b7280;
  background: #f4f6f8;
  padding: 1px 6px;
  border-radius: 5px;
  white-space: nowrap;
  flex-shrink: 0;
}

.mem-favor {
  font-size: 12px;
  font-weight: 700;
  color: #3482ff;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.mem-favor.high {
  color: #f43f5e;
}

.favor-track {
  height: 4px;
  border-radius: 999px;
  background: #f2f4f6;
  overflow: hidden;
}

.favor-bar {
  height: 100%;
  border-radius: 999px;
  background: #3482ff;
  transition: width 0.3s ease;
}

.favor-bar.high {
  background: #f43f5e;
}

.mem-notes {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 7px;
}

.note-pill {
  font-size: 11px;
  color: #6b7280;
  background: #f5f6f7;
  padding: 2px 8px;
  border-radius: 6px;
}

/* ============ 日志 ============ */
.log-card {
  padding: 12px;
  background: #1c1d1f;
}

.log-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 12px;
  color: #b6bac2;
}

.log-check {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}

.mini-btn {
  border: none;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 7px;
  cursor: pointer;
}

.log-view {
  height: 260px;
  overflow-y: auto;
  background: #141517;
  border-radius: 12px;
  padding: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.55;
}

.log-line {
  white-space: pre-wrap;
  word-break: break-all;
  color: #9aa0aa;
}

.empty-hint {
  text-align: center;
  font-size: 13px;
  color: #a8adb8;
  padding: 22px 16px;
}
</style>
