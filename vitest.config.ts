import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify('2.0.0-test'),
  },
  test: {
    environment: 'jsdom',
    include: ['tests/components/**/*.test.tsx'],
    setupFiles: ['./tests/components/setup.ts'],
    restoreMocks: true,
  },
});
