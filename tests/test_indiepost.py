import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from factory import indiepost, media, portal_settings, provider_api, store
from test_shorts_release import make_portrait


class IndiePostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-indiepost-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root, Path("tests/fixtures/VIDEO-TABLE.md"))
        self.account = {"id": "gta:youtube", "workspace": "gta", "platform": "youtube", "name": "fixture"}
        self.settings = dict(portal_settings.SECTIONS["uploads"], enabled=True, accounts=[self.account["id"]],
                             release="scheduled", zone="UTC", window_start="00:00", window_end="00:00")
        self.vault = Mock()
        self.vault.get.return_value = "ip_fixture"
        self.edition = "a"*64
        with store.connect(self.root, True) as db:
            source, _ = store.save_source(db, {"link": "https://youtu.be/test0000001"})
            job = store.enqueue(db, source)
            creator = db.execute("INSERT INTO streamer_creators(name,game,credit) VALUES('Test','GTA V','Test')").lastrowid
            account = db.execute("""INSERT INTO streamer_accounts(creator,platform,platform_id,url)
                                    VALUES(?,'youtube','fixture','https://www.youtube.com/@fixture')""",
                                 (creator,)).lastrowid
            db.execute("INSERT INTO streamer_streams(account,source,title,live_status) VALUES(?,?,'Fixture','was_live')",
                       (account, source))
            db.execute("INSERT INTO portal_creator_policy(creator,platforms) VALUES(?,'[\"youtube\"]')", (creator,))
            db.execute("UPDATE indiepost_connection SET reference='fixture',accounts=?", (json.dumps([self.account]),))
            version = db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",
                                 (json.dumps(self.settings),)).lastrowid
            plan = {"title": "Test title", "post": "Test caption"}
            db.execute("""INSERT INTO streamer_editions(id,job,candidate,plan,qualification,render_state,qa)
                          VALUES(?,?,'c1-1',?,'qualified','exported','Final QA')""",
                       (self.edition, job, json.dumps(plan)))
            target = self.root/"streamer-runs"/str(job)/"c1-1"/"editions"/self.edition
            target.mkdir(parents=True)
            self.file = make_portrait(target/"portrait.mp4")
            self.story = db.execute("""INSERT INTO done_stories(source,source_key,event_key,proof,proof_from,
                                    proof_to,source_title,source_url,creator)
                                    VALUES(?,'fixture','test',1,0,2,'Fixture','','Test')""", (source,)).lastrowid
            db.execute("INSERT INTO done_editions(edition,story,digest) VALUES(?,?,?)",
                       (self.edition, self.story, media.checksum(self.file)))
        self.fields = dict(edition=self.edition, account=self.account["id"], username=self.account["name"],
                           version=str(version), confirm="on")

    def test_send_idempotent_replay_and_platform_receipt(self):
        with patch.object(indiepost, "Client", autospec=True) as make_client:
            client = make_client.return_value
            client.discover.return_value = [self.account]
            upload = {"method": "PUT"}
            client.init_upload.return_value = ("media-test", upload)
            client.create.side_effect = [provider_api.ProviderError("timeout", retry=1), "group-test"]
            client.status.return_value = {"status": "pending"}
            client.put.side_effect = lambda spec, stream, length: self.assertEqual(stream.read(), self.file.read_bytes())
            with self.assertRaisesRegex(ValueError, "confirm"):
                indiepost.send(self.root, self.vault, self.story, dict(self.fields, confirm=""))
            make_client.assert_not_called()
            message = indiepost.send(self.root, self.vault, self.story, self.fields)
            self.assertIn("accepted", message)
            self.assertEqual(client.create.call_count, 2)
            self.assertEqual(*client.create.call_args_list)
            body, request_id = client.create.call_args.args
            self.assertEqual(body["workspaceId"], "gta")
            self.assertEqual(body["mediaIds"], ["media-test"])
            self.assertEqual(body["youtube"], {"format": "short", "title": "Test title", "privacyStatus": "private"})
            self.assertGreater(datetime.fromisoformat(body["scheduledAt"]), datetime.now(timezone.utc))
            client.confirm.assert_called_once_with("media-test")
            with self.assertRaisesRegex(ValueError, "already has a delivery"):
                indiepost.send(self.root, self.vault, self.story, self.fields)
            with store.connect(self.root) as db:
                row = indiepost.deliveries(db, self.story)[0]
                self.assertEqual(row["request_id"], request_id)
                self.assertEqual(row["group_id"], "group-test")
                intent = row["intent"]
            self.assertIn("Scheduled", indiepost.refresh(self.root, self.vault, self.story, intent))
            client.status.return_value = {"status": "posted", "externalPostId": "youtube-test",
                                          "postedAt": "2026-11-21T08:00:00Z"}
            self.assertIn("Published", indiepost.refresh(self.root, self.vault, self.story, intent))
            indiepost.refresh(self.root, self.vault, self.story, intent)
            with store.connect(self.root) as db:
                receipt, = db.execute("SELECT * FROM done_delivery_receipts")
                self.assertEqual((receipt["edition"], receipt["remote_id"], receipt["status"]),
                                 (self.edition, "youtube-test", "published"))

    def test_pause_during_upload_and_recoverable_retry(self):
        with patch.object(indiepost, "Client", autospec=True) as make_client:
            client = make_client.return_value
            client.discover.return_value = [self.account]
            client.init_upload.return_value = ("media-test", None)

            def pause(_):
                with store.connect(self.root, True) as db:
                    db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",
                               (json.dumps(dict(self.settings, enabled=False)),))
            client.confirm.side_effect = pause
            self.assertIn("changed during upload", indiepost.send(self.root, self.vault, self.story, self.fields))
            client.create.assert_not_called()
            with store.connect(self.root, True) as db:
                self.assertEqual(indiepost.deliveries(db, self.story)[0]["state"], "failed")
                version = db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",
                                     (json.dumps(self.settings),)).lastrowid
            client.confirm.side_effect = None
            client.create.return_value = "group-test"
            self.assertIn("accepted", indiepost.send(self.root, self.vault, self.story,
                                                     dict(self.fields, version=str(version))))

    def test_tiktok_only_me_receipt_does_not_invent_a_platform_id(self):
        from factory import factory_done

        with store.connect(self.root, True) as db:
            intent = factory_done.account_intent(db, self.story, self.edition, "tiktok", "gta:tiktok")["id"]
            db.execute("""INSERT INTO indiepost_deliveries(intent,account_name,workspace,payload,request_id,slot,state,group_id)
                          VALUES(?,'fixture','gta',?,'private-test','2026-11-21T08:00:00+00:00','pending','group-private')""",
                       (intent, json.dumps({"tiktok": {"privacyLevel": "SELF_ONLY"}})))
        with patch.object(indiepost.Client, "status", return_value={
                "status": "posted", "externalPostId": None, "postedAt": "2026-11-21T08:00:00Z",
                "tiktok": {"privacyLevel": "SELF_ONLY"}}):
            self.assertIn("Posted · Only me", indiepost.refresh(self.root, self.vault, self.story, intent))
        with store.connect(self.root) as db:
            self.assertEqual(indiepost.deliveries(db, self.story)[0]["state"], "private_posted")
            self.assertIsNone(db.execute("SELECT * FROM done_delivery_receipts").fetchone())

    def test_signed_upload_uses_only_storage_headers_and_disallows_redirects(self):
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        spec = {"method": "PUT", "url": "https://"+"a"*32+".r2.cloudflarestorage.com/fixture?demo"}
        with patch.object(indiepost.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = context
            client = indiepost.Client("indiepost-fixture-credential")
            client.put(spec, b"abc", 3)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual((request.get_method(), request.data), ("PUT", b"abc"))
            self.assertEqual(dict(request.header_items()), {"Content-type": "video/mp4", "Content-length": "3"})
            self.assertIsInstance(build.call_args.args[0], provider_api.NoRedirect)
            with self.assertRaisesRegex(ValueError, "storage host"):
                client.put(dict(spec, url="https://127.0.0.1/upload"), b"abc", 3)
