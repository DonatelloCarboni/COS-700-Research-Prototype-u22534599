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
sys.path.insert(0, HERE)
import extra

GOOD = "A line chart of accuracy against training epoch, ending at 94 percent."
DRAWN = "A rectangle with nothing else in it."


def workspace(name, entries):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(HERE, name), os.path.join(work, name))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    stem = os.path.splitext(name)[0]
    with open(os.path.join(work, stem + ".alt"), "w", encoding="utf-8") as fh:
        json.dump({"schema": "alt/1", "document": name, "entries": entries},
                  fh, indent=2)
    return work


def entry(alt, source="example.png", profile="chart"):
    return {"alt": alt, "profile": profile, "source": source, "hash": "sha256:0",
            "status": "approved", "by": "u22534599", "at": "2026-08-31T00:00Z"}


def run(name, entries):
    work = workspace(name, entries)
    state = {"document": os.path.join(work, name)}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        injector.inject(state)
        summary = validator.validate(state)
    return state, summary


def skipped(label, why):
    print(f"skip  {label}")
    print(f"        {why}")
    return True


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_clean_passes():
    state, summary = run("clean.tex", {"fig:clean": entry(GOOD)})
    v = state["validation"]
    problems = []
    if not v["ok"]:
        problems.append(f"a properly described document failed: {summary}")
    if v.get("good") != 1:
        problems.append(f"{v.get('good')} figures with real alt text, expected 1")
    if state.get("stop"):
        problems.append("stopped the run on a passing document")
    return report("described document passes", problems, note=summary)


def check_untouched_fails():
    state, summary = run("untouched.tex", {})
    v = state["validation"]
    problems = []
    if v["ok"]:
        problems.append("an undescribed document passed even though its alt "
                        "text is only a filename")
    if v.get("useless") != 1:
        problems.append(f"{v.get('useless')} filename fallbacks detected, expected 1")
    if not any("tells a reader nothing" in w for w in state.get("warnings", [])):
        problems.append("the fallback was not explained in the warnings")
    return report("undescribed document fails",
                  problems, note=summary)


def check_unreached_limitation():
    state, summary = run("unreached.tex",
                         {"fig:drawn": entry(DRAWN, source="(drawn inline)",
                                             profile="diagram")})
    v = state["validation"]
    problems = []
    if "fig:drawn" not in (v.get("unreached") or []):
        problems.append(f"fig:drawn not reported as unreached: {v.get('unreached')}")
    if not any("fig:drawn" in note for note in v.get("limits") or []):
        problems.append(f"not listed as a limitation: {v.get('limits')}")
    if v.get("blocking"):
        problems.append(f"counted as a failure rather than a limitation: "
                        f"{v.get('blocking')}")
    if not any("fig:drawn" in w for w in state.get("warnings", [])):
        problems.append("the gap does not appear in the run's warnings at all")
    return report("untaggable figure is a limitation", problems,
                  note=summary)


def check_no_pdf():
    state = {"document": "nowhere.tex", "injected": {"pdf": None}}
    with contextlib.redirect_stdout(io.StringIO()):
        summary = validator.validate(state)
    problems = []
    if state["validation"]["ok"]:
        problems.append("reported ok with no PDF at all")
    if "nothing to validate" not in summary:
        problems.append(f"unclear summary: {summary!r}")
    return report("missing PDF is reported", problems)


def check_unreadable_structure():
    work = tempfile.mkdtemp()
    fake = os.path.join(work, "empty.pdf")
    open(fake, "wb").write(b"%PDF-1.7\n% no structure tree here\n")
    state = {"document": os.path.join(work, "x.tex"),
             "figures": [{"key": "fig:a", "source": "example.png"}],
             "injected": {"pdf": fake}}
    with contextlib.redirect_stdout(io.StringIO()):
        summary = validator.validate(state)
    problems = []
    if state["validation"]["ok"]:
        problems.append("a PDF with no structure tree was reported as passing")
    if "no tagged figures" not in summary:
        problems.append(f"unclear summary: {summary!r}")
    return report("unreadable structure is reported", problems)


def check_no_figures_passes():
    work = tempfile.mkdtemp()
    fake = os.path.join(work, "empty.pdf")
    open(fake, "wb").write(b"%PDF-1.7\n% nothing here\n")
    state = {"document": os.path.join(work, "x.tex"), "figures": [],
             "injected": {"pdf": fake}}
    with contextlib.redirect_stdout(io.StringIO()):
        summary = validator.validate(state)
    problems = []
    if not state["validation"]["ok"]:
        problems.append(f"a figureless document failed: {summary}")
    if state.get("stop"):
        problems.append("stopped the run on a document with nothing to tag")
    return report("document with no figures passes", problems, note=summary)


def check_verapdf_is_found():
    found = validator.find_verapdf()
    if not found:
        return skipped("veraPDF found off PATH",
                       "veraPDF does not appear to be installed on this machine")
    problems = []
    if not os.path.isfile(found):
        problems.append(f"reported {found!r}, which is not a file")
    return report("veraPDF found off PATH", [], note=found)


