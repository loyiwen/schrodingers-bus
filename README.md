# Schrödinger's Bus

Until it arrives, a London bus is both coming and not coming.

Schrödinger's Bus is a serverless workflow on Azure Functions that measures how reliable London buses are from a passenger's point of view. It uses live arrival predictions from Transport for London (TfL), and for each route and stop it reports:

- **Gaps:** how long passengers wait between buses, with bootstrap confidence intervals.
- **Convoys:** buses arriving bunched together, three or more at a time.
- **Apparent vanished departures:** buses that drop out of the predictions shortly before they were due.

These are estimates from predictions, not official performance figures; a bus that vanishes from the feed hasn't necessarily been cancelled.

## How It Works

```
Load generator ──HTTP PUT──> Function 1 (receive_batch) ──> Blob: batches/{id}.jsonl
                                     │
                                     └──> Queue: batches-to-analyse (message = batch ID)
                                                   │ queue trigger
                                                   v
                                     Function 2 (analyse_batch) ──> Blob: results/{id}.json
```

- **Function 1** checks the batch ID and contents, uploads the batch, and only then queues its ID, so Function 2 never sees a half-written file.
- **Function 2** runs automatically for each message. It writes a result named after the batch, so a repeated message overwrites rather than duplicates. Failed messages are retried 5 times, then moved to the poison queue.
- **Each instance takes at most 3 messages at once** (`app/host.json`). The analysis is CPU-bound Python, so Azure adds instances rather than overloading one.
- **Every result records its timings, its host and a fingerprint of the deployed code** (`app/version.py`), so each experiment shows exactly which code produced it.

## Repository Layout

| Folder | Contents |
|---|---|
| `app/` | The two Azure Functions (Python 3.12), plus the batch parsing and reliability analysis they use |
| `replay/` | Tools to split recordings into overlapping batches, send them to Function 1 as a burst or at a steady rate, and collect and summarise the results |
| `analysis/` | Turn experiment runs and recordings into tables and figures: performance, feed delay, reliability findings, and the architecture diagram |
| `trial/` | The TfL recorder (`record.py`), and the first stand-alone analysis (`analyse.py`), since superseded by `app/reliability.py`. Recordings, batches, runs and analysis output go in `trial/data/`, which Git ignores |

## Requirements

- **Python 3.12.** The functions use `azure-functions` and `azure-storage-blob` (`app/requirements.txt`). The recorder, replay tools and tests use only the standard library. The analysis scripts need `matplotlib` (`analysis/requirements.txt`).
- **An Azure subscription,** for a Function App and storage account. A Linux VM in the same region is recommended for load tests, since uploading from a home connection dominates the timings.
- **A free TfL API key,** from the [TfL API portal](https://api-portal.tfl.gov.uk/).

Secrets are read from environment variables and never stored in the repository:

| Variable | Used by | Value |
|---|---|---|
| `TFL_APP_KEY` | `trial/record.py` | Your TfL API key |
| `FUNCTION_KEY` | `replay/send.py`, `replay/experiment.py` | The `default` key from **receive_batch → Function keys** |
| `RESULTS_SAS_URL` | `replay/collect.py`, `replay/experiment.py` | A **Blob SAS URL** for the `results` container, with Read and List permissions only |

To set one without it appearing on screen or in your shell history:

```bash
read -rs FUNCTION_KEY && export FUNCTION_KEY
```

## Deploying

1. In the Azure portal, create a **Function App** on the **Flex Consumption** plan, with Python 3.12 and 2,048 MB instances. This also creates its storage account.
2. In that storage account, create two blob containers, `batches` and `results`, and a queue named `batches-to-analyse`. The poison queue is created automatically.
3. Optionally, add **Application Insights,** and set `APPLICATIONINSIGHTS_CONNECTION_STRING` in the Function App's environment variables. `app/host.json` turns sampling off, so nothing is dropped under load.
4. Deploy the `app/` folder. In VS Code, use **Azure Functions: Deploy to Function App** (the workspace settings point the extension at `app/`). With Azure Functions Core Tools, run `func azure functionapp publish <your-app-name>` from inside `app/`.
5. Check that the deployment is live. Send one batch, as below, and confirm that the collected summary's `code_versions` matches the local fingerprint:

```bash
python3 app/version.py
```

## Recording and Batching

Record arrival predictions every 30 seconds. This example records 10 routes for 3 days:

```bash
python3 trial/record.py --lines 25,W5,38,29,18,73,149,86,207,43 --minutes 4320
```

Split a recording into overlapping one-route batches (60 minutes long, starting every 30 minutes, by default). Batches where most polls are empty, which happens overnight, are skipped:

```bash
python3 replay/split.py trial/data/tfl-<timestamp>.jsonl trial/data/batches/part1-60m --lines 25,38,29,18,73,149,86,207,43
```

Use a separate output folder for each recording, and for each batch length.

## Sending Batches and Collecting Results

Send a burst of 100 batches, in an order fixed by a seed, so that repeats send identical data:

```bash
python3 replay/send.py trial/data/batches/part1-60m --url https://<your-app-name>.azurewebsites.net --run demo1 --count 100
```

Add `--rate 5` to send 5 batches per second instead of all at once. Every run needs a new name, using letters, digits and underscores, so runs never overwrite each other's results.

Collect the run's results, waiting until none are missing. This writes a per-batch CSV and a summary to `trial/data/runs/`:

```bash
python3 replay/collect.py demo1 --wait 900
```

## Running the Experiments

`replay/experiment.py` runs the full plan of 39 runs: bursts of 10 to 1,000 batches, steady rates of 1 to 10 per second, and 30-, 60- and 120-minute batches, each repeated 3 times, in a fixed random order with 5 minutes between runs. It takes about 5 hours. If it's stopped, rerunning it carries on from where it left off. Run it on the VM inside `tmux`. To list the plan and check the batch folders without sending anything:

```bash
python3 replay/experiment.py --url https://<your-app-name>.azurewebsites.net --dry-run
```

## Analysis

Install `matplotlib`, then turn the runs into tables and figures in `trial/data/analysis/`:

```bash
pip install -r analysis/requirements.txt
```

```bash
python3 analysis/analyse_runs.py
```

The other scripts measure feed delay from the recordings, combine every batch into route-level reliability findings (counting overlapping batches once), and draw the architecture diagram:

```bash
python3 analysis/feed_delay.py
```

```bash
python3 analysis/reliability_summary.py
```

```bash
python3 analysis/diagram.py
```

## Tests

Each folder has tests, which use only the standard library. Fake HTTP and blob endpoints stand in for Azure, so they run offline:

```bash
for test in app/tests/test_*.py replay/test_*.py trial/test_*.py; do python3 "$test"; done
```

## Data

The data comes from TfL's [Unified API](https://api.tfl.gov.uk/), using the `Line/{id}/Arrivals` endpoint. Recordings aren't included in the repository because of their size.

Powered by TfL Open Data.

Contains OS data © Crown copyright and database rights 2016 and Geomni UK Map data © and database rights [2019]

Used under TfL's [Transport Data Service Licence](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service).
