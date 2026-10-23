import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")


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

import describer
import renderer
import resolver
import scanner
import writer

CALLS = {"n": 0}
WAITS_S = (5, 15)
PAUSE_S = 1

BANNED_OPENINGS = ("image of", "figure showing", "a picture of", "this image",
                   "the image", "picture of")


class RateLimited(Exception):
    pass


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def skipped(label, why):
    print(f"skip  {label}")
    print(f"        {why}")
    return None


def ask(image, prompt, model=None, key=None):
    context = {"model": model or MODEL, "key": key or KEY}
    for attempt in range(len(WAITS_S) + 1):
        CALLS["n"] += 1
        try:
            time.sleep(PAUSE_S)
            return describer.call_gemini(os.path.join(LIVE, image), prompt, context)
        except describer.Transient as exc:
            if "quota" in str(exc).lower() or "429" in str(exc):
                raise RateLimited(str(exc))
            if attempt == len(WAITS_S):
                raise
            time.sleep(WAITS_S[attempt])


def prompt_for(profile, ask_for, caption=None, max_chars=250):
    return describer.PROMPT.format(
        profile=profile, ask_for=ask_for, max_chars=max_chars,
        caption=f"\nThe figure's caption, for context: {caption}" if caption else "")


def check_full_chain():
    work = tempfile.mkdtemp()
    shutil.copyfile(os.path.join(LIVE, "paper.tex"), os.path.join(work, "paper.tex"))
    shutil.copyfile(os.path.join(LIVE, "accuracy-chart.png"),
                    os.path.join(work, "accuracy-chart.png"))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))

    state = {"document": os.path.join(work, "paper.tex")}
    CALLS["n"] += 1
    previous = os.environ.get(describer.ENV_NAME)
    os.environ[describer.ENV_NAME] = KEY
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
            describer.describe(state)
            writer.write(state)
    finally:
        if previous is None:
            os.environ.pop(describer.ENV_NAME, None)
        else:
            os.environ[describer.ENV_NAME] = previous

    problems = []
    described = state.get("described") or []
    if len(described) != 1:
        problems.append(f"{len(described)} described, expected 1")
        return report("full chain with the real model", problems), None

    got = described[0]
    if not got["ok"]:
        error = str(got["error"])
        if "quota" in error.lower() or "429" in error:
            raise RateLimited(error)
        problems.append(f"the call failed: {error}")
        return report("full chain with the real model", problems), None

    alt = got["alt"]
    store = os.path.join(work, "paper.alt")
    if not os.path.isfile(store):
        problems.append("stage 5 wrote no .alt store")
    else:
        entry = json.load(open(store, encoding="utf-8"))["entries"].get("fig:accuracy")
        if not entry:
            problems.append("no entry for fig:accuracy")
        else:
            if entry["alt"] != alt:
                problems.append("the stored text is not what the model returned")
            if entry["status"] != "generated":
                problems.append(f"status={entry['status']!r}, expected 'generated'")
            if entry["by"] != MODEL:
                problems.append(f"by={entry['by']!r}, expected {MODEL!r}")
    return report("full chain with the real model", problems), alt


def check_chart_facts(alt):
    if alt is None:
        return skipped("description includes the chart's facts",
                       "the chain produced no description to check")
    low = alt.lower()
    problems = []
    for needed, why in (("accuracy", "the y axis label"),
                        ("epoch", "the x axis label")):
        if needed not in low:
            problems.append(f"does not mention {needed!r} ({why})")
    if not any(ch.isdigit() for ch in alt):
        problems.append("contains no numbers")
    return report("description includes the chart's facts", problems,
                  note=alt)


def check_profile_ask(alt):
    if alt is None:
        return skipped("chart profile ask reaches the output",
                       "the chain produced no description to check")
    low = alt.lower()
    problems = []
    if not any(w in low for w in ("cross", "overtake", "above", "surpass")):
        problems.append("does not mention the crossing the two curves make")
    if not any(w in low for w in ("plateau", "flat", "level", "steady", "stabil")):
        problems.append("does not mention the plateau")
    return report("chart profile ask reaches the output", problems)


def check_prompt_rules(alt):
    if alt is None:
        return skipped("model follows the prompt rules",
                       "the chain produced no description to check")
    problems = []
    low = alt.lower().lstrip("\"' ")
    for banned in BANNED_OPENINGS:
        if low.startswith(banned):
            problems.append(f"opens with the banned {banned!r}")
    if alt != alt.strip() or "\n" in alt:
        problems.append("is not a single clean line")

    note = f"{len(alt)} characters"
    if len(alt) > 250:
        note += " (over the 250 budget, which is allowed)"
    return report("model follows the prompt rules", problems, note=note)


