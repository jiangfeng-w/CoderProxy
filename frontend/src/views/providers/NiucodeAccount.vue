<script setup lang="ts">
// 牛码 tab 账号区：登录（IM 静默优先 / 浏览器 PKCE 兜底）、账号信息、退出（二次确认）、同步模型目录。
// 登录态由 Shell 全局轮询写入 store.auth（本组件只读展示）；登录动作自顶栏迁入（供应商页 F3）。
import { computed, ref } from 'vue'
import { NButton, NPopconfirm, useMessage } from 'naive-ui'
import { authLoginStart, authLogout, authSync, openAuthorizeUrl } from '../../api'
import { store } from '../../store'

const message = useMessage()
const busy = ref(false)

const loggedIn = computed(() => store.auth.status === 'logged_in')
const accountLabel = computed(() => store.auth.account?.label || store.auth.account?.id || '')

async function onLogin() {
  busy.value = true
  try {
    const res = await authLoginStart()
    if (res.status === 'pending' && res.authorize_url) {
      openAuthorizeUrl(res.authorize_url)
      message.info('已在浏览器打开授权页，登录完成后自动同步')
    } else if (res.status === 'logged_in') {
      message.success('已登录')
    } else {
      message.warning(res.error || '登录未完成')
    }
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function onLogout() {
  busy.value = true
  try {
    await authLogout()
    message.info('已登出')
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

/** 同步模型目录：成功后 bump syncTick，牛码模型区 watch 刷新列表。 */
async function onSync() {
  busy.value = true
  try {
    const res = await authSync()
    message.success(`同步完成：${res.models.length} 个模型`)
    store.syncTick++
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card acct">
    <div class="head">
      <span
        class="cp-tag"
        :class="loggedIn ? 'green' : store.auth.status === 'pending' ? 'cyan' : 'gray'"
      >
        {{ loggedIn ? '已登录' : store.auth.status === 'pending' ? '登录中' : '未登录' }}
      </span>
      <span
        v-if="loggedIn"
        class="user"
      >
        {{ accountLabel }}
      </span>
      <div class="spacer" />
      <NButton
        v-if="loggedIn"
        size="small"
        :disabled="busy"
        :loading="busy"
        @click="onSync"
      >
        同步模型目录
      </NButton>
      <NPopconfirm
        v-if="loggedIn"
        @positive-click="onLogout"
      >
        <template #trigger>
          <NButton
            size="small"
            type="error"
            ghost
            :disabled="busy"
          >
            退出登录
          </NButton>
        </template>
        退出将清除登录态与已同步的模型目录，并停止对 agent 的服务，确认退出？
      </NPopconfirm>
      <NButton
        v-else
        size="small"
        type="primary"
        :disabled="busy"
        :loading="busy && store.auth.status === 'pending'"
        @click="onLogin"
      >
        登录牛码
      </NButton>
    </div>
    <p
      v-if="store.auth.status === 'pending'"
      class="tip"
    >
      正在等待浏览器授权完成，完成后将自动同步模型目录；若浏览器未打开，请重新点击登录。
    </p>
    <p
      v-else-if="!loggedIn"
      class="tip"
    >
      登录优先尝试本机银海通 IM 静默完成（:13631 可达时秒登）；不可用时将打开浏览器授权页， 登录成功后自动同步模型目录并启动服务。
    </p>
    <p
      v-else-if="store.auth.error"
      class="tip"
    >
      {{ store.auth.error }}
    </p>
  </div>
</template>

<style scoped>
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
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.user {
  color: var(--cp-text);
  font-size: 13px;
  font-weight: 600;
}
.tip {
  margin: 10px 0 0;
  color: var(--cp-dim);
  font-size: 12px;
  line-height: 1.7;
}
.cp-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
}
.cp-tag.green {
  background: rgba(34, 197, 94, 0.2);
  color: var(--cp-green);
}
.cp-tag.cyan {
  background: rgba(34, 211, 238, 0.2);
  color: var(--cp-cyan);
}
.cp-tag.gray {
  background: rgba(100, 116, 139, 0.2);
  color: var(--cp-dim);
}
</style>
