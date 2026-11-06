import 'element-plus/dist/index.css'
import './assets/main.css'

import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

// 前端阅读入口：Vue 创建页面，Pinia 保存共享状态，router 决定 URL 对应哪个视图。
// 页面不直接调用大模型；模型调用统一发生在 backend/app 中。
const app = createApp(App)

app.use(ElementPlus)
app.use(createPinia())
app.use(router)
app.mount('#app')
