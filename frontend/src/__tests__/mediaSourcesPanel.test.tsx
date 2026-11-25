import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MediaSourcesPanel } from '../components/MediaSourcesPanel';
import { useStudioStore } from '../store/useStudioStore';
import type { Capabilities, Job, MediaAsset, Project } from '../types/studio';

const NOW = '2026-11-22T00:00:00Z';
const AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth?scope=photospicker';
const PICKER_URI = 'https://photos.google.com/picker/s1';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1',
    name: 'Demo',
    bpm: 124,
    beat_offset_sec: 0,
    resolution_w: 1280,
    resolution_h: 720,
    fps: 30,
    ai_context: '',
    result_target: 'perfect_loop',
    suggested_sequence: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeCapabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  return {
    google_photos: false,
    gemini: true,
    bedrock: false,
    jamendo: false,
    freesound: false,
    s3: false,
    active_ai_provider: 'gemini',
    storage_backend: 'local',
    ...overrides,
  };
}

function makeAsset(overrides: Partial<MediaAsset> = {}): MediaAsset {
  return {
    id: 'a1',
    project_id: 'p1',
    kind: 'video',
    source: 'upload',
    filename: 'clip.mp4',
    local_path: 'media/p1/clip.mp4',
    duration_sec: 4,
    width: 1280,
    height: 720,
    fps: 30,
    has_audio: true,
    metadata: {},
    created_at: NOW,
    ...overrides,
  };
}

const IMPORT_JOB: Job = {
  id: 'job-imp',
  project_id: 'p1',
  kind: 'import',
  status: 'running',
  progress: 60,
  message: 'Importing 2 items',
  result: {},
  created_at: NOW,
  updated_at: NOW,
};

function jsonResponse(body: unknown, status = 200): Response {
  if (status === 204) return new Response(null, { status });
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface Call {
  url: string;
  method: string;
}

let calls: Call[];
let googleConnected: boolean;
let googleConfigured: boolean;
let uploadFailure: boolean;
let deleteFailure: boolean;
let importFailure: boolean;
let mediaListResponse: MediaAsset[];

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method });
      const path = url.split('?')[0];
      if (path.endsWith('/config/capabilities')) {
        return jsonResponse(useStudioStore.getState().capabilities ?? makeCapabilities());
      }
      if (path.endsWith('/gallery/google/status')) {
        return jsonResponse({ configured: googleConfigured, connected: googleConnected });
      }
      if (path.endsWith('/gallery/google/auth-url')) {
        return jsonResponse({ auth_url: AUTH_URL });
      }
      if (method === 'POST' && path.endsWith('/gallery/google/sessions')) {
        return jsonResponse({ session_id: 's1', picker_uri: PICKER_URI });
      }
      if (method === 'GET' && /\/gallery\/google\/sessions\/s1$/.test(path)) {
        return jsonResponse({ session_id: 's1', picker_uri: PICKER_URI, media_items_set: true });
      }
      if (method === 'POST' && path.endsWith('/gallery/google/sessions/s1/import')) {
        if (importFailure) return jsonResponse({ detail: 'Google Photos import failed' }, 500);
        return jsonResponse(IMPORT_JOB);
      }
      if (path.endsWith('/jobs/job-imp')) {
        return jsonResponse(IMPORT_JOB);
      }
      if (method === 'POST' && path.endsWith('/projects/p1/media/upload')) {
        if (uploadFailure) return jsonResponse({ detail: 'Upload failed' }, 500);
        return jsonResponse([makeAsset()]);
      }
      if (method === 'GET' && path.endsWith('/projects/p1/media')) {
        return jsonResponse(mediaListResponse);
      }
      if (method === 'DELETE' && path.endsWith('/media/a1')) {
        if (deleteFailure) return jsonResponse({ detail: 'Delete failed' }, 500);
        return jsonResponse(null, 204);
      }
      if (method === 'GET' && path.endsWith('/projects/p1/jobs')) {
        return jsonResponse([]);
      }
      if (method === 'GET' && path.endsWith('/projects/p1/parts')) {
        return jsonResponse([]);
      }
      return jsonResponse({});
    }),
  );
}

function setGoogle(configured: boolean, connected: boolean): void {
  googleConfigured = configured;
  googleConnected = connected;
  useStudioStore.setState({
    capabilities: makeCapabilities({ google_photos: configured }),
    googlePhotosStatus: { configured, connected },
  });
}

