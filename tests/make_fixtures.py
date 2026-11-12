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
# --- iPhone-style files -----------------------------------------------------
# JPEG with an extra embedded image (HDR gain map) -> Pillow reports "MPO"
main_img = Image.new("RGB", (1200, 1600), "#2a6f7f")
ImageDraw.Draw(main_img).text((40, 40), "iPhone JPEG + gain map", fill="white")
main_img.save(os.path.join(HERE, "IMG_1530.jpeg"), "MPO", save_all=True,
              append_images=[Image.new("RGB", (600, 800), (128, 128, 128))])
# PNG content saved with a .jpeg name (wrong extension)
photo("png_named.jpeg", 800, 1000, "#5a3f7f", "PNG named .jpeg", fmt="PNG")
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    h = Image.new("RGB", (1200, 1600), "#7f2a4b")
    ImageDraw.Draw(h).text((40, 40), "HEIC", fill="white")
    h.save(os.path.join(HERE, "IMG_2001.HEIC"), "HEIF", quality=80)
    # HEIC whose camera orientation says "rotate 90" (stored landscape, shown portrait)
    r = Image.new("RGB", (1600, 1200), "#2a7f4b")
    ex = Image.Exif(); ex[0x0112] = 6
    r.save(os.path.join(HERE, "IMG_2002_rotated.heic"), "HEIF", quality=80, exif=ex.tobytes())
    # real HEIC content but named .jpeg
    h.save(os.path.join(HERE, "heic_named.jpeg"), "HEIF", quality=80)
    data = open(os.path.join(HERE, "IMG_2001.HEIC"), "rb").read()
    with open(os.path.join(HERE, "truncated.heic"), "wb") as f:
        f.write(data[: len(data) // 3])
except ImportError:
    print("pillow-heif not installed: HEIC fixtures skipped")
with open(os.path.join(HERE, "fake.heic"), "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(200))   # MP4 video header, not a photo
print("fixtures in", HERE)
