import json

from fraudradar import geo
from fraudradar.generator import TransactionGenerator
from fraudradar.schemas import Decision, Transaction

BIG_CITIES = {"Hanoi": 30, "Ho Chi Minh City": 24, "Da Nang": 8, "Haiphong": 15, "Can Tho": 9}


def test_active_places_are_indexed_and_include_viet_nam():
    places = geo.active_places()
    assert [p["id"] for p in places] == list(range(len(places)))
    assert all(p["weight"] > 0 for p in places)
    vn = {p["name"] for p in places if p["iso"] == "VNM"}
    assert {"Hanoi", "Ho Chi Minh City", "Da Nang"} <= vn


def test_the_five_big_cities_have_their_districts():
    by_name = {p["name"]: p for p in geo.active_places()}
    for city, expected in BIG_CITIES.items():
        assert len(by_name[city]["district_ids"]) == expected, city
    # a district id must point at a district of that same city
    by_id = {d["id"]: d for d in geo.districts()}
    for place in geo.active_places():
        for did in place["district_ids"]:
            assert by_id[did]["city"] == place["name"]


def test_as_dicts_is_what_the_browser_needs():
    row = geo.as_dicts()[0]
    assert {"id", "pid", "name", "country", "iso", "capital", "lat", "lon", "districts"} <= set(row)


def test_district_files_match_what_the_places_file_promises():
    places = json.loads((geo.DATA_DIR / "places.geojson").read_text(encoding="utf-8"))["features"]
    promised = {f["properties"]["ascii"]: f["properties"]["districts"] for f in places if f["properties"]["districts"]}
    assert promised == BIG_CITIES


def test_generated_accounts_carry_districts_only_where_they_exist():
    gen = TransactionGenerator(seed=2)
    txs = [gen.legit(i) for i in range(4000)]
    by_id = {d["id"]: d for d in geo.districts()}
    with_district = 0
    for t in txs:
        place = gen.places[t.dst_city]
        if place["district_ids"]:
            assert t.dst_district in place["district_ids"]
            assert by_id[t.dst_district]["city"] == place["name"]
            with_district += 1
        else:
            assert t.dst_district == -1
    assert with_district > 0  # Viet Nam's big cities are part of the traffic


def test_ring_origin_and_mule_districts_are_consistent():
    gen = TransactionGenerator(seed=5)
    for _ in range(10):
        for t in gen.ring(0, size=10):
            for city, district in ((t.src_city, t.src_district), (t.dst_city, t.dst_district)):
                ids = gen.places[city]["district_ids"]
                assert (district in ids) if ids else (district == -1)


def test_districts_survive_serialisation():
    t = Transaction("t", 1, "A", "B", 5.0, 0, 1, "D", 100.0, src_district=7, dst_district=9)
    assert Transaction.from_bytes(t.to_bytes()) == t
    d = Decision("t", 1, "A", "B", 5.0, 0, 1, 0.1, "allow", 2, 1.0, src_district=7, dst_district=9)
    assert Decision.from_bytes(d.to_bytes()).dst_district == 9
