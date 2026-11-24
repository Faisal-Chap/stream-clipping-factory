import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from factory import daily_release, factory_done, indiepost, media, portal_settings, provider_api, reclamation, source_queue, storage_page, store
from factory import operator_workspace, readiness
from test_shorts_release import make_portrait

TODAY = datetime(2026,11,21,8,tzinfo=timezone.utc).timestamp()


class DailyReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-local-release-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root,Path("tests/fixtures/VIDEO-TABLE.md"))
        self.accounts = [{"id":"gta:"+p,"platform":p,"workspace":"gta","name":"fixture-"+p}
                         for p in ("youtube","tiktok","instagram","facebook")]
        self.values = dict(portal_settings.SECTIONS["uploads"],enabled=True,local_release=True,release="immediate",
                           zone="UTC",spacing=0,account_daily=50,accounts=[a["id"] for a in self.accounts])
        with store.connect(self.root,True) as db:
            self.source,_ = store.save_source(db,{"link":"https://youtu.be/test0000001"})
            self.job=store.enqueue(db,self.source)
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(self.job,))
            self.creator = db.execute("INSERT INTO streamer_creators(name,game,credit) VALUES('Fixture','GTA V','Fixture')").lastrowid
            owner = db.execute("""INSERT INTO streamer_accounts(creator,platform,platform_id,url)
                                  VALUES(?,'youtube','fixture-media','https://www.youtube.com/@fixture')""",
                               (self.creator,)).lastrowid
            db.execute("INSERT INTO streamer_streams(account,source,title,live_status) VALUES(?,?,'Fixture','was_live')",
                       (owner,self.source))
            db.execute("INSERT INTO portal_creator_policy(creator,platforms) VALUES(?,?)",
                       (self.creator,json.dumps([a["platform"] for a in self.accounts])))
            db.execute("UPDATE indiepost_connection SET reference='fixture',accounts=?",(json.dumps(self.accounts),))
            db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",(json.dumps(self.values),))

    def add_stories(self, n, start=1, playable=False, automated=False):
        with store.connect(self.root,True) as db:
            for i in range(start,start+n):
                edition = f"{i:064x}"
                candidate = f"c1-{i}"
                plan = {"title":f"Story {i}","post":"Fixture"}
                if automated:
                    plan.update(story_v4=True,owner_viewing=True,origin="manual")
                db.execute("""INSERT INTO streamer_editions(id,job,candidate,plan,qualification,render_state,qa)
                              VALUES(?,?,?,?,'qualified','exported',?)""",
                           (edition,self.job,candidate,json.dumps(plan),None if automated else "Final QA"))
                story = db.execute("""INSERT INTO done_stories(source,source_key,event_key,proof,proof_from,
                                   proof_to,source_title,source_url,creator)
                                   VALUES(?,'fixture',?,?,0,100,'Fixture','','Fixture')""",
                                   (self.source,str(i),i)).lastrowid
                if playable:
                    target = self.root/"streamer-runs"/str(self.job)/candidate/"editions"/edition
                    target.mkdir(parents=True)
                    self.movie = (make_portrait(target/"portrait.mp4",1800,2880000) if automated else
                                  make_portrait(target/"portrait.mp4"))
                    digest = media.checksum(self.movie)
                else:
                    digest = "fixture"
                db.execute("INSERT INTO done_editions(edition,story,digest) VALUES(?,?,?)",(edition,story,digest))

    def qualified_fixture(self, db, _root, _story, edition, *, automatic=False):
        return db.execute("""SELECT e.*,d.digest,? AS creator FROM streamer_editions e
                            JOIN done_editions d ON d.edition=e.id WHERE e.id=?""",(self.creator,edition)).fetchone()

    def test_automatic_publish_resumes_verified_backlog_after_pause_during_upload(self):
        self.add_stories(1,playable=True,automated=True)
        client = Mock()
        client.discover.return_value = self.accounts
        client.init_upload.return_value = ("approved-media",None)
        client.create.return_value = "confirmed-group"
        with store.connect(self.root,True) as db:
            self.assertEqual(daily_release.plan(db,self.root,self.values,TODAY)[1],1)
            row,_ = daily_release.claim(db,self.values,TODAY)
            with self.assertRaises(ValueError):
                indiepost.release_edition(db,self.root,row["story"],row["edition"])
        def turn_off(_):
            with store.connect(self.root,True) as db:
                db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",
                           (json.dumps(dict(self.values,enabled=False)),))
        client.confirm.side_effect = turn_off
        daily_release.dispatch(self.root,row,client,now=lambda:TODAY)
        client.create.assert_not_called()
        with store.connect(self.root,True) as db:
            first = daily_release.delivery_rows(db,row["story"])[0]
            self.assertEqual((first["state"],first["media_id"]),("local","approved-media"))
            db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",(json.dumps(self.values),))
            self.assertEqual(daily_release.plan(db,self.root,self.values,TODAY+86400)[1],1)
            resumed,_ = daily_release.claim(db,self.values,TODAY+86400)
        self.assertEqual(resumed["intent"],row["intent"])
        daily_release.dispatch(self.root,resumed,client,now=lambda:TODAY+86400)
        client.init_upload.assert_called_once()
        client.create.assert_called_once()

    def test_release_gap_applies_to_reels_across_all_accounts_and_midnight(self):
        self.add_stories(2)
        start = TODAY + 15*3600 + 30*60
        values = dict(self.values,spacing=80)
        client = Mock()
        client.discover.return_value = self.accounts
        client.create.side_effect = [f"fixture-group-{i}" for i in range(len(self.accounts))]
        with patch.object(indiepost,"release_edition",side_effect=self.qualified_fixture):
            with store.connect(self.root,True) as db:
                db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",(json.dumps(values),))
                self.assertEqual(daily_release.plan(db,self.root,values,start)[1],2)
                db.execute("UPDATE factory_release_destinations SET media_id='cached'")
            story = None
            for _ in self.accounts:
                with store.connect(self.root,True) as db:
                    row,_ = daily_release.claim(db,values,start)
                    story = story or row["story"]
                    self.assertEqual(row["story"],story)
                daily_release.dispatch(self.root,row,client,now=lambda:start)
            with store.connect(self.root,True) as db:
                later = start+80*60
                self.assertEqual(daily_release.plan(db,self.root,values,later-1)[1],1)
                self.assertEqual(daily_release.claim(db,values,later-1),
                                 (None,"Waiting for the next reel release slot."))
                row,_ = daily_release.claim(db,values,later)
                self.assertNotEqual(row["story"],story)
            self.assertEqual(client.create.call_count,4)

    def test_scheduler_updates_only_buffer_settings_and_publishing_gap(self):
        with store.connect(self.root,True) as db:
            settings,versions = portal_settings.current(db)
            previous = settings["uploads"]
            form = {"version":str(versions["uploads"]),"minimum":"45","maximum":"60","refill":"on"}
            for bad in ("-1","1.5","1441"):
                with self.assertRaisesRegex(ValueError,"publishing gap"):
                    portal_settings.save_scheduler(db,dict(form,spacing=bad))
            portal_settings.save_scheduler(db,dict(form,spacing="80"))
            saved,versions = portal_settings.current(db)
            self.assertEqual(saved["uploads"],dict(previous,refill=True,spacing=80))
            portal_settings.save_scheduler(db,dict(form,version=str(versions["uploads"])))
            self.assertEqual(portal_settings.current(db)[0]["uploads"]["spacing"],80)

    def test_publication_totals_count_unique_reels_and_confirmed_destinations(self):
        self.add_stories(3)
        now = datetime(2026,11,2,1,tzinfo=timezone.utc).timestamp()
        values = dict(self.values,zone="Asia/Shanghai")
        with (patch.object(indiepost,"release_edition",side_effect=self.qualified_fixture),
              store.connect(self.root,True) as db):
            daily_release.plan(db,self.root,values,now)
            stories = [row[0] for row in db.execute("SELECT story FROM factory_release_stories ORDER BY story")]
            first,second,third = [daily_release.delivery_rows(db,story) for story in stories]
            db.executemany("UPDATE factory_release_destinations SET state='confirmed',confirmed=? WHERE intent=?",
                           [(now-60,row["intent"]) for row in first])
            db.execute("UPDATE factory_release_destinations SET state='scheduled' WHERE intent=?",(second[1]["intent"],))
            for row, stamp, post in ((first[0],"2026-11-02T00:59:00+00:00","same-delivery"),
                                     (second[0],"2026-10-31T16:00:00+00:00","month-start"),
                                     (third[0],"2026-10-31T15:59:59+00:00","last-month")):
                factory_done.record(db,row["story"],row["edition"],row["platform"],row["account"],
                                    "published",post,stamp,"fixture")
            counts = daily_release.publication_counts(db,values,now)
            self.assertEqual(tuple(counts),(2,5,1,4))

    def test_automation_readiness_flags_cleanup_and_infeasible_schedule(self):
        with store.connect(self.root) as db:
            config,_ = portal_settings.current(db)
            config["uploads"].update(refill=True,reclaim=True,spacing=80,account_daily=15)
            config["connections"]["hosted"] = True
            with (patch.object(readiness.provider_vault,"vault_for",return_value=Mock()),
                  patch.object(readiness,"Connections"), patch.object(readiness,"admission_hold",return_value=""),
                  patch.object(media,"tool",return_value="ffmpeg"), patch.object(media,"downloader",return_value=True)):
                html=operator_workspace.automation_panel(db,self.root,config)
                self.assertIn("Ready for automatic operation",html)
                self.assertEqual(html.count('data-status="ready"'),7)
                config["uploads"].update(reclaim=False,spacing=100)
                html=operator_workspace.automation_panel(db,self.root,config)
                self.assertIn("2 checks need attention",html)
                self.assertIn("Automatic cleanup is off",html)
                self.assertIn("Publishing limits are below the daily reel target",html)

    def deliver_day(self, day, stamp):
        sent = []
        client = Mock()
        client.discover.return_value = self.accounts
        def post(body, identity):
            self.assertEqual(body["when"],"now")
            self.assertNotIn("scheduledAt",body)
            sent.append(identity)
            return f"fixture-group-{int(stamp)}-{len(sent)}"
        client.create.side_effect = post
        with store.connect(self.root,True) as db:
            db.execute("""UPDATE factory_release_destinations SET media_id='cached-media' WHERE intent IN (
                          SELECT i.id FROM done_delivery_intents i JOIN factory_release_stories r ON r.story=i.story
                          WHERE r.day=?)""",(day,))
        for _ in range(60):
            with store.connect(self.root,True) as db:
                target,_ = daily_release.claim(db,self.values,stamp)
            self.assertIsNotNone(target)
            daily_release.dispatch(self.root,target,client,now=lambda:stamp)
            with store.connect(self.root) as db:
                row = next(r for r in daily_release.delivery_rows(db,target["story"]) if r["intent"]==target["intent"])
            self.assertEqual(row["state"],"scheduled")
            client.status.return_value = {"status":"posted","format":"reel",
                "youtube":{"privacyStatus":"private"},"tiktok":{"privacyLevel":"SELF_ONLY"},
                "externalPostId":f"fixture-post-{int(stamp)}-{len(sent)}",
                "postedAt":datetime.fromtimestamp(stamp,timezone.utc).isoformat()}
            daily_release.reconcile(self.root,row,client,stamp)
        self.assertEqual(len(set(sent)),60)
        with store.connect(self.root) as db:
            self.assertEqual(tuple(daily_release.counts(db,day)),(15,15,15))

    def test_100_to_85_to_70_with_four_destinations_and_late_arrivals(self):
        with patch.object(indiepost,"release_edition",side_effect=self.qualified_fixture):
            self.add_stories(100)
            with store.connect(self.root,True) as db:
                first,count = daily_release.plan(db,self.root,self.values,TODAY)
                self.assertEqual(count,15)
                self.assertEqual(sum(1 for _ in daily_release.ready(db,self.root)),100)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM factory_release_destinations").fetchone()[0],60)
            self.deliver_day(first,TODAY)
            with store.connect(self.root) as db:
                self.assertEqual(sum(1 for _ in daily_release.ready(db,self.root)),85)
                restored = self.root/"gta-streamer-disposable-release-restore"
                restored.mkdir()
                with store.connect(restored) as target:
                    db.backup(target)
            self.root = restored
            store.initialize(self.root,Path("tests/fixtures/VIDEO-TABLE.md"))
            with store.connect(self.root,True) as db:
                second,count = daily_release.plan(db,self.root,self.values,TODAY+86400)
                self.assertEqual(count,15)
                self.assertNotEqual(first,second)
            self.deliver_day(second,TODAY+86400)
            with store.connect(self.root) as db:
                self.assertEqual(sum(1 for _ in daily_release.ready(db,self.root)),70)
            self.add_stories(1,start=101)
            with store.connect(self.root,True) as db:
                self.assertEqual(daily_release.plan(db,self.root,self.values,TODAY+86500)[1],0)
                self.assertEqual(sum(1 for _ in daily_release.eligible(db,self.root)),71)

    def test_claim_race_replay_and_partial_destination(self):
        self.add_stories(1,playable=True)
        with store.connect(self.root,True) as db:
            self.assertEqual(daily_release.plan(db,self.root,self.values,TODAY)[1],1)
        rows = []
        def claim():
            with store.connect(self.root,True) as db:
                rows.append(daily_release.claim(db,self.values,TODAY)[0])
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len({r["intent"] for r in rows}),2)
        youtube = next(r for r in rows if r["platform"]=="youtube")
        tiktok = next(r for r in rows if r["platform"]=="tiktok")
        client = Mock()
        client.discover.return_value = self.accounts
        client.init_upload.return_value = ("fixture-media",None)
        client.create.side_effect = [provider_api.ProviderError("Quota",429,60),"youtube-group",
                                     provider_api.ProviderError("Timeout",retry=8)]
        daily_release.dispatch(self.root,youtube,client,now=lambda:TODAY)
        with store.connect(self.root,True) as db:
            retry = next(r for r in daily_release.delivery_rows(db,youtube["story"]) if r["platform"]=="youtube")
            self.assertEqual(retry["state"],"retry")
            self.assertEqual(retry["check_after"],TODAY+60)
            retry,_ = daily_release.claim(db,self.values,TODAY+60)
            self.assertEqual(retry["intent"],youtube["intent"])
        daily_release.dispatch(self.root,retry,client,now=lambda:TODAY+60)
        self.assertEqual(client.create.call_args_list[0],client.create.call_args_list[1])
        self.assertEqual(client.create.call_args.args[0]["when"],"now")
        self.assertNotIn("scheduledAt",client.create.call_args.args[0])
        daily_release.dispatch(self.root,tiktok,client,now=lambda:TODAY+61)
        with store.connect(self.root) as db:
            statuses = daily_release.delivery_rows(db,youtube["story"])
            youtube = next(r for r in statuses if r["platform"]=="youtube")
            self.assertEqual({r["platform"]:r["state"] for r in statuses},
                             {"youtube":"scheduled","tiktok":"unknown","instagram":"local","facebook":"local"})
        client.status.return_value = {"status":"posted","youtube":{"privacyStatus":"private"},
                                      "externalPostId":"fixture-post","postedAt":"2026-11-21T08:03:00Z"}
        daily_release.reconcile(self.root,youtube,client,TODAY+200)
        with store.connect(self.root,True) as db:
            db.execute("UPDATE factory_release_destinations SET state='uploading' WHERE intent=?",
                       (next(r["intent"] for r in statuses if r["platform"]=="instagram"),))
            db.execute("UPDATE factory_release_destinations SET state='creating' WHERE intent=?",
                       (next(r["intent"] for r in statuses if r["platform"]=="facebook"),))
        daily_release.recover(self.root)
        with store.connect(self.root) as db:
            statuses = daily_release.delivery_rows(db,youtube["story"])
            self.assertEqual(next(r["state"] for r in statuses if r["platform"]=="youtube"),"confirmed")
            self.assertEqual(next(r["state"] for r in statuses if r["platform"]=="facebook"),"unknown")
            self.assertEqual(next(r["state"] for r in statuses if r["platform"]=="instagram"),"local")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM done_delivery_receipts").fetchone()[0],1)
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+5*86400,48),0)
        self.assertTrue(self.movie.is_file())

    def test_all_confirmed_expiry_recovery_and_exclusions(self):
        self.add_stories(1,playable=True)
        with store.connect(self.root,True) as db:
            daily_release.plan(db,self.root,self.values,TODAY)
            db.execute("UPDATE factory_release_destinations SET state='confirmed',confirmed=?",(TODAY+60,))
        self.assertFalse(daily_release.verified_receipt({"platform":"youtube","visibility":"public"},
                         {"status":"posted","youtube":{"privacyStatus":"private"}}))
        size = self.movie.stat().st_size
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+48*3600-1,48),0)
        reclamation.lease(self.root,self.job,"c1-1",TODAY+48*3600)
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+48*3600,48),0)
        with store.connect(self.root,True) as db:
            db.execute("UPDATE factory_release_artifacts SET protected=1")
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+51*3600,48),0)
        with store.connect(self.root,True) as db:
            db.execute("UPDATE factory_release_artifacts SET protected=0")
            db.execute("UPDATE sources SET location=? WHERE id=?",(str(self.movie),self.source))
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+51*3600,48),0)
        with store.connect(self.root,True) as db:
            db.execute("UPDATE sources SET location='' WHERE id=?",(self.source,))
            db.execute("UPDATE factory_release_artifacts SET state='deleting'")
        self.movie.unlink()
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+51*3600,48),size)
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+51*3600,48),0)
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT state FROM factory_release_artifacts").fetchone()[0],"removed")

    def test_reclamation_unlinks_only_the_registered_edition_artifact(self):
        self.add_stories(1,playable=True)
        size = self.movie.stat().st_size
        neighbor = self.movie.with_name("evidence.json")
        neighbor.write_text("{}")
        with store.connect(self.root,True) as db:
            self.assertEqual(daily_release.plan(db,self.root,self.values,TODAY)[1],1)
            db.execute("UPDATE factory_release_destinations SET state='confirmed',confirmed=?",(TODAY,))
        self.assertEqual(reclamation.reclaim_one(self.root,TODAY+49*3600,48),size)
        self.assertFalse(self.movie.exists())
        self.assertTrue(neighbor.is_file())
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM done_editions").fetchone()[0],1)

    def test_storage_cleanup_waits_four_days_and_runs_with_dispatch_off(self):
        self.add_stories(1,playable=True)
        with store.connect(self.root,True) as db:
            edition,story = db.execute("SELECT edition,story FROM done_editions").fetchone()
            factory_done.record(db,story,edition,"youtube","gta:youtube","published","fixture-post",
                                datetime.fromtimestamp(TODAY,timezone.utc).isoformat(),"Fixture confirmed receipt")
            values,_ = portal_settings.current(db)
            db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",
                       (json.dumps(dict(values["uploads"],enabled=False,local_release=False)),))
            _,versions = portal_settings.current(db)
            portal_settings.save_cleanup(db,{"version":str(versions["uploads"]),"days":"4","reclaim":"on"})
        vault,client = Mock(),Mock()
        daily_release.tick(self.root,vault,clock=lambda:TODAY+96*3600-1,client_type=client)
        self.assertTrue(self.movie.is_file())
        daily_release.tick(self.root,vault,clock=lambda:TODAY+96*3600,client_type=client)
        self.assertFalse(self.movie.exists())
        client.assert_not_called()

    def test_storage_manual_delete_preserves_source_references_and_history(self):
        self.add_stories(1,playable=True)
        with store.connect(self.root,True) as db:
            edition = db.execute("SELECT edition FROM done_editions").fetchone()[0]
            self.assertIn(f"/storage/delete/{edition}",storage_page.page(db,self.root,"fixture",{}))
            relative = self.movie.relative_to(self.root).as_posix()
            self.assertEqual(storage_page.explorer_path(self.root,relative),self.movie)
            self.assertIsNone(storage_page.explorer_path(self.root,"streamer-runs/../factory.sqlite3"))
            self.assertIsNone(storage_page.explorer_path(self.root,"factory.sqlite3"))
            db.execute("UPDATE sources SET location=? WHERE id=?",(str(self.movie),self.source))
        with self.assertRaisesRegex(ValueError,"protected"):
            reclamation.delete_portrait(self.root,edition)
        self.assertTrue(self.movie.is_file())
        with store.connect(self.root,True) as db:
            db.execute("UPDATE sources SET location='' WHERE id=?",(self.source,))
        size = self.movie.stat().st_size
        self.assertEqual(reclamation.delete_portrait(self.root,edition),size)
        self.assertFalse(self.movie.exists())
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM done_editions").fetchone()[0],1)
            self.assertEqual(tuple(db.execute("SELECT state,removal_method FROM factory_release_artifacts").fetchone()),
                             ("removed","manual"))

    def test_queue_saving_full_buffer_and_hysteresis_admit_one_only(self):
        with store.connect(self.root) as db:
            config = portal_settings.current(db)[0]
        config["uploads"] = dict(self.values,refill=True)
        config["connections"]["hosted"] = True
        with store.connect(self.root,True) as db:
            db.execute("INSERT INTO portal_settings(section,data) VALUES('connections',?)",
                       (json.dumps(config["connections"]),))
            db.execute("UPDATE factory_release_refill SET active=1")
            with patch.object(daily_release,"buffer_count",return_value=60):
                daily_release.refill(db,self.root,config,TODAY)
            self.assertEqual(db.execute("SELECT active FROM factory_release_refill").fetchone()[0],0)
            channel = "UC"+"a"*22
            for i in range(25):
                video = f"v{i:010d}"
                canonical = f"https://www.youtube.com/watch?v={video}"
                info = {"id":video,"channel_id":channel,"title":f"Broadcast {i}","duration":1200,"live_status":"was_live"}
                source_queue.attach(db,{"name":"Fixture queued creator","game":"GTA VI"},(canonical,"youtube",info,"ready"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_queue").fetchone()[0],25)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM portal_admissions").fetchone()[0],0)
        store.initialize(self.root,Path("tests/fixtures/VIDEO-TABLE.md"))
        with store.connect(self.root,True) as db:
            with patch.object(daily_release,"buffer_count",return_value=45):
                daily_release.refill(db,self.root,config,TODAY)
            self.assertEqual(db.execute("SELECT active FROM factory_release_refill").fetchone()[0],0)
            with patch.object(daily_release,"buffer_count",return_value=44):
                daily_release.refill(db,self.root,config,TODAY)
                daily_release.refill(db,self.root,config,TODAY)
            self.assertEqual(db.execute("SELECT active FROM factory_release_refill").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM portal_admissions").fetchone()[0],1)

    def test_buffer_scheduler_finishes_an_hour_before_stopping_and_continues_in_order(self):
        with store.connect(self.root,True) as db:
            store.set_source_state(db,self.source,"archived")
            config = portal_settings.current(db)[0]
            config["uploads"]["refill"] = True
            config["connections"]["hosted"] = True
            db.execute("INSERT INTO portal_settings(section,data) VALUES('connections',?)",
                       (json.dumps(config["connections"]),))
            sources = []
            for i in (1,2):
                video = f"s{i:010d}"
                info = {"id":video,"channel_id":"UC"+"b"*22,"title":f"Stream {i}",
                        "duration":10800 if i==1 else 3600,"live_status":"was_live"}
                source,_ = source_queue.attach(db,{"name":"Scheduler fixture","game":"GTA VI"},
                    (f"https://www.youtube.com/watch?v={video}","youtube",info,"ready"))
                sources.append(source)
            def tick(size):
                with patch.object(daily_release,"buffer_count",return_value=size):
                    daily_release.refill(db,self.root,config,TODAY)
                return db.execute("""SELECT id,source,state,json_extract(settings,'$.coverage_start'),
                                     json_extract(settings,'$.coverage_minutes')
                                     FROM jobs WHERE state IN ('running','queued')""").fetchone()
            self.assertIsNone(tick(45))
            first = tick(44)
            self.assertEqual(tuple(first)[1:],(sources[0],"queued",0,60))
            db.execute("UPDATE jobs SET state='running' WHERE id=?",(first["id"],))
            self.assertEqual(tick(63)["id"],first["id"])
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(first["id"],))
            self.assertIsNone(tick(59))
            second = tick(44)
            self.assertEqual(tuple(second)[1:],(sources[0],"queued",3600,60))
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(second["id"],))
            third = tick(59)
            self.assertEqual(tuple(third)[1:],(sources[0],"queued",7200,60))
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(third["id"],))
            self.assertEqual(tuple(tick(59))[1:],(sources[1],"queued",0,60))

    def test_long_replay_scheduler_and_jobs_share_the_11_hour_40_minute_cutoff(self):
        video = "s0000000001"
        with store.connect(self.root,True) as db:
            store.set_source_state(db,self.source,"archived")
            replay,_ = source_queue.attach(db,{"name":"Long replay fixture","game":"GTA VI"},
                (f"https://www.youtube.com/watch?v={video}","youtube",{"id":video,"channel_id":"UC"+"b"*22,
                 "title":"Long replay","duration":85000,"live_status":"was_live"},"ready"))
            for part in range(0,11*3600,3600):
                job = store.enqueue(db,replay,fresh=True,overrides={
                    "method":"streamer","acquisition":"audio-first","coverage_start":part,"coverage_minutes":60})
                db.execute("UPDATE jobs SET state='complete' WHERE id=?",(job,))
            self.assertEqual(daily_release.next_hour(db,self.root),[(replay,11*3600)])
            last,_ = portal_settings.start(db,replay,{"directory_part":11*3600,"coverage_start":"39600",
                "coverage_minutes":"60","hosted":"on"},buffer_admission=True)
            saved = db.execute("SELECT settings FROM jobs WHERE id=?",(last,)).fetchone()[0]
            self.assertEqual(json.loads(saved)["coverage_minutes"],40)
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(last,))
            self.assertEqual(daily_release.next_hour(db,self.root),[])
            page = operator_workspace.jobs_page(db,self.root,"fixture",{})
            self.assertIn("11:00:00–11:40:00",page)
            self.assertNotIn("Part 13",page)

    def test_dst_day_bounds_and_immediate_window(self):
        values = dict(self.values,zone="America/New_York")
        spring = datetime(2026,3,8,16,tzinfo=timezone.utc).timestamp()
        fall = datetime(2026,11,1,16,tzinfo=timezone.utc).timestamp()
        early = datetime(2026,3,8,7,tzinfo=timezone.utc).timestamp()
        for stamp,hours in ((spring,23),(fall,25)):
            start,end = daily_release.day_bounds(values,stamp)
            self.assertEqual(end-start,hours*3600)
        self.assertTrue(daily_release.window_open(values,early))
        self.assertFalse(daily_release.window_open(dict(values,release="scheduled"),early))
