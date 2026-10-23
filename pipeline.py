import argparse
import os
import sys
import traceback

import describer
import injector
import preflight
import renderer
import resolver
import scanner
import validator
import writer

STAGES = [
    ("preflight", preflight.preflight),
    ("scan",      scanner.scan),
    ("resolve",   resolver.resolve),
    ("render",    renderer.render),
    ("describe",  describer.describe),
    ("write",     writer.write),
    ("inject",    injector.inject),
    ("validate",  validator.validate),
]


CONTINUE, STOP, AUTO = "continue", "stop", "auto"


def ask_to_continue(next_stage, done, total):
    try:
        answer = input(f"  {done}/{total} done. [enter] {next_stage}   "
                       f"[a] run the rest   [q] stop  > ").strip().lower()
    except EOFError:
        print()
        print("  no input to read, so running the rest. "
              "Use --auto to skip these prompts.")
        return AUTO
    except KeyboardInterrupt:
        print()
        return STOP

    print()

    if answer in ("q", "quit", "n", "no"):
        return STOP
    if answer in ("a", "auto", "all"):
        return AUTO
    return CONTINUE


def run_stage(state, name, stage):
    try:
        summary = stage(state)
    except Exception as exc:
        print(f"[{name}] stopped: unexpected {type(exc).__name__} ({exc})")
        print(f"  earlier stages finished; "
              f"{'their' if name != 'preflight' else 'no'} output is on disk.")
        if state.get("verbose"):
            traceback.print_exc()
        else:
            print("  re-run with --verbose for the full traceback.")
        state["stop"] = True
        state["crashed"] = {"stage": name, "error": f"{type(exc).__name__}: {exc}"}
        return None
    print(f"[{name}] {summary}")
    return summary


def run(document, assume_yes=False, verbose=False, offline=False, pause=False):
    state = {"document": document, "assume_yes": assume_yes,
             "verbose": verbose, "figures": []}
    if offline:
        state["provider"] = "stub"
        print("[note] --offline: stage 4 writes placeholder text instead of "
              "calling the model.")

    total = len(STAGES)
    for index, (name, stage) in enumerate(STAGES):
        run_stage(state, name, stage)
        if state.get("stop"):
            return state

        if pause and index + 1 < total:
            answer = ask_to_continue(STAGES[index + 1][0], index + 1, total)
            if answer == STOP:
                left = total - index - 1
                print(f"  stopped after {name}; {left} stage(s) not run")
                state["stop"] = True
                state["stopped_by_user"] = name
                return state
            if answer == AUTO:
                pause = False

    return state


def main():
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Fill in the alt text for a LaTeX document.")
    parser.add_argument("--tex", required=True, metavar="FILE",
                        help="the main .tex file to work on")
    parser.add_argument("--yes", action="store_true",
                        help="apply stage 0's edits without asking")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show the warnings behind each stage's summary")
    parser.add_argument("--offline", action="store_true",
                        help="never call the model; stage 4 uses a stub instead")
    parser.add_argument("--auto", action="store_true",
                        help="run every stage straight through, without stopping "
                             "to ask after each one")
    args = parser.parse_args()

    if not os.path.isfile(args.tex):
        print(f"no such document: {args.tex}")
        return 1

    try:
        state = run(args.tex, assume_yes=args.yes, verbose=args.verbose,
                    offline=args.offline, pause=not args.auto)
    except UnicodeDecodeError:
        print(f"cannot read {args.tex} as UTF-8")
        return 1

    return 1 if state.get("stop") else 0


if __name__ == "__main__":
    sys.exit(main())
