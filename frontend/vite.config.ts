import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Fail fast if 5173 is taken instead of silently moving to another port - the
  // backend's CORS_ORIGINS (see backend/app/main.py) only allows this exact origin.
  server: { port: 5173, strictPort: true },
})
