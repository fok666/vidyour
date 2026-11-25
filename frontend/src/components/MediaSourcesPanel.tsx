import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { Job, MediaAsset, Project } from '../types/studio';

export interface MediaSourcesPanelProps {
  project?: Project | null;
  assets?: MediaAsset[];
  onUpload?: (files: File[]) => void;
  onDelete?: (assetId: string) => void;
  onOpenAudioCatalog?: () => void;
  windowOpen?: typeof window.open;
}

const ACCEPTED_EXTENSIONS =
  '.mp4,.mov,.webm,.mkv,.jpg,.jpeg,.png,.webp,.heic,.mp3,.wav,.m4a,.ogg,.flac';

const KIND_LABELS: Record<MediaAsset['kind'], string> = {
  video: 'Video',
  photo: 'Photo',
  live_photo: 'Live Photo',
  audio: 'Audio',
};

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isActive(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running';
}

function refreshGooglePhotosStatus(onError: (message: string) => void): void {
  useStudioStore
    .getState()
    .loadGooglePhotosStatus()
    .catch((err: unknown) => onError(describeError(err)));
}

/** The import job created since `knownJobIds`, else the project's most recent import job. */
function findImportJob(
  jobs: Record<string, Job>,
  knownJobIds: Set<string>,
  projectId: string,
): Job | undefined {
  const imports = Object.values(jobs).filter((job) => job.kind === 'import');
  return (
    imports.find((job) => !knownJobIds.has(job.id)) ??
    imports
      .filter((job) => job.project_id === projectId)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
  );
}

export function MediaSourcesPanel(props: MediaSourcesPanelProps = {}) {
  const { onUpload, onDelete, onOpenAudioCatalog } = props;
  const storeProject = useStudioStore((state) => state.project);
  const storeAssets = useStudioStore((state) => state.assets);
  const capabilities = useStudioStore((state) => state.capabilities);
  const googleStatus = useStudioStore((state) => state.googlePhotosStatus);
  const jobs = useStudioStore((state) => state.jobs);

  const project = props.project !== undefined ? props.project : storeProject;
  const assets = props.assets ?? storeAssets;

  const [importJobId, setImportJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refreshedJobRef = useRef<string | null>(null);

  const configured = Boolean(capabilities?.google_photos);
  const connected = Boolean(googleStatus?.connected);
  const importJob = importJobId ? jobs[importJobId] : undefined;

  // Load connection status once Google Photos is configured.
  useEffect(() => {
    if (!configured || googleStatus) return;
    refreshGooglePhotosStatus(setError);
  }, [configured, googleStatus]);

  // The OAuth callback closes its popup via postMessage; refresh status then.
  useEffect(() => {
    if (!configured) return;
    const handler = () => refreshGooglePhotosStatus(setError);
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [configured]);

  // Refresh the asset list once an import job completes.
  useEffect(() => {
    if (!importJob || importJob.status !== 'completed' || !project) return;
    if (refreshedJobRef.current === importJob.id) return;
    refreshedJobRef.current = importJob.id;
    apiClient
      .listProjectMedia(project.id)
      .then((list) => useStudioStore.getState().setAssets(list))
      .catch((err: unknown) => setError(describeError(err)));
  }, [importJob, project]);

  const openWindow = (url: string, name: string) => {
    const open = props.windowOpen ?? window.open;
    open(url, name, 'popup,width=600,height=720');
  };

  async function handleGooglePhotos() {
    if (!configured || busy) return;
    setError(null);
    setBusy(true);
    const store = useStudioStore.getState();
    try {
      if (!connected) {
        await store.loadGooglePhotosAuthUrl();
        const authUrl = useStudioStore.getState().googlePhotosAuthUrl;
        if (authUrl) openWindow(authUrl, 'google-photos-auth');
        return;
      }
      if (!project) {
        setError('Select a project before importing from Google Photos.');
        return;
      }
      await store.createGooglePhotosSession();
      const session = useStudioStore.getState().googlePhotosSession;
      if (!session) return;
      openWindow(session.picker_uri, 'google-photos-picker');
      // The backend waits for the Picker selection (mediaItemsSet) before downloading.
      const before = new Set(Object.keys(useStudioStore.getState().jobs));
      await store.importGooglePhotosSession(session.session_id, project.id);
      const created = findImportJob(useStudioStore.getState().jobs, before, project.id);
      if (created) setImportJobId(created.id);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function uploadFiles(files: File[]) {
    if (files.length === 0) return;
    if (!project) {
      setError('Select a project before uploading files.');
      return;
    }
    setError(null);
    setUploading(true);
    try {
      const uploaded = await apiClient.uploadMedia(project.id, files);
      const current = useStudioStore.getState().assets;
      const ids = new Set(uploaded.map((asset) => asset.id));
      useStudioStore
        .getState()
        .setAssets([...current.filter((asset) => !ids.has(asset.id)), ...uploaded]);
      onUpload?.(files);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setUploading(false);
    }
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    void uploadFiles(files);
    event.target.value = '';
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragOver(false);
    void uploadFiles(Array.from(event.dataTransfer?.files ?? []));
  }

  async function handleDelete(assetId: string) {
    setError(null);
    try {
      await apiClient.deleteMedia(assetId);
      const store = useStudioStore.getState();
      store.setAssets(store.assets.filter((asset) => asset.id !== assetId));
      onDelete?.(assetId);
    } catch (err) {
      setError(describeError(err));
    }
  }

  const googleLabel = !configured
    ? 'Not configured'
    : connected
      ? 'Connected'
      : 'Not connected';

  return (
    <section className="media-sources-panel" aria-label="Media & Audio Sources">
      <h2>Media &amp; Audio Sources</h2>

      <div className="media-sources-google">
        <button
          type="button"
          onClick={() => void handleGooglePhotos()}
          disabled={!configured || busy || (importJob ? isActive(importJob) : false)}
        >
          Google Photos
        </button>
        <span className="media-sources-google-status">{googleLabel}</span>
        {importJob && (
          <div className="media-sources-import-progress" role="status">
            <progress max={100} value={importJob.progress} />
            <span>{Math.round(importJob.progress)}%</span>
            <span>{importJob.message}</span>
            {importJob.status === 'failed' && importJob.error && (
              <span className="error">{importJob.error}</span>
            )}
          </div>
        )}
      </div>

      <div
        className={`media-sources-dropzone${dragOver ? ' drag-over' : ''}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        <label>
          Upload Files
          <input
            type="file"
            multiple
            accept={ACCEPTED_EXTENSIONS}
            onChange={handleFileChange}
            disabled={uploading}
          />
        </label>
        <p>Drop videos, photos, Live Photo pairs, or audio files here.</p>
        {uploading && <span role="status">Uploading…</span>}
      </div>

      <button type="button" onClick={() => onOpenAudioCatalog?.()}>
        Audio Catalog
      </button>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <ul className="media-sources-assets">
        {assets.map((asset) => (
          <li key={asset.id}>
            <span className="asset-kind">{KIND_LABELS[asset.kind] ?? asset.kind}</span>
            <span className="asset-name">{asset.filename}</span>
            <button
              type="button"
              aria-label={`Delete ${asset.filename}`}
              onClick={() => void handleDelete(asset.id)}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default MediaSourcesPanel;
