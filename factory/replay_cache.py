"""One marketplace acquisition per source and local replay lifecycle."""

import http.client
import json
import math
import os
import re
import shutil
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request
from pathlib import Path

from . import flashdata, media, provider_vault, store, streamer_asr, streamer_transfer as transfer
from . import video_download_api as vda
from .provider_meter import ProviderPending

REFERENCE = "video-download-rapidapi"
MAX_DURATION = 24 * 3600
MAX_BYTES = 56 * 1024**3
HOUR = 3600


def enabled():
    return os.environ.get("GTA_FACTORY_FULL_REPLAY") == "1"


def directory(root, source, revision):
    return root/"replay-cache"/f"source-{int(source)}-revision-{int(revision)}"


def state(root, source, revision):
    file = directory(root,source,revision)/"replay.json"
    try:
        return streamer_asr.read(file)
    except FileNotFoundError:
        return {}


def hours(duration):
    return [(start,min(start+HOUR,duration)) for start in range(0,math.ceil(duration),HOUR)]


def expires(item):
    downloaded = item["downloaded_at"]
    finished = item.get("completed_at")
    deadline = min(downloaded+24*HOUR,finished+12*HOUR) if finished else downloaded+24*HOUR
    return max(deadline,item.get("retain_until",0))


def extend(root, source, revision, hours_to_add):
    if hours_to_add not in (12,24):
        raise ValueError("Choose 12 or 24 more hours.")
    folder = directory(root,source,revision)
    try:
        with flashdata.attempt_lock(folder):
            item = state(root,source,revision)
            if item.get("state")!="verified" or time.time()>=expires(item):
                raise ValueError("The recording must be cached and within its retention period.")
            put(folder/"replay.json",item,retain_until=expires(item)+hours_to_add*HOUR)
    except ProviderPending:
        raise ValueError("The cache is busy. Try again in a moment.") from None


def set_paused(root, db, source, paused):
    row = db.execute("SELECT revision,state FROM sources WHERE id=?",(source,)).fetchone()
    if not row or row["state"]=="archived":
        raise ValueError("Source is unavailable.")
    if paused:
        store.set_source_state(db,source,"paused")
        return
    item = state(root,source,row["revision"])
    if item.get("state") in ("expiring","expired") or item.get("downloaded_at") and time.time()>=expires(item):
        raise ValueError("The recording reached its deletion deadline.")
    if row["state"]!="paused":
        return
    store.set_source_state(db,source,"queued")
    latest = db.execute("""SELECT id,state FROM jobs WHERE source=? AND revision=?
                           AND json_extract(settings,'$.method')='streamer' ORDER BY id DESC LIMIT 1""",
                        (source,row["revision"])).fetchone()
    if latest:
        if latest["state"]=="running":
            db.execute("UPDATE jobs SET stop=0 WHERE id=?",(latest["id"],))
        elif latest["state"]=="stopped":
            store.resume(db,latest["id"])
        elif latest["state"]=="complete":
            on_completed(root,db,latest["id"])


def guard(root, row):
    item = state(root,row["source"],row["revision"])
    if item.get("state") in ("expiring","expired") or item.get("downloaded_at") and time.time()>=expires(item):
        raise transfer.TransferHold("This recording reached its retention deadline.")


def put(file, item, **changes):
    item.update(changes)
    streamer_asr.write(file,item)


def cache_client(root, cache):
    secret = provider_vault.vault_for(root)
    key = os.environ.get("VIDEO_DOWNLOAD_RAPIDAPI_KEY") or (secret.get(REFERENCE) if secret.available else None)
    if not key:
        raise transfer.TransferHold("Connect the RapidAPI replay key before starting this source.")
    return vda.RapidClient(key.strip(),vda.RAPID_HOST,cache/"gateway.json",160)


