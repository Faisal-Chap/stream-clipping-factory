"""Audio-first fixture evidence. Platform and provider claims are outside these tests."""

import io
import json
import re
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from factory import (jobs, media, portal_settings, store, streamer_asr, streamer_catalog, streamer_event_review,
                     streamer_intake as intake, streamer_judge, streamer_layout, streamer_run, streamer_clip_repair,
                     streamer_transfer as transfer)
from factory.provider_meter import ProviderPending


TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()


def proposal(number, shots, setup, payoff):
    return dict(id=f"c1-{number}",start=shots[0][0],end=shots[-1][-1],shots=shots,
                reason="Specific fixture action and reaction.",score=9,rejection="",
                evidence=[dict(time=[setup,setup+.4],quote="Let's try this."),
                          dict(time=[payoff,payoff+.4],quote="That worked.")])


class PartPlanTests(unittest.TestCase):
    def test_exact_part_boundaries_and_replay_tail(self):
        self.assertEqual(transfer.parts(14400),[(0,14400)])
        self.assertEqual(transfer.parts(14401),[(0,14400),(14400,14401)])
        self.assertEqual(transfer.parts(85542),[(0,14400),(14400,28800),(28800,43200),(43200,57600),
                                               (57600,72000),(72000,85542)])

    def test_global_word_ids_and_deduplication_keep_two_separated_shots(self):
        def words(left,right,word):
            return {"words":[[1,left,right,word,.99]],"rms_dbfs_by_second":[-16]*14400}
        text = intake.aggregate([({"start":0,"end":14400},words(14392,14393,"Setup")),
                                 ({"start":14400,"end":28800},words(4,5,"Payoff"))],0,28800)
        self.assertEqual(text["words"],[[1,14392,14393,"Setup",.99],[2,14404,14405,"Payoff",.99]])
        story = proposal(1,[[14383,14395],[14402,14417]],14392,14404)
        duplicate = proposal(2,[[14382,14395],[14401,14417]],14392,14404)
        selected = intake.plan_selection([story,duplicate],text,0,28800,7,10)
        self.assertEqual(len(selected),1)
        self.assertEqual(intake.union(shot for row in selected for shot in row["shots"]),
                         [[14383,14395],[14402,14417]])
        self.assertIn("same payoff",duplicate["rejection"])

    def test_overlaps_merge_without_requesting_intervening_minutes(self):
        self.assertEqual(intake.union([(100,120),(500,525),(116,132),(510,519)]),[[100,132],[500,525]])

    def test_only_distinct_prospects_enter_the_reserve(self):
        first = proposal(1,[[3601,3621]],3604,3619)
        duplicate = proposal(2,[[3600,3621]],3604,3619)
        reserve = proposal(3,[[3630,3650]],3634,3649)
        selected = intake.plan_selection([first,duplicate,reserve],{"words":[]},3600,7200,1,3,reserves=1)
        self.assertEqual([(item["id"],item["reserve"]) for item in selected],[("c1-1",False),("c1-3",True)])
        self.assertEqual(selected[0]["inspection"],[[3600,3624]])

    def test_repair_requires_a_nearby_exact_phrase_and_never_reopens_a_contradiction(self):
        candidate = proposal(1,[[3608,3629]],3610,3624)
        words = [[i,start,start+.19,word,.95] for i,(start,word) in enumerate(
            [(3604.2,"here"),(3604.5,"it"),(3604.8,"comes"),(3644,"far"),(3644.2,"too"),(3644.4,"late")],1)]
        earlier = {"direction":"earlier","words":[1,3]}
        shots,cue = streamer_clip_repair.revised_cut(candidate,words,{"verdict":"hold","repair":earlier},3600,7200)
        self.assertEqual((shots,cue["quote"]),([[3603.6,3629]],"here it comes"))
        for verdict,direction,ids in (("reject","earlier",[1,3]),("hold","later",[4,6]),("hold","earlier",[1,2])):
            self.assertIsNone(streamer_clip_repair.revised_cut(candidate,words,
                {"verdict":verdict,"repair":{"direction":direction,"words":ids}},3600,7200))
        self.assertEqual(streamer_clip_repair.revised_cut(candidate,words,
            {"verdict":"hold","repair":{"direction":"dense"}},3600,7200),(candidate["shots"],None))

    def test_reconciled_word_spanning_the_split_has_one_absolute_caption(self):
        first = {"words":[[1,14399.7,14400,"land",.68]],"rms_dbfs_by_second":[]}
        next_part = {"words":[[1,0,.18,"ing",.61]],"rms_dbfs_by_second":[]}
        shifted = [({"start":0,"end":14400},first),({"start":14400,"end":14401},next_part)]
        text = intake.aggregate(shifted,0,14401,[(14400,[[0,14399.7,14400.18,"landing",.99]])])
        self.assertEqual(text["words"],[[1,14399.7,14400.18,"landing",.99]])


