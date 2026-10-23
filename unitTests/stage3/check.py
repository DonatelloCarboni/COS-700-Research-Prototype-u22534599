import contextlib
import io
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

import renderer
import resolver
import scanner
sys.path.insert(0, HERE)
import extra


def run(name, work=None):
    if work is None:
        work = tempfile.mkdtemp()
        shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
        for f in os.listdir(HERE):
            if f.endswith((".tex", ".alt")):
                shutil.copyfile(os.path.join(HERE, f), os.path.join(work, f))

    state = {"document": os.path.join(work, name)}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
        resolver.resolve(state)
        renderer.render(state)
    return state, work


def check(name, want_how, want_count=1):
    state, _ = run(name)
    renders = state["renders"]
    problems = []

    if len(renders) != want_count:
        problems.append(f"{len(renders)} render(s), expected {want_count}")
    elif want_count:
        got = renders[0]
        if got["how"] != want_how:
            problems.append(f"how={got['how']!r}, expected {want_how!r}")
        if want_how != "failed":
            if not got["image"] or not os.path.isfile(got["image"]):
                problems.append(f"no image on disk at {got['image']!r}")
            elif os.path.getsize(got["image"]) == 0:
                problems.append("the image written is empty")
            elif not got["image"].endswith(".png"):
                problems.append(f"not a png: {got['image']!r}")

    print(f"{'FAIL' if problems else 'ok  '}  {name}")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_failure_is_reported():
    state, work = run("broken-tikz.tex")
    problems = []
    got = state["renders"][0] if state["renders"] else None

    if not got or got["how"] != "failed":
        problems.append(f"how={got['how'] if got else None!r}, expected 'failed'")
    elif not got["error"]:
        problems.append("failed without saying why")
    if not any("could not render" in w for w in state.get("warnings", [])):
        problems.append(f"no warning raised; got {state.get('warnings')}")

    fragment = os.path.join(work, "_alt", "fragments", "fig-broken.tex")
    if not os.path.isfile(fragment):
        problems.append("the wrapper was not kept for reading")

    print(f"{'FAIL' if problems else 'ok  '}  broken-tikz.tex is reported")
    for p in problems:
        print(f"        {p}")
    return not problems


def check_cache():
    state, work = run("raster.tex")
    first = state["renders"][0]["how"]
    state, _ = run("raster.tex", work=work)
    second = state["renders"][0]["how"]

    ok = first == "copied" and second == "cached"
    print(f"{'ok  ' if ok else 'FAIL'}  raster.tex is cached on the second run")
    if not ok:
        print(f"        first={first!r}, second={second!r}, expected 'copied' then 'cached'")
    return ok


def check_preamble_reuse():
    state, work = run("tikz.tex")
    got = state["renders"][0]
    fragment = os.path.join(work, "_alt", "fragments", "fig-tikz.tex")
    problems = []
    if got["how"] != "compiled":
        problems.append(f"how={got['how']!r} ({got['error']})")
    if os.path.isfile(fragment):
        text = open(fragment, encoding="utf-8").read()
        for needed in ("usetikzlibrary", "definecolor", "standalone"):
            if needed not in text:
                problems.append(f"the wrapper does not carry {needed}")
    else:
        problems.append("no wrapper was written")

    print(f"{'ok  ' if not problems else 'FAIL'}  tikz.tex reuses the document preamble")
    for p in problems:
        print(f"        {p}")
    return not problems


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
        renderer.render(state)

    by_key = {r["key"]: r for r in state["renders"]}
    problems = []
    if "fig:in-chapter" not in by_key:
        problems.append("the chapter's image was not rendered")
    elif not by_key["fig:in-chapter"]["image"]:
        problems.append(f"no image produced: {by_key['fig:in-chapter']['error']}")

    drawn = by_key.get("fig:chapter-tikz")
    if drawn is None:
        problems.append("the chapter's tikzpicture was not rendered")
    elif drawn["how"] != "compiled":
        problems.append(f"the chapter's tikzpicture came out as "
                        f"{drawn['how']!r} ({drawn['error']})")
    elif not os.path.isfile(drawn["image"] or ""):
        problems.append("the compiled picture produced no file on disk")
    return report("chapter figures render", problems)

def main():
    results = [
        check("raster.tex", "copied"),
        check("vector.tex", "rasterised"),
        check("tikz.tex", "compiled"),
        check("skipped.tex", None, want_count=0),
    ]
    results.append(check_failure_is_reported())
    results.append(check_cache())
    results.append(check_preamble_reuse())

    results.append(check_multifile())
    results.extend(extra.run_all())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
