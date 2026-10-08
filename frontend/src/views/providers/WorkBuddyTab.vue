<script setup lang="ts">
// WorkBuddy tab（供应商页 F4）：账号区=多账号卡片（昵称/配额/签到状态/连签天数）+
// 添加（设备授权弹窗+轮询）/删除（二次确认）/签到（loading/已签到态）/配额刷新；
// 模型区（本需求落地）：WorkBuddy 目录（/v1/models?all=1 过滤 WorkBuddy/ 前缀——
// 目录来自 relay 的 v3/config 并集，转发已由聊天反代提供）+ 白名单启停 + 手动连通性测试。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton, NCard, NEmpty, NModal, NPagination, NPopconfirm, NTag, useMessage } from 'naive-ui'
import { getModels, openAuthorizeUrl, type OaiModel, type WbAccount } from '../../api'
import { workbuddyStore } from '../../stores/workbuddy'
import { store, useConfigStore } from '../../store'
import { testModel } from '../../modelCheck'

const message = useMessage()
const wb = workbuddyStore
const config = useConfigStore()
const DISABLE_ALL = '__none__'

// ── 模型区（WorkBuddy 目录：全名前缀过滤 + 白名单 + 手动测试） ──
const wbModels = ref<OaiModel[]>([])
const modelSearch = ref('')
const testingModel = ref<string | null>(null)
const WB_PREFIX = 'WorkBuddy/'

const filteredModels = computed(() => {
  const q = modelSearch.value.trim().toLowerCase()
  return q ? wbModels.value.filter(m => m.id.toLowerCase().includes(q)) : wbModels.value
})

function isModelEnabled(id: string): boolean {
  const w = config.model_whitelist
  if (w.length === 0) return true
  if (w.length === 1 && w[0] === DISABLE_ALL) return false
  return w.includes(id)
}

async function loadWbModels() {
  try {
    const all = (await getModels(true)).data
    wbModels.value = all.filter(m => m.id.startsWith(WB_PREFIX))
  } catch (e) {
    message.error(String(e))
  }
  if (!config.loaded) {
    try {
      await config.load()
    } catch (e) {
      message.error(String(e))
    }
  }
}

async function onToggleModel(id: string) {
  try {
    const w = config.model_whitelist
    let next: string[]
    if (w.length === 0) {
      // 全部启用 → 显式列出除该模型外的全部（含本 tab 与牛码条目，保证语义不变）
      const all = (await getModels(true)).data.map(m => m.id)
      next = all.filter(x => x !== id)
    } else if (w.length === 1 && w[0] === DISABLE_ALL) {
      next = [id]
    } else {
      next = w.includes(id) ? w.filter(x => x !== id) : [...w, id]
    }
    await config.update({ model_whitelist: next })
  } catch (e) {
    message.error(String(e))
  }
}

async function onAllEnableModels() {
  try {
    const w = config.model_whitelist
    if (w.length === 0) return
    if (w.length === 1 && w[0] === DISABLE_ALL) {
      await config.update({ model_whitelist: wbModels.value.map(m => m.id) })
      return
    }
    const wbIds = new Set(wbModels.value.map(m => m.id))
    const kept = w.filter(x => !wbIds.has(x))
    await config.update({ model_whitelist: [...kept, ...wbIds] })
  } catch (e) {
    message.error(String(e))
  }
}

async function onAllDisableModels() {
  try {
    const w = config.model_whitelist
    const wbIds = new Set(wbModels.value.map(m => m.id))
    if (w.length === 0) {
      // 全部启用 → 全禁用语义：列表只剩其它前缀之外没有 → 用哨兵（全局全禁）；
      // 有其它供应商条目时显式列出非 WorkBuddy 条目，避免误停牛码/自定义。
      const all = (await getModels(true)).data.map(m => m.id)
      const others = all.filter(x => !wbIds.has(x))
      await config.update({ model_whitelist: others.length ? others : [DISABLE_ALL] })
      return
    }
    await config.update({ model_whitelist: w.filter(x => !wbIds.has(x)) })
  } catch (e) {
    message.error(String(e))
  }
}

