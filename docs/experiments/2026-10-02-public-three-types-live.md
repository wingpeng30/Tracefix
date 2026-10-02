# Fresh authorized three-task campaign

2026-10-02 user authorized maximum five logical Agent runs/CNY five total;
failures count, either bound stops. Old eight-run evidence is untouched.
First runs: more-itertools, Markdown, Click. Two reserved rechecks require a
reproduced issue and recorded fix, not sampling until success.

Entrypoint: `python scripts/live_public_tasks.py --root runs/live-public-three-types-20261002 --task <id> --qualification <qualified-record.json> --env-file .env`.
`--prepare-only` is zero-call and writes separate evidence. Qualified source,
manifest and environment identities, process lock, fresh durable run/request
ledgers, recorded regression targets and independent verification are required.
Unknown request/result/usage stops further paid calls. Reference patches remain
outside the Agent checkout.

Official DeepSeek Flash, non-thinking, non-streaming, zero retries, local backend,
Skills/MCP off. Per run: 20 steps, 60,000 input/8,000 output cumulative tokens,
2,048 output per request, 16 tests, 900 active seconds, 60-second timeout.
Qualified request counting is unchanged; unavailable hard counter must stop.
Official 2026-10-02 peak prices: CNY 2/M uncached input, CNY 8/M output;
snapshot in `runs/live-public-three-types-20261002/prices.json`. Client reservation
is not a supplier billing hard cap. Request views remain local and reviewed.

Offline preparation: six ledger/budget tests passed with explicit Python 3.12;
Ruff, compileall and Diff passed. Default shell Python was unrelated Anaconda.
Sandbox temporary ACL blocked pytest; a new unsandboxed temporary directory
passed. Failed environment attempts are not behavioral evidence.

Run results will be appended after execution; no live success claimed yet.

First three runs at `4201ab5`: more-itertools completed and independently passed;
Markdown stopped after 65,671 input tokens with a failed independent patch;
Click stopped after 72,869 input tokens without a patch. These demonstrate the
ordinary cumulative Token limit was post-response, not a supplier hard cap.
All 23 requests have valid usage and no pending result. Source remained unchanged.

Targeted correction: new campaign adapter checks conservative per-request input
reservation plus known cumulative usage before sending; output is also reserved.
Formal P2 counting guards are unchanged. Rechecks lower the existing deterministic
compaction trigger from 32,000 to 6,000 estimated tokens; budgets are unchanged.
Sixteen offline tests (ledger, request rejection, context) passed. Rechecks are
two newly counted logical runs, not resumes or replacements for failed results.
