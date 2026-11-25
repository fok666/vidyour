import type { SnapSubdivision } from '../types/studio';

export function snapToBeatGrid(
  timeSec: number,
  bpm: number,
  beatOffset: number,
  subdivision: SnapSubdivision,
): number {
  if (subdivision === 'off') {
    return timeSec;
  }

  const secondsPerBeat = 60 / bpm;
  const beatsPerGridUnit =
    subdivision === 'bar' ? 4 : subdivision === 'half_beat' ? 0.5 : 1;
  const gridInterval = secondsPerBeat * beatsPerGridUnit;
  const snappedTime =
    beatOffset +
    Math.round((timeSec - beatOffset) / gridInterval) * gridInterval;

  return Math.max(0, snappedTime);
}

export function beatsToSeconds(beats: number, bpm: number): number {
  return (beats * 60) / bpm;
}

export function secondsToBeats(seconds: number, bpm: number): number {
  return (seconds * bpm) / 60;
}

export function formatBarBeat(
  timeSec: number,
  bpm: number,
  beatOffset: number,
): string {
  const beatPosition = Math.max(0, secondsToBeats(timeSec - beatOffset, bpm));
  const wholeBeats = Math.floor(beatPosition);
  const bar = Math.floor(wholeBeats / 4) + 1;
  const beat = (wholeBeats % 4) + 1;

  return `${bar}:${beat}`;
}

export function formatTimecode(seconds: number): string {
  const totalHundredths = Math.round(Math.max(0, seconds) * 100);
  const totalSeconds = Math.floor(totalHundredths / 100);
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  const hundredths = totalHundredths % 100;

  return `${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}.${String(hundredths).padStart(2, '0')}`;
}

export function clampFadeDurations(
  fadeIn: number,
  fadeOut: number,
  durationSec: number,
): { fadeIn: number; fadeOut: number } {
  const duration = Math.max(0, durationSec);
  const clampedFadeIn = Math.min(duration, Math.max(0, fadeIn));
  const clampedFadeOut = Math.min(
    Math.max(0, fadeOut),
    duration - clampedFadeIn,
  );

  return { fadeIn: clampedFadeIn, fadeOut: clampedFadeOut };
}
