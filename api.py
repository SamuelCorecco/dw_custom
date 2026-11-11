from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import threading
import tempfile
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

import yt_dlp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="Video Downloader API", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloader"
CACHE_DIR = DOWNLOAD_DIR / ".cache"
CACHE_FILE = CACHE_DIR / "media-info.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT = 4
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="download")
active_downloads: dict[str, dict[str, Any]] = {}
download_history: list[dict[str, Any]] = []
downloads_lock = threading.RLock()
cache_lock = threading.RLock()

# yt-dlp only enables Deno by default. Node is already part of the local
# application environment, so enable it explicitly for YouTube's JS challenges.
YTDLP_JS_OPTIONS: dict[str, Any] = {"js_runtimes": {"node": {}}}


class VideoInfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = "best"
    download_type: str = Field(default="video", pattern="^(video|audio)$")
    playlist: bool = False


class M3U8Request(BaseModel):
    url: str
    headers: Optional[dict[str, str]] = None
    filename: Optional[str] = None
    program_id: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_duration(seconds: Any) -> str:
    if seconds in (None, ""):
        return "--:--"
    try:
        seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_filesize(size: Any) -> Optional[str]:
    if not size:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def safe_headers(headers: Optional[dict[str, str]]) -> dict[str, str]:
    """Keep useful media headers and remove values unsafe for ffmpeg arguments."""
    if not headers:
        return {}
    allowed = {"authorization", "cookie", "origin", "referer", "user-agent", "accept", "accept-language"}
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        key = str(key).strip()
        value = str(value).replace("\r", "").replace("\n", "").strip()
        if key.lower() in allowed and value:
            cleaned[key] = value
    return cleaned


def ffmpeg_header_args(headers: Optional[dict[str, str]]) -> list[str]:
    cleaned = safe_headers(headers)
    if not cleaned:
        return []
    header_block = "".join(f"{key}: {value}\r\n" for key, value in cleaned.items())
    return ["-headers", header_block]


def hls_input_args(url: str) -> list[str]:
    # These endpoints receive known HLS inputs, including extensionless manifests.
    args = ["-f", "hls", "-allowed_extensions", "ALL"]
    parsed = urlparse(url)
    if parsed.path.lower().endswith(".php") or "m3u8" in parse_qs(parsed.query):
        # Proxy routes can serve MPEG-TS bytes under .php URLs. Match the content,
        # rather than requiring the segment URL suffix to match its media format.
        args += ["-extension_picky", "0"]
    return args


