"""OKÚ World core (docs/ECOSYSTEM-PLAN.md, docs/WORLD.md): the `oku_world` service (python -m oku_slack.world.service,
loopback 127.0.0.1:8798) owns the append-only diary (logs/world.sqlite3 + world.jsonl), the read-only projection,
budgets/quiet hours/kill switch and the storylet scheduler (standalone weekday porada). Event sources (wheel outbox,
usage.jsonl, schedule, ...) only hand it diary drafts. The wheel is one optional source at the edge."""
