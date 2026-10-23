import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

TIMEOUT_S = 90
ENV_NAME = "GEMINI_API_KEY"
ENV_FILE = ".env"

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")

REPLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"alt": {"type": "STRING"}},
    "required": ["alt"],
}

PROMPT = """You are writing the alternative text for a figure in an academic PDF.
It will be read aloud to someone who cannot see the figure, in place of it.

Describe this {profile}. Say {ask_for}.

Rules:
- Do not begin with "image of", "figure showing", "a picture of" or similar.
  The reader already knows it is a figure.
- Name what kind of figure it is, then say what it shows.
- Give the information a sighted reader would take from it, not a list of its
  visual parts.
- At most {max_chars} characters. One or two sentences.
- Plain text: no markup, no LaTeX, no quotation marks around the whole answer.
- Describe only what is actually visible. If the figure is too unclear to
  describe, say so plainly rather than guessing.
{caption}"""


def describe(state):
    renders = [r for r in state.get("renders", []) if r.get("image")]
    state["described"] = []

    if not renders:
        return "nothing to describe"

    config = state.get("config") or load_config_for(state["document"])
    settings = config.get("model", {})
    provider = state.get("provider") or settings.get("provider", "gemini")
    adapter = ADAPTERS.get(provider)
    warnings = []

    if adapter is None:
        state["stop"] = True
        print(f"  no adapter for provider {provider!r}; known: "
              f"{', '.join(sorted(ADAPTERS))}")
        return f"unknown provider {provider!r}"

    context = {
        "model": settings.get("name", "gemini-3.5-flash"),
        "prompt_version": settings.get("prompt_version", "fig-v1"),
        "max_chars": config.get("defaults", {}).get("alt_max_chars", 250),
        "profiles": config.get("profiles", {}),
        "by": settings.get("name", "gemini-3.5-flash") if provider != "stub" else "stub",
        "provider": provider,
    }

    if provider != "stub":
        key, why = find_key(state["document"])
        if not key:
            state["stop"] = True
            print(f"  {why}")
            return "no API key, so nothing was described"
        context["key"] = key

    folder = responses_dir(state["document"])
    described = []
    for render in renders:
        described.append(one(render, adapter, context, folder, warnings))

    state["described"] = described
    state["warnings"] = state.get("warnings", []) + warnings
    if state.get("verbose"):
        for w in warnings:
            print(f"  warning: {w}")

    ok = [d for d in described if d["ok"]]
    line = f"{len(ok)}/{len(described)} described"
    if provider == "stub":
        line += " (stub: no model was called)"
    if warnings and not state.get("verbose"):
        line += f", {len(warnings)} warning(s)"
    return line


def one(render, adapter, context, folder, warnings):
    key = render["key"]
    profile = render.get("profile") or "figure"
    spec = context["profiles"].get(profile, {})

    prompt = PROMPT.format(
        profile=profile,
        ask_for=spec.get("ask_for") or "what it shows and what a reader should take from it",
        max_chars=context["max_chars"],
        caption=(f"\nThe figure's caption, for context: {render['caption']}"
                 if render.get("caption") else ""),
    )

    alt, raw = None, None
    for attempt in range(RETRIES + 1):
        try:
            alt, raw = adapter(render["image"], prompt, context)
            break
        except Transient as exc:
            if attempt == RETRIES:
                warnings.append(f"could not describe {key}: {exc} (after "
                                f"{RETRIES + 1} attempts)")
                return result(render, None, context, ok=False, error=str(exc))
            wait = RETRY_WAITS_S[min(attempt, len(RETRY_WAITS_S) - 1)]
            asked = getattr(exc, "retry_after", None)
            if asked is not None:
                if asked > RETRY_AFTER_CAP_S:
                    warnings.append(
                        f"could not describe {key}: {exc}; the API asks for a "
                        f"{asked:.0f}s wait, which is too long. Run again "
                        f"after that")
                    return result(render, None, context, ok=False, error=str(exc))
                wait = max(wait, asked)
            time.sleep(wait)
        except DescribeError as exc:
            warnings.append(f"could not describe {key}: {exc}")
            return result(render, None, context, ok=False, error=str(exc))
        except Exception as exc:
            warnings.append(f"could not describe {key}: unexpected "
                            f"{type(exc).__name__} from the "
                            f"{context.get('provider', '?')} adapter ({exc})")
            return result(render, None, context, ok=False,
                          error=f"{type(exc).__name__}: {exc}")

    save_response(folder, key, prompt, raw)

    alt = (alt or "").strip()
    if not alt:
        warnings.append(f"could not describe {key}: the model returned no text")
        return result(render, None, context, ok=False, error="empty reply")

    if len(alt) > context["max_chars"]:
        warnings.append(
            f"{key}: the description is {len(alt)} characters, over the "
            f"{context['max_chars']} budget. It is kept as written"
        )
    return result(render, alt, context, ok=True)


