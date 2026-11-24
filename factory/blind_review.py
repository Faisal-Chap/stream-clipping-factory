"""Persist randomized A/B assignments separately from editorial decisions."""

import secrets

REASONS = {
    "ordinary": "Ordinary event", "setup": "Missing setup", "payoff": "Missing payoff",
    "illegible": "Illegible detail", "boundary": "Cut boundary or dialogue", "duplicate": "Repeated payoff",
    "complete": "Clear and distinctive story", "other": "Other",
}


def create(db, first, second):
    jobs = [db.execute("SELECT id,asset,state,settings FROM jobs WHERE id=?", (job,)).fetchone()
            for job in (first, second)]
    if (first == second or any(not job or job["state"] != "complete" for job in jobs)
            or not jobs[0]["asset"] or jobs[0]["asset"] != jobs[1]["asset"]):
        raise ValueError("Choose two completed jobs on the same master.")
    existing = list(db.execute("""SELECT id FROM blind_pairs WHERE
              (job_a=? AND job_b=?) OR (job_a=? AND job_b=?) ORDER BY rowid""", (first, second, second, first)))
    if existing:
        return [row["id"] for row in existing]
    sides = []
    for job in jobs:
        sides.append({row["rank"]: row["story"] for row in db.execute(
            "SELECT rank,story FROM job_stories WHERE job=?", (job["id"],))})
    ranks = sorted(sides[0].keys() | sides[1].keys())
    secrets.SystemRandom().shuffle(ranks)
    pairs = []
    for rank in ranks:
        options = [(first, sides[0].get(rank)), (second, sides[1].get(rank))]
        secrets.SystemRandom().shuffle(options)
        (job_a, story_a), (job_b, story_b) = options
        pair = secrets.token_hex(16)
        db.execute("INSERT INTO blind_pairs(id,story_a,story_b,job_a,job_b,rank) VALUES(?,?,?,?,?,?)",
                   (pair, story_a, story_b, job_a, job_b, rank))
        pairs.append(pair)
    return pairs


def group(db, pair):
    item = db.execute("SELECT * FROM blind_pairs WHERE id=?", (pair,)).fetchone()
    if not item:
        raise ValueError("This blind pair is unavailable.")
    return list(db.execute("""SELECT * FROM blind_pairs WHERE
                 (job_a=? AND job_b=?) OR (job_a=? AND job_b=?) ORDER BY rowid""",
                           (item["job_a"], item["job_b"], item["job_b"], item["job_a"])))


def assess(db, pair, fields):
    comparisons = group(db, pair)
    row = next(row for row in comparisons if row["id"] == pair)
    verdict, reason = fields["verdict"], fields["reason"]
    if verdict not in ("a", "b", "tie", "weak") or reason not in REASONS:
        raise ValueError("Select a verdict and a reason.")
    values = [pair]
    for side in ("a", "b"):
        usable = fields.get(f"usable_{side}") == "on"
        opening = fields.get(f"opening_{side}", "").strip()[:500]
        payoff = fields.get(f"payoff_{side}", "").strip()[:500]
        phone = fields.get(f"phone_{side}") == "on"
        if usable and not (row[f"story_{side}"] and opening and payoff and phone):
            raise ValueError("A usable cut needs a visible payoff, opening notes and phone legibility.")
        values += [usable, opening, payoff, phone]
    db.execute("INSERT OR REPLACE INTO blind_assessments VALUES(?,?,?,?,?,?,?,?,?)", values)
    db.execute("""UPDATE blind_pairs SET verdict=?,reason=?,note=?,reviewed=CURRENT_TIMESTAMP WHERE id=?""",
               (verdict, reason, fields.get("note", "").strip()[:1500], pair))
    return next((other["id"] for other in comparisons if other["id"] != pair and not other["reviewed"]), pair)


def results(db, pair):
    pairs = group(db, pair)
    if any(not row["reviewed"] for row in pairs):
        raise ValueError("Finish every pair before revealing methods.")
    return [dict(row) | dict(db.execute("SELECT * FROM blind_assessments WHERE pair=?", (row["id"],)).fetchone())
            for row in pairs]
