"""S1 queue stages and review artifacts."""

import json
import shutil

from . import media, shorts_release, streamer_asr, streamer_auto, streamer_judge, streamer_render, streamer_transfer
from .factory_metrics import hour_index, payoff
from .provider_meter import DISCOVERY, DOWNSTREAM, Meter, ProviderExhausted
from .store import connect


def directory(root, job):
    return root / "streamer-runs" / str(job)


def latest_production(folder):
    continued = sorted((path for path in folder.glob("production-continued-*.json")
                        if path.stem.rsplit("-",1)[1].isdigit()),
                       key=lambda path:int(path.stem.rsplit("-",1)[1]),reverse=True)
    for path in [*continued,folder/"production-revisit.json",folder/"production.json"]:
        if path.is_file():
            return streamer_asr.read(path)
    return {}


def queue_analysis_resume(root, db, job):
    from . import factory_events

    row = db.execute("""SELECT j.*,s.revision AS current_revision,s.state AS source_state
                        FROM jobs j JOIN sources s ON s.id=j.source WHERE j.id=?""",(job,)).fetchone()
    folder = directory(root,job)
    config = json.loads(row["settings"]) if row else {}
    if not row or row["state"]!="complete" or "hourly_target" not in config or not (folder/"selection.json").is_file():
        raise ValueError("Only a saved partial streamer attempt can resume analysis here.")
    if row["revision"]!=row["current_revision"] or row["source_state"]!="queued":
        raise ValueError("Resume the source and select the current recording revision.")
    selection = streamer_asr.read(folder/"selection.json")
    blocked = any(c.get("judgment")=="blocked" for c in selection["chapters"])
    pending = any(c.get("visual_check",{}).get("verdict")=="unreviewed" or
                  "Global ranking pending" in c.get("rejection","") for c in selection["proposals"])
    if not blocked and not pending:
        raise ValueError("The saved attempt has no incomplete chapters, ranking or frame checks.")
    needed = DOWNSTREAM+(DISCOVERY if blocked else 0)
    remaining = Meter(root,job,config["provider_cap_cents"]).available(db)
    if remaining < needed:
        raise ValueError(f"Resume held. USD {remaining/1_000_000:.4f} remains under the configured cap. "
                         f"This step requires USD {needed/1_000_000:.4f}. Saved evidence is available.")
    sequence = row["attempt"]+1
    streamer_asr.write(folder/f"selection-before-resume-{sequence}.json",selection)
    if prior := latest_production(folder):
        streamer_asr.write(folder/f"production-before-resume-{sequence}.json",prior)
    config["continue_analysis"] = sequence
    db.execute("""UPDATE jobs SET state='queued',settings=?,stop=0,error='',progress=? WHERE id=?""",
               (json.dumps(config),"Waiting to continue saved analysis within the configured cap",job))
    factory_events.record(db,job,"queued",item="Requested bounded analysis continuation")


