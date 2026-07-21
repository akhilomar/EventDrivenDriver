from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

src = Path("output/hd_icons")
icons = sorted(src.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))

cell = 240
label_h = 34
cols = 6
pad = 12
rows = (len(icons) + cols - 1) // cols
Wc, Hc = cols * cell, rows * (cell + label_h)

sheet = Image.new("RGB", (Wc, Hc), (24, 24, 27))
draw = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype("arial.ttf", 20)
except OSError:
    font = ImageFont.load_default()

for i, p in enumerate(icons):
    im = Image.open(p).convert("RGBA")
    im.thumbnail((cell - 2 * pad, cell - 2 * pad), Image.LANCZOS)
    col, row = i % cols, i // cols
    x0, y0 = col * cell, row * (cell + label_h)
    cx = x0 + (cell - im.width) // 2
    cy = y0 + (cell - im.height) // 2
    sheet.paste(im, (cx, cy), im)

    label = p.name
    tb = draw.textbbox((0, 0), label, font=font)
    tw = tb[2] - tb[0]
    draw.text((x0 + (cell - tw) // 2, y0 + cell + 6), label,
              fill=(230, 230, 235), font=font)

out = Path("output/icons_index.png")
sheet.save(out)
print(f"Saved {out} ({Wc}x{Hc})")
