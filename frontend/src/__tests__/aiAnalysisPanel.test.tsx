import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AiAnalysisPanel } from '../components/AiAnalysisPanel';
import { useStudioStore } from '../store/useStudioStore';
import type { DetectedPart, Job, Project } from '../types/studio';

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
    result_target: 'seamless_flow',
    suggested_sequence: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-a1',
    project_id: 'p1',
    kind: 'analyze',
    status: 'running',
    progress: 40,
    message: 'Slicing clips',
    result: {},
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makePart(overrides: Partial<DetectedPart> = {}): DetectedPart {
  return {
    id: 'part-a1',
    project_id: 'p1',
    asset_id: 'asset-a1',
    part_type: 'visual_clip',
    start_sec: 1,
    end_sec: 3,
    beat_length: 4,
    label: 'Sunset',
    score: 0.9,
    ai_reason: 'Warm light',
    suggested_effect: 'slow_zoom',
    created_at: NOW,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
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
let analyzeJob: Job;
let polledJob: Job;
let partsResponse: DetectedPart[];
let triggerError: string | null;
let pollError: string | null;
let updateProjectError: string | null;

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method });
      const path = url.split('?')[0];
      if (method === 'POST' && path.endsWith('/api/projects/p1/analyze')) {
        if (triggerError) return jsonResponse({ detail: triggerError }, 500);
        return jsonResponse(analyzeJob);
      }
      if (method === 'GET' && path.endsWith('/api/jobs/job-a1')) {
        if (pollError) return jsonResponse({ detail: pollError }, 500);
        return jsonResponse(polledJob);
      }
      if ((method === 'PATCH' || method === 'PUT') && path.endsWith('/api/projects/p1')) {
        if (updateProjectError) return jsonResponse({ detail: updateProjectError }, 500);
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        return jsonResponse({ ...useStudioStore.getState().project, ...body });
      }
      if (method === 'GET' && path.endsWith('/api/projects/p1/jobs')) {
        return jsonResponse([]);
      }
      if (method === 'GET' && path.endsWith('/api/projects/p1/parts')) {
        return jsonResponse(partsResponse);
      }
      return jsonResponse({});
    }),
  );
}

