<script setup lang="ts">
// 供应商页（2026-10-07 定稿）：横向标签页分平台，每 tab 统一「账号区（上）+ 模型区（下）」。
// 原模型页（Models.vue）移除，功能整合进牛码 tab；WorkBuddy tab 本期占位；
// 自定义供应商 tab 本期仅配置管理（转发待聚合需求 adapter 层）。
import { NTabPane, NTabs } from 'naive-ui'
import NiucodeAccount from './providers/NiucodeAccount.vue'
import NiucodeModels from './providers/NiucodeModels.vue'
import WorkBuddyTab from './providers/WorkBuddyTab.vue'
import CustomProvidersTab from './providers/CustomProvidersTab.vue'
</script>

<template>
  <div class="providers-page">
    <NTabs
      type="line"
      animated
    >
      <NTabPane
        name="niucode"
        tab="牛码"
      >
        <div class="stack">
          <NiucodeAccount />
          <NiucodeModels />
        </div>
      </NTabPane>
      <NTabPane
        name="workbuddy"
        tab="WorkBuddy"
      >
        <WorkBuddyTab />
      </NTabPane>
      <NTabPane
        name="custom"
        tab="自定义供应商"
      >
        <CustomProvidersTab />
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.providers-page {
  padding-top: 4px;
}
.stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
