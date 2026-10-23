import contextlib
import io
import os
import shutil
import subprocess
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
sys.path.insert(0, HERE)

import pipeline
import check as suite

PIPELINE = os.path.join(PROJECT, "pipeline.py")

PROMPT = "[a] run the rest"

STAGE_NAMES = [name for name, _ in pipeline.STAGES]


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def cli(work, *flags, keys=None, name="main.tex"):
    command = [sys.executable, PIPELINE, "--tex", os.path.join(work, name),
               "--yes", "--offline", *flags]
    if keys is None:
        return subprocess.run(command, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=600)
    return subprocess.run(command, input=keys, capture_output=True, text=True,
                          timeout=600)


STEPPED = {}
KEEP = []


def stepped_run():
    if not STEPPED:
        work = suite.build({"main.tex": suite.BARE})
        KEEP.append(work)
        STEPPED["work"] = work
        STEPPED["done"] = cli(work, keys="\n" * 12)
    return STEPPED["done"], STEPPED["work"]


def cleanup():
    while KEEP:
        shutil.rmtree(KEEP.pop(), ignore_errors=True)


def stages_in(output):
    return [name for name in STAGE_NAMES if f"[{name}]" in output]


def check_auto():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, "--auto")
    problems = []
    if PROMPT in done.stdout:
        problems.append("--auto stopped to ask anyway")
    ran = stages_in(done.stdout)
    if ran != STAGE_NAMES:
        problems.append(f"only {len(ran)} of {len(STAGE_NAMES)} stages ran: {ran}")
    if done.returncode != 0:
        problems.append(f"exited {done.returncode}: {done.stdout.strip()[-160:]}")
    shutil.rmtree(work, ignore_errors=True)
    return report("--auto runs all stages", problems)


def check_pauses_by_default():
    done, work = stepped_run()
    problems = []
    asked = done.stdout.count(PROMPT)
    if asked == 0:
        problems.append("the run never stopped to ask")
    ran = stages_in(done.stdout)
    if ran != STAGE_NAMES:
        problems.append(f"only {len(ran)} of {len(STAGE_NAMES)} stages ran: {ran}")
    if done.returncode != 0:
        problems.append(f"exited {done.returncode}")
    return report("pauses after each stage by default", problems,
                  note=f"asked {asked} time(s)")


def check_prompt_count():
    done, work = stepped_run()
    asked = done.stdout.count(PROMPT)
    expected = len(STAGE_NAMES) - 1
    problems = []
    if asked != expected:
        problems.append(f"asked {asked} time(s) for {len(STAGE_NAMES)} stages, "
                        f"expected {expected}")
    verdict = done.stdout.rfind("[validate]")
    if verdict < done.stdout.rfind(PROMPT):
        problems.append("the last thing the run did was ask a question")
    tail = done.stdout.rstrip().splitlines()[-1] if done.stdout.strip() else ""
    if PROMPT in tail:
        problems.append(f"a question and a stage summary share a line: {tail!r}")
    return report("one prompt between stages",
                  problems, note=f"{asked} question(s), 8 stages")


def check_q_stops_the_run():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, keys="q\n")
    ran = stages_in(done.stdout)
    problems = []
    if ran != ["preflight"]:
        problems.append(f"stages ran after the stop: {ran}")
    if "stage(s) not run" not in done.stdout:
        problems.append("it did not say what was left undone")
    if done.returncode != 1:
        problems.append(f"exited {done.returncode}, expected 1 since the run "
                        f"did not finish")
    if os.path.isfile(os.path.join(work, "main.alt")):
        problems.append("a store was written by a run stopped at stage 0")
    shutil.rmtree(work, ignore_errors=True)
    return report("q stops the run",
                  problems, note=f"ran: {', '.join(ran)}")


def check_q_partway():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, keys="\n\nq\n")
    ran = stages_in(done.stdout)
    problems = []
    if ran != ["preflight", "scan", "resolve"]:
        problems.append(f"expected to stop after resolve, ran: {ran}")
    if "5 stage(s) not run" not in done.stdout:
        problems.append(f"the count of skipped stages is wrong: "
                        f"{[l for l in done.stdout.splitlines() if 'not run' in l]}")
    if not os.path.isfile(os.path.join(work, "_alt", "main.altreq")):
        problems.append("the stage that did run left nothing behind")
    shutil.rmtree(work, ignore_errors=True)
    return report("q partway through",
                  problems, note=f"ran: {', '.join(ran)}")


