import contextlib
import io
import json
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


PROJECT = project_dir()
sys.path.insert(0, PROJECT)

import describer
import pipeline
import validator

PIPELINE = os.path.join(PROJECT, "pipeline.py")
IMAGES = os.path.join(PROJECT, "figures")


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


BARE = r"""%% A document that has never been near this pipeline.
\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\graphicspath{{figures/}}
\begin{document}
\section{Results}

\begin{figure}[h]
  \centering
  \includegraphics[width=0.45\linewidth]{accuracy-chart.png}
  \caption{Test accuracy against training epoch.}
  \label{fig:accuracy}
\end{figure}

\end{document}
"""


def build(files, images=(("accuracy-chart.png", "new-chart.png"),)):
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "figures"), exist_ok=True)
    for name, source in images:
        shutil.copyfile(os.path.join(IMAGES, source),
                        os.path.join(work, "figures", name))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    for relative, text in files.items():
        full = os.path.join(work, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    return work


CALLS = []


def fake_model(image_path, prompt, context):
    CALLS.append(os.path.basename(image_path))
    profile = "figure"
    for candidate in ("chart", "photo", "diagram", "artifact"):
        if f"Describe this {candidate}" in prompt:
            profile = candidate
            break
    return (f"A {profile} described by the test model, call number "
            f"{len(CALLS)}."), {"fake": True}


def run(work, name="main.tex", adapter=fake_model, offline=False, reset=True):
    if reset:
        CALLS.clear()
    saved = dict(describer.ADAPTERS)
    previous = os.environ.get(describer.ENV_NAME)
    describer.ADAPTERS["gemini"] = adapter
    os.environ[describer.ENV_NAME] = "test-key"
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            state = pipeline.run(os.path.join(work, name), assume_yes=True,
                                 verbose=True, offline=offline)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved)
        if previous is None:
            os.environ.pop(describer.ENV_NAME, None)
        else:
            os.environ[describer.ENV_NAME] = previous
    return state, output.getvalue()


def store(work, stem="main"):
    with open(os.path.join(work, stem + ".alt"), encoding="utf-8") as fh:
        return json.load(fh)


def alts_in_pdf(state):
    return (state.get("injected") or {}).get("in_pdf") or []


def check_full_run():
    work = build({"main.tex": BARE})
    state, output = run(work)
    problems = []

    source = open(os.path.join(work, "main.tex"), encoding="utf-8").read()
    for needed in (r"\DocumentMetadata", "tagging=on", "pdfstandard=ua-2",
                   r"\usepackage{graphicx}", r"\usepackage{tikz}",
                   "InputIfFileExists{_alt/main.alt.tex}"):
        if needed not in source:
            problems.append(f"stage 0 did not add {needed}")
    if not os.path.isfile(os.path.join(work, "main.tex.bak")):
        problems.append("no backup of the original was kept")

    if len(CALLS) != 1:
        problems.append(f"the model was called {len(CALLS)}x, expected 1")

    entries = store(work)["entries"]
    if "fig:accuracy" not in entries:
        problems.append(f"no entry was written: {list(entries)}")
    else:
        entry = entries["fig:accuracy"]
        if entry["status"] != "generated":
            problems.append(f"status={entry['status']!r}; only a person approves")
        if entry["profile"] != "chart":
            problems.append(f"profile={entry['profile']!r}, expected 'chart'")
        if entry["alt"] not in alts_in_pdf(state):
            problems.append("the written text is not in the PDF")

    v = state.get("validation") or {}
    if not v.get("ok"):
        problems.append(f"the run did not pass: {v.get('blocking')}")
    if v.get("good") != 1:
        problems.append(f"{v.get('good')} figure(s) with real alt text, expected 1")
    if state.get("stop"):
        problems.append("the run stopped")

    shutil.rmtree(work, ignore_errors=True)
    return report("bare document to tagged PDF",
                  problems, note=(state.get("validation") or {}).get("figures"))


