import json
import tempfile
import unittest
from pathlib import Path

from factory import operator_workspace, portal_settings, store


class PublicSetupTests(unittest.TestCase):
    def test_fresh_workspace_queues_an_owned_local_recording(self):
        with tempfile.TemporaryDirectory(prefix="stream-clipping-factory-disposable-first-run-") as directory:
            root = Path(directory)
            store.initialize(root,Path("docs/VIDEO-TABLE.md"))
            movie = root/"recording.mp4"
            movie.write_bytes(b"test media placeholder")
            with store.connect(root,True) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0],0)
                source,_ = operator_workspace.save(db,{"link":str(movie),"title":"First recording"})
                self.assertEqual(str(movie),db.execute("SELECT location FROM sources WHERE id=?",(source,)).fetchone()[0])
                job,created = portal_settings.start(db,source,{"hosted":"on"})
                self.assertTrue(created)
                settings,snapshot = db.execute("SELECT settings,snapshot FROM jobs WHERE id=?",(job,)).fetchone()
                self.assertTrue(json.loads(settings)["full_replay"])
                self.assertEqual(json.loads(snapshot)["location"],str(movie))
                self.assertIn("First recording",operator_workspace.sources(db,root,"test-token",{},None))
