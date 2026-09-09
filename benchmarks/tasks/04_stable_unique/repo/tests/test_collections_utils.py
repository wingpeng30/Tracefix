from collections_utils import stable_unique


def test_stable_unique_preserves_order() -> None:
    assert stable_unique(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_stable_unique_handles_empty_list() -> None:
    assert stable_unique([]) == []
