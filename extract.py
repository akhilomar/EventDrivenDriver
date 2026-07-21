from pathlib import Path
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption
from docling_core.types.doc import PictureItem, TableItem

image_path = Path("image1.jpeg")
output_dir = Path("output")
output_dir.mkdir(exist_ok=True)
images_dir = output_dir / "images"
images_dir.mkdir(exist_ok=True)

# Enable image generation so detected pictures/tables are rendered and saved
pipeline_options = PdfPipelineOptions()
pipeline_options.images_scale = 2.0
pipeline_options.generate_page_images = True
pipeline_options.generate_picture_images = True

converter = DocumentConverter(
    format_options={
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
    }
)

result = converter.convert(str(image_path))
doc = result.document

# --- TEXT ---
texts = []
for item, _ in doc.iterate_items():
    if hasattr(item, "text") and item.text:
        texts.append(item.text)

text_output = output_dir / "extracted_text.txt"
text_output.write_text("\n".join(texts), encoding="utf-8")
print(f"[TEXT] {len(texts)} text blocks saved to {text_output}")

# Also save markdown (preserves structure)
md_output = output_dir / "extracted.md"
md_output.write_text(doc.export_to_markdown(), encoding="utf-8")
print(f"[TEXT] Markdown saved to {md_output}")

# --- IMAGES ---
# Save the full rendered page image
page_img_count = 0
for page_no, page in doc.pages.items():
    if page.image is not None and page.image.pil_image is not None:
        p = images_dir / f"page_{page_no}.png"
        page.image.pil_image.save(str(p))
        print(f"[IMAGE] Full page saved to {p}")
        page_img_count += 1

# Save each detected picture / figure region separately
pic_count = 0
tbl_count = 0
for item, _ in doc.iterate_items():
    if isinstance(item, PictureItem):
        img = item.get_image(doc)
        if img is not None:
            p = images_dir / f"picture_{pic_count}.png"
            img.save(str(p))
            print(f"[IMAGE] Picture region saved to {p}")
            pic_count += 1
    elif isinstance(item, TableItem):
        img = item.get_image(doc)
        if img is not None:
            p = images_dir / f"table_{tbl_count}.png"
            img.save(str(p))
            print(f"[IMAGE] Table region saved to {p}")
            tbl_count += 1

print(f"\nSummary: {len(texts)} text blocks, {page_img_count} page image(s), "
      f"{pic_count} picture region(s), {tbl_count} table region(s).")
