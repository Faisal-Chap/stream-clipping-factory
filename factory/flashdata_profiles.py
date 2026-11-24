"""Persistent FlashData source inventory, rendition ranking and acquisition accounting."""

import json
import math
import re
import time
import uuid
from datetime import datetime, timezone

from . import media, store, streamer_transfer as transfer

DEFAULTS = {"preferred": 1080, "minimum": 480, "timeout": 300, "attempts": 3,
            "budget_cents": 300, "cache_hours": 24}
LIMITS = {"preferred": (480, 2160), "minimum": (480, 1080), "timeout": (60, 1800),
          "attempts": (1, 3), "budget_cents": (10, 10000), "cache_hours": (1, 168)}
CREDIT_USD = .05
FORMAT_FIELDS = ("format_id", "resolution", "width", "height", "vcodec", "acodec", "fps", "ext",
                 "tbr", "abr", "filesize", "filesize_approx", "language", "format_note", "protocol",
                 "audio_channels", "asr", "dynamic_range")


def settings(db):
    row = db.execute("SELECT data FROM portal_settings WHERE section='flashdata' ORDER BY id DESC LIMIT 1").fetchone()
    return DEFAULTS | (json.loads(row[0]) if row else {})


def save(db, fields):
    values = {key: int(fields[key]) for key in DEFAULTS}
    if (any(not low <= values[key] <= high for key, (low, high) in LIMITS.items())
            or values["minimum"] > values["preferred"]):
        raise ValueError("Use the displayed FlashData ranges and a minimum at or below the preferred quality.")
    db.execute("INSERT INTO portal_settings(section,data) VALUES('flashdata',?)", (json.dumps(values),))


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


def codec(value):
    name = str(value or "").lower()
    for prefix, label in (("avc", "h264"), ("vp09", "vp9"), ("av01", "av1"),
                          ("mp4a", "aac"), ("hvc", "hevc"), ("hev", "hevc")):
        if name.startswith(prefix):
            return label
    return name


def formats(rows):
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", str(row.get("format_id", ""))):
            continue
        item = {key: row[key] for key in FORMAT_FIELDS if key in row and
                (row[key] is None or type(row[key]) in (str, int, float, bool))}
        item["format_id"] = str(item["format_id"])
        yield item


def height(item):
    match = re.fullmatch(r"(?:\d+x(\d+)|(\d+)p)", str(item.get("resolution")))
    return int(number(item.get("height")) or (int(match[1] or match[2]) if match else 0))


def rank(profile, purpose, policy):
    if purpose == "audio":
        options = [item for item in profile["formats"]
                   if codec(item.get("acodec")) in ("aac", "opus", "vorbis", "mp3")
                   and item.get("vcodec") == "none"]
        return sorted(options, key=lambda item: (
            item.get("ext") == "m4a" and codec(item.get("acodec")) == "aac",
            not item.get("language") or "original" in str(item.get("format_note", "")).lower(),
            number(item.get("abr")) or number(item.get("tbr"))), reverse=True)
    low, high = (1, 360) if purpose == "preview" else (policy["minimum"], policy["preferred"])
    options = [item for item in profile["formats"] if low <= height(item) <= high
               and codec(item.get("vcodec")) in ("h264", "vp9", "av1", "hevc")
               and item.get("acodec") == "none"]
    def score(item):
        return (-height(item), ("h264", "vp9", "av1", "hevc").index(codec(item["vcodec"])),
                -min(number(item.get("fps")), 60), not bool(number(item.get("filesize"))),
                item.get("ext") != "mp4", item["format_id"])
    ordered = sorted(options, key=score)
    # Keep a codec alternative and a lower resolution reachable under the submission cap.
    first = list({(height(item), codec(item["vcodec"])): item for item in reversed(ordered)}.values())
    first.sort(key=score)
    if purpose != "preview" and len(first) > 3:
        lower = next((item for item in first[2:] if height(item) < height(first[0])), None)
        if lower:
            first.remove(lower)
            first.insert(2, lower)
    return first + [item for item in ordered if item not in first]


def usage(db, now):
    month = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.execute("""SELECT COALESCE(SUM(credits),0) AS credits,
                         COALESCE(SUM(CASE WHEN credits IS NULL THEN reserved ELSE 0 END),0) AS held,
                         COALESCE(SUM(bytes),0) AS bytes,
                         COALESCE(SUM(state IN ('submitting','queued','pending','processing','stalled')
                                      AND kind!='metadata'),0) AS outstanding
                         FROM flashdata_usage WHERE submitted>=? OR credits IS NULL""", (month.timestamp(),)).fetchone()


