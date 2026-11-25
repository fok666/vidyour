import { useState } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { Job } from '../types/studio';

export interface ExportModalProps {
  open?: boolean;
  job?: Job | null;
  onClose?: () => void;
  onRender?: () => void;
}

function isActive(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running';
}

function resultString(job: Job, key: string): string | null {
  const value = job.result?.[key];
  return typeof value === 'string' && value !== '' ? value : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function failureMessage(job: Job | null): string | null {
  if (job?.status !== 'failed') return null;
  return job.error || job.message || 'Render failed.';
}

function rememberJob(job: Job): void {
  const { jobs } = useStudioStore.getState();
  useStudioStore.setState({ jobs: { ...jobs, [job.id]: job } });
}

function withDownloadFlag(url: string): string {
  // Local fallback endpoint serves inline by default; ?download=1 forces attachment.
  if (!url.includes('/api/renders/')) return url;
  return `${url}${url.includes('?') ? '&' : '?'}download=1`;
}

export function ExportModal(props: ExportModalProps = {}) {
  const { open = false, job: jobProp, onClose, onRender } = props;
  const project = useStudioStore((state) => state.project);
  const jobs = useStudioStore((state) => state.jobs);
  const pollJob = useStudioStore((state) => state.pollJob);

  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const job: Job | null = jobProp ?? (jobId ? (jobs[jobId] ?? null) : null);
  const busy = starting || (job !== null && isActive(job));

  const handleRender = async (): Promise<void> => {
    onRender?.();
    if (!project) {
      setError('Open a project before rendering.');
      return;
    }
    setError(null);
    setStarting(true);
    try {
      const started = await apiClient.triggerRender(project.id);
      rememberJob(started);
      setJobId(started.id);
      if (isActive(started)) await pollJob(started.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  };

  const downloadUrl = job ? resultString(job, 'download_url') : null;
  const storageBackend = job ? resultString(job, 'storage_backend') : null;
  const failure = error ?? failureMessage(job);

  return (
    <div className="modal-backdrop export-backdrop">
      <div className="modal export-modal" role="dialog" aria-modal="true" aria-labelledby="export-title">
        <header className="modal__header">
          <h2 id="export-title">Render &amp; Export</h2>
          {onClose && (
            <button type="button" aria-label="Close export" onClick={onClose}>
              ×
            </button>
          )}
        </header>

        <p className="export-modal__summary">
          H.264/AAC MP4
          {project ? ` · ${project.resolution_w}×${project.resolution_h} · ${project.fps} fps` : ''}
        </p>

        <button type="button" className="export-modal__render" disabled={busy} onClick={() => void handleRender()}>
          Render &amp; Export
        </button>

        {job && isActive(job) && (
          <div className="export-modal__progress">
            <progress max={100} value={job.progress} aria-label="Render progress" />
            <span className="export-modal__percent">{Math.round(job.progress)}%</span>
            {job.message && <span className="export-modal__message">{job.message}</span>}
          </div>
        )}

        {failure && (
          <div className="export-modal__error" role="alert">
            <strong>Render failed:</strong> <span>{failure}</span>
            <p>Check the composition and backend logs, then try rendering again.</p>
          </div>
        )}

        {job?.status === 'completed' && (
          <div className="export-modal__result">
            {job.message && <p className="export-modal__message">{job.message}</p>}
            {storageBackend && (
              <span className={`badge badge--${storageBackend}`}>
                {storageBackend === 's3' ? 'AWS S3' : 'Local Storage'}
              </span>
            )}
            {downloadUrl ? (
              <>
                <video className="export-modal__video" src={downloadUrl} controls preload="metadata" />
                <a className="export-modal__download" href={withDownloadFlag(downloadUrl)} download>
                  Download MP4
                </a>
              </>
            ) : (
              <p role="alert">Render completed but no download URL was returned.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default ExportModal;
