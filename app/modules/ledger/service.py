from decimal import Decimal

from .rates import overtime_pricing, pending_term_issues, tier_costs


def compare(notice, clock, invoice, quarantined):
    actuals = {item["tier"]: item for item in invoice}
    if clock["overtime_min"] <= 0:
        pending = pending_term_issues(quarantined)
        pricing = {"rate_status": "RATE_PENDING" if pending else "COMPLETE", "rate_basis": [],
                   "uncovered": [], "pending_terms": pending}
    else:
        pricing = overtime_pricing(notice, clock["overtime_start"], clock["overtime_min"], quarantined)
    rows = []
    for tier in tier_costs(notice["term"], clock["overtime_min"], pricing):
        actual = actuals.get(tier["tier"])
        disputed = [q["line"] for q in quarantined if q["source"] == "invoice" and q["code"] == "KEY_CONFLICT"
                    and q["key"] == tier["tier"]]
        row_pending = tier["uncovered_min"] > 0 or bool(pricing["pending_terms"])
        if row_pending:
            code = "RATE_PENDING"
        elif actual is None:
            code = "INVOICE_CONFLICT" if disputed else "INVOICE_MISSING"
        else:
            delta = Decimal(tier["expected_usd"]) - Decimal(actual["usd"])
            code = "MATCH" if not delta and tier["minutes"] == actual["minutes"] else "TIER_DIFFERENCE"
        if actual is None or code == "RATE_PENDING":
            rows.append({**tier, "code": code, "invoice_line": None, "conflict_lines": disputed,
                         "actual_usd": None, "actual_minutes": None, "delta_usd": None})
            continue
        delta = Decimal(tier["expected_usd"]) - Decimal(actual["usd"])
        rows.append({**tier, "code": code, "invoice_line": actual["line"], "actual_usd": actual["usd"],
                     "actual_minutes": actual["minutes"], "delta_usd": f"{delta:.2f}"})
    complete = pricing["rate_status"] == "COMPLETE" and all(r["delta_usd"] is not None for r in rows)
    expected = sum((Decimal(r["expected_usd"]) for r in rows if r["expected_usd"] is not None), Decimal("0"))
    seen = sum((Decimal(r["actual_usd"]) for r in rows if r["actual_usd"] is not None), Decimal("0"))
    return {"expected_usd": f"{expected:.2f}" if pricing["rate_status"] == "COMPLETE" else None,
            "seen_invoice_usd": f"{seen:.2f}",
            "delta_usd": f"{expected - seen:.2f}" if complete else None,
            "complete": complete, "differences": sum(r["code"] not in {"MATCH", "RATE_PENDING"} for r in rows),
            "rate_status": pricing["rate_status"], "rate_basis": pricing["rate_basis"],
            "uncovered": pricing["uncovered"], "pending_terms": pricing["pending_terms"], "rows": rows}
