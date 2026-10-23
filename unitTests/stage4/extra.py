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

PROJECT = project_dir()

PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"the rest does not matter"
JPEG_BYTES = bytes.fromhex("ffd8ffe000104a464946") + b"the rest does not matter"


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def workspace():
    work = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "figures"), os.path.join(work, "figures"))
    for f in os.listdir(HERE):
        if f.endswith((".tex", ".alt")):
            shutil.copyfile(os.path.join(HERE, f), os.path.join(work, f))
    shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                    os.path.join(work, "alt.config.json"))
    return work


def describe(name, adapter, provider="gemini", work=None, state_extra=None):
    work = work or workspace()
    state = {"document": os.path.join(work, name), "provider": provider}
    state.update(state_extra or {})
    saved = dict(describer.ADAPTERS)
    previous = os.environ.get(describer.ENV_NAME)
    describer.ADAPTERS[provider] = adapter
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
    return state, work


def check_media_type():
    problems = []
    cases = [(PNG_BYTES, "image/png"),
             (JPEG_BYTES, "image/jpeg"),
             (b"GIF89a" + b"...", "image/gif"),
             (b"RIFF" + b"1234" + b"WEBP" + b"...", "image/webp")]
    for data, want in cases:
        got = describer.image_mime(data)
        if got != want:
            problems.append(f"{want}: read as {got!r}")
    if describer.image_mime(b"not an image at all") != "image/png":
        problems.append("unrecognised bytes do not fall back to png")
    return report("media type comes from the bytes",
                  problems)