def reserve(db, identity, video, kind, estimate, now):
    if db.execute("SELECT 1 FROM flashdata_usage WHERE id=?", (identity,)).fetchone():
        return False
    tally = usage(db, now)
    if (tally["credits"] + tally["held"] + estimate) * CREDIT_USD * 100 > settings(db)["budget_cents"]:
        raise transfer.TransferBudget("FlashData monthly acquisition budget would be exceeded.")
    db.execute("""INSERT INTO flashdata_usage(id,video,kind,submitted,reserved,state)
                  VALUES(?,?,?,?,?,'submitting')""", (identity, video, kind, now, estimate))
    return True


def record(root, identity, state, *, credits=None, size=0):
    charge = number(credits) if type(credits) in (int, float) and math.isfinite(credits) and credits >= 0 else None
    with store.connect(root, True) as db:
        db.execute("""UPDATE flashdata_usage SET state=?,credits=COALESCE(?,credits),bytes=MAX(bytes,?)
                      WHERE id=?""", (state, charge, number(size), identity))


def source(root, config, video, *, refresh=False):
    from . import flashdata
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise transfer.TransferHold("Invalid YouTube source ID.")
    now = time.time()
    identity = "flashdata-metadata-" + uuid.uuid4().hex
    with store.connect(root, True) as db:
        policy = settings(db)
        saved = db.execute("SELECT * FROM flashdata_profiles WHERE video=?", (video,)).fetchone()
        profile = json.loads(saved["profile"]) if saved and saved["profile"] else None
        ttl = min(120, policy["cache_hours"] * 3600) if profile and profile["live_status"] == "is_live" else policy["cache_hours"] * 3600
        if profile and not refresh and not saved["error"] and now - saved["fetched"] < ttl:
            return profile
        if saved and saved["refresh_until"] > now:
            if profile and not refresh:
                return dict(profile, stale=True)
            raise transfer.TransferHold("FlashData metadata refresh is already pending or backing off.")
        reserve(db, identity, video, "metadata", float(config.get("metadata_credit_ceiling", .02)), now)
        db.execute("""INSERT INTO flashdata_profiles(video,refresh_until) VALUES(?,?)
                      ON CONFLICT(video) DO UPDATE SET refresh_until=excluded.refresh_until""", (video, now+90))
    try:
        profile = flashdata.metadata(config, video)
    except (OSError, ValueError, media.MediaError) as error:
        record(root, identity, "failed",credits=0 if isinstance(error,flashdata.SubmissionRejected) else None)
        with store.connect(root, True) as db:
            db.execute("""UPDATE flashdata_profiles SET error=?,refresh_until=? WHERE video=?""",
                       ("FlashData metadata refresh failed", time.time()+120, video))
        if profile and not refresh:
            return dict(profile, stale=True)
        raise
    record(root, identity, "verified", credits=profile.get("credits"))
    profile = dict(profile, selection={
        role: [item["format_id"] for item in rank(profile,role,policy)] for role in ("hd","preview","audio")
    })
    with store.connect(root, True) as db:
        db.execute("""UPDATE flashdata_profiles SET profile=?,fetched=?,refresh_until=0,error='' WHERE video=?""",
                   (json.dumps(profile), time.time(), video))
    return profile


def remember(root, video, purpose, item):
    with store.connect(root, True) as db:
        db.execute("INSERT OR IGNORE INTO flashdata_assets VALUES(?,?,?,?,?,?)",
                   (video, purpose, item["start"], item["end"], item["hash"], json.dumps(item)))


def reuse(root, video, purpose, left, right, policy):
    with store.connect(root) as db:
        rows = db.execute("SELECT asset FROM flashdata_assets WHERE video=? AND purpose=? AND start=? AND end=?",
                          (video, purpose, left, right)).fetchall()
    items = sorted((json.loads(row[0]) for row in rows), key=lambda item: -(item["height"] or 0))
    for item in items:
        height_value = item["height"]
        if purpose == "hd" and not policy["minimum"] <= (height_value or 0) <= policy["preferred"]:
            continue
        path = root / item["path"]
        if path.is_relative_to(root) and path.is_file() and media.checksum(path) == item["hash"]:
            return item
    return None
