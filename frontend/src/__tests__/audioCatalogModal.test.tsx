import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AudioCatalogModal } from '../components/AudioCatalogModal';
import { useStudioStore } from '../store/useStudioStore';
import type { AudioCatalogTrack, MediaAsset, Project } from '../types/studio';

const NOW = '2026-11-22T00:00:00Z';

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

const TRACK: AudioCatalogTrack = {
  id: 'builtin-124',
  provider: 'builtin',
  title: '124 BPM House Drive',
  artist: 'Vidyour',
  bpm: 124,
  duration_sec: 32,
  preview_url: '/api/audio-catalog/builtin/builtin-124/preview.mp3',
  license: 'CC0',
};

const IMPORTED_ASSET: MediaAsset = {
  id: 'asset-124',
  project_id: 'p1',
  kind: 'audio',
  source: 'audio_catalog',
  filename: '124 BPM House Drive.mp3',
  local_path: 'data/assets/p1/builtin-124.mp3',
  duration_sec: 32,
  width: null,
  height: null,
  fps: null,
  has_audio: true,
  metadata: { track_id: 'builtin-124' },
  created_at: NOW,
} as unknown as MediaAsset;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface Call {
  url: string;
  method: string;
  body?: string;
}

let calls: Call[];
let searchErrorResponse: Response | null;
let importErrorResponse: Response | null;
let mediaRefreshErrorResponse: Response | null;
let projectRefreshErrorResponse: Response | null;
let searchResults: AudioCatalogTrack[];

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined });
      const path = url.split('?')[0];
      if (method === 'GET' && path.endsWith('/audio-catalog/search')) {
        if (searchErrorResponse) return searchErrorResponse;
        return jsonResponse(searchResults);
      }
      if (method === 'POST' && path.endsWith('/projects/p1/audio-catalog/import')) {
        if (importErrorResponse) return importErrorResponse;
        return jsonResponse(IMPORTED_ASSET);
      }
      if (method === 'GET' && path.endsWith('/projects/p1/media')) {
        if (mediaRefreshErrorResponse) return mediaRefreshErrorResponse;
        return jsonResponse([]);
      }
      if (method === 'GET' && path.endsWith('/projects/p1')) {
        if (projectRefreshErrorResponse) return projectRefreshErrorResponse;
        return jsonResponse(makeProject());
      }
      if (method === 'GET' && path.endsWith('/projects/p1/parts')) {
        return jsonResponse([]);
      }
      return jsonResponse({});
    }),
  );
}

async function searchHouse(): Promise<void> {
  fireEvent.change(screen.getByRole('textbox', { name: /search|query/i }), {
    target: { value: 'House' },
  });
  fireEvent.change(screen.getByLabelText(/min(imum)?\s*bpm/i), { target: { value: '115' } });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
}

