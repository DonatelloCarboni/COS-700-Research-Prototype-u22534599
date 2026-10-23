# Seeing for the Reader

Prototype for the COS700 research project *Seeing for the Reader: Multimodal
LLMs as a Bridge Between LaTeX Figures and PDF/UA*, University of Pretoria.

The pipeline adds alternative text to the figures in a LaTeX document and
builds a tagged PDF that targets PDF/UA-2. A vision model writes a first
description of each figure, and the text is stored in a `.alt` file beside the
document, where it can be edited or approved by hand. Text written or approved
by a person is never overwritten, and a figure is only sent to the model again
when it changes.

Student number: u22534599

## Requirements

- Python 3.8 or newer (standard library only; tested with 3.13)
- A LaTeX distribution with `pdflatex` and a 2024 or newer LaTeX kernel
  (TeX Live or MiKTeX)
- `pdftoppm` from Poppler, used to turn PDF figures and TikZ pictures into
  images
- `epstopdf`, for EPS figures (included with TeX Live and MiKTeX)
- [veraPDF](https://verapdf.org/) (optional), for the PDF/UA-2 check
- A Gemini API key, unless the pipeline is run with `--offline`

## Setup

Set the API key in the environment, or copy `.env.example` to `.env` beside
your document and fill it in:

```
GEMINI_API_KEY=your-key-here
```

## Usage

Run the pipeline on the main `.tex` file of a document:

```bash
python pipeline.py --tex main.tex
```

By default the run pauses after each stage. Useful options:

| Option | Effect |
|---|---|
| `--auto` | run every stage without pausing |
| `--yes` | apply stage 0's changes to the document without asking |
| `--offline` | use placeholder text instead of calling the model |
| `--verbose` | show every warning |

The exit code is 0 when the final PDF passes validation and 1 otherwise.

The same pipeline can be run from a window, which lists every figure, lets
you edit its alt text and runs the stages one at a time:

```bash
python app.py main.tex
```

`main.tex` in this folder is a test document with one figure for each case the
pipeline handles, including figures in the `chapters/` files it inputs.

## Stages

| Stage | Module | What it does |
|---|---|---|
| 0 preflight | `preflight.py` | checks the document compiles and adds `\DocumentMetadata`, `graphicx`, `tikz` and the line that reads the generated shim, after asking |
| 1 scan | `scanner.py` | finds every figure in the document and the files it inputs |
| 2 resolve | `resolver.py` | decides which figures already have usable text and which need describing |
| 3 render | `renderer.py` | turns each figure that needs describing into a PNG |
| 4 describe | `describer.py` | asks the model for a description of each image |
| 5 write | `writer.py` | adds the new descriptions to the `.alt` file |
| 6 inject | `injector.py` | writes `_alt/<name>.alt.tex` and builds the tagged PDF in `build/` |
| 7 validate | `validator.py` | checks every figure in the PDF has real alt text, and runs veraPDF if installed |

## The `.alt` file

`<name>.alt` is a JSON file with one entry per figure, keyed by the figure's
`\label` (or its image filename). Each entry has a status:

| Status | Meaning |
|---|---|
| `generated` | written by the model; described again if the figure changes |
| `edited` | written or changed by a person; never replaced |
| `approved` | checked by a person; never replaced |
| `artifact` | decorative; no alt text, left out of tagging |

Alt text written directly in the source with `\includegraphics[alt={...}]`
always takes priority. A figure can be given a profile with a comment on the
line above it, for example `%!alt chart` or `%!alt artifact`.

The `.alt` file is meant to be kept with the document. Everything under `_alt/`
and `build/` is regenerated on each run.

## Configuration

`alt.config.json` sets the model, the length budget for a description (250
characters) and the figure profiles. Each profile lists the keywords used to
guess it from a filename, label or caption, and what the model is asked to
describe for that kind of figure.

## Tests

```bash
cd unitTests
python run_all.py
```

This runs 266 checks across the stages, the whole pipeline and the window's
logic. No model is called. The suites that compile documents need `pdflatex`
and take about ten minutes in total. `unitTests/stage4/check_live.py` runs a
few extra checks against the real model.

## Layout

```
pipeline.py        runs the stages
app.py, project.py the window and the rules for editing the .alt file
preflight.py ...   one module per stage (see above)
alt.config.json    model and profile settings
main.tex, main.alt test document and its store
chapters/, figures/
unitTests/         one folder of checks per stage, plus pipeline/ and app/
```

## Licence

Released under the MIT Licence. See [LICENSE](LICENSE).
