import os
import re
import shutil
import subprocess

import injector

VERAPDF_TIMEOUT_S = 180

PDF_OBJECT = re.compile(rb"\d+\s+0\s+obj(.*?)endobj", re.DOTALL)

IS_FIGURE = re.compile(rb"/S\s*/Figure\b")
HAS_ALT = re.compile(rb"/Alt\s*[<(]")
HAS_ACTUALTEXT = re.compile(rb"/ActualText\s*[<(]")
ALT_HEX = re.compile(rb"/Alt\s*<([0-9A-Fa-f]+)>")
ALT_LITERAL = re.compile(rb"/Alt\s*\(([^)]*)\)")


def validate(state):
    injected = state.get("injected") or {}
    pdf = injected.get("pdf")

    if not pdf or not os.path.isfile(pdf):
        state["validation"] = {"ok": False, "reason": "no PDF to check"}
        return "no PDF was produced, so there is nothing to validate"

    figures = read_figures(pdf)

    unreached = missed_figures(state, figures)

    if not figures:
        if not (state.get("figures") or []) and not unreached:
            state["validation"] = {"ok": True, "figures": 0, "good": 0,
                                   "missing": 0, "useless": 0, "unreached": [],
                                   "verapdf": None}
            return "PASS: the document has no figures to tag"

        drawn = tikz_keys(state)
        blocking = [k for k in unreached if k not in drawn]
        limits = [f"{k}: its alt text is in the .alt store, but the kernel does "
                  f"not tag an inline tikzpicture as a Figure, so there is "
                  f"nothing in the PDF to attach it to. A known limitation of "
                  f"this pipeline" for k in unreached if k in drawn]
        for key in blocking:
            limits.append(f"{key}: has alt text in the .alt store, but the PDF "
                          f"has no tagged figures at all")

        explained = bool(limits) and not blocking
        state["validation"] = {"ok": explained, "figures": 0, "good": 0,
                               "missing": 0, "useless": 0, "unreached": unreached,
                               "verapdf": None, "blocking": blocking,
                               "limits": limits,
                               "reason": "no Figure structure found"}
        state["warnings"] = state.get("warnings", []) + limits
        if state.get("verbose"):
            for w in limits:
                print(f"  note: {w}")
        if blocking:
            state["stop"] = True
            return (f"FAIL: no tagged figures in {os.path.basename(pdf)}, and "
                    f"{len(blocking)} figure(s) in the store never reached it")
        if limits:
            return (f"PASS: nothing in {os.path.basename(pdf)} could be tagged, "
                    f"{len(limits)} limitation(s) noted")
        state["stop"] = True
        return (f"found no tagged figures in {os.path.basename(pdf)}; either "
                f"tagging is off, or the PDF stores its structure in a way "
                f"this stage cannot read")

    missing = [f for f in figures if not f["tagged"]]
    useless = [f for f in figures if f["tagged"] and is_filename(f["text"])]
    good = [f for f in figures if f["tagged"] and not is_filename(f["text"])]

    blocking, limits = [], []

    for f in missing:
        if f.get("empty"):
            blocking.append("a Figure's Alt entry is present but empty, so a "
                            "screen reader reads nothing")
        else:
            blocking.append("a Figure has no Alt or ActualText; veraPDF counts "
                            "this as a PDF/UA-2 failure")

    absent = absent_sources(state)
    for f in useless:
        if f["text"] in absent:
            limits.append(
                f"a Figure shows {f['text']!r} as its alt text because that file "
                f"is not on disk. Add the file and it will be described")
        else:
            blocking.append(
                f"a Figure's alt text is {f['text']!r}, which is the filename "
                f"tagpdf uses when no alt text was given. It passes a rule "
                f"checker but tells a reader nothing")

    for f in good:
        if is_placeholder(f["text"]):
            limits.append(
                f"a Figure's alt text is placeholder text from an --offline run "
                f"({f['text'][:60]!r}). Run again without --offline before "
                f"sharing this PDF")

    drawn = tikz_keys(state)
    for key in unreached:
        if key in drawn:
            limits.append(
                f"{key}: its alt text is in the .alt store, but the kernel does "
                f"not tag an inline tikzpicture as a Figure, so there is nothing "
                f"in the PDF to attach it to. A known limitation of this pipeline")
        else:
            blocking.append(f"{key}: has alt text in the .alt store, but no "
                            f"tagged figure with that text is in the PDF")

    warnings = list(blocking)

    vera = run_verapdf(pdf)

    if vera is not None and vera.get("unreadable"):
        limits.append(f"veraPDF ran but its report could not be read "
                      f"({vera.get('detail')}), so the PDF/UA-2 check was not "
                      f"applied")

    vera_alt_failures = (vera or {}).get("about_alt") or []
    for rule in vera_alt_failures:
        blocking.append(f"veraPDF {rule['clause']}: {rule['description']} "
                        f"({rule['checks']} failed check(s))")
    for rule in (vera or {}).get("failed") or []:
        if not rule["about_alt"]:
            limits.append(
                f"veraPDF {rule['clause']}: {rule['description']} "
                f"({rule['checks']} failed check(s)); not about alt text")

    ok = not blocking

    state["validation"] = {
        "ok": ok, "figures": len(figures), "good": len(good),
        "missing": len(missing), "useless": len(useless),
        "unreached": unreached, "verapdf": vera,
        "blocking": blocking, "limits": limits,
    }
    state["warnings"] = state.get("warnings", []) + blocking + limits
    if state.get("verbose"):
        for w in blocking:
            print(f"  warning: {w}")
        for w in limits:
            print(f"  note: {w}")
    if not ok:
        state["stop"] = True

    return summarise(figures, good, blocking, limits, vera,
                     state.get("verbose"))


