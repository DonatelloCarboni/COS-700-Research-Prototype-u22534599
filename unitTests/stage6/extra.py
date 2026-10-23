import contextlib
import io
import json
import os
import re
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

import injector
import renderer
import resolver
import scanner

PROJECT = project_dir()

HEAD = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\pdfcompresslevel=0
\pdfobjcompresslevel=0
\graphicspath{{figures/}}
\InputIfFileExists{_alt/%s.alt.tex}{}{}
\begin{document}
"""


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def project(name, body, entries, images=("example.png",), extra_files=None):
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "figures"))
    for image in images:
        shutil.copyfile(os.path.join(HERE, "figures", "example.png"),
                        os.path.join(work, "figures", image))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    stem = os.path.splitext(name)[0]
    with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
        fh.write(HEAD % stem + body + "\\end{document}\n")
    for relative, text in (extra_files or {}).items():
        full = os.path.join(work, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w", encoding="utf-8").write(text)
    with open(os.path.join(work, stem + ".alt"), "w", encoding="utf-8") as fh:
        json.dump({"schema": "alt/1", "document": name, "entries": entries},
                  fh, indent=2)
    return work


def entry(alt, source="example.png", status="approved", profile="chart"):
    return {"alt": alt, "profile": profile, "source": source, "hash": "sha256:0",
            "status": status, "by": "u22534599", "at": "2026-09-01T00:00Z"}


def run(work, name):
    state = {"document": os.path.join(work, name)}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        summary = injector.inject(state)
    return state, summary


def alts(state):
    return (state.get("injected") or {}).get("in_pdf") or []


def log_errors(work, stem):
    path = os.path.join(work, "build", stem + ".log")
    if not os.path.isfile(path):
        return ["no log was written"]
    text = open(path, encoding="utf-8", errors="replace").read()
    return [ln.strip() for ln in text.splitlines() if ln.startswith("!")]


def check_starred_includegraphics():
    text = "A chart, included with the starred form."
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics*[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Starred.}\label{fig:star}" "\n"
            r"\end{figure}" "\n")
    work = project("star.tex", body, {"fig:star": entry(text)})
    state, summary = run(work, "star.tex")
    errors = log_errors(work, "star")
    problems = []
    if errors:
        problems.append(f"the build reported {len(errors)} error(s): {errors[0]}")
    if text not in alts(state):
        problems.append(f"the alt text did not reach the PDF; found {alts(state)}")
    shutil.rmtree(work, ignore_errors=True)
    return report("\\includegraphics* builds and is tagged",
                  problems, note=summary)


def check_star_means_clip():
    problems = []
    if "clip," not in injector.SHIM_PATCH:
        problems.append("the wrapper does not pass the star on as clip")
    if "@ifstar" not in injector.SHIM_PATCH:
        problems.append("the wrapper does not read the star at all")
    return report("star is passed on as clip", problems)


def check_two_optional_form():
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[0,0][100,80]{example.png}" "\n"
            r"  \caption{Legacy form.}\label{fig:legacy}" "\n"
            r"\end{figure}" "\n")
    work = project("legacy.tex", body, {"fig:legacy": entry("Some text.")})
    state, summary = run(work, "legacy.tex")
    errors = log_errors(work, "legacy")
    problems = []
    if errors:
        problems.append(f"the build reported {len(errors)} error(s): {errors[0]}")
    if not (state.get("injected") or {}).get("pdf"):
        problems.append("no PDF was produced")
    shutil.rmtree(work, ignore_errors=True)
    return report("two-optional-argument form builds", problems,
                  note=summary)


def check_plain_includegraphics():
    text = "The ordinary case, described as usual."
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Ordinary.}\label{fig:plain}" "\n"
            r"\end{figure}" "\n")
    work = project("plain.tex", body, {"fig:plain": entry(text)})
    state, summary = run(work, "plain.tex")
    problems = []
    if text not in alts(state):
        problems.append(f"found {alts(state)}")
    if log_errors(work, "plain"):
        problems.append(f"errors: {log_errors(work, 'plain')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("plain includegraphics is tagged", problems)


def check_no_options():
    text = "No options at all."
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics{example.png}" "\n"
            r"  \caption{Bare.}\label{fig:bare}" "\n"
            r"\end{figure}" "\n")
    work = project("bare.tex", body, {"fig:bare": entry(text)})
    state, _ = run(work, "bare.tex")
    problems = [] if text in alts(state) else [f"found {alts(state)}"]
    if log_errors(work, "bare"):
        problems.append(f"errors: {log_errors(work, 'bare')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("includegraphics with no options is tagged", problems)


def check_shared_artifact():
    text = "A meaningful figure that happens to reuse the same image file."
    body = ("%!alt artifact\n"
            r"\noindent\includegraphics[width=\linewidth]{shared.png}" "\n\n"
            r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{shared.png}" "\n"
            r"  \caption{Meaningful.}\label{fig:meaning}" "\n"
            r"\end{figure}" "\n")
    work = project("shared.tex", body,
                   {"shared.png": entry(None, source="shared.png",
                                        status="artifact", profile="artifact"),
                    "fig:meaning": entry(text, source="shared.png")},
                   images=("shared.png",))
    state, summary = run(work, "shared.tex")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    problems = []
    if text not in alts(state):
        problems.append(f"the meaningful figure lost its alt text; "
                        f"found {alts(state)}")
    if "alt@artseq{shared.png}{1}" not in shim:
        problems.append("the decorative use was not pinned to its occurrence")
    if "alt@artifact{shared.png}" in shim:
        problems.append("the decorative mark was applied to the file name, so "
                        "every use of it is excluded")
    shutil.rmtree(work, ignore_errors=True)
    return report("shared image decorative in one place only",
                  problems, note=summary)


def check_single_artifact():
    body = ("%!alt artifact\n"
            r"\noindent\includegraphics[width=\linewidth]{divider.png}" "\n")
    work = project("art.tex", body,
                   {"divider.png": entry(None, source="divider.png",
                                         status="artifact", profile="artifact")},
                   images=("divider.png",))
    state, _ = run(work, "art.tex")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    problems = []
    if "alt@artifact{divider.png}" not in shim:
        problems.append("a lone decorative image was not marked")
    if any("divider" in t for t in alts(state)):
        problems.append(f"it was given alt text anyway: {alts(state)}")
    shutil.rmtree(work, ignore_errors=True)
    return report("single decorative image marked by name", problems)


def check_whitespace_collapsed():
    stored = "Accuracy over epochs 1--50 rises,\n\nwith  doubled   spacing."
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Spaced.}\label{fig:space}" "\n"
            r"\end{figure}" "\n")
    work = project("space.tex", body, {"fig:space": entry(stored)})
    state, _ = run(work, "space.tex")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    wanted = "Accuracy over epochs 1--50 rises, with doubled spacing."
    problems = []
    if wanted not in shim:
        problems.append("the shim did not collapse the whitespace")
    if wanted not in alts(state):
        problems.append(f"found {alts(state)}")
    shutil.rmtree(work, ignore_errors=True)
    return report("whitespace is collapsed",
                  problems)


def check_long_description():
    long_text = ("A line chart of test accuracy against training epoch. " * 6).strip()
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Long.}\label{fig:long}" "\n"
            r"\end{figure}" "\n")
    work = project("long.tex", body, {"fig:long": entry(long_text)})
    state, _ = run(work, "long.tex")
    problems = [] if long_text in alts(state) else [
        f"the long text did not arrive intact ({len(long_text)} chars)"]
    shutil.rmtree(work, ignore_errors=True)
    return report("long description arrives whole", problems,
                  note=f"{len(long_text)} characters")


def check_non_ascii():
    text = "Détection des lésions : précision de 94 %, mesurée sur 50 époques."
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Accents.}\label{fig:accent}" "\n"
            r"\end{figure}" "\n")
    work = project("accent.tex", body, {"fig:accent": entry(text)})
    state, _ = run(work, "accent.tex")
    problems = [] if text in alts(state) else [f"found {alts(state)}"]
    shutil.rmtree(work, ignore_errors=True)
    return report("accented characters reach the PDF", problems, note=text)


def check_latex_errors():
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{Fine.}\label{fig:ok}" "\n"
            r"\end{figure}" "\n"
            r"\thisisnotacommand" "\n")
    work = project("broken.tex", body, {"fig:ok": entry("Some text.")})
    state, summary = run(work, "broken.tex")
    problems = []
    if not log_errors(work, "broken"):
        problems.append("the fixture did not actually produce a LaTeX error")
    elif not any("the tagged build reported" in w
                 for w in state.get("warnings", [])):
        problems.append(f"the errors were not surfaced: {state.get('warnings')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("LaTeX errors are reported", problems)


def check_rerun_until_settled():
    body = (r"\section{One}\label{sec:one}" "\n"
            r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{See section~\ref{sec:two}.}\label{fig:ref}" "\n"
            r"\end{figure}" "\n"
            r"\newpage\section{Two}\label{sec:two}" "\n"
            r"As shown in figure~\ref{fig:ref} of section~\ref{sec:one}." "\n")
    work = project("refs.tex", body, {"fig:ref": entry("A chart.")})
    state, summary = run(work, "refs.tex")
    log = open(os.path.join(work, "build", "refs.log"),
               encoding="utf-8", errors="replace").read()
    problems = []
    if injector.wants_rerun(log):
        problems.append("the build still ends asking to be re-run")
    if "??" in log and "LaTeX Warning: Reference" in log:
        problems.append("references were still unresolved in the final pass")
    shutil.rmtree(work, ignore_errors=True)
    return report("document rebuilt until settled",
                  problems, note=summary)


def check_rerun_detection():
    problems = []
    for text in ("LaTeX Warning: Label(s) may have changed. Rerun to get "
                 "cross-references right.",
                 "Package rerunfilecheck Warning: File `x.out' has changed."):
        if not injector.wants_rerun(text):
            problems.append(f"not recognised as asking for a re-run: {text[:50]!r}")
    if injector.wants_rerun("Output written on doc.pdf (2 pages)."):
        problems.append("an ordinary log line was read as asking for a re-run")
    return report("rerun requests are detected",
                  problems)


def check_include_subdirectory():
    chapter = (r"\section{Results}" "\n"
               r"\begin{figure}[h]\centering" "\n"
               r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
               r"  \caption{In a chapter.}\label{fig:chap}" "\n"
               r"\end{figure}" "\n")
    work = project("main.tex", r"\include{chapters/results}" "\n",
                   {"fig:chap": entry("A chart in a chapter.")},
                   extra_files={"chapters/results.tex": chapter})
    state, summary = run(work, "main.tex")
    problems = []
    if not os.path.isdir(os.path.join(work, "build", "chapters")):
        problems.append("build/chapters/ was not created for the .aux file")
    if "A chart in a chapter." not in alts(state):
        problems.append(f"the chapter's figure was not tagged: {alts(state)}")
    shutil.rmtree(work, ignore_errors=True)
    return report("\\include output folder is created",
                  problems, note=summary)


def check_alt_parsing_matches_stage7():
    problems = []
    for data in (b"/Alt <0041>", b"/Alt<0041>", b"/Alt  <0041>"):
        if not injector.ALT_IN_PDF.search(data):
            problems.append(f"{data!r} was not recognised as an /Alt entry")
    if not injector.ALT_IN_PDF_LITERAL.search(b"/Alt (a literal string)"):
        problems.append("a literal (...) /Alt string was not recognised")
    return report("/Alt parsed as in stage 7", problems)


def check_inline_alt_shared():
    body = (r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth]{example.png}" "\n"
            r"  \caption{First.}\label{fig:a}" "\n"
            r"\end{figure}" "\n"
            r"\begin{figure}[h]\centering" "\n"
            r"  \includegraphics[width=0.3\linewidth,"
            r"alt={Written by hand in the source.}]{example.png}" "\n"
            r"  \caption{Second.}\label{fig:b}" "\n"
            r"\end{figure}" "\n")
    work = project("inline.tex", body,
                   {"fig:a": entry("The first, from the store."),
                    "fig:b": entry("STORED text that must lose.")})
    state, _ = run(work, "inline.tex")
    found = alts(state)
    problems = []
    if "Written by hand in the source." not in found:
        problems.append(f"the author's inline alt= is missing: {found}")
    if "STORED text that must lose." in found:
        problems.append("the store overrode the author's inline alt=")
    if "The first, from the store." not in found:
        problems.append("the other figure lost its stored text")
    shutil.rmtree(work, ignore_errors=True)
    return report("inline alt= kept on a shared file", problems)


def check_escaping():
    problems = []
    for ch in "\\{}#$%&_~^":
        got = injector.escape(ch)
        if got == ch and ch not in "":
            problems.append(f"{ch!r} was passed through unescaped")
    if injector.escape("plain text") != "plain text":
        problems.append("ordinary text was altered")
    return report("every TeX special character is escaped", problems)


CHECKS = [
    check_starred_includegraphics,
    check_star_means_clip,
    check_two_optional_form,
    check_plain_includegraphics,
    check_no_options,
    check_shared_artifact,
    check_single_artifact,
    check_whitespace_collapsed,
    check_long_description,
    check_non_ascii,
    check_latex_errors,
    check_rerun_until_settled,
    check_rerun_detection,
    check_include_subdirectory,
    check_alt_parsing_matches_stage7,
    check_inline_alt_shared,
    check_escaping,
]


def run_all():
    return [check() for check in CHECKS]
