import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    target: 'es2017',
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: undefined,
        // 文件名带内容 hash：静态资源发 immutable 强缓存，内容一变网址就变
        entryFileNames: 'assets/app.[hash].js',
        chunkFileNames: 'assets/[name].[hash].js',
        assetFileNames: 'assets/[name].[hash][extname]'
      }
    }
  }
})
