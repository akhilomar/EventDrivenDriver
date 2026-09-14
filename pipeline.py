"""
Image -> icons + text pipeline.

For an input image it:
  1. Runs docling once (layout + OCR).
  2. Extracts TEXT  -> <out>/text.txt
  3. Extracts ISOLATED ICONS (latest algorithm: native crop -> Real-ESRGAN 4x ->
     flood-fill background removal -> largest-component isolation -> autocrop)
     -> <out>/icons/icon_*.png
  4. Builds a DARK contact sheet -> <out>/<name>_dark.png
  5. (optional) Uploads the whole folder to Google Drive under a folder named
     after the image.

Usage:
  python pipeline.py "Human Respiratory system.png"            # local only
  python pipeline.py "image1.jpeg" --upload                    # + Google Drive
  python pipeline.py "img.png" --upload --parent <drive_folder_id>
"""
import argparse
from pathlib import Path
import numpy as np
import torch
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from scipy import ndimage
from skimage.segmentation import flood
from spandrel import ModelLoader
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption
from docling_core.types.doc import PictureItem
from names import safe_name

# anime_6B: 6 RRDB blocks vs 23 in x4plus -> ~3-4x faster on CPU, and better on
# flat cartoon/illustration art (which these icons are). Used for the extraction
# upscale; make_4k uses the same model for the 4K pass.
MODEL_FILE = "models/RealESRGAN_x4plus_anime_6B.pth"
STRUCT = np.ones((3, 3), dtype=bool)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# A pixel this dark counts as real ink. Everything within WALL_GROW of one is
# walled off, closing the soft ramp the background otherwise leaks through into
# a white subject. Kept below the ~200 that cream page stock sits at, so a
# textured background never walls itself off.
DARK_CORE = 180
WALL_GROW = 2
# Long side (px) the subject mask is computed at: WALL_GROW is a pixel count,
# so it only means the same thing at a fixed scale.
WORK_SCALE = 1200
# Long side (px) at or above which a region is already detailed enough to skip
# the 4x pass. The 4x exists to give a SMALL docling crop real pixels; on a
# whole-page fallback (docling found no picture, so the region is the entire
# 3000-4000px scan) it instead asks Real-ESRGAN for a ~15000px image -- thousands
# of tiles and a multi-GB accumulator, which is what ran past the batch timeout.
# make_4k still lifts the finished icon to 3840 afterwards.
UPSCALE_BELOW = 1200


_model = None


def get_model():
    global _model
    if _model is None:
        print(f"Loading upscaler on {DEVICE} ...")
        _model = ModelLoader().load_from_file(MODEL_FILE).to(DEVICE).eval()
    return _model


# ---------- icon extraction (latest algorithm) ----------
def _feather(h, w):
    wy = 1 - np.abs(np.linspace(-1, 1, h))
    wx = 1 - np.abs(np.linspace(-1, 1, w))
    return np.clip(np.outer(wy, wx), 1e-3, 1.0)[:, :, None].astype(np.float32)


def upscale_rgb(pil_rgb: Image.Image, tile=256, overlap=32) -> Image.Image:
    """Real-ESRGAN 4x. Tiles large crops so big images don't run out of memory."""
    arr = np.asarray(pil_rgb.convert("RGB"), dtype=np.float32) / 255.0
    H, W, _ = arr.shape
    model = get_model()
    s = 4
    if max(H, W) <= 400:                      # small -> single pass
        t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(DEVICE)
        with torch.no_grad():
            out = model(t).squeeze(0).permute(1, 2, 0).clamp(0, 1).cpu().numpy()
        return Image.fromarray((out * 255).round().astype(np.uint8))

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