def check_jpeg_mime():
    work = tempfile.mkdtemp()
    path = os.path.join(work, "photo.png")
    open(path, "wb").write(JPEG_BYTES)
    sent = {}

    class FakeResponse:
        def __enter__(self_):
            return self_
        def __exit__(self_, *a):
            return False
        def read(self_):
            return b'{"candidates":[{"content":{"parts":[{"text":"{\\"alt\\":\\"ok\\"}"}]}}]}'

    import urllib.request
    real = urllib.request.urlopen

    def fake(request, timeout=None):
        sent["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    urllib.request.urlopen = fake
    try:
        describer.call_gemini(path, "prompt", {"model": "m", "key": "k"})
    finally:
        urllib.request.urlopen = real

    problems = []
    parts = sent.get("payload", {}).get("contents", [{}])[0].get("parts", [])
    inline = next((p["inline_data"] for p in parts if "inline_data" in p), None)
    if inline is None:
        problems.append("no image was attached to the request")
    elif inline["mime_type"] != "image/jpeg":
        problems.append(f"a JPEG was declared {inline['mime_type']!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("JPEG sent as image/jpeg", problems)


def check_env_parsing():
    work = tempfile.mkdtemp()
    cases = {
        'GEMINI_API_KEY=plain\n': "plain",
        'GEMINI_API_KEY="double"\n': "double",
        "GEMINI_API_KEY='single'\n": "single",
        'export GEMINI_API_KEY=exported\n': "exported",
        '# GEMINI_API_KEY=commented\nGEMINI_API_KEY=real\n': "real",
        'GEMINI_API_KEY=  spaced  \n': "spaced",
    }
    problems = []
    path = os.path.join(work, ".env")
    for text, want in cases.items():
        open(path, "w", encoding="utf-8").write(text)
        got = describer.read_env(path)
        if got != want:
            problems.append(f"{text.strip()!r} -> {got!r}, expected {want!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report(".env quotes, export and comments",
                  problems)


def check_env_priority():
    work = tempfile.mkdtemp()
    open(os.path.join(work, ".env"), "w", encoding="utf-8").write(
        "GEMINI_API_KEY=from-the-file\n")
    open(os.path.join(work, "doc.tex"), "w", encoding="utf-8").write("x")
    previous = os.environ.get(describer.ENV_NAME)
    os.environ[describer.ENV_NAME] = "from-the-environment"
    try:
        got, _ = describer.find_key(os.path.join(work, "doc.tex"))
    finally:
        if previous is None:
            os.environ.pop(describer.ENV_NAME, None)
        else:
            os.environ[describer.ENV_NAME] = previous
    shutil.rmtree(work, ignore_errors=True)
    return report("environment variable beats .env",
                  [] if got == "from-the-environment" else [f"got {got!r}"])


def check_reply_shapes():
    problems = []

    good = {"candidates": [{"content": {"parts": [{"text": '{"alt": "A chart."}'}]}}]}
    if describer.read_reply(good) != "A chart.":
        problems.append("a well-formed reply was not read")

    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    try:
        describer.read_reply(blocked)
        problems.append("a safety block was not raised as an error")
    except describer.Transient:
        problems.append("a safety block was treated as worth retrying")
    except describer.DescribeError:
        pass

    empty = {"candidates": []}
    try:
        describer.read_reply(empty)
        problems.append("an empty reply was accepted")
    except describer.Transient:
        pass
    except describer.DescribeError:
        problems.append("an empty reply was not retried")

    not_json = {"candidates": [{"content": {"parts": [{"text": "just prose"}]}}]}
    try:
        describer.read_reply(not_json)
        problems.append("prose in place of the JSON shape was accepted")
    except describer.DescribeError:
        pass

    no_text = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    try:
        describer.read_reply(no_text)
        problems.append("a reply with no text at all was accepted")
    except describer.DescribeError:
        pass

    return report("reply shapes", problems)


def check_adapter_exception():
    def adapter(image_path, prompt, context):
        raise TypeError("'list' object has no attribute 'get'")

    state, work = describe("queued.tex", adapter)
    problems = []
    if not state["described"]:
        problems.append("no result was recorded")
    elif state["described"][0]["ok"]:
        problems.append("a broken reply was reported as a success")
    shutil.rmtree(work, ignore_errors=True)
    return report("adapter exceptions are recorded",
                  problems)


def check_all_attempts_used():
    calls = {"n": 0}

    def always_busy(image_path, prompt, context):
        calls["n"] += 1
        raise describer.Transient("the model was busy (HTTP 503)")

    saved = describer.RETRY_WAITS_S
    describer.RETRY_WAITS_S = (0, 0)
    try:
        state, work = describe("queued.tex", always_busy)
    finally:
        describer.RETRY_WAITS_S = saved

    problems = []
    if calls["n"] != describer.RETRIES + 1:
        problems.append(f"the adapter was called {calls['n']}x, expected "
                        f"{describer.RETRIES + 1}")
    if state["described"][0]["ok"]:
        problems.append("gave up but reported success")
    if state["described"][0]["alt"] is not None:
        problems.append("invented text after giving up")
    shutil.rmtree(work, ignore_errors=True)
    return report("all retries used before failing",
                  problems, note=f"{calls['n']} attempt(s)")


def http_error(code, error):
    import urllib.error
    body = json.dumps({"error": error}).encode()
    return urllib.error.HTTPError("https://example.invalid", code, "error", {},
                                  io.BytesIO(body))


def error_from_the_wire(exc):
    import urllib.request
    work = tempfile.mkdtemp()
    path = os.path.join(work, "figure.png")
    open(path, "wb").write(JPEG_BYTES)
    real = urllib.request.urlopen

    def fake(request, timeout=None):
        raise exc

    urllib.request.urlopen = fake
    try:
        describer.call_gemini(path, "prompt", {"model": "m", "key": "k"})
        return None
    except Exception as raised:
        return raised
    finally:
        urllib.request.urlopen = real
        shutil.rmtree(work, ignore_errors=True)


QUOTA_MESSAGE = ("You exceeded your current quota, please check your plan and "
                 "billing details.")


def check_rate_limit_message():
    raised = error_from_the_wire(http_error(429, {
        "code": 429, "message": QUOTA_MESSAGE, "status": "RESOURCE_EXHAUSTED"}))
    problems = []
    if not isinstance(raised, describer.RateLimited):
        problems.append(f"raised {type(raised).__name__}, expected RateLimited")
    elif not isinstance(raised, describer.Transient):
        problems.append("a rate limit is no longer retried at all")
    if raised is not None and "busy" in str(raised):
        problems.append(f"still reported as the model being busy: {raised}")
    return report("429 reported as a rate limit", problems,
                  note=str(raised))


def check_daily_quota():
    raised = error_from_the_wire(http_error(429, {
        "code": 429, "message": QUOTA_MESSAGE, "status": "RESOURCE_EXHAUSTED",
        "details": [{
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
        }]}))
    problems = []
    if not isinstance(raised, describer.DescribeError):
        problems.append(f"raised {type(raised).__name__}, expected a DescribeError")
    elif isinstance(raised, describer.Transient):
        problems.append("a used-up daily quota would be retried")
    elif "daily quota" not in str(raised):
        problems.append(f"the message does not say what ran out: {raised}")
    return report("daily quota is not retried", problems)


def check_real_daily_quota():
    body = json.load(open(os.path.join(HERE, "real-429-daily-quota.json"),
                          encoding="utf-8"))
    raised = error_from_the_wire(http_error(429, body["error"]))
    problems = []
    if isinstance(raised, describer.Transient):
        problems.append(f"a used-up daily quota would be retried "
                        f"(retry_after={getattr(raised, 'retry_after', None)!r})")
    elif not isinstance(raised, describer.DescribeError):
        problems.append(f"raised {type(raised).__name__}, expected a DescribeError")
    elif "daily quota" not in str(raised):
        problems.append(f"the message does not say what ran out: {raised}")
    return report("real daily-quota 429 is not retried",
                  problems)


def check_retry_delay_read():
    raised = error_from_the_wire(http_error(429, {
        "code": 429, "message": QUOTA_MESSAGE, "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "35s"},
        ]}))
    problems = []
    if not isinstance(raised, describer.RateLimited):
        problems.append(f"raised {type(raised).__name__}, expected RateLimited")
    elif raised.retry_after != 35.0:
        problems.append(f"retry_after={raised.retry_after!r}, expected 35.0")
    return report("retry delay read from a 429", problems)


def sleeps_recorded(run):
    waits = []
    real = describer.time.sleep
    describer.time.sleep = waits.append
    try:
        return run(), waits
    finally:
        describer.time.sleep = real


def check_retry_delay_waited():
    calls = {"n": 0}

    def limited_twice(image_path, prompt, context):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise describer.RateLimited("the API's rate limit was reached",
                                        retry_after=7)
        return "Line chart of accuracy.", {"ok": True}

    (state, work), waits = sleeps_recorded(lambda: describe("queued.tex", limited_twice))
    problems = []
    if waits != [7, 7]:
        problems.append(f"waited {waits}, expected [7, 7] as the API asked")
    if not state["described"][0]["ok"]:
        problems.append("the third attempt's success was not kept")
    shutil.rmtree(work, ignore_errors=True)
    return report("retry waits the asked delay", problems)


def check_long_retry_delay():
    calls = {"n": 0}

    def limited_for_ten_minutes(image_path, prompt, context):
        calls["n"] += 1
        raise describer.RateLimited("the API's rate limit was reached",
                                    retry_after=600)

    (state, work), waits = sleeps_recorded(
        lambda: describe("queued.tex", limited_for_ten_minutes))
    problems = []
    if calls["n"] != 1:
        problems.append(f"the adapter was called {calls['n']}x, expected once")
    if waits:
        problems.append(f"waited {waits} for a limit longer than the cap")
    if state["described"][0]["ok"] or state["described"][0]["alt"] is not None:
        problems.append("reported success, or invented text, after giving up")
    if not any("600s" in w for w in state.get("warnings", [])):
        problems.append(f"the warning does not say how long to wait: {state.get('warnings')}")
    shutil.rmtree(work, ignore_errors=True)
    return report("long retry delay gives up",
                  problems)


def check_config_from_state():
    seen = {}

    def adapter(image_path, prompt, context):
        seen["max_chars"] = context["max_chars"]
        return "described.", {"fake": True}

    work = workspace()
    state = {"document": os.path.join(work, "queued.tex"), "provider": "gemini"}
    saved = dict(describer.ADAPTERS)
    describer.ADAPTERS["gemini"] = adapter
    os.environ[describer.ENV_NAME] = "test-key"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
            state["config"] = {"model": {"name": "from-state"},
                               "defaults": {"alt_max_chars": 42}, "profiles": {}}
            describer.describe(state)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved)

    problems = []
    if not state["described"]:
        problems.append("nothing was described")
    if seen.get("max_chars") != 42:
        problems.append(f"the state's config was ignored: budget "
                        f"{seen.get('max_chars')}, expected 42")
    if state["described"] and state["described"][0]["by"] != "from-state":
        problems.append(f"provenance came from elsewhere: "
                        f"{state['described'][0]['by']!r}")

    fresh = {"document": os.path.join(work, "queued.tex")}
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(fresh)
    if not fresh.get("config"):
        problems.append("stage 1 left no config in the state for stage 4 to use")
    shutil.rmtree(work, ignore_errors=True)
    return report("config from stage 1 is used", problems)


