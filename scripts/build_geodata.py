#!/usr/bin/env python3
"""Build the geography datasets that FraudRadar's map and simulator use.

    python scripts/build_geodata.py            # download the sources, then build
    python scripts/build_geodata.py --raw DIR  # reuse sources already in DIR

Sources (see the README for licences):
  * Natural Earth 50m countries and 10m populated places (public domain)
  * geoBoundaries gbOpen Viet Nam ADM1 and ADM2 (CC BY 3.0 IGO, OCHA ROAP / Government of Viet Nam, 2020 vintage)

Output goes to src/fraudradar/data/. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import urllib.request
from pathlib import Path

NE = "https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@v5.1.2/geojson"
GB = "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/VNM"
SOURCES = {
    "countries.geojson": f"{NE}/ne_50m_admin_0_countries.geojson",
    "places.geojson": f"{NE}/ne_10m_populated_places_simple.geojson",
    "vnm_adm1.geojson": f"{GB}/ADM1/geoBoundaries-VNM-ADM1.geojson",
    "vnm_adm2.geojson": f"{GB}/ADM2/geoBoundaries-VNM-ADM2.geojson",
}
OUT = Path(__file__).resolve().parent.parent / "src" / "fraudradar" / "data"

PLACE_KINDS = {
    "Admin-0 capital": "capital",
    "Admin-0 capital alt": "capital",
    "Admin-0 region capital": "capital",
    "Admin-1 capital": "region",
    "Admin-1 region capital": "region",
    "Populated place": "place",
}


# ---------- geometry helpers ----------
def dp(points: list, tol: float) -> list:
    """Douglas-Peucker on an open polyline, iterative so deep rings cannot overflow the stack."""
    n = len(points)
    if n < 3:
        return points[:]
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        x1, y1 = points[i]
        x2, y2 = points[j]
        dx, dy = x2 - x1, y2 - y1
        d2 = dx * dx + dy * dy
        far, idx = -1.0, -1
        for k in range(i + 1, j):
            x, y = points[k]
            if d2 == 0:
                dist2 = (x - x1) ** 2 + (y - y1) ** 2
            else:
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / d2))
                dist2 = (x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2
            if dist2 > far:
                far, idx = dist2, k
        if far > tol2:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring: list, tol: float, prec: int) -> list | None:
    pts = [(round(x, prec), round(y, prec)) for x, y, *_ in ring[:-1]]
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    if len(pts) < 3:
        return None
    # split the closed ring at the point farthest from the start so Douglas-Peucker has two real ends
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    if far == 0:
        return None
    a = dp(pts[: far + 1], tol)
    b = dp(pts[far:] + [pts[0]], tol)
    out = a[:-1] + b
    if len(out) < 4:
        return None
    return [list(p) for p in out]


def simplify_geometry(geom: dict, tol: float, prec: int) -> dict | None:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in polys:
        outer = simplify_ring(poly[0], tol, prec)
        if outer is None:
            continue
        holes = [r for r in (simplify_ring(h, tol, prec) for h in poly[1:]) if r]
        out.append([outer] + holes)
    if not out:
        return None
    if len(out) == 1:
        return {"type": "Polygon", "coordinates": out[0]}
    return {"type": "MultiPolygon", "coordinates": out}


def in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_geometry(x: float, y: float, geom: dict) -> bool:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if in_ring(x, y, poly[0]) and not any(in_ring(x, y, h) for h in poly[1:]):
            return True
    return False


def ring_area_centroid(ring: list) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0, x1, y1 = ring[i][0], ring[i][1], ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        return 0.0, ring[0][0], ring[0][1]
    return a / 2, cx / (3 * a), cy / (3 * a)


def representative_point(geom: dict) -> tuple[float, float]:
    """A point that is really inside the shape: the centroid of the largest polygon, or if that falls outside
    (a crescent, say), the interior grid point nearest to it. A boundary vertex would be ambiguous between
    neighbours, which is what mis-assigned border districts before."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    best = max(polys, key=lambda p: abs(ring_area_centroid(p[0])[0]))
    shape = {"type": "Polygon", "coordinates": best}
    _, cx, cy = ring_area_centroid(best[0])
    if in_geometry(cx, cy, shape):
        return cx, cy
    x0, y0, x1, y1 = bbox(shape)
    n = 24
    candidates = []
    for i in range(1, n):
        for j in range(1, n):
            x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * j / n
            if in_geometry(x, y, shape):
                candidates.append((((x - cx) ** 2 + (y - cy) ** 2), x, y))
    if candidates:
        _, x, y = min(candidates)
        return x, y
    return best[0][0][0], best[0][0][1]


