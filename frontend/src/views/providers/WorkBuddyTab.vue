<script setup lang="ts">
// WorkBuddy tab（供应商页 F4）：账号区=多账号卡片（昵称/配额/签到状态/连签天数）+
// 添加（设备授权弹窗+轮询）/删除（二次确认）/签到（loading/已签到态）/配额刷新
// + IDE 账号切换（「切到此号」= 凭证写入本机官方客户端并重启，见 spec「WorkBuddy-IDE账号切换」）；
// 模型区：WorkBuddy 目录（all=1 过滤前缀；主列表仅可见模型）+ 白名单启停 + 手动连通性测试
// + 「模型显示」弹窗（模型列表过时治理：无档位默认隐藏，显示/启用双开关联动——隐藏即停用）。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton, NCard, NCheckbox, NEmpty, NInput, NModal, NPagination, NPopconfirm, NRadio, NRadioGroup, NSwitch, NTabPane, NTabs, NTag, useMessage } from 'naive-ui'
import { getModels, openAuthorizeUrl, saveTextFile, type OaiModel, type WbAccount } from '../../api'
import { useWorkbuddyStoreFor, WB_CN_PLATFORM } from '../../stores/workbuddy'
import { store, useConfigStore } from '../../store'
import { testModel } from '../../modelCheck'
import ModelEffortSelect from '../../components/ModelEffortSelect.vue'

// 双域（WorkBuddy双域账号支持 §7）：platform prop 参数化——
// CN `workbuddy`（wbcn/ 前缀；签到/IDE 切号/导出/本机导入全保留）
// 国际版 `workbuddy-global`（wbg/ 前缀；无签到、无 IDE 切号/导出/本机导入——按能力位隐藏）。
const props = withDefaults(defineProps<{ platform?: string }>(), {
  platform: WB_CN_PLATFORM
})
const isGlobal = computed(() => props.platform === 'workbuddy-global')
/** 签到能力（国际版无签到，定稿）——双域唯一能力位残差（2026-10-15 二期）。 */
const supportsCheckin = computed(() => !isGlobal.value)

const message = useMessage()
// ⚠ 不变量（2026-10-14 串台 bug 教训）：store 绑定发生在 setup 期，本组件**必须带显式 key**
// 使用（见 Providers.vue 两个 tab 的 key="wb-cn"/"wb-global"）。两个 WorkBuddyTab 同型
// 不同平台，若不换实例，pane 切换时 Vue 就地复用组件、props.platform 更新但 setup 不重跑，
// 会把旧池 store 画到新平台 tab（实测：国际版 tab 显示 CN 账号 + wbcn/ 模型）。
const wb = useWorkbuddyStoreFor(props.platform)
const config = useConfigStore()
const DISABLE_ALL = '__none__'

// ── 模型区（WorkBuddy 目录：全名前缀过滤 + 白名单 + 手动测试） ──
const wbModels = ref<OaiModel[]>([])
const modelSearch = ref('')
const testingModel = ref<string | null>(null)
const WB_PREFIX = computed(() => (isGlobal.value ? 'wbg/' : 'wbcn/'))
/** 显示名（tab / 弹窗标题 / 空态文案用；对齐后端 PlatformInfo.display_name）。 */
const displayName = computed(() => (isGlobal.value ? 'WorkBuddy' : 'WorkBuddy CN'))

const filteredModels = computed(() => {
  const q = modelSearch.value.trim().toLowerCase()
  return q ? visibleModels.value.filter(m => m.id.toLowerCase().includes(q)) : visibleModels.value
})

/** 主列表 = 仅可见模型（隐藏项只出现在「模型显示」弹窗；缺字段防御性视为可见）。 */
const visibleModels = computed(() => wbModels.value.filter(m => m.visible !== false))

function isModelEnabled(id: string): boolean {
  const w = config.model_whitelist
  if (w.length === 0) return true
  if (w.length === 1 && w[0] === DISABLE_ALL) return false
  return w.includes(id)
}

