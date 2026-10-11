<script setup lang="ts">
// 自定义供应商 tab（供应商页 F5 + 聚合 §4.4）：
// 账号区=供应商条目（名称/baseurl/apikey/启停，增删改，参考 cc-switch）；模型区=选中条目的模型清单（手填）。
// 请求转发已生效：agent 以「供应商名/模型名」调用即经 OpenAI 兼容协议转发至 base_url。
// 「模型显示」弹窗（对齐 WorkBuddy tab）：全部自定义模型逐条显示开关（显示+启用双写）；
// 自定义默认恒可见（无「无档位默认隐藏」规则），隐藏即停用；恢复默认只清自定义前缀覆盖。
// api_key 永不回显（响应只有 has_api_key）；编辑框留空 = 保留原 key。
import { computed, onMounted, ref } from 'vue'
import { NButton, NEmpty, NInput, NModal, NPopconfirm, NSwitch, NTag, useMessage } from 'naive-ui'
import { createCustomProvider, deleteCustomProvider, getModels, listCustomProviders, updateCustomProvider, type CustomProvider, type OaiModel } from '../../api'
import { useConfigStore } from '../../store'

const message = useMessage()
const config = useConfigStore()
const DISABLE_ALL = '__none__'

const providers = ref<CustomProvider[]>([])
const selectedId = ref('')
const loading = ref(false)

// 编辑弹窗：editing=null 新建，否则为编辑目标
const showModal = ref(false)
const editing = ref<CustomProvider | null>(null)
const saving = ref(false)
const form = ref({ name: '', base_url: '', api_key: '', enabled: true })

// 模型区添加输入
const newModel = ref('')

const selected = () => providers.value.find(p => p.id === selectedId.value)

