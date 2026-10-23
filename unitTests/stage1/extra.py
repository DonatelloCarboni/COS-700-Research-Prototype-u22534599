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

import scanner

PROJECT = project_dir()
PIXEL = os.path.join(HERE, "figures", "example.png")


def report(label, problems, note=None):
    print(f"{'FAIL' if problems else 'ok  '}  {label}")
    if note:
        print(f"        -> {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def project(files, images=("example.png",), config=True):
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "figures"), exist_ok=True)
    for name in images:
        shutil.copyfile(PIXEL, os.path.join(work, "figures", name))
    if config:
        shutil.copyfile(os.path.join(PROJECT, "alt.config.json"),
                        os.path.join(work, "alt.config.json"))
    for relative, text in files.items():
        full = os.path.join(work, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    return work


def scan(work, name="main.tex", state=None):
    state = dict(state or {})
    state["document"] = os.path.join(work, name)
    with contextlib.redirect_stdout(io.StringIO()):
        scanner.scan(state)
    return state


HEAD = r"""\DocumentMetadata{tagging=on, pdfstandard=ua-2}
\documentclass{article}
\usepackage{graphicx}
\usepackage{tikz}
\graphicspath{{figures/}}
\begin{document}
"""


def float_of(name, label=None, caption="A figure."):
    line = "\\begin{figure}[h]\n  \\centering\n"
    line += f"  \\includegraphics[width=0.3\\linewidth]{{{name}}}\n"
    line += f"  \\caption{{{caption}}}\n"
    if label:
        line += f"  \\label{{{label}}}\n"
    return line + "\\end{figure}\n"


def check_order_within_one_file():
    body = (HEAD
            + "%!alt chart\n"
            + "\\noindent\\includegraphics[width=0.2\\linewidth]{example.png}\n\n"
            + float_of("example.png", "fig:float")
            + "\\end{document}\n")
    work = project({"main.tex": body})
    keys = [f["key"] for f in scan(work)["figures"]]
    problems = []
    if keys != ["example.png", "fig:float"]:
        problems.append(f"got {keys}, expected the loose image first "
                        f"(it is written above the float)")
    shutil.rmtree(work, ignore_errors=True)
    return report("marked image above a float comes first", problems)


def check_order_across_inputs():
    main = (HEAD
            + float_of("example.png", "fig:before")
            + "\\input{chapters/mid}\n"
            + float_of("example.png", "fig:after")
            + "\\end{document}\n")
    chapter = "\\section{Middle}\n" + float_of("example.png", "fig:inside")
    work = project({"main.tex": main, "chapters/mid.tex": chapter})
    keys = [f["key"] for f in scan(work)["figures"]]
    problems = []
    if keys != ["fig:before", "fig:inside", "fig:after"]:
        problems.append(f"got {keys}, expected "
                        f"['fig:before', 'fig:inside', 'fig:after']")
    shutil.rmtree(work, ignore_errors=True)
    return report("chapter figures sort by \\input position",
                  problems)


def check_order_nested():
    main = (HEAD + float_of("example.png", "fig:a")
            + "\\input{chapters/one}\n"
            + float_of("example.png", "fig:d") + "\\end{document}\n")
    one = (float_of("example.png", "fig:b") + "\\input{chapters/two}\n")
    two = float_of("example.png", "fig:c")
    work = project({"main.tex": main, "chapters/one.tex": one,
                    "chapters/two.tex": two})
    keys = [f["key"] for f in scan(work)["figures"]]
    problems = []
    if keys != ["fig:a", "fig:b", "fig:c", "fig:d"]:
        problems.append(f"got {keys}, expected a, b, c, d")
    shutil.rmtree(work, ignore_errors=True)
    return report("nested inputs sort correctly", problems)


def check_many_collisions():
    figures = [{"key": "a.png", "where": "f.tex:10"} for _ in range(5)]
    warnings = []
    scanner.unique_keys(figures, warnings)
    keys = [f["key"] for f in figures]
    problems = []
    if len(set(keys)) != 5:
        problems.append(f"only {len(set(keys))} of 5 keys are distinct: {keys}")
    if len(warnings) != 4:
        problems.append(f"{len(warnings)} collision(s) reported, expected 4")
    return report("five equal keys become unique", problems,
                  note=", ".join(keys))


def check_images_on_one_line():
    body = (HEAD + "\\begin{figure}[h]\n"
            "  \\includegraphics{example.png}\\includegraphics{example.png}"
            "\\includegraphics{example.png}\n"
            "  \\caption{Three panels.}\n\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body})
    keys = [f["key"] for f in scan(work)["figures"]]
    problems = []
    if len(keys) != 3:
        problems.append(f"found {len(keys)} figure(s), expected 3")
    if len(set(keys)) != len(keys):
        problems.append(f"keys collide: {keys}")
    shutil.rmtree(work, ignore_errors=True)
    return report("three images on one line", problems,
                  note=", ".join(keys))


def check_folder_name_ignored():
    config = {"profiles": {"chart": {"match": ["chart", "plot"]},
                           "artifact": {"match": ["rule", "divider"],
                                        "from_caption": False}},
              "fallback": "figure"}
    cases = [("plots/leaf-specimen.jpg", "figure"),
             ("rules/architecture.png", "figure"),
             ("figures/accuracy-chart.pdf", "chart"),
             ("figures/divider.png", "artifact")]
    problems = []
    for name, want in cases:
        got = scanner.guess_profile(name, None, None, config)
        if got != want:
            problems.append(f"{name} -> {got!r}, expected {want!r}")
    return report("folder name does not set the profile", problems)


def check_guessed_decorative():
    body = (HEAD + "%!alt chart\n"
            + "\\noindent\\includegraphics{keep.png}\n\n"
            + "\\begin{figure}[h]\n  \\includegraphics{divider.png}\n"
            + "\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body}, images=("keep.png", "divider.png"))
    state = scan(work)
    problems = []
    decorative = [f for f in state["figures"] if f["profile"] == "artifact"]
    if len(decorative) != 1:
        problems.append(f"{len(decorative)} figure(s) called decorative, expected 1")
    if not any("guessed decorative" in w for w in state["warnings"]):
        problems.append(f"the guess was not announced: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("guessed decorative profile is reported",
                  problems)


def check_declared_decorative():
    body = (HEAD + "%!alt artifact\n"
            + "\\noindent\\includegraphics{plain.png}\n\n\\end{document}\n")
    work = project({"main.tex": body}, images=("plain.png",))
    state = scan(work)
    problems = []
    if state["figures"][0]["profile"] != "artifact":
        problems.append(f"the directive was not honoured: "
                        f"{state['figures'][0]['profile']!r}")
    if any("guessed decorative" in w for w in state["warnings"]):
        problems.append("a declared artifact was reported as a guess")
    shutil.rmtree(work, ignore_errors=True)
    return report("declared artifact gives no warning", problems)


def check_unknown_profile():
    body = (HEAD + "%!alt nonsense\n"
            + "\\noindent\\includegraphics{example.png}\n\n\\end{document}\n")
    work = project({"main.tex": body})
    state = scan(work)
    problems = []
    if state["figures"] and state["figures"][0]["profile"] == "nonsense":
        problems.append("an undefined profile was used")
    if not any("is not a profile" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("unknown %!alt profile is reported",
                  problems)


def check_directive_in_float():
    body = (HEAD + "\\begin{figure}[h]\n  \\centering\n  %!alt chart\n"
            "  \\includegraphics{results.png}\n"
            "  \\caption{The proportion of the analysed sample over time.}\n"
            "\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body}, images=("results.png",))
    state = scan(work)
    problems = []
    got = [f["profile"] for f in state["figures"]]
    if got != ["chart"]:
        problems.append(f"profiles {got}, expected ['chart'] from the directive")
    if any("applies to nothing" in w for w in state["warnings"]):
        problems.append("a directive that was applied was also reported as stray")
    shutil.rmtree(work, ignore_errors=True)
    return report("%!alt inside a float is used", problems)


def check_image_directive_priority():
    body = (HEAD + "%!alt diagram\n\\begin{figure}[h]\n"
            "  \\includegraphics{left.png}\n"
            "  %!alt photo\n"
            "  \\includegraphics{right.png}\n"
            "  \\caption{Both.}\n\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body}, images=("left.png", "right.png"))
    state = scan(work)
    problems = []
    got = [f["profile"] for f in state["figures"]]
    if got != ["diagram", "photo"]:
        problems.append(f"profiles {got}, expected ['diagram', 'photo']")
    if any("applies to nothing" in w for w in state["warnings"]):
        problems.append(f"an applied directive was reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("image %!alt overrides the float's", problems)


def check_stray_directive():
    body = (HEAD + "%!alt chart\n\n"
            + float_of("results.png", caption="A figure.")
            + "Some prose.\n%!alt photo\nMore prose.\n\\end{document}\n")
    work = project({"main.tex": body}, images=("results.png",))
    state = scan(work)
    stray = [w for w in state["warnings"] if "applies to nothing" in w]
    problems = []
    if len(stray) != 2:
        problems.append(f"{len(stray)} stray directive(s) reported, expected 2: "
                        f"{state['warnings']}")
    if stray and not stray[0].startswith("main.tex:7:"):
        problems.append(f"the first report does not name its line: {stray[0]!r}")
    if [f["profile"] for f in state["figures"]] == ["chart"]:
        problems.append("a directive separated by a blank line was applied anyway")
    shutil.rmtree(work, ignore_errors=True)
    return report("stray %!alt is reported",
                  problems)


def check_directive_in_verbatim():
    body = (HEAD + "\\begin{verbatim}\n%!alt chart\n"
            "\\includegraphics{results.png}\n\\end{verbatim}\n\\end{document}\n")
    work = project({"main.tex": body}, images=("results.png",))
    state = scan(work)
    problems = []
    if state["figures"]:
        problems.append(f"verbatim text was scanned as a figure: {state['figures']}")
    if any("applies to nothing" in w for w in state["warnings"]):
        problems.append("a %!alt inside verbatim was reported as a stray directive")
    shutil.rmtree(work, ignore_errors=True)
    return report("%!alt in verbatim is ignored",
                  problems)


def check_float_directive_once():
    body = (HEAD + "%!alt nonsense\n"
            "\\begin{figure}[h]\\includegraphics{results.png}\\end{figure}\n"
            "\\end{document}\n")
    work = project({"main.tex": body}, images=("results.png",))
    state = scan(work)
    refused = [w for w in state["warnings"] if "is not a profile" in w]
    problems = []
    if len(refused) != 1:
        problems.append(f"{len(refused)} report(s) of the unknown profile, "
                        f"expected 1: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("float %!alt is read once",
                  problems)


def check_chapter_bom():
    main = HEAD + "\\input{chapters/one}\n\\end{document}\n"
    chapter = float_of("example.png", "fig:bom")
    work = project({"main.tex": main, "chapters/one.tex": ""})
    with open(os.path.join(work, "chapters", "one.tex"), "w",
              encoding="utf-8-sig") as fh:
        fh.write(chapter)
    state = scan(work)
    keys = [f["key"] for f in state["figures"]]
    problems = []
    if "fig:bom" not in keys:
        problems.append(f"the chapter's figure was not found: {keys}")
    shutil.rmtree(work, ignore_errors=True)
    return report("chapter with a BOM is read", problems)


def check_figure_star():
    body = (HEAD + "\\begin{figure*}[t]\n  \\includegraphics{example.png}\n"
            "  \\caption{Wide.}\n  \\label{fig:wide}\n\\end{figure*}\n"
            "\\end{document}\n")
    work = project({"main.tex": body})
    keys = [f["key"] for f in scan(work)["figures"]]
    shutil.rmtree(work, ignore_errors=True)
    return report("figure* is scanned",
                  [] if keys == ["fig:wide"] else [f"got {keys}"])


def check_caption_parsing():
    body = (HEAD + "\\begin{figure}[h]\n  \\includegraphics{example.png}\n"
            "  \\caption[Short]{The \\textbf{long} one, at 94\\% accuracy}\n"
            "  \\label{fig:cap}\n\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body})
    figures = scan(work)["figures"]
    problems = []
    caption = figures[0]["caption"] if figures else None
    if caption != "The long one, at 94% accuracy":
        problems.append(f"caption read as {caption!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("optional caption argument and markup",
                  problems, note=caption)


def check_graphicspath_two_roots():
    main = (HEAD.replace("{{figures/}}", "{{figures/}{plates/}}")
            + float_of("in-plates.png", "fig:plates") + "\\end{document}\n")
    work = project({"main.tex": main})
    os.makedirs(os.path.join(work, "plates"), exist_ok=True)
    shutil.copyfile(PIXEL, os.path.join(work, "plates", "in-plates.png"))
    figures = scan(work)["figures"]
    problems = []
    if not figures:
        problems.append("no figure found at all")
    elif figures[0]["hash"] is None:
        problems.append("the second graphicspath root was not searched")
    shutil.rmtree(work, ignore_errors=True)
    return report("two graphicspath roots are searched", problems)


def check_verbatim_and_comments():
    body = (HEAD + "\\begin{verbatim}\n\\begin{figure}\n"
            "  \\includegraphics{ghost.pdf}\n\\end{figure}\n\\end{verbatim}\n\n"
            "% \\includegraphics{commented.png}\n\n"
            "\\begin{lstlisting}\n\\includegraphics{listed.png}\n"
            "\\end{lstlisting}\n\\end{document}\n")
    work = project({"main.tex": body})
    figures = scan(work)["figures"]
    shutil.rmtree(work, ignore_errors=True)
    return report("verbatim and commented figures are ignored",
                  [f"found {[f['key'] for f in figures]}"] if figures else [])


def check_empty_float():
    body = (HEAD + "\\begin{figure}[h]\n  \\caption{A table, in a figure float.}\n"
            "  \\label{fig:empty}\n\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body})
    state = scan(work)
    problems = []
    if state["figures"]:
        problems.append(f"reported a figure with no image: {state['figures']}")
    if not any("holds no image" in w for w in state["warnings"]):
        problems.append(f"it was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("float with no image is reported", problems)


def check_other_float_types():
    body = (HEAD + "\\begin{wrapfigure}{r}{4cm}\n"
            "  \\includegraphics{example.png}\n  \\caption{Wrapped.}\n"
            "\\end{wrapfigure}\n\\end{document}\n")
    work = project({"main.tex": body})
    state = scan(work)
    problems = []
    if not any("wrapfigure" in w for w in state["warnings"]):
        problems.append(f"the gap was not reported: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("unsupported float types are reported", problems)


def check_missing_config():
    work = project({"main.tex": HEAD + float_of("accuracy-chart.png", "fig:c")
                    + "\\end{document}\n"},
                   images=("accuracy-chart.png",), config=False)
    state = {"document": os.path.join(work, "main.tex")}
    config, warnings = scanner.load_config(state["document"])
    problems = []
    if config["path"] is None:
        if config["fallback"] != "figure":
            problems.append(f"fallback is {config['fallback']!r}")
        if not warnings:
            problems.append("a missing config was not reported")
    else:
        if not os.path.isfile(config["path"]):
            problems.append(f"reported a config at {config['path']!r}")
    shutil.rmtree(work, ignore_errors=True)
    return report("missing config uses the fallback profile", problems)


def check_unreadable_config():
    work = project({"main.tex": HEAD + "\\end{document}\n"})
    with open(os.path.join(work, "alt.config.json"), "w", encoding="utf-8") as fh:
        fh.write('{"profiles": {,,,}')
    config, warnings = scanner.load_config(os.path.join(work, "main.tex"))
    problems = []
    if not warnings:
        problems.append("an unreadable config was not reported")
    if config["profiles"]:
        problems.append("profiles were read out of a file that could not be parsed")
    shutil.rmtree(work, ignore_errors=True)
    return report("unreadable config is reported", problems)


def check_warnings_appended():
    work = project({"main.tex": HEAD + "\\end{document}\n"})
    state = scan(work, state={"warnings": ["something stage 0 said"]})
    problems = []
    if "something stage 0 said" not in state["warnings"]:
        problems.append(f"an existing warning was discarded: {state['warnings']}")
    shutil.rmtree(work, ignore_errors=True)
    return report("earlier warnings are kept",
                  problems)


def check_config_in_state():
    work = project({"main.tex": HEAD + "\\end{document}\n"})
    state = scan(work)
    problems = []
    config = state.get("config")
    if not config:
        problems.append("no config was left in the state")
    elif "chart" not in (config.get("profiles") or {}):
        problems.append(f"the config carried is not the project's: {config}")
    shutil.rmtree(work, ignore_errors=True)
    return report("config is stored in the state", problems)


def check_subfigure_labels():
    body = (HEAD + "\\begin{figure}[h]\n"
            "  \\begin{subfigure}{0.45\\linewidth}\n"
            "    \\includegraphics{left.png}\\caption{Left}\\label{fig:l}\n"
            "  \\end{subfigure}\n"
            "  \\begin{subfigure}{0.45\\linewidth}\n"
            "    \\includegraphics{right.png}\\caption{Right}\\label{fig:r}\n"
            "  \\end{subfigure}\n"
            "  \\caption{Both.}\\label{fig:both}\n\\end{figure}\n\\end{document}\n")
    work = project({"main.tex": body}, images=("left.png", "right.png"))
    figures = scan(work)["figures"]
    keys = [f["key"] for f in figures]
    captions = [f["caption"] for f in figures]
    problems = []
    if keys != ["fig:l", "fig:r"]:
        problems.append(f"keys {keys}, expected ['fig:l', 'fig:r']")
    if captions != ["Left", "Right"]:
        problems.append(f"captions {captions}, expected the panels' own")
    shutil.rmtree(work, ignore_errors=True)
    return report("subfigures keep their labels and captions", problems)


def check_inline_alt_brackets():
    body = (HEAD + "\\begin{figure}[h]\n"
            "  \\includegraphics[width=2cm, alt={See [1], then figure 2}]"
            "{example.png}\n"
            "  \\caption{A chart.}\\label{fig:inline}\n\\end{figure}\n"
            "\\end{document}\n")
    work = project({"main.tex": body})
    figures = scan(work)["figures"]
    got = figures[0]["alt"] if figures else None
    shutil.rmtree(work, ignore_errors=True)
    return report("inline alt= with brackets and commas",
                  [] if got == "See [1], then figure 2" else [f"got {got!r}"],
                  note=got)


CHECKS = [
    check_order_within_one_file,
    check_order_across_inputs,
    check_order_nested,
    check_many_collisions,
    check_images_on_one_line,
    check_folder_name_ignored,
    check_guessed_decorative,
    check_declared_decorative,
    check_unknown_profile,
    check_directive_in_float,
    check_image_directive_priority,
    check_stray_directive,
    check_directive_in_verbatim,
    check_float_directive_once,
    check_chapter_bom,
    check_figure_star,
    check_caption_parsing,
    check_graphicspath_two_roots,
    check_verbatim_and_comments,
    check_empty_float,
    check_other_float_types,
    check_missing_config,
    check_unreadable_config,
    check_warnings_appended,
    check_config_in_state,
    check_subfigure_labels,
    check_inline_alt_brackets,
]


def run():
    return [check() for check in CHECKS]