describe('AudioCatalogModal', () => {
  let playSpy: ReturnType<typeof vi.fn>;
  let pauseSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    calls = [];
    searchErrorResponse = null;
    importErrorResponse = null;
    mediaRefreshErrorResponse = null;
    projectRefreshErrorResponse = null;
    searchResults = [TRACK];
    installFetch();
    playSpy = vi.fn(() => Promise.resolve());
    pauseSpy = vi.fn();
    vi.spyOn(window.HTMLMediaElement.prototype, 'play').mockImplementation(
      playSpy as unknown as () => Promise<void>,
    );
    vi.spyOn(window.HTMLMediaElement.prototype, 'pause').mockImplementation(
      pauseSpy as unknown as () => void,
    );
    useStudioStore.setState({ project: makeProject(), assets: [], jobs: {} });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useStudioStore.setState({ project: null, assets: [], jobs: {} });
  });

  it('searches the catalog with q and bpm_min and renders the matching track', async () => {
    // GIVEN
    render(<AudioCatalogModal open />);

    // WHEN
    await searchHouse();

    // THEN
    await waitFor(() => {
      const search = calls.find(
        (call) => call.method === 'GET' && call.url.split('?')[0].endsWith('/api/audio-catalog/search')
          && new URL(call.url, 'http://localhost').searchParams.get('q') === 'House',
      );
      expect(search).toBeTruthy();
      const params = new URL(search!.url, 'http://localhost').searchParams;
      expect(params.get('q')).toBe('House');
      expect(params.get('bpm_min')).toBe('115');
    });
    expect(await screen.findByText('124 BPM House Drive')).toBeTruthy();
  });

  it('omits empty BPM filters from the catalog search', async () => {
    render(<AudioCatalogModal open />);

    fireEvent.submit(screen.getByRole('button', { name: /^search$/i }).closest('form')!);

    await waitFor(() => {
      const search = calls.find(
        (call) =>
          call.method === 'GET' &&
          call.url.split('?')[0].endsWith('/api/audio-catalog/search'),
      );
      expect(search).toBeTruthy();
      const params = new URL(search!.url, 'http://localhost').searchParams;
      expect(params.has('bpm_min')).toBe(false);
      expect(params.has('bpm_max')).toBe(false);
    });
  });

  it('omits non-positive BPM filters from the catalog search', async () => {
    render(<AudioCatalogModal open />);
    fireEvent.change(screen.getByLabelText(/min(imum)?\s*bpm/i), {
      target: { value: '-1' },
    });
    fireEvent.change(screen.getByLabelText(/max(imum)?\s*bpm/i), {
      target: { value: '0' },
    });

    fireEvent.submit(screen.getByRole('button', { name: /^search$/i }).closest('form')!);

    await waitFor(() => {
      const search = calls.find(
        (call) =>
          call.method === 'GET' &&
          call.url.split('?')[0].endsWith('/api/audio-catalog/search'),
      );
      expect(search).toBeTruthy();
      const params = new URL(search!.url, 'http://localhost').searchParams;
      expect(params.has('bpm_min')).toBe(false);
      expect(params.has('bpm_max')).toBe(false);
    });
  });

  it('plays the preview once and relabels the button to Stop', async () => {
    // GIVEN
    render(<AudioCatalogModal open />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /preview/i }));

    // THEN
    await waitFor(() => expect(playSpy).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /preview/i })).toBeNull();
  });

  it('disables preview when a result has no preview URL', async () => {
    searchResults = [{ ...TRACK, preview_url: undefined }];
    render(<AudioCatalogModal open />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    expect(screen.getByRole('button', { name: /preview/i }).hasAttribute('disabled')).toBe(true);
    expect(playSpy).not.toHaveBeenCalled();
  });

  it('stops the playing preview and restores the Preview button', async () => {
    const audio = {
      src: '',
      play: vi.fn(() => Promise.resolve()),
      pause: vi.fn(),
      onended: null as (() => void) | null,
    } as unknown as HTMLAudioElement;
    render(<AudioCatalogModal open audioElementFactory={() => audio} />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');
    fireEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(await screen.findByRole('button', { name: /^stop$/i })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /^stop$/i }));

    expect(audio.pause).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole('button', { name: /preview/i })).toBeTruthy();
  });

  it('resets the preview button when playback ends', async () => {
    const audio = {
      src: '',
      play: vi.fn(() => Promise.resolve()),
      pause: vi.fn(),
      onended: null as ((event: Event) => void) | null,
    } as unknown as HTMLAudioElement;
    render(<AudioCatalogModal open audioElementFactory={() => audio} />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');
    fireEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(await screen.findByRole('button', { name: /^stop$/i })).toBeTruthy();

    act(() => audio.onended?.(new Event('ended')));

    expect(await screen.findByRole('button', { name: /preview/i })).toBeTruthy();
  });

  it('shows an alert and resets the preview when playback is rejected', async () => {
    const audio = {
      src: '',
      play: vi.fn(() => Promise.reject(new Error('Preview playback failed'))),
      pause: vi.fn(),
      onended: null as (() => void) | null,
    } as unknown as HTMLAudioElement;
    render(<AudioCatalogModal open audioElementFactory={() => audio} />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    fireEvent.click(screen.getByRole('button', { name: /preview/i }));

    expect((await screen.findByRole('alert')).textContent).toContain('Preview playback failed');
    expect(await screen.findByRole('button', { name: /preview/i })).toBeTruthy();
  });

  it('sends the track id and provider and adds the returned audio asset to the store', async () => {
    // GIVEN
    render(<AudioCatalogModal open />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }));

    // THEN
    await waitFor(() => {
      const post = calls.find(
        (call) => call.method === 'POST' && call.url.endsWith('/api/projects/p1/audio-catalog/import'),
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(post!.body ?? '{}')).toEqual({
        track_id: 'builtin-124',
        provider: 'builtin',
      });
    });
    await waitFor(() => {
      const asset = useStudioStore.getState().assets.find((a) => a.id === 'asset-124');
      expect(asset).toBeTruthy();
      expect(asset!.kind).toBe('audio');
      expect(asset!.source).toBe('audio_catalog');
      expect(asset!.filename).toBe('124 BPM House Drive.mp3');
    });
    expect(calls.some((call) => call.url.includes('/jobs/'))).toBe(false);
  });

  it('shows the import error when the synchronous MediaAsset import fails', async () => {
    importErrorResponse = jsonResponse({ detail: 'Catalog import unavailable' }, 503);
    render(<AudioCatalogModal open />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    fireEvent.click(screen.getByRole('button', { name: /^add$/i }));

    expect((await screen.findByRole('alert')).textContent).toContain('Catalog import unavailable');
    expect(screen.getByRole('button', { name: /^add$/i })).toBeTruthy();
    expect(useStudioStore.getState().assets).toEqual([]);
  });

  it('handles a missing project without attempting an import', async () => {
    useStudioStore.setState({ project: null, assets: [], jobs: {} });
    render(<AudioCatalogModal open />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    const addButton = screen.getByRole('button', { name: /^add$/i });
    expect(addButton.hasAttribute('disabled')).toBe(true);
    fireEvent.click(addButton);

    expect(screen.queryByRole('alert')).toBeNull();
    expect(calls.some((call) => call.method === 'POST')).toBe(false);
  });

  it('keeps the imported asset when media and project refreshes fail', async () => {
    mediaRefreshErrorResponse = jsonResponse({ detail: 'Media refresh unavailable' }, 503);
    projectRefreshErrorResponse = jsonResponse({ detail: 'Project refresh unavailable' }, 503);
    const onAdd = vi.fn();
    render(<AudioCatalogModal open onAdd={onAdd} />);
    await searchHouse();
    await screen.findByText('124 BPM House Drive');

    fireEvent.click(screen.getByRole('button', { name: /^add$/i }));

    expect(await screen.findByRole('button', { name: /^added$/i })).toBeTruthy();
    await waitFor(() => {
      expect(useStudioStore.getState().assets).toContainEqual(IMPORTED_ASSET);
    });
    expect(onAdd).toHaveBeenCalledWith(TRACK);
    expect(calls.some((call) => call.url.endsWith('/api/projects/p1/media'))).toBe(true);
    expect(calls.some((call) => call.url.endsWith('/api/projects/p1'))).toBe(true);
  });

  it('shows an alert when catalog search fails', async () => {
    searchErrorResponse = jsonResponse({ detail: 'Catalog search unavailable' }, 503);
    render(<AudioCatalogModal open />);

    await searchHouse();

    expect((await screen.findByRole('alert')).textContent).toContain('Catalog search unavailable');
    expect(screen.queryByText('124 BPM House Drive')).toBeNull();
  });
});
