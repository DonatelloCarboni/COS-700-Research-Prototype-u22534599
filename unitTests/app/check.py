import builtins
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

import app
import pipeline
import preflight
import project
import renderer
import resolver
import scanner

FIXTURE = os.path.join(HERE, "fixture")

MODEL_TEXT = "Line chart of accuracy against epoch, rising from 60% to 94%."

EXPECTED = {
    "fig:fresh": "reuse",
    "fig:new": "describe",
    "fig:inline": "inline",
    "divider.png": "artifact",
    "fig:absent": "blocked",
    "fig:chapter": "describe",
    "fig:gone": "orphan",
}

BARE = r"""\documentclass{article}
\begin{document}
Hello.
\end{document}
"""


def workspace(store="default"):
    work = tempfile.mkdtemp()
    shutil.copytree(FIXTURE, work, dirs_exist_ok=True)
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    if store == "default":
        store = default_store(work)
    path = os.path.join(work, "main.alt")
    if isinstance(store, dict):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
            fh.write("\n")
    elif isinstance(store, str):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(store)
    return work, os.path.join(work, "main.tex")


def default_store(work, fresh_hash=None):
    live = scanner.hash_file(os.path.join(work, "figures", "fresh-chart.png"))
    return {
        "schema": "alt/1", "document": "main.tex", "updated": "2026-09-01T00:00Z",
        "entries": {
            "fig:fresh": {
                "alt": MODEL_TEXT, "profile": "chart",
                "source": "figures/fresh-chart.png", "hash": fresh_hash or live,
                "status": "generated", "by": "test-model", "at": "2026-09-01T00:00Z",
                "note": "a field added by hand",
            },
            "fig:gone": {
                "alt": "A figure that has since been deleted.", "profile": "chart",
                "source": "figures/gone.png", "hash": "sha256:0000000000000000",
                "status": "edited", "by": "u22534599", "at": "2026-09-01T00:00Z",
            },
        },
    }


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def rows(document):
    view = quiet(project.overview, document)
    return {r["key"]: r for r in view["rows"]}


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def entries(work):
    with open(os.path.join(work, "main.alt"), encoding="utf-8") as fh:
        return json.load(fh)["entries"]


def snapshot(folder):
    found = {}
    for base, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(base, name)
            found[os.path.relpath(path, folder)] = read_bytes(path)
    return found


def refused(fn, *args, **kwargs):
    try:
        quiet(fn, *args, **kwargs)
    except (ValueError, project.StoreError) as exc:
        return exc
    return None


def report(label, problems):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_open_creates_store():
    work, document = workspace(store=None)
    opened = project.open_document(document)
    problems = []
    if opened["error"]:
        problems.append(f"refused: {opened['error']}")
    if not opened["created"]:
        problems.append("did not say it created the .alt file")
    store = os.path.join(work, "main.alt")
    if not os.path.isfile(store):
        problems.append("there is no main.alt beside main.tex")
    else:
        with open(store, encoding="utf-8") as fh:
            data = json.load(fh)
        if (data.get("schema"), data.get("document"), data.get("entries")) != \
                ("alt/1", "main.tex", {}):
            problems.append(f"not an empty alt/1 store: {data}")
        outcomes = {k: r["outcome"] for k, r in rows(document).items()}
        if outcomes.get("fig:fresh") != "describe":
            problems.append(f"fig:fresh is {outcomes.get('fig:fresh')!r} against an "
                            f"empty store, not 'describe'")
    shutil.rmtree(work, ignore_errors=True)
    return report("an empty .alt file is created", problems)


def check_open_existing_store():
    work, document = workspace()
    store = os.path.join(work, "main.alt")
    before = read_bytes(store)
    opened = project.open_document(document)
    problems = []
    if opened["error"] or opened["created"]:
        problems.append(f"expected a plain read, got {opened}")
    if read_bytes(store) != before:
        problems.append("opening the document rewrote main.alt")
    if opened["store"] != store:
        problems.append(f"store is {opened['store']!r}, not the file beside main.tex")
    shutil.rmtree(work, ignore_errors=True)
    return report("an existing .alt file is read unchanged", problems)


