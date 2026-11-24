import json
import sqlite3
from contextlib import contextmanager

from .library import identify, starter_rows

DEFAULTS = {"method": "baseline", "max_previews": 5,
            "remote_minutes": 10, "remote_height": 360, "remote_mib": 120,
            "hosted": False, "sail_tier": "balanced", "provider_cap_cents": 25}
PHASE_2_SCHEMA = """
CREATE TABLE provider_connections (
 provider TEXT PRIMARY KEY, reference TEXT, revision INTEGER NOT NULL DEFAULT 0,
 verified TEXT, capability TEXT NOT NULL DEFAULT '', verified_tier TEXT,
 error TEXT NOT NULL DEFAULT ''
);
INSERT INTO provider_connections(provider) VALUES('google'),('sail');
CREATE TABLE provider_capabilities (
 provider TEXT NOT NULL REFERENCES provider_connections, reference TEXT NOT NULL,
 tier TEXT NOT NULL, capability TEXT NOT NULL, verified TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(provider,reference,tier)
);
CREATE TABLE provider_probes (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL REFERENCES provider_connections,
 reference TEXT NOT NULL, tier TEXT, replacement INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL, response_id TEXT,
 next_try REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE provider_calls (
 id TEXT PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs, provider TEXT NOT NULL,
 reference TEXT NOT NULL, response_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 reservation_micros INTEGER NOT NULL, usage TEXT, estimate_micros INTEGER, result TEXT,
 next_try REAL NOT NULL DEFAULT 0
);
CREATE TABLE blind_pairs (
 id TEXT PRIMARY KEY, story_a INTEGER REFERENCES stories, story_b INTEGER REFERENCES stories,
 job_a INTEGER NOT NULL REFERENCES jobs, job_b INTEGER NOT NULL REFERENCES jobs,
 rank INTEGER NOT NULL, verdict TEXT, reason TEXT, note TEXT,
 reviewed TEXT
);
"""
BLIND_ASSESSMENT_SCHEMA = """
CREATE TABLE blind_assessments (
 pair TEXT PRIMARY KEY REFERENCES blind_pairs,
 usable_a INTEGER NOT NULL, usable_b INTEGER NOT NULL,
 opening_a TEXT NOT NULL, payoff_a TEXT NOT NULL, phone_a INTEGER NOT NULL,
 opening_b TEXT NOT NULL, payoff_b TEXT NOT NULL, phone_b INTEGER NOT NULL
);
"""
SCHEMA = """
CREATE TABLE sources (
 id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, link TEXT NOT NULL,
 creator TEXT NOT NULL, game TEXT NOT NULL, location TEXT NOT NULL, notes TEXT NOT NULL,
 priority INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE assets (
 hash TEXT PRIMARY KEY, path TEXT NOT NULL, duration REAL NOT NULL, audio INTEGER NOT NULL,
 bytes INTEGER NOT NULL
);
CREATE TABLE revisions (
 source INTEGER REFERENCES sources, revision INTEGER, asset TEXT REFERENCES assets,
 fingerprint TEXT NOT NULL, route TEXT NOT NULL, PRIMARY KEY(source,revision,fingerprint)
);
CREATE TABLE analyses (
 id INTEGER PRIMARY KEY, asset TEXT REFERENCES assets, version TEXT NOT NULL,
 data TEXT NOT NULL, UNIQUE(asset,version)
);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, source INTEGER NOT NULL REFERENCES sources,
 revision INTEGER NOT NULL, snapshot TEXT NOT NULL, settings TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'Acquire',
 progress TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 stop INTEGER NOT NULL DEFAULT 0, asset TEXT REFERENCES assets, analysis INTEGER REFERENCES analyses,
 attempt INTEGER NOT NULL DEFAULT 0, timings TEXT NOT NULL DEFAULT '{}',
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE stories (
 id INTEGER PRIMARY KEY, source INTEGER NOT NULL REFERENCES sources,
 asset TEXT NOT NULL REFERENCES assets, analysis INTEGER NOT NULL REFERENCES analyses,
 plan_key TEXT NOT NULL, plan TEXT NOT NULL, preview TEXT,
 decision TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
 UNIQUE(source,asset,plan_key)
);
CREATE TABLE job_stories (
 job INTEGER REFERENCES jobs, story INTEGER REFERENCES stories, rank INTEGER NOT NULL,
 PRIMARY KEY(job,story)
);
CREATE TABLE settings (
 version INTEGER PRIMARY KEY, data TEXT NOT NULL, actor TEXT NOT NULL,
 changed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE metadata (key TEXT PRIMARY KEY);
"""


