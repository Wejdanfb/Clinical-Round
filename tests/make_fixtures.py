"""Create synthetic test photos (no real patient images are used in tests)."""
import io, os
from PIL import Image, ImageDraw
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(HERE, exist_ok=True)
COLORS = ["#2a7f7f", "#7f4f2a", "#3d5a99", "#8a2d4b", "#4f7f2a", "#6b6b6b", "#996b00", "#205060"]

def photo(name, w, h, color, label, fmt="JPEG", exif_orientation=None, alpha=False):
    mode = "RGBA" if alpha else "RGB"
    im = Image.new(mode, (w, h), color)
    d = ImageDraw.Draw(im)
    d.rectangle([12, 12, w - 12, h - 12], outline="white", width=10)
    d.line([0, 0, w, h], fill="white", width=8)
    d.line([0, h, w, 0], fill="white", width=8)
    d.text((40, 40), label, fill="white")
    if alpha:
        d.ellipse([w // 4, h // 4, w * 3 // 4, h * 3 // 4], fill=(0, 0, 0, 0))
    path = os.path.join(HERE, name)
    kw = {}
    if exif_orientation:
        ex = Image.Exif(); ex[0x0112] = exif_orientation; ex[0x010F] = "TestCam"
        kw["exif"] = ex.tobytes()
    im.save(path, fmt, **kw)
    return path

for i in range(8):
    photo(f"portrait_{i+1}.jpg", 1200, 1600, COLORS[i], f"Portrait {i+1}")
photo("landscape_1.jpg", 2000, 1200, "#305080", "Landscape")
photo("rotated_exif6.jpg", 1600, 1200, "#806030", "EXIF 6 -> should become portrait", exif_orientation=6)
photo("screenshot.png", 900, 1400, "#405060", "PNG", fmt="PNG", alpha=True)
with open(os.path.join(HERE, "not_an_image.jpg"), "wb") as f:
    f.write(b"this is plain text pretending to be a jpeg")
good = open(os.path.join(HERE, "portrait_1.jpg"), "rb").read()
with open(os.path.join(HERE, "truncated.jpg"), "wb") as f:
    f.write(good[: len(good) // 3])
with open(os.path.join(HERE, "document.gif"), "wb") as f:
    im = Image.new("RGB", (50, 50), "red"); b = io.BytesIO(); im.save(b, "GIF"); f.write(b.getvalue())
print("fixtures in", HERE)
