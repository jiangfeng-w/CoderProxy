<script setup lang="ts">
// WorkBuddy tab（本期占位）：账号区/模型区均为空态——多账号管理由「WorkBuddy多账号登录
// 与签到」实现后填充组件（挂接点即本 tab）；模型转发由「WorkBuddy聊天反代与多平台聚合」提供。
import { NCard, NEmpty } from 'naive-ui'
</script>

<template>
  <div class="stack">
    <NCard
      size="small"
      :bordered="true"
      class="zone-card"
    >
      <template #header>
        <span class="zone-title">账号</span>
      </template>
      <NEmpty description="WorkBuddy 多账号登录/签到/配额开发中（对接 /v1/platforms/workbuddy/*）" />
    </NCard>
    <NCard
      size="small"
      :bordered="true"
      class="zone-card"
    >
      <template #header>
        <span class="zone-title">模型</span>
      </template>
      <NEmpty description="WorkBuddy 模型转发由「WorkBuddy聊天反代与多平台聚合」需求提供，本页暂为占位" />
    </NCard>
  </div>
</template>

<style scoped>
.stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.zone-card {
  background: var(--cp-panel);
  border-radius: 10px;
}
.zone-title {
  font-size: 14px;
  font-weight: 600;
}
</style>
