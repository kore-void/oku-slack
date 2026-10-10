import time
from oku_slack.wheel import engine, store, config
def _eng(t, tmp_path):
    cfg = config.load(); return engine.Engine(cfg, store.Store(str(tmp_path / "s.db")), clock=lambda: t[0])
def test_expires_after_grace_and_frees_wheel(tmp_path):
    t = [1_000_000.0]; e = _eng(t, tmp_path); e.s["confirm_quorum"] = 2  # strict: both players (default is 1, D1)
    ev = e.spin("kore"); e.confirm_code(ev["id"], "kore", ev["code"])
    t[0] = ev["start_at"] + 30; e.tick()
    try: e.spin("kore"); assert False
    except engine.WheelError as err: assert "čeká se na potvrzení (ICIK) do" in str(err)
    t[0] = ev["start_at"] + 61; e.tick()
    assert e.active_event() is None
    assert [x for x in e.store.events() if x["id"] == ev["id"]][0]["state"] == "expired"
def test_status_text_czech(tmp_path):
    from oku_slack.wheel import slack_adapter as SA
    t = [1_000_000.0]; e = _eng(t, tmp_path); ev = e.spin("kore")
    s = SA.status_text(e, "kore", e.active_event(), 0)
    assert "čeká na potvrzení" in s and "pending" not in s
def test_card_text_sanitised():
    from oku_slack.wheel import render
    f = render._font(40)
    assert render.safe_text("Marty Prchal \u02d7\U0001F3A1", f) == "Marty Prchal"
    assert render.safe_text("Mimořádná schůze sněmovny", f) == "Mimořádná schůze sněmovny"
