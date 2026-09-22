"""Fixed project secrets for tests that need to know which one a store holds.

In `tests/support/`, not at the top of `tests/`: a top-level module that
is not `test_*.py` or `conftest.py` is one pytest never collects, and
`test_packaging_contract` refuses it (#347). No `__init__.py`, so the
directory is a namespace package and adds no second `__init__.py`
basename for the source-citation index to disambiguate.

Not a conftest autouse, and not a patch of the generator. A test that
decides a store's secret says so explicitly, per store, by inserting one
of these into that store's `project_secret` row. Patching the generator
instead would make every store in the process share one secret, and "two
stores that share a secret agree" would then pass by accident -- the
property the cross-store tests exist to check.

Through the store's private `_insert_project_secret`, the one door the
generator itself uses, because there is no public one: a project secret
stays in the store that generated it (#716), and 0.9.7's
`load_project_secret(path)`, which this used until then, is deleted. The
insert never replaces a row, so a store that already holds a secret keeps
it, and this raises rather than let a test run under a secret it did not
choose.
"""

#: Two distinct fixed secrets. Every literal pinned under one of them in
#: the suite was computed once from the implementation and pasted.
FIXED_A = bytes(range(32))
FIXED_B = bytes(range(1, 33))


def load_fixed_secret(session, directory=None, secret=FIXED_A):
    """Give `session`'s store `secret` before its first audit().

    `directory` is unused since #716 (there is no file to write) and kept
    so the suite's call sites stay as they are.
    """
    del directory
    store = session.store_backend
    # pylint: disable=protected-access
    inserted, held = store._insert_project_secret(secret, store._ORIGIN_LOADED)
    if not inserted or held != secret:
        raise RuntimeError(
            "load_fixed_secret: this store already holds a project secret; "
            "give a store its fixed secret before its first audit() or "
            "anonymize()")
