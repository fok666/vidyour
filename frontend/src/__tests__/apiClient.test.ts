import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AudioCatalogTrack,
  Capabilities,
  Composition,
  Job,
  MediaAsset,
  Project,
} from '../types/studio';

type FetchMock = ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch(body: unknown, status = 200): FetchMock {
  const mock = vi.fn(async () => jsonResponse(body, status));
  vi.stubGlobal('fetch', mock);
  return mock;
}

async function loadClient() {
  vi.resetModules();
  const mod = await import('../api/client');
  return mod.apiClient;
}

function requestOf(mock: FetchMock, index = 0): { method: string; path: string; init: RequestInit } {
  const [input, init = {}] = mock.mock.calls[index] as [RequestInfo | URL, RequestInit?];
  const raw =
    typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
  const url = new URL(raw, 'http://localhost');
  const method = (
    init.method ?? (typeof input === 'object' && 'method' in input ? input.method : 'GET')
  ).toUpperCase();
  return { method, path: `${url.pathname}${url.search}`, init };
}

const project = (id: string, name: string): Project => ({
  id,
  name,
  bpm: 120,
  beat_offset_sec: 0,
  resolution_w: 1920,
  resolution_h: 1080,
  fps: 30,
  ai_context: '',
  result_target: 'perfect_loop',
  suggested_sequence: [],
  created_at: '2026-11-22T00:00:00Z',
  updated_at: '2026-11-22T00:00:00Z',
});

const asset = (id: string): MediaAsset => ({
  id,
  project_id: 'p1',
  kind: 'video',
  source: 'upload',
  filename: `${id}.mp4`,
  local_path: `media/${id}.mp4`,
  duration_sec: 4,
  has_audio: true,
  metadata: {},
  created_at: '2026-11-22T00:00:00Z',
});

const job = (id: string, kind: Job['kind'], extra: Partial<Job> = {}): Job => ({
  id,
  project_id: 'p1',
  kind,
  status: 'queued',
  progress: 0,
  message: '',
  result: {},
  created_at: '2026-11-22T00:00:00Z',
  updated_at: '2026-11-22T00:00:00Z',
  ...extra,
});

