"""Workspace-scoped IndiePost account discovery, upload and delivery."""

import hashlib
import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from . import factory_portal, portal_settings, provider_api, shorts_release, store

BASE = "https://www.indiepost.app/api/v1/"
WORKSPACE = os.environ.get("GTA_FACTORY_INDIEPOST_WORKSPACE", "Stream Clipping Factory").strip().casefold()
PLATFORMS = portal_settings.PLATFORMS
PLATFORM_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook"}
STATES = {
    "uploading": "Uploading", "creating": "Sending to IndiePost", "pending": "Scheduled",
    "processing": "Processing", "posted": "Published", "private_posted": "Posted · Only me",
    "failed": "Failed", "unknown": "Check IndiePost",
}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("IndiePost returned an invalid identifier.")
    return value


def accounts(key, send=None):
    result = (send or provider_api.request)(BASE+"accounts", {"Authorization": f"Bearer {key}"}, timeout=15)
    workspaces = result.get("workspaces") if isinstance(result, dict) else None
    if not isinstance(workspaces, list):
        raise ValueError("IndiePost returned an unexpected account response.")
    matches = [w for w in workspaces if isinstance(w, dict) and
               str(w.get("name", "")).strip().casefold() == WORKSPACE]
    if len(matches) != 1:
        raise ValueError(f'IndiePost must return exactly one workspace named "{WORKSPACE}".')
    workspace = identifier(matches[0].get("id"))
    items = matches[0].get("accounts")
    if not isinstance(items, list):
        raise ValueError("IndiePost did not return a workspace account list.")
    found = {}
    for item in items:
        if not isinstance(item, dict) or item.get("connected") is not True:
            continue
        platform, name = item.get("platform"), item.get("username")
        if platform not in PLATFORMS or not isinstance(name, str) or not name.strip():
            continue
        if platform in found:
            raise ValueError("IndiePost returned multiple accounts for one platform in this workspace.")
        found[platform] = {"id": f"{workspace}:{platform}", "workspace": workspace,
                           "platform": platform, "name": name.strip()[:120]}
    return list(found.values())


class Client:
    def __init__(self, key):
        self.key = key

    def request(self, path, body=None, request_id=None):
        headers = {"Authorization": f"Bearer {self.key}"}
        if request_id:
            headers["Idempotency-Key"] = request_id
        return provider_api.request(BASE+path, headers, body)

    def discover(self):
        return accounts(self.key)

    def init_upload(self, filename, size):
        response = self.request("media", {"filename": filename, "mimeType": "video/mp4", "size": size})
        if not isinstance(response, dict):
            raise ValueError("IndiePost returned an unexpected upload response.")
        return identifier(response.get("mediaId")), response.get("upload")

    def put(self, upload, stream, size):
        if not isinstance(upload, dict) or upload.get("method") != "PUT":
            raise ValueError("IndiePost did not return a signed PUT upload.")
        url = upload.get("url")
        parsed = urlsplit(url if isinstance(url, str) else "")
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (443, None)
                or not re.fullmatch(r"[0-9a-f]{32}\.r2\.cloudflarestorage\.com", parsed.hostname or "")):
            raise ValueError("IndiePost returned an unexpected storage host.")
        request = urllib.request.Request(url, stream, {"Content-Type": "video/mp4", "Content-Length": str(size)},
                                         method="PUT")
        try:
            with urllib.request.build_opener(provider_api.NoRedirect()).open(request, timeout=90) as response:
                if not 200 <= response.status < 300:
                    raise OSError()
        except OSError:
            raise ValueError("IndiePost storage did not confirm the upload.") from None

    def confirm(self, media_id):
        result = self.request("media/confirm", {"mediaId": media_id})
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("mediaId") != media_id:
            raise ValueError("IndiePost did not confirm the media is ready.")

    def create(self, body, request_id):
        response = self.request("posts", body, request_id)
        if not isinstance(response, dict):
            raise ValueError("IndiePost returned an unexpected post response.")
        return identifier(response.get("postGroupId"))

    def status(self, group, platform):
        response = self.request("posts/"+identifier(group))
        if not isinstance(response, dict) or response.get("postGroupId") != group:
            raise ValueError("IndiePost returned a different post group.")
        posts = response.get("posts")
        rows = [post for post in posts if isinstance(post, dict) and post.get("platform") == platform] if isinstance(posts, list) else []
        if len(rows) != 1:
            raise ValueError("IndiePost did not return a receipt for this platform.")
        return rows[0]


def privacy(values, platform):
    if platform == "youtube":
        return values["youtube_privacy"].capitalize()
    if platform == "tiktok":
        return {"SELF_ONLY": "Only me", "FOLLOWER_OF_CREATOR": "Followers",
                "MUTUAL_FOLLOW_FRIENDS": "Mutual followers", "PUBLIC_TO_EVERYONE": "Public"}[values["tiktok_privacy"]]
    return "Public reel"


