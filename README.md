# FraudRadar

[![CI](https://github.com/nguyenhoangkhang-2010/fraudradar-realtime-fraud-detection/actions/workflows/ci.yml/badge.svg)](https://github.com/nguyenhoangkhang-2010/fraudradar-realtime-fraud-detection/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A real-time fraud detection command center. Synthetic payments stream through
Kafka, get scored one by one with streaming features, and show up on a live
map and account graph. A button launches a fraud ring so you can watch the
system find it.

> **Status: working scaffold.** The streaming path, feature logic, tests and UI
> run end to end. The scorer is still a hand-weighted heuristic and its state
> lives in memory. See [Roadmap](#roadmap) for what is deliberately not done yet.

## Run it

Requires Docker.

```bash
docker compose up --build
```

Open <http://localhost:8000> and press **Launch attack**.

The page is a flat world map you can drill into: **world, then country, then city, then district**.

- Click a country to zoom in. Its borders, capital and cities appear and the other countries dim.
- Click a city to zoom to it. Viet Nam's five big cities (Hanoi, Ho Chi Minh City, Da Nang, Haiphong,
  Can Tho) open into their districts.
- Click a district to zoom to it. The breadcrumb at the top, the side panel's list, and `Esc` all go back up.
- Hover anything for its name and its flagged-payment count over the last 60 s.

When a ring is detected the camera follows the money down to the mule account (country, then city, then
district), shock waves mark each blocked payment, and an account network and an incident card show what was
caught. Regions with flagged traffic turn amber to red. If you start exploring yourself during an incident,
the camera stays where you put it.

The map, the network graph and the fonts' glyphs come from a CDN or this repo as noted below, so the page
needs internet access for MapLibre and d3. `/globe.html` is an earlier 3D-globe version of the same stream.

Offline checks need no Docker or Kafka:

```bash
pip install -e ".[dev]"
pytest
python -m fraudradar.evaluate
```

## Training a model

Training is separate from running the services and needs none of Docker, Kafka or a GPU. Install the
training libraries (LightGBM, scikit-learn, SHAP, pandas, Polars, DuckDB, JupyterLab) in one go:

```bash
conda env create -f environment.yml
```

or, with plain pip in a virtual environment:

```bash
pip install -r requirements-train.txt
```

Then, in the activated environment, make `fraudradar.features` importable from notebooks without pulling in the
service dependencies:

```bash
pip install -e . --no-deps
```

Keep downloaded datasets in `training/data/` and trained models in `models/`; both are git-ignored. Link to
the dataset in this README rather than committing it.

## How it works

```
simulator ──► [transactions] ──► scorer ──► [decisions] ──► api ──WebSocket──► browser
    ▲                                                          │
    └──────────────────────[control]◄──────── POST /api/attack ┘
```

| Service | Job |
|---|---|
| `simulator` | Publishes legitimate traffic at a steady rate. Emits a fraud ring when it receives an `attack` command. |
| `scorer` | Computes features, scores, decides `allow`, `review` or `block`, publishes the decision. |
| `api` | Reads decisions, keeps rolling metrics, streams them to the browser. |
| `redpanda` | Kafka-compatible broker, single node. |

### Features (`src/fraudradar/features.py`)

Computed per transaction from the state *before* it, then the state is updated.
A transaction never sees itself, and `tests/test_features.py` checks that.

| Feature | Meaning |
|---|---|
| `new_account` | Sender is younger than 7 days |
| `amount_z` | Amount against the sender's own history (log scale) |
| `velocity` | Sender's transactions in the last 60 s |
| `shared_device` | Other accounts seen on the same device |
| `young_fan_in` | Distinct young accounts paying the same recipient in 60 s |
| `flagged_linked` | Recently blocked accounts among device mates, co-senders and the sender |

The last three are the graph-style signals that make a ring visible when each
single payment looks plausible.

### The data

All traffic is synthetic, from `src/fraudradar/generator.py`. Honest traffic is
made deliberately awkward: popular merchants receiving many payments, new but
honest accounts, family members sharing a device, rare large purchases. A fraud
ring is 3 to 60 young accounts on a few shared devices paying one mule.

Ground truth travels with each transaction as `truth` so the system can be
measured. The scorer never reads it, and a test asserts that.

### Geography

Accounts live in 96 "active places": the 80 largest places in the world plus Viet Nam's larger cities,
weighted by the square root of population, with Viet Nam boosted 4x (about a quarter of the traffic) so
the district view has something to show. In Hanoi, Ho Chi Minh City, Da Nang, Haiphong and Can Tho an
account also has a district. **All locations are simulated.** None of this is real customer data.

The map data lives in `src/fraudradar/data/` and is built by `python scripts/build_geodata.py`, which
downloads the sources, simplifies the shapes and checks the district counts.

| Data | Source | Licence |
|---|---|---|
| Countries, populated places, capitals | [Natural Earth](https://www.naturalearthdata.com/) 50m / 10m | Public domain |
| Viet Nam districts | [geoBoundaries](https://www.geoboundaries.org/) gbOpen ADM2 (OCHA ROAP / Government of Viet Nam) | CC BY 3.0 IGO |
| Label font | Open Sans glyphs from [OpenMapTiles fonts](https://github.com/openmaptiles/fonts) | Apache 2.0 |
| Map renderer | [MapLibre GL JS](https://maplibre.org/) 5.15.0 | BSD-3-Clause |

Limits worth knowing:

- **District data is the 2020 structure.** Viet Nam merged provinces and removed the district level in
  2025, so these are the boundaries as they stood before that. District names have no diacritics, as in the
  source.
- **District detail exists for five cities only.** The province layer in geoBoundaries does not line up
  with its district layer at several borders (Hanoi's province polygon swallows districts of Hung Yen, Ha
  Nam and Bac Ninh), so districts cannot be assigned to all 63 provinces automatically. Ho Chi Minh City and
  Da Nang are assigned spatially and match their known counts (24 and 8); Hanoi (30), Haiphong (15) and
  Can Tho (9) are listed by name. The build script prints each count and flags any mismatch.
- District shapes are simplified (about 30 m) and country shapes more (about 1 km), so borders are good
  for display, not for surveying. Antarctica is left out.
- Natural Earth shows disputed borders as it does; no claim is made about them.

## Results so far

Measured by `python -m fraudradar.evaluate` (seed 7, 120 s at 150 tx/s, two
rings of 14 accounts):

| Metric | Value |
|---|---|
| Honest transactions blocked | 0 of 18,000 |
| Ring transactions blocked | 69 of 81 (85%) |
| Time to first block after a ring starts | 0.4 s and 1.4 s for the two rings |

These come from synthetic data produced by the same project, so they show the
pipeline works. They do **not** show how it would do on real fraud.

In the live demo on a laptop (one broker, one scorer, 120 tx/s) the UI showed
end-to-end latency of about 7 ms p50 and 10 to 17 ms p99, event time to
decision. That is an observation, not a load test.

## Model results on PaySim

A LightGBM model trained with `training/paysim_lightgbm.ipynb` on the
[PaySim](https://www.kaggle.com/datasets/ealaxi/paysim1) dataset (6,362,620 payments over 743 hours). The split is
by time: train on hours 1-281, choose the alert threshold on hours 282-355, report on the untouched hours 356-743.
Everything below is the **test** part; the raw numbers are in [docs/results/paysim_v1](docs/results/paysim_v1).

| Model | Average precision | Fraud caught | Alerts that are fraud | Honest payments flagged |
|---|---|---|---|---|
| Logistic regression | 0.638 | 63.2% | 66.2% | 0.110% |
| LightGBM, payment features only (A) | 0.9995 | 99.98% | 71.0% | 0.139% |
| LightGBM, A + history (A+B) | 0.9985 | 99.7% | 88.2% | 0.046% |
| **LightGBM, all features (A+B+C)** | **0.9995** | **99.98%** | **89.7%** | **0.039%** |

"Fraud caught", "alerts that are fraud" and "honest payments flagged" are measured with the threshold set on the
validation part so that about 0.1% of honest payments are flagged. The last column shows what that threshold
actually did on the test part, which is why the rows are not comparable on precision alone. One prediction takes
about 1.8 ms (p99 6 ms) on a Kaggle CPU notebook, one process; that is not a load test.

How to read this honestly:

- **PaySim is easy.** The two strongest signals are "this payment empties the sender's account" and the ratio of the
  amount to the balance. That is the rule the PaySim simulator uses to create fraud, so a model that sees only the
  payment is already near-perfect. The score shows the pipeline and the split are sound; it says little about
  real-world fraud.
- **The history and chain features added nothing measurable.** The chain features never fire on PaySim (their SHAP
  value is zero), and the AP differences between A, A+B and A+B+C are about 0.001. That is from one seed and one
  split, so treat it as noise, not as evidence that any group helps or hurts.
- **Precision is not comparable across rows.** Each row's threshold flagged a different share of honest payments
  on the test part (last column).
- The PaySim fraud rate (about 0.13%) is a property of the simulation.

## Roadmap

- [x] Train a LightGBM model on PaySim and report it honestly (see above)
- [ ] Serve that model in the scorer in place of `HeuristicScorer`
- [ ] Move feature state to Redis so several scorer replicas can run; use more than one partition
- [ ] Calibrate the simulator against a public dataset (for example IEEE-CIS or PaySim)
- [ ] Load test, with p50/p99 under rising throughput
- [ ] Drift detection and automatic retraining when attackers change tactics
- [ ] Feature lineage and a training-serving skew check

## Layout

```
scripts/
  build_geodata.py  downloads and builds the map datasets
src/fraudradar/
  geo.py         active places and districts, shared by the simulator and the map
  data/          countries, places and districts as GeoJSON (built, committed)
  generator.py   synthetic traffic and fraud rings
  features.py    streaming features, point-in-time correct
  model.py       scorers and thresholds
  engine.py      features -> score -> action
  evaluate.py    offline replay and metrics
  simulator/     Kafka producer + control listener
  scorer/        Kafka consumer/producer around the engine
  api/           FastAPI, WebSocket hub, metrics, static UI (map, fonts)
tests/
```

## Licence

The code is released under the [MIT licence](LICENSE). The map data and the label font keep their own
licences, listed under [Geography](#geography); the sources must be credited as described there.
