"""Bounded HTTP transfer for seekable, separate platform renditions."""

import hashlib
import json
import math
import re
import shutil
import threading
import time
import urllib.request
from fractions import Fraction
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import media, streamer_asr, streamer_catalog

PART = 14400
BLOCK = 256 * 1024
REPLAY_LIMIT = (11*60+40)*60
REPLAY_LIMIT_MESSAGE = "YouTube replay processing ends at 11 hours 40 minutes."


class TransferHold(media.MediaError):
    pass


class TransferBudget(TransferHold):
    pass


def parts(duration, begin=0, end=None):
    end = duration if end is None else min(duration, end)
    return [(start, min(start+PART, end)) for start in range(begin, math.ceil(end), PART)]


def replay_hours(duration):
    end = min(duration,REPLAY_LIMIT)
    return [(start,min(start+3600,end)) for start in range(0,math.ceil(end),3600)]


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def completed_youtube(url, height):
    data = streamer_catalog.extract(url, "--no-playlist", "--skip-download", "-J")
    return replay_renditions(data, height)


def replay_renditions(data, height):
    if not isinstance(data,dict) or not isinstance(data.get("id"),str):
        raise TransferHold("YouTube did not verify a canonical replay ID.")
    if data.get("live_status") in ("is_live", "is_upcoming", "post_live") or not data.get("duration"):
        raise TransferHold("Waiting for a completed YouTube replay with seekable audio and video.")
    duration = float(data["duration"])
    if not math.isfinite(duration) or not 0 < duration <= 48*3600:
        raise TransferHold("YouTube did not supply a supported completed duration.")
    renditions = {"audio": [], "video": []}
    for item in data.get("formats", []):
        uri = urlsplit(item.get("url") or "")
        length = item.get("filesize")
        if (item.get("protocol") != "https" or uri.scheme != "https" or
                not uri.hostname or not uri.hostname.endswith(".googlevideo.com") or
                uri.username or uri.password or not isinstance(length, int) or length <= 0 or
                item.get("has_drm") or item.get("fragments") or item.get("ext") not in ("m4a","mp4","webm")):
            continue
        audio, video = item.get("acodec"), item.get("vcodec")
        if audio not in (None,"none") and video=="none":
            kind = "audio"
        elif video not in (None,"none") and audio=="none" and 0 < (item.get("height") or 0) <= height:
            kind = "video"
        else:
            continue
        renditions[kind].append(item)
    # Prefer high-resolution MP4 on ties, and an Opus audio track with stable seek cues.
    audio = sorted(renditions["audio"],key=lambda r:(not str(r["format_id"]).endswith("-drc"),
                                                    r.get("ext")=="webm",r.get("abr") or 0),reverse=True)
    video = sorted(renditions["video"],key=lambda r:(r.get("height") or 0,r.get("ext")=="mp4",
                                                    r.get("tbr") or 0),reverse=True)
    return {"id":data["id"],"duration":duration,"audio":audio,"video":video}


class TransferLedger:
    def __init__(self, root, path, config):
        self.root, self.path = root, path
        self.limit = config["remote_mib"] * media.CHUNK
        self.reserve = config["reserve_mib"] * media.CHUNK
        self.rate = config.get("bandwidth", 0)*1024
        self.next_read = 0
        self.lock = threading.Lock()
        self.data = streamer_asr.read(path) if path.is_file() else []

    def measure(self, kind):
        with self.lock:
            return sum(row["received"] for row in self.data if row["kind"]==kind)

    def read(self, attempt, length, callback, *, at=None):
        with self.lock:
            # Commit the maximum possible read first. A crash cannot restore that headroom.
            spent = sum(max(row["received"],row.get("pending",0)) for row in self.data)
            if spent+length > self.limit or shutil.disk_usage(self.root).free < self.reserve+length:
                raise TransferBudget("The aggregate transfer cap or free-space reserve stopped acquisition.")
            attempt["pending"] = attempt["received"]+length
            streamer_asr.write(self.path,self.data)
            if self.rate:
                # Keep the configured rate across both tracks and all inputs in this attempt.
                now = time.monotonic()
                wait = max(0,self.next_read-now)
                time.sleep(wait)
                self.next_read = now+wait+length/self.rate
            chunk = callback(length)
            attempt["received"] += len(chunk)
            if chunk and at is not None:
                spans = attempt.setdefault("source_bytes",[])
                if spans and spans[-1][1]==at:
                    spans[-1][1] += len(chunk)
                else:
                    spans.append([at,at+len(chunk)])
            attempt["pending"] = 0
            streamer_asr.write(self.path,self.data)
            return chunk

    def begin(self, kind, item, start, end):
        entry = {"kind":kind,"rendition":str(item["format_id"]),"start":start,"end":end,
                 "received":0,"pending":0,"requests":0,"state":"started"}
        from datetime import datetime, timezone
        entry["started"] = datetime.now(timezone.utc).isoformat()
        self.data.append(entry)
        streamer_asr.write(self.path,self.data)
        return entry

    def end(self, entry, state):
        entry["state"] = state
        streamer_asr.write(self.path,self.data)


