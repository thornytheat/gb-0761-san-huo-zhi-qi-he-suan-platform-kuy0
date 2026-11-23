from datetime import datetime, timedelta
from decimal import Decimal

from app.modules.notice.versions import effective


def _at(stamp, amount):
    return (datetime.fromisoformat(stamp) + timedelta(minutes=amount)).isoformat(timespec="minutes")


def _minutes(start, end):
    interval = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return interval.days * 1440 + interval.seconds // 60


def _slices(start, end, terms):
    """Split [start, end) at every term version boundary inside the window."""
    points = {start, end}
    for term in terms:
        for boundary in (term["start"], term["end"]):
            if start < boundary < end:
                points.add(boundary)
    ordered = sorted(points)
    return list(zip(ordered, ordered[1:]))


def overtime_pricing(notice, overtime_start, overtime_min, terms_issues):
    """Price continuous overtime minutes against every term version that becomes effective.

    The notice term anchors allowance and tier1 capacity for the whole voyage, so the
    free allowance is never granted twice and the tier-1/tier-2 boundary is crossed at
    most once. Each overtime minute is charged at the rate of the term effective at that
    instant. Minutes without an effective term stay uncovered (RATE_PENDING) until the
    late renewal record is supplied and the voyage is recomputed.
    """
    base = notice["term"]
    start, end = overtime_start, _at(overtime_start, overtime_min)
    basis_pieces, uncovered = [], []
    cumulative = 0
    for left, right in _slices(start, end, notice["terms"]):
        duration = _minutes(left, right)
        term, _ = effective(notice["terms"], left)
        if term is None:
            gap_left, gap_remaining = left, duration
            while gap_remaining:
                tier = 1 if cumulative < base["tier1_min"] else 2
                head = min(gap_remaining, base["tier1_min"] - cumulative) if tier == 1 else gap_remaining
                uncovered.append({"start": gap_left, "end": _at(gap_left, head), "minutes": head,
                                  "tier": tier, "cumulative_min": cumulative + head})
                gap_left = _at(gap_left, head)
                gap_remaining -= head
                cumulative += head
            continue
        spent = 0
        while spent < duration:
            tier = 1 if cumulative < base["tier1_min"] else 2
            head = min(duration - spent, base["tier1_min"] - cumulative) if tier == 1 else duration - spent
            rate = Decimal(term[f"rate{tier}_usd_min"])
            basis_pieces.append({"start": _at(left, spent), "end": _at(left, spent + head),
                                 "minutes": head, "tier": tier, "version": term["version"],
                                 "term_line": term["line"], "rate_usd_min": f"{rate:.2f}",
                                 "expected_usd": f"{rate * head:.2f}"})
            spent += head
            cumulative += head
    grouped = {}
    for piece in basis_pieces:
        key = (piece["tier"], piece["version"])
        row = grouped.setdefault(key, {k: piece[k] for k in ("tier", "version", "term_line", "rate_usd_min")}
                                 | {"minutes": 0, "expected_usd": Decimal("0")})
        row["minutes"] += piece["minutes"]
        row["expected_usd"] += Decimal(piece["expected_usd"])
    rate_basis = [{**row, "expected_usd": f"{row['expected_usd']:.2f}"}
                  for row in sorted(grouped.values(), key=lambda r: (r["tier"], r["version"], r["term_line"]))]
    pending_terms = pending_term_issues(terms_issues)
    rate_status = "RATE_PENDING" if uncovered or pending_terms else "COMPLETE"
    return {"rate_status": rate_status, "rate_basis": rate_basis, "uncovered": uncovered,
            "pending_terms": pending_terms}


def pending_term_issues(terms_issues):
    return [{"line": item["line"], "code": item["code"], "version": (item["raw"][0] if item["raw"] else None)}
            for item in terms_issues if item["source"] == "terms"]


def tier_costs(term, overtime, pricing):
    """Aggregate the continuous rate basis into the two invoice demurrage tiers."""
    for tier in (1, 2):
        pieces = [row for row in pricing["rate_basis"] if row["tier"] == tier]
        gap = sum(item["minutes"] for item in pricing["uncovered"] if item["tier"] == tier)
        minutes = sum(row["minutes"] for row in pieces)
        expected = sum((Decimal(row["expected_usd"]) for row in pieces), Decimal("0"))
        if pieces:
            rates = {row["rate_usd_min"] for row in pieces}
            rate = next(iter(rates)) if len(rates) == 1 else None
            versions = sorted({row["version"] for row in pieces})
            expected_usd = None if gap else f"{expected:.2f}"
        else:
            rate, versions, expected_usd = None, [], None
            if overtime == 0 and not gap:
                rate = f"{Decimal(term[f'rate{tier}_usd_min']):.2f}"
                versions = [term["version"]]
                expected_usd = "0.00"
        yield {"tier": tier, "minutes": minutes, "uncovered_min": gap, "rate_usd_min": rate,
               "rate_versions": versions, "expected_usd": expected_usd}
