"""Universal Doc Converter: a public web app that converts documents with LibreOffice.

Works from any device with a browser (Windows, Mac, Linux, iPhone, Android).
Apple formats (.pages, .numbers, .key) are read by LibreOffice's built-in iWork importer.
"""
import io
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "2"))
TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT_SECONDS", "120"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_10_MIN", "30"))
SOFFICE = os.environ.get("SOFFICE_BIN", "soffice")

# input extension -> family
FAMILIES = {
    "document": ["pages", "docx", "doc", "odt", "rtf", "txt", "wpd", "html"],
    "spreadsheet": ["numbers", "xlsx", "xls", "ods", "csv"],
    "presentation": ["key", "pptx", "ppt", "odp"],
}
INPUT_FAMILY = {ext: fam for fam, exts in FAMILIES.items() for ext in exts}

# family -> output extension -> LibreOffice --convert-to filter
OUTPUTS = {
    "document": {
        "pdf": "pdf:writer_pdf_Export",
        "docx": "docx:MS Word 2007 XML",
        "odt": "odt",
        "rtf": "rtf",
        "txt": "txt:Text (encoded):UTF8",
    },
    "spreadsheet": {
        "pdf": "pdf:calc_pdf_Export",
        "xlsx": "xlsx:Calc MS Excel 2007 XML",
        "ods": "ods",
        "csv": "csv:Text - txt - csv (StarCalc):44,34,76",
    },
    "presentation": {
        "pdf": "pdf:impress_pdf_Export",
        "pptx": "pptx:Impress MS PowerPoint 2007 XML",
        "odp": "odp",
    },
}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # real client IP behind the host's proxy

jobs = threading.BoundedSemaphore(MAX_JOBS)
hits = defaultdict(deque)
hits_lock = threading.Lock()


class ConvertError(Exception):
    def __init__(self, msg, code=400):
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


def run_libreoffice(src, outdir, profile, filt):
    cmd = [SOFFICE, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
           "--nologo", "--nodefault", "--nolockcheck", "--convert-to", filt, "--outdir", outdir, src]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        raise ConvertError("LibreOffice is not installed on the server.", 500)
    try:
        _, err = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise ConvertError("The conversion took too long. Try a smaller file.", 504)
    return err.decode(errors="replace")


def convert(data, ext, target):
    work = tempfile.mkdtemp(prefix="conv-")
    try:
        src = os.path.join(work, f"input.{ext}")
        outdir = os.path.join(work, "out")
        os.mkdir(outdir)
        with open(src, "wb") as f:
            f.write(data)
        err = run_libreoffice(src, outdir, os.path.join(work, "profile"), OUTPUTS[INPUT_FAMILY[ext]][target])
        produced = [n for n in os.listdir(outdir) if n.lower().endswith("." + target)]
        if not produced:
            app.logger.warning("conversion failed for .%s -> .%s: %s", ext, target, err[-500:])
            raise ConvertError("This file couldn't be converted. It may be damaged, password protected, "
                               "or use features the converter can't read.", 422)
        with open(os.path.join(outdir, produced[0]), "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)  # nothing is kept on the server


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
        "inputs": INPUT_FAMILY,
        "outputs": {fam: list(o) for fam, o in OUTPUTS.items()},
        "maxMB": MAX_MB,
    })


@app.post("/api/convert")
def api_convert():
    f = request.files.get("file")
    target = (request.form.get("to") or "pdf").lower()
    if not f or not f.filename:
        return jsonify(error="No file was uploaded."), 400
    stem, _, ext = f.filename.rpartition(".")
    ext = ext.lower()
    if not stem or ext not in INPUT_FAMILY:
        return jsonify(error=f"Unsupported file type: .{ext}" if stem else "The file needs an extension."), 400
    if target not in OUTPUTS[INPUT_FAMILY[ext]]:
        return jsonify(error=f".{ext} files can't be converted to .{target}."), 400
    if rate_limited(request.remote_addr or "?"):
        return jsonify(error="Too many conversions. Please wait a few minutes."), 429
    data = f.read()
    if not data:
        return jsonify(error="The file is empty."), 400
    if not jobs.acquire(timeout=60):
        return jsonify(error="The converter is busy. Please try again in a minute."), 503
    try:
        out = convert(data, ext, target)
    except ConvertError as e:
        return jsonify(error=str(e)), e.code
    finally:
        jobs.release()
    safe_stem = os.path.basename(stem.replace("\\", "/")) or "converted"
    return send_file(io.BytesIO(out), mimetype=MIME.get(target, "application/octet-stream"),
                     as_attachment=True, download_name=f"{safe_stem}.{target}")


@app.errorhandler(413)
def too_big(_):
    return jsonify(error=f"File is too large. The limit is {MAX_MB} MB."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False)