# ---------- icon extraction ----------
def _foreground_at_scale(rgb: Image.Image) -> np.ndarray:
    """The actual background removal, always run at WORK_SCALE.

    The flood already stops at any pixel darker than its tolerance, so the only
    way background escapes into a WHITE subject -- a lab coat, litmus paper, a
    karate gi -- is across the antialiased ramp beside an outline, where a few
    intermediate pixels stay bright enough to step through. Walling off that
    ramp (every pixel near a genuinely dark one) closes the leak.

    Grading the wall by darkness rather than by gradient is what makes this
    hold: a soft shadow fading into the page has no dark core, so it builds no
    wall, and the flood clears it exactly as before.
    """
    arr = np.asarray(rgb, dtype=np.float32)
    light = arr.min(axis=2)
    H, W = light.shape

    wall = ndimage.binary_dilation(light < DARK_CORE, structure=STRUCT,
                                   iterations=WALL_GROW)
    walled = light.copy()
    walled[wall] = 0.0
    bg = np.zeros((H, W), dtype=bool)
    seeds = [(0, 0), (0, W - 1), (H - 1, 0), (H - 1, W - 1),
             (0, W // 2), (H - 1, W // 2), (H // 2, 0), (H // 2, W - 1)]
    for y, x in seeds:
        if walled[y, x] > 185 and not bg[y, x]:
            bg |= flood(walled, (y, x), tolerance=55)
    # reclaim the antialiased fringe the wall held back, but only where it is
    # still background-bright, so the outline itself stays opaque.
    bg = ndimage.binary_dilation(bg, structure=STRUCT, iterations=2) & (light > 170)
    return ~bg


def foreground_from_flood(rgb_hd: Image.Image) -> np.ndarray:
    """Subject mask for a crop, computed at WORK_SCALE and resized back.

    WALL_GROW in `_foreground_at_scale` is a pixel count, so it only means the
    same thing at a fixed scale -- on a 4x upscale the same ramp is 4x wider and
    a 2px wall would no longer close it. Working small also keeps the flood off
    a 25-megapixel array.
    """
    W0, H0 = rgb_hd.size
    if max(W0, H0) <= WORK_SCALE:
        return _foreground_at_scale(rgb_hd)
    f = WORK_SCALE / max(W0, H0)
    small = rgb_hd.resize((max(1, round(W0 * f)), max(1, round(H0 * f))),
                          Image.LANCZOS)
    mask = _foreground_at_scale(small)
    up = Image.fromarray((mask * 255).astype(np.uint8), "L").resize(
        (W0, H0), Image.BILINEAR)
    return np.asarray(up) > 127


def isolate(fg: np.ndarray):
    fg = ndimage.binary_opening(fg, structure=STRUCT, iterations=1)
    lbl, n = ndimage.label(fg, structure=STRUCT)
    if n == 0:
        return None
    areas = np.bincount(lbl.ravel())
    areas[0] = 0
    keep = [i for i in range(1, n + 1) if areas[i] >= 0.18 * areas.max()]
    if len(keep) > 14:
        return None
    mask = np.isin(lbl, keep)
    # Absorb pieces that a background leak severed from the kept body (the white
    # sleeve case): adjacent to what we kept, and not a speck. Without this the
    # 18%-of-largest rule silently deletes them.
    near = ndimage.binary_dilation(mask, structure=STRUCT, iterations=6)
    touching = np.unique(lbl[near])
    extra = [i for i in touching
             if i > 0 and i not in keep and areas[i] >= 0.002 * areas.max()]
    if extra:
        mask |= np.isin(lbl, extra)
    return mask


def _picture_boxes(doc, ow: int, oh: int) -> list[tuple[int, int, int, int]]:
    """Pixel boxes of the pictures docling found, in original-image coords."""
    boxes = []
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
        if r - l >= 55 and b - t >= 55:
            boxes.append((l, t, r, b))
    return boxes


def _save_icon(original: Image.Image, box, icons_dir: Path, index: int) -> bool:
    """Upscale one region (only if it is small), isolate the subject, save it.
    True if written."""
    l, t, r, b = box
    crop = original.crop((l, t, r, b))
    hd = upscale_rgb(crop) if max(crop.size) < UPSCALE_BELOW else crop
    W, H = hd.size
    # Mask from the NATIVE crop, not the upscaled one: it is the sharpest copy
    # of the outlines the flood walls depend on, and upscaling first only blurs
    # them. The mask is then stretched onto the 4x image.
    mask = isolate(foreground_from_flood(crop))
    if mask is None:
        return False
    if mask.shape != (H, W):
        mask = np.asarray(Image.fromarray((mask * 255).astype(np.uint8), "L")
                          .resize((W, H), Image.BILINEAR)) > 127
    ys, xs = np.where(mask)
    if ys.size == 0:
        return False
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    w, h = x1 - x0, y1 - y0
    if max(w, h) < 120 or w / h > 4 or h / w > 4 or mask.sum() / (w * h) < 0.05:
        return False
    alpha = Image.fromarray((mask * 255).astype(np.uint8), "L").filter(
        ImageFilter.GaussianBlur(0.6))
    rgba = hd.copy()
    rgba.putalpha(alpha)
    pad = 8
    crop = (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad))
    rgba.crop(crop).save(str(icons_dir / f"icon_{index}.png"))
    return True


def extract_icons(doc, original: Image.Image, icons_dir: Path) -> int:
    ow, oh = original.size
    boxes = _picture_boxes(doc, ow, oh)
    if not boxes:
        # A full-bleed illustration with labels drawn over it reads as text-only
        # to docling's layout model, so it reports no picture at all. Fall back
        # to the whole page: background removal still isolates the subject.
        print("[ICONS] docling found no picture region; using the whole image.")
        boxes = [(0, 0, ow, oh)]
    saved = 0
    for box in boxes:
        if _save_icon(original, box, icons_dir, saved + 1):   # 1-based
            saved += 1
    return saved


# ---------- text ----------
_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def ocr_reading_order(image_path: Path) -> str:
    """Full-image OCR with column-aware reading order: detect vertical gutters,
    read each column top-to-bottom, columns left-to-right. Captures far more than
    docling's region OCR (incl. stylized/colored labels)."""
    r = get_ocr()(str(image_path))
    if r.boxes is None or not len(r.boxes):
        return ""
    items = []  # (x0, y0, x1, y1, txt)
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        if score < 0.4 or not txt.strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append((min(xs), min(ys), max(xs), max(ys), txt.strip()))
    if not items:
        return ""

    page_w = max(it[2] for it in items)
    Wp = int(page_w) + 2
    # coverage from non-wide boxes only, so full-width headers/titles don't
    # bridge the gutter between columns.
    cov = np.zeros(Wp, dtype=bool)
    for x0, _, x1, _, _ in items:
        if (x1 - x0) <= 0.45 * page_w:
            cov[int(x0):int(x1) + 1] = True

    # column cut points = midpoints of wide uncovered vertical gutters
    gutter = max(20.0, page_w * 0.05)
    cuts, i = [0], 0
    while i < Wp:
        if not cov[i]:
            j = i
            while j < Wp and not cov[j]:
                j += 1
            if (j - i) >= gutter and i > 0 and j < Wp:
                cuts.append((i + j) // 2)
            i = j
        else:
            i += 1
    cuts.append(Wp)
    cols = list(zip(cuts[:-1], cuts[1:]))

    def col_of(it):
        cx = (it[0] + it[2]) / 2
        for k, (a, b) in enumerate(cols):
            if a <= cx < b:
                return k
        return len(cols) - 1

    tol = (sum(it[3] - it[1] for it in items) / len(items)) * 0.6
    lines = []
    for k in range(len(cols)):                     # columns left -> right
        ci = [it for it in items if col_of(it) == k]
        if not ci:
            continue
        ci.sort(key=lambda it: (it[1], it[0]))     # top -> bottom
        cur, top = [], None
        for it in ci:
            if top is None or it[1] <= top + tol:
                cur.append(it)
                top = it[1] if top is None else top
            else:
                lines.append(" ".join(e[4] for e in sorted(cur)))
                cur, top = [it], it[1]
        if cur:
            lines.append(" ".join(e[4] for e in sorted(cur)))
    return "\n".join(lines)


def extract_text(doc, image_path: Path, text_path: Path):
    """Primary: full-image OCR (complete + reading flow). Fallback: docling."""
    text = ""
    try:
        text = ocr_reading_order(image_path)
    except Exception as e:
        print(f"[TEXT] OCR pass failed ({e}); using docling text.")
    if not text.strip():
        try:
            text = doc.export_to_text()
        except Exception:
            text = "\n".join(it.text for it, _ in doc.iterate_items()
                             if getattr(it, "text", ""))
    text_path.write_text(text, encoding="utf-8")


# ---------- dark contact sheet ----------
def build_dark_sheet(icons_dir: Path, out_png: Path):
    icons = sorted(icons_dir.glob("icon_*.png"),
                   key=lambda p: int(p.stem.split("_")[1]))
    if not icons:
        return
    cell, label_h, cols, pad = 240, 32, 5, 12
    rows = (len(icons) + cols - 1) // cols
    W, H = cols * cell, rows * (cell + label_h)
    sheet = Image.new("RGB", (W, H), (24, 24, 27))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for i, p in enumerate(icons):
        im = Image.open(p).convert("RGBA")
        im.thumbnail((cell - 2 * pad, cell - 2 * pad), Image.LANCZOS)
        x0, y0 = (i % cols) * cell, (i // cols) * (cell + label_h)
        sheet.paste(im, (x0 + (cell - im.width) // 2, y0 + (cell - im.height) // 2), im)
        tb = draw.textbbox((0, 0), p.name, font=font)
        draw.text((x0 + (cell - (tb[2] - tb[0])) // 2, y0 + cell + 6), p.name,
                  fill=(230, 230, 235), font=font)
    sheet.save(out_png)


# ---------- Google Drive ----------
def upload_to_drive(folder: Path, drive_name: str, parent: str | None):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    creds = None
    if Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path("credentials.json").exists():
                raise SystemExit(
                    "Google Drive upload needs 'credentials.json' (OAuth desktop "
                    "client). See the setup notes printed by the assistant.")
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        Path("token.json").write_text(creds.to_json())

    svc = build("drive", "v3", credentials=creds)
    meta = {"name": drive_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    top = svc.files().create(body=meta, fields="id, webViewLink").execute()
    top_id = top["id"]

    def make_subfolder(fname):
        return svc.files().create(
            body={"name": fname, "parents": [top_id],
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id").execute()["id"]

    # top-level files (text.txt, dark sheet)
    for f in sorted(folder.glob("*")):
        if f.is_file():
            svc.files().create(
                body={"name": f.name, "parents": [top_id]},
                media_body=MediaFileUpload(str(f))).execute()

    # icon subfolders (icons/, icons_4k/, numbered/ if present)
    for subname in ("icons", "icons_4k", "numbered"):
        subdir = folder / subname
        if subdir.is_dir() and any(subdir.glob("*.png")):
            sub_id = make_subfolder(subname)
            for f in sorted(subdir.glob("*.png")):
                svc.files().create(
                    body={"name": f.name, "parents": [sub_id]},
                    media_body=MediaFileUpload(str(f))).execute()
    return top.get("webViewLink")


# ---------- reusable local processing ----------
def process_local(img_path: Path, outbase: Path = Path("output"), res4k: bool = True,
                  ratios: bool = True, ratio_mode: str = "stretch",
                  ratio_bg: str = "white"):
    """Run the full local extraction for one image. Returns the output folder.
    Produces: icons/, icons_4k/ (optional), text.txt, <name>_dark.png,
    <name>_numbered.png + numbered/, and 4K versions of the ORIGINAL image —
    <name>_4k.png (its own aspect ratio, untouched) plus the reframed
    <name>_2x3.png and <name>_1x1.png — all in one folder named after the image."""
    img_path = Path(img_path)
    name = safe_name(img_path)
    out = Path(outbase) / name
    icons_dir = out / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    # clear stale icon outputs from a previous run (keeps names.txt, and keeps
    # icons_4k so a run killed by a timeout resumes instead of starting over)
    for sub in ("icons", "numbered"):
        d = out / sub
        if d.is_dir():
            for f in d.glob("*.png"):
                f.unlink()

    import time as _t
    t0 = _t.time()
    original = Image.open(img_path).convert("RGB")
    po = PdfPipelineOptions()
    po.generate_picture_images = True
    conv = DocumentConverter(format_options={
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=po)})
    doc = conv.convert(str(img_path)).document
    t_doc = _t.time()

    extract_text(doc, img_path, out / "text.txt")
    t_txt = _t.time()
    n = extract_icons(doc, original, icons_dir)
    t_icons = _t.time()
    build_dark_sheet(icons_dir, out / f"{name}_dark.png")
    print(f"[LOCAL] {n} icons, text.txt, dark sheet -> '{out}'")
    print(f"[TIME] docling {t_doc-t0:.0f}s | text {t_txt-t_doc:.0f}s | "
          f"icons {t_icons-t_txt:.0f}s")

    if ratios:
        # 4K versions of the ORIGINAL image (not the icons): its native ratio
        # plus the reframed 2:3 and 1:1 crops. Done BEFORE the per-icon 4K loop:
        # that loop can run for many minutes on CPU and a caller timeout used to
        # kill the run before these ever got written.
        from make_ratio import build_ratios
        build_ratios(img_path, out, 3840, ratio_mode, ratio_bg, stem=name)

    if res4k:
        from make_4k import to_4k
        icons_4k = out / "icons_4k"
        icons_4k.mkdir(exist_ok=True)
        icon_files = sorted(icons_dir.glob("icon_*.png"),
                            key=lambda q: int(q.stem.split("_")[1]))
        # extraction is deterministic, so a surviving icons_4k/x.png is still the
        # 4K of this run's icons/x.png and can be reused; one with no matching
        # icon is from an older, different run and must go.
        names = {q.name for q in icon_files}
        for q in icons_4k.glob("*.png"):
            if q.name not in names:
                q.unlink()
        total = len(icon_files)
        for i, p in enumerate(icon_files, 1):
            dst = icons_4k / p.name
            if dst.exists():              # resume after a timeout kill
                print(f"[4K] {i}/{total} {p.name} (skip, exists)", flush=True)
                continue
            ts = _t.time()
            sz = to_4k(p, dst, 3840)
            print(f"[4K] {i}/{total} {p.name} -> {sz[0]}x{sz[1]} "
                  f"({_t.time()-ts:.0f}s)", flush=True)

    # numbered montage (+ numbered/ copies) in the same folder
    from montage_numbered import build_montage
    build_montage(out)
    return out


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--upload", action="store_true", help="upload folder to Google Drive")
    ap.add_argument("--parent", default=None, help="Drive parent folder id (optional)")
    ap.add_argument("--outdir", default="output", help="base output dir")
    ap.add_argument("--res4k", action="store_true",
                    help="also produce 4K (3840px) icons for smartboard display")
    ap.add_argument("--no-ratios", action="store_true",
                    help="skip the 4K versions of the original image "
                         "(native ratio, 2:3 and 1:1)")
    ap.add_argument("--ratio-mode", choices=("stretch", "fill", "fit"),
                    default="stretch",
                    help="stretch = whole image scaled to the exact size, nothing "
                         "cropped or padded (default); fill = cover and crop; "
                         "fit = pad instead")
    ap.add_argument("--ratio-bg", default="white",
                    help="padding colour for --ratio-mode fit, or 'none' for transparent")
    args = ap.parse_args()

    out = process_local(Path(args.image), Path(args.outdir), res4k=args.res4k,
                        ratios=not args.no_ratios, ratio_mode=args.ratio_mode,
                        ratio_bg=args.ratio_bg)

    if args.upload:
        link = upload_to_drive(out, out.name, args.parent)
        print(f"[DRIVE] Uploaded folder '{out.name}'. Link: {link}")


if __name__ == "__main__":
    main()
