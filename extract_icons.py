import sys
from pathlib import Path
import numpy as np
import torch
from PIL import Image, ImageFilter
from scipy import ndimage
from skimage.segmentation import flood
from spandrel import ModelLoader
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption
from docling_core.types.doc import PictureItem

img_path = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading upscaler on {device} ...")
model = ModelLoader().load_from_file("models/RealESRGAN_x4plus.pth").to(device).eval()
STRUCT = np.ones((3, 3), dtype=bool)  # 8-connectivity


def upscale_rgb(pil_rgb: Image.Image) -> Image.Image:
    arr = np.array(pil_rgb.convert("RGB"), dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(t)
    out = out.squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    return Image.fromarray((out * 255).round().astype(np.uint8))


def foreground_from_flood(rgb_hd: Image.Image) -> np.ndarray:
    """Boolean foreground: everything not reachable as light background from the
    borders (stops at dark strokes)."""
    arr = np.asarray(rgb_hd, dtype=np.int16)
    light = arr.min(axis=2).astype(np.float32)
    H, W = light.shape
    bg = np.zeros((H, W), dtype=bool)
    seeds = [(0, 0), (0, W - 1), (H - 1, 0), (H - 1, W - 1),
             (0, W // 2), (H - 1, W // 2), (H // 2, 0), (H // 2, W - 1)]
    for y, x in seeds:
        if light[y, x] > 185 and not bg[y, x]:
            bg |= flood(light, (y, x), tolerance=55)
    return ~bg


def isolate(fg: np.ndarray):
    """Keep only substantial connected components (drops neighbour fragments and
    text specks). Returns cleaned mask or None if it looks like text/nothing."""
    fg = ndimage.binary_opening(fg, structure=STRUCT, iterations=1)
    lbl, n = ndimage.label(fg, structure=STRUCT)
    if n == 0:
        return None
    areas = np.bincount(lbl.ravel())
    areas[0] = 0
    max_a = areas.max()
    keep = [i for i in range(1, n + 1) if areas[i] >= 0.18 * max_a]
    if len(keep) > 14:            # many blobs -> text region
        return None
    mask = np.isin(lbl, keep)
    return mask


def process():
    original = Image.open(img_path).convert("RGB")
    ow, oh = original.size

    po = PdfPipelineOptions()
    po.generate_picture_images = True
    conv = DocumentConverter(format_options={
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=po)})
    doc = conv.convert(str(img_path)).document

    saved = 0
    for item, _ in doc.iterate_items():
        if not isinstance(item, PictureItem) or not item.prov:
            continue
        prov = item.prov[0]
        page = doc.pages[prov.page_no]
        pw, ph = page.size.width, page.size.height
        bb = prov.bbox.to_top_left_origin(page_height=ph)
        sx, sy = ow / pw, oh / ph
        l, t = max(0, int(bb.l * sx)), max(0, int(bb.t * sy))
        r, b = min(ow, int(bb.r * sx)), min(oh, int(bb.b * sy))
        if r - l < 55 or b - t < 55:
            continue

        crop = original.crop((l, t, r, b))
        hd = upscale_rgb(crop)
        W, H = hd.size

        mask = isolate(foreground_from_flood(hd))
        if mask is None:
            continue

        ys, xs = np.where(mask)
        if ys.size == 0:
            continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        w, h = x1 - x0, y1 - y0
        if max(w, h) < 120:                 # too small to be a real icon
            continue
        if w / h > 4 or h / w > 4:          # banner/line -> text
            continue
        if mask.sum() / (w * h) < 0.05:     # sparse -> text
            continue

        alpha = Image.fromarray((mask * 255).astype(np.uint8), "L")
        alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))
        rgba = hd.copy()
        rgba.putalpha(alpha)

        pad = 8
        box = (max(0, x0 - pad), max(0, y0 - pad),
               min(W, x1 + pad), min(H, y1 + pad))
        rgba.crop(box).save(str(out_dir / f"icon_{saved}.png"))
        print(f"icon_{saved}.png  {box[2]-box[0]}x{box[3]-box[1]}")
        saved += 1

    print(f"\nDone. {saved} isolated icons in '{out_dir}'.")


process()
