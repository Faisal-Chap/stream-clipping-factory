import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import jobs, portal_settings, source_queue, store


class SourceQueueTests(unittest.TestCase):
    def test_scheduler_resolves_legacy_saved_links_in_place(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-legacy-link-") as folder:
            root = Path(folder)
            store.initialize(root,Path("tests/fixtures/VIDEO-TABLE.md"))
            url = "https://www.youtube.com/watch?v=abcdefghijk"
            with store.connect(root,True) as db:
                db.execute("UPDATE sources SET state='archived'")
                source,_ = store.save_source(db,{"link":url,"priority":1})
            with patch.object(source_queue,"resolve",side_effect=ValueError("YouTube requires sign-in to verify this replay.")):
                source_queue.resolve_pending(root,1,1080)
            with store.connect(root) as db:
                row = db.execute("SELECT failures,detail FROM source_queue WHERE source=?",(source,)).fetchone()
                self.assertEqual(tuple(row),(1,"YouTube requires sign-in to verify this replay."))
            with patch.object(source_queue,"resolve",return_value=(url,"youtube",{
                    "id":"abcdefghijk","channel_id":"UC"+"a"*22,"title":"Fixture","duration":3600,
                    "live_status":"was_live"},"ready")):
                source_queue.resolve_pending(root,121,1080)
            with store.connect(root) as db:
                self.assertEqual(db.execute("SELECT readiness FROM source_queue WHERE source=?",(source,)).fetchone()[0],"ready")
                self.assertEqual(db.execute("SELECT source FROM streamer_streams").fetchone()[0],source)

    def test_worker_starts_and_claims_after_starting_readiness_and_release_threads(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-worker-boot-") as folder:
            root = Path(folder)
            store.initialize(root,Path("tests/fixtures/VIDEO-TABLE.md"))
            with store.connect(root,True) as db:
                source,_ = store.save_source(db,{"link":"https://youtu.be/abcdefghijk"})
                expected = store.enqueue(db,source)
            held = threading.Event()
            worker = jobs.Worker(root)
            received = []
            def process(row):
                received.append(row["id"])
                held.set()
            worker.process = process
            worker.start()
            try:
                self.assertTrue(held.wait(6))
                self.assertEqual(received,[expected])
            finally:
                worker.shutdown.set()
                worker.wake.set()
                worker.join(6)

    def test_live_replay_backoff_alias_and_replacement_preserve_identity(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-source-queue-") as folder:
            root = Path(folder)
            store.initialize(root,Path("tests/fixtures/VIDEO-TABLE.md"))
            video = "abcdefghijk"
            url = f"https://www.youtube.com/watch?v={video}"
            live = {"id":video,"live_status":"is_live","channel_id":"UC"+"a"*22,"title":"Fixture broadcast"}
            fields = {"name":"Fixture creator","game":"GTA VI"}
            with store.connect(root,True) as db:
                source,creator = source_queue.attach(db,fields,(url,"youtube",live,"waiting"))
                self.assertEqual(source_queue.link(f"https://youtu.be/{video}?t=30"),(url,"youtube"))
                duplicate,_ = source_queue.attach(db,{},(url,"youtube",live,"waiting"))
                self.assertEqual(source,duplicate)
                self.assertNotIn(source,[r["id"] for r in portal_settings.eligible(db)])
                db.execute("UPDATE source_queue SET next_check=0 WHERE source=?",(source,))
            self.assertTrue(source_queue.check_one(root,now=1,resolve_replay=lambda *_:{"id":video,"audio":[],"video":[]}))
            self.assertFalse(source_queue.check_one(root,now=120,resolve_replay=self.fail))
            self.assertTrue(source_queue.check_one(root,now=121,resolve_replay=lambda *_:
                            {"id":video,"audio":[{}],"video":[{}],"duration":1234}))
            with store.connect(root,True) as db:
                self.assertIn(source,[r["id"] for r in portal_settings.eligible(db)])
                replay = dict(live,id="bbbbbbbbbbb",live_status="was_live",duration=1200)
                replacement,_ = source_queue.attach(db,{"previous":str(source)},
                    ("https://www.youtube.com/watch?v=bbbbbbbbbbb","youtube",replay,"ready"))
                self.assertNotEqual(source,replacement)
                self.assertEqual(db.execute("SELECT state FROM sources WHERE id=?",(source,)).fetchone()[0],"archived")
                self.assertEqual(db.execute("SELECT previous FROM source_queue WHERE source=?",
                                            (replacement,)).fetchone()[0],source)
                store.set_source_state(db,source,"queued")
                self.assertIn(source,[r["id"] for r in portal_settings.eligible(db)])
