import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = ["stage0", "stage1", "stage2", "stage3", "stage4", "stage5", "stage6",
          "stage7", "pipeline", "app"]

WHAT = {
    "stage0": "preflight   check the document can be tagged",
    "stage1": "scan        find the figures",
    "stage2": "resolve     decide which figures need describing",
    "stage3": "render      figure to image",
    "stage4": "describe    image to alt text",
    "stage5": "write       alt text to the .alt store",
    "stage6": "inject      .alt to tagged PDF",
    "stage7": "validate    check the tagged PDF",
    "pipeline": "all stages, end to end",
    "app": "project.py and the window hooks",
}

TALLY = "passed"


def run(suite):
    folder = os.path.join(HERE, suite)
    if not os.path.isfile(os.path.join(folder, "check.py")):
        print(f"  no check.py in {suite}/")
        return False, 0, 0, False

    done = subprocess.run([sys.executable, "check.py"], cwd=folder,
                          capture_output=True, text=True)
    output = done.stdout or ""

    if output.lstrip().startswith("skipped:"):
        print(f"skip  {suite:<9} {WHAT.get(suite, '')}")
        print(f"      {output.strip().splitlines()[0]}")
        return True, 0, 0, True

    passed = total = 0
    for line in output.splitlines():
        if line.strip().endswith(TALLY) and "/" in line:
            counts = line.strip().split()[0]
            try:
                passed, total = (int(n) for n in counts.split("/"))
            except ValueError:
                pass

    ok = done.returncode == 0
    print(f"{'ok  ' if ok else 'FAIL'}  {suite:<9} {passed}/{total:<4} "
          f"{WHAT.get(suite, '')}")
    if not ok:
        for line in output.splitlines():
            if line.startswith("FAIL"):
                print(f"        {line}")
        if done.stderr.strip():
            print(f"        {done.stderr.strip().splitlines()[-1]}")
    return ok, passed, total, False


def main():
    wanted = sys.argv[1:] or SUITES
    unknown = [s for s in wanted if s not in SUITES]
    if unknown:
        print(f"no such suite: {', '.join(unknown)}")
        print(f"known: {', '.join(SUITES)}")
        return 2

    print(f"running {len(wanted)} suite(s) from {HERE}\n")
    results = [run(suite) for suite in wanted]

    passed = sum(r[1] for r in results)
    total = sum(r[2] for r in results)
    skipped = [w for w, r in zip(wanted, results) if r[3]]
    failed = [w for w, r in zip(wanted, results) if not r[0]]

    print(f"\n{passed}/{total} checks passed across "
          f"{len(wanted) - len(skipped)} suite(s)")
    if skipped:
        print(f"{len(skipped)} suite(s) skipped: {', '.join(skipped)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
