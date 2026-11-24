"""Persistent single-worker queue and checkpoint transitions."""

import json
import logging
import os
import re
import threading
from time import perf_counter, time

from . import analysis, context_judge, daily_release, editorial, factory_events, media, readiness, replay_cache, render, source_queue, streamer_edits, streamer_run
from .store import connect
from .provider_connections import ConnectionRequired, Connections
from .provider_meter import ProviderPending
from .streamer_transfer import TransferHold


class Worker(threading.Thread):
    def __init__(self, root, connections=None):
        super().__init__(name="gta-media-factory-worker", daemon=True)
        self.root = root
        self.connections = connections or Connections(root)
        self.shutdown = threading.Event()
        self.wake = threading.Event()

    def run(self):
        probes = threading.Thread(target=self.run_probes, name="gta-media-factory-provider-probes", daemon=True)
        probes.start()
        replay_checks = threading.Thread(target=self.check_replays, name="gta-streamer-factory-replay-readiness", daemon=True)
        replay_checks.start()
        releases = threading.Thread(target=daily_release.run,args=(self.root,self.connections.vault,self.shutdown),
                                    name="gta-streamer-factory-local-release",daemon=True)
        releases.start()
        next_retention = 0
        while not self.shutdown.is_set():
            if time()>=next_retention:
                next_retention = time()+15
                try:
                    replay_cache.reconcile(self.root)
                except (OSError, ValueError, TransferHold, ProviderPending) as error:
                    logging.error("Full replay maintenance failed with %s",type(error).__name__)
            with connect(self.root, True) as db:
                paused = db.execute("SELECT paused FROM factory_controls").fetchone()[0] or readiness.admission_hold()
                row = None if paused else db.execute("""SELECT j.* FROM jobs j JOIN sources s ON s.id=j.source
                                  WHERE j.state='queued' AND j.revision=s.revision AND s.state='queued'
                                  AND NOT EXISTS (SELECT 1 FROM provider_calls p WHERE p.job=j.id
                                      AND p.result IS NULL AND p.next_try>CAST(strftime('%s','now') AS REAL))
                                  ORDER BY CASE WHEN json_extract(j.settings,'$.full_replay')=1
                                    AND json_extract(j.settings,'$.coverage_start')>0 THEN 0 ELSE 1 END,
                                    s.priority,j.id LIMIT 1""").fetchone()
                if row:
                    db.execute("UPDATE jobs SET state='running',attempt=attempt+1 WHERE id=?", (row["id"],))
                    factory_events.record(db, row["id"], "claimed", item="Worker claimed this attempt")
                edition = streamer_edits.next_edit(db) if not paused and not row else None
            if row:
                self.process(row)
            elif edition:
                streamer_edits.process(self.root, edition)
            else:
                self.wake.wait(1)
                self.wake.clear()

    def run_probes(self):
        while not self.shutdown.is_set():
            self.connections.tick()
            self.shutdown.wait(.7)

    def check_replays(self):
        from . import flashdata, portal_settings
        while not self.shutdown.is_set():
            try:
                source_queue.check_one(self.root)
                if os.environ.get("GTA_FACTORY_YOUTUBE_PROVIDER") == "flashdata":
                    with connect(self.root) as db:
                        height = portal_settings.current(db)[0]["resources"]["remote_height"]
                    source_queue.resolve_pending(self.root,time(),height)
                flashdata.retry_cleanup(self.root)
                flashdata.reconcile_fallback(self.root)
                flashdata.reconcile_attempts(self.root)
                replay_cache.retire(self.root)
            except Exception as error:
                logging.error("Replay readiness check failed with %s",type(error).__name__)
            self.shutdown.wait(5)

    def process(self, row):
        finished = threading.Event()
        heartbeat = threading.Thread(target=factory_events.pulse, args=(self.root, row["id"], finished), daemon=True)
        heartbeat.start()
        try:
            self.process_run(row)
        finally:
            finished.set()
            heartbeat.join()
            with connect(self.root, True) as db:
                current = db.execute("SELECT state,progress FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if current["state"] == "complete":
                    factory_events.record(db, row["id"], "completed", item=current["progress"])
                    replay_cache.on_completed(self.root,db,row["id"])

    def process_run(self, row):
        job = row["id"]
        timings = json.loads(row["timings"])
        snapshot, settings = json.loads(row["snapshot"]), json.loads(row["settings"])
        method = settings["method"]

        def update(message, *, covered=None, total=None, size=None):
            if size is None:
                if amount := re.match(r"(?:Staged|Downloaded) (\d+) MiB", message):
                    size = int(amount[1])*media.CHUNK
            stopped = False
            with connect(self.root, True) as db:
                db.execute("UPDATE jobs SET progress=? WHERE id=?", (message, job))
                factory_events.record(db, job, "progress", item=message, covered=covered, total=total, size=size)
                if covered is not None and db.execute("SELECT stop FROM jobs WHERE id=?",(job,)).fetchone()[0]:
                    db.execute("""UPDATE jobs SET state='stopped',stop=0,
                               progress='Stopped at a persisted coverage checkpoint.' WHERE id=?""",(job,))
                    factory_events.record(db,job,"stopped",item="Stopped at a persisted coverage checkpoint",
                                          covered=covered,total=total)
                    stopped = True
            if stopped:
                raise JobStopped

        def checkpoint(stage, callback):
            if settings.get("full_replay"):
                replay_cache.guard(self.root,row)
            with connect(self.root, True) as db:
                state = db.execute("SELECT stop FROM jobs WHERE id=?", (job,)).fetchone()
                if state["stop"] or self.shutdown.is_set():
                    automatic = not state["stop"] and settings.get("acquisition")=="audio-first"
                    following = ("queued","Saved checkpoint. Resumes when the factory starts.") if automatic else (
                        "stopped","Stopped at a safe checkpoint.")
                    db.execute("UPDATE jobs SET state=?,stop=0,progress=? WHERE id=?",(*following,job))
                    factory_events.record(db,job,"deferred" if automatic else "stopped",item=following[1])
                    raise_after_commit = True
                else:
                    db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, job))
                    factory_events.record(db, job, "started", item=stage)
                    raise_after_commit = False
            if raise_after_commit:
                raise JobStopped
            start = perf_counter()
            try:
                result = callback()
                with connect(self.root, True) as db:
                    coverage = result[1]["duration"] if stage == "Transcribe" else None
                    factory_events.record(db, job, "finished", item=f"Completed in {perf_counter()-start:.1f}s", covered=coverage)
                return result
            finally:
                timings[stage] = round(timings.get(stage, 0) + perf_counter() - start, 3)
                with connect(self.root, True) as db:
                    db.execute("UPDATE jobs SET timings=? WHERE id=?", (json.dumps(timings), job))

        try:
            if method == "streamer":
                return streamer_run.process(self, row, snapshot, settings, checkpoint, update)
            asset = checkpoint("Acquire", lambda: self.asset(row, snapshot, settings, update))
            analysis_id, result = checkpoint("Analyze", lambda: self.analyze(job, asset, snapshot, settings, update))
            chosen, _ = analysis.select(result["ranked"], settings["max_previews"])
            for rank, candidate in enumerate(chosen, 1):
                checkpoint("Render", lambda: self.preview(
                    job, snapshot["id"], asset, analysis_id, candidate, rank, len(chosen), update))
            with connect(self.root, True) as db:
                message = f"{len(chosen)} previews ready" if chosen else "No story met the evidence threshold."
                db.execute("""UPDATE jobs SET state='complete',stage='Review',progress=?,stop=0
                           WHERE id=?""", (message, job))
        except JobStopped:
            pass
        except ProviderPending as waiting:
            with connect(self.root, True) as db:
                db.execute("""UPDATE jobs SET state=CASE WHEN stop=1 THEN 'stopped' ELSE 'queued' END,
                            progress=?,stop=0 WHERE id=?""", (str(waiting), job))
                factory_events.record(db, job, "deferred", item=str(waiting), error=type(waiting).__name__)
        except ConnectionRequired as waiting:
            with connect(self.root, True) as db:
                db.execute("""UPDATE jobs SET state='stopped',progress='Waiting for provider setup',error=?,stop=0
                            WHERE id=?""", (str(waiting), job))
                factory_events.record(db, job, "blocked", item="Provider connection needs attention",
                                      error=type(waiting).__name__)
        except TransferHold as waiting:
            with connect(self.root,True) as db:
                db.execute("UPDATE jobs SET state='stopped',error=?,progress=?,stop=0 WHERE id=?",
                           (str(waiting),str(waiting),job))
                factory_events.record(db,job,"stopped",item=str(waiting),error=type(waiting).__name__)
        except (media.MediaError, ValueError) as error:
            self.fail(job, str(error), type(error).__name__)
        except Exception as error:
            logging.error("Job %s failed with %s", job, type(error).__name__)
            self.fail(job, "Unexpected processing error. Check tool setup, local disk space, and retry.", type(error).__name__)

    def fail(self, job, reason, error_class):
        with connect(self.root, True) as db:
            db.execute("UPDATE jobs SET state='failed',error=?,stop=0 WHERE id=?", (reason, job))
            factory_events.record(db, job, "failed", error=error_class)

    def asset(self, row, snapshot, settings, update):
        fingerprint = media.input_fingerprint(snapshot)
        saved = self.retained_asset(row,fingerprint)
        if saved:
            asset = saved
            update("Reused the verified master")
        else:
            asset, route = media.acquire(self.root, row["id"], snapshot, update, settings)
            if fingerprint != media.input_fingerprint(snapshot):
                raise media.MediaError("Source file changed during acquisition. Retry with a stable file.")
            with connect(self.root, True) as db:
                db.execute("""INSERT OR IGNORE INTO assets(hash,path,duration,audio,bytes)
                           VALUES(:hash,:path,:duration,:audio,:bytes)""", asset)
                db.execute("""INSERT INTO revisions VALUES(?,?,?,?,?)
                           ON CONFLICT(source,revision,fingerprint) DO UPDATE SET asset=excluded.asset,route=excluded.route""",
                           (row["source"], row["revision"], asset["hash"], fingerprint, route))
        if row["asset"] and row["asset"] != asset["hash"]:
            with connect(self.root, True) as db:
                db.execute("DELETE FROM job_stories WHERE job=?", (row["id"],))
                db.execute("UPDATE jobs SET analysis=NULL WHERE id=?", (row["id"],))
        with connect(self.root, True) as db:
            db.execute("UPDATE jobs SET asset=? WHERE id=?", (asset["hash"], row["id"]))
        return asset

    def retained_asset(self, row, fingerprint):
        with connect(self.root) as db:
            saved = db.execute("""SELECT a.* FROM revisions r JOIN assets a ON a.hash=r.asset
                               WHERE r.source=? AND r.revision=? AND r.fingerprint=?""",
                               (row["source"], row["revision"], fingerprint)).fetchone()
        if saved and (self.root / saved["path"]).is_file() and media.checksum(self.root / saved["path"]) == saved["hash"]:
            return dict(saved)

    def analyze(self, job, asset, snapshot, settings, update):
        method = settings["method"]
        experimental = method not in ("baseline", "semantic")
        version = context_judge.version(method, job) if experimental else analysis.version(method)
        with connect(self.root) as db:
            cached = db.execute("SELECT * FROM analyses WHERE asset=? AND version=?",
                                (asset["hash"], version)).fetchone()
        if cached:
            analysis_id, result = cached["id"], json.loads(cached["data"])
            update("Reused completed analysis")
        else:
            result = (context_judge.analyze(self.root, asset, snapshot, settings, job, self.connections, update)
                      if experimental else analysis.analyze(self.root, asset, method, version, update))
            with connect(self.root, True) as db:
                analysis_id = db.execute("INSERT INTO analyses(asset,version,data) VALUES(?,?,?)",
                                         (asset["hash"], version, json.dumps(result))).lastrowid
        with connect(self.root, True) as db:
            old = db.execute("SELECT analysis FROM jobs WHERE id=?", (job,)).fetchone()["analysis"]
            if old and old != analysis_id:
                db.execute("DELETE FROM job_stories WHERE job=?", (job,))
            db.execute("UPDATE jobs SET analysis=? WHERE id=?", (analysis_id, job))
        return analysis_id, result

    def preview(self, job, source, asset, analysis_id, candidate, rank, total, update):
        plan = editorial.plan(candidate, asset["audio"])
        if candidate.get("context"):
            plan["judgment_version"] = f"job-{job}-analysis-{analysis_id}"
        key = editorial.plan_identity(plan)
        with connect(self.root, True) as db:
            db.execute("""INSERT OR IGNORE INTO stories(source,asset,analysis,plan_key,plan) VALUES(?,?,?,?,?)""",
                       (source, asset["hash"], analysis_id, key, json.dumps(plan)))
            story = db.execute("SELECT id FROM stories WHERE source=? AND asset=? AND plan_key=?",
                               (source, asset["hash"], key)).fetchone()["id"]
        update(f"Verifying preview {rank} of {total}")
        path = render.render_preview(self.root, asset, story, plan)
        if candidate.get("context"):
            render.render_context(self.root, asset, story, plan)
        with connect(self.root, True) as db:
            db.execute("UPDATE stories SET preview=? WHERE id=?", (path, story))
            db.execute("INSERT OR REPLACE INTO job_stories VALUES(?,?,?)", (job, story, rank))


class JobStopped(Exception):
    pass


def workspace_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "factory-instance.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        lock()
    except OSError:
        handle.close()
        raise RuntimeError("This factory workspace already has a running instance.") from None
    return handle