def process(worker, row, snapshot, config, checkpoint, update):
    from .library import identify

    root, job = worker.root, row["id"]
    if config.get("full_replay"):
        from . import streamer_intake
        return streamer_intake.process(worker,row,snapshot,config,checkpoint,update)
    retained = None
    canonical, _, route = identify(snapshot.get("location") or snapshot["link"])

    def check_space():
        if shutil.disk_usage(root).free < config.get("reserve_mib", 2048) * media.CHUNK:
            raise media.MediaError("The workspace is below the configured free-space reserve.")

    if route!="file":
        if route=="youtube" and config.get("coverage_start",0)>=streamer_transfer.REPLAY_LIMIT:
            raise streamer_transfer.TransferHold(streamer_transfer.REPLAY_LIMIT_MESSAGE)
        if not (directory(root,job)/"audio-first.json").is_file():
            retained = worker.retained_asset(row,"remote")
            if route=="youtube" and retained and retained["duration"]>streamer_transfer.REPLAY_LIMIT:
                retained = None
        if retained is None:
            if route!="youtube":
                raise media.MediaError("This platform has no verified audio-only and bounded-video adapter. Attach a local recording.")
            from . import streamer_intake
            checkpoint("Archive", check_space)
            config.setdefault("hourly_target",7)
            stream = config.setdefault("stream", {})
            if not stream:
                with connect(root) as db:
                    owner = db.execute("""SELECT st.*,a.creator,c.credit,c.camera FROM streamer_streams st
                        JOIN streamer_accounts a ON a.id=st.account JOIN streamer_creators c ON c.id=a.creator
                        WHERE st.source=?""",(row["source"],)).fetchone()
                if not owner:
                    raise media.MediaError("Assign this legacy source to a creator in Sources before resuming.")
                stream.update(dict(owner))
            stream.setdefault("platform_id",canonical.split(":",1)[1])
            return streamer_intake.process(worker,row,snapshot,config,checkpoint,update)

    def acquire():
        check_space()
        if retained:
            with connect(root,True) as db:
                db.execute("UPDATE jobs SET asset=? WHERE id=?",(retained["hash"],job))
            update("Reused the verified local archive. Zero acquisition traffic.")
            return retained
        asset = worker.asset(row, snapshot, config, update)
        if not asset["audio"]:
            raise media.MediaError("The archive has no original audio to transcribe.")
        if asset["duration"] > config["remote_minutes"]*60 or asset["bytes"] > config["remote_mib"]*media.CHUNK:
            raise media.MediaError("Archived recording exceeds this job's duration or size limit.")
        return asset

    asset = checkpoint("Archive", acquire)
    folder = directory(root, job)
    folder.mkdir(parents=True, exist_ok=True)
    transcript_path, transcript = checkpoint("Transcribe", lambda: streamer_asr.transcribe(
        root, asset, config, update, coverage=lambda done, total: update(
            "Local transcript coverage updated", covered=done, total=total)))
    report = folder / "selection.json"
    continuation = config.get("continue_analysis")
    if report.is_file() and not continuation:
        selection = streamer_asr.read(report)
        update("Reused the completed Sail chapter and global judgments")
    else:
        chat = streamer_judge.chat_activity(config["stream"]["chat_path"])
        chapters, candidates, ranked = checkpoint("Judge", lambda: streamer_judge.judge(
            root, asset, transcript, config, job, worker.connections, chat, update,
            coverage=lambda done,total: update("Chapter judgments saved", covered=done, total=total)))
        short = ranked if "hourly_target" in config else ranked[:config["target"]]
        if "hourly_target" not in config:
            for candidate in ranked[config["target"]:]:
                candidate["rejection"] = "Qualified, below the configured target cutoff."
        rejected = [c for c in candidates if c["rejection"] and c.get("evidence") and not c.get("merged_into")]
        comparison = max(rejected, key=lambda c: c["score"], default=None)
        selection = {"asset": dict(asset), "transcript": transcript_path.relative_to(root).as_posix(),
                     "chapters": [{key: c[key] for key in ("start", "end", "abstention", "judgment","followup") if key in c}
                                  for c in chapters],
                     "chat": chat, "proposals": candidates, "shortlist": [c["id"] for c in short],
                     "comparison": comparison["id"] if comparison else None, "target": config["target"],
                     "timebases": streamer_render.inspect_media(root / asset["path"])[2],
                     "transcript_words": len(transcript["words"]), "transcript_seconds": transcript["duration"],
                     "asr_seconds": transcript["elapsed_seconds"], "asr_model_revision": transcript["model_revision"]}
        if continuation:
            prior = streamer_asr.read(folder/f"selection-before-resume-{continuation}.json")
            used = {item["id"]:item for item in selection["proposals"]}
            old = {item["id"]:item for item in prior["proposals"]}
            for cid in old.keys() & used.keys():
                if old[cid]["start"]!=used[cid]["start"] or old[cid]["end"]!=used[cid]["end"]:
                    raise media.MediaError("Continued analysis returned a conflicting saved moment identity.")
            selection["proposals"] = list((old | used).values())
            selection["shortlist"] = list(dict.fromkeys(prior["shortlist"]+selection["shortlist"]))
        streamer_asr.write(report, selection)
    if "hourly_target" in config:
        if continuation:
            before = folder/f"production-before-resume-{continuation}.json"
            first = streamer_asr.read(before) if before.is_file() else None
            if first:
                with connect(root) as db:
                    saved_ids = {r["candidate"] for r in db.execute("""SELECT candidate FROM streamer_editions
                        WHERE job=? AND render_state='exported' AND qualification='qualified'
                        AND json_extract(plan,'$.origin')='automatic'""",(job,))}
                selection = dict(selection,shortlist=[cid for cid in selection["shortlist"] if cid not in saved_ids])
            result = streamer_auto.process(root,row,selection,transcript,config,checkpoint,update,first_run=first,
                                           destination=folder/f"production-continued-{continuation}.json")
        else:
            result = streamer_auto.process(root,row,selection,transcript,config,checkpoint,update)
        under = [i for i,hour in enumerate(result["hours"]) if hour["complete"] and hour["gap"]]
        if under and not result.get("limit") and not continuation:
            result = revisit(worker,row,selection,transcript,config,result,under,checkpoint,update)
        full = [hour for hour in result["hours"] if hour["complete"]]
        short = sum(hour["gap"]>0 for hour in full)
        message = f"{result['qualified']} automated qualifications. {short}/{len(full)} full hours below target. Review the hourly funnel."
        with connect(root,True) as db:
            db.execute("""UPDATE jobs SET state='complete',stage='Review',progress=?,stop=0,
                          settings=json_remove(settings,'$.continue_analysis') WHERE id=?""",(message,job))
        return
    by_id = {entry["id"]: entry for entry in selection["proposals"]}
    from . import streamer_edit_views, streamer_edits

    total = 0
    for identity in selection["shortlist"]:
        update(f"Preparing portrait and source context for {identity}")
        candidate = by_id[identity]
        legacy = folder/identity/"portrait.mp4"
        if legacy.is_file():
            total += shorts_release.inspect(legacy)["passed"]
        else:
            version = None
            try:
                with connect(root,True) as db:
                    _,_,_,setting = streamer_edits.source(root,db,job,identity)
                    fields = streamer_edit_views.draft(candidate,asset["duration"],setting,snapshot["game"])
                    version = streamer_edits.enqueue(root,db,job,identity,fields)
                    edition = db.execute("SELECT * FROM streamer_editions WHERE id=?",(version,)).fetchone()
            except ValueError as error:
                update(f"{identity} held. {error}")
            if version:
                if edition["render_state"]!="exported":
                    checkpoint("Render",lambda: streamer_edits.process(root,edition))
                with connect(root) as db:
                    total += db.execute("SELECT render_state FROM streamer_editions WHERE id=?",(version,)).fetchone()[0]=="exported"
        checkpoint("Render", lambda: streamer_render.export(root, asset, candidate, transcript,
                                                            config, folder / identity, portrait=False))
    if identity := selection["comparison"]:
        checkpoint("Render", lambda: streamer_render.export(root, asset, by_id[identity], transcript,
                                                            config, folder / identity, portrait=False))
    with connect(root, True) as db:
        db.execute("UPDATE jobs SET state='complete',stage='Review',progress=?,stop=0 WHERE id=?",
                   (f"{total}/{config['target']} proposals rendered. Review footage and original audio.", job))


