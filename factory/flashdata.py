"""Bounded FlashData acquisition through private, request-owned R2 objects."""

import gzip
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import flashdata_profiles as profiles, media, provider_vault, store, streamer_asr, streamer_transfer as transfer
from .provider_meter import ProviderPending
from .streamer_render import inspect_media

REFERENCE = "flashdata-r2"
PREFIX = "gta-streamer-factory/flashdata"
MAX_OBJECT = 256*1024**2
MAX_BUCKET = 9_000_000_000
QUERIES = "https://data.flashdata.dev/v1/queries"
JOBS = "https://api.flashdata.dev/v2/jobs/"
ACCESS = "https://api.flashdata.dev/v2/api-access"
CLEANUP_LOCK = Lock()
AUDIO_WAIT = 2 * 3600
VIDEO_WAIT = 30 * 60


class StatusUnavailable(Exception):
    def __init__(self, retry_after=""):
        self.retry_after = retry_after


class DeliveryStalled(transfer.TransferHold):
    pass


class DeliveryFailed(transfer.TransferHold):
    def __init__(self, message, *, format_error=False):
        self.format_error = format_error
        super().__init__(message)


class SubmissionRejected(transfer.TransferHold):
    def __init__(self, code):
        self.code = code
        super().__init__(f"FlashData rejected the request (HTTP {code}). Check the source, credentials or balance.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def configuration(root):
    vault = provider_vault.vault_for(root)
    raw = vault.get(REFERENCE) if vault.available else None
    if not raw:
        return None
    try:
        values = json.loads(raw)
        if (not re.fullmatch(r"https://[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com",values["endpoint"])
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",values["bucket"])
                or not all(re.fullmatch(r"[A-Za-z0-9_.-]{16,200}",values[k])
                           for k in ("api_key","access_key","secret_key"))):
            raise ValueError()
    except (KeyError,TypeError,ValueError):
        raise transfer.TransferHold("The Factory FlashData/R2 credential is invalid.") from None
    return values


def required(root):
    config = configuration(root)
    if config is None:
        raise transfer.TransferHold("FlashData/R2 is required for production YouTube acquisition.")
    return config


def youtube_configuration(root):
    if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
        return required(root)
    return configuration(root)


def api_headers(config):
    return {"X-API-Key":config["api_key"],"Accept":"application/json","User-Agent":"GTA-Streamer-Factory/1.0"}


def request(config, url, payload=None):
    headers = api_headers(config)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
    try:
        with build_opener(NoRedirect).open(request,timeout=40) as response:
            return json.load(response),response.headers.get("Retry-After","")
    except HTTPError as error:
        code = error.code
        retry_after = error.headers.get("Retry-After","")
        error.close()
        if payload is None and url.startswith(JOBS) and (code in (408,429) or 500<=code<600):
            raise StatusUnavailable(retry_after) from None
        if payload is not None and code in (400,401,402,403,404,422,429):
            raise SubmissionRejected(code) from None
        raise transfer.TransferHold(f"FlashData returned HTTP {code}. Check the connection, permissions or usage balance.") from None
    except (URLError,OSError,ValueError):
        if payload is None and url.startswith(JOBS):
            raise StatusUnavailable() from None
        raise transfer.TransferHold("FlashData did not return a valid response. Check its job console if a submission was interrupted.") from None


def result(config, remote, prefix):
    """Follow the documented signed result redirect without forwarding the API key."""
    opener = build_opener(NoRedirect)
    try:
        try:
            with opener.open(Request(JOBS+remote+"/results",headers=api_headers(config)),timeout=20) as reply:
                body = reply.read(2**20)
        except HTTPError as error:
            target, code = error.headers.get("Location",""), error.code
            error.close()
            access,_ = request(config,ACCESS)
            parsed = urlsplit(target)
            if (code != 302 or parsed.scheme != "https" or
                    f"https://{parsed.netloc}" != access.get("result_origin") or
                    not re.fullmatch(r"[a-f0-9]{32}(?:\.eu)?\.r2\.cloudflarestorage\.com", parsed.netloc)):
                raise ValueError()
            with opener.open(Request(target,headers={"Accept-Encoding":"identity"}),timeout=20) as reply:
                body = reply.read(2**20)
        if body.startswith(b"\x1f\x8b"):
            from io import BytesIO
            with gzip.GzipFile(fileobj=BytesIO(body)) as stream:
                body = stream.read(2**20)
        report = json.loads(body)
        row, = report["results"]
        uri = urlsplit(row["file_url"])
        path = unquote(uri.path).lstrip("/")
        if path.startswith(config["bucket"]+"/"):
            path = path[len(config["bucket"])+1:]
        if (not path.startswith(prefix+"/") or type(row["file_size"]) is not int or
                row.get("md5") and not re.fullmatch(r"[a-fA-F0-9]{32}",row["md5"])):
            raise ValueError()
        return {"key":path,"file_size":row["file_size"],"md5":row.get("md5"),
                "downloaded_bytes":row.get("downloaded_bytes"),"credits":(report.get("usage") or {}).get("credits")}
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        raise StatusUnavailable() from None


def metadata(config, video):
    result,_ = request(config,QUERIES+"/realtime",{"source":"youtube_metadata","video_id":video,
                       "params":{"include_formats":True,"include_chapters":False}})
    try:
        row = result["results"][0]
        duration = float(row["duration"] or 0)
        if (row["videoId"]!=video or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",row["channelId"])
                or not math.isfinite(duration) or duration<0):
            raise ValueError()
        live = "is_live" if row.get("isLive") else "was_live" if row.get("wasLive") else "not_live"
        thumbnails = row.get("thumbnails") or []
        return {"id":video,"channel_id":row["channelId"],"channel":row.get("channel"),"title":row["title"],
                "url":f"https://www.youtube.com/watch?v={video}","description":row.get("description"),
                "upload_date":row.get("uploadDate"),"thumbnails":[
                    {key:item[key] for key in ("url","height","width") if key in item}
                    for item in thumbnails if isinstance(item,dict) and str(item.get("url","")).startswith(
                        ("https://i.ytimg.com/", "https://img.youtube.com/"))],
                "duration":duration,"live_status":live,"formats":list(profiles.formats(row.get("formats") or [])),
                "credits":(result.get("usage") or {}).get("credits")}
    except (KeyError,IndexError,ValueError,TypeError):
        raise transfer.TransferHold("FlashData did not verify the replay identity and duration.") from None


def r2(config):
    import boto3
    from botocore.config import Config
    return boto3.client("s3",endpoint_url=config["endpoint"],region_name="auto",
                        aws_access_key_id=config["access_key"],aws_secret_access_key=config["secret_key"],
                        config=Config(signature_version="s3v4",s3={"addressing_style":"path"},
                                      connect_timeout=10,read_timeout=25,retries={"max_attempts":2,"mode":"standard"}))


def clock(second):
    return f"{second//3600:02}:{second%3600//60:02}:{second%60:02}"


def payload(config, video, purpose, left, right, prefix, rendition=None):
    if type(left) is not int or type(right) is not int or not 0<=left<right<=transfer.REPLAY_LIMIT:
        raise transfer.TransferHold(transfer.REPLAY_LIMIT_MESSAGE)
    audio = purpose=="audio"
    if right-left > (3600 if audio else 120):
        raise transfer.TransferBudget("The requested FlashData range exceeds the Factory transfer window.")
    params = {"download_type":"audio" if audio else "video","output_format":"m4a" if audio else "mp4",
              "start_at":clock(left),"end_at":clock(right),"upload_metadata":False,
              "storage":{"storage_type":"s3_compatible","storage_url":config["bucket"],
                         "endpoint_url":config["endpoint"],"region":"auto","object_prefix":prefix,
                         "access_key":config["access_key"],"secret_key":config["secret_key"]}}
    if rendition:
        params["yt_dlp_format"] = rendition["format_id"]
    elif audio:
        params.update(audio_language="original",audio_quality="128")
    else:
        params.update(video_quality={"preview":"<=360","hd":"==1080","hd720":"==720"}[purpose])
    return {"source":"youtube_download","video_id":video,"params":params}


def retry_seconds(hint, now):
    if hint.isascii() and hint.isdigit() and len(hint)<10:
        return int(hint)
    try:
        return max(0,math.ceil(parsedate_to_datetime(hint).timestamp()-now))
    except (TypeError,ValueError,OverflowError,OSError):
        return 0


def pending(root, job, identity, file, state, hint, *, unavailable=False, result_wait=False):
    now = time.time()
    limit = state.get("timeout", AUDIO_WAIT if state["purpose"]=="audio" else VIDEO_WAIT)
    age = now-state["submitted_at"]
    advised = retry_seconds(hint,now)
    if age>=limit:
        if result_wait:
            state["delivery_status"] = "result unavailable"
            streamer_asr.write(file,state)
            raise transfer.TransferHold("FlashData delivered the interval but its result record is unavailable. Resume the saved request.")
        state["delivery_status"] = "stalled"
        streamer_asr.write(file,state)
        if state.get("format"):
            profiles.record(root,identity,"stalled")
        raise DeliveryStalled(f"FlashData delivery paused at the {limit//60}-minute wait limit. "
                              "Resume to check the saved request again.")
    polls = state.get("poll_count",0)
    delay = max(min(300,15*2**min(polls,5),limit-age),advised)
    state.update(poll_count=polls+1,delivery_status="status unavailable" if unavailable else "waiting")
    streamer_asr.write(file,state)
    defer(root,job,identity,state["remote"],now+delay)
    raise ProviderPending("FlashData status is temporarily unavailable. The saved request will be checked again."
                          if unavailable else "Waiting for FlashData to deliver the requested range to R2.")


def defer(root, job, identity, remote, until):
    with store.connect(root,True) as db:
        db.execute("""INSERT INTO provider_calls(id,job,provider,reference,response_id,reservation_micros,next_try)
                      VALUES(?,?,'flashdata',?,?,0,?) ON CONFLICT(id) DO UPDATE SET next_try=excluded.next_try""",
                   (identity,job,REFERENCE,remote,until))


def done(root, identity):
    with store.connect(root,True) as db:
        db.execute("UPDATE provider_calls SET result=?,next_try=0 WHERE id=?",(json.dumps({"received":True}),identity))


def objects(client, bucket, prefix=""):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,Prefix=prefix):
        yield from page.get("Contents",[])


