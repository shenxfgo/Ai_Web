import { createApp } from "vue";
import { createPinia } from "pinia";

// 只引 base（CSS 变量与 reset），组件样式交给 resolver 按需注入
import "element-plus/theme-chalk/base.css";
import "@/styles/tokens.css";
import "@/styles/base.css";

import App from "./App.vue";
import { router } from "./router";

createApp(App).use(createPinia()).use(router).mount("#app");