def read_figures(pdf):
    try:
        data = open(pdf, "rb").read()
    except OSError:
        return []

    figures = []
    for match in PDF_OBJECT.finditer(data):
        body = match.group(1)
        if not IS_FIGURE.search(body):
            continue
        has_alt = bool(HAS_ALT.search(body))
        has_actual = bool(HAS_ACTUALTEXT.search(body))
        text = alt_of(body)
        empty = has_alt and not has_actual and not text.strip()
        figures.append({"tagged": (has_alt or has_actual) and not empty,
                        "empty": empty, "text": text})
    return figures


def alt_of(body):
    hexed = ALT_HEX.search(body)
    if hexed:
        try:
            return (bytes.fromhex(hexed.group(1).decode())
                    .decode("utf-16-be").lstrip("\ufeff"))
        except (ValueError, UnicodeDecodeError):
            return ""
    literal = ALT_LITERAL.search(body)
    if literal:
        return literal.group(1).decode("latin-1")
    return ""


def is_filename(text):
    return injector.looks_like_filename(text or "")


PLACEHOLDER = "[stub]"


def is_placeholder(text):
    return (text or "").strip().startswith(PLACEHOLDER)


def absent_sources(state):
    return {r.get("source") for r in state.get("resolved") or []
            if r.get("outcome") == "blocked" and r.get("source")}


def tikz_keys(state):
    return {r["key"] for r in state.get("resolved") or []
            if r.get("kind") == "tikz"}


def same_text(text):
    return " ".join((text or "").split())


def missed_figures(state, figures):
    in_pdf = {same_text(f["text"]) for f in figures if f["tagged"]}
    described = state.get("described") or []
    missed = []
    for r in state.get("resolved") or []:
        if r["outcome"] in ("artifact", "blocked"):
            continue

        text = None
        for d in described:
            if d.get("key") == r["key"] and d.get("ok"):
                text = d.get("alt")
        if text is None:
            if r["outcome"] == "describe":
                continue
            text = r.get("alt")

        if text and same_text(text) not in in_pdf:
            missed.append(r["key"])
    return missed


def find_verapdf():
    for name in ("verapdf", "verapdf.bat", "verapdf.exe"):
        found = shutil.which(name)
        if found:
            return found

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "verapdf"), os.path.join(home, "veraPDF"),
        r"C:\Program Files\veraPDF", r"C:\Program Files (x86)\veraPDF",
        "/opt/verapdf", "/usr/local/verapdf",
    ]
    for folder in candidates:
        for name in ("verapdf.bat", "verapdf"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return None


ALT_WORDS = ("alternate description", "alternative description", "alt entry",
             "alternate text", "alternative text", "figure")

FAILED_RULE = re.compile(
    r'<rule[^>]*clause="([^"]*)"[^>]*testNumber="([^"]*)"[^>]*status="failed"'
    r'[^>]*failedChecks="(\d+)"(.*?)</rule>', re.S)
DESCRIPTION = re.compile(r"<description>(.*?)</description>", re.S)


def run_verapdf(pdf):
    engine = find_verapdf()
    if not engine:
        return None
    try:
        done = subprocess.run([engine, "--flavour", "ua2", pdf],
                              capture_output=True, text=True,
                              timeout=VERAPDF_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None

    report = done.stdout or ""

    if "isCompliant" not in report:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return {"compliant": False, "failed": [], "about_alt": [],
                "engine": engine, "unreadable": True,
                "detail": detail[0][:160] if detail else "no output"}

    failed = []
    for clause, test, count, body in FAILED_RULE.findall(report):
        described = DESCRIPTION.search(body)
        text = re.sub(r"\s+", " ", described.group(1)).strip() if described else ""
        failed.append({"clause": clause, "test": test, "checks": int(count),
                       "description": text,
                       "about_alt": any(w in text.lower() for w in ALT_WORDS)})

    return {
        "compliant": 'isCompliant="true"' in report,
        "failed": failed,
        "about_alt": [f for f in failed if f["about_alt"]],
        "engine": engine,
    }


def summarise(figures, good, blocking, limits, vera, verbose):
    parts = [f"{len(figures)} tagged figure(s)", f"{len(good)} with real alt text"]
    if blocking:
        parts.append(f"{len(blocking)} problem(s)")

    if vera is not None and vera.get("unreadable"):
        parts.append("veraPDF: report could not be read, so nothing was checked")
    elif vera is not None:
        alt_failures = vera.get("about_alt") or []
        if vera["compliant"]:
            parts.append("veraPDF: compliant")
        elif alt_failures:
            parts.append(f"veraPDF: {len(alt_failures)} alt-text rule(s) failed")
        else:
            parts.append(f"veraPDF: {len(vera['failed'])} rule(s) failed, "
                         f"none about alt text")

    line = ("PASS" if not blocking else "FAIL") + ": " + ", ".join(parts)
    if vera is None:
        line += " (veraPDF not installed)"
    if limits:
        line += f", {len(limits)} limitation(s) noted"
        if not verbose:
            line += " (see --verbose)"
    return line
