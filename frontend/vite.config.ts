import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_DEV_PROXY_TARGET ?? "http://127.0.0.1:8000";

  return {
    plugins: [
      vue(),
      AutoImport({ resolvers: [ElementPlusResolver()], dts: true }),
      Components({ resolvers: [ElementPlusResolver()], dts: true }),
    ],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      // Vite dev 默认开 compression 中间件，会把 SSE 帧攒满一个缓冲块才吐出，表现为"问数卡住"
      compress: false,
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
          ws: false,
          configure(proxy) {
            proxy.on("proxyRes", (proxyRes) => {
              const type = String(proxyRes.headers["content-type"] ?? "");
              if (type.includes("text/event-stream")) {
                proxyRes.headers["cache-control"] = "no-cache, no-transform";
                proxyRes.headers["x-accel-buffering"] = "no";
                delete proxyRes.headers["content-encoding"];
              }
            });
          },
        },
      },
    },
  };
});
