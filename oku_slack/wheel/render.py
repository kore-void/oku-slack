"""Wheel renderer: SVG (for docs/room fallback) and PNG via Pillow (Slack can't animate; it gets a still image).
Geometry matches the room: pointer at top, segment i clockwise from 0 deg, wheel rotated by -target_angle."""
import re
import io, math
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
LABELS = {"porada": "Porada", "tiskovka": "Tiskovka", "kantyna": "Kantýna", "disko": "Diskotéka", "socky": "Sítě",
          "vina": "Viník", "bourak": "Zelená jízda", "snemovna": "Sněmovna"}
_fcache = {}

def _font(size):
    size = max(8, int(size))
    if size in _fcache: return _fcache[size]
    for f in FONT_CANDIDATES:
        try: _fcache[size] = ImageFont.truetype(f, size); return _fcache[size]
        except OSError: pass
    return ImageFont.load_default()

def _pt(cx, cy, r, deg):  # deg clockwise from top
    a = math.radians(deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)

def _rgb(h):
    h = h.lstrip("#"); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def _mix(c, d, t): return tuple(int(c[i] + (d[i] - c[i]) * t) for i in range(3))

def label_of(s): return s.get("label") or LABELS.get(s["key"], s["key"].capitalize())

def _radial(size, inner, outer, power=1.0):
    """RGB radial gradient (inner at center -> outer at edge)."""
    from PIL import ImageChops  # noqa: F401
    m = Image.radial_gradient("L").resize((size, size), Image.BICUBIC)  # 0 center .. 255 edge
    if power != 1.0: m = m.point(lambda v: int(255 * (v / 255) ** power))
    return Image.composite(Image.new("RGB", (size, size), outer), Image.new("RGB", (size, size), inner), m)

def wheel_layer(segments, R, view_angle=0.0):
    """Rotatable wheel face (segments + labels + separators) of radius R, RGBA 2R x 2R, drawn at angle 0.
    Labels are radial; a label that would end up on the left half once the wheel is rotated by view_angle is
    flipped 180 deg so it stays readable."""
    D = 2 * R; n = len(segments); seg = 360 / n
    face = Image.new("RGBA", (D, D), (0, 0, 0, 0))
    for i, s in enumerate(segments):
        c = _rgb(s.get("color", "#888888"))
        grad = _radial(D, _mix(c, (255, 255, 255), 0.35), _mix(c, (0, 0, 0), 0.35), power=0.8)
        mask = Image.new("L", (D, D), 0)
        ImageDraw.Draw(mask).pieslice([0, 0, D - 1, D - 1], i * seg - 90, (i + 1) * seg - 90, fill=255)
        face.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(face)
    for i in range(n):  # gold separators
        x, y = _pt(R, R, R, i * seg); d.line([(R, R), (x, y)], fill=(255, 214, 102), width=max(2, R // 90))
    fs = max(12, int(R * 0.105 * min(1.0, 8 / n) ** 0.5))
    for i, s in enumerate(segments):  # radial labels, reading outward
        txt = label_of(s); f = _font(fs)
        while f.getlength(txt) > R * 0.58 and fs > 10: fs -= 1; f = _font(fs)
        w = int(f.getlength(txt)) + fs; h = int(fs * 1.6)
        t = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(t).text((w / 2, h / 2), txt, font=f, fill="white", anchor="mm",
                               stroke_width=max(1, fs // 9), stroke_fill=(30, 15, 10))
        mid = (i + 0.5) * seg
        flip = 180 if ((mid - view_angle) % 360) > 180 else 0  # left half on screen -> read inward, not upside down
        t = t.rotate(90 - mid + flip, resample=Image.BICUBIC, expand=True)  # text baseline along the radius
        cx, cy = _pt(R, R, R * 0.62, mid)
        face.alpha_composite(t, (int(cx - t.width / 2), int(cy - t.height / 2)))
        fs = max(12, int(R * 0.105 * min(1.0, 8 / n) ** 0.5))
    return face

def _blur(im, r):
    from PIL import ImageFilter
    return im.filter(ImageFilter.GaussianBlur(r))

def stage(N, k, title=None):
    """Static parts at supersample k: background, shadow, rim, bulbs anchors. Returns (bg, geom)."""
    S = N * k; cx, cy, R = S / 2, S * 0.47, int(S * 0.38)
    bg = _radial(S, (52, 26, 78), (6, 4, 12), power=0.7).convert("RGBA")
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse([cx - R * 1.3, cy - R * 1.45, cx + R * 1.3, cy + R * 1.1], fill=120)
    bg.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (S, S), (255, 220, 150)).split(), _blur(glow, S / 14))))
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).ellipse([cx - R * 1.05, cy - R * 0.95 + S * 0.03, cx + R * 1.05, cy + R * 1.15 + S * 0.03], fill=190)
    bg.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (S, S), (0, 0, 0)).split(), _blur(sh, S / 40))))
    return bg, (cx, cy, R)

