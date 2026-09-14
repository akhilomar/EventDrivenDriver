"""
Reframe the ORIGINAL source image to fixed aspect ratios at 4K.

Produces, alongside the usual icon processing:
  <name>_4k.png    3840px long side, ORIGINAL aspect ratio (nothing reshaped)
  <name>_2x3.png   2560 x 3840   (portrait 2:3)
  <name>_1x1.png   3840 x 3840   (square)

All three are 3840px on the long side. The _4k one keeps the source ratio
exactly -- it is just the original image at 4K (AI-upscaled when the source is
smaller than the target) -- while the other two reframe it to a fixed ratio. Default mode is "stretch": the whole image is
scaled to the exact target size on both axes, so nothing is cropped and no bars
are added -- the drawing is reshaped to fit. "fill" instead covers the canvas
without distorting (trimming the blank margin first, then cropping the
overflow), and "fit" pads the remainder (--bg colour, or "none" for
transparent).

Usage:
  python make_ratio.py "image1.jpeg"                     -> beside the source
  python make_ratio.py "image1.jpeg" --outdir out/image1
  python make_ratio.py "img.png" --mode fill --target 3840
"""
import argparse
from pathlib import Path
import numpy as np
from PIL import Image

# name -> (w, h) ratio
RATIOS = {"2x3": (2, 3), "1x1": (1, 1)}


def canvas_size(ratio: tuple[int, int], target: int) -> tuple[int, int]:
    """Canvas with `target` px on its LONG side, in the given ratio."""
    rw, rh = ratio
    if rw >= rh:
        return target, max(1, round(target * rh / rw))
    return max(1, round(target * rw / rh)), target


def content_box(im: Image.Image, tol: int = 12) -> tuple[int, int, int, int]:
    """Bounding box of real content, i.e. everything that differs from the
    page's own border colour. Lets `fill` eat the blank margin before it eats
    any of the drawing."""
    rgb = np.asarray(im.convert("RGB"), dtype=np.int16)
    h, w, _ = rgb.shape
    border = np.concatenate([rgb[0, :], rgb[-1, :], rgb[:, 0], rgb[:, -1]])
    page = np.median(border, axis=0)
    content = np.abs(rgb - page).max(axis=2) > tol
    ys, xs = np.where(content)
    if ys.size == 0:
        return 0, 0, w, h
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def to_ratio(src: Path, out_path: Path, ratio: tuple[int, int], target: int = 3840,
             mode: str = "stretch", bg: str = "white") -> tuple[int, int]:
    """Write `src` reframed to `ratio` with `target` px on the long side.

    stretch (default): the whole image is scaled to the canvas on both axes at
    once. Nothing is cropped and nothing is padded -- every part of the drawing
    is still there, resized to the target shape.
    fill: covers the canvas without distorting, trimming the blank page margin
    first and centring the crop on the content; anything still overflowing is
    cut.
    fit: scales the whole image to sit inside the canvas and pads the remainder,
    so nothing is cropped but bars appear.
    """
    im = Image.open(src)
    im = im.convert("RGBA") if (im.mode in ("RGBA", "LA", "P") or bg == "none") \
        else im.convert("RGB")
    CW, CH = canvas_size(ratio, target)

    if mode == "stretch":
        canvas = im.resize((CW, CH), Image.LANCZOS)
        canvas.save(str(out_path))
        return canvas.size

    if mode == "fill":
        cl, ct, cr, cb = content_box(im)
        im = im.crop((cl, ct, cr, cb))
        sw, sh = im.size
        scale = max(CW / sw, CH / sh)
        nw, nh = max(CW, round(sw * scale)), max(CH, round(sh * scale))
        big = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - CW) // 2, (nh - CH) // 2
        canvas = big.crop((left, top, left + CW, top + CH))
        canvas.save(str(out_path))
        return canvas.size

    sw, sh = im.size
    scale = min(CW / sw, CH / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    small = im.resize((nw, nh), Image.LANCZOS)
    pos = ((CW - nw) // 2, (CH - nh) // 2)
    if bg == "none":
        canvas = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        small = small.convert("RGBA")
        canvas.paste(small, pos, small)
    else:
        canvas = Image.new("RGB", (CW, CH), bg)
        mask = small.getchannel("A") if small.mode == "RGBA" else None
        canvas.paste(small.convert("RGB"), pos, mask)
    canvas.save(str(out_path))
    return canvas.size


def to_native_4k(src: Path, out_path: Path, target: int = 3840) -> tuple[int, int]:
    """Write `src` at `target` px on its long side, keeping its OWN aspect ratio.

    No crop, no pad, no reshape: only the resolution changes. When the source is
    smaller than the target it gets the same Real-ESRGAN pass the icons use
    (sharp edges instead of a soft Lanczos blow-up), then a Lanczos fit to the
    exact long side; an already-larger source is simply Lanczos-fitted down.
    """
    im = Image.open(src)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    # The AI pass on a whole page is minutes of CPU, so it is spent only where a
    # plain resize would visibly soften: a source under half the target. Above
    # that the blow-up is <2x and Lanczos holds up, and the page is written in
    # under a second.
    if max(im.size) < target // 2:
        try:
            from make_4k import esrgan_tiled
            alpha = im.getchannel("A") if im.mode == "RGBA" else None
            big = esrgan_tiled(im.convert("RGB"))
            if alpha is not None:
                big = big.convert("RGBA")
                big.putalpha(alpha.resize(big.size, Image.LANCZOS))
            im = big
        except Exception as e:                      # no model/torch -> plain resize
            print(f"[RATIO] AI upscale unavailable ({e}); using Lanczos.")
    w, h = im.size
    scale = target / max(w, h)
    out = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                    Image.LANCZOS)
    out.save(str(out_path))
    return out.size


def build_ratios(src: Path, outdir: Path, target: int = 3840,
                 mode: str = "stretch", bg: str = "white",
                 stem: str | None = None) -> list[Path]:
    """Write the original at 4K plus every ratio variant of `src` into `outdir`.
    Returns the paths. `stem` overrides the output basename (the caller passes a
    filesystem-safe one; a Drive filename may not be)."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    stem = stem or Path(src).stem
    native = outdir / f"{stem}_4k.png"
    w, h = to_native_4k(Path(src), native, target)
    print(f"[RATIO] original -> {w}x{h}  {native.name}")
    written.append(native)
    for label, ratio in RATIOS.items():
        out = outdir / f"{stem}_{label}.png"
        w, h = to_ratio(Path(src), out, ratio, target, mode, bg)
        print(f"[RATIO] {label} -> {w}x{h}  {out.name}")
        written.append(out)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--outdir", default=None, help="where to write (default: beside the image)")
    ap.add_argument("--target", type=int, default=3840, help="long side px (default 3840)")
    ap.add_argument("--mode", choices=("stretch", "fill", "fit"), default="stretch",
                    help="stretch = scale the whole image to the exact size, nothing "
                         "cropped or padded (default); fill = cover and crop; "
                         "fit = pad instead")
    ap.add_argument("--bg", default="white",
                    help="padding colour for --mode fit, or 'none' for transparent")
    args = ap.parse_args()
    src = Path(args.image)
    build_ratios(src, Path(args.outdir) if args.outdir else src.parent,
                 args.target, args.mode, args.bg)


if __name__ == "__main__":
    main()