class RemoteRanges:
    """Expose a loopback seekable file to FFmpeg, with a cap on every upstream read."""

    def __init__(self, item, begin, end, kind, ledger, opener=None):
        self.item, self.length, self.ledger = item, item["filesize"], ledger
        self.opener = opener or urllib.request.urlopen
        self.error = ""
        self.budget_error = False
        self.stopped = False
        expected = math.ceil((end-begin)*item["filesize"]/item["source_duration"])
        # Index reads and keyframe decode are bounded even if FFmpeg seeks badly.
        self.cap = expected*2 + (4 if kind=="video" else 3)*media.CHUNK
        if kind=="video":
            self.cap = min(self.cap,self.length-1)
        self.entry = ledger.begin(kind,item,begin,end)
        self.entry.update(interval_byte_cap=self.cap,total_rendition_bytes=self.length)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                owner.respond(self)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.server.request_queue_size = 4
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.gate = threading.Lock()

    def respond(self, request):
        if request.path != "/rendition":
            return request.send_error(404)
        span = re.fullmatch(r"bytes=(\d+)-(\d*)",request.headers.get("Range","bytes=0-"))
        if not span:
            return request.send_error(416)
        begin = int(span[1])
        end = min(self.length-1,int(span[2])) if span[2] else self.length-1
        if begin > end:
            return request.send_error(416)
        request.send_response(206)
        request.send_header("Content-Length",str(end-begin+1))
        request.send_header("Content-Range",f"bytes {begin}-{end}/{self.length}")
        request.send_header("Accept-Ranges","bytes")
        request.end_headers()
        try:
            block = min(BLOCK,max(1024,int(self.ledger.rate)*2)) if self.ledger.rate else BLOCK
            for offset in range(begin,end+1,block):
                if self.error or self.stopped:
                    request.close_connection = True
                    return
                count = min(block,end-offset+1)
                with self.gate:
                    if self.entry["received"]+count > self.cap:
                        raise TransferHold("Decode overhead exceeded the per-interval transfer allowance. A full transfer was refused.")
                    headers = {k:v for k,v in self.item.get("http_headers",{}).items()
                               if k.lower() not in ("range","accept-encoding","cookie","authorization")}
                    last = offset+count-1
                    headers.update(Range=f"bytes={offset}-{last}", **{"Accept-Encoding":"identity"})
                    upstream = urllib.request.Request(self.item["url"],headers=headers)
                    self.entry["requests"] += 1
                    with self.opener(upstream,timeout=15) as reply:
                        if (reply.status != 206 or reply.headers.get("Content-Range") != f"bytes {offset}-{last}/{self.length}"
                                or reply.headers.get("Content-Encoding","identity").lower()!="identity"):
                            raise TransferHold("The platform ignored or changed an exact byte range. Full transfer was refused.")
                        chunk = self.ledger.read(self.entry,count,reply.read,at=offset)
                    if len(chunk)!=count:
                        raise TransferHold("The platform returned an incomplete range. Retry the saved interval.")
                request.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            request.close_connection = True  # FFmpeg closed the current seek.
        except TransferHold as error:
            request.close_connection = True
            self.error = str(error)
            self.budget_error = isinstance(error,TransferBudget)
        except (OSError,ValueError):
            request.close_connection = True
            self.error = "The selected CDN range is unavailable. Retry to resolve a fresh media URL."

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/rendition"

    def __exit__(self, kind, *_):
        self.stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        with self.gate:
            self.ledger.end(self.entry,"refused" if self.error else "interrupted" if kind else "received")


