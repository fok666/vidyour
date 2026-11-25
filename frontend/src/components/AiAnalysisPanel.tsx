import { useEffect, useRef, useState } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { Job, Project, ResultTarget } from '../types/studio';

export interface AiAnalysisPanelProps {
  project?: Project | null;
  onAnalyze?: () => void;
}

const PERSIST_DEBOUNCE_MS = 500;

const RESULT_TARGET_CHIPS: { value: ResultTarget; label: string }[] = [
  { value: 'perfect_loop', label: 'Perfect Loop' },
  { value: 'seamless_flow', label: 'Seamless Flow' },
  { value: 'high_energy', label: 'High Energy' },
  { value: 'chill_story', label: 'Chill Story' },
];

const DEFAULT_RESULT_TARGET: ResultTarget = 'seamless_flow';

const PRESET_TARGETS = new Set<string>(RESULT_TARGET_CHIPS.map((chip) => chip.value));

type GuidanceChanges = Pick<Partial<Project>, 'ai_context' | 'result_target'>;

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isActive(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running';
}

export function AiAnalysisPanel(props: AiAnalysisPanelProps = {}) {
  const { onAnalyze } = props;
  const storeProject = useStudioStore((state) => state.project);
  const jobs = useStudioStore((state) => state.jobs);
  const project = props.project !== undefined ? props.project : storeProject;

  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pendingRef = useRef<{ projectId: string; changes: GuidanceChanges } | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshedJobRef = useRef<string | null>(null);

  const job = jobId ? jobs[jobId] : undefined;
  const running = starting || (job ? isActive(job) : false);

  const cancelPersistTimer = (): void => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const flushPersist = async (): Promise<void> => {
    cancelPersistTimer();
    const pending = pendingRef.current;
    pendingRef.current = null;
    if (!pending) return;
    try {
      await apiClient.updateProject(pending.projectId, pending.changes);
    } catch (err) {
      setError(describeError(err));
    }
  };

  // Flush any pending guidance edits when unmounting.
  useEffect(
    () => () => {
      void flushPersist();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // When the analyze job finishes, refresh the detected parts bin.
  useEffect(() => {
    if (!job || job.status !== 'completed' || refreshedJobRef.current === job.id) return;
    refreshedJobRef.current = job.id;
    apiClient
      .listParts(job.project_id)
      .then((parts) => useStudioStore.getState().setParts(parts))
      .catch((err: unknown) => setError(describeError(err)));
  }, [job]);

  const updateGuidance = (changes: GuidanceChanges): void => {
    if (!project) return;
    const current = useStudioStore.getState().project;
    const base = current && current.id === project.id ? current : project;
    useStudioStore.getState().setProject({ ...base, ...changes });
    const previous = pendingRef.current?.projectId === project.id ? pendingRef.current.changes : {};
    pendingRef.current = { projectId: project.id, changes: { ...previous, ...changes } };
    cancelPersistTimer();
    timerRef.current = setTimeout(() => void flushPersist(), PERSIST_DEBOUNCE_MS);
  };

  const handleAnalyze = async (): Promise<void> => {
    if (!project || running) return;
    setError(null);
    setStarting(true);
    try {
      await flushPersist();
      const started = await apiClient.triggerAnalyze(project.id);
      const store = useStudioStore.getState();
      useStudioStore.setState({ jobs: { ...store.jobs, [started.id]: started } });
      setJobId(started.id);
      if (isActive(started)) await store.pollJob(started.id);
      onAnalyze?.();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setStarting(false);
    }
  };

  const resultTarget = project?.result_target ?? '';
  const customTarget = PRESET_TARGETS.has(resultTarget) ? '' : resultTarget;
  const disabled = !project;

  return (
    <section className="ai-analysis-panel" aria-label="AI Analysis">
      <h2>AI Guidance</h2>

      <label className="ai-analysis-panel__field">
        <span>Additional AI Context</span>
        <textarea
          aria-label="Additional AI Context"
          placeholder="e.g. Focus on sunset smiles and candid laughter"
          value={project?.ai_context ?? ''}
          disabled={disabled}
          onChange={(event) => updateGuidance({ ai_context: event.target.value })}
        />
      </label>

      <fieldset className="ai-analysis-panel__targets" disabled={disabled}>
        <legend>Result Target</legend>
        <div className="ai-analysis-panel__chips" role="group" aria-label="Result Target presets">
          {RESULT_TARGET_CHIPS.map((chip) => (
            <button
              key={chip.value}
              type="button"
              className={`chip${resultTarget === chip.value ? ' chip--active' : ''}`}
              aria-pressed={resultTarget === chip.value}
              onClick={() => updateGuidance({ result_target: chip.value })}
            >
              {chip.label}
            </button>
          ))}
        </div>
        <label className="ai-analysis-panel__field">
          <span>Custom Result Target</span>
          <input
            type="text"
            aria-label="Custom Result Target"
            placeholder="Describe your own target…"
            value={customTarget}
            onChange={(event) =>
              updateGuidance({
                result_target: event.target.value || DEFAULT_RESULT_TARGET,
              })
            }
          />
        </label>
      </fieldset>

      <button
        type="button"
        className="ai-analysis-panel__analyze"
        disabled={disabled || running}
        onClick={() => void handleAnalyze()}
      >
        {running ? 'Analyzing…' : 'Analyze Resources'}
      </button>

      {job && (
        <div className="ai-analysis-panel__progress" role="status" aria-live="polite">
          <progress max={100} value={job.progress} />
          <span className="ai-analysis-panel__percent">{`${Math.round(job.progress)}%`}</span>
          <span className="ai-analysis-panel__message">{job.message}</span>
          {job.status === 'failed' && (
            <span className="ai-analysis-panel__error" role="alert">
              {job.error ?? 'Analysis failed'}
            </span>
          )}
        </div>
      )}

      {error && (
        <p className="ai-analysis-panel__error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

export default AiAnalysisPanel;
