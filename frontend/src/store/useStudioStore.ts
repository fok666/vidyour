import { create, type StoreApi, type UseBoundStore } from 'zustand';

import { apiClient } from '../api/client';
import type {
  Composition,
  Capabilities,
  DetectedPart,
  Job,
  MediaAsset,
  Project,
  TimelineAudioClip,
  TimelineVisualClip,
} from '../types/studio';
import { beatsToSeconds, clampFadeDurations, snapToBeatGrid } from '../utils/bpm';

export interface StudioState {
  capabilities: Capabilities | null;
  googlePhotosStatus: { configured: boolean; connected: boolean } | null;
  googlePhotosAuthUrl: string | null;
  googlePhotosSession: { session_id: string; picker_uri: string } | null;
  project: Project | null;
  assets: MediaAsset[];
  parts: DetectedPart[];
  composition: Composition | null;
  jobs: Record<string, Job>;
  selectedClipId: string | null;
  playheadSec: number;
  isPlaying: boolean;
  loopPlayback: boolean;
  history: Composition[];
  setProject: (project: Project | null) => void;
  setCapabilities: (capabilities: Capabilities | null) => void;
  setGooglePhotosStatus: (
    status: { configured: boolean; connected: boolean } | null,
  ) => void;
  setGooglePhotosAuthUrl: (authUrl: string | null) => void;
  setGooglePhotosSession: (
    session: { session_id: string; picker_uri: string } | null,
  ) => void;
  loadCapabilities: () => Promise<void>;
  loadGooglePhotosStatus: () => Promise<void>;
  loadGooglePhotosAuthUrl: () => Promise<void>;
  createGooglePhotosSession: () => Promise<void>;
  importGooglePhotosSession: (sessionId: string, projectId: string) => Promise<void>;
  setAssets: (assets: MediaAsset[]) => void;
  setParts: (parts: DetectedPart[]) => void;
  setComposition: (composition: Composition | null) => void;
  setSelectedClipId: (clipId: string | null) => void;
  setPlayheadSec: (timeSec: number) => void;
  setIsPlaying: (playing: boolean) => void;
  setLoopPlayback: (looping: boolean) => void;
  addPartToTimeline: (part: DetectedPart) => void;
  moveClip: (clipId: string, startSec: number, bypassSnap?: boolean) => void;
  trimClip: (clipId: string, durationSec: number) => void;
  splitClipAtPlayhead: (clipId: string) => void;
  deleteSelectedClip: () => void;
  updateClip: (
    clipId: string,
    changes: Partial<TimelineVisualClip | TimelineAudioClip>,
  ) => void;
  undo: () => void;
  pollJob: (jobId: string) => Promise<void>;
  resetComposition: () => void;
}

export type StudioStore = UseBoundStore<StoreApi<StudioState>>;

export interface StudioScheduler {
  setTimeout: typeof globalThis.setTimeout;
  clearTimeout: typeof globalThis.clearTimeout;
}

const JOB_POLL_INTERVAL_MS = 1000;

type AnyClip = TimelineVisualClip | TimelineAudioClip;

function clampClipFades<T extends AnyClip>(clip: T): T {
  const { fadeIn, fadeOut } = clampFadeDurations(
    clip.fade_in_sec,
    clip.fade_out_sec,
    clip.duration_sec,
  );
  return { ...clip, fade_in_sec: fadeIn, fade_out_sec: fadeOut };
}

function trackEnd(clips: AnyClip[]): number {
  return clips.reduce((end, clip) => Math.max(end, clip.start_sec + clip.duration_sec), 0);
}

function isActiveJob(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running';
}

