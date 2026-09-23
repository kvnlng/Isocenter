"""Read a closed or open store's project secret, for a test that did not fix one.

A test whose subject is not the secret, and that therefore let the store
generate its own, still has to name the UIDs `anonymize()` replaced
under it (#544). Read straight from the `project_secret` row, so this
module names no `isocenter` module and draws no probe row; the test pairs
it with `isocenter.privacy._replacement_uid_for`, which it imports itself.
"""
import sqlite3


def secret_of(db) -> bytes:
    """The project secret the store at `db` holds; raises if it has none."""
    with sqlite3.connect(str(db)) as conn:
        (secret_hex,) = conn.execute("SELECT secret_hex FROM project_secret").fetchone()
    return bytes.fromhex(secret_hex)
