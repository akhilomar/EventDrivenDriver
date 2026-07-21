from pathlib import Path
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption
from docling_core.types.doc import PictureItem
from rembg import remove, new_session
from PIL import Image
import io

image_path = Path("image1.jpeg")
output_dir = Path("output")
cutouts_dir = output_dir / "cutouts"
cutouts_dir.mkdir(parents=True, exist_ok=True)

# Render crops at high scale for maximum fidelity from the 1600x900 source.
pipeline_options = PdfPipelineOptions()
pipeline_options.images_scale = 4.0          # 4x -> larger, cleaner crops
pipeline_options.generate_picture_images = True

converter = DocumentConverter(
    format_options={
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
    }
)

result = converter.convert(str(image_path))
doc = result.document

# Use u2netp (good general foreground/object segmentation) with alpha matting
# for cleaner edges. Alpha matting improves edge quality on transparent cutouts.
session = new_session("u2net")

MIN_SIDE = 40  # skip tiny slivers that are usually noise, not real graphics

count = 0
for item, _ in doc.iterate_items():
    if not isinstance(item, PictureItem):
        continue
    crop = item.get_image(doc)
    if crop is None:
        continue
    if min(crop.size) < MIN_SIDE:
        continue

    # Remove background -> RGBA with transparency
    cutout = remove(
        crop,
        session=session,
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=5,
    )

    out_path = cutouts_dir / f"cutout_{count}.png"
    cutout.save(str(out_path))
    print(f"[CUTOUT] {out_path}  ({crop.size[0]}x{crop.size[1]} px)")
    count += 1

print(f"\nDone. {count} transparent cutouts saved to {cutouts_dir}")
