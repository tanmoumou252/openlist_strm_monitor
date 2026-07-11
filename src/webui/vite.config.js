import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: './',
  // publicDir：目录内文件原样复制到 outDir（不加哈希、不改名），
  // 提供稳定访问路径，供需要稳定 URL 的资源使用：
  // - 壁纸 openlist_strm_bridge.png（CSS 绝对路径 /openlist_strm_bridge.png）
  // - logo.01~04.png（/logo.png 路由随机轮换）
  // - favicon.ico（浏览器自动请求 /favicon.ico 兜底）
  // 字体 woff2 由 main.css 相对路径 import，Vite 正确打包为哈希名，无需放入 publicDir。
  publicDir: 'public',
  build: {
    outDir: '../../dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          // Group core modules into a single "core" chunk
          if (id.includes('/modules/core/') || id.includes('/modules/components/')) {
            return 'core';
          }
        },
      },
    },
  },
});
