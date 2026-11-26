import { spawn, type ChildProcess } from 'node:child_process';
import { createWriteStream, mkdirSync, readFileSync } from 'node:fs';
import { once } from 'node:events';
import { resolve } from 'node:path';
import { test, expect } from 'playwright/test';

const frontendRoot = process.cwd();
const repositoryRoot = resolve(frontendRoot, '..');
const cacheDirectory = resolve(frontendRoot, 'node_modules/.cache/vidyour-playwright');
const runtimeDirectory = resolve(cacheDirectory, 'data');
const logsDirectory = resolve(cacheDirectory, 'logs');
const backendLog = resolve(logsDirectory, 'backend.log');
const frontendLog = resolve(logsDirectory, 'frontend.log');

mkdirSync(runtimeDirectory, { recursive: true });
mkdirSync(logsDirectory, { recursive: true });
process.env.TMPDIR = cacheDirectory;
process.env.TMP = cacheDirectory;
process.env.TEMP = cacheDirectory;

function startProcess(
  executable: string,
  args: string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; logFile: string },
): ChildProcess {
  const log = createWriteStream(options.logFile, { flags: 'w' });
  const child = spawn(executable, args, {
    cwd: options.cwd,
    env: options.env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout?.pipe(log);
  child.stderr?.pipe(log);
  child.once('close', () => log.end());
  return child;
}

async function waitForHttp(url: string, process: ChildProcess, logFile: string): Promise<Response> {
  const deadline = Date.now() + 60_000;
  let lastError: unknown;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      const log = readFileSync(logFile, 'utf8');
      throw new Error(`Server exited with code ${process.exitCode} before becoming ready:\n${log}`);
    }
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return response;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 250));
  }
  throw new Error(`Server did not become ready at ${url}: ${String(lastError)}`);
}

async function stopProcess(process: ChildProcess | undefined): Promise<void> {
  if (!process || process.exitCode !== null || process.signalCode !== null) return;
  process.kill('SIGTERM');
  await Promise.race([
    once(process, 'exit'),
    new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000)),
  ]);
  if (process.exitCode === null && process.signalCode === null) {
    process.kill('SIGKILL');
    await once(process, 'exit');
  }
}

test('real Chromium reaches the real FastAPI capabilities endpoint through Vite /api proxy', async ({ page }) => {
  const cleanEnvironment: NodeJS.ProcessEnv = {
    ...process.env,
    DATA_DIR: runtimeDirectory,
    AI_PROVIDER: 'local',
    GEMINI_API_KEY: '',
    AWS_ACCESS_KEY_ID: '',
    AWS_SECRET_ACCESS_KEY: '',
    GOOGLE_CLIENT_ID: '',
    GOOGLE_CLIENT_SECRET: '',
    GOOGLE_REDIRECT_URI: '',
    JAMENDO_CLIENT_ID: '',
    FREESOUND_API_KEY: '',
    S3_BUCKET: '',
    TMPDIR: cacheDirectory,
    TMP: cacheDirectory,
    TEMP: cacheDirectory,
  };
  let backend: ChildProcess | undefined;
  let frontend: ChildProcess | undefined;

  try {
    backend = startProcess(
      resolve(repositoryRoot, 'backend/.venv/bin/python'),
      ['-m', 'uvicorn', '--factory', 'app.main:create_app', '--host', '127.0.0.1', '--port', '8000', '--log-level', 'warning'],
      { cwd: resolve(repositoryRoot, 'backend'), env: cleanEnvironment, logFile: backendLog },
    );
    const backendResponse = await waitForHttp(
      'http://127.0.0.1:8000/api/config/capabilities',
      backend,
      backendLog,
    );
    const backendCapabilities = await backendResponse.json();
    expect(backendResponse.status).toBe(200);
    expect(backendCapabilities).toMatchObject({
      active_ai_provider: 'local',
      storage_backend: 'local',
    });

    frontend = startProcess(
      process.execPath,
      [
        resolve(frontendRoot, 'node_modules/vite/bin/vite.js'),
        '--host',
        '127.0.0.1',
        '--port',
        '5173',
        '--strictPort',
      ],
      {
        cwd: frontendRoot,
        env: {
          ...cleanEnvironment,
          NODE_OPTIONS: '--dns-result-order=ipv4first',
        },
        logFile: frontendLog,
      },
    );
    await waitForHttp('http://127.0.0.1:5173/', frontend, frontendLog);

    const capabilitiesResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith('/api/config/capabilities'),
    );
    await page.goto('http://127.0.0.1:5173/');
    const capabilitiesResponse = await capabilitiesResponsePromise;
    expect(capabilitiesResponse.status()).toBe(200);
    expect(await capabilitiesResponse.json()).toMatchObject({
      active_ai_provider: 'local',
      storage_backend: 'local',
    });
    await expect(page).toHaveTitle(/VidYour/);
    await expect(page.getByText('VidYour', { exact: true })).toBeVisible();
    await expect(page.getByText('Local Storage', { exact: true })).toBeVisible();
  } finally {
    await stopProcess(frontend);
    await stopProcess(backend);
  }
});
