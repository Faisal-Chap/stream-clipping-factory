import tempfile
import unittest
from pathlib import Path

from factory import media, shorts_release, streamer_asr, streamer_edit_render, streamer_edits


def make_portrait(path, video_frames=510, audio_samples=816000):
    command = [media.require("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
               "color=size=32x64:rate=30", "-f", "lavfi", "-i", "sine=sample_rate=48000",
               "-vf", f"trim=end_frame={video_frames}", "-af", f"atrim=end_sample={audio_samples}",
               "-c:v", "libx264", "-threads", "2", "-c:a", "aac", "-movie_timescale", "48000", str(path)]
    result = media.run(command, 25)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return path


class ReleaseLengthTests(unittest.TestCase):
    def test_story_v4_export_over_45_seconds_keeps_legacy_release_limit(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-story-v4-duration-") as folder:
            path = make_portrait(Path(folder)/"story-v4.mp4",4500,7200000)
            self.assertTrue(shorts_release.inspect(path,bounds=shorts_release.STORY_V4_BOUNDS)["passed"])
            self.assertFalse(shorts_release.inspect(path)["passed"])
            longer = make_portrait(Path(folder)/"story-v4-over-limit.mp4",4501,7201600)
            self.assertFalse(shorts_release.inspect(longer,bounds=shorts_release.STORY_V4_BOUNDS)["passed"])

    def test_decoded_boundaries_and_independent_tails(self):
        cases = (
            ("minimum", 510, 816000, True, "17.000", "17.000"),
            ("maximum", 1350, 2160000, True, "45.000", "45.000"),
            ("short_picture", 509, 816000, False, "16.966667", "17.000"),
            ("short_audio", 510, 815999, False, "17.000", "16.999979"),
            ("long_picture", 1351, 2160000, False, "45.033333", "45.000"),
            ("long_audio", 1350, 2160001, False, "45.000", "45.000021"),
        )
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-release-length-test-") as folder:
            for name, frames, samples, passed, video, audio in cases:
                with self.subTest(name=name):
                    path = make_portrait(Path(folder)/f"gta-streamer-disposable-{name}.mp4", frames, samples)
                    result = shorts_release.inspect(path)
                    self.assertEqual(result.get("video"), video, result)
                    self.assertEqual(result.get("audio"), audio, result)
                    self.assertIs(result["passed"], passed, result)

    def test_plan_uses_combined_picture_frames(self):
        values = dict(title="Recovery", rationale="The truck keeps going.", hook="Three wheels left",
                      context="GTA V", post="A truck recovery.", decision="candidate")
        for end, passed in ((26.966, False), (27, True), (55, True), (55.034, False)):
            with self.subTest(end=end):
                fields = dict(values, cuts=f"10 | {end} | wide |")
                if passed:
                    self.assertEqual(streamer_edits.normalize(fields, 100)["duration"], end-10)
                else:
                    with self.assertRaisesRegex(ValueError, "17.000"):
                        streamer_edits.normalize(fields, 100)
        split = dict(values, cuts="1000.1 | 1008.6 | wide |\n4000.2 | 4008.7 | wide | | Later")
        self.assertEqual(streamer_edits.normalize(split, 5000)["duration"], 17)

    def test_audition_and_master_renders_at_the_inclusive_limits(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-release-render-test-") as folder:
            root = Path(folder)
            (root/"masters").mkdir()
            (root/"streamer-transcripts").mkdir()
            archive = make_portrait(root/"masters"/"gta-streamer-disposable-render-source.mp4", 1560, 2496000)
            transcript = root/"streamer-transcripts"/"gta-streamer-disposable-render-words.json"
            streamer_asr.write(transcript, {"words":[]})
            base = dict(title="Proof", rationale="The same event continues.", hook="Follow the recovery",
                        context="GTA V", post="Source recovery.", decision="candidate")
            for name, shots, expected, audition in (
                ("audition", "1 | 9.5 | wide |\n11.2 | 19.7 | wide | | Later", "17.000", True),
                ("master", "1 | 46 | wide |", "45.000", False),
            ):
                with self.subTest(profile=name):
                    edit = streamer_edits.normalize(dict(base,cuts=shots), 52)
                    plan = dict(edit, recipe=streamer_edits.RECIPE, fps=30, camera=None, credit="Fixture",
                                archive=archive.relative_to(root).as_posix(), asset=media.checksum(archive),
                                transcript=transcript.relative_to(root).as_posix(), transcript_hash=media.checksum(transcript))
                    target = root/f"gta-streamer-disposable-release-{name}"
                    streamer_edit_render.render(root,target,plan,audition=audition)
                    length = shorts_release.inspect(target/"portrait.mp4")
                    self.assertTrue(length["passed"],length)
                    self.assertEqual({length[k] for k in ("video","audio","container")},{expected})
