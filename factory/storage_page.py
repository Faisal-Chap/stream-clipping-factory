"""Inventory and retention controls for the Factory workspace."""

import json
import os
import shutil
import stat
import time
from collections import Counter
from urllib.parse import urlencode

from . import daily_release, portal_settings, reclamation, shorts_release, views

h,form,button = views.h,views.form,views.button
VIDEO = {".mp4",".m4v",".mov",".mkv",".webm",".avi",".ts",".mpeg",".mpg"}
MEDIA_TYPES = {
    ".mp4":"video/mp4", ".m4v":"video/mp4", ".mov":"video/quicktime", ".mkv":"video/x-matroska",
    ".webm":"video/webm", ".avi":"video/x-msvideo", ".ts":"video/mp2t", ".mpeg":"video/mpeg", ".mpg":"video/mpeg",
    ".flac":"audio/flac", ".wav":"audio/wav", ".mp3":"audio/mpeg", ".m4a":"audio/mp4", ".ogg":"audio/ogg",
    ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png",
    ".ass":"text/plain; charset=utf-8", ".srt":"text/plain; charset=utf-8", ".vtt":"text/vtt; charset=utf-8",
}
GROUPS = {
    "streamer-runs":"Reels and job files",
    "streamer-intake":"Source footage and audio cache",
    "masters":"Source recordings",
    "staging":"Incoming recordings",
    "streamer-audio":"Local audio",
    "streamer-transcripts":"Transcripts",
    "previews":"Previews",
    "analysis":"Analysis",
    "deployment-evidence":"Deployment evidence",
}


def owned_info(path, device):
    try:
        info = path.lstat()
    except OSError:
        return None
    if (info.st_dev!=device or stat.S_ISLNK(info.st_mode)
            or getattr(info,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0)):
        return None
    return info


def file_type(relative):
    if relative.name=="transcript.json" and relative.parts[0]=="streamer-transcripts":
        return "application/json"
    return MEDIA_TYPES.get(relative.suffix.lower())


def explorer_path(root, value, *, folder=False):
    parts = value.split("/")
    if len(value)>2048 or parts[0] not in GROUPS or any(
            segment in ("",".","..") or any(c in segment for c in "\\:\x00") for segment in parts):
        return None
    device = root.stat().st_dev
    path = root
    for segment in parts:
        path = path/segment
        info = owned_info(path,device)
        if info is None:
            return None
    if folder:
        return path if stat.S_ISDIR(info.st_mode) else None
    if stat.S_ISREG(info.st_mode) and file_type(path.relative_to(root)):
        return path


def explorer_rows(root, folder):
    parent = explorer_path(root,folder,folder=True) if folder else root
    if not parent:
        raise ValueError("Folder unavailable.")
    folders,files = [],[]
    device = root.stat().st_dev
    for entry in sorted(parent.iterdir(),key=lambda path:path.name.casefold()):
        info = owned_info(entry,device)
        if info is None:
            continue
        relative = entry.relative_to(root)
        if stat.S_ISDIR(info.st_mode) and relative.parts[0] in GROUPS:
            label = entry.name if folder else GROUPS[entry.name]
            folders.append(f'<li><a href="/storage?{h(urlencode({"folder":relative.as_posix()}))}">{h(label)}</a></li>')
        elif folder and stat.S_ISREG(info.st_mode) and file_type(relative):
            files.append((info.st_mtime,relative.as_posix(),GROUPS[relative.parts[0]],info.st_size))
    return folders,files


def size(amount):
    if amount>=10**9:
        return f"{amount/10**9:,.2f} GB"
    if amount>=10**6:
        return f"{amount/10**6:,.1f} MB"
    if amount>=10**3:
        return f"{amount/10**3:,.1f} KB"
    return f"{amount:,} B"


