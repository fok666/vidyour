import { useEffect, useRef, useState, type FormEvent } from 'react';

import { apiClient } from '../api/client';
import { useStudioStore } from '../store/useStudioStore';
import type { AudioCatalogTrack, MediaAsset } from '../types/studio';

export interface AudioCatalogModalProps {
  open?: boolean;
  onClose?: () => void;
  onAdd?: (track: AudioCatalogTrack) => void;
  audioElementFactory?: () => HTMLAudioElement;
}

const PROVIDERS = [
  { value: 'all', label: 'All providers' },
  { value: 'builtin', label: 'Built-in CC0' },
  { value: 'jamendo', label: 'Jamendo' },
  { value: 'freesound', label: 'Freesound' },
];

function parseBpm(value: string): number | undefined {
  if (value.trim() === '') return undefined;
  const bpm = Number(value);
  return Number.isFinite(bpm) && bpm > 0 ? bpm : undefined;
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, '0')}`;
}

function errorMessage(err: unknown, fallback?: string): string {
  if (err instanceof Error) return err.message;
  return fallback ?? String(err);
}

function mergeAssets(existing: MediaAsset[], incoming: MediaAsset[]): MediaAsset[] {
  const byId = new Map(existing.map((asset) => [asset.id, asset]));
  for (const asset of incoming) byId.set(asset.id, asset);
  return Array.from(byId.values());
}

export function AudioCatalogModal(props: AudioCatalogModalProps = {}) {
  const { open = false, onClose, onAdd, audioElementFactory } = props;
  const project = useStudioStore((state) => state.project);

  const [query, setQuery] = useState('');
  const [bpmMin, setBpmMin] = useState('');
  const [bpmMax, setBpmMax] = useState('');
  const [provider, setProvider] = useState('all');
  const [results, setResults] = useState<AudioCatalogTrack[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [importing, setImporting] = useState<Record<string, boolean>>({});
  const [added, setAdded] = useState<Record<string, boolean>>({});

  const audioRef = useRef<HTMLAudioElement | null>(null);

  const stopPreview = (): void => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.onended = null;
    }
    audioRef.current = null;
    setPlayingId(null);
  };

  useEffect(() => {
    if (!open) stopPreview();
  }, [open]);

  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  if (!open) return null;

  const handleSearch = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    setSearching(true);
    setError(null);
    try {
      const tracks = await apiClient.searchAudioCatalog({
        q: query.trim() || undefined,
        bpm_min: parseBpm(bpmMin),
        bpm_max: parseBpm(bpmMax),
        provider,
      });
      setResults(tracks);
      setSearched(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSearching(false);
    }
  };

  const togglePreview = (track: AudioCatalogTrack): void => {
    if (playingId === track.id) {
      stopPreview();
      return;
    }
    stopPreview();
    if (!track.preview_url) return;
    const audio = audioElementFactory ? audioElementFactory() : new Audio();
    audio.src = track.preview_url;
    audio.onended = () => {
      if (audioRef.current === audio) {
        audioRef.current = null;
        setPlayingId(null);
      }
    };
    audioRef.current = audio;
    setPlayingId(track.id);
    const playing = audio.play();
    if (playing && typeof playing.catch === 'function') {
      playing.catch((err: unknown) => {
        if (audioRef.current === audio) {
          audioRef.current = null;
          setPlayingId(null);
          setError(errorMessage(err, 'Preview playback failed'));
        }
      });
    }
  };

  const handleAdd = async (track: AudioCatalogTrack): Promise<void> => {
    if (!project) {
      setError('Open a project before adding audio.');
      return;
    }
    const projectId = project.id;
    setImporting((prev) => ({ ...prev, [track.id]: true }));
    setError(null);
    try {
      const importedAsset = await apiClient.importAudioCatalogTrack(projectId, {
        track_id: track.id,
        provider: track.provider,
      });
      let media: MediaAsset[] = [importedAsset];
      try {
        media = mergeAssets(media, await apiClient.listProjectMedia(projectId));
      } catch {
        // keep the asset returned by the import endpoint
      }
      const state = useStudioStore.getState();
      state.setAssets(mergeAssets(state.assets, media));
      try {
        const refreshed = await apiClient.getProject(projectId);
        if (refreshed && refreshed.id === projectId) state.setProject(refreshed);
      } catch {
        // project BPM refresh is best effort
      }

      setAdded((prev) => ({ ...prev, [track.id]: true }));
      onAdd?.(track);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setImporting((prev) => ({ ...prev, [track.id]: false }));
    }
  };

  const close = (): void => {
    stopPreview();
    onClose?.();
  };

  return (
    <div className="modal-backdrop audio-catalog-backdrop">
      <div
        className="modal audio-catalog-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="audio-catalog-title"
      >
        <header className="modal__header">
          <h2 id="audio-catalog-title">Audio Catalog</h2>
          <button type="button" aria-label="Close audio catalog" onClick={close}>
            ×
          </button>
        </header>

        <form className="audio-catalog__search" onSubmit={handleSearch}>
          <label>
            Search query
            <input
              type="text"
              value={query}
              placeholder="e.g. House, Chill, Drums"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            Min BPM
            <input
              type="number"
              min={1}
              value={bpmMin}
              onChange={(event) => setBpmMin(event.target.value)}
            />
          </label>
          <label>
            Max BPM
            <input
              type="number"
              min={1}
              value={bpmMax}
              onChange={(event) => setBpmMax(event.target.value)}
            />
          </label>
          <label>
            Provider
            <select value={provider} onChange={(event) => setProvider(event.target.value)}>
              {PROVIDERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={searching}>
            Search
          </button>
        </form>

        {error && (
          <p className="audio-catalog__error" role="alert">
            {error}
          </p>
        )}
        {searching && <p className="audio-catalog__status">Searching…</p>}
        {!searching && searched && results.length === 0 && (
          <p className="audio-catalog__status">No tracks found.</p>
        )}

        <ul className="audio-catalog__results" aria-label="Audio catalog results">
          {results.map((track) => (
            <li key={`${track.provider}-${track.id}`} className="audio-catalog__track">
              <div className="audio-catalog__track-info">
                <span className="audio-catalog__track-title">{track.title}</span>
                <span className="audio-catalog__track-meta">
                  {track.artist} · {track.bpm ? `${Math.round(track.bpm)} BPM` : 'BPM unknown'} ·{' '}
                  {formatDuration(track.duration_sec)} · {track.provider}
                  {track.license ? ` · ${track.license}` : ''}
                </span>
                {track.attribution && (
                  <span className="audio-catalog__track-attribution">{track.attribution}</span>
                )}
              </div>
              <div className="audio-catalog__track-actions">
                <button
                  type="button"
                  disabled={!track.preview_url}
                  onClick={() => togglePreview(track)}
                >
                  {playingId === track.id ? 'Stop' : 'Preview'}
                </button>
                <button
                  type="button"
                  disabled={!project || importing[track.id]}
                  onClick={() => void handleAdd(track)}
                >
                  {importing[track.id] ? 'Adding…' : added[track.id] ? 'Added' : 'Add'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export default AudioCatalogModal;
