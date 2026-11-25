import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DetectedPartsBin } from '../components/DetectedPartsBin';
import { useStudioStore } from '../store/useStudioStore';
import type {
  Composition,
  DetectedPart,
  Project,
  TimelineVisualClip,
} from '../types/studio';

const NOW = '2026-11-22T00:00:00Z';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1',
    name: 'Demo',
    bpm: 120,
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

function makeComposition(overrides: Partial<Composition> = {}): Composition {
  return {
    project_id: 'p1',
    snap_subdivision: 'beat',
    video_clips: [],
    audio_clips: [],
    updated_at: NOW,
    ...overrides,
  };
}

const visualPart: DetectedPart = {
  id: 'part1',
  project_id: 'p1',
  asset_id: 'asset-v1',
  part_type: 'visual_clip',
  start_sec: 1,
  end_sec: 3,
  beat_length: 4,
  label: 'Golden Hour Smile',
  score: 0.92,
  ai_reason: 'Bright warm smile',
  suggested_effect: 'none',
  clip_path: 'clips/part1.mp4',
  thumbnail_path: 'thumbs/part1.jpg',
  created_at: NOW,
};

const audioPart: DetectedPart = {
  id: 'part2',
  project_id: 'p1',
  asset_id: 'asset-a1',
  part_type: 'audio_segment',
  start_sec: 30,
  end_sec: 38,
  beat_length: 16,
  label: 'Peak / Drop',
  score: 0.8,
  ai_reason: 'Energy peak',
  suggested_effect: 'none',
  clip_path: 'clips/part2.mp3',
  thumbnail_path: null,
  created_at: NOW,
};

function makeVisualClip(id: string, startSec: number): TimelineVisualClip {
  return {
    id,
    asset_id: 'asset-v1',
    part_id: 'part1',
    kind: 'video',
    start_sec: startSec,
    duration_sec: 2,
    trim_start_sec: 1,
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
let autoAssembleError: string | null;
let autoAssembleRejection: unknown;
const assembled = makeComposition({
  video_clips: [makeVisualClip('auto-1', 0), makeVisualClip('auto-2', 2)],
});

function installFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method });
      const path = url.split('?')[0];
      if (method === 'POST' && path.endsWith('/api/projects/p1/composition/auto-assemble')) {
        if (autoAssembleRejection !== undefined) throw autoAssembleRejection;
        if (autoAssembleError) return jsonResponse({ detail: autoAssembleError }, 500);
        return jsonResponse(assembled);
      }
      if (path.endsWith('/api/projects/p1/composition')) {
        return jsonResponse(useStudioStore.getState().composition ?? makeComposition());
      }
      if (method === 'GET' && path.endsWith('/api/projects/p1/parts')) {
        return jsonResponse([visualPart, audioPart]);
      }
      return jsonResponse({});
    }),
  );
}

/** Find the nearest ancestor of the part label that contains a button matching `name`. */
function partButton(label: string, name: RegExp): HTMLElement {
  let node: HTMLElement | null = screen.getByText(label);
  while (node) {
    const match = within(node).queryAllByRole('button', { name });
    if (match.length > 0) return match[0];
    node = node.parentElement;
  }
  throw new Error(`No button ${name} found near '${label}'`);
}

