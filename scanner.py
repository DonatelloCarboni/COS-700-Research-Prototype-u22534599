import hashlib
import json
import os
import re


FIGURE_RE = re.compile(r"\\begin\{(figure\*?)\}(.*?)\\end\{\1\}", re.DOTALL)
TIKZ_RE = re.compile(r"\\begin\{tikzpicture\}(.*?)\\end\{tikzpicture\}", re.DOTALL)
GRAPHICS_RE = re.compile(r"\\includegraphics\*?(?![a-zA-Z])")
SUB_RE = re.compile(
    r"\\begin\{(subfigure|subcaptionblock|minipage)\}(.*?)\\end\{\1\}", re.DOTALL
)
GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{(.*?)\}\s*$", re.MULTILINE | re.DOTALL)

VERBATIM_RE = re.compile(
    r"\\begin\{(verbatim\*?|lstlisting|minted)\}(?:\[[^\]]*\])?.*?\\end\{\1\}",
    re.DOTALL,
)

MARKER_RE = re.compile(r"^[ \t]*%!alt[ \t]+(?:profile=)?(\S+)[ \t]*$", re.MULTILINE)

INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
OTHER_FLOAT_RE = re.compile(r"\\begin\{(wrapfigure|sidewaysfigure|SCfigure)\}")

CONFIG_NAME = "alt.config.json"

NO_CONFIG = {"profiles": {}, "fallback": "figure", "model": {}, "defaults": {},
             "path": None}


def scan(state):
    document = state["document"]
    config, warnings = load_config(document)

    units, root = collect_units(document, warnings)
    state["tex_files"] = [u["rel"] for u in units]

    roots = graphics_roots("\n".join(u["text"] for u in units), root)

    figures = []
    for unit in units:
        figures.extend(scan_unit(unit, roots, config, warnings))

    figures.sort(key=lambda f: f["_order"])

    unique_keys(figures, warnings)

    for unit in units:
        for m in OTHER_FLOAT_RE.finditer(unit["text"]):
            warnings.append(f"{unit['rel']}:{line_of(unit['text'], m.start())}: "
                            f"{m.group(1)} is not scanned yet")

    missing = [f["key"] for f in figures if f["hash"] is None]
    for key in missing:
        warnings.append(f"cannot find the file for {key}; it cannot be hashed "
                        f"or rendered until it exists")

    state["figures"] = figures
    state["config"] = config
    warnings = state.get("warnings", []) + warnings
    state["warnings"] = warnings
    if state.get("verbose"):
        for w in warnings:
            print(f"  warning: {w}")

    parts = [f"{len(figures)} figure(s)"]
    if len(units) > 1:
        parts.append(f"{len(units)} file(s)")
    if missing:
        parts.append(f"{len(missing)} file(s) not found")
    if warnings and not state.get("verbose"):
        parts.append(f"{len(warnings)} warning(s)")
    return ", ".join(parts)


def scan_unit(unit, roots, config, warnings):
    text, name = unit["text"], unit["rel"]
    markers = find_markers(blank_verbatim(unit["raw"]))
    applied = set()
    figures = []

    for m in FIGURE_RE.finditer(text):
        body_start, body_end = m.start(2), m.end(2)
        line = line_of(text, m.start())
        if line - 1 in markers:
            applied.add(line - 1)
        override = valid_profile(markers.get(line - 1), config, warnings,
                                 f"{name}:{line}")
        found = scan_float(text, body_start, body_end, name, roots, override,
                           config, warnings, markers, applied)
        if not found:
            warnings.append(f"{name}:{line}: {m.group(1)} holds no image; "
                            f"nothing to describe")
        figures.extend(found)

    covered = [(m.start(), m.end()) for m in FIGURE_RE.finditer(text)]
    for hit in find_images(text, 0, len(text)):
        if any(a <= hit["offset"] < b for a, b in covered):
            continue
        line = line_of(text, hit["offset"])
        marked = markers.get(line - 1)
        if marked is None:
            warnings.append(
                f"{name}:{line}: image outside any figure is not scanned; put it in "
                f"a figure or mark it with %!alt <profile> on the line above"
            )
            continue
        applied.add(line - 1)
        profile = valid_profile(marked, config, warnings, f"{name}:{line}")
        figures.append(record(hit, name, line, roots, None, None, profile,
                              config, None, warnings))

    for line in sorted(set(markers) - applied):
        warnings.append(
            f"{name}:{line}: '%!alt {markers[line]}' applies to nothing; it must "
            f"be on the line directly above a figure environment or an image"
        )

    for f in figures:
        f["_order"] = unit["order"] + (f["offset"],)
    return figures