async function onTestModel(id: string) {
  testingModel.value = id
  store.modelStatus[id] = 'testing'
  try {
    const ok = await testModel(id)
    store.modelStatus[id] = ok ? 'success' : 'failure'
    if (ok) message.success(`模型 ${id} 连通`)
    else message.error(`模型 ${id} 连接失败`)
  } finally {
    testingModel.value = null
  }
}

function modelStatusText(id: string): string {
  const s = store.modelStatus[id]
  if (s === 'testing') return '测试中…'
  if (s === 'success') return '成功'
  if (s === 'failure') return '失败'
  return '未测试'
}

function modelStatusClass(id: string): string {
  const s = store.modelStatus[id]
  if (s === 'testing') return 'busy'
  if (s === 'success') return 'green'
  if (s === 'failure') return 'red'
  return 'gray'
}

// ── 登录弹窗与轮询 ──
const showLoginDlg = ref(false)
let pollTimer: number | undefined

function openLoginDlg() {
  showLoginDlg.value = true
  if (wb.loginPhase === 'pending' && wb.loginId) {
    // 复用进行中的登录（HMR/重开弹窗场景）：恢复轮询，不重复发起
    startPolling()
  } else {
    void doStartLogin()
  }
}

async function doStartLogin() {
  try {
    await wb.startLogin()
    // 对齐 cockpit-tools 形态：不自动开浏览器，链接展示在弹窗内由用户复制/手动打开
    startPolling()
  } catch (e) {
    wb.loginPhase = 'failed'
    wb.loginError = String(e)
  }
}

