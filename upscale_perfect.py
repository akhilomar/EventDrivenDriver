from pathlib import Path
import numpy as np
import torch
from PIL import Image, ImageFilter
from spandrel import ModelLoader
from rembg import remove, new_session

MODEL_FILE = "models/RealESRGAN_x4plus.pth"
src_dir = Path("output/native")
out_dir = Path("output/hd_perfect_cutouts")
out_dir.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading upscaler on {device} ...")
model = ModelLoader().load_from_file(MODEL_FILE).to(device).eval()

# isnet-general-use: cleaner, tighter masks than u2net.
session = new_session("isnet-general-use")


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

    # 1) Clean cutout at NATIVE size with alpha matting + foreground
    #    decontamination -> removes the white halo, memory-safe because small.
    rgba = remove(
        src, session=session,
        alpha_matting=True,
        alpha_matting_foreground_threshold=250,
        alpha_matting_background_threshold=5,
        alpha_matting_erode_size=3,
    ).convert("RGBA")

    rgb = rgba.convert("RGB")            # decontaminated foreground colors
    alpha = rgba.getchannel("A")

    # 2) Upscale the (decontaminated) RGB with Real-ESRGAN.
    rgb_hd = upscale_rgb(rgb)
    w, h = rgb_hd.size

    # 3) Upscale the clean alpha smoothly and recombine.
    alpha_hd = alpha.resize((w, h), Image.LANCZOS)

    # 4) Defringe: pull the edge in by ~1px so no stray white rim survives.
    alpha_hd = alpha_hd.filter(ImageFilter.MinFilter(3))

    out = rgb_hd.copy()
    out.putalpha(alpha_hd)
    out.save(str(out_dir / p.name))
    print(f"{p.name}: -> {w}x{h}")

print(f"\nDone. Perfect HD cutouts in '{out_dir}'.")
