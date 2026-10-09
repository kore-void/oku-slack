"""OKÚ zpravodajství (world/news.py): persona -> real person matching with Czech declension, RSS + pplx news sources
(mocked network / CLI), news podnety, the news director (budget, guardrails, dry run vs live hand-off), the digest
(/api/world/news + file), porada/chatter topics, and the bridge side (headline + URL + generated reaction)."""
import json, pathlib, subprocess, threading, time, pytest
from oku_slack import chatter as bridge_chatter, handoff
from oku_slack.world import budget, chatter as wchatter, news, podnet, pplx, scheduler, service, tz
from test_world_porada import Clock, P, requests
from test_chatter_bridge import mk as bmk, CFG as BCFG

MON15 = P(2026, 10, 12, 15)
REPO = pathlib.Path(__file__).resolve().parents[1]
NEWS_TOML = """
[news]
enabled = true
react = true
reactions_per_day = 4
per_person_per_day = 1
reply_rate = 0.0
channel_gap_h = 3
[news.pplx]
enabled = true
times = ["09:15"]
[[news.feeds]]
outlet = "iROZHLAS"
url = "https://www.irozhlas.cz/rss/irozhlas"
[[news.feeds]]
outlet = "Deník N"
url = "https://denikn.cz/feed/"
[news.people.babis]
person = "Andrej Babiš"
enabled = true
terms = ["Babiš*"]
exclude = ["Babišová", "Babišové"]
pplx_query = "Andrej Babiš"
[news.people.bourak]
person = "Filip Turek"
enabled = true
terms = ["Filip* Turek", "Filip* Turk*"]
weak_terms = ["Turek", "Turka", "Turkovi"]
context = ["Motorist*", "ministr*"]
[news.people.alenka]
person = "Alena Schillerová"
enabled = true
terms = ["Schillerov*"]
[news.people.kalousek]
person = "Miroslav Kalousek"
enabled = false
terms = ["Kalousek", "Kalousk*"]
"""

def rfc(ts): import email.utils; return email.utils.formatdate(ts, usegmt=True)

def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description><pubDate>{rfc(p)}</pubDate></item>" for t, l, d, p in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode("utf-8")

def mk(tmp_path, world=None, toml=NEWS_TOML):
    tmp_path.mkdir(parents=True, exist_ok=True); cfg = tmp_path / "config.toml"; nt = tmp_path / "news.toml"; nt.write_text(toml, encoding="utf-8")
    w = {"dry_run": True, "porada_enabled": False, "chatter_enabled": False}; w.update(world or {})
    def val(v): return json.dumps(v) if not isinstance(v, bool) else ("true" if v else "false")
    cfg.write_text("[world]\n" + "".join(f"{k} = {val(v)}\n" for k, v in w.items()), encoding="utf-8")
    clk = Clock(MON15)
    svc = service.WorldService(config_path=cfg, logs_dir=tmp_path / "logs", source_logs=tmp_path / "src", clock=clk, ctx={}, news_config=nt)
    svc.startup(); return svc, clk

def feeds(now):
    return {"https://www.irozhlas.cz/rss/irozhlas": rss([
                ("Babiš jednal s Bruselem o rozpočtu", "https://www.irozhlas.cz/zpravy-domov/babis-brusel_2610121400_abc?utm_source=rss", "Premiér Andrej Babiš dnes.", now - 3600),
                ("Schillerová představila nový rozpočet", "https://www.irozhlas.cz/ekonomika/schillerova-rozpocet_1", "Ministryně financí.", now - 7200),
                ("Turek koupil kebab", "https://www.irozhlas.cz/zivotni-styl/turek-kebab_1", "Muž z Ankary.", now - 600),           # weak, no context
                ("Ministr Turek odmítl kritiku", "https://www.irozhlas.cz/zpravy-domov/turek-kritika_1", "Motoristé.", now - 1800),  # weak + context
                ("Počasí: zítra prší", "https://www.irozhlas.cz/pocasi_1", "Bez politiky.", now - 100),
                ("Starý článek o Babišovi", "https://www.irozhlas.cz/stary_1", "", now - 30 * 3600)]),
            "https://denikn.cz/feed/": rss([
                ("Babišova vláda a Schillerová: spor o daně", "https://denikn.cz/123456/spor-o-dane/", "", now - 5400),
                ("Rozhovor s Monikou Babišovou", "https://denikn.cz/999/rozhovor/", "Babišová promluvila.", now - 900),
                ("Babišovi hrozí soud", "https://denikn.cz/777/soud/", "", now - 300)])}

