bad-names-rgxs mangles regular expressions with commas

### Bug description

Since pylint splits on commas in this option, instead of taking a list of strings, if there are any commas in the regular expression, the result is mangled before being parsed. The config below demonstrates this clearly by causing pylint to crash immediately.

### Configuration

```ini
[tool.pylint.basic]
# capture group ensures that the part after the comma is an invalid regular
# expression, causing pylint to crash
bad-name-rgxs = "(foo{1,3})"
```

### Command used

```shell
pylint foo.py
```

### Pylint output

Pylint crashes while parsing `_regexp_csv_transfomer`: the value is split at the comma inside `{1,3}`, and `re.compile()` then receives the invalid fragment `(foo{1`.

### Expected behavior

Any valid regular expression should be expressible in this option. If that cannot be supported directly, there should at least be a way to escape commas so the issue can be worked around.

### Pylint version

```text
pylint 2.14.4
astroid 2.11.7
Python 3.10.4
```

### OS / Environment

Pop! OS 22.04
