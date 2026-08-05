import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Distinct from frontend/ (5173) so both UIs can run side by side during the v2 migration.
  server: { port: 5174, strictPort: true },
})
