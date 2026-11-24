"""Loopback HTTP routes, form protection and ranged asset playback."""

import json
import re
import secrets
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import blind_review, report, settings, store, views, streamer_catalog, streamer_run, streamer_views, streamer_asr
from . import streamer_edits, streamer_edit_views, streamer_transfer
from . import factory_done, factory_events, factory_portal, factory_workspace, indiepost, media, reclamation
from . import operator_workspace, portal_controls, portal_settings, portal_sources, portal_templates, source_queue
from . import storage_page
from .library import identify

STATIC = Path(__file__).with_name("static")


class FactoryServer(ThreadingHTTPServer):
    def __init__(self, port, root, table, worker, *, listen="127.0.0.1"):
        super().__init__((listen, port), Handler)
        self.root, self.table, self.worker = root, table, worker
        self.host = f"127.0.0.1:{self.server_port}"
        self.url = f"http://{self.host}"
        self.token = secrets.token_urlsafe(32)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Source values, form data and request URLs never go into access logs.
        pass

    def send_body_headers(self, code, kind, length, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'; object-src 'none'; "
                         "img-src 'self' https://i.ytimg.com https://img.youtube.com")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()

    def respond(self, code, message="", kind="text/plain; charset=utf-8", extra=()):
        body = message.encode() if isinstance(message, str) else message
        self.send_body_headers(code, kind, len(body), extra)
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def allowed_host(self):
        if self.headers.get_all("Host") != [self.server.host]:
            self.respond(403, "Open the factory using its printed loopback URL.")
            return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.allowed_host():
            return
        target = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(target.query).items()}
        path = target.path
        retired = {"/":"/sources","/streamer":"/sources","/review":"/factory",
                   "/settings":"/controls#connections","/compare":"/factory#legacy-work"}
        if path in retired:
            destination = retired[path]
            if path=="/" and query.get("source","").isdigit():
                destination = "/sources"
            if path=="/review":
                query["review"] = "unreviewed"
            if query.get("job","").isdigit():
                query["attempt"] = query.pop("job")
            if path=="/review" and query.get("source","").isdigit():
                query["stream"] = query.pop("source")
                if "attempt" not in query:
                    with store.connect(self.server.root) as db:
                        attempt = db.execute("""SELECT id FROM jobs WHERE source=? ORDER BY id DESC LIMIT 1""",
                                             (query["stream"],)).fetchone()
                    if attempt:
                        query["attempt"] = attempt["id"]
            target,separator,anchor = destination.partition("#")
            return self.respond(303,extra=[("Location",target+("?" + urlencode(query) if query else "")+separator+anchor)])
        if match := re.fullmatch(r"/templates/asset/(proof|reference|font)/([a-z0-9-]+)",path):
            try:
                asset = portal_templates.asset(*match.groups())
                return self.send_file(asset,{"proof":"image/jpeg","reference":"image/png","font":"font/ttf"}[match[1]])
            except (ValueError,OSError):
                return self.respond(404,"Template asset unavailable.")
        if match := re.fullmatch(r"/factory/run/(\d+)/frame/(c\d+-\d+)",path):
            try:
                frame = factory_portal.frame(self.server.root,int(match[1]),match[2])
            except (ValueError,OSError,media.MediaError):
                return self.respond(404,"Source frame unavailable.")
            return self.respond(200,frame,"image/jpeg")
        if match := re.fullmatch(r"/streamer/run/(\d+)(?:/(c\d+-\d+)(?:/edition/([0-9a-f]{64}))?)?/poster\.jpg",path):
            try:
                return self.respond(200,self.streamer_poster(*match.groups(),query.get("at")),"image/jpeg")
            except (ValueError,OSError,media.MediaError):
                return self.respond(404,"Poster unavailable.")
        if path in ("/style.css", "/app.js"):
            kind = "text/css" if path.endswith(".css") else "text/javascript"
            return self.respond(200, (STATIC / path[1:]).read_bytes(), kind)
        if path=="/storage/file":
            root = self.server.root.resolve()
            file = storage_page.explorer_path(root,query.get("path",""))
            if not file:
                return self.respond(404,"Workspace file unavailable.")
            relative = file.relative_to(root)
            if match := re.fullmatch(r"streamer-runs/(\d+)/(c\d+-\d+)/editions/[0-9a-f]{64}/portrait\.mp4",
                                     relative.as_posix()):
                reclamation.lease(root,int(match[1]),match[2])
            return self.send_file(file,storage_page.file_type(relative))
        if path=="/storage/buffer":
            with store.connect(self.server.root) as db:
                enabled = portal_settings.current(db)[0]["uploads"]["refill"]
                data = storage_page.buffer_status(db,self.server.root,enabled)
            return self.respond(200,json.dumps(data),"application/json")
        if match := re.fullmatch(r"/media/(\d+)(?:/(?:frame/([0-4])|(context|alternate)))?", path):
            return self.serve_media(int(match[1]), match[2], match[3])
        if match := re.fullmatch(r"/blind/([0-9a-f]{32})/(a|b)", path):
            with store.connect(self.server.root) as db:
                pair = db.execute("SELECT story_a,story_b FROM blind_pairs WHERE id=?", (match[1],)).fetchone()
            return self.serve_media(pair[f"story_{match[2]}"] if pair else None, None)
        if match := re.fullmatch(r"/blind/([0-9a-f]{32})/results", path):
            try:
                with store.connect(self.server.root) as db:
                    result = blind_review.results(db, match[1])
            except ValueError as error:
                return self.respond(409, str(error))
            return self.respond(200, json.dumps(result, indent=2), "application/json")
        if match := re.fullmatch(r"/report/(\d+)", path):
            number = int(match[1])
            with store.connect(self.server.root) as db:
                data = report.job_report(db, number)
            if data is None:
                return self.respond(404, "Job not found.")
            return self.respond(200, json.dumps(data, indent=2), "application/json",
                                [("Content-Disposition", f'attachment; filename="factory-job-{number}.json"')])
        if match := re.fullmatch(r"/streamer/run/(\d+)/selection-range/(\d+)\.mp4",path):
            return self.selected_range(int(match[1]),int(match[2]))
        if match := re.fullmatch(r"/jobs/evidence/(\d+)/(deepseek|audio/(\d+)|transcript/(\d+))",path):
            return self.job_evidence(int(match[1]),match[2],match[3] or match[4])
        if match := re.fullmatch(r"/jobs/(\d+)/live-log",path):
            after = query.get("after","0")
            if not re.fullmatch(r"\d{1,18}",after):
                return self.respond(400,"Invalid log cursor.")
            return self.job_log(int(match[1]),int(after))
        if match := re.fullmatch(r"/jobs/source/(\d+)/replay-progress",path):
            from . import replay_cache
            with store.connect(self.server.root) as db:
                row = db.execute("SELECT id,revision FROM sources WHERE id=?",(int(match[1]),)).fetchone()
                if not row:
                    return self.respond(404,"Source not found.")
                payload = replay_cache.progress(self.server.root,db,row["id"],row["revision"])
            return self.respond(200,json.dumps(payload),"application/json")
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/hybrid/((?:overview|closer)-[1-6]\.jpg)",path):
            return self.streamer_media(int(match[1]),f"{match[2]}/hybrid/{match[3]}")
        if match := re.fullmatch(r"/streamer/run/(\d+)/(archive|transcript\.json|selection\.json|audio-selection\.json|production(?:-revisit)?\.json|c\d+-\d+/(?:portrait\.mp4|source-context\.mp4|words\.ass))", path):
            return self.streamer_media(int(match[1]), match[2])
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/edition/([0-9a-f]{64})/(portrait\.mp4|captions\.ass|manifest\.json)", path):
            job, candidate, identity, artifact = match.groups()
            with store.connect(self.server.root) as db:
                exists = db.execute("""SELECT 1 FROM streamer_editions WHERE job=? AND candidate=? AND id=?
                                       AND render_state='exported'""", (job, candidate, identity)).fetchone()
            if exists:
                if artifact=="portrait.mp4":
                    reclamation.lease(self.server.root,int(job),candidate)
                return self.streamer_media(int(job), f"{candidate}/editions/{identity}/{artifact}")
            return self.respond(404, "Edition artifact unavailable.")
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/audition/([0-9a-f]{64})/(portrait\.mp4|manifest\.json|qc\.json)",path):
            return self.streamer_media(int(match[1]),f"{match[2]}/auditions/{match[3]}/{match[4]}")
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/blind/([0-9a-f]{64})/([0-9a-f]{32})/([ab])", path):
            job, candidate, identity, ballot, slot = match.groups()
            try:
                with store.connect(self.server.root) as db:
                    original, version = streamer_edits.comparison(self.server.root, db, int(job), candidate, identity)
            except ValueError:
                return self.respond(404, "Comparison unavailable.")
            return self.send_file(version if slot == streamer_edits.version_slot(ballot) else original, "video/mp4")
        if path not in ("/factory","/sources","/jobs","/storage","/controls","/templates") and not re.fullmatch(
                r"/(?:factory/run/\d+|streamer/run/\d+(?:/c\d+-\d+/(?:edit|blind/[0-9a-f]{64}))?)", path):
            return self.respond(404, "Page not found.")
        try:
            self.screen(path, query)
        except ValueError:
            self.respond(400, "Invalid page parameters.")

    def screen(self, path, query, status=200, fields=None):
        token = self.server.token
        stream = query.get("stream") if query.get("stream","").isdigit() else None
        attempt = query.get("attempt") if query.get("attempt","").isdigit() else None
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/(?:edit|blind/[0-9a-f]{64})",path):
            reclamation.lease(self.server.root,int(match[1]),match[2])
        with store.connect(self.server.root) as db:
            if match := re.match(r"/(?:factory|streamer)/run/(\d+)",path):
                attempt = match[1]
                row = db.execute("SELECT source FROM jobs WHERE id=?",(attempt,)).fetchone()
                stream = row["source"] if row else None
            if path == "/factory":
                title, body = "Factory", factory_workspace.home(db,self.server.root,token,query)
            elif path=="/sources":
                title = "Sources"
                legacy = any(query.get(key) for key in ("roster","creator","queue","source"))
                body = (portal_sources.page(db,token,query,fields,self.server.root) if legacy else
                        operator_workspace.sources(db,self.server.root,token,query,fields))
            elif path=="/jobs":
                title,body = "Jobs",operator_workspace.jobs_page(db,self.server.root,token,query)
            elif path=="/storage":
                title,body = "Storage",storage_page.page(db,self.server.root,token,query)
            elif path=="/controls":
                title = "Controls & settings"
                body = portal_controls.page(db,self.server.root,token,self.server.worker.connections.vault.available,query,fields)
            elif path=="/templates":
                title,body = "Edit templates",portal_templates.page(db,token,query)
            elif match := re.fullmatch(r"/factory/run/(\d+)",path):
                title, body = "Run monitor", factory_portal.detail(db,self.server.root,token,int(match[1]),query)
            elif match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/edit", path):
                title = "Portrait editor"
                body = streamer_edit_views.page(db, self.server.root, token, int(match[1]), match[2], query.get("edition"), fields)
            elif match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/blind/([0-9a-f]{64})", path):
                title = "Blind comparison"
                body = streamer_edit_views.blind_page(db, self.server.root, token, int(match[1]), match[2], match[3])
            elif path.startswith("/streamer/run/"):
                title = "Streamer run"
                body = streamer_views.run_page(db, self.server.root, token, int(path.rsplit("/", 1)[1]))
            else:
                raise ValueError("Unknown workspace page.")
        active = path if path in ("/sources","/storage","/controls","/templates") else "/jobs"
        self.respond(status, views.page(title,active,body,query.get("message",""),stream,attempt), "text/html; charset=utf-8")

    def do_POST(self):
        if not self.allowed_host():
            return
        if (self.headers.get("Origin") not in (None, self.server.url)
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            return self.respond(403, "Cross-site requests are blocked.")
        path = urlsplit(self.path).path
        if path=="/sources/recording-upload":
            if not secrets.compare_digest(self.headers.get("X-CSRF","").encode(),self.server.token.encode()):
                return self.respond(403,"This form expired. Reload Sources.")
            return self.receive_recording()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 128 * 1024 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError()
            fields = {key: values[-1] for key, values in parse_qs(
                self.rfile.read(length).decode("utf-8"), keep_blank_values=True, max_num_fields=60).items()}
        except (UnicodeError, ValueError):
            return self.respond(400, "Invalid form submission.")
        if not secrets.compare_digest(fields.pop("csrf", "").encode(), self.server.token.encode()):
            return self.respond(403, "This form expired. Reload the page and try again.")
        try:
            if path=="/sources/assign":
                sid,creator = int(fields["source"]),int(fields["creator"])
                with store.connect(self.server.root) as db:
                    original = db.execute("SELECT * FROM sources WHERE id=?",(sid,)).fetchone()
                    if not original:
                        raise ValueError("Source could not be found.")
                self.source_intake(f"/sources/creator/{creator}/video/preview",
                                   {"link":original["link"],"location":original["location"]})
                return
            if match := re.fullmatch(r"/sources/link/(\d+)/start",path):
                destination,query,message = self.start_link(int(match[1]),fields)
            elif match := re.fullmatch(r"/storage/delete/([0-9a-f]{64})",path):
                if fields.get("confirm")!="on":
                    raise ValueError("Confirm that you want to delete this local reel.")
                try:
                    deleted = reclamation.delete_portrait(self.server.root,match[1])
                except OSError:
                    raise ValueError("The file could not be removed. A saved deletion will retry when possible.") from None
                destination,query = "/storage",{}
                message = (f"Local reel deleted. {storage_page.size(deleted)} reclaimed." if deleted
                           else "The file is already removed or its deletion is pending.")
            elif path in ("/sources/queue/resolve","/sources/queue/save"):
                result = self.queue_intake(path,fields)
                if result is None:
                    return
                destination,query,message = result
            elif match := re.fullmatch(r"/sources/link/(\d+)/refresh",path):
                from . import flashdata, flashdata_profiles
                root = self.server.root
                with store.connect(root) as db:
                    row = db.execute("SELECT identity FROM sources WHERE id=? AND state!='archived'",(int(match[1]),)).fetchone()
                if not row or not row["identity"].startswith("youtube:"):
                    raise ValueError("This YouTube source is unavailable.")
                flashdata_profiles.source(root,flashdata.required(root),row["identity"].removeprefix("youtube:"),refresh=True)
                destination,query,message = f"/sources#source-{match[1]}",{},"Source formats refreshed."
            elif re.fullmatch(r"/sources/(?:channel/(?:preview|save)|creator/\d+/(?:channel/(?:preview|save)|video/(?:preview|save)|discover/\d+))",path):
                result = self.source_intake(path,fields)
                if result is None:
                    return
                destination,query,message = result
            elif path.startswith("/connections/indiepost/"):
                destination,query,message = self.indiepost_action(path,fields)
            elif match := re.fullmatch(r"/factory/done/(\d+)/indiepost/(send|refresh)",path):
                story = int(match[1])
                vault = self.server.worker.connections.vault
                method = match[2]
                message = (indiepost.send(self.server.root,vault,story,fields) if method=="send"
                           else indiepost.refresh(self.server.root,vault,story,int(fields["intent"])))
                destination,query = f"/factory#story-{story}",{"work":"done"}
            elif path.startswith("/streamer/"):
                destination, query, message = self.streamer_action(path, fields)
            else:
                with store.connect(self.server.root, True) as db:
                    destination, query, message = self.action(db, path, fields)
        except (ValueError, KeyError,media.MediaError) as error:
            message = str(error) if not isinstance(error,KeyError) else "A required field is missing."
            if match := re.fullmatch(r"/controls/([a-z]+)/save",path):
                return self.screen("/controls",{"group":match[1],"message":message},400,fields)
            if path=="/sources/link/add":
                return self.screen("/sources",{"add":"1","message":message},400,fields)
            if match := re.fullmatch(r"/sources/link/(\d+)/edit",path):
                return self.screen("/sources",{"edit":match[1],"message":message},400,fields)
            if path == "/source/add" or re.fullmatch(r"/source/\d+/edit", path):
                query = {"message": message}
                if path.endswith("/edit"):
                    query["source"] = path.split("/")[2]
                return self.screen("/sources", query, 400, fields)
            if re.fullmatch(r"/streamer/run/\d+/c\d+-\d+/edit", path):
                return self.screen(path, {"message": message}, 400, fields)
            if re.fullmatch(r"/streamer/run/\d+/c\d+-\d+/blind/[0-9a-f]{64}", path):
                return self.screen(path, {"message": message}, 400)
            destination = ("/controls#connections" if path.startswith(("/settings/","/provider/","/connections/")) else
                           "/factory" if path.startswith(("/job/","/review/","/compare/")) else
                           "/templates" if path.startswith("/templates/") else "/sources")
            if path.startswith("/factory/"):
                destination = re.sub(r"/(?:resume|budget|revisit)$","",path) if "/run/" in path else "/factory"
            query = {}
            if path.startswith("/storage/"):
                destination = "/storage"
            if path=="/storage/scheduler":
                destination = "/storage#scheduler"
            if re.fullmatch(r"/jobs/\d+/(pause|resume)",path):
                destination = "/jobs"
            if match := re.fullmatch(r"/(sources|jobs)/source/(\d+)/replay/(pause|resume|extend)",path):
                section, source, _ = match.groups()
                destination = f"/jobs#job-source-{source}" if section=="jobs" else f"/sources#source-{source}"
            if match := re.fullmatch(r"/sources/link/(\d+)/(start|delete)",path):
                destination = f"/sources#source-{match[1]}"
            if match := re.fullmatch(r"/factory/done/(\d+)/(?:manual|indiepost/(?:send|refresh))",path):
                destination,query = f"/factory#story-{match[1]}",{"work":"done"}
            if match := re.match(r"/sources/creator/(\d+)",path):
                query["creator"] = match[1]
                destination = f"/sources#creator-{match[1]}"
            if match := re.match(r"/(?:sources/video|streamer/stream)/(\d+)",path):
                with store.connect(self.server.root) as db:
                    owner = db.execute("""SELECT a.creator FROM streamer_streams st JOIN streamer_accounts a
                                         ON a.id=st.account WHERE st.source=?""",(match[1],)).fetchone()
                if owner:
                    query["creator"] = owner["creator"]
                    destination = f"/sources#roster-stream-{match[1]}"
            if match := re.match(r"/source/(\d+)/",path):
                query["source"] = match[1]
                destination = "/sources#source-form"
            if match := re.fullmatch(r"/templates/([a-z0-9-]+)/(save|activate|archive|duplicate)",path):
                query["design"] = match[1]
            if match := re.fullmatch(r"/factory/run/(\d+)/(?:reel/.+|continue-analysis|workspace-(?:pause|resume))",path):
                with store.connect(self.server.root) as db:
                    source = db.execute("SELECT source FROM jobs WHERE id=?",(match[1],)).fetchone()
                destination = f'/factory#stream-{source["source"]}' if source else "/factory"
                query = {"attempt":match[1]}
        self.server.worker.wake.set()
        path, separator, anchor = destination.partition("#")
        location = path + "?" + urlencode(dict(query, message=message)) + separator + anchor
        self.respond(303, extra=[("Location", location)])

    def receive_recording(self):
        import hashlib

        root = self.server.root
        with store.connect(root) as db:
            limits = portal_settings.current(db)[0]["resources"]
        try:
            size = int(self.headers.get("Content-Length","0"))
            if (self.headers.get_content_type()!="application/octet-stream" or not 0<size<=limits["remote_mib"]*media.CHUNK
                    or shutil.disk_usage(root).free-size<limits["reserve_mib"]*media.CHUNK):
                raise ValueError()
        except ValueError:
            return self.respond(400,"Recording exceeds the configured archive size or free storage reserve.")
        folder = root/"gta-streamer-local-browser-recordings"
        folder.mkdir(exist_ok=True)
        pending = folder/f"gta-streamer-local-incoming-{secrets.token_hex(10)}.partial"
        digest = hashlib.sha256()
        try:
            with pending.open("xb") as writer:
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining,media.CHUNK))
                    if not chunk:
                        raise ValueError("File transfer ended before the recording arrived.")
                    digest.update(chunk)
                    writer.write(chunk)
                    remaining -= len(chunk)
            media.probe(pending)
            final = folder/f"gta-streamer-local-recording-{digest.hexdigest()}.media"
            if not final.exists():
                pending.replace(final)
            return self.respond(200,json.dumps({"path":str(final)}),"application/json")
        except (ValueError,OSError,media.MediaError):
            return self.respond(400,"The recording could not be received or decoded.")
        finally:
            pending.unlink(missing_ok=True)

    def source_intake(self,path,fields):
        root = self.server.root
        match = re.match(r"/sources/creator/(\d+)/",path)
        creator = int(match[1]) if match else None
        if "/channel/" in path:
            channel,_ = streamer_catalog.discovery(streamer_catalog.account_url(fields["account"]))
            if path.endswith("/preview"):
                body = portal_sources.confirm(self.server.token,path.replace("/preview","/save"),fields,
                                              "Matched YouTube account",channel)
                self.respond(200,views.page("Confirm channel","/sources",body),"text/html; charset=utf-8")
                return None
            with store.connect(root,True) as db:
                if creator:
                    streamer_catalog.roster(db,creator)
                creator = portal_sources.add_channel(db,fields,channel,creator)
            return f"/sources#creator-{creator}",{"creator":creator},"Creator account saved."
        if match := re.search(r"/discover/(\d+)$",path):
            with store.connect(root) as db:
                owner = streamer_catalog.roster(db,creator,match[1])
            _,entries = streamer_catalog.discovery(owner["url"])
            with store.connect(root,True) as db:
                db.execute("UPDATE streamer_accounts SET checked=CURRENT_TIMESTAMP WHERE id=?",(owner["account"],))
                db.execute("DELETE FROM streamer_discovery WHERE account=?",(owner["account"],))
                db.executemany("INSERT OR IGNORE INTO streamer_discovery VALUES(?,?,?)",
                               [(owner["account"],entry["id"],entry["title"]) for entry in entries])
            return f"/sources#creator-{creator}",{"creator":creator},f"Found {len(entries)} completed GTA broadcasts."
        with store.connect(root) as db:
            owner,canonical,info = portal_sources.resolve_video(db,creator,fields)
        fields = dict(fields,account=owner["account"],link=canonical)
        title = (info or {}).get("title") or fields.get("title") or Path(canonical).name
        fields["title"] = title
        if path.endswith("/preview"):
            body = portal_sources.confirm(self.server.token,path.replace("/preview","/save"),fields,title,
                                          f'{owner["name"]} · YouTube {owner["platform_id"]}')
            self.respond(200,views.page("Confirm recording","/sources",body),"text/html; charset=utf-8")
            return None
        duration = info.get("duration") if info else media.probe(Path(canonical))["duration"]
        with store.connect(root,True) as db:
            source = streamer_catalog.attach(db,creator,fields,info)
            db.execute("""INSERT INTO portal_source_metadata VALUES(?,?) ON CONFLICT(source)
                          DO UPDATE SET duration=excluded.duration""",(source,duration))
            source_queue.register(db,source,"youtube" if info else "file","attachment",
                                  "waiting" if info and info["live_status"] in source_queue.REPLAY_STATES
                                  and not fields.get("location") else "ready")
        return f"/sources#roster-stream-{source}",{"creator":creator},"Recording attached."

    def queue_intake(self,path,fields):
        root = self.server.root
        with store.connect(root) as db:
            height = portal_settings.current(db)[0]["resources"]["remote_height"]
        canonical,platform,info,readiness = resolved = source_queue.resolve(
            fields["link"],fields.get("archive_upload")=="on",height,root=root)
        if path.endswith("/save"):
            with store.connect(root,True) as db:
                source,creator = source_queue.attach(db,fields,resolved)
            return f"/sources#queue-source-{source}",{"queue":source},"Stream link saved in Sources."
        h,form,button = views.h,views.form,views.button
        hidden = "".join(f'<input type="hidden" name="{name}" value="{h(value)}">'
                         for name,value in (("link",canonical),("previous",fields.get("previous","")),
                                            ("archive_upload",fields.get("archive_upload",""))))
        title = (info.get("title") if info else None) or canonical
        if info:
            with store.connect(root) as db:
                owner = db.execute("""SELECT c.name FROM streamer_accounts a JOIN streamer_creators c ON c.id=a.creator
                                      WHERE a.platform='youtube' AND a.platform_id=?""",(info["channel_id"],)).fetchone()
            if owner:
                detail = f'Creator · {h(owner["name"])}'
            else:
                detail = 'Add the verified channel to the roster.' + portal_sources.field(
                    "name","Creator name",info.get("channel") or info.get("uploader") or "",required=True)
                detail += portal_sources.field("game","Game","GTA VI",required=True)
            detail += f'<p>YouTube channel · {h(info["channel_id"])}</p>'
        else:
            detail = "Save this link while a supported replay adapter is pending."
        if readiness=="waiting":
            detail += "<p>The factory will check for a seekable replay.</p>"
        content = (f'<section class="panel operator-panel"><h1>{h(title)}</h1>'
                   f'<p><a href="{h(canonical)}">{h(canonical)}</a></p>'
                   +form("/sources/queue/save",self.server.token,hidden+detail+button("Save stream link","primary"))
                   +'<p><a href="/sources#stream-queue">Back to Sources</a></p></section>')
        self.respond(200,views.page("Confirm stream link","/sources",content),"text/html; charset=utf-8")

    def indiepost_action(self,path,fields):
        vault = self.server.worker.connections.vault
        root = self.server.root
        operation = path.rsplit("/",1)[-1]
        if operation=="test":
            with store.connect(root) as db:
                reference = portal_controls.connection_state(db)["reference"]
            key = vault.get(reference) if reference else None
            if not key:
                raise ValueError("Add an IndiePost key before testing.")
            try:
                accounts = portal_controls.list_destinations(key)
                status = f"Connection verified · {len(accounts)} connected accounts"
            except ValueError as error:
                status,accounts = str(error),[]
            with store.connect(root,True) as db:
                if portal_controls.connection_state(db)["reference"]!=reference:
                    raise ValueError("The key changed during the test.")
                db.execute("""UPDATE indiepost_connection SET status=?,last_test=CURRENT_TIMESTAMP,
                              accounts=?""",(status,json.dumps(accounts)))
            return "/controls#connections",{},status
        with store.connect(root,True) as db:
            previous = portal_controls.connection_state(db)["reference"]
            if operation=="key":
                key = fields["key"].strip()
                if not re.fullmatch(r"ip_[A-Za-z0-9_-]{8,509}",key):
                    raise ValueError("Enter an IndiePost API key beginning with ip_.")
                reference = "indiepost-"+secrets.token_hex(16)
                vault.put(reference,key)
                state = "Key stored · account test pending"
            elif operation=="remove":
                reference,state = None,"Not configured"
            else:
                raise ValueError("Unknown IndiePost action.")
            db.execute("UPDATE indiepost_connection SET reference=?,status=?,last_test=NULL,accounts='[]'",(reference,state))
        if previous:
            vault.remove(previous)
        return "/controls#connections",{},state

    def streamer_action(self, path, fields):
        root = self.server.root
        info, entries = None, None
        creator_op = re.fullmatch(r"/streamer/creator/(\d+)/(discover|attach|edit)", path)
        if path == "/streamer/creator/add":
            channel, _ = streamer_catalog.discovery(streamer_catalog.account_url(fields["account"]))
            with store.connect(root, True) as db:
                creator = streamer_catalog.save_creator(db, fields, channel)
            return f"/sources#creator-{creator}",{"creator":creator}, "Creator account saved."
        if creator_op:
            creator, operation = int(creator_op[1]), creator_op[2]
            with store.connect(root) as db:
                owner = streamer_catalog.roster(db, creator)
            if operation == "discover":
                _, entries = streamer_catalog.discovery(owner["url"])
            if operation == "attach":
                _, link, route = identify(fields["link"])
                if route == "youtube":
                    info = source_queue.resolve(link,True,1080,root=root)[2]
            with store.connect(root, True) as db:
                if operation == "discover":
                    db.execute("UPDATE streamer_accounts SET checked=CURRENT_TIMESTAMP WHERE id=?", (owner["account"],))
                    db.execute("DELETE FROM streamer_discovery WHERE account=?", (owner["account"],))
                    db.executemany("INSERT OR IGNORE INTO streamer_discovery VALUES(?,?,?)",
                                   [(owner["account"], entry["id"], entry["title"]) for entry in entries])
                    message = f"Found {len(entries)} completed GTA streams in the latest 30 broadcasts."
                elif operation == "attach":
                    streamer_catalog.attach(db, creator, fields, info)
                    message = "Stream added to the archive queue. Open its production settings to start."
                else:
                    credit = fields["credit"].strip()
                    if not 1 <= len(credit) <= 100:
                        raise ValueError("Enter the creator credit.")
                    db.execute("UPDATE streamer_creators SET camera=?,credit=?,enabled=? WHERE id=?",
                               (json.dumps(streamer_catalog.crop(fields["camera"])), credit,
                                fields.get("enabled") == "on", creator))
                    message = "Creator layout saved for new runs."
            return f"/sources#creator-{creator}",{"creator":creator},message
        if match := re.fullmatch(r"/streamer/stream/(\d+)/start", path):
            with store.connect(root, True) as db:
                return self.start_stream(db,int(match[1]),fields)
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/review", path):
            job = int(match[1])
            with store.connect(root, True):
                streamer_run.save_review(root, job, match[2], fields)
            return f"/streamer/run/{job}", {}, "Clip decision saved."
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/edit", path):
            with store.connect(root, True) as db:
                identity = streamer_edits.enqueue(root, db, int(match[1]), match[2], fields)
            return path, {"edition": identity}, "Version saved."
        if match := re.fullmatch(r"/streamer/run/(\d+)/(c\d+-\d+)/blind/([0-9a-f]{64})", path):
            with store.connect(root, True) as db:
                streamer_edits.comparison(root, db, int(match[1]), match[2], match[3])
                streamer_edits.save_ballot(db, match[3], fields.get("ballot"), fields)
            return path, {}, "Ballot saved."
        if match := re.fullmatch(r"(/streamer/run/(\d+)/(c\d+-\d+)/edit)/([0-9a-f]{64})/(retry|qa|feedback)", path):
            with store.connect(root, True) as db:
                row = db.execute("SELECT * FROM streamer_editions WHERE id=? AND job=? AND candidate=?",
                                 (match[4], match[2], match[3])).fetchone()
                if not row:
                    raise ValueError("Edition was not found.")
                if match[5] == "retry":
                    db.execute("""UPDATE streamer_editions SET render_state='queued',error=''
                                  WHERE id=? AND render_state='failed'""", (row["id"],))
                elif match[5]=="qa":
                    streamer_edits.review(db, root, row["id"], fields.get("reason", ""))
                else:
                    streamer_edits.feedback(db,row,fields)
            return match[1], {"edition": row["id"]}, "Edition status saved."
        raise ValueError("Action not found.")

    def selected_range(self, job, index):
        root = self.server.root.resolve()
        state = streamer_run.directory(root,job)/"audio-first.json"
        if state.is_file():
            windows = streamer_asr.read(state)["ranges"]
            if index < len(windows):
                if file := operator_workspace.range_artifact(root,job,windows[index]["path"]):
                    return self.send_file(file,"video/mp4")
        return self.respond(404,"Selected source range unavailable.")

    def streamer_media(self, job, name):
        root = self.server.root.resolve()
        folder = streamer_run.directory(root, job)
        path = folder / name
        kind = "video/mp4"
        if name in ("archive", "transcript.json"):
            if name=="archive" and (folder/"audio-first.json").is_file():
                return self.respond(404,"This attempt has selected ranges and the original platform replay.")
            report_path = folder / "selection.json"
            if report_path.is_file():
                data = streamer_asr.read(report_path)
                path = root / (data["asset"]["path"] if name=="archive" else data["transcript"])
            elif name=="archive":
                with store.connect(root) as db:
                    asset = db.execute("""SELECT a.path FROM assets a JOIN jobs j ON j.asset=a.hash
                                         WHERE j.id=?""",(job,)).fetchone()
                if not asset:
                    return self.respond(404,"Verified archive unavailable.")
                path = root/asset["path"]
            else:
                return self.respond(404, "Run evidence unavailable.")
            allowed = root / ("masters" if name == "archive" else "streamer-transcripts")
        else:
            allowed = folder
        if name.endswith(".json"):
            kind = "application/json"
        elif name.endswith(".ass"):
            kind = "text/plain; charset=utf-8"
        elif name.endswith(".jpg"):
            kind = "image/jpeg"
        path = path.resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            return self.respond(404, "Run artifact unavailable.")
        if name=="archive":
            with path.open("rb") as reader:
                if reader.read(4)==b"\x1aE\xdf\xa3":
                    kind = "video/webm"
        return self.send_file(path, kind)

    def job_log(self, job, after=0):
        with store.connect(self.server.root) as db:
            row = db.execute("SELECT 1 FROM jobs WHERE id=?",(job,)).fetchone()
            if not row:
                return self.respond(404,"Job not found.")
            order = "ASC" if after else "DESC"
            events = db.execute(f"""SELECT id,utc,attempt,stage,kind,item FROM factory_events
                                    WHERE job=? AND id>? AND kind!='pulse' ORDER BY id {order} LIMIT 100""",
                                (job,after)).fetchall()
        if not after:
            events.reverse()
        lines = [factory_events.log_line(e["utc"],job,e["attempt"],e["stage"],e["kind"],e["item"]) for e in events]
        return self.respond(200,"\n".join(lines),extra=[
            ("Cache-Control","no-store"),("X-Factory-Log-Cursor",str(events[-1]["id"] if events else after))])

    def job_evidence(self, job, kind, index):
        root = self.server.root
        if kind=="deepseek":
            with store.connect(root) as db:
                rows = db.execute("""SELECT result FROM provider_calls WHERE job=? AND provider='sail'
                                     AND result IS NOT NULL ORDER BY rowid""",(job,)).fetchall()
                record = db.execute("SELECT settings FROM jobs WHERE id=?",(job,)).fetchone()
            if rows:
                version = json.loads(record["settings"]).get("hybrid_prompt","Unversioned")
                formatted = [f"Saved selection workflow: {version}"]
                from . import streamer_intake
                validation = streamer_intake.read(streamer_run.directory(root,job)/"audio-selection.json",{}).get("validation")
                if validation is not None:
                    formatted.append("Transcript selection validation\n"+json.dumps(validation,ensure_ascii=False,indent=2))
                for row in rows:
                    value = json.loads(row["result"])
                    label = "Saved DeepSeek reply"
                    if isinstance(value,str):
                        try:
                            data = json.loads(value[value.index("{"):value.rindex("}")+1])
                        except ValueError:
                            data = {}
                        if isinstance(data,dict):
                            if "moments" in data:
                                label = "Transcript selection reply"
                            elif "payoff" in data or data.get("decision")=="reject":
                                label = "Storyboard reply"
                    formatted.append(label+"\n"+(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)))
                return self.respond(200,"\n\n".join(formatted),"text/plain; charset=utf-8")
        elif len(index)<=7:
            number = int(index)
            with store.connect(root) as db:
                run = db.execute("SELECT asset FROM jobs WHERE id=?",(job,)).fetchone()
            if not run:
                return self.respond(404,"Job evidence unavailable.")
            audio,transcript = operator_workspace.artifacts(root,job,run["asset"])
            if kind.startswith("audio/") and number<len(audio):
                file = operator_workspace.range_artifact(root,job,audio[number]["path"])
                if file and file.suffix==".flac":
                    return self.send_file(file,"audio/flac")
            if kind.startswith("transcript/") and transcript:
                words = streamer_asr.read(transcript)["words"]
                lines = [f"{word[0]:>5}  {operator_workspace.clock(word[1]//1)}  {word[1]:.3f}–{word[2]:.3f}  {word[3]}"
                         for word in words if word[1]<number+3600 and word[2]>number]
                if lines:
                    return self.respond(200,"\n".join(lines)+"\n","text/plain; charset=utf-8")
        return self.respond(404,"Job evidence unavailable.")

    def streamer_poster(self, job, candidate, edition, at):
        root = self.server.root.resolve()
        folder = streamer_run.directory(root,job)
        if candidate:
            target = streamer_edits.directory(root,job,candidate,edition) if edition else folder/candidate
            movie = target/"portrait.mp4"
            if not movie.resolve().is_relative_to(folder):
                raise ValueError("Invalid portrait.")
            poster = target/"workspace-poster.jpg"
            stamp = .14
            flags = "scale=288:-2"
        else:
            flags = "scale=640:-2"
            status_path = folder/"audio-first.json"
            ranges = streamer_asr.read(status_path).get("ranges",[]) if status_path.is_file() else []
            window = next((item for item in ranges
                           if operator_workspace.range_artifact(root,job,item["path"])),None)
            if window:
                movie = operator_workspace.range_artifact(root,job,window["path"])
                stamp = min(3,(window["end"]-window["start"])*.2)
                poster = folder/"workspace-source-preview.jpg"
            else:
                with store.connect(root) as db:
                    asset = db.execute("""SELECT a.path,a.duration FROM assets a JOIN jobs j ON j.asset=a.hash
                                          WHERE j.id=?""",(job,)).fetchone()
                if not asset:
                    raise ValueError("Archive unavailable.")
                movie = root/asset["path"]
                if not movie.resolve().is_relative_to(root/"masters"):
                    raise ValueError("Invalid archive.")
                stamp = 12998 if at=="12998" and asset["duration"]>13000 else asset["duration"]*.26
                poster = folder/f"workspace-source-{round(stamp)}.jpg"
        if poster.is_file():
            return poster.read_bytes()
        frame = media.run([media.require("ffmpeg"),"-v","error","-threads","2","-ss",f"{stamp:.3f}",
                           "-i",str(movie),"-frames:v","1","-vf",flags,"-c:v","mjpeg","-threads","1",
                           "-f","image2pipe","-"],22)
        if frame.returncode or not frame.stdout:
            raise ValueError("Poster unavailable.")
        poster.parent.mkdir(parents=True,exist_ok=True)
        pending = poster.with_name(f"{poster.stem}-{secrets.token_hex(4)}.partial.jpg")
        pending.write_bytes(frame.stdout)
        pending.replace(poster)
        return frame.stdout

    def action(self, db, path, fields):
        if match := re.fullmatch(r"/(sources|jobs)/source/(\d+)/replay/(pause|resume|extend)",path):
            from . import replay_cache
            section, source, operation = match.groups()
            source = int(source)
            row = db.execute("SELECT revision,identity,state FROM sources WHERE id=?",(source,)).fetchone()
            if not row or not row["identity"].startswith(("youtube:","file:")) or row["state"]=="archived":
                raise ValueError("Replay source is unavailable.")
            if operation=="extend":
                hours = int(fields.get("hours","0"))
                replay_cache.extend(self.server.root,source,row["revision"],hours)
                message = f"Added {hours} hours to the recording's deletion deadline."
            else:
                paused = operation=="pause"
                replay_cache.set_paused(self.server.root,db,source,paused)
                message = ("Stream paused. Any running hour will stop at its next saved checkpoint."
                           if paused else "Stream resumed.")
            destination = f"/jobs#job-source-{source}" if section=="jobs" else f"/sources#source-{source}"
            return destination,{},message
        if path=="/storage/scheduler":
            portal_settings.save_scheduler(db,fields)
            return "/storage#scheduler",{},"Scheduler saved."
        if path=="/storage/settings":
            portal_settings.save_cleanup(db,fields)
            return "/storage#cleanup",{},"Cleanup settings saved."
        if path=="/sources/link/add" or (match := re.fullmatch(r"/sources/link/(\d+)/edit",path)):
            sid = None if path.endswith("/add") else int(match[1])
            source,message = operator_workspace.save(db,fields,sid)
            return f"/sources#source-{source}",{},message
        if match := re.fullmatch(r"/sources/link/(\d+)/delete",path):
            source = int(match[1])
            if db.execute("SELECT 1 FROM jobs WHERE source=? AND state IN ('queued','running')",(source,)).fetchone():
                raise ValueError("Stop the active job before deleting its source.")
            store.set_source_state(db,source,"archived")
            return "/sources",{},"Link removed from Sources. Saved jobs remain in History."
        if match := re.fullmatch(r"/jobs/(\d+)/(pause|resume)",path):
            job,action = int(match[1]),match[2]
            row = db.execute("SELECT source,state FROM jobs WHERE id=?",(job,)).fetchone()
            if not row:
                raise ValueError("Job not found.")
            if action=="resume" and row["state"] in ("stopped","failed"):
                store.resume(db,job)
                message = "Saved job queued."
            elif action=="pause" and row["state"]=="queued":
                db.execute("UPDATE jobs SET state='stopped',progress='Stopped by operator.' WHERE id=?",(job,))
                factory_events.record(db,job,"stopped",item="Stopped from Jobs")
                message = "Queued job stopped."
            elif action=="pause" and row["state"]=="running":
                db.execute("UPDATE jobs SET stop=1 WHERE id=?",(job,))
                message = "Stop requested at the next saved checkpoint."
            else:
                raise ValueError("This job has already changed state. Refresh Jobs.")
            source = row["source"]
            part = int(fields.get("part",0))
            return f"/jobs#job-part-{source}-{part}",{"source":source,"part":part},message
        if match := re.fullmatch(r"/factory/run/(\d+)/select-from-transcript",path):
            from . import streamer_intake
            job = int(match[1])
            streamer_intake.queue_operator_selection(self.server.root,db,job,fields)
            source = db.execute("SELECT source FROM jobs WHERE id=?",(job,)).fetchone()[0]
            return f"/factory#stream-{source}",{"attempt":job},"Saved audio edit queued for visual verification."
        if match := re.fullmatch(r"/factory/run/(\d+)/refine-audio-selection",path):
            from . import streamer_intake
            job = int(match[1])
            streamer_intake.retry_selection(self.server.root,db,job)
            source = db.execute("SELECT source FROM jobs WHERE id=?",(job,)).fetchone()[0]
            return f"/factory#stream-{source}",{"attempt":job},"Audio selection refinement queued."
        if match := re.fullmatch(r"/factory/done/(\d+)/hold",path):
            story = int(match[1])
            factory_done.set_hold(db,story,fields["edition"],fields.get("reason",""))
            return f"/factory#story-{story}",{"work":"done"},"Editorial hold saved."
        if match := re.fullmatch(r"/factory/done/(\d+)/manual",path):
            story = int(match[1])
            factory_done.record(db,story,fields["edition"],fields["platform"],fields["account"],
                                "manual",fields["remote"],fields["occurred"],"Local operator record")
            return f"/factory#story-{story}",{"work":"done"},"External post recorded manually."
        if match := re.fullmatch(r"/controls/(production|resources|uploads|connections)/save",path):
            portal_settings.save(db,match[1],fields)
            return f"/controls#{match[1]}",{}, "Controls saved."
        if path=="/controls/flashdata/save":
            from . import flashdata_profiles
            flashdata_profiles.save(db,fields)
            return "/controls#flashdata",{},"FlashData settings saved."
        if path=="/templates/create" or (match := re.fullmatch(r"/templates/([a-z0-9-]+)/(save|duplicate|activate|archive)",path)):
            action,template = ("create",None) if path=="/templates/create" else (match[2],match[1])
            template,revision = portal_templates.save(db,action,fields,template)
            message = {"create":"Draft created.","save":"Revision saved.","duplicate":"Draft duplicated.",
                       "activate":"Revision active for future admissions.","archive":"Design archived for future selection."}[action]
            return "/templates",{"design":template,"revision":revision},message
        if match := re.fullmatch(r"/sources/creator/(\d+)/(details|policy)",path):
            creator = int(match[1])
            streamer_catalog.roster(db,creator)
            if match[2]=="policy":
                portal_settings.save_policy(db,creator,fields)
            else:
                profile = [fields[name].strip() for name in ("name","game","credit")]
                if not all(1<=len(value)<=100 for value in profile):
                    raise ValueError("Enter a display name, game filter and credit of up to 100 characters.")
                db.execute("UPDATE streamer_creators SET name=?,game=?,credit=?,enabled=? WHERE id=?",
                           (*profile,fields.get("enabled")=="on",creator))
            return f"/sources#creator-{creator}",{"creator":creator}, "Creator preferences saved."
        if match := re.fullmatch(r"/sources/queue/(\d+)/update",path):
            source = int(match[1])
            if not db.execute("SELECT 1 FROM source_queue WHERE source=?",(source,)).fetchone():
                raise ValueError("Source queue entry not found.")
            if "priority" in fields:
                priority = int(fields["priority"])
                if not 1<=priority<=9999:
                    raise ValueError("Priority must be between 1 and 9999.")
                db.execute("UPDATE sources SET priority=? WHERE id=?",(priority,source))
            elif fields.get("state") in ("queued","paused","archived"):
                store.set_source_state(db,source,fields["state"])
            else:
                raise ValueError("Choose a valid queue action.")
            return f"/sources#queue-source-{source}",{"queue":source},"Source queue updated."
        if match := re.fullmatch(r"/sources/video/(\d+)/(start|evidence)",path):
            source = int(match[1])
            if match[2]=="start":
                return self.start_stream(db,source,fields)
            row = db.execute("""SELECT s.*,a.creator FROM sources s JOIN streamer_streams st ON st.source=s.id
                                JOIN streamer_accounts a ON a.id=st.account WHERE s.id=?""",(source,)).fetchone()
            if not row:
                raise ValueError("Recording not found.")
            for key in ("location","chat_path"):
                if fields[key] and (identify(fields[key])[2]!="file" or not Path(fields[key]).is_file()):
                    raise ValueError("Choose an existing file on the factory machine.")
            original = dict(db.execute("SELECT * FROM sources WHERE id=?",(source,)).fetchone())
            store.save_source(db,dict(original,location=fields["location"]),source)
            db.execute("UPDATE streamer_streams SET chat_path=? WHERE source=?",(fields["chat_path"] or None,source))
            db.execute("""UPDATE source_queue SET readiness='waiting',next_check=0
                          WHERE source=? AND platform='youtube' AND (readiness='waiting' OR ? IS NOT NULL)""",
                       (source,fields["location"] or original["location"] or None))
            return f"/sources#roster-stream-{source}",{"creator":row["creator"]},"Source evidence saved."
        if match := re.fullmatch(r"/factory/run/(\d+)/workspace-(pause|resume)",path):
            job,action = match.groups()
            row = db.execute("SELECT source,state FROM jobs WHERE id=?",(job,)).fetchone()
            if not row:
                raise ValueError("Attempt not found.")
            if action=="resume":
                if row["state"] not in ("failed","stopped"):
                    raise ValueError("This attempt is not paused or failed.")
                store.resume(db,int(job))
                message = "Saved attempt queued."
            else:
                if row["state"]!="running":
                    raise ValueError("This attempt is no longer running.")
                db.execute("UPDATE jobs SET stop=1 WHERE id=?",(job,))
                message = "Pause requested at the next saved checkpoint."
            return f'/factory#stream-{row["source"]}',{"attempt":job},message
        if match := re.fullmatch(r"/factory/run/(\d+)/continue-analysis",path):
            job = int(match[1])
            streamer_run.queue_analysis_resume(self.server.root,db,job)
            source = db.execute("SELECT source FROM jobs WHERE id=?",(job,)).fetchone()[0]
            return f"/factory#stream-{source}",{"attempt":job},"Continuation queued within the existing cap."
        if match := re.fullmatch(r"/factory/run/(\d+)/reel/(c\d+-\d+)/(original|[0-9a-f]{64})/(feedback|qa)",path):
            job,candidate,edition,action = match.groups()
            run = db.execute("SELECT source FROM jobs WHERE id=?",(job,)).fetchone()
            if not run:
                raise ValueError("Attempt not found.")
            if edition=="original" and action=="feedback":
                streamer_run.save_review(self.server.root,int(job),candidate,dict(fields,decision=fields.get("verdict")))
            else:
                row = db.execute("SELECT * FROM streamer_editions WHERE id=? AND job=? AND candidate=?",
                                 (edition,job,candidate)).fetchone()
                if not row:
                    raise ValueError("Edition not found.")
                if action=="feedback":
                    streamer_edits.feedback(db,row,fields)
                else:
                    streamer_edits.review(db,self.server.root,edition,fields.get("reason",""))
            return f"/factory#reel-{job}-{candidate}",{"attempt":job}, "Reel decision saved."
        if path == "/factory/settings":
            zone = fields.get("zone","")
            factory_portal.tz(zone)
            db.execute("UPDATE factory_controls SET paused=?,zone=?",(fields.get("paused")=="on",zone))
            return "/controls#resources",{},"Monitor settings saved."
        if match := re.fullmatch(r"/factory/run/(\d+)/(resume|budget|revisit)",path):
            job = int(match[1])
            if match[2]=="resume":
                store.resume(db,job)
                return f"/factory/run/{job}",{},"Retry queued."
            if match[2]=="revisit":
                folder = streamer_run.directory(self.server.root,job)
                row = db.execute("SELECT state FROM jobs WHERE id=?",(job,)).fetchone()
                if (not row or row["state"]!="complete" or not (folder/"production.json").is_file()
                        or (folder/"production-revisit.json").is_file()):
                    raise ValueError("This job has no pending underperforming-hour review.")
                first = streamer_asr.read(folder/"production.json")
                if not any(hour["complete"] and hour["gap"] for hour in first["hours"]):
                    raise ValueError("Every full hour reached the first-pass target.")
                db.execute("UPDATE jobs SET state='queued',progress='Waiting for bounded follow-up' WHERE id=?",(job,))
                factory_events.record(db,job,"queued",item="Requested the bounded follow-up on underperforming hours")
                return f"/factory/run/{job}",{},"Follow-up queued within the existing cap."
            row = db.execute("SELECT settings,state,error FROM jobs WHERE id=?",(job,)).fetchone()
            if not row or row["state"]!="failed" or "budget" not in row["error"].lower():
                raise ValueError("A cap increase is available when a run is blocked by its budget.")
            config = json.loads(row["settings"])
            old, cap = config["provider_cap_cents"],int(fields["cap"])
            if not old<cap<=1000:
                raise ValueError("Enter a higher cap up to 1000 cents.")
            config["provider_cap_cents"] = cap
            db.execute("UPDATE jobs SET settings=? WHERE id=?",(json.dumps(config),job))
            factory_events.record(db,job,"cap revised",item=f"Operator changed the cap from {old} to {cap} cents")
            return f"/factory/run/{job}",{},"New cap saved. Resume when ready."
        if path == "/source/add":
            source, created = store.save_source(db, fields)
            return "/sources#source-form", {"source": source}, "Source added." if created else "This recording is already in Sources."
        if path == "/source/bulk":
            links = [line.strip() for line in fields["links"].splitlines() if line.strip()]
            if not 1 <= len(links) <= 100:
                raise ValueError("Enter 1–100 video links or paths.")
            added = sum(store.save_source(db, {"link": link})[1] for link in links)
            return "/sources#unassigned", {}, f"Added {added} sources. Already in Sources: {len(links) - added}."
        if path == "/source/import":
            count = store.import_table(db, self.server.table)
            return "/sources#unassigned", {}, f"Imported {count} new sources."
        if match := re.fullmatch(r"/source/(\d+)/(edit|pause|resume|archive|start)", path):
            source, operation = int(match[1]), match[2]
            old = db.execute("SELECT state FROM sources WHERE id=?", (source,)).fetchone()
            if not old:
                raise ValueError("Source not found.")
            if operation == "start":
                return self.start_stream(db,source,fields)
            if operation == "edit":
                source, saved = store.save_source(db, dict(fields, state=old["state"]), source)
                return "/sources#source-form", {"source": source}, "Source updated." if saved else "That recording already exists."
            state = {"pause": "paused", "resume": "queued", "archive": "archived"}[operation]
            store.set_source_state(db, source, state)
            return "/sources#source-form", {"source":source}, f"Source {state}."
        if match := re.fullmatch(r"/job/(\d+)/(stop|resume|restart)", path):
            job = int(match[1])
            if match[2] == "resume":
                store.resume(db, job)
            elif match[2] == "restart":
                row = db.execute("SELECT source FROM jobs WHERE id=?", (job,)).fetchone()
                if not row:
                    raise ValueError("Job not found.")
                return self.start_stream(db,row["source"],dict(fields,rerun_from=str(job)))
            else:
                db.execute("UPDATE jobs SET state='stopped',progress='Stopped before claim.' "
                           "WHERE id=? AND state='queued'", (job,))
                db.execute("UPDATE jobs SET stop=1 WHERE id=? AND state='running'", (job,))
            row = db.execute("SELECT source FROM jobs WHERE id=?",(job,)).fetchone()
            return f'/factory#stream-{row["source"]}' if row else "/factory", {"attempt":job}, "Job updated."
        if match := re.fullmatch(r"/review/(\d+)/save", path):
            report.review(db, int(match[1]), fields)
            return "/factory#legacy-work", {"attempt": int(fields["job"])}, "Review saved."
        if path == "/settings/save":
            settings.save(db, fields)
            return "/controls#connections", {}, "Advanced settings saved for new jobs."
        if match := re.fullmatch(r"/provider/(google|sail)/(add|test|replace|remove|cancel)", path):
            message = self.server.worker.connections.action(db, match[1], match[2], fields)
            return "/controls#connections", {}, message
        if path == "/compare/create":
            pairs = blind_review.create(db, int(fields["first"]), int(fields["second"]))
            return "/factory#legacy-work", {"pair": pairs[0]} if pairs else {}, (
                "Comparison ready." if pairs else "Neither job emitted a clip.")
        if match := re.fullmatch(r"/compare/context/(\d+)", path):
            source = int(match[1])
            return self.start_stream(db,source,fields)
        if match := re.fullmatch(r"/compare/([0-9a-f]{32})", path):
            following = blind_review.assess(db, match[1], fields)
            return "/factory#legacy-work", {"pair": following}, "Assessment saved."
        raise ValueError("Action not found.")

    def start_stream(self,db,source,fields):
        job,created = portal_settings.start(db,source,fields)
        return f"/factory#stream-{source}",{"stream":source,"attempt":job}, "Clipping queued." if created else "Existing attempt opened."

    def start_link(self, source, fields):
        from . import replay_cache
        root = self.server.root
        part = int(fields.get("start","0"))
        if not 0<=part<=172799 or part%3600:
            raise ValueError("Choose a valid hourly part.")
        with store.connect(root) as db:
            row = db.execute("""SELECT s.link,s.location,s.state,s.revision,q.readiness,
                                 EXISTS(SELECT 1 FROM streamer_streams WHERE source=s.id) AS attached
                                 FROM sources s LEFT JOIN source_queue q ON q.source=s.id WHERE s.id=?""",(source,)).fetchone()
            config,_ = portal_settings.current(db)
        if not row or row["state"]!="queued":
            raise ValueError("This source is unavailable.")
        youtube = identify(row["link"])[2]=="youtube"
        full = youtube and replay_cache.enabled()
        remote = youtube and (not row["location"] or full)
        if remote and not full and part>=streamer_transfer.REPLAY_LIMIT:
            raise ValueError(streamer_transfer.REPLAY_LIMIT_MESSAGE)
        if not config["connections"]["hosted"] and fields.get("hosted")!="on":
            raise ValueError("Allow Sail analysis for this start in the source form or Controls & settings.")
        resolved = None
        if not row["attached"]:
            if identify(row["link"])[2]!="youtube":
                raise ValueError("This source needs a supported replay adapter before it can start.")
            resolved = source_queue.resolve(row["link"],True,config["resources"]["remote_height"],root=root)
            info = resolved[2]
            if not info or not info.get("duration") or part>=info["duration"]:
                if resolved[3]!="waiting":
                    raise ValueError("This hourly part is outside the replay duration.")
            name = (info.get("channel") or info.get("uploader") or info["channel_id"]).strip()[:100]
        with store.connect(root,True) as db:
            current = db.execute("SELECT link,state,revision FROM sources WHERE id=?",(source,)).fetchone()
            if not current or tuple(current)!=(row["link"],"queued",row["revision"]):
                raise ValueError("The source changed while resolving. Open it again.")
            if resolved:
                source_queue.attach(db,{"name":name,"game":"Gaming","archive_upload":"on"},resolved)
                if resolved[3]!="ready":
                    return f"/sources#source-{source}",{},"Waiting for a seekable replay. Start it when ready."
            directory_row = next(r for r in operator_workspace.listing(db) if r["id"]==source)
            if full and not any(j["revision"]==row["revision"] and json.loads(j["settings"]).get("full_replay")
                                for j in operator_workspace.attempts(db,source)):
                part = 0
            minutes = min(60,portal_settings.current(db)[0]["resources"]["remote_minutes"])
            if remote and directory_row["duration"] and part>=directory_row["duration"]:
                raise ValueError("This hourly part is outside the replay duration.")
            if remote:
                for saved in operator_workspace.attempts(db,source):
                    if saved["revision"]!=row["revision"]:
                        continue
                    left,right = operator_workspace.span(root,saved,directory_row["duration"])
                    if saved["state"] in operator_workspace.ACTIVE or (left<part+minutes*60 and right>part):
                        part = int(left)//3600*3600
                        return f"/jobs#job-part-{source}-{part}",{"source":source,"part":part},"Existing job opened."
                fields = dict(fields,coverage_start=str(part),coverage_minutes=str(minutes),directory_part=part)
            job,created = portal_settings.start(db,source,fields)
        return f"/jobs#job-part-{source}-{part}",{"source":source,"part":part},(
            f"Run {job} queued." if created else f"Existing run {job} opened.")

    def serve_media(self, story, frame, variant=None):
        with store.connect(self.server.root) as db:
            row = db.execute("SELECT preview FROM stories WHERE id=?", (story,)).fetchone()
        if not row or not row["preview"]:
            return self.respond(404, "Preview not found.")
        root = self.server.root.resolve()
        path = root / row["preview"]
        if frame is not None:
            path = path.with_name(f"evidence-{frame}.jpg")
        if variant:
            path = path.with_name({"context": "source-context.mp4", "alternate": "alternate-framing.mp4"}[variant])
        path = path.resolve()
        if not path.is_relative_to(root / "previews") or not path.is_file():
            return self.respond(404, "Media file not found.")
        return self.send_file(path, "image/jpeg" if frame is not None else "video/mp4")

    def send_file(self, path, kind):
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        headers = [("Accept-Ranges", "bytes")]
        code = 200
        if requested:
            match = re.fullmatch(r"bytes=(\d{0,16})-(\d{0,16})", requested)
            if match and (match[1] or match[2]):
                if match[1]:
                    start, end = int(match[1]), min(end, int(match[2])) if match[2] else end
                else:
                    start = max(0, size - int(match[2]))
            else:
                start = size
            if start > end:
                return self.respond(416, extra=[("Content-Range", f"bytes */{size}")])
            code = 206
            headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        count = end - start + 1
        self.send_body_headers(code, kind, count, headers)
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                if code == 200:
                    shutil.copyfileobj(stream, self.wfile, 256 * 1024)
                else:
                    while count:
                        chunk = stream.read(min(256 * 1024, count))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        count -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Browser may switch ranges when the user seeks.
