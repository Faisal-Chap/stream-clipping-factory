import io
import gzip
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from factory import flashdata, flashdata_profiles, source_queue, store, streamer_transfer
from factory import operator_workspace
from factory.provider_meter import ProviderPending


class FlashDataTests(unittest.TestCase):
    def test_requests_use_exact_whole_second_ranges_and_expected_quality(self):
        config = {"bucket":"fixture-bucket","endpoint":"https://fixture.invalid","access_key":"id","secret_key":"key"}
        for purpose,quality in (("audio",None),("preview","<=360"),("hd","==1080"),("hd720","==720")):
            request = flashdata.payload(config,"abcdefghijk",purpose,3600,3660,"fixture")
            self.assertEqual(request["params"]["start_at"],"01:00:00")
            self.assertEqual(request["params"]["end_at"],"01:01:00")
            self.assertEqual(request["params"].get("video_quality"),quality)
        limit = streamer_transfer.REPLAY_LIMIT
        final = flashdata.payload(config,"abcdefghijk","hd",limit-60,limit,"fixture")
        self.assertEqual(final["params"]["end_at"],"11:40:00")
        with self.assertRaisesRegex(streamer_transfer.TransferHold,"11 hours 40 minutes"):
            flashdata.payload(config,"abcdefghijk","hd",limit-60,limit+1,"fixture")

    def test_resumes_saved_provider_job_and_deletes_only_verified_owned_object(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-flashdata-transfer-") as folder:
            root = Path(folder)
            store.initialize(root,Path("tests/fixtures/VIDEO-TABLE.md"))
            with store.connect(root,True) as db:
                source,_ = store.save_source(db,{"link":"https://youtu.be/abcdefghijk"})
                job = store.enqueue(db,source)
            config = {"bucket":"fixture-bucket","endpoint":"https://fixture.invalid","access_key":"id","secret_key":"key"}
            values = {"remote_mib":128,"reserve_mib":0}
            ledger = streamer_transfer.TransferLedger(root,root/"fixture-transfer.json",values)
            client = Mock()
            client.get_object.return_value = {"Body":io.BytesIO(b"fixture")}
            alive = []
            def rpc(_config,url,body=None):
                if body:
                    prefix = body["params"]["storage"]["object_prefix"]
                    alive.append({"Key":prefix+"/clip.m4a","Size":7,"ETag":'"fixture"'})
                    return {"id":"job_fixture-1234"},""
                return {"job":{"status":"processing" if not seen else "completed"}},""
            seen = []
            def delete(**_params):
                alive.clear()
            client.delete_object.side_effect = delete
            with (patch.object(flashdata,"r2",return_value=client),patch.object(flashdata,"objects",side_effect=lambda *_:iter(alive[:])) ,
                  patch.object(flashdata,"request",side_effect=rpc) as api,
                  patch.object(streamer_transfer,"duration_ticks",return_value=2)):
                arguments = (root,config,job,"abcdefghijk","audio",0,2,ledger)
                with self.assertRaises(ProviderPending):
                    flashdata.receive(*arguments)
                file = next((root/"streamer-intake").glob("flashdata/run-*/*/request.json"))
                started = json.loads(file.read_text())["submitted_at"]
                with patch.object(flashdata.time,"time",return_value=started+flashdata.AUDIO_WAIT):
                    with self.assertRaisesRegex(flashdata.DeliveryStalled,"saved request"):
                        flashdata.receive(*arguments)
                    seen.append(True)
                    target,_ = flashdata.receive(*arguments)
                self.assertEqual(target.read_bytes(),b"fixture")
                self.assertTrue(json.loads(target.with_name("request.json").read_text())["deleted"])
                flashdata.receive(*arguments)
                self.assertEqual(sum(call.args[1]==flashdata.QUERIES for call in api.call_args_list),1)
                self.assertEqual(client.delete_object.call_count,1)
                self.assertTrue(client.delete_object.call_args.kwargs["Key"].startswith(f"{flashdata.PREFIX}/run-{job}/"))

    def test_status_polls_back_off_and_honor_retry_after(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-flashdata-backoff-") as folder:
            root = Path(folder)
            store.initialize(root,Path("tests/fixtures/VIDEO-TABLE.md"))
            with store.connect(root,True) as db:
                source,_ = store.save_source(db,{"link":"https://youtu.be/abcdefghijk"})
                job = store.enqueue(db,source)
            file = root/"streamer-intake"/"flashdata"/f"run-{job}"/"fixture"/"request.json"
            file.parent.mkdir(parents=True)
            state = {"submitted_at":1000,"remote":"job_fixture-1234","purpose":"hd"}
            for now,hint,expected in ((1000,"5",1015),(1015,"5",1045),(1045,"90",1135)):
                with patch.object(flashdata.time,"time",return_value=now), self.assertRaises(ProviderPending):
                    flashdata.pending(root,job,"fixture",file,state,hint)
                with store.connect(root) as db:
                    self.assertEqual(db.execute("SELECT next_try FROM provider_calls").fetchone()[0],expected)
            with patch.object(flashdata.time,"time",return_value=2800), self.assertRaises(flashdata.DeliveryStalled):
                flashdata.pending(root,job,"fixture",file,state,"")
            html = operator_workspace.download_diagnostics(root,{"id":job,"state":"stopped","error":"","progress":""},{"Resolve"})
            self.assertIn("Replay metadata available",html)
            self.assertIn("Download blocked",html)
            self.assertNotIn(state["remote"],html)

    def test_fallback_reuses_the_same_quality_after_a_worker_poll(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-flashdata-fallback-") as folder:
            root = Path(folder)
            left,right = 1247,1298
            receipt = flashdata.request_folder(root,1,"abcdefghijk","hd",left,right)/"request.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{"deleted":false}')
            update = Mock()
            with patch.object(flashdata,"materialize",side_effect=[
                flashdata.DeliveryStalled("hold"),ProviderPending("waiting"),{"height":720},
            ]) as materialize:
                args = (root,{},1,"abcdefghijk",left,right,None,update)
                with self.assertRaises(ProviderPending):
                    flashdata.final_picture(*args)
                self.assertEqual(flashdata.final_picture(*args),{"height":720})
            self.assertEqual([call.args[4] for call in materialize.call_args_list],["hd","hd720","hd720"])
            self.assertTrue((receipt.parent/"fallback.json").is_file())
            update.assert_called_once()


class ProductionFlashDataTests(unittest.TestCase):
    video = "abcdefghijk"
    config = {"bucket":"fixture-bucket","endpoint":"https://fixture.invalid","access_key":"id","secret_key":"key"}
    profile = {
        "id":video, "title":"Test replay", "channel_id":"UC"+"a"*22, "live_status":"was_live", "duration":85542,
        "credits":.01, "thumbnails":[],
        "formats":[
            {"format_id":"a", "resolution":"audio only", "vcodec":"none","acodec":"opus","tbr":150},
            {"format_id":"b", "resolution":"audio only", "vcodec":"none","acodec":"mp4a.40.2","ext":"m4a"},
            {"format_id":"c", "resolution":"1920x1080", "vcodec":"vp9","acodec":"none","fps":60},
            {"format_id":"d", "resolution":"1920x1080", "vcodec":"avc1.64002A","acodec":"none","fps":60},
            {"format_id":"e", "resolution":"1280x720", "vcodec":"avc1.64001F","acodec":"none","fps":30},
            {"format_id":"f", "resolution":"854x480", "vcodec":"avc1.4d401f","acodec":"none","fps":30},
            {"format_id":"g", "resolution":"640x360", "vcodec":"avc1.4d401e","acodec":"none","fps":30},
        ],
    }

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-production-flashdata-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        store.initialize(self.root,Path("tests/fixtures/VIDEO-TABLE.md"))
        with store.connect(self.root,True) as db:
            self.source,_ = store.save_source(db,{"link":"https://youtu.be/"+self.video})
            self.job = store.enqueue(db,self.source)

    def test_normalized_identity_caches_metadata_and_records_refresh(self):
        canonical,_ = source_queue.link("https://youtu.be/"+self.video+"?t=100")
        with store.connect(self.root,True) as db:
            duplicate,_ = store.save_source(db,{"link":canonical})
        self.assertEqual(duplicate,self.source)
        with patch.object(flashdata,"metadata",return_value=self.profile) as lookup:
            for _ in range(2):
                saved = flashdata_profiles.source(self.root,self.config,self.video)
                self.assertEqual(saved["id"],self.video)
                self.assertEqual(saved["selection"]["hd"][:2],["d","c"])
            lookup.assert_called_once()
            flashdata_profiles.source(self.root,self.config,self.video,refresh=True)
            self.assertEqual(lookup.call_count,2)
        with store.connect(self.root) as db:
            self.assertEqual(flashdata_profiles.usage(db,time.time())["credits"],.02)

    def test_refresh_keeps_the_previous_profile_marked_stale(self):
        with patch.object(flashdata,"metadata",side_effect=[
            self.profile,streamer_transfer.TransferHold("lookup failed"),
        ]):
            flashdata_profiles.source(self.root,self.config,self.video)
            with store.connect(self.root,True) as db:
                db.execute("UPDATE flashdata_profiles SET fetched=0")
            self.assertTrue(flashdata_profiles.source(self.root,self.config,self.video)["stale"])

    def test_exact_selectors_and_audio_are_ranked_independently(self):
        policy = flashdata_profiles.DEFAULTS
        final = flashdata_profiles.rank(self.profile,"hd",policy)
        self.assertEqual([item["format_id"] for item in final],["d","c","e","f"])
        self.assertEqual([item["format_id"] for item in flashdata_profiles.rank(self.profile,"audio",policy)],["b","a"])
        self.assertEqual([item["format_id"] for item in flashdata_profiles.rank(self.profile,"preview",policy)],["g"])
        req = flashdata.payload(self.config,self.video,"hd",1247,1257,"test",final[0])["params"]
        self.assertEqual((req["yt_dlp_format"],req["start_at"],req["end_at"]),("d","00:20:47","00:20:57"))
        self.assertTrue({"video_quality","fps","audio_language"}.isdisjoint(req))

    def test_stalled_attempt_persists_and_fallback_survives_resume(self):
        ledger = Mock()
        primary = flashdata_profiles.rank(self.profile,"hd",flashdata_profiles.DEFAULTS)[0]
        attempt = flashdata.request_folder(self.root,self.job,self.video,"hd",0,10,primary)
        attempt.mkdir(parents=True)
        (attempt/"request.json").write_text('{"remote":"job_acceptance-1234"}')
        target = self.root/"sample"
        target.write_bytes(b"media")
        from factory import media
        asset = {"height":1080,"start":0,"end":10,"path":"sample","hash":media.checksum(target)}
        args = (self.root,self.config,self.job,self.video,"hd",0,10,ledger)
        with (patch.object(flashdata_profiles,"source",return_value=self.profile),
              patch.object(flashdata,"materialize",side_effect=[
                  flashdata.DeliveryStalled("timeout"),ProviderPending("pending"),asset,
              ]) as download):
            with self.assertRaises(ProviderPending):
                flashdata.acquire(*args)
            self.assertEqual(flashdata.acquire(*args)["height"],1080)
            self.assertEqual(flashdata.acquire(*args)["height"],1080)
            self.assertEqual([call.kwargs["rendition"]["format_id"] for call in download.call_args_list],["d","c","c"])
        self.assertIn('"check_after"',(attempt/"request.json").read_text())

    def test_budget_reservation_is_atomic_and_ambiguous_submissions_keep_the_hold(self):
        now = time.time()
        with store.connect(self.root,True) as db:
            self.assertTrue(flashdata_profiles.reserve(db,"first",self.video,"hd",59,now))
            self.assertFalse(flashdata_profiles.reserve(db,"first",self.video,"hd",59,now))
        with store.connect(self.root,True) as db:
            with self.assertRaisesRegex(streamer_transfer.TransferBudget,"monthly"):
                flashdata_profiles.reserve(db,"second",self.video,"hd",2,now)
        flashdata_profiles.record(self.root,"first","verified",credits=.07,size=35000000)
        with store.connect(self.root,True) as db:
            self.assertEqual(flashdata_profiles.usage(db,now)["bytes"],35000000)
            self.assertTrue(flashdata_profiles.reserve(db,"second",self.video,"hd",2,now))

    def test_concurrent_workers_grant_one_submission(self):
        def claim(_):
            with store.connect(self.root,True) as db:
                return flashdata_profiles.reserve(db,"shared-request",self.video,"hd",1,time.time())
        with ThreadPoolExecutor(max_workers=4) as workers:
            occupied = list(workers.map(claim,range(8)))
        self.assertEqual(occupied.count(True),1)
        with store.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM flashdata_usage").fetchone()[0],1)

    def test_result_uses_standard_auth_then_follows_gzip_redirect_without_credentials(self):
        origin = "https://"+"a"*32+".r2.cloudflarestorage.com"
        signed = origin+"/signed-result?temporary=fixture"
        config = self.config | {"api_key":"fixture-token"}
        request_prefix = "factory/request"
        report = {"results":[{"file_url":"https://fixture.invalid/fixture-bucket/factory/request/sample.mp4",
                              "file_size":8,"md5":"a"*32}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = gzip.compress(json.dumps(report).encode())
        opener = Mock()
        opener.open.side_effect = [HTTPError("https://api.flashdata.dev",302,"",{"Location":signed},None),response]
        with (patch.object(flashdata,"build_opener",return_value=opener),
              patch.object(flashdata,"request",return_value=({"result_origin":origin},""))):
            result = flashdata.result(config,"job_fixture-1234",request_prefix)
        first,second = (call.args[0] for call in opener.open.call_args_list)
        self.assertEqual(first.get_header("Accept"),"application/json")
        self.assertEqual(first.get_header("X-api-key"),"fixture-token")
        self.assertEqual(second.full_url,signed)
        self.assertIsNone(second.get_header("X-api-key"))
        self.assertEqual(result["key"],"factory/request/sample.mp4")
