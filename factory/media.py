import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .library import identify

PROJECT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT / ".local" / "tools"
MODEL = TOOLS / "clip"
CHUNK = 1024 * 1024
MAX_DOWNLOAD = 20 * 1024**3


class MediaError(Exception):
    pass


def tool(name):
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [TOOLS / (name + suffix), *sorted(TOOLS.glob(f"ffmpeg-*/bin/{name}{suffix}"))]
    return next((str(path) for path in candidates if path.is_file()), None) or shutil.which(name)


def downloader():
    if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
        return None
    exe = tool("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


def require(name):
    path = tool(name)
    if not path:
        raise MediaError(f"Missing {name}. Install FFmpeg and ffprobe on PATH or see Settings.")
    return path


def run(args, timeout):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError("Media processing timed out. Use Jobs to retry.") from None
    return result


def probe(path):
    result = run([require("ffprobe"), "-v", "error", "-protocol_whitelist", "file",
                  "-show_streams", "-show_format", "-of", "json", str(path)], 90)
    if result.returncode:
        raise MediaError("Unsupported or damaged media. Attach a playable video file.")
    info = json.loads(result.stdout)
    format_info = info.get("format", {})
    formats = set(format_info.get("format_name", "").split(","))
    if not formats & {"mov", "matroska", "webm", "avi", "mpeg", "mpegts", "flv", "ogg", "asf"}:
        raise MediaError("Unsupported video container. Attach an MP4 or WebM file.")
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    duration = float(format_info.get("duration", 0))
    if not video or not 0 < duration < 48 * 3600:
        raise MediaError("A video stream and a duration below 48 hours are required.")
    return {"duration": duration, "audio": any(s["codec_type"] == "audio" for s in info["streams"]),
            "bytes": path.stat().st_size}


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def input_fingerprint(snapshot):
    _, value, route = identify(snapshot["location"] or snapshot["link"])
    if route == "file":
        try:
            stat = Path(value).stat()
        except OSError:
            raise MediaError("Local media is unavailable. Check the path or attach a file.") from None
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    return "remote"


def copy_media(reader, dest, update, limit=MAX_DOWNLOAD):
    total, watermark = 0, 0
    with dest.open("wb") as target:
        while chunk := reader.read(CHUNK):
            total += len(chunk)
            if total > limit:
                raise MediaError(f"Media exceeds the {limit // CHUNK} MiB acquisition limit.")
            target.write(chunk)
            if total - watermark >= 8 * CHUNK:
                update(f"Staged {total // CHUNK} MiB")
                watermark = total
    return total


class HttpRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme not in ("http", "https") or url.username or url.password:
            raise MediaError("Unsupported redirect. Attach a direct video URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def direct_opener(url):
    host = urlsplit(url).hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    handlers = [HttpRedirect()]
    if loopback:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def download_watch(command, staging, value, update, settings):
    height = settings.get("remote_height", 480)
    minutes = settings.get("remote_minutes")
    mib = settings.get("remote_mib", MAX_DOWNLOAD // CHUNK)
    arguments = [*command, "--ignore-config", "--no-playlist", "--no-quiet", "--progress", "--newline",
                 "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
                 "--ffmpeg-location", str(Path(require("ffmpeg")).parent),
                 "--js-runtimes", "node", "--max-filesize", f"{mib}M",
                 "-f", (f"bv*[height<={height}][protocol=https]+ba[protocol=https]/b[height<={height}]"
                        if settings.get("method") == "streamer" else f"b[height<={height}]/bv*[height<={height}]+ba"),
                 "--merge-output-format", "mkv",
                 "-o", str(staging / "watch.%(ext)s"), "--print", "after_move:filepath", "--", value]
    if minutes:
        arguments[len(command):len(command)] = ["--match-filter", f"duration <= {minutes * 60} & !is_live"]
    deadline = time.monotonic() + 3600
    last_mib = 0
    with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8")) as process:
        while True:
            try:
                output, error = process.communicate(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise MediaError("YouTube acquisition timed out. Retry or attach a local file.") from None
                tracks = [candidate for candidate in staging.glob("watch.f*") if candidate.is_file()
                          and re.fullmatch(r"watch\.f[\w-]+\.[\w]+(?:\.part)?",candidate.name)]
                partials = tracks or [candidate for candidate in staging.glob("watch*.part") if candidate.is_file()
                                       and not candidate.name.endswith(".ytdl.part")]
                try:
                    downloaded = sum(candidate.stat().st_size for candidate in partials) // CHUNK
                except FileNotFoundError:
                    continue  # yt-dlp promoted a partial during this check.
                if downloaded > mib:
                    process.kill()
                    process.communicate()
                    raise MediaError(f"YouTube download exceeded {mib} MiB. Choose a smaller recording.")
                if downloaded >= last_mib + 8:
                    update(f"Downloaded {downloaded} MiB from YouTube")
                    last_mib = downloaded
                if settings.get("reserve_mib") and shutil.disk_usage(staging).free < settings["reserve_mib"] * CHUNK:
                    process.kill()
                    process.communicate()
                    raise MediaError("Acquisition paused at the configured free-space reserve.")
        result = next((Path(line.strip()).resolve() for line in reversed(output.splitlines())
                       if line.strip().startswith(str(staging))), None)
        if process.returncode or result is None:
            detail = (error + output).lower()
            if "does not pass filter" in detail:
                raise MediaError(f"YouTube skipped this recording under the {minutes}-minute download limit.")
            if "larger than max-filesize" in detail or "file is larger" in detail:
                raise MediaError(f"YouTube skipped a download larger than {mib} MiB.")
            if any(word in detail for word in ("timed out", "timeout", "network", "failed to connect",
                                                "10060", "10061", "connection")):
                raise MediaError("YouTube connection failed. Retry later or attach a local file.")
            if "sign in" in detail or "bot" in detail:
                raise MediaError("YouTube requires verification. Attach a local file or direct video URL.")
            if "requested format is not available" in detail:
                raise MediaError(f"YouTube offered no playable format at {height}p or lower.")
            raise MediaError("YouTube media is unavailable to yt-dlp. Retry or attach a local file.")
        if result.parent != staging.resolve() or not result.is_file():
            raise MediaError("The YouTube resolver did not produce a staged video.")
        return result


def acquire(root, job, snapshot, update, settings):
    _, value, route = identify(snapshot["location"] or snapshot["link"])
    if route == "youtube" and os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
        raise MediaError("Start this YouTube replay in Sources to use bounded FlashData acquisition.")
    staging = root / "staging" / f"job-{job}"
    staging.mkdir(parents=True, exist_ok=True)
    data = staging / "master.partial"
    limit = settings.get("remote_mib", MAX_DOWNLOAD // CHUNK) * CHUNK
    update({"file": "Copying local video", "direct": "Downloading direct media",
            "youtube": "Resolving YouTube with yt-dlp"}[route])
    try:
        if route == "file":
            source = Path(value)
            if not source.is_file():
                raise MediaError("Local media is unavailable. Check the path or attach a file.")
            with source.open("rb") as stream:
                copy_media(stream, data, update, limit if settings.get("reserve_mib") else MAX_DOWNLOAD)
        elif route == "direct":
            request = urllib.request.Request(value, headers={"User-Agent": "GTA-Media-Factory/1"})
            with direct_opener(value).open(request, timeout=30) as stream:
                if stream.headers.get_content_type() in ("text/html", "application/json"):
                    raise MediaError("The link returned a page. Attach a direct video URL or local file.")
                expected = stream.headers.get("Content-Length", "")
                if expected.isdigit() and int(expected) > limit:
                    raise MediaError(f"Direct media exceeds {limit // CHUNK} MiB. Choose a smaller file.")
                received = copy_media(stream, data, update, limit)
                if expected.isdigit() and int(expected) != received:
                    raise MediaError("The direct download ended early. Retry the job.")
        else:
            command = downloader()
            if not command:
                raise MediaError("Install yt-dlp for YouTube, or attach a local video in Library.")
            data = download_watch(command, staging, value, update, settings)
        update("Probing streams and computing SHA-256")
        info = probe(data)
        if route != "file" and info["bytes"] > limit:
            raise MediaError(f"Remote media exceeds {limit // CHUNK} MiB. Choose a smaller file.")
        if (route != "file" or settings.get("reserve_mib")) and (minutes := settings.get("remote_minutes")) and info["duration"] > minutes*60:
            raise MediaError(f"Remote video exceeds {minutes} minutes. Choose a shorter recording.")
        digest = checksum(data)
        master = root / "masters" / digest / "recording"
        master.parent.mkdir(parents=True, exist_ok=True)
        data.replace(master)
        return dict(info, hash=digest, path=master.relative_to(root).as_posix()), route
    except urllib.error.HTTPError as error:
        raise MediaError(f"Direct media returned HTTP {error.code}. Check the link or retry.") from None
    except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead):
        raise MediaError("Direct media connection failed. Retry or attach a local video.") from None
    except OSError:
        raise MediaError("Could not read or stage media. Check the file and local disk space.") from None