def resolve_hls_source(url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Keep playlist proxies intact; unwrap HTML players carrying an HLS URL."""
    embedded = (parse_qs(urlparse(url).query).get("m3u8") or [None])[0]
    if not embedded or urlparse(embedded).scheme not in {"http", "https"}:
        return url, headers
    try:
        with urlopen(Request(url, headers=headers), timeout=4) as response:
            if "mpegurl" in response.headers.get("Content-Type", "").lower():
                return url, headers
            beginning = response.read(4096).decode("utf-8-sig", errors="replace").lstrip()
            if beginning.startswith("#EXTM3U"):
                return url, headers
    except Exception:
        pass

    source_headers = dict(headers)
    outer = urlparse(url)
    inner = urlparse(embedded)
    if (outer.scheme, outer.netloc) != (inner.scheme, inner.netloc):
        # Credentials for the player host must not be sent to the media host.
        source_headers = {
            key: value for key, value in headers.items()
            if key.lower() not in {"cookie", "authorization", "origin", "referer"}
        }
        source_headers.update({"Referer": url, "Origin": f"{outer.scheme}://{outer.netloc}"})
    return embedded, source_headers


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


media_cache = _read_cache()


def _write_cache() -> None:
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(media_cache, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CACHE_FILE)


def cache_key(kind: str, url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def cached_media_info(kind: str, url: str, ttl: timedelta, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    key = cache_key(kind, url)
    with cache_lock:
        cached = media_cache.get(key)
        if cached:
            try:
                saved_at = datetime.fromisoformat(cached["saved_at"])
                if datetime.now(timezone.utc) - saved_at < ttl:
                    return {**cached["value"], "cached": True}
            except (KeyError, TypeError, ValueError):
                pass

    value = loader()
    if "error" not in value:
        with cache_lock:
            media_cache[key] = {"saved_at": now_iso(), "value": value}
            try:
                _write_cache()
            except OSError:
                pass
    return {**value, "cached": False}


def create_download_entry(download_id: str, url: str, title: str, kind: str) -> None:
    with downloads_lock:
        active_downloads[download_id] = {
            "id": download_id,
            "url": url,
            "title": title,
            "kind": kind,
            "status": "queued",
            "progress": 0.0,
            "speed": "",
            "eta": "",
            "filename": "",
            "error": None,
            "diagnostic_log": "",
            "queue_position": 0,
            "item_index": None,
            "item_count": None,
            "started_at": now_iso(),
            "completed_at": None,
        }
        refresh_queue_positions()


def refresh_queue_positions() -> None:
    queued = [item for item in active_downloads.values() if item["status"] == "queued"]
    queued.sort(key=lambda item: item["started_at"])
    for position, item in enumerate(queued, start=1):
        item["queue_position"] = position


def update_download(download_id: str, **changes: Any) -> None:
    with downloads_lock:
        entry = active_downloads.get(download_id)
        if not entry:
            return
        entry.update({key: value for key, value in changes.items() if value is not None})
        if changes.get("status") == "downloading":
            entry["queue_position"] = 0
        refresh_queue_positions()


def complete_download(download_id: str, filename: Optional[str] = None, error: Optional[Exception | str] = None) -> None:
    with downloads_lock:
        entry = active_downloads.get(download_id)
        if not entry:
            return
        entry.update(
            status="error" if error else "completed",
            progress=entry["progress"] if error else 100.0,
            filename=filename or entry["filename"],
            error=str(error) if error else None,
            completed_at=now_iso(),
        )
        download_history.append(entry.copy())
        del download_history[:-100]
        refresh_queue_positions()

    def remove_completed() -> None:
        with downloads_lock:
            active_downloads.pop(download_id, None)
            refresh_queue_positions()

    timer = threading.Timer(4, remove_completed)
    timer.daemon = True
    timer.start()


def submit_download(worker: Callable[..., None], *args: Any) -> None:
    executor.submit(worker, *args)


@app.get("/")
def root() -> dict[str, Any]:
    with downloads_lock:
        running = sum(item["status"] == "downloading" for item in active_downloads.values())
        queued = sum(item["status"] == "queued" for item in active_downloads.values())
    return {
        "status": "online",
        "message": "Video Downloader API v3",
        "yt_dlp_version": yt_dlp.version.__version__,
        "running": running,
        "queued": queued,
        "max_concurrent": MAX_CONCURRENT,
    }


@app.post("/video-info")
def get_video_info(req: VideoInfoRequest) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        try:
            options = {
                **YTDLP_JS_OPTIONS,
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(req.url, download=False)
            if not info:
                return {"error": "Informazioni video non disponibili"}

            formats = [
                item for item in info.get("formats", [])
                if item.get("vcodec") != "none" and item.get("acodec") != "none"
            ]
            best = max(formats, key=lambda item: (item.get("height") or 0, item.get("tbr") or 0), default=None)
            query = parse_qs(urlparse(req.url).query)
            playlist_id = info.get("playlist_id") or (query.get("list") or [None])[0]
            playlist_count = info.get("n_entries") or info.get("playlist_count")
            return {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "duration_string": format_duration(info.get("duration")),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader") or info.get("channel"),
                "webpage_url": info.get("webpage_url") or req.url,
                "best_format": {
                    "format_id": "best",
                    "resolution": (
                        f"{best.get('width')}x{best.get('height')}" if best and best.get("width") else "Migliore"
                    ),
                    "filesize": format_filesize(best.get("filesize") or best.get("filesize_approx")) if best else None,
                },
                "playlist": {
                    "available": bool(playlist_id),
                    "id": playlist_id,
                    "title": info.get("playlist_title") or info.get("playlist"),
                    "count": playlist_count,
                },
            }
        except Exception as exc:
            return {"error": str(exc)}

    return cached_media_info("youtube", req.url, timedelta(days=7), load)


@app.post("/download")
def download_video(req: DownloadRequest) -> dict[str, Any]:
    download_id = str(uuid.uuid4())
    kind = f"playlist-{req.download_type}" if req.playlist else req.download_type
    title = "Playlist YouTube" if req.playlist else ("Audio YouTube" if req.download_type == "audio" else "Video YouTube")
    create_download_entry(download_id, req.url, title, kind)
    submit_download(run_download, req, download_id)
    return {"status": "queued", "download_id": download_id}


def run_download(req: DownloadRequest, download_id: str) -> None:
    try:
        update_download(download_id, status="downloading")

        def progress_hook(data: dict[str, Any]) -> None:
            if data.get("status") != "downloading":
                return
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = downloaded / total * 100 if total else _parse_percent(data.get("_percent_str"))
            info = data.get("info_dict") or {}
            index = info.get("playlist_index")
            count = info.get("n_entries") or info.get("playlist_count")
            overall = percent
            if req.playlist and index and count:
                overall = ((int(index) - 1) + percent / 100) / int(count) * 100
            update_download(
                download_id,
                progress=min(overall, 99.9),
                speed=(data.get("_speed_str") or "").strip(),
                eta=(data.get("_eta_str") or "").strip(),
                title=info.get("title"),
                item_index=index,
                item_count=count,
            )

        if req.playlist:
            output_template = str(DOWNLOAD_DIR / "%(playlist_title)s" / "%(playlist_index)03d - %(title)s.%(ext)s")
        else:
            output_template = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")

        options: dict[str, Any] = {
            **YTDLP_JS_OPTIONS,
            "outtmpl": output_template,
            "restrictfilenames": True,
            "noplaylist": not req.playlist,
            "ignoreerrors": req.playlist,
            "progress_hooks": [progress_hook],
            "retries": 5,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "quiet": True,
        }
        if req.download_type == "audio":
            options.update(
                format="bestaudio/best",
                # Download the YouTube thumbnail temporarily, then embed it in
                # the MP3 as its ID3 front cover. EmbedThumbnail removes the
                # sidecar image after a successful post-processing pass.
                writethumbnail=True,
                postprocessors=[
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    },
                    {
                        "key": "FFmpegMetadata",
                        "add_metadata": True,
                    },
                    {
                        "key": "EmbedThumbnail",
                    },
                ],
            )
        else:
            selected = req.format_id or "best"
            options.update(
                format="bestvideo+bestaudio/best" if selected == "best" else f"{selected}+bestaudio/best",
                merge_output_format="mp4",
            )

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(req.url, download=True)
            if not info:
                raise RuntimeError("Nessun elemento scaricabile")
            title = info.get("title") or info.get("playlist_title") or "Download"
            update_download(download_id, title=title)
            if req.playlist:
                filename = str(DOWNLOAD_DIR / title)
            else:
                prepared_filename = Path(ydl.prepare_filename(info))
                filename = (
                    str(prepared_filename.with_suffix(".mp3"))
                    if req.download_type == "audio"
                    else str(prepared_filename)
                )
        complete_download(download_id, filename=filename)
    except Exception as exc:
        complete_download(download_id, error=exc)


def _parse_percent(value: Any) -> float:
    try:
        return float(str(value or "0").strip().replace("%", ""))
    except ValueError:
        return 0.0


@app.post("/m3u8-info")
def get_m3u8_info(req: M3U8Request) -> dict[str, Any]:
    headers = safe_headers(req.headers)

    def load() -> dict[str, Any]:
        source_url, source_headers = resolve_hls_source(req.url, headers)
        probe = probe_stream(source_url, source_headers)
        if "error" in probe:
            return probe
        thumbnail = create_stream_thumbnail(source_url, source_headers)
        return {
            **probe, "thumbnail": thumbnail, "title": "Stream HLS",
            "source_url": source_url,
            "source_headers": source_headers if source_headers != headers else None,
            "child_playlists": read_hls_child_playlists(source_url, source_headers),
        }

    # Previous cache entries only described the first video stream.
    return cached_media_info("hls-qualities-v1", req.url, timedelta(hours=24), load)


def hls_child_playlists(manifest: str, base_url: str) -> list[str]:
    if not manifest.lstrip("\ufeff \r\n").startswith("#EXTM3U"):
        return []
    children = []
    next_is_playlist = False
    for raw_line in manifest.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            next_is_playlist = True
        elif line.startswith("#EXT-X-MEDIA:"):
            uri = re.search(r'(?:[:,])URI="([^"]+)"', line)
            if uri:
                children.append(urljoin(base_url, uri.group(1)))
        elif line and not line.startswith("#"):
            if next_is_playlist:
                children.append(urljoin(base_url, line))
            next_is_playlist = False
    return list(dict.fromkeys(children))


def read_hls_child_playlists(url: str, headers: dict[str, str]) -> list[str]:
    if urlparse(url).scheme not in {"http", "https"}:
        return []
    try:
        with urlopen(Request(url, headers=headers), timeout=4) as response:
            manifest = response.read(1024 * 1024).decode("utf-8-sig")
            return hls_child_playlists(manifest, response.geturl())
    except Exception:
        return []


@app.post("/probe-m3u8")
def probe_m3u8(req: M3U8Request) -> dict[str, Any]:
    return probe_stream(req.url, safe_headers(req.headers))


def probe_stream(url: str, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-rw_timeout", "15000000"]
    command += ffmpeg_header_args(headers)
    command += hls_input_args(url)
    command += ["-show_programs", "-show_streams", "-show_format", "-of", "json", url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=18)
        data = json.loads(result.stdout)
        videos = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"]
        video_key = lambda stream: (stream.get("height") or 0, stream.get("width") or 0)
        video = max(videos, key=video_key, default=None)
        qualities = []
        for program in data.get("programs", []):
            program_video = max(
                (stream for stream in program.get("streams", []) if stream.get("codec_type") == "video"),
                key=video_key, default=None,
            )
            if not program_video:
                continue
            try:
                bitrate = int(program.get("tags", {}).get("variant_bitrate") or program_video.get("bit_rate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            qualities.append({
                "program_id": program.get("program_id"),
                "width": program_video.get("width"), "height": program_video.get("height"),
                "bandwidth": bitrate,
            })
        if not qualities and video:
            qualities.append({"program_id": None, "width": video.get("width"), "height": video.get("height"), "bandwidth": 0})
        qualities.sort(key=lambda quality: (quality["height"] or 0, quality["width"] or 0, quality["bandwidth"]), reverse=True)
        duration = float(data.get("format", {}).get("duration") or 0)
        width = video.get("width") if video else None
        height = video.get("height") if video else None
        return {
            "duration": duration,
            "duration_string": format_duration(duration),
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else "Auto",
            "qualities": qualities,
            "has_video": bool(videos),
            "has_audio": any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])),
        }
    except subprocess.TimeoutExpired:
        return {"error": "Analisi scaduta", "duration_string": "--:--", "resolution": "Auto"}
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or "").strip() or f"ffprobe è terminato con codice {exc.returncode}"
        return {"error": reason[-3000:], "duration_string": "--:--", "resolution": "Auto"}
    except Exception as exc:
        return {"error": str(exc), "duration_string": "--:--", "resolution": "Auto"}


def create_stream_thumbnail(url: str, headers: Optional[dict[str, str]] = None) -> Optional[str]:
    path = CACHE_DIR / f"preview-{uuid.uuid4().hex}.jpg"
    command = ["ffmpeg", "-y", "-loglevel", "error", "-rw_timeout", "15000000"]
    command += ffmpeg_header_args(headers)
    command += hls_input_args(url)
    command += ["-i", url, "-ss", "1", "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(path)]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=18)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None
    finally:
        path.unlink(missing_ok=True)


@app.post("/download-m3u8")
def download_m3u8(req: M3U8Request) -> dict[str, Any]:
    download_id = str(uuid.uuid4())
    url, headers = resolve_hls_source(req.url, safe_headers(req.headers))
    create_download_entry(download_id, url, (req.filename or "").strip() or "Stream HLS", "hls")
    submit_download(run_m3u8_download, url, download_id, headers, req.filename, req.program_id, req.height)
    return {"status": "queued", "download_id": download_id}


def rename_stream_download(filename: str, requested_name: Optional[str]) -> str:
    if not requested_name or not requested_name.strip():
        return filename
    source = Path(filename)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", requested_name).strip()
    if source.suffix and name.lower().endswith(source.suffix.lower()):
        name = name[:-len(source.suffix)]
    name = name.strip(" .").encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    if not name:
        return filename
    target = source.with_name(f"{name}{source.suffix}")
    if target == source:
        return filename
    index = 1
    while True:
        try:
            # Link within the same folder to reserve the name without overwriting.
            target.hardlink_to(source)
        except FileExistsError:
            target = source.with_name(f"{name} ({index}){source.suffix}")
            index += 1
        else:
            source.unlink()
            return str(target)


def run_m3u8_download(
    url: str, download_id: str, headers: Optional[dict[str, str]] = None, requested_name: Optional[str] = None,
    program_id: Optional[int] = None, height: Optional[int] = None,
) -> None:
    try:
        update_download(download_id, status="downloading", speed="ffmpeg")
        try:
            filename = run_ffmpeg_hls(url, download_id, headers, program_id)
        except Exception as ffmpeg_error:
            partial_output = DOWNLOAD_DIR / f"stream_{download_id[:8]}.mp4"
            partial_output.unlink(missing_ok=True)
            try:
                if program_id is not None and height is None:
                    quality = next((item for item in probe_stream(url, headers).get("qualities", []) if item["program_id"] == program_id), None)
                    if not quality or not quality["height"]:
                        raise RuntimeError("Impossibile verificare la qualità selezionata")
                    height = quality["height"]
                filename = run_ytdlp_hls_fallback(url, download_id, headers, height)
            except Exception as ytdlp_error:
                raise RuntimeError(f"ffmpeg: {ffmpeg_error}; yt-dlp: {ytdlp_error}") from ytdlp_error
        complete_download(download_id, filename=rename_stream_download(filename, requested_name))
    except Exception as exc:
        complete_download(download_id, error=exc)


def run_ffmpeg_hls(
    url: str, download_id: str, headers: Optional[dict[str, str]] = None, program_id: Optional[int] = None
) -> str:
    output = DOWNLOAD_DIR / f"stream_{download_id[:8]}.mp4"
    duration = probe_stream(url, headers).get("duration") or 0
    command = ["ffmpeg", "-y", "-loglevel", "warning", "-xerror", "-progress", "pipe:1"]
    if url.lower().startswith(("http://", "https://")):
        command += [
            "-rw_timeout", "20000000", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "404,408,429,5xx",
            "-reconnect_delay_max", "5", "-reconnect_max_retries", "5", "-reconnect_delay_total_max", "30",
        ]
    command += ffmpeg_header_args(headers)
    command += hls_input_args(url) + ["-seg_max_retry", "5", "-i", url]
    if program_id is not None:
        command += ["-map", f"0:p:{program_id}:v:0", "-map", f"0:p:{program_id}:a:0?"]
    # Automatic selection chooses the best video when no quality was requested.
    command += ["-sn", "-dn", "-c", "copy", "-movflags", "+faststart", str(output)]
    # Drain progress separately from diagnostics: a full stderr pipe can otherwise
    # stall the download, and HLS skip warnings can occur even with exit code zero.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as diagnostics:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=diagnostics, text=True, bufsize=1) as process:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time_ms=") and duration:
                    try:
                        elapsed = int(line.split("=", 1)[1]) / 1_000_000
                        update_download(download_id, progress=min(elapsed / duration * 100, 99.9), speed="ffmpeg")
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    update_download(download_id, speed=line.split("=", 1)[1] or "ffmpeg")
            process.wait()
        diagnostics.seek(0)
        error = ffmpeg_download_error(process.returncode, diagnostics)
        if error:
            diagnostics.seek(0)
            log_path = save_ffmpeg_log(download_id, process.returncode, diagnostics)
            if log_path:
                error += f"\nLog completo: {log_path}"
            raise RuntimeError(error)
    return str(output)


def ffmpeg_download_error(returncode: int, lines: Iterable[str]) -> Optional[str]:
    tail: deque[str] = deque(maxlen=30)
    integrity_errors: deque[str] = deque(maxlen=5)
    read_errors: deque[str] = deque(maxlen=5)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        tail.append(line)
        if re.search(
            r"segment\b.*\bskipping\b|skipping\s+\d+\s+segments?\b|packet corrupt|corrupt input packet"
            r"|failed to open an initialization section",
            line, re.IGNORECASE,
        ):
            integrity_errors.append(line)
        elif re.search(r"error during demuxing|input/output error", line, re.IGNORECASE):
            read_errors.append(line)
    if integrity_errors:
        return "Download incompleto: segmenti mancanti o danneggiati.\n" + "\n".join(integrity_errors)[-2000:]
    if read_errors:
        return "Errore di lettura dello stream.\n" + "\n".join(tail)[-3000:]
    if returncode:
        return "\n".join(tail)[-3000:] or f"Errore ffmpeg (codice {returncode})"
    return None


def save_ffmpeg_log(download_id: str, returncode: int, lines: Iterable[str]) -> Optional[str]:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", download_id)[:64] or "unknown"
    path = CACHE_DIR / "logs" / f"ffmpeg-{safe_id}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as log:
            log.write(f"FFmpeg exit code: {returncode}\n\n")
            log.writelines(lines)
    except OSError:
        return None
    update_download(download_id, diagnostic_log=str(path))
    return str(path)


def hls_fallback_format(url: str, height: Optional[int]) -> str:
    fixed_height = None
    candidate = url
    for _ in range(3):
        query = parse_qs(urlparse(candidate).query)
        rendition = (query.get("rendition") or [""])[0]
        match = re.fullmatch(r"([1-9]\d{1,4})p", rendition, re.IGNORECASE)
        if match:
            fixed_height = int(match.group(1))
        embedded = (query.get("m3u8") or [None])[0]
        if not embedded or urlparse(embedded).scheme not in {"http", "https"}:
            break
        candidate = embedded

    if height and fixed_height and height != fixed_height:
        raise RuntimeError(f"La playlist è fissata a {fixed_height}p, ma è stata richiesta la qualità {height}p")
    selected_height = height or fixed_height
    if not selected_height:
        return "bestvideo+bestaudio/best"
    # A fixed rendition already constrains the source quality. Its media playlist
    # may omit RESOLUTION, so yt-dlp's height can be unknown. Known mismatches are
    # still rejected, and master playlists retain their strict height filter.
    height_filter = f"height={'?' if fixed_height else ''}{selected_height}"
    return f"best[{height_filter}]/bestvideo[{height_filter}]+bestaudio"


def hls_retry_delay(attempt: int) -> int:
    return min(2 ** min(max(attempt - 1, 0), 3), 8)


def run_ytdlp_hls_fallback(
    url: str, download_id: str, headers: Optional[dict[str, str]] = None, height: Optional[int] = None
) -> str:
    output_template = str(DOWNLOAD_DIR / f"stream_{download_id[:8]}.%(ext)s")
    update_download(download_id, speed="yt-dlp fallback", progress=0.0, eta="")

    def progress_hook(data: dict[str, Any]) -> None:
        if data.get("status") != "downloading":
            return
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        percent = downloaded / total * 100 if total else _parse_percent(data.get("_percent_str"))
        update_download(
            download_id,
            progress=min(percent, 99.9),
            speed=(data.get("_speed_str") or "yt-dlp").strip(),
            eta=(data.get("_eta_str") or "").strip(),
        )

    options = {
        **YTDLP_JS_OPTIONS,
        "format": hls_fallback_format(url, height),
        "outtmpl": output_template,
        "noplaylist": True,
        "http_headers": headers or {},
        "retries": 5,
        "fragment_retries": 15,
        "skip_unavailable_fragments": False,
        "hls_prefer_native": True,
        "retry_sleep_functions": {"fragment": hls_retry_delay, "http": hls_retry_delay},
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress_hook],
        "merge_output_format": "mp4",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("Stream non disponibile")
        return ydl.prepare_filename(info)


@app.get("/downloads/active")
def get_active_downloads() -> dict[str, Any]:
    with downloads_lock:
        downloads = sorted(active_downloads.values(), key=lambda item: item["started_at"])
        return {"downloads": downloads, "count": len(downloads), "max_concurrent": MAX_CONCURRENT}


@app.get("/downloads/history")
def get_download_history() -> dict[str, Any]:
    with downloads_lock:
        return {"downloads": list(download_history), "count": len(download_history)}


@app.delete("/downloads/history")
def clear_download_history() -> dict[str, str]:
    with downloads_lock:
        download_history.clear()
    return {"status": "cleared"}


@app.get("/downloads/{download_id}")
def get_download_status(download_id: str) -> dict[str, Any]:
    with downloads_lock:
        if download_id in active_downloads:
            return active_downloads[download_id]
        return next((item for item in reversed(download_history) if item["id"] == download_id), {"error": "Download non trovato"})


if __name__ == "__main__":
    import uvicorn

    print("Video Downloader API v3")
    print(f"Download: {DOWNLOAD_DIR}")
    print(f"Server: http://127.0.0.1:8000")
    print(f"Download paralleli: {MAX_CONCURRENT}")
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