def copy(values, plan):
    return ((values["title"] or plan.get("title") or "Stream clip")[:100],
            (values["caption"] or plan.get("post") or "")[:2000])


def payload(values, account, plan, slot):
    platform = account["platform"]
    title, caption = copy(values, plan)
    body = {"workspaceId": account["workspace"], "platforms": [platform], "caption": caption}
    if values["release"] == "scheduled":
        body["scheduledAt"] = slot.isoformat()
    else:
        body["when"] = "now"
    if platform == "youtube":
        body[platform] = {"format": "short", "title": title, "privacyStatus": values["youtube_privacy"]}
    elif platform == "tiktok":
        body[platform] = {"privacyLevel": values["tiktok_privacy"]}
    else:
        body[platform] = {"format": "reel"}
    return body


def next_slot(db, account, values, now, exclude_intent=0):
    zone = factory_portal.tz(values["zone"])
    scheduled = values["release"] == "scheduled"
    earliest = (now+timedelta(minutes=10 if scheduled else 5)).astimezone(zone)
    opening, closing = [time.fromisoformat(values[key]) for key in ("window_start", "window_end")]
    gap = timedelta(minutes=values["spacing"])
    slots = sorted(datetime.fromisoformat(r[0]).astimezone(zone) for r in db.execute(
        """SELECT d.slot FROM indiepost_deliveries d JOIN done_delivery_intents i ON i.id=d.intent
           WHERE i.account=? AND d.slot>=? AND i.id!=? AND (d.state!='failed' OR d.group_id IS NOT NULL)""",
        (account.casefold(), (now-timedelta(days=2)).astimezone(timezone.utc).isoformat(), exclude_intent)))
    for offset in range(32 if scheduled else 1):
        day = earliest.date()+timedelta(days=offset)
        if sum(slot.date() == day for slot in slots) >= values["account_daily"]:
            continue
        candidate = max(earliest, datetime.combine(day, time.min, zone))
        if scheduled and opening < closing:
            candidate = max(candidate, datetime.combine(day, opening, zone))
        elif scheduled and closing < opening and closing <= candidate.time() < opening:
            candidate = datetime.combine(day, opening, zone)
        for slot in slots:
            if gap and slot-gap < candidate < slot+gap:
                candidate = slot+gap
                if scheduled and closing < opening and closing <= candidate.time() < opening:
                    candidate = datetime.combine(candidate.date(), opening, zone)
        if candidate.date() != day or not scheduled and candidate != earliest:
            continue
        if scheduled and opening < closing and candidate.time() >= closing:
            continue
        return candidate.astimezone(timezone.utc)
    raise ValueError("No available slot within the account limit and release window. Review Uploads settings.")


def destinations(db, creator):
    values, versions = portal_settings.current(db)
    settings = values["uploads"]
    platforms = portal_settings.policy(db, creator)["platforms"]
    connection = db.execute("SELECT accounts FROM indiepost_connection").fetchone()
    rows = [item for item in json.loads(connection["accounts"]) if item.get("workspace")
            and item["id"] in settings["accounts"] and item["platform"] in platforms]
    return settings, versions.get("uploads", 0), rows


def deliveries(db, story):
    return db.execute("""SELECT d.*,i.platform,i.account,i.edition FROM indiepost_deliveries d
                         JOIN done_delivery_intents i ON i.id=d.intent WHERE i.story=? ORDER BY d.created,i.id""",
                      (story,)).fetchall()


def recover(root):
    # The process holds the workspace lock here. No previous HTTP handler can still finish a request.
    with store.connect(root, True) as db:
        db.execute("""UPDATE indiepost_deliveries SET state='failed',
                      detail='The upload was interrupted before scheduling. Review the edition to retry.',
                      updated=CURRENT_TIMESTAMP WHERE state='uploading'""")
        db.execute("""UPDATE indiepost_deliveries SET state='unknown',
                      detail='The scheduling request was interrupted. Check the IndiePost Calendar for this post.',
                      updated=CURRENT_TIMESTAMP WHERE state='creating'""")


def factory_verified(plan):
    # Done records are created only after the source workflow verifies the master and saves QC.
    return bool(plan.get("story_v4") and plan.get("owner_viewing"))


