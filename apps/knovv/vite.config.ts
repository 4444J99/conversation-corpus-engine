import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { cloudflare } from "@cloudflare/vite-plugin";
const target = process.env.KNOVV_DEV_API;
export default defineConfig({
  plugins: [
    react(),
    ...(process.env.KNOVV_CF_BUILD === "1" ? [cloudflare()] : []),
  ],
  build: { outDir: "dist" },
  server: {
    proxy: target
      ? { "/api": { target, changeOrigin: true, headers: { origin: target } } }
      : undefined,
  },
  test: { maxWorkers: 1 },
});
