import { describe, expect, it } from 'vitest';
import {
  beatsToSeconds,
  clampFadeDurations,
  formatBarBeat,
  formatTimecode,
  secondsToBeats,
  snapToBeatGrid,
} from '../utils/bpm';

describe('BPM timeline utilities', () => {
  it('snaps to the nearest bar grid point', () => {
    const result = snapToBeatGrid(1.9, 120, 0.1, 'bar');

    expect(result).toBeCloseTo(2.1);
  });

  it('snaps to the nearest beat grid point', () => {
    const result = snapToBeatGrid(0.8, 120, 0.1, 'beat');

    expect(result).toBeCloseTo(0.6);
  });

  it('snaps to the nearest half-beat grid point', () => {
    const result = snapToBeatGrid(0.76, 120, 0.1, 'half_beat');

    expect(result).toBeCloseTo(0.85);
  });

  it('leaves time unchanged when snapping is off', () => {
    const result = snapToBeatGrid(1.37, 120, 0.1, 'off');

    expect(result).toBe(1.37);
  });

  it('clamps a negative snapped time to zero', () => {
    const result = snapToBeatGrid(-0.4, 120, 0.1, 'beat');

    expect(result).toBe(0);
  });

  it('converts beats to seconds', () => {
    const result = beatsToSeconds(8, 120);

    expect(result).toBe(4);
  });

  it('converts seconds to beats', () => {
    const result = secondsToBeats(2.5, 120);

    expect(result).toBe(5);
  });

  it('uses the supplied BPM when converting seconds to beats', () => {
    expect(secondsToBeats(2, 60)).toBe(2);
  });

  it('formats time as a one-based bar and beat position', () => {
    const result = formatBarBeat(2.5, 120, 0);

    expect(result).toBe('2:2');
  });

  it('accounts for a non-zero beat offset when formatting bar and beat', () => {
    expect(formatBarBeat(2.05, 120, 0)).toBe('2:1');
    expect(formatBarBeat(2.05, 120, 0.1)).toBe('1:4');
  });

  it('formats seconds as a padded timecode with hundredths', () => {
    const result = formatTimecode(65.25);

    expect(result).toBe('01:05.25');
  });

  it('reduces overlapping fade-out duration to fit the clip', () => {
    const result = clampFadeDurations(1.5, 1.2, 2.0);

    expect(result).toEqual({ fadeIn: 1.5, fadeOut: 0.5 });
  });

  it('clamps negative fade-in duration to zero', () => {
    const result = clampFadeDurations(-0.5, 0.4, 2.0);

    expect(result).toEqual({ fadeIn: 0, fadeOut: 0.4 });
  });

  it('clamps fade-in duration to the clip duration', () => {
    const result = clampFadeDurations(3.0, 0.4, 2.0);

    expect(result).toEqual({ fadeIn: 2.0, fadeOut: 0 });
  });
});