async function load(keepSelection = true) {
  loading.value = true
  try {
    const res = await listCustomProviders()
    providers.value = res.providers
    if (!keepSelection || !res.providers.some(p => p.id === selectedId.value)) {
      selectedId.value = res.providers[0]?.id ?? ''
    }
  } catch (e) {
    message.error(String(e))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = { name: '', base_url: '', api_key: '', enabled: true }
  showModal.value = true
}

function openEdit(p: CustomProvider) {
  editing.value = p
  // api_key 不回显：编辑框恒为空，留空提交 = 保留原 key
  form.value = { name: p.name, base_url: p.base_url, api_key: '', enabled: p.enabled }
  showModal.value = true
}

async function onSave() {
  if (!form.value.name.trim()) {
    message.warning('请填写名称')
    return
  }
  if (!/^https?:\/\//.test(form.value.base_url.trim())) {
    message.warning('base_url 需以 http:// 或 https:// 开头')
    return
  }
  saving.value = true
  try {
    // api_key 仅在填写了非空值时提交（新建可为空；编辑留空 = 保留原 key）
    const body: Record<string, unknown> = {
      name: form.value.name.trim(),
      base_url: form.value.base_url.trim(),
      enabled: form.value.enabled
    }
    if (form.value.api_key.trim()) body.api_key = form.value.api_key.trim()
    if (editing.value) {
      await updateCustomProvider(editing.value.id, body)
      message.success('已保存')
    } else {
      const res = await createCustomProvider(body)
      selectedId.value = res.provider.id
      message.success('已添加')
    }
    showModal.value = false
    await load()
  } catch (e) {
    message.error(String(e))
  } finally {
    saving.value = false
  }
}

async function onDelete(p: CustomProvider) {
  try {
    await deleteCustomProvider(p.id)
    message.info('已删除')
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

async function onToggle(p: CustomProvider, enabled: boolean) {
  try {
    await updateCustomProvider(p.id, { enabled })
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

async function addModel() {
  const p = selected()
  const name = newModel.value.trim()
  if (!p || !name) return
  if (p.models.includes(name)) {
    message.warning('模型已存在')
    return
  }
  try {
    await updateCustomProvider(p.id, { models: [...p.models, name] })
    newModel.value = ''
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

async function removeModel(p: CustomProvider, model: string) {
  try {
    await updateCustomProvider(p.id, { models: p.models.filter(m => m !== model) })
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

// ── 「模型显示」弹窗（对齐 WorkBuddy tab；自定义默认恒可见，无「无档位默认隐藏」规则） ──
const showVisDlg = ref(false)
const visSearch = ref('')
const customModels = ref<OaiModel[]>([])
const visLoading = ref(false)

/** 目录中属于自定义供应商的条目（前缀=供应商名；/v1/models 仅聚合 enabled 条目）。 */
async function refreshVisModels() {
  const all = (await getModels(true)).data
  customModels.value = all.filter(m => providers.value.some(p => m.id.startsWith(`${p.name}/`)))
}

async function openVisDlg() {
  try {
    visLoading.value = true
    await load() // 供应商名是前缀判定来源，先取最新清单
    await refreshVisModels()
    if (!config.loaded) await config.load()
    showVisDlg.value = true
  } catch (e) {
    message.error(String(e))
  } finally {
    visLoading.value = false
  }
}

const visibilityList = computed(() => {
  const q = visSearch.value.trim().toLowerCase()
  return q ? customModels.value.filter(m => m.id.toLowerCase().includes(q)) : customModels.value
})

const hiddenCount = computed(() => customModels.value.filter(m => m.visible === false).length)

/** 白名单三态加入（空=全启用 / __none__ 哨兵 / 显式名单）；null = 无需变更。 */
function wlAdd(id: string): string[] | null {
  const w = config.model_whitelist
  if (w.length === 0) return null
  if (w.length === 1 && w[0] === DISABLE_ALL) return [id]
  return w.includes(id) ? null : [...w, id]
}

/** 白名单三态移除；null = 无需变更。 */
async function wlRemove(id: string): Promise<string[] | null> {
  const w = config.model_whitelist
  if (w.length === 0) {
    // 全部启用 → 显式列出除该模型外的全部（含牛码/WorkBuddy 条目，保证其它供应商语义不变）
    const all = (await getModels(true)).data.map(m => m.id)
    const next = all.filter(x => x !== id)
    return next.length ? next : [DISABLE_ALL]
  }
  if (w.length === 1 && w[0] === DISABLE_ALL) return null
  const next = w.filter(x => x !== id)
  return next.length ? next : [DISABLE_ALL] // 空名单语义 = 全部启用，落哨兵防反转
}

/** 弹窗「显示」开关（对齐 WorkBuddy 拍板简化）：同时操控显示与启用——
 * 关 ⇒ 隐藏+停用成对写入（chat 404）；开 ⇒ 显示+启用双开。 */
async function onToggleShow(m: OaiModel, show: boolean) {
  try {
    const patch: Record<string, unknown> = {
      model_visibility: { ...config.model_visibility, [m.id]: show ? 'show' : 'hide' }
    }
    const next = show ? wlAdd(m.id) : await wlRemove(m.id)
    if (next) patch.model_whitelist = next
    await config.update(patch)
    await refreshVisModels() // 重拉 all=1 刷新 visible（口径由 relay 单点计算，前端不复刻）
  } catch (e) {
    message.error(String(e))
  }
}

/** 「全部显示」：当前隐藏项全部双开（显示 + 启用）。 */
async function onShowAllHidden() {
  try {
    const hidden = customModels.value.filter(m => m.visible === false)
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
    await refreshVisModels()
  } catch (e) {
    message.error(String(e))
  }
}

/** 「恢复默认」（与 WorkBuddy 的差异点）：默认判定=自定义恒可见，故只清**自定义前缀**
 * 的显示覆盖，不动牛码/WorkBuddy 的覆盖；白名单不动。 */
async function onResetVisibility() {
  try {
    const vis = { ...config.model_visibility }
    const ids = new Set(customModels.value.map(m => m.id))
    for (const k of Object.keys(vis)) {
      if (ids.has(k)) delete vis[k]
    }
    await config.update({ model_visibility: vis })
    await refreshVisModels()
    message.info('已恢复默认（自定义模型重新全部显示）')
  } catch (e) {
    message.error(String(e))
  }
}

/** 点击模型名即复制到剪贴板（对齐 WorkBuddy/牛码 tab）。 */
async function onCopy(id: string) {
  try {
    await navigator.clipboard.writeText(id)
    message.success(`已复制模型名：${id}`)
  } catch (e) {
    message.error(`复制失败：${String(e)}`)
  }
}

onMounted(() => load(false))
</script>

<template>
  <div class="stack">
    <!-- 账号区：供应商条目 -->
    <div class="card">
      <div class="head">
        <span class="zone-title">账号 · 供应商条目</span>
        <span class="note">OpenAI 兼容转发已生效：agent 以「供应商名/模型名」调用即转发至 base_url</span>
        <div class="spacer" />
        <NButton
          size="small"
          type="primary"
          @click="openCreate"
        >
          添加供应商
        </NButton>
        <NButton
          size="small"
          :loading="visLoading"
          @click="openVisDlg"
        >
          模型显示
        </NButton>
      </div>

      <div
        v-if="providers.length === 0 && !loading"
        class="empty"
      >
        暂无自定义供应商，点击「添加供应商」创建（baseurl + apikey）
      </div>
      <div
        v-for="p in providers"
        :key="p.id"
        class="entry"
        :class="{ active: p.id === selectedId }"
        @click="selectedId = p.id"
      >
        <div class="entry-main">
          <div class="entry-name">
            {{ p.name }}
            <NTag
              size="small"
              :type="p.has_api_key ? 'success' : 'default'"
              :bordered="false"
            >
              {{ p.has_api_key ? '已配置 Key' : '无 Key' }}
            </NTag>
          </div>
          <div class="entry-url mono"> {{ p.base_url }} · {{ p.models.length }} 个模型 </div>
        </div>
        <div
          class="entry-ops"
          @click.stop
        >
          <NSwitch
            size="small"
            :value="p.enabled"
            @update:value="(v: boolean) => onToggle(p, v)"
          />
          <NButton
            size="tiny"
            @click="openEdit(p)"
          >
            编辑
          </NButton>
          <NPopconfirm @positive-click="onDelete(p)">
            <template #trigger>
              <NButton
                size="tiny"
                type="error"
                ghost
              >
                删除
              </NButton>
            </template>
            删除供应商「{{ p.name }}」？其模型清单一并删除。
          </NPopconfirm>
        </div>
      </div>
    </div>

    <!-- 模型区：选中供应商的模型清单（手填） -->
    <div class="card">
      <div class="head">
        <span class="zone-title">模型</span>
        <span
          v-if="selected()"
          class="note"
          >{{ selected()!.name }} 的模型清单（手填）</span
        >
      </div>
      <NEmpty
        v-if="!selected()"
        description="先在上方选择或添加一个供应商，再维护其模型清单"
        size="small"
      />
      <template v-else>
        <div class="model-add">
          <NInput
            v-model:value="newModel"
            size="small"
            placeholder="模型名（回车添加）"
            @keyup.enter="addModel"
          />
          <NButton
            size="small"
            :disabled="!newModel.trim()"
            @click="addModel"
          >
            添加
          </NButton>
        </div>
        <div
          v-if="selected()!.models.length === 0"
          class="empty"
        >
          暂无模型，在上方输入模型名添加
        </div>
        <div
          v-else
          class="model-tags"
        >
          <NTag
            v-for="m in selected()!.models"
            :key="m"
            size="small"
            closable
            class="mono"
            @close="removeModel(selected()!, m)"
          >
            {{ m }}
          </NTag>
        </div>
      </template>
    </div>

    <!-- 新建/编辑弹窗 -->
    <NModal
      v-model:show="showModal"
      preset="card"
      :title="editing ? `编辑供应商：${editing.name}` : '添加供应商'"
      :style="{ width: '460px' }"
      :mask-closable="false"
    >
      <div class="form">
        <label class="fld">
          <span class="fld-label">名称</span>
          <NInput
            v-model:value="form.name"
            placeholder="如：我的中转"
          />
        </label>
        <label class="fld">
          <span class="fld-label">Base URL</span>
          <NInput
            v-model:value="form.base_url"
            placeholder="https://api.example.com/v1"
          />
        </label>
        <label class="fld">
          <span class="fld-label">API Key</span>
          <NInput
            v-model:value="form.api_key"
            type="password"
            show-password-on="click"
            :placeholder="editing ? '留空保持原 Key 不变' : '可留空（部分本地服务无需 Key）'"
          />
        </label>
        <div class="fld">
          <span class="fld-label">启用</span>
          <NSwitch v-model:value="form.enabled" />
        </div>
        <p class="form-note"> 保存即生效：agent 以「供应商名/模型名」调用，请求经 OpenAI 兼容协议转发至该 Base URL。 </p>
      </div>
      <template #footer>
        <div class="dlg-actions">
          <NButton @click="showModal = false">取消</NButton>
          <NButton
            type="primary"
            :loading="saving"
            @click="onSave"
          >
            保存
          </NButton>
        </div>
      </template>
    </NModal>

    <!-- 模型显示弹窗（对齐 WorkBuddy tab）：列出全部自定义模型（含隐藏）逐条开关；
         单一「显示」开关同时驱动显示+启用（隐藏 ⇒ 双关，chat 404）。 -->
    <NModal
      v-model:show="showVisDlg"
      preset="card"
      title="模型显示"
      :style="{ width: '640px' }"
    >
      <p class="dlg-text vis-hint"> 自定义模型默认全部显示。开关同时控制「显示」与「启用」：关 = 隐藏且停用（agent 侧不可见且调用 404）；开 = 显示且启用。即时保存。 </p>
      <div class="vis-toolbar">
        <NInput
          v-model:value="visSearch"
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
          清空自定义模型的显示覆盖？自定义模型将恢复默认全部显示，启用状态不变；WorkBuddy/牛码的显示设置不受影响。
        </NPopconfirm>
      </div>
      <NEmpty
        v-if="visibilityList.length === 0"
        description="暂无自定义供应商模型——请先添加供应商并在其模型清单里维护模型"
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
            @click="showVisDlg = false"
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
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
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
.empty {
  color: var(--cp-dim);
  padding: 18px 0;
  text-align: center;
  font-size: 13px;
}
.entry {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.entry:hover {
  border-color: var(--cp-dim);
}
.entry.active {
  border-color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.06);
}
.entry-main {
  flex: 1;
  min-width: 0;
}
.entry-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}
.entry-url {
  margin-top: 3px;
  color: var(--cp-dim);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.entry-ops {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.mono {
  font-family: var(--cp-mono, monospace);
}
.model-add {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  max-width: 420px;
}
.model-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.fld {
  display: flex;
  align-items: center;
  gap: 10px;
}
.fld-label {
  width: 72px;
  flex: none;
  color: var(--cp-dim);
  font-size: 13px;
}
.form-note {
  margin: 0;
  color: var(--cp-dim);
  font-size: 12px;
  line-height: 1.6;
}
.dlg-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.dlg-text {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.7;
}
.copyable {
  cursor: pointer;
  user-select: text;
  transition: color 0.15s;
}
.copyable:hover {
  color: var(--cp-cyan);
}
/* 模型显示弹窗（对齐 WorkBuddy tab）：提示行 + 工具行 + 全量模型行内滚动列表 */
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
.vis-switch-label {
  flex: none;
  color: var(--cp-dim);
  font-size: 12px;
}
</style>
