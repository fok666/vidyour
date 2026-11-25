import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { TimelineDock } from '../components/TimelineDock';
import { useStudioStore } from '../store/useStudioStore';
import type {
  Composition,
  MediaAsset,
  Project,
  TimelineAudioClip,
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
    result_target: 'perfect_loop',
    suggested_sequence: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeAudioAsset(overrides: Partial<MediaAsset> = {}): MediaAsset {
  return {
    id: 'music',
    project_id: 'p1',
    kind: 'audio',
    source: 'builtin',
    filename: 'music.mp3',
    local_path: 'assets/music.mp3',
    still_path: null,
    thumbnail_path: null,
    duration_sec: 8,
    width: null,
    height: null,
    fps: null,
    has_audio: true,
    bpm: 120,
    beat_grid: { beat_times: [], downbeats: [], waveform: [0.2, 0.8, 0.5] },
    metadata: {},
    created_at: NOW,
    ...overrides,
  };
}

function makeVisualClip(overrides: Partial<TimelineVisualClip> = {}): TimelineVisualClip {
  return {
    id: 'v1',
    asset_id: 'a1',
    part_id: null,
    kind: 'photo',
    start_sec: 0,
    duration_sec: 2,
    trim_start_sec: 0,
    speed: 1,
    transition_in: 'none',
    transition_duration_sec: 0,
    ken_burns: 'none',
    color_preset: 'original',
    volume: 1,
    muted: false,
    fade_in_sec: 0,
    fade_out_sec: 0,
    ...overrides,
  };
}

function makeAudioClip(overrides: Partial<TimelineAudioClip> = {}): TimelineAudioClip {
  return {
    id: 'au1',
    asset_id: 'music',
    part_id: null,
    track_index: 0,
    start_sec: 0,
    duration_sec: 4,
    trim_start_sec: 0,
    volume: 1,
    muted: false,
    fade_in_sec: 0,
    fade_out_sec: 0,
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

function seed(project: Project, composition: Composition, extra: Record<string, unknown> = {}) {
  useStudioStore.setState({
    project,
    composition,
    assets: [makeAudioAsset()],
    selectedClipId: null,
    playheadSec: 0,
    history: [],
    ...extra,
  });
}

function renderDock(project: Project, composition: Composition) {
  return render(<TimelineDock project={project} composition={composition} />);
}

function drag(element: Element, deltaPx: number, altKey = false) {
  const startX = 100;
  fireEvent.pointerDown(element, { pointerId: 1, button: 0, clientX: startX, altKey });
  fireEvent.pointerMove(element, { pointerId: 1, clientX: startX + deltaPx / 2, altKey });
  fireEvent.pointerMove(element, { pointerId: 1, clientX: startX + deltaPx, altKey });
  fireEvent.pointerUp(element, { pointerId: 1, clientX: startX + deltaPx, altKey });
}

beforeAll(() => {
  if (typeof window.PointerEvent === 'undefined') {
    class PointerEventPolyfill extends MouseEvent {
      pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 0;
      }
    }
    (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventPolyfill;
  }
  const proto = Element.prototype as unknown as Record<string, unknown>;
  if (!proto.setPointerCapture) proto.setPointerCapture = () => {};
  if (!proto.releasePointerCapture) proto.releasePointerCapture = () => {};
  if (!proto.hasPointerCapture) proto.hasPointerCapture = () => false;
});

describe('TimelineDock', () => {
  beforeEach(() => {
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(
      '00000000-0000-4000-8000-000000000002',
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders V1/A1/A2 lanes, bar:beat ruler, waveform peaks, and fade ramp overlays', () => {
    const project = makeProject();
    const composition = makeComposition({
      video_clips: [makeVisualClip({ duration_sec: 4 })],
      audio_clips: [
        makeAudioClip({ id: 'au1', track_index: 0, fade_in_sec: 0.5, fade_out_sec: 0.5 }),
        makeAudioClip({ id: 'au2', track_index: 1, start_sec: 2, duration_sec: 2 }),
      ],
    });
    seed(project, composition);

    renderDock(project, composition);

    expect(screen.getByText('V1')).toBeTruthy();
    expect(screen.getByText('A1')).toBeTruthy();
    expect(screen.getByText('A2')).toBeTruthy();
    expect(screen.getByText('1:1')).toBeTruthy();
    expect(screen.getByText('2:1')).toBeTruthy();
    const a1Clip = screen.getByTestId('clip-au1');
    expect(within(a1Clip).getAllByTestId('waveform-peak')).toHaveLength(3);
    const fadeIn = within(a1Clip).getByTestId('fade-in-ramp');
    const fadeOut = within(a1Clip).getByTestId('fade-out-ramp');
    expect(fadeIn.closest('svg')).not.toBeNull();
    expect(fadeOut.closest('svg')).not.toBeNull();
  });

  it('updates composition.snap_subdivision when a new snap subdivision is selected', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);

    fireEvent.change(screen.getByRole('combobox', { name: /snap/i }), {
      target: { value: 'half_beat' },
    });

    expect(useStudioStore.getState().composition?.snap_subdivision).toBe('half_beat');
  });

  it('rescales clip width when horizontal zoom changes', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip({ duration_sec: 2 })] });
    seed(project, composition);
    renderDock(project, composition);

    fireEvent.change(screen.getByRole('slider', { name: /zoom/i }), {
      target: { value: '200' },
    });

    expect((screen.getByTestId('clip-v1') as HTMLElement).style.width).toBe('400px');
  });

  it('sets the playhead from the clicked ruler position', () => {
    const project = makeProject();
    const composition = makeComposition();
    seed(project, composition);
    renderDock(project, composition);
    const ruler = screen.getByTestId('timeline-ruler');
    vi.spyOn(ruler, 'getBoundingClientRect').mockReturnValue({
      left: 50,
      right: 1650,
      top: 0,
      bottom: 20,
      width: 1600,
      height: 20,
      x: 50,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    fireEvent.click(ruler, { clientX: 250 });

    expect(useStudioStore.getState().playheadSec).toBe(2);
  });

  it('snaps a dragged clip start to the beat grid', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);

    drag(screen.getByTestId('clip-v1'), 82);

    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(1.0);
  });

  it('bypasses snap when Alt is held during drag', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);

    drag(screen.getByTestId('clip-v1'), 82, true);

    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBeCloseTo(
      0.82,
      6,
    );
  });

  it('ignores non-primary starts and pointer events from another pointer', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);
    const clip = screen.getByTestId('clip-v1');

    fireEvent.pointerDown(clip, { pointerId: 1, button: 2, clientX: 100 });
    fireEvent.pointerMove(clip, { pointerId: 1, clientX: 182 });
    fireEvent.pointerUp(clip, { pointerId: 1, clientX: 182 });
    expect(useStudioStore.getState().selectedClipId).toBeNull();
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(0);

    fireEvent.pointerDown(clip, { pointerId: 1, button: 0, clientX: 100 });
    fireEvent.pointerMove(clip, { pointerId: 2, clientX: 182 });
    fireEvent.pointerUp(clip, { pointerId: 2, clientX: 182 });
    expect(clip.style.left).toBe('0px');
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(0);

    fireEvent.pointerMove(clip, { pointerId: 1, clientX: 182 });
    fireEvent.pointerUp(clip, { pointerId: 1, clientX: 182 });
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(1);
  });

  it('continues a drag when pointer capture and release both fail', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);
    const clip = screen.getByTestId('clip-v1');
    const setPointerCapture = vi
      .spyOn(clip, 'setPointerCapture')
      .mockImplementation(() => {
        throw new Error('pointer capture unavailable');
      });
    const releasePointerCapture = vi
      .spyOn(clip, 'releasePointerCapture')
      .mockImplementation(() => {
        throw new Error('pointer release unavailable');
      });

    fireEvent.pointerDown(clip, { pointerId: 1, button: 0, clientX: 100 });
    fireEvent.pointerMove(clip, { pointerId: 1, clientX: 200 });
    fireEvent.pointerUp(clip, { pointerId: 1, clientX: 200 });

    expect(setPointerCapture).toHaveBeenCalledWith(1);
    expect(releasePointerCapture).toHaveBeenCalledWith(1);
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(1);
  });

  it('cancels a drag preview without changing the clip', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    renderDock(project, composition);
    const clip = screen.getByTestId('clip-v1');

    fireEvent.pointerDown(clip, { pointerId: 1, button: 0, clientX: 100 });
    fireEvent.pointerMove(clip, { pointerId: 1, clientX: 182 });
    expect(clip.style.left).toBe('82px');

    fireEvent.pointerCancel(clip, { pointerId: 1 });
    fireEvent.pointerUp(clip, { pointerId: 1, clientX: 182 });

    expect(clip.style.left).toBe('0px');
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(0);
  });

  it('does not commit a drag with zero horizontal movement', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition);
    const moveClip = vi.spyOn(useStudioStore.getState(), 'moveClip');
    renderDock(project, composition);
    const clip = screen.getByTestId('clip-v1');

    fireEvent.pointerDown(clip, { pointerId: 1, button: 0, clientX: 100 });
    fireEvent.pointerUp(clip, { pointerId: 1, clientX: 100 });

    expect(moveClip).not.toHaveBeenCalled();
    expect(useStudioStore.getState().composition?.video_clips[0].start_sec).toBe(0);
  });

  it('trims clip duration to the beat grid when the right edge handle is dragged', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip({ duration_sec: 2 })] });
    seed(project, composition);
    renderDock(project, composition);

    drag(screen.getByTestId('trim-right-v1'), 60);

    const clip = useStudioStore.getState().composition?.video_clips[0];
    expect(clip?.start_sec).toBe(0);
    expect(clip?.duration_sec).toBe(2.5);
  });

  it('splits the selected clip at the playhead when Split is clicked', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip({ duration_sec: 4 })] });
    seed(project, composition, { selectedClipId: 'v1', playheadSec: 2 });
    renderDock(project, composition);

    fireEvent.click(screen.getByRole('button', { name: /split/i }));

    const spans = useStudioStore
      .getState()
      .composition?.video_clips.map((clip) => [clip.start_sec, clip.start_sec + clip.duration_sec]);
    expect(spans).toEqual([
      [0, 2],
      [2, 4],
    ]);
  });

  it('removes the selected clip when Delete is clicked', () => {
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(project, composition, { selectedClipId: 'v1' });
    renderDock(project, composition);

    fireEvent.click(screen.getByRole('button', { name: /delete/i }));

    expect(useStudioStore.getState().composition?.video_clips.map((clip) => clip.id)).toEqual(
      [],
    );
  });
});
