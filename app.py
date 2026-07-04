import io
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import img2pdf
import streamlit as st
from markdown_pdf import MarkdownPdf, Section
from PIL import Image

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

st.set_page_config(
    page_title="File to PDF Converter",
    page_icon="📄",
    layout="centered",
)

MARKDOWN_CSS = """
body {
    font-family: Helvetica, Arial, sans-serif;
    color: #1f2937;
    font-size: 11pt;
    line-height: 1.6;
}
h1, h2, h3, h4, h5, h6 {
    color: #111827;
    margin-top: 18px;
    margin-bottom: 8px;
    line-height: 1.25;
}
h1 { text-align: center; }
code {
    font-family: Courier, monospace;
    background: #f3f4f6;
}
pre {
    background: #f3f4f6;
    border: 1px solid #e5e7eb;
    padding: 10px;
    white-space: pre-wrap;
}
blockquote {
    border-left: 4px solid #d1d5db;
    color: #4b5563;
    padding-left: 12px;
}
table, th, td {
    border: 1px solid #d1d5db;
    border-collapse: collapse;
}
th, td { padding: 8px; vertical-align: top; }
a { color: #2563eb; }
"""

QUALITY_PRESETS = {
    "Original (lossless)": {"max_dim": None, "jpeg_quality": None},
    "Balanced":            {"max_dim": 2048, "jpeg_quality": 82},
    "Compressed":          {"max_dim": 1280, "jpeg_quality": 65},
    "Smallest":            {"max_dim": 900,  "jpeg_quality": 40},
}

MAX_TOTAL_UPLOAD_MB = 300  # guardrail to avoid holding too much in memory at once
MAX_WORKERS = 4            # conservative thread count to avoid CPU/memory spikes


# ── System resource display ────────────────────────────────────────────────────

def render_system_stats():
    if not PSUTIL_AVAILABLE:
        st.caption("ℹ️ System stats unavailable (psutil not installed).")
        return

    try:
        vm = psutil.virtual_memory()
        # Non-blocking call: uses the delta since the last call instead of sleeping.
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True) or 1

        col1, col2, col3 = st.columns(3)
        col1.metric(
            "🧠 RAM used",
            f"{vm.used / (1024 ** 3):.1f} / {vm.total / (1024 ** 3):.1f} GB",
            f"{vm.percent:.0f}%",
        )
        col2.metric("⚙️ CPU usage", f"{cpu_percent:.0f}%")
        col3.metric("🧮 CPU cores", f"{cpu_count}")
    except Exception:
        st.caption("ℹ️ System stats temporarily unavailable.")


# ── Helpers ─────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def natural_sort_key(text: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def sort_uploaded_images(files, sort_mode: str):
    reverse = sort_mode == "Filename Z → A"
    return sorted(files, key=lambda f: natural_sort_key(f.name), reverse=reverse)


def has_heif_files(files) -> bool:
    heif_exts = {"heif", "heic", "heifs", "heics", "hif"}
    return any(f.name.rsplit(".", 1)[-1].lower() in heif_exts for f in files)


def ensure_heif_support() -> bool:
    """Lazily register the HEIF opener only when a HEIF/HEIC file is present.
    Returns True if registration succeeded, False otherwise (missing dependency)."""
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        return True
    except Exception:
        return False


def preprocess_image_bytes(file_bytes: bytes, max_dim, jpeg_quality) -> bytes:
    """
    Pure function (no Streamlit calls, no caching) so it is safe to run inside
    worker threads: decode → optionally resize → re-encode to JPEG.
    Returns raw bytes unchanged if no resize/re-encode requested (lossless).
    """
    if max_dim is None and jpeg_quality is None:
        return file_bytes

    with Image.open(io.BytesIO(file_bytes)) as img:
        img.load()  # force decode while the BytesIO buffer is still in scope

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        if max_dim is not None:
            w, h = img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue()


