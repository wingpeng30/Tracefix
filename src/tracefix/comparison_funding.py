"""Link new evaluation batches to the same authorization without reusing funds."""

from pathlib import Path

from tracefix.checkpoint import ProcessLock


def freeze_parent_budget(parent: Path) -> dict:
    from tracefix.comparison import digest, file_sha, read_json
    from tracefix.comparison_campaign import validate_requests
    from tracefix.comparison_profiles import protocol_profile

    with ProcessLock(parent):
        protocol = read_json(parent / "protocol.json")
        if digest(protocol) != read_json(parent / "protocol.sha256.json")["sha256"]:
            raise ValueError("parent protocol corrupted")
        if protocol_profile(protocol)["limits"]["limit_cny"] != 300:
            raise ValueError("parent must belong to the same 300 CNY authorization")
        requests = validate_requests(parent, protocol)
        if any(r["status"] != "completed" for r in requests):
            raise ValueError("parent has unknown fees; cannot start another paid batch")
        prior = prior_spend(protocol) + sum(r["peak_cost_cny"] for r in requests)
        if prior < 0 or prior > 300:
            raise ValueError("invalid prior authorization spend")
        return {
            "parent_campaign": str(parent.resolve()),
            "requests_sha256": file_sha(parent / "requests.json"),
            "parent_protocol_sha256": file_sha(parent / "protocol.json"),
            "prior_conservative_cny": prior,
            "authorization_cny": 300.0,
        }


def prior_spend(protocol: dict) -> float:
    from tracefix.comparison import file_sha, read_json

    funding = protocol.get("funding")
    if funding is None:
        return 0.0
    parent = Path(funding["parent_campaign"])
    if file_sha(parent / "requests.json") != funding["requests_sha256"]:
        raise ValueError("parent funding ledger changed")
    if file_sha(parent / "protocol.json") != funding["parent_protocol_sha256"]:
        raise ValueError("parent funding protocol changed")
    prior_spend(read_json(parent / "protocol.json"))
    prior = funding["prior_conservative_cny"]
    if funding["authorization_cny"] != 300.0 or not 0 <= prior <= 300:
        raise ValueError("authorization funding identity changed")
    return float(prior)
