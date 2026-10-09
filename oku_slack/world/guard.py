"""Satire guardrails shared by world-side planners (stdlib only). The SAME patterns as the bridge's
oku_slack/chatter.py (SENSITIVE_RE / QUOTE_RE); tests/test_world_podnet.py asserts they stay identical."""
import re

SENSITIVE_RE = re.compile(
    r"(?i)(?<!\w)(nemoc\w*|nemocnic\w*|rakovin\w*|zdraví|zdravotn\w*|diagnó\w*|infarkt\w*|covid\w*|hospitaliz\w*|"
    r"rodin\w*|manžel\w*|dcer\w*|syn(a|em|ovi)?|dět[ií]\w*|vnuk\w*|vnouč\w*|"
    r"soud(?!ruh)\w*|polici\w*|trestn\w*|obžalob\w*|obvin\w*|stíhán\w*|vězen\w*|kriminál\w*|podvod\w*|úplat\w*)(?!\w)")
QUOTE_RE = re.compile(r"[„\"“»][^„\"“”»«\n]{12,}[“\"”«]")
# English words for the same topics (pplx/X titles may be English)
SENSITIVE_EN_RE = re.compile(r"(?i)(?<!\w)(ill(ness)?|disease|cancer|hospital\w*|health|diagnos\w*|family|wife|husband|daughter|son|children|"
                             r"court|police|criminal|charged|indict\w*|prosecut\w*|prison|fraud|brib\w*)(?!\w)")

def guard_hit(text, english=False):
    """Reason string when text touches a forbidden topic or contains a quotation, else None."""
    t = text or ""
    m = SENSITIVE_RE.search(t)
    if m: return "sensitive:" + m.group(1).lower()
    if english:
        m = SENSITIVE_EN_RE.search(t)
        if m: return "sensitive:" + m.group(1).lower()
    if QUOTE_RE.search(t): return "quote"
    return None
