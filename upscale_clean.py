from pathlib import Path
import numpy as np
import torch
from PIL import Image, ImageFilter
from spandrel import ModelLoader
from rembg import remove, new_session

MODEL_FILE = "models/RealESRGAN_x4plus.pth"
src_dir = Path("output/native")
out_dir = Path("output/hd_clean_cutouts")
out_dir.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading upscaler on {device} ...")
model = ModelLoader().load_from_file(MODEL_FILE).to(device).eval()

# u2net gave the tightest object masks for these icons.
session = new_session("u2net")


def upscale_rgb(pil_rgb: Image.Image) -> Image.Image:
    arr = np.array(pil_rgb.convert("RGB"), dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(t)
    out = out.squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    return Image.fromarray((out * 255).round().astype(np.uint8))


icons = sorted(src_dir.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))
for p in icons:
    src = Image.open(p).convert("RGB")

    # 1) Sharp HD image.
    rgb_hd = upscale_rgb(src)
    W, H = rgb_hd.size

    # 2) Object mask at native size, upscaled smoothly to HD.
    mask = remove(src, session=session, only_mask=True, post_process_mask=True)
    mask_hd = mask.resize((W, H), Image.LANCZOS)

    a = np.asarray(mask_hd, dtype=np.float32) / 255.0
    rgb = np.asarray(rgb_hd, dtype=np.float32)

    # 3) Defringe: in the soft edge band, drop pixels that are whitish
    #    (the leftover background halo) so no white rim survives.
    near_white = rgb.min(axis=2) > 224
    edge_band = (a > 0.03) & (a < 0.92)
    a[near_white & edge_band] = 0.0

    # 4) Firm up the edge: remap so soft partials become clearly in/out.
    a = np.clip((a - 0.30) / 0.40, 0.0, 1.0)

    alpha = Image.fromarray((a * 255).round().astype(np.uint8), "L")
    # 5) Pull edge in ~1px as a final guard against any 1px rim.
    alpha = alpha.filter(ImageFilter.MinFilter(3))

    out = rgb_hd.copy()
    out.putalpha(alpha)
    out.save(str(out_dir / p.name))
    print(f"{p.name}: -> {W}x{H}")

print(f"\nDone. Clean HD cutouts in '{out_dir}'.")
