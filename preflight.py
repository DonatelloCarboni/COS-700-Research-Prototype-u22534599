import os
import re
import shutil
import subprocess
import tempfile

COMPILE_TIMEOUT_S = 60


REQUIRED_KEYS = [("tagging", "on"), ("pdfstandard", "ua-2")]

REQUIRED_PACKAGES = [
    ("graphicx", r"\includegraphics and its alt= key"),
    ("tikz", r"inline pictures, which also take alt="),
]

SHIM_DIR = "_alt"


def shim_input(document):
    stem = os.path.splitext(os.path.basename(document))[0]
    relative = f"{SHIM_DIR}/{stem}.alt.tex"
    return f"\\InputIfFileExists{{{relative}}}{{}}{{}}", relative

UNPARSEABLE = object()

DOCUMENTCLASS_RE = re.compile(
    r"\\documentclass(?![a-zA-Z])\s*(?:\[.*?\])?\s*\{.*?\}", re.DOTALL
)
BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")
METADATA_RE = re.compile(r"\\DocumentMetadata(?![a-zA-Z])")
PACKAGE_RE = re.compile(
    r"\\(?:usepackage|RequirePackage)(?![a-zA-Z])\s*(?:\[.*?\])?\s*\{([^}]*)\}",
    re.DOTALL,
)


SHIM_INPUT_RE = re.compile(
    r"\\(?:InputIfFileExists|input|include)(?![a-zA-Z])\s*\{([^}]*)\}"
)


def preflight(state):
    path = state["document"]

    where = locate(blank_comments(read(path)))
    if where["error"]:
        state["stop"] = True
        state["preflight"] = {"ok": False, "error": where["error"]}
        return where["error"]

    status, detail = check_compiles(path)
    if status == "skip":
        if state.get("verbose"):
            print(f"  note: {detail}, skipping the compile check")
    elif status == "fail":
        print()
        print(f"  {os.path.basename(path)} does not compile as it stands:")
        for line in detail:
            print(f"    {line}")
        print()
        state["stop"] = True
        state["preflight"] = {"ok": False, "error": "does not compile", "detail": detail}
        return "does not compile; fix the errors above first"

    raw = read(path)
    bom = had_bom(path)
    code = blank_comments(raw)
    where = locate(code)

    problems = (check_metadata(code, where) + check_packages(code, where)
                + check_shim(code, where, path))
    state["preflight"] = {"ok": not problems, "problems": problems}

    if not problems:
        return "all four requirements present"

    blocked = [p for p in problems if p["fix"] is None]
    if blocked:
        state["stop"] = True
        print()
        for p in blocked:
            print(f"  cannot continue: {p['what']}")
            print(f"    {p['why']}")
        print()
        return f"{len(blocked)} problem(s) to fix by hand"

    shown = report(path, raw, problems)

    if not consent(path, state.get("assume_yes", False), state.get("ask"), shown):
        state["stop"] = True
        return "declined; the later stages need these, so stopping here"

    backup = back_up(path)
    write(path, apply(raw, [p["fix"] for p in problems]), bom)

    again = blank_comments(read(path))
    where = locate(again)
    left = (check_metadata(again, where) + check_packages(again, where)
            + check_shim(again, where, path))
    if left:
        state["stop"] = True
        state["preflight"] = {"ok": False, "problems": left}
        return f"edited {os.path.basename(path)} but {len(left)} item(s) still missing"

    state["preflight"] = {"ok": True, "added": [p["what"] for p in problems],
                          "backup": backup}
    return (f"added {len(problems)} item(s) to {os.path.basename(path)} "
            f"(backup: {os.path.basename(backup)})")