def inventory(root):
    device = root.stat().st_dev
    totals,videos,seen = Counter(),[],set()

    for folder,dirs,files in os.walk(root,followlinks=False):
        parent = root.joinpath(folder)
        dirs[:] = [name for name in dirs if owned_info(parent/name,device) is not None]
        for name in files:
            path = parent/name
            info = owned_info(path,device)
            if info is None or not stat.S_ISREG(info.st_mode):
                continue
            relative = path.relative_to(root)
            group = GROUPS.get(relative.parts[0],"Other Factory files")
            identity = (info.st_dev,info.st_ino)
            if info.st_nlink==1 or not info.st_ino or identity not in seen:
                totals[group] += info.st_blocks*512 if hasattr(info,"st_blocks") else info.st_size
            if info.st_nlink>1 and info.st_ino:
                seen.add(identity)
            if path.suffix.lower() in VIDEO and relative.parts[0] in GROUPS:
                videos.append((info.st_mtime,relative.as_posix(),group,info.st_size))
    return totals,sorted(videos,reverse=True)


def reels(db, root):
    rows = db.execute("""SELECT e.id,e.job,e.candidate,e.plan,d.story,d.digest,j.source,
                                COALESCE(a.protected,0) AS protected, COALESCE(a.state,'owned') AS state
                         FROM done_editions d JOIN streamer_editions e ON e.id=d.edition
                         JOIN jobs j ON j.id=e.job LEFT JOIN factory_release_artifacts a ON a.edition=e.id
                             AND a.path LIKE '%/portrait.mp4'
                         WHERE e.render_state='exported'""")
    return {shorts_release.portrait_path(root,row).relative_to(root).as_posix():row for row in rows}


def buffer_status(db, root, enabled):
    detail = db.execute("SELECT detail FROM factory_release_refill").fetchone()[0]
    return {"count":daily_release.buffer_count(db,root),
            "detail":(detail or "Waiting for the next check.") if enabled else "Scheduler off."}


