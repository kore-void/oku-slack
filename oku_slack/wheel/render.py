"""Wheel renderer: SVG (for docs/room fallback) and PNG via Pillow (Slack can't animate; it gets a still image).
Geometry matches the room: pointer at top, segment i clockwise from 0 deg, wheel rotated by -target_angle."""
import io, math
from PIL import Image, ImageDraw, ImageFont

def _font(size):
    for f in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try: return ImageFont.truetype(f, size)
        except OSError: pass
    return ImageFont.load_default()

def _pt(cx, cy, r, deg):  # deg clockwise from top
    a = math.radians(deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)

def svg(segments, angle=0.0, size=512):
    n, c, r = len(segments), size / 2, size / 2 - 12
    seg, out = 360 / n, [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">',
                         f'<g transform="rotate({-angle:.3f} {c} {c})">']
    for i, s in enumerate(segments):
        x1, y1 = _pt(c, c, r, i * seg); x2, y2 = _pt(c, c, r, (i + 1) * seg)
        tx, ty = _pt(c, c, r * 0.62, (i + 0.5) * seg)
        out.append(f'<path d="M{c},{c} L{x1:.1f},{y1:.1f} A{r},{r} 0 0 1 {x2:.1f},{y2:.1f} Z" fill="{s["color"]}" stroke="#fff" stroke-width="2"/>')
        out.append(f'<text x="{tx:.1f}" y="{ty:.1f}" fill="#fff" font-family="Arial" font-size="18" font-weight="bold" '
                   f'text-anchor="middle" transform="rotate({(i + 0.5) * seg:.1f} {tx:.1f} {ty:.1f})">{s["key"]}</text>')
    out.append('</g>')
    out.append(f'<polygon points="{c - 16},4 {c + 16},4 {c},40" fill="#e74c3c" stroke="#fff" stroke-width="2"/></svg>')
    return "".join(out)

def png(segments, angle=0.0, size=512, title=None):
    """Draw the wheel already stopped at `angle` (the value from engine.spin). Returns PNG bytes."""
    S = size * 2  # supersample
    img = Image.new("RGBA", (S, S + (120 if title else 0)), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    n, c, r = len(segments), S / 2, S / 2 - 24
    seg, f = 360 / n, _font(34)
    for i, s in enumerate(segments):
        a0 = i * seg - angle - 90  # Pillow: 0 deg = 3 o'clock, clockwise
        d.pieslice([c - r, c - r, c + r, c + r], a0, a0 + seg, fill=s["color"], outline="white", width=4)
        tx, ty = _pt(c, c, r * 0.62, (i + 0.5) * seg - angle)
        d.text((tx, ty), s["key"], fill="white", font=f, anchor="mm")
    d.ellipse([c - 40, c - 40, c + 40, c + 40], fill="white")
    d.polygon([(c - 32, 8), (c + 32, 8), (c, 80)], fill="#e74c3c", outline="white")
    if title: d.text((c, S + 60), title, fill="black", font=_font(48), anchor="mm")
    img = img.resize((size, img.height // 2), Image.LANCZOS)
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()

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
