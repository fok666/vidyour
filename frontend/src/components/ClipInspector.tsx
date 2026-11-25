import type { ChangeEvent } from 'react';

import type {
  ColorPreset,
  KenBurns,
  TimelineAudioClip,
  TimelineVisualClip,
  Transition,
} from '../types/studio';
import { beatsToSeconds, clampFadeDurations, secondsToBeats } from '../utils/bpm';

export interface ClipInspectorProps {
  clip?: TimelineAudioClip | TimelineVisualClip | null;
  bpm?: number;
  onChange?: (changes: Partial<TimelineAudioClip | TimelineVisualClip>) => void;
}

const QUICK_BEATS: ReadonlyArray<number> = [2, 4, 8, 16];
const TRANSITIONS: ReadonlyArray<Transition> = ['none', 'crossfade', 'fade_black', 'wipe_left'];
const KEN_BURNS: ReadonlyArray<KenBurns> = ['none', 'zoom_in', 'zoom_out', 'pan_lr'];
const COLOR_PRESETS: ReadonlyArray<ColorPreset> = ['original', 'warm', 'cool', 'bw', 'vivid', 'vignette'];
const MIN_SPEED = 0.5;
const MAX_SPEED = 2.0;
const MIN_DURATION_SEC = 0.05;

function isVisualClip(clip: TimelineAudioClip | TimelineVisualClip): clip is TimelineVisualClip {
  return 'kind' in clip;
}

