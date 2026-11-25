import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const cacheDirectory = resolve(process.cwd(), 'node_modules/.cache/vidyour-playwright');
mkdirSync(cacheDirectory, { recursive: true });
process.env.TMPDIR = cacheDirectory;
process.env.TMP = cacheDirectory;
process.env.TEMP = cacheDirectory;

const config = {
  testDir: './e2e',
  outputDir: './node_modules/.cache/vidyour-playwright-results',
  reporter: 'list',
  workers: 1,
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
};

export default config;