def release_edition(db, root, story, edition, *, automatic=False):
    row = db.execute("""SELECT e.*,d.digest,a.creator FROM done_editions d JOIN streamer_editions e ON e.id=d.edition
                      JOIN jobs j ON j.id=e.job JOIN streamer_streams st ON st.source=j.source
                      JOIN streamer_accounts a ON a.id=st.account
                      WHERE d.story=? AND d.edition=?""", (story, edition)).fetchone()
    verdict = db.execute("SELECT verdict FROM streamer_edition_feedback WHERE edition=? ORDER BY id DESC LIMIT 1",
                         (edition,)).fetchone()
    if (not row or row["qualification"] != "qualified" or row["render_state"] != "exported"
            or verdict and verdict[0] != "accepted"):
        raise ValueError("IndiePost requires a current qualified edition with final QA and no release hold.")
    plan = json.loads(row["plan"])
    verified_factory_export = automatic and factory_verified(plan)
    bounds = shorts_release.bounds_for(plan) if automatic else shorts_release.RELEASE_BOUNDS
    releasable = (shorts_release.available(db,row) if verified_factory_export else
                  row["qa"] and shorts_release.eligible(db,root,row,bounds=bounds))
    if not releasable:
        raise ValueError("IndiePost requires a current qualified edition with final QA and no release hold.")
    if not shorts_release.matches_master(shorts_release.portrait_path(root,row),row["digest"]):
        raise ValueError("The local portrait differs from the final-QA master.")
    return row


def reserve(db, root, story, fields):
    from . import factory_done

    row = release_edition(db, root, story, fields["edition"])
    values, version, available = destinations(db, row["creator"])
    if values["local_release"]:
        raise ValueError("Factory-owned daily release is active. This edition uses the local buffer.")
    account = next((item for item in available if item["id"] == fields["account"]), None)
    if not values["enabled"] or not account or fields.get("confirm") != "on":
        raise ValueError("Enable publishing, select an allowed account and confirm the delivery.")
    if int(fields["version"]) != version or fields["username"] != account["name"]:
        raise ValueError("The destination or upload settings changed. Review the delivery again.")
    reference = db.execute("SELECT reference FROM indiepost_connection").fetchone()[0]
    if not reference:
        raise ValueError("Connect IndiePost before sending.")
    manual = db.execute("""SELECT 1 FROM done_delivery_intents i JOIN done_delivery_receipts r ON r.intent=i.id
                          WHERE i.story=? AND i.platform=? AND i.account IN (?,?,?)""",
                        (story, account["platform"], account["id"].casefold(), account["name"].casefold(),
                         "@"+account["name"].lstrip("@").casefold())).fetchone()
    if manual:
        raise ValueError("A delivery receipt already exists for this story and destination.")
    intent = factory_done.account_intent(db, story, row["id"], account["platform"], account["id"])
    previous = db.execute("SELECT * FROM indiepost_deliveries WHERE intent=?", (intent["id"],)).fetchone()
    if (intent["edition"] != row["id"] or previous and
            (previous["state"] != "failed" or previous["first_submit"] or previous["account_name"] != account["name"])):
        raise ValueError("This story already has a delivery for the selected account. Check its status below.")
    slot = next_slot(db, account["id"], values, datetime.now(timezone.utc))
    body = payload(values, account, json.loads(row["plan"]), slot)
    db.execute("""INSERT INTO indiepost_deliveries(intent,account_name,workspace,slot,payload,request_id,state)
                  VALUES(?,?,?,?,?,?, 'uploading')
                  ON CONFLICT(intent) DO UPDATE SET slot=excluded.slot,payload=excluded.payload,
                  request_id=excluded.request_id,state='uploading',detail='',updated=CURRENT_TIMESTAMP""",
               (intent["id"], account["name"], account["workspace"], slot.isoformat(), json.dumps(body),
                "gta-factory-"+secrets.token_hex(24)))
    return intent["id"], reference, account, shorts_release.portrait_path(root, row), row["digest"]