def check_compiles(path):
    engine = shutil.which("pdflatex")
    if not engine:
        return "skip", "pdflatex is not on PATH"

    directory = os.path.dirname(path) or "."
    name = os.path.basename(path)
    stem, _ = os.path.splitext(name)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [engine, "-interaction=nonstopmode", f"-output-directory={tmp}", name],
                cwd=directory, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return "fail", [f"pdflatex did not finish within {COMPILE_TIMEOUT_S}s"]
        except OSError as exc:
            return "skip", f"could not run pdflatex ({exc})"

        pdf = os.path.join(tmp, stem + ".pdf")
        if os.path.isfile(pdf) and os.path.getsize(pdf) > 0:
            return "ok", None

        log = os.path.join(tmp, stem + ".log")
        if not os.path.isfile(log):
            return "fail", ["pdflatex produced no PDF and no log"]
        text = open(log, encoding="utf-8", errors="replace").read()
        lines = [ln.strip() for ln in text.splitlines() if ln.startswith("!")]
        if not lines:
            return "ok", None
        return "fail", lines[:5]


def locate(code):
    cls = DOCUMENTCLASS_RE.search(code)
    doc = BEGIN_DOC_RE.search(code)
    if not cls:
        return {"error": r"no \documentclass; is this the main .tex file?"}
    if not doc:
        return {"error": r"no \begin{document}; is this the main .tex file?"}
    if doc.start() < cls.start():
        return {"error": r"\begin{document} comes before \documentclass"}
    return {
        "error": None,
        "class_start": cls.start(),
        "class_end": cls.end(),
        "preamble": (cls.end(), doc.start()),
    }


def check_metadata(code, where):
    found = find_metadata(code, where["preamble"][1])

    if found is None:
        line = r"\DocumentMetadata{" + ", ".join(f"{k}={v}" for k, v in REQUIRED_KEYS) + "}"
        slot = first_code_line(code)
        return [problem(
            what=line,
            why="tagging has to be on before the document class is read",
            fix=(slot, slot, block(code, slot, [line])),
        )]

    start, end, body = found

    if body is UNPARSEABLE:
        return [problem(
            what=r"\DocumentMetadata{...} never closes",
            why="a { inside it is not closed. Fix it by hand.",
            fix=None,
        )]

    if start > where["class_start"]:
        return [problem(
            what=r"\DocumentMetadata appears after \documentclass",
            why=(r"it must come before \documentclass or it is ignored. Move it "
                 "by hand."),
            fix=None,
        )]

    if end > where["class_start"]:
        return [problem(
            what=r"\DocumentMetadata{...} closes further into the file than expected",
            why=(r"its closing } was not found before \documentclass, so a { inside "
                 "it is probably not closed. Fix it by hand."),
            fix=None,
        )]

    keys = parse_keys(body)
    wrong = [(k, v) for k, v in REQUIRED_KEYS if dict(keys).get(k) != v]
    if not wrong:
        return []

    merged = dict(keys)
    merged.update(dict(REQUIRED_KEYS))
    line = (r"\DocumentMetadata{"
            + ", ".join(k if v is None else f"{k}={v}" for k, v in merged.items())
            + "}")
    return [problem(
        what=r"\DocumentMetadata is missing " + ", ".join(f"{k}={v}" for k, v in wrong),
        why="the other keys you set are kept",
        fix=(start, end, line),
    )]


def preamble_slot(code, where, after):
    slot = min(next_line(code, after), line_start(code, where["preamble"][1]))
    return slot if slot > where["class_start"] else None


NO_ROOM = (r"\documentclass and \begin{document} share a line, so there is no "
           "line to insert on. Put them on separate lines and run this again.")


def check_packages(code, where):
    loaded = set()
    last = None
    for m in PACKAGE_RE.finditer(code, *where["preamble"]):
        loaded.update(n.strip() for n in m.group(1).split(","))
        last = m

    missing = [(name, why) for name, why in REQUIRED_PACKAGES if name not in loaded]
    if not missing:
        return []

    slot = preamble_slot(code, where, last.end() if last else where["class_end"])
    lines = [f"\\usepackage{{{name}}}" for name, _ in missing]
    if slot is None:
        return [problem(what=", ".join(lines), why=NO_ROOM, fix=None)]
    return [problem(
        what=", ".join(lines),
        why="; ".join(why for _, why in missing),
        fix=(slot, slot, block(code, slot, lines)),
    )]


