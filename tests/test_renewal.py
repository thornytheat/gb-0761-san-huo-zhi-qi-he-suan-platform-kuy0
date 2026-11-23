"""中途续价：旧价保住起算时已挣滞期，续价后超时按新价；额度与档位连续累计。"""
import hashlib
import json

from app.modules.runs.storage import lookup
from app.pipeline import evaluate


def ledger(sources):
    return evaluate(sources)["ledger"]


def test_renewal_splits_overtime_at_effective_boundary(renewal):
    result = ledger(renewal)
    assert result["rate_status"] == "COMPLETE"
    assert result["expected_usd"] == "340.00"
    assert [(b["tier"], b["version"], b["minutes"], b["rate_usd_min"], b["expected_usd"])
            for b in result["rate_basis"]] == [
        (1, "R1", 20, "2.00", "40.00"),
        (2, "R1", 60, "4.00", "240.00"),
        (2, "R2", 10, "6.00", "60.00"),
    ]


def test_renewal_sample_invoice_matches_and_is_settleable(client):
    report = client.post("/api/runs", json={"sample": "renewal"}).json["snapshot"]
    pack = client.get(f"/api/evidence/{report['run_id']}").json
    assert pack["can_settle"] is True
    assert [r["code"] for r in report["results"]["ledger"]["rows"]] == ["MATCH", "MATCH"]


def test_allowance_is_granted_once_even_after_renewal(renewal):
    renewal["terms"] = renewal["terms"].replace("R2,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,45,30,",
                                                "R2,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,90,30,")
    clock = evaluate(renewal, "clock")["clock"]
    result = ledger(renewal)
    # R2 的 90 min 免计不能重新授一遍：超额仍为 90 min，而不是 90 - 90
    assert (clock["allowance_min"], clock["overtime_min"]) == (30, 90)
    assert result["expected_usd"] == "340.00"
    assert sum(b["minutes"] for b in result["rate_basis"]) == 90


def test_tier_one_capacity_accumulates_across_renewal(renewal):
    # 续价点挪到一档尚未耗尽时：20 min 一档容量在 R1/R2 间连续填满，不重新给一档
    renewal["terms"] = renewal["terms"].replace(
        "R1,2026-10-01T00:00Z,2026-10-20T12:00Z,10,10,30,20,2,4",
        "R1,2026-10-01T00:00Z,2026-10-20T10:50Z,10,10,30,20,2,4").replace(
        "R2,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,45,30,3,6",
        "R2,2026-10-20T10:50Z,2026-11-01T00:00Z,10,10,45,30,3,6")
    result = ledger(renewal)
    tier1 = [(b["version"], b["minutes"]) for b in result["rate_basis"] if b["tier"] == 1]
    tier2_minutes = sum(b["minutes"] for b in result["rate_basis"] if b["tier"] == 2)
    # 超额 10:40 起：前 10 min 按 R1 一档价，一档 20 min 容量的后 10 min 按 R2 一档价填满
    assert tier1 == [("R1", 10), ("R2", 10)]
    assert tier2_minutes == 70
    assert result["expected_usd"] == f"{10 * 2 + 10 * 3 + 70 * 6}.00"


def test_old_rate_earned_before_renewal_is_kept(renewal):
    renewal["terms"] = renewal["terms"].replace(",3,6", ",9,18")
    result = ledger(renewal)
    # 续价前 R1 已挣 40 + 240 = 280 保持旧价；仅续价后 10 min 用新价
    assert [b["expected_usd"] for b in result["rate_basis"]] == ["40.00", "240.00", "180.00"]
    assert result["expected_usd"] == "460.00"


def test_late_renewal_gap_is_pending_until_supplied(renewal):
    renewal["terms"] = renewal["terms"].replace("R2,2026-10-20T12:00Z,2026-11-01T00:00Z,",
                                                "R2,2026-10-20T12:05Z,2026-11-01T00:00Z,")
    result = ledger(renewal)
    assert result["rate_status"] == "RATE_PENDING"
    assert result["expected_usd"] is None and result["delta_usd"] is None
    gap = result["uncovered"][0]
    assert (gap["start"], gap["end"], gap["minutes"]) == ("2026-10-20T12:00+00:00", "2026-10-20T12:05+00:00", 5)
    assert [r["code"] for r in result["rows"]] == ["MATCH", "RATE_PENDING"]
    assert result["rows"][1]["uncovered_min"] == 5
    # 一档未触及空档，仍可与账单逐档核对
    assert result["rows"][0]["delta_usd"] == "0.00"
    # 补齐续价后重算：同一输入加回 R2 覆盖窗即恢复完整
    renewal["terms"] = renewal["terms"].replace("2026-10-20T12:05Z", "2026-10-20T12:00Z")
    fixed = ledger(renewal)
    assert fixed["rate_status"] == "COMPLETE" and fixed["expected_usd"] == "340.00"