def send(root, vault, story, fields):
    with store.connect(root, True) as db:
        intent, reference, account, path, digest = reserve(db, root, story, fields)
        delivery = db.execute("SELECT * FROM indiepost_deliveries WHERE intent=?", (intent,)).fetchone()
    remote_attempt = False
    try:
        key = vault.get(reference)
        if not key:
            raise ValueError("The IndiePost key is unavailable.")
        client = Client(key)
        if account not in client.discover():
            raise ValueError("The connected account changed at IndiePost. Run the connection test again.")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("The portrait differs from the final-QA master.")
            size = stream.tell()
            stream.seek(0)
            media_id, upload = client.init_upload(f"gta-factory-short-{digest[:24]}.mp4", size)
            with store.connect(root, True) as db:
                db.execute("UPDATE indiepost_deliveries SET media_id=?,updated=CURRENT_TIMESTAMP WHERE intent=?",
                           (media_id, intent))
            if upload:
                client.put(upload, stream, size)
        client.confirm(media_id)
        body = json.loads(delivery["payload"]) | {"mediaIds": [media_id]}
        if account not in client.discover():
            raise ValueError("The connected account changed during upload. Run the connection test again.")
        with store.connect(root, True) as db:
            row = release_edition(db, root, story, fields["edition"])
            values, version, targets = destinations(db, row["creator"])
            if (not values["enabled"] or version != int(fields["version"]) or account not in targets
                    or db.execute("SELECT reference FROM indiepost_connection").fetchone()[0] != reference):
                raise ValueError("Publishing settings or account authorization changed during upload.")
            now = datetime.now(timezone.utc)
            slot = datetime.fromisoformat(delivery["slot"])
            if body.get("when") == "now":
                slot = next_slot(db, account["id"], values, now, exclude_intent=intent)
            elif slot <= now+timedelta(minutes=1):
                raise ValueError("The release slot is too close. Retry to reserve a new time.")
            db.execute("""UPDATE indiepost_deliveries SET state='creating',payload=?,first_submit=CURRENT_TIMESTAMP,
                          slot=?,attempts=1,updated=CURRENT_TIMESTAMP WHERE intent=?""",
                       (json.dumps(body), slot.isoformat(), intent))
        remote_attempt = True
        try:
            group = client.create(body, delivery["request_id"])
        except provider_api.ProviderError as error:
            if not error.retry or error.status == 429:
                raise
            with store.connect(root, True) as db:
                db.execute("UPDATE indiepost_deliveries SET attempts=2 WHERE intent=?", (intent,))
            group = client.create(body, delivery["request_id"])
    except (ValueError, OSError) as error:
        if remote_attempt:
            detail = "Create could not be confirmed. Check the IndiePost Calendar before sending again."
            state = "unknown"
        else:
            detail = str(error) if isinstance(error, ValueError) else "The local portrait could not be read."
            state = "failed"
        with store.connect(root, True) as db:
            db.execute("UPDATE indiepost_deliveries SET state=?,detail=?,updated=CURRENT_TIMESTAMP WHERE intent=?",
                       (state, detail, intent))
        return detail
    with store.connect(root, True) as db:
        db.execute("UPDATE indiepost_deliveries SET group_id=?,state='pending',updated=CURRENT_TIMESTAMP WHERE intent=?",
                   (group, intent))
    return "IndiePost accepted the post. Refresh the receipt for the platform outcome."


def refresh(root, vault, story, intent):
    from . import factory_done

    with store.connect(root) as db:
        delivery = next((row for row in deliveries(db, story) if row["intent"] == intent), None)
        reference = db.execute("SELECT reference FROM indiepost_connection").fetchone()[0]
    if not delivery or not delivery["group_id"]:
        raise ValueError("This delivery has no IndiePost post group to refresh.")
    key = vault.get(reference) if reference else None
    if not key:
        raise ValueError("Connect IndiePost to refresh this delivery.")
    row = Client(key).status(delivery["group_id"], delivery["platform"])
    status = row.get("status")
    external, occurred = row.get("externalPostId"), row.get("postedAt")
    detail = ""
    tiktok = row.get("tiktok")
    requested = json.loads(delivery["payload"]).get("tiktok", {})
    private_tiktok = (delivery["platform"] == "tiktok" and requested.get("privacyLevel") == "SELF_ONLY"
                      and isinstance(tiktok, dict) and tiktok.get("privacyLevel") == "SELF_ONLY")
    if status == "posted" and occurred and not external and private_tiktok:
        stamp = datetime.fromisoformat(occurred)
        if stamp.tzinfo is None:
            raise ValueError("IndiePost returned a receipt without a timezone.")
        status = "private_posted"
        detail = ("IndiePost confirmed publication at " + stamp.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
                  + ". TikTok did not return a platform post ID.")
    elif status == "posted" and (not external or not occurred):
        status = "processing"
        detail = "IndiePost has yet to return the platform post ID and timestamp."
    elif status == "failed":
        detail = "IndiePost reports a platform failure. Open its Calendar for details."
    elif status == "publishing":
        status = "processing"
    elif status not in ("pending", "processing", "posted"):
        status = "unknown"
        detail = "IndiePost returned an unfamiliar platform status. Check its Calendar."
    with store.connect(root, True) as db:
        current = db.execute("SELECT state FROM indiepost_deliveries WHERE intent=?", (intent,)).fetchone()[0]
        if current == "posted":
            return "IndiePost status · Published"
        if current == "private_posted" and status != "posted":
            return "IndiePost status · Posted · Only me"
        if status == "posted":
            remote_id = identifier(external)
            remote = f"https://www.youtube.com/watch?v={remote_id}" if delivery["platform"] == "youtube" else remote_id
            factory_done.record(db, story, delivery["edition"], delivery["platform"], delivery["account"], "published",
                                remote, occurred, f'IndiePost group {delivery["group_id"]}')
        db.execute("UPDATE indiepost_deliveries SET state=?,detail=?,updated=CURRENT_TIMESTAMP WHERE intent=?",
                   (status, detail, intent))
    return f"IndiePost status · {STATES[status]}"
