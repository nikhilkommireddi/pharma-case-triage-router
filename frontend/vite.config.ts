import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Own ports (not Vite/uvicorn defaults) so this never collides with other
// local projects. strictPort fails loudly instead of silently picking another.
const API = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8100'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5291,
    strictPort: true,
    proxy: {
      '/api': API,
      '/health': API,
    },
  },
})
