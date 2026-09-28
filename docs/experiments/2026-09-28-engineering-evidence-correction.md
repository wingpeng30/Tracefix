# Engineering evidence correction (2026-09-28)

This addendum corrects the summary of `pytest-only-final-v2`. The original files under
`E:\TraceFixRunsActive\reproduce-zero-call-20260928\pytest-only-final-v2` remain unchanged.

| Evidence | Value recorded by the source artifact |
| --- | --- |
| Interpreter | Python 3.12.5 on Windows |
| JUnit | 602 passed, 0 failed |
| Duration | 681.40 seconds |
| Coverage JSON statements | 8,389 / 9,128 |
| Coverage JSON branches | 2,414 / 2,926 |
| Exact combined coverage | 10,803 / 12,054 = 89.62170233947238% |
| Coverage terminal result | Coverage.py printed that the 90% minimum was not reached |
| Saved `exit-code.txt` | `0` |

The previous summaries incorrectly listed 8,392 covered statements and 2,415 covered branches,
and treated the coverage failure message as a saved process exit code of 1. The artifact contains
different JSON counts and a saved exit-code file containing `0`. This evidence therefore proves
the coverage gate failed; it does **not** reliably establish the pytest process return code because
the saved code conflicts with the reported pytest-cov failure. Preserve both observations until a
new runner records the child process return code directly.

New engineering runs use one fresh output directory and save the subprocess return code immediately,
before any later shell command. Acceptance is computed independently from the exact JSON numerator
and denominator, JUnit test/failure counts, report presence, and raw return code. A rounded terminal
`90%` display is never accepted as proof of the threshold.

No combined historical coverage data is used for the current checkout gate.
