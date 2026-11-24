"""Read-only dependency and host checks for the factory service."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
from pathlib import Path

from . import (flashdata, media, portal_settings, portal_templates, provider_vault, replay_cache,
               template_motion)
from .provider_connections import ConnectionRequired, Connections


def available_memory():
    try:
        with Path("/proc/meminfo").open(encoding="ascii") as stream:
            field = next(line for line in stream if line.startswith("MemAvailable:"))
        return int(field.split()[1])*1024
    except (OSError,StopIteration,ValueError,IndexError):
        return None


def admission_hold():
    memory = available_memory() if platform.system()=="Linux" else None
    return "Host memory pressure is holding new media work." if memory is not None and memory<2*1024**3 else ""


def automation(db, root, config):
    uploads, resources, hosted = (config[k] for k in ("uploads","resources","connections"))
    vault = provider_vault.vault_for(root)

    def has_key(reference):
        try:
            return bool(reference and vault.get(reference))
        except provider_vault.VaultError:
            return False

    processing = ("New work is paused" if resources["paused"] else
                  "Automatic scheduling is off" if not uploads["refill"] else
                  "Allow at least 60 minutes of processing" if resources["remote_minutes"]<60 else "")
    resources_hold = admission_hold()
    if not resources_hold and not all(media.tool(tool) for tool in ("ffmpeg","ffprobe")):
        resources_hold = "FFmpeg or FFprobe is missing"
    if not resources_hold and shutil.disk_usage(root).free<resources["reserve_mib"]*media.CHUNK:
        resources_hold = "Free space is below the reserve"
    acquisition = ""
    try:
        if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER")=="flashdata":
            flashdata.required(root)
        elif not replay_cache.enabled() and not media.downloader():
            acquisition = "YouTube replay acquisition is unavailable"
        if replay_cache.enabled() and not (os.environ.get("VIDEO_DOWNLOAD_RAPIDAPI_KEY") or has_key(replay_cache.REFERENCE)):
            acquisition = "Connect the RapidAPI replay key"
    except media.MediaError:
        acquisition = "Check FlashData and R2 setup"
    analysis = ""
    if not hosted["hosted"]:
        analysis = "Default hosted analysis is off"
    else:
        try:
            Connections(root,vault).key("sail",tier=hosted["sail_tier"],images=True)
        except (ConnectionRequired,provider_vault.VaultError):
            analysis = "Verify DeepSeek text and image access"
    connection = db.execute("SELECT reference,accounts FROM indiepost_connection").fetchone()
    accounts = json.loads(connection["accounts"])
    selected = {item["platform"] for item in accounts if item["id"] in uploads["accounts"]}
    missing = set(portal_settings.PLATFORMS)-selected
    if not uploads["enabled"] or not uploads["local_release"]:
        publishing = "Automatic publishing is off"
    elif not has_key(connection["reference"]) or not accounts:
        publishing = "Verify the IndiePost connection"
    elif missing:
        publishing = f"{len(selected)} of 4 destinations selected"
    elif any(not selected.issubset(portal_settings.policy(db,creator[0])["platforms"])
             for creator in db.execute("SELECT id FROM streamer_creators WHERE enabled=1")):
        publishing = "An enabled creator is missing destination permissions"
    else:
        publishing = ""

    def minutes(stamp):
        hour, minute = map(int,stamp.split(":"))
        return hour*60+minute

    window = (minutes(uploads["window_end"])-minutes(uploads["window_start"]))%1440 or 1440
    daily, gap = uploads["daily_stories"], uploads["spacing"]
    capacity = daily
    if gap:
        capacity = 1440//gap
        if uploads["release"]=="scheduled":
            capacity = min(capacity,1+(window-1)//gap)
    schedule = ("Publishing limits are below the daily reel target" if
                min(capacity,uploads["account_daily"],uploads["provider_daily"]//max(1,len(selected)))<daily else "")
    return [
        ("Auto processing",processing,"Buffer scheduler on",
         "/controls#resources" if resources["paused"] or resources["remote_minutes"]<60 else "/storage#scheduler"),
        ("Worker capacity",resources_hold,"Media tools and storage ready","/controls#resources"),
        ("Replay downloads",acquisition,"Replay acquisition configured","/controls#connections"),
        ("DeepSeek",analysis,"Text and storyboard access verified","/controls#connections"),
        ("Auto publishing",publishing,"All four accounts selected","/controls#uploads"),
        ("Publishing schedule",schedule,f"{daily} reels/day · {gap} min gap","/storage#scheduler"),
        ("Auto cleanup","" if uploads["reclaim"] else "Automatic cleanup is off",
         f"{uploads['grace_hours']//24} days after confirmed delivery","/storage#cleanup"),
    ]


def thread_limit(configured):
    if platform.system()!="Linux":
        return configured
    return max(1,min(configured,len(os.sched_getaffinity(0))))


def inspect(root):
    checks = {}
    checks["native_linux_arm64"] = platform.system()=="Linux" and platform.machine() in ("aarch64","arm64")
    checks["credential_store"] = provider_vault.vault_for(root).available
    checks["sqlite"] = bool(sqlite3.connect(":memory:").execute("SELECT json_valid('{}')").fetchone()[0])
    if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
        try:
            checks["flashdata_r2_configuration"] = bool(flashdata.required(root))
        except media.MediaError:
            checks["flashdata_r2_configuration"] = False
    else:
        node = media.tool("node")
        version = media.run([node,"--version"],5) if node else None
        major = version.stdout.strip().removeprefix(b"v").split(b".",1)[0] if version and version.returncode==0 else b""
        checks["node_and_yt_dlp"] = bool(major.isdigit() and int(major)>=22 and media.downloader())
        try:
            checks["yt_dlp_ejs"] = bool(importlib.metadata.version("yt-dlp-ejs"))
        except importlib.metadata.PackageNotFoundError:
            checks["yt_dlp_ejs"] = False
    if replay_cache.enabled():
        try:
            checks["rapidapi_replay_key"] = bool(provider_vault.vault_for(root).get(replay_cache.REFERENCE))
        except provider_vault.VaultError:
            checks["rapidapi_replay_key"] = False
    for name, flag, required in (
        ("ffmpeg_h264", "-encoders", ("libx264","aac")),
        ("ffmpeg_subtitles", "-filters", ("subtitles",)),
        ("ffprobe", "-version", ("ffprobe version",)),
    ):
        binary = media.tool("ffprobe" if name=="ffprobe" else "ffmpeg")
        result = media.run([binary,"-hide_banner",flag],15) if binary else None
        checks[name] = bool(result and result.returncode==0 and all(
            token in result.stdout.decode(errors="replace") for token in required))
    try:
        import ctranslate2
        import faster_whisper
        checks["cpu_int8_asr"] = bool(faster_whisper.WhisperModel and
                                      "int8" in ctranslate2.get_supported_compute_types("cpu"))
    except (ImportError,RuntimeError):
        checks["cpu_int8_asr"] = False
    try:
        template_motion.library()
        for original in portal_templates.ORIGINALS:
            template_motion.pin({"template":original,"revision":1})
        checks["pinned_fonts_raqm"] = True
    except (ValueError,KeyError,media.MediaError):
        checks["pinned_fonts_raqm"] = False
    return {"machine":platform.machine(),"os":platform.system(),"cpus":os.cpu_count(),
            "memory_available_bytes":available_memory(),"data_free_bytes":shutil.disk_usage(root).free,
            "checks":checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data",type=Path,required=True)
    args = parser.parse_args()
    result = inspect(args.data.resolve())
    print(json.dumps(result,indent=2))
    return 0 if all(result["checks"].values()) else 1


if __name__=="__main__":
    raise SystemExit(main())
