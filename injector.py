import os
import re
import shutil
import subprocess

COMPILE_TIMEOUT_S = 120
BUILD_DIR = "build"

MAX_PASSES = 3

RERUN_PHRASES = (
    "Rerun to get",
    "Rerun LaTeX",
    "Please rerun",
    "rerunfilecheck Warning: File",
)

ALT_IN_PDF = re.compile(rb"/Alt\s*<([0-9A-Fa-f]+)>")
ALT_IN_PDF_LITERAL = re.compile(rb"/Alt\s*\(([^)]*)\)")

SHIM_HEADER = r"""\makeatletter
\providecommand{\alt@set}[2]{\expandafter\gdef\csname alt@text@#1\endcsname{#2}}
\providecommand{\alt@seq}[3]{\expandafter\gdef\csname alt@seq@#1@#2\endcsname{#3}}
\providecommand{\alt@artifact}[1]{\expandafter\gdef\csname alt@art@#1\endcsname{1}}
\providecommand{\alt@artseq}[2]{\expandafter\gdef\csname alt@arts@#1@#2\endcsname{1}}
"""

SHIM_PATCH = r"""
\def\alt@ig@start{%
  \@ifstar{\alt@ig@opt{clip,}}{\alt@ig@opt{}}%
}
\def\alt@ig@opt#1{%
  \@ifnextchar[{\alt@ig@one{#1}}{\alt@ig@one{#1}[]}%
}
\def\alt@ig@one#1[#2]{%
  \@ifnextchar[{\alt@ig@two{#1}[#2]}{\alt@ig@go{#1}[#2]}%
}
\def\alt@ig@two#1[#2][#3]#4{\alt@orig@includegraphics[#2][#3]{#4}}
\def\alt@ig@count#1{%
  \@ifundefined{alt@n@#1}%
    {\expandafter\gdef\csname alt@n@#1\endcsname{0}}{}%
  \edef\alt@i{\the\numexpr\csname alt@n@#1\endcsname+1\relax}%
  \expandafter\xdef\csname alt@n@#1\endcsname{\alt@i}%
}
\def\alt@ig@go#1[#2]#3{%
  \alt@ig@count{#3}%
  \ifcsname alt@arts@#3@\alt@i\endcsname
    \alt@orig@includegraphics[#1#2,artifact]{#3}%
  \else
  \ifcsname alt@art@#3\endcsname
    \alt@orig@includegraphics[#1#2,artifact]{#3}%
  \else
  \ifcsname alt@seq@#3@\alt@i\endcsname
    \edef\alt@this{\csname alt@seq@#3@\alt@i\endcsname}%
    \expandafter\alt@orig@includegraphics\expandafter[alt={\alt@this},#1#2]{#3}%
  \else
  \ifcsname alt@text@#3\endcsname
    \edef\alt@this{\csname alt@text@#3\endcsname}%
    \expandafter\alt@orig@includegraphics\expandafter[alt={\alt@this},#1#2]{#3}%
  \else
    \alt@orig@includegraphics[#1#2]{#3}%
  \fi\fi\fi\fi
}
\AtBeginDocument{%
  \@ifundefined{includegraphics}{}{%
    \let\alt@orig@includegraphics\includegraphics
    \let\includegraphics\alt@ig@start
  }%
}
\makeatother
"""


def inject(state):
    document = state["document"]
    resolved = state.get("resolved") or []
    warnings = []

    entries = collect(state, warnings)
    shim_path = write_shim(document, entries)

    if not shutil.which("pdflatex"):
        state["injected"] = {"shim": shim_path, "pdf": None, "entries": entries}
        return f"wrote the shim for {len(entries)} figure(s); pdflatex not on PATH"

    pdf, errors = compile_document(document, state.get("tex_files") or [])
    if errors and pdf is not None:
        for line in errors[:5]:
            warnings.append(f"the tagged build reported: {line}")
        if len(errors) > 5:
            warnings.append(f"...and {len(errors) - 5} more LaTeX error(s); see "
                            f"{os.path.join(BUILD_DIR, os.path.splitext(os.path.basename(document))[0] + '.log')}")
    if pdf is None:
        state["stop"] = True
        print(f"  the tagged build failed:")
        for line in errors[:5]:
            print(f"    {line}")
        state["injected"] = {"shim": shim_path, "pdf": None, "entries": entries}
        return "the document did not compile with the shim in place"

    found = alt_texts(pdf)
    state["injected"] = {"shim": shim_path, "pdf": pdf, "entries": entries,
                         "in_pdf": found}

    fallbacks = [t for t in found if looks_like_filename(t)]
    real = [t for t in found if t not in fallbacks]
    for text in fallbacks:
        warnings.append(
            f"the PDF has {text!r} as alt text, which is the filename tagpdf "
            f"falls back to when no alt text is given")

    state["warnings"] = state.get("warnings", []) + warnings
    if state.get("verbose"):
        for w in warnings:
            print(f"  warning: {w}")

    artifacts = sum(1 for e in entries if e["kind"] == "artifact")
    parts = [f"{len(real)} tagged", f"{artifacts} artifact"]
    if fallbacks:
        parts.append(f"{len(fallbacks)} filename fallback")
    line = ", ".join(parts) + f" in {os.path.basename(pdf)}"
    if warnings and not state.get("verbose"):
        line += f", {len(warnings)} warning(s)"
    return line