def fake_http(table):
    def get(url, timeout=15, max_bytes=0):
        if url not in table: raise OSError("unreachable")
        v = table[url]; return (v[0], v[1]) if isinstance(v, tuple) else (v, url)
    return get

def test_committed_news_toml_maps_all_six_personas():
    c = news.Config(REPO / "oku_slack" / "world" / "news.toml").get()
    assert set(c["people"]) == {"babis", "bourak", "peta", "kalousek", "marty", "alenka"}
    assert {k: p["person"] for k, p in c["people"].items()} == {"babis": "Andrej Babiš", "bourak": "Filip Turek", "peta": "Petr Macinka",
                                                                "kalousek": "Miroslav Kalousek", "marty": "Marek Prchal", "alenka": "Alena Schillerová"}
    assert all(p["enabled"] for p in c["people"].values()) and c["enabled"] and c["reactions_per_day"] == 10 and c["per_person_per_day"] == 2 and c["channel_gap_h"] == 1
    assert len(c["feeds"]) >= 10 and all(f["url"].startswith("https://") for f in c["feeds"]) and c["channel"] == "socky"
    assert c["hours"] == ["07:00", "22:00"] and c["rss_interval_min"] == 30 and c["pplx"]["enabled"] and len(c["pplx"]["times"]) == 2

def test_czech_declension_and_weak_terms():
    ppl = news.Config(REPO / "oku_slack" / "world" / "news.toml").get()["people"]
    def who(h, lede=""): return news.classify(h, lede, ppl)[0]
    for h in ("Babiš řekl", "Podle Babiše", "Babišovi se nelíbí", "s Babišem", "Babišova vláda", "Babišových ministrů"): assert who(h) == ["babis"], h
    assert who("Babišová otevřela školku") == [] and who("Rozhovor s Monikou Babišovou") == []          # wife: family topic
    assert who("Macinkovi došla trpělivost") == ["peta"] and who("Kalouskovi se to nelíbí") == ["kalousek"] and who("Kalousek") == ["kalousek"]
    assert who("Schillerové rozpočet") == ["alenka"] and who("se Schillerovou") == ["alenka"]
    assert who("Filip Turek promluvil") == ["bourak"] and who("Filipa Turka kritizují") == ["bourak"]
    assert who("Turek koupil kebab") == [] and who("Turka zadrželi", "na hranici") == [] and who("Ministr Turek odmítl") == ["bourak"]
    assert who("Prchal vyhrál maraton") == [] and who("Marek Prchal chystá kampaň") == ["marty"] and who("Prchal chystá kampaň ANO") == ["marty"]
    assert who("Bez jmen") == [] and who("babiš malými") == []

def test_parse_feed_rss_atom_and_no_dtd():
    items = news.parse_feed(rss([("A &amp; <![CDATA[<b>B</b>]]>", "https://x.cz/a", "<![CDATA[<p>Lede</p>]]>", MON15)]))
    assert items == [{"headline": "A & B", "link": "https://x.cz/a", "lede": "Lede", "published_at": MON15}]
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link rel="alternate" href="https://y.cz/t"/>'
            '<summary>S</summary><published>2026-10-12T13:00:00Z</published></entry></feed>').encode()
    assert news.parse_feed(atom)[0]["published_at"] == news.parse_date("2026-10-12T13:00:00Z") and news.parse_feed(atom)[0]["link"] == "https://y.cz/t"
    with pytest.raises(ValueError): news.parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>')
    assert news.parse_date("Fri, 09 Oct 2026 19:35:00 +0200") == news.parse_date("2026-10-09T17:35:00Z") and news.parse_date("nope") is None

