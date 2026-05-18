import tempfile
import re
from pathlib import Path

import img2pdf
import streamlit as st
from markdown_pdf import MarkdownPdf, Section
from PIL import Image


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
th, td {
    padding: 8px;
    vertical-align: top;
}
a {
    color: #2563eb;
}
"""


def sanitize_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def natural_sort_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def sort_uploaded_images(files, sort_mode: str):
    reverse = sort_mode == "Filename Z → A"
    return sorted(files, key=lambda item: natural_sort_key(item.name), reverse=reverse)


def convert_images_to_pdf(uploaded_files):
    image_bytes_list = []
    for uploaded_file in uploaded_files:
        uploaded_file.seek(0)
        image_bytes_list.append(uploaded_file.read())
    return img2pdf.convert(image_bytes_list)


def read_uploaded_text(uploaded_file) -> str:
    raw = uploaded_file.getvalue()
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def convert_markdown_to_pdf(markdown_text: str, document_title: str):
    pdf = MarkdownPdf(toc_level=2)
    pdf.meta["title"] = document_title
    pdf.add_section(Section(markdown_text), user_css=MARKDOWN_CSS)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
        tmp_path = tmp_pdf.name

    try:
        pdf.save(tmp_path)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


st.title("📄 File to PDF Converter")
st.markdown("Convert **images** or **Markdown** files into downloadable PDF documents.")

image_tab, markdown_tab = st.tabs(["🖼️ Image to PDF", "📝 Markdown to PDF"])

with image_tab:
    st.subheader("Image to PDF")
    st.write("Upload one or more JPG, PNG, or WebP files, sort them, and combine them into a single PDF.")

    uploaded_images = st.file_uploader(
        "Choose image files",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        help="Select one or more image files to convert to a single PDF.",
        key="image_uploader",
    )

    if uploaded_images:
        st.success(f"✓ {len(uploaded_images)} file(s) uploaded")

        sort_mode = st.radio(
            "Sort images before conversion",
            options=["Filename A → Z", "Filename Z → A"],
            horizontal=True,
            key="image_sort_mode",
        )
        sorted_images = sort_uploaded_images(uploaded_images, sort_mode)

        st.subheader("Sorted order")
        st.caption("The PDF page order will follow this list.")
        for index, file in enumerate(sorted_images, start=1):
            st.write(f"{index}. {file.name}")

        st.subheader("Preview")
        cols_per_row = 3
        for i in range(0, len(sorted_images), cols_per_row):
            cols = st.columns(cols_per_row)
            for j, col in enumerate(cols):
                if i + j < len(sorted_images):
                    with col:
                        file = sorted_images[i + j]
                        image = Image.open(file)
                        st.image(image, caption=f"{i + j + 1}. {file.name}", use_container_width=True)
                        file.seek(0)

        image_pdf_filename = st.text_input(
            "Output filename (without extension)",
            value="converted_images",
            key="image_filename",
        )

        if st.button("🔄 Convert images to PDF", type="primary", key="image_convert"):
            try:
                with st.spinner("Converting images to PDF..."):
                    pdf_bytes = convert_images_to_pdf(sorted_images)

                final_name = sanitize_filename(image_pdf_filename, "converted_images")
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
                st.info(f"📊 PDF size: {pdf_size_mb:.2f} MB | Pages: {len(sorted_images)}")
            except Exception as exc:
                st.error(f"❌ Error converting images to PDF: {exc}")
                st.exception(exc)
    else:
        st.info("👆 Upload image files to get started")

with markdown_tab:
    st.subheader("Markdown to PDF")
    st.write("Upload a Markdown file, preview it, and export it as a PDF using the markdown-pdf library.")

    uploaded_markdown = st.file_uploader(
        "Choose a Markdown file",
        type=["md", "markdown"],
        accept_multiple_files=False,
        help="Upload a .md or .markdown file.",
        key="markdown_uploader",
    )

    if uploaded_markdown is not None:
        markdown_text = read_uploaded_text(uploaded_markdown)
        default_name = sanitize_filename(uploaded_markdown.name.rsplit('.', 1)[0], "converted_markdown")

        st.success(f"✓ Uploaded: {uploaded_markdown.name}")
        markdown_pdf_filename = st.text_input(
            "Output filename (without extension)",
            value=default_name,
            key="markdown_filename",
        )

        st.subheader("Preview")
        st.markdown(markdown_text)

        with st.expander("Show raw Markdown"):
            st.code(markdown_text, language="markdown")

        if st.button("🔄 Convert Markdown to PDF", type="primary", key="markdown_convert"):
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
                st.exception(exc)
    else:
        st.info("👆 Upload a Markdown file to get started")

with st.expander("ℹ️ Features"):
    st.markdown(
        """
- Image mode supports JPG, JPEG, PNG, and WebP.
- Uploaded images are sorted by filename before conversion.
- Markdown mode uses the `markdown-pdf` library for PDF generation.
- PDF files are generated in memory and downloaded directly from the app.
        """
    )

st.markdown("---")
st.caption("Built with Streamlit, img2pdf, pillow, and markdown-pdf")
