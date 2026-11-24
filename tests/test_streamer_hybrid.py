import tempfile
import unittest
from pathlib import Path

from factory import media, streamer_hybrid as hybrid


class HybridTests(unittest.TestCase):
    def test_continuous_envelope_covers_old_gap_and_part_edge(self):
        self.assertEqual(hybrid.envelope([[14375,14383],[14398,14420]],14370,14435),[[14370,14435]])

    def test_lock_checks_ids_order_length_and_visual_witness(self):
        frames = {"F01":18560,"F02":18567,"F03":18577,"F04":18596,"F05":18605}
        reply = dict(decision="accept",start="F01",payoff="F02",end="F03",basis="visible",
                     visible=["F01","F02"],result="A wheel comes loose.",focus=[.3,.6],
                     hook="What comes loose?")
        self.assertEqual(hybrid.decide(reply,frames,[])["interval"],[18560,18577])
        self.assertEqual(hybrid.decide(dict(reply,end="F05"),frames,[])["interval"],[18560,18605])
        for change in (dict(end="F02"),dict(start="F02"),dict(payoff="F99"),dict(visible=[]),
                       dict(start="F05"),dict(basis="dialogue")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                hybrid.decide(dict(reply,**change),frames,[])

    def test_decoded_sheets_cover_range_in_order_and_bound_attachments(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-hybrid-sheet-test-") as folder:
            root = Path(folder)
            asset = root/"fixture.mp4"
            result = media.run([media.require("ffmpeg"),"-v","error","-f","lavfi","-i",
                "testsrc2=s=192x108:r=30:d=51","-c:v","libx264","-threads","2",str(asset)],30)
            self.assertEqual(result.returncode,0,result.stderr)
            window = {"path":asset.name,"start":14370}
            overview = hybrid.sheets(root,root,window,14370,14420,"overview")
            stamps = list(overview["frames"].values())
            self.assertEqual(stamps,sorted(set(stamps)))
            self.assertEqual(stamps[0],14370)
            self.assertGreater(stamps[-1],14419)
            self.assertEqual(len(overview["sheets"]),5)
            near = hybrid.sheets(root,root,window,14399,14401.25,"closer",closer=True)
            calls = []
            def sail(operation,payload,output,**kwargs):
                parts = payload[0]["content"]
                calls.append((operation,output))
                self.assertEqual(sum(p["type"]=="input_image" for p in parts),6)
                self.assertIn("C01",parts[0]["text"])
                if len(calls)==1:
                    return {"decision":"accept","start":"F01","payoff":"F02","end":"F03"}
                return {"decision":"reject","reason":"No event."}
            self.assertEqual(hybrid.review(root,overview,near,"Fixture lead",[],sail)[0]["decision"],"reject")
            self.assertEqual(calls,[(hybrid.PROMPT+"-closer",950),(hybrid.PROMPT+"-closer-correct",720)])
            self.assertEqual(hybrid.sheets(root,root,window,14370,14420,"overview"),overview)
