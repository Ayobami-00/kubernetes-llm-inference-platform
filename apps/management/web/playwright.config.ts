import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: 'tests',
  testMatch: '*.spec.ts',
  timeout: 90000,
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.MANAGEMENT_WEB_URL || 'http://localhost:5173',
    viewport: { width: 1600, height: 1050 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  reporter: 'list',
});
