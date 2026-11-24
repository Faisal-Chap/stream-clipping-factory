import json

from .analysis import select
from .provider_meter import report as meter_report


def job_report(db, job_id, include_rejected=True):
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    config = json.loads(row["settings"])
    snapshot = json.loads(row["snapshot"])
    analysis = db.execute("SELECT * FROM analyses WHERE id=?", (row["analysis"],)).fetchone()
    result = json.loads(analysis["data"]) if analysis else {}
    chosen, rejected = select(result.get("ranked", []), config["max_previews"])
    stories = []
    for record in db.execute("""SELECT s.*,js.rank FROM job_stories js JOIN stories s ON s.id=js.story
                                WHERE js.job=? ORDER BY js.rank""", (job_id,)):
        plan = json.loads(record["plan"])
        current = next((item for item in chosen if item["start"] == plan["start"]
                        and item["end"] == plan["end"]), None)
        if current:
            plan.update(score=current["score"], labels=current["labels"])
        stories.append(dict(id=record["id"], rank=record["rank"], plan=plan,
                            preview=f"/media/{record['id']}", decision=record["decision"], reason=record["reason"]))
    asset = db.execute("SELECT * FROM assets WHERE hash=?", (row["asset"],)).fetchone()
    revision = db.execute("""SELECT route FROM revisions WHERE source=? AND revision=? AND asset=?
                             LIMIT 1""", (row["source"], row["revision"], row["asset"])).fetchone()
    return dict(job=job_id, source=row["source"], creator=snapshot["creator"], game=snapshot["game"],
                state=row["state"], progress=row["progress"], error=row["error"], settings=config,
                route=revision["route"] if revision else None, asset=dict(asset) if asset else None,
                analysis=result.get("version"), samples=result.get("samples"),
                timings=json.loads(row["timings"]), **meter_report(db, job_id), stories=stories,
                windows=result.get("windows"), speech_status=result.get("speech_status"),
                rejected=rejected if include_rejected else rejected[:10], rejected_count=len(rejected),
                empty_reason=result.get("empty_reason", ""))


def review(db, story, fields):
    decision = fields["decision"]
    reason = fields.get("reason", "").strip()
    if decision not in ("accepted", "rejected", "pending"):
        raise ValueError("Choose a review decision.")
    if decision != "pending" and not reason:
        raise ValueError("Record a reason with the decision.")
    changed = db.execute("UPDATE stories SET decision=?,reason=? WHERE id=? AND preview IS NOT NULL",
                         (decision, reason, story))
    if not changed.rowcount:
        raise ValueError("This preview is unavailable.")
