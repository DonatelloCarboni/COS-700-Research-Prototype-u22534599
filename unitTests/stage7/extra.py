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

import describer
import injector
import renderer
import resolver
import scanner
import validator
import writer

PROJECT = project_dir()


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def workspace(name, entries):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(HERE, name), os.path.join(work, name))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    stem = os.path.splitext(name)[0]
    with open(os.path.join(work, stem + ".alt"), "w", encoding="utf-8") as fh:
        json.dump({"schema": "alt/1", "document": name, "entries": entries},
                  fh, indent=2)
    return work


def entry(alt, source="example.png", profile="chart"):
    return {"alt": alt, "profile": profile, "source": source, "hash": "sha256:0",
            "status": "approved", "by": "u22534599", "at": "2026-09-01T00:00Z"}


def run(name, entries, provider=None):
    work = workspace(name, entries)
    state = {"document": os.path.join(work, name)}
    if provider:
        state["provider"] = provider
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        if provider:
            describer.describe(state)
            writer.write(state)
        injector.inject(state)
        summary = validator.validate(state)
    return state, summary, work


def check_whitespace_only_difference():
    stored = "Accuracy over epochs 1--50 rises,\n\nwith  doubled   spacing."
    state, summary, work = run("clean.tex", {"fig:clean": entry(stored)})
    v = state["validation"]
    problems = []
    if v.get("unreached"):
        problems.append(f"reported as never reaching the PDF: {v['unreached']}")
    if not v["ok"]:
        problems.append(f"a correctly tagged figure failed: {v.get('blocking')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("whitespace difference is not a failure",
                  problems, note=summary)


def check_different_text_caught():
    problems = []
    if validator.same_text("a  b") != validator.same_text("a b"):
        problems.append("whitespace still separates two identical texts")
    if validator.same_text("A chart.") == validator.same_text("A photograph."):
        problems.append("two different sentences compare as equal")
    if validator.same_text(None) != "":
        problems.append("None does not normalise to the empty string")
    return report("different text is still caught",
                  problems)


