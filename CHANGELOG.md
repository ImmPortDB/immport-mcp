# Changelog

## 1.4.0

First public release on PyPI (`uvx immport-mcp`).

- Several studies per call (up to 20, comma-separated) for `get_study_data`,
  `get_study_data_availability` and the study file manifest.
- `get_release_notes`: data releases, the notes published for a release, and each study's release history.
- `get_data_model`: tables of the ImmPort database with columns, keys, joins and indexes (no API key needed).
- Usage metrics can be turned off with `IMMPORT_METRICS_ENABLED=false`.
