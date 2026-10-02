# Quoted braces in Markdown attributes

Repair Python-Markdown #1390 / #1414. Quoted attribute values containing `{}` must work with single and double quotes in block, inline and fenced-code syntax. Inline trailing text must survive; extra unquoted closing braces must not turn an invalid block/fence into valid syntax or cause an infinite loop. Preserve ordinary attributes, empty attribute-list literal behavior, tables, fenced-code languages, IDs and classes in the frozen existing tests. Only the declared tests are the acceptance contract, not the entire library. Do not modify tests.

Required task test: `tests/test_tracefix_quoted_braces.py`.
Required regression tests: `tests/test_syntax/extensions/test_attr_list.py::TestAttrList` and `tests/test_syntax/extensions/test_fenced_code.py::TestFencedCode`.
Source: https://github.com/Python-Markdown/markdown/pull/1414 . Reference patch is outside the Agent checkout; qualification replay is not autonomous repair.
