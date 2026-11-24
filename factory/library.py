import re
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

YOUTUBE = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def identify(value):
    value = value.strip().strip('"')
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a video URL or an absolute local file path.")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return "file:" + str(Path(value).resolve()), value, "file"
    try:
        url = urlsplit(value)
    except ValueError:
        raise ValueError("Enter a valid video URL.") from None
    if url.scheme == "file" and url.netloc in ("", "localhost"):
        path = unquote(url.path)
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if Path(path).is_absolute() or PureWindowsPath(path).is_absolute():
            return identify(path)
        raise ValueError("Use an absolute file URL.")
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Use an HTTP(S) link or an absolute local file path.")
    if url.hostname in YOUTUBE:
        parts = url.path.strip("/").split("/")
        video = parse_qs(url.query).get("v", [""])[0]
        if url.hostname == "youtu.be":
            video = parts[0]
        elif parts[0] in ("shorts", "live", "embed") and len(parts) == 2:
            video = parts[1]
        elif parts[0] != "watch":
            video = ""
        if not VIDEO_ID.fullmatch(video):
            raise ValueError("This YouTube link needs a valid video ID.")
        return "youtube:" + video, "https://www.youtube.com/watch?v=" + video, "youtube"
    canonical = urlunsplit((url.scheme, url.netloc.lower(), url.path or "/", url.query, ""))
    return "url:" + canonical, canonical, "direct"


def starter_rows(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line)[1:-1]
        cells = [cell.strip().replace(r"\|", "|") for cell in cells]
        if len(cells) != 6 or cells[0] == "Source link" or re.fullmatch(r"[-:]+", cells[0]):
            continue
        link, creator, game, location, state, notes = cells
        if state not in ("queued", "paused"):
            raise ValueError("Starter rows must be queued or paused.")
        rows.append(dict(link=link, creator=creator, game=game, location=location,
                         state=state, notes=notes, priority=len(rows) + 1))
    return rows