def looks_like_filename(text):
    stripped = text.strip()
    if " " in stripped or len(stripped) > 120:
        return False
    return bool(re.fullmatch(r"[\w./\\-]+\.\w{2,4}", stripped))


def collect(state, warnings):
    resolved = state.get("resolved") or []
    entries = []
    occurrence, total = {}, {}

    for r in resolved:
        filename = r.get("source")
        if r.get("kind") != "tikz" and filename:
            total[filename] = total.get(filename, 0) + 1

    for r in resolved:
        key, outcome = r["key"], r["outcome"]
        filename = r.get("source")

        index = None
        if r.get("kind") != "tikz" and filename:
            occurrence[filename] = occurrence.get(filename, 0) + 1
            index = occurrence[filename]

        if outcome == "inline":
            continue
        if outcome == "blocked":
            continue

        if r.get("kind") == "tikz":
            if outcome != "artifact":
                warnings.append(
                    f"{key}: its alt text cannot be injected yet, because it is an "
                    f"inline tikzpicture and the shim works by filename. The text "
                    f"is in the .alt file and stage 7 will report the gap")
            continue

        if not filename:
            continue

        if outcome == "artifact":
            entries.append({"kind": "artifact", "key": key, "file": filename,
                            "index": index, "shared": total[filename] > 1})
            if total[filename] > 1:
                warnings.append(
                    f"{key}: {filename!r} is used by more than one figure and this "
                    f"one is decorative; only this use is excluded from tagging")
            continue

        alt = text_for(state, r)
        if not alt:
            continue
        entries.append({"kind": "alt", "key": key, "file": filename, "alt": alt,
                        "index": index, "shared": total[filename] > 1})

    return entries


def text_for(state, resolution):
    for d in state.get("described") or []:
        if d.get("key") == resolution["key"] and d.get("ok") and d.get("alt"):
            return d["alt"]
    return resolution.get("alt")


def write_shim(document, entries):
    folder = os.path.join(os.path.dirname(document) or ".", "_alt")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(document))[0]
    path = os.path.join(folder, stem + ".alt.tex")

    lines = [SHIM_HEADER]
    for entry in entries:
        if entry["kind"] == "artifact":
            if entry["shared"]:
                lines.append(f"\\alt@artseq{{{entry['file']}}}{{{entry['index']}}}")
            else:
                lines.append(f"\\alt@artifact{{{entry['file']}}}")
        elif entry["shared"]:
            lines.append(f"\\alt@seq{{{entry['file']}}}{{{entry['index']}}}"
                         f"{{{escape(one_line(entry['alt']))}}}")
        else:
            lines.append(f"\\alt@set{{{entry['file']}}}"
                         f"{{{escape(one_line(entry['alt']))}}}")
    lines.append(SHIM_PATCH)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "#": r"\#",
    "$": r"\textdollar{}",
    "%": r"\%",
    "&": r"\&",
    "_": r"\textunderscore{}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def one_line(text):
    return " ".join((text or "").split())


def escape(text):
    out = []
    for ch in text:
        out.append(ESCAPES.get(ch, ch))
    return "".join(out)


def compile_document(document, tex_files=()):
    folder = os.path.dirname(os.path.abspath(document))
    name = os.path.basename(document)
    stem = os.path.splitext(name)[0]
    build = os.path.join(folder, BUILD_DIR)
    os.makedirs(build, exist_ok=True)

    for rel in tex_files:
        sub_dir = os.path.dirname(rel)
        if sub_dir:
            os.makedirs(os.path.join(build, sub_dir), exist_ok=True)

    pdf = os.path.join(build, stem + ".pdf")
    log = os.path.join(build, stem + ".log")
    errors = []

    for _ in range(MAX_PASSES):
        try:
            subprocess.run(
                ["pdflatex", "-interaction=nonstopmode",
                 f"-output-directory={build}", name],
                cwd=folder, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return None, [f"pdflatex did not finish within {COMPILE_TIMEOUT_S}s"]
        except OSError as exc:
            return None, [f"could not run pdflatex ({exc})"]

        text = ""
        if os.path.isfile(log):
            text = open(log, encoding="utf-8", errors="replace").read()
        errors = [ln.strip() for ln in text.splitlines() if ln.startswith("!")]
        if not wants_rerun(text):
            break

    if not os.path.isfile(pdf):
        return None, errors or ["pdflatex produced no PDF"]
    return pdf, errors


def wants_rerun(log_text):
    return any(phrase in log_text for phrase in RERUN_PHRASES)


def alt_texts(pdf):
    try:
        data = open(pdf, "rb").read()
    except OSError:
        return []
    out = []
    for match in ALT_IN_PDF.finditer(data):
        try:
            out.append(bytes.fromhex(match.group(1).decode())
                       .decode("utf-16-be").lstrip("\ufeff"))
        except (ValueError, UnicodeDecodeError):
            continue
    for match in ALT_IN_PDF_LITERAL.finditer(data):
        out.append(match.group(1).decode("latin-1"))
    return out
