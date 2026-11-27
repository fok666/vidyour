import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { CSSProperties, JSX } from 'react';

import { useStudioStore } from '../store/useStudioStore';
import type {
  ColorPreset,
  Composition,
  KenBurns,
  MediaAsset,
  Project,
  TimelineAudioClip,
  TimelineVisualClip,
} from '../types/studio';
import { clampFadeDurations, formatBarBeat, formatTimecode } from '../utils/bpm';

export interface PreviewPlayerProps {
  project?: Project | null;
  composition?: Composition | null;
  audioContextFactory?: () => AudioContext;
  clock?: () => number;
  requestAnimationFrame?: (callback: FrameRequestCallback) => number;
  cancelAnimationFrame?: (handle: number) => void;
  mediaElementFactory?: (kind: 'audio' | 'video') => HTMLMediaElement;
}

/** URL under which the backend serves files stored relative to DATA_DIR. */
export function fileUrl(localPath: string): string {
  return `/api/files/${localPath.replace(/^\/+/, '')}`;
}

/** CSS filter approximations of the FFmpeg colour presets used at render time. */
export const COLOR_FILTERS: Record<ColorPreset, string> = {
  original: 'none',
  warm: 'sepia(0.35) saturate(1.25) hue-rotate(-10deg)',
  cool: 'saturate(1.1) hue-rotate(15deg) brightness(1.03)',
  bw: 'grayscale(1) contrast(1.1)',
  vivid: 'saturate(1.6) contrast(1.15)',
  vignette: 'contrast(1.05)',
};

const KEN_BURNS_KEYFRAMES = `
@keyframes vidyour-ken-burns-zoom_in { from { transform: scale(1); } to { transform: scale(1.2); } }
@keyframes vidyour-ken-burns-zoom_out { from { transform: scale(1.2); } to { transform: scale(1); } }
@keyframes vidyour-ken-burns-pan_lr { from { transform: scale(1.15) translateX(-6%); } to { transform: scale(1.15) translateX(6%); } }
`;

function clipEnd(clip: { start_sec: number; duration_sec: number }): number {
  return clip.start_sec + clip.duration_sec;
}

export function compositionDuration(composition: Composition | null | undefined): number {
  if (!composition) return 0;
  const clips = [...composition.video_clips, ...composition.audio_clips];
  return clips.reduce((max, clip) => Math.max(max, clipEnd(clip)), 0);
}

export function activeVisualClip(
  composition: Composition | null | undefined,
  timeSec: number,
): TimelineVisualClip | null {
  if (!composition) return null;
  return (
    composition.video_clips.find(
      (clip) => timeSec >= clip.start_sec && timeSec < clipEnd(clip),
    ) ?? null
  );
}

interface AudioVoice {
  clip: TimelineAudioClip;
  element: HTMLMediaElement;
  source: AudioNode;
  gain: GainNode;
  started: boolean;
}

/**
 * Schedule a clip's volume envelope (fade-in, sustain, fade-out) on a GainNode.
 * `now` is the AudioContext time that corresponds to `playheadSec` on the timeline.
 */
export function scheduleClipGain(
  gain: GainNode,
  clip: TimelineAudioClip,
  playheadSec: number,
  now: number,
): void {
  const param = gain.gain;
  const volume = clip.muted ? 0 : clip.volume;
  const { fadeIn, fadeOut } = clampFadeDurations(
    clip.fade_in_sec,
    clip.fade_out_sec,
    clip.duration_sec,
  );
  const delay = Math.max(0, clip.start_sec - playheadSec);
  const offset = Math.max(0, playheadSec - clip.start_sec);
  const clipStart = now + delay;
  const fadeOutStart = clip.duration_sec - fadeOut;
  const at = (clipTime: number) => clipStart + (clipTime - offset);

  param.cancelScheduledValues(now);
  if (delay > 0) param.setValueAtTime(0, now);

  if (fadeIn > 0 && offset < fadeIn) {
    param.setValueAtTime((volume * offset) / fadeIn, clipStart);
    param.linearRampToValueAtTime(volume, at(fadeIn));
  } else if (fadeOut > 0 && offset >= fadeOutStart) {
    const remaining = clip.duration_sec - offset;
    param.setValueAtTime((volume * remaining) / fadeOut, clipStart);
  } else {
    param.setValueAtTime(volume, clipStart);
  }

  if (fadeOut > 0) {
    if (offset < fadeOutStart) param.setValueAtTime(volume, at(fadeOutStart));
    param.linearRampToValueAtTime(0, at(clip.duration_sec));
  } else {
    param.setValueAtTime(0, at(clip.duration_sec));
  }
}

