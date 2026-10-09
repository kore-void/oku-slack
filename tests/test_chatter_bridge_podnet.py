"""Bridge side of P-004 podnet reactions over the chatter hand-off: 1-2 personas, 1-2 turns, the opener (URL + comment)
posted top-level as the 'repost', one optional reply that gets PODNET_RULES (it does not know the link's content) and
the persona's world memory brief in its prompt."""
import pytest
from oku_slack import chatter as bridge_chatter, handoff
from test_chatter_bridge import mk as bmk, CFG as BCFG

def test_bridge_podnet_reaction_repost_and_one_reply():
    url = "https://x.com/andrejbabis/status/77"
    r = {"id": "p1", "kind": "chatter", "source": "world:chatter", "channel": "C0C76ATGLAH", "thread_ts": None, "personas": ["kalousek"],
         "topic": "Sdílený odkaz", "opener": url + "\nPřikládám odkaz. Kdyby se něco pokazilo, já to nebyl.", "turns": 1,
         "storylet": "PODNET", "podnet": {"url": url, "kind": "news", "key": "podnet:x"}}
    assert bridge_chatter.validate(BCFG, r, {"kalousek": 1}) == (["kalousek"], 1)
    for bad in (dict(r, opener="bez odkazu"), dict(r, personas=["kalousek", "babis", "marty"]), dict(r, turns=3),
                dict(r, podnet={"url": "javascript:x"}), dict(r, podnet=None, personas=["kalousek"])):
        with pytest.raises(ValueError): bridge_chatter.validate(BCFG, bad, {"kalousek": 1, "babis": 1, "marty": 1})
    c, slack, seen = bmk()
    res = c.start_chatter(r, sleep=lambda d: None); c.chatter_thread.join(5)
    assert res[0] == "started" and len(slack.posts) == 1 and slack.posts[0]["text"].startswith(url) and "thread_ts" not in slack.posts[0]
    assert seen == [] and handoff.acks_for("p1")[-1]["turns"] == 1
    c, slack, seen = bmk()
    r2 = dict(r, id="p2", personas=["kalousek", "babis"], turns=2, briefs={"babis": "Nedávno: porada."})
    res = c.start_chatter(r2, sleep=lambda d: None); c.chatter_thread.join(5)
    assert [p["user"] for p in slack.posts] == ["UKALOUSEK", "UBABIS"] and len(seen) == 1
    assert bridge_chatter.PODNET_RULES in seen[0][0] and "OBSAH ODKAZU NEZNÁŠ" in seen[0][0]
    assert bridge_chatter.MEMORY_LABEL + "Nedávno: porada." in seen[0][0]
