import io
import contextlib
import os
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

import preflight

PIPELINE = os.path.join(project_dir(), "pipeline.py")


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def scratch(name, text, encoding="utf-8", newline="\n"):
    work = tempfile.mkdtemp()
    path = os.path.join(work, name)
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(text.replace("\n", newline))
    return work, path


def run_stage(path, assume_yes=True):
    state = {"document": path, "assume_yes": assume_yes}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        summary = preflight.preflight(state)
    return state, summary, buffer.getvalue()


READY = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\InputIfFileExists{_alt/%s.alt.tex}{}{}
\begin{document}
Hello.
\end{document}
"""


def check_shim_mention():
    body = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\begin{document}
Stage 6 writes \verb|_alt/paper.alt.tex|, which the document reads.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    state, summary, _ = run_stage(path)
    after = open(path, encoding="utf-8").read()

    problems = []
    if "InputIfFileExists{_alt/paper.alt.tex}" not in after:
        problems.append("the shim line was not added; a mention in the text was "
                        "counted as reading it")
    if state.get("stop"):
        problems.append(f"the run stopped: {summary}")
    shutil.rmtree(work, ignore_errors=True)
    return report("shim path in text is not an input", problems)


def check_shim_present():
    work, path = scratch("paper.tex", READY % "paper")
    before = open(path, encoding="utf-8").read()
    state, summary, _ = run_stage(path)
    problems = []
    if open(path, encoding="utf-8").read() != before:
        problems.append("a document with all four requirements was edited anyway")
    if not state["preflight"].get("ok"):
        problems.append(f"reported not ok: {summary}")
    shutil.rmtree(work, ignore_errors=True)
    return report("existing shim line is recognised", problems)


def check_shim_rechecked():
    source = open(os.path.join(project_dir(), "preflight.py"), encoding="utf-8").read()
    body = source[source.index("again = blank_comments(read(path))"):]
    body = body[:body.index("state[\"preflight\"] = {\"ok\": True")]
    problems = []
    if "check_shim" not in body:
        problems.append("the re-check after editing does not include "
                        "check_shim")
    return report("shim line is re-checked after editing", problems)


def check_backup_kept():
    body = r"""\documentclass{article}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    original = open(path, encoding="utf-8").read()

    run_stage(path)
    backup = path + ".bak"
    problems = []
    if not os.path.isfile(backup):
        problems.append("no .bak was written at all")
    elif open(backup, encoding="utf-8").read() != original:
        problems.append(".bak does not hold the original")

    edited = open(path, encoding="utf-8").read().replace(
        r"\usepackage{graphicx}", "")
    open(path, "w", encoding="utf-8").write(edited)
    state, summary, _ = run_stage(path)

    if os.path.isfile(backup) and open(backup, encoding="utf-8").read() != original:
        problems.append("the second run overwrote .bak, losing the original")
    if not os.path.isfile(path + ".bak.1"):
        problems.append("the second run kept no backup of its own")
    shutil.rmtree(work, ignore_errors=True)
    return report("second run keeps the first backup", problems)


def check_one_line_preamble():
    body = "\\documentclass{article}\\begin{document}Hi\\end{document}\n"
    work, path = scratch("oneline.tex", body)
    before = open(path, encoding="utf-8").read()
    state, summary, output = run_stage(path)

    problems = []
    if open(path, encoding="utf-8").read() != before:
        problems.append("the document was edited; it should have been refused")
    if not state.get("stop"):
        problems.append("the run did not stop")
    if "share a line" not in output:
        problems.append(f"the reason was not explained; got {output.strip()[:90]!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("one-line preamble is refused",
                  problems, note=summary)


def check_metadata_merge():
    body = r"""\DocumentMetadata{lang=en-ZA, testphase={phase-III,math}}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\InputIfFileExists{_alt/paper.alt.tex}{}{}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    run_stage(path)
    after = open(path, encoding="utf-8").read()
    problems = []
    for kept in ("lang=en-ZA", "testphase={phase-III,math}"):
        if kept not in after:
            problems.append(f"{kept} was dropped when the line was rewritten")
    for added in ("tagging=on", "pdfstandard=ua-2"):
        if added not in after:
            problems.append(f"{added} was not added")
    shutil.rmtree(work, ignore_errors=True)
    return report("existing metadata keys are kept", problems)


def check_commented_package():
    body = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
% \usepackage{graphicx}
\usepackage{tikz}
\InputIfFileExists{_alt/paper.alt.tex}{}{}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    run_stage(path)
    after = open(path, encoding="utf-8").read()
    problems = []
    if after.count(r"\usepackage{graphicx}") != 2:
        problems.append("graphicx was not added; the commented-out line was "
                        "counted as loading it")
    shutil.rmtree(work, ignore_errors=True)
    return report("commented-out package is ignored", problems)


def check_bom_crlf():
    body = r"""\documentclass{article}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body, encoding="utf-8-sig", newline="\r\n")
    run_stage(path)
    data = open(path, "rb").read()
    problems = []
    if not data.startswith(b"\xef\xbb\xbf"):
        problems.append("the byte-order mark was dropped")
    if b"\r\n" not in data:
        problems.append("CRLF line endings were not preserved")
    if b"\n" in data.replace(b"\r\n", b""):
        problems.append("the edit introduced bare LF endings into a CRLF file")
    if b"DocumentMetadata" not in data:
        problems.append("nothing was actually added")
    shutil.rmtree(work, ignore_errors=True)
    return report("BOM and CRLF are kept", problems)


def check_decline():
    body = r"""\documentclass{article}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    before = open(path, encoding="utf-8").read()
    result = subprocess.run([sys.executable, PIPELINE, "--tex", path, "--auto"],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    problems = []
    if open(path, encoding="utf-8").read() != before:
        problems.append("the file was edited without a yes")
    if os.path.exists(path + ".bak"):
        problems.append("a backup was written for an edit that never happened")
    if result.returncode == 0:
        problems.append("declining reported success")
    shutil.rmtree(work, ignore_errors=True)
    return report("declining changes nothing", problems)


def check_no_documentclass():
    work, path = scratch("chapter.tex", "\\section{Method}\nSome prose.\n")
    state, summary, _ = run_stage(path)
    problems = []
    if not state.get("stop"):
        problems.append("did not stop")
    if "documentclass" not in summary:
        problems.append(f"the reason does not name the problem: {summary!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing documentclass is reported",
                  problems, note=summary)


def check_unclosed_metadata():
    body = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\begin{document}
Hello.
\end{document}
"""
    work, path = scratch("paper.tex", body)
    before = open(path, encoding="utf-8").read()
    state, summary, output = run_stage(path)
    problems = []
    if open(path, encoding="utf-8").read() != before:
        problems.append("the file was edited despite the unbalanced brace")
    if not state.get("stop"):
        problems.append("the run did not stop")
    shutil.rmtree(work, ignore_errors=True)
    return report("unclosed DocumentMetadata is refused", problems)


def check_apply_order():
    raw = "AB"
    fixes = [(0, 0, "first\n"), (0, 0, "second\n")]
    got = preflight.apply(raw, fixes)
    problems = []
    if got != "first\nsecond\nAB":
        problems.append(f"got {got!r}, expected the fixes in list order")
    return report("fixes at one offset keep list order", problems)


CHECKS = [
    check_shim_mention,
    check_shim_present,
    check_shim_rechecked,
    check_backup_kept,
    check_one_line_preamble,
    check_metadata_merge,
    check_commented_package,
    check_bom_crlf,
    check_decline,
    check_no_documentclass,
    check_unclosed_metadata,
    check_apply_order,
]


def run():
    return [check() for check in CHECKS]
