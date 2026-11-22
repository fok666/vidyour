# VidYour — BPM-Aware AI Video Composition Studio (Design Spec)

**Date:** 2026-11-22  
**Status:** Draft for Review  
**Target Directory:** `/Users/kornfer/projects/poc/vidyour`

---

## 1. Overview & Goals

**VidYour** is a local-first web application PoC for assembling beat-synchronized video compositions from user galleries (photos, videos, Live Photos) and copyright-free / public-domain music tracks.

### Core User Workflow
1. **Ingest Media**: Import visual assets from **Google Photos** (via Google Photos Picker API OAuth) or local drag-and-drop (including Apple Live Photo `.heic`/`.jpg` + `.mov` pairs), and add one or more audio tracks from a copyright-free / public-domain catalog (**Jamendo**, **Freesound**, built-in CC0 demo BPM tracks) or local upload.
2. **Analyze & Cut**: Automatically extract BPM, beat grid, waveform peaks, and musical energy sections from audio; use AI (**GCP Gemini** primary, pluggable **AWS Bedrock** adapter, plus deterministic local fallback) guided by user-supplied **additional AI context** and a **result target** (e.g., *"perfect loop"*, *"seamless flow"*, *"high-energy cuts"*) to identify key visual moments at BPM-dependent beat lengths and cut them into previewable parts.
3. **Assemble & Edit**: Browse detected visual clips and audio segments, optionally auto-assemble according to the AI's suggested sequence, and arrange/manipulate items on a multi-track **snap-to-BPM** timeline with audio fade-in/fade-out, volume, transitions, photo Ken Burns motion, speed changes, and color presets.
4. **Render & Export**: Render and encode the composition to H.264/AAC MP4 via FFmpeg, store the artifact in **AWS S3** (with local disk fallback when S3 credentials are unset), and return a downloadable link plus inline preview.

### Non-Goals (Out of Scope for PoC)
- Multi-tenant authentication / user accounts (single local workspace with SQLite).
- Distributed multi-node worker infrastructure (Celery/Redis/Kubernetes).
- Keyframe animation curves, chroma keying, or 3D/WebGL compositing beyond lightweight FFmpeg filters.
- Unofficial reverse-engineered Apple iCloud web scraping (Apple media is ingested via local/Live Photo upload).

---

## 2. System Architecture

