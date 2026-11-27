// @vitest-environment node

import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

interface PostcssConfig {
  plugins: Record<string, unknown>;
}

const postcssConfigUrl = new URL('../../postcss.config.js', import.meta.url);
const indexCssUrl = new URL('../../src/index.css', import.meta.url);
const tailwindConfigPath = fileURLToPath(new URL('../../tailwind.config.js', import.meta.url));

describe('Tailwind 4 setup', () => {
  it('uses the @tailwindcss/postcss plugin as the only PostCSS plugin', async () => {
    const { default: config } = (await import(postcssConfigUrl.href)) as { default: PostcssConfig };

    expect(Object.keys(config.plugins)).toEqual(['@tailwindcss/postcss']);
  });

  it('imports tailwindcss in index.css instead of the Tailwind 3 directives', () => {
    const css = readFileSync(indexCssUrl, 'utf8').trimStart();

    expect(css.startsWith('@import "tailwindcss";')).toBe(true);
    expect(css).not.toMatch(/@tailwind\b/);
  });

  it('has no tailwind.config.js, since Tailwind 4 needs no JS config for this project', () => {
    expect(existsSync(tailwindConfigPath)).toBe(false);
  });
});