describe('DetectedPartsBin', () => {
  beforeEach(() => {
    calls = [];
    autoAssembleError = null;
    autoAssembleRejection = undefined;
    installFetch();
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(
      '00000000-0000-4000-8000-000000000001',
    );
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
    useStudioStore.setState({
      project: makeProject(),
      assets: [],
      parts: [visualPart, audioPart],
      composition: makeComposition(),
      history: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useStudioStore.setState({ project: null, parts: [], composition: null, history: [] });
  });

  it('displays visual clip label, beat pill, score, AI reason tooltip and audio segment with beat pill', () => {
    // GIVEN store with visual clip and audio segment (beforeEach)

    // WHEN
    render(<DetectedPartsBin />);

    // THEN
    expect(screen.getByText('Golden Hour Smile')).toBeTruthy();
    expect(screen.getByText('4b')).toBeTruthy();
    expect(screen.getByText(/92%/)).toBeTruthy();
    expect(screen.getByTitle('Bright warm smile')).toBeTruthy();
    expect(screen.getByText('Peak / Drop')).toBeTruthy();
    expect(screen.getByText('16b')).toBeTruthy();
  });

  it('renders an inline video preview with the clip file URL when the preview button is clicked', async () => {
    // GIVEN
    const { container } = render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(partButton('Golden Hour Smile', /preview/i));

    // THEN
    await waitFor(() => {
      const video = container.querySelector('video');
      expect(video).not.toBeNull();
      expect(video?.getAttribute('src')).toBe('/api/files/clips/part1.mp4');
    });
  });

  it('renders a playable inline audio preview when an audio segment preview button is clicked', async () => {
    // GIVEN
    const { container } = render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(partButton('Peak / Drop', /preview/i));

    // THEN
    await waitFor(() => {
      const audio = container.querySelector('audio');
      expect(audio).not.toBeNull();
      expect(audio?.getAttribute('src')).toBe('/api/files/clips/part2.mp3');
      expect(audio?.hasAttribute('controls')).toBe(true);
      expect(partButton('Peak / Drop', /preview/i).getAttribute('aria-pressed')).toBe('true');
    });
  });

  it('shows the no-preview fallback when a part has no clip path and Preview is clicked', () => {
    // GIVEN
    render(<DetectedPartsBin parts={[{ ...visualPart, clip_path: null }]} />);

    // WHEN
    fireEvent.click(partButton('Golden Hour Smile', /preview/i));

    // THEN
    expect(screen.getByText('No preview available')).toBeTruthy();
    expect(partButton('Golden Hour Smile', /preview/i).getAttribute('aria-pressed')).toBe('true');
  });

  it("appends a 2.0s TimelineVisualClip for part1 when '+ Add' is clicked at 120 BPM", async () => {
    // GIVEN
    render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(partButton('Golden Hour Smile', /add/i));

    // THEN
    await waitFor(() => {
      const clips = useStudioStore.getState().composition?.video_clips ?? [];
      expect(clips).toHaveLength(1);
      expect(clips[0].part_id).toBe('part1');
      expect(clips[0].duration_sec).toBe(2.0);
    });
  });

  it("POSTs auto-assemble and stores the assembled composition when 'Auto-Assemble for Target' is clicked", async () => {
    // GIVEN
    render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /auto-assemble for target/i }));

    // THEN
    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.method === 'POST' &&
            call.url.split('?')[0].endsWith('/api/projects/p1/composition/auto-assemble'),
        ),
      ).toBe(true);
      expect(useStudioStore.getState().composition?.video_clips).toHaveLength(2);
    });
  });

  it('calls the optional auto-assemble callback instead of making an API request', () => {
    // GIVEN
    const onAutoAssemble = vi.fn();
    render(<DetectedPartsBin onAutoAssemble={onAutoAssemble} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /auto-assemble for target/i }));

    // THEN
    expect(onAutoAssemble).toHaveBeenCalledOnce();
    expect(
      calls.some((call) => call.method === 'POST' && call.url.includes('/composition/auto-assemble')),
    ).toBe(false);
  });

  it('shows the auto-assemble error alert and preserves composition when the request fails', async () => {
    // GIVEN
    autoAssembleError = 'Auto-assemble failed';
    const initialComposition = useStudioStore.getState().composition;
    render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /auto-assemble for target/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toBe('Auto-assemble failed');
    expect(
      calls.some(
        (call) =>
          call.method === 'POST' &&
          call.url.split('?')[0].endsWith('/api/projects/p1/composition/auto-assemble'),
      ),
    ).toBe(true);
    expect(useStudioStore.getState().composition).toBe(initialComposition);
    expect(
      screen.getByRole('button', { name: /auto-assemble for target/i }).hasAttribute('disabled'),
    ).toBe(false);
  });

  it('formats a non-Error auto-assemble rejection as an alert message', async () => {
    // GIVEN
    autoAssembleRejection = 'plain auto-assemble failure';
    render(<DetectedPartsBin />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /auto-assemble for target/i }));

    // THEN
    expect((await screen.findByRole('alert')).textContent).toBe('plain auto-assemble failure');
  });
});
