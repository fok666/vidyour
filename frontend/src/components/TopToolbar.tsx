import { useEffect, useState, type KeyboardEvent } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { Capabilities, Project } from '../types/studio';

export interface TopToolbarProps {
  project?: Project | null;
  onBpmChange?: (bpm: number) => void;
  onResolutionChange?: (width: number, height: number) => void;
  onOpenExport?: () => void;
}

const RESOLUTIONS: ReadonlyArray<{ value: string; width: number; height: number; label: string }> = [
  { value: '1280x720', width: 1280, height: 720, label: '1280×720 (720p)' },
  { value: '1920x1080', width: 1920, height: 1080, label: '1920×1080 (1080p)' },
];

interface Badge {
  label: string;
  kind: 'active' | 'fallback';
}

function capabilityBadges(capabilities: Capabilities | null): Badge[] {
  if (!capabilities) return [];
  const badges: Badge[] = [];
  const integrations: Array<[boolean, string]> = [
    [capabilities.google_photos, 'Google Photos'],
    [capabilities.gemini, 'Gemini'],
    [capabilities.bedrock, 'Bedrock'],
    [capabilities.jamendo, 'Jamendo'],
    [capabilities.freesound, 'Freesound'],
    [capabilities.s3, 'AWS S3'],
  ];
  for (const [active, label] of integrations) {
    if (active) badges.push({ label, kind: 'active' });
  }
  if (capabilities.active_ai_provider === 'local' || (!capabilities.gemini && !capabilities.bedrock)) {
    badges.push({ label: 'Local AI', kind: 'fallback' });
  }
  if (!capabilities.s3 || capabilities.storage_backend === 'local') {
    badges.push({ label: 'Local Storage', kind: 'fallback' });
  }
  return badges;
}

function formatBpm(project: Project | null): string {
  return project ? String(project.bpm) : '';
}

export function TopToolbar(props: TopToolbarProps = {}) {
  const { onBpmChange, onResolutionChange, onOpenExport } = props;
  const storeProject = useStudioStore((state) => state.project);
  const capabilities = useStudioStore((state) => state.capabilities);
  const setProject = useStudioStore((state) => state.setProject);
  const project = props.project !== undefined ? props.project : storeProject;

  const [bpmDraft, setBpmDraft] = useState<string>(formatBpm(project));

  useEffect(() => {
    setBpmDraft(formatBpm(project));
  }, [project?.id, project?.bpm]);

  async function persist(changes: Partial<Project>) {
    if (!project) return;
    // Optimistic local update, then reconcile with the backend response.
    const current = useStudioStore.getState().project;
    if (current && current.id === project.id) setProject({ ...current, ...changes });
    try {
      const updated = await apiClient.updateProject(project.id, changes);
      const latest = useStudioStore.getState().project;
      if (updated && latest && latest.id === updated.id) setProject(updated);
    } catch (error) {
      console.error('Failed to update project', error);
    }
  }

  function commitBpm() {
    if (!project) return;
    const bpm = Number(bpmDraft);
    if (!Number.isFinite(bpm) || bpm <= 0) {
      setBpmDraft(formatBpm(project));
      return;
    }
    if (bpm === project.bpm) return;
    onBpmChange?.(bpm);
    void persist({ bpm });
  }

  function handleBpmKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    } else if (event.key === 'Escape') {
      setBpmDraft(formatBpm(project));
      event.currentTarget.blur();
    }
  }

  function handleResolutionChange(value: string) {
    const option = RESOLUTIONS.find((candidate) => candidate.value === value);
    if (!option || !project) return;
    onResolutionChange?.(option.width, option.height);
    void persist({ resolution_w: option.width, resolution_h: option.height });
  }

  const resolutionValue = project ? `${project.resolution_w}x${project.resolution_h}` : RESOLUTIONS[0].value;
  const badges = capabilityBadges(capabilities);

  return (
    <header className="top-toolbar" role="toolbar" aria-label="Studio toolbar">
      <div className="top-toolbar__brand">
        <span className="top-toolbar__title">VidYour</span>
        {project && <span className="top-toolbar__project">{project.name}</span>}
      </div>

      <ul className="top-toolbar__badges" aria-label="Capabilities">
        {badges.map((badge) => (
          <li
            key={badge.label}
            className={`badge badge--${badge.kind}`}
            data-kind={badge.kind}
            title={badge.kind === 'active' ? 'Active integration' : 'Local fallback'}
          >
            {badge.label}
          </li>
        ))}
      </ul>

      <div className="top-toolbar__controls">
        <label className="top-toolbar__field">
          <span>BPM</span>
          <input
            type="number"
            min={1}
            step="0.1"
            value={bpmDraft}
            disabled={!project}
            onChange={(event) => setBpmDraft(event.target.value)}
            onBlur={commitBpm}
            onKeyDown={handleBpmKeyDown}
          />
        </label>

        <label className="top-toolbar__field">
          <span>Resolution</span>
          <select
            value={resolutionValue}
            disabled={!project}
            onChange={(event) => handleResolutionChange(event.target.value)}
          >
            {RESOLUTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <button type="button" className="top-toolbar__export" onClick={() => onOpenExport?.()}>
          Export
        </button>
      </div>
    </header>
  );
}

export default TopToolbar;
