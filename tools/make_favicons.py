"""Generate browser icons from the brand logo.

    python tools/make_favicons.py

Reads static/brand/logo.png (never modified) and writes separate icon files
next to it. Only the fully transparent border around the logo is left out,
so the icon shows the logo square itself; nothing is recoloured or redrawn,
and the aspect ratio is kept (square).
"""
import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.path.join(os.path.dirname(HERE), "static", "brand")
SIZES = {"favicon-32.png": 32, "favicon-48.png": 48, "apple-touch-icon.png": 180, "icon-192.png": 192}


def main():
    src = os.path.join(BRAND, "logo.png")
    with Image.open(src) as im:
        im = im.convert("RGBA")
        bbox = im.getchannel("A").getbbox()          # the logo square, without the transparent border
        logo = im.crop(bbox)
        side = max(logo.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))
        for name, px in SIZES.items():
            square.resize((px, px), Image.LANCZOS).save(os.path.join(BRAND, name), optimize=True)
            print("wrote", name)


if __name__ == "__main__":
    main()
