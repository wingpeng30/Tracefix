# Python-Markdown quoted attribute braces

This BSD-3-Clause task freezes PR #1414's actual parent and product-only diff.
It covers parsing and cooperation between `attr_list` and `fenced_code`.

```powershell
git clone --no-checkout https://github.com/Python-Markdown/markdown.git <local-source>
python scripts/qualify_public_task.py --task markdown-quoted-braces-1414 --source-repo <local-source> --test-python <python> --output <new-directory>
```

Use an installed TraceFix and a prepared pytest interpreter. No dependencies are
installed and no provider is contacted by qualification. Flat-layout `markdown`
must import from each independent checkout. The script rejects reused output.

The task has nine new assertions (including six quote/context combinations),
two frozen existing attribute-list tests, and nineteen existing fenced-code tests.
Coverage is limited to these targets, not the whole project.

`reference-product.patch` is the exact upstream product diff. Actual qualification
found that this patch reorders trailing inline text after extra closing braces.
With user authorization, `reference-correction.patch` separately preserves the
original tail order and whitespace. The qualified reference is **both patches**,
not upstream commit #1414 alone. All patches and the contract are hashed in the
manifest. The original failed run is retained; qualification also audits the
uncorrected upstream patch before testing the corrected reference twice.

`diagnostic-wrong.patch` applies that corrected reference but incorrectly accepts
empty attribute lists; it must pass the new task while failing the existing
attribute-list tests. Reference replay through the production Runner, validation
gate and independent verification demonstrates harness behavior, not model repair.

Results, raw stdout/stderr, JUnit, source/environment fingerprints, report and
export are written to the output directory. A manifest alone is not qualification
evidence; inspect `qualification.json` and the exact CI checkout identity.
