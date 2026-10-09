"""Link new evaluation batches to the same authorization without reusing funds."""

from pathlib import Path

from tracefix.checkpoint import ProcessLock


def freeze_new_budget(authorization: Path, campaign: Path) -> dict:
    """Claim one explicit new authorization for one campaign, including failed preparation."""
    from tracefix.comparison import digest, file_sha, read_json, write_json
    from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE

    authorization = authorization.resolve()
    campaign = campaign.resolve()
    if authorization.parent == campaign:
        raise ValueError("authorization must be outside the campaign lock directory")
    with ProcessLock(authorization.parent):
        record = read_json(authorization)
        if (
            record.get("kind") != "tracefix_paid_authorization"
            or record.get("profile") != AC_HOLDOUT_PROFILE
            or record.get("limit_cny") != 300.0
            or record.get("authorization_path") != str(authorization)
            or not isinstance(record.get("authorization_id"), str)
            or not record["authorization_id"].strip()
            or not record.get("user_instruction")
        ):
            raise ValueError("explicit independent 300 CNY authorization required")
        claim = {"campaign": str(campaign), "authorization_sha256": file_sha(authorization)}
        claim_path = authorization.with_suffix(".claim.json")
        if claim_path.exists() and read_json(claim_path) != claim:
            raise ValueError("authorization already claimed by another campaign")
        write_json(claim_path, claim)
        return {
            "kind": "independent_authorization",
            "authorization_cny": 300.0,
            "prior_conservative_cny": 0.0,
            "authorization": str(authorization),
            "authorization_sha256": file_sha(authorization),
            "authorization_id": record["authorization_id"],
            "campaign": str(campaign),
            "claim_sha256": digest(claim),
        }


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
    from tracefix.comparison import digest, file_sha, read_json

    funding = protocol.get("funding")
    if funding is None:
        return 0.0
    if funding.get("kind") == "independent_authorization":
        from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE

        authorization = Path(funding["authorization"])
        record = read_json(authorization)
        claim = read_json(authorization.with_suffix(".claim.json"))
        if (
            protocol.get("profile", {}).get("name") != AC_HOLDOUT_PROFILE
            or file_sha(authorization) != funding["authorization_sha256"]
            or record.get("limit_cny") != 300.0
            or funding["authorization_cny"] != 300.0
            or record.get("authorization_id") != funding["authorization_id"]
            or funding["prior_conservative_cny"] != 0.0
            or digest(claim) != funding["claim_sha256"]
            or claim
            != {
                "campaign": funding["campaign"],
                "authorization_sha256": funding["authorization_sha256"],
            }
        ):
            raise ValueError("independent authorization identity changed")
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