function round(value: number, digits = 3): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Parses a numeric input value; returns null for empty or invalid input. */
function readNumber(event: ChangeEvent<HTMLInputElement>): number | null {
  const raw = event.target.value;
  if (raw.trim() === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function renderOptions(values: ReadonlyArray<string>) {
  return values.map((value) => (
    <option key={value} value={value}>
      {value}
    </option>
  ));
}

export function ClipInspector({ clip = null, bpm = 120, onChange }: ClipInspectorProps = {}) {
  if (!clip) {
    return (
      <aside className="clip-inspector clip-inspector--empty" aria-label="Clip Inspector">
        <h2>Clip Inspector</h2>
        <p>Select a clip on the timeline to edit its properties.</p>
      </aside>
    );
  }

  const emit = (changes: Partial<TimelineAudioClip | TimelineVisualClip>) => onChange?.(changes);
  const safeBpm = bpm > 0 ? bpm : 120;
  const visual = isVisualClip(clip) ? clip : null;
  const startBeat = round(secondsToBeats(clip.start_sec, safeBpm));

  /** Builds an input change handler that ignores empty or invalid numbers. */
  const onNumber = (apply: (value: number) => void) => (event: ChangeEvent<HTMLInputElement>) => {
    const value = readNumber(event);
    if (value !== null) apply(value);
  };

  const emitFades = (fadeIn: number, fadeOut: number) => {
    const fades = clampFadeDurations(fadeIn, fadeOut, clip.duration_sec);
    emit({ fade_in_sec: fades.fadeIn, fade_out_sec: fades.fadeOut });
  };

  const setDuration = (durationSec: number) => {
    const duration = Math.max(MIN_DURATION_SEC, durationSec);
    const fades = clampFadeDurations(clip.fade_in_sec, clip.fade_out_sec, duration);
    const changes: Partial<TimelineAudioClip | TimelineVisualClip> = { duration_sec: duration };
    if (fades.fadeIn !== clip.fade_in_sec) changes.fade_in_sec = fades.fadeIn;
    if (fades.fadeOut !== clip.fade_out_sec) changes.fade_out_sec = fades.fadeOut;
    emit(changes);
  };

  const handleStartSec = onNumber((value) => emit({ start_sec: Math.max(0, value) }));
  const handleStartBeat = onNumber((value) =>
    emit({ start_sec: Math.max(0, beatsToSeconds(value, safeBpm)) }),
  );
  const handleDuration = onNumber(setDuration);
  const handleTrim = onNumber((value) => emit({ trim_start_sec: Math.max(0, value) }));
  const handleVolume = onNumber((value) => emit({ volume: clamp(value, 0, 1.5) }));
  const handleFadeIn = onNumber((value) => emitFades(value, clip.fade_out_sec));
  const handleFadeOut = onNumber((value) => emitFades(clip.fade_in_sec, value));
  const handleSpeed = onNumber((value) => emit({ speed: clamp(value, MIN_SPEED, MAX_SPEED) }));
  const handleTransitionDuration = onNumber((value) =>
    emit({ transition_duration_sec: clamp(value, 0, clip.duration_sec) }),
  );

  const durationBeats = round(secondsToBeats(clip.duration_sec, safeBpm), 2);

  return (
    <aside className="clip-inspector" aria-label="Clip Inspector">
      <h2>Clip Inspector</h2>
      <p className="clip-inspector__meta">
        {visual ? visual.kind : `audio track A${(clip as TimelineAudioClip).track_index + 1}`} · {clip.id}
      </p>

      <fieldset className="clip-inspector__group">
        <legend>Timing</legend>
        <label>
          Start (sec)
          <input type="number" min={0} step={0.01} value={round(clip.start_sec)} onChange={handleStartSec} />
        </label>
        <label>
          Start Beat
          <input type="number" min={0} step={1} value={startBeat} onChange={handleStartBeat} />
        </label>
        <label>
          Duration (sec)
          <input
            type="number"
            min={MIN_DURATION_SEC}
            step={0.01}
            value={round(clip.duration_sec)}
            onChange={handleDuration}
          />
        </label>
        <span className="clip-inspector__beats">{durationBeats} beats</span>
        <div className="clip-inspector__quick-beats" role="group" aria-label="Quick duration in beats">
          {QUICK_BEATS.map((beats) => (
            <button
              key={beats}
              type="button"
              className={Math.abs(durationBeats - beats) < 1e-6 ? 'active' : undefined}
              onClick={() => setDuration(beatsToSeconds(beats, safeBpm))}
            >
              {beats}b
            </button>
          ))}
        </div>
        {visual?.kind !== 'photo' && (
          <label>
            Trim Offset (sec)
            <input
              type="number"
              min={0}
              step={0.01}
              value={round(clip.trim_start_sec)}
              onChange={handleTrim}
            />
          </label>
        )}
        {visual && visual.kind !== 'photo' && (
          <label>
            Speed (×)
            <input
              type="number"
              min={MIN_SPEED}
              max={MAX_SPEED}
              step={0.05}
              value={visual.speed}
              onChange={handleSpeed}
            />
          </label>
        )}
      </fieldset>

      <fieldset className="clip-inspector__group">
        <legend>Audio</legend>
        <label>
          Volume
          <input type="range" min={0} max={1.5} step={0.01} value={clip.volume} onChange={handleVolume} />
        </label>
        <label>
          <input
            type="checkbox"
            checked={clip.muted}
            onChange={(event) => emit({ muted: event.target.checked })}
          />
          Mute
        </label>
        <label>
          Audio Fade In
          <input
            type="range"
            min={0}
            max={clip.duration_sec}
            step={0.01}
            value={clip.fade_in_sec}
            onChange={handleFadeIn}
          />
        </label>
        <label>
          Audio Fade Out
          <input
            type="range"
            min={0}
            max={clip.duration_sec}
            step={0.01}
            value={clip.fade_out_sec}
            onChange={handleFadeOut}
          />
        </label>
      </fieldset>

      {visual && (
        <fieldset className="clip-inspector__group">
          <legend>Look</legend>
          <label>
            Transition
            <select
              value={visual.transition_in}
              onChange={(event) => emit({ transition_in: event.target.value as Transition })}
            >
              {renderOptions(TRANSITIONS)}
            </select>
          </label>
          <label>
            Transition Duration (sec)
            <input
              type="number"
              min={0}
              step={0.05}
              value={visual.transition_duration_sec}
              disabled={visual.transition_in === 'none'}
              onChange={handleTransitionDuration}
            />
          </label>
          {visual.kind === 'photo' && (
            <label>
              Ken Burns
              <select
                value={visual.ken_burns}
                onChange={(event) => emit({ ken_burns: event.target.value as KenBurns })}
              >
                {renderOptions(KEN_BURNS)}
              </select>
            </label>
          )}
          <label>
            Color Look
            <select
              value={visual.color_preset}
              onChange={(event) => emit({ color_preset: event.target.value as ColorPreset })}
            >
              {renderOptions(COLOR_PRESETS)}
            </select>
          </label>
        </fieldset>
      )}
    </aside>
  );
}

export default ClipInspector;
