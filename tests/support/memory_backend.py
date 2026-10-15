"""A store backend for tests that drive `RedactionService` over a graph
built by hand, with no session behind it.

Since #1113 a `RedactionService` with no store backend refuses to redact:
with nowhere to store the blanked frame, it would write the redaction's
record (new UID, hash, `DERIVED`, Burned In Annotation `NO`) over pixels
the loader still reads unredacted. These tests hold their pixels in memory
only (no loader, no sidecar), so there is nothing to persist; this stub
records each persist and each audit row instead, and holds no project
secret, so a caller passes `project_secret=` as before.

In `tests/support/` for the reason `project_secret.py` gives.
"""


class MemoryBackend:
    """Records `persist_pixel_data` calls and `log_audit` rows."""

    def __init__(self):
        self.persisted = []
        self.rows = []

    def persist_pixel_data(self, inst):
        """Record the persist; the pixels stay where they are, in memory."""
        self.persisted.append(inst.sop_instance_uid)

    def log_audit(self, action_type, entity_uid, details, **_):
        """Record the row."""
        self.rows.append((action_type, entity_uid, details))
