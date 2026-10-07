from oku_slack import moderation as M
from oku_slack.bridge import Bridge, register
from oku_slack import core

OWNER, BOT, CH = "U0C6XAN3EG3", "UBOT", "C0C76PTMJ9Z"
CFG = dict(core.load_config(), moderation={"enabled": True, "allowed_channels": [CH], "owner_user_id": OWNER})

class SlackErr(Exception):
    def __init__(self, err): self.response = type("R", (), {"data": {"error": err}})()

class C:
    def __init__(self, fail=None):
        self.posts, self.kicks, self.invites, self.fail = [], [], [], fail
    def chat_postMessage(self, **kw): self.posts.append(kw)
    def conversations_kick(self, **kw):
        if self.fail: raise SlackErr(self.fail)
        self.kicks.append(kw)
    def conversations_invite(self, **kw): self.invites.append(kw)
    def users_list(self, **kw): return {"members": [
        {"id": "UX", "name": "xavier", "real_name": "Xaver Novák", "profile": {"display_name": "Xaver"}},
        {"id": "UY", "name": "yvona", "real_name": "Yvona Malá", "profile": {}},
        {"id": "UB", "name": "botik", "is_bot": True, "profile": {}}]}
    def conversations_replies(self, **kw): return {"messages": []}

def ev(text, user=OWNER, ch=CH): return {"channel": ch, "ts": "1.0", "user": user, "text": text}

def test_kick_mention():
    c = C(); assert M.handle(c, CFG, BOT, ev(f"<@{BOT}> vyhoď <@UX>"))
    assert c.kicks == [{"channel": CH, "user": "UX"}] and len(c.posts) == 1 and "<@UX>" in c.posts[0]["text"]

def test_kick_alias_plain_name():
    c = C(); assert M.handle(c, CFG, BOT, ev("Andreji, vyhoď Xaver"))
    assert c.kicks == [{"channel": CH, "user": "UX"}]

def test_invite_mentions():
    c = C(); assert M.handle(c, CFG, BOT, ev(f"<@{BOT}> pozvi <@UX> <@UY>"))
    assert c.invites == [{"channel": CH, "users": "UX,UY"}]

def test_invite_plain_folded():
    c = C(); assert M.handle(c, CFG, BOT, ev("Babiši pozvi yvona mala"))
    assert c.invites == [{"channel": CH, "users": "UY"}]

def test_non_owner_denied():
    c = C(); assert M.handle(c, CFG, BOT, ev(f"<@{BOT}> vyhoď <@UX>", user="UEVIL"))
    assert not c.kicks and c.posts[0]["text"] == M.REPLY["deny"]

def test_protected_owner_and_bot():
    c = C(); M.handle(c, CFG, BOT, ev(f"<@{BOT}> vyhoď <@{OWNER}>"))
    assert not c.kicks and c.posts[0]["text"] == M.REPLY["protected"]
    c = C(); M.handle(c, CFG, BOT, ev("Andreji vyhoď <@UBOT|x>"))
    assert not c.kicks

def test_other_channel_and_not_addressed_and_disabled():
    c = C()
    assert not M.handle(c, CFG, BOT, ev(f"<@{BOT}> vyhoď <@UX>", ch="COTHER"))
    assert not M.handle(c, CFG, BOT, ev("vyhoď <@UX>"))
    assert not M.handle(c, CFG, BOT, ev(f"<@{BOT}> jak se máš"))
    assert not M.handle(c, dict(CFG, moderation={"enabled": False, "allowed_channels": [CH], "owner_user_id": OWNER}), BOT, ev(f"<@{BOT}> vyhoď <@UX>"))
    assert not c.posts and not c.kicks

def test_unknown_and_failure():
    c = C(); M.handle(c, CFG, BOT, ev("Andreji vyhoď nikdo")); assert c.posts[0]["text"] == M.REPLY["unknown"]
    c = C(fail="missing_scope"); M.handle(c, CFG, BOT, ev(f"<@{BOT}> vyhoď <@UX>")); assert "missing_scope" in c.posts[0]["text"]

def test_bridge_mention_path_babis_only():
    c = C(); b = Bridge(c, dict(CFG, icon_base_url=""), BOT, "babis", gen=lambda s, h: "llm")
    b.handle(ev(f"<@{BOT}> vyhoď <@UX>")); assert c.kicks and len(c.posts) == 1
    c2 = C(); b2 = Bridge(c2, CFG, "UK", "kalousek", gen=lambda s, h: "llm")
    b2.handle(ev("<@UK> vyhoď <@UX>")); assert not c2.kicks and c2.posts[0]["text"] == "llm"

def test_message_path_alias_no_mention():
    c = C(); b = Bridge(c, CFG, BOT, "babis", gen=lambda s, h: "llm")
    class App:
        h = {}
        def event(self, name):
            def d(f): App.h[name] = f; return f
            return d
    register(App(), b)
    App.h["message"](ev("Andreji, vyhoď <@UX>"))
    assert c.kicks == [{"channel": CH, "user": "UX"}]
    c.kicks.clear(); App.h["message"](ev(f"<@{BOT}> vyhoď <@UX>"))  # mention -> app_mention path handles it
    assert not c.kicks
    App.h["message"](ev("Andreji, vyhoď <@UX>", user="UEVIL"))  # non-owner without mention: silently ignored
    assert not c.kicks and not c.posts[1:]
