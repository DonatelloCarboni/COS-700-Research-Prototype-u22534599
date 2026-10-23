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
sys.path.insert(0, HERE)
import extra


def run(name, provider="stub", adapter=None, env_key="test-key"):
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    for f in os.listdir(HERE):
        if f.endswith((".tex", ".alt")):
            shutil.copyfile(os.path.join(HERE, f), os.path.join(work, f))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))

    state = {"document": os.path.join(work, name), "provider": provider}

    saved_adapters = dict(describer.ADAPTERS)
    saved_env = os.environ.get(describer.ENV_NAME)
    if adapter is not None:
        describer.ADAPTERS[provider] = adapter
    if env_key is None:
        os.environ.pop(describer.ENV_NAME, None)
    else:
        os.environ[describer.ENV_NAME] = env_key
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
            describer.describe(state)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved_adapters)
        if saved_env is None:
            os.environ.pop(describer.ENV_NAME, None)
        else:
            os.environ[describer.ENV_NAME] = saved_env
    return state, work


def report(label, problems):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    for p in problems:
        print(f"        {p}")
    return not problems


def adapter_ok(image_path, prompt, context):
    return "A line chart of accuracy against epoch, rising to 94%.", {"fake": True}


def adapter_boom(image_path, prompt, context):
    raise describer.DescribeError("the model refused the request (HTTP 503)")


def adapter_empty(image_path, prompt, context):
    return "   ", {"fake": True}


def adapter_long(image_path, prompt, context):
    return "x" * 400, {"fake": True}


ATTEMPTS = {"transient": 0, "permanent": 0}


def adapter_transient_once(image_path, prompt, context):
    ATTEMPTS["transient"] += 1
    if ATTEMPTS["transient"] == 1:
        raise describer.Transient("the model was busy (HTTP 503)")
    return "Recovered on the second attempt.", {"fake": True}


def adapter_permanent(image_path, prompt, context):
    ATTEMPTS["permanent"] += 1
    raise describer.DescribeError("the model declined the figure (SAFETY)")


CAPTURED = {}


def adapter_capture(image_path, prompt, context):
    CAPTURED["prompt"] = prompt
    CAPTURED["image"] = image_path
    return "captured.", {"fake": True}


def check_describes():
    state, _ = run("queued.tex", adapter=adapter_ok)
    d = state["described"]
    problems = []
    if len(d) != 1:
        problems.append(f"{len(d)} described, expected 1")
    else:
        if not d[0]["ok"]:
            problems.append(f"not ok: {d[0]['error']}")
        if d[0]["alt"] != "A line chart of accuracy against epoch, rising to 94%.":
            problems.append(f"alt not carried through: {d[0]['alt']!r}")
    return report("queued.tex is described", problems)


def check_status_generated():
    state, _ = run("queued.tex", adapter=adapter_ok)
    d = state["described"][0]
    problems = []
    if d["status"] != "generated":
        problems.append(f"status is {d['status']!r}, must always be 'generated'")
    return report("status is 'generated'", problems)


def check_stub_signature():
    state, _ = run("queued.tex", provider="stub")
    d = state["described"][0]
    problems = []
    if d["by"] != "stub":
        problems.append(f"by={d['by']!r}, expected 'stub'")
    if "[stub]" not in (d["alt"] or ""):
        problems.append(f"stub text is not marked as such: {d['alt']!r}")
    return report("stub text is marked as stub", problems)


def check_nothing_queued():
    state, _ = run("resolved.tex", adapter=adapter_ok)
    problems = []
    if state["described"]:
        problems.append(f"{len(state['described'])} described, expected 0 "
                        f"(nothing was queued)")
    return report("resolved.tex calls nothing", problems)


def check_failed_call():
    state, _ = run("queued.tex", adapter=adapter_boom)
    d = state["described"][0]
    problems = []
    if d["ok"]:
        problems.append("reported ok despite the adapter failing")
    if d["alt"] is not None:
        problems.append(f"invented text on failure: {d['alt']!r}")
    if d["by"] is not None:
        problems.append(f"claimed provenance on failure: {d['by']!r}")
    if not any("could not describe" in w for w in state.get("warnings", [])):
        problems.append(f"no warning raised; got {state.get('warnings')}")
    return report("failed call gives no text", problems)


def check_empty_reply():
    state, _ = run("queued.tex", adapter=adapter_empty)
    d = state["described"][0]
    problems = []
    if d["ok"] or d["alt"]:
        problems.append(f"an empty reply was accepted: {d['alt']!r}")
    return report("empty reply is a failure", problems)


def check_over_budget():
    state, _ = run("queued.tex", adapter=adapter_long)
    d = state["described"][0]
    problems = []
    if not d["ok"]:
        problems.append("over-budget text was rejected; it should be kept")
    if len(d["alt"] or "") != 400:
        problems.append(f"text was truncated to {len(d['alt'] or '')} chars")
    if not any("over the" in w for w in state.get("warnings", [])):
        problems.append("no warning about the length")
    return report("over-budget text is kept and flagged", problems)


def check_prompt():
    CAPTURED.clear()
    state, _ = run("queued.tex", adapter=adapter_capture)
    prompt = CAPTURED.get("prompt", "")
    problems = []
    for needed in ("chart", "trend", "250", "accuracy against training epoch"):
        if needed not in prompt:
            problems.append(f"prompt does not carry {needed!r}")
    for banned in ("image of", "figure showing"):
        if banned not in prompt:
            problems.append(f"prompt does not forbid {banned!r}")
    return report("prompt uses profile and caption", problems)


