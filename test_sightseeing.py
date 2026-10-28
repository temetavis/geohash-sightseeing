"""Unit tests for sightseeing.py (no network required)."""

import math
from pathlib import Path

import pytest

import sightseeing as ss


def test_haversine_known_distance():
    # Seattle (47.6062, -122.3321) to Portland (45.5152, -122.6784) ~ 233 km
    d = ss.haversine_m(47.6062, -122.3321, 45.5152, -122.6784)
    assert 220_000 < d < 245_000


def test_haversine_zero():
    assert ss.haversine_m(10.0, 20.0, 10.0, 20.0) == 0.0


def test_haversine_one_degree_latitude():
    d = ss.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert math.isclose(d, 111_195, rel_tol=0.01)


def test_geohash_known_values():
    # Classic reference values (geohashing wiki)
    assert ss.geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert ss.geohash_encode(42.605, -5.603, 5) == "ezs42"
    assert ss.geohash_encode(0.0, 0.0, 1) == "s"
    assert ss.geohash_encode(-33.8688, 151.2093, 6) == "r3gx2f"


def test_geohash_precision_length():
    for prec in (1, 5, 9, 12):
        assert len(ss.geohash_encode(47.6, -122.3, prec)) == prec


def test_build_sparql_contains_center_radius_and_limit():
    q = ss.build_sparql(47.6062, -122.3321, 1500.0, 25)
    assert "Point(-122.332100 47.606200)" in q
    assert 'wikibase:radius "1.500000"' in q
    assert "LIMIT 25" in q
    assert "wikibase:around" in q
    assert "wdt:P625" in q
    assert "wd:Q570116" in q  # tourist attraction


def test_build_sparql_radius_kilometers_conversion():
    q = ss.build_sparql(0.0, 0.0, 500.0, 10)
    assert '"0.500000"' in q


def test_parse_coord_wkt():
    b = {"coord": {"value": "Point(-122.3321 47.6062)"}}
    lat, lon = ss.parse_coord(b)
    assert lat == pytest.approx(47.6062)
    assert lon == pytest.approx(-122.3321)


def test_parse_coord_missing():
    assert ss.parse_coord({}) == (None, None)
    assert ss.parse_coord({"coord": {"value": "garbage"}}) == (None, None)


def test_commons_thumb_url():
    url = ss.commons_thumb_url("File:Example.jpg", width=800)
    assert url.startswith("https://upload.wikimedia.org/wikipedia/commons/thumb/")
    assert "/800px-Example.jpg" in url
    # md5("Example.jpg") = a91fe217e45a700fc2dab0cc476f01c7 -> a/a9...
    assert "/a/a9/Example.jpg/" in url


def test_commons_thumb_url_spaces():
    url = ss.commons_thumb_url("File:My File Name.png", width=640)
    assert "My_File_Name.png" in url
    assert "640px-My_File_Name.png" in url


def test_fetch_sights_parses_bindings(monkeypatch):
    fake_bindings = [
        {
            "item": {"value": "http://www.wikidata.org/entity/Q123"},
            "itemLabel": {"value": "Space Needle"},
            "itemDescription": {"value": "observation tower"},
            "coord": {"value": "Point(-122.3493 47.6205)"},
            "distance": {"value": "1.4"},  # km
            "image": {"value": "https://commons.wikimedia.org/wiki/File:Space_Needle.jpg"},
            "article": {"value": "https://en.wikipedia.org/wiki/Space_Needle"},
        },
        {
            "item": {"value": "http://www.wikidata.org/entity/Q123"},  # duplicate
            "itemLabel": {"value": "Space Needle"},
            "coord": {"value": "Point(-122.3493 47.6205)"},
            "distance": {"value": "1.4"},
        },
        {
            "item": {"value": "http://www.wikidata.org/entity/Q456"},
            "itemLabel": {"value": "No Image Cafe"},
            "coord": {"value": "Point(-122.3300 47.6100)"},
            "distance": {"value": "0.5"},
        },
    ]
    monkeypatch.setattr(ss, "run_sparql", lambda q, s, timeout=60: fake_bindings)

    sights = ss.fetch_sights(47.6062, -122.3321, 2000, 10)
    assert len(sights) == 2  # duplicate removed
    nearest = sights[0]
    assert nearest.qid == "Q456"
    assert nearest.distance_m == pytest.approx(500.0)
    assert nearest.image_url is None
    far = sights[1]
    assert far.qid == "Q123"
    assert far.distance_m == pytest.approx(1400.0)
    assert far.lat == pytest.approx(47.6205)
    assert far.lon == pytest.approx(-122.3493)
    assert far.article.endswith("Space_Needle")


