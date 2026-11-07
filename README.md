# Doc Converter

A free, public web converter. Anyone can open it in a browser on Windows, Mac, Linux, iPhone or Android,
drop a file, and the site detects its type and offers every format it can become.

| From | To |
|---|---|
| Documents: .pages .docx .doc .odt .rtf .txt .html .wpd .md | PDF, DOCX, DOC, ODT, RTF, TXT, HTML, EPUB, PNG, JPG |
| PDF | DOCX, DOC, ODT, RTF, TXT, HTML, EPUB, PNG, JPG |
| Spreadsheets: .numbers .xlsx .xls .ods .csv | XLSX, XLS, ODS, CSV, PDF, HTML, PNG, JPG |
| Presentations: .key .pptx .ppt .odp | PPTX, PPT, ODP, PDF, PNG, JPG |
| Images: .jpg .png .webp .gif .bmp .tiff .heic .ico | JPG, PNG, WEBP, GIF, BMP, TIFF, ICO, PDF |

Multi-page exports to PNG/JPG download as a zip with one image per page.

Engines: LibreOffice (Office + Apple iWork files), pdf2docx (PDF to Word), PyMuPDF (PDF to images/text),
Pillow + pillow-heif (images, including iPhone HEIC photos). Uploaded files are deleted as soon as each conversion finishes.

## Put it online (free)

The easiest way is Render's free plan:

1. Put this folder in a GitHub repository.
2. On https://render.com, sign in with GitHub and choose **New → Blueprint**.
3. Pick the repository. Render reads `render.yaml`, builds the Docker image and gives you a public link like `https://doc-converter.onrender.com`.

On the free plan the site goes to sleep after 15 minutes without visitors, so the first visit after that takes about a minute to load.
Any host that runs Docker works the same way (Railway, Fly.io, Google Cloud Run, a VPS).

## Run it on your own computer

You need Docker Desktop installed:

```bash
docker build -t doc-converter .
docker run --rm -p 8080:8080 doc-converter
```

Then open http://localhost:8080.

## Settings

Set these as environment variables on your host:

| Variable | Default | What it does |
|---|---|---|
| `MAX_UPLOAD_MB` | 50 | Largest file allowed |
| `MAX_PARALLEL_JOBS` | 2 | Conversions running at the same time (each uses ~200–400 MB of memory) |
| `CONVERT_TIMEOUT_SECONDS` | 120 | Stop conversions that take longer than this |
| `RATE_LIMIT_PER_10_MIN` | 30 | Conversions allowed per visitor every 10 minutes |

## Known limits

- Apple files are read by LibreOffice, not by Apple's apps. Simple documents look right; complex layouts, unusual fonts or very new Pages features may shift a little.
- Password-protected files can't be converted.
