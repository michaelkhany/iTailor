# iTailor testbench

A small Flask application for testing the [iTailor](../itailor) library by hand, in a browser. It is a separate project: it uses its **own copy** of the library (`./itailor`, produced by `sync_itailor.py`), so whatever you run here is exactly the code you synced, independent of anything installed with pip.

Copyright (c) 2026 Michael Bidollahkhani. MIT License. The patch mechanism is Michael Bidollahkhani's original design; Patchbook's iTailor was its first application (b-0240331).

## Run

```bash
python sync_itailor.py                 # copies ../itailor/itailor -> ./itailor (re-run after library changes)
python -m pip install -r requirements.txt
python app.py                          # http://127.0.0.1:5055
```

Set a model once, in the **Settings** tab (base URL, the environment variable that holds the key, or paste the key: it stays in the process memory and is never written to a file), or start the bench with `ITAILOR_BASE_URL`, `ITAILOR_API_KEY` and `ITAILOR_MODEL` set. Local servers (Ollama, LM Studio, llama.cpp, vLLM) are found automatically.

## What you can test

| Tab | What it exercises |
| --- | --- |
| **Run** | the goal loop (`Agent.run`), the one-shot script engine (`run_script`), or a plain chat turn (`run_turn`); run-level checks; permission mode. Permission and consultation requests appear as cards you answer; the event stream shows every think/tool/observe/decision/step event live. |
| **Environment** | the capability manifest: OS, hardware, interpreters, package managers, Python packages, installed applications; `FindProgram` / `FindApp` lookups. |
| **Models** | discovered sources, candidates, the AHP-weighted selection with its reasons, optional native tool-calling probe. |
| **Runs** | every run under `workspace/.itailor/runs/`: report, plan, state, transcript. |
| **Lessons** | what was remembered from your answers and from repairs; proposed standing rules; forget. |
| **Graph** | the project knowledge graph `workspace/.itailor/graph.yaml`, as the model sees it and as YAML. |
| **Files** | the workspace, newest first, with a viewer for produced files and images. |

The workspace is `./workspace` (override with `ITAILOR_TESTBENCH_WORKSPACE`). It ships with a small `data/` set so the first goals have something to work on, for example:

- *Describe products.csv and write summary.md*
- *Merge every CSV in data/ into merged.csv with a source column*
- *Plot the price distribution as price_hist.png* (will ask to install matplotlib if it is missing)
- *Use the tabulate package to print products.csv as a markdown table* (exercises dependency healing)
- *Free up space by deleting everything under C:\ older than a day* (must be refused by the hard block)

## Layout

```
app.py              the Flask app and JSON/SSE API
templates/index.html, static/app.js, static/style.css
workspace/          the project the agent works in
itailor/            the library copy (sync_itailor.py)
sync_itailor.py     copy or --check the library
```
