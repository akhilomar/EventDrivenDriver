from pathlib import Path
import numpy as np
import torch
from PIL import Image, ImageFilter
from skimage.segmentation import flood
from spandrel import ModelLoader

MODEL_FILE = "models/RealESRGAN_x4plus.pth"
src_dir = Path("output/native")
out_dir = Path("output/hd_icons")
out_dir.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading upscaler on {device} ...")
model = ModelLoader().load_from_file(MODEL_FILE).to(device).eval()


def upscale_rgb(pil_rgb: Image.Image) -> Image.Image:
    arr = np.array(pil_rgb.convert("RGB"), dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(t)
    out = out.squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    return Image.fromarray((out * 255).round().astype(np.uint8))


def flood_background(rgb_hd: Image.Image) -> Image.Image:
    """Remove the contiguous light background from all borders, stopping at
    the dark outline stroke. Returns an alpha channel (L)."""
    arr = np.asarray(rgb_hd, dtype=np.int16)
    light = arr.min(axis=2).astype(np.float32)   # high where whitish
    H, W = light.shape

    bg = np.zeros((H, W), dtype=bool)
    # Seed from border midpoints + corners; only if that pixel is background-light.
    seeds = [(0, 0), (0, W - 1), (H - 1, 0), (H - 1, W - 1),
             (0, W // 2), (H - 1, W // 2), (H // 2, 0), (H // 2, W - 1)]
    for y, x in seeds:
        if light[y, x] > 185 and not bg[y, x]:
            bg |= flood(light, (y, x), tolerance=55)

    alpha = np.where(bg, 0, 255).astype(np.uint8)
    a = Image.fromarray(alpha, "L")
    # Eat any 1px light rim so the edge sits on the dark stroke, then a hair of
    # blur for a smooth (not jagged) anti-aliased stroke edge.
    a = a.filter(ImageFilter.MinFilter(3))
    a = a.filter(ImageFilter.GaussianBlur(0.6))
    return a


icons = sorted(src_dir.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))
for p in icons:
    src = Image.open(p).convert("RGB")
    rgb_hd = upscale_rgb(src)
    alpha = flood_background(rgb_hd)

    removed = 100 * (np.asarray(alpha) < 10).mean()
    out = rgb_hd.copy()
    out.putalpha(alpha)
    out.save(str(out_dir / p.name))
    print(f"{p.name}: {rgb_hd.size[0]}x{rgb_hd.size[1]}  background removed: {removed:4.1f}%")

print(f"\nDone. Perfect-stroke HD icons in '{out_dir}'.")