def check_a_runs_rest():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, keys="a\n")
    problems = []
    asked = done.stdout.count(PROMPT)
    if asked != 1:
        problems.append(f"asked {asked} time(s) after 'a', expected 1")
    ran = stages_in(done.stdout)
    if ran != STAGE_NAMES:
        problems.append(f"only {len(ran)} of {len(STAGE_NAMES)} stages ran: {ran}")
    if done.returncode != 0:
        problems.append(f"exited {done.returncode}")
    shutil.rmtree(work, ignore_errors=True)
    return report("a runs the rest", problems)


def check_enter_continues():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, keys="\ny\nyes\n \nk\n\n\n\n")
    ran = stages_in(done.stdout)
    problems = []
    if ran != STAGE_NAMES:
        problems.append(f"an unrecognised answer did not continue: {ran}")
    shutil.rmtree(work, ignore_errors=True)
    return report("other answers continue",
                  problems)


def check_no_stdin():
    work = suite.build({"main.tex": suite.BARE})
    done = cli(work, keys=None)
    ran = stages_in(done.stdout)
    problems = []
    if ran != STAGE_NAMES:
        problems.append(f"a run with no stdin stalled after {len(ran)} stage(s)")
    if "no input to read" not in done.stdout:
        problems.append("it carried on without saying so")
    if done.stdout.count(PROMPT) > 1:
        problems.append("it kept asking after stdin was closed")
    if done.returncode != 0:
        problems.append(f"exited {done.returncode}")
    shutil.rmtree(work, ignore_errors=True)
    return report("no stdin runs the rest", problems)


def check_no_prompt_after_stop():
    broken = suite.BARE.replace(r"\end{document}",
                                "\\textbf{this brace is never closed\n"
                                "\\end{document}")
    work = suite.build({"main.tex": broken})
    done = cli(work, keys="\n" * 12)
    problems = []
    if PROMPT in done.stdout:
        problems.append("it asked whether to continue a run that had stopped")
    if "does not compile" not in done.stdout:
        problems.append("the run did not stop at stage 0 as expected")
    if done.returncode != 1:
        problems.append(f"exited {done.returncode}, expected 1")
    shutil.rmtree(work, ignore_errors=True)
    return report("no prompt after a stop",
                  problems)


def check_prompt_names_next():
    done, work = stepped_run()
    problems = []
    for name in STAGE_NAMES[1:]:
        if f"] {name}   " not in done.stdout:
            problems.append(f"no question offered to continue to {name!r}")
    if "1/8 done" not in done.stdout:
        problems.append("the question does not say how far through the run is")
    return report("prompt names the next stage", problems)


def check_run_does_not_pause():
    problems = []
    work = suite.build({"main.tex": suite.BARE})

    asked = {"n": 0}
    real = pipeline.ask_to_continue

    def counting(next_stage, done, total):
        asked["n"] += 1
        return pipeline.CONTINUE

    pipeline.ask_to_continue = counting
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run(os.path.join(work, "main.tex"), assume_yes=True,
                         offline=True)
    finally:
        pipeline.ask_to_continue = real

    if asked["n"]:
        problems.append(f"run() asked {asked['n']} time(s) with no pause "
                        f"requested; every suite calling it would hang")

    asked["n"] = 0
    pipeline.ask_to_continue = counting
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run(os.path.join(work, "main.tex"), assume_yes=True,
                         offline=True, pause=True)
    finally:
        pipeline.ask_to_continue = real
    if asked["n"] != len(STAGE_NAMES) - 1:
        problems.append(f"pause=True asked {asked['n']} time(s), expected "
                        f"{len(STAGE_NAMES) - 1}")
    shutil.rmtree(work, ignore_errors=True)
    return report("run() pauses only when asked", problems)


def check_auto_in_help():
    done = subprocess.run([sys.executable, PIPELINE, "--help"],
                          capture_output=True, text=True)
    problems = []
    if "--auto" not in done.stdout:
        problems.append("--auto is missing from --help")
    return report("--auto in --help", problems)


CHECKS = [
    check_auto,
    check_pauses_by_default,
    check_prompt_count,
    check_q_stops_the_run,
    check_q_partway,
    check_a_runs_rest,
    check_enter_continues,
    check_no_stdin,
    check_no_prompt_after_stop,
    check_prompt_names_next,
    check_run_does_not_pause,
    check_auto_in_help,
]


def run_all():
    try:
        return [check() for check in CHECKS]
    finally:
        cleanup()
