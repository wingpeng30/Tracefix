# Click shell-completion resource lifecycle

This BSD-3-Clause task freezes Click #2800's actual parent and exact product diff.
The source uses `src/click`; each independent checkout's import probe and pytest
audit must confirm its modules are loaded instead of an installed Click copy.

```powershell
git clone --no-checkout https://github.com/pallets/click.git <local-source>
python scripts/qualify_public_task.py --task click-completion-resources-2800 --source-repo <local-source> --test-python <python> --output <new-directory>
```

Prepare TraceFix, pytest and Click's Windows dependency `colorama` yourself before
qualification. The engineering locks contain the dependency. Qualification does
not install anything, run a real shell, or contact a provider.

Four new cases cover command, group, nested group and chain. Each queries actual
`ShellComplete` with Choice values and checks created contexts, callback counts,
context-manager closed state and exactly one resource close. Strong references
to resources make this independent of garbage collection and ResourceWarning.
Three frozen existing regression targets cover command options, chain completion
and shared parent/child short options; each is one test with multiple assertions.

The upstream product-only `reference-product.patch` fixes some resource closing
but consumes parent arguments when resolving nested and chain contexts. Actual
tests show two new cases fail despite all selected existing tests passing.
`reference-correction.patch` is a separately hashed, user-approved two-line fix
using the newly created child argument lists. The qualified reference is the
combination, not upstream commit #2800 alone. The uncorrected upstream patch is
audited separately. No assertions are relaxed to admit it.

The diagnostic mutant applies that corrected reference but drops single-dash
option completions. It passes lifecycle/Choice cases but must fail two existing
option targets. Base and corrected reference each run twice; every expected
failure must be an assertion with original JUnit evidence, not an import error.

Production adapter/Runner/tools/validation gate, independent verification, report
and export then run a recorded reference replay. Its seven Agent pytest processes
are distinct from independent verification. Usage is simulated, supplier cost is
unknown, and this is not autonomous model repair. Original clones remain unchanged.
