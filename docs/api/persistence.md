# Audit trail

`session.store_backend` is the session's SQLite store. The methods below
read its audit rows and its flattened instance table. They are
*documented but internal* (see [API stability](stability.md)): the words
in the rows are frozen, and these methods that return them may change in
a 1.x release. What each `action_type` means is on
[Analytics & Reporting](../analytics.md).

::: isocenter.persistence.SqliteStore
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false
      show_source: false
      members:
        - get_audit_summary
        - get_audit_errors
        - get_audit_losses
        - get_audit_declines
        - get_audit_scan_gaps
        - get_audit_drops
        - get_flattened_instances
