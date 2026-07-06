import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  build: {
    outDir: '../src/nh_ui/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/nh': 'http://127.0.0.1:8080',
    },
  },
});
