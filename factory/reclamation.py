"""Release-owned portrait expiry with a durable unlink journal."""

import re
import time
from pathlib import Path

from . import media, shorts_release, store

APPROVED_NAMES = ("portrait.mp4","portrait.partial.mp4")

def own_portrait(db, root, story, edition, now):
    path = shorts_release.portrait_path(root,edition)
    for name in APPROVED_NAMES:
        artifact = path.with_name(name)
        try:
            info = artifact.stat()
            digest = edition["digest"] if name=="portrait.mp4" else media.checksum(artifact)
        except OSError:
            continue
        db.execute("""INSERT OR IGNORE INTO factory_release_artifacts(path,story,edition,digest,bytes,changed)
                      VALUES(?,?,?,?,?,?)""",(artifact.relative_to(root).as_posix(),story,edition["id"],
                                             digest,info.st_size,now))


def lease(root, job, candidate, now=None):
    now = time.time() if now is None else now
    with store.connect(root,True) as db:
        if db.execute("SELECT 1 FROM jobs WHERE id=?",(job,)).fetchone():
            db.execute("""INSERT INTO factory_release_editor_leases VALUES(?,?,?) ON CONFLICT(job,candidate)
                         DO UPDATE SET until=MAX(until,excluded.until)""",(job,candidate,now+2*3600))


def confirmed_at(db, edition):
    rows = db.execute("""SELECT CASE
                WHEN d.state='confirmed' THEN d.confirmed
                WHEN d.intent IS NOT NULL THEN NULL
                WHEN p.state='private_posted' THEN unixepoch(p.updated)
                WHEN p.intent IS NULL OR p.state='posted' THEN
                    (SELECT MAX(unixepoch(r.occurred)) FROM done_delivery_receipts r
                     WHERE r.intent=i.id AND r.edition=i.edition AND r.status='published'
                     AND r.remote_id IS NOT NULL)
                END AS confirmed
                FROM done_delivery_intents i
                LEFT JOIN factory_release_destinations d ON d.intent=i.id
                LEFT JOIN indiepost_deliveries p ON p.intent=i.id
                WHERE i.edition=?""",(edition,)).fetchall()
    return max(r["confirmed"] for r in rows) if rows and all(r["confirmed"] is not None for r in rows) else None


def can_reclaim(db, entry, now, grace, *, manual=False):
    if entry["protected"] or not re.fullmatch(r"c[0-9]+-[0-9]+",entry["candidate"]):
        return False
    if not re.fullmatch(r"[a-f0-9]{64}",entry["edition"]) or entry["source"] is None:
        return False
    if manual:
        if shorts_release.hold(db,entry["story"],entry["edition"]):
            return False
    else:
        confirmed = confirmed_at(db,entry["edition"])
        if confirmed is None or now<confirmed+grace or shorts_release.restriction(db,entry["edition"]):
            return False
    if db.execute("""SELECT 1 FROM done_delivery_intents i
                     LEFT JOIN factory_release_destinations d ON d.intent=i.id
                     LEFT JOIN indiepost_deliveries p ON p.intent=i.id
                     WHERE i.edition=? AND (d.state NOT IN ('confirmed','failed')
                       OR p.state NOT IN ('posted','private_posted','failed'))""",(entry["edition"],)).fetchone():
        return False
    if db.execute("""SELECT 1 FROM jobs WHERE source=? AND state IN ('queued','running')""",(entry["source"],)).fetchone():
        return False
    if db.execute("""SELECT 1 FROM factory_release_editor_leases WHERE job=? AND candidate=? AND until>?""",
                  (entry["job"],entry["candidate"],now)).fetchone():
        return False
    if db.execute("""SELECT 1 FROM streamer_editions WHERE job=? AND candidate=?
                    AND render_state IN ('queued','rendering')""",(entry["job"],entry["candidate"])).fetchone():
        return False
    absolute = Path(entry["absolute"]).resolve()
    for source in db.execute("SELECT location AS file FROM sources WHERE location<>'' UNION SELECT link FROM sources"):
        name = Path(source["file"])
        if name.is_absolute() and name.resolve()==absolute:
            return False
    return not db.execute("""SELECT 1 FROM assets WHERE path=? OR path=?
                           UNION ALL SELECT 1 FROM streamer_editions e WHERE e.id<>? AND
                           (json_extract(e.plan,'$.archive')=? OR EXISTS (SELECT 1 FROM json_each(e.plan,'$.source_windows') w
                            WHERE json_extract(w.value,'$.path')=?))""",
                          (entry["path"],str(absolute),entry["edition"],entry["path"],entry["path"])).fetchone()