def check_deterministic():
    prompt = prompt_for("chart", "the trend, axis meaning, and end values")
    first, _ = ask("accuracy-chart.png", prompt)
    second, _ = ask("accuracy-chart.png", prompt)
    problems = []
    if first != second:
        problems.append("two identical calls gave different text:")
        problems.append(f"  1: {first}")
        problems.append(f"  2: {second}")
    return report("same description twice at temperature 0", problems)


def check_biggest_bar():
    prompt = prompt_for("chart", "the trend, the axis meaning, and the largest value",
                        caption="Samples per disease class.")
    alt, _ = ask("class-counts.png", prompt)
    problems = []
    if "early blight" not in alt.lower():
        problems.append("does not name 'early blight', the tallest bar by far")
    return report("largest bar is named", problems, note=alt)


def check_blank_figure():
    prompt = prompt_for("chart", "the trend, axis meaning, end values, and any "
                                 "crossing or plateau")
    try:
        alt, _ = ask("blank.png", prompt)
    except describer.DescribeError as exc:
        return report("nothing is invented for a blank figure",
                      [f"the call failed: {exc}"])
    low = alt.lower()
    problems = []
    said_unclear = any(w in low for w in
                       ("blank", "empty", "no ", "nothing", "unclear", "cannot",
                        "does not contain", "without"))
    if not said_unclear:
        problems.append("did not say the figure is empty")
    if "%" in alt:
        problems.append("invented a percentage for a figure with no data in it")
    return report("nothing is invented for a blank figure", problems, note=alt)


def check_bad_model():
    problems = []
    try:
        ask("accuracy-chart.png", prompt_for("chart", "the trend"),
            model="gemini-does-not-exist")
        problems.append("a nonexistent model did not fail")
    except describer.Transient as exc:
        problems.append(f"classified as transient and would be retried: {exc}")
    except describer.DescribeError as exc:
        if "404" not in str(exc):
            problems.append(f"unexpected error: {exc}")
    return report("wrong model name fails permanently", problems)


def check_bad_key():
    problems = []
    try:
        ask("accuracy-chart.png", prompt_for("chart", "the trend"),
            key="not-a-real-key")
        problems.append("an invalid key did not fail")
    except describer.Transient as exc:
        problems.append(f"classified as transient and would be retried: {exc}")
    except describer.DescribeError as exc:
        if "API key" not in str(exc):
            problems.append(f"the error does not mention the key: {exc}")
    return report("invalid key fails permanently", problems)


def main():
    global KEY, MODEL
    KEY, why = describer.find_key(os.path.join(PROJECT, "main.tex"))
    if not KEY:
        print(f"skipped: {why}")
        print("These checks call the real model. check.py covers stage 4 without one.")
        return 0

    config, _ = scanner.load_config(os.path.join(PROJECT, "main.tex"))
    MODEL = config.get("model", {}).get("name", "gemini-3.5-flash")
    print(f"calling {MODEL}; this uses API quota and takes about a minute\n")

    def guarded(label, fn, *args):
        try:
            return fn(*args)
        except RateLimited as exc:
            return skipped(label, f"out of API quota: {exc}")

    ok_chain, alt = guarded("the whole chain runs against the real model",
                            check_full_chain) or (None, None)
    results = [
        ok_chain,
        guarded("the description carries the figure's own facts",
                check_chart_facts, alt),
        guarded("the chart profile's asks reach the output",
                check_profile_ask, alt),
        guarded("a real model obeys the prompt's rules",
                check_prompt_rules, alt),
        guarded("temperature 0 gives the same description twice",
                check_deterministic),
        guarded("a bar chart's largest category is named",
                check_biggest_bar),
        guarded("an empty figure is not invented",
                check_blank_figure),
        guarded("a wrong model name fails permanently",
                check_bad_model),
        guarded("an invalid key fails permanently, and says so",
                check_bad_key),
    ]

    passed = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if r is False)
    skips = sum(1 for r in results if r is None)
    line = f"\n{passed}/{len(results)} passed"
    if skips:
        line += f", {skips} skipped (API quota)"
    print(f"{line}, {CALLS['n']} API call(s)")
    if skips and not failed:
        print("Nothing failed. Re-run when the quota resets to check the rest.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
