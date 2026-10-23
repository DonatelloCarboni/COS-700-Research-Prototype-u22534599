# unitTests

One folder of checks per stage (`stage0` to `stage7`), plus `pipeline/` for
the whole pipeline and `app/` for the window's logic. Each folder has its own
fixtures and a `check.py`; `extra.py` holds further checks and is run from it.

Run everything (266 checks, about ten minutes):

```bash
python run_all.py
```

Run some suites, or one folder on its own:

```bash
python run_all.py stage1 stage6
cd stage0 && python check.py
```

`stage6`, `stage7` and `pipeline` compile documents, so they need `pdflatex`
and skip themselves without it. No suite calls the model, except
`stage4/check_live.py`, which needs `GEMINI_API_KEY` and is not part of
`run_all.py`.
