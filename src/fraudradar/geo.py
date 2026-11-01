"""Geography shared by the simulator and the map.

The datasets in `data/` come from `scripts/build_geodata.py`:
  places.geojson       ~7,300 populated places (Natural Earth), each with country, kind and population
  countries.geojson    country polygons with labels (Natural Earth)
  vn_districts.geojson districts of Viet Nam's five centrally governed cities (geoBoundaries, 2020 boundaries)

Simulated accounts live in a subset of those places ("active places"). A transaction's `src_city` and `dst_city`
are indexes into `active_places()`; `src_district` and `dst_district` are ids in `vn_districts.geojson`, or -1.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

TOP_PLACES = 80  # the largest places in the world are active
MIN_VN_POP = 300_000  # plus Viet Nam's larger cities
VN_BOOST = 4.0  # Viet Nam gets extra traffic so the district view has something to show


def _read(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def districts() -> list[dict]:
    """District id, name and the city it belongs to."""
    return [dict(f["properties"]) for f in _read("vn_districts.geojson")["features"]]


@lru_cache(maxsize=1)
def active_places() -> list[dict]:
    feats = _read("places.geojson")["features"]  # already sorted by population, largest first
    by_city: dict[str, list[int]] = {}
    for d in districts():
        by_city.setdefault(d["city"], []).append(d["id"])

    picked = []
    for f in feats:
        p = f["properties"]
        keep = len(picked) < TOP_PLACES or (p["iso"] == "VNM" and (p["pop"] >= MIN_VN_POP or p["districts"] > 0))
        if keep:
            picked.append({
                "pid": f["id"],
                "name": p["name"],
                "country": p["country"],
                "iso": p["iso"],
                "capital": p["kind"] == "capital",
                "pop": p["pop"],
                "lon": f["geometry"]["coordinates"][0],
                "lat": f["geometry"]["coordinates"][1],
                "district_ids": by_city.get(p["ascii"], []) if p["districts"] else [],
            })
    for i, place in enumerate(picked):
        place["id"] = i
        place["weight"] = math.sqrt(max(place["pop"], 1)) * (VN_BOOST if place["iso"] == "VNM" else 1.0)
    return picked


def as_dicts() -> list[dict]:
    """What the browser needs to place and label each active place."""
    return [
        {
            "id": p["id"], "pid": p["pid"], "name": p["name"], "country": p["country"], "iso": p["iso"],
            "capital": p["capital"], "lat": p["lat"], "lon": p["lon"], "districts": len(p["district_ids"]),
        }
        for p in active_places()
    ]