VidYour uses a **Modular Monolith Backend (Python 3.14 + FastAPI + SQLite + Native FFmpeg)** paired with a **React + Vite + TypeScript** frontend studio.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Frontend (React + Vite + TS)                        │
│  Media/Audio Sources │ AI Prompt & Parts Bin │ Live Preview │ BPM Dock  │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ HTTP REST (/api/*) + Static/Range Media
┌───────────────────────────────────▼─────────────────────────────────────┐
│                     Backend (Python FastAPI)                            │
│  ┌─────────────┐  ┌───────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ API Routers │─▶│  JobManager   │─▶│   Services   │─▶│  SQLite DB  │ │
│  └─────────────┘  │ (async pool)  │  │ AudioAnalyzer│  │ (repos)     │ │
│                   └───────────────┘  │ SceneExtract │  └─────────────┘ │
│                                      │ RenderCompile│                  │
│  Pluggable Providers (configured via .env, graceful local fallbacks):  │
│  • Gallery: GooglePhotosPickerProvider | LocalUploadHandler            │
│  • Audio:   JamendoProvider | FreesoundProvider | BuiltinDemoProvider  │
│  • AI:      GeminiProvider | BedrockProvider | HeuristicFallback       │
│  • Storage: S3StorageProvider (boto3) | LocalStorageProvider           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Repository Structure
```
vidyour/
├── .env.example
├── .gitignore
├── README.md
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                  # FastAPI app, CORS, router mounting, startup init
│   │   ├── config.py                # Pydantic Settings from env vars + capability flags
│   │   ├── db.py                    # SQLite connection, schema init, transaction helpers
│   │   ├── models.py                # Pydantic request/response & domain models
│   │   ├── jobs.py                  # In-process async JobManager (ThreadPoolExecutor)
│   │   ├── api/
│   │   │   ├── config_api.py        # GET /api/config/capabilities
│   │   │   ├── projects.py          # Project CRUD & settings (BPM, ai_context, result_target)
│   │   │   ├── media.py             # Local upload (incl. Live Photo pairing), media streaming
│   │   │   ├── gallery.py           # Google Photos Picker OAuth + session polling + import
│   │   │   ├── audio_catalog.py     # Search royalty-free catalog + import track into project
│   │   │   ├── analysis.py          # Trigger analyze job, list/delete DetectedParts
│   │   │   ├── compositions.py      # Get/save Composition, auto-assemble endpoint
│   │   │   └── renders.py           # Trigger render job, status polling, local download endpoint
│   │   ├── providers/
│   │   │   ├── gallery/
│   │   │   │   └── google_photos.py # OAuth2 + Google Photos Picker API client
│   │   │   ├── audio/
│   │   │   │   ├── base.py          # AudioCatalogProvider protocol
│   │   │   │   ├── builtin.py       # Procedural CC0 multi-BPM demo tracks (90/110/124/140 BPM)
│   │   │   │   ├── jamendo.py       # Jamendo API v3 CC music search
│   │   │   │   └── freesound.py     # Freesound API v2 search
│   │   │   ├── ai/
│   │   │   │   ├── base.py          # AIProvider protocol & structured analysis schema
│   │   │   │   ├── gemini.py        # Google GenAI SDK multimodal video/photo/audio analyzer
│   │   │   │   ├── bedrock.py       # AWS Bedrock Converse API keyframe analyzer
│   │   │   │   └── heuristic.py     # FFmpeg scene-detect + audio energy fallback
│   │   │   └── storage/
│   │   │       ├── base.py          # StorageProvider protocol
│   │   │       ├── s3.py            # boto3 S3 upload + presigned GET URL (supports S3_ENDPOINT_URL)
│   │   │       └── local.py         # Local disk storage + /api/renders/{id}/download URL
│   │   └── services/
│   │       ├── media_probe.py       # ffprobe wrapper + HEIC/thumbnail utilities + path safety
│   │       ├── audio_analyzer.py    # PCM decode + onset/autocorrelation BPM, beat grid, waveform, sections
│   │       ├── scene_extractor.py   # Beat-length snapping + FFmpeg clip/thumbnail slicing
│   │       └── render_compiler.py   # Pure build_render_plan() + FFmpeg runner with progress parsing
│   └── tests/
│       ├── conftest.py              # Temp workspace, FastAPI TestClient, synthetic FFmpeg media fixtures
│       ├── test_audio_analyzer.py
│       ├── test_ai_providers.py
│       ├── test_scene_extractor.py
│       ├── test_render_compiler.py
│       ├── test_storage_providers.py
│       └── test_e2e_pipeline.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts               # Dev proxy /api -> http://localhost:8000
│   ├── src/
│   │   ├── api/client.ts            # Typed API client
│   │   ├── types/studio.ts          # Shared TypeScript interfaces matching backend schemas
│   │   ├── store/useStudioStore.ts  # Zustand store (project, assets, parts, composition, playback, jobs)
│   │   ├── utils/bpm.ts             # Pure snapToBeatGrid, beat formatting, fade clamping helpers
│   │   ├── components/
│   │   │   ├── TopToolbar.tsx
│   │   │   ├── MediaSourcesPanel.tsx
│   │   │   ├── AudioCatalogModal.tsx
│   │   │   ├── AiAnalysisPanel.tsx
│   │   │   ├── DetectedPartsBin.tsx
│   │   │   ├── PreviewPlayer.tsx
│   │   │   ├── ClipInspector.tsx
│   │   │   ├── TimelineDock.tsx
│   │   │   └── ExportModal.tsx
│   │   └── __tests__/
│   │       ├── bpm.test.ts
│   │       └── studioStore.test.ts
└── data/                            # Gitignored local storage (vidyour.db, assets/, clips/, thumbs/, renders/, tmp/)
```

---

## 3. Data Model (SQLite)

All tables live in `data/vidyour.db` with foreign keys enabled (`PRAGMA foreign_keys = ON`).

### `projects`
| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT PK` | UUID4 |
| `name` | `TEXT NOT NULL` | User-visible project name |
| `bpm` | `REAL NOT NULL DEFAULT 120.0` | Active project BPM (auto-updated when first audio is analyzed; editable) |
| `beat_offset_sec` | `REAL NOT NULL DEFAULT 0.0` | First beat alignment offset from primary audio track |
| `resolution_w` | `INTEGER NOT NULL DEFAULT 1280` | Render width (`1280` or `1920`) |
| `resolution_h` | `INTEGER NOT NULL DEFAULT 720` | Render height (`720` or `1080`) |
| `fps` | `INTEGER NOT NULL DEFAULT 30` | Render frame rate |
| `ai_context` | `TEXT NOT NULL DEFAULT ''` | Additional free-text guidance for AI key-moment analysis |
| `result_target` | `TEXT NOT NULL DEFAULT 'seamless_flow'` | Preset (`perfect_loop`, `seamless_flow`, `high_energy`, `chill_story`) or custom description |
| `suggested_sequence_json` | `TEXT NOT NULL DEFAULT '[]'` | AI-recommended ordered `DetectedPart` IDs + transition hints |
| `created_at` / `updated_at` | `TEXT NOT NULL` | ISO-8601 UTC timestamps |

### `media_assets`
| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT PK` | UUID4 |
| `project_id` | `TEXT FK -> projects(id) ON DELETE CASCADE` | Owning project |
| `kind` | `TEXT NOT NULL` | `video \| photo \| live_photo \| audio` |
| `source` | `TEXT NOT NULL` | `google_photos \| upload \| audio_catalog \| builtin` |
| `filename` | `TEXT NOT NULL` | Sanitized original display filename |
| `local_path` | `TEXT NOT NULL` | Relative path inside `data/assets/` (for `live_photo`, points to the motion `.mov`/`.mp4`) |
| `still_path` | `TEXT` | Optional companion still image path for `live_photo` |
| `thumbnail_path` | `TEXT` | Relative path inside `data/thumbs/` |
| `duration_sec` | `REAL NOT NULL` | Probed duration (default `4.0` placeholder for photos) |
| `width` / `height` | `INTEGER` | Video/photo pixel dimensions |
| `fps` | `REAL` | Video frame rate |
| `has_audio` | `INTEGER NOT NULL DEFAULT 0` | `1` if media contains an audio stream |
| `bpm` | `REAL` | Detected BPM (for audio assets) |
| `beat_grid_json` | `TEXT` | JSON: `{ beat_times: number[], downbeats: number[], waveform: number[] }` |
| `metadata_json` | `TEXT NOT NULL DEFAULT '{}'` | License/attribution, Google Photos ID, codec info |
| `created_at` | `TEXT NOT NULL` | ISO-8601 UTC |

### `detected_parts`
| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT PK` | UUID4 |
| `project_id` | `TEXT FK -> projects(id) ON DELETE CASCADE` | Owning project |
| `asset_id` | `TEXT FK -> media_assets(id) ON DELETE CASCADE` | Source asset |
| `part_type` | `TEXT NOT NULL` | `visual_clip \| audio_segment` |
| `start_sec` | `REAL NOT NULL` | Start offset in source asset |
| `end_sec` | `REAL NOT NULL` | End offset in source asset |
| `beat_length` | `REAL NOT NULL` | Duration expressed in project beats (e.g., `2`, `4`, `8`, `16`) |
| `label` | `TEXT NOT NULL` | Short title (e.g. `"Golden Hour Smile"`, `"Main Drop"`) |
| `score` | `REAL NOT NULL` | AI/heuristic salience score `0.0–1.0` |
| `ai_reason` | `TEXT NOT NULL DEFAULT ''` | Explanation from AI or heuristic |
| `suggested_effect` | `TEXT NOT NULL DEFAULT 'none'` | Enum: `none \| zoom_in \| zoom_out \| pan_lr \| crossfade \| fade_black \| wipe_left` |
| `clip_path` | `TEXT` | Relative path to pre-cut preview MP4/WAV (`data/clips/`) |
| `thumbnail_path` | `TEXT` | Relative path to JPEG thumbnail (`data/thumbs/`) |
| `created_at` | `TEXT NOT NULL` | ISO-8601 UTC |

### `compositions`
| Column | Type | Notes |
|---|---|---|
| `project_id` | `TEXT PK FK -> projects(id) ON DELETE CASCADE` | 1:1 active composition per project |
| `snap_subdivision` | `TEXT NOT NULL DEFAULT 'beat'` | `bar \| beat \| half_beat \| off` |
| `tracks_json` | `TEXT NOT NULL` | JSON object `{ video_clips: TimelineVisualClip[], audio_clips: TimelineAudioClip[] }` |
| `updated_at` | `TEXT NOT NULL` | ISO-8601 UTC |

#### Timeline Item Schemas (inside `tracks_json`)
- **`TimelineVisualClip`**:
  - `id: string`, `asset_id: string`, `part_id: string | null`, `kind: "video" | "photo" | "live_photo"`
  - `start_sec: number` (timeline start position), `duration_sec: number` (timeline span)
  - `trim_start_sec: number` (offset inside source asset), `speed: number` (`0.5`–`2.0`, default `1.0`)
  - `transition_in: "none" | "crossfade" | "fade_black" | "wipe_left"`, `transition_duration_sec: number` (default `0.5`)
  - `ken_burns: "none" | "zoom_in" | "zoom_out" | "pan_lr"` (photos only)
  - `color_preset: "original" | "warm" | "cool" | "bw" | "vivid" | "vignette"`
  - `volume: number` (`0.0`–`1.5`, default `0.0` muted when music track exists, user-toggleable), `muted: boolean`, `fade_in_sec: number`, `fade_out_sec: number`
- **`TimelineAudioClip`**:
  - `id: string`, `asset_id: string`, `part_id: string | null`, `track_index: 0 | 1` (`A1` / `A2`)
  - `start_sec: number`, `duration_sec: number`, `trim_start_sec: number`
  - `volume: number` (`0.0`–`1.5`, default `1.0`), `muted: boolean`
  - `fade_in_sec: number` (default `0.0`), `fade_out_sec: number` (default `0.0`)

### `jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT PK` | UUID4 |
| `project_id` | `TEXT FK -> projects(id) ON DELETE CASCADE` | Owning project |
| `kind` | `TEXT NOT NULL` | `import \| analyze \| render` |
| `status` | `TEXT NOT NULL` | `queued \| running \| completed \| failed` |
| `progress` | `INTEGER NOT NULL DEFAULT 0` | `0`–`100` |
| `message` | `TEXT NOT NULL DEFAULT ''` | Current stage description |
| `result_json` | `TEXT NOT NULL DEFAULT '{}'` | Output payload (e.g. `{ storage_backend, s3_key, download_url, duration_sec, size_bytes }`) |
| `error` | `TEXT` | User-readable failure detail if `failed` |
| `created_at` / `updated_at` | `TEXT NOT NULL` | ISO-8601 UTC |

---

## 4. Provider Contracts & Ingestion Flows

### 4.1 Gallery Integration
- **Google Photos Picker API (`GooglePhotosPickerProvider`)**:
  - Enabled when `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` are set.
  - Endpoints:
    - `GET /api/gallery/google/auth-url` → returns Google OAuth2 URL requesting scope `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.
    - `GET /api/gallery/google/callback` → exchanges authorization code for tokens (stored in server memory/local token store for the local PoC) and closes the popup via `postMessage`.
    - `POST /api/gallery/google/sessions` → creates a Google Photos Picker session (`POST https://photospicker.googleapis.com/v1/sessions`) and returns `{ session_id, picker_uri }`.
    - `POST /api/gallery/google/sessions/{session_id}/import?project_id=...` → polls `mediaItemsSet`, downloads selected items via their `baseUrl` (`=dv` for videos, `=d` for photos), probes them with `ffprobe`, and creates `MediaAsset` records via an async `import` job.
- **Local & Apple Live Photo Upload (`POST /api/projects/{project_id}/media/upload`)**:
  - Accepts multipart multi-file uploads (`.mp4`, `.mov`, `.webm`, `.mkv`, `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`).
  - **Live Photo Pairing**: When a batch contains both an image (`.heic`/`.jpg`) and a short `.mov` (≤ 5s) sharing the same case-insensitive filename stem (e.g., `IMG_1042.HEIC` + `IMG_1042.MOV`), pairs them into a single `MediaAsset(kind="live_photo")` using the `.mov` for motion and converting the `.heic` via FFmpeg/Pillow as `still_path` + thumbnail.
  - Standalone `.heic` photos are converted to standard JPEG so browsers and FFmpeg render them uniformly.

### 4.2 Copyright-Free / Public-Domain Audio Catalog (`AudioCatalogProvider`)
- `GET /api/audio-catalog/search?q=&bpm_min=&bpm_max=&provider=all` aggregates results from all active providers:
  - **`BuiltinDemoAudioProvider`** (always active): Ships 4 clean, procedurally synthesized CC0 rhythmic backing tracks generated locally on first use via NumPy/FFmpeg WAV→MP3 at exact tempos (**90 BPM Chill Groove**, **110 BPM Sunset Pulse**, **124 BPM House Drive**, **140 BPM High-Energy Sprint**), each with distinct intro/buildup/drop/outro energy sections so BPM detection, section extraction, and timeline snapping work out of the box with zero API keys.
  - **`JamendoAudioProvider`** (active when `JAMENDO_CLIENT_ID` is set): Queries Jamendo v3 `/tracks` filtered to Creative Commons audio downloads.
  - **`FreesoundAudioProvider`** (active when `FREESOUND_API_KEY` is set): Queries Freesound v2 text search filtered to `license:"Creative Commons 0"` / CC-BY preview/download URLs.
- `POST /api/projects/{project_id}/audio-catalog/import` downloads/copies the selected catalog track into `data/assets/`, runs `AudioAnalyzer`, updates project BPM (if no audio BPM is set yet), and creates the `MediaAsset`.

---

## 5. Analysis, BPM Extraction & AI Key-Moment Pipeline

### 5.1 Audio BPM, Beat Grid & Section Extraction (`AudioAnalyzer`)
1. Decodes any audio file to 22,050 Hz mono float32 PCM via FFmpeg (`-f f32le -ac 1 -ar 22050`).
2. Computes frame-wise spectral-flux / half-wave-rectified energy onset envelope using `numpy`/`scipy`.
3. Estimates **BPM** via autocorrelation + comb-filter scoring across `60–180 BPM` with octave preference (favoring 85–155 BPM when harmonics tie).
4. Tracks **beat timestamps** (`beat_times`) by finding the phase offset (`beat_offset_sec`) that maximizes onset energy alignment along the detected beat period, plus **downbeats** every 4 beats.
5. Generates a 300-bucket normalized peak **waveform** array for fast frontend rendering.
6. Segments the track into bar-aligned **musical energy parts** (e.g., 8-bar or 4-bar windows labeled *Intro*, *Verse / Groove*, *Peak / Drop*, *Breakdown*, *Outro* by RMS energy curve) saved as `DetectedPart(part_type="audio_segment")`.

### 5.2 AI Key-Moment Identification (`AIProvider`)
Protocol:
```python
class AIProvider(Protocol):
    name: str
    def analyze_project_media(
        self,
        assets: list[MediaAsset],
        bpm: float,
        allowed_beat_lengths: list[int],
        ai_context: str,
        result_target: str,
    ) -> AIAnalysisResult: ...
```
- **Beat-Length Computation**: `beat_sec = 60.0 / bpm`. Allowed clip lengths default to `[2, 4, 8, 16]` beats (filtered so durations fall within `0.75s – 12.0s`).
- **`GeminiProvider`** (primary when `GEMINI_API_KEY` is set; model configurable via `GEMINI_MODEL`, default `gemini-2.5-flash`):
  - For videos > 10 MB or > 720p, transcodes a fast 480p proxy (`data/tmp/`) before uploading with `google-genai` Files API (and deletes remote uploaded file after completion).
  - Prompts Gemini with structured JSON output (`response_mime_type="application/json"` + Pydantic schema) containing:
    - Project BPM and exact beat durations (`2 beats = Xs`, `4 beats = Ys`, `8 beats = Zs`).
    - User's `ai_context` and `result_target` (with explicit instructions for `"perfect_loop"` to prefer visually/motion-matched opening & closing clips and for `"seamless_flow"` to favor continuous motion/color continuity).
    - Request for per-asset key moments (`asset_id`, `start_sec`, `end_sec`, `beat_length`, `label`, `score`, `reason`, `suggested_effect`) plus `suggested_sequence` (ordered clip refs).
- **`BedrockProvider`** (activated when `AI_PROVIDER=bedrock` and AWS credentials are configured; model via `BEDROCK_MODEL_ID`, default `amazon.nova-lite-v1:0` or Claude Sonnet):
  - Extracts evenly-spaced timestamped JPEG keyframes (up to 12 frames per video) + photo stills via FFmpeg and sends them via `boto3.client("bedrock-runtime").converse(...)` requesting the exact same JSON schema.
- **`HeuristicFallbackProvider`** (used when `AI_PROVIDER=local` or no AI keys are configured):
  - Uses FFmpeg scene-change detection (`select='gt(scene,0.28)'`, `showinfo`) combined with frame luminance/contrast variance to locate visual transitions and high-activity windows, snapping each segment to `4` or `8` beats.
  - Respects `result_target` deterministically (e.g., for `perfect_loop`, pairs the highest-similarity first/last clips; for `high_energy`, selects shorter `2`/`4`-beat cuts; for `seamless_flow`, suggests `crossfade`).

### 5.3 Backend Scene Slicing (`SceneExtractor`)
For each visual moment returned by `AIProvider`:
1. Computes exact snapped duration `target_dur = beat_length * (60.0 / bpm)`.
2. Centers/clamps `[start_sec, start_sec + target_dur]` within `[0, asset.duration_sec]` (for videos/Live Photos); if asset is shorter than `target_dur`, snaps down to the largest fitting beat multiple.
3. Cuts preview clip via FFmpeg (`-ss <start> -t <dur> -c:v libx264 -preset ultrafast -crf 24 -c:a aac -movflags +faststart`) into `data/clips/<part_id>.mp4` and extracts a mid-clip JPEG thumbnail into `data/thumbs/<part_id>.jpg`.
4. For still photos, generates a thumbnail and registers the `DetectedPart` with `duration = beat_length * beat_sec` and `suggested_effect` (e.g., `zoom_in`).

---

## 6. Interactive Frontend Studio & BPM Timeline

### Key UI Capabilities
- **Capabilities Banner / Badges**: Reads `GET /api/config/capabilities` on mount so the user immediately sees active integrations (`Google Photos`, `Gemini`, `Bedrock`, `Jamendo`, `Freesound`, `AWS S3`) vs. local fallback badges.
- **Media & Audio Sources Panel**:
  - **Google Photos** button (starts OAuth if needed, opens Picker session popup, shows import progress).
  - **Upload Files** dropzone (supports multiple videos, photos, Live Photo pairs, and audio files).
  - **Audio Catalog** modal (search + play preview + one-click add to project).
- **AI Guidance & Detected Parts Bin**:
  - Inputs for `Additional AI Context` and `Result Target` (quick chips for *Perfect Loop*, *Seamless Flow*, *High Energy*, *Chill Story* + custom text input).
  - **Analyze Resources** trigger with live job progress polling (`GET /api/jobs/{id}`).
  - **Detected Visual Clips** & **Audio Segments** lists with beat-length pills, AI score/reason tooltip, hover/click preview, `+ Add` button, and **Auto-Assemble for Target** button (`POST /api/projects/{id}/composition/auto-assemble`).
- **Live Synchronized Preview (`PreviewPlayer`)**:
  - Plays `V1` clips sequentially (HTML5 `<video>` for video/Live Photo clips, styled `<img>` with CSS keyframe Ken Burns for photos, CSS filter preview for color presets) synchronized with `A1`/`A2` audio routed through Web Audio API `GainNode` scheduling for accurate real-time volume and fade-in/fade-out playback.
  - Play/pause (`Space`), scrub bar, timecode + `Bar:Beat` readout, and **Loop Playback** toggle.
- **Multi-Track BPM Timeline (`TimelineDock`)**:
  - Beat/bar ruler driven by `project.bpm` and `project.beat_offset_sec`, with horizontal zoom (`px/sec`).
  - `V1` visual track + `A1` & `A2` audio tracks (rendering waveform peaks + visual fade-in / fade-out diagonal ramp overlays).
  - Pointer-driven horizontal drag to reposition/reorder and edge-handle drag to trim duration.
  - **Snap-to-BPM** (`snapToBeatGrid(timeSec, bpm, beatOffset, subdivision)`): snaps drag starts and trim ends to `Bar` (4 beats), `Beat` (1 beat), `½ Beat` (0.5 beat), or `Off`; holding `Alt` bypasses snap temporarily.
  - Split clip at playhead (`S`), delete selected clip (`Delete`/`Backspace`).
- **Clip Inspector (`ClipInspector`)**:
  - Edits exact start beat/sec, duration (quick beat buttons `2b / 4b / 8b / 16b`), trim offset, volume, **Audio Fade In / Fade Out** sliders, mute, speed (`0.5×–2.0×`), transition (`none`, `crossfade`, `fade_black`, `wipe_left` + duration), photo Ken Burns preset, and color look (`original`, `warm`, `cool`, `bw`, `vivid`, `vignette`).
  - Edits auto-save (debounced 500ms) via `PUT /api/projects/{id}/composition`.

---

## 7. FFmpeg Render Pipeline & S3 Export

### 7.1 `RenderCompiler`
Separated into a pure planner `build_render_plan(project, composition, assets_by_id, output_path) -> RenderPlan` (returns exact FFmpeg inputs, `filter_complex` string, audio/video map labels, and estimated total duration) and `execute_render_plan(plan, progress_cb)` which spawns `ffmpeg -progress pipe:1`.

#### Video Graph Construction
1. Each visual clip on `V1` is normalized into a uniform stream `[v_i]` at `resolution_w x resolution_h`, `fps`, `format=yuv420p`, `setsar=1`:
   - *Video / Live Photo*: `trim=start=...:duration=...`, `setpts=(PTS-STARTPTS)/speed`, `scale=...:force_original_aspect_ratio=increase,crop=W:H`, `fps=FPS`, optional color filter.
   - *Photo*: `-loop 1 -t <dur>` input → scale + optional `zoompan` Ken Burns filter (`zoom_in`, `zoom_out`, `pan_lr`) at target `W x H @ FPS` → optional color filter.
2. Any gap before the first clip or between non-overlapping clips inserts a black filler stream (`color=c=black:s=WxH:r=FPS:d=<gap>`).
3. Adjacent clips with `transition_in != "none"` (or auto ½-beat crossfade when `result_target == "seamless_flow"` and transition is unset) are joined via `xfade=transition=<type>:duration=<d>:offset=<cumulative_offset>`; hard cuts use `concat=n=2:v=1:a=0`.
4. **Perfect Loop Post-Pass**: When `result_target == "perfect_loop"` and total duration `T >= 2.0s`, let overlap `L = min(0.5 * beat_sec, 0.5)`. Split video into `head [0, L]`, `body [L, T-L]`, and `tail [T-L, T]`; blend `tail` into `head` over `L` seconds (`xfade=transition=fade:duration=L:offset=0`), then concatenate `body + loop_bridge` (final duration `T - L`, where the final frame lands cleanly on original timestamp `L` — identical to frame 0 of `body`). Apply the matching `acrossfade=d=L` bridge on the mixed audio track so repeat playback has zero visual jump or audio click.

#### Audio Graph Construction
1. Collects all non-muted `TimelineAudioClip`s plus any non-muted `TimelineVisualClip`s whose source asset `has_audio == True`.
2. For each active audio clip:
   - `atrim=start=<trim_start>:duration=<source_dur>`, `asetpts=PTS-STARTPTS`
   - Speed adjustment via chained `atempo` (for unmuted visual clip audio when `speed != 1.0`)
   - `volume=<volume>`
   - If `fade_in_sec > 0`: `afade=t=in:st=0:d=<fade_in_sec>`
   - If `fade_out_sec > 0`: `afade=t=out:st=<max(0, clip_dur - fade_out_sec)>:d=<fade_out_sec>`
   - Timeline placement: `adelay=<int(start_sec * 1000)>:all=1`
3. Mixes all audio streams with `amix=inputs=N:duration=longest:dropout_transition=0:normalize=0`, trims to total composition duration (`atrim=0:<total_dur>`), and applies `alimiter=limit=0.95` to prevent clipping. If no audio streams exist, inserts a silent stereo track (`anullsrc`) so the exported MP4 always has valid audio+video streams.

#### Encoding Parameters
- Video: `-c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p`
- Audio: `-c:a aac -b:a 192k -ar 48000 -ac 2`
- Container: `-movflags +faststart`
- Progress: parses `out_time_us` from `-progress pipe:1` against `plan.total_duration_sec` to update `Job.progress` (`5%–90%` for render, `90%–100%` for S3 upload).

### 7.2 Storage & Download Link (`StorageProvider`)
- **`S3StorageProvider`** (selected when `S3_BUCKET` is non-empty):
  - Uses `boto3.client("s3", region_name=..., endpoint_url=S3_ENDPOINT_URL or None)`.
  - Uploads to key `renders/{project_id}/{job_id}.mp4` with `ContentType="video/mp4"` and `ContentDisposition='attachment; filename="{safe_project_name}.mp4"'`.
  - Generates a presigned `get_object` URL valid for `S3_PRESIGNED_URL_TTL` seconds (default `86400` = 24h).
- **`LocalStorageProvider`** (fallback when `S3_BUCKET` is unset):
  - Saves rendered MP4 at `data/renders/{job_id}.mp4`.
  - Returns download URL `/api/renders/{job_id}/download` (supports HTTP Range requests for inline preview + `?download=1` for `Content-Disposition: attachment`).

---

## 8. API Surface Summary

| Endpoint | Purpose |
|---|---|
| `GET /api/config/capabilities` | Reports active vs. fallback providers (`google_photos`, `gemini`, `bedrock`, `jamendo`, `freesound`, `s3`) |
| `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}` | Project lifecycle + BPM, `ai_context`, `result_target`, resolution |
| `POST /api/projects/{id}/media/upload` | Multi-file upload with automatic Live Photo pairing & HEIC conversion |
| `GET /api/projects/{id}/media`, `DELETE /api/media/{asset_id}` | List/remove project assets |
| `GET /api/files/{rel_path:path}` | Safe range-enabled streaming for assets, pre-cut clips, and thumbnails |
| `GET /api/gallery/google/status`, `/auth-url`, `/callback` | Google Photos OAuth2 connection status & auth flow |
| `POST /api/gallery/google/sessions`, `POST /api/gallery/google/sessions/{sid}/import` | Google Photos Picker session & media import |
| `GET /api/audio-catalog/search`, `POST /api/projects/{id}/audio-catalog/import` | Royalty-free / CC0 audio search & import |
| `POST /api/projects/{id}/analyze` | Start async BPM + AI key-moment analysis & scene slicing job |
| `GET /api/projects/{id}/parts`, `DELETE /api/parts/{part_id}` | List/remove detected visual clips & audio segments |
| `GET/PUT /api/projects/{id}/composition` | Load/save timeline composition |
| `POST /api/projects/{id}/composition/auto-assemble` | Build timeline from AI suggested sequence + active audio |
| `POST /api/projects/{id}/render` | Start async FFmpeg encode + S3 upload job |
| `GET /api/jobs/{job_id}`, `GET /api/projects/{id}/jobs` | Poll job status, progress, download URL, or error |
| `GET /api/renders/{job_id}/download` | Local fallback download & streaming endpoint |

---

## 9. Configuration (`.env.example`)

```ini
# App & Data
DATA_DIR=../data
CORS_ORIGINS=http://localhost:5173

# AI Provider: auto | gemini | bedrock | local
AI_PROVIDER=auto
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=amazon.nova-lite-v1:0

# Google Photos Picker OAuth (optional)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8000/api/gallery/google/callback

# Royalty-Free Audio Catalogs (optional; built-in CC0 demo tracks always available)
JAMENDO_CLIENT_ID=
FREESOUND_API_KEY=

# S3 Render Storage (optional; falls back to local disk download URL if S3_BUCKET is empty)
S3_BUCKET=
S3_REGION=us-east-1
S3_ENDPOINT_URL=
S3_PRESIGNED_URL_TTL=86400
```

---

## 10. Error Handling, Security & Testing

### Error Handling & Safety
- All file paths passed to `/api/files/{rel_path}` and FFmpeg are resolved against `DATA_DIR` and rejected with `400` if they attempt path traversal.
- Uploaded filenames are sanitized; unsupported MIME/extensions fail cleanly with `400`.
- `JobManager` catches all exceptions (including `subprocess.CalledProcessError` from FFmpeg), records the last 20 lines of stderr in `Job.error`, marks status `failed`, and cleans up temporary files in `data/tmp/`.
- External API errors (e.g., Gemini rate limit/invalid key, Google Photos expired token, S3 permission error) surface explicit actionable messages in job status or HTTP 4xx/5xx responses rather than crashing the server.

### Verification & Testing Plan
1. **Backend (`pytest`)**:
   - `test_audio_analyzer.py`: generates synthetic percussive click tracks at 90, 120, and 140 BPM and verifies estimated BPM is within `±1.5 BPM`, beat timestamps are monotonic, waveform length matches expectation, and energy sections are bar-aligned.
   - `test_ai_providers.py`: tests Gemini JSON parsing (mocked SDK client), Bedrock Converse response parsing (mocked `boto3`), provider selection (`auto`/`gemini`/`bedrock`/`local`), and heuristic fallback honoring `perfect_loop` & `seamless_flow`.
   - `test_scene_extractor.py`: verifies beat-length clamping and real FFmpeg clip slicing + thumbnail creation on a generated test MP4 and PNG.
   - `test_render_compiler.py`: unit-tests `build_render_plan()` for gaps, `xfade` offsets, photo Ken Burns `zoompan`, color presets, `afade` in/out, `perfect_loop` filter generation, and empty-audio fallback.
   - `test_storage_providers.py`: verifies `S3StorageProvider` upload + presigned URL calls (mocked `boto3`) and `LocalStorageProvider` persistence + download endpoint.
   - `test_e2e_pipeline.py`: full API round-trip — create project → upload synthetic video + photo + Live Photo pair + import built-in audio track → run `/analyze` job → call `/auto-assemble` → save composition edits with fades/transitions/effects → run `/render` job → verify rendered MP4 via `ffprobe` (video + audio streams, expected duration) and HTTP 200 download.
2. **Frontend (`vitest` + `tsc` + `vite build`)**:
   - `bpm.test.ts`: unit tests for `snapToBeatGrid` across `bar`/`beat`/`half_beat`/`off` with non-zero beat offsets, time↔beat formatting, and fade duration clamping.
   - `studioStore.test.ts`: unit tests for adding parts, moving/trimming/splitting/deleting clips, updating fades & effects, and undo/reset consistency.
   - Type-check (`npx tsc --noEmit`) and production build (`npm run build`), plus live browser smoke verification against the running backend.