class RangeReaderTests(unittest.TestCase):
    def test_nonzero_keyframe_seek_and_audio_part_seam(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-source-clock-test-") as folder:
            root = Path(folder)
            original = root/"fixture-replay.mp4"
            ffmpeg = media.require("ffmpeg")
            tone = ("aevalsrc=if(between(t\\,9.8\\,10.1)\\,.2*sin(1500*t+1200*(t-9.8)*(t-9.8))"
                    "+.18*sin(3000*t+3300*(t-9.8)*(t-9.8))\\,0):s=48000:d=32")
            generated = media.run([ffmpeg,"-v","error","-y","-f","lavfi","-i",
                "testsrc2=size=160x90:rate=10:duration=3622", "-f","lavfi","-i",tone,
                "-vf","drawbox=0:0:40:40:red:t=fill:enable='gte(t,3600.4)'",
                "-af","adelay=3590000:all=1","-c:v","libx264","-preset","ultrafast","-g","30","-sc_threshold","0",
                "-threads","2","-c:a","aac","-movie_timescale","48000","-movflags","+faststart",str(original)],140)
            self.assertEqual(generated.returncode,0,generated.stderr)
            length = original.stat().st_size
            def upstream(request, timeout):
                left,right = map(int,re.fullmatch(r"bytes=(\d+)-(\d+)",request.headers["Range"]).groups())
                with original.open("rb") as raw:
                    raw.seek(left)
                    response = io.BytesIO(raw.read(right-left+1))
                response.status = 206
                response.headers = {"Content-Range":f"bytes {left}-{right}/{length}"}
                return response
            item = {"url":"https://fixture.invalid/replay","format_id":"clock", "height":90,
                    "filesize":length,"source_duration":3622}
            ledger = transfer.TransferLedger(root,root/"clock-transfer.json",{"remote_mib":64,"reserve_mib":0})
            for name,kind,start,end in (("first.flac","audio",3598,3600),("second.flac","audio",3600,3617),
                                        ("picture.mp4","video",3598,3617)):
                transfer.acquire(item,start,end,kind,root/name,ledger,opener=upstream)
            index = media.run([media.require("ffprobe"),"-v","error","-read_intervals","3598%+#1","-select_streams","v:0",
                               "-show_entries","packet=pts_time,flags","-of","json",str(original)],12)
            packet = json.loads(index.stdout)["packets"][0]
            self.assertEqual(float(packet["pts_time"]),3597)
            self.assertIn("K",packet["flags"])

            def pixel(path, at):
                result = media.run([ffmpeg,"-v","error","-ss",str(at),"-i",str(path),"-frames:v","1",
                                    "-vf","crop=16:16:8:8,scale=1:1,format=rgb24","-f","rawvideo","-"],20)
                self.assertGreaterEqual(len(result.stdout),3)
                return tuple(result.stdout[:3])

            at = pixel(root/"picture.mp4",2.42)
            self.assertGreater(at[0]-max(at[1:]),160)
            self.assertLess(max(abs(a-b) for a,b in zip(at,pixel(original,3600.42))),15)
            self.assertLess(max(abs(a-b) for a,b in zip(pixel(root/"picture.mp4",0),pixel(original,3598))),20)
            parts = [{"start":a,"end":b,"path":name} for a,b,name in (
                     (3598,3600,"first.flac"),(3600,3617,"second.flac"))]
            original_picture = {"path":"picture.mp4","start":3598,"end":3617,"hash":media.checksum(root/"picture.mp4")}
            joined = intake.join_picture(root,root/"retained",parts,
                [(3598,3600,original_picture),(3600,3617,original_picture)],3598,3617)
            reference = root/"linear-reference.wav"
            result = media.run([ffmpeg,"-v","error","-vn","-i",str(original),"-af",
                "aresample=48000:first_pts=0,atrim=start_sample=172788480:end_sample=172808640,"
                "asetpts=PTS-STARTPTS",str(reference)],100)
            self.assertEqual(result.returncode,0,result.stderr)
            cue = streamer_clip_repair.cue_alignment(reference,0,root/joined["path"],1.76,.42)
            self.assertLessEqual(abs(cue["offset_ms"]),1,cue)
            self.assertGreater(cue["correlation"],.98,cue)
            self.assertGreater(ledger.measure("video"),0)

    def test_upstream_must_return_exact_partial_content_before_any_body_read(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-range-reader-test-") as folder:
            root = Path(folder)
            item = {"url":"https://fixture.invalid/audio","format_id":"fixture-audio","filesize":8*media.CHUNK,
                    "source_duration":600}
            for expected in (206,200):
                with self.subTest(http_status=expected):
                    reads = []

                    class Payload(io.BytesIO):
                        def read(self, length=-1):
                            reads.append(length)
                            return super().read(length)

                    def upstream(request, timeout):
                        start,end = map(int,re.fullmatch(r"bytes=(\d+)-(\d+)",request.headers["Range"]).groups())
                        response = Payload(b"a"*(end-start+1))
                        response.status = expected
                        response.headers = {"Content-Range":f"bytes {start}-{end}/{item['filesize']}"}
                        return response

                    ledger = transfer.TransferLedger(root,root/f"gta-streamer-disposable-ledger-{expected}.json",
                                                     {"remote_mib":64,"reserve_mib":0})
                    reader = transfer.RemoteRanges(item,75,85,"audio",ledger,upstream)
                    request = urllib.request.Request
                    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with reader as url:
                        with local.open(request(url,headers={"Range":"bytes=20000-21023"}),timeout=5) as result:
                            self.assertEqual(result.read(16), b"a"*16 if expected==206 else b"")
                    self.assertEqual(reads,[1024] if expected==206 else [])
                    self.assertEqual(ledger.measure("audio"),1024 if expected==206 else 0)
                    if expected==200:
                        self.assertIn("Full transfer was refused",reader.error)

    def test_interrupted_read_reservation_counts_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-transfer-ledger-test-") as folder:
            root = Path(folder)
            path = root/"gta-streamer-disposable-interrupted-ledger.json"
            caps = {"remote_mib":1,"reserve_mib":0}
            ledger = transfer.TransferLedger(root,path,caps)
            event = ledger.begin("audio",{"format_id":"fixture"},0,10)
            def interrupt(_):
                raise OSError("Interruption after read reservation")
            with self.assertRaises(OSError):
                ledger.read(event,media.CHUNK,interrupt)
            saved = transfer.TransferLedger(root,path,caps)
            retry = saved.begin("audio",{"format_id":"fixture"},0,10)
            with self.assertRaisesRegex(transfer.TransferHold,"aggregate"):
                saved.read(retry,1,self.fail)
            self.assertEqual(saved.measure("audio"),0)


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-audio-first-worker-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store.initialize(self.root,TABLE)
        with store.connect(self.root,True) as db:
            creator = streamer_catalog.save_creator(db,{"name":"Fixture","game":"GTA V"},"UC"+"w"*22)
            self.source = streamer_catalog.attach(db,creator,{"link":"https://youtu.be/testbound01"},
                {"live_status":"was_live","channel_id":"UC"+"w"*22,"title":"Fixture replay","duration":101})
            self.job,_ = portal_settings.start(db,self.source,{"hosted":"on"})
        self.folder = self.root/"streamer-runs"/str(self.job)
        self.raw = self.root/"gta-streamer-disposable-fixture-original.mp4"
        result = media.run([media.require("ffmpeg"),"-v","error","-y","-f","lavfi","-i",
                            "testsrc2=size=320x180:rate=30","-f","lavfi","-i","sine=sample_rate=48000",
                            "-t","101","-c:v","libx264","-threads","2","-c:a","aac","-movie_timescale","48000",
                            str(self.raw)],35)
        self.assertEqual(result.returncode,0,result.stderr)
        self.moves = []
        self.fixture = {"id":"testbound01","duration":101,
                        "audio":[{"format_id":"fixture-audio","filesize":3000000}],
                        "video":[{"format_id":"fixture-picture","filesize":12000000,"height":720}]}

    def row(self):
        with store.connect(self.root) as db:
            return db.execute("SELECT * FROM jobs WHERE id=?",(self.job,)).fetchone()

    def local_fixture(self, item, start, end, kind, path, ledger):
        self.moves.append((kind,start,end))
        if kind=="video":
            selected = streamer_asr.read(self.folder/"audio-selection.json")
            self.assertEqual(selected["zero_video_bytes"],0)
            if self.fail_once and start==43:
                if self.fail_once != "always":
                    self.fail_once = False
                raise transfer.TransferHold("Fixture refused the requested range.")
        codec = (["-vn","-c:a","flac","-ac","2"] if kind=="audio"
                 else ["-an","-c:v","libx264","-threads","2"])
        result = media.run([media.require("ffmpeg"),"-v","error","-y","-ss",str(start),"-i",str(self.raw),
                            "-t",str(end-start),*codec,str(path)],40)
        self.assertEqual(result.returncode,0,result.stderr)
        event = ledger.begin(kind,item,start,end)
        ledger.read(event,4096,lambda count:b"a"*count)
        ledger.end(event,"received")
        return dict(actual=end-start,bytes=path.stat().st_size,hash=media.checksum(path),
                    rendition=item["format_id"],height=item.get("height"),transfer=4096)

    def words(self, root, asset, config, update):
        start,end = asset["start"],asset["end"]
        markers = [(7,"First try."),(48,"Try the recovery."),(55,"It worked."),
                   (65,"Then"),(65.45,"it"),(65.9,"locks."),(88,"Finally landed.")]
        values = [[i,t-start,t-start+.4,word,.99] for i,(t,word) in enumerate(markers,1) if start<=t<end]
        return None, dict(words=values,duration=end-start,rms_dbfs_by_second=[-17]*int(end-start),
                          elapsed_seconds=0,model_revision="fixture",audio_path=self.raw.name)

    def judge(self, root, asset, text, config, job, connections, chat, update, **options):
        self.assertTrue(options["audio_only"])
        self.assertNotIn("video",[kind for kind,_,_ in self.moves])
        if self.winners:
            chosen = [proposal(1,[[43,61]],48,55),proposal(2,[[5,14],[78,91]],7,88),
                      proposal(3,[[43,62]],48,55)]
        else:
            chosen = []
        return [{"start":0,"end":101,"judgment":"fixture"}],chosen,chosen

    def visual(self, candidate, words, stream, sail, read, fit_hook):
        a,b = [part["time"][0] for part in candidate["evidence"]]
        self.assertTrue(read(a))
        self.assertTrue(read(b))
        detail = dict(hook="Can this move work?",post="Fixture move.",event="fixture-"+candidate["id"],
                      complete_event=True,verified_hook={"choice":"question","options":{"question":{"score":9}}})
        return dict(verdict="accept",reason="Moving synthetic picture.",sample_seconds=[a,b],frames=[1,2]),detail

    def rig(self, recipe=4):
        with store.connect(self.root,True) as db:
            db.execute("""UPDATE jobs SET settings=json_remove(settings,'$.hybrid_version','$.hybrid_prompt','$.owner_viewing')
                          WHERE id=?""",(self.job,))
        for target,name,options in (
            (intake,"RECIPE",dict(new=recipe)),
            (transfer,"PART",dict(new=50)),
            (transfer,"completed_youtube",dict(return_value=self.fixture)),
            (transfer,"acquire",dict(side_effect=self.local_fixture)),
            (streamer_asr,"transcribe",dict(side_effect=self.words)),
            (streamer_judge,"judge",dict(side_effect=self.judge)),
            (streamer_judge,"request_runner",dict(return_value=None)),
            (streamer_event_review,"review",dict(side_effect=self.visual)),
            (streamer_layout,"evidence",dict(return_value=([.04,.12,.2,.28],"Fixture camera"))),
        ):
            self.enterContext(patch.object(target,name,**options))
        class Connection:
            def key(self,*_,**__):
                return "fixture", "fixture"
        return jobs.Worker(self.root,Connection())

    def test_retry_refused_range_finishes_two_stories_without_operator_action(self):
        self.winners,self.fail_once = True,True
        worker = self.rig()
        worker.process_run(self.row())
        self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        self.assertEqual(self.moves,[("audio",0,50),("audio",50,100),("audio",100,101),("video",5,14),
                                    ("video",43,61),("video",43,61),("video",78,91)])
        results = streamer_asr.read(self.folder/"production.json")
        self.assertEqual(results["qualified"],2,results)
        with store.connect(self.root) as db:
            rows = db.execute("SELECT e.* FROM streamer_editions e JOIN done_editions d ON d.edition=e.id").fetchall()
            self.assertEqual(len(rows),2)
            for row in rows:
                plan = json.loads(row["plan"])
                self.assertIn(plan["duration"],(18,22))
                self.assertTrue(plan["post"].startswith(plan["hook"]))
                self.assertIn("Automatic final QA",row["qa"])
                cues = streamer_asr.read(intake.streamer_edits.directory(self.root,self.job,row["candidate"],row["id"])/"manifest.json")["caption_track"]
                self.assertTrue(any(c["start"]>5 for c in cues),cues)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM portal_admissions WHERE source=?",(self.source,)).fetchone()[0],1)

    def test_unrecoverable_picture_window_does_not_stop_other_stories(self):
        self.winners,self.fail_once = True,"always"
        worker = self.rig()
        worker.process_run(self.row())
        self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        result = streamer_asr.read(self.folder/"production.json")
        self.assertEqual(result["qualified"],1,result)
        self.assertEqual(result["items"][0]["state"],"held")
        self.assertEqual(self.moves.count(("video",43,61)),3)

    def test_zero_winners_never_call_video(self):
        self.winners,self.fail_once = False,False
        worker = self.rig()
        worker.process_run(self.row())
        self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        self.assertTrue(all(kind=="audio" for kind,_,_ in self.moves))
        self.assertEqual(streamer_asr.read(self.folder/"audio-selection.json")["edits"],[])

    def test_saved_extension_resumes_after_decode_and_reaches_done(self):
        self.winners,self.fail_once = True,False
        with store.connect(self.root,True) as db:
            config = json.loads(self.row()["settings"])
            config["hourly_target"] = 1
            db.execute("UPDATE jobs SET settings=? WHERE id=?",(json.dumps(config),self.job))
        worker = self.rig(recipe=5)
        attempts = []

        def proof(candidate,words,sail,read,**options):
            self.assertEqual(candidate["id"],"c1-1")
            attempts.append(options["dense"])
            if not options["dense"]:
                ids = [word[0] for word in words if 65<=word[1]<66]
                return {"verdict":"hold","reason":"The end is missing.",
                        "repair":{"direction":"later","words":[ids[0],ids[-1]]}},{}
            saved = streamer_asr.read(self.folder/"clip-repair.json")[candidate["id"]]["repair"]
            self.assertEqual(saved["justification"]["quote"],"Then it locks.")
            self.assertGreater(saved["shots"][0][-1],66)
            self.assertTrue(read(65.9))
            if attempts.count(True)==1:
                raise ProviderPending("Fixture wait after retaining the revised range.")
            return {"verdict":"accept","reason":"The lock is visible.","basis":"visible","anchors":[48,65.9],
                    "payoff_center":[.73,.61],"witness":"The object locks."},{"event":"Fixture lock","complete_event":True}

        def opening(result,detail,stream,sail,fit,**options):
            hook = "Can this lock work?"
            self.assertTrue(fit(hook,65.9))
            return result,dict(detail,hook=hook,post="It locks.",verified_hook={"choice":"question"})

        def visible(sail,source,portrait,witness,hook):
            self.assertTrue(source.startswith(b"\xff\xd8"))
            self.assertTrue(portrait.startswith(b"\xff\xd8"))
            self.assertNotEqual(source,portrait)
            self.assertEqual((witness,hook),("The object locks.","Can this lock work?"))
            return "Fixture detail remains visible."

        with (patch.object(streamer_event_review,"inspect_event",side_effect=proof),
              patch.object(streamer_event_review,"write_opening",side_effect=opening),
              patch.object(streamer_event_review,"portrait_proof",side_effect=visible)):
            worker.process_run(self.row())
            self.assertEqual(self.row()["state"],"queued",self.row()["error"])
            prior = list(self.moves)
            self.assertEqual(prior[3:][0],("video",40,64))
            self.assertEqual(prior[4][:2],("video",64))
            worker.process_run(self.row())
            self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        self.assertEqual(attempts,[False,True,True])
        self.assertEqual(self.moves,prior)
        results = streamer_asr.read(self.folder/"production.json")
        self.assertEqual(results["qualified"],1,results)
        self.assertIn("portrait_proof",results["items"][0])
        self.assertEqual(results["items"][1]["state"],"skipped")
        with store.connect(self.root) as db:
            saved = db.execute("SELECT plan FROM streamer_editions WHERE id=?",(results["items"][0]["edition"],)).fetchone()
        self.assertEqual(json.loads(saved["plan"])["cuts"][0]["keys"],[[65.9,.73,.61]])

    def test_contradicted_event_holds_and_distinct_reserve_is_checked(self):
        self.winners,self.fail_once = True,False
        worker = self.rig(recipe=5)
        with store.connect(self.root,True) as db:
            config = json.loads(self.row()["settings"])
            config["hourly_target"] = 1
            db.execute("UPDATE jobs SET settings=? WHERE id=?",(json.dumps(config),self.job))
        no = {"verdict":"reject","reason":"A spoken claim contradicts the actual picture.",
              "repair":{"direction":"dense"}}
        with patch.object(streamer_event_review,"inspect_event",return_value=(no,{})) as judge:
            worker.process_run(self.row())
        self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        self.assertEqual(judge.call_count,2)
        self.assertTrue(all(not call.kwargs["dense"] for call in judge.call_args_list))
        data = streamer_asr.read(self.folder/"clip-repair.json")
        self.assertTrue(data["c1-2"]["reserve"])
        self.assertTrue(all(item["outcome"]["state"]=="held" and "repair" not in item for item in data.values()))

    def test_verified_local_master_never_resolves_or_downloads(self):
        asset = dict(media.probe(self.raw),path=self.raw.name,hash=media.checksum(self.raw))
        with store.connect(self.root,True) as db:
            db.execute("INSERT INTO assets VALUES(:hash,:path,:duration,:audio,:bytes)",asset)
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)",(self.source,self.row()["revision"],asset["hash"],"remote","youtube"))
        self.folder.mkdir(parents=True)
        streamer_asr.write(self.folder/"selection.json",{"asset":asset})
        worker = jobs.Worker(self.root)
        with (patch.object(transfer,"completed_youtube",side_effect=AssertionError("Remote resolve")),
              patch.object(media,"acquire",side_effect=AssertionError("Remote acquire")),
              patch.object(streamer_asr,"transcribe",return_value=(None,{"duration":101})),
              patch.object(streamer_run.streamer_auto,"process",return_value={"hours":[],"qualified":0})):
            worker.process_run(self.row())
        self.assertEqual(self.row()["state"],"complete",self.row()["error"])
        self.assertEqual(self.row()["asset"],asset["hash"])
