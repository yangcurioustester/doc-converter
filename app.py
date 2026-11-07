"""Universal Doc Converter: a public web app that converts documents, PDFs and images.

Works from any device with a browser (Windows, Mac, Linux, iPhone, Android).
- Office + Apple iWork files (.pages/.numbers/.key): LibreOffice
- PDF -> Word: pdf2docx;  PDF -> images/text: PyMuPDF
- Images (incl. iPhone HEIC): Pillow
"""
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque

import fitz  # PyMuPDF
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from werkzeug.middleware.proxy_fix import ProxyFix

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 100_000_000

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "2"))
TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT_SECONDS", "120"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_10_MIN", "30"))
SOFFICE = os.environ.get("SOFFICE_BIN", "soffice")
MAX_IMAGE_PAGES = 100
RENDER_DPI = 150

# ---------------------------------------------------------------- format registry
KINDS = {
    "text": ["pages", "docx", "doc", "odt", "rtf", "txt", "html", "htm", "wpd", "md"],
    "sheet": ["numbers", "xlsx", "xls", "ods", "csv"],
    "slides": ["key", "pptx", "ppt", "odp"],
    "pdf": ["pdf"],
    "image": ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif", "heic", "heif", "ico"],
}
KIND_OF = {ext: k for k, exts in KINDS.items() for ext in exts}
KIND_LABEL = {"text": "Document", "sheet": "Spreadsheet", "slides": "Presentation", "pdf": "Document", "image": "Image"}
TARGETS = {
    "text": ["pdf", "docx", "doc", "odt", "rtf", "txt", "html", "epub", "png", "jpg"],
    "sheet": ["xlsx", "xls", "ods", "csv", "pdf", "html", "png", "jpg"],
    "slides": ["pptx", "ppt", "odp", "pdf", "png", "jpg"],
    "pdf": ["docx", "doc", "odt", "rtf", "txt", "html", "epub", "png", "jpg"],
    "image": ["png", "jpg", "webp", "gif", "bmp", "tiff", "ico", "pdf"],
}
CATEGORY = {
    **dict.fromkeys(["pdf", "docx", "doc", "odt", "rtf", "txt", "html"], "Document"),
    "epub": "Ebook",
    **dict.fromkeys(["png", "jpg", "webp", "gif", "bmp", "tiff", "ico"], "Image"),
    **dict.fromkeys(["pptx", "ppt", "odp"], "Presentation"),
    **dict.fromkeys(["xlsx", "xls", "ods", "csv"], "Spreadsheet"),
}
DESCRIPTIONS = {
    "pdf": "PDF document", "docx": "Microsoft Word", "doc": "Word 97-2003", "odt": "OpenDocument Text",
    "rtf": "Rich Text", "txt": "Plain text", "html": "Web page", "epub": "E-book",
    "png": "PNG image", "jpg": "JPEG image", "webp": "WebP image", "gif": "GIF image", "bmp": "Bitmap",
    "tiff": "TIFF image", "ico": "Icon", "pptx": "Microsoft PowerPoint", "ppt": "PowerPoint 97-2003",
    "odp": "OpenDocument Slides", "xlsx": "Microsoft Excel", "xls": "Excel 97-2003",
    "ods": "OpenDocument Sheet", "csv": "Comma-separated values",
}
LO_FILTERS = {
    "text": {
        "pdf": "pdf:writer_pdf_Export", "docx": "docx:MS Word 2007 XML", "doc": "doc:MS Word 97",
        "odt": "odt", "rtf": "rtf:Rich Text Format", "txt": "txt:Text (encoded):UTF8",
        "html": "html:XHTML Writer File:UTF8", "epub": "epub:EPUB",
    },
    "sheet": {
        "pdf": "pdf:calc_pdf_Export", "xlsx": "xlsx:Calc MS Excel 2007 XML", "xls": "xls:MS Excel 97",
        "ods": "ods", "csv": "csv:Text - txt - csv (StarCalc):44,34,76", "html": "html:XHTML Calc File:UTF8",
    },
    "slides": {
        "pdf": "pdf:impress_pdf_Export", "pptx": "pptx:Impress MS PowerPoint 2007 XML",
        "ppt": "ppt:MS PowerPoint 97", "odp": "odp",
    },
}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf", "txt": "text/plain; charset=utf-8", "csv": "text/csv; charset=utf-8",
    "html": "text/html; charset=utf-8", "epub": "application/epub+zip", "zip": "application/zip",
    "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "bmp": "image/bmp", "tiff": "image/tiff", "ico": "image/x-icon",
}
SAME = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "heif": "heic"}