STREAMER_SCHEMA = """
CREATE TABLE streamer_creators (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, game TEXT NOT NULL, credit TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, camera TEXT
);
CREATE TABLE streamer_accounts (
 id INTEGER PRIMARY KEY, creator INTEGER NOT NULL REFERENCES streamer_creators,
 platform TEXT NOT NULL, platform_id TEXT NOT NULL, url TEXT NOT NULL,
 checked TEXT, UNIQUE(platform,platform_id)
);
CREATE TABLE streamer_streams (
 id INTEGER PRIMARY KEY, account INTEGER NOT NULL REFERENCES streamer_accounts,
 source INTEGER NOT NULL UNIQUE REFERENCES sources, platform_id TEXT,
 title TEXT NOT NULL, live_status TEXT NOT NULL, chat_path TEXT,
 UNIQUE(account,platform_id)
);
CREATE TABLE streamer_discovery (
 account INTEGER NOT NULL REFERENCES streamer_accounts, video TEXT NOT NULL, title TEXT NOT NULL,
 PRIMARY KEY(account,video)
);
"""

STREAMER_EDITION_SCHEMA = """
CREATE TABLE streamer_editions (
 id TEXT PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs, candidate TEXT NOT NULL,
 plan TEXT NOT NULL,
 qualification TEXT NOT NULL CHECK(qualification IN ('candidate','qualified','rejected')),
 render_state TEXT NOT NULL CHECK(render_state IN ('held','queued','rendering','exported','failed')),
 qa TEXT, error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX streamer_editions_run ON streamer_editions(job,candidate);
"""

STREAMER_BLIND_SCHEMA = """
CREATE TABLE streamer_edition_ballots (
 edition TEXT NOT NULL REFERENCES streamer_editions, ballot TEXT NOT NULL,
 version_slot TEXT NOT NULL CHECK(version_slot IN ('a','b')),
 verdict TEXT NOT NULL CHECK(verdict IN ('a','b','tie','both_weak')),
 observations TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(edition,ballot)
);
"""

FACTORY_EVENT_SCHEMA = """
CREATE TABLE factory_events (
 id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs,
 attempt INTEGER NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL,
 utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 item TEXT NOT NULL DEFAULT '', covered_seconds REAL, total_seconds REAL,
 bytes INTEGER, reserved_micros INTEGER, error_class TEXT DEFAULT '', retry_utc TEXT
);
CREATE INDEX factory_events_job ON factory_events(job,id);
CREATE TABLE factory_controls (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), paused INTEGER NOT NULL DEFAULT 0,
 zone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
);
INSERT INTO factory_controls(singleton) VALUES(1);
"""

EDITION_FEEDBACK_SCHEMA = """
CREATE TABLE streamer_edition_feedback (
 id INTEGER PRIMARY KEY, edition TEXT NOT NULL REFERENCES streamer_editions,
 verdict TEXT NOT NULL CHECK(verdict IN ('accepted','rework','rejected')),
 reason TEXT NOT NULL, utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX streamer_edition_feedback_edition ON streamer_edition_feedback(edition,id);
"""

PORTAL_SCHEMA = """
CREATE TABLE portal_settings (
 id INTEGER PRIMARY KEY, section TEXT NOT NULL, data TEXT NOT NULL,
 saved TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX portal_settings_section ON portal_settings(section,id);
CREATE TABLE portal_creator_policy (
 creator INTEGER PRIMARY KEY REFERENCES streamer_creators,
 style TEXT NOT NULL DEFAULT '{"mode":"inherit","ids":[]}',
 daily_limit INTEGER, platforms TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE portal_admissions (
 day TEXT NOT NULL, source INTEGER NOT NULL REFERENCES sources,
 creator INTEGER NOT NULL REFERENCES streamer_creators,
 first_job INTEGER NOT NULL REFERENCES jobs,
 PRIMARY KEY(day,source)
);
CREATE TABLE portal_source_metadata (
 source INTEGER PRIMARY KEY REFERENCES sources, duration REAL
);
CREATE TABLE template_designs (
 id TEXT PRIMARY KEY, original TEXT NOT NULL, active_revision INTEGER, archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE template_revisions (
 template TEXT NOT NULL REFERENCES template_designs, revision INTEGER NOT NULL,
 recipe TEXT NOT NULL, saved TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(template,revision)
);
CREATE TABLE template_assignments (
 job INTEGER NOT NULL REFERENCES jobs, story TEXT NOT NULL,
 slot INTEGER NOT NULL, template TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(job,story), UNIQUE(job,slot),
 FOREIGN KEY(template,revision) REFERENCES template_revisions(template,revision)
);
CREATE TABLE indiepost_connection (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), reference TEXT, last_test TEXT,
 status TEXT NOT NULL DEFAULT 'Not configured', accounts TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO indiepost_connection(singleton) VALUES(1);
"""

