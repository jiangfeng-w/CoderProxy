import { createApp } from 'vue'
import App from './App.vue'
import { pinia } from './pinia'
import './style.css'

createApp(App).use(pinia).mount('#app')