def collect_units(document, warnings):
    root = os.path.dirname(os.path.abspath(document)) or "."
    units, seen = [], set()

    def walk(path, came_from, order=()):
        real = os.path.abspath(path)
        if real in seen:
            warnings.append(f"{came_from}: {os.path.relpath(real, root)} is already "
                            f"in this project; not following it a second time")
            return
        seen.add(real)

        try:
            with open(path, encoding="utf-8-sig") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"{came_from or 'the document'}: cannot read "
                            f"{os.path.relpath(real, root)} ({exc})")
            return

        text = blank_comments(blank_verbatim(raw))
        rel = os.path.relpath(real, root).replace(os.sep, "/")
        units.append({"path": real, "rel": rel, "raw": raw, "text": text,
                      "order": order})

        for m in INPUT_RE.finditer(text):
            where = f"{rel}:{line_of(text, m.start())}"
            target = resolve_input(m.group(1).strip(), root, os.path.dirname(real))
            if target is None:
                warnings.append(f"{where}: cannot find {m.group(1).strip()!r}; "
                                f"any figures in it are not in this list")
                continue
            walk(target, where, order + (m.start(),))

    walk(os.path.abspath(document), None)
    return units, root


def resolve_input(name, root, including_dir):
    for folder in (root, including_dir):
        for candidate in (name, name + ".tex"):
            full = os.path.join(folder, candidate)
            if os.path.isfile(full):
                return full
    return None


def scan_float(text, body_start, body_end, name, roots, override, config,
               warnings=None, markers=None, applied=None):
    images = find_images(text, body_start, body_end)
    if not images:
        return []
    markers = markers or {}
    float_line = line_of(text, body_start)

    subs = [(m.start(2), m.end(2)) for m in SUB_RE.finditer(text[body_start:body_end])]
    subs = [(body_start + a, body_start + b) for a, b in subs]
    subs = [(a, b) for a, b in subs if any(a <= h["offset"] < b for h in images)]

    own = blank_spans(text, subs) if len(images) > 1 else text
    float_caption = macro_arg(own, "caption", body_start, body_end)
    float_label = macro_arg(own, "label", body_start, body_end)

    out = []
    for index, hit in enumerate(images, start=1):
        region = innermost(subs, hit["offset"])
        if region and len(images) > 1:
            caption = macro_arg(text, "caption", *region) or float_caption
            label = macro_arg(text, "label", *region)
        else:
            caption, label = float_caption, float_label

        fallback = f"{float_label}/{index}" if float_label and len(images) > 1 else None
        line = line_of(text, hit["offset"])
        profile = override
        marked = markers.get(line - 1) if line - 1 >= float_line else None
        if marked is not None:
            if applied is not None:
                applied.add(line - 1)
            profile = valid_profile(marked, config, warnings,
                                    f"{name}:{line}") or override
        out.append(record(hit, name, line, roots, caption, label, profile,
                          config, fallback, warnings))
    return out


DECORATIVE = "artifact"


def record(hit, name, line, roots, caption, label, override, config,
           fallback=None, warnings=None):
    where = f"{name}:{line}"

    if hit["kind"] == "tikz":
        return {
            "key": label or fallback or f"tikz:{line}",
            "kind": "tikz",
            "offset": hit["offset"],
            "source": "(drawn inline)",
            "file": None,
            "body": hit["body"],
            "where": where,
            "caption": caption,
            "label": label,
            "alt": None,
            "profile": override or "diagram",
            "hash": hash_text(hit["body"]),
        }

    filename = hit["path"]
    full = find_file(filename, roots)
    return {
        "key": label or os.path.basename(filename) or fallback or f"image:{line}",
        "kind": "includegraphics",
        "offset": hit["offset"],
        "source": filename,
        "file": full,
        "body": None,
        "where": where,
        "caption": caption,
        "label": label,
        "alt": option(hit["options"], "alt"),
        "profile": guessed_profile(filename, label, caption, config, override,
                                   where, warnings),
        "hash": hash_file(full) if full else None,
    }


def guessed_profile(filename, label, caption, config, override, where, warnings):
    if override:
        return override
    profile = guess_profile(filename, label, caption, config)
    if profile == DECORATIVE and warnings is not None:
        warnings.append(
            f"{where}: {os.path.basename(filename)!r} was guessed decorative from "
            f"its name, so it will have no alt text. If it is not decorative, give "
            f"it a %!alt <profile> line or a \\label that does not match"
        )
    return profile


def find_images(text, start, end):
    hits = []

    for m in GRAPHICS_RE.finditer(text, start, end):
        i = skip_space(text, m.end())
        options = []
        while i < end and text[i] == "[":
            opts, i = read_brackets(text, i)
            if opts is None:
                break
            options.append(opts)
            i = skip_space(text, i)
        path, _ = read_braces(text, i)
        if path is None:
            continue
        hits.append({"kind": "includegraphics", "offset": m.start(),
                     "path": path.strip(), "options": ",".join(options)})

    for m in TIKZ_RE.finditer(text, start, end):
        hits.append({"kind": "tikz", "offset": m.start(), "body": m.group(1)})

    hits.sort(key=lambda h: h["offset"])
    return hits


def macro_arg(text, macro, start, end):
    for m in re.finditer(r"\\" + macro + r"(?![a-zA-Z])", text[start:end]):
        i = skip_space(text, start + m.end())
        if i < end and text[i] == "[":
            _, i = read_brackets(text, i)
            i = skip_space(text, i)
        arg, _ = read_braces(text, i)
        if arg is not None:
            return flatten(arg)
    return None


