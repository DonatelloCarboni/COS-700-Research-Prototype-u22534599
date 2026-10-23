import contextlib
import io
import json
import os
import re
import shutil
import subprocess
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

import injector
import renderer
import resolver
import scanner
sys.path.insert(0, HERE)
import extra

PLAIN = "A line chart of accuracy against training epoch, ending at 94 percent."
DRAWN = "A rectangle, drawn inline."
STORED_INLINE = "STORED text that must lose to the inline alt= in the source."


NO_ENTRIES = object()


def workspace(entries=NO_ENTRIES):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(HERE, "paper.tex"), os.path.join(work, "paper.tex"))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))

    def entry(alt, status="approved", profile="chart", source="example.png"):
        return {"alt": alt, "profile": profile, "source": source,
                "hash": "sha256:0", "status": status, "by": "u22534599",
                "at": "2026-08-31T00:00Z"}

    default = {
        "fig:plain": entry(PLAIN),
        "fig:inline": entry(STORED_INLINE),
        "fig:drawn": entry(DRAWN, profile="diagram", source="(drawn inline)"),
        "divider.png": entry(None, status="artifact", profile="artifact",
                             source="divider.png"),
    }
    store = {"schema": "alt/1", "document": "paper.tex",
             "entries": default if entries is NO_ENTRIES else entries}
    with open(os.path.join(work, "paper.alt"), "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2)
    return work


def run(work):
    state = {"document": os.path.join(work, "paper.tex")}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        injector.inject(state)
    return state


def alts(state):
    return (state.get("injected") or {}).get("in_pdf") or []


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_text_reaches_the_pdf():
    state = run(workspace())
    found = alts(state)
    problems = []
    if not (state.get("injected") or {}).get("pdf"):
        problems.append("no PDF was produced")
    if PLAIN not in found:
        problems.append(f"the stored text is not in the PDF; found {found}")
    return report("stored text reaches the PDF", problems,
                  note=f"{len(found)} /Alt entries in the PDF")


def check_inline_kept():
    state = run(workspace())
    found = alts(state)
    problems = []
    if "Written by hand in the source." not in found:
        problems.append("the author's inline alt= is missing from the PDF")
    if STORED_INLINE in found:
        problems.append("the .alt entry overrode the author's inline alt=")
    return report("inline alt= is kept", problems)


def check_artifact():
    state = run(workspace())
    found = alts(state)
    problems = []
    if any("divider" in t for t in found):
        problems.append(f"the decorative image was given alt text: {found}")
    shim = open((state["injected"])["shim"], encoding="utf-8").read()
    if "alt@artifact{divider.png}" not in shim:
        problems.append("the shim does not mark divider.png as an artifact")
    return report("decorative figure is an artifact", problems)


def check_tikz_gap():
    state = run(workspace())
    problems = []
    if DRAWN in alts(state):
        problems.append("a tikzpicture was injected, so the known gap is closed; "
                        "update this check")
    if not any("tikzpicture" in w for w in state.get("warnings", [])):
        problems.append(f"the gap was not reported; warnings: {state.get('warnings')}")
    return report("inline tikzpicture gap is reported", problems)


def check_specials_survive():
    nasty = r"Accuracy rose 50% & cost $3 per run; see file_name #2 {braces} ~ ^ 100\% sure"
    work = workspace({"fig:plain": {
        "alt": nasty, "profile": "chart", "source": "example.png",
        "hash": "sha256:0", "status": "approved", "by": "u22534599",
        "at": "2026-08-31T00:00Z"}})
    state = run(work)
    found = alts(state)
    problems = []
    if not (state["injected"]).get("pdf"):
        problems.append("the document did not compile with special characters in "
                        "the alt text")
    elif nasty not in found:
        near = [t for t in found if "Accuracy rose" in t]
        problems.append(f"the text did not survive escaping; got {near or found}")
    return report("TeX special characters reach the PDF", problems,
                  note=nasty)


def check_shim_regenerated():
    work = workspace()
    run(work)
    first = open(os.path.join(work, "_alt", "paper.alt.tex"), encoding="utf-8").read()
    run(work)
    second = open(os.path.join(work, "_alt", "paper.alt.tex"), encoding="utf-8").read()
    problems = []
    if first != second:
        problems.append("the shim changed between two identical runs")
    emitted = second.count("{example.png}")
    if emitted != 1:
        problems.append(f"example.png is emitted {emitted} times, expected 1")
    return report("shim is regenerated", problems)


def check_one_file_two_figures():
    shared = "The first use of the shared image."
    second = "The SECOND use, described differently."
    work = workspace({
        "fig:plain": {"alt": shared, "profile": "chart", "source": "example.png",
                      "hash": "sha256:0", "status": "approved", "by": "u22534599",
                      "at": "2026-09-01T00:00Z"},
        "fig:inline": {"alt": second, "profile": "chart", "source": "example.png",
                       "hash": "sha256:0", "status": "approved", "by": "u22534599",
                       "at": "2026-09-01T00:00Z"},
    })
    state = run(work)
    found = alts(state)
    problems = []
    if shared not in found:
        problems.append(f"the first use lost its text; got {found}")
    if "Written by hand in the source." not in found:
        problems.append("the author's inline alt= stopped winning once the file "
                        "was shared")
    if second in found:
        problems.append("the stored text overrode the author's inline alt=")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    if "alt@seq{example.png}{1}" not in shim:
        problems.append(f"the shared file was not emitted per occurrence")
    return report("two figures sharing one file",
                  problems)


def check_filename_fallback():
    work = workspace({})
    state = run(work)
    found = alts(state)
    problems = []
    if not any(injector.looks_like_filename(t) for t in found):
        problems.append(f"no filename fallback detected in {found}")
    if not any("falls back" in w for w in state.get("warnings", [])):
        problems.append("the fallback was not reported as useless alt text")
    return report("filename fallback is flagged", problems,
                  note=f"{[t for t in found if injector.looks_like_filename(t)]}")


def check_looks_like_filename():
    problems = []
    for text in ("example.png", "figures/chart.pdf", "a-b_c.jpeg"):
        if not injector.looks_like_filename(text):
            problems.append(f"{text!r} should be seen as a filename")
    for text in ("A line chart of accuracy.", "Accuracy vs epoch.",
                 "The figure is too unclear to describe."):
        if injector.looks_like_filename(text):
            problems.append(f"{text!r} should not be seen as a filename")
    return report("filenames and descriptions are told apart", problems)


def main():
    if not shutil.which("pdflatex"):
        print("skipped: pdflatex is not on PATH, and these checks read real PDFs")
        return 0
    results = [
        check_text_reaches_the_pdf(),
        check_inline_kept(),
        check_artifact(),
        check_tikz_gap(),
        check_specials_survive(),
        check_shim_regenerated(),
        check_one_file_two_figures(),
        check_filename_fallback(),
        check_looks_like_filename(),
    ]
    results += extra.run_all()
    results += [
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