def test_fetch_sights_falls_back_to_haversine(monkeypatch):
    fake_bindings = [
        {
            "item": {"value": "http://www.wikidata.org/entity/Q1"},
            "itemLabel": {"value": "X"},
            "coord": {"value": "Point(0.001 0.0)"},
        }
    ]
    monkeypatch.setattr(ss, "run_sparql", lambda q, s, timeout=60: fake_bindings)
    sights = ss.fetch_sights(0.0, 0.0, 1000, 5)
    # Point(0.001 0.0) -> 0.001 degrees of longitude at the equator ~ 111.2 m
    assert sights[0].distance_m == pytest.approx(111.195, rel=0.01)


def test_download_image_success(tmp_path, monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}

        def iter_content(self, n):
            yield b"\xff\xd8\xff"  # fake JPEG magic
            yield b"data"

    monkeypatch.setattr(ss.requests.Session, "get", lambda self, url, **kw: FakeResp())
    dest = tmp_path / "sub" / "img.jpg"
    assert ss.download_image("http://x/img.jpg", dest, ss.requests.Session())
    assert dest.read_bytes() == b"\xff\xd8\xffdata"


def test_download_image_failure(tmp_path, monkeypatch):
    class FakeResp:
        status_code = 404
        headers = {"Content-Type": "text/html"}

        def iter_content(self, n):
            return iter(())

    monkeypatch.setattr(ss.requests.Session, "get", lambda self, url, **kw: FakeResp())
    assert not ss.download_image("http://x/img.jpg", tmp_path / "img.jpg",
                                 ss.requests.Session())


def test_download_image_rejects_html(tmp_path, monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def iter_content(self, n):
            yield b"<html>not an image</html>"

    monkeypatch.setattr(ss.requests.Session, "get", lambda self, url, **kw: FakeResp())
    assert not ss.download_image("http://x/img.jpg", tmp_path / "img.jpg",
                                 ss.requests.Session())
    assert not (tmp_path / "img.jpg").exists()


def test_download_images_uses_qid_prefix_and_skips_existing(tmp_path, monkeypatch):
    calls = []

    def fake_download(url, dest, session, timeout=30):
        calls.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"img")
        return True

    monkeypatch.setattr(ss, "download_image", fake_download)
    sights = [
        ss.Sight(qid="Q1", label="A",
                 image_url="https://commons.wikimedia.org/wiki/File:A.jpg"),
        ss.Sight(qid="Q2", label="B", image_url=None),
        ss.Sight(qid="Q3", label="C",
                 image_url="https://commons.wikimedia.org/wiki/File:C.png"),
    ]
    # pre-existing file for Q3
    (tmp_path / "Q3_C.png").write_bytes(b"old")
    ok = ss.download_images(sights, tmp_path)
    assert ok == 2
    assert sights[0].image_path.endswith("Q1_A.jpg")
    assert sights[1].image_path is None
    assert sights[2].image_path.endswith("Q3_C.png")
    # only one network download happened (Q3 was already on disk)
    assert len(calls) == 1
    assert "1280px-A.jpg" in calls[0][0]