def check_second_run():
    work = build({"main.tex": BARE})
    run(work)
    before = open(os.path.join(work, "main.alt"), "rb").read()

    state, output = run(work)
    problems = []
    if CALLS:
        problems.append(f"the model was called {len(CALLS)}x on an unchanged "
                        f"document; the cache is not working")
    if open(os.path.join(work, "main.alt"), "rb").read() != before:
        problems.append("the .alt file was rewritten with nothing to write")
    if "nothing to render" not in output:
        problems.append("something was rendered again")
    if not (state.get("validation") or {}).get("ok"):
        problems.append("the second run did not pass")
    if (state.get("validation") or {}).get("good") != 1:
        problems.append("the reused text did not reach the PDF")

    altreq = open(os.path.join(work, "_alt", "main.altreq"), encoding="utf-8").read()
    if altreq.splitlines()[0].strip() != "0":
        problems.append(f"the request file is not empty: {altreq.splitlines()[0]!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("second run makes no calls or writes", problems)


def check_changed_figure():
    body = BARE.replace(
        r"\end{document}",
        r"""\begin{figure}[h]
  \centering
  \includegraphics[width=0.45\linewidth]{loss-curve.png}
  \caption{Training loss against epoch.}
  \label{fig:loss}
\end{figure}

\end{document}""")
    work = build({"main.tex": body},
                 images=(("accuracy-chart.png", "new-chart.png"),
                         ("loss-curve.png", "stale-chart.png")))
    run(work)
    problems = []
    if len(CALLS) != 2:
        problems.append(f"the first run made {len(CALLS)} call(s), expected 2")
    first = {k: v["alt"] for k, v in store(work)["entries"].items()}

    shutil.copyfile(os.path.join(IMAGES, "method-diagram.png"),
                    os.path.join(work, "figures", "loss-curve.png"))

    state, output = run(work)
    if len(CALLS) != 1:
        problems.append(f"the second run made {len(CALLS)} call(s), expected 1 "
                        f"(only one figure changed)")
    after = {k: v["alt"] for k, v in store(work)["entries"].items()}
    if after.get("fig:accuracy") != first.get("fig:accuracy"):
        problems.append("the unchanged figure was re-described")
    if after.get("fig:loss") == first.get("fig:loss"):
        problems.append("the changed figure kept its old description")
    if not (state.get("validation") or {}).get("ok"):
        problems.append(f"the run failed: {(state.get('validation') or {}).get('blocking')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("only the changed figure is described again",
                  problems)


def check_failed_redescribe():
    work = build({"main.tex": BARE})
    run(work)
    original = store(work)["entries"]["fig:accuracy"]["alt"]

    shutil.copyfile(os.path.join(IMAGES, "stale-chart.png"),
                    os.path.join(work, "figures", "accuracy-chart.png"))

    def refuses(image_path, prompt, context):
        raise describer.DescribeError("the model refused the request (HTTP 400)")

    state, output = run(work, adapter=refuses)
    problems = []
    if original in alts_in_pdf(state):
        problems.append("the out-of-date description was injected anyway")
    if (state.get("validation") or {}).get("ok"):
        problems.append("the run passed with a figure carrying no usable alt text")
    if original not in json.dumps(store(work)):
        problems.append("the old text was deleted from the store; it should stay "
                        "there until something replaces it")
    shutil.rmtree(work, ignore_errors=True)
    return report("failed re-description injects no old text",
                  problems)


def check_locked_text():
    work = build({"main.tex": BARE})
    run(work)

    written = os.path.join(work, "main.alt")
    data = json.load(open(written, encoding="utf-8"))
    human = ("Line chart of test accuracy against training epoch, rising from "
             "61 to 94 percent over 50 epochs.")
    data["entries"]["fig:accuracy"]["alt"] = human
    data["entries"]["fig:accuracy"]["status"] = "edited"
    data["entries"]["fig:accuracy"]["by"] = "u22534599"
    data["entries"]["fig:accuracy"]["reviewed"] = "2026-09-01"
    json.dump(data, open(written, "w", encoding="utf-8"), indent=2)

    shutil.copyfile(os.path.join(IMAGES, "stale-chart.png"),
                    os.path.join(work, "figures", "accuracy-chart.png"))

    state, output = run(work)
    entry = store(work)["entries"]["fig:accuracy"]
    problems = []
    if CALLS:
        problems.append(f"the model was called {len(CALLS)}x for human-owned text")
    if entry["alt"] != human:
        problems.append("the human's text was overwritten")
    if entry["status"] != "edited":
        problems.append(f"status became {entry['status']!r}")
    if entry.get("reviewed") != "2026-09-01":
        problems.append("a field the author added by hand was lost")
    if human not in alts_in_pdf(state):
        problems.append("the human's text did not reach the PDF")
    if "has changed since" not in output:
        problems.append("the drift under the locked entry was not reported")
    shutil.rmtree(work, ignore_errors=True)
    return report("edited and approved text is kept", problems)


def check_inline_alt():
    body = BARE.replace(
        r"\includegraphics[width=0.45\linewidth]{accuracy-chart.png}",
        r"\includegraphics[width=0.45\linewidth,"
        r"alt={Written by hand in the source, and not to be overridden.}]"
        r"{accuracy-chart.png}")
    work = build({"main.tex": body})
    state, output = run(work)
    found = alts_in_pdf(state)
    problems = []
    if CALLS:
        problems.append("the model was called for a figure the author described")
    if "Written by hand in the source, and not to be overridden." not in found:
        problems.append(f"the author's text is not in the PDF: {found}")
    if os.path.isfile(os.path.join(work, "main.alt")):
        problems.append("a store was written for a document with nothing to store")
    if not (state.get("validation") or {}).get("ok"):
        problems.append("the run did not pass")
    shutil.rmtree(work, ignore_errors=True)
    return report("inline alt= is used", problems)


def check_decorative():
    body = BARE.replace(r"\section{Results}", r"""\section{Results}

%!alt artifact
\noindent\includegraphics[width=\linewidth]{divider.png}
""")
    work = build({"main.tex": body},
                 images=(("accuracy-chart.png", "new-chart.png"),
                         ("divider.png", "divider.png")))
    state, output = run(work)
    found = alts_in_pdf(state)
    problems = []
    if len(CALLS) != 1:
        problems.append(f"the model was called {len(CALLS)}x, expected 1 "
                        f"(the divider must not be described)")
    if any("divider" in t for t in found):
        problems.append(f"the divider was given alt text: {found}")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    if "alt@artifact{divider.png}" not in shim:
        problems.append("the divider was not marked as an artifact")
    if not (state.get("validation") or {}).get("ok"):
        problems.append(f"the run failed: {(state.get('validation') or {}).get('blocking')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("decorative image is not described",
                  problems)


def check_shared_image():
    body = BARE.replace(r"\section{Results}", r"""\section{Results}

%!alt chart
\noindent\includegraphics[width=0.2\linewidth]{accuracy-chart.png}
""")
    work = build({"main.tex": body})
    state, output = run(work)
    found = alts_in_pdf(state)
    problems = []
    if len(CALLS) != 2:
        problems.append(f"the model was called {len(CALLS)}x, expected 2")
    if len(found) != 2:
        problems.append(f"{len(found)} /Alt entries, expected 2: {found}")
    elif found[0] == found[1]:
        problems.append("both uses were handed the same description")
    else:
        if "call number 1" not in found[0]:
            problems.append(f"the first figure in the document did not get the "
                            f"first description: {found}")
    shim = open(state["injected"]["shim"], encoding="utf-8").read()
    if "alt@seq{accuracy-chart.png}{1}" not in shim:
        problems.append("the shared file was not pinned per occurrence")
    shutil.rmtree(work, ignore_errors=True)
    return report("shared image keeps separate text",
                  problems, note=" | ".join(t[-18:] for t in found))


def check_multifile():
    main = r"""\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\graphicspath{{figures/}}
\begin{document}
\begin{figure}[h]
  \centering
  \includegraphics[width=0.4\linewidth]{accuracy-chart.png}
  \caption{Test accuracy against training epoch.}
  \label{fig:accuracy}
\end{figure}

\input{chapters/method}
\include{chapters/results}
\end{document}
"""
    method = r"""\section{Method}
\begin{figure}[h]
  \centering
  \includegraphics[width=0.4\linewidth]{architecture.png}
  \caption{The classification pipeline, drawn as a block diagram.}
  \label{fig:architecture}
\end{figure}

\input{chapters/detail}
"""
    detail = r"""\subsection{Detail}
\begin{figure}[h]
  \centering
  \begin{tikzpicture}
    \draw (0,0) rectangle (2,1) node[midway] {CNN};
  \end{tikzpicture}
  \caption{A classifier block, drawn inline.}
  \label{fig:block}
\end{figure}
"""
    results = r"""\section{Results}
\begin{figure}[h]
  \centering
  \includegraphics[width=0.4\linewidth]{leaf.jpg}
  \caption{A photograph of a single leaf.}
  \label{fig:leaf}
\end{figure}
"""
    work = build({"main.tex": main, "chapters/method.tex": method,
                  "chapters/detail.tex": detail,
                  "chapters/results.tex": results},
                 images=(("accuracy-chart.png", "new-chart.png"),
                         ("architecture.png", "method-diagram.png"),
                         ("leaf.jpg", "unlabelled-photo.jpg")))
    state, output = run(work)
    problems = []

    files = state.get("tex_files") or []
    for wanted in ("main.tex", "chapters/method.tex", "chapters/detail.tex",
                   "chapters/results.tex"):
        if wanted not in files:
            problems.append(f"{wanted} was never walked; got {files}")

    entries = store(work)["entries"]
    for key in ("fig:accuracy", "fig:architecture", "fig:block", "fig:leaf"):
        if key not in entries:
            problems.append(f"{key} never reached the store")

    stray = [f for f in os.listdir(os.path.join(work, "chapters"))
             if f.endswith(".alt")]
    if stray:
        problems.append(f"a second store was written beside a chapter: {stray}")

    profiles = {k: entries[k]["profile"] for k in entries}
    if profiles.get("fig:architecture") != "diagram":
        problems.append(f"fig:architecture profile={profiles.get('fig:architecture')!r}")
    if profiles.get("fig:block") != "diagram":
        problems.append(f"fig:block profile={profiles.get('fig:block')!r}")

    v = state.get("validation") or {}
    if v.get("good", 0) < 3:
        problems.append(f"only {v.get('good')} figure(s) carry real alt text; "
                        f"{v.get('useless', 0)} are filename fallbacks")
    if not v.get("ok"):
        problems.append(f"the run failed: {v.get('blocking')}")
    if not any("fig:block" in note for note in v.get("limits") or []):
        problems.append("the inline tikzpicture's gap was not reported")

    shutil.rmtree(work, ignore_errors=True)
    return report("main file with three chapters", problems,
                  note=f"{v.get('figures')} tagged, {v.get('good')} with real text")


def check_offline_then_real():
    work = build({"main.tex": BARE})
    state, output = run(work, offline=True)
    problems = []
    entry = store(work)["entries"]["fig:accuracy"]
    if not entry["alt"].startswith("[stub]"):
        problems.append(f"the offline run wrote {entry['alt']!r}")
    if entry.get("by") != "stub":
        problems.append(f"the placeholder is signed {entry.get('by')!r}")
    if "placeholder" not in output:
        problems.append("the offline run did not say its text is a placeholder")

    state, output = run(work)
    if len(CALLS) != 1:
        problems.append(f"the real run called the model {len(CALLS)}x, expected 1; "
                        f"the placeholder was reused")
    after = store(work)["entries"]["fig:accuracy"]
    if after["alt"].startswith("[stub]"):
        problems.append("the placeholder is still there after a real run")
    if after["alt"] not in alts_in_pdf(state):
        problems.append("the real description did not reach the PDF")
    shutil.rmtree(work, ignore_errors=True)
    return report("offline text replaced by a real run",
                  problems)


def check_missing_file():
    body = BARE.replace(r"\end{document}", r"""\begin{figure}[h]
  \centering
  \includegraphics[width=0.4\linewidth]{not-made-yet.pdf}
  \caption{A chart whose file has not been generated.}
  \label{fig:absent}
\end{figure}

\end{document}""")
    work = build({"main.tex": body})
    state, output = run(work)
    problems = []
    keys = [r["key"] for r in state["resolved"]]
    if "fig:absent" not in keys:
        problems.append(f"the figure was dropped from the scan: {keys}")
    blocked = [r for r in state["resolved"] if r["outcome"] == "blocked"]
    if len(blocked) != 1:
        problems.append(f"{len(blocked)} blocked, expected 1")
    if "cannot find the file" not in output:
        problems.append("the missing file was not named")
    v = state.get("validation") or {}
    if not v.get("ok"):
        problems.append(f"a missing figure file failed the run: "
                        f"{v.get('blocking')}")
    if not any("not on disk" in note for note in v.get("limits") or []):
        problems.append(f"it was not reported as a limitation: {v.get('limits')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing figure file is reported",
                  problems)


def check_idempotent():
    work = build({"main.tex": BARE})
    run(work)
    store_bytes = open(os.path.join(work, "main.alt"), "rb").read()
    shim_bytes = open(os.path.join(work, "_alt", "main.alt.tex"), "rb").read()
    source = open(os.path.join(work, "main.tex"), "rb").read()

    run(work)
    problems = []
    if open(os.path.join(work, "main.alt"), "rb").read() != store_bytes:
        problems.append("the .alt store changed on a second identical run")
    if open(os.path.join(work, "_alt", "main.alt.tex"), "rb").read() != shim_bytes:
        problems.append("the shim changed on a second identical run")
    if open(os.path.join(work, "main.tex"), "rb").read() != source:
        problems.append("the document was edited again on the second run")
    if os.path.isfile(os.path.join(work, "main.tex.bak.1")):
        problems.append("a second backup was taken with nothing to back up")
    shutil.rmtree(work, ignore_errors=True)
    return report("second identical run changes nothing", problems)


def check_undescribed():
    ready = BARE.replace(r"\documentclass",
                         "\\DocumentMetadata{tagging=on, pdfstandard=ua-2}\n"
                         "\\documentclass")
    ready = ready.replace(r"\usepackage[T1]{fontenc}",
                          "\\usepackage[T1]{fontenc}\n\\usepackage{graphicx}\n"
                          "\\usepackage{tikz}")
    work = build({"main.tex": ready})

    def refuses(image_path, prompt, context):
        raise describer.DescribeError("the model refused the request")

    state, output = run(work, adapter=refuses)
    v = state.get("validation") or {}
    problems = []
    if v.get("ok"):
        problems.append("an undescribed document passed even though its alt "
                        "text is only a filename")
    if v.get("useless") != 1:
        problems.append(f"{v.get('useless')} filename fallback(s) found, expected 1")
    if not state.get("stop"):
        problems.append("the run did not stop on a failing document")
    shutil.rmtree(work, ignore_errors=True)
    return report("undescribed document fails",
                  problems)


def check_broken_document():
    broken = BARE.replace(r"\end{document}",
                          "\\textbf{this brace is never closed\n"
                          "\\end{document}")
    work = build({"main.tex": broken})
    before = open(os.path.join(work, "main.tex"), "rb").read()
    state, output = run(work)
    problems = []
    if not state.get("stop"):
        problems.append("the run did not stop")
    if open(os.path.join(work, "main.tex"), "rb").read() != before:
        problems.append("a document that does not build was edited anyway")
    if os.path.isfile(os.path.join(work, "main.alt")):
        problems.append("a store was written for a document that does not build")
    if "does not compile" not in output:
        problems.append(f"the reason was not given: {output.strip()[:120]!r}")
    if state.get("figures"):
        problems.append("later stages ran on a document stage 0 rejected")
    shutil.rmtree(work, ignore_errors=True)
    return report("broken document stops at stage 0",
                  problems)


def check_exit_codes():
    problems = []

    work = build({"main.tex": BARE})
    passing = subprocess.run([sys.executable, PIPELINE, "--tex",
                              os.path.join(work, "main.tex"), "--yes",
                              "--offline", "--auto"],
                             capture_output=True, text=True)
    if passing.returncode != 0:
        problems.append(f"a passing run exited {passing.returncode}: "
                        f"{passing.stdout.strip()[-160:]}")
    shutil.rmtree(work, ignore_errors=True)

    ready = BARE.replace(r"\documentclass",
                         "\\DocumentMetadata{tagging=on, pdfstandard=ua-2}\n"
                         "\\documentclass")
    ready = ready.replace(r"\usepackage[T1]{fontenc}",
                          "\\usepackage[T1]{fontenc}\n\\usepackage{graphicx}\n"
                          "\\usepackage{tikz}")
    work = build({"main.tex": ready})
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env["GEMINI_API_KEY"] = ""
    failing = subprocess.run([sys.executable, PIPELINE, "--tex",
                              os.path.join(work, "main.tex"), "--yes", "--auto"],
                             capture_output=True, text=True, env=env)
    if failing.returncode != 1:
        problems.append(f"a stopped run exited {failing.returncode}, expected 1")
    shutil.rmtree(work, ignore_errors=True)

    missing = subprocess.run([sys.executable, PIPELINE, "--tex", "no-such.tex",
                              "--auto"], capture_output=True, text=True)
    if missing.returncode != 1:
        problems.append(f"a missing document exited {missing.returncode}")
    if "no such document" not in missing.stdout:
        problems.append(f"unclear message: {missing.stdout.strip()!r}")
    return report("exit codes",
                  problems)


def check_stage_exception():
    work = build({"main.tex": BARE})
    saved = pipeline.STAGES

    def explode(state):
        raise RuntimeError("unexpected failure")

    pipeline.STAGES = [("preflight", saved[0][1]), ("scan", explode)]
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            state = pipeline.run(os.path.join(work, "main.tex"), assume_yes=True)
    except Exception as exc:
        pipeline.STAGES = saved
        shutil.rmtree(work, ignore_errors=True)
        return report("stage exception is reported",
                      [f"the exception escaped: {type(exc).__name__}: {exc}"])
    finally:
        pipeline.STAGES = saved

    text = output.getvalue()
    problems = []
    if not state.get("stop"):
        problems.append("the run did not stop")
    if (state.get("crashed") or {}).get("stage") != "scan":
        problems.append(f"the failing stage was not recorded: {state.get('crashed')}")
    if "[scan] stopped" not in text:
        problems.append(f"the output does not name the stage: {text.strip()[-160:]!r}")
    if "RuntimeError" not in text:
        problems.append("the output does not say what went wrong")
    shutil.rmtree(work, ignore_errors=True)
    return report("stage exception is reported", problems)


def check_offline():
    work = build({"main.tex": BARE})
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    result = subprocess.run([sys.executable, PIPELINE, "--tex",
                             os.path.join(work, "main.tex"), "--yes", "--offline",
                             "--auto"],
                            capture_output=True, text=True, env=env)
    problems = []
    if result.returncode != 0:
        problems.append(f"exited {result.returncode}: {result.stdout.strip()[-200:]}")
    if not os.path.isfile(os.path.join(work, "main.alt")):
        problems.append("no store was written")
    if "stub: no model was called" not in result.stdout:
        problems.append("the run did not say the model was never called")
    shutil.rmtree(work, ignore_errors=True)
    return report("--offline needs no key", problems)


def check_verbose():
    body = BARE.replace(r"\section{Results}", r"""\section{Results}

\noindent\includegraphics[width=0.2\linewidth]{accuracy-chart.png}
""")
    work = build({"main.tex": body})
    state, verbose = run(work)

    saved = dict(describer.ADAPTERS)
    describer.ADAPTERS["gemini"] = fake_model
    os.environ[describer.ENV_NAME] = "test-key"
    quiet_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(quiet_out):
            pipeline.run(os.path.join(work, "main.tex"), assume_yes=True,
                         verbose=False)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved)
    quiet = quiet_out.getvalue()

    problems = []
    if "warning(s)" not in quiet:
        problems.append("a quiet run does not count its warnings")
    if "image outside any figure" not in verbose:
        problems.append("a verbose run does not show the warning itself")
    if "image outside any figure" in quiet:
        problems.append("a quiet run prints the full warnings")
    shutil.rmtree(work, ignore_errors=True)
    return report("--verbose shows warnings",
                  problems)


