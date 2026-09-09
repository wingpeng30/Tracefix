from events import Event, merge_events


def test_sequence_breaks_timestamp_tie():
    result = merge_events([
        Event("api", "x", 10, 2, {"winner": "old", "keep": 1}),
        Event("API", " X ", 10, 3, {"winner": "new"}),
    ])
    assert result[0].sequence == 3
    assert result[0].payload == {"winner": "new", "keep": 1}


def test_fully_tied_event_keeps_first_value_but_absorbs_new_fields():
    result = merge_events([
        Event("api", "x", 10, 2, {"winner": "first"}),
        Event("api", "x", 10, 2, {"winner": "second", "extra": True}),
    ])
    assert result[0].payload == {"winner": "first", "extra": True}


def test_identity_is_casefolded_and_trimmed():
    assert len(merge_events([Event("CRM", " Ä ", 1, 1), Event("crm", "ä", 2, 1)])) == 1
