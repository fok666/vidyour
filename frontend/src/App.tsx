import { useEffect, useMemo, useRef, useState } from 'react';

import { apiClient } from './api/client';
import AiAnalysisPanel from './components/AiAnalysisPanel';
import AudioCatalogModal from './components/AudioCatalogModal';
import ClipInspector from './components/ClipInspector';
import DetectedPartsBin from './components/DetectedPartsBin';
import ExportModal from './components/ExportModal';
import MediaSourcesPanel from './components/MediaSourcesPanel';
import PreviewPlayer from './components/PreviewPlayer';
import TimelineDock from './components/TimelineDock';
import TopToolbar from './components/TopToolbar';
import { useStudioStore } from './store/useStudioStore';
import type { Composition, TimelineAudioClip, TimelineVisualClip } from './types/studio';

type TimelineClip = TimelineAudioClip | TimelineVisualClip;

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function firstClipId(composition: Composition): string | null {
  return composition.video_clips[0]?.id ?? composition.audio_clips[0]?.id ?? null;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.tagName === 'SELECT'
  );
}

export default function App() {
  const project = useStudioStore((state) => state.project);
  const composition = useStudioStore((state) => state.composition);
  const assets = useStudioStore((state) => state.assets);
  const parts = useStudioStore((state) => state.parts);
  const selectedClipId = useStudioStore((state) => state.selectedClipId);
  const playheadSec = useStudioStore((state) => state.playheadSec);
  const isPlaying = useStudioStore((state) => state.isPlaying);
  const setProject = useStudioStore((state) => state.setProject);
  const setCapabilities = useStudioStore((state) => state.setCapabilities);
  const setAssets = useStudioStore((state) => state.setAssets);
  const setParts = useStudioStore((state) => state.setParts);
  const setComposition = useStudioStore((state) => state.setComposition);
  const setSelectedClipId = useStudioStore((state) => state.setSelectedClipId);
  const setIsPlaying = useStudioStore((state) => state.setIsPlaying);
  const updateClip = useStudioStore((state) => state.updateClip);
  const splitClipAtPlayhead = useStudioStore((state) => state.splitClipAtPlayhead);
  const deleteSelectedClip = useStudioStore((state) => state.deleteSelectedClip);
  const addPartToTimeline = useStudioStore((state) => state.addPartToTimeline);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [audioCatalogOpen, setAudioCatalogOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const readyProjectId = useRef<string | null>(null);
  const savedComposition = useRef<Composition | null>(null);

  useEffect(() => {
    let active = true;

    async function hydrateStudio() {
      setLoading(true);
      setLoadError(null);
      try {
        const [capabilities, projects] = await Promise.all([
          apiClient.getCapabilities(),
          apiClient.listProjects(),
        ]);
        if (!active) return;
        setCapabilities(capabilities);

        const selectedProject =
          projects[0] ?? (await apiClient.createProject({ name: 'My first video' }));
        if (!active) return;
        setProject(selectedProject);

        const [nextAssets, nextParts, nextComposition] = await Promise.all([
          apiClient.listProjectMedia(selectedProject.id),
          apiClient.listParts(selectedProject.id),
          apiClient.getComposition(selectedProject.id),
        ]);
        if (!active) return;

        readyProjectId.current = selectedProject.id;
        savedComposition.current = nextComposition;
        setAssets(nextAssets);
        setParts(nextParts);
        setComposition(nextComposition);
        setSelectedClipId(firstClipId(nextComposition));
      } catch (error) {
        if (active) setLoadError(describeError(error));
      } finally {
        if (active) setLoading(false);
      }
    }

    void hydrateStudio();
    return () => {
      active = false;
    };
  }, [setAssets, setCapabilities, setComposition, setParts, setProject, setSelectedClipId]);

  // Persist user edits only; the initial server snapshot is marked saved during hydration.
  useEffect(() => {
    if (
      !project ||
      !composition ||
      readyProjectId.current !== project.id ||
      savedComposition.current === composition
    ) {
      return undefined;
    }

    const snapshot = composition;
    const timer = globalThis.setTimeout(() => {
      void apiClient
        .saveComposition(project.id, snapshot)
        .then(() => {
          if (readyProjectId.current === project.id) savedComposition.current = snapshot;
        })
        .catch((error: unknown) => setLoadError(describeError(error)));
    }, 500);

    return () => globalThis.clearTimeout(timer);
  }, [composition, project]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || isEditableTarget(event.target)) {
        return;
      }

      if (event.code === 'Space' || event.key === ' ') {
        event.preventDefault();
        setIsPlaying(!useStudioStore.getState().isPlaying);
      } else if (event.key.toLowerCase() === 's' && selectedClipId) {
        event.preventDefault();
        splitClipAtPlayhead(selectedClipId);
      } else if ((event.key === 'Delete' || event.key === 'Backspace') && selectedClipId) {
        event.preventDefault();
        deleteSelectedClip();
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [deleteSelectedClip, selectedClipId, setIsPlaying, splitClipAtPlayhead]);

  const selectedClip = useMemo<TimelineClip | null>(() => {
    if (!composition || !selectedClipId) return null;
    return (
      composition.video_clips.find((clip) => clip.id === selectedClipId) ??
      composition.audio_clips.find((clip) => clip.id === selectedClipId) ??
      null
    );
  }, [composition, selectedClipId]);

  const handleAutoAssemble = async () => {
    if (!project) return;
    try {
      const nextComposition = await apiClient.autoAssembleComposition(project.id);
      savedComposition.current = nextComposition;
      setComposition(nextComposition);
      setSelectedClipId(firstClipId(nextComposition));
    } catch (error) {
      setLoadError(describeError(error));
    }
  };

  const handleCompositionChange = (changes: Partial<TimelineClip>) => {
    if (selectedClipId) updateClip(selectedClipId, changes);
  };

  if (loading) {
    return <main className="studio-loading" role="status">Loading VidYour Studio…</main>;
  }

  if (loadError && !project) {
    return (
      <main className="studio-error" role="alert">
        <h1>VidYour Studio could not load</h1>
        <p>{loadError}</p>
      </main>
    );
  }

  return (
    <main className="studio-shell">
      <TopToolbar onOpenExport={() => setExportOpen(true)} />
      {loadError && <p className="studio-error-banner" role="alert">{loadError}</p>}
      <div className="studio-workspace">
        <aside className="studio-sidebar">
          <MediaSourcesPanel onOpenAudioCatalog={() => setAudioCatalogOpen(true)} />
          <AiAnalysisPanel />
          <DetectedPartsBin
            parts={parts}
            onAdd={addPartToTimeline}
            onAutoAssemble={() => void handleAutoAssemble()}
          />
        </aside>
        <section className="studio-editor" aria-label="Video editor">
          <PreviewPlayer />
          <ClipInspector
            clip={selectedClip}
            bpm={project?.bpm ?? 120}
            onChange={handleCompositionChange}
          />
          <TimelineDock />
        </section>
      </div>
      <AudioCatalogModal open={audioCatalogOpen} onClose={() => setAudioCatalogOpen(false)} />
      <ExportModal open={exportOpen} onClose={() => setExportOpen(false)} />
      <span className="sr-only" aria-live="polite">
        {isPlaying ? `Playback at ${playheadSec.toFixed(2)} seconds` : ''}
        {assets.length ? `${assets.length} media assets loaded` : ''}
      </span>
    </main>
  );
}