def delete_import(client, config, folder, state, prefix, suffix):
    key = state["key"]
    if (state.get("bucket")!=config["bucket"] or state.get("endpoint")!=config["endpoint"] or
            not re.fullmatch(re.escape(prefix)+r"/[A-Za-z0-9_.-]+"+re.escape(suffix),key)):
        raise transfer.TransferHold("Refused to delete an R2 object outside this request.")
    with CLEANUP_LOCK:
        if any(obj["Key"]==key for obj in objects(client,config["bucket"],prefix+"/")):
            client.delete_object(Bucket=config["bucket"],Key=key,IfMatch=state["etag"])
        if any(obj["Key"]==key for obj in objects(client,config["bucket"],prefix+"/")):
            raise transfer.TransferHold("R2 still reports the imported object.")
        state["deleted"] = True
        streamer_asr.write(folder/"request.json",state)
        (folder/"cleanup.json").unlink(missing_ok=True)


def retry_cleanup(root):
    pending = iter((root/"streamer-intake"/"flashdata").glob("run-*/*/cleanup.json"))
    if not (marker := next(pending,None)) or not (config := configuration(root)):
        return
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        folder = marker.parent
        run,digest = folder.parent.name,folder.name
        if not re.fullmatch(r"run-\d+",run) or not re.fullmatch(r"[0-9a-f]{64}",digest):
            return
        with attempt_lock(folder):
            state = streamer_asr.read(folder/"request.json")
            suffix = ".m4a" if state.get("purpose")=="audio" else ".mp4"
            target = folder/("received"+suffix)
            if target.is_file() and media.checksum(target)==state.get("checksum"):
                delete_import(r2(config),config,folder,state,f"{PREFIX}/{run}/{digest}",suffix)
    except (BotoCoreError,ClientError,KeyError,OSError,ValueError,transfer.TransferHold,ProviderPending):
        # Keep the receipt for a later pass.
        pass


