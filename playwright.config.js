const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
    testDir: './tests',
    fullyParallel: true,
    timeout: 20_000,
    use: {
        baseURL: 'http://127.0.0.1:8765',
        browserName: 'chromium',
        trace: 'retain-on-failure',
    },
    webServer: {
        command: 'node tests/serve-frontend.js',
        url: 'http://127.0.0.1:8765',
        reuseExistingServer: !process.env.CI,
    },
});
