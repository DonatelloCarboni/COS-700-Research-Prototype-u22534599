import io
import shutil
import tempfile
import os
import sys
import contextlib

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
sys.path.insert(0, HERE)
import extra

CASES = {
    "missing.tex":      ("describe", True),
    "reuse.tex":        ("reuse",    False),
    "stale.tex":        ("describe", True),
    "locked.tex":       ("locked",   False),
    "locked-stale.tex": ("locked",   False),
    "inline.tex":       ("inline",   False),
    "blocked.tex":      ("blocked",  False),
    "artifact.tex":     ("artifact", False),
}


def run(name):
    state = {"document": os.path.join(HERE, name)}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
    return state


def check(name, want_outcome, want_queued):
    state = run(name)
    problems = []

    if len(state["resolved"]) != 1:
        problems.append(f"{len(state['resolved'])} figure(s) resolved, expected 1")
    else:
        got = state["resolved"][0]
        if got["outcome"] != want_outcome:
            problems.append(f"outcome {got['outcome']!r}, expected {want_outcome!r}")

    queued = len(state["requests"]) > 0
    if queued != want_queued:
        problems.append(f"{'queued' if queued else 'not queued'}, "
                        f"expected {'queued' if want_queued else 'not queued'}")

    print(f"{'FAIL' if problems else 'ok  '}  {name}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_text_kept():
    problems = []

    got = run("locked.tex")["resolved"][0]
    if got["alt"] != "The text a human approved.":
        problems.append(f"locked lost the approved text: {got['alt']!r}")

    got = run("inline.tex")["resolved"][0]
    if got["alt"] != "The inline text that must win.":
        problems.append(f"inline did not win: {got['alt']!r}")

    got = run("reuse.tex")["resolved"][0]
    if got["alt"] != "The stored description.":
        problems.append(f"reuse lost the stored text: {got['alt']!r}")

    print(f"{'FAIL' if problems else 'ok  '}  stored text is carried forward")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_warns(name, phrase, label):
    state = run(name)
    hit = any(phrase in w for w in state.get("warnings", []))
    print(f"{'ok  ' if hit else 'FAIL'}  {label}")
    if not hit:
        print(f"        no warning containing {phrase!r}; got {state.get('warnings')}")
    return hit


def check_malformed():
    state = run("malformed.tex")
    ok = state.get("stop") is True and not state.get("resolved")
    print(f"{'ok  ' if ok else 'FAIL'}  malformed.tex stops the run")
    if not ok:
        print(f"        stop={state.get('stop')}, resolved={state.get('resolved')}")
    return ok


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


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
    state = {"document": os.path.join(work, "main.tex")}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)

    by_key = {r["key"]: r for r in state["resolved"]}
    problems = []
    for key in ("fig:in-main", "fig:in-chapter", "fig:chapter-tikz"):
        if key not in by_key:
            problems.append(f"{key} was not resolved at all")
        elif by_key[key]["outcome"] != "describe":
            problems.append(f"{key}: outcome {by_key[key]['outcome']!r}, "
                            f"expected 'describe' (no store yet)")
    requests = {r["key"] for r in state["requests"]}
    if "fig:in-chapter" not in requests:
        problems.append(f"the chapter's figure was not queued: {requests}")
    altreq = os.path.join(work, "_alt", "main.altreq")
    if not os.path.isfile(altreq):
        problems.append("no request file was written")
    elif "fig:in-chapter" not in open(altreq, encoding="utf-8").read():
        problems.append("the chapter's figure is missing from _alt/main.altreq")
    return report("chapter figures are queued", problems)

def main():
    results = [check(name, *expect) for name, expect in CASES.items()]
    results.append(check_text_kept())
    results.append(check_warns("locked-stale.tex", "has changed since",
                               "locked-stale.tex warns the text may no longer fit"))
    results.append(check_warns("orphan.tex", "fig:renamed-away",
                               "orphan.tex reports the left-behind entry"))
    results.append(check_warns("inline.tex", "inline alt=",
                               "inline.tex notes the entry was overridden"))
    results.append(check_malformed())

    results.append(check_multifile())
    results.extend(extra.run())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
