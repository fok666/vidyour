import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type {
  Composition,
  DetectedPart,
  Job,
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
    resolution_w: 1920,
    resolution_h: 1080,
    fps: 30,
    ai_context: '',
    result_target: 'perfect_loop',
    suggested_sequence: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeAsset(overrides: Partial<MediaAsset> = {}): MediaAsset {
  return {
    id: 'asset-v',
    project_id: 'p1',
    kind: 'video',
    source: 'upload',
    filename: 'clip.mp4',
    local_path: '/data/clip.mp4',
    duration_sec: 10,
    has_audio: true,
    metadata: {},
    created_at: NOW,
    ...overrides,
  };
}

function makeVisualClip(overrides: Partial<TimelineVisualClip> = {}): TimelineVisualClip {
  return {
    id: 'v1',
    asset_id: 'asset-v',
    part_id: null,
    kind: 'video',
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
    id: 'a1',
    asset_id: 'asset-a',
    part_id: null,
    track_index: 0,
    start_sec: 0,
    duration_sec: 8,
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

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-a1',
    project_id: 'p1',
    kind: 'analyze',
    status: 'running',
    progress: 40,
    message: 'Analyzing',
    result: {},
    error: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function seed(composition: Composition, extra: Record<string, unknown> = {}) {
  useStudioStore.setState({
    project: makeProject(),
    assets: [makeAsset(), makeAsset({ id: 'asset-a', kind: 'audio', filename: 'song.mp3' })],
    composition,
    ...extra,
  });
}

function videoClips(): TimelineVisualClip[] {
  return useStudioStore.getState().composition?.video_clips ?? [];
}

function clipById(id: string): TimelineVisualClip | undefined {
  return videoClips().find((c) => c.id === id);
}

function pathOf(input: unknown): string {
  const raw =
    typeof input === 'string' ? input : input instanceof URL ? input.toString() : (input as Request).url;
  return new URL(raw, 'http://localhost').pathname;
}

let idCounter = 0;

beforeEach(() => {
  useStudioStore.setState(useStudioStore.getInitialState(), true);
  idCounter = 0;
  vi.spyOn(globalThis.crypto, 'randomUUID').mockImplementation(
    () => `00000000-0000-4000-8000-${String(++idCounter).padStart(12, '0')}` as `${string}-${string}-${string}-${string}-${string}`,
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useStudioStore timeline mutations', () => {
  it('adds a beat-length visual part muted under an existing music track', () => {
    // GIVEN a 120 BPM project with an A1 audio clip and a detected visual part of 4 beats
    seed(makeComposition({ audio_clips: [makeAudioClip()] }));
    const visualPart: DetectedPart = {
      id: 'part-1',
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 3,
      end_sec: 8,
      beat_length: 4,
      label: 'jump',
      score: 0.9,
      ai_reason: 'peak motion',
      suggested_effect: 'none',
    };

    // WHEN
    useStudioStore.getState().addPartToTimeline(visualPart);

    // THEN
    const added = videoClips();
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({
      asset_id: 'asset-v',
      part_id: 'part-1',
      duration_sec: 4 * 60 / 120,
      muted: true,
      volume: 0.0,
    });
  });

  it('does not mute a beat-length visual part when no music track exists', () => {
    // GIVEN a 120 BPM project without an audio clip and a visual part whose source span is 5 seconds
    seed(makeComposition({ audio_clips: [] }));
    const visualPart: DetectedPart = {
      id: 'part-no-music',
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 1,
      end_sec: 6,
      beat_length: 3,
      label: 'opening',
      score: 0.9,
      ai_reason: 'opening shot',
      suggested_effect: 'none',
    };

    // WHEN
    useStudioStore.getState().addPartToTimeline(visualPart);

    // THEN
    const added = videoClips();
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({
      asset_id: 'asset-v',
      part_id: 'part-no-music',
      duration_sec: 3 * 60 / 120,
      muted: false,
      volume: 1.0,
    });
  });

  it('adds an audio segment part to the end of the A1 track', () => {
    // GIVEN a 120 BPM project with an existing A1 clip and a detected audio segment
    seed(makeComposition({ audio_clips: [makeAudioClip()] }));
    const audioPart: DetectedPart = {
      id: 'part-audio',
      project_id: 'p1',
      asset_id: 'asset-a',
      part_type: 'audio_segment',
      start_sec: 3,
      end_sec: 7,
      beat_length: 4,
      label: 'drum fill',
      score: 0.9,
      ai_reason: 'rhythmic accent',
      suggested_effect: 'none',
    };

    // WHEN
    useStudioStore.getState().addPartToTimeline(audioPart);

    // THEN
    const added = useStudioStore.getState().composition?.audio_clips[1];
    expect(added).toMatchObject({
      asset_id: 'asset-a',
      part_id: 'part-audio',
      track_index: 0,
      start_sec: 8,
      duration_sec: 2,
      trim_start_sec: 3,
    });
  });

  it('uses the project BPM for visual and audio detected-part durations', () => {
    // GIVEN a 90 BPM project with an existing A1 clip
    seed(makeComposition({ audio_clips: [makeAudioClip()] }), {
      project: makeProject({ bpm: 90 }),
    });
    const visualPart: DetectedPart = {
      id: 'part-visual-90bpm',
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 1,
      end_sec: 5,
      beat_length: 4,
      label: 'visual phrase',
      score: 0.9,
      ai_reason: 'four-beat phrase',
      suggested_effect: 'none',
    };
    const audioPart: DetectedPart = {
      id: 'part-audio-90bpm',
      project_id: 'p1',
      asset_id: 'asset-a',
      part_type: 'audio_segment',
      start_sec: 3,
      end_sec: 6,
      beat_length: 3,
      label: 'audio phrase',
      score: 0.9,
      ai_reason: 'three-beat phrase',
      suggested_effect: 'none',
    };

    // WHEN
    const store = useStudioStore.getState();
    store.addPartToTimeline(visualPart);
    store.addPartToTimeline(audioPart);

    // THEN both branches use 90 BPM (not a hard-coded 120 BPM)
    expect(videoClips()[0]?.duration_sec).toBeCloseTo(4 * 60 / 90);
    expect(useStudioStore.getState().composition?.audio_clips[1]?.duration_sec).toBeCloseTo(3 * 60 / 90);
  });

  it.each(['photo', 'live_photo'] as const)('preserves the %s kind when adding a visual part', (kind) => {
    seed(makeComposition(), { assets: [makeAsset({ kind })] });
    const visualPart: DetectedPart = {
      id: `part-${kind}`,
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 1,
      end_sec: 3,
      beat_length: 4,
      label: 'opening',
      score: 0.9,
      ai_reason: 'opening shot',
      suggested_effect: 'none',
    };

    useStudioStore.getState().addPartToTimeline(visualPart);

    expect(videoClips()[0]?.kind).toBe(kind);
  });

  it('creates a composition when adding a part before one exists', () => {
    useStudioStore.setState({
      project: makeProject(),
      assets: [makeAsset()],
      composition: null,
    });
    const visualPart: DetectedPart = {
      id: 'part-new-composition',
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 1,
      end_sec: 3,
      beat_length: 4,
      label: 'opening',
      score: 0.9,
      ai_reason: 'opening shot',
      suggested_effect: 'none',
    };

    useStudioStore.getState().addPartToTimeline(visualPart);

    expect(useStudioStore.getState().composition).toMatchObject({
      project_id: 'p1',
      snap_subdivision: 'beat',
      video_clips: [{ id: '00000000-0000-4000-8000-000000000001', part_id: 'part-new-composition' }],
      audio_clips: [],
    });
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('does not create a composition when adding a part without a project', () => {
    const visualPart: DetectedPart = {
      id: 'part-no-project',
      project_id: 'p1',
      asset_id: 'asset-v',
      part_type: 'visual_clip',
      start_sec: 1,
      end_sec: 3,
      beat_length: 4,
      label: 'opening',
      score: 0.9,
      ai_reason: 'opening shot',
      suggested_effect: 'none',
    };

    useStudioStore.getState().addPartToTimeline(visualPart);

    expect(useStudioStore.getState().composition).toBeNull();
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('snaps a moved clip to the nearest beat', () => {
    seed(makeComposition({ video_clips: [makeVisualClip({ start_sec: 0 })] }));

    useStudioStore.getState().moveClip('v1', 0.82, false);

    expect(clipById('v1')?.start_sec).toBe(1.0);
  });

  it('snaps a moved clip relative to the project beat offset', () => {
    seed(makeComposition({ video_clips: [makeVisualClip({ start_sec: 0 })] }), {
      project: makeProject({ beat_offset_sec: 0.1 }),
    });

    useStudioStore.getState().moveClip('v1', 0.58, false);

    expect(clipById('v1')?.start_sec).toBe(0.6);
  });

  it('moves a clip to the exact position when snapping is bypassed', () => {
    seed(makeComposition({ video_clips: [makeVisualClip({ start_sec: 0 })] }));

    useStudioStore.getState().moveClip('v1', 0.82, true);

    expect(clipById('v1')?.start_sec).toBe(0.82);
  });

  it('trims a clip and clamps its fades to the new duration', () => {
    seed(
      makeComposition({
        video_clips: [makeVisualClip({ duration_sec: 4, fade_in_sec: 1.5, fade_out_sec: 1.0 })],
      }),
    );

    useStudioStore.getState().trimClip('v1', 2.0);

    expect(clipById('v1')).toMatchObject({
      duration_sec: 2.0,
      fade_in_sec: 1.5,
      fade_out_sec: 0.5,
    });
  });

  it('splits a clip at the playhead into two contiguous clips', () => {
    seed(
      makeComposition({
        video_clips: [makeVisualClip({ start_sec: 1, trim_start_sec: 0.5, duration_sec: 4 })],
      }),
      { playheadSec: 3.0 },
    );

    useStudioStore.getState().splitClipAtPlayhead('v1');

    const segments = videoClips()
      .map((c) => ({ start_sec: c.start_sec, trim_start_sec: c.trim_start_sec, duration_sec: c.duration_sec }))
      .sort((a, b) => a.start_sec - b.start_sec);
    expect(segments).toEqual([
      { start_sec: 1.0, trim_start_sec: 0.5, duration_sec: 2.0 },
      { start_sec: 3.0, trim_start_sec: 2.5, duration_sec: 2.0 },
    ]);
    const ids = videoClips().map((c) => c.id);
    expect(new Set(ids).size).toBe(2);
  });

  it('splits an audio clip at the playhead and preserves its track', () => {
    seed(
      makeComposition({
        audio_clips: [makeAudioClip({ start_sec: 1, trim_start_sec: 0.5, duration_sec: 4 })],
      }),
      { playheadSec: 3.0 },
    );

    useStudioStore.getState().splitClipAtPlayhead('a1');

    const composition = useStudioStore.getState().composition;
    expect(composition?.audio_clips).toEqual([
      expect.objectContaining({
        id: 'a1',
        track_index: 0,
        start_sec: 1,
        trim_start_sec: 0.5,
        duration_sec: 2,
        fade_out_sec: 0,
      }),
      expect.objectContaining({
        id: '00000000-0000-4000-8000-000000000001',
        track_index: 0,
        start_sec: 3,
        trim_start_sec: 2.5,
        duration_sec: 2,
        fade_in_sec: 0,
      }),
    ]);
    expect(composition?.video_clips).toEqual([]);
    expect(useStudioStore.getState().history).toHaveLength(1);
  });

  it('deletes the selected clip and clears the selection', () => {
    seed(
      makeComposition({
        video_clips: [makeVisualClip({ id: 'v1' }), makeVisualClip({ id: 'v2', start_sec: 2 })],
      }),
      { selectedClipId: 'v1' },
    );

    useStudioStore.getState().deleteSelectedClip();

    const state = useStudioStore.getState();
    expect(videoClips().map((c) => c.id)).toEqual(['v2']);
    expect(state.selectedClipId).toBeNull();
  });

  it('updates clip properties with fades clamped to the clip duration', () => {
    seed(makeComposition({ video_clips: [makeVisualClip({ duration_sec: 2.0 })] }), {
      selectedClipId: 'v1',
    });

    useStudioStore.getState().updateClip('v1', {
      fade_in_sec: 1.5,
      fade_out_sec: 1.0,
      transition_in: 'crossfade',
      ken_burns: 'zoom_in',
      color_preset: 'vivid',
      speed: 1.25,
      volume: 1.2,
    });

    expect(clipById('v1')).toMatchObject({
      fade_in_sec: 1.5,
      fade_out_sec: 0.5,
      transition_in: 'crossfade',
      ken_burns: 'zoom_in',
      color_preset: 'vivid',
      speed: 1.25,
      volume: 1.2,
    });
  });

  it('leaves composition and history unchanged when updating an unknown clip id', () => {
    const composition = makeComposition({
      video_clips: [makeVisualClip()],
      audio_clips: [makeAudioClip()],
    });
    seed(composition);

    useStudioStore.getState().updateClip('missing', { volume: 0.25 });

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(clipById('v1')?.volume).toBe(1);
    expect(useStudioStore.getState().composition?.audio_clips[0]?.volume).toBe(1);
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('leaves composition and history unchanged for a non-positive trim', () => {
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(composition);

    useStudioStore.getState().trimClip('v1', 0);

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(clipById('v1')?.duration_sec).toBe(2);
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('leaves composition and history unchanged when splitting an unknown clip', () => {
    const composition = makeComposition({
      video_clips: [makeVisualClip()],
      audio_clips: [makeAudioClip()],
    });
    seed(composition, { playheadSec: 1 });

    useStudioStore.getState().splitClipAtPlayhead('missing');

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('clears selection without changing composition when deleting an unknown selected id', () => {
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(composition, { selectedClipId: 'missing' });

    useStudioStore.getState().deleteSelectedClip();

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(videoClips().map((clip) => clip.id)).toEqual(['v1']);
    expect(useStudioStore.getState().selectedClipId).toBeNull();
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('does nothing when deleting without a selected clip', () => {
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(composition);

    useStudioStore.getState().deleteSelectedClip();

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(useStudioStore.getState().selectedClipId).toBeNull();
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('does nothing when undo history is empty', () => {
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seed(composition);

    useStudioStore.getState().undo();

    expect(useStudioStore.getState().composition).toBe(composition);
    expect(useStudioStore.getState().history).toEqual([]);
  });

  it('undoes a move without mutating the prior snapshot', () => {
    seed(makeComposition({ video_clips: [makeVisualClip({ start_sec: 0 })] }));
    const before = useStudioStore.getState().composition as Composition;
    const beforeCopy = structuredClone(before);
    useStudioStore.getState().moveClip('v1', 2.0, false);
    expect(clipById('v1')?.start_sec).toBe(2.0);

    useStudioStore.getState().undo();

    expect(clipById('v1')?.start_sec).toBe(0.0);
    expect(before).toEqual(beforeCopy);
  });

  it('resets the composition to empty tracks', () => {
    seed(
      makeComposition({
        video_clips: [makeVisualClip()],
        audio_clips: [makeAudioClip()],
      }),
    );

    useStudioStore.getState().resetComposition();

    const composition = useStudioStore.getState().composition;
    expect(composition?.video_clips).toEqual([]);
    expect(composition?.audio_clips).toEqual([]);
  });
});

describe('useStudioStore job polling', () => {
  it('polls a running job and updates its progress and message', async () => {
    // GIVEN a running analyze job at 40% with fake timers and a stubbed job endpoint
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    const updated = makeJob({ progress: 75, message: 'Extracting thumbnails' });
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(updated), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    seed(makeComposition(), { jobs: { 'job-a1': makeJob() } });

    // WHEN the next scheduled job poll runs
    await useStudioStore.getState().pollJob('job-a1');
    await vi.runOnlyPendingTimersAsync();
    await Promise.resolve();

    // THEN
    const paths = fetchMock.mock.calls.map((call) => pathOf((call as unknown[])[0]));
    expect(paths).toContain('/api/jobs/job-a1');
    expect(useStudioStore.getState().jobs['job-a1']).toMatchObject({
      progress: 75,
      message: 'Extracting thumbnails',
    });
  });

  it('marks a job failed and stores the API error when polling fails', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'job service unavailable' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    seed(makeComposition(), { jobs: { 'job-a1': makeJob() } });

    await useStudioStore.getState().pollJob('job-a1');
    await vi.runOnlyPendingTimersAsync();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(pathOf((fetchMock.mock.calls[0] as unknown[])[0])).toBe('/api/jobs/job-a1');
    expect(useStudioStore.getState().jobs['job-a1']).toMatchObject({
      status: 'failed',
      error: 'job service unavailable',
    });
    expect(useStudioStore.getState().jobs['job-a1'].progress).toBe(40);
  });

  it('formats a non-Error polling failure with String', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    vi.spyOn(apiClient, 'getJob').mockRejectedValue('connection lost');
    seed(makeComposition(), { jobs: { 'job-a1': makeJob() } });

    await useStudioStore.getState().pollJob('job-a1');
    await vi.runOnlyPendingTimersAsync();

    expect(useStudioStore.getState().jobs['job-a1']).toMatchObject({
      status: 'failed',
      error: 'connection lost',
    });
  });
});

describe('useStudioStore API-backed settings', () => {
  it('loads capabilities and Google Photos status into the store', async () => {
    const capabilities = {
      google_photos: true,
      gemini: false,
      bedrock: true,
      jamendo: false,
      freesound: true,
      s3: false,
      active_ai_provider: 'bedrock',
      storage_backend: 'local',
    };
    const googlePhotosStatus = { configured: true, connected: false };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = pathOf(input);
      const body = path === '/api/config/capabilities' ? capabilities : googlePhotosStatus;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await useStudioStore.getState().loadCapabilities();
    await useStudioStore.getState().loadGooglePhotosStatus();

    expect(fetchMock.mock.calls.map((call) => pathOf(call[0]))).toEqual([
      '/api/config/capabilities',
      '/api/gallery/google/status',
    ]);
    expect(useStudioStore.getState().capabilities).toEqual(capabilities);
    expect(useStudioStore.getState().googlePhotosStatus).toEqual(googlePhotosStatus);
  });
});
