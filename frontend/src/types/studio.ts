export type MediaKind = 'video' | 'photo' | 'live_photo' | 'audio';
export type ResultTarget =
  | 'perfect_loop'
  | 'seamless_flow'
  | 'high_energy'
  | 'chill_story'
  | string;
export type SnapSubdivision = 'bar' | 'beat' | 'half_beat' | 'off';
export type Transition = 'none' | 'crossfade' | 'fade_black' | 'wipe_left';
export type KenBurns = 'none' | 'zoom_in' | 'zoom_out' | 'pan_lr';
export type ColorPreset = 'original' | 'warm' | 'cool' | 'bw' | 'vivid' | 'vignette';

export interface Project {
  id: string;
  name: string;
  bpm: number;
  beat_offset_sec: number;
  resolution_w: number;
  resolution_h: number;
  fps: number;
  ai_context: string;
  result_target: ResultTarget;
  suggested_sequence: unknown[];
  created_at: string;
  updated_at: string;
}

export interface MediaAsset {
  id: string;
  project_id: string;
  kind: MediaKind;
  source: 'google_photos' | 'upload' | 'audio_catalog' | 'builtin';
  filename: string;
  local_path: string;
  still_path?: string | null;
  thumbnail_path?: string | null;
  duration_sec: number;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  has_audio: boolean;
  bpm?: number | null;
  beat_grid?: {
    beat_times: number[];
    downbeats: number[];
    waveform: number[];
  } | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface DetectedPart {
  id: string;
  project_id: string;
  asset_id: string;
  part_type: 'visual_clip' | 'audio_segment';
  start_sec: number;
  end_sec: number;
  beat_length: number;
  label: string;
  score: number;
  ai_reason: string;
  suggested_effect: string;
  clip_path?: string | null;
  thumbnail_path?: string | null;
  created_at?: string;
}

export interface TimelineVisualClip {
  id: string;
  asset_id: string;
  part_id: string | null;
  kind: 'video' | 'photo' | 'live_photo';
  start_sec: number;
  duration_sec: number;
  trim_start_sec: number;
  speed: number;
  transition_in: Transition;
  transition_duration_sec: number;
  ken_burns: KenBurns;
  color_preset: ColorPreset;
  volume: number;
  muted: boolean;
  fade_in_sec: number;
  fade_out_sec: number;
}

export interface TimelineAudioClip {
  id: string;
  asset_id: string;
  part_id: string | null;
  track_index: 0 | 1;
  start_sec: number;
  duration_sec: number;
  trim_start_sec: number;
  volume: number;
  muted: boolean;
  fade_in_sec: number;
  fade_out_sec: number;
}

export interface Composition {
  project_id: string;
  snap_subdivision: SnapSubdivision;
  video_clips: TimelineVisualClip[];
  audio_clips: TimelineAudioClip[];
  updated_at: string;
}

export interface Job {
  id: string;
  project_id: string;
  kind: 'import' | 'analyze' | 'render';
  status: 'queued' | 'running' | 'completed' | 'failed';
  progress: number;
  message: string;
  result: Record<string, unknown>;
  error?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AudioCatalogTrack {
  id: string;
  provider: string;
  title: string;
  artist: string;
  bpm?: number | null;
  duration_sec: number;
  preview_url?: string;
  download_url?: string;
  license?: string;
  attribution?: string;
}

export interface Capabilities {
  google_photos: boolean;
  gemini: boolean;
  bedrock: boolean;
  jamendo: boolean;
  freesound: boolean;
  s3: boolean;
  active_ai_provider: string;
  storage_backend: string;
}
