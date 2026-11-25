import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../App';
import { useStudioStore } from '../store/useStudioStore';
import type {
  Capabilities,
  Composition,
  DetectedPart,
  MediaAsset,
  Project,
  TimelineVisualClip,
} from '../types/studio';

const timestamp = '2026-11-24T00:00:00Z';

const capabilities: Capabilities = {
  google_photos: false,
  gemini: false,
  bedrock: false,
  jamendo: false,
  freesound: false,
  s3: false,
  active_ai_provider: 'local',
  storage_backend: 'local',
};

const project: Project = {
  id: 'project-a1',
  name: 'Integration project',
  bpm: 120,
  beat_offset_sec: 0,
  resolution_w: 1280,
  resolution_h: 720,
  fps: 30,
  ai_context: '',
  result_target: 'perfect_loop',
  suggested_sequence: [],
  created_at: timestamp,
  updated_at: timestamp,
};

const asset: MediaAsset = {
  id: 'asset-a1',
  project_id: project.id,
  kind: 'video',
  source: 'upload',
  filename: 'source.mp4',
  local_path: 'assets/source.mp4',
  duration_sec: 8,
  has_audio: true,
  metadata: {},
  created_at: timestamp,
};

const part: DetectedPart = {
  id: 'part-a1',
  project_id: project.id,
  asset_id: asset.id,
  part_type: 'visual_clip',
  start_sec: 0,
  end_sec: 4,
  beat_length: 8,
  label: 'Opening shot',
  score: 0.9,
  ai_reason: 'A clear opening scene',
  suggested_effect: 'none',
  created_at: timestamp,
};

const clip: TimelineVisualClip = {
  id: 'v1',
  asset_id: asset.id,
  part_id: part.id,
  kind: 'video',
  start_sec: 0,
  duration_sec: 4,
  trim_start_sec: 0,
  speed: 1,
  transition_in: 'none',
  transition_duration_sec: 0,
  ken_burns: 'none',
  color_preset: 'original',
  volume: 1,
  muted: false,
  fade_in_sec: 0,
  fade_out_sec: 0,
};

const composition: Composition = {
  project_id: project.id,
  snap_subdivision: 'beat',
  video_clips: [clip],
  audio_clips: [],
  updated_at: timestamp,
};

const requests: Array<{ url: string; method: string; body?: string }> = [];
let listedProjects: Project[] = [project];
let createdProjectResponse: Project = {
  ...project,
  id: 'project-created',
  name: 'My first video',
};
let currentComposition: Composition;
let compositionSaveError: string | null = null;
let autoAssembleError: string | null = null;

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Created',
    text: async () => JSON.stringify(data),
  } as Response;
}

function installFakeBackend() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      const method = init.method ?? 'GET';
      requests.push({ url, method, body: typeof init.body === 'string' ? init.body : undefined });

      if (url === '/api/config/capabilities') return jsonResponse(capabilities);
      if (url === '/api/projects' && method === 'GET') return jsonResponse(listedProjects);
      if (url === '/api/projects' && method === 'POST') return jsonResponse(createdProjectResponse, 201);
      const projectId = url.match(/^\/api\/projects\/([^/]+)\//)?.[1];
      if (projectId && url.endsWith('/media')) {
        return jsonResponse([{ ...asset, project_id: projectId }]);
      }
      if (projectId && url.endsWith('/parts')) {
        return jsonResponse([{ ...part, project_id: projectId }]);
      }
      if (projectId && url.endsWith('/composition/auto-assemble') && method === 'POST' && autoAssembleError) {
        return jsonResponse({ detail: autoAssembleError }, 500);
      }
      if (projectId && url.endsWith('/composition') && method === 'GET') {
        return jsonResponse(currentComposition);
      }
      if (projectId && url.endsWith('/composition') && method === 'PUT') {
        if (compositionSaveError) return jsonResponse({ detail: compositionSaveError }, 500);
        currentComposition = JSON.parse(String(init.body)) as Composition;
        return jsonResponse(currentComposition);
      }
      return jsonResponse({ detail: `Unexpected fake API request: ${method} ${url}` }, 404);
    }),
  );
}

function resetStore() {
  useStudioStore.setState({
    capabilities: null,
    googlePhotosStatus: null,
    googlePhotosAuthUrl: null,
    googlePhotosSession: null,
    project: null,
    assets: [],
    parts: [],
    composition: null,
    jobs: {},
    selectedClipId: null,
    playheadSec: 0,
    isPlaying: false,
    loopPlayback: false,
    history: [],
  });
}

async function renderHydratedApp() {
  render(<App />);
  await screen.findByRole('toolbar', { name: /studio toolbar/i });
  await waitFor(() => expect(useStudioStore.getState().composition).not.toBeNull());
}

function currentVideoClips() {
  return useStudioStore.getState().composition?.video_clips ?? [];
}

