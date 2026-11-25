import { useEffect, useRef, useState } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { DetectedPart } from '../types/studio';
import { beatsToSeconds } from '../utils/bpm';

type MediaKind = 'audio' | 'video';
type MediaElementFactory = (kind: MediaKind) => HTMLMediaElement;

export interface DetectedPartsBinProps {
  parts?: DetectedPart[];
  onPreview?: (part: DetectedPart) => void;
  onAdd?: (part: DetectedPart) => void;
  onAutoAssemble?: () => void;
  mediaElementFactory?: MediaElementFactory;
}

function fileUrl(path: string): string {
  return `/api/files/${path.replace(/^\/+/, '')}`;
}

interface PartRowProps {
  part: DetectedPart;
  bpm: number | null;
  previewing: boolean;
  onTogglePreview: (part: DetectedPart) => void;
  onHover: (part: DetectedPart | null) => void;
  onAdd: (part: DetectedPart) => void;
  mediaElementFactory?: MediaElementFactory;
}

function InlinePreview({
  part,
  mediaElementFactory,
}: {
  part: DetectedPart;
  mediaElementFactory?: MediaElementFactory;
}) {
  const kind: MediaKind = part.part_type === 'audio_segment' ? 'audio' : 'video';
  const src = part.clip_path ? fileUrl(part.clip_path) : null;
  const ref = useRef<HTMLMediaElement | null>(null);

  useEffect(() => {
    if (!src) return undefined;
    let element = ref.current;
    if (mediaElementFactory) {
      element = mediaElementFactory(kind);
      element.src = src;
    }
    if (!element) return undefined;
    void element.play()?.catch(() => undefined);
    return () => element.pause();
  }, [src, kind, mediaElementFactory]);

  if (!src) return <div className="parts-bin__preview-empty">No preview available</div>;
  if (mediaElementFactory) return null;
  if (kind === 'audio') {
    return (
      <audio
        ref={(el) => {
          ref.current = el;
        }}
        className="parts-bin__preview"
        src={src}
        controls
      />
    );
  }
  return (
    <video
      ref={(el) => {
        ref.current = el;
      }}
      className="parts-bin__preview"
      src={src}
      poster={part.thumbnail_path ? fileUrl(part.thumbnail_path) : undefined}
      muted
      loop
      playsInline
    />
  );
}

function PartRow({
  part,
  bpm,
  previewing,
  onTogglePreview,
  onHover,
  onAdd,
  mediaElementFactory,
}: PartRowProps) {
  const durationSec = bpm ? beatsToSeconds(part.beat_length, bpm) : part.end_sec - part.start_sec;
  const scorePct = Math.round(part.score * 100);
  return (
    <li
      className="parts-bin__item"
      data-part-id={part.id}
      onMouseEnter={() => onHover(part)}
      onMouseLeave={() => onHover(null)}
    >
      <div className="parts-bin__row" title={part.ai_reason}>
        <span className="parts-bin__label">{part.label}</span>
        <span className="parts-bin__beats">{`${part.beat_length}b`}</span>
        <span className="parts-bin__duration">{`${durationSec.toFixed(1)}s`}</span>
        <span className="parts-bin__score">{`${scorePct}%`}</span>
      </div>
      <div className="parts-bin__actions">
        <button
          type="button"
          aria-label={`Preview ${part.label}`}
          aria-pressed={previewing}
          onClick={() => onTogglePreview(part)}
        >
          {previewing ? 'Stop' : 'Preview'}
        </button>
        <button type="button" aria-label={`Add ${part.label}`} onClick={() => onAdd(part)}>
          + Add
        </button>
      </div>
      {previewing ? <InlinePreview part={part} mediaElementFactory={mediaElementFactory} /> : null}
    </li>
  );
}

export function DetectedPartsBin(props: DetectedPartsBinProps = {}) {
  const { onPreview, onAdd, onAutoAssemble, mediaElementFactory } = props;
  const storeParts = useStudioStore((state) => state.parts);
  const project = useStudioStore((state) => state.project);
  const addPartToTimeline = useStudioStore((state) => state.addPartToTimeline);
  const setComposition = useStudioStore((state) => state.setComposition);
  const parts = props.parts ?? storeParts;

  const [clickedId, setClickedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [assembling, setAssembling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const visual = parts.filter((part) => part.part_type === 'visual_clip');
  const audio = parts.filter((part) => part.part_type === 'audio_segment');
  const activeId = clickedId ?? hoveredId;

  const togglePreview = (part: DetectedPart) => {
    setClickedId((current) => (current === part.id ? null : part.id));
    onPreview?.(part);
  };

  const handleAdd = (part: DetectedPart) => {
    if (onAdd) onAdd(part);
    else addPartToTimeline(part);
  };

  const handleAutoAssemble = async () => {
    if (onAutoAssemble) {
      onAutoAssemble();
      return;
    }
    if (!project) return;
    setAssembling(true);
    setError(null);
    try {
      const composition = await apiClient.autoAssembleComposition(project.id);
      const { composition: current, history } = useStudioStore.getState();
      if (current) useStudioStore.setState({ history: [...history, current] });
      setComposition(composition);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAssembling(false);
    }
  };

  const renderList = (items: DetectedPart[], empty: string) =>
    items.length === 0 ? (
      <p className="parts-bin__empty">{empty}</p>
    ) : (
      <ul className="parts-bin__list">
        {items.map((part) => (
          <PartRow
            key={part.id}
            part={part}
            bpm={project?.bpm ?? null}
            previewing={activeId === part.id}
            onTogglePreview={togglePreview}
            onHover={(hovered) => setHoveredId(hovered ? hovered.id : null)}
            onAdd={handleAdd}
            mediaElementFactory={mediaElementFactory}
          />
        ))}
      </ul>
    );

  return (
    <section className="parts-bin" aria-label="Detected parts">
      <h3>Detected Visual Clips</h3>
      {renderList(visual, 'No visual clips detected yet.')}
      <h3>Audio Segments</h3>
      {renderList(audio, 'No audio segments detected yet.')}
      <button
        type="button"
        className="parts-bin__auto-assemble"
        disabled={assembling || (!onAutoAssemble && !project)}
        onClick={() => void handleAutoAssemble()}
      >
        {assembling ? 'Assembling…' : 'Auto-Assemble for Target'}
      </button>
      {error ? (
        <p className="parts-bin__error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

export default DetectedPartsBin;
