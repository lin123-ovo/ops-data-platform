<script setup lang="ts">
import { computed, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import en from 'element-plus/es/locale/lang/en'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { language } from '@/utils/locale'

const route = useRoute()
const componentLocale = computed(() => language.value === 'en' ? en : zhCn)

watchEffect(() => {
  document.documentElement.lang = language.value === 'en' ? 'en' : 'zh-CN'
})

watchEffect(() => {
  const title = route.meta.title
  const pageTitle = Array.isArray(title) ? title[1] : undefined
  document.title = pageTitle ? `${pageTitle} | Ops Data Platform` : 'Ops Data Platform'
})
</script>

<template>
  <el-config-provider :locale="componentLocale">
    <RouterView />
  </el-config-provider>
</template>
