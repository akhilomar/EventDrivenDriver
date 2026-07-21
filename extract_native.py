from pathlib import Path
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption
from docling_core.types.doc import PictureItem
from rembg import remove, new_session
from PIL import Image

image_path = Path("image1.jpeg")
output_dir = Path("output")
native_dir = output_dir / "native"            # 1:1 crops, exactly as in source
native_cutout_dir = output_dir / "native_cutouts"  # same, background removed
native_dir.mkdir(parents=True, exist_ok=True)
native_cutout_dir.mkdir(parents=True, exist_ok=True)

# Load the ORIGINAL image at full native resolution — crops come from this,
# so there is zero upscaling/resampling: pixel-for-pixel identical to source.
original = Image.open(image_path).convert("RGB")
orig_w, orig_h = original.size

# We only need docling for the layout (where the pictures are). Scale is
# irrelevant to crop quality because we crop from `original`, not the page.
pipeline_options = PdfPipelineOptions()
pipeline_options.generate_picture_images = True

converter = DocumentConverter(
    format_options={
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
    }
)
result = converter.convert(str(image_path))
doc = result.document

session = new_session("u2net")
MIN_SIDE = 40
count = 0

for item, _ in doc.iterate_items():
    if not isinstance(item, PictureItem) or not item.prov:
        continue
    prov = item.prov[0]
    page = doc.pages[prov.page_no]
    pw, ph = page.size.width, page.size.height

    # Map bbox (page coords) -> original pixel coords, top-left origin.
    bb = prov.bbox.to_top_left_origin(page_height=ph)
    sx, sy = orig_w / pw, orig_h / ph
    left, top = max(0, int(bb.l * sx)), max(0, int(bb.t * sy))
    right, bottom = min(orig_w, int(bb.r * sx)), min(orig_h, int(bb.b * sy))
    if right - left < MIN_SIDE or bottom - top < MIN_SIDE:
        continue

    # 1:1 native crop — as sharp as the source allows, no scaling.
    crop = original.crop((left, top, right, bottom))
    crop.save(str(native_dir / f"icon_{count}.png"))

    # Background-removed version at the same native resolution.
    cutout = remove(crop, session=session, alpha_matting=True,
                    alpha_matting_foreground_threshold=240,
                    alpha_matting_background_threshold=10,
                    alpha_matting_erode_size=5)
    cutout.save(str(native_cutout_dir / f"icon_{count}.png"))

    print(f"icon_{count}.png  {crop.size[0]}x{crop.size[1]} px")
    count += 1

print(f"\nDone. {count} native-size icons in '{native_dir}' "
      f"and transparent versions in '{native_cutout_dir}'.")