def _rim(img, cx, cy, R, phase, nb=24):
    d = ImageDraw.Draw(img); w = R * 0.11
    for j in range(12):  # gold ring with vertical-ish shading via concentric strokes
        t = j / 11; col = _mix((120, 80, 10), (255, 225, 120), 1 - abs(t - 0.4) * 1.6 if abs(t - 0.4) < 0.6 else 0.05)
        r = R + w * (1 - t)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=int(w / 10) + 2)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(90, 55, 5), width=max(2, int(R / 120)))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0)); gd = ImageDraw.Draw(glow)
    rb = R * 0.032; rr = R + w * 0.5
    for b in range(nb):
        x, y = _pt(cx, cy, rr, b * 360 / nb)
        lit = (b + phase) % 2 == 0
        if lit: gd.ellipse([x - rb * 2.6, y - rb * 2.6, x + rb * 2.6, y + rb * 2.6], fill=(255, 230, 120, 150))
    img.alpha_composite(_blur(glow, rb * 1.4))
    d = ImageDraw.Draw(img)
    for b in range(nb):
        x, y = _pt(cx, cy, rr, b * 360 / nb); lit = (b + phase) % 2 == 0
        d.ellipse([x - rb, y - rb, x + rb, y + rb], fill=(255, 250, 215) if lit else (120, 90, 40), outline=(80, 50, 0))
        if lit: d.ellipse([x - rb * 0.45, y - rb * 0.6, x + rb * 0.1, y - rb * 0.1], fill=(255, 255, 255))