def check_shim(code, where, document):
    line, relative = shim_input(document)
    if reads_shim(code, where, relative):
        return []

    slot = preamble_slot(code, where, where["class_end"])
    if slot is None:
        return [problem(what=line, why=NO_ROOM, fix=None)]
    return [problem(
        what=line,
        why=f"stage 6 writes {relative}; without this the alt text never "
            f"reaches the PDF",
        fix=(slot, slot, block(code, slot, [line])),
    )]


def reads_shim(code, where, relative):
    start, end = where["preamble"]
    for m in SHIM_INPUT_RE.finditer(code, start, end):
        if m.group(1).strip() == relative:
            return True
    return False


def problem(what, why, fix):
    return {"what": what, "why": why, "fix": fix}


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


def find_metadata(code, limit):
    m = METADATA_RE.search(code, 0, limit)
    if not m:
        return None
    i = m.end()
    while i < len(code) and code[i] in " \t\r\n":
        i += 1
    if i >= len(code) or code[i] != "{":
        return m.start(), m.end(), ""
    group = brace_group(code, i)
    if not group:
        return m.start(), len(code), UNPARSEABLE
    return m.start(), group[1], code[group[0] + 1:group[1] - 1]


def brace_group(text, i):
    if i >= len(text) or text[i] != "{":
        return None
    opened, depth = i, 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return opened, i + 1
        i += 1
    return None


def parse_keys(body):
    pairs = []
    for item in split_top_level(body):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.partition("=")
        pairs.append((key.strip(), value.strip() or None))
    return pairs


def split_top_level(body):
    parts, depth, current = [], 0, []
    for ch in body:
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


def first_code_line(text):
    m = re.search(r"\S", text)
    return line_start(text, m.start()) if m else 0


def line_start(text, off):
    return text.rfind("\n", 0, off) + 1


def next_line(text, off):
    nl = text.find("\n", off)
    return len(text) if nl < 0 else nl + 1


def newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def block(code, slot, lines):
    nl = newline_of(code)
    text = nl.join(lines) + nl
    rest = code[slot:]
    if lines[0].startswith(r"\DocumentMetadata") and rest.strip() and not rest.startswith(nl):
        text += nl
    return text


def apply(raw, fixes):
    ordered = sorted(enumerate(fixes), key=lambda pair: (pair[1][0], pair[0]),
                     reverse=True)
    for _, (start, end, text) in ordered:
        raw = raw[:start] + text + raw[end:]
    return raw


def back_up(path):
    candidate = path + ".bak"
    if not os.path.exists(candidate):
        shutil.copyfile(path, candidate)
        return candidate
    n = 1
    while os.path.exists(f"{path}.bak.{n}"):
        n += 1
    candidate = f"{path}.bak.{n}"
    shutil.copyfile(path, candidate)
    return candidate


def report(path, raw, problems):
    lines = raw.splitlines()
    shown = ["", f"  {len(problems)} thing(s) missing from {os.path.basename(path)}:", ""]
    for p in problems:
        start, end, text = p["fix"]
        at = raw.count("\n", 0, start) + 1
        old = raw[start:end].splitlines()
        new = text.splitlines()
        shown.append(f"    {p['why']}")
        if at > 1:
            shown.append(f"      {at - 1:>4} | {lines[at - 2]}")
        for line in old:
            shown.append(f"      {'':>4} - {line}")
        for line in new:
            shown.append(f"      {'':>4} + {line}")
        after = at - 1 + len(old)
        if after < len(lines):
            shown.append(f"      {after + 1:>4} | {lines[after]}")
        shown.append("")
    for line in shown:
        print(line)
    return shown


def consent(path, assume_yes, ask=None, shown=()):
    if assume_yes:
        print("  --yes given, applying")
        return True
    question = (f"add these to {os.path.basename(path)}? "
                f"the original is kept as .bak")
    if ask is not None:
        return ask(question, "\n".join(shown)) is True
    try:
        answer = input(f"  {question}  [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        print("  no input to read. Re-run with --yes to apply the changes.")
        return False
    return answer.strip().lower() in ("y", "yes")


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return fh.read()


def write(path, text, bom):
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
        fh.write(text)


def had_bom(path):
    with open(path, "rb") as fh:
        return fh.read(3) == b"\xef\xbb\xbf"
