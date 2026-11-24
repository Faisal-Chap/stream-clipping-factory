import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import factory_events, factory_metrics, store, streamer_auto, streamer_run
from factory.jobs import Worker
from factory.provider_connections import ConnectionRequired
from factory.provider_meter import DISCOVERY, DOWNSTREAM, FRAME_CHECK, RANK, Meter, ProviderExhausted, ProviderPending

TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()


class PortalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-autonomous-factory-disposable-monitor-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sequence = 0
        store.initialize(self.root,TABLE)

    def job(self):
        self.sequence += 1
        with store.connect(self.root,True) as db:
            source,_ = store.save_source(db,{"link":f"https://www.youtube.com/watch?v=fixture{self.sequence:04d}"})
            job = store.enqueue(db,source,fresh=True,overrides={"method":"streamer","reserve_mib":256})
            db.execute("UPDATE jobs SET state='running',attempt=1 WHERE id=?",(job,))
            factory_events.record(db,job,"claimed",item="Test worker")
            return db.execute("SELECT * FROM jobs WHERE id=?",(job,)).fetchone()

    def test_discovery_leaves_room_for_ranking_and_a_frame_check(self):
        job = self.job()["id"]
        meter = Meter(self.root,job,2)
        calls = []

        def submit(identity):
            calls.append(identity)
            return {"id":identity,"status":"completed","output":[
                {"type":"message","content":[{"type":"output_text","text":"{}"}]}]}

        self.assertEqual(meter.call("sail","first","saved",8000,submit,downstream=DOWNSTREAM),"{}")
        with self.assertRaisesRegex(ProviderExhausted,"held for ranking"):
            meter.call("sail","next","saved",DISCOVERY,submit,downstream=DOWNSTREAM)
        self.assertEqual(meter.call("sail","first","saved",8000,submit,downstream=DOWNSTREAM),"{}")
        meter.call("sail","rank","saved",RANK,submit)
        meter.call("sail","frames","saved",FRAME_CHECK,submit)
        self.assertEqual(len(calls),3)
        with store.connect(self.root) as db:
            self.assertEqual(meter.available(db),2500)

    def test_full_disk_persists_a_real_stage_failure(self):
        job = self.job()
        with patch.object(streamer_run.shutil,"disk_usage",return_value=type("Disk",(),{"free":0})()):
            Worker(self.root,connections=object()).process(job)
        with store.connect(self.root) as db:
            row = db.execute("SELECT state,error FROM jobs WHERE id=?",(job["id"],)).fetchone()
            self.assertEqual(row["state"],"failed")
            self.assertIn("free-space reserve",row["error"])
            events = [tuple(e) for e in db.execute("SELECT stage,kind FROM factory_events WHERE job=?",(job["id"],))]
            self.assertEqual(events[-2:],[("Archive","started"),("Archive","failed")])

    def test_key_timeout_restart_and_retry_events(self):
        first = self.job()
        with patch.object(streamer_run,"process",side_effect=ConnectionRequired("Sail key expired.")):
            Worker(self.root,connections=object()).process(first)
        second = self.job()
        with store.connect(self.root,True) as db:
            db.execute("""INSERT INTO provider_calls(id,job,provider,reference,attempts,reservation_micros,next_try)
                       VALUES('fixture',?,'sail','opaque',1,4500,?)""",(second["id"],time.time()+60))
        with patch.object(streamer_run,"process",side_effect=ProviderPending("Backoff until retry window.")):
            Worker(self.root,connections=object()).process(second)
        third = self.job()
        store.initialize(self.root,TABLE)
        with store.connect(self.root,True) as db:
            kinds = {r["job"]:r for r in db.execute("""SELECT e.* FROM factory_events e
                JOIN (SELECT job,MAX(id) AS latest FROM factory_events GROUP BY job) m ON e.id=m.latest""")}
            self.assertEqual(kinds[first["id"]]["error_class"],"ConnectionRequired")
            self.assertEqual(kinds[second["id"]]["kind"],"deferred")
            self.assertIsNotNone(kinds[second["id"]]["retry_utc"])
            self.assertEqual(kinds[second["id"]]["reserved_micros"],4500)
            self.assertEqual(kinds[third["id"]]["kind"],"interrupted")
            store.resume(db,third["id"])
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?",(third["id"],)).fetchone()[0],"queued")

    def test_hourly_accounting_uses_payoff_and_never_counts_editions_twice(self):
        run = self.job()
        job = run["id"]
        data = {"asset":{"duration":14435},"chapters":[{"start":0,"end":7200},{"start":7400,"end":14435}],
                "proposals":[dict(id="c1-1",start=3588,primary_payoff=3603),
                              dict(id="c1-2",start=3588,primary_payoff=3603,merged_into="c1-1"),
                              dict(id="c4-1",start=14415,primary_payoff=14430)]}
        with store.connect(self.root,True) as db:
            for identity in ("focus","alternate"):
                db.execute("""INSERT INTO streamer_editions(id,job,candidate,plan,qualification,render_state)
                            VALUES(?,?,'c1-1','{}','qualified','exported')""",(identity,job))
            rows = factory_metrics.funnel(self.root,db,job,data)
        self.assertEqual([len(r["qualified"]) for r in rows],[0,1,0,0,0])
        self.assertEqual(len(rows[1]["exported"]),2)
        self.assertEqual(rows[1]["merged"],["c1-2"])
        self.assertEqual(rows[2]["covered"],3400)
        self.assertFalse(rows[4]["complete"])
        self.assertEqual(sum(r["complete"] for r in rows),4)

    def test_automatic_hooks_reject_truncation_and_hold_offscreen_callbacks(self):
        setup = {"quote":"That same tyre explosion again?"}
        self.assertEqual(streamer_auto.complete_hook({"hook":"The vehicle tilts and then"},setup),setup["quote"])
        self.assertEqual(streamer_auto.complete_hook({"hook":"What happens next?"},{"quote":"Yeah."}),"")
        earlier = {"id":"c1-1","event":"Tyre explosion","primary_payoff":27,"evidence":[setup],
                   "visual_check":{"verdict":"hold"}}
        candidate = {"id":"c2-1","kind":"callback","start":3690,"evidence":[setup],
                     "visual_check":{"verdict":"accept"}}
        self.assertIsNone(streamer_auto.callback_proof(candidate,[earlier]))
        earlier["visual_check"]["verdict"] = "accept"
        self.assertEqual(streamer_auto.callback_proof(candidate,[earlier])["id"],"c1-1")
