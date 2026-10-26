# Audit trail

`session.store_backend` is the session's SQLite store. The methods below
read its audit rows and its flattened instance table. They are
*documented but internal* (see [API stability](stability.md)). The words
in the rows are frozen. The methods that return them may change in a
minor 1.x release, never in a patch release, with a `Breaking`
CHANGELOG entry
([API stability](stability.md#renames-and-removals-after-10)). What
each `action_type` means is on
[Analytics & Reporting](../analytics.md).

```{eval-rst}
.. autoclass:: isocenter.persistence.SqliteStore

   .. automethod:: get_audit_summary
   .. automethod:: get_audit_errors
   .. automethod:: get_audit_losses
   .. automethod:: get_audit_declines
   .. automethod:: get_audit_scan_gaps
   .. automethod:: get_audit_drops
   .. automethod:: get_flattened_instances
```