def province_of(x: float, y: float, provinces: list) -> str | None:
    for name, geom, (x0, y0, x1, y1) in provinces:
        if x0 <= x <= x1 and y0 <= y <= y1 and in_geometry(x, y, geom):
            return name
    return None


def bbox(geom: dict) -> tuple[float, float, float, float]:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    xs = [p[0] for poly in polys for p in poly[0]]
    ys = [p[1] for poly in polys for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


# ---------- build ----------
def load(raw: Path, name: str) -> dict:
    with open(raw / name, encoding="utf-8") as fh:
        return json.load(fh)


def write(name: str, collection: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  {name}: {len(collection['features'])} features, {path.stat().st_size / 1e6:.2f} MB")


def build_countries(raw: Path) -> None:
    src = load(raw, "countries.geojson")
    feats = []
    for f in src["features"]:
        p = f["properties"]
        if p["ADM0_A3"] == "ATA":  # Antarctica fills the bottom of a Mercator map and carries no traffic
            continue
        geom = simplify_geometry(f["geometry"], tol=0.01, prec=3)
        if geom is None:
            continue
        feats.append({
            "type": "Feature",
            "id": len(feats),
            "properties": {
                "iso": p["ADM0_A3"],
                "name": p["NAME"],
                "name_vi": p.get("NAME_VI") or p["NAME"],
                "continent": p["CONTINENT"],
                "pop": p["POP_EST"],
                "lx": p["LABEL_X"],
                "ly": p["LABEL_Y"],
                "minl": p["MIN_LABEL"],
            },
            "geometry": geom,
        })
    write("countries.geojson", {"type": "FeatureCollection", "features": feats})


# District drill-down is built for Viet Nam's five centrally governed cities only. The geoBoundaries province
# layer (ADM1) does not line up with its district layer at several borders (Ha Noi's polygon swallows districts of
# Hung Yen, Ha Nam and Bac Ninh), so a spatial join cannot be trusted for every province. Two cities check out
# spatially against their known district counts (Ho Chi Minh City 24, Da Nang 8); the other three are listed by
# name, using the 2020 administrative structure that this dataset represents.
CITY_BY_PROVINCE = {"Ho Chi Minh": "Ho Chi Minh City", "Đà Nẵng": "Da Nang"}
CITY_DISTRICTS = {
    "Hanoi": [
        "Ba Dinh", "Hoan Kiem", "Hai Ba Trung", "Dong Da", "Tay Ho", "Cau Giay", "Thanh Xuan", "Hoang Mai",
        "Long Bien", "Nam Tu Liem", "Bac Tu Liem", "Ha Dong", "Son Tay", "Ba Vi", "Chuong My", "Dan Phuong",
        "Dong Anh", "Gia Lam", "Hoai Duc", "Me Linh", "My Duc", "Phu Xuyen", "Phuc Tho", "Quoc Oai", "Soc Son",
        "Thach That", "Thanh Oai", "Thanh Tri", "Thuong Tin", "Ung Hoa",
    ],
    "Haiphong": [
        "Hong Bang", "Le Chan", "Ngo Quyen", "Kien An", "Hai An", "Do Son", "Duong Kinh", "An Duong", "An Lao",
        "Bach Long Vi", "Cat Hai", "Kien Thuy", "Thuy Nguyen", "Tien Lang", "Vinh Bao",
    ],
    "Can Tho": [
        "Ninh Kieu", "Binh Thuy", "Cai Rang", "O Mon", "Thot Not", "Phong Dien", "Co Do", "Thoi Lai", "Vinh Thanh",
    ],
}
EXPECTED = {"Hanoi": 30, "Ho Chi Minh City": 24, "Da Nang": 8, "Haiphong": 15, "Can Tho": 9}
# Hoang Sa is an island district of Da Nang with no mainland province underneath it.
ISLAND_DISTRICTS = {"Hoang Sa": "Da Nang"}


def build_vietnam(raw: Path, places: dict) -> None:
    adm1 = load(raw, "vnm_adm1.geojson")
    provinces = [
        (f["properties"]["shapeName"].strip(), f["geometry"], bbox(f["geometry"]))
        for f in adm1["features"]
        if f["properties"]["shapeName"].strip() in CITY_BY_PROVINCE
    ]
    by_name = {name: city for city, names in CITY_DISTRICTS.items() for name in names}

    adm2 = load(raw, "vnm_adm2.geojson")
    candidates: dict[str, list] = {}
    for f in adm2["features"]:
        name = f["properties"]["shapeName"].strip()
        x, y = representative_point(f["geometry"])
        city = CITY_BY_PROVINCE.get(province_of(x, y, provinces) or "") or ISLAND_DISTRICTS.get(name)
        if city:
            candidates.setdefault(city, []).append((name, f, (x, y)))
        elif name in by_name:  # listed by name; the same name can exist elsewhere, so keep the one nearest the city
            cx, cy = places[by_name[name]]
            candidates.setdefault("_named_" + by_name[name], []).append((name, f, (x, y), (x - cx) ** 2 + (y - cy) ** 2))

    chosen: dict[str, list] = {c: [(n, f) for n, f, *_ in v] for c, v in candidates.items() if not c.startswith("_named_")}
    for city, names in CITY_DISTRICTS.items():
        pool = candidates.get("_named_" + city, [])
        for name in names:
            matches = sorted((m for m in pool if m[0] == name), key=lambda m: m[3])
            if matches:
                chosen.setdefault(city, []).append((name, matches[0][1]))

    feats = []
    for city in sorted(chosen):
        got = len(chosen[city])
        flag = "" if got == EXPECTED[city] else f"   <-- expected {EXPECTED[city]}"
        print(f"  {city}: {got} districts{flag}")
        for name, f in chosen[city]:
            geom = simplify_geometry(f["geometry"], tol=0.0003, prec=4)
            if geom is None:
                continue
            feats.append({
                "type": "Feature",
                "id": len(feats),
                "properties": {"id": len(feats), "name": name, "city": city},
                "geometry": geom,
            })
    write("vn_districts.geojson", {"type": "FeatureCollection", "features": feats})
    return {c: len(v) for c, v in chosen.items()}


def build_places(raw: Path, district_counts: dict) -> dict:
    src = load(raw, "places.geojson")
    rows = []
    for f in src["features"]:
        p = f["properties"]
        kind = PLACE_KINDS.get(p["featurecla"])
        if kind is None:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        rows.append({
            "name": p["name"],
            "ascii": p["nameascii"],
            "country": p["adm0name"],
            "iso": p["adm0_a3"],
            "pop": int(p["pop_max"] or 0),
            "kind": kind,
            "rank": p["scalerank"],
            "minzoom": p["min_zoom"],
            "districts": district_counts.get(p["nameascii"], 0) if p["adm0_a3"] == "VNM" else 0,
            "_lon": round(lon, 4),
            "_lat": round(lat, 4),
        })
    rows.sort(key=lambda r: -r["pop"])
    feats = []
    for i, r in enumerate(rows):
        lon, lat = r.pop("_lon"), r.pop("_lat")
        r["id"] = i
        feats.append({"type": "Feature", "id": i, "properties": r, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    write("places.geojson", {"type": "FeatureCollection", "features": feats})
    return {r["properties"]["ascii"]: r["geometry"]["coordinates"] for r in feats if r["properties"]["iso"] == "VNM"}


def fetch(raw: Path) -> None:
    for name, url in SOURCES.items():
        target = raw / name
        if target.exists():
            continue
        print(f"  downloading {name}")
        urllib.request.urlretrieve(url, target)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, help="directory that already holds the source files")
    args = ap.parse_args()

    raw = args.raw or Path(tempfile.mkdtemp(prefix="fraudradar-geo-"))
    raw.mkdir(parents=True, exist_ok=True)
    fetch(raw)  # only downloads what is missing, so --raw can point at a partial or full set

    print("building")
    build_countries(raw)
    vn_places = build_places(raw, {})  # first pass: coordinates of Viet Nam's cities
    counts = build_vietnam(raw, vn_places)
    build_places(raw, counts)  # second pass: now each city knows how many districts it has
    return 0


if __name__ == "__main__":
    sys.exit(main())
