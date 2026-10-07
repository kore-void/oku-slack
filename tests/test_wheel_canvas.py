import io, random, pytest
from PIL import Image
from oku_slack.wheel import canvas, config, engine, render, store, slack_adapter as SA

class Clock:
    def __init__(self, t=3_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class Err(Exception):
    def __init__(self, code): super().__init__(code); self.response = {"error": code}

class FakeSlack:
    def __init__(self, fail=None):
        self.calls, self.fail = [], dict(fail or {})
    def _rec(self, name, kw):
        self.calls.append((name, kw))
        if name in self.fail: raise Err(self.fail[name])
    def conversations_canvases_create(self, **kw): self._rec("conv_create", kw); return {"canvas_id": "F0CANVAS"}
    def canvases_create(self, **kw): self._rec("create", kw); return {"canvas_id": "F0STANDALONE"}
    def canvases_access_set(self, **kw): self._rec("access", kw); return {"ok": True}
    def canvases_edit(self, **kw): self._rec("edit", kw); return {"ok": True}
    def chat_postMessage(self, **kw): self._rec("post", kw); return {"ts": "111.222"}
    def files_upload_v2(self, **kw): self._rec("upload", kw); return {"file": {"permalink": "https://void.slack.com/files/U/F1/kolo.png"}}
    def names(self): return [n for n, _ in self.calls]

def mk():
    clk = Clock(); st = store.Store()
    e = engine.Engine(config.load(), st, clock=clk, rng=random.Random(5)); return e, st, clk

def test_canvas_created_once_and_reused_across_restart(tmp_path):
    clk = Clock(); db = tmp_path / "w.db"; cfg = config.load()
    e = engine.Engine(cfg, store.Store(db), clock=clk); sl = FakeSlack()
    cs = canvas.CanvasSync(e, sl, "C0C6W8E6NP9", e.store, clock=clk)
    assert cs.flush() and sl.names() == ["conv_create"]
    cs.request(); clk.adv(3); assert cs.flush() and sl.names() == ["conv_create", "edit"]
    e2 = engine.Engine(cfg, store.Store(db), clock=clk); sl2 = FakeSlack()
    cs2 = canvas.CanvasSync(e2, sl2, "C0C6W8E6NP9", e2.store, clock=clk)
    assert cs2.flush() and sl2.names() == ["edit"] and sl2.calls[0][1]["canvas_id"] == "F0CANVAS"

def test_existing_channel_canvas_falls_back_to_shared_standalone():
    e, st, clk = mk(); sl = FakeSlack({"conv_create": "channel_canvas_already_exists"})
    cs = canvas.CanvasSync(e, sl, "C1", st, clock=clk); assert cs.flush()
    assert sl.names() == ["conv_create", "create", "access"] and st.kv_get("canvas_id") == "F0STANDALONE"

def test_edit_throttled_max_one_per_3s():
    e, st, clk = mk(); sl = FakeSlack(); cs = canvas.CanvasSync(e, sl, "C1", st, clock=clk)
    cs.flush()
    for _ in range(10): cs.request(); clk.adv(0.5); cs.flush()   # 5 s of rapid changes
    assert sl.names().count("edit") == 1                        # at 3.0 s only, rest throttled
    clk.adv(1); cs.flush(); assert sl.names().count("edit") == 2  # pending dirty flushed after the window
    clk.adv(10); assert not cs.flush()                          # nothing dirty, no refresh yet
    clk.adv(60); assert cs.flush()                              # periodic refresh (cooldown minutes)

def test_missing_scope_and_not_in_channel_degrade_gracefully():
    e, st, clk = mk(); sl = FakeSlack({"conv_create": "missing_scope"}); cs = canvas.CanvasSync(e, sl, "C1", st, clock=clk)
    assert cs.flush() is False and st.kv_get("canvas_id") is None
    clk.adv(10); assert cs.flush() is False and sl.names() == ["conv_create"]  # backoff, no hammering
    sl.fail.clear(); clk.adv(300); assert cs.flush() and st.kv_get("canvas_id") == "F0CANVAS"
    sl.fail["edit"] = "not_in_channel"; cs.request(); clk.adv(5); assert cs.flush() is False and cs.dirty

def test_canvas_markdown_contains_statuses():
    e, st, clk = mk(); ev = e.spin("kore", force="snemovna", lead_s=60)
    e.confirm_code(ev["id"], "kore", ev["code"]); e.use_command("icik"); e.chat("kore", "Čau lidi")
    md = canvas.markdown(e, image_url="https://x/kolo.png")
    for must in ("OKÚ Kolo · živě", "LEGENDÁRNÍ", "Mimořádná schůze sněmovny", "Kore: ✅ potvrzeno (kód)", "ICIK: ⏳ čeká",
                 "Monika Babišová:", "Nabité příkazy", "Kore · _Sorry jako_: ✅ připraven", "ICIK", "nabíjí se",
                 "Běží: **Kalousek za to může**", "Chat z roomky", "Čau lidi", "![Kolo](https://x/kolo.png)"):
        assert must in md, must
    assert ev["code"] not in md  # code never in the shared canvas
    e.confirm_code(ev["id"], "icik", ev["code"]); clk.adv(61); e.tick()
    clk.t = e.active_event()["live_at"] + 215; md = canvas.markdown(e)
    assert "PŘÍMÝ PŘENOS" in md and "Ledovec na obzoru" in md and "Petr Macinka:" in md

def test_poster_flow_spin_gif_in_thread_and_result_image_in_canvas():
    e, st, clk = mk(); sl = FakeSlack(); cs = canvas.CanvasSync(e, sl, "C1", st, clock=clk)
    post = SA.make_poster(e, sl, "C1", cs); ev = e.spin("kore", force="porada"); post("spin", ev)
    up = [kw for n, kw in sl.calls if n == "upload"][0]
    assert up["thread_ts"] == "111.222" and up["filename"].endswith(".gif")
    clk.adv(7); post("reveal", dict(e.tick())["reveal"])
    assert cs.image_url and cs.image_url.startswith("https://") and "![Kolo](" in canvas.markdown(e, cs.image_url)
    sl.fail["post"] = "not_in_channel"; post("alarm", ev)  # logged, no exception

def test_spin_gif_frames_size_and_final_frame():
    segs = [{"key": x["key"], "color": x["color"]} for x in config.load()["events"]]
    g = render.spin_gif(segs, 200.0, title="Porada")
    assert len(g) < 2_000_000
    im = Image.open(io.BytesIO(g)); assert im.n_frames == 40 and im.size == (420, 420)
    im.seek(im.n_frames - 1); assert im.info["duration"] >= 2000
    assert render.segment_at(len(segs), 200.0) == 4

def test_png_hq_size():
    segs = [{"key": x["key"], "color": x["color"]} for x in config.load()["events"]]
    im = Image.open(io.BytesIO(render.png(segs, 10, title="Kantýna"))); assert im.size == (800, 800)