export function createStudioStore(
  api: typeof apiClient = apiClient,
  clock: () => number = Date.now,
  idGenerator: () => string = () => globalThis.crypto.randomUUID(),
  scheduler: StudioScheduler = globalThis,
): StudioStore {
  const pollTimers = new Map<string, ReturnType<typeof globalThis.setTimeout>>();

  return create<StudioState>((set, get) => {
    const stamp = (composition: Composition): Composition => ({
      ...composition,
      updated_at: new Date(clock()).toISOString(),
    });

    /** Apply a composition mutation, recording the previous snapshot for undo. */
    const commit = (mutate: (composition: Composition) => Composition | null): void => {
      const current = get().composition;
      if (!current) return;
      const next = mutate(current);
      if (!next || next === current) return;
      set({ composition: stamp(next), history: [...get().history, current] });
    };

    const mapClip = (
      composition: Composition,
      clipId: string,
      update: (clip: AnyClip) => AnyClip,
    ): Composition | null => {
      let found = false;
      const video_clips = composition.video_clips.map((clip) => {
        if (clip.id !== clipId) return clip;
        found = true;
        return update(clip) as TimelineVisualClip;
      });
      const audio_clips = composition.audio_clips.map((clip) => {
        if (clip.id !== clipId) return clip;
        found = true;
        return update(clip) as TimelineAudioClip;
      });
      return found ? { ...composition, video_clips, audio_clips } : null;
    };

    const ensureComposition = (): Composition | null => {
      const { composition, project } = get();
      if (composition) return composition;
      if (!project) return null;
      return {
        project_id: project.id,
        snap_subdivision: 'beat',
        video_clips: [],
        audio_clips: [],
        updated_at: new Date(clock()).toISOString(),
      };
    };

    const storeJob = (job: Job): void => {
      set({ jobs: { ...get().jobs, [job.id]: job } });
    };

    const schedulePoll = (jobId: string): void => {
      const existing = pollTimers.get(jobId);
      if (existing !== undefined) scheduler.clearTimeout(existing);
      const timer = scheduler.setTimeout(async () => {
        pollTimers.delete(jobId);
        try {
          const job = await api.getJob(jobId);
          storeJob(job);
          if (isActiveJob(job)) schedulePoll(jobId);
        } catch (error) {
          const previous = get().jobs[jobId];
          if (previous) {
            storeJob({
              ...previous,
              status: 'failed',
              error: error instanceof Error ? error.message : String(error),
            });
          }
        }
      }, JOB_POLL_INTERVAL_MS);
      pollTimers.set(jobId, timer);
    };

    return {
      capabilities: null,
      googlePhotosStatus: null,
      googlePhotosAuthUrl: null,
      googlePhotosSession: null,
      project: null,
      assets: [],
      parts: [],
      composition: null,
      jobs: {},
      selectedClipId: null,
      playheadSec: 0,
      isPlaying: false,
      loopPlayback: false,
      history: [],
      setCapabilities: (capabilities) => set({ capabilities }),
      setGooglePhotosStatus: (googlePhotosStatus) => set({ googlePhotosStatus }),
      setGooglePhotosAuthUrl: (googlePhotosAuthUrl) => set({ googlePhotosAuthUrl }),
      setGooglePhotosSession: (googlePhotosSession) => set({ googlePhotosSession }),
      loadCapabilities: async () => {
        set({ capabilities: await api.getCapabilities() });
      },
      loadGooglePhotosStatus: async () => {
        set({ googlePhotosStatus: await api.getGooglePhotosStatus() });
      },
      loadGooglePhotosAuthUrl: async () => {
        const { auth_url } = await api.getGooglePhotosAuthUrl();
        set({ googlePhotosAuthUrl: auth_url });
      },
      createGooglePhotosSession: async () => {
        set({ googlePhotosSession: await api.createGooglePhotosSession() });
      },
      importGooglePhotosSession: async (sessionId, projectId) => {
        const job = await api.importGooglePhotosSession(sessionId, projectId);
        storeJob(job);
        if (isActiveJob(job)) await get().pollJob(job.id);
      },
      setProject: (project) => set({ project }),
      setAssets: (assets) => set({ assets }),
      setParts: (parts) => set({ parts }),
      setComposition: (composition) => set({ composition }),
      setSelectedClipId: (selectedClipId) => set({ selectedClipId }),
      setPlayheadSec: (timeSec) => set({ playheadSec: Math.max(0, timeSec) }),
      setIsPlaying: (isPlaying) => set({ isPlaying }),
      setLoopPlayback: (loopPlayback) => set({ loopPlayback }),
      addPartToTimeline: (part) => {
        const { project, assets } = get();
        const composition = ensureComposition();
        if (!project || !composition) return;
        const durationSec = beatsToSeconds(part.beat_length, project.bpm);
        let next: Composition;
        if (part.part_type === 'visual_clip') {
          const assetKind = assets.find((asset) => asset.id === part.asset_id)?.kind;
          const kind: TimelineVisualClip['kind'] =
            assetKind === 'photo' || assetKind === 'live_photo' ? assetKind : 'video';
          const hasMusic = composition.audio_clips.length > 0;
          const clip: TimelineVisualClip = {
            id: idGenerator(),
            asset_id: part.asset_id,
            part_id: part.id,
            kind,
            start_sec: trackEnd(composition.video_clips),
            duration_sec: durationSec,
            trim_start_sec: part.start_sec,
            speed: 1,
            transition_in: 'none',
            transition_duration_sec: 0,
            ken_burns: 'none',
            color_preset: 'original',
            volume: hasMusic ? 0 : 1,
            muted: hasMusic,
            fade_in_sec: 0,
            fade_out_sec: 0,
          };
          next = { ...composition, video_clips: [...composition.video_clips, clip] };
        } else {
          const trackClips = composition.audio_clips.filter((clip) => clip.track_index === 0);
          const clip: TimelineAudioClip = {
            id: idGenerator(),
            asset_id: part.asset_id,
            part_id: part.id,
            track_index: 0,
            start_sec: trackEnd(trackClips),
            duration_sec: durationSec,
            trim_start_sec: part.start_sec,
            volume: 1,
            muted: false,
            fade_in_sec: 0,
            fade_out_sec: 0,
          };
          next = { ...composition, audio_clips: [...composition.audio_clips, clip] };
        }
        set({
          composition: stamp(next),
          history: get().composition ? [...get().history, composition] : get().history,
        });
      },
      moveClip: (clipId, startSec, bypassSnap = false) => {
        const project = get().project;
        commit((composition) => {
          const target =
            bypassSnap || !project
              ? startSec
              : snapToBeatGrid(
                  startSec,
                  project.bpm,
                  project.beat_offset_sec,
                  composition.snap_subdivision,
                );
          return mapClip(composition, clipId, (clip) => ({
            ...clip,
            start_sec: Math.max(0, target),
          }));
        });
      },
      trimClip: (clipId, durationSec) => {
        if (!(durationSec > 0)) return;
        commit((composition) =>
          mapClip(composition, clipId, (clip) =>
            clampClipFades({ ...clip, duration_sec: durationSec }),
          ),
        );
      },
      splitClipAtPlayhead: (clipId) => {
        const playhead = get().playheadSec;
        commit((composition) => {
          const splitList = <T extends AnyClip>(clips: T[]): T[] | null => {
            const index = clips.findIndex((clip) => clip.id === clipId);
            if (index < 0) return null;
            const clip = clips[index];
            const offset = playhead - clip.start_sec;
            if (offset <= 0 || offset >= clip.duration_sec) return null;
            const speed = 'speed' in clip ? clip.speed : 1;
            const head = clampClipFades({ ...clip, duration_sec: offset, fade_out_sec: 0 });
            const tail = clampClipFades({
              ...clip,
              id: idGenerator(),
              start_sec: playhead,
              duration_sec: clip.duration_sec - offset,
              trim_start_sec: clip.trim_start_sec + offset * speed,
              fade_in_sec: 0,
              ...('transition_in' in clip
                ? { transition_in: 'none', transition_duration_sec: 0 }
                : {}),
            });
            return [...clips.slice(0, index), head, tail, ...clips.slice(index + 1)];
          };
          const video = splitList(composition.video_clips);
          if (video) return { ...composition, video_clips: video };
          const audio = splitList(composition.audio_clips);
          if (audio) return { ...composition, audio_clips: audio };
          return null;
        });
      },
      deleteSelectedClip: () => {
        const selected = get().selectedClipId;
        if (!selected) return;
        commit((composition) => {
          const video_clips = composition.video_clips.filter((clip) => clip.id !== selected);
          const audio_clips = composition.audio_clips.filter((clip) => clip.id !== selected);
          if (
            video_clips.length === composition.video_clips.length &&
            audio_clips.length === composition.audio_clips.length
          ) {
            return null;
          }
          return { ...composition, video_clips, audio_clips };
        });
        set({ selectedClipId: null });
      },
      updateClip: (clipId, changes) => {
        commit((composition) =>
          mapClip(composition, clipId, (clip) =>
            clampClipFades({ ...clip, ...changes, id: clip.id } as AnyClip),
          ),
        );
      },
      undo: () => {
        const history = get().history;
        if (history.length === 0) return;
        set({
          composition: history[history.length - 1],
          history: history.slice(0, -1),
        });
      },
      pollJob: async (jobId) => {
        schedulePoll(jobId);
      },
      resetComposition: () => {
        commit((composition) => ({ ...composition, video_clips: [], audio_clips: [] }));
      },
    };
  });
}

export const useStudioStore = createStudioStore();
