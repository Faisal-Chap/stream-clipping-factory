import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from unittest.mock import Mock, patch

from factory import (factory_events, operator_workspace, replay_cache, store, streamer_asr,
                     streamer_story_v4, streamer_transfer, web)


class FullReplayCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-replay-cache-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        store.initialize(self.root,Path("tests/fixtures/VIDEO-TABLE.md"))
        with store.connect(self.root,True) as db:
            self.source,_ = store.save_source(db,{"link":"https://youtu.be/fixture0002"})
            self.job = store.enqueue(db,self.source,fresh=True,overrides={
                "method":"streamer","full_replay":True,"coverage_start":0,"directory_part":0})
            db.execute("INSERT INTO portal_source_metadata VALUES(?,?)",(self.source,3600*20+12))
        self.row = {"id":self.job,"source":self.source,"revision":1}
        self.cache = replay_cache.directory(self.root,self.source,1)
        self.cache.mkdir(parents=True)
        self.file = self.cache/"replay.json"

    def test_retention_deadline_is_the_earlier_limit(self):
        self.assertEqual(replay_cache.expires({"downloaded_at":100,"completed_at":200}),200+12*3600)
        self.assertEqual(replay_cache.expires({"downloaded_at":100,"completed_at":100+20*3600}),100+24*3600)
        self.assertEqual(replay_cache.expires({"downloaded_at":100}),100+24*3600)

    def test_retirement_removes_only_owned_source_recording(self):
        video = self.cache/"recording.mp4"
        video.write_bytes(b"recording")
        exported = self.root/"finished-portrait.mp4"
        exported.write_bytes(b"keep")
        streamer_asr.write(self.file,{"state":"verified","downloaded_at":100,
                                      "asset":{"path":video.relative_to(self.root).as_posix()}})
        with patch.object(replay_cache.time,"time",return_value=100+24*3600):
            replay_cache.reconcile(self.root)
        self.assertFalse(video.exists())
        self.assertTrue(exported.is_file())
        self.assertEqual(replay_cache.state(self.root,self.source,1)["state"],"expired")
        with self.assertRaises(streamer_transfer.TransferHold):
            replay_cache.guard(self.root,self.row)

    def test_only_job_scoped_local_media_is_available_for_playback(self):
        window = self.cache/"working"/f"run-{self.job}"/"hd-1.000000-31.000000-source.mp4"
        window.parent.mkdir(parents=True)
        window.touch()
        recording = self.cache/"recording.mp4"
        recording.touch()
        self.assertEqual(window.resolve(),operator_workspace.range_artifact(self.root,self.job,window))
        self.assertIsNone(operator_workspace.range_artifact(self.root,self.job+1,window))
        self.assertIsNone(operator_workspace.range_artifact(self.root,self.job,recording))

    def test_story_selection_sends_only_timed_transcript_and_editorial_prompt(self):
        sail = Mock(return_value={"moments":[]})
        result = streamer_story_v4.leads([[1,2.1,2.8,"Look.",.91]],0,3600,sail,Mock())
        self.assertEqual(result[:2],([],[]))
        request = sail.call_args.args[1]
        self.assertIn("2.100-2.800 W1-1 | Look.",request)
        self.assertIn("video_title",request)
        self.assertIn("25–150 seconds",request)
        self.assertNotIn("OCR",request)
        self.assertNotIn(".91",request)

    def test_story_selection_accepts_150_seconds_and_rejects_longer(self):
        start = 1260.99
        words = [[1,1261,1262,"Setup.",1],[2,1409,1410,"Payoff.",1]]
        by_id = {word[0]:word for word in words}
        moment = dict(start=start,end=start+150,words=[1,2],premise="The setup reaches a payoff.",
                      hook="Wait for the payoff",video_title="A good setup reaches an unexpected payoff")
        for end, expected in ((1410.99,1),(1411.00,0)):
            selected,invalid,_ = streamer_story_v4.validate(
                {"moments":[dict(moment,end=end)]},words,by_id,0,3600)
            self.assertEqual(len(selected),expected)
            self.assertEqual(bool(invalid),not expected)

    def test_story_progress_counts_saved_portraits_and_unfinished_separately(self):
        folder = self.root/"streamer-runs"/str(self.job)
        folder.mkdir(parents=True)
        self.assertEqual(operator_workspace.story_progress(self.root,self.job,0,[]),"")
        streamer_asr.write(folder/"selection.json",{"shortlist":["c1-1","c1-2","c2-1"],
            "proposals":[{"id":"c1-1","start":5},{"id":"c1-2","start":60},{"id":"c2-1","start":3650}]})
        streamer_asr.write(folder/"hybrid-results.json",{"c1-2":{"outcome":{"state":"technical"}}})
        panel = operator_workspace.story_progress(self.root,self.job,0,[("c1-1",)])
        self.assertIn('value="1" max="2"',panel)
        self.assertIn("1 of 2 selected reels rendered · 1 unfinished",panel)

    def test_audio_extraction_restricts_ffmpeg_to_the_cached_file(self):
        recording = self.cache/"recording.mp4"
        recording.touch()
        def transcode(args, timeout):
            self.assertEqual(args[args.index("-protocol_whitelist")+1],"file")
            self.assertEqual(Path(args[args.index("-i")+1]),recording.resolve())
            Path(args[-1]).write_bytes(b"local FLAC")
            return Mock(returncode=0)
        with (patch.object(replay_cache.media,"require",return_value="ffmpeg"),
              patch.object(replay_cache.media,"run",side_effect=transcode) as run,
              patch.object(streamer_transfer,"duration_ticks",return_value=60)):
            asset = {"path":recording.relative_to(self.root).as_posix()}
            first = replay_cache.local_range(self.root,self.row,asset,0,60,"audio")
            self.assertEqual(first,replay_cache.local_range(self.root,self.row,asset,0,60,"audio"))
        self.assertEqual(first["transfer"],0)
        run.assert_called_once()

    def test_live_log_returns_only_new_worker_events_after_cursor(self):
        with store.connect(self.root,True) as db:
            factory_events.record(db,self.job,"progress",item="Starting FFmpeg")
        with web.FactoryServer(0,self.root,Path("tests/fixtures/VIDEO-TABLE.md"),None) as server:
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                url = f"{server.url}/jobs/{self.job}/live-log"
                client = build_opener(ProxyHandler({}))
                with client.open(url,timeout=3) as response:
                    cursor = response.headers["X-Factory-Log-Cursor"]
                    self.assertIn("Starting FFmpeg",response.read().decode())
                with store.connect(self.root,True) as db:
                    factory_events.record(db,self.job,"pulse",item="Starting FFmpeg")
                    factory_events.record(db,self.job,"progress",item="Local FLAC ready")
                with client.open(f"{url}?after={cursor}",timeout=3) as response:
                    output = response.read().decode()
                self.assertIn("Local FLAC ready",output)
                self.assertNotIn("Starting FFmpeg",output)
            finally:
                server.shutdown()
                thread.join()

    def test_extension_survives_completion_and_prevents_original_expiry(self):
        video = self.cache/"recording.mp4"
        video.write_bytes(b"recording")
        streamer_asr.write(self.file,{"state":"verified","downloaded_at":100,
                                      "asset":{"path":video.relative_to(self.root).as_posix()}})
        with patch.object(replay_cache.time,"time",return_value=200):
            replay_cache.extend(self.root,self.source,1,12)
            replay_cache.extend(self.root,self.source,1,24)
        item = streamer_asr.read(self.file)
        item["completed_at"] = 300
        streamer_asr.write(self.file,item)
        self.assertEqual(replay_cache.expires(item),100+60*3600)
        with patch.object(replay_cache.time,"time",return_value=100+24*3600):
            replay_cache.retire(self.root)
        self.assertTrue(video.exists())

    def test_source_pause_and_resume_reuses_the_current_job(self):
        with store.connect(self.root,True) as db:
            replay_cache.set_paused(self.root,db,self.source,True)
            self.assertEqual("paused",db.execute("SELECT state FROM sources WHERE id=?",(self.source,)).fetchone()[0])
            self.assertEqual("stopped",db.execute("SELECT state FROM jobs WHERE id=?",(self.job,)).fetchone()[0])
            replay_cache.set_paused(self.root,db,self.source,False)
            self.assertEqual([(self.job,"queued")],[tuple(r) for r in db.execute("SELECT id,state FROM jobs")])
            db.execute("UPDATE jobs SET state='running' WHERE id=?",(self.job,))
            replay_cache.set_paused(self.root,db,self.source,True)
            self.assertEqual(1,db.execute("SELECT stop FROM jobs WHERE id=?",(self.job,)).fetchone()[0])
            db.execute("UPDATE jobs SET state='complete',stop=0 WHERE id=?",(self.job,))
            replay_cache.on_completed(self.root,db,self.job)
            self.assertEqual(1,db.execute("SELECT count(*) FROM jobs").fetchone()[0])
            replay_cache.set_paused(self.root,db,self.source,False)
            self.assertEqual(2,db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_resume_polls_saved_receipt_using_same_recording(self):
        streamer_asr.write(self.file,{"state":"pending","remote":"job-saved","route":"/api/progress","duration":4000})
        part = self.cache/"recording.partial.mp4"
        part.write_bytes(b"fixture")
        client = Mock()
        client.progress.return_value = (1000,"https://dave15.savenow.to/file",None)
        media = {"path":(self.cache/"recording.mp4").relative_to(self.root).as_posix(),"bytes":7,"duration":4000}
        with (patch.object(replay_cache,"cache_client",return_value=client),
              patch.object(replay_cache,"transfer_file",return_value=part),
              patch.object(replay_cache,"verified",return_value=media)):
            self.assertEqual(replay_cache.acquire(self.root,self.row,{},{}),media)
            self.assertEqual(replay_cache.acquire(self.root,self.row,{},{}),media)
        client.submit.assert_not_called()
        client.progress.assert_called_once_with("job-saved","/api/progress")

    def test_next_hour_continues_past_legacy_limit_without_duplicating_run(self):
        with store.connect(self.root,True) as db:
            db.execute("""UPDATE jobs SET state='complete',
                      settings=json_set(settings,'$.coverage_start',43200,'$.directory_part',43200) WHERE id=?""",
                       (self.job,))
            replay_cache.on_completed(self.root,db,self.job)
            replay_cache.on_completed(self.root,db,self.job)
            rows = db.execute("SELECT state,json_extract(settings,'$.coverage_start') FROM jobs ORDER BY id").fetchall()
            self.assertEqual([tuple(r) for r in rows],[("complete",43200),("queued",46800)])
        self.assertEqual(replay_cache.hours(20*3600+12)[-1],(20*3600,20*3600+12))

    def test_progress_exposes_bytes_and_absolute_transcription_coverage(self):
        streamer_asr.write(self.file,{"state":"transfer","received_bytes":1024,"expected_bytes":4096})
        with store.connect(self.root,True) as db:
            db.execute("UPDATE jobs SET stage='Transcribe' WHERE id=?",(self.job,))
            factory_events.record(db,self.job,"progress",covered=3900,total=7200)
            progress = replay_cache.progress(self.root,db,self.source,1)
        self.assertEqual((progress["bytes"],progress["expected"]),(1024,4096))
        self.assertEqual((progress["transcribed"],progress["duration"]),(3900,7200))

    def test_retention_waits_for_unheld_outcomes_in_each_part(self):
        with store.connect(self.root,True) as db:
            db.execute("UPDATE portal_source_metadata SET duration=7200 WHERE source=?",(self.source,))
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(self.job,))
            replay_cache.on_completed(self.root,db,self.job)
            last = db.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
            db.execute("UPDATE jobs SET state='complete' WHERE id=?",(last,))
            streamer_asr.write(self.file,{"state":"verified","downloaded_at":100})
            for index in (self.job,last):
                output = self.root/"streamer-runs"/str(index)
                output.mkdir(parents=True)
                streamer_asr.write(output/"production.json",{"partial":False,
                    "items":[{"state":"held"}] if index==last else []})
            replay_cache.on_completed(self.root,db,last)
            self.assertNotIn("completed_at",streamer_asr.read(self.file))
            streamer_asr.write(output/"production.json",{"partial":False,"items":[{"state":"qualified"}]})
            replay_cache.on_completed(self.root,db,last)
            self.assertIn("completed_at",streamer_asr.read(self.file))
