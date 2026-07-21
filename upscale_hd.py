import sys
from pathlib import Path
import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader
from rembg import remove, new_session

# Choose model: "general" (photos/mixed illustrations) or "anime" (flat cartoon/line art)
MODEL_CHOICE = sys.argv[1] if len(sys.argv) > 1 else "general"
MODEL_FILE = {
    "general": "models/RealESRGAN_x4plus.pth",
    "anime":   "models/RealESRGAN_x4plus_anime_6B.pth",
}[MODEL_CHOICE]

src_dir = Path("output/native")                       # native 1:1 crops (best source)
hd_dir = Path(f"output/hd_{MODEL_CHOICE}")             # 4x upscaled, opaque
hd_cutout_dir = Path(f"output/hd_{MODEL_CHOICE}_cutouts")  # 4x upscaled, transparent
hd_dir.mkdir(parents=True, exist_ok=True)
hd_cutout_dir.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading {MODEL_FILE} on {device} ...")
model = ModelLoader().load_from_file(MODEL_FILE).to(device).eval()

session = new_session("u2net")

def upscale(pil_img: Image.Image) -> Image.Image:
    arr = np.array(pil_img.convert("RGB"), dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(t)
    out = out.squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    return Image.fromarray((out * 255).round().astype(np.uint8))

icons = sorted(src_dir.glob("icon_*.png"), key=lambda p: int(p.stem.split("_")[1]))
for p in icons:
    src = Image.open(p)
    hd = upscale(src)
    hd.save(str(hd_dir / p.name))

    # Plain mask (no alpha matting): light on memory, clean edges at high res.
    cut = remove(hd, session=session, post_process_mask=True)
    cut.save(str(hd_cutout_dir / p.name))

    print(f"{p.name}: {src.size[0]}x{src.size[1]} -> {hd.size[0]}x{hd.size[1]}")

print(f"\nDone. HD icons in '{hd_dir}', transparent HD in '{hd_cutout_dir}'.")