def request_folder(root, job, video, purpose, left, right, rendition=None):
    a,b = math.floor(left),math.ceil(right)
    identity = f"{job}:{video}:{purpose}:{a}:{b}"
    if rendition:
        identity += ":" + rendition["format_id"]
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return root/"streamer-intake"/"flashdata"/f"run-{job}"/digest


@contextmanager
def attempt_lock(folder):
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/"owner.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"1")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProviderPending("Another worker is verifying this FlashData interval.") from None
        yield


def verify_delivery(partial, purpose, rendition, seconds):
    response = media.run([media.require("ffprobe"),"-v","error","-show_streams","-of","json",str(partial)],25)
    try:
        streams = json.loads(response.stdout)["streams"]
        kind = "audio" if purpose=="audio" else "video"
        stream, = [item for item in streams if item["codec_type"]==kind]
        frame_rate = stream.get("avg_frame_rate") or "0/1"
        numerator,denominator = map(int,frame_rate.split("/"))
        fps = numerator/denominator if denominator else 0
        expected_fps = profiles.number(rendition.get("fps"))
        if (response.returncode or abs(transfer.duration_ticks(partial,kind[0])-seconds)>.35 or
                kind=="video" and (stream["height"]!=profiles.height(rendition) or
                                  stream["codec_name"]!=profiles.codec(rendition["vcodec"]) or
                                  expected_fps and abs(fps-expected_fps)>1)):
            raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise DeliveryFailed("FlashData media did not match the selected rendition or clock range.") from None
    decoded = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin","-i",str(partial),
                         "-map",f"0:{kind[0]}:0","-f","null","-"],300)
    if decoded.returncode:
        raise DeliveryFailed("FlashData media failed the full decode check.")
    return {"height":stream.get("height"),"width":stream.get("width"),"codec":stream["codec_name"],"fps":fps,
            "duration":transfer.duration_ticks(partial,kind[0])}


