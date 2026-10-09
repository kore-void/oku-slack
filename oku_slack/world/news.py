"""OKÚ zpravodajství: real current news ABOUT the real people behind the OKÚ personas, and the personas' reactions.

Kore: "I want news: what is being written about them, and their reactions." Config: oku_slack/world/news.toml
(env OKU_NEWS_CONFIG), re-read on change. Stdlib only.

Sources (both only APPEND podnety kind="news" to the podnet inbox, source news_rss / news_pplx; the inbox tail
validates and dedupes them BY URL like every other podnet, see podnet.py):
- RSS (`NewsSource.run_rss`): every rss_interval_min (45) inside `hours` (07:00-22:00 Prague) it reads the Czech news
  feeds of [[news.feeds]] and matches each item's headline + lede against every enabled person's terms (Czech
  declension via `stem*`, weak surname-only terms need a context word, exclusions). Only items published within
  max_age_h (24 h, pubDate) are kept.
- pplx (`NewsSource.run_pplx`): at `[news.pplx] times` (09:15, 17:45) one `void-pplx-ask` call per enabled person, one
  after another (pplx.CLI_SLOTS: never two pplx calls at once, shared with the podnet pplx poller). The answer is
  UNTRUSTED DATA: only URLs are taken, the host must be a known news domain (NEWS_HOSTS), the article path non-trivial,
  and with verify=true each article is fetched: the final host must still be a news domain, the page title (og:title)
  must name the person, and a published time older than max_age_h drops it. Headline/lede come from the page, never
  from the pplx text.

Reaction (`NewsDirector`, one at a time): the freshest headline-matched article (published within react_max_age_h) of
a person who has not had a reaction today -> the matching persona shares "*headline* (outlet)\\nURL\\n<reaction>" in
the person's channel (default #oku-socky); the bridge generates the 1-2 sentence in-character reaction from the
headline + lede only (oku_slack/chatter.py NEWS_RULES) and falls back to a template line. With reply_rate one other
persona replies once. Guardrails: a headline/lede touching health/family/crime (guard.py) -> `news.skipped`.
Budget: the world budget (posts/day, per-channel gap = [news] channel_gap_h, LLM cap, quiet hours, wheel live, kill
switch) PLUS reactions_per_day (4) and per_person_per_day (1). dry_run -> `news.dry_run` only; live -> a kind=chatter
hand-off line (storylet NEWS, podnet + news payload) + `news.requested`; acks -> news.started / news.ended / news.failed.

Digest: `digest()` = today's matched headlines per person ("OKÚ zpravodajství"), served at GET /api/world/news
(?date=YYYY-MM-DD, &format=md) and written to <logs>/news/zpravodajstvi-<date>.md after each fetch. `headline_for()`
feeds the porada topic and the ZPRAVY_DNE chatter storylet.

CLI: python -m oku_slack.world.news fetch [--dry] [--pplx] | digest [--date YYYY-MM-DD] [--md]"""
import argparse, datetime, email.utils, hashlib, html, json, logging, os, pathlib, random, re, subprocess, sys, threading, time, tomllib
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from . import budget, guard, podnet, pplx, scheduler, state, tz, xsource
from .. import handoff

log = logging.getLogger("oku_world.news")
CONFIG = pathlib.Path(__file__).with_name("news.toml")
DEFAULTS = {"enabled": False, "react": True, "hours": ["07:00", "22:00"], "rss_interval_min": 45, "max_age_h": 24,
            "react_max_age_h": 6, "react_requires_headline": True, "reactions_per_day": 4, "per_person_per_day": 1,
            "reply_rate": 0.5, "channel": "socky", "channel_gap_h": 3.0, "fetch_timeout_s": 15, "max_new_per_cycle": 60,
            "ack_timeout_s": 120, "done_timeout_s": 600}
PPLX_DEFAULTS = {"enabled": False, "times": ["09:15", "17:45"], "mode": "fast", "timeout_s": 150, "command": "void-pplx-ask",
                 "max_per_person": 3, "verify": True}
FEEDER = ("news_rss", "news_pplx")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OKU-zpravodajstvi/1.0"
MAX_FEED_BYTES, MAX_PAGE_BYTES = 3_000_000, 2_000_000
NEWS_HOSTS = ("irozhlas.cz", "rozhlas.cz", "seznamzpravy.cz", "novinky.cz", "idnes.cz", "lidovky.cz", "aktualne.cz", "denikn.cz",
              "ceskatelevize.cz", "echo24.cz", "denik.cz", "hn.cz", "ihned.cz", "ceskenoviny.cz", "e15.cz", "forum24.cz", "info.cz",
              "respekt.cz", "reflex.cz", "blesk.cz", "expres.cz", "tn.nova.cz", "cnn.iprima.cz", "iprima.cz", "euro.cz", "denikreferendum.cz",
              "eurozpravy.cz", "ctk.cz", "reuters.com", "apnews.com", "politico.eu", "bbc.com", "bbc.co.uk", "euractiv.com", "euractiv.cz",
              "theguardian.com", "ft.com", "bloomberg.com", "dw.com", "radio.cz", "sme.sk", "dennikn.sk", "aktuality.sk", "pravda.sk")
