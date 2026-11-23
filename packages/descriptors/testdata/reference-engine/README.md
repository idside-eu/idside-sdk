# Reference engine (test data, frozen)

An earlier version of the prompt-cleaning engine — its catalogue
(`content-rules.json`), its Python modules (`python/`) and its Node modules
(`node/`) — kept byte for byte as it was. It is never edited, never imported by
either SDK and never shipped: the tests pin the SHA-256 of every file here.

It is the oracle of `content-oracle.json`: both suites clean a generated corpus
with it and with the engine under test, and every character it masked must
still be masked. A change to a pattern or to the engine may mask more, never
less, except where a named exclusion of that file says why.

`descriptors.ts` and `exceptions.ts` hand the frozen Node modules the two
imports they make from the SDK; the Python suite loads the frozen modules under
a package name of their own (`tests/_reference_engine.py`).
