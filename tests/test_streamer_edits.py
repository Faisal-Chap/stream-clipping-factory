import json
import tempfile
import unittest
from pathlib import Path

from factory import media, store, streamer_asr, streamer_edits, streamer_edit_render, streamer_layout

TABLE = Path("tests/fixtures/VIDEO-TABLE.md").resolve()


def fields(**changes):
    return dict({"title": "Snowy recovery", "hook": "Can it keep going?", "post": "A recovery in GTA.",
                 "context": "GTA V", "rationale": "The wheel leaves the truck and it continues.",
                 "cuts": "2 | 5 | focus | 2,.5,.5; 4,.55,.5\n8 | 22 | reaction |",
                 "decision": "qualify", "beats": "2.5, 4, 9",
                 "captions": "2.5 | 3.1 | Storm. | Storm\n8.2 | 9 | It survived. | survived"}, **changes)


class EditionPlanTests(unittest.TestCase):
    def test_auto_camera_geometry_detects_moving_overlays_on_either_side(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("Local OpenCV is not installed")
        for left in (0,504):
            frames = []
            for index in range(12):
                frame = np.full((360,640),30+index,dtype=np.uint8)
                frame[145:230,left:left+136] = 150
                frame[180:204,left+18+index:left+50+index] = 210
                frames.append(frame)
            camera = streamer_layout.overlay_boxes(frames,cv2,np)[0]
            for actual,expected in zip(camera,(left/640,145/360,136/640,85/360)):
                self.assertAlmostEqual(actual,expected,delta=.01)

    def test_nonmonotonic_shots_and_subtitle_intersections_fail(self):
        invalid = [
            fields(cuts="8 | 10 | wide |\n2 | 5 | wide |"),
            fields(cuts="2 | 5 | wide |\n4.5 | 9 | focus |"),
            fields(cuts="2 | 5 | focus | 3,1.1,.5\n8 | 10 | wide |"),
            fields(captions="2.5 | 4 | Storm. |\n3 | 4.7 | Next. |"),
            fields(captions="4.9 | 8.5 | Crossed gap. |"),
            fields(beats="2.5, 6, 9"),
        ]
        for submitted in invalid:
            with self.subTest(submitted=submitted), self.assertRaises(ValueError):
                streamer_edits.normalize(submitted, 30)

    def test_caption_remap_and_audio_handle_across_a_splice(self):
        plan = streamer_edits.normalize(fields(), 30)
        self.assertEqual(plan["duration"], 17)
        self.assertEqual(plan["cuts"][1]["audio_handle"], .02)
        self.assertEqual(plan["beats"]["payoff"], {"source": 9.0, "edit": 4.0})
        words = [[1, 2.6, 2.8, "Replace.", .9], [2, 6, 6.2, "Omitted.", .9],
                 [3, 7.9, 8.15, "Partial.", .9], [4, 9.2, 9.45, "Complete.", .9]]
        self.assertEqual([(c["start"], c["end"], c["text"])
                          for c in streamer_edit_render.captions({"words": words}, plan)],
                         [(2.5-2, 1.1, "Storm."), (3.2, 4, "It survived."), (4.2, 4.45, "Complete.")])

    def test_historical_recipe_retains_its_original_fallback(self):
        plan = streamer_edits.normalize(fields(), 30)
        graph = streamer_edit_render.game_filter(plan["cuts"][1], None, (640, 360), 1)
        self.assertIn("pad=1080:1920", graph)
        self.assertNotIn("crop", graph)
        focus = streamer_edit_render.game_filter(plan["cuts"][0], None, (640, 360), 0)
        self.assertIn("if(lt(t,", focus)

    def test_labeled_rewind_remaps_voice_and_keeps_source_proof(self):
        plan = streamer_edits.normalize(fields(
            cuts="8 | 10 | focus | | Preview\n2 | 5 | wide | | Earlier\n8 | 22 | focus |",
            captions="2.5 | 3.1 | Setup |\n8.2 | 9 | Payoff |"),30)
        self.assertEqual([c.get("time_cue") for c in plan["cuts"]],["Preview","Earlier",None])
        cues = streamer_edit_render.captions({"words":[]},plan)
        self.assertEqual([(c["text"],c["start"],c["source"]) for c in cues],
                         [("Payoff",.2,[8.2,9]),("Setup",2.5,[2.5,3.1]),("Payoff",5.2,[8.2,9])])
        self.assertEqual(plan["beats"]["payoff"]["source"],9.0)


class EditionStoreTests(unittest.TestCase):
    def test_standard_master_is_held_when_the_recording_has_no_camera(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-missing-camera-test-") as folder:
            root = Path(folder)
            store.initialize(root,TABLE)
            archive = root/"masters"/"gameplay-only-fixture.mp4"
            archive.parent.mkdir(exist_ok=True)
            transcript = root/"streamer-transcripts"/"gameplay-only-fixture.json"
            transcript.parent.mkdir(exist_ok=True)
            ffmpeg = media.require("ffmpeg")
            result = media.run([ffmpeg,"-v","error","-f","lavfi","-i","testsrc2=size=640x360:rate=30",
                                "-f","lavfi","-i","sine=frequency=320","-t","20","-c:v","libx264",
                                "-threads","2","-c:a","aac",str(archive)],25)
            self.assertEqual(result.returncode,0)
            streamer_asr.write(transcript,{"words":[]})
            selection = {"asset":{"hash":media.checksum(archive),"path":archive.relative_to(root).as_posix(),"duration":20},
                         "transcript":transcript.relative_to(root).as_posix()}
            with store.connect(root,True) as db:
                job = store.enqueue(db,1)
                plan = streamer_edits.pin(root,job,"c1-1",
                    fields(cuts="1 | 18 | wide |",captions="",decision="candidate",beats="",camera_crop=".03,.16,.16,.19"),
                    selection,1,{"camera":None,"credit":"Fixture"})
                self.assertEqual(plan["recipe"],streamer_layout.RECIPE)
                identity = streamer_edits.register(db,plan)
                row = streamer_edits.next_edit(db)
            streamer_edits.process(root,row)
            with store.connect(root) as db:
                saved = db.execute("SELECT render_state,error FROM streamer_editions WHERE id=?",(identity,)).fetchone()
            self.assertEqual(saved["render_state"],"failed")
            self.assertIn("Needs camera framing / source unsuitable",saved["error"])
            target = streamer_edits.directory(root,job,"c1-1",identity)
            self.assertFalse((target/"portrait.mp4").exists())
            with self.assertRaises(streamer_layout.FramingRequired):
                streamer_edit_render.render(root,target/"audition",plan,audition=True)
            self.assertFalse((target/"audition"/"portrait.mp4").exists())

    def test_immutable_versions_restart_and_rejected_evidence(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-editions-disposable-test-") as folder:
            root = Path(folder)
            store.initialize(root, TABLE)
            transcript = root/"streamer-transcripts"/"fixture-transcript.json"
            transcript.parent.mkdir()
            streamer_asr.write(transcript, {"words": []})
            with store.connect(root, True) as db:
                job = store.enqueue(db, 1)
                db.execute("INSERT INTO assets VALUES('fixture-asset','masters/fixture-recording',30,1,100)")
                db.execute("UPDATE jobs SET state='complete',asset='fixture-asset',settings=? WHERE id=?",
                           (json.dumps({"stream": {"credit": "Fixture", "camera": None}}), job))
            folder = root/"streamer-runs"/str(job)
            original = folder/"c1-1"
            original.mkdir(parents=True)
            (original/"portrait.mp4").write_bytes(b"original portrait")
            (original/"review.json").write_bytes(b"original review")
            selection = dict(asset=dict(hash="fixture-asset", path="masters/fixture-recording", duration=30),
                             transcript=transcript.relative_to(root).as_posix(),
                             proposals=[dict(id="c1-1", evidence=[{"time": [2.5,3]}])])
            streamer_asr.write(folder/"selection.json", selection)
            with store.connect(root, True) as db:
                identity = streamer_edits.enqueue(root, db, job, "c1-1", fields())
                self.assertEqual(identity, streamer_edits.enqueue(root, db, job, "c1-1", fields()))
                altered = streamer_edits.enqueue(root, db, job, "c1-1", fields(hook="Still going?"))
                held = streamer_edits.enqueue(root, db, job, "c1-1", fields(decision="reject"))
                self.assertNotEqual(identity, altered)
                self.assertEqual(streamer_edits.next_edit(db)["id"], identity)
            store.initialize(root, TABLE)
            with store.connect(root, True) as db:
                states = {row["id"]: row["render_state"] for row in db.execute("SELECT id,render_state FROM streamer_editions")}
                self.assertEqual(states, {identity:"queued", altered:"queued", held:"held"})
                with self.assertRaisesRegex(ValueError, "qualified export"):
                    streamer_edits.review(db, root, identity, "Early QA.")
                db.execute("UPDATE streamer_editions SET render_state='exported' WHERE id=?", (identity,))
                with self.assertRaisesRegex(media.MediaError, "Could not verify"):
                    streamer_edits.review(db, root, identity, "Missing portrait.")
                ballot = "b" * 32
                observations = dict(verdict="both_weak", mechanic="Storm", picture="Road", reason="Both cuts held.")
                streamer_edits.save_ballot(db, identity, ballot, observations)
                streamer_edits.save_ballot(db, identity, ballot, observations)
                vote = db.execute("SELECT version_slot,verdict FROM streamer_edition_ballots").fetchall()
                self.assertEqual([tuple(row) for row in vote], [("b","both_weak")])
                streamer_asr.write(transcript, {"words": [[1, 0, 1, "Changed."]]})
                new_transcript = streamer_edits.enqueue(root, db, job, "c1-1", fields())
                self.assertNotEqual(identity, new_transcript)
            self.assertEqual((original/"portrait.mp4").read_bytes(), b"original portrait")
            self.assertEqual((original/"review.json").read_bytes(), b"original review")
