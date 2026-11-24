import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from factory import streamer_asr, video_download_api as vda


class VideoDownloadExperimentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-vda-tests-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.profile = {"id":"fixture0001","duration":85542,"quotes_microusd":{"480":200,"1080":1600}}
        self.args = argparse.Namespace(interval=(1247,1307), audio=False, extended_minutes=0, reference=None,
                                       audio_reference=None, reference_origin=0, wait=0, max_bytes=8000000,
                                       budget_microusd=10000)
        self.client = Mock()
        self.client.reserve.side_effect = lambda profile,fmt,args:vda.reservation(profile,fmt,args.extended_minutes)
        self.client.resolve_progress.side_effect = vda.progress_route

    def test_default_route_leaves_flashdata_selected(self):
        with patch.dict(vda.os.environ,{},clear=True):
            self.assertEqual(vda.configured_provider(),"flashdata")

    def test_legacy_uses_timestamps_and_v2_is_reserved_for_full_sources(self):
        cut = vda.request_fields(self.profile,"1080",(1247,1307),0)
        self.assertEqual((cut["start_time"],cut["end_time"],cut["allow_extended_duration"]),(1247,1307,0))
        with self.assertRaisesRegex(vda.EvaluationError,"Full-source downloads over 180"):
            vda.request_fields(self.profile,"1080",None,0)
        short = dict(self.profile,duration=3638)
        whole = vda.request_fields(short,"1080",None,0)
        self.assertNotIn("start_time",whole)
        client = vda.Client("fixture")
        with patch.object(client,"read",return_value={"id":"job123","success":True}) as read:
            client.submit(cut,clipping=True)
            self.assertIn("/ajax/download.php?",read.call_args.args[0])
            client.submit(whole,clipping=False)
            self.assertIn("/api/v2/download?",read.call_args.args[0])
            self.assertNotIn("start_time",read.call_args.args[0])

    def test_progress_contract(self):
        client = vda.Client("fixture")
        link = vda.BASE+"/storage/downloads/fixture/movie.mp4"
        with patch.object(client,"read",side_effect=[
            {"progress":0,"success":False},
            {"progress":999,"success":True,"download_url":link},
            {"progress":1000,"success":1,"download_url":link},
            {"progress":1000,"success":True,"download_url":""},
        ]):
            self.assertEqual(client.progress("job"),(0,None,None))
            self.assertEqual(client.progress("job"),(999,None,None))
            self.assertEqual(client.progress("job"),(1000,link,None))
            self.assertEqual(client.progress("job"),(1000,None,"provider_failure"))
        self.assertEqual(vda.progress_route(vda.BASE+"/api/progress?id=job","job"),"/api/progress")
        with self.assertRaises(vda.EvaluationError):
            vda.progress_route(vda.BASE+"/api/progress?id=job&api=do-not-log","job")
        self.assertEqual(vda.failure_reason({"message":"Download link expired."}),"delivery_link_expired")

    def test_live_metadata_clock_strings_are_parsed_as_seconds(self):
        client = vda.Client("fixture")
        with patch.object(client,"read",return_value={"id":self.profile["id"],"duration":"23:45:42"}):
            self.assertEqual(client.metadata(self.profile["id"])["duration"],85542)
        self.assertEqual(vda.duration_seconds("1:00:39"),3639)
        self.assertIsNone(vda.duration_seconds("1:60:39"))

    def test_live_delivery_hosts_get_no_api_credential(self):
        client = vda.Client("fixture")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {"Content-Length":"3"}
        response.read.side_effect = [b"mp4",b""]
        with patch.object(client.opener,"open",return_value=response) as request:
            target = self.folder/"sample"
            vda.receive(client,"https://dave15.savenow.to/media",target,1024)
            self.assertEqual(target.read_bytes(),b"mp4")
            self.assertNotIn("fixture",request.call_args.args[0].full_url)
            with self.assertRaisesRegex(vda.EvaluationError,"unapproved"):
                vda.receive(client,"https://logan14.savenow.to.evil.invalid/media",target,1024)
            request.assert_called_once()

    def test_marketplace_key_stays_on_gateway_and_usage_is_redacted(self):
        journal = self.folder/"quota.json"
        client = vda.RapidClient("separate-marketplace-key",vda.RAPID_HOST,journal,5)
        self.assertEqual(client.download_endpoint(False),"/ajax/download.php")
        gateway = client.api_url("/ajax/api.php",{"u":"source"},"api")
        self.assertNotIn(client.key,gateway)
        self.assertEqual(client.headers(vda.RAPID_HOST)["X-RapidAPI-Key"],client.key)
        self.assertNotIn("X-RapidAPI-Key",client.headers(vda.HOST))
        with self.assertRaisesRegex(vda.Rejected,"preflight"):
            client.record_request("/ajax/download.php",True,True)
        metadata = client.record_request("/ajax/api.php",True,False)
        client.record_response(metadata,200,{"X-RateLimit-Units-Remaining":"500","X-API-Key":client.key})
        entry = client.record_request("/ajax/download.php",True,True)
        client.record_response(entry,200,{"X-RateLimit-Units-Remaining":"498"})
        self.assertNotIn(client.key,journal.read_text())
        self.assertEqual(streamer_asr.read(journal)[1]["quota"]["x-ratelimit-units-remaining"],498)
        self.assertEqual(client.resolve_progress(vda.BASE+"/api/progress?id=job","job"),vda.BASE+"/api/progress")
        self.assertEqual(client.resolve_progress("https://unknown.invalid/api/progress?id=job","job"),
                         f"https://{vda.RAPID_HOST}/api/progress")

    def test_stall_holds_one_job_and_resume_uses_the_same_id(self):
        self.client.submit.return_value = ("job123", None)
        self.client.progress.side_effect = [(0,None,None),(500,None,None)]
        for _ in range(2):
            result = vda.evaluate(self.client,self.folder,self.profile,self.args)
            self.assertEqual((result["state"],result["id"]),("stalled","job123"))
        self.client.submit.assert_called_once()
        self.assertEqual([c.args[0] for c in self.client.progress.call_args_list],["job123","job123"])

    def test_ambiguous_submission_holds_reservation_and_prevents_second_request(self):
        self.client.submit.side_effect = vda.EvaluationError("Submission outcome is unknown.")
        for _ in range(2):
            with self.assertRaisesRegex(vda.EvaluationError,"outcome is unknown"):
                vda.evaluate(self.client,self.folder,self.profile,self.args)
        self.client.submit.assert_called_once()
        [record] = streamer_asr.read(self.folder/"attempts.json")
        self.assertEqual((record["state"],record["reserved_microusd"]),("submitting",1600))

    def test_terminal_format_failures_fall_back_serially(self):
        self.client.submit.side_effect = [("one",None),("two",None),("three",None)]
        self.client.progress.side_effect = [(1000,None,"unsupported_format"),(1000,None,"provider_failure"),
                                            (1000,vda.BASE+"/download",None)]
        with (patch.object(vda,"receive",side_effect=lambda _,_url,path,_size:path.write_bytes(b"media")),
              patch.object(vda,"verify",return_value={"height":480})):
            result = vda.evaluate(self.client,self.folder,self.profile,self.args)
        self.assertEqual((result["id"],result["probe"]["height"]),("three",480))
        self.assertEqual([c.args[0]["format"] for c in self.client.submit.call_args_list],["1080","720","480"])

    def test_duration_rejection_and_budget_stop_before_another_job(self):
        self.args.budget_microusd = 0
        with self.assertRaisesRegex(vda.EvaluationError,"test budget"):
            vda.evaluate(self.client,self.folder,self.profile,self.args)
        self.client.submit.assert_not_called()
        self.args.budget_microusd = 2000
        self.client.submit.return_value = ("job123", None)
        self.client.progress.return_value = (1000,None,"source_duration_limit")
        result = vda.evaluate(self.client,self.folder,self.profile,self.args)
        self.assertEqual(result["error"],"source_duration_limit")
        self.client.submit.assert_called_once()

    def test_real_ffmpeg_checks_video_and_extracts_the_local_interval(self):
        ffmpeg = vda.media.tool("ffmpeg")
        if not ffmpeg or not vda.media.tool("ffprobe"):
            self.skipTest("FFmpeg and FFprobe are required for the synthetic media test.")
        source = self.folder/"fixture.mp4"
        output = vda.media.run([ffmpeg,"-v","error","-f","lavfi","-i","testsrc2=size=854x480:rate=30",
                                "-f","lavfi","-i","sine=frequency=440:sample_rate=48000",
                                "-t","4","-c:v","libx264","-preset","ultrafast","-c:a","aac",str(source)],30)
        self.assertEqual(output.returncode,0,output.stderr.decode(errors="replace"))
        probe = vda.verify(source,"480",4)
        self.assertEqual((probe["height"],probe["audio_codec"],probe["fps"]),(480,"aac","30/1"))
        with self.assertRaises(vda.Rejected):
            vda.verify(source,"480",60)
        item = vda.extract(self.folder,{"file":source.name,"request":{"format":"480"},"probe":probe},(1,3),8000000)
        self.assertAlmostEqual(item["probe"]["duration"],2,places=1)
        self.assertTrue((self.folder/item["file"]).is_file())
