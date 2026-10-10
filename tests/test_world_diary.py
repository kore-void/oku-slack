"""World diary (logs/world.sqlite3 + world.jsonl): ingest validation, dedupe_key, causal parents, JSONL mirror,
never raising, git version in a worktree."""
import json
from oku_slack.world import log as wlog

NOW = 1_800_000_000.0
def mk(tmp_path=None, **kw):
    if tmp_path: return wlog.Diary(tmp_path / "world.sqlite3", jsonl=tmp_path / "world.jsonl", clock=lambda: NOW, version="test", **kw)
    return wlog.Diary(clock=lambda: NOW, version="test", **kw)

def D(**k): return dict({"type": "wheel.spin", "source": "wheel", "actor": "kore", "subject": "wheel:event:e1", "payload": {"key": "porada"}, "ts": NOW - 5}, **k)

def test_ingest_assigns_ids_parents_envelope_and_mirrors(tmp_path):
    d = mk(tmp_path)
    st, a = d.ingest(D(dedupe_key="wheel:1")); assert st == "ok" and a["id"] == "we_0001" and a["seq"] == 1 and a["causal_parents"] == []
    st, b = d.ingest(D(type="wheel.confirm", dedupe_key="wheel:2")); assert b["causal_parents"] == ["we_0001"]
    st, c = d.ingest(D(type="bridge.reply", source="bridge", subject=None, actor="babis", causal_parents=["we_0002"]))
    assert c["causal_parents"] == ["we_0002"] and c["dedupe_key"] is None and c["regime"] == "A_scarce" and c["content_version"] == "test"
    rows = d.events(); assert [r["id"] for r in rows] == ["we_0001", "we_0002", "we_0003"] and rows[0]["payload"] == {"key": "porada"}
    lines = [json.loads(x) for x in (tmp_path / "world.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and all("dedupe_key" in x for x in lines) and lines[1]["dedupe_key"] == "wheel:2"
    assert d.by_dedupe("wheel:2")["id"] == "we_0002" and d.by_dedupe("nope") is None and d.last(2)[0]["id"] == "we_0002"

def test_dedupe_key_is_unique_and_survives_reopen(tmp_path):
    d = mk(tmp_path); assert d.ingest(D(dedupe_key="x:1"))[0] == "ok"
    assert d.ingest(D(dedupe_key="x:1", actor="icik"))[0] == "dup" and d.count() == 1
    d.close(); d2 = mk(tmp_path)
    assert d2.ingest(D(dedupe_key="x:1"))[0] == "dup" and d2.ingest(D(dedupe_key="x:2"))[0] == "ok" and d2.count() == 2
    assert d2.ingest(D())[0] == "ok" and d2.ingest(D())[0] == "ok"   # no key = no dedupe
    assert d2.stats["dup"] == 1

def test_validation_rejects_bad_drafts():
    d = mk()
    bad = [("not a dict", "not_object"), (D(type="Wheel Spin"), "bad_type"), (D(type=""), "bad_type"), (D(source="evil"), "bad_source"),
           (D(actor=""), "bad_actor"), (D(actor="x" * 65), "bad_actor"), (D(subject=5), "bad_subject"), (D(payload=[1]), "bad_payload"),
           (D(payload={"text": "tajná zpráva"}), "free_text_key:text"), (D(payload={"message": "x"}), "free_text_key:message"),
           (D(payload={"blob": "x" * 5000}), "payload_too_big"), (D(payload={"n": float("nan")}), "payload_not_json"),
           (D(ts="yesterday"), "bad_ts"), (D(ts=True), "bad_ts"), (D(ts=1000.0), "ts_out_of_range"), (D(ts=NOW + 2 * 86400), "ts_out_of_range"),
           (D(dedupe_key=""), "bad_dedupe_key"), (D(causal_parents="we_1"), "bad_causal_parents")]
    for draft, why in bad:
        assert d.ingest(draft) == ("invalid", why), why
    assert d.count() == 0 and d.stats["invalid"] == len(bad)
    st, row = d.ingest(D(ts=None)); assert st == "ok" and row["ts"] == NOW          # ts defaults to now

def test_record_never_raises_and_kv():
    d = mk(); assert d.record("world.started", None, None, {"dry_run": True})["source"] == "world"
    assert d.record("Bad Type") is None
    d.kv_set("a", 1); assert d.kv_get("a") == "1"; d.kv_set("a", None); assert d.kv_get("a", "dflt") == "dflt"
    d.db.close()                                                                    # broken connection: still no exception
    assert d.ingest(D()) == ("error", None) and d.record("world.x") is None

def test_git_version_reads_worktree_gitdir(tmp_path):
    common = tmp_path / "main" / ".git"; wt = common / "worktrees" / "w"; wt.mkdir(parents=True)
    (common / "refs" / "heads").mkdir(parents=True); (common / "refs" / "heads" / "feat").write_text("abcdef1234\n")
    (wt / "HEAD").write_text("ref: refs/heads/feat\n"); (wt / "commondir").write_text("../..\n")
    root = tmp_path / "wt"; root.mkdir(); (root / ".git").write_text(f"gitdir: {wt}\n")
    assert wlog.git_version(root) == "abcdef1"