OUTLETS = {"irozhlas.cz": "iROZHLAS", "seznamzpravy.cz": "Seznam Zprávy", "novinky.cz": "Novinky.cz", "idnes.cz": "iDNES.cz",
           "lidovky.cz": "Lidovky.cz", "aktualne.cz": "Aktuálně.cz", "denikn.cz": "Deník N", "ceskatelevize.cz": "ČT24", "echo24.cz": "Echo24",
           "denik.cz": "Deník.cz", "hn.cz": "Hospodářské noviny", "ihned.cz": "Hospodářské noviny", "ceskenoviny.cz": "ČTK / České noviny",
           "e15.cz": "E15", "forum24.cz": "Forum24", "respekt.cz": "Respekt", "reflex.cz": "Reflex", "blesk.cz": "Blesk",
           "cnn.iprima.cz": "CNN Prima News", "tn.nova.cz": "TN.cz", "reuters.com": "Reuters", "politico.eu": "Politico"}
REPLIERS = {"babis": ["kalousek", "marty"], "bourak": ["peta", "kalousek"], "peta": ["bourak", "kalousek"], "kalousek": ["babis", "bourak"],
            "marty": ["babis", "kalousek"], "alenka": ["babis", "kalousek"]}
# template line if the bridge cannot generate a reaction (and the text an old bridge would post): nothing about the article
COMMENTS = {"babis": "Zase o mně píšou. Kolegové, na poradě k tomu chci čísla.",
            "kalousek": "Čtu to a hned říkám: já za to nemůžu.",
            "bourak": "Zase jsem ve zprávách. Bourák jede, ať to vidí celé OKÚ.",
            "peta": "Miláčci, zase o mně píšou. Sdílím, ať to vidí celé disko.",
            "marty": "Tohle stříhám rovnou na reels. Kdo dá první lajk?",
            "alenka": "Zase o mně píšou. Hlavně ať jsou u toho hranolky."}
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`|]+")
ATOM, DC = "{http://www.w3.org/2005/Atom}", "{http://purl.org/dc/elements/1.1/}"

# ---------------- config ----------------
def term_re(t):
    out = []
    for part in re.split(r"(\*| )", t.strip()):
        if part == "*": out.append(r"\w{0,6}")
        elif part == " ": out.append(r"\s+")
        elif part: out.append(re.escape(part))
    return "".join(out)

def compile_terms(terms):
    ts = [t for t in (terms or []) if isinstance(t, str) and t.strip()]
    # a term written in lower case matches any case ("ministr*" also matches "Ministr"); capitalised terms are exact
    alts = [("(?i:" + term_re(t) + ")") if t.strip()[:1].islower() else term_re(t) for t in ts]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)") if ts else None

def compile_people(raw):
    out = {}
    for key, p in (raw or {}).items():
        if not isinstance(p, dict): continue
        persona = p.get("persona") or key
        if persona not in state.PERSONAS: log.warning("news: unknown persona %s ignored", persona); continue
        out[persona] = {"persona": persona, "person": str(p.get("person") or persona)[:64], "enabled": bool(p.get("enabled", False)),
                        "terms": compile_terms(p.get("terms")), "weak": compile_terms(p.get("weak_terms")),
                        "context": compile_terms(p.get("context")), "exclude": compile_terms(p.get("exclude")),
                        "pplx_query": str(p.get("pplx_query") or p.get("person") or "")[:120], "channel": p.get("channel"),
                        "repliers": [r for r in (p.get("repliers") or REPLIERS.get(persona, [])) if r in state.PERSONAS and r != persona]}
    return out

def build(raw):
    n = (raw or {}).get("news") or {}
    c = dict(DEFAULTS); c.update({k: v for k, v in n.items() if k not in ("pplx", "feeds", "people")})
    c["pplx"] = dict(PPLX_DEFAULTS, **(n.get("pplx") or {}))
    c["feeds"] = [{"url": f["url"], "outlet": str(f.get("outlet") or host_of(f["url"]))[:64]} for f in (n.get("feeds") or [])
                  if isinstance(f, dict) and str(f.get("url", "")).startswith("https://")]
    c["people"] = compile_people(n.get("people"))
    return c

class Config:
    """news.toml, re-read when it changes; a broken file keeps the last good config (error in /healthz)."""
    def __init__(self, path=None):
        self.path = pathlib.Path(path or os.environ.get("OKU_NEWS_CONFIG") or CONFIG)
        self._c, self._m, self.error = None, None, None
    def get(self):
        try: m = self.path.stat().st_mtime
        except OSError: m = None
        if self._c is None or m != self._m:
            self._m = m
            try:
                with open(self.path, "rb") as f: self._c = build(tomllib.load(f)); self.error = None
            except FileNotFoundError: self._c = self._c or build({}); self.error = "missing"
            except (OSError, tomllib.TOMLDecodeError, re.error, ValueError) as e:
                self.error = type(e).__name__; log.warning("news.toml unreadable: %s", self.error); self._c = self._c or build({})
        return self._c

# ---------------- matching ----------------
def matches(p, text, ctx=None):
    """Person config p matches text (headline, or headline + lede). Weak terms need a context word in ctx (or text)."""
    if not p.get("enabled") or not text: return False
    if p["exclude"] and p["exclude"].search(ctx or text): return False
    if p["terms"] and p["terms"].search(text): return True
    return bool(p["weak"] and p["context"] and p["weak"].search(text) and p["context"].search(ctx or text))

def classify(headline, lede, people):
    """-> (personas named in the headline, personas named in headline or lede), config order."""
    both = f"{headline}\n{lede or ''}"
    head = [k for k, p in people.items() if matches(p, headline, both)]
    anyp = [k for k, p in people.items() if matches(p, both)]
    return head, anyp

def sensitive(text):
    """Health/family/crime words (Czech + English) in a headline/lede; quotes are allowed (the outlet's real headline)."""
    t = text or ""
    m = guard.SENSITIVE_RE.search(t) or guard.SENSITIVE_EN_RE.search(t)
    return ("sensitive:" + m.group(1).lower()) if m else None

# ---------------- parsing ----------------
def clean_text(s, n):
    t = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t); t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= n: return t
    cut = t.rfind(" ", 0, n - 1)
    return t[: cut if cut > n // 2 else n - 1].rstrip(" ,;:") + "…"

def parse_date(s):
    s = (s or "").strip()
    if not s: return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try: d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError: return None
    if d is None: return None
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()

def _t(e):
    return "".join(e.itertext()).strip() if e is not None else ""

def parse_feed(data):
    """RSS 2.0 / Atom bytes -> [{"headline", "link", "lede", "published_at"}]. Untrusted input: no DTD/entities."""
    if not data: return []
    head = data[:2000].lower()
    if b"<!entity" in head or b"<!doctype" in head: raise ValueError("dtd_not_allowed")
    root = ET.fromstring(data); out = []
    for it in root.iter("item"):
        link = _t(it.find("link"))
        g = it.find("guid")
        if not link and g is not None and (g.get("isPermaLink") or "true") != "false": link = _t(g)
        out.append({"headline": clean_text(_t(it.find("title")), 280), "link": link, "lede": clean_text(_t(it.find("description")), 300),
                    "published_at": parse_date(_t(it.find("pubDate")) or _t(it.find(DC + "date")))})
    for e in root.iter(ATOM + "entry"):
        link = next((l.get("href") for l in e.findall(ATOM + "link") if (l.get("rel") or "alternate") == "alternate" and l.get("href")), "")
        out.append({"headline": clean_text(_t(e.find(ATOM + "title")), 280), "link": link,
                    "lede": clean_text(_t(e.find(ATOM + "summary")) or _t(e.find(ATOM + "content")), 300),
                    "published_at": parse_date(_t(e.find(ATOM + "published")) or _t(e.find(ATOM + "updated")))})
    return [x for x in out if x["headline"] and x["link"]]

def host_of(url):
    h = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h

def news_host(host):
    h = (host or "").lower().rstrip(".")
    return next((a for a in NEWS_HOSTS if h == a or h.endswith("." + a)), None)

def outlet_for(url, default=None):
    a = news_host(host_of(url))
    return default or OUTLETS.get(a) or a or host_of(url)

def match_items(entries, people, now, max_age_h=24):
    """[(outlet, parsed item)] -> {normalised url: hit}; one hit per URL (persons merged), only fresh items naming someone."""
    hits = {}
    for outlet, it in entries:
        pub = it.get("published_at")
        if pub is not None and (pub < now - max_age_h * 3600 or pub > now + 3600): continue
        head, anyp = classify(it["headline"], it.get("lede"), people)
        if not anyp: continue
        try: url = podnet.normalize_url(it["link"])
        except podnet.Invalid: continue
        if url in hits:
            h = hits[url]; h["persons"] = list(dict.fromkeys(h["persons"] + anyp)); continue
        hits[url] = {"url": url, "outlet": outlet, "headline": it["headline"], "lede": it.get("lede") or "", "published_at": pub,
                     "persona": (head or anyp)[0], "persons": anyp, "in_headline": bool(head),
                     "person": people[(head or anyp)[0]]["person"]}
    return hits

# ---------------- network (tests mock http_get) ----------------
def http_get(url, timeout=15, max_bytes=MAX_FEED_BYTES):
    """GET -> (bytes, final_url). Public https/http only, size-capped."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes + 1)[:max_bytes], r.geturl()

META_RE = re.compile(r"<meta\s[^>]*>", re.I)
def _meta(page, names):
    for m in META_RE.finditer(page):
        tag = m.group(0)
        k = re.search(r"(?:property|name|itemprop)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        v = re.search(r"content\s*=\s*\"([^\"]*)\"|content\s*=\s*'([^']*)'", tag, re.I)
        if k and v and k.group(1).lower() in names: return v.group(1) if v.group(1) is not None else v.group(2)
    return None

def page_info(data):
    """Article HTML -> {"headline", "lede", "published_at", "site"} from og:/article: meta, JSON-LD, <title>, <time>."""
    page = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    page = page[:MAX_PAGE_BYTES]
    title = _meta(page, ("og:title", "twitter:title")) or (re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S) or [None, ""])[1]
    lede = _meta(page, ("og:description", "description", "twitter:description")) or ""
    pub = _meta(page, ("article:published_time", "datepublished", "pubdate", "date", "dc.date.issued", "publish-date"))
    if not pub:
        m = re.search(r"\"datePublished\"\s*:\s*\"([^\"]{8,40})\"", page) or re.search(r"<time[^>]+datetime=\"([^\"]{8,40})\"", page, re.I)
        pub = m.group(1) if m else None
    return {"headline": clean_text(title, 280), "lede": clean_text(lede, 300), "published_at": parse_date(pub) if pub else None,
            "site": clean_text(_meta(page, ("og:site_name",)) or "", 64)}

def pplx_question(person):
    return (f"Zprávy z posledních 24 hodin o osobě {person}: najdi zpravodajské články (česká i zahraniční média), které o ní "
            "vyšly za posledních 24 hodin. Pro každý článek vypiš přesně jeden řádek: URL | titulek | médium | datum a čas publikace "
            "(ISO 8601). Jen přímé URL článků (žádné rubriky, vyhledávání ani sociální sítě), nejvýš 6 řádků, nic jiného.")

def parse_pplx(output):
    """pplx answer (UNTRUSTED) -> [normalised article URLs on news domains]; nothing else is taken from it. Pure."""
    out = []
    for line in str(output or "").splitlines()[:300]:
        for raw in URL_RE.findall(line)[:6]:
            raw = raw.rstrip(".,;:!?)]}*_")
            try: url = podnet.normalize_url(raw)
            except podnet.Invalid: continue
            p = urllib.parse.urlsplit(url)
            if not news_host(p.hostname) or len(p.path.strip("/")) < 8 or url in out: continue
            if re.search(r"(?i)/(hledat|search|tag|tema|rubrika|autor|author)(/|$)", p.path): continue
            out.append(url)
    return out[:20]

# ---------------- digest ----------------
def is_news(r):
    pl = r.get("payload") or {}
    return r["type"] == "podnet.received" and pl.get("kind") == "news" and pl.get("source") in FEEDER and not pl.get("test")

def _day_bounds(now, zone, date=None):
    if date: d = datetime.date.fromisoformat(date)
    else: l = tz.local(now, zone); d = datetime.date(l.year, l.month, l.day)
    n = d + datetime.timedelta(days=1)
    return tz.to_ts(d.year, d.month, d.day, 0, 0, zone), tz.to_ts(n.year, n.month, n.day, 0, 0, zone), d.isoformat()

def digest(diary, now, cfg, zone=tz.PRAGUE, date=None):
    """Today's (or date's) matched headlines per person + reactions. Pure over diary rows."""
    day0, day1, ds = _day_bounds(now, zone, date)
    rows = diary.events(since_ts=day0 - 2 * 86400)
    acts = {}
    for r in rows:
        if r["type"].startswith("news.") and day0 <= r["ts"] < day1: acts.setdefault(r["subject"], []).append(r)
    people = cfg.get("people") or {}
    per = {k: {"persona": k, "person": p["person"], "enabled": p["enabled"], "items": []} for k, p in people.items()}
    seen = set(); reactions = []
    for r in rows:
        if not is_news(r) or r["subject"] in seen: continue
        pl = r["payload"]; t = pl.get("published_at") or r["ts"]
        if not day0 <= t < day1: continue
        seen.add(r["subject"])
        a = [x["type"] for x in acts.get(r["subject"], [])]
        reacted = "news.started" in a or "news.requested" in a
        item = {"headline": pl.get("title"), "outlet": pl.get("outlet"), "url": pl.get("url"), "published_at": pl.get("published_at"),
                "time": "%02d:%02d" % (tz.local(t, zone).hour, tz.local(t, zone).minute), "in_headline": pl.get("in_headline", True),
                "feeder": pl.get("source"), "key": r["subject"], "reacted": reacted, "dry_run": "news.dry_run" in a,
                "skipped": next(((x["payload"] or {}).get("reason") for x in acts.get(r["subject"], []) if x["type"] == "news.skipped"), None)}
        for k in pl.get("persons") or [pl.get("persona")]:
            if k in per: per[k]["items"].append(item)
    for k in per: per[k]["items"].sort(key=lambda x: -(x["published_at"] or 0)); per[k]["count"] = len(per[k]["items"])
    for subj, xs in acts.items():
        for x in xs:
            if x["type"] in ("news.requested", "news.dry_run", "news.started", "news.ended", "news.failed"):
                p = x["payload"] or {}
                reactions.append({"type": x["type"], "persona": x.get("actor"), "headline": p.get("headline"), "url": p.get("url"),
                                  "channel_name": p.get("channel_name"), "thread_ts": p.get("thread_ts"), "status": p.get("status"),
                                  "time": "%02d:%02d" % (tz.local(x["ts"], zone).hour, tz.local(x["ts"], zone).minute)})
    return {"title": "OKÚ zpravodajství", "date": ds, "total": len(seen), "people": list(per.values()), "reactions": reactions}

def markdown(dg):
    lines = [f"# {dg['title']} {dg['date']}", ""]
    for p in dg["people"]:
        if not p["enabled"]: lines.append(f"## {p['person']} ({p['persona']}): vypnuto"); lines.append(""); continue
        lines.append(f"## {p['person']} ({p['persona']}): {p['count']}")
        for it in p["items"]:
            mark = " [reakce]" if it["reacted"] else ""
            lines.append(f"- {it['time']} {it['headline']} ({it['outlet']}) {it['url']}{mark}")
        lines.append("")
    if dg["reactions"]:
        lines.append("## Reakce")
        for r in dg["reactions"]: lines.append(f"- {r['time']} {r['type']} {r['persona']} #{r.get('channel_name') or '?'}: {r.get('headline')}")
    return "\n".join(lines).rstrip() + "\n"

def headline_for(diary, now, persona=None, hours=18, people=None):
    """Freshest guard-passing, headline-matched news item (of persona, or anyone) from the last `hours`, or None."""
    best = None
    for r in diary.events(since_ts=now - (hours + 24) * 3600, types=["podnet.received"]):
        if not is_news(r): continue
        pl = r["payload"]; t = pl.get("published_at") or r["ts"]
        if t < now - hours * 3600 or not pl.get("in_headline", True): continue
        if persona and persona not in (pl.get("persons") or [pl.get("persona")]): continue
        if sensitive(f"{pl.get('title')} {pl.get('lede') or ''}"): continue
        if best is None or t > best["t"]:
            best = {"t": t, "headline": pl.get("title"), "outlet": pl.get("outlet"), "url": pl.get("url"), "persona": pl.get("persona"),
                    "person": pl.get("person")}
    return best

# ---------------- source (poller) ----------------
class NewsSource:
    """World poller: RSS every rss_interval_min inside hours, pplx at [news.pplx] times. Background threads; only
    append to the podnet inbox (never post anything)."""
    name = "news"
    def __init__(self, svc, http=None, runner=None, background=True):
        self.svc, self.background = svc, background
        self.http = http or (lambda *a, **k: http_get(*a, **k))
        self.runner = runner or (lambda *a, **k: pplx.run_cli(*a, **k))
        self.state, self.pplx_state, self.last_rss, self.last_pplx, self.feeds = "idle", "idle", None, None, {}
        self.rss_running = self.pplx_running = False; self._lock = threading.Lock(); self.last_hits = []

    def cfg(self): return self.svc.news_settings()

    def poll(self, now=None, force=False):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); d = self.svc.diary; zone = self.svc.settings().get("timezone", tz.PRAGUE)
        if not c.get("enabled") and not force: self.state = "disabled: config"; return {"state": self.state}
        res = {}
        if force or xsource.in_hours(now, c.get("hours"), zone):
            if force or now >= float(d.kv_get("news:rss_next", 0) or 0):
                d.kv_set("news:rss_next", now + max(10.0, float(c["rss_interval_min"])) * 60)
                res["rss"] = self._start(self.run_rss, now, c, "rss_running", force)
        else: self.state = "idle: outside hours"
        pc = c["pplx"]
        if pc.get("enabled") and pc.get("times"):
            try: slot = scheduler.due_slot("daily " + ",".join(pc["times"]), now, zone, 90)
            except ValueError: slot = None
            if slot and not slot[2] and d.kv_get("news:pplx_slot") != slot[0]:
                d.kv_set("news:pplx_slot", slot[0]); res["pplx"] = self._start(self.run_pplx, now, c, "pplx_running", force)
        return res

    def _start(self, fn, now, c, flag, force):
        with self._lock:
            if getattr(self, flag): return "running"
            setattr(self, flag, True)
        if self.background and not force:
            threading.Thread(target=fn, args=(now, c), daemon=True, name="news-" + flag.split("_")[0]).start(); return "started"
        return fn(now, c)

    def known(self, url):
        return bool(self.svc.diary.by_dedupe(podnet.key_for(url)))

    def append(self, hit, source, now):
        podnet.append(hit["url"], "news", hit["headline"], None, source=source, observed_at=now, path=getattr(self.svc, "inbox_path", None),
                      logs_dir=self.svc.logs_dir, now=now,
                      news={k: hit.get(k) for k in ("outlet", "published_at", "person", "persona", "persons", "lede", "in_headline")})

    def run_rss(self, now, c):
        res = {"state": "ok", "feeds_ok": 0, "feeds_failed": 0, "items": 0, "matched": 0, "appended": 0, "by_person": {}}
        try:
            entries = []
            for f in c["feeds"]:
                try:
                    data, _ = self.http(f["url"], float(c["fetch_timeout_s"]), MAX_FEED_BYTES)
                    items = parse_feed(data); res["feeds_ok"] += 1; res["items"] += len(items)
                    self.feeds[f["url"]] = {"outlet": f["outlet"], "ok": True, "items": len(items), "at": now}
                    entries += [(f["outlet"], it) for it in items]
                except Exception as e:
                    res["feeds_failed"] += 1; self.feeds[f["url"]] = {"outlet": f["outlet"], "ok": False, "error": type(e).__name__, "at": now}
                    log.info("news feed %s failed: %s", f["outlet"], type(e).__name__)
            hits = match_items(entries, c["people"], now, float(c["max_age_h"]))
            res["matched"] = len(hits); self.last_hits = list(hits.values())
            for h in sorted(hits.values(), key=lambda h: -(h["published_at"] or now)):
                for k in h["persons"]: res["by_person"][k] = res["by_person"].get(k, 0) + 1
                if res["appended"] >= int(c["max_new_per_cycle"]) or self.known(h["url"]): continue
                try: self.append(h, "news_rss", now); res["appended"] += 1
                except podnet.Invalid as e: log.info("news item rejected: %s", e)
            log.info("news rss: %d/%d feeds ok, %d items, %d matched, %d new", res["feeds_ok"], len(c["feeds"]), res["items"], res["matched"], res["appended"])
        except Exception as e: res["state"] = "error"; log.warning("news rss run failed: %s", type(e).__name__)
        finally:
            self.rss_running = False; self.state = res["state"]; self.last_rss = dict(res, at=now)
            self.write_digest(now)
        return res

    def verify(self, url, p, c, now):
        """Fetch the article: news host (also after redirects), headline names the person, fresh. -> hit or reason str."""
        try: data, final = self.http(url, float(c["fetch_timeout_s"]), MAX_PAGE_BYTES)
        except Exception as e: return "fetch:" + type(e).__name__
        if not news_host(host_of(final or url)): return "redirect_host"
        info = page_info(data)
        if not info["headline"]: return "no_title"
        if not matches(p, info["headline"], f"{info['headline']}\n{info['lede']}") and not matches(p, f"{info['headline']}\n{info['lede']}"): return "no_name"
        pub = info["published_at"]
        if pub is not None and (pub < now - float(c["max_age_h"]) * 3600 or pub > now + 3600): return "stale"
        try: url = podnet.normalize_url(final or url)
        except podnet.Invalid: return "bad_url"
        return {"url": url, "outlet": outlet_for(url), "headline": info["headline"], "lede": info["lede"], "published_at": pub,
                "persona": p["persona"], "persons": [p["persona"]], "in_headline": matches(p, info["headline"], f"{info['headline']}\n{info['lede']}"),
                "person": p["person"]}

    def run_pplx(self, now, c):
        pc = c["pplx"]; res = {"state": "ok", "calls": 0, "urls": 0, "appended": 0, "rejected": {}, "by_person": {}}
        try:
            for k, p in c["people"].items():
                if not p["enabled"] or not p["pplx_query"]: continue
                try: out = self.runner(pc["command"], pplx_question(p["pplx_query"]), pc["mode"], pc["timeout_s"]); res["calls"] += 1
                except subprocess.TimeoutExpired: res["rejected"]["timeout"] = res["rejected"].get("timeout", 0) + 1; continue
                except FileNotFoundError: res["state"] = "error: no cli"; break
                n = 0
                for url in parse_pplx(out):
                    res["urls"] += 1
                    if n >= int(pc["max_per_person"]) or self.known(url): continue
                    hit = self.verify(url, p, c, now) if pc.get("verify", True) else None
                    if isinstance(hit, str): res["rejected"][hit] = res["rejected"].get(hit, 0) + 1; continue
                    if hit is None: continue   # verify=false: unverified pplx URLs are never trusted
                    if self.known(hit["url"]): continue
                    try: self.append(hit, "news_pplx", now); n += 1; res["appended"] += 1
                    except podnet.Invalid: pass
                res["by_person"][k] = n
            log.info("news pplx: %d call(s), %d url(s), %d new, rejected %s", res["calls"], res["urls"], res["appended"], res["rejected"])
        except Exception as e: res["state"] = "error"; log.warning("news pplx run failed: %s", type(e).__name__)
        finally:
            self.pplx_running = False; self.pplx_state = res["state"]; self.last_pplx = dict(res, at=now)
        return res

    def write_digest(self, now):
        try:
            c = self.cfg(); zone = self.svc.settings().get("timezone", tz.PRAGUE)
            dg = digest(self.svc.diary, now, c, zone)
            p = pathlib.Path(self.svc.logs_dir) / "news" / f"zpravodajstvi-{dg['date']}.md"; p.parent.mkdir(parents=True, exist_ok=True)
            txt = markdown(dg)
            if not p.exists() or p.read_text(encoding="utf-8") != txt: p.write_text(txt, encoding="utf-8")
        except Exception as e: log.info("news digest file failed: %s", type(e).__name__)

# ---------------- director ----------------
def plan(hit, c, channels):
    """Deterministic reaction plan (keyed RNG NEWS:<key>). Pure. None when no channel fits."""
    p = c["people"].get(hit["persona"]) or {}
    name = p.get("channel") or c.get("channel") or "socky"
    if name not in channels: name = "socky" if "socky" in channels else next(iter(channels), None)
    if not name: return None
    rng = random.Random(int(hashlib.sha256(f"NEWS:{hit['key']}".encode()).hexdigest()[:12], 16))
    reps = list(p.get("repliers") or REPLIERS.get(hit["persona"], []))
    reply = rng.choice(reps) if reps and rng.random() < float(c["reply_rate"]) else None
    comment = COMMENTS.get(hit["persona"], "Sdílím, kolegové.")
    opener = f"{hit['url']}\n{comment}" if len(hit["url"]) + len(comment) < 395 else hit["url"]
    return {"persona": hit["persona"], "reply_persona": reply, "channel_name": name, "channel": channels[name], "comment": comment,
            "opener": opener, "turns": 2 if reply else 1, "llm_estimate": 2 if reply else 1, "rng_key": f"NEWS:{hit['key']}"}

class NewsDirector:
    """`svc` provides diary, world, settings(), news_settings(), logs_dir, outbox_dir, clock, briefs_for."""
    TYPES = ("news.requested", "news.dry_run", "news.skipped", "news.started", "news.ended", "news.failed")
    def __init__(self, svc): self.svc = svc

    def cfg(self):
        c = dict(self.svc.news_settings()); w = self.svc.settings()
        from . import chatter as wchatter
        ch = w.get("chatter_channels")
        c["channels"] = {k: v for k, v in (ch.items() if isinstance(ch, dict) else wchatter.CHANNELS.items()) if v}
        return c, w

    def usage(self, now, zone):
        day0 = tz.day_start(now, zone)
        rows = self.svc.diary.events(since_ts=day0 - 1, types=["news.requested", "news.dry_run"])
        rows = [r for r in rows if not (r["payload"] or {}).get("test")]
        per = {}
        for r in rows: per[r["actor"]] = per.get(r["actor"], 0) + 1
        return {"reactions_today": len(rows), "per_person": per}

    def candidates(self, now, c):
        d = self.svc.diary
        decided = {r["subject"] for r in d.events(since_ts=now - 3 * 86400, types=list(self.TYPES))}
        out = []
        for r in d.events(since_ts=now - (float(c["react_max_age_h"]) + 48) * 3600, types=["podnet.received"]):
            if not is_news(r) or r["subject"] in decided: continue
            pl = r["payload"]; pub = pl.get("published_at")
            if pub is None or pub < now - float(c["react_max_age_h"]) * 3600: continue   # unknown date: digest only
            if c.get("react_requires_headline", True) and not pl.get("in_headline", True): continue
            if pl.get("persona") not in c["people"] or not c["people"][pl["persona"]]["enabled"]: continue
            out.append({"key": r["subject"], "row": r["id"], "url": pl["url"], "headline": pl.get("title") or "", "lede": pl.get("lede") or "",
                        "outlet": pl.get("outlet") or outlet_for(pl["url"]), "published_at": pub, "persona": pl["persona"],
                        "person": pl.get("person") or c["people"][pl["persona"]]["person"]})
        return sorted(out, key=lambda h: -h["published_at"])

    def tick(self, now=None):
        now = self.svc.clock() if now is None else now
        c, w = self.cfg(); out = []; d = self.svc.diary
        try: self.check_ack(now, c, out)
        except Exception as e: log.warning("news ack check failed: %s", type(e).__name__)
        if not c.get("react", True) or not c.get("people") or d.kv_get("news:pending"): return out
        zone = w.get("timezone", tz.PRAGUE); u = self.usage(now, zone)
        if u["reactions_today"] >= int(c["reactions_per_day"]): return out
        for h in self.candidates(now, c):
            if u["per_person"].get(h["persona"], 0) >= int(c["per_person_per_day"]): continue
            hit = sensitive(f"{h['headline']} {h['lede']}")
            if hit:
                out.append(d.record("news.skipped", h["persona"], h["key"], {"url": h["url"], "headline": h["headline"], "reason": "guard:" + hit},
                                    source="director", parents=[h["row"]], dedupe_key=f"news:decided:{h['key']}")); continue
            out.append(self.decide(h, now, c, w)); break
        return [x for x in out if x]

    def decide(self, h, now, c, w):
        d = self.svc.diary; p = plan(h, c, c["channels"])
        base = {"podnet": h["key"], "url": h["url"], "headline": h["headline"], "outlet": h["outlet"], "person": h["person"],
                "published_at": h["published_at"]}
        if not p:
            return d.record("news.skipped", h["persona"], h["key"], dict(base, reason="no_channel"), source="director", parents=[h["row"]],
                            dedupe_key=f"news:decided:{h['key']}")
        zone = w.get("timezone", tz.PRAGUE); day0 = tz.day_start(now, zone)
        cw = dict(budget.DEFAULTS); cw.update(w); cw["per_channel_gap_h"] = float(c.get("channel_gap_h", cw.get("per_channel_gap_h", 3)))
        rows = d.events(since_ts=min(day0, now - cw["per_channel_gap_h"] * 3600) - 1)
        kill = budget.killed(cw, self.svc.logs_dir, d.kv_get, now)
        dec = budget.check("post", rows, now, cw, channel=p["channel"], llm=p["llm_estimate"],
                           wheel_live=state.wheel_live(self.svc.world.state(), now), kill=kill)
        participants = [x for x in (p["persona"], p["reply_persona"]) if x]
        info = dict(base, persona=p["persona"], reply_persona=p["reply_persona"], participants=participants, channel=p["channel"],
                    channel_name=p["channel_name"], turns=p["turns"], llm_estimate=p["llm_estimate"], rng_key=p["rng_key"], why=dec["why"])
        if not dec["ok"]:
            hour = "%s-%02d" % (tz.local(now, zone).date(), tz.local(now, zone).hour)
            if not all(x.split(" ")[0] in ("quiet_hours", "per_channel_gap", "wheel_live") for x in dec["reasons"]):
                d.record("budget.denied", p["persona"], f"news:{hour}", dict(info, storylet="NEWS", reasons=dec["reasons"]), source="director",
                         dedupe_key=f"news:denied:{hour}")
            return None   # waits; the article may still be picked while it is fresh
        if w.get("dry_run", True):
            log.info("news %s DRY RUN: %s in %s (reply %s)", h["key"], p["persona"], p["channel_name"], p["reply_persona"])
            return d.record("news.dry_run", p["persona"], h["key"], dict(info, opener=p["opener"], would={"file": "meeting_start.jsonl", "kind": "chatter",
                            "storylet": "NEWS", "top_level": True}), source="director", parents=[h["row"]], dedupe_key=f"news:decided:{h['key']}")
        briefs = getattr(self.svc, "briefs_for", lambda ps: {})(participants)
        news = {"headline": h["headline"], "outlet": h["outlet"], "lede": h["lede"], "person": h["person"], "published_at": h["published_at"], "generate": True}
        req = handoff.request_chatter(p["channel"], participants, f"Článek ({h['outlet']}): {h['headline']}"[:300], p["opener"], p["turns"],
                                      storylet="NEWS", slot=h["key"], outbox_dir=self.svc.outbox_dir, clock=self.svc.clock,
                                      podnet={"url": h["url"], "kind": "news", "key": h["key"]}, news=news, briefs=briefs, min_personas=1)
        row = d.record("news.requested", p["persona"], h["key"], dict(info, request_id=req["id"], llm_calls=p["llm_estimate"], top_level=True),
                       source="director", parents=[h["row"]], dedupe_key=f"news:decided:{h['key']}")
        d.kv_set("news:pending", json.dumps({"rid": req["id"], "key": h["key"], "at": now, "parent": row["id"] if row else None,
                                             "personas": participants, "channel": p["channel"], "channel_name": p["channel_name"],
                                             "headline": h["headline"], "url": h["url"], "started": None}))
        log.info("news %s requested (id=%s) %s in %s", h["key"], req["id"], p["persona"], p["channel_name"])
        return row

    def check_ack(self, now, c, out):
        d = self.svc.diary; raw = d.kv_get("news:pending")
        if not raw: return
        try: pend = json.loads(raw)
        except ValueError: d.kv_set("news:pending", None); return
        rid, key = pend["rid"], pend["key"]; par = [pend["parent"]] if pend.get("parent") else None
        base = {"podnet": key, "request_id": rid, "channel": pend.get("channel"), "channel_name": pend.get("channel_name"),
                "participants": pend.get("personas"), "headline": pend.get("headline"), "url": pend.get("url")}
        lead = (pend.get("personas") or [None])[0]
        acks = {a.get("status"): a for a in handoff.acks_for(rid, self.svc.outbox_dir)}
        st = acks.get("started")
        if st and not pend.get("started"):
            r = d.record("news.started", lead, key, dict(base, thread_ts=st.get("thread_ts"), status="started", reaction=st.get("reaction")),
                         source="director", parents=par, dedupe_key=f"news:started:{rid}")
            out.append(r); pend["started"] = now; pend["started_id"] = r["id"] if r else None; d.kv_set("news:pending", json.dumps(pend))
        end = next((acks[s] for s in ("done", "stopped", "error") if s in acks), None)
        par2 = [pend["started_id"]] if pend.get("started_id") else par
        if end:
            t = "news.ended" if (st or pend.get("started")) else "news.failed"
            out.append(d.record(t, lead, key, dict(base, status=end.get("status"), turns=end.get("turns"), llm_calls_actual=end.get("llm_calls"),
                                thread_ts=end.get("thread_ts")), source="director", parents=par2, dedupe_key=f"news:ended:{rid}"))
            d.kv_set("news:pending", None); return
        bad = next((acks[s] for s in acks if s not in ("done", "stopped", "error", "started")), None)
        if bad and not st:
            out.append(d.record("news.failed", lead, key, dict(base, status=bad.get("status"), reason=bad.get("reason")), source="director",
                                parents=par, dedupe_key=f"news:ended:{rid}")); d.kv_set("news:pending", None); return
        if not st and now - float(pend.get("at") or 0) > float(c["ack_timeout_s"]):
            out.append(d.record("news.failed", lead, key, dict(base, status="no_ack"), source="director", parents=par, dedupe_key=f"news:ended:{rid}"))
            d.kv_set("news:pending", None)
        elif st and now - float(pend.get("started") or now) > float(c["done_timeout_s"]):
            out.append(d.record("news.ended", lead, key, dict(base, status="timeout"), source="director", parents=par2, dedupe_key=f"news:ended:{rid}"))
            d.kv_set("news:pending", None)

# ---------------- CLI ----------------
class _CliSvc:
    def __init__(self, cfg_path=None):
        from . import log as wlog
        self.news_cfg = Config(cfg_path); self.logs_dir = pathlib.Path(os.environ.get("OKU_WORLD_LOGS") or pathlib.Path(__file__).resolve().parents[2] / "logs")
        self.inbox_path = podnet.inbox_path(self.logs_dir); self.diary = wlog.Diary(":memory:"); self.clock = time.time
    def news_settings(self): return self.news_cfg.get()
    def settings(self): return {"timezone": tz.PRAGUE}

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m oku_slack.world.news", description="OKÚ zpravodajství: fetch news / show the digest.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="one RSS cycle (and pplx with --pplx); prints matches per person")
    f.add_argument("--dry", action="store_true", help="print only, do not append to the podnet inbox")
    f.add_argument("--pplx", action="store_true"); f.add_argument("--config", default=None)
    g = sub.add_parser("digest", help="today's OKÚ zpravodajství from the running world (loopback /api/world/news)")
    g.add_argument("--date", default=None); g.add_argument("--md", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "digest":
        q = {"format": "md"} if a.md else {}
        if a.date: q["date"] = a.date
        url = (os.environ.get("OKU_WORLD_URL") or "http://127.0.0.1:8798") + "/api/world/news" + ("?" + urllib.parse.urlencode(q) if q else "")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r: body = r.read().decode("utf-8")
        print(json.loads(body)["markdown"] if a.md else body); return 0
    svc = _CliSvc(a.config); c = svc.news_settings(); now = time.time()
    src = NewsSource(svc, background=False)
    if a.dry: src.append = lambda hit, source, now: None
    src.write_digest = lambda now: None
    r = src.run_rss(now, c)
    print(json.dumps({k: v for k, v in r.items()}, ensure_ascii=False))
    for h in sorted(src.last_hits, key=lambda h: (h["persona"], -(h["published_at"] or 0))):
        print(f"{h['persona']:<9} {'H' if h['in_headline'] else 'l'} {h['outlet']:<18} {h['headline']} {h['url']}")
    for fd in c["feeds"]:
        st = src.feeds.get(fd["url"], {}); print(f"feed {'OK ' if st.get('ok') else 'ERR'} {fd['outlet']:<20} {fd['url']} {st.get('items', st.get('error'))}")
    if a.pplx: print(json.dumps(src.run_pplx(now, c), ensure_ascii=False))
    return 0

if __name__ == "__main__": sys.exit(main())