def targets_for(ext):
    norm = SAME.get(ext, ext)
    return [t for t in TARGETS[KIND_OF[ext]] if t != norm]


# ---------------------------------------------------------------- app + limits
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # real client IP behind the host's proxy

jobs = threading.BoundedSemaphore(MAX_JOBS)
hits = defaultdict(deque)
hits_lock = threading.Lock()


class ConvertError(Exception):
    def __init__(self, msg, code=422):
        super().__init__(msg)
        self.code = code


def rate_limited(ip):
    now = time.time()
    with hits_lock:
        q = hits[ip]
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        if len(hits) > 10000:  # keep memory bounded
            for k in [k for k, v in hits.items() if not v]:
                del hits[k]
        return False


# ---------------------------------------------------------------- engines
def run(cmd):
    """Run a converter process with a hard timeout; kill its whole process group if it hangs."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        raise ConvertError("The converter is not installed on the server.", 500)
    try:
        _, err = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise ConvertError("The conversion took too long. Try a smaller file.", 504)
    return err.decode(errors="replace")


def libreoffice(work, data, ext, kind, target):
    src = os.path.join(work, f"lo-in.{ext}")
    outdir = os.path.join(work, f"lo-out-{target}")
    os.makedirs(outdir, exist_ok=True)
    with open(src, "wb") as f:
        f.write(data)
    err = run([SOFFICE, f"-env:UserInstallation=file://{work}/profile", "--headless", "--norestore",
               "--nologo", "--nodefault", "--nolockcheck", "--convert-to", LO_FILTERS[kind][target],
               "--outdir", outdir, src])
    produced = [n for n in os.listdir(outdir) if n.lower().endswith("." + target)]
    if not produced:
        app.logger.warning("libreoffice .%s -> .%s failed: %s", ext, target, err[-500:])
        raise ConvertError("This file couldn't be converted. It may be damaged, password protected, "
                           "or use features the converter can't read.")
    with open(os.path.join(outdir, produced[0]), "rb") as f:
        return f.read()


def open_pdf(data):
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise ConvertError("This PDF couldn't be opened. It may be damaged.")
    if doc.needs_pass:
        raise ConvertError("This PDF is password protected.")
    return doc


def pdf_to_docx(work, data):
    open_pdf(data).close()  # clear error for damaged / locked files
    src, dst = os.path.join(work, "pdf-in.pdf"), os.path.join(work, "pdf-out.docx")
    with open(src, "wb") as f:
        f.write(data)
    err = run([sys.executable, "-c", "import sys; from pdf2docx import parse; parse(sys.argv[1], sys.argv[2])",
               src, dst])
    if not os.path.exists(dst):
        app.logger.warning("pdf2docx failed: %s", err[-500:])
        raise ConvertError("This PDF couldn't be converted to Word.")
    with open(dst, "rb") as f:
        return f.read()


def pdf_to_text(data):
    with open_pdf(data) as doc:
        return "\n\n".join(page.get_text() for page in doc).encode("utf-8")


def pdf_to_images(data, target, stem):
    """Render each page. One page -> one image file; several pages -> a zip of images."""
    with open_pdf(data) as doc:
        if doc.page_count > MAX_IMAGE_PAGES:
            raise ConvertError(f"Too many pages for image export (limit {MAX_IMAGE_PAGES}).")
        images = []
        for page in doc:
            pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
            if target == "png":
                images.append(pix.tobytes("png"))
            else:
                buf = io.BytesIO()
                Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(buf, "JPEG", quality=90)
                images.append(buf.getvalue())
    if len(images) == 1:
        return images[0], target
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        width = len(str(len(images)))
        for i, img in enumerate(images, 1):
            z.writestr(f"{stem}-{str(i).zfill(width)}.{target}", img)
    return buf.getvalue(), "zip"


def flatten(img):
    """RGB on white, for formats without transparency."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img.convert("RGB")