def check_non_alt_verapdf_failures():
    saved = validator.run_verapdf
    fonts_only = {
        "compliant": False, "engine": "fake", "about_alt": [],
        "failed": [{"clause": "8.4.5.8", "test": "1", "checks": 53,
                    "description": "The Font dictionary shall define ToUnicode",
                    "about_alt": False}],
    }
    alt_failure = {
        "compliant": False, "engine": "fake",
        "failed": [{"clause": "8.4.4", "test": "1", "checks": 2,
                    "description": "A figure shall have an alternative description",
                    "about_alt": True}],
        "about_alt": [{"clause": "8.4.4", "test": "1", "checks": 2,
                       "description": "A figure shall have an alternative description",
                       "about_alt": True}],
    }
    problems = []
    try:
        validator.run_verapdf = lambda pdf: fonts_only
        state, summary = run("clean.tex", {"fig:clean": entry(GOOD)})
        if not state["validation"]["ok"]:
            problems.append(f"a font-only veraPDF failure failed the stage: {summary}")
        if "none about alt text" not in summary:
            problems.append(f"the summary does not say the failures are unrelated: "
                            f"{summary!r}")

        validator.run_verapdf = lambda pdf: alt_failure
        state, summary = run("clean.tex", {"fig:clean": entry(GOOD)})
        if state["validation"]["ok"]:
            problems.append("an alt-text veraPDF failure did not fail the stage")
    finally:
        validator.run_verapdf = saved
    return report("only alt-text veraPDF failures count", problems)


def check_filename_shape():
    problems = []
    for text in ("example.png", "figures/chart.pdf", "a-b_c.jpeg"):
        if not validator.is_filename(text):
            problems.append(f"{text!r} should be seen as a filename")
    for text in (GOOD, "Accuracy vs epoch.", "The figure is too unclear.", ""):
        if validator.is_filename(text):
            problems.append(f"{text!r} should not be seen as a filename")
    return report("filenames and descriptions are told apart", problems)


def check_verapdf_verdict():
    state, summary = run("clean.tex", {"fig:clean": entry(GOOD)})
    vera = state["validation"]["verapdf"]
    problems = []

    if vera is None:
        if "veraPDF not installed" not in summary:
            problems.append("veraPDF is absent but the summary does not say so")
        note = "not installed"
    else:
        if vera.get("about_alt") and state["validation"]["ok"]:
            problems.append("veraPDF reported an alt-text failure, but the stage "
                            "passed anyway")
        if "veraPDF" not in summary:
            problems.append(f"the verdict is not in the summary: {summary!r}")
        note = (f"compliant={vera['compliant']}, "
                f"{len(vera['failed'])} rule(s) failed, "
                f"{len(vera['about_alt'])} about alt text")
    return report("veraPDF verdict is reported", problems,
                  note=note)


def multifile_workspace():
    work = tempfile.mkdtemp()
    src = os.path.join(HERE, "multifile")
    shutil.copytree(os.path.join(src, "figures"), os.path.join(work, "figures"))
    shutil.copytree(os.path.join(src, "chapters"), os.path.join(work, "chapters"))
    shutil.copyfile(os.path.join(src, "main.tex"), os.path.join(work, "main.tex"))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    return work


def check_multifile():
    work = multifile_workspace()
    state = {"document": os.path.join(work, "main.tex"), "provider": "stub"}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        describer.describe(state)
        writer.write(state)
        injector.inject(state)
        summary = validator.validate(state)

    v = state["validation"]
    problems = []

    if v.get("figures", 0) < 2:
        problems.append(f"only {v.get('figures')} tagged figure(s) seen; the "
                        f"chapter's figure never reached the PDF")
    if v.get("good", 0) < 2:
        problems.append(f"only {v.get('good')} figure(s) carry real alt text; "
                        f"{v.get('useless', 0)} are filename fallbacks")
    if not v.get("ok"):
        problems.append(f"the run failed: {v.get('blocking')}")

    store = json.load(open(os.path.join(work, "main.alt"), encoding="utf-8"))
    for key in ("fig:in-chapter", "fig:chapter-tikz"):
        if key not in store["entries"]:
            problems.append(f"{key} never reached the store")
    return report("chapter figure is tagged and validated", problems,
                  note=summary)

def main():
    if not shutil.which("pdflatex"):
        print("skipped: pdflatex is not on PATH, and these checks read real PDFs")
        return 0
    results = [
        check_clean_passes(),
        check_untouched_fails(),
        check_unreached_limitation(),
        check_no_pdf(),
        check_unreadable_structure(),
        check_no_figures_passes(),
        check_verapdf_is_found(),
        check_non_alt_verapdf_failures(),
        check_filename_shape(),
        check_verapdf_verdict(),
    ]
    results.append(check_multifile())
    results.extend(extra.run_all())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