// ── 「模型显示」弹窗（过时治理：全部 WB 模型逐条开关；显示主导、隐藏即停用） ──
const showVisibilityDlg = ref(false)
const visibilitySearch = ref('')

const visibilityList = computed(() => {
  const q = visibilitySearch.value.trim().toLowerCase()
  return q ? wbModels.value.filter(m => m.id.toLowerCase().includes(q)) : wbModels.value
})

const hiddenCount = computed(() => wbModels.value.filter(m => m.visible === false).length)

/** 白名单三态加入（空=全启用 / __none__ 哨兵 / 显式名单）；null = 无需变更。 */
async function wlAdd(id: string): Promise<string[] | null> {
  const w = config.model_whitelist
  if (w.length === 0) return null
  if (w.length === 1 && w[0] === DISABLE_ALL) return [id]
  return w.includes(id) ? null : [...w, id]
}

/** 白名单三态移除；null = 无需变更。 */
async function wlRemove(id: string): Promise<string[] | null> {
  const w = config.model_whitelist
  if (w.length === 0) {
    // 全部启用 → 显式列出除该模型外的全部（含牛码/自定义条目，保证其它供应商语义不变）
    const all = (await getModels(true)).data.map(m => m.id)
    const next = all.filter(x => x !== id)
    return next.length ? next : [DISABLE_ALL]
  }
  if (w.length === 1 && w[0] === DISABLE_ALL) return null
  const next = w.filter(x => x !== id)
  return next.length ? next : [DISABLE_ALL] // 空名单语义 = 全部启用，落哨兵防反转
}

/** 弹窗「显示」开关（唯一开关，2026-10-11 用户拍板简化）：同时操控显示与启用——
 * 关 ⇒ 隐藏+停用成对写入（chat 404）；开 ⇒ 显示+启用双开。
 * 「可见但停用」的微调态只由主列表「启用」开关产生。 */
async function onToggleShow(m: OaiModel, show: boolean) {
  try {
    const patch: Record<string, unknown> = {
      model_visibility: { ...config.model_visibility, [m.id]: show ? 'show' : 'hide' }
    }
    const next = show ? await wlAdd(m.id) : await wlRemove(m.id)
    if (next) patch.model_whitelist = next
    await config.update(patch)
    await loadWbModels() // 重拉 all=1 刷新 visible（口径由 relay 单点计算，前端不复刻）
  } catch (e) {
    message.error(String(e))
  }
}

/** 「全部显示」：当前隐藏项全部双开（显示 + 启用）；此后新无档位模型仍按默认隐藏。 */
async function onShowAllHidden() {
  try {
    const hidden = wbModels.value.filter(m => m.visible === false)
    if (hidden.length === 0) return
    const vis = { ...config.model_visibility }
    for (const m of hidden) vis[m.id] = 'show'
    const patch: Record<string, unknown> = { model_visibility: vis }
    const w = config.model_whitelist
    if (w.length === 1 && w[0] === DISABLE_ALL) {
      patch.model_whitelist = hidden.map(m => m.id)
    } else if (w.length > 0) {
      const ids = new Set(hidden.map(m => m.id))
      patch.model_whitelist = [...w.filter(x => !ids.has(x)), ...ids]
    } // 白名单空 = 全启用，无需动
    await config.update(patch)
    await loadWbModels()
  } catch (e) {
    message.error(String(e))
  }
}

/** 「恢复默认」：清空显示覆盖回默认判定（将变隐藏者由门控天然 404），白名单不动。 */
async function onResetVisibility() {
  try {
    await config.update({ model_visibility: {} })
    await loadWbModels()
    message.info('已恢复默认（无档位模型重新隐藏）')
  } catch (e) {
    message.error(String(e))
  }
}

/** 弹窗行档位描述：有档位列 level；无档位灰字（解释默认隐藏来源）。 */
function effortText(m: OaiModel): string {
  return m.reasoning_efforts?.length ? m.reasoning_efforts.join(' / ') : '无档位'
}