def image_convert(data, target):
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError:
        raise ConvertError("This image is too large.")
    except Exception:
        raise ConvertError("This image couldn't be opened. It may be damaged.")
    buf = io.BytesIO()
    if target == "pdf":
        frames = []
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            frames.append(flatten(ImageOps.exif_transpose(img)))
        frames[0].save(buf, "PDF", save_all=True, append_images=frames[1:], resolution=150)
        return buf.getvalue()
    img = ImageOps.exif_transpose(img)  # keep phone photos the right way up
    if target == "jpg":
        flatten(img).save(buf, "JPEG", quality=92)
    elif target == "bmp":
        flatten(img).save(buf, "BMP")
    elif target == "ico":
        img = img.convert("RGBA")
        img.thumbnail((256, 256))
        img.save(buf, "ICO")
    elif target == "gif":
        img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buf, "GIF")
    elif target == "webp":
        img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buf, "WEBP", quality=90)
    elif target == "png":
        (img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else img.convert("RGB")).save(buf, "PNG")
    elif target == "tiff":
        img.save(buf, "TIFF", compression="tiff_deflate")
    return buf.getvalue()


def convert(data, ext, target, stem):
    """Returns (bytes, output extension). Output extension is 'zip' for multi-page image exports."""
    kind = KIND_OF[ext]
    if kind == "image":
        return image_convert(data, target), target
    work = tempfile.mkdtemp(prefix="conv-")
    try:
        if kind == "pdf":
            if target in ("png", "jpg"):
                return pdf_to_images(data, target, stem)
            if target == "txt":
                return pdf_to_text(data), target
            docx = pdf_to_docx(work, data)
            if target == "docx":
                return docx, target
            return libreoffice(work, docx, "docx", "text", target), target
        if target in ("png", "jpg"):
            return pdf_to_images(libreoffice(work, data, ext, kind, "pdf"), target, stem)
        return libreoffice(work, data, ext, kind, target), target
    finally:
        shutil.rmtree(work, ignore_errors=True)  # nothing is kept on the server


# ---------------------------------------------------------------- routes
@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


@app.get("/")
def index():
    return render_template("index.html", max_mb=MAX_MB)


@app.get("/healthz")
def health():
    return "ok"


@app.get("/api/formats")
def formats():
    return jsonify({
        "inputs": {ext: {"kind": KIND_LABEL[k], "targets": targets_for(ext)} for ext, k in KIND_OF.items()},
        "categories": ["Document", "Ebook", "Image", "Presentation", "Spreadsheet"],
        "categoryOf": CATEGORY,
        "descriptions": DESCRIPTIONS,
        "maxMB": MAX_MB,
    })


@app.post("/api/convert")
def api_convert():
    f = request.files.get("file")
    target = (request.form.get("to") or "").lower()
    if not f or not f.filename:
        return jsonify(error="No file was uploaded."), 400
    stem, _, ext = f.filename.rpartition(".")
    ext = ext.lower()
    if not stem or ext not in KIND_OF:
        return jsonify(error=f"Unsupported file type: .{ext}" if stem else "The file needs an extension."), 400
    if target not in targets_for(ext):
        return jsonify(error=f".{ext} files can't be converted to .{target}."), 400
    if rate_limited(request.remote_addr or "?"):
        return jsonify(error="Too many conversions. Please wait a few minutes."), 429
    data = f.read()
    if not data:
        return jsonify(error="The file is empty."), 400
    if not jobs.acquire(timeout=60):
        return jsonify(error="The converter is busy. Please try again in a minute."), 503
    safe_stem = os.path.basename(stem.replace("\\", "/")) or "converted"
    try:
        out, out_ext = convert(data, ext, target, safe_stem)
    except ConvertError as e:
        return jsonify(error=str(e)), e.code
    except Exception:
        app.logger.exception("unexpected failure .%s -> .%s", ext, target)
        return jsonify(error="Something went wrong converting this file."), 500
    finally:
        jobs.release()
    name = f"{safe_stem}-{target}.zip" if out_ext == "zip" else f"{safe_stem}.{out_ext}"
    return send_file(io.BytesIO(out), mimetype=MIME.get(out_ext, "application/octet-stream"),
                     as_attachment=True, download_name=name)


@app.errorhandler(413)
def too_big(_):
    return jsonify(error=f"File is too large. The limit is {MAX_MB} MB."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False)