def check_empty_alt():
    work = tempfile.mkdtemp()
    fake = os.path.join(work, "empty-alt.pdf")
    open(fake, "wb").write(
        b"%PDF-1.7\n"
        b"1 0 obj\n<< /S /Figure /Alt <> >>\nendobj\n"
        b"2 0 obj\n<< /S /Figure /Alt <0041004200430044> >>\nendobj\n")
    figures = validator.read_figures(fake)
    problems = []
    if len(figures) != 2:
        problems.append(f"read {len(figures)} Figure element(s), expected 2")
    else:
        blank = figures[0]
        if blank["tagged"]:
            problems.append("an empty /Alt was counted as tagged")
        if not blank.get("empty"):
            problems.append("an empty /Alt was not identified as empty")
        if not figures[1]["tagged"]:
            problems.append("a real /Alt was not counted as tagged")

    state = {"document": os.path.join(work, "x.tex"),
             "figures": [{"key": "fig:a", "source": "example.png"}],
             "injected": {"pdf": fake}}
    saved = validator.run_verapdf
    validator.run_verapdf = lambda pdf: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            summary = validator.validate(state)
    finally:
        validator.run_verapdf = saved
    if state["validation"]["ok"]:
        problems.append("a document with an empty /Alt passed")
    if not any("empty" in w for w in state["validation"].get("blocking") or []):
        problems.append(f"the reason was not explained: "
                        f"{state['validation'].get('blocking')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("empty /Alt fails", problems)


def check_placeholder_reported():
    state, summary, work = run("clean.tex", {}, provider="stub")
    v = state["validation"]
    problems = []
    if not any("placeholder" in note for note in v.get("limits") or []):
        problems.append(f"placeholder text was not reported: {v.get('limits')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("placeholder text is reported",
                  problems, note=summary)


def check_placeholder_detection():
    problems = []
    if validator.is_placeholder("A line chart of accuracy against epoch."):
        problems.append("a real description was called a placeholder")
    if not validator.is_placeholder("[stub] a chart; no model was called."):
        problems.append("the stub's own text was not recognised")
    if validator.is_placeholder(""):
        problems.append("the empty string was called a placeholder")
    return report("only stub text counts as a placeholder",
                  problems)


def check_described_this_run():
    state, summary, work = run("unreached.tex", {}, provider="stub")
    v = state["validation"]
    problems = []
    if "fig:drawn" not in (v.get("unreached") or []):
        problems.append(f"a figure described on this run was not checked for "
                        f"arrival: unreached={v.get('unreached')}")
    if not any("fig:drawn" in note for note in v.get("limits") or []):
        problems.append(f"the gap was not reported as a limitation: "
                        f"{v.get('limits')}")
    if v.get("blocking"):
        problems.append(f"it was counted as a failure: {v.get('blocking')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("figure described this run is checked",
                  problems, note=summary)


def check_failed_description():
    state = {
        "document": "x.tex",
        "resolved": [{"key": "fig:a", "outcome": "describe", "alt": None,
                      "kind": "includegraphics", "source": "a.png"}],
        "described": [{"key": "fig:a", "ok": False, "alt": None}],
    }
    missed = validator.missed_figures(state, [])
    return report("undescribed figure is not unreached",
                  [f"got {missed}"] if missed else [])


def check_blocked_limitation():
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    body = open(os.path.join(HERE, "clean.tex"), encoding="utf-8").read()
    body = body.replace("example.png", "not-generated-yet.pdf")
    open(os.path.join(work, "absent.tex"), "w", encoding="utf-8").write(body)
    state = {"document": os.path.join(work, "absent.tex")}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        injector.inject(state)
        summary = validator.validate(state)
    v = state["validation"]
    problems = []
    if v.get("blocking"):
        problems.append(f"a missing figure file failed the run: "
                        f"{v['blocking']}")
    if not any("not on disk" in note for note in v.get("limits") or []):
        problems.append(f"it was not reported as a limitation: {v.get('limits')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing figure file is a limitation",
                  problems, note=summary)


def check_verapdf_crash():
    class Fake:
        stdout = "Exception in thread \"main\" java.lang.OutOfMemoryError"
        stderr = ""

    import subprocess
    saved_run, saved_find = subprocess.run, validator.find_verapdf
    subprocess.run = lambda *a, **k: Fake()
    validator.find_verapdf = lambda: "fake-verapdf"
    try:
        vera = validator.run_verapdf("x.pdf")
    finally:
        subprocess.run, validator.find_verapdf = saved_run, saved_find

    problems = []
    if not vera.get("unreadable"):
        problems.append("a crashed run was not marked unreadable")

    saved = validator.run_verapdf
    validator.run_verapdf = lambda pdf: vera
    try:
        state, summary, work = run("clean.tex", {"fig:clean": entry("A chart.")})
    finally:
        validator.run_verapdf = saved
    if "could not be read" not in summary:
        problems.append(f"the summary does not say the check did not run: "
                        f"{summary!r}")
    if "none about alt text" in summary:
        problems.append("a crashed check was reported as finding nothing wrong")
    shutil.rmtree(work, ignore_errors=True)
    return report("veraPDF crash is reported",
                  problems, note=summary)


def check_verapdf_absent():
    saved = validator.run_verapdf
    validator.run_verapdf = lambda pdf: None
    try:
        state, summary, work = run("clean.tex", {"fig:clean": entry("A chart.")})
    finally:
        validator.run_verapdf = saved
    problems = []
    if "not installed" not in summary:
        problems.append(f"the summary does not say veraPDF is missing: {summary!r}")
    if not state["validation"]["ok"]:
        problems.append("the run failed merely because veraPDF is not installed")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing veraPDF is reported",
                  problems)


def check_shared_filename_rule():
    problems = []
    cases = ["example.png", "figures/chart.pdf", "a-b_c.jpeg",
             "A line chart of accuracy.", "", "   ",
             "The figure is too unclear to describe.", "x" * 200]
    for text in cases:
        if injector.looks_like_filename(text) != validator.is_filename(text):
            problems.append(f"the two stages disagree about {text[:40]!r}")
    source = open(os.path.join(PROJECT, "validator.py"), encoding="utf-8").read()
    if "FILENAME_SHAPE = re.compile" in source:
        problems.append("validator.py still carries its own copy of the rule")
    return report("stages 6 and 7 share the filename rule", problems)


def check_verdict_counts_limitations():
    line = validator.summarise(
        figures=[{"tagged": True}], good=[{"tagged": True}], blocking=[],
        limits=["something the pipeline cannot fix"], vera=None, verbose=False)
    problems = []
    if not line.startswith("PASS"):
        problems.append(f"expected a pass: {line!r}")
    if "1 limitation(s)" not in line:
        problems.append(f"the limitation is not counted in the line: {line!r}")
    return report("PASS line counts limitations", problems,
                  note=line)


def check_fail_stops_run():
    state, summary, work = run("untouched.tex", {})
    problems = []
    if not state.get("stop"):
        problems.append("a failing document did not stop the run")
    if not summary.startswith("FAIL"):
        problems.append(f"the verdict does not read as a failure: {summary!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("failing verdict stops the run", problems)


CHECKS = [
    check_whitespace_only_difference,
    check_different_text_caught,
    check_empty_alt,
    check_placeholder_reported,
    check_placeholder_detection,
    check_described_this_run,
    check_failed_description,
    check_blocked_limitation,
    check_verapdf_crash,
    check_verapdf_absent,
    check_shared_filename_rule,
    check_verdict_counts_limitations,
    check_fail_stops_run,
]


def run_all():
    return [check() for check in CHECKS]
