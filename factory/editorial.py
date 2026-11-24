"""Turn evidence windows into reviewable, deliberately provisional plans."""

import hashlib
import json

RENDER_VERSION = "vertical-inset-v1"


def plan(candidate, audio):
    start, peak, end = candidate["start"], candidate["time"], candidate["end"]
    if candidate.get("context"):
        return context_plan(candidate, audio)
    return {
        "start": start, "end": end, "peak": peak, "score": candidate["score"],
        "hook": "What changes here?",
        "setup": f"Review the preceding action from {start:g}s.",
        "payoff": f"Check the visual/audio change near {peak:g}s and its immediate aftermath.",
        "selection": f"Whole-frame change {candidate['change']:.3f}, audio RMS {candidate['audio']:.3f}.",
        "labels": candidate["labels"],
        "evidence": [start, max(start, peak-3), peak, min(end-.25, peak+3), end-.25],
        "framing": "Wide inset on 9:16 to retain the subject, HUD and action at the frame edges.",
        "audio": "Retain source sound. Review dialogue for caption needs." if audio else "Silent source.",
        "narration": "None proposed. Verify setup and payoff before adding context.",
        "confidence": "Unverified candidate",
    }


def plan_identity(plan):
    content = [RENDER_VERSION, plan["start"], plan["end"], plan["framing"], plan["audio"]]
    if "judgment_version" in plan:
        content = ["phase2-edit-v1", plan]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def context_plan(candidate, audio):
    start, peak, end = candidate["start"], candidate["time"], candidate["end"]
    model = "standout" in candidate
    evidence = candidate.get("evidence_times") or [start, start+(peak-start)/2, peak, peak+(end-peak)/2, end-.4]
    evidence = sorted(set(min(end-.15, max(start, stamp)) for stamp in evidence))[:5]
    if len(evidence) < 5:
        evidence = [round(start + (end-start-.4)*i/4, 3) for i in range(5)]
    signal = candidate.get("signal", {})
    choice = (candidate["standout"] + " · Retrieval: " + candidate["route"] if model
              else f"Temporal signal {signal.get('before', 0):.3f} before, {signal.get('after', 0):.3f} after. "
                   "The cut spans the surrounding action for review.")
    return dict(start=start, end=end, peak=peak, score=candidate["score"], evidence=evidence, labels=[],
                hook=candidate.get("hook", "Identify the question in the opening action."),
                setup=candidate.get("setup", f"Continuous context before {peak:g}s."),
                payoff=candidate.get("payoff", "Outcome requires editorial verification."),
                change=candidate.get("change") if model else None, selection=choice,
                confidence=(("Image-supported proposal" if candidate.get("visual") else "Transcript-only proposal")
                            if model else "Local context signal · meaning unverified"),
                framing="Wide inset with an optional 4:3 center crop for comparison.",
                audio="Original audio" if audio else "Silent source",
                narration=candidate.get("audio_need") or "None proposed. Review original audio and captions.",
                speech_status=candidate.get("speech_status", ""), speech=candidate.get("speech", []),
                evidence_ids=candidate.get("evidence_ids", []), evidence_frames=candidate.get("frames", []))
