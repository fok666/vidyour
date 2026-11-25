"""Render-plan data structure and compiler entry points."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app import config as app_config
from app.models import Composition, Project
from app.services.media_probe import resolve_safe_path


@dataclass
class RenderPlan:
    command: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    filter_complex: str = ""
    video_map: str = ""
    audio_map: str = ""
    output_path: Path = field(default_factory=Path)
    total_duration_sec: float = 0.0


EPS = 1e-3
VIDEO_KINDS = ("video", "live_photo")
TRANSITIONS = {"crossfade": "fade", "fade_black": "fadeblack", "wipe_left": "wipeleft"}
COLOR_FILTERS = {
    "warm": "colorbalance=rm=0.08:gm=0.02:bm=-0.08",
    "cool": "colorbalance=rm=-0.08:bm=0.08",
    "bw": "hue=s=0",
    "vivid": "eq=saturation=1.4:contrast=1.05",
    "vignette": "vignette=PI/5",
}
KEN_BURNS = {
    "zoom_in": ("min(1+0.2*on/{n},1.2)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "zoom_out": ("max(1.2-0.2*on/{n},1)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "pan_lr": ("1.2", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
    "pan_left": ("1.2", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
    "pan_right": ("1.2", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
}


def _f(value: float) -> str:
    return str(round(float(value), 4))


def _get_asset(assets_by_id: dict[str, Any], asset_id: str) -> Any:
    asset = assets_by_id.get(asset_id)
    if asset is None:
        raise ValueError(f"Unknown asset: {asset_id}")
    return asset


def build_render_plan(
    project: Project,
    composition: Composition,
    assets_by_id: dict[str, Any],
    output_path: str | Path,
) -> RenderPlan:
    data_dir = app_config.settings.data_dir
    w, h, fps = project.resolution_w, project.resolution_h, project.fps
    half_beat = 60.0 / project.bpm / 2.0 if project.bpm else 0.25
    out_path = resolve_safe_path(data_dir, output_path)

    visuals = sorted(composition.video_clips, key=lambda c: c.start_sec)
    audios = sorted(composition.audio_clips, key=lambda c: c.start_sec)
    total = max(
        [c.start_sec + c.duration_sec for c in visuals]
        + [c.start_sec + c.duration_sec for c in audios]
        + [0.0]
    )
    if total <= EPS:
        raise ValueError("Composition is empty")

    input_args: list[str] = []
    inputs: list[str] = []
    graph: list[str] = []
    audio_labels: list[str] = []

    def add_input(path: Path, pre: list[str]) -> int:
        input_args.extend(pre + ["-i", str(path)])
        inputs.append(str(path))
        return len(inputs) - 1

    # Plan visual segments: (clip or None for gap, start, duration, transition into this segment).
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    for clip in visuals:
        if clip.start_sec > cursor + EPS:
            segments.append({"clip": None, "dur": clip.start_sec - cursor, "trans": None})
        trans = None
        if segments and segments[-1]["clip"] is not None and abs(clip.start_sec - cursor) <= EPS:
            name = TRANSITIONS.get(clip.transition_in)
            dur = clip.transition_duration_sec
            if name is None and project.result_target == "seamless_flow":
                name, dur = "fade", half_beat
            if name is not None:
                dur = min(dur, segments[-1]["dur"], clip.duration_sec)
                if dur > EPS:
                    trans = (name, dur)
        segments.append({"clip": clip, "dur": clip.duration_sec, "trans": trans})
        cursor = max(cursor, clip.start_sec + clip.duration_sec)
    if total > cursor + EPS:
        segments.append({"clip": None, "dur": total - cursor, "trans": None})

    for seg in segments:
        seg["overlap"] = 0.0
        seg["tail"] = 0.0
    for i, seg in enumerate(segments):
        if not seg["trans"]:
            continue
        trans_duration = seg["trans"][1]
        previous = segments[i - 1]
        if trans_duration < previous["dur"] - EPS:
            # Move the incoming clip earlier by the transition duration and
            # extend its source so its original timeline end stays unchanged.
            seg["overlap"] = trans_duration
        else:
            # When the transition consumes the whole outgoing clip, preserve
            # the existing boundary-start behavior and keep its frames alive.
            previous["tail"] += trans_duration
    for i, seg in enumerate(segments):
        # Account for incoming overlap on this segment and any outgoing tail
        # needed when a transition consumes the previous segment completely.
        seg["len"] = seg["dur"] + seg["overlap"] + seg["tail"]
        label = f"v{i}"
        seg["label"] = label
        clip = seg["clip"]
        if clip is None:
            graph.append(
                f"color=c=black:s={w}x{h}:r={fps}:d={_f(seg['len'])},format=yuv420p,setsar=1,settb=AVTB[{label}]"
            )
            continue
        asset = _get_asset(assets_by_id, clip.asset_id)
        path = resolve_safe_path(data_dir, asset.local_path)
        length = seg["len"]
        if clip.kind == "photo" or asset.kind == "photo":
            idx = add_input(path, ["-loop", "1", "-framerate", str(fps), "-t", _f(length)])
            kb = KEN_BURNS.get(clip.ken_burns)
            if kb:
                n = max(int(round(length * fps)), 1)
                z, x, y = (part.format(n=n) for part in kb)
                chain = [
                    f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase",
                    f"crop={w * 2}:{h * 2}",
                    f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}",
                ]
            else:
                chain = [
                    f"scale={w}:{h}:force_original_aspect_ratio=increase",
                    f"crop={w}:{h}",
                    f"fps={fps}",
                ]
            chain += [f"trim=duration={_f(length)}", "setpts=PTS-STARTPTS"]
        else:
            idx = add_input(path, [])
            speed = clip.speed
            chain = [
                f"trim=start={_f(clip.trim_start_sec)}:duration={_f(length * speed)}",
                f"setpts=(PTS-STARTPTS)/{_f(speed)}",
                f"fps={fps}",
                f"scale={w}:{h}:force_original_aspect_ratio=increase",
                f"crop={w}:{h}",
                f"tpad=stop_mode=clone:stop_duration={_f(length)}",
                f"trim=duration={_f(length)}",
                "setpts=PTS-STARTPTS",
            ]
            if not clip.muted and asset.has_audio and clip.volume > 0:
                a_label = f"va{i}"
                a_chain = _audio_chain(clip, clip.duration_sec * speed, speed=speed)
                graph.append(f"[{idx}:a]{a_chain}[{a_label}]")
                audio_labels.append(a_label)
        chain += _fades("fade", clip.fade_in_sec, clip.fade_out_sec, length)
        if clip.color_preset in COLOR_FILTERS:
            chain.append(COLOR_FILTERS[clip.color_preset])
        chain += ["setsar=1", "format=yuv420p", "settb=AVTB"]
        graph.append(f"[{idx}:v]" + ",".join(chain) + f"[{label}]")

    # Join segments: xfade on transitions, concat on hard cuts.
    current = segments[0]["label"]
    cumulative = segments[0]["dur"]
    for i, seg in enumerate(segments[1:], start=1):
        out = f"vj{i}"
        if seg["trans"]:
            name, dur = seg["trans"]
            graph.append(
                f"[{current}][{seg['label']}]xfade=transition={name}:duration={_f(dur)}"
                f":offset={_f(cumulative - seg['overlap'])}[{out}]"
            )
            cumulative += seg["dur"]
        else:
            graph.append(f"[{current}][{seg['label']}]concat=n=2:v=1:a=0[{out}]")
            cumulative += seg["dur"]
        current = out
    video_label = current

    # Audio tracks.
    for j, clip in enumerate(audios):
        if clip.muted or clip.volume <= 0:
            continue
        asset = _get_asset(assets_by_id, clip.asset_id)
        idx = add_input(resolve_safe_path(data_dir, asset.local_path), [])
        a_label = f"ta{j}"
        graph.append(f"[{idx}:a]{_audio_chain(clip, clip.duration_sec)}[{a_label}]")
        audio_labels.append(a_label)

    if not audio_labels:
        graph.append(f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_f(total)}[amixed]")
    else:
        mixed = "".join(f"[{label}]" for label in audio_labels)
        if len(audio_labels) > 1:
            mixed += f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
        else:
            mixed += "anull,"
        graph.append(mixed + f"alimiter=limit=0.95,apad,atrim=duration={_f(total)}[amixed]")
    audio_label = "amixed"

    total_out = total
    if project.result_target == "perfect_loop" and total >= 2.0:
        loop = min(half_beat, 0.5, total / 4.0)
        lo, hi = _f(loop), _f(total - loop)
        graph += [
            f"[{video_label}]split=3[lvh][lvb][lvt]",
            f"[lvh]trim=start=0:end={lo},setpts=PTS-STARTPTS[lvhead]",
            f"[lvb]trim=start={lo}:end={hi},setpts=PTS-STARTPTS[lvbody]",
            f"[lvt]trim=start={hi}:end={_f(total)},setpts=PTS-STARTPTS[lvtail]",
            f"[lvtail][lvhead]xfade=transition=fade:duration={lo}:offset=0[lvblend]",
            "[lvbody][lvblend]concat=n=2:v=1:a=0[lvout]",
            f"[{audio_label}]asplit=3[lah][lab][lat]",
            f"[lah]atrim=start=0:end={lo},asetpts=PTS-STARTPTS[lahead]",
            f"[lab]atrim=start={lo}:end={hi},asetpts=PTS-STARTPTS[labody]",
            f"[lat]atrim=start={hi}:end={_f(total)},asetpts=PTS-STARTPTS[latail]",
            f"[latail][lahead]acrossfade=d={lo}[lablend]",
            "[labody][lablend]concat=n=2:v=0:a=1[laout]",
        ]
        video_label, audio_label = "lvout", "laout"
        total_out = total - loop

    filter_complex = ";".join(graph)
    command = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats"]
        + input_args
        + ["-filter_complex", filter_complex, "-map", f"[{video_label}]", "-map", f"[{audio_label}]"]
        + ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps)]
        + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        + ["-t", _f(total_out), "-movflags", "+faststart", "-progress", "pipe:1", str(out_path)]
    )
    return RenderPlan(
        command=command,
        inputs=inputs,
        filter_complex=filter_complex,
        video_map=f"[{video_label}]",
        audio_map=f"[{audio_label}]",
        output_path=out_path,
        total_duration_sec=total_out,
    )


def _fades(filter_name: str, fade_in: float, fade_out: float, duration: float) -> list[str]:
    """Fade-in/out filters (``fade`` for video, ``afade`` for audio) over a clip of ``duration``."""
    fades = []
    if fade_in > 0:
        fades.append(f"{filter_name}=t=in:st=0:d={_f(fade_in)}")
    if fade_out > 0:
        fades.append(f"{filter_name}=t=out:st={_f(max(duration - fade_out, 0))}:d={_f(fade_out)}")
    return fades


def _audio_chain(clip: Any, source_duration: float, speed: float | None = None) -> str:
    """Trim, retime, level, fade and place a clip's audio on the 48 kHz stereo timeline."""
    chain = [
        f"atrim=start={_f(clip.trim_start_sec)}:duration={_f(source_duration)}",
        "asetpts=PTS-STARTPTS",
        *([f"atempo={_f(speed)}"] if speed is not None else []),
        f"volume={_f(clip.volume)}",
        *_fades("afade", clip.fade_in_sec, clip.fade_out_sec, clip.duration_sec),
        f"adelay={int(round(clip.start_sec * 1000))}:all=1",
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "asetpts=N/SR/TB",
    ]
    return ",".join(chain)


def execute_render_plan(
    plan: RenderPlan,
    progress_cb: Callable[[int, str], Any] | None = None,
    process_factory: Callable[..., Any] | None = None,
) -> Path:
    factory = process_factory or subprocess.Popen
    process = factory(
        plan.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    last = 0
    other: list[str] = []
    total = plan.total_duration_sec

    def report(pct: int) -> None:
        nonlocal last
        if pct > last:
            last = pct
            if progress_cb is not None:
                progress_cb(pct, "Rendering")

    for raw in process.stdout:
        line = raw.strip()
        key, sep, value = line.partition("=")
        if not sep:
            if line:
                other.append(line)
            continue
        if key in ("out_time_us", "out_time_ms") and total > 0:
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            report(5 + int(85 * min(max(seconds / total, 0.0), 1.0)))
        elif key == "progress" and value == "end":
            report(90)
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({returncode}): {' '.join(other[-20:])}")
    report(90)
    return plan.output_path