def check_provenance():
    def adapter(image_path, prompt, context):
        return "described.", {"fake": True}

    state, work = describe("queued.tex", adapter)
    d = state["described"][0]
    problems = []
    for field in ("by", "prompt_version", "at", "hash", "status"):
        if not d.get(field):
            problems.append(f"the proposal carries no {field!r}")
    if d.get("status") != "generated":
        problems.append(f"status is {d.get('status')!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("proposal records model and time", problems,
                  note=f"by={d.get('by')}, prompt={d.get('prompt_version')}")


def check_caption_context():
    seen = {}

    def adapter(image_path, prompt, context):
        seen["prompt"] = prompt
        return "described.", {"fake": True}

    state, work = describe("queued.tex", adapter)
    prompt = seen.get("prompt", "")
    problems = []
    if "caption, for context" not in prompt:
        problems.append("the caption is not marked as context")
    if prompt.index("Rules:") > prompt.index("Describe this"):
        pass
    for rule in ("Do not begin with", "Describe only what is actually visible"):
        if rule not in prompt:
            problems.append(f"the prompt has lost the rule {rule!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("caption given as context",
                  problems)


def check_failed_render_skipped():
    calls = {"n": 0}

    def adapter(image_path, prompt, context):
        calls["n"] += 1
        return "described.", {"fake": True}

    work = workspace()
    state = {"document": os.path.join(work, "queued.tex"), "provider": "gemini"}
    saved = dict(describer.ADAPTERS)
    describer.ADAPTERS["gemini"] = adapter
    os.environ[describer.ENV_NAME] = "test-key"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan(state)
            resolver.resolve(state)
            renderer.render(state)
            for r in state["renders"]:
                r["image"], r["how"], r["error"] = None, "failed", "no image"
            describer.describe(state)
    finally:
        describer.ADAPTERS.clear()
        describer.ADAPTERS.update(saved)
    shutil.rmtree(work, ignore_errors=True)
    return report("failed render is not sent",
                  [f"the model was called {calls['n']}x"] if calls["n"] else [])


CHECKS = [
    check_media_type,
    check_jpeg_mime,
    check_env_parsing,
    check_env_priority,
    check_reply_shapes,
    check_adapter_exception,
    check_all_attempts_used,
    check_rate_limit_message,
    check_daily_quota,
    check_real_daily_quota,
    check_retry_delay_read,
    check_retry_delay_waited,
    check_long_retry_delay,
    check_config_from_state,
    check_provenance,
    check_caption_context,
    check_failed_render_skipped,
]


def run():
    return [check() for check in CHECKS]
