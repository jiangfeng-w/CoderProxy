<script setup lang="ts">
// 根组件：只负责注入 Naive UI 主题与消息上下文，具体布局在 Shell.vue。
import {
  NConfigProvider, NMessageProvider, darkTheme, zhCN, dateZhCN,
  type GlobalThemeOverrides,
} from "naive-ui";
import Shell from "./Shell.vue";

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: "#22d3ee",
    primaryColorHover: "#67e8f9",
    primaryColorPressed: "#06b6d4",
    primaryColorSuppl: "#22d3ee",
    borderRadius: "8px",
  },
};
</script>

<template>
  <n-config-provider
    :theme="darkTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-message-provider>
      <shell />
    </n-message-provider>
  </n-config-provider>
</template>
