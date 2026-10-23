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

import writer

PROJECT = project_dir()


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def workspace(store=None):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(HERE, "queued.tex"),
                    os.path.join(work, "queued.tex"))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    if store is not None:
        with open(os.path.join(work, "queued.alt"), "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
    return work


def proposal(key="fig:one", **over):
    base = {"key": key, "alt": "A line chart of accuracy against epoch.",
            "profile": "chart", "hash": "sha256:1111222233334444",
            "where": "queued.tex:8", "status": "generated",
            "by": "test-model", "at": "2026-08-18T00:00Z", "ok": True,
            "error": None}
    base.update(over)
    return base


def run_writer(work, proposals, figures=None):
    state = {"document": os.path.join(work, "queued.tex"),
             "described": proposals,
             "figures": figures or [{"key": "fig:one", "source": "example.png"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        summary = writer.write(state)
    return state, summary


def load(work):
    return json.load(open(os.path.join(work, "queued.alt"), encoding="utf-8"))


def check_stub_flagged():
    work = workspace()
    state, summary = run_writer(work, [proposal(
        by="stub", alt="[stub] a chart; no model was called.")])
    problems = []
    if state["written"]["added"] != ["fig:one"]:
        problems.append(f"the entry was not written: {state['written']}")
    if state["written"].get("stubbed") != ["fig:one"]:
        problems.append(f"it was not recorded as a stub: {state['written']}")
    if not any("placeholder" in w for w in state.get("warnings", [])):
        problems.append(f"no warning was raised: {state.get('warnings')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("placeholder text is flagged", problems)


def check_real_text_not_flagged():
    work = workspace()
    state, _ = run_writer(work, [proposal()])
    problems = []
    if state["written"].get("stubbed"):
        problems.append("a real description was reported as a placeholder")
    if any("placeholder" in w for w in state.get("warnings", [])):
        problems.append("a real description raised the placeholder warning")
    shutil.rmtree(work, ignore_errors=True)
    return report("real text is not flagged", problems)


def check_store_warnings_kept():
    store = {"schema": "alt/99", "document": "queued.tex", "entries": {
        "fig:one": {"alt": "Human text.", "profile": "chart",
                    "source": "example.png", "hash": "sha256:0",
                    "status": "approved", "by": "u22534599",
                    "at": "2026-01-01T00:00Z"}}}
    work = workspace(store)
    state, _ = run_writer(work, [proposal()])
    problems = []
    if state["written"]["refused"] != ["fig:one"]:
        problems.append("the locked entry was not refused")
    if not any("schema" in w for w in state.get("warnings", [])):
        problems.append(f"the schema warning was dropped: {state.get('warnings')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("store warnings kept when nothing is written",
                  problems)


def check_valid_json():
    work = workspace()
    nasty = ('Accuracy rose 50% & cost $3; see file_name #2 {braces} '
             '"quoted" \\backslash and a café, plus 中文')
    run_writer(work, [proposal(alt=nasty)])
    problems = []
    try:
        entry = load(work)["entries"]["fig:one"]
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        return report("store is valid JSON",
                      [f"could not read it back: {type(exc).__name__}: {exc}"])
    if entry["alt"] != nasty:
        problems.append(f"the text changed on the way through: {entry['alt']!r}")
    raw = open(os.path.join(work, "queued.alt"), encoding="utf-8").read()
    if "\\u" in raw and "café" not in raw:
        problems.append("non-ASCII was escaped in the JSON")
    shutil.rmtree(work, ignore_errors=True)
    return report("store is valid JSON",
                  problems)


def check_unknown_figure_written():
    work = workspace()
    state, _ = run_writer(work, [proposal(key="fig:ghost")], figures=[])
    problems = []
    if state["written"]["added"] != ["fig:ghost"]:
        problems.append(f"nothing was written: {state['written']}")
    else:
        entry = load(work)["entries"]["fig:ghost"]
        if "source" in entry and entry["source"] is not None:
            problems.append(f"a source was invented: {entry['source']!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("proposal without a scan entry is written",
                  problems)


def check_timestamps():
    work = workspace()
    run_writer(work, [proposal()])
    entry = load(work)["entries"]["fig:one"]
    store = load(work)
    problems = []
    if entry.get("by") != "test-model":
        problems.append(f"by={entry.get('by')!r}")
    if not entry.get("at"):
        problems.append("the entry has no timestamp")
    if not store.get("updated"):
        problems.append("the store has no updated timestamp")
    if store.get("schema") != "alt/1":
        problems.append(f"schema={store.get('schema')!r}")
    if store.get("document") != "queued.tex":
        problems.append(f"document={store.get('document')!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("timestamps and author are recorded", problems)


def check_write_twice():
    work = workspace()
    run_writer(work, [proposal()])
    first = load(work)
    run_writer(work, [proposal()])
    second = load(work)
    problems = []
    if first["entries"] != second["entries"]:
        problems.append("the entries changed between two identical writes")
    if list(first["entries"]) != list(second["entries"]):
        problems.append("the entry order changed")
    shutil.rmtree(work, ignore_errors=True)
    return report("writing twice is stable", problems)


def check_malformed_reasons():
    cases = {
        "no key": (proposal(key=None), "no key"),
        "empty alt": (proposal(alt="   "), "no alt text"),
        "alt is a number": (proposal(alt=42), "no alt text"),
        "already approved": (proposal(status="approved"), "status"),
        "no provenance": (proposal(by=""), "what produced it"),
        "no hash": (proposal(hash=None), "hash"),
    }
    problems = []
    for label, (bad, phrase) in cases.items():
        why = writer.validate(bad)
        if why is None:
            problems.append(f"{label}: was accepted")
        elif phrase not in why:
            problems.append(f"{label}: reason {why!r} does not mention {phrase!r}")
    if writer.validate(proposal()) is not None:
        problems.append("a good proposal was rejected")
    return report("malformed proposals give a reason", problems)


def check_locked_entry_unchanged():
    entry = {"alt": "Human text.", "profile": "photo", "source": "figures/x.jpg",
             "hash": "sha256:abcd", "status": "approved", "by": "u22534599",
             "at": "2026-01-01T00:00Z", "reviewed": "yes", "note": "keep"}
    work = workspace({"schema": "alt/1", "document": "queued.tex",
                      "entries": {"fig:one": dict(entry)}})
    run_writer(work, [proposal()])
    after = load(work)["entries"]["fig:one"]
    shutil.rmtree(work, ignore_errors=True)
    return report("refused entry is unchanged",
                  [] if after == entry else [f"it changed: {after}"])


def check_no_store_when_refused():
    work = workspace()
    state, summary = run_writer(work, [proposal(alt="")])
    problems = []
    if os.path.isfile(os.path.join(work, "queued.alt")):
        problems.append("a store was created for a run that wrote nothing")
    if state["written"]["rejected"] != ["fig:one"]:
        problems.append(f"the proposal was not rejected: {state['written']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("no store created when nothing is written", problems,
                  note=summary)


CHECKS = [
    check_stub_flagged,
    check_real_text_not_flagged,
    check_store_warnings_kept,
    check_valid_json,
    check_unknown_figure_written,
    check_timestamps,
    check_write_twice,
    check_malformed_reasons,
    check_locked_entry_unchanged,
    check_no_store_when_refused,
]


def run():
    return [check() for check in CHECKS]
