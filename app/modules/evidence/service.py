import hashlib
import json


def certify(snapshot):
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    evidence = snapshot["results"]
    ledger = evidence["ledger"]
    return {"snapshot": snapshot, "integrity_sha256": hashlib.sha256(payload).hexdigest(),
            "can_settle": ledger["complete"] and ledger["rate_status"] == "COMPLETE"
            and not evidence["clock"]["missing"] and not ledger["differences"]
            and evidence["clock"]["balanced"],
            "evidence_counts": {"intervals": len(evidence["clock"]["rows"]), "tiers": len(ledger["rows"]),
                                "rate_basis": len(ledger["rate_basis"]),
                                "isolated": len(evidence["intake"]["isolated"])}}
