import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/upload': 'http://127.0.0.1:7860',
      '/stream_task': 'http://127.0.0.1:7860',
      '/task_status': 'http://127.0.0.1:7860',
      '/task_media_info': 'http://127.0.0.1:7860',
      '/task_segment_results': 'http://127.0.0.1:7860',
      '/task': 'http://127.0.0.1:7860',
      '/files': 'http://127.0.0.1:7860',
      '/media': 'http://127.0.0.1:7860',
      '/segments': 'http://127.0.0.1:7860',
      '/cache': 'http://127.0.0.1:7860',
      '/avatars': 'http://127.0.0.1:7860',
      '/api/auth': 'http://127.0.0.1:7860',
      '/api/me': 'http://127.0.0.1:7860',
      '/api/click_log': 'http://127.0.0.1:7860',
    },
  },
})