def receive(root, config, job, video, purpose, left, right, ledger, *, completed=False, rendition=None):
    from botocore.exceptions import BotoCoreError, ClientError

    # The remote API accepts whole seconds. The worker trims the fractional edges locally.
    a,b = math.floor(left),math.ceil(right)
    if not 0<=a<b<=transfer.REPLAY_LIMIT:
        raise transfer.TransferHold(transfer.REPLAY_LIMIT_MESSAGE)
    folder = request_folder(root,job,video,purpose,left,right,rendition)
    identity = "flashdata-"+folder.name
    folder.mkdir(parents=True,exist_ok=True)
    file = folder/"request.json"
    state = streamer_asr.read(file) if file.is_file() else {}
    prefix = f"{PREFIX}/run-{job}/{folder.name}"
    suffix = ".m4a" if purpose=="audio" else ".mp4"
    target = folder/("received"+suffix)
    client = r2(config)
    bucket = config["bucket"]
    try:
        if not state:
            if completed:
                raise transfer.TransferHold("The saved FlashData request is missing.")
            used = sum(obj["Size"] for obj in objects(client,bucket))
            if used+MAX_OBJECT>=MAX_BUCKET:
                raise transfer.TransferBudget("FlashData held because the R2 bucket is close to its storage ceiling.")
            body = payload(config,video,purpose,a,b,prefix,rendition)
            if rendition:
                now = time.time()
                with store.connect(root,True) as db:
                    timeout = profiles.settings(db)["timeout"]
                    if not profiles.reserve(db,identity,video,purpose,MAX_OBJECT*float(config.get("credits_per_tb",2000))/1e12,now):
                        raise transfer.TransferHold("FlashData submission is unresolved. Check its provider receipt.")
            streamer_asr.write(file,{"submitting":True})
            try:
                receipt,_ = request(config,QUERIES,body)
            except SubmissionRejected:
                if rendition:
                    profiles.record(root,identity,"failed",credits=0)
                raise
            remote = receipt.get("id")
            if not isinstance(remote,str) or not re.fullmatch(r"job_[A-Za-z0-9-]{8,90}",remote):
                raise transfer.TransferHold("FlashData did not return a valid job receipt.")
            state = {"remote":remote,"bucket":bucket,"endpoint":config["endpoint"],"purpose":purpose,
                     "submitted_at":time.time()}
            if rendition:
                state.update(format=rendition,timeout=timeout,video=video,range=[a,b])
            streamer_asr.write(file,state)
            if rendition:
                profiles.record(root,identity,"pending")
                defer(root,job,identity,remote,0)
        if not state.get("remote"):
            raise transfer.TransferHold("FlashData submission is unresolved. Check the provider console before retrying.")
        if not state.get("checksum") and (not completed or rendition):
            state.setdefault("submitted_at",file.stat().st_mtime)
            try:
                status,hint = request(config,JOBS+state["remote"])
            except StatusUnavailable as error:
                pending(root,job,identity,file,state,error.retry_after,unavailable=True)
            job_status = status.get("job",status) if isinstance(status,dict) else None
            stage = job_status.get("status") if isinstance(job_status,dict) else None
            if rendition and stage:
                state["remote_status"] = stage
                if profiles.number(job_status.get("durationMs")):
                    state["provider_processing_ms"] = job_status["durationMs"]
                credits = job_status.get("credits")
                if stage in ("pending","processing","queued"):
                    credits = None
                if credits is not None:
                    state["credits"] = credits
                profiles.record(root,identity,stage,credits=credits)
            if not stage:
                pending(root,job,identity,file,state,hint,unavailable=True)
            if stage in ("pending","processing","queued"):
                pending(root,job,identity,file,state,hint)
            if stage!="completed":
                state["delivery_status"] = "failed"
                streamer_asr.write(file,state)
                if rendition and re.search(r"credential|balance|insufficient credits|private video|invalid timestamp",
                                           str(job_status.get("error","")),re.I):
                    raise transfer.TransferHold("FlashData reported a permanent source, credential or balance error.")
                unavailable = bool(re.search(r"requested format.*not available|format unavailable|format not available",
                                             str(job_status.get("error","")),re.I))
                raise DeliveryFailed("FlashData reported an unavailable format." if unavailable else
                                     "FlashData did not complete this media range. See its job console.",
                                     format_error=unavailable)
            if rendition and "result" not in state:
                try:
                    state["result"] = result(config,state["remote"],prefix)
                except StatusUnavailable as error:
                    if completed:
                        raise
                    pending(root,job,identity,file,state,error.retry_after,unavailable=True,result_wait=True)
                streamer_asr.write(file,state)
                profiles.record(root,identity,"delivered",credits=state.get("credits",state["result"]["credits"]),
                                size=state["result"]["downloaded_bytes"] or state["result"]["file_size"])
        if not state.get("checksum"):
            found = list(objects(client,bucket,prefix+"/"))
            if len(found)!=1 or not re.fullmatch(re.escape(prefix)+r"/[A-Za-z0-9_.-]+"+re.escape(suffix),found[0]["Key"]):
                raise transfer.TransferHold("FlashData delivery did not identify one owned media object.")
            obj = found[0]
            if not 0<obj["Size"]<=MAX_OBJECT:
                raise transfer.TransferBudget("FlashData returned a range outside the authorized storage size.")
            if rendition and (obj["Size"]!=state["result"]["file_size"] or obj["Key"]!=state["result"]["key"]):
                raise transfer.TransferHold("FlashData's result does not match the owned R2 object.")
            kind = "audio" if purpose=="audio" else "video"
            entry = ledger.begin(kind,{"format_id":rendition["format_id"] if rendition else "flashdata-"+purpose},a,b)
            entry["purpose"] = purpose
            partial = target.with_suffix(".partial"+suffix)
            if rendition:
                profiles.record(root,identity,"verifying")
            md5 = hashlib.md5(usedforsecurity=False)
            with (client.get_object(Bucket=bucket,Key=obj["Key"],IfMatch=obj["ETag"])["Body"] as response,
                  partial.open("wb") as output):
                remaining = obj["Size"]
                while remaining:
                    chunk = ledger.read(entry,min(remaining,media.CHUNK),response.read)
                    if not chunk:
                        raise transfer.TransferHold("The R2 transfer ended before the verified object size.")
                    output.write(chunk)
                    md5.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if rendition:
                expected = state["result"].get("md5")
                if expected and md5.hexdigest()!=expected.lower():
                    raise transfer.TransferHold("FlashData's checksum did not match the delivered bytes.")
                state["probe"] = verify_delivery(partial,purpose,rendition,b-a)
                state["md5"] = md5.hexdigest()
                state["verified_after"] = round(time.time()-state["submitted_at"],3)
            elif abs(transfer.duration_ticks(partial,"a" if purpose=="audio" else "v")-(b-a))>.35:
                raise transfer.TransferHold("FlashData returned media with an unexpected range duration.")
            partial.replace(target)
            state.update(checksum=media.checksum(target),key=obj["Key"],etag=obj["ETag"])
            streamer_asr.write(file,state)
            ledger.end(entry,"received")
        if not target.is_file() or media.checksum(target)!=state["checksum"]:
            raise transfer.TransferHold("The verified local copy of this FlashData range is missing or changed.")
        if rendition:
            report = state["result"]
            profiles.record(root,identity,"verified",credits=state.get("credits",report["credits"]),
                            size=report["downloaded_bytes"] or report["file_size"])
        if not state.get("deleted"):
            streamer_asr.write(folder/"cleanup.json",{"verified":True})
            delete_import(client,config,folder,state,prefix,suffix)
        done(root,identity)
        return target,a
    except (BotoCoreError,ClientError) as error:
        code = error.response.get("Error",{}).get("Code") if isinstance(error,ClientError) else ""
        if rendition and state.get("remote") and code not in ("AccessDenied","InvalidAccessKeyId","SignatureDoesNotMatch"):
            retries = state.get("storage_retries",0)+1
            state["storage_retries"] = retries
            streamer_asr.write(file,state)
            if retries<=3 and not completed:
                defer(root,job,identity,state["remote"],time.time()+10*2**(retries-1))
                raise ProviderPending("R2 transfer will retry with the saved FlashData request.") from None
        if state.get("checksum"):
            # A conditional cleanup failure cannot invalidate an already verified asset.
            done(root,identity)
            return target,a
        raise transfer.TransferHold("R2 could not verify or transfer this FlashData object.") from None


