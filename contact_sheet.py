from pathlib import Path
from PIL import Image

src = Path("output/hd_icons")
icons = sorted(src.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))

cell = 220
cols = 6
pad = 10
rows = (len(icons) + cols - 1) // cols
Wc, Hc = cols * cell, rows * cell

# Dark background reveals any leftover white halo/residue.
sheet = Image.new("RGB", (Wc, Hc), (20, 20, 22))
for i, p in enumerate(icons):
    im = Image.open(p).convert("RGBA")
    im.thumbnail((cell - 2 * pad, cell - 2 * pad), Image.LANCZOS)
    cx = (i % cols) * cell + (cell - im.width) // 2
    cy = (i // cols) * cell + (cell - im.height) // 2
    sheet.paste(im, (cx, cy), im)

out = Path("output/contact_sheet_dark.png")
sheet.save(out)
print(f"Saved {out} ({Wc}x{Hc})")
