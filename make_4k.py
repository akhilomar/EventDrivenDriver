"""
Upscale isolated icons to 4K (3840px long side) for smartboard display.

Uses a tiled Real-ESRGAN pass (AI) so large outputs don't run out of memory,
then a high-quality Lanczos fit to the exact 4K target. Transparency preserved.

Usage:
  python make_4k.py "output/image1/icons"            -> writes ".../icons_4k"
  python make_4k.py <icons_dir> --target 3840
"""
import argparse
from pathlib import Path
import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader

# anime_6B: 6 RRDB blocks (vs 23 in x4plus) -> ~3-4x faster on CPU and better on
# flat cartoon/illustration art like these icons.
MODEL_FILE = "models/RealESRGAN_x4plus_anime_6B.pth"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_model = None


def get_model():
    global _model
    if _model is None:
        print(f"Loading upscaler on {DEVICE} ...")
        _model = ModelLoader().load_from_file(MODEL_FILE).to(DEVICE).eval()
    return _model


def _feather(h, w):
    """2D triangular window for smooth tile blending."""
    wy = 1 - np.abs(np.linspace(-1, 1, h))
    wx = 1 - np.abs(np.linspace(-1, 1, w))
    return np.clip(np.outer(wy, wx), 1e-3, 1.0)[:, :, None].astype(np.float32)


def esrgan_tiled(pil_rgb: Image.Image, tile=256, overlap=32) -> Image.Image:
    """4x upscale with overlapped tiles + feathered blending (low memory)."""
    model = get_model()
    arr = np.asarray(pil_rgb.convert("RGB"), dtype=np.float32) / 255.0
    H, W, _ = arr.shape
    s = 4
    acc = np.zeros((H * s, W * s, 3), np.float32)
    wsum = np.zeros((H * s, W * s, 1), np.float32)
    step = tile - overlap
    for y0 in range(0, H, step):
        for x0 in range(0, W, step):
            y1, x1 = min(y0 + tile, H), min(x0 + tile, W)
            yb, xb = max(0, y1 - tile), max(0, x1 - tile)
            patch = arr[yb:y1, xb:x1]
            t = torch.from_numpy(patch).permute(2, 0, 1).unsqueeze(0).to(DEVICE)
            with torch.no_grad():
                o = model(t).squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
            ph, pw = o.shape[:2]
            fw = _feather(ph, pw)
            oy, ox = yb * s, xb * s
            acc[oy:oy + ph, ox:ox + pw] += o * fw
            wsum[oy:oy + ph, ox:ox + pw] += fw
    out = acc / np.clip(wsum, 1e-6, None)
    return Image.fromarray((out * 255).round().astype(np.uint8))


def to_4k(icon_path: Path, out_path: Path, target: int):
    im = Image.open(icon_path).convert("RGBA")
    rgb = im.convert("RGB")
    alpha = im.getchannel("A")
    long_side = max(im.size)

    # AI second pass ONLY for small icons that truly need it. Icons already >=1000px
    # (the base extraction is Real-ESRGAN 4x of the native crop) just get a
    # high-quality Lanczos resize — the AI pass on a large image explodes it to
    # ~10000px (hundreds of tiles, GBs of RAM) and is what caused the long hang.
    if long_side < 1000:
        rgb = esrgan_tiled(rgb)
        alpha = alpha.resize(rgb.size, Image.LANCZOS)

    # Fit exactly to the 4K target (up or down) with high-quality Lanczos.
    w, h = rgb.size
    scale = target / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    rgb = rgb.resize((nw, nh), Image.LANCZOS)
    alpha = alpha.resize((nw, nh), Image.LANCZOS)

    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    out.save(str(out_path))
    return out.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icons_dir")
    ap.add_argument("--target", type=int, default=3840, help="4K long side px")
    args = ap.parse_args()

    src = Path(args.icons_dir)
    dst = src.parent / (src.name + "_4k")
    dst.mkdir(parents=True, exist_ok=True)

    icons = sorted(src.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))
    for p in icons:
        target_file = dst / p.name
        if target_file.exists():          # resume: skip already-done icons
            print(f"{p.name} (skip, exists)")
            continue
        size = to_4k(p, target_file, args.target)
        print(f"{p.name} -> {size[0]}x{size[1]}")
    print(f"\nDone. {len(icons)} 4K icons in '{dst}'.")


if __name__ == "__main__":
    main()