def submit(root, row, config, cache, client, item, file):
    video = config["stream"]["platform_id"]
    with store.connect(root) as db:
        saved = db.execute("""SELECT m.duration,st.live_status FROM streamer_streams st
          JOIN portal_source_metadata m ON m.source=st.source WHERE st.source=? AND st.platform_id=?""",
          (row["source"],video)).fetchone()
    if not saved or saved["live_status"] not in ("was_live","archive_upload") or not saved["duration"]:
        raise transfer.TransferHold("Wait for a completed YouTube replay before downloading its full recording.")
    answer = client.read(client.api_url("/ajax/api.php",{"function":"i","u":vda.source_url(video)}))
    duration = vda.duration_seconds(answer.get("duration"))
    if answer.get("id")!=video or not duration or abs(duration-saved["duration"])>3 or duration>MAX_DURATION:
        raise transfer.TransferHold("The replay clock could not be verified for a full download up to 24 hours.")
    multiplier = ((answer.get("download_cost_estimates") or {}).get("1080") or {}).get("multiplier")
    quota = client.rows[-1]["quota"].get("x-ratelimit-units-remaining")
    if type(multiplier) is not int or multiplier<1 or quota is None or quota<max(50,4*multiplier):
        raise transfer.TransferHold("RapidAPI's available unit balance cannot cover this replay estimate.")
    if shutil.disk_usage(cache).free < config.get("reserve_mib",2048)*media.CHUNK+media.CHUNK:
        raise transfer.TransferHold("The workspace has insufficient free space for replay acquisition.")
    fields = {"url":vda.source_url(video),"format":"1080","allow_extended_duration":1,
              "max_duration":math.ceil(duration/60),"no_merge":0}
    put(file,item,state="submitting",video=video,duration=duration,units_before=quota,started=time.time())
    try:
        identity, route = client.submit(fields,clipping=False)
    except vda.Rejected as error:
        put(file,item,state="rejected",message=str(error))
        raise transfer.TransferHold(str(error)) from None
    put(file,item,state="pending",remote=identity,route=client.resolve_progress(route,identity))


def poll(root, row, client, file, item):
    identity = f"full-replay-{row['source']}-{row['revision']}"
    p, link, error = client.progress(item["remote"],item["route"])
    put(file,item,provider_progress=p)
    if error:
        put(file,item,state="failed",message=error)
        raise transfer.TransferHold(f"RapidAPI could not prepare the full recording ({error}).")
    if not link:
        delay = min(120,15*2**min(item.get("polls",0),3))
        put(file,item,state="pending",polls=item.get("polls",0)+1)
        with store.connect(root,True) as db:
            db.execute("""INSERT INTO provider_calls(id,job,provider,reference,response_id,reservation_micros,next_try)
                          VALUES(?,?,'rapidapi',?,?,0,?) ON CONFLICT(id) DO UPDATE SET
                          job=excluded.job,next_try=excluded.next_try""",
                       (identity,row["id"],REFERENCE,item["remote"],time.time()+delay))
        raise ProviderPending("Waiting for RapidAPI to prepare the full recording.")
    flashdata.done(root,identity)
    return link


def transfer_file(client, link, cache, file, item, reserve, checkpoint):
    host = urlsplit(link).netloc
    if host!="video-download-api.com" and not re.fullmatch(r"[a-z][a-z0-9-]{0,61}\.savenow\.to",host):
        raise transfer.TransferHold("The replay delivery host is outside the provider's domain.")
    vda.safe_https(link,(host,))
    partial = cache/"recording.partial.mp4"
    current = partial.stat().st_size if partial.is_file() else 0
    expected = item.get("expected_bytes")
    if current and current==expected:
        return partial
    headers = {"Accept-Encoding":"identity"}
    if current and item.get("etag"):
        headers.update({"Range":f"bytes={current}-","If-Range":item["etag"]})
    else:
        current = 0
    try:
        with client.opener.open(Request(link,headers=headers),timeout=60) as reply:
            if reply.status==206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",reply.headers.get("Content-Range",""))
                if not current or not match or int(match[1])!=current or int(match[3])!=expected or (
                        reply.headers.get("ETag")!=item.get("etag")):
                    raise transfer.TransferHold("The provider returned a different byte range on resume.")
            elif reply.status==200:
                current = 0
                length = reply.headers.get("Content-Length","")
                expected = int(length) if length.isdigit() else None
            else:
                raise transfer.TransferHold("Unexpected full-recording HTTP response.")
            free = shutil.disk_usage(cache).free
            if expected is not None and (not 0<expected<=MAX_BYTES or expected-current>free-reserve):
                raise transfer.TransferHold("The full recording exceeds the cache size or free-space limit.")
            tag = reply.headers.get("ETag")
            put(file,item,state="transfer",received_bytes=current,expected_bytes=expected,
                etag=tag if tag and not tag.startswith("W/") else None)
            last = time.monotonic()
            with partial.open("ab" if current else "wb") as out:
                while block := reply.read(media.CHUNK):
                    current += len(block)
                    if current>MAX_BYTES or shutil.disk_usage(cache).free<=reserve+len(block):
                        put(file,item,state="held",received_bytes=out.tell())
                        raise transfer.TransferHold("The replay reached the cache size or free-space limit.")
                    out.write(block)
                    if time.monotonic()-last>=3:
                        out.flush()
                        put(file,item,received_bytes=current)
                        checkpoint()
                        last = time.monotonic()
                out.flush()
                os.fsync(out.fileno())
    except (OSError,URLError,http.client.IncompleteRead):
        put(file,item,received_bytes=partial.stat().st_size if partial.exists() else 0)
        raise transfer.TransferHold("The media transfer was interrupted. Resume the saved RapidAPI request.") from None
    if current<=0 or expected is not None and current!=expected:
        raise transfer.TransferHold("The delivery ended before the advertised number of bytes.")
    put(file,item,state="verifying",received_bytes=current,transfer_completed_at=time.time())
    return partial