def revisit(worker,row,selection,transcript,config,first,under,checkpoint,update):
    root,job = worker.root,row["id"]
    folder = directory(root,job)
    saved = folder/"revisit-selection.json"
    production = folder/"production-revisit.json"
    if production.is_file():
        return streamer_asr.read(production)
    if not saved.is_file():
        asset = selection["asset"]
        with connect(root) as db:
            reserved = db.execute("""SELECT COALESCE(SUM(attempts*reservation_micros),0)
                                   FROM provider_calls WHERE job=?""",(job,)).fetchone()[0]
        if config["provider_cap_cents"]*10000-reserved<DISCOVERY+DOWNSTREAM:
            result = dict(first,items=[],limit="Provider budget headroom is below discovery, ranking and one frame check.")
            streamer_asr.write(production,result)
            return result
        try:
            worker.connections.key("sail",tier=config["sail_tier"],images=True)
        except ValueError:
            picture_leads = {}
        else:
            index = folder/"visual-leads.json"
            if not index.is_file():
                leads,count = checkpoint("Visual map",lambda:streamer_judge.visual_leads(
                    root,asset,update,transcript["rms_dbfs_by_second"]))
                streamer_asr.write(index,{"samples":count,"chapters":leads})
            picture_leads = {int(k):v for k,v in streamer_asr.read(index)["chapters"].items()}
        try:
            chapters,candidates,ranked = checkpoint("Revisit",lambda:streamer_judge.judge(
                root,asset,transcript,config,job,worker.connections,selection["chat"],update,
                revisit_hours=under,picture_leads=picture_leads))
        except (ProviderExhausted,streamer_judge.EditorialJSONError,streamer_judge.api.OutputLimit) as failure:
            result = dict(first,items=[],limit=str(failure))
            streamer_asr.write(folder/"production-revisit.json",result)
            return result
        prior = {c["id"]:c for c in selection["proposals"]}
        accepted = [prior[item["id"]] for item in first["items"] if item["state"]=="qualified"]
        additions = [c for c in candidates if c["id"] not in prior]
        for candidate in additions:
            if candidate.get("rejection"):
                continue
            overlap = next((other for other in accepted if
                            min(candidate["end"],other["end"])-max(candidate["start"],other["start"]) >=
                            .5*min(candidate["end"]-candidate["start"],other["end"]-other["start"])),None)
            if overlap:
                candidate["merged_into"] = overlap["id"]
                candidate["rejection"] = "Already qualified from the initial pass."
        new = [c["id"] for c in ranked if c["id"] not in prior and not c.get("rejection")
               and hour_index(payoff(c),asset["duration"]) in under]
        supplement = dict(selection, proposals=additions, shortlist=new,
                          chapters=[{k:c[k] for k in ("start","end","abstention","judgment","followup") if k in c}
                                    for c in chapters])
        streamer_asr.write(saved,supplement)
    supplement = streamer_asr.read(saved)
    merged = dict(selection,chapters=supplement["chapters"],
                  shortlist=list(dict.fromkeys(selection["shortlist"]+supplement["shortlist"])),
                  proposals=list({c["id"]:c for c in selection["proposals"]+supplement["proposals"]}.values()))
    if not (folder/"initial-selection.json").is_file():
        streamer_asr.write(folder/"initial-selection.json",selection)
    streamer_asr.write(folder/"selection.json",merged)
    return streamer_auto.process(root,row,supplement,transcript,config,checkpoint,update,first_run=first)


def save_review(root, job, identity, fields):
    folder = directory(root, job)
    path = folder / "selection.json"
    if not path.is_file() or identity not in {item["id"] for item in streamer_asr.read(path)["proposals"]}:
        raise ValueError("Candidate could not be found for this run.")
    decision = fields.get("decision")
    reason = fields.get("reason", "").strip()
    if decision not in ("accepted", "rejected") or not 1 <= len(reason) <= 4096:
        raise ValueError("Enter a review decision and the observed evidence.")
    if decision == "accepted" and not (folder / identity / "portrait.mp4").is_file():
        raise ValueError("A playable portrait master is required for acceptance.")
    candidate = folder / identity
    candidate.mkdir(exist_ok=True)
    streamer_asr.write(candidate / "review.json", {"decision": decision, "reason": reason})
