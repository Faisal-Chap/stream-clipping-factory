"""YouTube roster intake and per-run configuration."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import media, store
from .library import identify, YOUTUBE

CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
DEFAULTS = {"target": 10, "hourly_target": 7, "editor_minutes": 60, "remote_minutes": 600, "remote_height": 1080, "remote_mib": 8192,
            "reserve_mib": 2048, "asr_threads": 6, "asr_model": "base.en"}


def account_url(value):
    url = urlsplit(value.strip())
    path = url.path.rstrip("/")
    if (url.scheme != "https" or url.netloc.lower() not in YOUTUBE - {"youtu.be"} or
            not re.fullmatch(r"/(?:channel/UC[A-Za-z0-9_-]{22}|@[\w.-]{2,70})", path)):
        raise ValueError("Enter a YouTube channel URL with an @handle or channel ID.")
    return "https://www.youtube.com" + path


def extract(url, *options):
    command = media.downloader()
    if not command:
        if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
            raise ValueError("Add a specific YouTube replay URL in Sources to look up its FlashData profile.")
        raise ValueError("Install yt-dlp before looking up YouTube streams.")
    result = media.run([*command, "--ignore-config", "--js-runtimes", "node",
                        "--socket-timeout", "12", "--retries", "0", "--encoding", "utf-8",
                        *options, "--", url], 90)
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8",errors="replace").lower()
        if "sign in" in detail or "not a bot" in detail:
            raise ValueError("YouTube requires sign-in to verify this replay.")
        if "429" in detail or "too many requests" in detail:
            raise ValueError("YouTube rate-limited replay verification.")
        raise ValueError("YouTube metadata is unavailable. Retry or attach a local recording.")
    return [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()][0]


def discovery(account):
    try:
        data = extract(account.rstrip("/") + "/streams", "--flat-playlist", "--playlist-end", "30", "-J")
    except ValueError:
        # A creator may publish full archives on a dedicated uploads channel without a Streams tab.
        data = extract(account.rstrip("/") + "/videos", "--flat-playlist", "--playlist-end", "30", "-J")
    channel = data.get("channel_id") or data.get("id")
    if not CHANNEL.fullmatch(channel or ""):
        raise ValueError("YouTube did not resolve a stable channel ID.")
    return channel, [entry for entry in data.get("entries", []) if entry and
                     entry.get("live_status") == "was_live"]


def save_creator(db, fields, channel):
    name, game = fields["name"].strip(), fields["game"].strip()
    if not name or len(name) > 100 or not game or len(game) > 100:
        raise ValueError("Enter a creator and game label of up to 100 characters.")
    existing = db.execute("SELECT creator FROM streamer_accounts WHERE platform='youtube' AND platform_id=?",
                          (channel,)).fetchone()
    if existing:
        return existing["creator"]
    creator = db.execute("INSERT INTO streamer_creators(name,game,credit) VALUES(?,?,?)",
                         (name, game, name)).lastrowid
    db.execute("INSERT INTO streamer_accounts(creator,platform,platform_id,url) VALUES(?,'youtube',?,?)",
               (creator, channel, f"https://www.youtube.com/channel/{channel}"))
    return creator


def roster(db, creator, account=None):
    row = db.execute("""SELECT c.*,a.id AS account,a.url,a.platform_id,a.checked FROM streamer_creators c
                        JOIN streamer_accounts a ON a.creator=c.id WHERE c.id=? AND (? IS NULL OR a.id=?)
                        ORDER BY a.id LIMIT 1""", (creator,account,account)).fetchone()
    if not row:
        raise ValueError("Creator account was not found.")
    return row


def attach(db, creator, fields, info=None):
    owner = roster(db, creator, fields.get("account") or None)
    identity, canonical, route = identify(fields["link"])
    archive_upload = False
    if route == "youtube":
        archive_upload = bool(info and (fields.get("archive_upload") == "on" or info.get("known_attachment"))
                              and info.get("live_status") == "not_live" and (info.get("duration") or 0) >= 1200)
        if not info or (info.get("live_status") not in ("was_live","is_live","is_upcoming","post_live") and not archive_upload
                        or info.get("channel_id") != owner["platform_id"]):
            raise ValueError("Use a broadcast from the selected creator account.")
    elif route != "file":
        raise ValueError("Attach a YouTube broadcast or an absolute path to a local recording.")
    if route=="file" and not Path(canonical).is_file():
        raise ValueError("The server cannot read that recording. Check the path or select a file in the browser.")
    for key in ("location","chat_path"):
        if fields.get(key) and (identify(fields[key])[2]!="file" or not Path(fields[key].strip().strip('"')).is_file()):
            raise ValueError("Use an existing server file for the recording override or chat.")
    source, new = store.save_source(db, {"link": canonical, "creator": owner["name"], "game": owner["game"],
                                         "location": fields.get("location", "")})
    previous = db.execute("SELECT * FROM streamer_streams WHERE source=?", (source,)).fetchone()
    if previous and previous["account"] != owner["account"]:
        raise ValueError("This recording is already assigned to another creator.")
    if not new and fields.get("location"):
        details = dict(db.execute("SELECT * FROM sources WHERE id=?",(source,)).fetchone())
        if details["location"]!=fields["location"]:
            store.save_source(db,dict(details,location=fields["location"]),source)
    title = (info or {}).get("title") or fields.get("title", "").strip() or "Attached recording"
    if not previous:
        db.execute("""INSERT INTO streamer_streams(account,source,platform_id,title,live_status,chat_path)
                      VALUES(?,?,?,?,?,?)""",
                   (owner["account"], source, identity.split(":")[1] if route == "youtube" else None,
                    title[:250], ("archive_upload" if archive_upload else info["live_status"]) if info else "manual",
                    fields.get("chat_path") or None))
    elif fields.get("chat_path"):
        db.execute("UPDATE streamer_streams SET chat_path=? WHERE source=?",(fields["chat_path"],source))
    if previous and info and not info.get("known_attachment"):
        db.execute("UPDATE streamer_streams SET live_status=? WHERE source=?",
                   ("archive_upload" if archive_upload else info["live_status"],source))
    return source


def crop(value):
    if not value.strip():
        return None
    try:
        x, y, width, height = map(float, value.split(","))
    except ValueError:
        raise ValueError("Enter four normalized crop values for x, y, width and height.") from None
    if not (0 <= x < 1 and 0 <= y < 1 and .03 <= width <= 1-x and .03 <= height <= 1-y):
        raise ValueError("Camera crop must fit within the video frame.")
    return [x, y, width, height]


def enqueue(db, source, fields):
    from .portal_settings import start
    return start(db,source,fields)[0]
