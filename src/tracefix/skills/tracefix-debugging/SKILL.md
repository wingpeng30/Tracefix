---
name: tracefix-debugging
description: Trace a reproducible Python repository failure to its likely root cause and make a small verified fix. Use when TraceFix is asked to debug a failing test, traceback, or reported behavior.
metadata:
  version: "1.0.0"
---

# TraceFix debugging

Use the existing `search_code`, `read_file`, `apply_patch`, `run_tests`, and `get_git_diff` tools.

1. Read the failure evidence first. Keep the original error, command, and observed behavior distinct from guesses.
2. Search for the failing symbol, then read its implementation, callers, and relevant tests. Follow the call path only as far as the evidence requires.
3. State one root-cause hypothesis that predicts an observable result. Check it against the code or a focused test before changing code.
4. Make the smallest change that addresses that cause. Preserve unrelated user changes and do not edit, skip, or weaken tests to make them pass.
5. Inspect the returned patch result. If it reports no effective change or an error, reread the affected file and adapt; do not repeat the same patch.
6. Run the narrowest relevant test with `run_tests`. Respect its protected paths, command validation, timeout, and run budget. If the test cannot safely run or exceeds budget, report that without claiming verification.
7. Inspect `get_git_diff` after the test. Tie the final explanation to the actual diff and test result; separate verified facts from remaining uncertainty.

Do not broaden exploration or run unrelated suites merely because this skill is active. The skill does not grant permissions beyond the registered tools.
