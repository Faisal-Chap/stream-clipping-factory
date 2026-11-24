import json
import tempfile
import unittest
from pathlib import Path

from factory import analysis, library, settings, store
from factory.jobs import Worker, workspace_lock

TABLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "VIDEO-TABLE.md"


class LibraryTests(unittest.TestCase):
    def test_youtube_identity_and_invalid_input(self):
        video = "fixture0001"
        identity = f"youtube:{video}"
        for link in (f"https://youtu.be/{video}?si=sample",
                     f"https://m.youtube.com/watch?v={video}&t=12",
                     f"https://www.youtube.com/live/{video}",
                     f"https://www.youtube.com/shorts/{video}"):
            self.assertEqual(library.identify(link)[0], identity)
        for value in ("", "broken input", "https://youtu.be/no", "file:relative", "ftp://example.com/video"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                library.identify(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gta-media-factory-disposable-store-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store.initialize(self.root, TABLE)

    def test_seed_preserves_owner_edits_and_settings_detect_conflict(self):
        with store.connect(self.root, True) as db:
            db.execute("UPDATE sources SET notes='Owner note',state='archived' WHERE id=1")
            config = {"version": "1", "method": "baseline", "max_previews": "2"}
            settings.save(db, config)
            with self.assertRaises(ValueError):
                settings.save(db, config)
        store.initialize(self.root, TABLE)
        with store.connect(self.root, True) as db:
            self.assertEqual(store.import_table(db, TABLE), 0)
            self.assertEqual(tuple(db.execute("SELECT notes,state FROM sources").fetchone()),
                             ("Owner note", "archived"))
            self.assertEqual(settings.get(db)["max_previews"], 2)

    def test_restart_recovers_active_job_and_keeps_snapshot(self):
        with store.connect(self.root, True) as db:
            job = store.enqueue(db, 1)
            self.assertEqual(store.enqueue(db, 1), job)
            db.execute("UPDATE jobs SET state='running',stage='Analyze',stop=1 WHERE id=?", (job,))
            settings.save(db, {"version": "1", "method": "baseline", "max_previews": "2"})
        store.initialize(self.root, TABLE)
        with store.connect(self.root, True) as db:
            row = db.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual((row["state"], row["stage"], row["stop"]), ("stopped", "Analyze", 0))
            store.resume(db, job)
            self.assertEqual(json.loads(row["settings"])["max_previews"], 5)
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "queued")

    def test_new_run_uses_updated_download_limits(self):
        with store.connect(self.root, True) as db:
            old = store.enqueue(db, 1)
            db.execute("UPDATE jobs SET state='failed' WHERE id=?", (old,))
            config = {"version": "1", "method": "baseline", "max_previews": "3",
                      "remote_minutes": "3", "remote_height": "240", "remote_mib": "25"}
            settings.save(db, config)
            new = store.enqueue(db, 1, fresh=True)
            self.assertNotEqual(old, new)
            self.assertEqual(store.enqueue(db, 1, fresh=True), new)
            previous = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?", (old,)).fetchone()[0])
            current = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?", (new,)).fetchone()[0])
            self.assertEqual((previous["remote_minutes"], previous["remote_height"], previous["remote_mib"]),
                             (10, 360, 120))
            self.assertEqual((current["remote_minutes"], current["remote_height"], current["remote_mib"]),
                             (3, 240, 25))

    def test_editing_media_stops_stale_queued_job(self):
        with store.connect(self.root, True) as db:
            old_job = store.enqueue(db, 1)
            row = dict(db.execute("SELECT * FROM sources").fetchone())
            row["location"] = "https://example.com/replacement.mp4"
            store.save_source(db, row, 1)
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "stopped")
            with self.assertRaises(ValueError):
                store.resume(db, old_job)
            self.assertNotEqual(store.enqueue(db, 1), old_job)

    def test_stop_resumes_verified_acquisition_and_reuses_analysis(self):
        from unittest.mock import patch

        master = self.root / "master"
        master.write_bytes(b"fixture")
        from factory.media import checksum
        digest = checksum(master)
        with store.connect(self.root, True) as db:
            job = store.enqueue(db, 1)
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
            row = db.execute("SELECT * FROM jobs").fetchone()

        worker = Worker(self.root)

        def acquisition(*_):
            with store.connect(self.root, True) as db:
                db.execute("UPDATE jobs SET stop=1 WHERE id=?", (job,))
            return dict(hash=digest, path="master", duration=30, audio=False, bytes=7), "youtube"

        with patch("factory.media.acquire", side_effect=acquisition) as acquire:
            worker.process(row)
            self.assertEqual(acquire.call_count, 1)
        store.initialize(self.root, TABLE)
        with store.connect(self.root, True) as db:
            stopped = db.execute("SELECT state,asset FROM jobs").fetchone()
            self.assertEqual(tuple(stopped), ("stopped", digest))
            store.resume(db, job)
            row = db.execute("SELECT * FROM jobs").fetchone()
        with patch("factory.media.acquire") as acquire, patch("factory.analysis.analyze", return_value={
            "ranked": [], "version": analysis.version("baseline")
        }) as analyze:
            worker.process(row)
            worker.process(row)
            acquire.assert_not_called()
            self.assertEqual(analyze.call_count, 1)
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "complete")

    def test_workspace_single_owner(self):
        with workspace_lock(self.root):
            with self.assertRaises(RuntimeError):
                workspace_lock(self.root)
        with workspace_lock(self.root):
            self.assertTrue((self.root / "factory-instance.lock").exists())


class RankTests(unittest.TestCase):
    def test_blackout_does_not_become_a_candidate_payoff(self):
        samples = [{"t": t, "change": 0.4, "audio": 0.1} for t in range(0, 45, 3)]
        rows = {r["time"]: r for r in analysis.rank(samples, 45, "baseline", [21])}
        self.assertIn("blackout", rows[21]["rejection"])
        self.assertIn("setup", rows[24]["rejection"])
        self.assertEqual(rows[15]["end"], 21)
        self.assertFalse(rows[15]["rejection"])

    def test_static_input_abstains(self):
        samples = [{"t": t, "change": 0, "audio": 0} for t in range(0, 45, 3)]
        selected, rejected = analysis.select(analysis.rank(samples, 45, "baseline"), 5)
        self.assertFalse(selected)
        self.assertTrue(rejected)

    def test_overlapping_windows_are_rejected(self):
        candidates = [{"start": start, "end": start+21, "rejection": ""} for start in (12, 18, 45)]
        chosen, rejected = analysis.select(candidates, 5)
        self.assertEqual([c["start"] for c in chosen], [12, 45])
        self.assertIn("Overlaps", rejected[0]["rejection"])
