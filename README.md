# Schrödinger's Bus

Until it arrives, a London bus is both coming and not coming.

Schrödinger's Bus is a serverless workflow on Azure Functions that measures how reliable London buses are from a passenger's point of view. It uses live arrival predictions from Transport for London (TfL), and for each route and stop it reports:

- **Gaps:** how long passengers wait between buses, with bootstrap confidence intervals.
- **Convoys:** buses arriving bunched together, three or more at a time.
- **Apparent vanished departures:** buses that drop out of the predictions shortly before they were due.

These are estimates from predictions, not official performance figures; a bus that vanishes from the feed hasn't necessarily been cancelled.

## Repository Layout

| Folder | Contents |
|---|---|
| `app/` | The two Azure Functions (Python 3.12), plus the batch parsing and reliability analysis they use |
| `replay/` | Tools to split recordings into overlapping batches, send them to Function 1 as a burst or at a steady rate, and collect and summarise the results |
| `trial/` | The TfL recorder and an early stand-alone analysis. Recordings go in `trial/data/`, which Git ignores |

Each folder has tests, which use only the standard library. For example:

```bash
python3 replay/test_split.py
```

## Data

The data comes from TfL's [Unified API](https://api.tfl.gov.uk/), using the `Line/{id}/Arrivals` endpoint. Recordings aren't included in the repository because of their size.

Powered by TfL Open Data.

Contains OS data © Crown copyright and database rights 2016 and Geomni UK Map data © and database rights [2019]

Used under TfL's [Transport Data Service Licence](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service).
