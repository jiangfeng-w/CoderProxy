<script setup lang="ts">
// 自定义供应商 tab（供应商页 F5，本期仅配置管理，转发待聚合需求 adapter 层）：
// 账号区=供应商条目（名称/baseurl/apikey/启停，增删改，参考 cc-switch）；模型区=选中条目的模型清单（手填）。
// api_key 永不回显（响应只有 has_api_key）；编辑框留空 = 保留原 key。
import { onMounted, ref } from 'vue'
import { NButton, NEmpty, NInput, NModal, NPopconfirm, NSwitch, NTag, useMessage } from 'naive-ui'
import { createCustomProvider, deleteCustomProvider, listCustomProviders, updateCustomProvider, type CustomProvider } from '../../api'

const message = useMessage()

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

onMounted(() => load(false))
</script>

<template>
  <div class="stack">
    <!-- 账号区：供应商条目 -->
    <div class="card">
      <div class="head">
        <span class="zone-title">账号 · 供应商条目</span>
        <span class="note">本期仅配置管理，请求转发将在多平台聚合需求落地后生效</span>
        <div class="spacer" />
        <NButton
          size="small"
          type="primary"
          @click="openCreate"
        >
          添加供应商
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
        <p class="form-note"> 本期仅保存配置；该供应商的请求转发将在「多平台聚合」adapter 层落地后生效。 </p>
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
</style>
