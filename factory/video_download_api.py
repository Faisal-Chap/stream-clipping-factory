"""Isolated Video Download API evaluation client and resumable smoke-test runner."""


import argparse
import array
import json
import math
import os
import re
import shutil
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener

from . import flashdata, media, streamer_asr
from .provider_meter import ProviderPending

HOST = "p.savenow.to"
BASE = f"https://{HOST}"
RAPID_HOST = "youtube-info-download-api.p.rapidapi.com"
RAPID_MEDIA_PROVIDER = "video_download_rapidapi"
VIDEO_FORMATS = ("1080", "720", "480")
PRICES_MICRO_USD = {"1080": 200, "720": 200, "m4a": 150}
STANDARD_SECONDS = 180 * 60
OWNER = "gta-streamer-factory"
EVIDENCE_FOLDER = "gta-streamer-factory-disposable-video-download-api-evaluation"


class EvaluationError(Exception):
    pass


class Rejected(EvaluationError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Video Download API rejected the request: {reason}.")


def configured_provider():
    # VDA_EXPERIMENT: Gate the standalone experiment. The production worker retains its original routing.
    provider = os.environ.get("MEDIA_PROVIDER", "flashdata")
    if provider not in ("flashdata", "video_download_api", RAPID_MEDIA_PROVIDER):
        raise EvaluationError("Choose flashdata, video_download_api, or video_download_rapidapi in MEDIA_PROVIDER.")
    return provider


def source_url(video):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise EvaluationError("Invalid YouTube video ID.")
    return f"https://www.youtube.com/watch?v={video}"


def safe_https(url, hosts):
    if not isinstance(url, str) or not url.strip():
        raise EvaluationError("The provider did not supply a usable URL.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc not in hosts or parsed.username or parsed.password
                or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise EvaluationError("The provider returned an unapproved HTTPS destination.") from None
    return parsed


def failure_reason(data):
    code = data.get("error_code")
    if code in ("insufficient_balance", "invalid_api_key", "api_key_required"):
        return code
    message = " ".join(str(data.get(k, "")) for k in ("text", "message", "error")).lower()
    if "duration limit" in message or "maximum duration" in message or "exceeds the duration" in message:
        return "source_duration_limit"
    if "not enough balance" in message:
        return "insufficient_balance"
    if "not subscribed" in message or "not authorized to access" in message:
        return "marketplace_plan_access"
    if "endpoint" in message and ("does not exist" in message or "not found" in message):
        return "marketplace_endpoint_unavailable"
    if "invalid format" in message or "unsupported format" in message or "unknown format" in message:
        return "unsupported_format"
    if "downloads are failing on our side" in message:
        return "provider_outage"
    if "expired" in message:
        return "delivery_link_expired"
    return "provider_failure"


def accepted(value):
    return value is True or type(value) is int and value == 1


def duration_seconds(value):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return value
    if isinstance(value,str) and re.fullmatch(r"(?:\d{1,3}:)?\d{1,3}:[0-5]\d",value):
        parts = list(map(int,value.split(":")))
        if len(parts)==3 and parts[1]>=60:
            return None
        seconds = sum(part*60**index for index,part in enumerate(reversed(parts)))
        return seconds or None
    return None


class Client:
    host = HOST

    def __init__(self, key):
        if not key or not key.strip():
            raise EvaluationError("The selected provider needs its own API key.")
        self.key = key
        self.opener = build_opener(flashdata.NoRedirect)

    def api_url(self, route, fields, secret_name=None):
        return f"https://{self.host}{route}?{urlencode(fields | ({secret_name:self.key} if secret_name else {}))}"

    def headers(self, _host):
        return {"Accept":"application/json","User-Agent":"GTA-Streamer-Factory/1.0"}

    def record_request(self, _route, _gateway, _submission):
        return None

    def record_response(self, _entry, _status, _headers):
        pass

    def read(self, url, *, submission=False):
        parsed = safe_https(url, (self.host,HOST))
        request = Request(url, headers=self.headers(parsed.hostname))
        entry = self.record_request(parsed.path,parsed.hostname==self.host,submission)
        try:
            with self.opener.open(request, timeout=25) as reply:
                self.record_response(entry,reply.status,reply.headers)
                result = json.loads(reply.read(1024**2))
        except HTTPError as error:
            with error:
                self.record_response(entry,error.code,error.headers)
                try:
                    body = json.loads(error.read(64*1024))
                except (ValueError, OSError):
                    body = {}
            if error.code in (400, 401, 402, 403, 404, 422) and isinstance(body, dict) and not body.get("id"):
                raise Rejected(failure_reason(body) if body else f"HTTP {error.code}") from None
            raise EvaluationError(f"Video Download API returned HTTP {error.code}. A saved submission must be reconciled.") from None
        except (URLError, OSError, ValueError):
            message = ("Submission outcome is unknown. Check the provider console before another submission."
                       if submission else "Video Download API status or metadata is temporarily unavailable.")
            raise EvaluationError(message) from None
        if not isinstance(result, dict):
            raise EvaluationError("Video Download API returned an unexpected response.")
        return result

    def metadata(self, video):
        fields = {"function": "i", "u": source_url(video)}
        answer = self.read(self.api_url("/ajax/api.php",fields,"api"))
        if answer.get("success") is False:
            raise Rejected(failure_reason(answer))
        duration = duration_seconds(answer.get("duration"))
        if answer.get("id") != video or not duration:
            raise EvaluationError("Provider metadata did not verify the source ID and duration.")
        prices = answer.get("download_cost_estimates") or {}
        return {"id": video, "duration": duration, "title": answer.get("title"), "channel": answer.get("channel"),
                "quotes_microusd": {name: price_micros(values.get("final_price"))
                                    for name, values in prices.items() if name in (*VIDEO_FORMATS, "m4a")
                                    and isinstance(values, dict)}}

    def download_endpoint(self, clipping):
        return "/ajax/download.php" if clipping else "/api/v2/download"

    def submit(self, fields, *, clipping):
        answer = self.read(self.api_url(self.download_endpoint(clipping),fields,"apikey"), submission=True)
        job = answer.get("id")
        if not job and answer.get("success") is False:
            raise Rejected(failure_reason(answer))
        if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job):
            raise EvaluationError("Submission outcome is unknown. The provider did not return a valid job ID.")
        return job, answer.get("progress_url")

    def progress(self, job, route="/ajax/progress.php"):
        base = route if route.startswith("https://") else f"https://{self.host}{route}"
        answer = self.read(f"{base}?{urlencode({'id':job})}")
        progress = answer.get("progress")
        if type(progress) not in (int, float) or not 0 <= progress <= 1000:
            raise EvaluationError("Video Download API returned invalid progress.")
        if progress < 1000:
            return progress, None, None
        link = answer.get("download_url")
        if not accepted(answer.get("success")) or not isinstance(link, str) or not link.strip():
            return progress, None, failure_reason(answer)
        return progress, link, None

    def resolve_progress(self, value, job):
        return progress_route(value,job)

    def reserve(self, profile, format_id, args):
        return reservation(profile,format_id,args.extended_minutes)


class RapidClient(Client):
    """RapidAPI plan credentials and usage stay in the marketplace experiment."""

    host = RAPID_HOST

    def __init__(self, key, host, journal, max_calls):
        if host != RAPID_HOST:
            raise EvaluationError("Set VIDEO_DOWNLOAD_RAPIDAPI_HOST to the documented marketplace gateway.")
        super().__init__(key)
        self.journal = journal
        self.max_calls = max_calls
        self.rows = streamer_asr.read(journal) if journal.is_file() else []

    def api_url(self, route, fields, secret_name=None):
        return super().api_url(route,fields)

    def headers(self, host):
        result = super().headers(host)
        if host == self.host:
            result.update({"X-RapidAPI-Key":self.key,"X-RapidAPI-Host":self.host})
        return result

    def metadata(self, video):
        profile = super().metadata(video)
        profile.pop("quotes_microusd")
        return profile

    def download_endpoint(self, _clipping):
        return "/ajax/download.php"

    def resolve_progress(self, value, job):
        if value:
            try:
                return progress_route(value,job,marketplace=True)
            except EvaluationError:
                pass
        # Some marketplace receipts omit a usable progress URL. Poll the documented gateway route using the saved ID.
        return f"https://{self.host}/api/progress"

    def reserve(self, profile, format_id, args):
        if args.extended_minutes:
            raise EvaluationError("Marketplace extended-duration pricing must be confirmed before submitting.")
        return 0  # RapidAPI quota is checked separately from the direct provider's dollar wallet.

    def record_request(self, route, gateway, submission):
        if len(self.rows) >= self.max_calls:
            if submission:
                raise Rejected("marketplace_test_call_cap")
            raise EvaluationError("The isolated RapidAPI call cap was reached. Saved provider IDs are retained.")
        last = next((item for item in reversed(self.rows) if item["gateway"] and item.get("quota")),None)
        if gateway and last:
            remaining = [value for name,value in last["quota"].items() if name.endswith("-remaining")]
            if remaining and min(remaining)<=0:
                if submission:
                    raise Rejected("marketplace_quota_exhausted")
                raise EvaluationError("RapidAPI quota exhausted. The trial is holding further requests.")
        if submission and (not last or last["quota"].get("x-ratelimit-units-remaining",0)<50 or
                           not any(row.get("route")=="/ajax/api.php" and row.get("http")==200 for row in self.rows)):
            raise Rejected("marketplace_metadata_or_quota_preflight_required")
        item = {"route":route,"gateway":gateway,"submitted":time.time()}
        self.rows.append(item)
        streamer_asr.write(self.journal,self.rows)
        return item

    def record_response(self, entry, status, headers):
        quota = {}
        for name,value in headers.items():
            if (re.fullmatch(r"x-rate-?limit-[a-z0-9-]+-(?:remaining|limit|reset)",name.lower())
                    and value.isascii() and value.isdigit() and len(value)<15):
                quota[name.lower()] = int(value)
        entry.update(http=status,seconds=round(time.time()-entry["submitted"],3),quota=quota)
        streamer_asr.write(self.journal,self.rows)


def progress_route(value, job, *, marketplace=False):
    if value is None:
        return "/ajax/progress.php"
    url = safe_https(value, (HOST,RAPID_HOST) if marketplace else (HOST,))
    if url.path not in ("/ajax/progress.php", "/ajax/progress", "/api/progress", "/api/progress.php") or (
            parse_qs(url.query, keep_blank_values=True) != {"id": [job]}):
        raise EvaluationError("The provider returned an unexpected progress endpoint. The saved job ID is retained.")
    return f"https://{url.netloc}{url.path}" if marketplace else url.path


def price_micros(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and 0 < amount < 100:
            return int((amount * 10**6).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation:
        pass
    return None


def request_fields(profile, format_id, bounds, extended_minutes):
    duration = profile["duration"]
    if format_id not in (*VIDEO_FORMATS, "m4a"):
        raise EvaluationError("Choose 1080p, 720p, 480p, or M4A.")
    if bounds and not 0 <= bounds[0] < bounds[1] <= duration:
        raise EvaluationError("The requested interval is outside the verified source clock.")
    if not bounds and duration > STANDARD_SECONDS:
        raise EvaluationError("Full-source downloads over 180 minutes are outside this experiment.")
    if extended_minutes and (not bounds or not math.ceil(duration/60) <= extended_minutes <= 1440):
        raise EvaluationError("Set an approved duration cap covering the source and at most 24 hours.")
    fields = {"url":source_url(profile["id"]), "format":format_id,
              "allow_extended_duration":int(bool(extended_minutes))}
    if bounds:
        fields.update(start_time=bounds[0], end_time=bounds[1], no_merge=0)
    if extended_minutes:
        fields["max_duration"] = extended_minutes
    return fields


def reservation(profile, format_id, extended_minutes):
    quote = profile["quotes_microusd"].get(format_id)
    if extended_minutes and not quote:
        raise EvaluationError("Extended-duration testing requires a provider price estimate.")
    if quote and extended_minutes:
        return quote
    price = max(PRICES_MICRO_USD.get(format_id, 0), quote or 0)
    if not price:
        raise EvaluationError("A provider price estimate is required for this format.")
    return price


def receive(client, link, partial, max_bytes):
    try:
        host = urlsplit(link).netloc
    except ValueError:
        raise EvaluationError("The provider returned an unapproved media host.") from None
    # VDA_EXPERIMENT: The provider allocates delivery hosts under its own DNS zone.
    if host!="video-download-api.com" and not re.fullmatch(r"[a-z][a-z0-9-]{0,61}\.savenow\.to",host):
        raise EvaluationError("The provider returned an unapproved media host.")
    safe_https(link,(host,))
    if shutil.disk_usage(partial.parent).free < max_bytes + 256*1024**2:
        raise EvaluationError("Oracle disk space is below the experiment download reserve.")
    try:
        with client.opener.open(Request(link, headers={"Accept-Encoding":"identity"}),timeout=25) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or not 0 < int(length) <= max_bytes):
                raise EvaluationError("The provider file exceeds the approved byte cap or is empty.")
            written, deadline = 0, time.monotonic()+600
            with partial.open("wb") as output:
                while block := response.read(media.CHUNK):
                    written += len(block)
                    if written > max_bytes or time.monotonic()>deadline:
                        raise EvaluationError("The media transfer exceeded its byte or time limit.")
                    output.write(block)
                if not written or length is not None and written!=int(length):
                    raise EvaluationError("The media transfer was incomplete.")
                output.flush()
                os.fsync(output.fileno())
    except (URLError, OSError) as error:
        code = error.code if isinstance(error, HTTPError) else "connection or file error"
        raise EvaluationError(f"Media transfer failed ({code}). Resume the saved provider job.") from None


def verify(path, format_id, seconds):
    response = media.run([media.require("ffprobe"),"-v","error","-protocol_whitelist","file",
                          "-format_whitelist","mov","-show_streams","-show_format","-of","json",str(path)],30)
    try:
        probe = json.loads(response.stdout)
        video = next((s for s in probe["streams"] if s["codec_type"] == "video"),None)
        audio = next((s for s in probe["streams"] if s["codec_type"] == "audio"),None)
        track = audio if format_id == "m4a" else video
        duration = float(probe["format"]["duration"])
        tolerance = .35
        if (response.returncode or not track or not audio or not math.isfinite(duration) or
                abs(duration-seconds) > tolerance or any(abs(float(s.get("duration",duration))-seconds)>tolerance
                                                        for s in (audio,track))):
            raise ValueError()
        if format_id == "m4a":
            if video or audio["codec_name"] != "aac":
                raise ValueError()
        elif (video["height"] < 480 or abs(float(video.get("start_time","0"))-
                                            float(audio.get("start_time","0")))>tolerance):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise Rejected("media_duration_tracks_or_resolution_failed") from None
    decode = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin",
                        "-protocol_whitelist","file","-format_whitelist","mov","-i",str(path),
                        "-map","0:a:0","-map","0:v:0?","-f","null","-"],600)
    if decode.returncode:
        raise Rejected("media_decode_failed")
    return {"duration":duration, "audio_codec":audio["codec_name"], "bytes":path.stat().st_size,
            "sha256":media.checksum(path), "width":video["width"] if video else None,
            "height":video["height"] if video else None, "codec":video["codec_name"] if video else None,
            "fps":video.get("avg_frame_rate") if video else None}


