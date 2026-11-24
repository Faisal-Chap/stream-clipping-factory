import json
import tempfile
import unittest
from pathlib import Path

from factory import factory_done, media, shorts_release, store, streamer_asr, streamer_edit_render, template_motion
from test_shorts_release import make_portrait

TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()
ASSET = "a"*64


def evidence(event="snow-roadblock", point=600, left=599, right=601):
    return {"asset":ASSET,"proof":{"event":event,"source":point,"start":left,"end":right}}


class DoneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-done-reconciliation-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store.initialize(self.root,TABLE)
        with store.connect(self.root,True) as db:
            self.source,_ = store.save_source(db,{"link":"https://youtu.be/test0000001"})
            creator = db.execute("INSERT INTO streamer_creators(name,game,credit) VALUES('Fixture','GTA V','Fixture')").lastrowid
            account = db.execute("""INSERT INTO streamer_accounts(creator,platform,platform_id,url)
                                  VALUES(?,'youtube','fixture-account','https://www.youtube.com/@fixture')""",(creator,)).lastrowid
            db.execute("INSERT INTO streamer_streams(account,source,title,live_status) VALUES(?,?,?,'was_live')",
                       (account,self.source,"Fixture recording"))

    def job(self,db):
        job = store.enqueue(db,self.source,fresh=True)
        db.execute("UPDATE jobs SET state='complete' WHERE id=?",(job,))
        return job

    def edition(self,db,job,candidate,plan,name):
        plan = dict(plan,render_style={"name":"Seam card"})
        db.execute("""INSERT INTO streamer_editions(id,job,candidate,plan,qualification,render_state)
                      VALUES(?,?,?,?,'qualified','exported')""",(name,job,candidate,json.dumps(plan)))
        clip = self.root/"gta-streamer-disposable-done-validated"/name
        clip.mkdir(parents=True)
        movie = make_portrait(clip/"portrait.mp4")
        streamer_asr.write(clip/"manifest.json",{"rendered_style":plan["render_style"],"profile":"master",
                                                "portrait_sha256":media.checksum(movie)})
        streamer_asr.write(clip/"qc.json",{"passed":True})
        return plan,clip

    def test_source_aliases_and_nearby_distinct_proofs(self):
        with store.connect(self.root,True) as db:
            a,b = self.job(db),self.job(db)
            original = factory_done.match(db,a,"c1-1",evidence())
            changed = factory_done.match(db,b,"c2-5",evidence(point=601.2,left=601.1,right=601.7))
            self.assertEqual(changed,original)
            different = factory_done.match(db,b,"c2-6",evidence("truck-wheel",603,602,604))
            self.assertNotEqual(different,original)
            with self.assertRaises(factory_done.ReconciliationRequired):
                factory_done.match(db,b,"c2-7",evidence("unclear",600.2,599.9,600.5))
        store.initialize(self.root,TABLE)
        with store.connect(self.root,True) as db:
            self.assertEqual(factory_done.match(db,b,"c2-5",evidence(point=601.2,left=601.1,right=601.7)),original)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM done_stories").fetchone()[0],2)

    def test_atomic_write_failure_missing_master_and_retry(self):
        with store.connect(self.root,True) as db:
            job = self.job(db)
            plan,path = self.edition(db,job,"c1-1",evidence(),"fixture-partial")
            db.execute("""CREATE TEMP TRIGGER fixture_write_failure BEFORE INSERT ON done_editions
                        BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
            db.execute("SAVEPOINT attempted_done")
            with self.assertRaisesRegex(Exception,"fixture failure"):
                factory_done.finish(self.root,db,job,"c1-1","fixture-partial",plan,path)
            db.execute("ROLLBACK TO attempted_done")
            db.execute("RELEASE attempted_done")
            db.execute("DROP TRIGGER fixture_write_failure")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM done_stories").fetchone()[0],0)
            movie = path/"portrait.mp4"
            movie.rename(path/"held-missing.mp4")
            with self.assertRaisesRegex(media.MediaError,"playable master"):
                factory_done.finish(self.root,db,job,"c1-1","fixture-partial",plan,path)
            (path/"held-missing.mp4").rename(movie)
            first = factory_done.finish(self.root,db,job,"c1-1","fixture-partial",plan,path)
            self.assertEqual(factory_done.finish(self.root,db,job,"c1-1","fixture-partial",plan,path),first)

    def test_receipt_stays_with_original_edition_and_remote_ids_are_unique(self):
        with store.connect(self.root,True) as db:
            job = self.job(db)
            code = "fixture-held-remote"
            plan,path = self.edition(db,job,"c1-1",evidence(),"edition-1")
            story = factory_done.finish(self.root,db,job,"c1-1","edition-1",plan,path)
            plan,path = self.edition(db,job,"c1-1",evidence(),"edition-2")
            self.assertEqual(factory_done.finish(self.root,db,job,"c1-1","edition-2",plan,path),story)
            when = "2026-11-20T19:05:00+08:00"
            first = factory_done.account_intent(db,story,"edition-1","youtube","fixture")
            self.assertEqual(factory_done.account_intent(db,story,"edition-2","youtube","Fixture")["id"],first["id"])
            factory_done.record(db,story,"edition-1","youtube","fixture","scheduled",code,when)
            factory_done.record(db,story,"edition-2","youtube","fixture","published",code,when,"Fixture provider confirmation")
            factory_done.record(db,story,"edition-2","instagram","fixture","failed")
            receipts = list(db.execute("SELECT * FROM done_delivery_receipts"))
            self.assertEqual(factory_done.status(receipts),"Partially published")
            self.assertEqual(receipts[0]["edition"],"edition-1")
            self.assertEqual(factory_done.status(receipts[1:]),"No recorded publication")
            self.assertEqual(factory_done.status([{"platform":"youtube","account":"test","status":"unknown"}]),"No recorded publication")
            plan,path = self.edition(db,job,"c1-2",evidence("other",610,609,611),"edition-3")
            other = factory_done.finish(self.root,db,job,"c1-2","edition-3",plan,path)
            with self.assertRaisesRegex(ValueError,"already recorded"):
                factory_done.record(db,other,"edition-3","youtube","fixture","manual",code,when)

    def test_out_of_range_master_cannot_replace_a_held_current_edition(self):
        with store.connect(self.root,True) as db:
            job = self.job(db)
            plan,path = self.edition(db,job,"c1-1",evidence(),"original-edition")
            story = factory_done.finish(self.root,db,job,"c1-1","original-edition",plan,path)
            factory_done.set_hold(db,story,"original-edition","Payoff cannot be followed.")
            self.assertEqual(shorts_release.restriction(db,"original-edition"),"Payoff cannot be followed.")
            plan,later = self.edition(db,job,"c1-1",evidence(),"attempted-edition")
            movie = make_portrait(later/"portrait.mp4", audio_samples=815999)
            manifest = streamer_asr.read(later/"manifest.json")
            manifest["portrait_sha256"] = media.checksum(movie)
            streamer_asr.write(later/"manifest.json",manifest)
            with self.assertRaisesRegex(media.MediaError,"Release length"):
                factory_done.finish(self.root,db,job,"c1-1","attempted-edition",plan,later)
            self.assertEqual(db.execute(shorts_release.CURRENT_DONE,(story,)).fetchone()["edition"],"original-edition")
            plan,later = self.edition(db,job,"c1-1",evidence(),"valid-edition")
            self.assertEqual(factory_done.finish(self.root,db,job,"c1-1","valid-edition",plan,later),story)
            self.assertEqual(shorts_release.restriction(db,"valid-edition"),"")
            self.assertEqual(shorts_release.hold(db,story,"original-edition"),"Payoff cannot be followed.")

    def test_opening_fits_reading_time_before_visual_payoff(self):
        plan = {"hook":"Can this sedan fly?","proof":{"edit":3.50},"fps":30}
        self.assertLess(template_motion.opening_time(plan,{}),3.50)
        plan["proof"]["edit"] = 1.20
        with self.assertRaisesRegex(media.MediaError,"clear before"):
            template_motion.opening_time(plan,{})

    def test_first_visible_caption_waits_for_a_real_phrase_boundary(self):
        words = [[1,0,1,"Is",.9],[2,1,1.4,"this",.9],[3,1.4,1.95,"the",.9],
                 [4,1.95,2.4,"road?",.9],[5,3,3.3,"Okay.",.9]]
        cues = [{"source":[1.95,2.4],"start":1.95,"text":"road?"},
                {"source":[3,3.3],"start":3,"text":"Okay."}]
        self.assertEqual(streamer_edit_render.initial_dialogue(cues,words,{"captions":[]},1.7),cues[1:])
