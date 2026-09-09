from retry import run_with_retries


def test_max_retries_excludes_initial_attempt() -> None:
    attempts = 0

    def succeeds_on_third_attempt() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return "ok"

    assert run_with_retries(succeeds_on_third_attempt, max_retries=2) == "ok"
    assert attempts == 3
