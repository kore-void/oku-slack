"""Europe/Prague wall clock without a hard tzdata dependency.
zoneinfo is used when the tz database is available; Windows Pythons often ship without it (no `tzdata` package in
.venv/.venv-wheel), so the EU rule for Europe/Prague is built in as a fallback: CET (UTC+1), CEST (UTC+2) from the
last Sunday of March 01:00 UTC to the last Sunday of October 01:00 UTC. Other zones need zoneinfo."""
import datetime as _dt

UTC = _dt.timezone.utc
PRAGUE = "Europe/Prague"

def _last_sunday_utc(year, month):
    d = _dt.datetime(year, month + 1, 1, 1, 0, tzinfo=UTC) - _dt.timedelta(days=1)   # last day of month, 01:00 UTC
    return d - _dt.timedelta(days=(d.weekday() + 1) % 7)

def prague_offset_s(ts):
    """UTC offset in seconds for Europe/Prague at unix time ts (EU DST rule)."""
    t = _dt.datetime.fromtimestamp(float(ts), UTC)
    start, end = _last_sunday_utc(t.year, 3), _last_sunday_utc(t.year, 10)
    return 7200 if start <= t < end else 3600

def _zone(name):
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return None

def local(ts, tz=PRAGUE, force_fallback=False):
    """Aware datetime for unix ts in zone tz (fallback: fixed EU rule for Europe/Prague)."""
    z = None if force_fallback else _zone(tz)
    if z is not None: return _dt.datetime.fromtimestamp(float(ts), z)
    if tz != PRAGUE: raise ValueError(f"time zone {tz!r} needs the tz database (pip install tzdata)")
    off = prague_offset_s(ts)
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone(_dt.timedelta(seconds=off)))

def to_ts(y, mo, d, h=0, mi=0, tz=PRAGUE, force_fallback=False):
    """Unix ts of a Prague wall-clock time (ambiguous/missing DST hours resolve to the earlier offset)."""
    z = None if force_fallback else _zone(tz)
    if z is not None: return _dt.datetime(y, mo, d, h, mi, tzinfo=z).timestamp()
    if tz != PRAGUE: raise ValueError(f"time zone {tz!r} needs the tz database (pip install tzdata)")
    naive = _dt.datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()
    for off in (7200, 3600):
        if prague_offset_s(naive - off) == off: return naive - off
    return naive - 3600

def day_start(ts, tz=PRAGUE, force_fallback=False):
    """Unix ts of local midnight of the day containing ts."""
    d = local(ts, tz, force_fallback)
    return to_ts(d.year, d.month, d.day, 0, 0, tz, force_fallback)

def hm(s):
    """'22:00' -> minutes after midnight."""
    h, m = str(s).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60): raise ValueError(f"bad time {s!r}")
    return h * 60 + m

def minutes(ts, tz=PRAGUE, force_fallback=False):
    d = local(ts, tz, force_fallback); return d.hour * 60 + d.minute
