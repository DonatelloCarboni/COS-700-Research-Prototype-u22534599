import os
import re
import shutil
import subprocess
import tempfile

RASTER = (".png", ".jpg", ".jpeg")
VECTOR = (".pdf", ".eps", ".ps")
POSTSCRIPT = (".eps", ".ps")

DPI = "200"
COMPILE_TIMEOUT_S = 60

WRAPPER = r"""\documentclass[border=2pt]{standalone}
%(preamble)s
\begin{document}
\begin{tikzpicture}%(body)s
\end{tikzpicture}
\end{document}
"""


def render(state):
    requests = state.get("requests", [])
    document = state["document"]
    warnings = []

    if not requests:
        state["renders"] = []
        return "nothing to render"

    absent = absent_tools()

    renders_dir = alt_dir(document, "renders")
    fragments_dir = alt_dir(document, "fragments")
    preamble = read_preamble(document)

    renders = []
    counts = {}
    for request in requests:
        result = one(request, document, renders_dir, fragments_dir, preamble,
                     warnings, absent)
        counts[result["how"]] = counts.get(result["how"], 0) + 1
        renders.append(result)

    state["renders"] = renders
    state["warnings"] = state.get("warnings", []) + warnings
    if state.get("verbose"):
        for w in warnings:
            print(f"  warning: {w}")

    order = ["compiled", "rasterised", "copied", "cached", "failed"]
    parts = [f"{counts[k]} {k}" for k in order if k in counts]
    line = f"{len(renders)} figure(s): " + ", ".join(parts)
    if absent:
        line += f" ({' and '.join(sorted(absent))} not on PATH)"
    if warnings and not state.get("verbose"):
        line += f", {len(warnings)} warning(s)"
    return line


def one(request, document, renders_dir, fragments_dir, preamble, warnings,
        absent=()):
    key = request["key"]
    target = os.path.join(renders_dir, render_name(request))

    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return done(request, target, "cached")

    lacking = [t for t in tools_for(request) if t in absent]
    if lacking:
        error = (f"{' and '.join(lacking)} not on PATH, and this figure cannot be "
                 f"rendered without it")
        warnings.append(f"could not render {key}: {error}")
        return done(request, None, "failed", error)

    if request["kind"] == "tikz":
        image, error = compile_tikz(request, document, fragments_dir, target, preamble)
        how = "compiled"
    else:
        image, error = convert(request, target)
        how = "copied" if is_raster(request.get("file")) else "rasterised"

    if image is None:
        warnings.append(f"could not render {key}: {error}")
        return done(request, None, "failed", error)
    return done(request, image, how)


def done(request, image, how, error=None):
    return {
        "key": request["key"],
        "image": image,
        "how": how,
        "error": error,
        "profile": request["profile"],
        "caption": request["caption"],
        "where": request["where"],
        "hash": request["hash"],
    }


def convert(request, target):
    source = request.get("file")
    if not source or not os.path.isfile(source):
        return None, f"its file is not on disk ({request.get('source')!r})"

    if is_raster(source):
        part = target + ".part"
        try:
            shutil.copyfile(source, part)
            os.replace(part, target)
        except OSError as exc:
            remove(part)
            return None, f"could not copy the image ({exc})"
        return target, None

    if not is_vector(source):
        return None, (f"{os.path.splitext(source)[1] or 'that file type'} is not "
                      f"a format this stage can rasterise")

    if is_postscript(source):
        return rasterise_eps(source, target)
    return rasterise(source, target)


