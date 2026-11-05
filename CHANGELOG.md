# Changelog

## Unreleased

- Redesigned web UI: status badge, key figures (open findings, power measured now, learning phase), findings as
  cards with a "Was ok" button, devices as tiles with a power bar (problems first, then by power), collapsible
  notification timeline, report chips. Light/dark, two columns on phones, reloads every minute while visible.

## 1.1.0 – 2026-11-05

- English and German for notifications, web UI, monthly report, HA sensor states and CLI (`language: en | de`, default `en`).
- English configuration keys and device types (`devices`, `type: cycle`, `power`, …); the German keys of 1.0.0 keep working.
- Plugs that only send no fresh values while still connected are no longer pushed (`monitoring.notify_no_data: false`);
  a push comes when a plug has been `unavailable`/`unknown` for 30 minutes (`unavailable_after: 30m`, was 15m).
- Grafana dashboard in English (`grafana/power-hog-detective.json`), German variant via `--lang de`.

## 1.0.0 – 2026-11-05

- First release: per-plug learning (cycle, continuous, program, usage, charger, schedule devices), plain-language
  findings, live checks (runs too long, no consumption, plug unavailable, entity renamed/missing, HA down),
  "Was ok" feedback (phone button, HA script, web UI, CLI), HA sensors, Prometheus metrics, Grafana dashboard,
  monthly HTML report with top-5 savings, SQLite with tiered retention, Docker Compose deployment.