def check_unknown_provider():
    state, _ = run("queued.tex", provider="nosuchprovider")
    problems = []
    if not state.get("stop"):
        problems.append("did not stop on an unknown provider")
    if state["described"]:
        problems.append("described something anyway")
    return report("unknown provider stops the run", problems)


def check_missing_key():
    state, _ = run("queued.tex", provider="gemini", env_key=None)
    problems = []
    if not state.get("stop"):
        problems.append("did not stop when the key was missing")
    if state["described"]:
        problems.append(f"described {len(state['described'])} anyway, "
                        f"without a key")
    return report("missing key stops the run", problems)


def check_retry():
    ATTEMPTS["transient"] = 0
    state, _ = run("queued.tex", adapter=adapter_transient_once)
    d = state["described"][0]
    problems = []
    if not d["ok"]:
        problems.append(f"did not recover after a transient failure: {d['error']}")
    if ATTEMPTS["transient"] != 2:
        problems.append(f"adapter called {ATTEMPTS['transient']}x, expected 2")

    ATTEMPTS["permanent"] = 0
    state, _ = run("queued.tex", adapter=adapter_permanent)
    if state["described"][0]["ok"]:
        problems.append("a permanent refusal was reported as ok")
    if ATTEMPTS["permanent"] != 1:
        problems.append(f"a refusal was retried {ATTEMPTS['permanent']}x, expected 1")
    return report("transient errors are retried, refusals are not", problems)


def check_connection_reset():
    def adapter_reset(image_path, prompt, context):
        raise ConnectionResetError(10054, "An existing connection was forcibly closed")

    problems = []
    state, _ = run("queued.tex", adapter=adapter_reset)
    if not state.get("described"):
        problems.append("the stage produced no result at all")
    elif state["described"][0]["ok"]:
        problems.append("a dropped connection was reported as success")

    import urllib.error
    if not issubclass(urllib.error.URLError, OSError):
        problems.append("URLError is not an OSError; the handler order assumption "
                        "in call_gemini no longer holds")
    source = open(os.path.join(project_dir(), "describer.py"), encoding="utf-8").read()
    block = source[source.index("with urllib.request.urlopen"):]
    order = [block.find("except urllib.error.HTTPError"),
             block.find("except urllib.error.URLError"),
             block.find("except OSError")]
    if -1 in order or order != sorted(order):
        problems.append("call_gemini catches OSError before the narrower errors, "
                        "so HTTP status would never be inspected")
    return report("connection reset is retried", problems)


def check_response_saved():
    state, work = run("queued.tex", adapter=adapter_ok)
    path = os.path.join(work, "_alt", "responses", "fig-one.json")
    problems = []
    if not os.path.isfile(path):
        problems.append("no raw response was saved")
    else:
        saved = json.load(open(path, encoding="utf-8"))
        for field in ("key", "prompt", "response"):
            if field not in saved:
                problems.append(f"saved response has no {field!r}")
    return report("raw reply is saved", problems)


CHAPTER_PROMPTS = []


def adapter_record(image_path, prompt, context):
    CHAPTER_PROMPTS.append(prompt)
    return "A confusion matrix with a strong diagonal.", {"fake": True}


def check_multifile():
    work = tempfile.mkdtemp()
    src = os.path.join(HERE, "multifile")
    shutil.copytree(os.path.join(src, "figures"), os.path.join(work, "figures"))
    shutil.copytree(os.path.join(src, "chapters"), os.path.join(work, "chapters"))
    shutil.copyfile(os.path.join(src, "main.tex"), os.path.join(work, "main.tex"))
    shutil.copyfile(os.path.join(project_dir(), "alt.config.json"),
                    os.path.join(work, "alt.config.json"))

    CHAPTER_PROMPTS.clear()
    state = {"document": os.path.join(work, "main.tex"), "provider": "gemini"}
    saved = dict(describer.ADAPTERS)
    previous = os.environ.get(describer.ENV_NAME)
    describer.ADAPTERS["gemini"] = adapter_record
    os.environ[describer.ENV_NAME] = "test-key"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
            describer.describe(state)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved)
        if previous is None:
            os.environ.pop(describer.ENV_NAME, None)
        else:
            os.environ[describer.ENV_NAME] = previous

    described = state["described"]
    problems = []
    if len(described) != 1:
        problems.append(f"{len(described)} described, expected 1")
    elif not described[0]["ok"]:
        problems.append(f"the chapter's figure was not described: "
                        f"{described[0]['error']}")
    if not CHAPTER_PROMPTS:
        problems.append("no prompt was built at all")
    else:
        prompt = CHAPTER_PROMPTS[0]
        if "confusion matrix" not in prompt.lower():
            problems.append("the chapter's caption did not reach the prompt")
        if "chart" not in prompt.lower():
            problems.append("the profile guessed from the chapter's caption did "
                            "not reach the prompt")
    return report("chapter caption reaches the prompt", problems)

def main():
    results = [
        check_describes(),
        check_status_generated(),
        check_stub_signature(),
        check_nothing_queued(),
        check_failed_call(),
        check_empty_reply(),
        check_over_budget(),
        check_prompt(),
        check_unknown_provider(),
        check_missing_key(),
        check_retry(),
        check_connection_reset(),
        check_response_saved(),
        check_multifile(),
    ]
    results += extra.run()
    results += [
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