def convert_images_to_pdf(uploaded_files, quality_preset: str):
    preset = QUALITY_PRESETS[quality_preset]
    max_dim = preset["max_dim"]
    jpeg_quality = preset["jpeg_quality"]

    raw_bytes_list = []
    for f in uploaded_files:
        f.seek(0)
        raw_bytes_list.append(f.read())
        f.seek(0)

    results = [None] * len(raw_bytes_list)
    errors = []

    worker_count = max(1, min(MAX_WORKERS, len(raw_bytes_list)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_index = {
            executor.submit(preprocess_image_bytes, raw, max_dim, jpeg_quality): idx
            for idx, raw in enumerate(raw_bytes_list)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                errors.append((uploaded_files[idx].name, str(exc)))

    if errors:
        error_list = ", ".join(f"{name} ({msg})" for name, msg in errors)
        raise ValueError(f"Failed to process {len(errors)} file(s): {error_list}")

    return img2pdf.convert(results)


def read_uploaded_text(uploaded_file) -> str:
    raw = uploaded_file.getvalue()
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@st.cache_data(show_spinner=False, max_entries=20, ttl=1800)
def convert_markdown_to_pdf(markdown_text: str, document_title: str) -> bytes:
    has_h1 = any(line.startswith("# ") or line == "#" for line in markdown_text.splitlines())
    toc_level = 2 if has_h1 else 0
    pdf = MarkdownPdf(toc_level=toc_level)
    pdf.meta["title"] = document_title
    pdf.add_section(Section(markdown_text, toc=bool(toc_level)), user_css=MARKDOWN_CSS)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = tmp.name
    try:
        pdf.save(tmp_path)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("📄 File to PDF Converter")
render_system_stats()
st.markdown("Convert **images** or **Markdown** files into downloadable PDF documents.")

image_tab, markdown_tab = st.tabs(["🖼️ Image to PDF", "📝 Markdown to PDF"])

# ── Image tab ─────────────────────────────────────────────────────────────────
with image_tab:
    st.subheader("Image to PDF")
    st.write("Upload images, choose a sort order and quality preset, then convert.")

    uploaded_images = st.file_uploader(
        "Choose image files",
        type=["jpg", "jpeg", "png", "webp", "heif", "heic", "heifs", "heics", "hif"],
        accept_multiple_files=True,
        help="Supports JPG, PNG, WebP, and HEIF/HEIC formats.",
        key="image_uploader",
    )

    if uploaded_images:
        total_mb = sum(f.size for f in uploaded_images) / (1024 * 1024)
        st.success(f"✓ {len(uploaded_images)} file(s) uploaded ({total_mb:.1f} MB total)")

        if total_mb > MAX_TOTAL_UPLOAD_MB:
            st.warning(
                f"⚠️ You've uploaded {total_mb:.0f} MB of images, which exceeds the "
                f"{MAX_TOTAL_UPLOAD_MB} MB guideline. Converting this much data at once "
                "may use a lot of memory and could cause the app to crash. Consider using "
                "the Compressed or Smallest quality preset, or converting in smaller batches."
            )

        heif_present = has_heif_files(uploaded_images)
        heif_ready = True
        if heif_present:
            heif_ready = ensure_heif_support()
            if not heif_ready:
                st.error(
                    "❌ HEIF/HEIC files were detected, but the `pillow-heif` library "
                    "isn't available. Please install it or remove HEIF files."
                )

        with st.form("image_settings_form"):
            col_sort, col_quality = st.columns(2)

            with col_sort:
                sort_mode = st.radio(
                    "Sort order",
                    options=["Filename A → Z", "Filename Z → A"],
                    key="image_sort_mode",
                )

            with col_quality:
                quality_preset = st.radio(
                    "PDF quality",
                    options=list(QUALITY_PRESETS.keys()),
                    key="image_quality",
                    help=(
                        "**Original**: lossless — images embedded as-is, largest files.\n\n"
                        "**Balanced**: resizes images > 2048 px and re-encodes to JPEG 82.\n\n"
                        "**Compressed**: resizes to 1280 px max and re-encodes to JPEG 65.\n\n"
                        "**Smallest**: resizes to 900 px max and re-encodes to JPEG 40 — most aggressive, smallest files."
                    ),
                )

            image_pdf_filename = st.text_input(
                "Output filename (without extension)",
                value="converted_images",
                key="image_filename",
            )

            submitted = st.form_submit_button(
                "🔄 Convert images to PDF", type="primary", disabled=(heif_present and not heif_ready)
            )

        sorted_images = sort_uploaded_images(uploaded_images, st.session_state["image_sort_mode"])

        with st.expander("Sorted image list"):
            st.caption("The PDF page order will follow this list.")
            for idx, f in enumerate(sorted_images, 1):
                st.write(f"{idx}. {f.name}")

        if submitted:
            try:
                with st.spinner("Converting images to PDF..."):
                    pdf_bytes = convert_images_to_pdf(sorted_images, st.session_state["image_quality"])

                final_name = sanitize_filename(st.session_state["image_filename"], "converted_images")
                st.success("✓ PDF created successfully!")
                st.download_button(
                    label="📥 Download image PDF",
                    data=pdf_bytes,
                    file_name=f"{final_name}.pdf",
                    mime="application/pdf",
                    type="primary",
                    key="image_download",
                )
                pdf_size_mb = len(pdf_bytes) / (1024 * 1024)
                st.info(f"📊 PDF size: {pdf_size_mb:.2f} MB | Pages: {len(sorted_images)} | Quality: {st.session_state['image_quality']}")
            except Exception as exc:
                st.error(f"❌ Error converting images to PDF: {exc}")
    else:
        st.info("👆 Upload image files to get started")

# ── Markdown tab ──────────────────────────────────────────────────────────────
with markdown_tab:
    st.subheader("Markdown to PDF")
    st.write("Upload a Markdown file **or** paste plain text / Markdown directly.")

    md_source = st.radio(
        "Input source",
        options=["Upload .md file", "Paste text"],
        horizontal=True,
        key="md_source",
    )

    markdown_text = None
    default_name = "converted_markdown"

    if md_source == "Upload .md file":
        uploaded_markdown = st.file_uploader(
            "Choose a Markdown file",
            type=["md", "markdown"],
            accept_multiple_files=False,
            key="markdown_uploader",
        )
        if uploaded_markdown is not None:
            markdown_text = read_uploaded_text(uploaded_markdown)
            default_name = sanitize_filename(
                uploaded_markdown.name.rsplit(".", 1)[0], "converted_markdown"
            )
            st.success(f"✓ Uploaded: {uploaded_markdown.name}")
        else:
            st.info("👆 Upload a Markdown file to get started")

    else:  # Paste text
        pasted = st.text_area(
            "Paste your text or Markdown here",
            height=300,
            placeholder='# My Document\n\nPaste plain text or Markdown here...',
            key="md_paste",
        )
        if pasted.strip():
            markdown_text = pasted
        else:
            st.info("👆 Paste some text above to get started")

    if markdown_text:
        markdown_pdf_filename = st.text_input(
            "Output filename (without extension)",
            value=default_name,
            key="markdown_filename",
        )

        with st.expander("Preview"):
            st.markdown(markdown_text)

        with st.expander("Show raw Markdown"):
            st.code(markdown_text, language="markdown")

        if st.button("🔄 Convert to PDF", type="primary", key="markdown_convert"):
            try:
                with st.spinner("Converting Markdown to PDF..."):
                    final_name = sanitize_filename(markdown_pdf_filename, "converted_markdown")
                    pdf_bytes = convert_markdown_to_pdf(markdown_text, final_name)

                st.success("✓ PDF created successfully!")
                st.download_button(
                    label="📥 Download Markdown PDF",
                    data=pdf_bytes,
                    file_name=f"{final_name}.pdf",
                    mime="application/pdf",
                    type="primary",
                    key="markdown_download",
                )
                pdf_size_kb = len(pdf_bytes) / 1024
                st.info(f"📊 PDF size: {pdf_size_kb:.1f} KB")
            except Exception as exc:
                st.error(f"❌ Error converting Markdown to PDF: {exc}")
    else:
        st.info("👆 Paste some text above to get started")

# ── Footer ────────────────────────────────────────────────────────────────────
with st.expander("ℹ️ Features"):
    st.markdown(
        """
- **Image to PDF**: supports JPG, PNG, WebP, and HEIF/HEIC formats.
- **Sorting**: images are sorted by filename (A→Z or Z→A) before conversion.
- **PDF quality presets**:
  - *Original* — lossless, images embedded as-is.
  - *Balanced* — resizes images > 2048 px and re-encodes to JPEG quality 82.
  - *Compressed* — resizes to 1280 px max and re-encodes to JPEG quality 65.
  - *Smallest* — resizes to 900 px max and re-encodes to JPEG quality 40, for the smallest possible files.
- **Markdown to PDF**: powered by `markdown-pdf`; accepts uploaded `.md` files or pasted plain text / Markdown.
- **Reliability**: bounded caching, per-file error isolation, and an upload size guardrail help prevent crashes.
        """
    )

st.markdown("---")
st.caption("Built with Streamlit · img2pdf · pillow-heif · markdown-pdf · psutil")
