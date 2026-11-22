"""Generate browser icons from the brand logo.

    python tools/make_favicons.py

Reads static/brand/logo.png (never modified) and writes separate icon files
next to it. Only the fully transparent margin around the logo mark is left
out; the mark is centred on a square canvas (aspect ratio kept), nothing is
recoloured or redrawn.

  favicon-32/48, icon-192 : transparent background
  apple-touch-icon        : white background, because iOS shows transparent
                            areas as black (a dark logo would disappear)

It also prints the numbers used by the .logo-frame rules in static/style.css,
so the page can show the mark without its transparent margin.
"""
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.path.join(os.path.dirname(HERE), "static", "brand")
TRANSPARENT = {"favicon-32.png": 32, "favicon-48.png": 48, "icon-192.png": 192}
APPLE = ("apple-touch-icon.png", 180)


def main():
    src = os.path.join(BRAND, "logo.png")
    with Image.open(src) as im:
        im = im.convert("RGBA")
        W, H = im.size
        bbox = im.getchannel("A").getbbox()          # the visible logo mark
        mark = im.crop(bbox)
        side = max(mark.size)
        pad = round(side * 0.08)                     # breathing room inside the icon
        canvas = side + 2 * pad
        square = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        square.paste(mark, ((canvas - mark.width) // 2, (canvas - mark.height) // 2))
        for name, px in TRANSPARENT.items():
            square.resize((px, px), Image.LANCZOS).save(os.path.join(BRAND, name), optimize=True)
            print("wrote", name)
        name, px = APPLE
        big_pad = round(side * 0.18)
        inner = side + 2 * big_pad
        white = Image.new("RGBA", (inner, inner), (255, 255, 255, 255))
        white.alpha_composite(mark, ((inner - mark.width) // 2, (inner - mark.height) // 2))
        white.convert("RGB").resize((px, px), Image.LANCZOS).save(os.path.join(BRAND, name), optimize=True)
        print("wrote", name)
    l, t, r, b = bbox
    print(f"\nCSS frame numbers: canvas {W}x{H}, mark at left={l} top={t} width={r - l} height={b - t}")


if __name__ == "__main__":
    main()
