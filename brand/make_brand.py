"""Аватарка и баннер приветствия. Рисуем с запасом по разрешению и
уменьшаем — так края получаются гладкими без возни со сглаживанием."""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path("/Users/type/Documents/vpn/brand")
OUT.mkdir(exist_ok=True)

BLACK_BOLD = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
REG = "/System/Library/Fonts/Supplemental/Arial.ttf"

NAVY = (11, 22, 34)
DEEP = (6, 12, 20)
CYAN = (0, 229, 255)
BLUE = (42, 171, 238)


def vgrad(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (1, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.point((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img.resize((w, h), Image.BICUBIC)


def radial_glow(size, center, radius, color, strength=140):
    glow = Image.new("L", size, 0)
    d = ImageDraw.Draw(glow)
    cx, cy = center
    steps = 46
    for i in range(steps, 0, -1):
        r = radius * i / steps
        v = int(strength * (1 - i / steps) ** 2)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=v)
    glow = glow.filter(ImageFilter.GaussianBlur(radius / 7))
    layer = Image.new("RGB", size, color)
    return layer, glow


def bolt(cx, cy, h):
    """Молния как многоугольник — толстая, читается в мелком размере."""
    w = h * 0.46
    pts = [
        (0.10, -0.50), (-0.42, 0.06), (-0.06, 0.06),
        (-0.14, 0.50), (0.42, -0.08), (0.05, -0.08),
    ]
    return [(cx + x * w * 2.05, cy + y * h) for x, y in pts]


def make_avatar(path, scale=4):
    S = 512 * scale
    img = vgrad((S, S), (17, 32, 52), DEEP).convert("RGB")

    layer, mask = radial_glow((S, S), (S * 0.5, S * 0.42), S * 0.52, (10, 90, 150), 120)
    img = Image.composite(layer, img, mask)

    d = ImageDraw.Draw(img)

    # Щит строим по правой половине и зеркалим — иначе левый и правый
    # низ считаются разными формулами и силуэт заметно кособочит.
    cx, cy = S / 2, S * 0.46
    w, h = S * 0.52, S * 0.62
    top, bot = cy - h / 2, cy + h / 2
    shoulder = top + h * 0.44

    right = [(cx + w / 2, top), (cx + w / 2, shoulder)]
    for i in range(1, 91):
        t = i / 90
        # Квадратичная Безье от плеча к острию: плавное сужение книзу.
        x = (1 - t) ** 2 * (cx + w / 2) + 2 * (1 - t) * t * (cx + w * 0.47) + t ** 2 * cx
        y = (1 - t) ** 2 * shoulder + 2 * (1 - t) * t * (bot - h * 0.04) + t ** 2 * bot
        right.append((x, y))
    shield = [(2 * cx - x, y) for x, y in reversed(right)] + right

    d.polygon(shield, fill=(255, 255, 255))

    # Молния вырезана из щита — negative space читается лучше наложения.
    inner = vgrad((S, S), (14, 120, 200), (0, 229, 255))
    bolt_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bolt_mask).polygon(bolt(cx, cy + S * 0.005, S * 0.36), fill=255)
    shield_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shield_mask).polygon(shield, fill=255)

    filled = Image.composite(inner, img, shield_mask)
    img = Image.composite(img, filled, bolt_mask)

    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(BLACK_BOLD, int(S * 0.115))
    txt = "VPN"
    bb = d.textbbox((0, 0), txt, font=f)
    d.text(((S - (bb[2] - bb[0])) / 2 - bb[0], S * 0.845 - bb[1]), txt,
           font=f, fill=(236, 248, 255))

    img.resize((512, 512), Image.LANCZOS).save(path, "PNG")
    print("аватарка:", path)


def shield_shape(cx, cy, w, h):
    """Тот же силуэт, что на аватарке, — баннер должен с ней рифмоваться."""
    top, bot = cy - h / 2, cy + h / 2
    shoulder = top + h * 0.44
    right = [(cx + w / 2, top), (cx + w / 2, shoulder)]
    for i in range(1, 91):
        t = i / 90
        x = (1 - t) ** 2 * (cx + w / 2) + 2 * (1 - t) * t * (cx + w * 0.47) + t ** 2 * cx
        y = (1 - t) ** 2 * shoulder + 2 * (1 - t) * t * (bot - h * 0.04) + t ** 2 * bot
        right.append((x, y))
    return [(2 * cx - x, y) for x, y in reversed(right)] + right


def make_banner(path, scale=2):
    W, H = 1280 * scale, 720 * scale
    TEXT_ZONE = 0.56          # левее этой доли ширины декор не заходит
    img = vgrad((W, H), (13, 26, 42), (5, 10, 17)).convert("RGB")

    for c, r, col, st in (
        ((W * 0.80, H * 0.46), W * 0.40, (10, 85, 145), 120),
        ((W * 0.10, H * 0.88), W * 0.34, (55, 28, 120), 70),
    ):
        layer, mask = radial_glow((W, H), c, r, col, st)
        img = Image.composite(layer, img, mask)

    # Дуги живут только в правой части, чтобы не резать заголовок.
    am = Image.new("L", (W, H), 0)
    ad = ImageDraw.Draw(am)
    for y0, thick, alpha in ((0.24, 4, 120), (0.50, 3, 90), (0.76, 3, 70)):
        pts = []
        for i in range(160):
            t = i / 159
            x = W * (TEXT_ZONE + 0.02 + (0.98 - TEXT_ZONE - 0.02) * t)
            y = H * y0 - math.sin(t * math.pi) * H * 0.07
            pts.append((x, y))
        ad.line(pts, fill=alpha, width=thick * scale, joint="curve")
    am = am.filter(ImageFilter.GaussianBlur(2 * scale))
    img = Image.composite(Image.new("RGB", (W, H), CYAN), img, am)

    # Щит справа: тот же знак, что на аватарке.
    scx, scy = W * 0.785, H * 0.50
    sw, sh = W * 0.145, H * 0.46
    shield = shield_shape(scx, scy, sw, sh)

    halo, hmask = radial_glow((W, H), (scx, scy), sw * 1.9, (0, 160, 230), 105)
    img = Image.composite(halo, img, hmask)

    smask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(smask).polygon(shield, fill=255)
    img = Image.composite(vgrad((W, H), (20, 140, 215), (0, 229, 255)), img, smask)

    # Молния белая — ровно как на аватарке, иначе знак выглядит разным.
    bmask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bmask).polygon(bolt(scx, scy, sh * 0.58), fill=255)
    img = Image.composite(Image.new("RGB", (W, H), (255, 255, 255)), img, bmask)

    d = ImageDraw.Draw(img)
    # Узлы — только справа и не поверх щита.
    for x, y, r in ((0.62, 0.20, 5), (0.94, 0.30, 4), (0.64, 0.80, 4), (0.93, 0.72, 5)):
        px, py, rr = W * x, H * y, r * scale
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=(150, 240, 255))

    f_title = ImageFont.truetype(BLACK_BOLD, int(H * 0.115))
    f_sub = ImageFont.truetype(BOLD, int(H * 0.055))
    f_link = ImageFont.truetype(BOLD, int(H * 0.050))

    x = W * 0.07
    d.text((x, H * 0.28), "Бесплатный VPN", font=f_title, fill=(255, 255, 255))
    d.text((x, H * 0.45), "Всегда рабочие конфиги", font=f_sub, fill=(150, 214, 245))

    link = "@vpn_dam_bot"
    bb = d.textbbox((0, 0), link, font=f_link)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad_x, pad_y = int(H * 0.034), int(H * 0.026)
    bx0, by0 = x, H * 0.615
    d.rounded_rectangle([bx0, by0, bx0 + tw + pad_x * 2, by0 + th + pad_y * 2],
                        radius=int(H * 0.034), fill=(0, 132, 196))
    d.text((bx0 + pad_x - bb[0], by0 + pad_y - bb[1]), link, font=f_link,
           fill=(255, 255, 255))

    img.resize((1280, 720), Image.LANCZOS).save(path, "PNG")
    print("баннер:  ", path)


make_avatar(OUT / "avatar.png")
make_banner(OUT / "welcome.png")