def verified(root, cache, partial, item):
    from .streamer_render import inspect_media

    info = media.probe(partial)
    _,height,*_ = inspect_media(partial)
    if height!=1080 or not info["audio"] or abs(info["duration"]-item["duration"])>3:
        raise transfer.TransferHold("The downloaded recording did not match the full replay duration or audio.")
    for at in (0,info["duration"]/2,max(0,info["duration"]-5)):
        check = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin","-protocol_whitelist","file",
                           "-ss",str(at),"-i",str(partial),"-t","4","-map","0:v:0","-map","0:a:0","-f","null","-"],60)
        if check.returncode:
            raise transfer.TransferHold("The replay did not decode near its start, middle or end.")
    return dict(info,hash=media.checksum(partial),path=(cache/"recording.mp4").relative_to(root).as_posix())


def acquire(root, row, config, snapshot, update=None):
    note = update or (lambda message: None)
    logged_at = 0

    def transfer_checkpoint():
        nonlocal logged_at
        with store.connect(root) as db:
            stop = db.execute("SELECT stop FROM jobs WHERE id=?",(row["id"],)).fetchone()
        if stop and stop[0]:
            raise transfer.TransferHold("The full recording was paused at a saved transfer checkpoint.")
        if time.monotonic()-logged_at>=30:
            total = f" / {item['expected_bytes']/1e9:.2f} GB" if item.get("expected_bytes") else ""
            note(f"RapidAPI replay transfer: {item['received_bytes']/1e9:.2f} GB{total} written to disk")
            logged_at = time.monotonic()

    link = snapshot.get("link")
    local = snapshot.get("location") or (link if link and Path(link).is_absolute() else None)
    if local:
        path = Path(local).resolve()
        if not path.is_file():
            raise transfer.TransferHold("The attached local recording is unavailable.")
        note("Using the attached local recording")
        return dict(media.probe(path),path=str(path),hash=media.input_fingerprint(snapshot))
    cache = directory(root,row["source"],row["revision"])
    file, target = cache/"replay.json",cache/"recording.mp4"
    with flashdata.attempt_lock(cache):
        item = state(root,row["source"],row["revision"])
        guard(root,row)
        if item.get("state") in ("verified","verifying") and item.get("asset") and target.is_file():
            if target.stat().st_size!=item["asset"]["bytes"]:
                raise transfer.TransferHold("The retained full recording changed after verification.")
            if item["state"]=="verifying":
                put(file,item,state="verified")
            note("Using the verified recording already on disk")
            return item["asset"]
        if item.get("state") in ("rejected","failed"):
            raise transfer.TransferHold(f"Full download held ({item.get('message','provider failure')}).")
        if item and not item.get("remote"):
            raise transfer.TransferHold("The prior full download submission was ambiguous. Reconcile its receipt before trying again.")
        partial = cache/"recording.partial.mp4"
        if not (item.get("state")=="verifying" and partial.is_file()
                and item.get("received_bytes")==partial.stat().st_size):
            try:
                client = cache_client(root,cache)
                if not item:
                    note("Submitting one full-replay download to RapidAPI")
                    submit(root,row,config,cache,client,item,file)
                note("Checking the saved RapidAPI download request")
                link = poll(root,row,client,file,item)
                partial = transfer_file(client,link,cache,file,item,config.get("reserve_mib",2048)*media.CHUNK,
                                        transfer_checkpoint)
            except vda.EvaluationError as error:
                raise transfer.TransferHold(str(error)) from None
        note("Verifying the downloaded file with FFprobe and local decode")
        asset = verified(root,cache,partial,item)
        put(file,item,asset=asset,downloaded_at=item.get("transfer_completed_at",time.time()),expected_bytes=asset["bytes"])
        partial.replace(target)
        put(file,item,state="verified")
        return asset