DONE_SCHEMA = """
CREATE TABLE factory_focus (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), job INTEGER REFERENCES jobs
);
INSERT INTO factory_focus(singleton) VALUES(1);
CREATE TABLE done_stories (
 id INTEGER PRIMARY KEY, source INTEGER NOT NULL REFERENCES sources, source_key TEXT NOT NULL,
 event_key TEXT NOT NULL, proof REAL NOT NULL, proof_from REAL NOT NULL, proof_to REAL NOT NULL,
 source_title TEXT NOT NULL, source_url TEXT NOT NULL, creator TEXT NOT NULL,
 completed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_key,event_key,proof)
);
CREATE INDEX done_source_proof ON done_stories(source_key,proof);
CREATE TABLE done_matches (
 job INTEGER NOT NULL REFERENCES jobs, candidate TEXT NOT NULL, story INTEGER NOT NULL REFERENCES done_stories,
 provenance TEXT NOT NULL, PRIMARY KEY(job,candidate)
);
CREATE TABLE done_editions (
 edition TEXT PRIMARY KEY REFERENCES streamer_editions, story INTEGER NOT NULL REFERENCES done_stories,
 digest TEXT NOT NULL, completed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX done_story_editions ON done_editions(story,completed);
CREATE TABLE done_delivery_intents (
 id INTEGER PRIMARY KEY, story INTEGER NOT NULL REFERENCES done_stories,
 edition TEXT NOT NULL REFERENCES done_editions, platform TEXT NOT NULL, account TEXT NOT NULL,
 UNIQUE(story,platform,account)
);
CREATE TABLE done_delivery_receipts (
 id INTEGER PRIMARY KEY, intent INTEGER NOT NULL REFERENCES done_delivery_intents,
 edition TEXT NOT NULL REFERENCES done_editions, platform TEXT NOT NULL, account TEXT NOT NULL,
 status TEXT NOT NULL, remote_id TEXT, remote_url TEXT, occurred TEXT,
 evidence TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(platform,account,remote_id)
);
"""

INDIEPOST_SCHEMA = """
CREATE TABLE indiepost_deliveries (
 intent INTEGER PRIMARY KEY REFERENCES done_delivery_intents,
 account_name TEXT NOT NULL, workspace TEXT NOT NULL, payload TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE, slot TEXT NOT NULL,
 state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 media_id TEXT, group_id TEXT UNIQUE, first_submit TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SOURCE_QUEUE_SCHEMA = """
