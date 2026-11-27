// @vitest-environment node

import { dirname, resolve } from 'node:path';
import type { Rollup } from 'vite';
import { describe, expect, it } from 'vitest';
import ts from 'typescript';
import { build } from 'vite';

const frontendRoot = process.cwd();

describe('frontend production readiness', () => {
  it('type-checks every configured source file with no emitted files', () => {
    const configPath = resolve(frontendRoot, 'tsconfig.json');
    const configFile = ts.readConfigFile(configPath, ts.sys.readFile);
    expect(configFile.error).toBeUndefined();

    const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, dirname(configPath));
    const compilerOptions = { ...parsed.options, noEmit: true };
    expect(compilerOptions.noEmit).toBe(true);

    const program = ts.createProgram({
      rootNames: parsed.fileNames,
      options: compilerOptions,
      projectReferences: parsed.projectReferences,
    });
    const diagnostics = ts.getPreEmitDiagnostics(program);
    expect(
      diagnostics.map((diagnostic) =>
        ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'),
      ),
    ).toEqual([]);
  });

  it('builds the real app and stylesheet into an in-memory production bundle', async () => {
    const result = await build({
      configFile: resolve(frontendRoot, 'vite.config.ts'),
      mode: 'production',
      logLevel: 'silent',
      build: {
        write: false,
        minify: false,
        rollupOptions: {
          input: {
            index: resolve(frontendRoot, 'index.html'),
            studio: resolve(frontendRoot, 'src/main.tsx'),
          },
        },
      },
    });
    const bundles: unknown[] = Array.isArray(result) ? result : [result];
    const outputs: Array<Rollup.OutputAsset | Rollup.OutputChunk> = bundles.flatMap(
      (bundle): Array<Rollup.OutputAsset | Rollup.OutputChunk> => {
      if (typeof bundle !== 'object' || bundle === null || !('output' in bundle)) return [];
        return Array.isArray(bundle.output)
          ? (bundle.output as Array<Rollup.OutputAsset | Rollup.OutputChunk>)
          : [];
      },
    );
    const entry = outputs.find(
      (item) =>
        item.type === 'chunk' &&
        item.isEntry &&
        item.facadeModuleId?.replaceAll('\\', '/').endsWith('/src/main.tsx'),
    );
    const html = outputs.find((item) => item.type === 'asset' && item.fileName === 'index.html');
    const css = outputs.find((item) => item.type === 'asset' && item.fileName.endsWith('.css'));

    expect(entry?.type).toBe('chunk');
    if (entry?.type === 'chunk') {
      expect(entry.facadeModuleId?.replaceAll('\\', '/')).toMatch(/\/src\/main\.tsx$/);
      expect(entry.code.length).toBeGreaterThan(0);
    }
    expect(html?.type).toBe('asset');
    expect(css?.type).toBe('asset');
    if (css?.type === 'asset') {
      const source = String(css.source);
      expect(source.length).toBeGreaterThan(0);
      expect(source).toContain('.bg-indigo-600');
      expect(source).toContain('.timeline-dock');
      expect(source).not.toContain('@tailwind');
    }
  });
});
