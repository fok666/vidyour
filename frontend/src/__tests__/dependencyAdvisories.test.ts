// @vitest-environment node

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

interface PackageJson {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

interface PackageLock {
  packages: Record<string, { version?: string }>;
}

const packageJson = JSON.parse(
  readFileSync(new URL('../../package.json', import.meta.url), 'utf8'),
) as PackageJson;

const packageLock = JSON.parse(
  readFileSync(new URL('../../package-lock.json', import.meta.url), 'utf8'),
) as PackageLock;

function parseVersion(version: string): number[] {
  return version.split(/[-+]/)[0].split('.').map(Number);
}

// Fails closed: a version without three numeric parts (e.g. "8" or "latest") is treated as below any floor.
function isBelow(version: string, threshold: string): boolean {
  const current = parseVersion(version);
  const minimum = parseVersion(threshold);
  if (current.length < 3 || current.some(Number.isNaN)) {
    return true;
  }
  for (let index = 0; index < minimum.length; index += 1) {
    if (current[index] !== minimum[index]) {
      return current[index] < minimum[index];
    }
  }
  return false;
}

// Every lockfile entry whose package name is exactly `name`, at any nesting depth.
function instancesOf(name: string): [string, string][] {
  return Object.entries(packageLock.packages)
    .filter(([key, entry]) => entry.version !== undefined && key.split('node_modules/').pop() === name)
    .map(([key, entry]) => [key, entry.version as string]);
}

function vulnerableInstances(name: string, threshold: string): string[] {
  return instancesOf(name)
    .filter(([, version]) => isBelow(version, threshold))
    .map(([key, version]) => `${key}@${version}`);
}

// The lower bound of a caret, tilde or >= range, e.g. "^8.4.0" -> "8.4.0".
function lowerBound(range: string): string {
  return range.replace(/^(\^|~|>=)/, '');
}

// Declared ranges (dependencies and devDependencies) below their floor; a missing package counts.
function belowFloorDeclarations(
  floors: Record<string, string>,
  declared: Record<string, string> = { ...packageJson.dependencies, ...packageJson.devDependencies },
): string[] {
  return Object.entries(floors).flatMap(([name, floor]) => {
    const range = declared[name];
    if (range === undefined) {
      return [`${name}@missing`];
    }
    return isBelow(lowerBound(range), floor) ? [`${name}@${range}`] : [];
  });
}

// Lockfile instances below their floor at any depth; a package with no entry at all counts.
function lockfileFloorViolations(floors: Record<string, string>): string[] {
  return Object.entries(floors).flatMap(([name, floor]) => {
    const found = instancesOf(name);
    if (found.length === 0) {
      return [`${name}@missing`];
    }
    return found.filter(([, version]) => isBelow(version, floor)).map(([key, version]) => `${key}@${version}`);
  });
}

describe('dependency advisories', () => {
  it('declares the frontend toolchain floors in package.json', () => {
    expect(
      belowFloorDeclarations({
        vite: '8.4.0',
        vitest: '5.1.2',
        '@vitejs/plugin-react': '6.3.0',
        tailwindcss: '4.3.5',
        react: '19.0.0',
        'react-dom': '19.3.1',
        '@types/react': '19.0.0',
        '@types/react-dom': '19.3.0',
        '@types/node': '26.6.6',
      }),
    ).toEqual([]);
  });

  it('resolves every vitest instance to 4.1.12 or later', () => {
    expect(instancesOf('vitest').length).toBeGreaterThan(0);
    expect(vulnerableInstances('vitest', '4.1.12')).toEqual([]);
  });

  it('resolves every @vitest/mocker instance, nested or top-level, to 4.1.12 or later', () => {
    expect(vulnerableInstances('@vitest/mocker', '4.1.12')).toEqual([]);
  });

  it('resolves every vite instance, nested or top-level, to 6.4.4 or later', () => {
    expect(vulnerableInstances('vite', '6.4.4')).toEqual([]);
  });

  it('resolves every esbuild instance, nested or top-level, to 0.25.0 or later', () => {
    expect(vulnerableInstances('esbuild', '0.25.0')).toEqual([]);
  });

  it('resolves every tinypool instance, if present, to 2.1.2 or later', () => {
    expect(vulnerableInstances('tinypool', '2.1.2')).toEqual([]);
  });

  it('reports short or non-numeric declared ranges as below their floor', () => {
    expect(
      belowFloorDeclarations(
        { vite: '8.4.0', vitest: '5.1.2' },
        { vite: '^8', vitest: 'latest' },
      ),
    ).toEqual(['vite@^8', 'vitest@latest']);
  });

  it('declares @types/node so typecheck passes after a clean install', () => {
    expect(packageJson.devDependencies).toHaveProperty('@types/node');
  });

  it('declares @tailwindcss/postcss at 4.3.5 or later and resolves every instance to it', () => {
    expect(belowFloorDeclarations({ '@tailwindcss/postcss': '4.3.5' })).toEqual([]);
    expect(instancesOf('@tailwindcss/postcss').length).toBeGreaterThan(0);
    expect(vulnerableInstances('@tailwindcss/postcss', '4.3.5')).toEqual([]);
  });

  it('no longer declares autoprefixer, which Tailwind 4 replaces', () => {
    expect(packageJson.devDependencies).not.toHaveProperty('autoprefixer');
  });

  it('resolves every toolchain package in the lockfile, at any depth, to its floor or later', () => {
    expect(
      lockfileFloorViolations({
        vite: '8.4.0',
        vitest: '5.1.2',
        '@vitejs/plugin-react': '6.3.0',
        tailwindcss: '4.3.5',
        'react-dom': '19.3.1',
        '@types/react-dom': '19.3.0',
        '@types/node': '26.6.6',
      }),
    ).toEqual([]);
  });

  it('has no esbuild entry anywhere in the lockfile', () => {
    expect(instancesOf('esbuild').map(([key, version]) => `${key}@${version}`)).toEqual([]);
  });
});