describe('AiAnalysisPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    calls = [];
    analyzeJob = makeJob();
    polledJob = makeJob();
    partsResponse = [];
    triggerError = null;
    pollError = null;
    updateProjectError = null;
    installFetch();
    useStudioStore.setState({ project: makeProject(), jobs: {} });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    useStudioStore.setState({ project: null, jobs: {} });
  });

  it('updates project.ai_context in the store when typing Additional AI Context', async () => {
    // GIVEN
    render(<AiAnalysisPanel />);
    const input = screen.getByLabelText(/additional ai context/i);

    // WHEN
    fireEvent.change(input, { target: { value: 'Focus on sunset smiles' } });

    // THEN
    await waitFor(() =>
      expect(useStudioStore.getState().project?.ai_context).toBe('Focus on sunset smiles'),
    );
  });

  it('uses an explicit project override for displayed and persisted guidance', async () => {
    // GIVEN
    const override = makeProject({
      id: 'override-project',
      name: 'Override',
      ai_context: 'Override context',
    });
    useStudioStore.setState({ project: makeProject() });
    render(<AiAnalysisPanel project={override} />);
    const input = screen.getByLabelText(/additional ai context/i);
    expect((input as HTMLTextAreaElement).value).toBe('Override context');

    // WHEN
    fireEvent.change(input, { target: { value: 'Use the override project' } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    // THEN
    expect(useStudioStore.getState().project).toMatchObject({
      id: 'override-project',
      ai_context: 'Use the override project',
    });
    expect(
      calls.some(
        (call) =>
          call.method === 'PATCH' && call.url.endsWith('/api/projects/override-project'),
      ),
    ).toBe(true);
  });

  it('disables analysis and guidance controls when there is no project', () => {
    // GIVEN
    render(<AiAnalysisPanel project={null} />);

    // THEN
    expect(screen.getByLabelText(/additional ai context/i).matches(':disabled')).toBe(true);
    expect(screen.getByLabelText(/custom result target/i).matches(':disabled')).toBe(true);
    expect(
      (screen.getByRole('button', { name: /analyze resources/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(calls).toEqual([]);
  });

  it('sets project.result_target to perfect_loop when the Perfect Loop chip is clicked', async () => {
    // GIVEN
    useStudioStore.setState({ project: makeProject({ result_target: 'seamless_flow' }) });
    render(<AiAnalysisPanel />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /perfect loop/i }));

    // THEN
    await waitFor(() =>
      expect(useStudioStore.getState().project?.result_target).toBe('perfect_loop'),
    );
  });

  it('sets project.result_target to the custom Result Target text', async () => {
    // GIVEN
    render(<AiAnalysisPanel />);
    const input = screen.getByLabelText(/custom result target/i);

    // WHEN
    fireEvent.change(input, { target: { value: 'Dreamy slow-motion travel montage' } });

    // THEN
    await waitFor(() =>
      expect(useStudioStore.getState().project?.result_target).toBe(
        'Dreamy slow-motion travel montage',
      ),
    );
  });

  it('POSTs /api/projects/p1/analyze and shows the job progress and message', async () => {
    // GIVEN
    render(<AiAnalysisPanel />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));

    // THEN
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === 'POST' && c.url.endsWith('/api/projects/p1/analyze')),
      ).toBe(true),
    );
    expect(await screen.findByText('40%')).toBeTruthy();
    expect(screen.getByText(/Slicing clips/)).toBeTruthy();
  });

  it('calls the optional onAnalyze callback after starting a completed job', async () => {
    // GIVEN
    analyzeJob = makeJob({ status: 'completed', progress: 100, message: 'Analysis complete' });
    const onAnalyze = vi.fn();
    render(<AiAnalysisPanel onAnalyze={onAnalyze} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));

    // THEN
    await waitFor(() => expect(onAnalyze).toHaveBeenCalledOnce());
  });

  it('formats a non-Error callback failure as an alert message', async () => {
    // GIVEN
    analyzeJob = makeJob({ status: 'completed', progress: 100, message: 'Analysis complete' });
    render(
      <AiAnalysisPanel
        onAnalyze={() => {
          throw 'callback failed';
        }}
      />,
    );

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('callback failed');
  });

  it('polls GET /api/jobs/job-a1 and updates the visible progress to 75%', async () => {
    // GIVEN
    render(<AiAnalysisPanel />);
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));
    expect(await screen.findByText('40%')).toBeTruthy();
    expect(screen.getByText(/Slicing clips/)).toBeTruthy();
    polledJob = makeJob({ progress: 75, message: 'Extracting thumbnails' });

    // WHEN
    await act(async () => {
      await vi.advanceTimersToNextTimerAsync();
    });

    // THEN
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === 'GET' && c.url.endsWith('/api/jobs/job-a1')),
      ).toBe(true),
    );
    expect(await screen.findByText('75%')).toBeTruthy();
    expect(screen.getByText(/Extracting thumbnails/)).toBeTruthy();
    expect(screen.queryByText('40%')).toBeNull();
    expect(screen.queryByText(/Slicing clips/)).toBeNull();
  });

  it('refreshes detected parts in the store when the analyze job completes', async () => {
    // GIVEN
    const refreshedPart = makePart({ id: 'part-refreshed' });
    partsResponse = [refreshedPart];
    useStudioStore.setState({ parts: [makePart({ id: 'part-stale' })] });
    render(<AiAnalysisPanel />);
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));
    expect(await screen.findByText('40%')).toBeTruthy();
    polledJob = makeJob({ status: 'completed', progress: 100, message: 'Analysis complete' });

    // WHEN
    await act(async () => {
      await vi.advanceTimersToNextTimerAsync();
    });

    // THEN
    await waitFor(() => {
      expect(
        calls.some((c) => c.method === 'GET' && c.url.endsWith('/api/projects/p1/parts')),
      ).toBe(true);
      expect(useStudioStore.getState().parts).toEqual([refreshedPart]);
    });
    expect(useStudioStore.getState().jobs['job-a1']?.status).toBe('completed');
    expect(useStudioStore.getState().parts).not.toContainEqual(makePart({ id: 'part-stale' }));
  });

  it('shows the failed analyze job error in an alert', async () => {
    // GIVEN
    polledJob = makeJob({
      status: 'failed',
      error: 'Analysis provider failed',
      message: 'Analysis failed',
    });
    render(<AiAnalysisPanel />);
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));
    expect(await screen.findByText('40%')).toBeTruthy();

    // WHEN
    await act(async () => {
      await vi.advanceTimersToNextTimerAsync();
    });

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Analysis provider failed');
    expect(useStudioStore.getState().jobs['job-a1']).toMatchObject({
      status: 'failed',
      error: 'Analysis provider failed',
    });
  });

  it('shows a trigger error in an alert and does not add a job to the store', async () => {
    // GIVEN
    triggerError = 'Analyze request failed';
    render(<AiAnalysisPanel />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Analyze request failed');
    expect(useStudioStore.getState().jobs).toEqual({});
  });

  it('marks a job failed and shows the polling error in an alert when polling fails', async () => {
    // GIVEN
    pollError = 'Job polling failed';
    render(<AiAnalysisPanel />);
    fireEvent.click(screen.getByRole('button', { name: /analyze resources/i }));
    expect(await screen.findByText('40%')).toBeTruthy();

    // WHEN
    await act(async () => {
      await vi.advanceTimersToNextTimerAsync();
    });

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Job polling failed');
    expect(useStudioStore.getState().jobs['job-a1']).toMatchObject({
      status: 'failed',
      error: 'Job polling failed',
    });
  });

  it('shows a guidance persistence error in an alert', async () => {
    // GIVEN
    updateProjectError = 'Could not save guidance';
    render(<AiAnalysisPanel />);
    fireEvent.change(screen.getByLabelText(/additional ai context/i), {
      target: { value: 'Save this guidance' },
    });

    // WHEN
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    // THEN
    expect((await screen.findByRole('alert')).textContent).toContain('Could not save guidance');
    expect(useStudioStore.getState().project?.ai_context).toBe('Save this guidance');
  });
});
