"""SQLite checkpoints for external calls and estimated budget reservations."""

import hashlib
import json
import math
import time

from . import provider_api as api
from .store import connect


class ProviderPending(Exception):
    pass


class ProviderExhausted(ValueError):
    pass


# Per-submit upper estimates used for local admission, in millionths of a dollar.
DISCOVERY = 4500
RANK = 4500
FRAME_CHECK = 5000
DOWNSTREAM = RANK + FRAME_CHECK


class Meter:
    def __init__(self, root, job, cap_cents):
        self.root, self.job = root, job
        self.cap = cap_cents * 10_000

    def identity(self, provider, operation):
        return hashlib.sha256(f"{self.job}:{provider}:{operation}".encode()).hexdigest()

    def completed_text(self, provider, operation):
        with connect(self.root) as db:
            row = db.execute("SELECT result FROM provider_calls WHERE id=?",
                             (self.identity(provider, operation),)).fetchone()
        result = json.loads(row["result"]) if row and row["result"] is not None else None
        return result if isinstance(result, str) else None

    def available(self, db):
        reserved = db.execute("""SELECT COALESCE(SUM(reservation_micros * attempts),0)
                               FROM provider_calls WHERE job=?""",(self.job,)).fetchone()[0]
        return self.cap-reserved

    def call(self, provider, operation, reference, reservation, submit, poll=None, *, downstream=0):
        identity = self.identity(provider, operation)
        with connect(self.root, True) as db:
            db.execute("""INSERT OR IGNORE INTO provider_calls(id,job,provider,reference,reservation_micros)
                       VALUES(?,?,?,?,?)""", (identity, self.job, provider, reference, reservation))
            row = db.execute("SELECT * FROM provider_calls WHERE id=?", (identity,)).fetchone()
            if row["result"] is not None:
                saved = json.loads(row["result"])
                if provider == "sail" and saved == {"status": "output_limit"}:
                    raise api.OutputLimit()
                return saved
            if row["reference"] != reference:
                raise ValueError("Provider key changed during a pending request. Start a new run with current settings.")
            if row["next_try"] > time.time():
                raise ProviderPending("Waiting for the provider retry or Flex poll window.")
            if not row["response_id"]:
                remaining = self.available(db)
                if reservation+downstream > remaining:
                    if downstream:
                        raise ProviderExhausted("Discovery reached its budget limit. Remaining headroom is held for ranking and frame checks.")
                    raise ProviderExhausted("This job reached its estimated provider budget. Start a new run to change the cap.")
                if row["attempts"] >= 3:
                    raise ProviderExhausted("This request reached its submission retry limit.")
                db.execute("UPDATE provider_calls SET attempts=attempts+1 WHERE id=?", (identity,))
            elif not poll:
                raise ValueError("Background response has no supported poll operation.")
        try:
            result = poll(row["response_id"]) if row["response_id"] else submit(identity)
        except api.OutputLimit as error:
            self.save(identity, provider, error.payload or {}, incomplete=True)
            raise
        except api.ProviderError as error:
            if error.retry:
                self.defer(identity, time.time()+error.retry)
                raise ProviderPending("Provider unavailable. Waiting for Retry-After.") from None
            raise
        if poll and result["status"] != "completed":
            self.defer(identity, time.time()+8, result["id"])
            raise ProviderPending("Sail Flex is processing. The response ID is saved.")
        return self.save(identity, provider, result)

    def save(self, identity, provider, result, *, incomplete=False):
        usage = result.get("usage") if isinstance(result, dict) else None
        counts = None
        if isinstance(usage, dict):
            values = [usage.get(key) for key in ("input_tokens", "output_tokens")]
            if all(type(value) is int and value >= 0 for value in values):
                counts = dict(zip(("input_tokens", "output_tokens"), values))
        cost = None
        if counts and provider == "sail":
            rates = {"balanced": (.07, .005, .22), "flex": (.05, .004, .15)}
            input_rate, cached_rate, output_rate = rates.get(result.get("service_tier"), (.09, .006, .30))
            details = usage.get("input_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else 0
            cached = cached if type(cached) is int and 0 <= cached <= counts["input_tokens"] else 0
            counts["cached_input_tokens"] = cached
            cost = math.ceil((counts["input_tokens"]-cached)*input_rate + cached*cached_rate +
                             counts["output_tokens"]*output_rate)
        # Keep only the provider text required by editorial plus usage metadata. Reasoning is discarded.
        clean = {"status": "output_limit"} if incomplete else (
            api.response_text(result) if provider == "sail" else result)
        with connect(self.root, True) as db:
            db.execute("""UPDATE provider_calls SET result=?,usage=?,estimate_micros=?,next_try=0,
                       response_id=COALESCE(response_id,?) WHERE id=?""",
                       (json.dumps(clean), json.dumps(counts) if counts is not None else None, cost,
                        result.get("id") if isinstance(result, dict) else None, identity))
        return clean

    def defer(self, identity, until, response_id=None):
        with connect(self.root, True) as db:
            db.execute("UPDATE provider_calls SET next_try=?,response_id=COALESCE(?,response_id) WHERE id=?",
                       (until, response_id, identity))


def report(db, job):
    rows = list(db.execute("""SELECT provider,attempts,reservation_micros,usage,estimate_micros
                              FROM provider_calls WHERE job=? AND attempts>0 ORDER BY provider""", (job,)))
    counts = [row["estimate_micros"] for row in rows if row["estimate_micros"] is not None]
    return {
        "api_spend_usd": None if rows else 0,
        "provider_usage": {
            "submission_attempts": sum(row["attempts"] for row in rows),
            "reserved_usd": sum(row["reservation_micros"] * row["attempts"] for row in rows)/1_000_000,
            "known_estimate_usd": sum(counts)/1_000_000 if counts else None,
            "unpriced_calls": len(rows)-len(counts),
            "estimate_basis": "Sail published token rates for the returned tier, or default rates when absent. Provider billing and Google cost are unknown." if rows else "",
            "metered_calls": [dict(row, usage=json.loads(row["usage"]) if row["usage"] else None) for row in rows],
        },
    }