def check_open_chapter():
    work, document = workspace(store=None)
    opened = project.open_document(os.path.join(work, "chapters", "method.tex"))
    problems = []
    if not opened["error"]:
        problems.append("a chapter was accepted as the document")
    elif "main" not in opened["error"]:
        problems.append(f"the refusal does not point at the main file: {opened['error']}")
    if os.path.exists(os.path.join(work, "chapters", "method.alt")):
        problems.append("a .alt file was created beside the chapter")
    if opened["created"] or opened["document"]:
        problems.append(f"refused, but still reported a document: {opened}")
    shutil.rmtree(work, ignore_errors=True)
    return report("a chapter is refused", problems)


def check_open_non_tex():
    work, document = workspace()
    problems = []
    for path in (os.path.join(work, "main.alt"),
                 os.path.join(work, "no-such-document.tex"), ""):
        if not project.open_document(path)["error"]:
            problems.append(f"accepted {path!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("a file that is not .tex is refused", problems)


def check_broken_store():
    work, document = workspace(store='{"entries": {oops')
    store = os.path.join(work, "main.alt")
    before = read_bytes(store)
    problems = []
    opened = project.open_document(document)
    if opened["error"] or opened["created"]:
        problems.append(f"opening should succeed and create nothing: {opened}")
    view = quiet(project.overview, document)
    if not view["error"]:
        problems.append("the overview did not report the broken file")
    if view["rows"]:
        problems.append("the overview guessed at figures from a store it could not read")
    for label, err in (
            ("save", refused(project.save_entry, document, "fig:new", "Text.",
                             "edited", figure={"hash": "sha256:1"})),
            ("remove", refused(project.remove_entry, document, "fig:new"))):
        if not isinstance(err, project.StoreError):
            problems.append(f"{label} did not refuse with StoreError: {err!r}")
    if read_bytes(store) != before:
        problems.append("the broken main.alt was overwritten")
    shutil.rmtree(work, ignore_errors=True)
    return report("a broken .alt file is reported and kept", problems)


def check_overview_outcomes():
    work, document = workspace()
    problems = []
    found = {k: r["outcome"] for k, r in rows(document).items()}
    if found != EXPECTED:
        problems.append(f"outcomes {found}, expected {EXPECTED}")

    other, other_doc = workspace()
    state = {"document": other_doc, "figures": []}
    quiet(scanner.scan, state)
    quiet(resolver.resolve, state)
    stage2 = {r["key"]: r["outcome"] for r in state["resolved"]}
    ours = {k: v for k, v in found.items() if v != "orphan"}
    if stage2 != ours:
        problems.append(f"stage 2 says {stage2}, the overview says {ours}")
    if not rows(document)["fig:chapter"]["where"].startswith("chapters/method.tex:"):
        problems.append("a chapter's figure does not say which file it is written in")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(other, ignore_errors=True)
    return report("overview matches stage 2", problems)


def check_overview_writes_nothing():
    work, document = workspace()
    before = snapshot(work)
    quiet(project.overview, document)
    after = snapshot(work)
    problems = []
    if set(after) != set(before):
        problems.append(f"files appeared or vanished: {sorted(set(after) ^ set(before))}")
    changed = [k for k in before if k in after and before[k] != after[k]]
    if changed:
        problems.append(f"files changed: {changed}")
    shutil.rmtree(work, ignore_errors=True)
    return report("overview writes no files", problems)


def check_save_new_text():
    work, document = workspace()
    row = rows(document)["fig:new"]
    result = quiet(project.save_entry, document, "fig:new",
                   "  Bar chart of\n\n class counts.  ", "edited", figure=row,
                   who="tester")
    entry = entries(work).get("fig:new") or {}
    problems = []
    want = {"alt": "Bar chart of class counts.", "status": "edited", "by": "tester",
            "hash": row["hash"], "source": "figures/new-chart.png",
            "profile": row["profile"]}
    for field, value in want.items():
        if entry.get(field) != value:
            problems.append(f"{field} is {entry.get(field)!r}, expected {value!r}")
    if not entry.get("at") or not result["wrote"]:
        problems.append(f"not stamped or not written: {result}")
    if rows(document)["fig:new"]["outcome"] != "locked":
        problems.append("the next run would not leave it alone")
    shutil.rmtree(work, ignore_errors=True)
    return report("new text is saved as edited", problems)


def check_changed_text_is_edited():
    work, document = workspace()
    row = rows(document)["fig:fresh"]
    result = quiet(project.save_entry, document, "fig:fresh",
                   "Accuracy climbs from 60% to 94% over 50 epochs.", "generated",
                   figure=row, who="tester")
    entry = entries(work)["fig:fresh"]
    problems = []
    if entry["status"] != "edited":
        problems.append(f"status is {entry['status']!r}; changed words stayed generated")
    if entry["by"] != "tester":
        problems.append(f"by is {entry['by']!r}; the words are the person's now")
    if "edited" not in result["note"]:
        problems.append(f"the person is not told why: {result['note']!r}")
    if rows(document)["fig:fresh"]["outcome"] != "locked":
        problems.append("the next run could still replace the person's words")
    shutil.rmtree(work, ignore_errors=True)
    return report("changed text is saved as edited", problems)


def check_approve():
    work, document = workspace()
    row = rows(document)["fig:fresh"]
    quiet(project.save_entry, document, "fig:fresh", MODEL_TEXT, "approved",
          figure=row, who="tester")
    entry = entries(work)["fig:fresh"]
    problems = []
    if entry["status"] != "approved":
        problems.append(f"status is {entry['status']!r}")
    if entry["alt"] != MODEL_TEXT:
        problems.append("approving changed the words")
    if entry["by"] != "test-model":
        problems.append(f"by is {entry['by']!r}; the model still wrote these words")
    if entry.get("note") != "a field added by hand":
        problems.append("a field added by hand was lost")
    if app.status_of(rows(document)["fig:fresh"])[0] != "approved":
        problems.append("the list does not show it as approved")
    shutil.rmtree(work, ignore_errors=True)
    return report("approving keeps the text, author and extra fields",
                  problems)


def check_recheck_changed_figure():
    work, _ = workspace(store=None)
    store = default_store(work, fresh_hash="sha256:1111111111111111")
    work, document = workspace(store=store)
    row = rows(document)["fig:fresh"]
    problems = []
    if row["outcome"] != "describe" or row.get("previous") != MODEL_TEXT:
        problems.append(f"setup: expected a changed figure with its old text, got "
                        f"{row['outcome']!r}")
    quiet(project.save_entry, document, "fig:fresh", row["previous"], "generated",
          figure=row, who="tester")
    entry = entries(work)["fig:fresh"]
    if entry["hash"] != row["hash"]:
        problems.append("the hash was not brought up to the figure as it is now")
    if entry["status"] != "edited":
        problems.append(f"status is {entry['status']!r}; a re-checked text stayed "
                        f"machine-owned")
    if rows(document)["fig:fresh"]["outcome"] != "locked":
        problems.append("the next run would still describe it again")
    shutil.rmtree(work, ignore_errors=True)
    return report("re-checking a changed figure updates its hash",
                  problems)


def check_save_unchanged():
    work, document = workspace()
    store = os.path.join(work, "main.alt")
    before = read_bytes(store)
    row = rows(document)["fig:fresh"]
    result = quiet(project.save_entry, document, "fig:fresh", MODEL_TEXT,
                   "generated", figure=row)
    problems = []
    if result["wrote"]:
        problems.append("reported a write when nothing changed")
    if read_bytes(store) != before:
        problems.append("main.alt changed (a new timestamp is still a diff)")
    shutil.rmtree(work, ignore_errors=True)
    return report("saving without changes writes nothing", problems)


def check_save_decorative():
    work, document = workspace()
    row = rows(document)["fig:new"]
    quiet(project.save_entry, document, "fig:new", "Some text typed first.",
          "artifact", figure=row, who="tester")
    entry = entries(work)["fig:new"]
    problems = []
    if entry["alt"] is not None or entry["status"] != "artifact":
        problems.append(f"expected alt null and status artifact, got {entry}")
    if rows(document)["fig:new"]["outcome"] != "artifact":
        problems.append("stage 2 does not treat it as decorative")
    shutil.rmtree(work, ignore_errors=True)
    return report("decorative save stores no text", problems)


def check_refused_saves():
    work, document = workspace()
    store = os.path.join(work, "main.alt")
    before = read_bytes(store)
    found = rows(document)
    problems = []
    cases = [
        ("empty text", "fig:new", "   ", "edited"),
        ("inline alt= in the source", "fig:inline", "Different text.", "edited"),
        ("a figure whose file is missing", "fig:absent", "A chart.", "edited"),
        ("un-decorating what the source marks decorative", "divider.png",
         "A thin rule.", "edited"),
        ("an unknown status", "fig:new", "Text.", "final"),
    ]
    for label, key, text, status in cases:
        err = refused(project.save_entry, document, key, text, status, figure=found[key])
        if not isinstance(err, ValueError):
            problems.append(f"{label}: not refused ({err!r})")
        elif len(str(err)) < 20:
            problems.append(f"{label}: refused without saying why ({err})")
    if read_bytes(store) != before:
        problems.append("a refused save still changed main.alt")
    shutil.rmtree(work, ignore_errors=True)
    return report("invalid saves are refused",
                  problems)


def check_save_keeps_other_entries():
    work, _ = workspace(store=None)
    store = default_store(work)
    store["comment"] = "a top-level field added by hand"
    store["entries"]["broken"] = "a half-written entry, just a string"
    work, document = workspace(store=store)
    row = rows(document)["fig:new"]
    quiet(project.save_entry, document, "fig:new", "Bar chart of class counts.",
          "edited", figure=row, who="tester")
    with open(os.path.join(work, "main.alt"), encoding="utf-8") as fh:
        data = json.load(fh)
    problems = []
    if data.get("comment") != store["comment"]:
        problems.append("a top-level field was lost")
    if data["entries"].get("broken") != store["entries"]["broken"]:
        problems.append("the malformed entry was deleted by an unrelated save")
    if data["entries"].get("fig:gone") != store["entries"]["fig:gone"]:
        problems.append("the orphaned entry was changed")
    order = list(data["entries"])
    if order != ["fig:fresh", "fig:gone", "broken", "fig:new"]:
        problems.append(f"order is {order}; existing entries should keep their place "
                        f"and the new one be appended")
    shutil.rmtree(work, ignore_errors=True)
    return report("other entries and fields are kept", problems)


def check_remove_entry():
    work, document = workspace()
    store = os.path.join(work, "main.alt")
    problems = []
    if not quiet(project.remove_entry, document, "fig:gone"):
        problems.append("did not report removing an entry that was there")
    if list(entries(work)) != ["fig:fresh"]:
        problems.append(f"entries left: {list(entries(work))}")
    before = read_bytes(store)
    if quiet(project.remove_entry, document, "fig:gone"):
        problems.append("reported removing an entry that was not there")
    if read_bytes(store) != before:
        problems.append("removing nothing still rewrote the file")
    if "fig:gone" in rows(document):
        problems.append("the removed orphan is still listed")
    shutil.rmtree(work, ignore_errors=True)
    return report("remove entry",
                  problems)


def run_preflight(answer):
    work = tempfile.mkdtemp()
    path = os.path.join(work, "bare.tex")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(BARE)
    asked = []

    def ask(question, details):
        asked.append((question, details))
        return answer

    def no_terminal(*_args):
        raise AssertionError("input() was called although ask was given")

    real_input = builtins.input
    builtins.input = no_terminal
    try:
        state = {"document": path, "ask": ask}
        quiet(preflight.preflight, state)
    finally:
        builtins.input = real_input
    return work, path, state, asked


def check_consent_no():
    problems = []
    for answer in (False, "yes"):
        work, path, state, asked = run_preflight(answer)
        if len(asked) != 1:
            problems.append(f"answer {answer!r}: asked {len(asked)} time(s), not once")
        elif "+ \\usepackage{graphicx}" not in asked[0][1]:
            problems.append(f"answer {answer!r}: the preview does not show the lines "
                            f"to be added")
        if not state.get("stop"):
            problems.append(f"answer {answer!r}: the run did not stop")
        if read_bytes(path) != BARE.encode():
            problems.append(f"answer {answer!r}: the document was changed")
        if any(n.endswith(".bak") for n in os.listdir(work)):
            problems.append(f"answer {answer!r}: a backup was made for an edit not done")
        shutil.rmtree(work, ignore_errors=True)
    return report("stage 0 callback: no",
                  problems)


def check_consent_yes():
    work, path, state, asked = run_preflight(True)
    problems = []
    if state.get("stop"):
        problems.append(f"the run stopped: {state.get('preflight')}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for needed in ("\\DocumentMetadata{tagging=on, pdfstandard=ua-2}",
                   "\\usepackage{graphicx}", "\\usepackage{tikz}",
                   "\\InputIfFileExists{_alt/bare.alt.tex}{}{}"):
        if needed not in text:
            problems.append(f"{needed} was not added")
    if not os.path.isfile(path + ".bak"):
        problems.append("no .bak of the original was kept")
    shutil.rmtree(work, ignore_errors=True)
    return report("stage 0 callback: yes",
                  problems)


def check_run_stage():
    problems = []
    state = {"document": "main.tex"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        summary = pipeline.run_stage(state, "scan", lambda s: "3 figure(s)")
    if summary != "3 figure(s)" or out.getvalue() != "[scan] 3 figure(s)\n":
        problems.append(f"a normal stage: summary {summary!r}, printed {out.getvalue()!r}")
    if state.get("stop"):
        problems.append("a stage that finished stopped the run")

    def explode(_state):
        raise RuntimeError("unexpected failure")

    crashed = {"document": "main.tex"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            summary = pipeline.run_stage(crashed, "render", explode)
    except Exception as exc:
        problems.append(f"the exception escaped: {exc!r}")
        return report("run_stage() output and crashes", problems)
    if summary is not None or not crashed.get("stop"):
        problems.append(f"a crash returned {summary!r} and stop={crashed.get('stop')}")
    if crashed.get("crashed") != {"stage": "render",
                                  "error": "RuntimeError: unexpected failure"}:
        problems.append(f"the crash was not recorded: {crashed.get('crashed')}")
    if "[render] stopped: unexpected RuntimeError" not in out.getvalue():
        problems.append(f"the crash was not reported: {out.getvalue()!r}")
    return report("run_stage() output and crashes",
                  problems)


def check_status_text():
    work, document = workspace()
    found = rows(document)
    problems = []
    want = {"fig:fresh": "model text", "fig:new": "needs alt text",
            "fig:inline": "alt= in source", "divider.png": "decorative",
            "fig:absent": "file missing", "fig:gone": "not in document"}
    for key, label in want.items():
        got = app.status_of(found[key])
        if got[0] != label or not got[1]:
            problems.append(f"{key}: {got}, expected {label!r} with an explanation")
    for outcome in ("describe", "reuse", "locked", "artifact", "inline", "blocked",
                    "orphan"):
        label, sentence = app.status_of({"outcome": outcome, "entry": {"status": "edited"}})
        if not sentence or label == outcome and outcome not in ("locked",):
            problems.append(f"outcome {outcome!r} has no words of its own: "
                            f"{(label, sentence)}")
    shutil.rmtree(work, ignore_errors=True)
    return report("every status has a label and explanation", problems)


def check_preview_candidates():
    work, document = workspace()
    found = rows(document)
    problems = []
    if app.preview_candidates(document, found["fig:new"]) != [found["fig:new"]["file"]]:
        problems.append("a PNG figure is not previewed from its own file")
    pdf = {"key": "fig:vector", "file": os.path.join(work, "figures", "plot.pdf"),
           "hash": "sha256:abcdabcdabcdabcd"}
    if app.preview_candidates(document, pdf):
        problems.append("a PDF figure offered a preview Tk cannot read")
    render = os.path.join(work, "_alt", "renders", renderer.render_name(pdf))
    os.makedirs(os.path.dirname(render))
    shutil.copyfile(found["fig:new"]["file"], render)
    if app.preview_candidates(document, pdf) != [render]:
        problems.append("stage 3's render is not used once it exists")
    shutil.rmtree(work, ignore_errors=True)
    return report("preview images",
                  problems)


def main():
    results = [
        check_open_creates_store(),
        check_open_existing_store(),
        check_open_chapter(),
        check_open_non_tex(),
        check_broken_store(),
        check_overview_outcomes(),
        check_overview_writes_nothing(),
        check_save_new_text(),
        check_changed_text_is_edited(),
        check_approve(),
        check_recheck_changed_figure(),
        check_save_unchanged(),
        check_save_decorative(),
        check_refused_saves(),
        check_save_keeps_other_entries(),
        check_remove_entry(),
        check_consent_no(),
        check_consent_yes(),
        check_run_stage(),
        check_status_text(),
        check_preview_candidates(),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