def test_match_items_fresh_dedupe_and_merge():
    c = news.build({"news": {"people": {"babis": {"person": "Andrej Babiš", "enabled": True, "terms": ["Babiš*"]},
                                        "alenka": {"person": "Alena Schillerová", "enabled": True, "terms": ["Schillerov*"]}}}})
    e = [("A", {"headline": "Babiš a Schillerová", "link": "https://a.cz/1?utm_source=rss", "lede": "", "published_at": MON15 - 60}),
         ("B", {"headline": "Jiný titulek", "link": "https://www.a.cz/1", "lede": "Schillerová", "published_at": MON15 - 60}),
         ("A", {"headline": "Babiš včera", "link": "https://a.cz/2", "lede": "", "published_at": MON15 - 25 * 3600}),
         ("A", {"headline": "Jen v perexu", "link": "https://a.cz/3", "lede": "Babiš", "published_at": None})]
    h = news.match_items(e, c["people"], MON15, 24)
    assert set(h) == {"https://a.cz/1", "https://a.cz/3"} and h["https://a.cz/1"]["persons"] == ["babis", "alenka"]
    assert h["https://a.cz/1"]["persona"] == "babis" and h["https://a.cz/1"]["in_headline"] and not h["https://a.cz/3"]["in_headline"]

def news_rows(svc, t="podnet.received"): return [r for r in svc.diary.events(types=[t])]

def run_rss(svc, clk):
    src = svc.pollers["news"]; src.background = False; src.http = fake_http(feeds(clk.t))
    return src.run_rss(clk.t, svc.news_settings())

def test_rss_cycle_writes_news_podnety_and_generic_reactor_ignores_them(tmp_path):
    svc, clk = mk(tmp_path)
    r = run_rss(svc, clk)
    assert r["feeds_ok"] == 2 and r["matched"] == 5 and r["appended"] == 5
    lines = [json.loads(x) for x in svc.inbox_path.read_text(encoding="utf-8").splitlines()]
    assert all(l["kind"] == "news" and l["source"] == "news_rss" for l in lines)
    b = next(l for l in lines if "babis-brusel" in l["url"])
    assert b["url"] == "https://irozhlas.cz/zpravy-domov/babis-brusel_2610121400_abc" and b["outlet"] == "iROZHLAS" and b["persona"] == "babis"
    assert b["title"] == "Babiš jednal s Bruselem o rozpočtu" and b["person"] == "Andrej Babiš" and b["published_at"] == clk.t - 3600 and b["in_headline"]
    assert not any("kebab" in l["url"] or "pocasi" in l["url"] or "stary" in l["url"] or "rozhovor" in l["url"] for l in lines)
    svc.tick()
    rec = news_rows(svc); assert len(rec) == 5 and all(x["payload"]["kind"] == "news" for x in rec)
    assert any(x["payload"].get("lede") == "Premiér Andrej Babiš dnes." for x in rec) and all("text" not in x["payload"] for x in rec)
    assert not news_rows(svc, "podnet.dry_run") and not news_rows(svc, "podnet.skipped")      # generic reactor leaves news alone
    assert run_rss(svc, clk)["appended"] == 0                                                   # dedupe by URL against the diary
    st = svc.pollers["news"].feeds; assert all(v["ok"] for v in st.values())

def test_director_dry_run_budget_per_person_and_guard(tmp_path):
    svc, clk = mk(tmp_path, {"per_channel_gap_h": 3}); run_rss(svc, clk); svc.tick()
    dr = news_rows(svc, "news.dry_run"); sk = news_rows(svc, "news.skipped")
    assert len(dr) == 1 and dr[0]["actor"] == "bourak" and "turek-kritika" in dr[0]["payload"]["url"]    # freshest passing one
    # freshest headline-matched first: denikn 'Babišovi hrozí soud' (5 min) is guard-skipped, then 'Ministr Turek' etc.
    assert any(x["payload"]["reason"] == "guard:sensitive:soud" for x in sk)
    for _ in range(6): clk.t += 60; svc.tick()
    dr = news_rows(svc, "news.dry_run"); who = [x["actor"] for x in dr]
    assert sorted(who) == ["alenka", "babis", "bourak"] and len(set(who)) == len(who)          # 1 per person per day
    p = dr[0]["payload"]; assert p["channel_name"] == "socky" and p["channel"] == wchatter.CHANNELS["socky"] and p["opener"].startswith(p["url"])
    assert not requests(svc)                                                                     # dry run: no hand-off
    assert svc.budget_view()["news"]["reactions_today"] == 3

