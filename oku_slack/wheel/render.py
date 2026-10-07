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
