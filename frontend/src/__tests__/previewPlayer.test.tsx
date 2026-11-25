import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PreviewPlayer } from '../components/PreviewPlayer';
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

function makeAsset(overrides: Partial<MediaAsset> = {}): MediaAsset {
  return {
    id: 'a1',
    project_id: 'p1',
    kind: 'photo',
    source: 'upload',
    filename: 'p.jpg',
    local_path: 'assets/p.jpg',
    still_path: null,
    thumbnail_path: null,
    duration_sec: 0,
    width: 1280,
    height: 720,
    fps: null,
    has_audio: false,
    bpm: null,
    beat_grid: null,
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
    duration_sec: 4,
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

type GainCall = { method: string; value: number; time: number };

class FakeAudioParam {
  value = 1;
  calls: GainCall[] = [];
  setValueAtTime(value: number, time: number) {
    this.calls.push({ method: 'setValueAtTime', value, time });
    this.value = value;
    return this;
  }
  linearRampToValueAtTime(value: number, time: number) {
    this.calls.push({ method: 'linearRampToValueAtTime', value, time });
    return this;
  }
  exponentialRampToValueAtTime(value: number, time: number) {
    this.calls.push({ method: 'exponentialRampToValueAtTime', value, time });
    return this;
  }
  setTargetAtTime(value: number, time: number) {
    this.calls.push({ method: 'setTargetAtTime', value, time });
    return this;
  }
  cancelScheduledValues(time: number) {
    this.calls.push({ method: 'cancelScheduledValues', value: Number.NaN, time });
    return this;
  }
  cancelAndHoldAtTime(time: number) {
    this.calls.push({ method: 'cancelAndHoldAtTime', value: Number.NaN, time });
    return this;
  }
}

class FakeNode {
  connect(target?: unknown) {
    return target;
  }
  disconnect() {}
}

class FakeGainNode extends FakeNode {
  gain = new FakeAudioParam();
}

class FakeSourceNode extends FakeNode {
  buffer: unknown = null;
  playbackRate = new FakeAudioParam();
  start() {}
  stop() {}
}

class FakeAudioContext {
  currentTime = 10;
  state: AudioContextState = 'running';
  destination = new FakeNode();
  gains: FakeGainNode[] = [];
  createGain() {
    const node = new FakeGainNode();
    this.gains.push(node);
    return node;
  }
  createMediaElementSource() {
    return new FakeSourceNode();
  }
  createBufferSource() {
    return new FakeSourceNode();
  }
  async decodeAudioData() {
    return {};
  }
  async resume() {
    this.state = 'running';
  }
  async suspend() {
    this.state = 'suspended';
  }
  async close() {
    this.state = 'closed';
  }
}

function fakeMediaElement(kind: 'audio' | 'video'): HTMLMediaElement {
  const element = document.createElement(kind);
  Object.defineProperty(element, 'play', { value: vi.fn(() => Promise.resolve()) });
  Object.defineProperty(element, 'pause', { value: vi.fn() });
  Object.defineProperty(element, 'load', { value: vi.fn() });
  return element;
}

function playerProps(audioContext: FakeAudioContext = new FakeAudioContext()) {
  return {
    audioContextFactory: () => audioContext as unknown as AudioContext,
    clock: () => 0,
    requestAnimationFrame: vi.fn(() => 1),
    cancelAnimationFrame: vi.fn(),
    mediaElementFactory: fakeMediaElement,
  };
}

function playbackClock() {
  let now = 0;
  let nextHandle = 0;
  const frames = new Map<number, FrameRequestCallback>();

  return {
    clock: () => now,
    requestAnimationFrame: vi.fn((callback: FrameRequestCallback) => {
      const handle = ++nextHandle;
      frames.set(handle, callback);
      return handle;
    }),
    cancelAnimationFrame: vi.fn((handle: number) => {
      frames.delete(handle);
    }),
    stepTo(timeMs: number) {
      now = timeMs;
      const pending = frames.entries().next().value as
        | [number, FrameRequestCallback]
        | undefined;
      if (!pending) throw new Error('No animation frame is pending');
      frames.delete(pending[0]);
      act(() => pending[1](timeMs));
    },
    get pendingFrames() {
      return frames.size;
    },
  };
}

function seedStore(project: Project, composition: Composition, assets: MediaAsset[], playheadSec = 0) {
  useStudioStore.setState({
    project,
    composition,
    assets,
    playheadSec,
    isPlaying: false,
    loopPlayback: false,
  });
}

describe('PreviewPlayer', () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useStudioStore.setState({
      project: null,
      composition: null,
      assets: [],
      playheadSec: 0,
      isPlaying: false,
      loopPlayback: false,
    });
  });

  it('continues playback when the Web Audio API is unavailable', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip()],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    vi.stubGlobal('AudioContext', undefined);
    const clock = playbackClock();
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
        mediaElementFactory={fakeMediaElement}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });

    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(clock.pendingFrames).toBe(1);
    clock.stepTo(1000);
    expect(useStudioStore.getState().playheadSec).toBe(1);
  });

  it('continues playback when AudioContext construction fails', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip()],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    const clock = playbackClock();
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps()}
        audioContextFactory={() => {
          throw new Error('AudioContext construction failed');
        }}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });

    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(clock.pendingFrames).toBe(1);
  });

  it('continues playback when resuming a suspended AudioContext is rejected', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip()],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    const audioContext = new FakeAudioContext();
    audioContext.state = 'suspended';
    audioContext.resume = vi.fn(() => Promise.reject(new Error('resume failed')));
    const clock = playbackClock();
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps(audioContext)}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
      await Promise.resolve();
    });

    expect(audioContext.resume).toHaveBeenCalledTimes(1);
    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(clock.pendingFrames).toBe(1);
  });

  it('skips an audio clip whose asset is missing without interrupting playback', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip({ asset_id: 'missing' })],
    });
    seedStore(project, composition, []);
    const mediaElementFactory = vi.fn(fakeMediaElement);
    const clock = playbackClock();
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps()}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
        mediaElementFactory={mediaElementFactory}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });

    expect(mediaElementFactory).not.toHaveBeenCalled();
    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(clock.pendingFrames).toBe(1);
  });

  it('renders the active photo clip as an <img> with Ken Burns, CSS colour filter, timecode and Bar:Beat', () => {
    // GIVEN a 120 BPM timeline at 2.5s whose active V1 clip is a bw zoom_in photo
    const project = makeProject({ bpm: 120 });
    const composition = makeComposition({
      video_clips: [
        makeVisualClip({ kind: 'photo', ken_burns: 'zoom_in', color_preset: 'bw' }),
      ],
    });
    seedStore(project, composition, [makeAsset()], 2.5);

    // WHEN
    const { container } = render(
      <PreviewPlayer project={project} composition={composition} {...playerProps()} />,
    );

    // THEN
    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toBe('/api/files/assets/p.jpg');
    const kenBurns = `${img!.getAttribute('class') ?? ''} ${img!.getAttribute('style') ?? ''}`;
    expect(kenBurns).toMatch(/zoom[-_]?in/i);
    expect(container.innerHTML).toMatch(/grayscale/);
    expect(screen.getByText('00:02.50')).toBeTruthy();
    expect(screen.getByText('2:2')).toBeTruthy();
  });

  it('renders the active video clip as an HTML5 <video> with a warm sepia CSS filter', () => {
    // GIVEN a 120 BPM timeline at 0.5s whose active V1 clip is a warm video
    const project = makeProject({ bpm: 120 });
    const composition = makeComposition({
      video_clips: [makeVisualClip({ kind: 'video', color_preset: 'warm' })],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ kind: 'video', filename: 'clip.mp4', local_path: 'assets/clip.mp4', duration_sec: 4 })],
      0.5,
    );

    // WHEN
    const { container } = render(
      <PreviewPlayer project={project} composition={composition} {...playerProps()} />,
    );

    // THEN
    const video = container.querySelector('video');
    expect(video).not.toBeNull();
    expect(video!.getAttribute('src')).toBe('/api/files/assets/clip.mp4');
    expect(container.innerHTML).toMatch(/sepia/);
  });

  it('starts playback on Play and schedules GainNode fade-in and fade-out ramps for A1', async () => {
    // GIVEN an A1 clip 0-4s at volume 0.8 with 0.5s fades and a fake AudioContext at t=10
    const project = makeProject({ bpm: 120 });
    const composition = makeComposition({
      audio_clips: [
        makeAudioClip({ volume: 0.8, fade_in_sec: 0.5, fade_out_sec: 0.5 }),
      ],
    });
    seedStore(
      project,
      composition,
      [
        makeAsset({
          id: 'music',
          kind: 'audio',
          filename: 'music.mp3',
          local_path: 'assets/music.mp3',
          duration_sec: 4,
          has_audio: true,
        }),
      ],
      0,
    );
    const audioContext = new FakeAudioContext();
    const t = audioContext.currentTime;
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps(audioContext)}
      />,
    );

    // WHEN
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });

    // THEN
    expect(useStudioStore.getState().isPlaying).toBe(true);
    const calls = audioContext.gains.flatMap((node) => node.gain.calls);
    const fadeIn = calls.find(
      (call) =>
        call.method === 'linearRampToValueAtTime' &&
        Math.abs(call.value - 0.8) < 1e-6 &&
        Math.abs(call.time - (t + 0.5)) < 1e-6,
    );
    const fadeInStart = calls.find(
      (call) =>
        call.method === 'setValueAtTime' &&
        Math.abs(call.value) < 1e-6 &&
        Math.abs(call.time - t) < 1e-6,
    );
    const fadeOut = calls.find(
      (call) =>
        call.method === 'linearRampToValueAtTime' &&
        Math.abs(call.value) < 1e-6 &&
        Math.abs(call.time - (t + 4.0)) < 1e-6,
    );
    expect(fadeInStart).toBeDefined();
    expect(fadeIn).toBeDefined();
    expect(fadeOut).toBeDefined();
  });

  it('advances the playhead from the RAF clock while audio continues playing', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip({ duration_sec: 4 })],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    const clock = playbackClock();
    const mediaElements: HTMLMediaElement[] = [];
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps()}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
        mediaElementFactory={(kind) => {
          const element = fakeMediaElement(kind);
          mediaElements.push(element);
          return element;
        }}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });
    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(mediaElements[0].play).toHaveBeenCalledTimes(1);

    clock.stepTo(1250);

    expect(useStudioStore.getState().playheadSec).toBe(1.25);
    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(mediaElements[0].pause).not.toHaveBeenCalled();
    expect(clock.pendingFrames).toBe(1);
  });

  it('stops playback and audio at the composition end when looping is off', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip({ duration_sec: 2 })],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    const clock = playbackClock();
    const mediaElements: HTMLMediaElement[] = [];
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps()}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
        mediaElementFactory={(kind) => {
          const element = fakeMediaElement(kind);
          mediaElements.push(element);
          return element;
        }}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });
    clock.stepTo(2000);

    expect(useStudioStore.getState().playheadSec).toBe(2);
    expect(useStudioStore.getState().isPlaying).toBe(false);
    expect(mediaElements[0].play).toHaveBeenCalledTimes(1);
    expect(mediaElements[0].pause).toHaveBeenCalled();
    expect(clock.pendingFrames).toBe(0);
  });

  it('wraps the playhead to zero and restarts audio when looping reaches the end', async () => {
    const project = makeProject();
    const composition = makeComposition({
      audio_clips: [makeAudioClip({ duration_sec: 2 })],
    });
    seedStore(
      project,
      composition,
      [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
    );
    const clock = playbackClock();
    const mediaElements: HTMLMediaElement[] = [];
    render(
      <PreviewPlayer
        project={project}
        composition={composition}
        {...playerProps()}
        clock={clock.clock}
        requestAnimationFrame={clock.requestAnimationFrame}
        cancelAnimationFrame={clock.cancelAnimationFrame}
        mediaElementFactory={(kind) => {
          const element = fakeMediaElement(kind);
          mediaElements.push(element);
          return element;
        }}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /loop/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
    });
    clock.stepTo(2000);

    expect(useStudioStore.getState().playheadSec).toBe(0);
    expect(useStudioStore.getState().isPlaying).toBe(true);
    expect(useStudioStore.getState().loopPlayback).toBe(true);
    expect(mediaElements).toHaveLength(2);
    expect(mediaElements[0].pause).toHaveBeenCalled();
    expect(mediaElements[1].play).toHaveBeenCalledTimes(1);
    expect(clock.pendingFrames).toBe(1);
  });

  it.each([2, 2.5])(
    'starts playback from zero when Play is requested at or past the composition end (%s)',
    async (playheadSec) => {
      const project = makeProject();
      const composition = makeComposition({
        audio_clips: [makeAudioClip({ duration_sec: 2 })],
      });
      seedStore(
        project,
        composition,
        [makeAsset({ id: 'music', kind: 'audio', has_audio: true })],
        playheadSec,
      );
      const clock = playbackClock();
      const mediaElements: HTMLMediaElement[] = [];
      render(
        <PreviewPlayer
          project={project}
          composition={composition}
          {...playerProps()}
          clock={clock.clock}
          requestAnimationFrame={clock.requestAnimationFrame}
          cancelAnimationFrame={clock.cancelAnimationFrame}
          mediaElementFactory={(kind) => {
            const element = fakeMediaElement(kind);
            mediaElements.push(element);
            return element;
          }}
        />,
      );

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /^play$/i }));
      });

      expect(useStudioStore.getState().playheadSec).toBe(0);
      expect(useStudioStore.getState().isPlaying).toBe(true);
      expect(mediaElements[0].play).toHaveBeenCalledTimes(1);
      expect(clock.pendingFrames).toBe(1);
    },
  );

  it('moves the playhead when the scrub bar changes and updates timecode and Bar:Beat', () => {
    // GIVEN a 4.0s timeline at 120 BPM with beat offset 0
    const project = makeProject({ bpm: 120, beat_offset_sec: 0 });
    const composition = makeComposition({
      video_clips: [makeVisualClip({ duration_sec: 4 })],
    });
    seedStore(project, composition, [makeAsset()], 0);
    render(<PreviewPlayer project={project} composition={composition} {...playerProps()} />);

    // WHEN
    fireEvent.change(screen.getByRole('slider'), { target: { value: '1.5' } });

    // THEN
    expect(useStudioStore.getState().playheadSec).toBe(1.5);
    expect(screen.getByText('00:01.50')).toBeTruthy();
    expect(screen.getByText('1:4')).toBeTruthy();
  });

  it('ignores a non-finite scrub value', () => {
    const project = makeProject();
    const composition = makeComposition({
      video_clips: [makeVisualClip({ duration_sec: 4 })],
    });
    seedStore(project, composition, [makeAsset()], 0.5);
    render(<PreviewPlayer project={project} composition={composition} {...playerProps()} />);
    const scrub = screen.getByRole('slider');

    // A real range input sanitizes invalid values, so present the malformed event
    // value through a text input to exercise the handler's defensive guard.
    (scrub as HTMLInputElement).type = 'text';
    fireEvent.change(scrub, { target: { value: 'not-a-number' } });

    expect(useStudioStore.getState().playheadSec).toBe(0.5);
    expect(screen.getByTestId('timecode').textContent).toBe('00:00.50');
  });

  it('toggles Loop Playback on and reflects it with aria-pressed', () => {
    // GIVEN loopPlayback=false
    const project = makeProject();
    const composition = makeComposition({ video_clips: [makeVisualClip()] });
    seedStore(project, composition, [makeAsset()], 0);
    render(<PreviewPlayer project={project} composition={composition} {...playerProps()} />);

    // WHEN
    fireEvent.click(screen.getByRole('button', { name: /loop/i }));

    // THEN
    expect(useStudioStore.getState().loopPlayback).toBe(true);
    expect(
      screen.getByRole('button', { name: /loop/i }).getAttribute('aria-pressed'),
    ).toBe('true');
  });
});