def entries(db, root):
    for entry in db.execute("""SELECT a.*,e.candidate,e.job,s.source FROM factory_release_artifacts a
                    JOIN streamer_editions e ON e.id=a.edition JOIN done_stories s ON s.id=a.story
                    WHERE a.state IN ('owned','deleting') ORDER BY a.state='owned',a.changed"""):
        record = dict(entry)
        record["absolute"] = str(root/entry["path"])
        yield record


def verified_path(root, entry):
    expected = shorts_release.portrait_path(root,entry,entry["edition"])
    claimed = root/entry["path"]
    if claimed.name not in APPROVED_NAMES:
        return None
    expected = expected.with_name(claimed.name)
    resolved_root = root.resolve()
    if claimed != expected or expected.resolve() != resolved_root / expected.relative_to(root):
        return None
    if claimed.exists():
        op = claimed.stat()
        if not claimed.is_file() or op.st_nlink!=1 or op.st_size!=entry["bytes"] or media.checksum(claimed)!=entry["digest"]:
            return None
    return claimed


def reclaim_one(root, now, grace_hours):
    grace = max(24,min(96,grace_hours))*3600
    selected = None
    with store.connect(root,True) as db:
        for edition in db.execute("""SELECT e.id,e.job,e.candidate,n.story,n.digest
                    FROM done_editions n JOIN streamer_editions e ON e.id=n.edition
                    WHERE e.render_state='exported'
                    AND EXISTS (SELECT 1 FROM done_delivery_intents i WHERE i.edition=e.id)
                    AND NOT EXISTS (SELECT 1 FROM factory_release_artifacts a WHERE a.edition=e.id)"""):
            confirmed = confirmed_at(db,edition["id"])
            if confirmed is not None and now>=confirmed+grace:
                own_portrait(db,root,edition["story"],edition,now)
        for entry in entries(db,root):
            if entry["removal_method"]!="automatic" or not can_reclaim(db,entry,now,grace):
                continue
            path = verified_path(root,entry)
            if path is None or (not path.exists() and entry["state"]!="deleting"):
                continue
            db.execute("UPDATE factory_release_artifacts SET state='deleting',changed=? WHERE path=?",(now,entry["path"]))
            selected = entry
            break
    if not selected:
        return 0
    return finish_removal(root,selected["path"],now,grace,manual=False)


def finish_removal(root, relative, now, grace, *, manual):
    with store.connect(root,True) as db:
        claimed = next((e for e in entries(db,root) if e["path"]==relative),None)
        method = "manual" if manual else "automatic"
        if (not claimed or claimed["state"]!="deleting" or claimed["removal_method"]!=method or
                not can_reclaim(db,claimed,now,grace,manual=manual)):
            return 0
        path = verified_path(root,claimed)
        if path is None:
            return 0
        path.unlink(missing_ok=True)
        db.execute("UPDATE factory_release_artifacts SET state='removed',changed=? WHERE path=?",(now,claimed["path"]))
        return claimed["bytes"]


def delete_portrait(root, identity):
    now = time.time()
    with store.connect(root,True) as db:
        edition = db.execute("""SELECT e.id,e.job,e.candidate,n.story,n.digest
                       FROM done_editions n JOIN streamer_editions e ON e.id=n.edition
                       WHERE e.id=? AND e.render_state='exported'""",(identity,)).fetchone()
        if not edition:
            raise ValueError("This file has no owned final reel record.")
        path = shorts_release.portrait_path(root,edition).relative_to(root).as_posix()
        if db.execute("SELECT state FROM factory_release_artifacts WHERE path=? AND state='removed'",(path,)).fetchone():
            return 0
        own_portrait(db,root,edition["story"],edition,now)
        claimed = next((e for e in entries(db,root) if e["path"]==path),None)
        if not claimed or not can_reclaim(db,claimed,now,0,manual=True):
            raise ValueError("This reel is protected by active work, a review hold, a pending delivery or a source reference.")
        checked = verified_path(root,claimed)
        if checked is None or (not checked.exists() and claimed["state"]!="deleting"):
            raise ValueError("The reel file is missing or its ownership checksum could not be verified.")
        db.execute("""UPDATE factory_release_artifacts SET state='deleting',removal_method='manual',changed=?
                      WHERE path=?""",(now,path))
    return finish_removal(root,path,now,0,manual=True)


def recover_manual_one(root, now):
    with store.connect(root) as db:
        pending = [entry[0] for entry in db.execute("""SELECT path FROM factory_release_artifacts
                                                     WHERE state='deleting' AND removal_method='manual'""")]
    for path in pending:
        if finish_removal(root,path,now,0,manual=True):
            break
