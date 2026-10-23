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
import renderer
import resolver
import scanner
import writer
sys.path.insert(0, HERE)
import extra


def workspace(store=None):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    shutil.copyfile(os.path.join(HERE, "queued.tex"),
                    os.path.join(work, "queued.tex"))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
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


def store_with(status, alt="Text a human owns.", extra=None):
    entry = {"alt": alt, "profile": "chart", "source": "figures/example.png",
             "hash": "sha256:0000000000000000", "status": status,
             "by": "u22534599", "at": "2026-08-18T00:00Z"}
    if extra:
        entry.update(extra)
    return {"schema": "alt/1", "document": "queued.tex", "entries": {"fig:one": entry}}


def load(work):
    return json.load(open(os.path.join(work, "queued.alt"), encoding="utf-8"))


def report(label, problems):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_end_to_end():
    work = workspace()
    state = {"document": os.path.join(work, "queued.tex"), "provider": "stub"}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
        describer.describe(state)
        writer.write(state)

    problems = []
    path = os.path.join(work, "queued.alt")
    if not os.path.isfile(path):
        problems.append("no .alt store was created")
    else:
        entry = load(work)["entries"].get("fig:one")
        if not entry:
            problems.append("no entry written for fig:one")
        else:
            for field in ("alt", "profile", "source", "hash", "status", "by", "at"):
                if field not in entry:
                    problems.append(f"entry has no {field!r}")
            if entry.get("status") != "generated":
                problems.append(f"status={entry.get('status')!r}")
    return report("real run writes a complete entry", problems)


def check_locked():
    problems = []
    for status in ("approved", "edited", "artifact"):
        work = workspace(store_with(status))
        before = load(work)["entries"]["fig:one"]["alt"]
        state, _ = run_writer(work, [proposal()])
        after = load(work)["entries"]["fig:one"]["alt"]
        if after != before:
            problems.append(f"{status}: text was overwritten")
        if "fig:one" not in state["written"]["refused"]:
            problems.append(f"{status}: not reported as refused")
    return report("edited, approved and artifact entries are kept", problems)


def check_adds_and_refreshes():
    work = workspace()
    state, _ = run_writer(work, [proposal()])
    problems = []
    if state["written"]["added"] != ["fig:one"]:
        problems.append(f"not added: {state['written']}")

    state, _ = run_writer(work, [proposal(alt="A revised description.")])
    if state["written"]["refreshed"] != ["fig:one"]:
        problems.append(f"not refreshed: {state['written']}")
    if load(work)["entries"]["fig:one"]["alt"] != "A revised description.":
        problems.append("the refreshed text was not written")
    return report("adds new entries, refreshes generated ones", problems)


def check_malformed():
    cases = {
        "no alt": proposal(alt=""),
        "alt is not text": proposal(alt=["a", "list"]),
        "no hash": proposal(hash=None),
        "no provenance": proposal(by=None),
        "status is not generated": proposal(status="approved"),
    }
    problems = []
    for label, bad in cases.items():
        work = workspace()
        state, _ = run_writer(work, [bad])
        if state["written"]["rejected"] != ["fig:one"]:
            problems.append(f"{label}: was not rejected")
        if os.path.isfile(os.path.join(work, "queued.alt")):
            problems.append(f"{label}: wrote a store anyway")
    return report("malformed proposals are rejected", problems)


def check_nothing_to_write():
    work = workspace(store_with("generated"))
    path = os.path.join(work, "queued.alt")
    before = open(path, "rb").read()
    state, summary = run_writer(work, [])
    problems = []
    if open(path, "rb").read() != before:
        problems.append("the file was rewritten with nothing to write")
    if summary != "nothing to write":
        problems.append(f"summary was {summary!r}")
    return report("nothing to write leaves the file", problems)


def check_extra_fields_kept():
    store = store_with("generated", extra={"_note": "keep me", "reviewed_by": "a person"})
    store["entries"]["fig:gone"] = {
        "alt": "A figure no longer in the document.", "profile": "chart",
        "source": "x", "hash": "sha256:dead", "status": "approved",
        "by": "u22534599", "at": "2026-08-18T00:00Z"}
    work = workspace(store)
    run_writer(work, [proposal()])
    entries = load(work)["entries"]

    problems = []
    for field in ("_note", "reviewed_by"):
        if field not in entries["fig:one"]:
            problems.append(f"{field!r} was lost when the entry was rewritten")
    if "fig:gone" not in entries:
        problems.append("an orphaned entry was deleted")
    return report("extra fields and orphans are kept", problems)


def check_order_is_stable():
    store = store_with("generated")
    store["entries"] = {
        "aaa": dict(store["entries"]["fig:one"]),
        "fig:one": dict(store["entries"]["fig:one"]),
        "zzz": dict(store["entries"]["fig:one"]),
    }
    work = workspace(store)
    run_writer(work, [proposal(), proposal(key="fig:new")],
               figures=[{"key": "fig:one", "source": "example.png"},
                        {"key": "fig:new", "source": "example.png"}])
    order = list(load(work)["entries"])
    problems = []
    if order[:3] != ["aaa", "fig:one", "zzz"]:
        problems.append(f"existing entries moved: {order}")
    if order[-1] != "fig:new":
        problems.append(f"the new entry was not appended: {order}")
    return report("entry order is stable", problems)


def check_unreadable_store():
    work = workspace()
    with open(os.path.join(work, "queued.alt"), "w", encoding="utf-8") as fh:
        fh.write('{ "entries": { "fig:one": { "alt": "oops", } }\n')
    before = open(os.path.join(work, "queued.alt"), "rb").read()
    state, _ = run_writer(work, [proposal()])
    problems = []
    if not state.get("stop"):
        problems.append("did not stop on an unreadable store")
    if open(os.path.join(work, "queued.alt"), "rb").read() != before:
        problems.append("overwrote a store it could not read")
    return report("unreadable store stops the run", problems)


def check_no_temp_file():
    work = workspace()
    run_writer(work, [proposal()])
    leftovers = [f for f in os.listdir(work) if f.endswith(".tmp")]
    return report("no .tmp file left",
                  [f"found {leftovers}"] if leftovers else [])


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

    problems = []
    store = os.path.join(work, "main.alt")
    if not os.path.isfile(store):
        problems.append("no .alt store was written")
        return report("one store for all chapters", problems)

    entries = json.load(open(store, encoding="utf-8"))["entries"]
    for key in ("fig:in-main", "fig:in-chapter", "fig:chapter-tikz"):
        if key not in entries:
            problems.append(f"{key} is missing from the store")
    stray = [f for f in os.listdir(os.path.join(work, "chapters"))
             if f.endswith(".alt")]
    if stray:
        problems.append(f"a second store was written next to a chapter: {stray}")
    return report("one store for all chapters", problems)

def main():
    results = [
        check_end_to_end(),
        check_locked(),
        check_adds_and_refreshes(),
        check_malformed(),
        check_nothing_to_write(),
        check_extra_fields_kept(),
        check_order_is_stable(),
        check_unreadable_store(),
        check_no_temp_file(),
    ]
    results.append(check_multifile())
    results.extend(extra.run())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
