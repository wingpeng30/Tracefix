from events import Event, merge_events


def test_newer_event_wins_and_payload_is_merged():
    events = [
        Event("CRM", " A-1 ", 20, 1, {"owner": "lin", "status": "open"}),
        Event("crm", "a-1", 10, 9, {"status": "stale", "note": "old"}),
    ]
    result = merge_events(events)
    assert len(result) == 1
    assert result[0].timestamp == 20
    assert result[0].payload == {"owner": "lin", "status": "open", "note": "old"}


def test_output_retains_first_seen_identity_order():
    result = merge_events([
        Event("z", "2", 1, 1), Event("a", "1", 1, 1), Event("Z", "2", 2, 1)
    ])
    assert [event.external_id.strip() for event in result] == ["2", "1"]
