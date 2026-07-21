import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

src = Path(sys.argv[1])
out = Path(sys.argv[2])
icons = sorted(src.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))

cell, label_h, cols, pad = 260, 34, 5, 12
rows = (len(icons) + cols - 1) // cols or 1
Wc, Hc = cols * cell, rows * (cell + label_h)
sheet = Image.new("RGB", (Wc, Hc), (24, 24, 27))
draw = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype("arial.ttf", 18)
except OSError:
    font = ImageFont.load_default()

for i, p in enumerate(icons):
    im = Image.open(p).convert("RGBA")
    im.thumbnail((cell - 2 * pad, cell - 2 * pad), Image.LANCZOS)
    col, row = i % cols, i // cols
    x0, y0 = col * cell, row * (cell + label_h)
    sheet.paste(im, (x0 + (cell - im.width) // 2, y0 + (cell - im.height) // 2), im)
    tb = draw.textbbox((0, 0), p.name, font=font)
    draw.text((x0 + (cell - (tb[2] - tb[0])) // 2, y0 + cell + 6), p.name,
              fill=(230, 230, 235), font=font)

sheet.save(out)
print(f"Saved {out} ({Wc}x{Hc})")