def test_write_html_report(tmp_path):
    sights = [
        ss.Sight(qid="Q9", label='Eiffel "Tower" <test>',
                 description="Iron lattice tower",
                 lat=48.8584, lon=2.2945, distance_m=120.0,
                 image_path=str(tmp_path / "imgs" / "Q9_Eiffel.jpg"),
                 article="https://en.wikipedia.org/wiki/Eiffel_Tower"),
        ss.Sight(qid="Q10", label="NoPhoto", lat=None, lon=None),
    ]
    (tmp_path / "imgs").mkdir()
    (tmp_path / "imgs" / "Q9_Eiffel.jpg").write_bytes(b"fake")
    out = ss.write_html_report(sights, 48.8566, 2.3522, 1000.0,
                               tmp_path / "report.html", tmp_path / "imgs")
    text = out.read_text(encoding="utf-8")
    assert "Eiffel&quot; &quot;Tower&quot; &lt;test&gt;" in text or "Eiffel" in text
    assert "Iron lattice tower" in text
    assert "120 m from point" in text
    assert "imgs/Q9_Eiffel.jpg" in text
    assert "no image" in text
    assert "geohash" in text.lower()
    assert "Q10" in text


def test_html_img_src_keeps_path_separator(tmp_path):
    """img src must contain a literal '/' between dir and file (regression:
    urllib.parse.quote was encoding the separator as %2F)."""
    sights = [
        ss.Sight(qid="Q1", label="A", image_path=str(tmp_path / "imgs" / "Q1_a.jpg")),
    ]
    (tmp_path / "imgs").mkdir()
    (tmp_path / "imgs" / "Q1_a.jpg").write_bytes(b"fake")
    out = ss.write_html_report(sights, 0.0, 0.0, 100.0,
                               tmp_path / "report.html", tmp_path / "imgs")
    text = out.read_text(encoding="utf-8")
    assert 'src="imgs/Q1_a.jpg"' in text
    assert "%2F" not in text


def test_html_img_src_separate_dirs(tmp_path):
    """When the image dir is a sibling of the report dir, the relative path
    uses '../' — the separator must survive quoting."""
    sights = [
        ss.Sight(qid="Q2", label="B", image_path=str(tmp_path / "img" / "Q2_b.jpg")),
    ]
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "Q2_b.jpg").write_bytes(b"fake")
    out = ss.write_html_report(sights, 0.0, 0.0, 100.0,
                               tmp_path / "out" / "report.html", tmp_path / "img")
    text = out.read_text(encoding="utf-8")
    assert 'src="../img/Q2_b.jpg"' in text
    assert "%2F" not in text


def test_print_summary_no_sights(capsys):
    ss.print_summary(0.0, 0.0, 100.0, [])
    out = capsys.readouterr().out
    assert "No sights found" in out
    assert "geohash" in out


def test_main_end_to_end(tmp_path, monkeypatch, capsys):
    fake_bindings = [
        {
            "item": {"value": "http://www.wikidata.org/entity/Q42"},
            "itemLabel": {"value": "Douglas Adams"},
            "itemDescription": {"value": "author"},
            "coord": {"value": "Point(2.0 48.0)"},
            "distance": {"value": "0.1"},
            "image": {"value": "https://commons.wikimedia.org/wiki/File:DA.jpg"},
        }
    ]
    monkeypatch.setattr(ss, "run_sparql", lambda q, s, timeout=60: fake_bindings)

    def fake_download(url, dest, session, timeout=30):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"img")
        return True

    monkeypatch.setattr(ss, "download_image", fake_download)

    out_html = tmp_path / "r.html"
    out_json = tmp_path / "r.json"
    imgs = tmp_path / "imgs"
    rc = ss.main([
        "--lat", "48.0", "--lon", "2.0", "--radius", "500",
        "--out", str(out_html), "--images", str(imgs), "--json", str(out_json),
    ])
    assert rc == 0
    assert out_html.exists()
    assert out_json.exists()
    assert (imgs / "Q42_DA.jpg").exists()
    stdout = capsys.readouterr().out
    assert "Douglas Adams" in stdout
    assert "100 m" in stdout


def test_main_rejects_bad_coords():
    assert ss.main(["--lat", "91", "--lon", "0"]) == 2
    assert ss.main(["--lat", "0", "--lon", "-181"]) == 2
