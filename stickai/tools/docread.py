"""Read a document's text exactly (not from a screenshot) and lay it out as Markdown.

* PDF: pypdf (local files, or a PDF the browser shows from the web, downloaded to memory);
* .docx: the text of its paragraphs (from the XML inside the zip);
* text files as they are.
The layout is done locally: wrapped prose lines are joined, task headings ("Úloha 6.1:",
"Task 2.3", "Задание 1.") become headings, sample input / output stays line by line in code blocks.
"""
from __future__ import annotations

import io
import os
import re
import urllib.parse
import urllib.request
import zipfile

MAX_CHARS = 200_000
MAX_DOWNLOAD = 15_000_000
TEXT_EXT = {".txt", ".md", ".py", ".c", ".h", ".cpp", ".java", ".js", ".ts", ".json", ".csv", ".log", ".ini", ".toml"}

HEADING = re.compile(r"^\s*((?:Úloha|Uloha|Zadanie|Task|Exercise|Problem|Задание|Задача|Упражнение|Úkol|Aufgabe)"
                     r"\s*\d+(?:\.\d+)*\s*[:.)]?|\d+\.\d+(?:\.\d+)*\s*[:.)])\s*(.*)$", re.IGNORECASE)
SAMPLE_LABEL = re.compile(r"^\s*(Ukážka|Ukazka|Výstup|Vystup|Vstup|Príklad|Example|Sample|Input|Output|Пример|Ввод|Вывод|"
                          r"Входные|Выходные)\b.*:\s*$", re.IGNORECASE)


class ReadError(Exception):
    pass


def path_from_address(address: str) -> str | None:
    """'file:///C:/Users/x/a.pdf' or 'C:/Users/x/a.pdf' (what the address bar shows) -> a local path."""
    a = address.strip()
    if a.lower().startswith("file:"):
        a = urllib.parse.unquote(a[5:].lstrip("/"))
    a = a.replace("/", os.sep)
    return a if re.match(r"^[A-Za-z]:\\", a) and os.path.isfile(a) else None


def _pdf_text(data_or_path) -> str:
    try:
        import pypdf
    except ImportError as e:  # requirements.txt has it; an old .venv might not
        raise ReadError("pypdf is not installed (.venv\\Scripts\\pip install pypdf)") from e
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    try:
        reader = pypdf.PdfReader(src)
        pages = [p.extract_text() or "" for p in reader.pages[:200]]
    except Exception as e:  # noqa: BLE001 - damaged / encrypted PDF
        raise ReadError(f"couldn't read the PDF: {e}") from e
    return "\n\n".join(pages)


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    out = []
    for p in paras:
        t = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
        out.append(re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", t))))
    return "\n".join(out)


def read_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text = _pdf_text(path)
    elif ext == ".docx":
        text = _docx_text(path)
    elif ext in TEXT_EXT:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_CHARS)
    else:
        raise ReadError(f"can't read {ext or 'this'} files as text")
    return text[:MAX_CHARS]


def read_url_pdf(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ReadError("the PDF is too big")
    return _pdf_text(data)[:MAX_CHARS]


def to_markdown(text: str, title: str = "") -> str:
    """Local layout: headings for tasks, joined prose, samples kept line by line."""
    lines = [ln.rstrip() for ln in text.replace("\r", "").replace("\u00a0", " ").split("\n")]
    out: list[str] = [f"# {title}", ""] if title else []
    para: list[str] = []
    code: list[str] | None = None

    def flush_para():
        if para:
            joined = " ".join(para)
            joined = re.sub(r"(\w)- (\w)", r"\1\2", joined)  # hy- phenated
            out.extend([" ".join(joined.split()), ""])
            para.clear()

    def flush_code():
        nonlocal code
        if code is not None:
            while code and not code[-1].strip():
                code.pop()
            if code:
                out.extend(["```", *code, "```", ""])
            code = None

    for ln in lines:
        s = ln.strip()
        m = HEADING.match(ln)
        if m and len(s) < 400:
            flush_para()
            flush_code()
            out.extend([f"### {m.group(1).rstrip(':.) ')}", ""])
            if m.group(2).strip():
                para.append(m.group(2).strip())
            continue
        if SAMPLE_LABEL.match(ln):
            flush_para()
            flush_code()
            out.extend([f"**{s}**", ""])
            code = []
            continue
        if code is not None:
            if not s and code and not code[-1].strip():  # two blank lines: the sample is over
                flush_code()
                continue
            if s and len(s) > 70 and s[-1:] in ".:;!?" and " " in s and not re.search(r"\d\s+\d\s+\d", s):
                flush_code()  # a sentence: back to prose
                para.append(s)
                continue
            code.append(ln)
            continue
        if not s:
            flush_para()
            continue
        para.append(s)
        if len(s) < 45 and s[-1:] in ".:!?" and len(para) == 1:
            flush_para()  # a short standalone line
    flush_para()
    flush_code()
    return "\n".join(out).strip() + "\n"