def test_director_daily_cap_and_quiet_hours(tmp_path):
    svc, clk = mk(tmp_path); c = svc.news_settings()
    nt = tmp_path / "news.toml"; nt.write_text(NEWS_TOML.replace("reactions_per_day = 4", "reactions_per_day = 1"), encoding="utf-8")
    time.sleep(0.01); import os; os.utime(nt, (time.time() + 5, time.time() + 5))
    run_rss(svc, clk)
    for _ in range(5): clk.t += 60; svc.tick()
    assert len(news_rows(svc, "news.dry_run")) == 1
    svc2, clk2 = mk(tmp_path / "q"); clk2.t = P(2026, 10, 12, 22, 30)
    src = svc2.pollers["news"]; src.http = fake_http(feeds(clk2.t)); src.background = False
    src.run_rss(clk2.t, svc2.news_settings()); svc2.tick()
    assert not news_rows(svc2, "news.dry_run") and not news_rows(svc2, "budget.denied")       # quiet hours: waits silently

def test_live_handoff_ack_and_budget(tmp_path):
    svc, clk = mk(tmp_path, {"dry_run": False}); run_rss(svc, clk); svc.tick()
    reqs = requests(svc); assert len(reqs) == 1
    r = reqs[0]; assert r["kind"] == "chatter" and r["storylet"] == "NEWS" and r["podnet"]["kind"] == "news" and r["news"]["generate"] is True
    assert r["opener"].startswith(r["podnet"]["url"]) and r["news"]["headline"] and r["news"]["outlet"] and len(r["personas"]) == 1
    row = news_rows(svc, "news.requested")[0]; assert row["payload"]["llm_calls"] == 1 and row["payload"]["top_level"] is True
    u = budget.usage(svc.diary.events(), clk.t, svc.settings()); assert u["posts_today"] == 1 and u["llm_today"] == 1
    clk.t += 30; svc.tick(); assert len(requests(svc)) == 1                                         # one at a time while pending
    handoff._append(handoff.ACKS, {"id": r["id"], "status": "started", "thread_ts": "1.2", "reaction": "llm"}, svc.outbox_dir)
    handoff._append(handoff.ACKS, {"id": r["id"], "status": "done", "turns": 1, "llm_calls": 1}, svc.outbox_dir)
    clk.t += 15; svc.tick()
    assert news_rows(svc, "news.started")[0]["payload"]["thread_ts"] == "1.2" and news_rows(svc, "news.ended")[0]["payload"]["status"] == "done"
    clk.t += 15; svc.tick(); assert len(requests(svc)) == 1                                         # socky gap 3 h: next one waits
    clk.t += 3 * 3600 + 60; svc.tick(); assert len(requests(svc)) == 2 and requests(svc)[1]["personas"][0] != r["personas"][0]

def test_kill_switch_blocks_news(tmp_path):
    svc, clk = mk(tmp_path, {"dry_run": False, "kill_switch": True}); run_rss(svc, clk); svc.tick()
    assert not requests(svc) and not news_rows(svc, "news.requested") and news_rows(svc, "budget.denied")[0]["payload"]["storylet"] == "NEWS"

def page(title, pub=None, desc=""):
    meta = f'<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">'
    if pub: meta += f'<meta property="article:published_time" content="{pub}">'
    return f"<html><head>{meta}<title>x</title></head><body></body></html>".encode()