def read_braces(text, i):
    if i >= len(text) or text[i] != "{":
        return None, i
    depth, j = 0, i
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return None, i


def read_brackets(text, i):
    if i >= len(text) or text[i] != "[":
        return None, i
    depth, j = 0, i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        elif text[j] == "]" and depth == 0:
            return text[i + 1:j], j + 1
        j += 1
    return None, i


def option(options, key):
    for item in split_top_level(options):
        k, sep, v = item.partition("=")
        if sep and k.strip() == key:
            v = v.strip()
            if v.startswith("{") and v.endswith("}"):
                v = v[1:-1]
            return flatten(v)
    return None


def split_top_level(options):
    parts, depth, current = [], 0, []
    for ch in options:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def flatten(text):
    if text is None:
        return None
    for escaped, plain in (("\\%", "%"), ("\\&", "&"), ("\\_", "_"),
                           ("\\$", "$"), ("\\#", "#")):
        text = text.replace(escaped, plain)
    text = re.sub(r"\\[a-zA-Z]+\s*", "", text)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split()) or None


def blank_comments(text):
    out = list(text)
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "%":
            while i < len(text) and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def blank_verbatim(text):
    return blank_spans(text, [(m.start(), m.end()) for m in VERBATIM_RE.finditer(text)])


def blank_spans(text, spans):
    out = list(text)
    for start, end in spans:
        for i in range(start, end):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def find_markers(raw):
    return {line_of(raw, m.start()): m.group(1) for m in MARKER_RE.finditer(raw)}


def skip_space(text, i):
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def innermost(spans, offset):
    holding = [(a, b) for a, b in spans if a <= offset < b]
    return min(holding, key=lambda s: s[1] - s[0]) if holding else None


def graphics_roots(text, root):
    roots = []
    m = GRAPHICSPATH_RE.search(text)
    if m:
        roots += [os.path.join(root, p) for p in re.findall(r"\{([^{}]*)\}", m.group(1))]
    return roots + [root, os.path.join(root, "figures")]


def find_file(name, roots):
    stem, ext = os.path.splitext(name)
    tries = [name] if ext else []
    tries += [stem + e for e in (".pdf", ".png", ".jpg", ".jpeg", ".eps")]
    for folder in roots:
        for candidate in tries:
            full = os.path.join(folder, candidate)
            if os.path.isfile(full):
                return os.path.abspath(full)
    return None


def unique_keys(figures, warnings):
    seen = {}
    for f in figures:
        if f["key"] not in seen:
            seen[f["key"]] = f["where"]
            continue
        original = f["key"]
        candidate = f"{original}@{f['where']}"
        n = 2
        while candidate in seen:
            candidate = f"{original}@{f['where']}#{n}"
            n += 1
        f["key"] = candidate
        warnings.append(
            f"{f['where']}: two figures would both be keyed {original!r} "
            f"(the other is at {seen[original]}); using {f['key']!r} here. "
            f"Add a \\label to give it a stable key"
        )
        seen[f["key"]] = f["where"]


def load_config(document):
    folder = os.path.dirname(os.path.abspath(document))
    path = None
    while True:
        candidate = os.path.join(folder, CONFIG_NAME)
        if os.path.isfile(candidate):
            path = candidate
            break
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent

    if path is None:
        return dict(NO_CONFIG), [
            f"no {CONFIG_NAME} found beside {os.path.basename(document)} or above it; "
            f"every profile falls back to {NO_CONFIG['fallback']!r}"
        ]
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return dict(NO_CONFIG), [
            f"{path} could not be read ({exc}); every profile falls back to "
            f"{NO_CONFIG['fallback']!r}"
        ]

    return {
        "profiles": loaded.get("profiles", {}),
        "fallback": loaded.get("fallback_profile", NO_CONFIG["fallback"]),
        "model": loaded.get("model", {}),
        "defaults": loaded.get("defaults", {}),
        "path": path,
    }, []


def valid_profile(name, config, warnings, where):
    if name is None or name in config["profiles"]:
        return name
    warnings.append(
        f"{where}: %!alt {name!r} is not a profile in {CONFIG_NAME} "
        f"({', '.join(config['profiles']) or 'none defined'}); guessing instead"
    )
    return None


def guess_profile(filename, label, caption, config):
    profiles = config["profiles"]
    name = f"{os.path.basename(filename)} {label or ''}".lower()
    for profile, spec in profiles.items():
        if any(w.lower() in name for w in spec.get("match", [])):
            return profile

    text = (caption or "").lower()
    for profile, spec in profiles.items():
        if spec.get("from_caption", True) and any(
            w.lower() in text for w in spec.get("match", [])
        ):
            return profile

    return config["fallback"]


def hash_file(path):
    with open(path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()[:16]


def hash_text(text):
    normalised = " ".join(text.split())
    return "sha256:" + hashlib.sha256(normalised.encode()).hexdigest()[:16]
