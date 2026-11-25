import { useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react';

import { useStudioStore } from '../store/useStudioStore';
import type {
  Composition,
  MediaAsset,
  Project,
  SnapSubdivision,
  TimelineAudioClip,
  TimelineVisualClip,
} from '../types/studio';
import { formatBarBeat, snapToBeatGrid } from '../utils/bpm';

export interface TimelineDockProps {
  project?: Project | null;
  composition?: Composition | null;
}

type AnyClip = TimelineVisualClip | TimelineAudioClip;

const DEFAULT_ZOOM_PX_PER_SEC = 100;
const MIN_ZOOM_PX_PER_SEC = 20;
const MAX_ZOOM_PX_PER_SEC = 400;
const MIN_TIMELINE_SEC = 16;
const TAIL_PADDING_SEC = 4;
const BEATS_PER_BAR = 4;

const SNAP_OPTIONS: { value: SnapSubdivision; label: string }[] = [
  { value: 'bar', label: 'Bar' },
  { value: 'beat', label: 'Beat' },
  { value: 'half_beat', label: '½ Beat' },
  { value: 'off', label: 'Off' },
];

type DragMode = 'move' | 'trim';

interface DragState {
  clipId: string;
  mode: DragMode;
  startX: number;
  pointerId: number;
}

function FadeRamps({ clip }: { clip: AnyClip }) {
  if (!(clip.duration_sec > 0) || (clip.fade_in_sec <= 0 && clip.fade_out_sec <= 0)) {
    return null;
  }
  const inPct = Math.min(100, (clip.fade_in_sec / clip.duration_sec) * 100);
  const outPct = Math.min(100, (clip.fade_out_sec / clip.duration_sec) * 100);
  return (
    <svg
      className="timeline-clip__fades"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-hidden="true"
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
    >
      {clip.fade_in_sec > 0 && (
        <polygon
          data-testid="fade-in-ramp"
          className="timeline-clip__fade timeline-clip__fade--in"
          points={`0,0 ${inPct},0 0,100`}
          fill="rgba(0,0,0,0.35)"
        />
      )}
      {clip.fade_out_sec > 0 && (
        <polygon
          data-testid="fade-out-ramp"
          className="timeline-clip__fade timeline-clip__fade--out"
          points={`${100 - outPct},0 100,0 100,100`}
          fill="rgba(0,0,0,0.35)"
        />
      )}
    </svg>
  );
}

function Waveform({ peaks }: { peaks: number[] }) {
  if (peaks.length === 0) return null;
  return (
    <div
      className="timeline-clip__waveform"
      aria-hidden="true"
      style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', pointerEvents: 'none' }}
    >
      {peaks.map((peak, index) => (
        <div
          key={index}
          data-testid="waveform-peak"
          className="timeline-clip__peak"
          style={{ flex: 1, height: `${Math.max(0, Math.min(1, peak)) * 100}%` }}
        />
      ))}
    </div>
  );
}

export function TimelineDock(props: TimelineDockProps = {}) {
  const storeProject = useStudioStore((state) => state.project);
  const storeComposition = useStudioStore((state) => state.composition);
  const assets = useStudioStore((state) => state.assets);
  const selectedClipId = useStudioStore((state) => state.selectedClipId);
  const playheadSec = useStudioStore((state) => state.playheadSec);
  const setComposition = useStudioStore((state) => state.setComposition);
  const setSelectedClipId = useStudioStore((state) => state.setSelectedClipId);
  const setPlayheadSec = useStudioStore((state) => state.setPlayheadSec);
  const moveClip = useStudioStore((state) => state.moveClip);
  const trimClip = useStudioStore((state) => state.trimClip);
  const splitClipAtPlayhead = useStudioStore((state) => state.splitClipAtPlayhead);
  const deleteSelectedClip = useStudioStore((state) => state.deleteSelectedClip);

  const project = storeProject ?? props.project ?? null;
  const composition = storeComposition ?? props.composition ?? null;

  const [zoom, setZoom] = useState(DEFAULT_ZOOM_PX_PER_SEC);
  const [dragPreview, setDragPreview] = useState<{ clipId: string; mode: DragMode; deltaPx: number } | null>(null);
  const dragRef = useRef<DragState | null>(null);

  const bpm = project?.bpm && project.bpm > 0 ? project.bpm : 120;
  const beatOffset = project?.beat_offset_sec ?? 0;
  const snap: SnapSubdivision = composition?.snap_subdivision ?? 'beat';
  const videoClips = composition?.video_clips ?? [];
  const audioClips = composition?.audio_clips ?? [];
  const allClips: AnyClip[] = [...videoClips, ...audioClips];

  const contentEnd = allClips.reduce((end, clip) => Math.max(end, clip.start_sec + clip.duration_sec), 0);
  const totalSec = Math.max(MIN_TIMELINE_SEC, contentEnd + TAIL_PADDING_SEC);
  const totalWidth = totalSec * zoom;

  const secPerBeat = 60 / bpm;
  const beatTicks: { timeSec: number; isBar: boolean; beatIndex: number }[] = [];
  for (let beatIndex = 0; beatOffset + beatIndex * secPerBeat <= totalSec; beatIndex += 1) {
    beatTicks.push({
      timeSec: beatOffset + beatIndex * secPerBeat,
      isBar: beatIndex % BEATS_PER_BAR === 0,
      beatIndex,
    });
  }

  const assetById = new Map<string, MediaAsset>(assets.map((asset) => [asset.id, asset]));

  const beginDrag = (
    event: ReactPointerEvent<HTMLElement>,
    clipId: string,
    mode: DragMode,
  ) => {
    if (event.button !== 0) return;
    event.stopPropagation();
    dragRef.current = { clipId, mode, startX: event.clientX, pointerId: event.pointerId };
    setSelectedClipId(clipId);
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // pointer capture is best-effort
    }
  };

  const continueDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.stopPropagation();
    setDragPreview({ clipId: drag.clipId, mode: drag.mode, deltaPx: event.clientX - drag.startX });
  };

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.stopPropagation();
    dragRef.current = null;
    setDragPreview(null);
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // pointer capture is best-effort
    }
    const deltaPx = event.clientX - drag.startX;
    if (deltaPx === 0) return;
    const clip = allClips.find((candidate) => candidate.id === drag.clipId);
    if (!clip) return;
    const deltaSec = deltaPx / zoom;
    if (drag.mode === 'move') {
      moveClip(clip.id, Math.max(0, clip.start_sec + deltaSec), event.altKey);
      return;
    }
    const rawEnd = clip.start_sec + clip.duration_sec + deltaSec;
    const end = event.altKey ? rawEnd : snapToBeatGrid(rawEnd, bpm, beatOffset, snap);
    const duration = end - clip.start_sec;
    if (duration > 0) trimClip(clip.id, duration);
  };

  const cancelDrag = () => {
    dragRef.current = null;
    setDragPreview(null);
  };

  const dragHandlers = (clipId: string, mode: DragMode) => ({
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => beginDrag(event, clipId, mode),
    onPointerMove: continueDrag,
    onPointerUp: endDrag,
    onPointerCancel: cancelDrag,
  });

  const renderClip = (clip: AnyClip, isAudio: boolean) => {
    const asset = assetById.get(clip.asset_id);
    let left = clip.start_sec * zoom;
    let width = clip.duration_sec * zoom;
    if (dragPreview?.clipId === clip.id) {
      if (dragPreview.mode === 'move') left = Math.max(0, left + dragPreview.deltaPx);
      else width = Math.max(1, width + dragPreview.deltaPx);
    }
    const selected = selectedClipId === clip.id;
    const peaks = isAudio ? asset?.beat_grid?.waveform ?? [] : [];
    const label = asset?.filename ?? ('kind' in clip ? clip.kind : 'audio');
    return (
      <div
        key={clip.id}
        data-testid={`clip-${clip.id}`}
        className={`timeline-clip${isAudio ? ' timeline-clip--audio' : ' timeline-clip--video'}${
          selected ? ' timeline-clip--selected' : ''
        }${clip.muted ? ' timeline-clip--muted' : ''}`}
        aria-selected={selected}
        title={label}
        style={{ position: 'absolute', top: 0, bottom: 0, left: `${left}px`, width: `${width}px`, touchAction: 'none' }}
        {...dragHandlers(clip.id, 'move')}
      >
        <Waveform peaks={peaks} />
        <FadeRamps clip={clip} />
        <span className="timeline-clip__label">{label}</span>
        <div
          data-testid={`trim-right-${clip.id}`}
          className="timeline-clip__trim-handle timeline-clip__trim-handle--right"
          aria-label={`Trim end of ${label}`}
          style={{ position: 'absolute', top: 0, bottom: 0, right: 0, width: '6px', cursor: 'ew-resize' }}
          {...dragHandlers(clip.id, 'trim')}
        />
      </div>
    );
  };

  const tracks: { id: string; label: string; clips: AnyClip[]; isAudio: boolean }[] = [
    { id: 'V1', label: 'V1', clips: videoClips, isAudio: false },
    { id: 'A1', label: 'A1', clips: audioClips.filter((clip) => clip.track_index === 0), isAudio: true },
    { id: 'A2', label: 'A2', clips: audioClips.filter((clip) => clip.track_index === 1), isAudio: true },
  ];

  const handleSnapChange = (value: SnapSubdivision) => {
    if (!composition) return;
    setComposition({ ...composition, snap_subdivision: value });
  };

  const handleRulerClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setPlayheadSec((event.clientX - rect.left) / zoom);
  };

  return (
    <section className="timeline-dock" aria-label="Timeline">
      <div className="timeline-dock__toolbar">
        <label className="timeline-dock__control">
          Snap
          <select
            aria-label="Snap"
            value={snap}
            disabled={!composition}
            onChange={(event) => handleSnapChange(event.target.value as SnapSubdivision)}
          >
            {SNAP_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="timeline-dock__control">
          Zoom
          <input
            type="range"
            aria-label="Zoom"
            min={MIN_ZOOM_PX_PER_SEC}
            max={MAX_ZOOM_PX_PER_SEC}
            step={10}
            value={zoom}
            onChange={(event) => setZoom(Number(event.target.value) || DEFAULT_ZOOM_PX_PER_SEC)}
          />
        </label>
        <button
          type="button"
          disabled={!selectedClipId}
          onClick={() => selectedClipId && splitClipAtPlayhead(selectedClipId)}
        >
          Split
        </button>
        <button type="button" disabled={!selectedClipId} onClick={() => deleteSelectedClip()}>
          Delete
        </button>
      </div>
      <div className="timeline-dock__body" style={{ display: 'flex' }}>
        <div className="timeline-dock__track-labels">
          <div className="timeline-dock__ruler-spacer" />
          {tracks.map((track) => (
            <div key={track.id} className="timeline-dock__track-label">
              {track.label}
            </div>
          ))}
        </div>
        <div className="timeline-dock__scroll" style={{ overflowX: 'auto', flex: 1 }}>
          <div className="timeline-dock__content" style={{ position: 'relative', width: `${totalWidth}px` }}>
            <div
              className="timeline-dock__ruler"
              data-testid="timeline-ruler"
              style={{ position: 'relative', height: '20px' }}
              onClick={handleRulerClick}
            >
              {beatTicks.map((tick) => (
                <div
                  key={tick.beatIndex}
                  className={`timeline-dock__tick${tick.isBar ? ' timeline-dock__tick--bar' : ''}`}
                  style={{ position: 'absolute', left: `${tick.timeSec * zoom}px` }}
                >
                  {tick.isBar ? formatBarBeat(tick.timeSec, bpm, beatOffset) : null}
                </div>
              ))}
            </div>
            {tracks.map((track) => (
              <div
                key={track.id}
                className="timeline-dock__lane"
                data-testid={`lane-${track.id}`}
                style={{ position: 'relative', height: '48px' }}
                onPointerDown={() => setSelectedClipId(null)}
              >
                {track.clips.map((clip) => renderClip(clip, track.isAudio))}
              </div>
            ))}
            <div
              className="timeline-dock__playhead"
              data-testid="timeline-playhead"
              aria-hidden="true"
              style={{ position: 'absolute', top: 0, bottom: 0, left: `${playheadSec * zoom}px`, width: '1px', pointerEvents: 'none' }}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

export default TimelineDock;