def materialize(root, config, job, video, purpose, left, right, ledger, *, rendition=None, completed=False):
    raw,origin = (receive(root,config,job,video,purpose,left,right,ledger,rendition=rendition,completed=completed) if rendition
                  else receive(root,config,job,video,purpose,left,right,ledger))
    audio = purpose=="audio"
    name = f"{purpose}-{left:.6f}-{right:.6f}"
    file = raw.with_name(name+".json")
    saved = streamer_asr.read(file) if file.is_file() else None
    if saved and (root/saved["path"]).is_file() and media.checksum(root/saved["path"])==saved["hash"]:
        return saved
    if not audio:
        _,height,*_ = inspect_media(raw)
        expected = profiles.height(rendition) if rendition else {"hd":1080,"hd720":720}.get(purpose)
        if not (height==expected if expected else height<=360):
            raise DeliveryFailed("FlashData returned a different picture resolution than requested.")
    else:
        height = None
    ext = ".flac" if audio else ".mp4"
    target = raw.with_name(name+ext)
    partial = target.with_suffix(".partial"+ext)
    codec = (["-map","0:a:0","-vn","-ar","48000","-ac","2","-c:a","flac"] if audio else
             ["-map","0:v:0","-an","-vf","fps=30","-c:v","libx264","-threads","2","-preset","veryfast"])
    result = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin","-y",
                        "-ss",f"{left-origin:.6f}","-i",str(raw),"-t",f"{right-left:.6f}",*codec,str(partial)],250)
    track = "a" if audio else "v"
    if result.returncode or abs(transfer.duration_ticks(partial,track)-(right-left))>(.035 if audio else .08):
        raise transfer.TransferHold("The FlashData range could not be fitted to the original source clock.")
    partial.replace(target)
    item = {"path":target.relative_to(root).as_posix(),"hash":media.checksum(target),"start":left,"end":right,
            "actual":right-left,"bytes":target.stat().st_size,"height":height,"rendition":"flashdata-"+purpose,
            "transfer":raw.stat().st_size}
    if rendition:
        item["rendition"] = rendition["format_id"]
        item["codec"] = profiles.codec(rendition.get("acodec" if audio else "vcodec"))
    streamer_asr.write(file,item)
    return item


