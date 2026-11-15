import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright配置文件
 * 用于端到端测试配置
 */
export default defineConfig({
  // 测试目录
  testDir: './tests/e2e',

  // 测试超时时间
  timeout: 30000,

  // 期望超时时间
  expect: {
    timeout: 5000,
  },

  // 失败时重试次数
  retries: process.env.CI ? 2 : 0,

  // 并发工作进程
  workers: process.env.CI ? 1 : undefined,

  // 测试报告
  reporter: [
    ['html', { outputFolder: 'playwright-report' }],
    ['json', { outputFile: 'test-results.json' }],
    ['list'],
  ],

  // 全局配置
  use: {
    // 基础URL（与 vite.config.ts 的 server.port / `npm run dev:react` 同口径：3000）
    baseURL: 'http://localhost:3000',

    // 追踪配置
    trace: 'on-first-retry',

    // 截图配置
    screenshot: 'only-on-failure',

    // 视频配置
    video: 'retain-on-failure',

    // 浏览器上下文
    contextOptions: {
      ignoreHTTPSErrors: true,
    },
  },

  // 测试项目配置
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },

    // 可选的其他浏览器
    // {
    //   name: 'firefox',
    //   use: { ...devices['Desktop Firefox'] },
    // },

    // {
    //   name: 'webkit',
    //   use: { ...devices['Desktop Safari'] },
    // },

    // 移动端测试
    // {
    //   name: 'Mobile Chrome',
    //   use: { ...devices['Pixel 5'] },
    // },
  ],

  // Web服务器配置（只起前端 dev server；e2e 不需要 Electron 壳）
  webServer: {
    command: 'npm run dev:react',
    port: 3000,
    timeout: 120 * 1000,
    reuseExistingServer: !process.env.CI,
  },
});
