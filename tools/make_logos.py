"""Derive web-sized logo assets from the original artwork in docs/.

    uvx --with pillow python tools/make_logos.py    (from the repository root)
"""
from PIL import Image, ImageDraw

STATIC = "server/otter/static"
img = Image.open("docs/logo_no_bg.png").convert("RGBA")

# The emblem comes from the dark-background artwork, where the circle doesn't touch the
# wordmark (in the transparent one, the letters overlap its bottom edge).
art = Image.open("docs/logo_bg.png").convert("RGB")
bg = art.getpixel((10, 10))
w, h = art.size
# Pixels clearly different from the background, above the wordmark.
upper = art.crop((0, 0, w, int(h * 0.66)))
visible = upper.point(lambda v: 255 if v > 60 else 0).convert("L").point(lambda v: 255 if v > 0 else 0)
left, top, right, bottom = visible.getbbox()
radius = (right - left) / 2
cx, cy = (left + right) / 2, top + radius
print(f"circle: center=({cx:.0f},{cy:.0f}) radius={radius:.0f}  (bbox bottom {bottom})")

box = (round(cx - radius), round(cy - radius), round(cx + radius), round(cy + radius))
mark = art.crop(box).convert("RGBA")
# Supersampled circular mask for a smooth edge.
big = Image.new("L", (mark.width * 4, mark.height * 4), 0)
ImageDraw.Draw(big).ellipse((0, 0, big.width - 1, big.height - 1), fill=255)
mark.putalpha(big.resize(mark.size, Image.LANCZOS))

def save(image, path, size, **kw):
    out = image.resize((size, size), Image.LANCZOS)
    out.save(path, optimize=True, **kw)
    print(f"{path}: {size}px")

save(mark, f"{STATIC}/logo-mark.png", 128)      # header, shown at 32 px (retina-ready)
save(mark, f"{STATIC}/favicon.png", 64)
# iOS home screen icons can't be transparent: emblem on the artwork's dark background.
touch = Image.new("RGBA", (180, 180), bg + (255,))
small = mark.resize((152, 152), Image.LANCZOS)
touch.alpha_composite(small, (14, 14))
touch.convert("RGB").save(f"{STATIC}/apple-touch-icon.png", optimize=True)
print(f"{STATIC}/apple-touch-icon.png: 180px on {bg}")

# README: the full transparent logo, 512 px, palette-quantized to stay light.
readme = img.resize((512, 512), Image.LANCZOS)
readme.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save("docs/logo_readme.png", optimize=True)
print("docs/logo_readme.png: 512px")