def final_picture(root, config, job, video, left, right, ledger, update):
    folder = request_folder(root,job,video,"hd",left,right)
    choice = folder/"fallback.json"
    if not choice.is_file():
        try:
            return materialize(root,config,job,video,"hd",left,right,ledger)
        except (DeliveryFailed,DeliveryStalled) as error:
            state = streamer_asr.read(folder/"request.json")
            streamer_asr.write(choice,{"video":video,"range":[left,right],
                "check_after":time.time()+300 if isinstance(error,DeliveryStalled) and not state.get("deleted") else None})
            update("1080p delivery unavailable. Requesting the same cut at 720p.")
    return materialize(root,config,job,video,"hd720",left,right,ledger)


def acquire(root, config, job, video, purpose, left, right, ledger, update=lambda _: None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",video) or not (
            math.isfinite(left) and math.isfinite(right) and 0<=left<right<=transfer.REPLAY_LIMIT):
        raise transfer.TransferHold(transfer.REPLAY_LIMIT_MESSAGE)
    with store.connect(root) as db:
        policy = profiles.settings(db)
    if saved := profiles.reuse(root,video,purpose,left,right,policy):
        return saved
    folder = request_folder(root,job,video,purpose,left,right)
    with attempt_lock(folder):
        if saved := profiles.reuse(root,video,purpose,left,right,policy):
            return saved
        if (folder/"request.json").is_file():
            # Resume existing requests with their original selectors and identities.
            if purpose == "hd":
                item = final_picture(root,config,job,video,left,right,ledger,update)
            else:
                item = materialize(root,config,job,video,purpose,left,right,ledger)
            profiles.remember(root,video,purpose,item)
            return item
        file = folder/f"plan-{left:.6f}-{right:.6f}.json"
        if file.is_file():
            plan = streamer_asr.read(file)
        else:
            profile = profiles.source(root,config,video)
            if profile["live_status"]=="is_live" or right>profile["duration"]:
                raise transfer.TransferHold("The requested interval is outside the verified replay clock.")
            if profile.get("stale"):
                raise transfer.TransferHold("FlashData formats are stale. Refresh the Source Profile before acquiring new media.")
            plan = {"options":profiles.rank(profile,purpose,policy),"failed":[],"limit":policy["attempts"],
                    "video":video,"purpose":purpose,"range":[left,right],"refreshed":False}
            streamer_asr.write(file,plan)
        while len(plan["failed"])<plan["limit"]:
            rendition = next((item for item in plan["options"] if item["format_id"] not in plan["failed"]),None)
            if not rendition:
                break
            ident = rendition["format_id"]
            attempt = request_folder(root,job,video,purpose,left,right,rendition)
            receipt = attempt/"request.json"
            if not receipt.exists() and plan["failed"]:
                update(f'FlashData fallback attempt {len(plan["failed"])+1} using format {ident}.')
            with attempt_lock(attempt):
                try:
                    item = materialize(root,config,job,video,purpose,left,right,ledger,rendition=rendition)
                except (DeliveryFailed,DeliveryStalled) as error:
                    state = streamer_asr.read(receipt)
                    state["delivery_status"] = "stalled" if isinstance(error,DeliveryStalled) else "failed"
                    if isinstance(error,DeliveryStalled):
                        state.update(check_after=time.time()+300, requested=[left,right])
                    streamer_asr.write(receipt,state)
                    plan["failed"].append(ident)
                    if isinstance(error,DeliveryFailed) and error.format_error and not plan["refreshed"]:
                        plan["refreshed"] = True
                        streamer_asr.write(file,plan)
                        try:
                            latest = profiles.source(root,config,video,refresh=True)
                        except SubmissionRejected as refresh_error:
                            if refresh_error.code!=429:
                                raise
                        else:
                            if right>latest["duration"] or latest["live_status"]=="is_live":
                                raise transfer.TransferHold("The refreshed replay clock no longer covers this interval.")
                            plan["options"] = profiles.rank(latest,purpose,policy)
                    streamer_asr.write(file,plan)
                else:
                    item["attempt"] = len(plan["failed"])+1
                    profiles.remember(root,video,purpose,item)
                    return item
        raise DeliveryFailed("No acceptable FlashData rendition completed within the configured attempt limit.")