def _hub(img, cx, cy, R):
    r = int(R * 0.17); D = 2 * r
    disc = _radial(D, (255, 236, 160), (150, 95, 10), power=1.3).convert("RGBA")
    m = Image.new("L", (D, D), 0); ImageDraw.Draw(m).ellipse([0, 0, D - 1, D - 1], fill=255); disc.putalpha(m)
    hl = Image.new("L", (D, D), 0); ImageDraw.Draw(hl).ellipse([D * 0.18, D * 0.06, D * 0.82, D * 0.5], fill=140)
    from PIL import ImageChops
    disc.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (D, D), (255, 255, 255)).split(), ImageChops.multiply(_blur(hl, D / 25), m))))
    d = ImageDraw.Draw(disc)
    d.ellipse([2, 2, D - 3, D - 3], outline=(110, 60, 0), width=max(2, D // 40))
    d.text((r, r + D * 0.02), "OKÚ", font=_font(D * 0.3), fill=(140, 20, 30), anchor="mm", stroke_width=max(1, D // 60), stroke_fill=(255, 240, 200))
    img.alpha_composite(disc, (int(cx - r), int(cy - r)))

def _pointer(img, cx, cy, R):
    top = cy - R - R * 0.16; tip = cy - R + R * 0.13; hw = R * 0.085
    poly = [(cx - hw, top), (cx + hw, top), (cx, tip)]
    sh = Image.new("L", img.size, 0); ImageDraw.Draw(sh).polygon([(x + R * 0.02, y + R * 0.03) for x, y in poly], fill=170)
    img.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", img.size, (0, 0, 0)).split(), _blur(sh, R * 0.02))))
    d = ImageDraw.Draw(img)
    d.polygon(poly, fill=(215, 25, 40), outline=(255, 214, 102), width=max(2, int(R / 90)))
    d.polygon([(cx - hw * 0.55, top + R * 0.02), (cx - hw * 0.1, top + R * 0.02), (cx - hw * 0.05, tip - R * 0.07)], fill=(255, 110, 110))
    d.ellipse([cx - hw * 0.45, top - hw * 0.45, cx + hw * 0.45, top + hw * 0.45], fill=(255, 214, 102), outline=(120, 70, 0))

def _banner(img, title, S):
    d = ImageDraw.Draw(img); f = _font(S * 0.045)
    txt = title
    while f.getlength(txt) > S * 0.8: f = _font(f.size - 2)
    w = f.getlength(txt) + S * 0.08; y = S * 0.915; h = S * 0.085
    x0, x1 = S / 2 - w / 2, S / 2 + w / 2
    d.polygon([(x0 - S * 0.04, y - h * 0.3), (x0, y - h * 0.3), (x0, y + h * 0.7), (x0 - S * 0.04, y + h * 0.7), (x0 - S * 0.02, y + h * 0.2)], fill=(150, 15, 25))
    d.polygon([(x1 + S * 0.04, y - h * 0.3), (x1, y - h * 0.3), (x1, y + h * 0.7), (x1 + S * 0.04, y + h * 0.7), (x1 + S * 0.02, y + h * 0.2)], fill=(150, 15, 25))
    d.rounded_rectangle([x0, y - h / 2, x1, y + h / 2], radius=int(h / 4), fill=(205, 25, 40), outline=(255, 214, 102), width=max(2, int(S / 300)))
    d.text((S / 2, y), txt, font=f, fill="white", anchor="mm", stroke_width=max(1, int(S / 500)), stroke_fill=(90, 0, 10))

class WheelRenderer:
    """Caches static layers so GIF frames only rotate the face. k = supersampling factor."""
    def __init__(self, segments, size=800, k=2, view_angle=0.0):
        self.N, self.k = size, k
        self.bg, (self.cx, self.cy, self.R) = stage(size, k)
        self.face = wheel_layer(segments, self.R, view_angle)

    def frame(self, angle, phase=0, title=None):
        img = self.bg.copy()
        f = self.face.rotate(angle % 360, resample=Image.BICUBIC)
        img.alpha_composite(f, (int(self.cx - self.R), int(self.cy - self.R)))
        _rim(img, self.cx, self.cy, self.R, phase)
        _hub(img, self.cx, self.cy, self.R)
        _pointer(img, self.cx, self.cy, self.R)
        if title: _banner(img, title, self.N * self.k)
        return img.convert("RGB").resize((self.N, self.N), Image.LANCZOS)

def png(segments, angle=0.0, size=800, title=None, k=3):
    """High-quality still of the wheel stopped at `angle` (from engine.spin), k-times supersampled. PNG bytes."""
    img = WheelRenderer(segments, size, k, view_angle=angle).frame(angle, 0, title)
    b = io.BytesIO(); img.save(b, "PNG", optimize=True); return b.getvalue()

def spin_gif(segments, target_angle, turns=5, frames=40, seconds=3.2, size=420, title=None, hold_ms=2200, k=2):
    """Animated spin: same geometry and ease-out as the room ((turns*360+target) * (1-(1-t)^3)).
    Marquee bulbs blink, last frame = exact result with banner held ~2 s. Adaptive palette per frame."""
    wr = WheelRenderer(segments, size, k, view_angle=target_angle); total = turns * 360 + target_angle; out = []
    for i in range(frames):
        t = i / (frames - 1)
        a = target_angle if i == frames - 1 else (total * (1 - (1 - t) ** 3)) % 360
        im = wr.frame(a, phase=(i // 2) % 2, title=title if i == frames - 1 else None)
        out.append(im.quantize(colors=128, method=Image.Quantize.MEDIANCUT))  # own palette per frame
    durs = [int(seconds * 1000 / frames)] * (frames - 1) + [hold_ms]
    b = io.BytesIO()
    out[0].save(b, "GIF", save_all=True, append_images=out[1:], duration=durs, loop=0, optimize=True, disposal=1)
    return b.getvalue()

def segment_at(n, angle):
    """Which segment is under the top pointer when the wheel is rotated by -angle."""
    return int((angle % 360) // (360 / n))

def titanic_poster(script=None, size=(1024, 640)):
    """Poster for the legendary Titanic scene: sunset, sea, ship bow over parliament benches, iceberg,
    PŘÍMÝ PŘENOS badge. Characters are flat silhouettes with name tags only (no likenesses)."""
    script = script or {}
    W, H = size; img = Image.new("RGB", size); d = ImageDraw.Draw(img)
    hz = int(H * 0.55)
    for y in range(hz):  # sunset gradient
        t = y / hz; d.line([(0, y), (W, y)], fill=(int(40 + 215 * t), int(30 + 110 * t), int(90 - 40 * t)))
    d.ellipse([W * 0.62, hz - 90, W * 0.62 + 180, hz + 90], fill=(255, 190, 80))
    for y in range(hz, H):  # sea
        t = (y - hz) / (H - hz); d.line([(0, y), (W, y)], fill=(int(20 + 30 * (1 - t)), int(60 + 40 * (1 - t)), int(110 + 40 * (1 - t))))
    for k in range(6):  # waves
        yy = hz + 20 + k * 40
        d.line([(x, yy + 6 * math.sin(x / 40 + k)) for x in range(0, W, 8)], fill=(150, 190, 220), width=2)
    d.polygon([(W * 0.80, hz + 5), (W * 0.88, hz - 120), (W * 0.93, hz - 60), (W * 0.98, hz + 5)], fill=(235, 245, 255))
    d.text((W * 0.89, hz - 20), "NEDŮVĚRA", fill=(40, 70, 110), font=_font(18), anchor="mm")
    for r in range(4):  # parliament benches (semicircle rows)
        rr = 200 + r * 60
        d.arc([W * 0.30 - rr, H - rr * 0.55, W * 0.30 + rr, H + rr * 0.55], 180, 360, fill=(110, 60, 40), width=18)
    d.polygon([(40, H - 40), (W * 0.52, H - 40), (W * 0.62, hz + 40), (W * 0.30, hz + 90)], fill=(60, 35, 30))  # bow / rostrum
    d.line([(W * 0.30, hz + 90), (W * 0.62, hz + 40)], fill=(220, 200, 160), width=4)
    deck = lambda x: (hz + 90) + (x - W * 0.30) * (-50) / (W * 0.32)  # y of the bow rail at x
    for x, name, arms, tag_dy in ((W * 0.53, "MACINKA", True, -150), (W * 0.42, "TUREK", False, -120)):  # silhouettes only
        foot = deck(x); top = foot - 110
        d.ellipse([x - 16, top, x + 16, top + 32], fill=(15, 15, 25))
        d.rectangle([x - 14, top + 32, x + 14, foot], fill=(15, 15, 25))
        if arms: d.line([(x - 90, top + 44), (x + 90, top + 44)], fill=(15, 15, 25), width=10)
        ty = foot + tag_dy - 40
        d.rounded_rectangle([x - 52, ty - 13, x + 52, ty + 13], 6, fill=(255, 255, 255))
        d.text((x, ty), name, fill=(20, 20, 20), font=_font(16), anchor="mm")
    d.rounded_rectangle([24, 24, 270, 70], 8, fill=(200, 20, 30))
    d.ellipse([38, 38, 56, 56], fill="white")
    d.text((162, 47), "PŘÍMÝ PŘENOS", fill="white", font=_font(24), anchor="mm")
    d.text((W / 2, 110), script.get("title", "Titanic scéna"), fill="white", font=_font(30), anchor="mm")
    d.text((W / 2, 150), "Mimořádná schůze sněmovny", fill=(255, 230, 180), font=_font(24), anchor="mm")
    d.rectangle([0, H - 36, W, H], fill=(0, 0, 0))
    d.text((W / 2, H - 18), script.get("credits", "Produkce: Marty Prchal, marketingový génius"), fill="white", font=_font(18), anchor="mm")
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()

def svg(segments, angle=0.0, size=512):
    """Lightweight SVG of the wheel (docs / fallback). Same geometry as png()."""
    n, c, r = len(segments), size / 2, size / 2 - 12
    seg, out = 360 / n, [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">',
                         f'<g transform="rotate({-angle:.3f} {c} {c})">']
    for i, s in enumerate(segments):
        x1, y1 = _pt(c, c, r, i * seg); x2, y2 = _pt(c, c, r, (i + 1) * seg)
        tx, ty = _pt(c, c, r * 0.62, (i + 0.5) * seg)
        out.append(f'<path d="M{c},{c} L{x1:.1f},{y1:.1f} A{r},{r} 0 0 1 {x2:.1f},{y2:.1f} Z" fill="{s["color"]}" stroke="#ffd666" stroke-width="2"/>')
        out.append(f'<text x="{tx:.1f}" y="{ty:.1f}" fill="#fff" font-family="Segoe UI, Arial" font-size="18" font-weight="bold" '
                   f'text-anchor="middle" transform="rotate({(i + 0.5) * seg - 90:.1f} {tx:.1f} {ty:.1f})">{label_of(s)}</text>')
    out.append('</g>')
    out.append(f'<polygon points="{c - 16},4 {c + 16},4 {c},40" fill="#d71928" stroke="#ffd666" stroke-width="2"/></svg>')
    return "".join(out)

def render_assets(out_dir, segments, script=None):
    """Static images for the Slack panel (hosted on itzkore.cz/oku/kolo/img/): idle wheel, per-event
    result PNG and spin GIF landing in the segment centre, Titanic poster."""
    import pathlib
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    (out / "kolo.png").write_bytes(png(segments, 0))
    n = len(segments)
    for i, s in enumerate(segments):
        a = (i + 0.5) * 360 / n
        title = s.get("title") or label_of(s)
        (out / f"kolo-{s['key']}.png").write_bytes(png(segments, a, title=title))
        (out / f"kolo-spin-{s['key']}.gif").write_bytes(spin_gif(segments, a, title=title))
        (out / f"card-{s['key']}.png").write_bytes(result_card(title, s.get("host_name", ""), "", s.get("color", "#1d4f91"), s.get("avatar")))
    (out / "titanic.png").write_bytes(titanic_poster(script))
    return sorted(p.name for p in out.iterdir())

def _wrap(d, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) > maxw and cur: lines.append(cur); cur = w
        else: cur = t
    return lines + ([cur] if cur else [])

def safe_text(text, font):
    """Drop characters the font cannot render (would show as tofu/stray marks)."""
    try:
        from fontTools.ttLib import TTFont  # optional
    except ImportError:
        TTFont = None
    out = []
    for ch in str(text or ""):
        if ch.isspace() or ch.isalnum() or ch in ".,:;!?-–'\"()&/+%#@":
            try:
                if font.getmask(ch).getbbox() is None and not ch.isspace(): continue
            except Exception: continue
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()

def result_card(title, host_name="", start_text="", color="#1d4f91", avatar_path=None, size=(800, 450), k=2, seed=7):
    """Result card for the Slack panel: stage background, gold frame, confetti, big event title, host and start.
    The host is shown only via an existing repo avatar asset (assets/avatars) or a neutral silhouette."""
    import random as _r
    W, H = size[0] * k, size[1] * k
    img = _radial(max(W, H), _mix(_rgb(color), (60, 30, 90), 0.5), (6, 4, 12), power=0.7).crop((0, 0, W, H)).convert("RGBA")
    glow = Image.new("L", (W, H), 0); ImageDraw.Draw(glow).ellipse([W * 0.15, -H * 0.4, W * 0.85, H * 0.7], fill=110)
    img.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (W, H), (255, 220, 150)).split(), _blur(glow, W / 18))))
    title = safe_text(title, _font(58 * k)); host_name = safe_text(host_name, _font(28 * k)); start_text = safe_text(start_text, _font(24 * k))
    d = ImageDraw.Draw(img); rnd = _r.Random(seed)
    for _ in range(140):  # confetti, kept out of the text band so nothing reads as a stray glyph
        x, y, s = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(6, 16) * k / 2
        if 0.12 * W < x < 0.88 * W and 0.08 * H < y < 0.92 * H: continue
        c = rnd.choice([(255, 214, 102), (215, 25, 40), (255, 255, 255), (17, 69, 126), (46, 204, 113)])
        a = rnd.uniform(0, 3.14)
        pts = [(x + s * math.cos(a + t), y + s * 0.5 * math.sin(a + t)) for t in (0, 1.6, 3.14, 4.7)]
        d.polygon(pts, fill=c)
    m = 18 * k  # gold frame with bulbs
    for j in range(5):
        d.rounded_rectangle([m + j * 2, m + j * 2, W - m - j * 2, H - m - j * 2], radius=28 * k, outline=_mix((140, 95, 15), (255, 225, 120), j / 4), width=2 * k)
    for i in range(22):
        t = i / 22; x = m + (W - 2 * m) * t
        for y in (m + 3 * k, H - m - 3 * k):
            d.ellipse([x - 5 * k, y - 5 * k, x + 5 * k, y + 5 * k], fill=(255, 245, 200) if i % 2 == 0 else (150, 110, 40))
    d.text((W / 2, 70 * k), "VÝSLEDEK KOLA", font=_font(26 * k), fill=(255, 214, 102), anchor="mm", stroke_width=k, stroke_fill=(60, 20, 0))
    f = _font(58 * k); lines = _wrap(d, title, f, W - 140 * k)
    while len(lines) > 2: f = _font(f.size - 4 * k); lines = _wrap(d, title, f, W - 140 * k)
    y0 = H * 0.38 - (len(lines) - 1) * f.size * 0.55
    for i, ln in enumerate(lines):
        d.text((W / 2, y0 + i * f.size * 1.1), ln, font=f, fill="white", anchor="mm", stroke_width=3 * k, stroke_fill=(70, 10, 20))
    # host chip: avatar asset or silhouette
    cy = H * 0.64; r = 38 * k; cx = W / 2 - (d.textlength(host_name, font=_font(28 * k)) / 2 + r + 10 * k) / 2 if host_name else W / 2
    if host_name:
        av = None
        if avatar_path:
            try: av = Image.open(avatar_path).convert("RGBA").resize((2 * r, 2 * r), Image.LANCZOS)
            except OSError: av = None
        ring = Image.new("L", (2 * r, 2 * r), 0); ImageDraw.Draw(ring).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        if av is None:
            av = Image.new("RGBA", (2 * r, 2 * r), (40, 30, 60, 255)); ad = ImageDraw.Draw(av)
            ad.ellipse([r * 0.6, r * 0.35, r * 1.4, r * 1.15], fill=(15, 15, 25)); ad.ellipse([r * 0.25, r * 1.2, r * 1.75, r * 2.6], fill=(15, 15, 25))
        av.putalpha(ring); img.alpha_composite(av, (int(cx - r), int(cy - r)))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 214, 102), width=3 * k)
        d.text((cx + r + 14 * k, cy), host_name, font=_font(28 * k), fill="white", anchor="lm")
    if start_text:
        d.rounded_rectangle([W / 2 - 150 * k, H - 100 * k, W / 2 + 150 * k, H - 58 * k], radius=18 * k, fill=(205, 25, 40), outline=(255, 214, 102), width=2 * k)
        d.text((W / 2, H - 79 * k), start_text, font=_font(24 * k), fill="white", anchor="mm")
    out = img.convert("RGB").resize(size, Image.LANCZOS)
    b = io.BytesIO(); out.save(b, "PNG", optimize=True); return b.getvalue()
