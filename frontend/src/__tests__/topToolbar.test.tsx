import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TopToolbar } from '../components/TopToolbar';
import { useStudioStore } from '../store/useStudioStore';
import type { Capabilities, Project } from '../types/studio';

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

function makeCapabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  return {
    google_photos: false,
    gemini: true,
    bedrock: false,
    jamendo: false,
    freesound: false,
    s3: false,
    active_ai_provider: 'gemini',
    storage_backend: 'local',
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('TopToolbar', () => {
  beforeEach(() => {
    // Stub fetch: echo PATCH/PUT bodies merged onto the current project.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        const current = useStudioStore.getState().project ?? makeProject();
        const changes = init?.body ? JSON.parse(String(init.body)) : {};
        return jsonResponse({ ...current, ...changes });
      }),
    );
    useStudioStore.setState({
      project: makeProject(),
      capabilities: makeCapabilities(),
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStudioStore.setState({ project: null, capabilities: null });
  });

  it('renders active provider badge, local storage fallback badge, BPM, resolution and Export', () => {
    // GIVEN gemini active, local storage, 124 BPM 1280x720 project (beforeEach)
    // WHEN
    render(<TopToolbar />);

    // THEN
    expect(screen.getByText('Gemini').getAttribute('data-kind')).toBe('active');
    expect(screen.getByText('Local Storage').getAttribute('data-kind')).toBe('fallback');
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    expect(bpmInput.value).toBe('124');
    const resolution = screen.getByLabelText(/resolution/i) as HTMLSelectElement;
    expect(resolution.value).toBe('1280x720');
    expect(screen.getByRole('button', { name: /export/i })).toBeTruthy();
  });

  it('commits an edited BPM value to the store project', async () => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '132' } });
    fireEvent.blur(bpmInput);

    // THEN
    await waitFor(() => {
      expect(useStudioStore.getState().project?.bpm).toBe(132);
      expect(bpmInput.value).toBe('132');
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/projects/p1');
    expect(init?.method).toBe('PATCH');
    expect(JSON.parse(String(init?.body))).toEqual({ bpm: 132 });
  });

  it('does not persist BPM or resolution changes when there is no project', () => {
    // GIVEN
    render(<TopToolbar project={null} />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const resolution = screen.getByLabelText(/resolution/i) as HTMLSelectElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '132' } });
    fireEvent.blur(bpmInput);
    fireEvent.change(resolution, { target: { value: '1920x1080' } });

    // THEN
    expect(useStudioStore.getState().project?.bpm).toBe(124);
    expect(useStudioStore.getState().project?.resolution_w).toBe(1280);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not persist an unchanged BPM or call its callback', () => {
    // GIVEN
    const onBpmChange = vi.fn();
    render(<TopToolbar onBpmChange={onBpmChange} />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '124' } });
    fireEvent.blur(bpmInput);

    // THEN
    expect(bpmInput.value).toBe('124');
    expect(useStudioStore.getState().project?.bpm).toBe(124);
    expect(onBpmChange).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('persists BPM and resolution changes when optional callbacks are absent', async () => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const resolution = screen.getByLabelText(/resolution/i) as HTMLSelectElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '132' } });
    fireEvent.blur(bpmInput);
    fireEvent.change(resolution, { target: { value: '1920x1080' } });

    // THEN
    await waitFor(() => {
      const project = useStudioStore.getState().project;
      expect(project?.bpm).toBe(132);
      expect(project?.resolution_w).toBe(1920);
      expect(project?.resolution_h).toBe(1080);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
  });

  it.each([
    ['an empty draft', ''],
    ['a zero BPM', '0'],
    ['a negative BPM', '-12'],
  ])('reverts %s to the stored BPM without persisting', (_description, draft) => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: draft } });
    expect(bpmInput.value).toBe(draft);
    fireEvent.blur(bpmInput);

    // THEN
    expect(bpmInput.value).toBe('124');
    expect(useStudioStore.getState().project?.bpm).toBe(124);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('cancels a BPM draft on Escape without changing the store or calling the API', () => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '140' } });
    fireEvent.keyDown(bpmInput, { key: 'Escape' });

    // THEN
    expect(bpmInput.value).toBe('124');
    expect(useStudioStore.getState().project?.bpm).toBe(124);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('commits a BPM draft on Enter', async () => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);

    // WHEN
    bpmInput.focus();
    fireEvent.change(bpmInput, { target: { value: '132' } });
    fireEvent.keyDown(bpmInput, { key: 'Enter' });

    // THEN
    await waitFor(() => {
      expect(bpmInput.value).toBe('132');
      expect(useStudioStore.getState().project?.bpm).toBe(132);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/projects/p1');
    expect(init?.method).toBe('PATCH');
    expect(JSON.parse(String(init?.body))).toEqual({ bpm: 132 });
  });

  it('keeps the optimistic BPM update and reports a failed persistence request', async () => {
    // GIVEN
    render(<TopToolbar />);
    const bpmInput = screen.getByLabelText(/bpm/i) as HTMLInputElement;
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockRejectedValueOnce(new Error('offline'));
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    // WHEN
    fireEvent.change(bpmInput, { target: { value: '132' } });
    fireEvent.blur(bpmInput);

    // THEN
    await waitFor(() => {
      expect(bpmInput.value).toBe('132');
      expect(useStudioStore.getState().project?.bpm).toBe(132);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(consoleError).toHaveBeenCalledWith(
        'Failed to update project',
        expect.objectContaining({ message: 'offline' }),
      );
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/projects/p1');
    expect(init?.method).toBe('PATCH');
    expect(JSON.parse(String(init?.body))).toEqual({ bpm: 132 });
    consoleError.mockRestore();
  });

  it('updates the store project resolution when 1920x1080 is selected', async () => {
    // GIVEN
    render(<TopToolbar />);
    const resolution = screen.getByLabelText(/resolution/i) as HTMLSelectElement;

    // WHEN
    fireEvent.change(resolution, { target: { value: '1920x1080' } });

    // THEN
    await waitFor(() => {
      const project = useStudioStore.getState().project;
      expect(project?.resolution_w).toBe(1920);
      expect(project?.resolution_h).toBe(1080);
    });
  });

  it('calls onOpenExport exactly once when Export is clicked', () => {
    // GIVEN
    const onOpenExport = vi.fn();
    render(<TopToolbar onOpenExport={onOpenExport} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /export/i }));

    // THEN
    expect(onOpenExport).toHaveBeenCalledTimes(1);
  });
});
