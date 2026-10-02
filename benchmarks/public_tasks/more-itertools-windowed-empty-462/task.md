# Empty input must not produce padded windows

Fix `more_itertools.windowed` so empty lists, tuples and generators yield no windows for positive window widths, including explicit fill values and steps. Preserve the frozen version's existing behavior for nonempty short-input padding, normal sliding windows and step sizes, invalid width/step exceptions, and zero width (`[()]`). Do not modify tests.

Required target: `tests/test_tracefix_windowed_empty.py` (36 public combinations).
Required regression target: `tests/test_more.py::WindowedTests` (six existing methods, including multiple step cases).
Only these targets are the declared acceptance contract. They do not prove correctness of the whole library. A recorded reference patch replay is not autonomous model repair.
