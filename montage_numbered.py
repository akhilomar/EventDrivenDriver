"""
Build a numbered overview sheet of a set of icons, and save numbered copies so
the numbers in the sheet map 1:1 to files on disk (1.png, 2.png, ...).
Optional names.txt in the base folder ('icon_0 = Lungs' per line) adds item names.

Usage:
  python montage_numbered.py "output/Human Respiratory system"
"""
import argparse
import re
import shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def load_names(base: Path) -> dict:
    names = {}
    f = base / "names.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                names[key.strip()] = val.strip()
    return names


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_font(size, bold=True):
    for name in (("arialbd.ttf", "arial.ttf") if bold else ("arial.ttf",)):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_montage(base: Path, src="icons", hires="icons_4k"):
    base = Path(base)
    src_dir = base / src
    hires_dir = base / hires
    icons = sorted(src_dir.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))
    if not icons:
        print(f"No icons in {src_dir}; skipping montage.")
        return None

    names = load_names(base)

    def name_for(num, p):
        return names.get(p.stem) or names.get(str(num)) or p.name

    numbered_dir = base / "numbered"
    numbered_dir.mkdir(exist_ok=True)

    cell, cols, pad, label_h = 300, 4, 58, 72
    rows = (len(icons) + cols - 1) // cols
    W = cols * cell
    H = rows * (cell + label_h) + 90
    sheet = Image.new("RGB", (W, H), (247, 247, 249))
    draw = ImageDraw.Draw(sheet)
    title_font, num_font, cap_font = load_font(38), load_font(30), load_font(22, False)

    tb = draw.textbbox((0, 0), base.name, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) // 2, 26), base.name, fill=(30, 30, 40), font=title_font)

    for i, p in enumerate(icons):
        num = int(p.stem.split("_")[1])   # badge = the file's own number
        item_name = name_for(num, p)
        source = (hires_dir / p.name) if (hires_dir / p.name).exists() else p
        shutil.copyfile(source, numbered_dir / f"{num}.png")
        if item_name != p.name:
            shutil.copyfile(source, numbered_dir / f"{num}_{slug(item_name)}.png")

        col, row = i % cols, i // cols
        x0, y0 = col * cell, 90 + row * (cell + label_h)
        draw.rounded_rectangle([x0 + 8, y0 + 8, x0 + cell - 8, y0 + cell - 8],
                               radius=18, fill=(255, 255, 255), outline=(225, 225, 230), width=2)
        im = Image.open(p).convert("RGBA")
        im.thumbnail((cell - 3 * pad, cell - 3 * pad), Image.LANCZOS)
        sheet.paste(im, (x0 + (cell - im.width) // 2, y0 + (cell - im.height) // 2), im)

        bx, by, r = x0 + 30, y0 + 30, 26
        draw.ellipse([bx - r, by - r, bx + r, by + r], fill=(37, 99, 235))
        nb = draw.textbbox((0, 0), str(num), font=num_font)
        draw.text((bx - (nb[2] - nb[0]) / 2, by - (nb[3] - nb[1]) / 2 - nb[1]),
                  str(num), fill=(255, 255, 255), font=num_font)

        cb = draw.textbbox((0, 0), item_name, font=num_font)
        draw.text((x0 + (cell - (cb[2] - cb[0])) // 2, y0 + cell + 4), item_name,
                  fill=(30, 30, 40), font=num_font)
        sub = f"#{num}  ·  {p.name}"
        sbb = draw.textbbox((0, 0), sub, font=cap_font)
        draw.text((x0 + (cell - (sbb[2] - sbb[0])) // 2, y0 + cell + 36), sub,
                  fill=(120, 120, 130), font=cap_font)

    out = base / f"{base.name}_numbered.png"
    sheet.save(out)
    print(f"Saved sheet: {out} ({W}x{H})")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--src", default="icons")
    ap.add_argument("--hires", default="icons_4k")
    a = ap.parse_args()
    build_montage(Path(a.base), a.src, a.hires)
