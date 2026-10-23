import os
import sys

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

import scanner
sys.path.insert(0, HERE)
import extra

NOT_NONE = object()


def run(name):
    state = {"document": os.path.join(HERE, name)}
    scanner.scan(state)
    return state["figures"]


def expect(name, count, **first):
    figures = run(name)
    problems = []
    if len(figures) != count:
        problems.append(f"found {len(figures)} figure(s), expected {count}")
    elif first:
        actual = figures[0]
        for field, want in first.items():
            got = actual.get(field)
            ok = (got is not None) if want is NOT_NONE else (got == want)
            if not ok:
                problems.append(f"{field}={got!r}, expected {want!r}")

    print(f"{'FAIL' if problems else 'ok  '}  {name}")
    for p in problems:
        print(f"        {p}")
    return not problems


CASES = [
    ("labeled.tex",      dict(count=1, key="fig:labeled", kind="includegraphics", hash=NOT_NONE)),
    ("unlabeled.tex",    dict(count=1, key="example.png", hash=NOT_NONE)),
    ("tikz.tex",         dict(count=1, key="fig:tikz", kind="tikz", profile="diagram", hash=NOT_NONE)),
    ("artifact.tex",     dict(count=1, key="divider.png", profile="artifact", hash=NOT_NONE)),
    ("missing-file.tex", dict(count=1, key="fig:missing", hash=None)),
    ("not-a-figure.tex", dict(count=0)),
    ("inline-alt.tex",   dict(count=1, key="fig:inline-alt",
                              alt="A hand-written description, see [1].",
                              caption="The bold case.")),
]


def keys_of(name):
    return [f["key"] for f in run(name)]


def scan_multifile(name="main.tex"):
    state = {"document": os.path.join(HERE, "multifile", name)}
    scanner.scan(state)
    return state


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def multifile_checks():
    state = scan_multifile()
    figures = state["figures"]
    keys = [f["key"] for f in figures]
    files = state["tex_files"]
    results = []

    problems = []
    for wanted in ("main.tex", "chapters/method.tex",
                   "chapters/method-detail.tex", "chapters/results.tex"):
        if wanted not in files:
            problems.append(f"{wanted} was not walked; got {files}")
    results.append(report("\\input and \\include are followed",
                          problems, note=" -> ".join(files)))

    problems = []
    for wanted in ("fig:in-main", "fig:in-chapter", "fig:deep-tikz", "fig:in-results"):
        if wanted not in keys:
            problems.append(f"{wanted} was not found; got {keys}")
    results.append(report("figures in chapters are found", problems))

    where = {f["key"]: f["where"] for f in figures}
    problems = []
    for key, prefix in (("fig:in-main", "main.tex:"),
                        ("fig:in-chapter", "chapters/method.tex:"),
                        ("fig:deep-tikz", "chapters/method-detail.tex:"),
                        ("fig:in-results", "chapters/results.tex:")):
        if not (where.get(key) or "").startswith(prefix):
            problems.append(f"{key} is at {where.get(key)!r}, expected {prefix}<line>")
    results.append(report("each figure records its file", problems))

    chapter = next((f for f in figures if f["key"] == "fig:in-chapter"), None)
    problems = []
    if chapter is None:
        problems.append("the chapter figure was not found at all")
    elif chapter["hash"] is None:
        problems.append("its image was not resolved; \\graphicspath from the "
                        "main preamble did not reach a figure in a chapter")
    results.append(report("graphicspath applies to chapters",
                          problems))

    problems = []
    if len(keys) != len(set(keys)):
        problems.append(f"duplicate keys across files: {keys}")
    if not any(k.startswith("fig:in-main@") for k in keys):
        problems.append(f"the cross-file duplicate label was not made unique: {keys}")
    if not any("would both be keyed" in w for w in state["warnings"]):
        problems.append("the collision was not reported")
    results.append(report("duplicate labels across files are made unique", problems))

    problems = []
    if not any("chapters/missing" in w for w in state["warnings"]):
        problems.append(f"the missing \\input was not reported: {state['warnings']}")
    results.append(report("missing \\input is reported", problems))

    problems = []
    try:
        cyclic = scan_multifile("cyclic.tex")
        if not any("already in this project" in w for w in cyclic["warnings"]):
            problems.append(f"the cycle was not reported: {cyclic['warnings']}")
        if "fig:in-cycle" not in [f["key"] for f in cyclic["figures"]]:
            problems.append("the real figure in the ring was lost")
    except RecursionError:
        problems.append("a ring of \\input was not stopped")
    results.append(report("\\input cycle stops", problems))

    return results


def main():
    results = [expect(name, **kw) for name, kw in CASES]

    got = keys_of("two-panel.tex")
    ok = got == ["fig:left", "fig:right"]
    print(f"{'ok  ' if ok else 'FAIL'}  two-panel.tex")
    if not ok:
        print(f"        keys={got}, expected ['fig:left', 'fig:right']")
    results.append(ok)

    got = keys_of("duplicate-keys.tex")
    ok = len(got) == 2 and len(set(got)) == 2 and got[0] == "example.png"
    print(f"{'ok  ' if ok else 'FAIL'}  duplicate-keys.tex")
    if not ok:
        print(f"        keys={got}, expected two distinct keys")
    results.append(ok)

    config, config_warnings = scanner.load_config(os.path.join(HERE, "labeled.tex"))
    problems = []
    if config["path"] is None:
        problems.append(f"no alt.config.json found: {config_warnings}")
    else:
        for wanted in ("chart", "photo", "diagram", "artifact"):
            if wanted not in config["profiles"]:
                problems.append(f"config defines no {wanted!r} profile")
    print(f"{'FAIL' if problems else 'ok  '}  alt.config.json (found and complete)")
    for p in problems:
        print(f"        {p}")
    results.append(not problems)

    results.extend(multifile_checks())
    results.extend(extra.run())

    figures = run(os.path.join(PROJECT, "main.tex"))
    keys = {f["key"] for f in figures}
    want = {"fig:new-chart", "fig:approved-diagram", "divider.png", "fig:absent-chart",
            "fig:method-diagram", "fig:chapter-tikz", "fig:results-plot"}
    problems = []
    if len(figures) != 13:
        problems.append(f"found {len(figures)} figure(s), expected 13")
    if want - keys:
        problems.append(f"missing key(s): {want - keys}")
    print(f"{'FAIL' if problems else 'ok  '}  main.tex (9 states + 4 in chapters)")
    for p in problems:
        print(f"        {p}")
    results.append(not problems)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