def local_range(root, row, source, left, right, purpose, update=None):
    guard(root,row)
    audio = purpose=="audio"
    target_dir = directory(root,row["source"],row["revision"])/"working"/f"run-{row['id']}"
    target_dir.mkdir(parents=True,exist_ok=True)
    target = target_dir/f"{purpose}-{left:.6f}-{right:.6f}{'.flac' if audio else '.mp4'}"
    journal = target.with_suffix(".json")
    if journal.is_file() and target.is_file():
        past = streamer_asr.read(journal)
        if media.checksum(target)==past["hash"]:
            if update:
                update(f"Reused local audio ({left:.1f}s–{right:.1f}s)")
            return past
    recording = (root/source["path"]).resolve()
    if not recording.is_file():
        raise transfer.TransferHold("The local recording is unavailable for extraction.")
    if update:
        update(f"FFmpeg extracting original audio from the local replay ({left:.1f}s–{right:.1f}s)")
    partial = target.with_suffix(".partial"+target.suffix)
    settings = (["-map","0:a:0","-vn","-ar","48000","-ac","2","-c:a","flac"] if audio else
                ["-map","0:v:0","-map","0:a:0","-vf",
                 "scale=-2:360" if purpose=="preview" else "null",
                 "-c:v","libx264","-preset","veryfast","-threads","2","-c:a","aac","-movflags","+faststart"])
    result = media.run([media.require("ffmpeg"),"-v","error","-xerror","-nostdin","-y","-ss",f"{left:.6f}",
                       "-protocol_whitelist","file","-i",str(recording),"-t",f"{right-left:.6f}",*settings,str(partial)],
                       1200 if audio else max(300,math.ceil((right-left)*4)))
    if result.returncode or abs(transfer.duration_ticks(partial,"a" if audio else "v")-(right-left))>.18:
        raise transfer.TransferHold("The local recording interval could not be aligned with the source clock.")
    partial.replace(target)
    from .streamer_render import inspect_media
    height = None if audio else inspect_media(target)[1]
    item = {"path":target.relative_to(root).as_posix(),"hash":media.checksum(target),"start":left,"end":right,
            "actual":right-left,"bytes":target.stat().st_size,"height":height,
            "transfer":0,"rendition":"local-cache"}
    streamer_asr.write(journal,item)
    return item


def is_local_range(root, job, path):
    if not path.is_relative_to(root.resolve()):
        return False
    parts = path.relative_to(root.resolve()).parts
    return (len(parts)==5 and parts[0]=="replay-cache"
            and re.fullmatch(r"source-\d+-revision-\d+",parts[1]) is not None
            and parts[2:4]==("working",f"run-{int(job)}")
            and re.fullmatch(r"(audio|preview|hd)-\d+\.\d+-\d+\.\d+(?:-source)?\.(flac|mp4)",parts[4]) is not None)


def on_completed(root, db, job):
    row = db.execute("""SELECT j.*,s.state AS source_state,s.revision AS current_revision,
                       m.duration FROM jobs j JOIN sources s ON s.id=j.source
                       LEFT JOIN portal_source_metadata m ON m.source=j.source WHERE j.id=?""",(job,)).fetchone()
    config = json.loads(row["settings"])
    if (not config.get("full_replay") or row["state"]!="complete" or row["source_state"]!="queued" or
            row["revision"]!=row["current_revision"] or not row["duration"]):
        return
    part = config.get("coverage_start",0)+HOUR
    cache = directory(root,row["source"],row["revision"])
    item = state(root,row["source"],row["revision"])
    if part>=row["duration"]:
        if item.get("state")=="verified" and not item.get("completed_at") and all_hours_finished(root,db,row):
            put(cache/"replay.json",item,completed_at=time.time())
        return
    if item.get("downloaded_at") and time.time()>=expires(item):
        return
    if db.execute("""SELECT 1 FROM jobs WHERE source=? AND revision=?
                     AND json_extract(settings,'$.directory_part')=?""",(row["source"],row["revision"],part)).fetchone():
        return
    next_config = dict(config,coverage_start=part,coverage_minutes=60,directory_part=part)
    store.enqueue(db,row["source"],fresh=True,overrides=next_config)


