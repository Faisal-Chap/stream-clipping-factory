import json
import os

from .media import downloader, tool
from .semantic import availability
from .store import DEFAULTS
from .provider_api import validate_tier

METHODS = {
    "baseline": "Scene and audio baseline",
    "semantic": "Local CLIP + scene and audio",
    "context": "Local context experiment",
    "judge": "Sail context judge",
    "embedding_judge": "Google retrieval + Sail context judge",
}


def get(db):
    row = db.execute("SELECT * FROM settings ORDER BY version DESC LIMIT 1").fetchone()
    data = dict(DEFAULTS, **json.loads(row["data"]))
    return dict(data, version=row["version"], changed=row["changed"], actor=row["actor"])


def save(db, fields):
    current = get(db)
    if int(fields["version"]) != current["version"]:
        raise ValueError("Settings changed in another tab. Reload and try again.")
    method, limit = fields["method"], int(fields["max_previews"])
    minutes = int(fields.get("remote_minutes", current["remote_minutes"]))
    height = int(fields.get("remote_height", current["remote_height"]))
    mib = int(fields.get("remote_mib", current["remote_mib"]))
    if method not in METHODS or not 1 <= limit <= 10:
        raise ValueError("Choose a ranker and a preview limit between 1 and 10.")
    if method == "semantic" and not availability():
        raise ValueError("Install the optional local CLIP model before selecting it.")
    if not 1 <= minutes <= 180 or not 10 <= mib <= 2048 or height not in (144, 240, 360, 480, 720):
        raise ValueError("Choose a supported resolution, 1–180 minutes and 10–2048 MiB.")
    tier = validate_tier(fields.get("sail_tier", current["sail_tier"]))
    cap = int(fields.get("provider_cap_cents", current["provider_cap_cents"]))
    if not 1 <= cap <= 1000:
        raise ValueError("Use a provider cap between 1 and 1,000 US cents per job.")
    hosted = fields.get("hosted") == "on"
    if method in ("judge", "embedding_judge") and not hosted:
        raise ValueError("Enable hosted analysis to select a hosted ranker.")
    config = dict(method=method, max_previews=limit, remote_minutes=minutes, remote_height=height,
                  remote_mib=mib, hosted=hosted, sail_tier=tier, provider_cap_cents=cap)
    db.execute("INSERT INTO settings(version,data,actor) VALUES(?,?,'local operator')",
               (current["version"] + 1, json.dumps(config)))


def checks():
    checks = [
        ("FFmpeg and ffprobe", bool(tool("ffmpeg") and tool("ffprobe")), "Install both executables on PATH."),
        ("Local CLIP", availability(), "Optional CPU model. See the setup instructions in README."),
    ]
    if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") != "flashdata":
        checks.append(("YouTube resolver",bool(downloader() and tool("node")),"Install yt-dlp with EJS and Node.js."))
    return checks
