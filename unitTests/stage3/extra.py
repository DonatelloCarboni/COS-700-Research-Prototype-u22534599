import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def project_dir():
    folder = HERE
    while not os.path.isfile(os.path.join(folder, "pipeline.py")):
        parent = os.path.dirname(folder)
        if parent == folder:
            raise SystemExit(f"cannot find pipeline.py above {HERE}")
        folder = parent
    return folder


sys.path.insert(0, project_dir())

import renderer
import resolver
import scanner

PROJECT = project_dir()


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def workspace():
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    for f in os.listdir(HERE):
        if f.endswith((".tex", ".alt")):
            shutil.copyfile(os.path.join(HERE, f), os.path.join(work, f))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    return work


def run(name, work=None, absent=None):
    work = work or workspace()
    state = {"document": os.path.join(work, name)}
    real = renderer.absent_tools
    if absent is not None:
        renderer.absent_tools = lambda: set(absent)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
    finally:
        renderer.absent_tools = real
    return state, work


def check_raster_no_tools():
    state, work = run("raster.tex", absent={"pdftoppm", "pdflatex", "epstopdf"})
    problems = []
    if not state["renders"]:
        problems.append("nothing was rendered; the copy was skipped because of "
                        "a missing tool it does not need")
    else:
        got = state["renders"][0]
        if got["how"] != "copied":
            problems.append(f"how={got['how']!r} ({got.get('error')})")
        elif not os.path.isfile(got["image"] or ""):
            problems.append("no image on disk")
    shutil.rmtree(work, ignore_errors=True)
    return report("raster copied without converters",
                  problems)


def check_vector_missing_tool():
    state, work = run("vector.tex", absent={"pdftoppm"})
    problems = []
    if not state["renders"]:
        problems.append("no result was recorded at all")
    else:
        got = state["renders"][0]
        if got["how"] != "failed":
            problems.append(f"how={got['how']!r}, expected 'failed'")
        if "pdftoppm" not in (got.get("error") or ""):
            problems.append(f"the error does not name the tool: {got.get('error')!r}")
    if not any("pdftoppm" in w for w in state.get("warnings", [])):
        problems.append("no warning named the missing tool")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing converter is named", problems)


def check_tools_per_figure():
    cases = [
        ({"kind": "includegraphics", "file": "a.png"}, []),
        ({"kind": "includegraphics", "file": "a.jpg"}, []),
        ({"kind": "includegraphics", "file": "a.pdf"}, ["pdftoppm"]),
        ({"kind": "includegraphics", "file": "a.eps"}, ["epstopdf", "pdftoppm"]),
        ({"kind": "tikz", "file": None}, ["pdflatex", "pdftoppm"]),
    ]
    problems = []
    for request, want in cases:
        got = renderer.tools_for(request)
        if got != want:
            problems.append(f"{request.get('file') or request['kind']}: "
                            f"{got}, expected {want}")
    return report("tools needed per figure",
                  problems)


def check_eps_uses_epstopdf():
    problems = []
    if "epstopdf" not in renderer.tools_for({"kind": "includegraphics",
                                             "file": "figure.eps"}):
        problems.append("an .eps figure does not ask for epstopdf")
    if not renderer.is_postscript("figure.eps"):
        problems.append(".eps is not recognised as PostScript")
    if renderer.is_postscript("figure.pdf"):
        problems.append(".pdf was called PostScript")

    work = workspace()
    open(os.path.join(work, "figures", "drawing.eps"), "w").write("%!PS-Adobe-3.0\n")
    with open(os.path.join(work, "eps.tex"), "w", encoding="utf-8") as fh:
        fh.write(open(os.path.join(work, "vector.tex"), encoding="utf-8")
                 .read().replace("vector.pdf", "drawing.eps")
                 .replace("fig:vector", "fig:eps"))
    state, _ = run("eps.tex", work=work, absent={"epstopdf"})
    if state["renders"]:
        error = state["renders"][0].get("error") or ""
        if "epstopdf" not in error:
            problems.append(f"the failure does not name epstopdf: {error!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("EPS goes through epstopdf",
                  problems)


def check_empty_render():
    state, work = run("raster.tex")
    image = state["renders"][0]["image"]
    open(image, "wb").close()

    state, _ = run("raster.tex", work=work)
    got = state["renders"][0]
    problems = []
    if got["how"] == "cached":
        problems.append("an empty file was served from the cache")
    elif got["how"] != "copied":
        problems.append(f"how={got['how']!r} ({got.get('error')})")
    elif os.path.getsize(got["image"]) == 0:
        problems.append("the render is still empty after being redone")
    shutil.rmtree(work, ignore_errors=True)
    return report("empty render is redone",
                  problems)