def reconcile_attempts(root):
    for file in (root/"streamer-intake"/"flashdata").glob("run-*/*/request.json"):
        state = streamer_asr.read(file)
        now = time.time()
        if not state.get("check_after") or state["check_after"]>now:
            continue
        with attempt_lock(file.parent):
            state = streamer_asr.read(file)
            if not state.get("check_after") or state["check_after"]>now:
                continue
            state["check_after"] = now+300
            streamer_asr.write(file,state)
            config = required(root)
            job = int(file.parent.parent.name.removeprefix("run-"))
            left,right = state["requested"]
            identity = "flashdata-"+file.parent.name
            try:
                reply,_ = request(config,JOBS+state["remote"])
                stage = reply.get("job",reply).get("status")
                if stage=="completed":
                    ledger = transfer.TransferLedger(root,file.parent/"late-transfer.json",
                                                    {"remote_mib":256,"reserve_mib":256})
                    item = materialize(root,config,job,state["video"],state["purpose"],left,right,ledger,
                                       rendition=state["format"],completed=True)
                    profiles.remember(root,state["video"],state["purpose"],item)
                elif stage=="failed":
                    profiles.record(root,identity,"failed",credits=reply.get("job",reply).get("credits"))
                else:
                    return
                state = streamer_asr.read(file)
                state.pop("check_after",None)
                streamer_asr.write(file,state)
            except (StatusUnavailable,transfer.TransferHold,ProviderPending):
                pass
        return