async function loadWbModels() {
  try {
    const all = (await getModels(true)).data
    wbModels.value = all.filter(m => m.id.startsWith(WB_PREFIX.value))
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

/** 主列表「启用」开关（语义不变）：白名单成员管理（三态），不动显示覆盖。 */
async function onToggleModel(id: string) {
  try {
    const next = isModelEnabled(id) ? await wlRemove(id) : await wlAdd(id)
    if (next) await config.update({ model_whitelist: next })
  } catch (e) {
    message.error(String(e))
  }
}

/** 点击模型名即复制到剪贴板（对齐牛码 tab）。 */
async function onCopy(id: string) {
  try {
    await navigator.clipboard.writeText(id)
    message.success(`已复制模型名：${id}`)
  } catch (e) {
    message.error(`复制失败：${String(e)}`)
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
    const kept = w.filter(x => !wbIds.has(x))
    // 全禁后名单为空 = 全部启用语义反转 → 落哨兵保持「全禁」意图
    await config.update({ model_whitelist: kept.length ? kept : [DISABLE_ALL] })
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

/** 上下文窗口格式化：200000 → 200K，1048576 → 1M（对齐牛码 tab）。 */
function fmtContext(w?: number | null): string {
  if (!w || w <= 0) return '—'
  if (w >= 1_000_000) {
    const m = w / 1_000_000
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`
  }
  return `${Math.round(w / 1000)}K`
}

// ── 登录弹窗与轮询 ──
const showLoginDlg = ref(false)
let pollTimer: number | undefined

// 添加账号三 tab（对齐 cockpit-tools：OAuth 授权 / Token·JSON 粘贴 / 本地导入）
type AddTab = 'oauth' | 'token' | 'local'
const addTab = ref<AddTab>('oauth')
const importText = ref('')
const importing = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

watch(addTab, () => {
  // 切 tab 清空输入（对齐 cockpit resetAddModalState）
  importText.value = ''
})

function openLoginDlg() {
  showLoginDlg.value = true
  addTab.value = 'oauth' // 打开即 OAuth 授权页（默认主路径）
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
        message.success(`${displayName.value} 账号添加成功`)
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

// ── 导入动作（Token/JSON 粘贴、本机客户端、JSON 文件）──
async function onImportText() {
  if (!importText.value.trim() || importing.value) return
  importing.value = true
  try {
    const n = await wb.importText(importText.value)
    showLoginDlg.value = false
    message.success(`成功导入 ${n} 个账号`)
  } catch (e) {
    message.error(String(e))
  } finally {
    importing.value = false
  }
}

async function onImportLocal() {
  if (importing.value) return
  importing.value = true
  try {
    await wb.importLocal()
    showLoginDlg.value = false
    message.success('成功导入本机 WorkBuddy 登录账号')
  } catch (e) {
    message.error(String(e))
  } finally {
    importing.value = false
  }
}

function pickJsonFile() {
  fileInput.value?.click()
}

async function onJsonFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许重复选择同一文件
  if (!file || importing.value) return
  importing.value = true
  try {
    const n = await wb.importText(await file.text())
    showLoginDlg.value = false
    message.success(`成功导入 ${n} 个账号`)
  } catch (e) {
    message.error(String(e))
  } finally {
    importing.value = false
  }
}

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

// ── IDE 账号切换（用户拍板 2026-10-11：客户端运行中自动关闭→写入→重启）──
const switchingUid = ref('')
/** 当前官方客户端登录的账号 uid（读登录态文件 account.uid，明文无需密钥）。 */
const ideUid = computed(() => wb.ideState?.current_uid ?? '')

async function onSwitchIde(a: WbAccount) {
  switchingUid.value = a.uid
  try {
    await wb.switchIde(a.uid)
    message.success(`官方客户端已切换登录：${a.nickname || a.uid}`)
  } catch (e) {
    message.error(String(e))
  } finally {
    switchingUid.value = ''
  }
}

// ── 账号导出（WorkBuddy账号导出：cockpit 兼容 / 9router 读取合并流）──
// 弹窗列出全部账号供勾选（默认全选）；导出走系统「另存为」选择保存位置。
const showExportDlg = ref(false)
const exportUids = ref<string[]>([])
const exportFormat = ref<'cockpit' | '9router'>('cockpit')
const exporting = ref(false)
const exportNote = ref('')

function openExportDlg() {
  if (wb.accounts.length === 0) {
    message.warning('暂无可导出的账号')
    return
  }
  exportUids.value = wb.accounts.map(a => a.uid) // 每次打开默认全选
  exportNote.value = ''
  showExportDlg.value = true
}

function toggleExportUid(uid: string) {
  const idx = exportUids.value.indexOf(uid)
  if (idx >= 0) exportUids.value.splice(idx, 1)
  else exportUids.value.push(uid)
}

function toggleAllExport() {
  exportUids.value = exportUids.value.length === wb.accounts.length ? [] : wb.accounts.map(a => a.uid)
}

async function onExport() {
  if (exportUids.value.length === 0) {
    message.warning('请先勾选要导出的账号')
    return
  }
  exporting.value = true
  try {
    const res = await wb.exportAccounts(exportUids.value, exportFormat.value)
    if (res.mode === 'fragment') {
      // 9router 读不到库 → 复制片段（用户手动合并进备份）
      await navigator.clipboard.writeText(res.content)
      message.success('未检测到 9router，已复制条目片段（请手动合并进备份）')
    } else {
      // 系统「另存为」对话框选择保存位置（用户取消则什么都不做）
      const out = await saveTextFile(res.filename, res.content)
      if (!out.saved) return
      message.success(`已保存到 ${out.path}`)
    }
    exportNote.value = res.note
    // cockpit 无关键提示直接关；9router 保留弹窗展示 note（恢复语义/手动合并指引）
    if (exportFormat.value === 'cockpit') showExportDlg.value = false
  } catch (e) {
    message.error(String(e))
  } finally {
    exporting.value = false
  }
}

onMounted(() => {
  void wb.load().catch(e => message.error(String(e)))
  void wb.loadIdeState()
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
        <span class="note">设备授权登录加入账号池；「切到此号」把该账号写入本机官方客户端并自动重启</span>
        <div class="spacer" />
        <NButton
          size="small"
          type="primary"
          @click="openLoginDlg"
        >
          添加账号
        </NButton>
        <NButton
          size="small"
          :disabled="wb.accounts.length === 0"
          @click="openExportDlg"
        >
          导出{{ exportUids.length ? `（${exportUids.length}）` : '' }}
        </NButton>
        <NButton
          size="small"
          @click="showVisibilityDlg = true"
        >
          模型显示
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
              v-if="supportsCheckin"
              size="small"
              :type="a.checkin.today_checked_in ? 'success' : 'warning'"
              :bordered="false"
            >
              {{ a.checkin.today_checked_in ? '今日已签' : '今日未签' }}
            </NTag>
            <NTag
              v-if="ideUid === a.uid"
              size="small"
              type="info"
              :bordered="false"
            >
              IDE 在用
            </NTag>
            <span class="spacer" />
            <div class="acct-ops">
              <NPopconfirm @positive-click="onSwitchIde(a)">
                <template #trigger>
                  <NButton
                    size="tiny"
                    type="primary"
                    ghost
                    :loading="switchingUid === a.uid"
                    :disabled="ideUid === a.uid || switchingUid !== '' || wb.busyUid !== ''"
                  >
                    {{ ideUid === a.uid ? 'IDE 登录中' : '切到此号' }}
                  </NButton>
                </template>
                将关闭并重启本机 {{ displayName }} 客户端，登录切换为「{{ a.nickname || a.uid }}」。确定切换？
              </NPopconfirm>
              <NButton
                v-if="supportsCheckin"
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
                v-if="supportsCheckin"
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
            <span v-if="supportsCheckin">连签 {{ a.checkin.streak_days ?? 0 }} 天</span>
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
          <span class="note">{{ displayName }} 目录 · 无思考档位的模型默认隐藏，可在账号区「模型显示」中恢复</span>
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
        :description="`暂无 ${displayName} 模型目录（请先添加账号；relay 会从上游拉取目录）`"
        size="small"
      />
      <NEmpty
        v-else-if="visibleModels.length === 0"
        description="全部模型已隐藏——点账号区「模型显示」恢复"
        size="small"
      />
      <table
        v-else
        class="tbl"
      >
        <thead>
          <tr>
            <th>模型名</th>
            <th class="col-ctx">上下文</th>
            <th class="col-effort">思考强度</th>
            <th class="col-status">状态（点击测试）</th>
            <th class="col-toggle">启用</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="m in filteredModels"
            :key="m.id"
          >
            <td
              class="mono copyable"
              title="点击复制模型名"
              @click="onCopy(m.id)"
            >
              {{ m.id }}
            </td>
            <td class="mono dim col-ctx">
              {{ fmtContext(m.context_window) }}
            </td>
            <td class="col-effort">
              <ModelEffortSelect :model="m" />
            </td>
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
              <label class="switch">
                <input
                  type="checkbox"
                  :checked="isModelEnabled(m.id)"
                  @change="onToggleModel(m.id)"
                />
                <span class="slider" />
              </label>
            </td>
          </tr>
        </tbody>
      </table>
    </NCard>

    <!-- 添加账号弹窗：三 tab（OAuth 授权 / Token·JSON 粘贴 / 本地导入，对齐 cockpit-tools） -->
    <NModal
      v-model:show="showLoginDlg"
      preset="card"
      :title="`添加 ${displayName} 账号`"
      :style="{ width: '480px' }"
      :mask-closable="false"
    >
      <NTabs
        v-model:value="addTab"
        type="segment"
        size="small"
      >
        <!-- Tab 1：OAuth 授权（设备授权 + 轮询等待，原有流程原样迁入） -->
        <NTabPane
          name="oauth"
          tab="OAuth 授权"
        >
          <template v-if="wb.loginPhase === 'pending'">
            <p class="dlg-text">点击下方按钮将在浏览器中打开 {{ displayName }} 授权页面。</p>
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
            <p
              v-if="isGlobal"
              class="dlg-note"
            >
              提示：授权完成后浏览器可能自动唤起「WorkBuddy CN」客户端——这是上游页面的协议回跳 （<code>workbuddy://</code> 被 CN 客户端注册，AI 页面回跳未区分）， <strong>不影响本次登录</strong>，关掉即可。
            </p>
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
        </NTabPane>

        <!-- Tab 2：Token / JSON 粘贴导入 -->
        <NTabPane
          name="token"
          tab="Token / JSON"
        >
          <p class="dlg-text imp-hint">粘贴 {{ displayName }} 的 access token：</p>
          <NInput
            v-model:value="importText"
            type="textarea"
            :rows="5"
            placeholder='示例：ghu_xxx / sk-ws-xxx / {"access_token":"eyJ...","refresh_token":"rt_..."} / [{...}]'
          />
          <NButton
            class="imp-btn"
            type="primary"
            size="large"
            block
            :loading="importing"
            :disabled="!importText.trim()"
            @click="onImportText"
          >
            导入
          </NButton>
          <p class="dlg-expire">JSON 支持单对象 / 数组 / accounts 包裹；纯 token 将联网校验并补全资料</p>
        </NTabPane>

        <!-- Tab 3：本地导入（本机客户端登录态 / JSON 文件；双域） -->
        <NTabPane
          name="local"
          tab="本地导入"
        >
          <p class="dlg-text imp-hint">支持从本机 {{ displayName }} 客户端或 JSON 文件导入账号数据。</p>
          <NButton
            size="large"
            block
            :loading="importing"
            @click="onImportLocal"
          >
            从本机 {{ displayName }} 导入
          </NButton>
          <p class="dlg-or">或从 JSON 文件导入</p>
          <NButton
            type="primary"
            size="large"
            block
            :loading="importing"
            @click="pickJsonFile"
          >
            选择 JSON 文件导入
          </NButton>
          <input
            ref="fileInput"
            type="file"
            accept="application/json"
            class="file-hidden"
            @change="onJsonFile"
          />
        </NTabPane>
      </NTabs>
      <template #footer>
        <div class="dlg-actions">
          <template v-if="addTab === 'oauth'">
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
          </template>
          <NButton
            v-else
            @click="showLoginDlg = false"
          >
            关闭
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

    <!-- 导出弹窗（WorkBuddy账号导出）：账号勾选列表 + 格式二选；
         cockpit → 另存为兼容文件；9router → 读到库另存为完整备份（读不到复制片段）。 -->
    <NModal
      v-model:show="showExportDlg"
      preset="card"
      title="导出 WorkBuddy 账号"
      :style="{ width: '520px' }"
      :mask-closable="false"
    >
      <div class="export-list-head">
        <NCheckbox
          size="small"
          :checked="exportUids.length === wb.accounts.length && wb.accounts.length > 0"
          :indeterminate="exportUids.length > 0 && exportUids.length < wb.accounts.length"
          @update:checked="toggleAllExport"
        >
          全选（已选 {{ exportUids.length }} / {{ wb.accounts.length }}）
        </NCheckbox>
      </div>
      <div class="export-list">
        <label
          v-for="a in wb.accounts"
          :key="a.uid"
          class="export-row"
        >
          <NCheckbox
            size="small"
            :checked="exportUids.includes(a.uid)"
            @update:checked="toggleExportUid(a.uid)"
          />
          <span class="export-row-name">{{ a.nickname || a.uid }}</span>
          <span class="export-row-uid mono">{{ a.uid }}</span>
        </label>
      </div>
      <NRadioGroup
        v-model:value="exportFormat"
        class="export-formats"
      >
        <div class="export-opt">
          <NRadio value="cockpit"> cockpit-tools 兼容（备份 / 互导） </NRadio>
          <p class="export-desc"> 导出 workbuddy_accounts 数组文件：可用于本应用备份迁移，或直接在 cockpit-tools 导入（同 uid 覆盖）。 </p>
        </div>
        <div class="export-opt">
          <NRadio value="9router"> 9router 完整备份（读取本机配置合并） </NRadio>
          <p class="export-desc"> 只读本机 9router 配置并把选中账号合并进去，产出完整可恢复备份（其余连接与 API key / 设置原样保留）。 未检测到 9router 时降级为条目片段（复制到剪贴板，手动合并进备份）。 </p>
        </div>
      </NRadioGroup>
      <p
        v-if="exportNote"
        class="export-note"
      >
        {{ exportNote }}
      </p>
      <template #footer>
        <div class="dlg-actions">
          <NButton
            size="small"
            :disabled="exporting"
            @click="showExportDlg = false"
          >
            关闭
          </NButton>
          <NButton
            size="small"
            type="primary"
            :loading="exporting"
            :disabled="exportUids.length === 0"
            @click="onExport"
          >
            导出{{ exportUids.length ? `（${exportUids.length}）` : '' }}
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="showVisibilityDlg"
      preset="card"
      title="模型显示"
      :style="{ width: '640px' }"
    >
      <p class="dlg-text vis-hint"> 无思考档位的模型默认隐藏。开关同时控制「显示」与「启用」：关 = 隐藏且停用（agent 侧不可见且调用 404）；开 = 显示且启用。需「可见但停用」请用主列表的「启用」开关。即时保存。 </p>
      <div class="vis-toolbar">
        <NInput
          v-model:value="visibilitySearch"
          size="small"
          clearable
          placeholder="搜索模型名"
        />
        <NButton
          size="small"
          :disabled="hiddenCount === 0"
          @click="onShowAllHidden"
        >
          全部显示（{{ hiddenCount }}）
        </NButton>
        <NPopconfirm @positive-click="onResetVisibility">
          <template #trigger>
            <NButton size="small"> 恢复默认 </NButton>
          </template>
          清空全部显示覆盖？无思考档位的模型将恢复默认隐藏（隐藏即停用），启用状态不变。
        </NPopconfirm>
      </div>
      <NEmpty
        v-if="visibilityList.length === 0"
        description="暂无 WorkBuddy 模型目录（请先添加账号）"
        size="small"
      />
      <div
        v-else
        class="vis-list"
      >
        <div
          v-for="m in visibilityList"
          :key="m.id"
          class="vis-row"
        >
          <span
            class="mono copyable vis-name"
            title="点击复制模型名"
            @click="onCopy(m.id)"
            >{{ m.id }}</span
          >
          <span
            class="vis-effort"
            :class="{ dim: !m.reasoning_efforts?.length }"
            :title="m.reasoning_efforts?.length ? '思考档位' : '无档位（默认隐藏的判定来源）'"
            >{{ effortText(m) }}</span
          >
          <span class="vis-switch-label">显示</span>
          <NSwitch
            size="small"
            :value="m.visible !== false"
            @update:value="(v: boolean) => onToggleShow(m, v)"
          />
        </div>
      </div>
      <template #footer>
        <div class="dlg-actions">
          <NButton
            size="small"
            @click="showVisibilityDlg = false"
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
.dim {
  color: var(--cp-dim);
}
.copyable {
  cursor: pointer;
  user-select: text;
  transition: color 0.15s;
}
.copyable:hover {
  color: var(--cp-cyan);
}
/* 启用滑杆（对齐牛码 tab：label.switch > input + span.slider） */
.switch {
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}
.slider {
  position: absolute;
  inset: 0;
  background: #334155;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s;
}
.slider::before {
  content: '';
  position: absolute;
  width: 14px;
  height: 14px;
  left: 3px;
  top: 3px;
  border-radius: 50%;
  background: #cbd5e1;
  transition: transform 0.15s;
}
.switch input:checked + .slider {
  background: var(--cp-cyan);
}
.switch input:checked + .slider::before {
  transform: translateX(16px);
  background: #04121a;
}
.col-status {
  width: 124px;
}
.col-toggle {
  width: 60px;
}
.col-ctx {
  width: 72px;
  white-space: nowrap;
}
.col-effort {
  width: 150px;
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
/* 国际版 OAuth 弹窗提示（workbuddy:// 回跳唤起 CN 客户端的知情说明） */
.dlg-note {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-dim);
  font-size: 12px;
  line-height: 1.7;
}
.dlg-note code {
  font-family: var(--cp-mono, monospace);
  color: var(--cp-text);
}
.dlg-note strong {
  color: var(--cp-text);
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
/* 导入 tab（Token/JSON 粘贴 + 本地导入） */
.imp-hint {
  margin-bottom: 10px;
}
.imp-btn {
  margin-top: 12px;
}
.dlg-or {
  margin: 10px 0;
  text-align: center;
  color: var(--cp-dim);
  font-size: 12px;
}
.file-hidden {
  display: none;
}
/* 导出弹窗（格式二选 + 说明行） */
.export-formats {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 10px;
}
/* 弹窗内账号勾选列表（滚动容器） */
.export-list-head {
  margin-bottom: 6px;
}
.export-list {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  margin-bottom: 4px;
}
.export-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
  font-size: 13px;
  cursor: pointer;
}
.export-row:last-child {
  border-bottom: none;
}
.export-row-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.export-row-uid {
  margin-left: auto;
  color: var(--cp-dim);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
}
.export-opt {
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  padding: 10px 12px;
}
.export-desc {
  margin: 6px 0 0 22px;
  color: var(--cp-dim);
  font-size: 12px;
  line-height: 1.7;
}
.export-note {
  margin: 10px 0 0;
  color: var(--cp-cyan);
  font-size: 12px;
  line-height: 1.7;
}
/* 模型显示弹窗（过时治理）：提示行 + 工具行 + 全量模型行内滚动列表 */
.vis-hint {
  color: var(--cp-dim);
  font-size: 12px;
  margin-bottom: 10px;
}
.vis-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.vis-list {
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
}
.vis-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
  font-size: 13px;
}
.vis-row:last-child {
  border-bottom: none;
}
.vis-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vis-effort {
  flex: none;
  width: 170px;
  font-size: 12px;
  color: var(--cp-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vis-switch-label {
  flex: none;
  color: var(--cp-dim);
  font-size: 12px;
}
</style>
