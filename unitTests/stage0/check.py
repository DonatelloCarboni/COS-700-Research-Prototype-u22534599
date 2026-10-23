import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import extra


def project_dir():
    folder = HERE
    while not os.path.isfile(os.path.join(folder, "pipeline.py")):
        parent = os.path.dirname(folder)
        if parent == folder:
            raise SystemExit(f"cannot find pipeline.py above {HERE}")
        folder = parent
    return folder


PIPELINE = os.path.join(project_dir(), "pipeline.py")

CASES = {
    "ok.tex":                 (0, False),
    "missing-all.tex":        (0, True),
    "wrong-metadata.tex":     (0, True),
    "missing-packages.tex":   (0, True),
    "broken.tex":             (1, False),
    "misplaced-metadata.tex": (1, False),
}


def check(name, want_exit, want_changed):
    original = os.path.join(HERE, name)
    before = open(original, encoding="utf-8").read()

    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, name)
        shutil.copyfile(original, copy)

        result = subprocess.run(
            [sys.executable, PIPELINE, "--tex", copy, "--yes", "--auto"],
            capture_output=True, text=True,
        )
        after = open(copy, encoding="utf-8").read()

    problems = []
    if result.returncode != want_exit:
        problems.append(f"exit {result.returncode}, expected {want_exit}")
    changed = after != before
    if changed != want_changed:
        problems.append(f"file {'changed' if changed else 'unchanged'}, "
                        f"expected {'changed' if want_changed else 'unchanged'}")

    print(f"{'FAIL' if problems else 'ok  '}  {name}")
    for p in problems:
        print(f"        {p}")
        print(f"        [{result.stdout.strip()}]")
    return not problems


def check_multifile():
    src = os.path.join(HERE, "multifile")
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(src, "figures"), os.path.join(work, "figures"))
    shutil.copytree(os.path.join(src, "chapters"), os.path.join(work, "chapters"))
    shutil.copyfile(os.path.join(src, "main.tex"), os.path.join(work, "main.tex"))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))

    chapter = os.path.join(work, "chapters", "method.tex")
    before = open(chapter, encoding="utf-8").read()

    subprocess.run([sys.executable, PIPELINE, "--tex",
                    os.path.join(work, "main.tex"), "--yes", "--offline",
                    "--auto"],
                   capture_output=True, text=True)

    main = open(os.path.join(work, "main.tex"), encoding="utf-8").read()
    problems = []
    for needed in (r"\DocumentMetadata", r"\usepackage{graphicx}",
                   r"\usepackage{tikz}", "InputIfFileExists"):
        if needed not in main:
            problems.append(f"{needed} was not added to the main file")
    if open(chapter, encoding="utf-8").read() != before:
        problems.append("the chapter was edited; only the main file should be")
    if os.path.exists(chapter + ".bak"):
        problems.append("a backup was written beside the chapter")

    print(f"{'FAIL' if problems else 'ok  '}  requirements go in the main file only")
    for p_ in problems:
        print(f"        {p_}")
    return not problems

def main():
    results = [check(name, *expect) for name, expect in CASES.items()]
    results.append(check_multifile())
    results.extend(extra.run())
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
