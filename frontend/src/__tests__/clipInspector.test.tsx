import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ClipInspector } from '../components/ClipInspector';
import { useStudioStore } from '../store/useStudioStore';
import type { Composition, Project, TimelineAudioClip, TimelineVisualClip } from '../types/studio';

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

function makeVisualClip(overrides: Partial<TimelineVisualClip> = {}): TimelineVisualClip {
  return {
    id: 'v1',
    asset_id: 'a1',
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

function makeComposition(clip: TimelineVisualClip): Composition {
  return {
    project_id: 'p1',
    snap_subdivision: 'beat',
    video_clips: [clip],
    audio_clips: [],
    updated_at: NOW,
  };
}

/** Wires the inspector to the real store: selected clip in, store mutation out. */
function Harness() {
  const composition = useStudioStore((s) => s.composition);
  const selectedClipId = useStudioStore((s) => s.selectedClipId);
  const project = useStudioStore((s) => s.project);
  const updateClip = useStudioStore((s) => s.updateClip);
  const clip = composition?.video_clips.find((c) => c.id === selectedClipId) ?? null;
  return (
    <ClipInspector
      clip={clip}
      bpm={project?.bpm ?? 120}
      onChange={(changes) => {
        if (selectedClipId) updateClip(selectedClipId, changes);
      }}
    />
  );
}

function setup(overrides: Partial<TimelineVisualClip> = {}, bpm = 120) {
  const clip = makeVisualClip(overrides);
  useStudioStore.setState({
    project: makeProject({ bpm }),
    composition: makeComposition(clip),
    selectedClipId: clip.id,
    playheadSec: 0,
    history: [],
  });
  render(<Harness />);
}

function storedClip(): TimelineVisualClip {
  const clip = useStudioStore.getState().composition?.video_clips.find((c) => c.id === 'v1');
  if (!clip) throw new Error('clip v1 missing from store');
  return clip;
}

function change(element: HTMLElement, value: string) {
  fireEvent.change(element, { target: { value } });
}

describe('ClipInspector', () => {
  afterEach(() => {
    cleanup();
  });

  it('updates start_sec from the Start (sec) input and reflects it as Start Beat 3', () => {
    setup({ start_sec: 0 });
    change(screen.getByLabelText(/start \(sec\)/i), '1.5');
    expect(storedClip().start_sec).toBe(1.5);
    const beatInput = screen.getByLabelText(/start beat/i) as HTMLInputElement;
    expect(Number(beatInput.value)).toBe(3);
  });

  it('ignores empty and non-finite numeric input', () => {
    setup({ start_sec: 1.5 });
    const input = screen.getByLabelText(/start \(sec\)/i) as HTMLInputElement;

    change(input, '');
    expect(storedClip().start_sec).toBe(1.5);

    // Number inputs sanitize strings like "Infinity", so expose that raw value
    // to the handler to exercise its non-finite guard.
    Object.defineProperty(input, 'value', { configurable: true, get: () => 'Infinity' });
    fireEvent.change(input);
    expect(storedClip().start_sec).toBe(1.5);
  });

  it('converts the Start Beat input to start_sec at 120 BPM', () => {
    setup({ start_sec: 0 });
    change(screen.getByLabelText(/start beat/i), '4');
    expect(storedClip().start_sec).toBe(2.0);
  });

  it.each([0, -120])('uses the 120 BPM fallback when BPM is %s', (bpm) => {
    setup({ start_sec: 0 }, bpm);
    change(screen.getByLabelText(/start beat/i), '4');
    expect(storedClip().start_sec).toBe(2.0);
  });

  it('sets duration_sec to 8 beats when the 8b quick button is clicked', () => {
    setup({ kind: 'photo', duration_sec: 2.0 });
    fireEvent.click(screen.getByRole('button', { name: /^8b$/i }));
    expect(storedClip().duration_sec).toBe(4.0);
  });

  it('updates trim_start_sec from the Trim Offset input', () => {
    setup({ kind: 'video', trim_start_sec: 0 });
    change(screen.getByLabelText(/trim offset/i), '1.25');
    expect(storedClip().trim_start_sec).toBe(1.25);
  });

  it('updates volume from the Volume slider', () => {
    setup({ volume: 1.0, muted: false });
    change(screen.getByLabelText(/volume/i), '0.6');
    expect(storedClip().volume).toBe(0.6);
  });

  it('allows a visual clip volume of 1.5 and stores the edited value', () => {
    setup({ kind: 'video', volume: 1.0, muted: false });
    const volumeInput = screen.getByLabelText(/volume/i) as HTMLInputElement;

    expect(volumeInput.max).toBe('1.5');
    change(volumeInput, '1.5');
    expect(storedClip().volume).toBe(1.5);
  });

  it('turns muted off when the Mute control is toggled', () => {
    setup({ kind: 'video', muted: true, volume: 0 });
    fireEvent.click(screen.getByLabelText(/mute/i));
    expect(storedClip().muted).toBe(false);
  });

  it('updates fade_in_sec from the Fade In slider', () => {
    setup({ duration_sec: 2.0, fade_in_sec: 0, fade_out_sec: 0.5 });
    change(screen.getByLabelText(/fade in/i), '0.75');
    expect(storedClip().fade_in_sec).toBe(0.75);
  });

  it('clamps fade_out_sec so fades never exceed the clip duration', () => {
    setup({ duration_sec: 2.0, fade_in_sec: 1.5, fade_out_sec: 0 });
    change(screen.getByLabelText(/fade out/i), '1.0');
    const clip = storedClip();
    expect(clip.fade_out_sec).toBe(0.5);
    expect(clip.fade_in_sec + clip.fade_out_sec).toBe(2.0);
  });

  it('does not emit unchanged fade values when duration increases', () => {
    const emitted: Array<Partial<TimelineAudioClip | TimelineVisualClip>> = [];
    render(
      <ClipInspector
        clip={makeVisualClip({ duration_sec: 2, fade_in_sec: 0.25, fade_out_sec: 0.25 })}
        bpm={120}
        onChange={(changes) => {
          emitted.push(changes);
        }}
      />,
    );

    change(screen.getByLabelText(/^duration \(sec\)$/i), '3');

    expect(emitted).toEqual([{ duration_sec: 3 }]);
  });

  it('updates transition_in from the transition dropdown', () => {
    setup({ transition_in: 'none' });
    change(screen.getByLabelText(/^transition(?! duration)/i), 'crossfade');
    expect(storedClip().transition_in).toBe('crossfade');
  });

  it('updates transition_duration_sec from the Transition Duration input', () => {
    setup({ transition_in: 'crossfade', transition_duration_sec: 0.5 });
    change(screen.getByLabelText(/transition duration/i), '0.75');
    expect(storedClip().transition_duration_sec).toBe(0.75);
  });

  it('updates ken_burns from the Ken Burns dropdown on a photo clip', () => {
    setup({ kind: 'photo', ken_burns: 'none' });
    change(screen.getByLabelText(/ken burns/i), 'pan_lr');
    expect(storedClip().ken_burns).toBe('pan_lr');
  });

  it('updates color_preset from the Color Look dropdown', () => {
    setup({ color_preset: 'original' });
    change(screen.getByLabelText(/color look/i), 'warm');
    expect(storedClip().color_preset).toBe('warm');
  });

  it('updates speed from the Speed input on a video clip', () => {
    setup({ kind: 'video', speed: 1.0 });
    change(screen.getByLabelText(/speed/i), '1.5');
    expect(storedClip().speed).toBe(1.5);
  });
});