def compare_frames(path, reference, left, seconds):
    def frame(file, at):
        reply = media.run([media.require("ffmpeg"),"-v","error","-protocol_whitelist","file","-ss",str(at),"-i",str(file),
                           "-frames:v","1","-vf","scale=96:54,format=gray","-f","rawvideo","-"],20)
        if reply.returncode or len(reply.stdout)!=96*54:
            raise Rejected("source_frame_comparison_failed")
        return reply.stdout
    differences = []
    for offset in (seconds*.1, seconds*.5, seconds*.9):
        actual, expected = frame(path,offset),frame(reference,left+offset)
        differences.append(round(sum(abs(a-b) for a,b in zip(actual,expected))/len(actual),3))
    if max(differences)>18:
        raise Rejected("source_frame_comparison_failed")
    return differences


def compare_audio(path, reference, left, seconds):
    def envelope(file, at):
        reply = media.run([media.require("ffmpeg"),"-v","error","-protocol_whitelist","file","-ss",str(at),
                           "-i",str(file),"-t","4","-map","0:a:0","-ac","1","-ar","2000","-f","s16le","-"],20)
        if reply.returncode or len(reply.stdout)<15600:
            raise Rejected("source_audio_comparison_failed")
        data = array.array("h",reply.stdout)
        return [sum(abs(sample) for sample in data[i:i+40])/40 for i in range(0,len(data)-39,40)]
    scores = []
    for offset in (seconds*.1,seconds*.5,seconds*.85):
        a,b = envelope(path,offset),envelope(reference,left+offset)
        candidates = []
        for shift in range(-12,13):
            pairs = list(zip(a[max(0,shift):],b[max(0,-shift):]))
            ax,by = (sum(x)/len(pairs) for x in zip(*pairs))
            av,bv = sum((x-ax)**2 for x,_ in pairs),sum((y-by)**2 for _,y in pairs)
            if av and bv:
                score = sum((x-ax)*(y-by) for x,y in pairs)/math.sqrt(av*bv)
                candidates.append((score,-abs(shift),shift))
        if candidates:
            score,_,shift = max(candidates)
            scores.append({"similarity":round(score,3),"lag_ms":shift*20})
    if len(scores)<2 or any(s["similarity"]<.7 or abs(s["lag_ms"])>180 for s in scores):
        raise Rejected("source_audio_comparison_failed")
    return scores