/** 复制授权链接到剪贴板（弹窗内链接行右侧按钮）。 */
async function copyAuthorizeUrl() {
  try {
    await navigator.clipboard.writeText(wb.authorizeUrl)
    message.success('授权链接已复制')
  } catch (e) {
    message.error(`复制失败：${String(e)}`)
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const phase = await wb.pollLogin()
      if (phase === 'success') {
        stopPolling()
        showLoginDlg.value = false
        message.success('WorkBuddy 账号添加成功')
      } else if (phase === 'failed') {
        stopPolling()
      }
    } catch {
      /* 单次轮询失败继续等（relay 短暂不可达） */
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer !== undefined) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

watch(showLoginDlg, open => {
  if (!open && wb.loginPhase === 'pending') {
    void wb.cancelLogin()
    stopPolling()
  }
})

// ── 账号动作 ──
const accounts = computed(() => wb.accounts)

// 配额明细弹窗（用户拍板 2026-10-07：配额数据点开弹窗看，不挤卡片行）
const showQuotaDlg = ref(false)
/** 弹窗只记 uid；账号数据实时从 store 取（快照引用会让弹窗内「刷新配额」后时间/数据不更新）。 */
const quotaUid = ref('')
const quotaAccount = computed<WbAccount | null>(() => wb.accounts.find(a => a.uid === quotaUid.value) ?? null)

function openQuotaDlg(a: WbAccount) {
  quotaUid.value = a.uid
  quotaPage.value = 1
  showQuotaDlg.value = true
}

/** 当前弹窗账号的逐包明细（computed 收窄 undefined，模板直接用）。 */
const quotaPackages = computed(() => quotaAccount.value?.quota.packages ?? [])

// 逐包表格分页（对齐官方套餐页：底部「共N条数据 + 页码」）
const quotaPage = ref(1)
const quotaPageSize = 10
const pagedPackages = computed(() => {
  const start = (quotaPage.value - 1) * quotaPageSize
  return quotaPackages.value.slice(start, start + quotaPageSize)
})

function fmtQuota(a: WbAccount): string {
  const q = a.quota
  const remaining = q.remaining ?? 0
  const total = q.total ?? 0
  if (!total) return '配额未刷新'
  const pct = total > 0 ? Math.round((remaining / total) * 100) : 0
  return `${fmtNum(remaining)} / ${fmtNum(total)}（${pct}%）`
}

function fmtNum(n: number): string {
  return n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(Math.round(n * 10) / 10)
}

/** ISO 时间串 → 本地可读格式（YYYY-MM-DD HH:MM:SS）；非 ISO 原样返回。 */
function fmtIso(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function onCheckin(a: WbAccount) {
  try {
    const res = await wb.checkin(a.uid)
    if (res.success) message.success(`${a.nickname || a.uid}：${res.message}`)
    else message.warning(res.message || '签到失败')
  } catch (e) {
    message.error(String(e))
  }
}

async function onRefreshQuota(a: WbAccount) {
  try {
    await wb.refreshQuota(a.uid)
    message.info('配额已刷新')
  } catch (e) {
    message.error(String(e))
  }
}

async function onRefreshStatus(a: WbAccount) {
  try {
    await wb.refreshCheckinStatus(a.uid)
    message.info('签到状态已刷新')
  } catch (e) {
    message.error(String(e))
  }
}

async function onRemove(a: WbAccount) {
  try {
    await wb.remove(a.uid)
    message.info('账号已删除')
  } catch (e) {
    message.error(String(e))
  }
}

onMounted(() => {
  void wb.load().catch(e => message.error(String(e)))
  void loadWbModels()
})
// 账号区「添加账号」成功后 / 其它页面同步目录 → 刷新模型列表
watch(
  () => wb.accounts.length,
  () => void loadWbModels()
)
onUnmounted(stopPolling)
</script>

<template>
  <div class="stack">
    <!-- 账号区：多账号卡片 -->
    <div class="card">
      <div class="head">
        <span class="zone-title">账号</span>
        <span class="note">设备授权登录（浏览器完成授权后自动加入账号池）</span>
        <div class="spacer" />
        <NButton
          size="small"
          type="primary"
          @click="openLoginDlg"
        >
          添加账号
        </NButton>
      </div>

      <NEmpty
        v-if="wb.loaded && accounts.length === 0"
        description="暂无 WorkBuddy 账号，点击「添加账号」用浏览器完成授权登录"
        size="small"
      />
      <div
        v-else
        class="grid"
      >
        <div
          v-for="a in accounts"
          :key="a.uid"
          class="acct-card"
        >
          <div class="acct-head">
            <span
              class="acct-name"
              :title="a.uid"
              >{{ a.nickname || a.uid }}</span
            >
            <NTag
              size="small"
              :type="a.checkin.today_checked_in ? 'success' : 'warning'"
              :bordered="false"
            >
              {{ a.checkin.today_checked_in ? '今日已签' : '今日未签' }}
            </NTag>
            <span class="spacer" />
            <div class="acct-ops">
              <NButton
                size="tiny"
                type="primary"
                ghost
                :loading="wb.busyUid === a.uid && a.checkin.today_checked_in !== true"
                :disabled="a.checkin.today_checked_in === true || wb.busyUid === a.uid"
                @click="onCheckin(a)"
              >
                {{ a.checkin.today_checked_in ? '已签到' : '签到' }}
              </NButton>
              <NButton
                size="tiny"
                :loading="wb.busyUid === a.uid"
                :disabled="wb.busyUid !== ''"
                @click="onRefreshQuota(a)"
              >
                刷新配额
              </NButton>
              <NButton
                size="tiny"
                :disabled="wb.busyUid !== ''"
                @click="onRefreshStatus(a)"
              >
                刷新状态
              </NButton>
              <NPopconfirm @positive-click="onRemove(a)">
                <template #trigger>
                  <NButton
                    size="tiny"
                    type="error"
                    ghost
                  >
                    删除
                  </NButton>
                </template>
                删除账号「{{ a.nickname || a.uid }}」？其凭证与签到/配额缓存一并清除。
              </NPopconfirm>
            </div>
          </div>
          <div class="acct-meta">
            <span>连签 {{ a.checkin.streak_days ?? 0 }} 天</span>
            <span
              v-if="a.quota.total"
              class="mono quota-link"
              title="点击查看配额明细"
              @click="openQuotaDlg(a)"
              >{{ fmtQuota(a) }} ›</span
            >
            <span
              v-else-if="a.quota.fetched_at"
              class="mono quota-link"
              title="上游未返回资源包（免费号/企业号形态差异），点击查看详情"
              @click="openQuotaDlg(a)"
              >无配额数据 · 到期 {{ a.quota.expire_at || '—' }} ›</span
            >
            <span
              v-else
              class="mono quota-link"
              title="登录预取失败或尚未刷新，点「刷新配额」拉取"
              @click="openQuotaDlg(a)"
              >配额未刷新 ›</span
            >
          </div>
        </div>
      </div>
    </div>

    <!-- 模型区：WorkBuddy 目录（前缀过滤）+ 白名单启停 + 手动连通性测试 -->
    <NCard
      size="small"
      :bordered="true"
      class="zone-card"
    >
      <template #header>
        <div class="head">
          <span class="zone-title">模型</span>
          <span class="note">目录来自 WorkBuddy（v3/config），请求模型名用 WorkBuddy/ 前缀</span>
          <div class="spacer" />
          <input
            v-model="modelSearch"
            class="search"
            placeholder="搜索模型名"
          />
          <NButton
            size="small"
            :disabled="wbModels.length === 0"
            @click="onAllEnableModels"
          >
            全部启用
          </NButton>
          <NButton
            size="small"
            :disabled="wbModels.length === 0"
            @click="onAllDisableModels"
          >
            全部禁用
          </NButton>
        </div>
      </template>
      <NEmpty
        v-if="wbModels.length === 0"
        description="暂无 WorkBuddy 模型目录（请先添加账号；relay 会从上游拉取目录）"
        size="small"
      />
      <table
        v-else
        class="tbl"
      >
        <thead>
          <tr>
            <th>模型名</th>
            <th class="col-status">状态（点击测试）</th>
            <th class="col-toggle">启用</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="m in filteredModels"
            :key="m.id"
          >
            <td class="mono">{{ m.id }}</td>
            <td class="col-status">
              <span
                class="wp-cap pressable"
                :class="modelStatusClass(m.id)"
                :title="modelStatusText(m.id)"
                @click="onTestModel(m.id)"
              >
                {{ testingModel === m.id ? '测试中…' : modelStatusText(m.id) }}
              </span>
            </td>
            <td class="col-toggle">
              <input
                type="checkbox"
                :checked="isModelEnabled(m.id)"
                @change="onToggleModel(m.id)"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </NCard>

    <!-- 添加账号弹窗（设备授权 + 轮询等待） -->
    <NModal
      v-model:show="showLoginDlg"
      preset="card"
      title="添加 WorkBuddy 账号"
      :style="{ width: '440px' }"
      :mask-closable="false"
    >
      <template v-if="wb.loginPhase === 'pending'">
        <p class="dlg-text">点击下方按钮将在浏览器中打开 WorkBuddy（CodeBuddy CN）授权页面。</p>
        <div class="auth-box">
          <p class="auth-box-title">授权 IDE 登录信息</p>
          <ul class="auth-box-list">
            <li>在浏览器完成 OAuth 后即可添加账号并用于 IDE 切换。</li>
            <li>授权完成后会自动刷新资源包配额数据。</li>
            <li>账号卡片将按资源包展示额度、进度和刷新/到期时间。</li>
          </ul>
        </div>
        <div class="url-row">
          <input
            class="url-input mono"
            readonly
            :value="wb.authorizeUrl"
            @focus="($event.target as HTMLInputElement).select()"
          />
          <button
            class="copy-btn"
            title="复制授权链接"
            @click="copyAuthorizeUrl"
          >
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect
                x="9"
                y="9"
                width="13"
                height="13"
                rx="2"
                ry="2"
              />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          </button>
        </div>
        <p class="dlg-expire">授权有效期：600s；轮询间隔：2s</p>
        <NButton
          class="open-btn"
          type="primary"
          size="large"
          @click="openAuthorizeUrl(wb.authorizeUrl)"
        >
          <span class="open-btn-inner">&#127760; 在浏览器中打开</span>
        </NButton>
      </template>
      <template v-else-if="wb.loginPhase === 'failed'">
        <p class="dlg-text err"> 登录失败：{{ wb.loginError || '请重试' }} </p>
      </template>
      <template v-else-if="wb.loginPhase === 'starting'">
        <p class="dlg-text">正在发起设备授权…</p>
      </template>
      <template #footer>
        <div class="dlg-actions">
          <NButton
            v-if="wb.loginPhase === 'failed'"
            type="primary"
            @click="doStartLogin"
          >
            重试
          </NButton>
          <NButton @click="showLoginDlg = false">
            {{ wb.loginPhase === 'pending' ? '取消' : '关闭' }}
          </NButton>
        </div>
      </template>
    </NModal>

    <!-- 配额明细弹窗 -->
    <NModal
      v-model:show="showQuotaDlg"
      preset="card"
      :title="`配额明细 · ${quotaAccount?.nickname || quotaAccount?.uid || ''}`"
      :style="{ width: '680px' }"
    >
      <template v-if="quotaAccount">
        <div
          v-if="quotaAccount.quota.total || quotaPackages.length > 0"
          class="quota-detail"
        >
          <div class="quota-summary">
            <span class="q-total mono">{{ fmtNum(quotaAccount.quota.used ?? 0) }}/{{ fmtNum(quotaAccount.quota.total ?? 0) }}积分</span>
            <span class="q-dim">（总计）</span>
          </div>

          <!-- 逐包表格（对齐官方套餐页形态：到期时间/获取方式/已用·总量；e2e 用户拍板 2026-10-08） -->
          <table class="pkg-tbl">
            <thead>
              <tr>
                <th>到期时间</th>
                <th>获取方式</th>
                <th class="col-num">已用/总量</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(p, i) in pagedPackages"
                :key="i"
              >
                <td class="mono">{{ p.expire_at || '长期有效' }}</td>
                <td>{{ p.name || '未命名资源包' }}</td>
                <td class="mono col-num">{{ fmtNum(p.used ?? 0) }}/{{ fmtNum(p.total ?? 0) }}</td>
              </tr>
            </tbody>
          </table>

          <div class="pkg-footer">
            <span class="q-dim">共{{ quotaPackages.length }}条数据</span>
            <NPagination
              v-model:page="quotaPage"
              :page-size="quotaPageSize"
              :item-count="quotaPackages.length"
              size="small"
            />
          </div>

          <div class="quota-row">
            <span class="q-label">刷新时间</span>
            <span class="mono q-val">{{ fmtIso(quotaAccount.quota.fetched_at) }}</span>
          </div>
        </div>
        <p
          v-else-if="quotaAccount.quota.fetched_at"
          class="dlg-text"
        >
          上游已响应但未返回资源包（免费号/企业号的常见形态）。 基础体验包与活动赠送包的额度仅在产生用量后出现。
        </p>
        <p
          v-else
          class="dlg-text"
        >
          尚未获取到配额数据，请先点「刷新配额」。
        </p>
      </template>
      <template #footer>
        <div class="dlg-actions">
          <NButton
            size="small"
            :loading="quotaAccount ? wb.busyUid === quotaAccount.uid : false"
            @click="quotaAccount && onRefreshQuota(quotaAccount)"
          >
            刷新配额
          </NButton>
          <NButton
            size="small"
            type="primary"
            @click="showQuotaDlg = false"
          >
            关闭
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.card {
  background: var(--cp-panel);
  border: 1px solid var(--cp-border);
  border-radius: 10px;
  padding: 14px 16px;
}
.zone-card {
  background: var(--cp-panel);
  border-radius: 10px;
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.zone-title {
  font-size: 14px;
  font-weight: 600;
}
.note {
  color: var(--cp-dim);
  font-size: 12px;
}
.spacer {
  flex: 1;
}
.grid {
  display: grid;
  /* 一账号占满整行（用户拍板 2026-10-07）：minmax 下限抬高，窄容器也保持单列 */
  grid-template-columns: repeat(auto-fill, minmax(560px, 1fr));
  gap: 10px;
}
.acct-card {
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.acct-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.acct-head .spacer {
  flex: 1;
}
.acct-ops {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.quota-link {
  cursor: pointer;
  transition: color 0.15s;
}
.quota-link:hover {
  color: var(--cp-cyan);
}
/* 配额明细弹窗 */
.quota-detail {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 总计行（对齐官方页「0/1700积分（总计）」形态） */
.quota-summary {
  margin: 2px 0 6px;
}
.q-total {
  font-size: 15px;
  font-weight: 600;
  color: var(--cp-text);
}
.q-dim {
  color: var(--cp-dim);
  font-size: 12px;
}
.quota-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}
.q-label {
  color: var(--cp-dim);
}
/* 逐包表格（对齐官方套餐页：三列表格 + 底部分页栏） */
.pkg-tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.pkg-tbl th {
  text-align: left;
  color: var(--cp-dim);
  font-weight: 500;
  padding: 10px 8px;
  border-bottom: 1px solid var(--cp-border);
}
.pkg-tbl td {
  padding: 12px 8px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
}
.pkg-tbl .col-num {
  text-align: right;
  white-space: nowrap;
}
.pkg-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0 2px;
}
.acct-name {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.acct-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--cp-dim);
  font-size: 12px;
}
.mono {
  font-family: var(--cp-mono, monospace);
}
/* 模型区（WorkBuddy 目录表；样式对齐牛码 tab 的模型表口径） */
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.tbl th {
  text-align: left;
  color: var(--cp-dim);
  font-weight: 500;
  padding: 8px;
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 8px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
}
.search {
  width: 200px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-text);
  padding: 5px 10px;
  font-size: 13px;
}
.wp-cap {
  padding: 2px 10px;
  border-radius: 4px;
  border: 1px solid transparent;
  font-size: 12px;
  line-height: 16px;
  display: inline-block;
}
.wp-cap.green {
  background: rgba(34, 197, 94, 0.2);
  color: var(--cp-green);
}
.wp-cap.red {
  background: rgba(239, 68, 68, 0.2);
  color: var(--cp-red);
}
.wp-cap.busy {
  background: rgba(34, 211, 238, 0.2);
  color: var(--cp-cyan);
}
.wp-cap.gray {
  background: rgba(100, 116, 139, 0.2);
  color: var(--cp-dim);
}
.pressable {
  cursor: pointer;
  transition: filter 0.15s;
}
.pressable:hover {
  filter: brightness(1.2);
}
.col-status {
  width: 124px;
}
.col-toggle {
  width: 60px;
}
.dlg-text {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.7;
}
.dlg-text.err {
  color: var(--cp-red);
}
.dlg-expire {
  margin: 8px 0 10px;
  text-align: center;
  color: var(--cp-dim);
  font-size: 12px;
}
/* 授权说明盒（对齐 cockpit-tools：「授权 IDE 登录信息」信息框） */
.auth-box {
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 12px;
}
.auth-box-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
}
.auth-box-list {
  margin: 0;
  padding-left: 18px;
  color: var(--cp-dim);
  font-size: 12px;
  line-height: 1.9;
}
/* 授权链接行：只读输入框 + 复制按钮 */
.url-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.url-input {
  flex: 1;
  min-width: 0;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-text);
  padding: 8px 10px;
  font-size: 12px;
}
.url-input:focus {
  outline: none;
  border-color: var(--cp-cyan);
}
.copy-btn {
  flex: none;
  width: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-dim);
  cursor: pointer;
  transition:
    color 0.15s,
    border-color 0.15s;
}
.copy-btn:hover {
  color: var(--cp-cyan);
  border-color: var(--cp-cyan);
}
/* 整行打开浏览器按钮 */
.open-btn {
  width: 100%;
}
.open-btn-inner {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.dlg-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
