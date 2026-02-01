import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    strictPort: true,
    // If you serve Trader/DataHub on localhost with different ports, you'll
    // want these proxies to avoid CORS during dev.
    proxy: {
      '/trader': {
        target: 'http://127.0.0.1:7002',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/trader/, ''),
      },
      '/datahub': {
        target: 'http://127.0.0.1:7001',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/datahub/, ''),
      },
    },
  },
});