def extract(folder, attempt, bounds, max_bytes):
    left,right = bounds
    if not 0<=left<right<=attempt["probe"]["duration"] or right-left>3600:
        raise EvaluationError("The local extraction must be inside the verified source and at most one hour.")
    source = folder/attempt["file"]
    audio = source.suffix==".m4a"
    if not audio and right-left>60:
        raise EvaluationError("The local picture extraction is capped at 60 seconds.")
    target = source.with_name(f"{source.stem}-local-{left}-{right}{source.suffix}")
    partial = target.with_suffix(".partial"+target.suffix)
    encoding = ["-vn","-c:a","aac"] if audio else ["-c:v","libx264","-c:a","aac","-preset","veryfast"]
    started = time.monotonic()
    cut = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin","-y",
                     "-protocol_whitelist","file","-format_whitelist","mov","-ss",str(left),"-i",str(source),
                     "-t",str(right-left),"-fs",str(max_bytes),*encoding,str(partial)],240)
    if cut.returncode:
        raise EvaluationError("The local FFmpeg extraction failed.")
    checked = verify(partial,attempt["request"]["format"],right-left)
    partial.replace(target)
    return {"file":target.name,"range":[left,right],"seconds":round(time.monotonic()-started,3),"probe":checked}


def workspace(parent, *, marketplace=False):
    name = EVIDENCE_FOLDER.replace("-evaluation","-rapidapi-evaluation") if marketplace else EVIDENCE_FOLDER
    folder = parent.resolve()/name
    folder.mkdir(mode=0o700,parents=False,exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise EvaluationError("An isolated experiment directory is required.")
    return folder


def evaluate(client, folder, profile, args):
    bounds = tuple(args.interval) if args.interval else None
    if bounds and bounds[1]-bounds[0] > (3600 if args.audio else 60):
        raise EvaluationError("The experiment caps video clips at 60 seconds and audio segments at one hour.")
    if (args.reference or args.audio_reference) and (not bounds or args.reference_origin > bounds[0] or
                                                   args.audio and args.reference):
        raise EvaluationError("Comparison references must cover the requested audio or picture interval.")
    group = f"{profile['id']}-{'audio' if args.audio else 'video'}-"
    group += f"{bounds[0]}-{bounds[1]}" if bounds else "full"
    journal = folder/"attempts.json"
    rows = streamer_asr.read(journal) if journal.exists() else []
    group_rows = [item for item in rows if item["group"]==group]
    ranked = ("m4a",) if args.audio else VIDEO_FORMATS
    for format_id in ranked:
        fields = request_fields(profile,format_id,bounds,args.extended_minutes)
        previous = [item for item in group_rows if item["request"] == fields]
        attempt = previous[-1] if previous else None
        if attempt and attempt["state"] == "verified":
            path = folder/attempt["file"]
            if path.is_file() and media.checksum(path)==attempt["probe"]["sha256"]:
                return attempt
            raise EvaluationError("The verified experiment file changed or is missing.")
        if attempt and attempt["state"] in ("failed","rejected"):
            if attempt.get("fallback"):
                continue
            return attempt
        blockers = [item for item in group_rows if item["state"] in ("submitting","pending","stalled","delivered")]
        if not attempt and blockers:
            raise EvaluationError("This interval already has a saved or unresolved job. Resume its original parameters.")
        if not attempt:
            reserve = client.reserve(profile,format_id,args)
            spent = sum(item["reserved_microusd"] for item in rows if item["state"]!="rejected")
            if spent+reserve>args.budget_microusd:
                raise EvaluationError("The authorized cumulative test budget would be exceeded.")
            attempt = {"group":group,"request":fields,"reserved_microusd":reserve,"state":"submitting","submitted":time.time()}
            rows.append(attempt)
            streamer_asr.write(journal,rows)
            try:
                job, reported_route = client.submit(fields,clipping=bool(bounds))
            except Rejected as error:
                attempt.update(state="rejected", error=error.reason, fallback=error.reason=="unsupported_format")
                streamer_asr.write(journal,rows)
                if attempt["fallback"]:
                    continue
                return attempt
            attempt.update(id=job,state="pending")
            streamer_asr.write(journal,rows)
            attempt["progress_route"] = client.resolve_progress(reported_route,job)
            streamer_asr.write(journal,rows)
        if not attempt.get("id"):
            raise EvaluationError("Submission outcome is unknown. Reconcile the recorded attempt before retrying.")
        deadline, delay = time.monotonic()+args.wait, 5
        while True:
            route = attempt.get("progress_route") or client.resolve_progress(None,attempt["id"])
            fraction, link, failure = client.progress(attempt["id"],route)
            attempt.update(progress=fraction, checked=time.time())
            if failure:
                attempt.update(state="failed",error=failure,
                               fallback=failure not in ("source_duration_limit","insufficient_balance","provider_outage",
                                                        "invalid_api_key","api_key_required","delivery_link_expired"))
                streamer_asr.write(journal,rows)
                break
            if link:
                attempt["state"] = "delivered"
                attempt.setdefault("ready_seen",attempt["checked"])
                streamer_asr.write(journal,rows)
                target = folder/(group+f"-{format_id}-{len(rows)}."+("m4a" if args.audio else "mp4"))
                partial = target.with_suffix(".partial"+target.suffix)
                receive(client,link,partial,args.max_bytes)
                try:
                    seconds = bounds[1]-bounds[0] if bounds else profile["duration"]
                    attempt["probe"] = verify(partial,format_id,seconds)
                    if args.reference:
                        attempt["frame_differences"] = compare_frames(partial,args.reference,bounds[0]-args.reference_origin,seconds)
                    if args.audio_reference:
                        attempt["audio_alignment"] = compare_audio(partial,args.audio_reference,
                                                                   bounds[0]-args.reference_origin,seconds)
                except Rejected as error:
                    attempt.update(state="failed",error=error.reason,
                                   fallback=error.reason not in ("source_frame_comparison_failed","source_audio_comparison_failed"))
                    streamer_asr.write(journal,rows)
                    break
                partial.replace(target)
                attempt.update(state="verified",file=target.name,verified=time.time())
                streamer_asr.write(journal,rows)
                return attempt
            if time.monotonic() >= deadline:
                attempt["state"] = "stalled"
                streamer_asr.write(journal,rows)
                return attempt
            attempt["state"] = "pending"
            streamer_asr.write(journal,rows)
            time.sleep(min(delay,max(0,deadline-time.monotonic())))
            delay = min(delay*2,60)
        if not attempt.get("fallback"):
            return attempt
    return attempt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video",required=True,help="YouTube video ID")
    parser.add_argument("--evidence-parent",type=Path)
    parser.add_argument("--metadata-only",action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interval",type=int,nargs=2,metavar=("START","END"))
    mode.add_argument("--full",action="store_true")
    parser.add_argument("--audio",action="store_true")
    parser.add_argument("--extended-minutes",type=int,default=0)
    parser.add_argument("--budget-microusd",type=int,default=0)
    parser.add_argument("--max-bytes",type=int,default=128*1024**2)
    parser.add_argument("--wait",type=int,default=300)
    parser.add_argument("--max-api-calls",type=int,default=40)
    parser.add_argument("--reference",type=Path)
    parser.add_argument("--audio-reference",type=Path)
    parser.add_argument("--reference-origin",type=int,default=0)
    parser.add_argument("--extract",type=int,nargs=2,metavar=("START","END"))
    args = parser.parse_args()
    try:
        selected = configured_provider()
        if selected=="flashdata":
            print("FlashData remains selected. The experiment made no provider requests.")
            return 0
        if not (0<=args.wait<=3600 and 0<=args.budget_microusd<=100000 and 0<args.max_bytes<=8*1024**3):
            raise EvaluationError("Set an observation window up to 3600 seconds, a budget up to $0.10, and a byte cap up to 8 GiB.")
        if not args.metadata_only and (not args.evidence_parent or not (args.interval or args.full)):
            raise EvaluationError("A media test requires an evidence parent and an explicit interval or full-source test.")
        if args.extract and not args.full:
            raise EvaluationError("Use --extract with a verified full-source test.")
        marketplace = selected==RAPID_MEDIA_PROVIDER
        if marketplace and (not args.evidence_parent or not 1<=args.max_api_calls<=60):
            raise EvaluationError("The marketplace test requires an evidence parent and a call cap from 1 through 60.")
        # VDA_EXPERIMENT: Export the selected credential in the experiment only.
        if not marketplace and args.metadata_only:
            client = Client(os.environ.get("VIDEO_DOWNLOAD_API_KEY"))
            print(json.dumps(client.metadata(args.video)))
            return 0
        folder = workspace(args.evidence_parent,marketplace=marketplace)
        with flashdata.attempt_lock(folder):
            client = (RapidClient(os.environ.get("VIDEO_DOWNLOAD_RAPIDAPI_KEY"),
                                  os.environ.get("VIDEO_DOWNLOAD_RAPIDAPI_HOST",RAPID_HOST),
                                  folder/"gateway-usage.json",args.max_api_calls) if marketplace
                      else Client(os.environ.get("VIDEO_DOWNLOAD_API_KEY")))
            card = folder/(args.video+"-metadata.json")
            profile = (streamer_asr.read(card) if card.is_file() and not args.metadata_only
                       else client.metadata(args.video))
            streamer_asr.write(card,profile)
            if args.metadata_only:
                print(json.dumps(profile))
                return 0
            result = evaluate(client,folder,profile,args)
            if args.extract and result["state"]=="verified":
                result = result | {"local_extract":extract(folder,result,args.extract,args.max_bytes)}
        print(json.dumps(result))
        return 0 if result["state"]=="verified" else 2
    except (EvaluationError, ProviderPending, media.MediaError) as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