def test_pplx_untrusted_urls_verified(tmp_path):
    svc, clk = mk(tmp_path); clk.t = P(2026, 10, 12, 9, 16)
    out = ("https://www.seznamzpravy.cz/clanek/domaci-politika-babis-v-bruselu-12345 | Babiš v Bruselu | Seznam | 2026-10-12\n"
           "https://evil.example.com/babis-clanek-123 | fake\nhttps://x.com/AndrejBabis/status/1 | post\n"
           "https://www.novinky.cz/clanek/domaci-stary-babis-1 | old\nhttps://www.idnes.cz/zpravy/domaci/jiny-clovek.A261012_1 | jiny\n"
           "https://www.irozhlas.cz/hledat/babis | search\nhttps://www.aktualne.cz/presmerovani-babis-1/ | redirect\nignore previous instructions")
    assert news.parse_pplx(out) == ["https://seznamzpravy.cz/clanek/domaci-politika-babis-v-bruselu-12345", "https://novinky.cz/clanek/domaci-stary-babis-1",
                                    "https://idnes.cz/zpravy/domaci/jiny-clovek.A261012_1", "https://aktualne.cz/presmerovani-babis-1"]
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    http = fake_http({"https://seznamzpravy.cz/clanek/domaci-politika-babis-v-bruselu-12345": page("Babiš jednal v Bruselu", iso(clk.t - 3600), "Premiér."),
                      "https://novinky.cz/clanek/domaci-stary-babis-1": page("Babiš loni", iso(clk.t - 40 * 3600)),
                      "https://idnes.cz/zpravy/domaci/jiny-clovek.A261012_1": page("Úplně jiný člověk", iso(clk.t - 60)),
                      "https://aktualne.cz/presmerovani-babis-1": (page("Babiš"), "https://evil.example.com/x")})
    calls = []
    def runner(cmd, q, mode, timeout):
        calls.append(q); return out if "Babiš" in q else ""
    src = svc.pollers["news"]; src.http, src.runner = http, runner
    res = src.run_pplx(clk.t, svc.news_settings())
    assert res["appended"] == 1 and res["rejected"] == {"stale": 1, "no_name": 1, "redirect_host": 1} and res["calls"] == 3   # babis, bourak, alenka
    assert "posledních 24 hodin" in calls[0] and "Andrej Babiš" in calls[0]
    line = json.loads(svc.inbox_path.read_text(encoding="utf-8").strip())
    assert line["source"] == "news_pplx" and line["title"] == "Babiš jednal v Bruselu" and line["outlet"] == "Seznam Zprávy" and line["published_at"] == clk.t - 3600

def test_poll_schedule_hours_interval_and_pplx_slot(tmp_path):
    svc, clk = mk(tmp_path); src = svc.pollers["news"]; ran = []
    src.run_rss = lambda now, c: ran.append(("rss", now)); src.run_pplx = lambda now, c: ran.append(("pplx", now)); src.background = False
    def start(fn, now, c, flag, force): fn(now, c); return "ran"
    src._start = start
    clk.t = P(2026, 10, 12, 6, 30); src.poll(); assert ran == []                                       # before 07:00
    clk.t = P(2026, 10, 12, 7, 1); src.poll(); clk.t += 600; src.poll(); assert [x[0] for x in ran] == ["rss"]
    clk.t = P(2026, 10, 12, 9, 16); src.poll(); assert [x[0] for x in ran] == ["rss", "rss", "pplx"]
    clk.t += 60; src.poll(); assert [x[0] for x in ran].count("pplx") == 1                              # one run per slot
    clk.t = P(2026, 10, 12, 22, 5); n = len(ran); src.poll(); assert len(ran) == n                      # after 22:00

