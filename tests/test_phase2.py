import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import context_judge, context_rank, editorial, provider_api as api, store, streamer_judge, timeline
from factory.provider_connections import Connections
from factory.provider_meter import Meter, ProviderPending

TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()


class FixtureVault:
    available = True

    def __init__(self):
        self.values = {}

    def put(self, reference, secret):
        self.values[reference] = secret

    def get(self, reference):
        return self.values.get(reference)

    def remove(self, reference):
        self.values.pop(reference, None)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-media-factory-disposable-provider-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root, TABLE)

    def test_explicit_sail_tiers_and_retry_identity(self):
        requests = []

        def send(url, headers, body, timeout):
            requests.append((url, headers, body, timeout))
            return {"id": "resp_demo", "status": "completed", "service_tier": body["service_tier"]}

        for tier in api.TIERS:
            api.sail_submit("fixture", tier, "same-key", "tiny test", send=send)
        for tier in (None, "", "asap", "Balanced"):
            with self.assertRaises(ValueError):
                api.sail_submit("fixture", tier, "same-key", "tiny test", send=send)
        self.assertEqual(len(requests), 2)
        for (url, headers, body, _), tier in zip(requests, api.TIERS, strict=True):
            self.assertEqual((url, headers["Idempotency-Key"], body["model"], body["service_tier"],
                              body["background"], body["stream"]),
                             (api.SAIL_URL, "same-key", api.SAIL_MODEL, tier, tier == "flex", False))
        with self.assertRaises(api.ProviderError):
            api.sail_submit("fixture", "flex", "same-key", "tiny test",
                            send=lambda *_a, **_k: {"id": "resp_demo", "status": "completed",
                                                     "service_tier": "balanced"})

    def test_flex_poll_timeout_resumes_the_saved_response(self):
        with store.connect(self.root, True) as db:
            job = store.enqueue(db, 1)
        submit_ids, poll_ids = [], []

        def submit(identity):
            submit_ids.append(identity)
            return {"id": "resp_saved", "status": "queued"}

        def poll(identity):
            poll_ids.append(identity)
            if len(poll_ids) == 1:
                raise api.ProviderError("Network timeout.", retry=1)
            return {"id": identity, "status": "completed", "output": [], "usage": {
                "input_tokens": 10, "output_tokens": 5}}

        for attempt in range(3):
            meter = Meter(self.root, job, 5)
            if attempt < 2:
                with self.assertRaises(ProviderPending):
                    meter.call("sail", "candidate", "fixture-ref", 10000, submit, poll)
            else:
                self.assertEqual(meter.call("sail", "candidate", "fixture-ref", 10000, submit, poll), "")
            with store.connect(self.root, True) as db:
                db.execute("UPDATE provider_calls SET next_try=?", (time.time()-1,))
        self.assertEqual(len(submit_ids), 1)
        self.assertEqual(poll_ids, ["resp_saved", "resp_saved"])
        with self.assertRaisesRegex(ValueError, "budget"):
            Meter(self.root, job, 1).call("sail", "other", "fixture-ref", 10000, submit, poll)

    def test_truncated_streamer_chapter_resumes_with_two_smaller_requests(self):
        with store.connect(self.root, True) as db:
            job = store.enqueue(db, 1)
        transcript = {"duration": 740, "words": [
            [1, 12, 13, "Setup."], [2, 18, 19, "Payoff."],
            [3, 490, 491, "Second setup."], [4, 499, 500, "Second payoff."]],
            "rms_dbfs_by_second": [], "active_median_dbfs": None}
        config = {"sail_tier": "balanced", "provider_cap_cents": 2, "target": 12,
                  "stream": {"name": "Fixture", "title": "GTA", "credit": "Fixture"}}
        chat = streamer_judge.chat_activity(None)
        legacy = streamer_judge.legacy_chapter_prompt(config["stream"], transcript, chat, 720, 740, 660, 740, 9)
        digest = hashlib.sha256(legacy.encode()).hexdigest()[:24]
        meter = Meter(self.root, job, config["provider_cap_cents"])
        meter.call("sail", f"streamer-s1-v1:chapter-1:balanced:{digest}", "fixture-ref", 4500,
                   lambda _: {"status": "completed", "output": [{"type": "message", "content": [
                       {"type": "output_text", "text": '{"moments":[],"abstention":"Saved quiet chapter."}'}]}]})
        requests = []

        class Fixture:
            def key(self, *_args, **_kwargs):
                return "fixture-ref", "fixture"

            def sender(self, _url, headers, body, **_kwargs):
                requests.append((headers["Idempotency-Key"], body))
                prompt = body["input"]
                if "core 0.0–720.0s" in prompt:
                    return {"id": "resp_truncated", "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                            "usage": {"input_tokens": 4321, "output_tokens": body["max_output_tokens"]},
                            "output": [{"type": "message", "content": [
                                {"type": "output_text", "text": '{"moments":[{"score":10'}]}]}
                first = "core 0.0–360.0s" in prompt
                moment = dict(start=10, end=22, setup_ids=[1, 1], payoff_ids=[2, 2], score=6, reason="Fixture")
                if not first:
                    moment.update(start=488, end=503, setup_ids=[3, 3], payoff_ids=[4, 4])
                return {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps({"moments": [moment]})}]}]}

        for _ in range(2):
            chapters, proposals, chosen = streamer_judge.judge(
                self.root, {"duration": 740}, transcript, config, job, Fixture(), chat, lambda _: None)
            self.assertEqual([chapter["judgment"] for chapter in chapters], ["split-retry", "reused"])
            self.assertEqual([item["start"] for item in proposals], [10, 488])
            self.assertEqual(chosen, [])
        self.assertEqual(len(requests), 3)
        self.assertEqual(len({identity for identity, _ in requests}), 3)
        self.assertEqual([body["max_output_tokens"] for _, body in requests], [1800, 1050, 1050])
        self.assertTrue(all(body["service_tier"] == "balanced" for _, body in requests))
        self.assertLess(max(len(body["input"]) for _, body in requests[1:]), len(requests[0][1]["input"]))
        with store.connect(self.root) as db:
            saved = db.execute("SELECT result, usage FROM provider_calls WHERE id=?",
                               (requests[0][0],)).fetchone()
            self.assertEqual(json.loads(saved["result"]), {"status": "output_limit"})
            self.assertEqual(json.loads(saved["usage"])["output_tokens"], 1800)
        with self.assertRaisesRegex(ValueError, "budget"):
            meter.call("sail", "over-cap", "fixture-ref", 4500, lambda _: self.fail("Unexpected submission"))

    def test_invalid_chapter_evidence_gets_the_bounded_retry(self):
        with store.connect(self.root,True) as db:
            job = store.enqueue(db,1)
        transcript = {"duration":720,"words":[[1,12,13,"Setup."],[2,20,21,"Payoff."]],
                      "rms_dbfs_by_second":[],"active_median_dbfs":None}
        config = {"sail_tier":"balanced","provider_cap_cents":25,"target":7,"hourly_target":7,
                  "stream":{"name":"Fixture","title":"Replay","credit":"Fixture"}}
        calls = []
        def send(operation, prompt, output, **_):
            calls.append((operation,output))
            if operation.endswith("second-half"):
                return {"moments":[]}
            moment = dict(start=10,end=23,setup_ids=[1,1],payoff_ids=[2,2],score=6,reason="Fixture")
            if operation.endswith("whole"):
                moment["setup_ids"] = [101,102]
            return {"moments":[moment]}
        with patch.object(streamer_judge,"request_runner",return_value=send):
            chapters,proposals,_ = streamer_judge.judge(self.root,{"duration":720},transcript,config,job,None,
                                          streamer_judge.chat_activity(None),lambda _:None,audio_only=True)
        self.assertEqual(chapters[0]["judgment"],"split-retry")
        self.assertEqual(proposals[0]["evidence"][0]["ids"],[1,1])
        self.assertEqual([output for _,output in calls],[1800,1050,1050])
        self.assertEqual(len({operation for operation,_ in calls}),3)

    def test_complete_cut_uses_the_highest_supported_hook(self):
        candidate = {"start":10,"end":32,"shots":[[10,32]],"verify_opening":True,"reason":"Fixture",
                     "evidence":[{"time":[12,13]},{"time":[27,28]}]}
        payload = dict.fromkeys(streamer_judge.EDITORIAL_FIELDS,"Grounded text")
        payload.update(verdict="accept",setup_frame=1,payoff_frame=4,complete_event=True,
                       hook_options={"question":{"text":"Can he keep the car?","score":8,"supported":True,"frame":1},
                                     "emotional":{"text":"That car nearly got away","score":9,"supported":True,"frame":4}})
        def verify(**changes):
            return streamer_judge.verify_frames(candidate,[],60,{"credit":"Fixture"},True,
                lambda *_,**__:dict(payload,**changes),lambda _:b"synthetic-jpeg",True)
        result,detail=verify()
        self.assertEqual(result["verdict"],"accept")
        self.assertEqual(detail["hook"],"That car nearly got away")
        self.assertEqual(detail["verified_hook"]["choice"],"emotional")
        result,_=verify(complete_event=False)
        self.assertEqual(result["verdict"],"hold")

    def test_failed_rotation_keeps_active_key_and_redacts_storage(self):
        vault = FixtureVault()

        def send(url, headers, body, **_):
            if headers["Authorization"].endswith("-invalid"):
                raise api.ProviderError("Provider returned HTTP 401.", 401)
            return {"id": "resp_demo", "status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "RED BLUE"}]}]}

        def action(kind, **fields):
            with store.connect(self.root, True) as db:
                revision = db.execute("SELECT revision FROM provider_connections WHERE provider='sail'").fetchone()[0]
                return Connections(self.root, vault, send).action(db, "sail", kind, dict(fields, revision=revision))

        service = Connections(self.root, vault, send)
        action("add", key="fixture-secret-active")
        action("test", tier="balanced")
        service.tick()
        action("replace", key="fixture-secret-invalid", tier="balanced")
        service.tick()
        self.assertEqual(Connections(self.root, vault, send).key("sail", tier="balanced")[1], "fixture-secret-active")
        with store.connect(self.root) as db:
            self.assertNotIn("fixture-secret-", "\n".join(db.iterdump()))
        action("remove")
        self.assertEqual(vault.values, {})


class ContextTests(unittest.TestCase):
    def test_context_edits_cannot_inherit_legacy_or_other_run_reviews(self):
        legacy = dict(start=2, end=20, framing="inset", audio="original")
        first = dict(legacy, judgment_version="job-1", hook="Will it stop?")
        second = dict(legacy, judgment_version="job-2", hook="Will it stop?")
        self.assertEqual(len({editorial.plan_identity(p) for p in (legacy, first, second)}), 3)

    def test_captions_keep_time_alignment_and_strip_markup(self):
        rows = timeline.parse_captions("WEBVTT\n\n00:02.500 --> 00:04.000\n<i>Look!</i>", 3.5)
        self.assertEqual(rows, [{"start": 2.5, "end": 3.5, "text": "Look!"}])
        self.assertEqual(timeline.windows(41), [(0, 24), (12, 36), (24, 41)])

    def test_quiet_sustained_prioritizes_over_isolated_menu_peak(self):
        samples = [{"t": t, "change": .065 if 18 <= t <= 39 else .01, "audio": .005}
                   for t in range(0, 78, 3)]
        samples[18]["change"] = .5  # one high-motion jump
        rows = {row["time"]: row for row in context_rank.local_rank(samples, 78, [36], [])}
        self.assertFalse(rows[27]["rejection"])
        self.assertTrue(rows[54]["rejection"])
        self.assertIn("Interrupted", rows[36]["rejection"])

    def test_judge_rejects_unknown_evidence_and_invalid_bounds(self):
        fixture = dict(decision="accept", premise="Collision", hook="Will the plane recover?", setup="Plane flies low",
                       change="Hits the hill", payoff="The plane bursts", standout="Sudden end",
                       evidence_ids=["f1", "f2"], start=5, end=21, score=9)
        bounds = {"start": 4, "end": 24}
        frames = [{"id": "f1", "time": 8}, {"id": "f2", "time": 19}]
        self.assertEqual(context_judge.verify(json.dumps(fixture), bounds, frames, [], images=True)[0], "accept")
        for patch in ({"evidence_ids": ["f1", "f404"]}, {"end": 100}, {"payoff": "No outcome"}):
            self.assertEqual(context_judge.verify(json.dumps(dict(fixture, **patch)), bounds, frames, [],
                                                  images=True)[0], "reject")
        self.assertEqual(context_judge.verify(json.dumps(fixture), bounds, frames, [], images=False)[0], "reject")
