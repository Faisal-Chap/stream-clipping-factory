import json
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from factory import portal_controls, portal_settings, portal_templates, store

TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()
TODAY = datetime(2026,11,20,5,tzinfo=timezone.utc)


class PortalMilestoneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-portal-admission-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        store.initialize(self.root,TABLE)
        self.index = 0

    def creator(self,db,name):
        creator = db.execute("INSERT INTO streamer_creators(name,game,credit) VALUES(?,'GTA V',?)",(name,name)).lastrowid
        return creator,self.channel(db,creator)

    def channel(self,db,creator):
        self.index += 1
        return db.execute("""INSERT INTO streamer_accounts(creator,platform,platform_id,url)
                   VALUES(?,'youtube',?,'https://www.youtube.com/@fixture')""",(creator,f"fixture-{self.index}")).lastrowid

    def source(self,db,account):
        self.index += 1
        source,_ = store.save_source(db,{"link":f"https://youtu.be/test{self.index:07d}"})
        db.execute("INSERT INTO streamer_streams(account,source,title,live_status) VALUES(?,?,?,'was_live')",
                   (account,source,f"Fixture recording {self.index}"))
        return source

    def start(self,source,now=TODAY,**more):
        with store.connect(self.root,True) as db:
            return portal_settings.start(db,source,dict(hosted="on",**more),now=now)

    def test_round_robin_distinct_creators_and_total_cap(self):
        with store.connect(self.root,True) as db:
            a,ac = self.creator(db,"First")
            b,bc = self.creator(db,"Second")
            c,cc = self.creator(db,"Third")
            d,dc = self.creator(db,"Fourth")
            a1,a2 = [self.source(db,ac) for _ in range(2)]
            b1,b2 = [self.source(db,bc) for _ in range(2)]
            c1,c2 = [self.source(db,cc) for _ in range(2)]
            d1 = self.source(db,dc)
        self.start(a1)
        with self.assertRaisesRegex(ValueError,"eligible creator"):
            self.start(a2)
        for source in (b1,c1,a2,b2):
            self.start(source)
        with self.assertRaisesRegex(ValueError,"total source-video"):
            self.start(c2)
        with self.assertRaisesRegex(ValueError,"total source-video"):
            self.start(d1)
        with store.connect(self.root) as db:
            _,counts,sources = portal_settings.admission_state(db,portal_settings.current(db)[0],TODAY)
        self.assertEqual((len(sources),len(counts)),(5,3))

    def test_duplicate_race_linked_accounts_midnight_and_restart_keep_the_same_job(self):
        with store.connect(self.root,True) as db:
            creator,account = self.creator(db,"Only creator")
            linked = self.channel(db,creator)
            first,second,third = [self.source(db,a) for a in (account,linked,account)]
        before_midnight = datetime(2026,11,20,15,59,tzinfo=timezone.utc)
        after_midnight = datetime(2026,11,20,16,1,tzinfo=timezone.utc)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _:self.start(first,before_midnight),range(6)))
        self.assertEqual(sum(created for _,created in results),1)
        self.assertEqual(len({job for job,_ in results}),1)
        job = results[0][0]
        self.start(second,before_midnight)
        with self.assertRaisesRegex(ValueError,"creator has reached"):
            self.start(third,before_midnight)
        self.assertEqual(self.start(first,after_midnight),(job,False))
        self.start(third,after_midnight)
        store.initialize(self.root,TABLE)
        with store.connect(self.root,True) as db:
            configuration,versions = portal_settings.current(db)
            rows = list(db.execute("SELECT day,source FROM portal_admissions ORDER BY day,source"))
            self.assertEqual([r["day"] for r in rows],["2026-11-20","2026-11-20","2026-11-21"])
            self.assertEqual(portal_settings.day(dict(configuration["resources"],reset="04:00"),after_midnight),"2026-11-20")
            old = db.execute("SELECT settings FROM jobs WHERE id=?",(job,)).fetchone()[0]
            group = dict(configuration["production"],ids="",version=0,hourly_target=12)
            portal_settings.save(db,"production",{k:str(v) for k,v in group.items()})
            db.execute("UPDATE jobs SET state='failed' WHERE id=?",(job,))
            store.resume(db,job)
            self.assertEqual(db.execute("SELECT settings FROM jobs WHERE id=?",(job,)).fetchone()[0],old)
            with self.assertRaisesRegex(ValueError,"another tab"):
                portal_settings.save(db,"production",{k:str(v) for k,v in group.items()})

    def test_concurrent_sources_reserve_the_last_slot_once(self):
        with store.connect(self.root,True) as db:
            _,account = self.creator(db,"Last slot")
            sources = [self.source(db,account) for _ in range(2)]
            group = dict(portal_settings.SECTIONS["production"],ids="",version=0,daily_videos=1,
                         daily_creators=1,daily_per_creator=1)
            portal_settings.save(db,"production",{key:str(value) for key,value in group.items()})

        def race(source):
            try:
                return self.start(source,hourly_target="12")
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race,sources))
        accepted = [result[0] for result in results if isinstance(result,tuple)]
        self.assertEqual(len(accepted),1)
        self.assertIn("Today’s total source-video ceiling has been reached.",results)
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM portal_admissions").fetchone()[0],1)
            pinned = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?",(accepted[0],)).fetchone()[0])
            self.assertEqual(pinned["portal_overrides"],{"hourly_target":12})

    def test_existing_roster_attempts_backfill_one_slot_per_source_day(self):
        with store.connect(self.root,True) as db:
            creator,account = self.creator(db,"Historical creator")
            source = self.source(db,account)
            db.execute("DELETE FROM metadata WHERE key='portal-admissions-from-legacy'")
            for hour in (1,2):
                job = store.enqueue(db,source,fresh=True,overrides={"method":"streamer"})
                db.execute("UPDATE jobs SET state='complete',created=? WHERE id=?",(f"2026-11-20 0{hour}:00:00",job))
        store.initialize(self.root,TABLE)
        store.initialize(self.root,TABLE)
        with store.connect(self.root) as db:
            _,counts,sources = portal_settings.admission_state(db,portal_settings.current(db)[0],TODAY)
        self.assertEqual(counts,Counter({creator:1}))
        self.assertEqual(list(sources),[source])

    def test_fixed_random_even_and_historical_activated_drafts(self):
        with store.connect(self.root,True) as db:
            _,account = self.creator(db,"Fixture creator")
            source = self.source(db,account)
            self.assertEqual(len(portal_templates.active(db)),4)
            name, number = portal_templates.save(db,"create",{"base":"hook-card","name":"Fifth design"})
            self.assertNotIn(name,portal_templates.active(db))
            _, recipe = portal_templates.revision(db,name)
            original = portal_templates.draft_values(recipe)
            draft = dict(original,name="Fifth design",text="Changed draft",fill="#DDAA33",version="1")
            self.assertEqual(portal_templates.save(db,"save",{k:str(v) for k,v in draft.items()},name),(name,2))
            portal_templates.save(db,"activate",{"revision":"2"},name)
            self.assertEqual(portal_templates.active(db)[name]["revision"],2)
            self.assertEqual(portal_templates.revision(db,name,1)[1]["draft_style"],original)
            fixed = portal_templates.plan(db,{"mode":"fixed","ids":[name]},0)
            even = portal_templates.plan(db,{"mode":"even","ids":[]},2)
            rand = portal_templates.plan(db,{"mode":"random","ids":["hook-card",name]},0)
            jobs = []
            for policy in (fixed,even,rand):
                job = store.enqueue(db,source,fresh=True,overrides={"template_plan":policy})
                jobs.append(job)
                db.execute("UPDATE jobs SET state='complete' WHERE id=?",(job,))
            even_stories = [portal_templates.assign(db,jobs[1],f"story-{i}",even) for i in range(8)]
            self.assertEqual(sorted(Counter(item["template"] for item in even_stories).values()),[2,2,2,2])
            self.assertEqual(even_stories[0]["template"],portal_templates.ORIGINALS[2])
            self.assertEqual(portal_templates.assign(db,jobs[1],"story-2",even),even_stories[2])
            draw = portal_templates.assign(db,jobs[2],"story-retry",rand)
            self.assertIn(draw["template"],{"hook-card",name})
            self.assertEqual(portal_templates.assign(db,jobs[2],"story-retry",rand),draw)
            portal_templates.save(db,"archive",{"revision":"2"},name)
            self.assertEqual(portal_templates.assign(db,jobs[0],"fixed-story",fixed)["revision"],2)
            with self.assertRaisesRegex(ValueError,"needs repair"):
                portal_templates.plan(db,{"mode":"fixed","ids":[name]},0)

    def test_indiepost_probe_is_read_only_and_account_preview_groups_destinations(self):
        def read_accounts(url,headers,timeout):
            self.assertEqual(url,"https://www.indiepost.app/api/v1/accounts")
            self.assertEqual(headers,{"Authorization":"Bearer fixture"})
            return {"workspaces":[
                {"id":"separate","name":"Other workspace","accounts":[
                    {"platform":"youtube","connected":True,"username":"Unrelated account"}]},
                {"id":"fixture","name":"Stream Clipping Factory","accounts":[
                    {"platform":"youtube","connected":True,"username":"Fixture account"},
                    {"platform":"instagram","connected":True,"username":"Fixture reels"},
                    {"platform":"tiktok","connected":False,"username":"Disconnected"}]}]}
        accounts = portal_controls.list_destinations("fixture",read_accounts)
        self.assertEqual([a["id"] for a in accounts],["fixture:youtube","fixture:instagram"])
        with store.connect(self.root,True) as db:
            creator,account = self.creator(db,"Fixture creator")
            source = self.source(db,account)
            job = store.enqueue(db,source)
            db.execute("INSERT INTO portal_creator_policy(creator,platforms) VALUES(?,?)",
                       (creator,json.dumps(["youtube","instagram"])))
            edition = "a"*64
            db.execute("""INSERT INTO streamer_editions(id,job,candidate,plan,qualification,render_state,qa)
                          VALUES(?,?,'c1-1',?,'qualified','exported','Fixture QA')""",
                       (edition,job,json.dumps({"title":"Fixture reel","post":"Fixture caption"})))
            target = self.root/"streamer-runs"/str(job)/"c1-1"/"editions"/edition
            target.mkdir(parents=True)
            from test_shorts_release import make_portrait
            make_portrait(target/"portrait.mp4")
            values = dict(portal_settings.SECTIONS["uploads"],accounts=["fixture:youtube","fixture:instagram"],
                          window_start="23:00",window_end="04:00")
            html = portal_controls.schedule_preview(db,self.root,values,accounts,now=TODAY)
            make_portrait(target/"portrait.mp4", video_frames=509)
            held = portal_controls.schedule_preview(db,self.root,values,accounts,now=TODAY)
        self.assertIn("1 local post groups · 2 account deliveries",html)
        self.assertIn("20 Nov 23:00",html)
        self.assertIn("No delivery attempt",html)
        self.assertIn("Public reel",html)
        self.assertIn("Private",html)
        options = portal_controls.field("accounts",values,accounts)
        self.assertIn('Fixture reels <span class="meta">· Public reel</span>',options)
        self.assertIn("No current final-QA editions",held)
