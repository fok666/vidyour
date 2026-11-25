import type {
  AudioCatalogTrack,
  Capabilities,
  Composition,
  DetectedPart,
  Job,
  MediaAsset,
  Project,
} from '../types/studio';

export interface AudioCatalogSearch {
  q?: string;
  bpm_min?: number;
  bpm_max?: number;
  provider?: string;
}

const API_BASE = '/api';

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

async function errorMessage(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const data = JSON.parse(text);
    if (data && data.detail !== undefined) {
      return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    }
  } catch {
    // fall through to raw text
  }
  return text || `HTTP ${response.status} ${response.statusText}`;
}

export function createApiClient(fetcher?: typeof fetch) {
  async function request<T>(path: string, init: RequestInit = { method: 'GET' }): Promise<T> {
    const doFetch = fetcher ?? globalThis.fetch;
    const response = await doFetch(`${API_BASE}${path}`, init);
    if (!response.ok) {
      throw new Error(await errorMessage(response));
    }
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  const enc = encodeURIComponent;
  const projectPath = (projectId: string, suffix = '') => `/projects/${enc(projectId)}${suffix}`;

  return {
    async getCapabilities(): Promise<Capabilities> {
      return request('/config/capabilities');
    },
    async listProjects(): Promise<Project[]> {
      return request('/projects');
    },
    async createProject(payload: { name: string }): Promise<Project> {
      return request('/projects', jsonInit('POST', payload));
    },
    async getProject(projectId: string): Promise<Project> {
      return request(projectPath(projectId));
    },
    async updateProject(projectId: string, changes: Partial<Project>): Promise<Project> {
      return request(projectPath(projectId), jsonInit('PATCH', changes));
    },
    async deleteProject(projectId: string): Promise<void> {
      await request(projectPath(projectId), { method: 'DELETE' });
    },
    async listProjectMedia(projectId: string): Promise<MediaAsset[]> {
      return request(projectPath(projectId, '/media'));
    },
    async deleteMedia(assetId: string): Promise<void> {
      await request(`/media/${enc(assetId)}`, { method: 'DELETE' });
    },
    async uploadMedia(projectId: string, files: File[]): Promise<MediaAsset[]> {
      const form = new FormData();
      for (const file of files) form.append('files', file, file.name);
      return request(projectPath(projectId, '/media/upload'), { method: 'POST', body: form });
    },
    async getGooglePhotosStatus(): Promise<{ configured: boolean; connected: boolean }> {
      return request('/gallery/google/status');
    },
    async getGooglePhotosAuthUrl(): Promise<{ auth_url: string }> {
      return request('/gallery/google/auth-url');
    },
    async createGooglePhotosSession(): Promise<{ session_id: string; picker_uri: string }> {
      return request('/gallery/google/sessions', { method: 'POST' });
    },
    async importGooglePhotosSession(sessionId: string, projectId: string): Promise<Job> {
      return request(
        `/gallery/google/sessions/${enc(sessionId)}/import?project_id=${enc(projectId)}`,
        { method: 'POST' },
      );
    },
    async searchAudioCatalog(search: AudioCatalogSearch = {}): Promise<AudioCatalogTrack[]> {
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(search)) {
        if (value !== undefined && value !== null && value !== '') params.append(key, String(value));
      }
      const query = params.toString();
      return request(`/audio-catalog/search${query ? `?${query}` : ''}`);
    },
    async importAudioCatalogTrack(
      projectId: string,
      payload: { track_id: string; provider: string },
    ): Promise<MediaAsset> {
      return request(projectPath(projectId, '/audio-catalog/import'), jsonInit('POST', payload));
    },
    async listParts(projectId: string): Promise<DetectedPart[]> {
      return request(projectPath(projectId, '/parts'));
    },
    async deletePart(partId: string): Promise<void> {
      await request(`/parts/${enc(partId)}`, { method: 'DELETE' });
    },
    async getComposition(projectId: string): Promise<Composition> {
      return request(projectPath(projectId, '/composition'));
    },
    async saveComposition(projectId: string, composition: Composition): Promise<Composition> {
      return request(projectPath(projectId, '/composition'), jsonInit('PUT', composition));
    },
    async autoAssembleComposition(projectId: string): Promise<Composition> {
      return request(projectPath(projectId, '/composition/auto-assemble'), { method: 'POST' });
    },
    async triggerAnalyze(projectId: string): Promise<Job> {
      return request(projectPath(projectId, '/analyze'), { method: 'POST' });
    },
    async triggerRender(projectId: string): Promise<Job> {
      return request(projectPath(projectId, '/render'), { method: 'POST' });
    },
    async getJob(jobId: string): Promise<Job> {
      return request(`/jobs/${enc(jobId)}`);
    },
    async listProjectJobs(projectId: string): Promise<Job[]> {
      return request(projectPath(projectId, '/jobs'));
    },
    getRenderDownloadUrl(jobId: string): string {
      return `${API_BASE}/renders/${enc(jobId)}/download`;
    },
  };
}

export const apiClient = createApiClient();
