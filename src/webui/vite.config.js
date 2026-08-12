import { defineConfig } from 'vite';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Python 服务器为 /logo.png（随机轮换）与 /openlist_strm_bridge.png 提供别名
// （server.py 静态路由）。Vite dev/preview 缺少这两个别名，导致 `npx vite`
// 下 logo/壁纸 404。此插件补齐同一语义：/logo.png 从 public/assets/logo.*.png
// 随机轮换，/openlist_strm_bridge.png 指向真实壁纸文件。
function staticAliasPlugin() {
  const assetsDir = join(__dirname, 'public', 'assets');
  const logos = readdirSync(assetsDir)
    .filter(f => /^logo\.\d+\.png$/.test(f))
    .sort();
  const servePng = (res, file) => {
    res.statusCode = 200;
    res.setHeader('Content-Type', 'image/png');
    res.end(readFileSync(file));
  };
  const apply = (server) => {
    server.middlewares.use((req, res, next) => {
      const url = (req.url || '').split('?')[0];
      if (url === '/logo.png' && logos.length) {
        const pick = logos[Math.floor(Math.random() * logos.length)];
        servePng(res, join(assetsDir, pick));
      } else if (url === '/openlist_strm_bridge.png') {
        const wall = join(assetsDir, 'openlist_strm_bridge.png');
        if (existsSync(wall)) servePng(res, wall);
        else next();
      } else {
        next();
      }
    });
  };
  return {
    name: 'strm-static-aliases',
    configureServer: apply,
    configurePreviewServer: apply,
  };
}

export default defineConfig({
  root: '.',
  base: './',
  plugins: [staticAliasPlugin()],
  // publicDir：目录内文件原样复制到 outDir（不加哈希、不改名），
  // 提供稳定访问路径，供需要稳定 URL 的资源使用：
  // - 壁纸 openlist_strm_bridge.png（CSS 绝对路径 /assets/openlist_strm_bridge.png）
  // - logo.01~04.png（/logo.png 别名随机轮换，见上方插件）
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
