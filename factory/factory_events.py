"""Append-only worker telemetry with UTC timestamps and bounded, public progress fields."""

import logging
import re
from datetime import datetime, timezone

from .store import connect

console = logging.getLogger("factory.worker.events")
console.setLevel(logging.INFO)
console.addHandler(logging.StreamHandler())
console.propagate = False


def log_line(utc, job, attempt, stage, kind, item):
    detail = f" | {item}" if item and item!=stage else ""
    return f"{utc} | Run {job} attempt {attempt} | {stage} | {kind}{detail}"


def record(db, job, kind, *, stage=None, item="", covered=None, total=None, size=None, error=""):
    row = db.execute("SELECT attempt,stage,asset FROM jobs WHERE id=?", (job,)).fetchone()
    stage = stage or row["stage"]
    if row["asset"] and (size is None or total is None):
        asset = db.execute("SELECT bytes,duration FROM assets WHERE hash=?", (row["asset"],)).fetchone()
        if asset:
            size = asset["bytes"] if size is None else size
            total = asset["duration"] if total is None else total
    if covered is None and kind in ("pulse", "progress", "finished", "deferred"):
        latest = db.execute("""SELECT covered_seconds FROM factory_events WHERE job=? AND attempt=?
                               AND stage=? AND covered_seconds IS NOT NULL ORDER BY id DESC LIMIT 1""",
                            (job,row["attempt"],stage)).fetchone()
        covered = latest["covered_seconds"] if latest else None
    if size is None and kind in ("pulse","deferred"):
        previous = db.execute("""SELECT bytes FROM factory_events WHERE job=? AND attempt=? AND stage=?
                                 AND bytes IS NOT NULL ORDER BY id DESC LIMIT 1""",(job,row["attempt"],stage)).fetchone()
        size = previous["bytes"] if previous else None
    cost, retry = db.execute("""SELECT COALESCE(SUM(reservation_micros*attempts),0),
                               MAX(CASE WHEN result IS NULL AND next_try>strftime('%s','now')+0 THEN next_try END)
                               FROM provider_calls WHERE job=?""", (job,)).fetchone()
    scheduled = datetime.fromtimestamp(retry, timezone.utc).isoformat() if retry else None
    clean = re.sub(r"(?i)\b(?:https?://|data:|bearer\s+)\S+", "[redacted]", item)
    clean = " ".join(clean.split())[:240]
    event = db.execute("""INSERT INTO factory_events(job,attempt,stage,kind,item,covered_seconds,total_seconds,
                          bytes,reserved_micros,error_class,retry_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING utc""",
                       (job,row["attempt"],stage,kind,clean,covered,total,size,cost,error,scheduled)).fetchone()
    if kind!="pulse":
        console.info("%s",log_line(event["utc"],job,row["attempt"],stage,kind,clean))


def pulse(root, job, finished):
    while not finished.wait(8):
        with connect(root, True) as db:
            row = db.execute("SELECT state,progress FROM jobs WHERE id=?", (job,)).fetchone()
            if row and row["state"] == "running":
                record(db, job, "pulse", item=row["progress"])