def reconcile_fallback(root):
    for choice in (root/"streamer-intake"/"flashdata").glob("run-*/*/fallback.json"):
        check = streamer_asr.read(choice)
        now = time.time()
        if check["check_after"] is None or check["check_after"]>now:
            continue
        folder = choice.parent
        job = int(folder.parent.name.removeprefix("run-"))
        left,right = check["range"]
        if request_folder(root,job,check["video"],"hd",left,right)!=folder:
            continue
        config = configuration(root)
        if not config:
            return
        check["check_after"] = now+300
        streamer_asr.write(choice,check)
        state = streamer_asr.read(folder/"request.json")
        try:
            if not state.get("deleted"):
                reply,_ = request(config,JOBS+state["remote"])
                stage = reply.get("job",reply).get("status")
                if stage in ("pending","processing","queued"):
                    return
                if stage=="completed":
                    budget = transfer.TransferLedger(root,folder/"late-transfer.json",{"remote_mib":256,"reserve_mib":256})
                    receive(root,config,job,check["video"],"hd",left,right,budget,completed=True)
                elif stage!="failed":
                    return
            check["check_after"] = None
            streamer_asr.write(choice,check)
        except (StatusUnavailable,transfer.TransferHold):
            pass  # Keep the exact receipt and try again on the next scheduled check.
        return