def test_cli_slots_serialise_pplx_calls():
    live, peak = [0], [0]; lock = threading.Lock()
    def fake(cmd, q, mode, t):
        with lock: live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock: live[0] -= 1
        return ""
    ts = [threading.Thread(target=pplx.slot_call, args=(fake, "x", "q", "fast", 1)) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join(5)
    assert peak[0] == 1 and pplx.run_cli.__name__ != "run_cli" or peak[0] == 1

def test_digest_api_and_file(tmp_path):
    svc, clk = mk(tmp_path); run_rss(svc, clk); svc.tick()
    dg = svc.api_news({}); assert dg["title"] == "OKÚ zpravodajství" and dg["date"] == "2026-10-12" and dg["total"] == 5
    per = {p["persona"]: p for p in dg["people"]}
    assert per["babis"]["count"] == 3 and per["alenka"]["count"] == 2 and per["bourak"]["count"] == 1 and not per["kalousek"]["enabled"]
    assert per["babis"]["items"][0]["headline"] == "Babišovi hrozí soud" and dg["budget"]["reactions_per_day"] == 4
    md = svc.api_news({"format": "md"})["markdown"]; assert md.startswith("# OKÚ zpravodajství 2026-10-12") and "Miroslav Kalousek (kalousek): vypnuto" in md
    assert svc.api_news({"date": "bad"}) is None and svc.api_news({"date": "2026-10-11"})["total"] == 0
    f = tmp_path / "logs" / "news" / "zpravodajstvi-2026-10-12.md"; assert f.exists() and "Schillerová představila" in f.read_text(encoding="utf-8")
    h = svc.news_headline("babis"); assert h["headline"] == "Babiš jednal s Bruselem o rozpočtu"          # soud headline is guarded out
    assert svc.health()["news"]["feeds_ok"] == 2

def test_porada_topic_and_chatter_storylet_use_news():
    nh = {"headline": "Babiš jednal s Bruselem", "outlet": "iROZHLAS"}
    kinds = {scheduler.topic_for({}, f"2026-10-{d:02d} 10:00", news=nh)[0] for d in range(1, 29)}
    assert "news" in kinds and "news" not in {scheduler.topic_for({}, f"2026-10-{d:02d} 10:00")[0] for d in range(1, 29)}
    k, topic, opener = next(scheduler.topic_for({}, f"2026-10-{d:02d} 10:00", news=nh) for d in range(1, 29) if scheduler.topic_for({}, f"2026-10-{d:02d} 10:00", news=nh)[0] == "news")
    assert "Babiš jednal s Bruselem." in topic and "iROZHLAS" in opener
    ctx, have = wchatter.facts({}, news=nh); assert "news_today" in have
    picks = {wchatter.pick(f"s{i}", ctx, have)["storylet"] for i in range(80)}; assert "ZPRAVY_DNE" in picks
    for o in wchatter.BY_KEY["ZPRAVY_DNE"]["openers"]: assert bridge_chatter.guard_hit(o.format(**ctx)) is None

# ---------------- bridge side ----------------
URL = "https://irozhlas.cz/zpravy-domov/babis-brusel_1"
def nreq(**kw):
    r = {"id": "n1", "kind": "chatter", "source": "world:chatter", "channel": "C0C76ATGLAH", "thread_ts": None, "personas": ["babis"],
         "topic": "Článek (iROZHLAS): Babiš jednal", "opener": URL + "\nZase o mně píšou. Kolegové, na poradě k tomu chci čísla.", "turns": 1,
         "storylet": "NEWS", "podnet": {"url": URL, "kind": "news", "key": "podnet:n"},
         "news": {"headline": "Babiš jednal s Bruselem <o> rozpočtu & dluhu", "outlet": "iROZHLAS", "lede": "Premiér dnes.", "person": "Andrej Babiš", "generate": True}}
    r.update(kw); return r

def test_bridge_news_reaction_headline_url_generated_line():
    c, slack, seen = bmk(gen=lambda s, h: "Brusel mi zase závidí, makáme dál. https://spam.example")
    res = c.start_chatter(nreq(), sleep=lambda d: None); c.chatter_thread.join(5)
    assert res[0] == "started" and res[1]["reaction"] == "llm" and len(slack.posts) == 1
    t = slack.posts[0]["text"]
    assert t == f"*Babiš jednal s Bruselem &lt;o&gt; rozpočtu &amp; dluhu* (iROZHLAS)\n{URL}\nBrusel mi zase závidí, makáme dál."
    assert bridge_chatter.NEWS_RULES in seen[0][0] and "TITULEK: Babiš jednal" in seen[0][1] and "PEREX: Premiér dnes." in seen[0][1]
    assert handoff.acks_for("n1")[-1]["llm_calls"] == 1

def test_bridge_news_guard_fallback_and_reply_rules():
    c, slack, seen = bmk(gen=lambda s, h: "Za tohle by měl rozhodnout soud." if "TITULEK" in h[-1]["content"] else "Já za to nemůžu.")
    r = nreq(id="n2", personas=["babis", "kalousek"], turns=2)
    res = c.start_chatter(r, sleep=lambda d: None); c.chatter_thread.join(5)
    assert res[1]["reaction"] == "template" and slack.posts[0]["text"].endswith("\nZase o mně píšou. Kolegové, na poradě k tomu chci čísla.")
    assert [p["user"] for p in slack.posts] == ["UBABIS", "UKALOUSEK"] and bridge_chatter.NEWS_REPLY_RULES in seen[-1][0]
    assert bridge_chatter.PODNET_RULES not in seen[-1][0]
    with pytest.raises(ValueError): bridge_chatter.validate(BCFG, nreq(news={"headline": " "}), {"babis": 1})
    assert bridge_chatter.validate(BCFG, nreq(), {"babis": 1}) == (["babis"], 1)

def test_old_style_podnet_without_news_unchanged():
    c, slack, seen = bmk()
    r = nreq(id="n3"); r.pop("news")
    c.start_chatter(r, sleep=lambda d: None); c.chatter_thread.join(5)
    assert slack.posts[0]["text"] == URL + " Zase o mně píšou. Kolegové, na poradě k tomu chci čísla." and seen == []