def rasterise_eps(source, target):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "figure.pdf")
        try:
            done_ = subprocess.run(["epstopdf", f"--outfile={pdf}", source],
                                   capture_output=True, text=True,
                                   timeout=COMPILE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, f"epstopdf did not finish within {COMPILE_TIMEOUT_S}s"
        except OSError as exc:
            return None, (f"pdftoppm cannot read PostScript, and epstopdf is not "
                          f"available to convert it first ({exc})")
        if not os.path.isfile(pdf):
            detail = (done_.stderr or done_.stdout or "no output").strip().splitlines()
            return None, f"epstopdf produced no PDF ({detail[0] if detail else '?'})"
        return rasterise(pdf, target)


def compile_tikz(request, document, fragments_dir, target, preamble):
    if not request.get("body"):
        return None, "stage 1 recorded no picture source for it"

    fragment = os.path.join(fragments_dir, safe_name(request["key"]) + ".tex")

    for junk in (".pdf", ".aux", ".log"):
        remove(os.path.splitext(fragment)[0] + junk)

    with open(fragment, "w", encoding="utf-8") as fh:
        fh.write(WRAPPER % {"key": request["key"], "preamble": preamble,
                            "body": request["body"].rstrip()})

    ok, detail = run_pdflatex(fragment, os.path.dirname(os.path.abspath(document)))
    if not ok:
        return None, f"the standalone wrapper did not compile ({detail}). " \
                     f"The wrapper is kept at {rel(fragment)}"

    pdf = os.path.splitext(fragment)[0] + ".pdf"
    if not os.path.isfile(pdf):
        return None, f"compiling produced no PDF (wrapper kept at {rel(fragment)})"
    return rasterise(pdf, target)


def rasterise(source, target):
    stem = os.path.splitext(target)[0] + ".part"
    part = stem + ".png"
    try:
        done_ = subprocess.run(
            ["pdftoppm", "-png", "-r", DPI, "-singlefile", source, stem],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        remove(part)
        return None, f"pdftoppm did not finish within {COMPILE_TIMEOUT_S}s"
    except OSError as exc:
        remove(part)
        return None, f"could not run pdftoppm ({exc})"

    if not os.path.isfile(part) or os.path.getsize(part) == 0:
        remove(part)
        detail = (done_.stderr or done_.stdout or "no output").strip().splitlines()
        return None, f"pdftoppm produced no image ({detail[0] if detail else '?'})"
    os.replace(part, target)
    return target, None


def run_pdflatex(fragment, document_dir):
    env = dict(os.environ)
    env["TEXINPUTS"] = document_dir + os.pathsep + env.get("TEXINPUTS", "")
    try:
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", os.path.basename(fragment)],
            cwd=os.path.dirname(fragment), stdin=subprocess.DEVNULL, env=env,
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"pdflatex did not finish within {COMPILE_TIMEOUT_S}s"
    except OSError as exc:
        return False, f"could not run pdflatex ({exc})"

    log = os.path.splitext(fragment)[0] + ".log"
    if os.path.isfile(log):
        text = open(log, encoding="utf-8", errors="replace").read()
        errors = [ln.strip() for ln in text.splitlines() if ln.startswith("!")]
        if errors:
            return False, errors[0]
    return True, None


def read_preamble(document):
    try:
        with open(document, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    start = re.search(r"\\documentclass(?![a-zA-Z])\s*(?:\[.*?\])?\s*\{.*?\}",
                      text, re.DOTALL)
    end = re.search(r"\\begin\s*\{document\}", text)
    if not start or not end or end.start() < start.end():
        return ""
    return text[start.end():end.start()].strip()


def tools_for(request):
    if request.get("kind") == "tikz":
        return ["pdflatex", "pdftoppm"]
    source = request.get("file")
    if is_postscript(source):
        return ["epstopdf", "pdftoppm"]
    if is_vector(source):
        return ["pdftoppm"]
    return []


def absent_tools():
    return {t for t in ("pdflatex", "pdftoppm", "epstopdf") if shutil.which(t) is None}


def which_missing(requests):
    needed = set()
    for r in requests:
        needed.update(tools_for(r))
    absent = sorted(t for t in needed if shutil.which(t) is None)
    if not absent:
        return None
    return f"{' and '.join(absent)} not on PATH"


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def alt_dir(document, name):
    folder = os.path.join(os.path.dirname(document) or ".", "_alt", name)
    os.makedirs(folder, exist_ok=True)
    return folder


def render_name(request):
    digest = (request.get("hash") or "nohash").split(":")[-1][:12]
    return f"{safe_name(request['key'])}-{digest}.png"


def safe_name(key):
    return re.sub(r"[^A-Za-z0-9._-]", "-", key)


def is_raster(path):
    return bool(path) and os.path.splitext(path)[1].lower() in RASTER


def is_vector(path):
    return bool(path) and os.path.splitext(path)[1].lower() in VECTOR


def is_postscript(path):
    return bool(path) and os.path.splitext(path)[1].lower() in POSTSCRIPT


def rel(path):
    try:
        return os.path.relpath(path)
    except ValueError:
        return path