def page(db, root, token, query):
    totals,videos = inventory(root.resolve())
    folder = query.get("folder","")
    folders,folder_files = explorer_rows(root.resolve(),folder)
    files = folder_files if folder else videos
    crumbs = '<a href="/storage#files">Files</a>'
    if folder:
        parts = folder.split("/")
        for index,part in enumerate(parts,1):
            label = GROUPS.get(part,part) if index==1 else part
            crumbs += f'<a href="/storage?{h(urlencode({"folder":"/".join(parts[:index])}))}#files">{h(label)}</a>'
    owned = reels(db,root)
    settings,versions = portal_settings.current(db)
    cleanup = settings["uploads"]
    now = time.time()
    last_page = max(1,(len(files)+29)//30)
    current = min(last_page,max(1,int(query.get("page","1")))) if query.get("page","1").isdigit() else 1
    rows = []
    for _,path,group,amount in files[(current-1)*30:current*30]:
        name,status = path.rsplit("/",1)[-1],"Protected"
        label = "Play" if path.lower().endswith(tuple(VIDEO)) else "Open"
        actions = f'<a href="/storage/file?{h(urlencode({"path":path}))}" target="_blank">{label}</a>'
        if row := owned.get(path):
            name = json.loads(row["plan"]).get("title") or name
            identity = row["id"]
            confirmed = reclamation.confirmed_at(db,identity)
            status = "Published" if confirmed is not None else "Retained reel"
            entry = dict(row,edition=identity,path=path,absolute=str(root/path))
            if row["state"]=="deleting":
                status = "Deletion pending"
            elif row["state"]=="owned" and reclamation.can_reclaim(db,entry,now,0,manual=True):
                actions += ('<details class="storage-delete"><summary>Delete</summary>'
                           +form(f"/storage/delete/{identity}",token,
                                 '<label class="toggle"><input type="checkbox" name="confirm" required>'
                                 ' Delete the local video file</label>'+button("Delete file","operator-danger"))
                           +'</details>')
            else:
                status += " · Protected"
        rows.append(f'<tr><th scope="row"><strong>{h(name)}</strong><small>{h(path)}</small></th>'
                    f'<td>{h(group)}</td><td class="operator-number">{size(amount)}</td>'
                    f'<td>{h(status)}</td><td>{actions}</td></tr>')
    categories = "".join(f'<li><span>{h(group)}</span><strong>{size(amount)}</strong></li>'
                         for group,amount in totals.most_common())
    options = "".join(f'<option value="{days}"{" selected" if cleanup["grace_hours"]==days*24 else ""}>'
                      f'{days} days</option>' for days in (3,4))
    controls = form("/storage/settings",token,
                    f'<input type="hidden" name="version" value="{versions.get("uploads",0)}">'
                    f'<label class="toggle"><input type="checkbox" name="reclaim"{" checked" if cleanup["reclaim"] else ""}>'
                    ' Automatically remove published reels</label>'
                    f'<label>Keep after confirmation<select name="days">{options}</select></label>'
                    +button("Save cleanup","operator-primary"))
    buffer = buffer_status(db,root,cleanup["refill"])
    minimum_gap, maximum_gap = portal_settings.NUMBERS["spacing"]
    schedule = form("/storage/scheduler",token,
                    f'<input type="hidden" name="version" value="{versions.get("uploads",0)}">'
                    f'<label class="toggle"><input type="checkbox" name="refill"'
                    f'{" checked" if cleanup["refill"] else ""}> Automatic scheduling</label>'
                    f'<label>Minimum<input name="minimum" type="number" min="1" max="999" required value="{cleanup["refill_low"]}"></label>'
                    f'<label>Maximum<input name="maximum" type="number" min="2" max="1000" required value="{cleanup["refill_high"]}"></label>'
                    f'<label>Gap between reels (minutes)<input name="spacing" type="number" min="{minimum_gap}" '
                    f'max="{maximum_gap}" step="1" required value="{cleanup["spacing"]}"></label>'
                    +button("Save scheduler","operator-primary"))
    nav = ""
    if last_page>1:
        previous = f'<a href="/storage?{h(urlencode({"folder":folder,"page":current-1}))}#files">Previous</a>' if current>1 else ""
        following = f'<a href="/storage?{h(urlencode({"folder":folder,"page":current+1}))}#files">Next</a>' if current<last_page else ""
        nav = f'<nav class="storage-pagination" aria-label="Video pages">{previous}<span>{current} / {last_page}</span>{following}</nav>'
    return ('<div class="operator-workspace storage-workspace"><header class="operator-heading"><div><h1>Storage</h1>'
            f'<p>{size(sum(totals.values()))} used by Factory · {size(shutil.disk_usage(root).free)} free on this disk</p>'
            '</div></header><div class="storage-overview"><section><h2>Usage by category</h2>'
            f'<ul>{categories}</ul></section><section id="cleanup"><h2>Automatic cleanup</h2>'
            '<p>Every recorded destination must confirm publishing before the countdown starts. '
            'Source recordings, evidence, and files in use are protected.</p>'+controls+'</section>'
            '<section id="scheduler"><h2>Buffer scheduler</h2>'
            f'<p><strong data-buffer-count>{views.count_label(buffer["count"],"reel")} in buffer</strong></p>'
            f'<p data-buffer-detail>{h(buffer["detail"])}</p>'
            '<p>Starts below minimum. At maximum, the current hour finishes before the scheduler waits.</p>'
            +schedule+'</section></div>'
            f'<h2 class="storage-video-heading" id="files">File explorer</h2>'
            f'<nav class="storage-crumbs" aria-label="Folders">{crumbs}</nav>'
            +('<ul class="storage-folders">'+''.join(folders)+'</ul>' if folders else '')
            +f'<h2 class="storage-video-heading">{"Files" if folder else "Stored videos"} <span>{len(files)}</span></h2>'
            '<div class="directory-table storage-table" tabindex="0" role="region" aria-label="Stored videos"><table>'
            '<thead><tr><th scope="col">Video</th><th scope="col">Type</th><th scope="col" class="operator-number">Size</th>'
            '<th scope="col">Status</th><th scope="col">Actions</th></tr></thead><tbody>'
            +("".join(rows) or '<tr><td class="operator-empty" colspan="5">No files to open here.</td></tr>')
            +'</tbody></table></div>'+nav+'</div>')