def test_defective_renewal_record_marks_pending_with_line(renewal):
    renewal["terms"] += "R3,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,45,30,3\n"
    result = ledger(renewal)
    assert result["rate_status"] == "RATE_PENDING"
    pending = result["pending_terms"][0]
    assert (pending["line"], pending["code"], pending["version"]) == (4, "COLUMN_COUNT", "R3")
    # 已覆盖部分仍给出连续依据，待核不抹掉旧价已挣金额
    assert [(b["version"], b["expected_usd"]) for b in result["rate_basis"]][:2] == [
        ("R1", "40.00"), ("R1", "240.00")]


def test_pending_rate_cannot_be_certified(client, renewal):
    renewal["terms"] = renewal["terms"].replace("R2,2026-10-20T12:00Z,2026-11-01T00:00Z,",
                                                "R2,2026-10-20T12:05Z,2026-11-01T00:00Z,")
    run_id = client.post("/api/runs", json={"sources": renewal}).json["run_id"]
    assert client.get(f"/api/evidence/{run_id}").json["can_settle"] is False


def test_old_run_keeps_rates_when_replayed(client, renewal):
    first = client.post("/api/runs", json={"sample": "renewal"}).json["snapshot"]
    stored = lookup(client.application.config["DATABASE"], first["run_id"])
    # 留档内容按源文本与规则版本固化：重跑返回同一份快照，费率依据不被改写
    again = client.post("/api/runs", json={"sample": "renewal"}).json["snapshot"]
    assert again == stored == first
    assert first["rule"] == "laytime-1.1"
    identity = json.dumps([first["rule"], first["source_sha256"], first["parameters"]],
                          sort_keys=True).encode()
    assert hashlib.sha256(identity).hexdigest() == first["run_id"]
    basis = {(b["tier"], b["version"]): b["expected_usd"] for b in first["results"]["ledger"]["rate_basis"]}
    assert basis == {(1, "R1"): "40.00", (2, "R1"): "240.00", (2, "R2"): "60.00"}


def test_review_page_shows_payable_and_rate_basis(client):
    page = client.get("/ledger?sample=renewal").data
    assert "采用费率依据".encode() in page
    assert "340.00 USD".encode() in page and b"R1" in page and b"R2" in page
    assert "续价不重复授予".encode() in page
    # 二档跨 R1/R2 两版：分档行不显示空费率，引导到费率依据表
    assert "多档费率见下".encode() in page
    text = page.decode()
    second = text.split('第 2 档')[1]
    assert "None" not in second.split("</div>")[0]


def test_gap_inside_tier_one_is_charged_to_first_tier(renewal):
    # R1 在一档耗尽前提前结束、R2 晚到：空档按累计分钟归入一档，而不是写死二档
    renewal["terms"] = renewal["terms"].replace(
        "R1,2026-10-01T00:00Z,2026-10-20T12:00Z,10,10,30,20,2,4",
        "R1,2026-10-01T00:00Z,2026-10-20T10:50Z,10,10,30,20,2,4").replace(
        "R2,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,45,30,3,6",
        "R2,2026-10-20T10:55Z,2026-11-01T00:00Z,10,10,45,30,3,6")
    result = ledger(renewal)
    assert result["rate_status"] == "RATE_PENDING"
    gap = result["uncovered"][0]
    assert (gap["tier"], gap["start"], gap["end"], gap["minutes"]) == (
        1, "2026-10-20T10:50+00:00", "2026-10-20T10:55+00:00", 5)
    assert result["rows"][0]["uncovered_min"] == 5 and result["rows"][1]["uncovered_min"] == 0


def test_gap_straddling_tier_boundary_splits_continuously(renewal):
    # 空档 10:55–11:05：一档在 11:00 填满，空档切成 5+5，跨档分钟连续累计
    renewal["terms"] = renewal["terms"].replace(
        "R1,2026-10-01T00:00Z,2026-10-20T12:00Z,10,10,30,20,2,4",
        "R1,2026-10-01T00:00Z,2026-10-20T10:55Z,10,10,30,20,2,4").replace(
        "R2,2026-10-20T12:00Z,2026-11-01T00:00Z,10,10,45,30,3,6",
        "R2,2026-10-20T11:05Z,2026-11-01T00:00Z,10,10,45,30,3,6")
    result = ledger(renewal)
    assert [(g["tier"], g["minutes"], g["cumulative_min"]) for g in result["uncovered"]] == [
        (1, 5, 20), (2, 5, 25)]
    assert (result["rows"][0]["uncovered_min"], result["rows"][1]["uncovered_min"]) == (5, 5)
    assert sum(b["minutes"] for b in result["rate_basis"]) == 80


def test_review_page_flags_pending_renewal(client, renewal):
    renewal["terms"] = renewal["terms"].replace("R2,2026-10-20T12:00Z,2026-11-01T00:00Z,",
                                                "R2,2026-10-20T12:05Z,2026-11-01T00:00Z,")
    page = client.post("/ledger", data={"sample": "custom", **renewal}).data
    assert "费率待核".encode() in page and "无有效费率区间".encode() in page
