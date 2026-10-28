"""Draws the integration's icon (custom_components/ptz_360/brand/): a PTZ dome camera inside a 360° arrow."""
import math
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "..", "custom_components", "ptz_360", "brand")
BLUE = (3, 169, 244, 255)


def draw(dome, lens_ring, size=2048):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, s = size / 2, size / 1024
    # The 360° arc: most of a circle, open at the top right, with an arrow head at its end.
    r, w = 430 * s, 74 * s
    start, end = -50, 250  # degrees, clockwise from 3 o'clock (PIL)
    d.arc([c - r, c - r, c + r, c + r], start=start, end=end, fill=BLUE, width=int(w))
    a = math.radians(start)
    tip = (c + r * math.cos(a), c + r * math.sin(a))
    # The arrow head points along the arc (clockwise at its start: towards the top).
    tang = (math.sin(a), -math.cos(a))
    norm = (math.cos(a), math.sin(a))
    head, base = 150 * s, 120 * s
    p1 = (tip[0] + tang[0] * head * 0.85, tip[1] + tang[1] * head * 0.85)
    p2 = (tip[0] + norm[0] * base, tip[1] + norm[1] * base)
    p3 = (tip[0] - norm[0] * base, tip[1] - norm[1] * base)
    d.polygon([p1, p2, p3], fill=BLUE)
    # The camera: a mount on top, the housing, the dome below with its lens.
    d.rounded_rectangle([c - 60 * s, c - 250 * s, c + 60 * s, c - 160 * s], radius=18 * s, fill=dome)
    d.rounded_rectangle([c - 230 * s, c - 175 * s, c + 230 * s, c - 60 * s], radius=40 * s, fill=dome)
    d.pieslice([c - 200 * s, c - 260 * s, c + 200 * s, c + 140 * s], start=0, end=180, fill=dome)
    lx, ly, lr = c + 40 * s, c + 40 * s, 78 * s
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=lens_ring)
    lr2 = 46 * s
    d.ellipse([lx - lr2, ly - lr2, lx + lr2, ly + lr2], fill=dome)
    lr3 = 16 * s
    d.ellipse([lx - lr2 * 0.55 - lr3, ly - lr2 * 0.55 - lr3, lx - lr2 * 0.55 + lr3, ly - lr2 * 0.55 + lr3], fill=lens_ring)
    return img


for name, dome, ring in (("icon", (38, 50, 56, 255), (3, 169, 244, 255)), ("dark_icon", (207, 216, 220, 255), (3, 169, 244, 255))):
    big = draw(dome, ring)
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(OUT, f"{name}@2x.png"))
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, f"{name}.png"))
print("drawn")
