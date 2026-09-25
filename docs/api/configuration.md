# Configuration API

A session's configuration is `session.configuration`, an
`IsocenterConfiguration`. `load_config()` fills it from a YAML file;
the methods below change it in memory and write it back with `save()`.
The file format is on [Configuration](../configuration.md). The class,
its fields and these six methods are frozen at 1.0
([API stability](stability.md#frozen-at-10)).

::: isocenter.configuration.IsocenterConfiguration
    handler: python
    options:
      show_root_full_path: false
      members:
        - save
        - add_rule
        - update_rule
        - delete_rule
        - set_phi_tag
        - get_rule