def result(render, alt, context, ok, error=None):
    return {
        "key": render["key"],
        "alt": alt,
        "profile": render.get("profile"),
        "hash": render.get("hash"),
        "where": render.get("where"),
        "status": "generated",
        "by": context["by"] if ok else None,
        "prompt_version": context["prompt_version"],
        "at": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
        "ok": ok,
        "error": error,
    }


class DescribeError(Exception):
    pass


class Transient(DescribeError):
    pass


class RateLimited(Transient):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


TRANSIENT_HTTP = (500, 502, 503, 504)

RETRY_AFTER_CAP_S = 60

RETRIES = 2
RETRY_WAITS_S = (2, 5)


def call_gemini(image_path, prompt, context):
    try:
        with open(image_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise DescribeError(f"could not read the render ({exc})")

    encoded = base64.b64encode(data).decode()

    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": image_mime(data), "data": encoded}},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": REPLY_SCHEMA,
            "temperature": 0,
        },
    }

    request = urllib.request.Request(
        ENDPOINT.format(model=context["model"]),
        data=json.dumps(payload).encode(),
        headers={"x-goog-api-key": context["key"],
                 "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        error = error_body(exc)
        detail = f"HTTP {exc.code}: {error_message(error)}"
        if exc.code == 429:
            raise limit_error(error, detail)
        if exc.code in TRANSIENT_HTTP:
            raise Transient(f"the model was busy ({detail})")
        raise DescribeError(f"the model refused the request ({detail})")
    except urllib.error.URLError as exc:
        raise Transient(f"could not reach the model ({exc.reason})")
    except json.JSONDecodeError as exc:
        raise DescribeError(f"the reply was not JSON ({exc})")
    except OSError as exc:
        raise Transient(f"the connection broke ({exc})")

    return read_reply(body), body


MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_mime(data):
    for signature, mime in MAGIC:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def read_reply(body):
    candidates = body.get("candidates") or []
    if not candidates:
        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise DescribeError(f"the model declined the figure ({blocked})")
        raise Transient("the model returned nothing")

    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = candidates[0].get("finishReason")
        raise Transient(f"the model returned no text"
                        + (f" (finishReason {reason})" if reason else ""))

    try:
        return json.loads(text).get("alt")
    except json.JSONDecodeError:
        raise DescribeError("the reply was not the JSON shape that was asked for")


def call_stub(image_path, prompt, context):
    profile = re.search(r"Describe this (\w+)", prompt)
    return (f"[stub] a {profile.group(1) if profile else 'figure'}; "
            f"no model was called, so this is placeholder text."), {"stub": True}


ADAPTERS = {"gemini": call_gemini, "stub": call_stub}


def find_key(document):
    key = os.environ.get(ENV_NAME)
    if key:
        return key.strip(), None

    folder = os.path.dirname(os.path.abspath(document))
    while True:
        candidate = os.path.join(folder, ENV_FILE)
        if os.path.isfile(candidate):
            found = read_env(candidate)
            if found:
                return found, None
        parent = os.path.dirname(folder)
        if parent == folder:
            return None, (f"no {ENV_NAME}: set it in the environment or in a "
                          f"{ENV_FILE} file beside the document")
        folder = parent


def read_env(path):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != ENV_NAME:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return value
    return None


def load_config_for(document):
    import scanner
    config, _ = scanner.load_config(document)
    return config


def responses_dir(document):
    folder = os.path.join(os.path.dirname(document) or ".", "_alt", "responses")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_response(folder, key, prompt, raw):
    path = os.path.join(folder, safe_name(key) + ".json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "prompt": prompt, "response": raw},
                      fh, indent=2, ensure_ascii=False)
    except (OSError, TypeError):
        pass


def safe_name(key):
    return re.sub(r"[^A-Za-z0-9._-]", "-", key)


def error_body(exc):
    try:
        body = json.loads(exc.read().decode())
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {}
    except Exception:
        return {}


def error_message(error):
    return (error.get("message") or "no message")[:160]


def limit_error(error, detail):
    details = [d for d in (error.get("details") or []) if isinstance(d, dict)]

    quota_ids = []
    for d in details:
        if "QuotaFailure" in str(d.get("@type", "")):
            quota_ids += [str(v.get("quotaId", "")) for v in d.get("violations") or []
                          if isinstance(v, dict)]
    if any("perday" in q.lower() for q in quota_ids):
        return DescribeError(f"the daily quota for this model is used up ({detail}); "
                             f"it will not reset during this run")

    retry_after = None
    for d in details:
        if "RetryInfo" in str(d.get("@type", "")):
            retry_after = seconds(d.get("retryDelay"))
    return RateLimited(f"the API's rate limit was reached ({detail})",
                       retry_after=retry_after)


def seconds(duration):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(duration or ""))
    return float(match.group(1)) if match else None