CREATE TABLE source_queue (
 source INTEGER PRIMARY KEY REFERENCES sources, previous INTEGER REFERENCES sources,
 platform TEXT NOT NULL, provenance TEXT NOT NULL, readiness TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', checked TEXT, next_check REAL NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX source_queue_check ON source_queue(readiness,next_check);
INSERT INTO source_queue(source,platform,provenance,readiness)
 SELECT s.id,CASE WHEN s.link LIKE 'https://www.youtube.com/watch?%' THEN 'youtube' ELSE 'file' END,
 'Existing creator attachment',
 CASE WHEN st.live_status IN ('is_live','is_upcoming','post_live') AND s.location='' THEN 'waiting' ELSE 'ready' END
 FROM sources s JOIN streamer_streams st ON st.source=s.id;
"""

LOCAL_RELEASE_SCHEMA = """
CREATE TABLE factory_release_days (
 day TEXT PRIMARY KEY, zone TEXT NOT NULL, target INTEGER NOT NULL
);
CREATE TABLE factory_release_stories (
 story INTEGER PRIMARY KEY REFERENCES done_stories, edition TEXT NOT NULL REFERENCES done_editions,
 day TEXT NOT NULL REFERENCES factory_release_days, created REAL NOT NULL, submitted REAL
);
CREATE TABLE factory_release_destinations (
 intent INTEGER PRIMARY KEY REFERENCES done_delivery_intents,
 account_name TEXT NOT NULL, workspace TEXT NOT NULL, visibility TEXT NOT NULL,
 payload TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'local',
 attempts INTEGER NOT NULL DEFAULT 0, attempted REAL, check_after REAL NOT NULL DEFAULT 0,
 media_id TEXT, group_id TEXT UNIQUE, confirmed REAL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX factory_release_pending ON factory_release_destinations(state,check_after);
CREATE TABLE factory_release_refill (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 active INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
);
INSERT INTO factory_release_refill(singleton) VALUES(1);
CREATE TABLE factory_release_artifacts (
 path TEXT PRIMARY KEY, story INTEGER NOT NULL REFERENCES done_stories,
 edition TEXT NOT NULL REFERENCES done_editions, digest TEXT NOT NULL,
 bytes INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'owned', changed REAL NOT NULL
);
"""

RECLAMATION_SCHEMA = """
ALTER TABLE factory_release_artifacts ADD COLUMN protected INTEGER NOT NULL DEFAULT 0;
CREATE TABLE factory_release_editor_leases (
 job INTEGER NOT NULL REFERENCES jobs, candidate TEXT NOT NULL, until REAL NOT NULL, PRIMARY KEY(job,candidate)
);
"""


@contextmanager
def connect(root, write=False):
    db = sqlite3.connect(root / "factory.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        if write:
            db.execute("BEGIN IMMEDIATE")
        with db:
            yield db
    finally:
        db.close()


def initialize(root, table):
    root.mkdir(parents=True, exist_ok=True)
    with connect(root) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=1; COMMIT;")
            version = 1
        if version == 1:
            db.executescript("BEGIN IMMEDIATE;\n" + PHASE_2_SCHEMA + "\nPRAGMA user_version=2; COMMIT;")
            version = 2
        if version == 2:
            db.executescript("BEGIN IMMEDIATE;\n" + BLIND_ASSESSMENT_SCHEMA + "\nPRAGMA user_version=3; COMMIT;")
            version = 3
        if version == 3:
            db.executescript("BEGIN IMMEDIATE;\n" + STREAMER_SCHEMA + "\nPRAGMA user_version=4; COMMIT;")
            version = 4
        if version == 4:
            db.executescript("BEGIN IMMEDIATE;\n" + STREAMER_EDITION_SCHEMA + "\nPRAGMA user_version=5; COMMIT;")
            version = 5
        if version == 5:
            db.executescript("BEGIN IMMEDIATE;\n" + STREAMER_BLIND_SCHEMA + "\nPRAGMA user_version=6; COMMIT;")
            version = 6
        if version == 6:
            db.executescript("BEGIN IMMEDIATE;\n" + FACTORY_EVENT_SCHEMA + "\nPRAGMA user_version=7; COMMIT;")
            version = 7
        if version == 7:
            db.executescript("BEGIN IMMEDIATE;\n" + EDITION_FEEDBACK_SCHEMA + "\nPRAGMA user_version=8; COMMIT;")
            version = 8
        if version == 8:
            db.executescript("BEGIN IMMEDIATE;\n" + PORTAL_SCHEMA + "\nPRAGMA user_version=9; COMMIT;")
            version = 9
        if version == 9:
            db.executescript("BEGIN IMMEDIATE;\n" + DONE_SCHEMA + "\nPRAGMA user_version=10; COMMIT;")
            version = 10
        if version == 10:
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE done_release_holds (
                    story INTEGER NOT NULL REFERENCES done_stories,
                    edition TEXT NOT NULL REFERENCES done_editions,
                    reason TEXT NOT NULL,
                    recorded TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(story,edition)
                );
                PRAGMA user_version=11;
                COMMIT;""")
            version = 11
        if version == 11:
            db.executescript("BEGIN IMMEDIATE;\n" + INDIEPOST_SCHEMA + "\nPRAGMA user_version=12; COMMIT;")
            version = 12
        if version == 12:
            db.executescript("BEGIN IMMEDIATE;\n" + SOURCE_QUEUE_SCHEMA + "\nPRAGMA user_version=13; COMMIT;")
            version = 13
        if version == 13:
            db.executescript("BEGIN IMMEDIATE;\n" + LOCAL_RELEASE_SCHEMA + "\nPRAGMA user_version=14; COMMIT;")
            version = 14
        if version == 14:
            db.executescript("BEGIN IMMEDIATE;\n" + RECLAMATION_SCHEMA + "\nPRAGMA user_version=15; COMMIT;")
            version = 15
        if version == 15:
            db.executescript("""BEGIN IMMEDIATE;
                ALTER TABLE sources ADD COLUMN tracking_title TEXT NOT NULL DEFAULT '';
                PRAGMA user_version=16;
                COMMIT;""")
            version = 16
        if version == 16:
            db.executescript("""BEGIN IMMEDIATE;
                ALTER TABLE factory_release_artifacts ADD COLUMN removal_method TEXT NOT NULL DEFAULT 'automatic';
                CREATE INDEX done_delivery_edition ON done_delivery_intents(edition);
                CREATE INDEX factory_release_artifact_edition ON factory_release_artifacts(edition,state);
                PRAGMA user_version=17;
                COMMIT;""")
            version = 17
        if version == 17:
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE flashdata_profiles (
                    video TEXT PRIMARY KEY, profile TEXT, fetched REAL,
                    refresh_until REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE flashdata_usage (
                    id TEXT PRIMARY KEY, video TEXT NOT NULL, kind TEXT NOT NULL,
                    submitted REAL NOT NULL, reserved REAL NOT NULL, credits REAL,
                    bytes INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL
                );
                CREATE INDEX flashdata_usage_month ON flashdata_usage(submitted);
                CREATE TABLE flashdata_assets (
                    video TEXT NOT NULL, purpose TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                    digest TEXT NOT NULL, asset TEXT NOT NULL,
                    PRIMARY KEY(video,purpose,start,end,digest)
                );
                PRAGMA user_version=18;
                COMMIT;""")
            version = 18
        if version != 18:
            raise RuntimeError(f"Unsupported database schema {version}")
    with connect(root, True) as db:
        from . import portal_templates
        portal_templates.seed(db)
        db.execute("INSERT OR IGNORE INTO settings VALUES(1,?,'default',CURRENT_TIMESTAMP)",
                   (json.dumps(DEFAULTS),))
        if not db.execute("SELECT 1 FROM metadata WHERE key='starter-imported'").fetchone():
            import_table(db, table)
            db.execute("INSERT INTO metadata VALUES('starter-imported')")
        from . import portal_settings
        portal_settings.backfill_admissions(db)
        db.execute("""INSERT INTO factory_events(job,attempt,stage,kind,item,error_class)
                      SELECT id,attempt,stage,'interrupted','Worker restarted during this stage','Interrupted'
                      FROM jobs WHERE state='running'""")
        db.execute("""UPDATE jobs SET state='queued',error='',progress='Resuming saved audio-first checkpoints.'
                   WHERE state='running' AND stop=0 AND json_extract(settings,'$.acquisition')='audio-first'
                   AND EXISTS (SELECT 1 FROM sources s WHERE s.id=jobs.source
                               AND s.revision=jobs.revision AND s.state='queued')""")
        db.execute("""UPDATE jobs SET state='stopped', stop=0,
                   progress='Interrupted. Resume from the last verified stage.' WHERE state='running'""")
        db.execute("UPDATE streamer_editions SET render_state='queued' WHERE render_state='rendering'")


def save_source(db, data, source=None):
    identity, link, _ = identify(data["link"])
    location = data.get("location", "").strip()
    if location:
        _, location, _ = identify(location)
    priority = int(data.get("priority") or 100)
    if not 1 <= priority <= 9999:
        raise ValueError("Priority must be between 1 and 9999.")
    state = data.get("state", "queued")
    if state not in ("queued", "paused", "archived"):
        raise ValueError("Invalid source state.")
    values = (identity, link, data.get("creator", "").strip(), data.get("game", "").strip(),
              location, data.get("notes", "").strip(), priority, state)
    existing = db.execute("SELECT id FROM sources WHERE identity=?", (identity,)).fetchone()
    if existing and existing["id"] != source:
        return existing["id"], False
    if source is None:
        result = db.execute("""INSERT INTO sources
            (identity,link,creator,game,location,notes,priority,state) VALUES(?,?,?,?,?,?,?,?)""", values)
        return result.lastrowid, True
    old = db.execute("SELECT identity,location FROM sources WHERE id=?", (source,)).fetchone()
    if old["identity"] != identity and db.execute("SELECT 1 FROM source_queue WHERE source=?", (source,)).fetchone():
        raise ValueError("Replace this link from the Sources queue to preserve its recording history.")
    if old["identity"] != identity or old["location"] != location:
        db.execute("""UPDATE jobs SET state='stopped',progress='Source media changed. Start the current revision.'
                   WHERE source=? AND state='queued'""", (source,))
        db.execute("UPDATE jobs SET stop=1 WHERE source=? AND state='running'", (source,))
    set_source_state(db, source, state)
    db.execute("""UPDATE sources SET revision=revision+(identity<>? OR location<>?),
               identity=?,link=?,creator=?,game=?,location=?,notes=?,priority=?,state=? WHERE id=?""",
               (identity, location, *values, source))
    return source, True


def import_table(db, path):
    return sum(save_source(db, row)[1] for row in starter_rows(path))


def set_source_state(db, source, state):
    db.execute("UPDATE sources SET state=? WHERE id=?", (state, source))
    if state != "queued":
        db.execute("UPDATE jobs SET state='stopped',progress='Source paused or archived.' "
                   "WHERE source=? AND state='queued'", (source,))
        db.execute("UPDATE jobs SET stop=1 WHERE source=? AND state='running'", (source,))


def enqueue(db, source, fresh=False, overrides=None):
    row = db.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()
    if not row or row["state"] != "queued":
        raise ValueError("Resume the source before starting a job.")
    streamer = (overrides or {}).get("method") == "streamer"
    recent = db.execute("""SELECT id,state FROM jobs WHERE source=? AND revision=? AND
                        state IN ('queued','running','failed','stopped','complete')
                        AND (COALESCE(json_extract(settings,'$.method'),'baseline')='streamer')=?
                        ORDER BY (state IN ('queued','running')) DESC,id DESC LIMIT 1""",
                        (source, row["revision"], streamer)).fetchone()
    if recent and (recent["state"] in ("queued", "running")
                   or (not fresh and recent["state"] in ("stopped", "failed"))):
        resume(db, recent["id"])
        return recent["id"]
    latest = db.execute("SELECT data FROM settings ORDER BY version DESC").fetchone()[0]
    snapshot = dict(DEFAULTS, **json.loads(latest))
    snapshot.update(overrides or {})
    floor = db.execute("""SELECT COALESCE(MAX(CAST(substr(key,length('factory-run-id-floor:')+1) AS INTEGER)),0)
                          FROM metadata WHERE key LIKE 'factory-run-id-floor:%'""").fetchone()[0]
    identity = db.execute("""INSERT INTO jobs(id,source,revision,snapshot,settings)
                          VALUES((SELECT MAX(COALESCE(MAX(id),0),?)+1 FROM jobs),?,?,?,?)""",
                          (floor,source,row["revision"],
                           json.dumps(dict(row)),json.dumps(snapshot))).lastrowid
    db.execute("INSERT INTO factory_events(job,attempt,stage,kind,item) VALUES(?,0,'Acquire','queued','Entered queue')",
               (identity,))
    return identity


def resume(db, job):
    from . import streamer_transfer
    row = db.execute("""SELECT j.state,j.revision,j.settings,s.location,s.identity,s.revision AS current,s.state AS source_state
                     FROM jobs j JOIN sources s ON s.id=j.source WHERE j.id=?""", (job,)).fetchone()
    if not row or row["source_state"] != "queued" or row["revision"] != row["current"]:
        raise ValueError("Resume the source and start its current media revision from Library.")
    if not row["location"] and row["identity"].startswith("youtube:"):
        config = json.loads(row["settings"])
        if not config.get("full_replay") and config.get("coverage_start",0)>=streamer_transfer.REPLAY_LIMIT:
            raise ValueError(streamer_transfer.REPLAY_LIMIT_MESSAGE)
    changed = db.execute("UPDATE jobs SET state='queued', stop=0,error='',progress='Waiting for worker' "
                         "WHERE id=? AND state IN ('failed','stopped')", (job,))
    if changed.rowcount:
        db.execute("""INSERT INTO factory_events(job,attempt,stage,kind,item)
                      SELECT id,attempt,stage,'queued','Operator resumed job' FROM jobs WHERE id=?""", (job,))
