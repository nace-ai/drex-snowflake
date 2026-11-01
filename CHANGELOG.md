# Changelog

## 0.2.0 (2026-11-01)

The first public release.

- Five functions in `DREX.PUBLIC`: `drex_noul`, `drex_choice`, `drex_score`, `drex_decide` and `drex_decide_image`,
  vectorized Python UDFs on Python 3.12 that call Drex through an external access integration.
- `VARCHAR` overloads for text input, and for a single image URL in `drex_decide_image`.
- `drex_decide` and `drex_decide_image` return `OBJECT`, so a row without an answer is SQL NULL.
- Optional OpenRouter routing, with the app attribution set by the `drex_openrouter_referer` and
  `drex_openrouter_title` secrets.
- Requests identify themselves as `drex-snowflake/0.2.0` in `X-Nace-Client`.

To upgrade an installation, check out the release and run `sql/setup.sql` again ([docs/INSTALL.md](docs/INSTALL.md)).