function defaultAudioContextFactory(): AudioContext {
  const Ctor =
    (globalThis as { AudioContext?: typeof AudioContext }).AudioContext ??
    (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) throw new Error('Web Audio API is not available');
  return new Ctor();
}

function defaultMediaElementFactory(kind: 'audio' | 'video'): HTMLMediaElement {
  return document.createElement(kind);
}

function safePlay(element: HTMLMediaElement): void {
  try {
    const result = element.play();
    if (result && typeof result.catch === 'function') result.catch(() => undefined);
  } catch {
    /* media playback unavailable */
  }
}

function safePause(element: HTMLMediaElement): void {
  try {
    element.pause();
  } catch {
    /* media playback unavailable */
  }
}

function safeSetCurrentTime(element: HTMLMediaElement, timeSec: number): void {
  try {
    element.currentTime = Math.max(0, timeSec);
  } catch {
    /* not seekable yet */
  }
}

export function PreviewPlayer(props: PreviewPlayerProps = {}): JSX.Element {
  const {
    audioContextFactory = defaultAudioContextFactory,
    clock = () => performance.now(),
    requestAnimationFrame: raf = (cb) => window.requestAnimationFrame(cb),
    cancelAnimationFrame: caf = (handle) => window.cancelAnimationFrame(handle),
    mediaElementFactory = defaultMediaElementFactory,
  } = props;

  const storeProject = useStudioStore((state) => state.project);
  const storeComposition = useStudioStore((state) => state.composition);
  const assets = useStudioStore((state) => state.assets);
  const playheadSec = useStudioStore((state) => state.playheadSec);
  const isPlaying = useStudioStore((state) => state.isPlaying);
  const loopPlayback = useStudioStore((state) => state.loopPlayback);
  const setPlayheadSec = useStudioStore((state) => state.setPlayheadSec);
  const setIsPlaying = useStudioStore((state) => state.setIsPlaying);
  const setLoopPlayback = useStudioStore((state) => state.setLoopPlayback);

  const project = props.project !== undefined ? props.project : storeProject;
  const composition = props.composition !== undefined ? props.composition : storeComposition;
  const bpm = project?.bpm ?? 120;
  const beatOffset = project?.beat_offset_sec ?? 0;
  const totalSec = compositionDuration(composition);

  const assetsById = useMemo(() => {
    const map = new Map<string, MediaAsset>();
    for (const asset of assets) map.set(asset.id, asset);
    return map;
  }, [assets]);

  // Keep the latest values reachable from the animation loop without restarting it.
  const latest = useRef({ composition, assetsById, totalSec, loopPlayback, clock, raf, caf });
  latest.current = { composition, assetsById, totalSec, loopPlayback, clock, raf, caf };

  const audioContextRef = useRef<AudioContext | null>(null);
  const voicesRef = useRef<AudioVoice[]>([]);
  const frameRef = useRef<number | null>(null);
  const originRef = useRef<{ clockMs: number; playheadSec: number }>({ clockMs: 0, playheadSec: 0 });
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const stopVoices = useCallback(() => {
    for (const voice of voicesRef.current) {
      safePause(voice.element);
      try {
        voice.source.disconnect();
        voice.gain.disconnect();
      } catch {
        /* ignore */
      }
    }
    voicesRef.current = [];
  }, []);

  const stopEngine = useCallback(() => {
    if (frameRef.current !== null) {
      latest.current.caf(frameRef.current);
      frameRef.current = null;
    }
    stopVoices();
  }, [stopVoices]);

  const startVoicesDue = useCallback((timeSec: number) => {
    for (const voice of voicesRef.current) {
      if (voice.started) continue;
      if (timeSec >= voice.clip.start_sec && timeSec < clipEnd(voice.clip)) {
        voice.started = true;
        safeSetCurrentTime(
          voice.element,
          voice.clip.trim_start_sec + (timeSec - voice.clip.start_sec),
        );
        safePlay(voice.element);
      }
    }
    for (const voice of voicesRef.current) {
      if (voice.started && timeSec >= clipEnd(voice.clip)) safePause(voice.element);
    }
  }, []);

  const startEngine = useCallback(
    (fromSec: number) => {
      stopEngine();
      const { composition: comp, assetsById: byId } = latest.current;
      originRef.current = { clockMs: latest.current.clock(), playheadSec: fromSec };

      // Audio tracks A1/A2 routed through Web Audio GainNodes.
      const audioClips = (comp?.audio_clips ?? []).filter((clip) => clipEnd(clip) > fromSec);
      if (audioClips.length > 0) {
        let ctx = audioContextRef.current;
        if (!ctx) {
          try {
            ctx = audioContextFactory();
            audioContextRef.current = ctx;
          } catch {
            ctx = null;
          }
        }
        if (ctx) {
          if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined);
          const now = ctx.currentTime;
          for (const clip of audioClips) {
            const asset = byId.get(clip.asset_id);
            if (!asset) continue;
            const element = mediaElementFactory('audio');
            element.src = fileUrl(asset.local_path);
            element.preload = 'auto';
            const source = ctx.createMediaElementSource(element);
            const gain = ctx.createGain();
            source.connect(gain);
            gain.connect(ctx.destination);
            scheduleClipGain(gain, clip, fromSec, now);
            voicesRef.current.push({ clip, element, source, gain, started: false });
          }
        }
      }
      startVoicesDue(fromSec);

      const tick = () => {
        const state = latest.current;
        const elapsed = (state.clock() - originRef.current.clockMs) / 1000;
        const time = originRef.current.playheadSec + elapsed;
        if (state.totalSec > 0 && time >= state.totalSec) {
          if (state.loopPlayback) {
            setPlayheadSec(0);
            startEngine(0);
            return;
          }
          setPlayheadSec(state.totalSec);
          stopEngine();
          setIsPlaying(false);
          return;
        }
        setPlayheadSec(time);
        startVoicesDue(time);
        frameRef.current = state.raf(tick);
      };
      frameRef.current = latest.current.raf(tick);
    },
    [audioContextFactory, mediaElementFactory, setIsPlaying, setPlayheadSec, startVoicesDue, stopEngine],
  );

  // Start/stop the playback engine whenever the store's playing flag or the timeline changes.
  useEffect(() => {
    if (!isPlaying) {
      stopEngine();
      return undefined;
    }
    let from = useStudioStore.getState().playheadSec;
    if (totalSec > 0 && from >= totalSec) {
      from = 0;
      setPlayheadSec(0);
    }
    startEngine(from);
    return () => stopEngine();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, composition, assetsById]);

  useEffect(
    () => () => {
      stopEngine();
      const ctx = audioContextRef.current;
      audioContextRef.current = null;
      if (ctx && ctx.state !== 'closed') void ctx.close().catch(() => undefined);
    },
    [stopEngine],
  );

  const activeClip = activeVisualClip(composition, playheadSec);
  const activeAsset = activeClip ? assetsById.get(activeClip.asset_id) ?? null : null;
  const clipOffset = activeClip ? playheadSec - activeClip.start_sec : 0;
  const isVideo = activeClip !== null && activeClip.kind !== 'photo';

  // Keep the HTML5 <video> in sync with the timeline playhead and clip properties.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !activeClip || !isVideo) return;
    const target = activeClip.trim_start_sec + clipOffset * activeClip.speed;
    try {
      video.playbackRate = activeClip.speed;
      video.muted = activeClip.muted;
      video.volume = Math.min(1, Math.max(0, activeClip.volume));
    } catch {
      /* ignore */
    }
    if (!isPlaying || Math.abs((video.currentTime || 0) - target) > 0.25) {
      safeSetCurrentTime(video, target);
    }
    if (isPlaying) {
      if (video.paused) safePlay(video);
    } else {
      safePause(video);
    }
  }, [activeClip, isVideo, clipOffset, isPlaying]);

  const handleScrub = (value: number) => {
    if (!Number.isFinite(value)) return;
    setPlayheadSec(value);
    if (useStudioStore.getState().isPlaying) startEngine(value);
  };

  const renderVisual = () => {
    if (!activeClip || !activeAsset) {
      return <div className="h-full w-full bg-black" data-testid="preview-empty" />;
    }
    const filter = COLOR_FILTERS[activeClip.color_preset] ?? 'none';
    const transitionActive =
      activeClip.transition_in !== 'none' &&
      activeClip.transition_duration_sec > 0 &&
      clipOffset < activeClip.transition_duration_sec;
    const opacity = transitionActive ? clipOffset / activeClip.transition_duration_sec : 1;
    const baseStyle: CSSProperties = {
      filter,
      opacity,
      width: '100%',
      height: '100%',
      objectFit: 'cover',
    };
    const vignette =
      activeClip.color_preset === 'vignette' ? (
        <div
          className="pointer-events-none absolute inset-0"
          style={{ boxShadow: 'inset 0 0 120px 40px rgba(0,0,0,0.75)' }}
        />
      ) : null;

    if (isVideo) {
      return (
        <>
          <video
            key={activeClip.id}
            ref={videoRef}
            src={fileUrl(activeAsset.local_path)}
            playsInline
            preload="auto"
            style={baseStyle}
          />
          {vignette}
        </>
      );
    }

    const kenBurns: KenBurns = activeClip.ken_burns;
    const playDuration = activeClip.duration_sec / (activeClip.speed || 1);
    const style: CSSProperties =
      kenBurns === 'none'
        ? baseStyle
        : {
            ...baseStyle,
            animationName: `vidyour-ken-burns-${kenBurns}`,
            animationDuration: `${Math.max(0.01, playDuration)}s`,
            animationTimingFunction: 'linear',
            animationFillMode: 'both',
            animationDelay: `${-clipOffset / (activeClip.speed || 1)}s`,
            animationPlayState: isPlaying ? 'running' : 'paused',
          };
    return (
      <>
        <img
          key={activeClip.id}
          src={fileUrl(activeAsset.local_path)}
          alt={activeAsset.filename}
          className={kenBurns === 'none' ? 'ken-burns-none' : `ken-burns ken-burns-${kenBurns}`}
          style={style}
        />
        {vignette}
      </>
    );
  };

  return (
    <section className="flex flex-col gap-2 rounded-lg bg-neutral-900 p-3 text-neutral-100" aria-label="Preview">
      <style>{KEN_BURNS_KEYFRAMES}</style>
      <div
        className="relative w-full overflow-hidden bg-black"
        style={{
          aspectRatio: `${project?.resolution_w ?? 16} / ${project?.resolution_h ?? 9}`,
        }}
      >
        {renderVisual()}
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          className="rounded bg-indigo-600 px-3 py-1 text-sm font-medium"
          onClick={() => setIsPlaying(!isPlaying)}
        >
          {isPlaying ? 'Pause' : 'Play'}
        </button>
        <input
          type="range"
          aria-label="Scrub"
          className="flex-1"
          min={0}
          max={Math.max(totalSec, 0)}
          step="any"
          value={Math.min(playheadSec, Math.max(totalSec, 0))}
          onChange={(event) => handleScrub(Number(event.target.value))}
        />
        <span className="font-mono text-sm" data-testid="timecode">
          {formatTimecode(playheadSec)}
        </span>
        <span className="font-mono text-xs text-neutral-400">/ {formatTimecode(totalSec)}</span>
        <span className="font-mono text-sm" data-testid="bar-beat" title="Bar:Beat">
          {formatBarBeat(playheadSec, bpm, beatOffset)}
        </span>
        <button
          type="button"
          aria-pressed={loopPlayback}
          className={`rounded px-3 py-1 text-sm ${loopPlayback ? 'bg-emerald-600' : 'bg-neutral-700'}`}
          onClick={() => setLoopPlayback(!loopPlayback)}
        >
          Loop
        </button>
      </div>
    </section>
  );
}

export default PreviewPlayer;