def check_state_keys():
    work = build({"main.tex": BARE})
    state, _ = run(work)
    problems = []
    expected = {
        "preflight": dict, "figures": list, "tex_files": list, "config": dict,
        "resolved": list, "requests": list, "renders": list, "described": list,
        "written": dict, "injected": dict, "validation": dict, "warnings": list,
    }
    for key, kind in expected.items():
        if key not in state:
            problems.append(f"the state carries no {key!r} after a full run")
        elif not isinstance(state[key], kind):
            problems.append(f"{key} is {type(state[key]).__name__}, "
                            f"expected {kind.__name__}")

    for figure in state.get("figures") or []:
        for field in ("key", "kind", "source", "where", "caption", "label",
                      "alt", "profile", "hash", "offset"):
            if field not in figure:
                problems.append(f"a figure record has no {field!r}")
            break
    shutil.rmtree(work, ignore_errors=True)
    return report("state has the expected keys",
                  problems)


CHECKS = [
    check_full_run,
    check_second_run,
    check_changed_figure,
    check_failed_redescribe,
    check_locked_text,
    check_inline_alt,
    check_decorative,
    check_shared_image,
    check_multifile,
    check_offline_then_real,
    check_missing_file,
    check_idempotent,
    check_undescribed,
    check_broken_document,
    check_exit_codes,
    check_stage_exception,
    check_offline,
    check_verbose,
    check_state_keys,
]


def main():
    if not shutil.which("pdflatex"):
        print("skipped: pdflatex is not on PATH, and these run the real chain")
        return 0
    results = [check() for check in CHECKS]
    import extra_auto
    results.extend(extra_auto.run_all())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