describe('apiClient', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('getCapabilities requests GET /api/config/capabilities and returns Capabilities', async () => {
    const caps: Capabilities = {
      google_photos: true,
      gemini: false,
      bedrock: true,
      jamendo: true,
      freesound: false,
      s3: false,
      active_ai_provider: 'bedrock',
      storage_backend: 'local',
    };
    const fetchMock = stubFetch(caps);
    const apiClient = await loadClient();

    const result = await apiClient.getCapabilities();

    expect(result).toEqual(caps);
    expect(requestOf(fetchMock)).toMatchObject({ method: 'GET', path: '/api/config/capabilities' });
  });

  it('listProjects requests GET /api/projects and returns 2 projects', async () => {
    const projects = [project('p1', 'One'), project('p2', 'Two')];
    const fetchMock = stubFetch(projects);
    const apiClient = await loadClient();

    const result = await apiClient.listProjects();

    expect(result).toHaveLength(2);
    expect(result).toEqual(projects);
    expect(requestOf(fetchMock)).toMatchObject({ method: 'GET', path: '/api/projects' });
  });

  it('createProject POSTs JSON body to /api/projects and returns the Project', async () => {
    const created = project('p9', 'Trip');
    const fetchMock = stubFetch(created, 201);
    const apiClient = await loadClient();

    const result = await apiClient.createProject({ name: 'Trip' });

    expect(result).toEqual(created);
    const req = requestOf(fetchMock);
    expect(req).toMatchObject({ method: 'POST', path: '/api/projects' });
    expect(req.init.body).toBe('{"name":"Trip"}');
  });

  it('listProjectMedia requests GET /api/projects/p1/media and returns assets', async () => {
    const assets = [asset('a1'), asset('a2')];
    const fetchMock = stubFetch(assets);
    const apiClient = await loadClient();

    const result = await apiClient.listProjectMedia('p1');

    expect(result).toEqual(assets);
    expect(requestOf(fetchMock)).toMatchObject({ method: 'GET', path: '/api/projects/p1/media' });
  });

  it('uploadMedia POSTs FormData with the file to /api/projects/p1/media/upload', async () => {
    const created = [asset('a3')];
    const fetchMock = stubFetch(created, 201);
    const apiClient = await loadClient();
    const file = new File(['abc'], 'clip.mp4', { type: 'video/mp4' });

    const result = await apiClient.uploadMedia('p1', [file]);

    expect(result).toEqual(created);
    const req = requestOf(fetchMock);
    expect(req).toMatchObject({ method: 'POST', path: '/api/projects/p1/media/upload' });
    expect(req.init.body).toBeInstanceOf(FormData);
    const names = Array.from((req.init.body as FormData).values()).map((v) =>
      typeof v === 'string' ? v : v.name,
    );
    expect(names).toContain('clip.mp4');
  });

  it('getGooglePhotosStatus requests GET /api/gallery/google/status', async () => {
    const fetchMock = stubFetch({ configured: true, connected: false });
    const apiClient = await loadClient();

    const result = await apiClient.getGooglePhotosStatus();

    expect(result).toEqual({ configured: true, connected: false });
    expect(requestOf(fetchMock)).toMatchObject({
      method: 'GET',
      path: '/api/gallery/google/status',
    });
  });

  it('getGooglePhotosAuthUrl requests GET /api/gallery/google/auth-url', async () => {
    const body = { auth_url: 'https://accounts.google.com/o/oauth2/v2/auth' };
    const fetchMock = stubFetch(body);
    const apiClient = await loadClient();

    const result = await apiClient.getGooglePhotosAuthUrl();

    expect(result).toEqual(body);
    expect(requestOf(fetchMock)).toMatchObject({
      method: 'GET',
      path: '/api/gallery/google/auth-url',
    });
  });

  it('createGooglePhotosSession requests POST /api/gallery/google/sessions', async () => {
    const body = { session_id: 's1', picker_uri: 'https://photos.google.com/picker/s1' };
    const fetchMock = stubFetch(body);
    const apiClient = await loadClient();

    const result = await apiClient.createGooglePhotosSession();

    expect(result).toEqual(body);
    expect(requestOf(fetchMock)).toMatchObject({
      method: 'POST',
      path: '/api/gallery/google/sessions',
    });
  });

  it('importGooglePhotosSession POSTs to /api/gallery/google/sessions/s1/import?project_id=p1', async () => {
    const queued = job('job-i1', 'import');
    const fetchMock = stubFetch(queued, 202);
    const apiClient = await loadClient();

    const result = await apiClient.importGooglePhotosSession('s1', 'p1');

    expect(result).toEqual(queued);
    expect(requestOf(fetchMock)).toMatchObject({
      method: 'POST',
      path: '/api/gallery/google/sessions/s1/import?project_id=p1',
    });
  });

  it('searchAudioCatalog requests GET /api/audio-catalog/search with query params', async () => {
    const tracks: AudioCatalogTrack[] = [
      { id: 't1', provider: 'jamendo', title: 'Deep', artist: 'A', bpm: 124, duration_sec: 180 },
    ];
    const fetchMock = stubFetch(tracks);
    const apiClient = await loadClient();

    const result = await apiClient.searchAudioCatalog({
      q: 'house',
      bpm_min: 115,
      bpm_max: 130,
      provider: 'all',
    });

    expect(result).toEqual(tracks);
    expect(requestOf(fetchMock)).toMatchObject({
      method: 'GET',
      path: '/api/audio-catalog/search?q=house&bpm_min=115&bpm_max=130&provider=all',
    });
  });

  it('omits optional audio search parameters when they are not supplied', async () => {
    const fetchMock = stubFetch([]);
    const apiClient = await loadClient();

    await apiClient.searchAudioCatalog({ q: 'house', bpm_min: undefined, provider: '' });

    expect(requestOf(fetchMock)).toMatchObject({
      method: 'GET',
      path: '/api/audio-catalog/search?q=house',
    });
  });

  it('saveComposition PUTs serialized JSON to /api/projects/p1/composition', async () => {
    const composition: Composition = {
      project_id: 'p1',
      snap_subdivision: 'beat',
      video_clips: [],
      audio_clips: [],
      updated_at: '2026-11-22T00:00:00Z',
    };
    const fetchMock = stubFetch(composition);
    const apiClient = await loadClient();

    const result = await apiClient.saveComposition('p1', composition);

    expect(result).toEqual(composition);
    const req = requestOf(fetchMock);
    expect(req).toMatchObject({ method: 'PUT', path: '/api/projects/p1/composition' });
    expect(JSON.parse(req.init.body as string)).toEqual(composition);
  });

  it('triggerAnalyze POSTs to /api/projects/p1/analyze and returns the Job', async () => {
    const queued = job('job-a1', 'analyze');
    const fetchMock = stubFetch(queued, 202);
    const apiClient = await loadClient();

    const result = await apiClient.triggerAnalyze('p1');

    expect(result).toEqual(queued);
    expect(requestOf(fetchMock)).toMatchObject({ method: 'POST', path: '/api/projects/p1/analyze' });
  });

  it('triggerRender POSTs to /api/projects/p1/render and returns the Job', async () => {
    const queued = job('job-r1', 'render');
    const fetchMock = stubFetch(queued, 202);
    const apiClient = await loadClient();

    const result = await apiClient.triggerRender('p1');

    expect(result).toEqual(queued);
    expect(requestOf(fetchMock)).toMatchObject({ method: 'POST', path: '/api/projects/p1/render' });
  });

  it('getJob requests GET /api/jobs/job-a1 and returns progress and message', async () => {
    const running = job('job-a1', 'analyze', {
      status: 'running',
      progress: 75,
      message: 'Extracting thumbnails',
    });
    const fetchMock = stubFetch(running);
    const apiClient = await loadClient();

    const result = await apiClient.getJob('job-a1');

    expect(result.progress).toBe(75);
    expect(result.message).toBe('Extracting thumbnails');
    expect(requestOf(fetchMock)).toMatchObject({ method: 'GET', path: '/api/jobs/job-a1' });
  });

  it('resolves an empty successful response body as undefined', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 200 })));
    const apiClient = await loadClient();

    await expect(apiClient.deleteProject('p1')).resolves.toBeUndefined();
  });

  it('uploadMedia rejects with the backend detail on HTTP 400', async () => {
    stubFetch({ detail: 'Unsupported file extension .exe' }, 400);
    const apiClient = await loadClient();
    const badFile = new File(['MZ'], 'virus.exe', { type: 'application/octet-stream' });

    const outcome = apiClient.uploadMedia('p1', [badFile]);

    await expect(outcome).rejects.toThrow('Unsupported file extension .exe');
  });

  it('uses the raw response text when an error body is not valid JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('proxy said nope', { status: 502, statusText: 'Bad Gateway' })),
    );
    const apiClient = await loadClient();

    await expect(apiClient.getCapabilities()).rejects.toThrow('proxy said nope');
  });

  it('serializes a non-string detail from a JSON error body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: { reason: 'maintenance' } }, 503)),
    );
    const apiClient = await loadClient();

    await expect(apiClient.getCapabilities()).rejects.toThrow('{"reason":"maintenance"}');
  });
});