describe('App studio integration', () => {
  beforeEach(() => {
    resetStore();
    requests.length = 0;
    listedProjects = [project];
    createdProjectResponse = {
      ...project,
      id: 'project-created',
      name: 'My first video',
    };
    currentComposition = structuredClone(composition);
    compositionSaveError = null;
    autoAssembleError = null;
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
    installFakeBackend();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('hydrates project data and composes all studio panels from the real components', async () => {
    await renderHydratedApp();

    expect(useStudioStore.getState().capabilities).toEqual(capabilities);
    expect(useStudioStore.getState().project).toEqual(project);
    expect(useStudioStore.getState().assets).toEqual([asset]);
    expect(useStudioStore.getState().parts).toEqual([part]);
    expect(useStudioStore.getState().selectedClipId).toBe('v1');
    expect(screen.getByText('Integration project')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /media & audio sources/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /ai analysis/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /detected parts/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /preview/i })).toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: /clip inspector/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /timeline/i })).toBeInTheDocument();
  });

  it('shows the no-project error screen when initial hydration fails', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValueOnce(new Error('capabilities unavailable'));

    render(<App />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('VidYour Studio could not load');
    expect(alert).toHaveTextContent('capabilities unavailable');
    expect(useStudioStore.getState().project).toBeNull();
    expect(screen.queryByRole('toolbar', { name: /studio toolbar/i })).not.toBeInTheDocument();
  });

  it('creates and hydrates My first video when the project list is empty', async () => {
    listedProjects = [];
    currentComposition = {
      ...structuredClone(composition),
      project_id: createdProjectResponse.id,
    };

    render(<App />);
    await screen.findByText('My first video');

    const createRequest = requests.find(
      (request) => request.method === 'POST' && request.url === '/api/projects',
    );
    expect(createRequest).toBeDefined();
    expect(JSON.parse(createRequest?.body ?? '{}')).toEqual({ name: 'My first video' });
    expect(useStudioStore.getState().project).toEqual(createdProjectResponse);
    expect(useStudioStore.getState().assets).toEqual([{ ...asset, project_id: createdProjectResponse.id }]);
    expect(useStudioStore.getState().parts).toEqual([{ ...part, project_id: createdProjectResponse.id }]);
    expect(useStudioStore.getState().composition?.project_id).toBe(createdProjectResponse.id);
  });

  it('debounces real inspector edits and saves the composition through the API client', async () => {
    await renderHydratedApp();
    vi.useFakeTimers();

    fireEvent.change(screen.getByLabelText(/audio fade in/i), { target: { value: '0.5' } });
    expect(requests.filter((request) => request.method === 'PUT')).toHaveLength(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    const saved = requests.find(
      (request) => request.method === 'PUT' && request.url === `/api/projects/${project.id}/composition`,
    );
    expect(saved).toBeDefined();
    expect(JSON.parse(saved?.body ?? '{}').video_clips[0].fade_in_sec).toBe(0.5);
  });

  it('shows an error alert when composition autosave is rejected', async () => {
    await renderHydratedApp();
    compositionSaveError = 'composition save failed';
    vi.useFakeTimers();

    fireEvent.change(screen.getByLabelText(/audio fade in/i), { target: { value: '0.5' } });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    vi.useRealTimers();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('composition save failed');
    expect(requests).toContainEqual(
      expect.objectContaining({
        url: `/api/projects/${project.id}/composition`,
        method: 'PUT',
      }),
    );
  });

  it('shows an error alert when Auto-Assemble API request fails', async () => {
    await renderHydratedApp();
    autoAssembleError = 'auto-assemble failed';

    fireEvent.click(screen.getByRole('button', { name: /auto-assemble for target/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('auto-assemble failed');
    expect(requests).toContainEqual(
      expect.objectContaining({
        url: `/api/projects/${project.id}/composition/auto-assemble`,
        method: 'POST',
      }),
    );
  });

  it('toggles preview playback with Space outside an editable input', async () => {
    await renderHydratedApp();
    fireEvent.keyDown(window, { key: ' ', code: 'Space' });
    expect(useStudioStore.getState().isPlaying).toBe(true);

    fireEvent.keyDown(screen.getByLabelText(/additional ai context/i), { key: ' ', code: 'Space' });
    expect(useStudioStore.getState().isPlaying).toBe(true);
  });

  it('splits the selected timeline clip at the playhead with S', async () => {
    await renderHydratedApp();
    act(() => useStudioStore.getState().setPlayheadSec(2));

    fireEvent.keyDown(window, { key: 's' });

    expect(currentVideoClips()).toHaveLength(2);
    expect(currentVideoClips().map(({ start_sec, duration_sec }) => [start_sec, duration_sec])).toEqual([
      [0, 2],
      [2, 2],
    ]);
  });

  it.each(['Delete', 'Backspace'])('removes the selected timeline clip with %s', async (key) => {
    await renderHydratedApp();
    fireEvent.keyDown(window, { key });
    expect(currentVideoClips()).toEqual([]);
  });
});
