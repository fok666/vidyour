// @vitest-environment node

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

interface PackageJson {
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

function isBelow(version: string, threshold: string): boolean {
  const current = parseVersion(version);
  const minimum = parseVersion(threshold);
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

describe('dependency advisories', () => {
  it('declares vitest 4.1.12 or later as the devDependency', () => {
    expect(packageJson.devDependencies?.vitest).toBe('^4.1.12');
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

  it('declares @types/node so typecheck passes after a clean install', () => {
    expect(packageJson.devDependencies).toHaveProperty('@types/node');
  });
});
