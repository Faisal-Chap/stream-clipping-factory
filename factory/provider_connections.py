"""Provider key lifecycle and restart-safe capability probes."""

import re
import threading
import time
from uuid import uuid4

from . import provider_api as api
from .media import require, run, MediaError
from .provider_vault import vault_for, VaultError
from .store import connect

NAMES = {"google": "Google AI Studio", "sail": "Sail Research"}


class ConnectionRequired(ValueError):
    """A job needs a usable key or a verified provider capability."""


class Connections:
    def __init__(self, root, vault=None, sender=None):
        self.root = root
        self.vault = vault or vault_for(root)
        self.sender = sender
        self.lock = threading.Lock()

    def action(self, db, provider, operation, fields):
        config = db.execute("SELECT * FROM provider_connections WHERE provider=?", (provider,)).fetchone()
        if not config or int(fields.get("revision", -1)) != config["revision"]:
            raise ValueError("Connection changed in another tab. Reload and try again.")
        pending = db.execute("SELECT * FROM provider_probes WHERE provider=? AND state='queued'",
                             (provider,)).fetchone()
        if operation == "cancel":
            if not pending:
                raise ValueError("There is no queued connection test.")
            db.execute("UPDATE provider_probes SET state='cancelled' WHERE id=?", (pending["id"],))
            db.execute("UPDATE provider_connections SET revision=revision+1 WHERE provider=?", (provider,))
            if pending["replacement"]:
                self.remove_inactive(pending["reference"])
            return "Local connection test cancelled."
        if pending:
            raise ValueError("A connection test is already queued. Wait for its result.")
        reference = config["reference"]
        if not self.vault.available:
            raise VaultError("The factory credential store is unavailable.")
        if operation == "remove":
            if reference:
                self.vault.remove(reference)
            db.execute("""UPDATE provider_connections SET revision=revision+1,reference=NULL,verified=NULL,
                       capability='',verified_tier=NULL,error='' WHERE provider=?""", (provider,))
            return "Local key removed."
        secret = fields.get("key", "").strip()
        if operation == "add" and reference:
            raise ValueError("Use Replace to update an existing key.")
        if operation != "add" and not reference:
            raise ValueError("Add a key before testing or replacing it.")
        if operation in ("add", "replace") and not re.fullmatch(r"[!-~]{8,512}", secret):
            raise ValueError("Enter the full provider key without spaces.")
        if operation == "add":
            new_ref = uuid4().hex
            self.vault.put(new_ref, secret)
            db.execute("""UPDATE provider_connections SET revision=revision+1,reference=?,verified=NULL,
                       capability='',verified_tier=NULL,error='' WHERE provider=?""", (new_ref, provider))
            return "Key saved. Run Test to verify the connection."
        tier = api.validate_tier(fields.get("tier")) if provider == "sail" else None
        test_reference = reference
        if operation == "replace":
            test_reference = uuid4().hex
            self.vault.put(test_reference, secret)
        db.execute("""INSERT INTO provider_probes(id,provider,reference,tier,replacement,stage)
                   VALUES(?,?,?,?,?,?)""", (uuid4().hex, provider, test_reference, tier, operation == "replace",
                                           "text" if provider == "google" else "images"))
        db.execute("UPDATE provider_connections SET revision=revision+1,error='' WHERE provider=?", (provider,))
        return "Connection test queued. Refresh Settings for its result."

    def key(self, provider, *, tier="", images=False):
        with connect(self.root) as db:
            row = db.execute("SELECT * FROM provider_connections WHERE provider=?", (provider,)).fetchone()
            verified = db.execute("""SELECT capability FROM provider_capabilities WHERE provider=?
                                  AND reference=? AND tier=?""", (provider, row["reference"], tier)).fetchone()
        if not row["reference"]:
            raise ConnectionRequired(f"{NAMES[provider]} is awaiting a key. Open Settings, then resume this job.")
        flag = "ordered images" if images else ("text and video" if provider == "google" else "text")
        if not verified or (images and verified["capability"] != "text and ordered images"):
            raise ConnectionRequired(f"{NAMES[provider]} requires a successful {flag} test for this method.")
        secret = self.vault.get(row["reference"])
        if not secret:
            raise ConnectionRequired(f"{NAMES[provider]} key is unavailable in the local vault.")
        return row["reference"], secret

    def tick(self):
        with connect(self.root) as db:
            row = db.execute("""SELECT * FROM provider_probes WHERE state='queued' AND next_try<=?
                             ORDER BY rowid LIMIT 1""", (time.time(),)).fetchone()
        if not row:
            return
        try:
            key = self.vault.get(row["reference"])
            if not key:
                raise VaultError("Key is missing from the local vault.")
            if row["provider"] == "google":
                self.google(row, key)
            else:
                self.sail(row, key)
        except (api.ProviderError, VaultError, MediaError) as error:
            if getattr(error, "retry", None) and row["attempts"] < 4:
                self.update(row, next_try=time.time() + error.retry, attempts=row["attempts"]+1)
            else:
                self.finish(row, error=str(error))
        except Exception:
            self.finish(row, error="Provider capability test could not process its response.")

    def update(self, row, **fields):
        with connect(self.root, True) as db:
            db.execute("UPDATE provider_probes SET " + ",".join(f"{field}=?" for field in fields)
                       + " WHERE id=?", (*fields.values(), row["id"]))

    def google(self, row, key):
        if row["stage"] == "text":
            api.embed(key, text="A solid red frame.", send=self.sender)
            self.update(row, stage="video", attempts=0, next_try=0)
            return
        folder = self.root / "provider-probe-fixtures"
        folder.mkdir(exist_ok=True)
        asset = folder / "solid-red-two-seconds.mp4"
        if not asset.is_file():
            result = run([require("ffmpeg"), "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                          "color=red:s=128x96:r=12:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(asset)], 30)
            if result.returncode:
                raise MediaError("Could not generate the Google test video. Check FFmpeg.")
        api.embed(key, video=asset.read_bytes(), send=self.sender)
        self.finish(row, capability=f"text and video · {api.DIMENSION} dimensions")

    def sail(self, row, key):
        images = row["stage"] == "images"
        prompt = [{"type": "input_text", "text": "Return the dominant color of each image in order, as two English words."}]
        if images:
            prompt += [api.image_part("image/png", api.probe_image(rgb)) for rgb in ((255, 0, 0), (0, 0, 255))]
        content = [{"role": "user", "content": prompt}] if images else "Reply with the single word READY."
        try:
            if row["response_id"]:
                result = api.sail_poll(key, row["tier"], row["response_id"], self.sender)
            else:
                result = api.sail_submit(key, row["tier"], f'{row["id"]}-{row["stage"]}', content,
                                         max_output=256, send=self.sender)
        except api.ProviderError as error:
            if images and error.status in (400, 415, 422):
                self.update(row, stage="text", response_id=None, attempts=0, next_try=0)
                return
            raise
        if result["status"] != "completed":
            self.update(row, response_id=result["id"], next_try=time.time()+8, attempts=0)
            return
        words = re.findall(r"[A-Z]+", api.response_text(result).upper())
        if images and words != ["RED", "BLUE"]:
            self.update(row, stage="text", response_id=None, attempts=0, next_try=0)
        elif not images and words != ["READY"]:
            raise api.ProviderError("Sail returned no verifiable response to the text fixture.")
        else:
            self.finish(row, capability="text and ordered images" if images else "text · ordered images unverified")

    def finish(self, row, capability="", error=""):
        with self.lock, connect(self.root, True) as db:
            state = db.execute("SELECT state FROM provider_probes WHERE id=?", (row["id"],)).fetchone()
            if state["state"] != "queued":
                return
            if error:
                db.execute("UPDATE provider_connections SET error=?,revision=revision+1 WHERE provider=?",
                           (error, row["provider"]))
            else:
                old = db.execute("SELECT reference FROM provider_connections WHERE provider=?",
                                 (row["provider"],)).fetchone()["reference"]
                db.execute("""UPDATE provider_connections SET reference=?,verified=CURRENT_TIMESTAMP,capability=?,
                           verified_tier=?,error='',revision=revision+1 WHERE provider=?""",
                           (row["reference"], capability, row["tier"], row["provider"]))
                db.execute("""INSERT INTO provider_capabilities(provider,reference,tier,capability) VALUES(?,?,?,?)
                           ON CONFLICT(provider,reference,tier) DO UPDATE SET capability=excluded.capability,
                           verified=CURRENT_TIMESTAMP""", (row["provider"], row["reference"], row["tier"] or "", capability))
                if row["replacement"] and old:
                    self.remove_inactive(old)
            if error and row["replacement"]:
                self.remove_inactive(row["reference"])
            db.execute("UPDATE provider_probes SET state=? WHERE id=?", ("failed" if error else "complete", row["id"]))

    def remove_inactive(self, reference):
        try:
            self.vault.remove(reference)
        except VaultError:
            # Do not undo a valid active reference if the old credential cannot be deleted.
            pass