def all_hours_finished(root, db, row):
    runs = db.execute("""SELECT id,state,json_extract(settings,'$.directory_part') AS part FROM jobs
          WHERE source=? AND revision=? AND json_extract(settings,'$.full_replay')=1 ORDER BY id""",
          (row["source"],row["revision"])).fetchall()
    latest = {run["part"]:run for run in runs}
    for start,_ in hours(row["duration"]):
        run = latest.get(start)
        if not run or run["state"]!="complete":
            return False
        try:
            result = streamer_asr.read(root/"streamer-runs"/str(run["id"])/"production.json")
        except FileNotFoundError:
            return False
        if result.get("partial") or any(item.get("state")=="held" for item in result.get("items",[])):
            return False
    return True


def retire(root):
    for folder in (root/"replay-cache").glob("source-*-revision-*"):
        match = re.fullmatch(r"source-(\d+)-revision-(\d+)",folder.name)
        if not match or folder.is_symlink():
            continue
        source,revision = map(int,match.groups())
        item = state(root,source,revision)
        if item.get("downloaded_at") and item.get("state") in ("verified","expiring") and time.time()>=expires(item):
            with flashdata.attempt_lock(folder), store.connect(root,True) as db:
                item = state(root,source,revision)
                if item.get("state") not in ("verified","expiring") or time.time()<expires(item):
                    continue
                active = db.execute("""UPDATE jobs SET stop=1
                     WHERE source=? AND revision=? AND state='running'
                       AND json_extract(settings,'$.full_replay')=1""",(source,revision)).rowcount
                if active:
                    continue
                path = folder/"recording.mp4"
                if (path.is_symlink() or folder.resolve()!=folder.absolute() or
                        item["asset"]["path"]!=path.relative_to(root).as_posix()):
                    raise transfer.TransferHold("The replay retention journal does not identify an owned recording.")
                put(folder/"replay.json",item,state="expiring")
                path.unlink(missing_ok=True)
                put(folder/"replay.json",item,state="expired",expired_at=time.time())
                db.execute("""UPDATE jobs SET state='stopped',stop=0,progress='The local recording reached its retention deadline.'
                            WHERE source=? AND revision=? AND state='queued'
                              AND json_extract(settings,'$.full_replay')=1""",(source,revision))


def reconcile(root):
    retire(root)
    with store.connect(root,True) as db:
        for row in db.execute("""SELECT j.id FROM jobs j WHERE j.state='complete'
            AND json_extract(j.settings,'$.full_replay')=1
            AND j.id=(SELECT MAX(p.id) FROM jobs p WHERE p.source=j.source AND p.revision=j.revision)""").fetchall():
            on_completed(root,db,row["id"])


def progress(root, db, source, revision):
    item = state(root,source,revision)
    if not item:
        local = db.execute("SELECT location FROM sources WHERE id=? AND revision=?",(source,revision)).fetchone()
        if local and local[0] and (path:=Path(local[0])).is_file():
            item = {"state":"verified","received_bytes":path.stat().st_size,"expected_bytes":path.stat().st_size}
    covered = db.execute("""SELECT MAX(e.covered_seconds),MAX(e.total_seconds)
        FROM factory_events e JOIN jobs j ON j.id=e.job WHERE j.source=? AND j.revision=?
        AND json_extract(j.settings,'$.full_replay')=1 AND e.stage='Transcribe'""",(source,revision)).fetchone()
    metadata = db.execute("SELECT duration FROM portal_source_metadata WHERE source=?",(source,)).fetchone()
    return {"state":item.get("state","queued"),"bytes":item.get("received_bytes",0),
            "expected":item.get("expected_bytes"),"provider":item.get("provider_progress"),
            "transcribed":covered[0] or 0,"duration":covered[1] or item.get("duration") or (metadata and metadata[0]),
            "expires":expires(item) if item.get("downloaded_at") else None,"server_time":time.time()}