def check_no_part_files():
    state, work = run("raster.tex")
    renders = os.path.join(work, "_alt", "renders")
    leftover = [f for f in os.listdir(renders) if ".part" in f]
    shutil.rmtree(work, ignore_errors=True)
    return report("no .part files left",
                  [f"found {leftover}"] if leftover else [])


def check_failed_recompile():
    state, work = run("tikz.tex")
    fragment = os.path.join(work, "_alt", "fragments", "fig-tikz.tex")
    pdf = os.path.splitext(fragment)[0] + ".pdf"
    problems = []
    if not os.path.isfile(pdf):
        problems.append("the first compile produced no fragment PDF to test with")
        shutil.rmtree(work, ignore_errors=True)
        return report("failed recompile does not reuse the old PDF", problems)

    marker = b"%STALE-FROM-THE-PREVIOUS-RUN"
    with open(pdf, "ab") as fh:
        fh.write(b"\n" + marker + b"\n")

    text = open(os.path.join(work, "tikz.tex"), encoding="utf-8").read()
    broken = text.replace(r"\draw", r"\drawnosuchcommand", 1)
    open(os.path.join(work, "tikz.tex"), "w", encoding="utf-8").write(broken)
    os.remove(os.path.join(work, "tikz.alt")) if os.path.isfile(
        os.path.join(work, "tikz.alt")) else None

    state, _ = run("tikz.tex", work=work)
    got = state["renders"][0] if state["renders"] else None
    if got is None:
        problems.append("nothing was rendered at all")
    elif got["how"] != "failed":
        problems.append(f"a picture that cannot compile came back as "
                        f"{got['how']!r}; the old PDF was reused")
    if os.path.isfile(pdf) and marker in open(pdf, "rb").read():
        problems.append("the previous run's PDF is still there to be picked up")
    shutil.rmtree(work, ignore_errors=True)
    return report("failed recompile does not reuse the old PDF",
                  problems)


def check_render_name():
    a = renderer.render_name({"key": "fig:x", "hash": "sha256:aaaaaaaaaaaaaaaa"})
    b = renderer.render_name({"key": "fig:x", "hash": "sha256:bbbbbbbbbbbbbbbb"})
    problems = []
    if a == b:
        problems.append("two different hashes give the same render filename")
    if not a.endswith(".png"):
        problems.append(f"the render is not named as a png: {a!r}")
    return report("render name includes the hash", problems,
                  note=f"{a} vs {b}")


def check_safe_name():
    got = renderer.safe_name("fig:chapter/one.png")
    problems = []
    for bad in (":", "/", "\\"):
        if bad in got:
            problems.append(f"{bad!r} survived into a filename: {got!r}")
    return report("keys are made safe for filenames", problems, note=got)


def check_wrapper_preamble():
    work = workspace()
    preamble = renderer.read_preamble(os.path.join(work, "tikz.tex"))
    problems = []
    if r"\documentclass" in preamble:
        problems.append("the document's own class was carried into the wrapper")
    if "usetikzlibrary" not in preamble:
        problems.append("the preamble the picture depends on was not carried")
    shutil.rmtree(work, ignore_errors=True)
    return report("wrapper uses the preamble without documentclass",
                  problems)


def check_non_utf8_preamble():
    work = tempfile.mkdtemp()
    path = os.path.join(work, "latin.tex")
    with open(path, "wb") as fh:
        fh.write("\\documentclass{article}\n% caf\xe9\n\\begin{document}\n"
                 "\\end{document}\n".encode("latin-1"))
    problems = []
    try:
        renderer.read_preamble(path)
    except Exception as exc:
        problems.append(f"raised {type(exc).__name__}: {exc}")
    shutil.rmtree(work, ignore_errors=True)
    return report("non-UTF-8 preamble is handled", problems)


def check_nothing_queued():
    state = {"document": "nowhere.tex", "requests": []}
    summary = renderer.render(state)
    problems = []
    if state["renders"]:
        problems.append("something was rendered with nothing queued")
    if summary != "nothing to render":
        problems.append(f"summary was {summary!r}")
    return report("nothing queued renders nothing", problems)


CHECKS = [
    check_raster_no_tools,
    check_vector_missing_tool,
    check_tools_per_figure,
    check_eps_uses_epstopdf,
    check_empty_render,
    check_no_part_files,
    check_failed_recompile,
    check_render_name,
    check_safe_name,
    check_wrapper_preamble,
    check_non_utf8_preamble,
    check_nothing_queued,
]


def run_all():
    return [check() for check in CHECKS]
