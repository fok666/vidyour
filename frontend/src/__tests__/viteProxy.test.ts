// @vitest-environment node

import { createServer as createHttpServer } from 'node:http';
import { once } from 'node:events';
import { afterEach, describe, expect, it } from 'vitest';
import { createServer as createViteServer, loadConfigFromFile } from 'vite';

const configPath = new URL('../../vite.config.ts', import.meta.url).pathname;

describe('Vite API development proxy integration', () => {
  let vite: Awaited<ReturnType<typeof createViteServer>> | undefined;
  let backend: ReturnType<typeof createHttpServer> | undefined;

  afterEach(async () => {
    if (vite) {
      await vite.close();
      vite = undefined;
    }
    if (backend?.listening) {
      backend.close();
      await once(backend, 'close');
      backend = undefined;
    }
  });

  it('uses localhost:8000 without a path rewrite for /api', async () => {
    const loaded = await loadConfigFromFile({ command: 'serve', mode: 'development' }, configPath);
    expect(loaded).not.toBeNull();
    const proxy = loaded?.config.server?.proxy?.['/api'];
    expect(proxy).toMatchObject({ target: 'http://localhost:8000' });
    expect(proxy).not.toHaveProperty('rewrite');
  });

  it('forwards a real HTTP request and query string unchanged through the configured proxy', async () => {
    let forwardedPath: string | undefined;
    backend = createHttpServer((request, response) => {
      forwardedPath = request.url;
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ progress: 75 }));
    });
    backend.listen(0, '127.0.0.1');
    await once(backend, 'listening');
    const backendAddress = backend.address();
    if (!backendAddress || typeof backendAddress === 'string') throw new Error('Backend did not bind a TCP port');

    const loaded = await loadConfigFromFile({ command: 'serve', mode: 'development' }, configPath);
    if (!loaded) throw new Error('Could not load the real Vite config');
    const configuredProxy = loaded.config.server?.proxy?.['/api'];
    if (
      !configuredProxy ||
      typeof configuredProxy !== 'object' ||
      Array.isArray(configuredProxy)
    ) {
      throw new Error('The /api proxy is not configured');
    }

    vite = await createViteServer({
      ...loaded.config,
      configFile: false,
      logLevel: 'silent',
      server: {
        ...loaded.config.server,
        host: '127.0.0.1',
        port: 0,
        strictPort: false,
        proxy: {
          ...loaded.config.server?.proxy,
          '/api': { ...configuredProxy, target: `http://127.0.0.1:${backendAddress.port}` },
        },
      },
    });
    await vite.listen();
    const viteAddress = vite.httpServer?.address();
    if (!viteAddress || typeof viteAddress === 'string') throw new Error('Vite did not bind a TCP port');

    const response = await fetch(`http://127.0.0.1:${viteAddress.port}/api/jobs/job-a1?view=progress`);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ progress: 75 });
    expect(forwardedPath).toBe('/api/jobs/job-a1?view=progress');
  });
});
