import unittest

from factory import provider_api, streamer_event_review as event


class EventReviewTests(unittest.TestCase):
    def setUp(self):
        self.words = [[i+1,t,t+.2,word,.95] for i,(t,word) in enumerate(zip(
            [103,104,105,121,122,123],["Is","that","free?","Only","the","box."]))]
        self.candidate = {"start":101,"end":128,"shots":[[101,128]],"reason":"Fixture exchange",
                          "evidence":[{"time":[103,105.2]},{"time":[121,123.2]}]}
        self.reply = {"verdict":"accept","complete":True,"event":"Unexpected empty-box offer",
                      "reason":"The answer reverses the apparent offer and resolves the exchange.",
                      "basis":"dialogue","witness":"The answer says only the box is free.",
                      "setup_words":[1,3],"payoff_words":[4,6],"depends_on_unseen_action":False}

    def test_does_not_confuse_offscreen_action_or_out_of_cut_quote_with_dialogue(self):
        with self.assertRaisesRegex(ValueError,"chronological picture"):
            event.verify(dict(self.reply,depends_on_unseen_action=True),self.words,[[101,128]],[104,123])
        with self.assertRaisesRegex(ValueError,"outside the retained"):
            event.verify(self.reply,self.words,[[101,122]],[104,121])
        decision,_ = event.verify(self.reply,self.words,[[101,128]],[104,123])
        self.assertEqual(decision["anchors"],[103,121])
        self.assertEqual(decision["speech"][1]["quote"],"Only the box.")

    def test_invalid_proof_and_oversized_hook_retry_independently(self):
        calls = []
        def sail(operation,content,limit,**_):
            calls.append((operation,content,limit))
            if operation=="event-proof":
                raise provider_api.OutputLimit()
            if operation=="event-proof-short":
                return self.reply
            if operation=="event-opening":
                return {"question":{"text":"Could anything about the entire offer really be free?","score":9,"supported":True}}
            return {"question":{"text":"Is the offer free?","score":9,"supported":True}}
        decision,copy = event.review(self.candidate,self.words,{"credit":"Fixture"},sail,
                                     lambda _:b"fixture-jpeg",lambda text,time:time==121 and len(text)<24)
        self.assertEqual(decision["verdict"],"accept")
        self.assertEqual(copy["hook"],"Is the offer free?")
        self.assertEqual([call[0] for call in calls],
                         ["event-proof","event-proof-short","event-opening","event-opening-short"])
        self.assertLess(calls[3][2],calls[2][2])
        self.assertNotEqual(calls[3][1],calls[2][1])

    def test_genuine_hold_skips_copy(self):
        calls = []
        def sail(operation,*_,**__):
            calls.append(operation)
            return {"verdict":"hold","reason":"The action is outside the retained cut."}
        result,_ = event.review(self.candidate,self.words,{"credit":"Fixture"},sail,lambda _:b"fixture-jpeg",self.fail)
        self.assertEqual(result["verdict"],"hold")
        self.assertEqual(calls,["event-proof"])

    def test_visible_claim_cannot_be_approved_as_dialogue(self):
        candidate = dict(self.candidate,basis="visible",reason="The player actually arrives.")
        result,_ = event.inspect_event(candidate,self.words,lambda *a,**k:self.reply,
                                      lambda _:b"fixture-jpeg",dense=True)
        self.assertEqual(result["verdict"],"hold")
        self.assertIn("visible proof",result["reason"])
        self.assertLessEqual(len(event.samples(candidate["shots"],candidate["evidence"],dense=True)),6)
