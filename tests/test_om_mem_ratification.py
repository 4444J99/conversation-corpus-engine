"""Guard the OM-MEM-001 ratification request packet stays honest.

IRF-CCE-015 / GH#14: the criterion may not be marked ratified by automation.
These tests pin the packet's decision-pending shape until the human governance
gate at organvm-engine#239 records accept/revise/reject.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKET = REPO_ROOT / "docs" / "ratification" / "om-mem-001-ratification-request.json"


def load_packet() -> dict:
    return json.loads(PACKET.read_text(encoding="utf-8"))


def test_ratification_packet_parses_and_targets_om_mem_001() -> None:
    packet = load_packet()
    assert packet["record"] == "om-mem-001-ratification-request"
    assert packet["irf"] == "IRF-CCE-015"
    assert packet["criterion"]["id"] == "OM-MEM-001"


def test_ratification_packet_is_not_marked_ratified() -> None:
    packet = load_packet()
    assert packet["status"] == "proposed-decision-requested"
    assert packet["resolution"] is None
    assert packet["decision_record"] is None


def test_ratification_packet_carries_evidence_requirements() -> None:
    packet = load_packet()
    evidence_ids = {e["id"] for e in packet["criterion"]["evidence"]}
    assert evidence_ids == {"a", "b", "c"}
    assert packet["criterion"]["measurement"].strip()
    assert packet["criterion"]["gap"].strip()


def test_ratification_packet_routes_to_canonical_owner() -> None:
    packet = load_packet()
    route = packet["amendment_route"]
    assert "organvm-engine" in route["canonical_owner"]
    assert "issues/239" in route["decision_request_issue"]
    assert "human" in route["human_gate"].lower()


def test_ratification_packet_proposes_free_scorecard_id() -> None:
    packet = load_packet()
    # Draft spec proposed #18, which collides with the live omega #18
    # (First Organic Revenue). The packet must flag a free id instead.
    assert packet["criterion"]["proposed_number"] != 18
    assert packet["impact_if_ratified"]["new_criterion_status"] == "NOT_MET"
