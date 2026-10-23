import contextlib
import io
import json
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

import resolver
import scanner

PROJECT = project_dir()
PIXEL = os.path.join(HERE, "figures", "example.png")

HEAD = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\graphicspath{{figures/}}
\begin{document}
"""

FIGURE = r"""\begin{figure}[h]
  \centering
  \includegraphics[width=0.3\linewidth]{example.png}
  \caption{Test accuracy against training epoch.}
  \label{fig:one}
\end{figure}
"""


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def workspace(entries=None, body=None):
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "figures"))
    shutil.copyfile(PIXEL, os.path.join(work, "figures", "example.png"))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    with open(os.path.join(work, "doc.tex"), "w", encoding="utf-8") as fh:
        fh.write(HEAD + (body if body is not None else FIGURE) + "\\end{document}\n")
    if entries is not None:
        with open(os.path.join(work, "doc.alt"), "w", encoding="utf-8") as fh:
            json.dump({"schema": "alt/1", "document": "doc.tex",
                       "entries": entries}, fh, indent=2)
    return work


def resolve(work):
    state = {"document": os.path.join(work, "doc.tex")}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
    return state


def live_hash(work):
    return resolve(work)["figures"][0]["hash"]


def entry(**over):
    base = {"alt": "A line chart of accuracy against epoch.", "profile": "chart",
            "source": "example.png", "hash": "sha256:0000000000000000",
            "status": "generated", "by": "some-model", "at": "2026-01-01T00:00Z"}
    base.update(over)
    return base


def check_stale_text():
    old = "OUT OF DATE: the chart as it looked before it was replotted."
    work = workspace({"fig:one": entry(alt=old, hash="sha256:staleaaaaaaaaaa")})
    state = resolve(work)
    got = state["resolved"][0]
    problems = []
    if got["outcome"] != "describe":
        problems.append(f"outcome {got['outcome']!r}, expected 'describe'")
    if got["alt"] is not None:
        problems.append(f"stale text was handed on as this figure's alt text: "
                        f"{got['alt']!r}")
    if got.get("previous") != old:
        problems.append("the old text was not kept for reporting")
    if old not in open(os.path.join(work, "doc.alt"), encoding="utf-8").read():
        problems.append("the old text was deleted from the store; it should stay "
                        "until something replaces it")
    shutil.rmtree(work, ignore_errors=True)
    return report("stale text is not used", problems)


def check_fresh_text():
    work = workspace({})
    digest = live_hash(work)
    shutil.rmtree(work, ignore_errors=True)

    text = "A line chart rising from 61 to 94 percent over 50 epochs."
    work = workspace({"fig:one": entry(alt=text, hash=digest)})
    got = resolve(work)["resolved"][0]
    problems = []
    if got["outcome"] != "reuse":
        problems.append(f"outcome {got['outcome']!r}, expected 'reuse'")
    if got["alt"] != text:
        problems.append(f"the stored text was not carried: {got['alt']!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("unchanged figure reuses its text", problems)


def check_stub_requeued():
    work = workspace({})
    digest = live_hash(work)
    shutil.rmtree(work, ignore_errors=True)

    work = workspace({"fig:one": entry(
        alt="[stub] a chart; no model was called, so this is placeholder text.",
        by="stub", hash=digest)})
    state = resolve(work)
    got = state["resolved"][0]
    problems = []
    if got["outcome"] != "describe":
        problems.append(f"outcome {got['outcome']!r}, expected 'describe'; the "
                        f"placeholder was reused")
    if got["alt"] is not None:
        problems.append(f"the placeholder was handed on: {got['alt']!r}")
    if not any("placeholder" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("offline placeholder is queued again",
                  problems)


def check_model_entry_reused():
    work = workspace({})
    digest = live_hash(work)
    shutil.rmtree(work, ignore_errors=True)
    work = workspace({"fig:one": entry(by="gemma-4-26b-a4b-it", hash=digest)})
    got = resolve(work)["resolved"][0]
    shutil.rmtree(work, ignore_errors=True)
    return report("model entry is reused",
                  [] if got["outcome"] == "reuse" else
                  [f"outcome {got['outcome']!r}, expected 'reuse'"])


def check_non_object_entry():
    work = workspace({"fig:one": "someone typed the text straight in"})
    problems = []
    try:
        state = resolve(work)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        return report("non-object entry is skipped",
                      [f"the run died with {type(exc).__name__}: {exc}"])
    if state.get("stop"):
        problems.append("the run stopped; a single bad entry is not fatal")
    if not state["resolved"] or state["resolved"][0]["outcome"] != "describe":
        problems.append("the figure was not simply queued for description")
    if not any("not an object" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("non-object entry is skipped", problems)


def check_non_text_alt():
    work = workspace({"fig:one": entry(alt=250)})
    problems = []
    try:
        state = resolve(work)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        return report("non-text alt is skipped",
                      [f"the run died with {type(exc).__name__}: {exc}"])
    if state["resolved"][0]["alt"] is not None:
        problems.append(f"a number was carried on as alt text: "
                        f"{state['resolved'][0]['alt']!r}")
    if not any("non-text" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("non-text alt is skipped",
                  problems)


def check_null_alt_artifact():
    work = workspace({"fig:one": entry(alt=None, status="artifact",
                                       profile="artifact")})
    state = resolve(work)
    got = state["resolved"][0]
    problems = []
    if got["outcome"] != "artifact":
        problems.append(f"outcome {got['outcome']!r}, expected 'artifact'")
    if any("non-text" in w for w in state["warnings"]):
        problems.append("a legitimate null alt was reported as malformed")
    shutil.rmtree(work, ignore_errors=True)
    return report("artifact with null alt is valid", problems)


def check_bad_entry_isolated():
    work = workspace({})
    digest = live_hash(work)
    shutil.rmtree(work, ignore_errors=True)
    work = workspace({"junk": ["not", "an", "entry"],
                      "fig:one": entry(hash=digest)})
    state = resolve(work)
    problems = []
    if state["resolved"][0]["outcome"] != "reuse":
        problems.append(f"the good entry was lost too: "
                        f"{state['resolved'][0]['outcome']!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("one bad entry does not affect others", problems)


def check_wrong_schema():
    work = workspace({"fig:one": entry()})
    path = os.path.join(work, "doc.alt")
    store = json.load(open(path, encoding="utf-8"))
    store["schema"] = "alt/99"
    json.dump(store, open(path, "w", encoding="utf-8"), indent=2)
    state = resolve(work)
    problems = []
    if state.get("stop"):
        problems.append("an unfamiliar schema stopped the run")
    if not any("schema" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("unknown schema gives a warning", problems)


def check_complete_store():
    work = workspace({})
    digest = live_hash(work)
    shutil.rmtree(work, ignore_errors=True)
    work = workspace({"fig:one": entry(hash=digest)})
    state = resolve(work)
    altreq = os.path.join(work, "_alt", "doc.altreq")
    problems = []
    if state["requests"]:
        problems.append(f"{len(state['requests'])} request(s) for a complete store")
    if not os.path.isfile(altreq):
        problems.append("no request file was written")
    else:
        first = open(altreq, encoding="utf-8").read().splitlines()[0]
        if first.strip() != "0":
            problems.append(f"the request file opens with {first!r}, expected '0'")
    shutil.rmtree(work, ignore_errors=True)
    return report("complete store gives an empty request file", problems)


def check_request_file():
    work = workspace({})
    state = resolve(work)
    altreq = open(os.path.join(work, "_alt", "doc.altreq"), encoding="utf-8").read()
    problems = []
    if not altreq.startswith("1"):
        problems.append(f"the count is wrong: {altreq.splitlines()[0]!r}")
    if "fig:one" not in altreq:
        problems.append("the queued figure is not named in the request file")
    shutil.rmtree(work, ignore_errors=True)
    return report("request file lists queued figures", problems)


def check_blocked_figure():
    body = FIGURE.replace("example.png", "nowhere.png")
    work = workspace({}, body=body)
    state = resolve(work)
    got = state["resolved"][0]
    problems = []
    if got["outcome"] != "blocked":
        problems.append(f"outcome {got['outcome']!r}, expected 'blocked'")
    if state["requests"]:
        problems.append("a figure with no file was queued for the model anyway")
    if got["hash"] is not None:
        problems.append("a file that is not there was given a hash")
    shutil.rmtree(work, ignore_errors=True)
    return report("figure with no file is blocked", problems)


def check_locked_changed_figure():
    text = "The description a person wrote and signed off."
    work = workspace({"fig:one": entry(alt=text, status="approved",
                                       hash="sha256:longgone0000000")})
    state = resolve(work)
    got = state["resolved"][0]
    problems = []
    if got["outcome"] != "locked":
        problems.append(f"outcome {got['outcome']!r}, expected 'locked'")
    if got["alt"] != text:
        problems.append("the approved text was not carried forward")
    if not any("has changed since" in w for w in state["warnings"]):
        problems.append("the drift was not reported to the author")
    shutil.rmtree(work, ignore_errors=True)
    return report("approved text kept when figure changes", problems)


def check_status_without_text():
    work = workspace({"fig:one": entry(alt="")})
    got = resolve(work)["resolved"][0]
    shutil.rmtree(work, ignore_errors=True)
    return report("entry without text is queued",
                  [] if got["outcome"] == "describe" else
                  [f"outcome {got['outcome']!r}"])


def check_summary():
    got = resolver.summarise([{"outcome": "describe"}, {"outcome": "describe"},
                              {"outcome": "reuse"}, {"outcome": "blocked"},
                              {"outcome": "artifact"}])
    problems = []
    for wanted in ("5 figure(s)", "2 describe", "1 blocked", "1 reuse",
                   "1 artifact"):
        if wanted not in got:
            problems.append(f"the summary does not say {wanted!r}")
    return report("summary counts every outcome", problems,
                  note=got)


CHECKS = [
    check_stale_text,
    check_fresh_text,
    check_stub_requeued,
    check_model_entry_reused,
    check_non_object_entry,
    check_non_text_alt,
    check_null_alt_artifact,
    check_bad_entry_isolated,
    check_wrong_schema,
    check_complete_store,
    check_request_file,
    check_blocked_figure,
    check_locked_changed_figure,
    check_status_without_text,
    check_summary,
]


def run():
    return [check() for check in CHECKS]