def duration_ticks(path, kind):
    report = media.run([media.require("ffprobe"),"-v","error","-select_streams",kind+":0","-show_entries",
                        "stream=duration_ts,time_base","-of","json",str(path)],30)
    try:
        row = json.loads(report.stdout)["streams"][0]
        return float(Fraction(row["time_base"])*int(row["duration_ts"]))
    except (ValueError, KeyError, IndexError):
        raise TransferHold("Could not verify the retained rendition's playable duration.") from None


@lru_cache(maxsize=4)
def http_read_options(ffmpeg):
    report = media.run([ffmpeg,"-hide_banner","-h","protocol=http"],10)
    if report.returncode or not re.search(rb"(?m)^\s+-request_size\s", report.stdout):
        return ()
    # Keep an abandoned seek from buffering several upstream blocks before FFmpeg repositions.
    return ("-request_size",str(BLOCK),"-short_seek_size",str(BLOCK),"-multiple_requests","1")


def acquire(item, start, end, kind, path, ledger, *, opener=None):
    part = path.with_name(path.stem+".partial"+path.suffix)
    length = end-start
    decode_from = start
    if kind=="audio":
        # A seek directly into AAC can advance the decoded cue by a partial packet.
        # Decode a short preroll, then trim to the requested absolute sample boundaries.
        decode_from = max(0,start-2)
        preroll = start-decode_from
        take = ["-t",f"{length+preroll+.25:.6f}"]
        codec = ["-map","0:a:0","-vn","-af",
                 f"aresample=48000:first_pts=0,atrim=start_sample={round(preroll*48000)}:"
                 f"end_sample={round((preroll+length)*48000)},asetpts=N/SR/TB",
                 "-c:a","flac","-ar","48000","-ac","2"]
    else:
        take = []
        codec = ["-map","0:v:0","-an","-vf","fps=30","-c:v","libx264","-preset","veryfast",
                 "-threads","4","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart"]
    ffmpeg = media.require("ffmpeg")
    ranges = RemoteRanges(item,start,end,kind,ledger,opener)
    try:
        with ranges as url:
            result = media.run([ffmpeg,"-nostdin","-v","error","-xerror","-y",
                                "-protocol_whitelist","http,tcp","-rw_timeout","18000000",*http_read_options(ffmpeg),
                                "-ss",f"{decode_from:.6f}",
                                *take,"-i",url,*(["-t",f"{length:.6f}"] if not take else []),*codec,str(part)],
                               max(150, math.ceil(length*2)))
    except media.MediaError:
        if ranges.budget_error:
            raise TransferBudget(ranges.error) from None
        raise TransferHold(ranges.error or "The selected rendition timed out during bounded decoding.") from None
    if ranges.error or result.returncode:
        lines = result.stderr.decode(errors="replace").splitlines()
        detail = re.sub(r"https?://\S+","[local range reader]",lines[-1])[:160] if lines else ""
        ledger.end(ranges.entry,"refused" if ranges.error else "decode-failed")
        error_type = TransferBudget if ranges.budget_error else TransferHold
        raise error_type(ranges.error or ("The rendition failed bounded decoding. "+detail).strip())
    actual = duration_ticks(part,kind[0])
    if abs(actual-length) > (.08 if kind=="video" else .035):
        ledger.end(ranges.entry,"incomplete")
        raise TransferHold("The retained range has a clock gap or ended early. Retry or attach a verified recording.")
    part.replace(path)
    return {"actual":actual,"bytes":path.stat().st_size,"hash":media.checksum(path),
            "rendition":str(item["format_id"]),"height":item.get("height") if kind=="video" else None,
            "transfer":ranges.entry["received"],"decode_from":decode_from}
