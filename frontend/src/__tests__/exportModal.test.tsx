import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ExportModal } from '../components/ExportModal';
import { useStudioStore } from '../store/useStudioStore';
import type { Job, Project } from '../types/studio';

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

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    kind: 'render',
    status: 'running',
    progress: 45,
    message: 'Encoding H.264',
    result: {},
    error: null,
    created_at: NOW,
    updated_at: NOW,
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
let pollJob: Job;

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method });
      const path = url.split('?')[0];
      if (method === 'POST' && path.endsWith('/api/projects/p1/render')) {
        return jsonResponse(makeJob());
      }
      if (method === 'GET' && path.endsWith('/api/jobs/j1')) {
        return jsonResponse(pollJob);
      }
      if (method === 'GET' && path.endsWith('/api/projects/p1/jobs')) {
        return jsonResponse([]);
      }
      return jsonResponse({});
    }),
  );
}

function clickRender(): void {
  // Non-throwing lookup: a missing button must surface as a failed THEN
  // expectation (no POST / no progress), not as a lookup error in the WHEN.
  const button = screen.queryByRole('button', { name: /render\s*&\s*export/i });
  if (button) {
    fireEvent.click(button);
  }
}

async function settleRunningJob(): Promise<void> {
  // GIVEN precondition for the polling test: wait for the running job to show,
  // without throwing, so an absent render state is reported by the THEN checks.
  await waitFor(() => {
    expect(screen.queryByText(/45%/)).not.toBeNull();
    expect(screen.queryByText(/Encoding H\.264/)).not.toBeNull();
  }).catch(() => undefined);
}

describe('ExportModal', () => {
  beforeEach(() => {
    calls = [];
    pollJob = makeJob();
    installFetch();
    useStudioStore.setState({ project: makeProject(), jobs: {} });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useStudioStore.setState({ project: null, jobs: {} });
  });

  it('POSTs the render request and shows the returned progress and message', async () => {
    // GIVEN
    render(<ExportModal open />);

    // WHEN
    clickRender();

    // THEN
    await waitFor(() => {
      expect(
        calls.some((call) => call.method === 'POST' && call.url.endsWith('/api/projects/p1/render')),
      ).toBe(true);
    });
    expect(await screen.findByText(/45%/)).toBeTruthy();
    expect(await screen.findByText(/Encoding H\.264/)).toBeTruthy();
  });

  it('polls GET /api/jobs/j1 on the next timer tick and updates progress', async () => {
    // GIVEN
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
      shouldAdvanceTime: true,
    });
    render(<ExportModal open />);
    clickRender();
    await settleRunningJob();
    pollJob = makeJob({ progress: 80, message: 'Uploading render' });
    const getsBefore = calls.filter(
      (call) => call.method === 'GET' && call.url.endsWith('/api/jobs/j1'),
    ).length;

    // WHEN
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    // THEN
    await waitFor(() => {
      const gets = calls.filter(
        (call) => call.method === 'GET' && call.url.endsWith('/api/jobs/j1'),
      ).length;
      expect(gets).toBeGreaterThan(getsBefore);
    });
    expect(await screen.findByText(/80%/)).toBeTruthy();
    expect(await screen.findByText(/Uploading render/)).toBeTruthy();
  });

  it('shows inline video, download link and AWS S3 badge for a completed render', () => {
    // GIVEN
    const job = makeJob({
      status: 'completed',
      progress: 100,
      message: 'Render complete',
      result: {
        storage_backend: 's3',
        download_url: 'https://s3.example/p1/j1.mp4',
        s3_key: 'p1/j1.mp4',
      },
    });

    // WHEN
    const { container } = render(<ExportModal open job={job} />);

    // THEN
    const video = container.querySelector('video');
    expect(video).not.toBeNull();
    expect(video!.getAttribute('src')).toBe('https://s3.example/p1/j1.mp4');
    const link = screen.getByRole('link', { name: /download/i });
    expect(link.getAttribute('href')).toBe('https://s3.example/p1/j1.mp4');
    expect(screen.getByText('AWS S3')).toBeTruthy();
  });

  it('shows the failure error inside an alert for a failed render', () => {
    // GIVEN
    const job = makeJob({
      status: 'failed',
      progress: 45,
      message: 'Encoding H.264',
      error: 'FFmpeg failed: invalid filter graph',
    });

    // WHEN
    render(<ExportModal open job={job} />);

    // THEN
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText(/FFmpeg failed: invalid filter graph/)).toBeTruthy();
  });

  it('alerts and does not send a render request when no project is open', async () => {
    // GIVEN
    useStudioStore.setState({ project: null });
    render(<ExportModal open />);

    // WHEN
    clickRender();

    // THEN
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Open a project before rendering.')).toBeTruthy();
    expect(calls.some((call) => call.method === 'POST' && call.url.endsWith('/render'))).toBe(
      false,
    );
  });

  it('alerts with the API error when starting a render is rejected', async () => {
    // GIVEN
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        calls.push({
          url: String(input),
          method: (init?.method ?? 'GET').toUpperCase(),
        });
        throw new Error('Render service unavailable');
      }),
    );
    render(<ExportModal open />);

    // WHEN
    clickRender();

    // THEN
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Render service unavailable')).toBeTruthy();
    expect(
      calls.some(
        (call) => call.method === 'POST' && call.url.endsWith('/api/projects/p1/render'),
      ),
    ).toBe(true);
  });

  it('calls onClose when the close button is clicked without requesting a render', () => {
    // GIVEN
    const onClose = vi.fn();
    render(<ExportModal open onClose={onClose} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: 'Close export' }));

    // THEN
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(calls).toHaveLength(0);
  });

  it('alerts when a completed render has no download URL', () => {
    // GIVEN
    const job = makeJob({
      status: 'completed',
      progress: 100,
      message: 'Render complete',
      result: {},
    });

    // WHEN
    render(<ExportModal open job={job} />);

    // THEN
    const alert = screen.getByRole('alert');
    expect(
      within(alert).getByText('Render completed but no download URL was returned.'),
    ).toBeTruthy();
    expect(screen.queryByRole('link', { name: /download/i })).toBeNull();
    expect(calls).toHaveLength(0);
  });
});