describe('MediaSourcesPanel', () => {
  let openSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    calls = [];
    googleConfigured = false;
    googleConnected = false;
    uploadFailure = false;
    deleteFailure = false;
    importFailure = false;
    mediaListResponse = [];
    installFetch();
    openSpy = vi.fn(() => null);
    vi.stubGlobal('open', openSpy);
    window.open = openSpy as unknown as typeof window.open;
    useStudioStore.setState({
      project: makeProject(),
      capabilities: makeCapabilities(),
      googlePhotosStatus: { configured: false, connected: false },
      googlePhotosAuthUrl: null,
      googlePhotosSession: null,
      assets: [],
      jobs: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    useStudioStore.setState({
      project: null,
      capabilities: null,
      googlePhotosStatus: null,
      googlePhotosAuthUrl: null,
      googlePhotosSession: null,
      assets: [],
      jobs: {},
    });
  });

  it('disables the Google Photos button and shows Not configured when google_photos=false', () => {
    // GIVEN
    setGoogle(false, false);

    // WHEN
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);

    // THEN
    const button = screen.getByRole('button', { name: /google photos/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/not configured/i)).toBeTruthy();
  });

  it('opens the Google OAuth URL when Google Photos is clicked while not connected', async () => {
    // GIVEN
    setGoogle(true, false);
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));

    // THEN
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    expect(openSpy.mock.calls[0][0]).toBe(AUTH_URL);
    expect(calls.some((c) => c.url.includes('/api/gallery/google/auth-url'))).toBe(true);
  });

  it('opens the Picker session and shows import progress when connected', async () => {
    // GIVEN
    setGoogle(true, true);
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const importButton = screen.queryByRole('button', { name: /^import/i });
    if (importButton) fireEvent.click(importButton);

    // THEN
    expect(openSpy.mock.calls[0][0]).toBe(PICKER_URI);
    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === 'POST' && c.url.includes('/api/gallery/google/sessions/s1/import'),
        ),
      ).toBe(true);
    });
    expect(await screen.findByText(/60%/)).toBeTruthy();
    expect(screen.getByText(/Importing 2 items/)).toBeTruthy();
  });

  it('refreshes project media in the store when a Google Photos import completes', async () => {
    // GIVEN
    setGoogle(true, true);
    const importedAsset = makeAsset({ id: 'a-imported', filename: 'from-photos.jpg' });
    mediaListResponse = [importedAsset];
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === 'POST' &&
            call.url.includes('/api/gallery/google/sessions/s1/import'),
        ),
      ).toBe(true),
    );
    act(() => {
      useStudioStore.setState({
        jobs: {
          'job-imp': {
            ...IMPORT_JOB,
            status: 'completed',
            progress: 100,
            message: 'Import complete',
          },
        },
      });
    });

    // THEN
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === 'GET' && call.url.endsWith('/api/projects/p1/media'),
        ),
      ).toBe(true),
    );
    await waitFor(() => {
      expect(useStudioStore.getState().assets).toEqual([importedAsset]);
      expect(screen.getByText('from-photos.jpg')).toBeTruthy();
    });
  });

  it('refreshes Google Photos connection status after the OAuth popup posts a message', async () => {
    // GIVEN
    setGoogle(true, false);
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));
    await waitFor(() => expect(openSpy).toHaveBeenCalledWith(AUTH_URL, expect.anything(), expect.anything()));
    googleConnected = true;
    const statusRequestsBeforeMessage = calls.filter((call) =>
      call.url.endsWith('/api/gallery/google/status'),
    ).length;

    // WHEN
    fireEvent(window, new Event('message'));

    // THEN
    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy());
    expect(
      calls.filter((call) => call.url.endsWith('/api/gallery/google/status')).length,
    ).toBeGreaterThan(statusRequestsBeforeMessage);
    expect(useStudioStore.getState().googlePhotosStatus?.connected).toBe(true);
  });

  it('uploads a selected file and lists the returned asset', async () => {
    // GIVEN
    const { container } = render(
      <MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    const file = new File(['data'], 'clip.mp4', { type: 'video/mp4' });

    // WHEN
    fireEvent.change(input, { target: { files: [file] } });

    // THEN
    await waitFor(() => {
      expect(
        calls.some((c) => c.method === 'POST' && c.url.includes('/api/projects/p1/media/upload')),
      ).toBe(true);
    });
    expect(await screen.findByText('clip.mp4')).toBeTruthy();
    expect(useStudioStore.getState().assets).toEqual([makeAsset()]);
  });

  it('marks the dropzone during dragover and uploads dropped files', async () => {
    // GIVEN
    const { container } = render(
      <MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />,
    );
    const dropzone = container.querySelector('.media-sources-dropzone') as HTMLDivElement;
    const dragOver = createEvent.dragOver(dropzone);
    const file = new File(['dropped'], 'dropped.mp4', { type: 'video/mp4' });

    // WHEN
    fireEvent(dropzone, dragOver);

    // THEN
    expect(dragOver.defaultPrevented).toBe(true);
    expect(dropzone.classList.contains('drag-over')).toBe(true);

    // WHEN
    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });

    // THEN
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === 'POST' && call.url.endsWith('/api/projects/p1/media/upload'),
        ),
      ).toBe(true),
    );
    expect(useStudioStore.getState().assets).toEqual([makeAsset()]);
    expect(screen.getByText('clip.mp4')).toBeTruthy();
  });

  it('ignores an empty drop without issuing an upload request', () => {
    // GIVEN
    const existingAsset = makeAsset();
    useStudioStore.setState({ assets: [existingAsset] });
    const { container } = render(
      <MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />,
    );
    const dropzone = container.querySelector('.media-sources-dropzone') as HTMLDivElement;

    // WHEN
    const drop = createEvent.drop(dropzone, { dataTransfer: { files: [] } });
    fireEvent(dropzone, drop);

    // THEN
    expect(drop.defaultPrevented).toBe(true);
    expect(calls.some((call) => call.method === 'POST' && call.url.endsWith('/media/upload'))).toBe(
      false,
    );
    expect(useStudioStore.getState().assets).toEqual([existingAsset]);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows an alert and preserves assets when an upload fails', async () => {
    // GIVEN
    uploadFailure = true;
    const existingAsset = makeAsset();
    useStudioStore.setState({ assets: [existingAsset] });
    const { container } = render(
      <MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    // WHEN
    fireEvent.change(input, {
      target: { files: [new File(['data'], 'failed.mp4', { type: 'video/mp4' })] },
    });

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Upload failed');
    expect(
      calls.some(
        (call) => call.method === 'POST' && call.url.endsWith('/api/projects/p1/media/upload'),
      ),
    ).toBe(true);
    expect(useStudioStore.getState().assets).toEqual([existingAsset]);
  });

  it('rejects uploads without a project without issuing an upload request', async () => {
    // GIVEN
    const { container } = render(
      <MediaSourcesPanel
        project={null}
        windowOpen={openSpy as unknown as typeof window.open}
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    // WHEN
    fireEvent.change(input, {
      target: { files: [new File(['data'], 'clip.mp4', { type: 'video/mp4' })] },
    });

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Select a project before uploading files.',
    );
    expect(
      calls.some((call) => call.url.endsWith('/api/projects/p1/media/upload')),
    ).toBe(false);
  });

  it('shows an alert when Google Photos import fails', async () => {
    // GIVEN
    setGoogle(true, true);
    importFailure = true;
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Google Photos import failed',
    );
    expect(
      calls.some(
        (call) =>
          call.method === 'POST' &&
          call.url.includes('/api/gallery/google/sessions/s1/import'),
      ),
    ).toBe(true);
  });

  it('rejects Google Photos imports without a project', async () => {
    // GIVEN
    setGoogle(true, true);
    render(
      <MediaSourcesPanel
        project={null}
        windowOpen={openSpy as unknown as typeof window.open}
      />,
    );

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /google photos/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Select a project before importing from Google Photos.',
    );
    expect(
      calls.some((call) => call.url.endsWith('/api/gallery/google/sessions')),
    ).toBe(false);
  });

  it('calls onOpenAudioCatalog exactly once when Audio Catalog is clicked', () => {
    // GIVEN
    const onOpenAudioCatalog = vi.fn();
    render(
      <MediaSourcesPanel
        onOpenAudioCatalog={onOpenAudioCatalog}
        windowOpen={openSpy as unknown as typeof window.open}
      />,
    );

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /audio catalog/i }));

    // THEN
    expect(onOpenAudioCatalog).toHaveBeenCalledTimes(1);
  });

  it('deletes an asset via DELETE /api/media/a1 and removes it from the list', async () => {
    // GIVEN
    useStudioStore.setState({ assets: [makeAsset()] });
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);
    expect(await screen.findByText('clip.mp4')).toBeTruthy();

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /delete|remove/i }));

    // THEN
    await waitFor(() => {
      expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/api/media/a1'))).toBe(
        true,
      );
    });
    await waitFor(() => expect(screen.queryByText('clip.mp4')).toBeNull());
    expect(useStudioStore.getState().assets).toEqual([]);
  });

  it('shows an alert and keeps an asset listed when delete fails', async () => {
    // GIVEN
    deleteFailure = true;
    const asset = makeAsset();
    useStudioStore.setState({ assets: [asset] });
    render(<MediaSourcesPanel windowOpen={openSpy as unknown as typeof window.open} />);
    expect(await screen.findByText('clip.mp4')).toBeTruthy();

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /delete clip\.mp4/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Delete failed');
    expect(calls.some((call) => call.method === 'DELETE' && call.url.endsWith('/api/media/a1'))).toBe(
      true,
    );
    expect(useStudioStore.getState().assets).toEqual([asset]);
    expect(screen.getByText('clip.mp4')).toBeTruthy();
  });
});
