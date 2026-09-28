# Dependency locks and license notes

## Frozen inputs

These locks were produced with pip-tools 7.5.1, pip 24.3.1, setuptools 75.8.2, and wheel 0.45.1. Windows engineering locks were resolved separately with CPython 3.11.16 x64 and CPython 3.12.5 x64. The public smoke lock targets Python 3.11 and is also used in the Linux amd64 task image. Every resolved wheel/sdist line includes accepted SHA-256 hashes; install with pip `--require-hashes`. TraceFix itself is built from the checked-out source with `--no-deps --no-build-isolation`, so project build isolation cannot resolve floating build requirements.

| Lock | Purpose | SHA-256 |
|---|---|---|
| `requirements/locks/build-tools.txt` | Frozen pip/build/lock toolchain | `e199a977a6113a619a70675749da0788c0c7cc16962012a02025d6ddb962aaa0` |
| `requirements/locks/engineering-py311.txt` | Windows x64 Python 3.11 full pytest, coverage, Ruff, TLS and adapter checks | `17f7dbb43d0e4861324091b3da350265722fe0a46779a66d1e07bcfec4fd011e` |
| `requirements/locks/engineering-py312.txt` | Windows x64 Python 3.12 full pytest, coverage, Ruff, TLS and adapter checks | `b6331b22e5d00e540809023badf548a4b2f2dbee1e91c1a2c81f6ef45f6f049d` |
| `requirements/locks/reproduction-py311.txt` | Minimal public synthetic task image dependencies | `37400f53804e26e768195a67e312f871adfa390b0027abd1aeb4c0f3addbd6cd2` |

Editable installation and wheel installation use the same source project version but distinct environments. The wheel has no vendored third-party dependencies. `benchmark` dataset acquisition packages and historical task images are excluded from the public smoke lock. The public Docker task base is `python:3.11.16-bookworm@sha256:00f0ecbf74ff8f915020d5a40c4bc6a83f46cd7b83f47db51c7e204f0d8a3ec2`, an immutable upstream image manifest; a built task image receives a new local/CI image ID. The Dockerfile source and digest are reviewed together; do not replace this with a mutable tag or claim a derived ID is the base digest.

To regenerate the locks, create a fresh tool environment from a reviewed pip-tools release, explicitly use `https://pypi.org/simple`, and compile each `.in` file with `pip-compile --generate-hashes --strip-extras`. For the engineering locks resolve under the corresponding Windows Python minor; for the reproduction lock resolve under Python 3.11 with all platform artifacts hashed. Regeneration is a dependency change: review all version changes and license metadata, then update the recorded lock SHA-256. Do not install build scripts from an upstream repository as a shortcut.

## License obligations

License identifiers below were read from installed distribution metadata for the Python 3.11 engineering lock (where an upstream distribution omitted a SPDX expression, its published classifier/license text should be checked against the selected wheel before redistribution). This is an inventory, not legal advice or proof that a downstream bundle already contains notices. MIT/Apache/BSD dependencies require their applicable copyright/license text to accompany binary redistribution; MPL-2.0 remains under its file-level copyleft terms. TraceFix does not vendor these distributions in its wheel.

| License metadata | Locked engineering packages |
|---|---|
| Apache-2.0 / Apache 2.0 | `aiohttp`, `aiosignal`, `coverage`, `frozenlist`, `hf-xet`, `huggingface_hub`, `importlib_metadata`, `openai`, `propcache`, `yarl` |
| Apache-2.0 OR BSD-3-Clause | `cryptography` |
| BSD-3-Clause | `fsspec`, `httpcore`, `httpcore2`, `httpx`, `httpx2`, `idna`, `MarkupSafe`, `python-dotenv`, `requests`, `Jinja2` |
| BSD-2-Clause | `Pygments` |
| MIT / MIT-0 | `annotated-types`, `anyio`, `attrs`, `cffi`, `charset-normalizer`, `filelock`, `h11`, `iniconfig`, `jiter`, `jsonschema`, `jsonschema-specifications`, `litellm`, `pluggy`, `pydantic`, `pydantic_core`, `PyYAML`, `referencing`, `rpds-py`, `ruff`, `tiktoken`, `truststore`, `urllib3`, `zipp` |
| MIT OR Apache-2.0 | `sniffio` |
| Apache-2.0 | `tokenizers` (upstream wheel classifier; its metadata omits the SPDX field) |
| MPL-2.0 | `certifi` |
| Apache-2.0 AND CNRI-Python | `regex` |
| PSF-2.0 | `aiohappyeyeballs`, `typing_extensions` |
| Apache License 2.0 | `multidict` |
| MPL-2.0 AND MIT | `tqdm` |

The lock generator/build tools also carry their upstream notices; inspect wheel license files for `build`, `click`, `colorama`, `packaging`, `pip`, `pip-tools`, `pyproject-hooks`, `setuptools`, and `wheel` when redistributing a packaged environment. Review the authoritative package metadata and license files for the exact locked release before producing a combined binary distribution. The locks identify packages and artifacts; they do not grant redistribution rights beyond each license.
