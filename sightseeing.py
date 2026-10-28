#!/usr/bin/env python3
"""Find sightseeing targets near a geohash point (Wikidata-powered).

The Sightseeing achievement in geohashing requires visiting a coordinate
together with a notable point of interest. This tool takes a lat/lon pair
(WGS84) and a radius in meters, queries Wikidata for notable places within
that radius, downloads their photos from Wikimedia Commons, and produces:

  * a terminal summary, and
  * a self-contained-ish HTML report with large, easy-to-inspect images.

Usage:
    python sightseeing.py --lat 47.6062 --lon -122.3321 --radius 1000
    python sightseeing.py --lat 47.6062 --lon -122.3321 --radius 2000 \
        --limit 30 --out report.html --images ./photos
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "geohash-sightseeing-tool/1.0 (https://github.com/temetavis/geohash-sightseeing)"

# Wikidata classes (P31/P279*) considered "sights worth visiting".
SIGHT_CLASSES = [
    "wd:Q570116",   # tourist attraction
    "wd:Q33506",    # museum
    "wd:Q4989906",  # monument
    "wd:Q2319498",  # cultural heritage monument
    "wd:Q16560",    # castle
    "wd:Q23413",    # castle (alt)
    "wd:Q41176",    # building
    "wd:Q4886",     # church building
    "wd:Q16970",    # cathedral
    "wd:Q32815",    # university
    "wd:Q43501",    # zoo
    "wd:Q1286517",  # botanical garden
    "wd:Q167346",   # botanical garden (alt)
    "wd:Q22698",    # park
    "wd:Q194195",   # amusement park
    "wd:Q57821",    # library
    "wd:Q7075",     # theatre
    "wd:Q24354",    # cinema
    "wd:Q41253",    # art museum
    "wd:Q210272",   # religious building
    "wd:Q3947",     # house
    "wd:Q11755880", # observation tower
    "wd:Q1144661",  # lighthouse
    "wd:Q39715",    # bridge
    "wd:Q12518",    # tower
    "wd:Q811979",   # architectural structure
    "wd:Q1549591",  # historic district
    "wd:Q5765596",  # archaeological site
    "wd:Q839954",   # archaeological site (alt)
    "wd:Q8502",     # mountain
]


@dataclass
class Sight:
    qid: str
    label: str
    description: str = ""
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None
    instance_of: list[str] = field(default_factory=list)
    image_url: str | None = None
    image_path: str | None = None  # local file, relative to report dir
    article: str | None = None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Geohash (base32) encoding, the classic algorithm used by geohashing.
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 9) -> str:
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    bits = []
    gh = []
    bit = 0
    ch = 0
    while len(gh) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        bit += 1
        if bit == 5:
            gh.append(_B32[ch])
            bit = 0
            ch = 0
    return "".join(gh)


def build_sparql(lat: float, lon: float, radius_m: float, limit: int) -> str:
    classes = " ".join(SIGHT_CLASSES)
    return f"""
SELECT DISTINCT ?item ?itemLabel ?itemDescription ?coord ?image ?article
       (?dist AS ?distance)
WHERE {{
  SERVICE wikibase:around {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon:.6f} {lat:.6f})"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "{radius_m / 1000.0:.6f}" .
    bd:serviceParam wikibase:distance ?dist .
  }}
  ?item wdt:P31/wdt:P279* ?class .
  VALUES ?class {{ {classes} }}
  OPTIONAL {{ ?item wdt:P18 ?image . }}
  OPTIONAL {{
    ?article schema:about ?item .
    ?article schema:isPartOf <https://en.wikipedia.org/> .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,de,fr,es". }}
}}
ORDER BY ?distance
LIMIT {int(limit)}
"""


def run_sparql(query: str, session: requests.Session, timeout: int = 60) -> list[dict]:
    resp = session.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": USER_AGENT,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", {}).get("bindings", [])


def parse_coord(binding: dict) -> tuple[float | None, float | None]:
    coord = binding.get("coord", {}).get("value", "")
    m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", coord)
    if not m:
        return None, None
    return float(m.group(2)), float(m.group(1))  # WKT is lon lat


def commons_thumb_url(filename: str, width: int = 1280) -> str:
    """Build a thumbnail URL for a Commons file title ('File:...')."""
    name = filename.split(":", 1)[-1].replace(" ", "_")
    quoted = urllib.parse.quote(name)
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/"
        f"{digest[0]}/{digest[:2]}/{quoted}/{width}px-{quoted}"
    )


def is_image_response(resp: requests.Response) -> bool:
    ctype = resp.headers.get("Content-Type", "")
    return resp.status_code == 200 and (
        ctype.startswith("image/") or "octet-stream" in ctype
    )


def download_image(url: str, dest: Path, session: requests.Session, timeout: int = 30) -> bool:
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True)
        if not is_image_response(resp):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)
        return dest.stat().st_size > 0
    except requests.RequestException:
        return False


def fetch_sights(lat: float, lon: float, radius_m: float, limit: int,
                 session: requests.Session | None = None) -> list[Sight]:
    session = session or requests.Session()
    query = build_sparql(lat, lon, radius_m, limit)
    bindings = run_sparql(query, session)
    sights: list[Sight] = []
    seen: set[str] = set()
    for b in bindings:
        qid = b.get("item", {}).get("value", "").rsplit("/", 1)[-1]
        if not qid or qid in seen:
            continue
        seen.add(qid)
        plat, plon = parse_coord(b)
        dist = None
        if b.get("distance", {}).get("value"):
            dist = float(b["distance"]["value"]) * 1000.0
        elif plat is not None and plon is not None:
            dist = haversine_m(lat, lon, plat, plon)
        image = b.get("image", {}).get("value")
        sights.append(
            Sight(
                qid=qid,
                label=b.get("itemLabel", {}).get("value", qid),
                description=b.get("itemDescription", {}).get("value", ""),
                lat=plat,
                lon=plon,
                distance_m=dist,
                image_url=image,
                article=b.get("article", {}).get("value"),
            )
        )
    sights.sort(key=lambda s: (s.distance_m is None, s.distance_m or 0.0))
    return sights


def download_images(sights: list[Sight], image_dir: Path,
                    session: requests.Session | None = None,
                    thumb_width: int = 1280) -> int:
    """Download one image per sight; returns count of successes."""
    session = session or requests.Session()
    ok = 0
    for s in sights:
        if not s.image_url:
            continue
        filename = urllib.parse.unquote(s.image_url.rsplit("/", 1)[-1]).split(":", 1)[-1]
        safe = re.sub(r"[^\w.-]+", "_", filename)[:120]
        if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".svg")):
            safe += ".jpg"
        dest = image_dir / f"{s.qid}_{safe}"
        if dest.exists():
            s.image_path = str(dest)
            ok += 1
            continue
        thumb = commons_thumb_url("File:" + filename, width=thumb_width)
        if download_image(thumb, dest, session):
            s.image_path = str(dest)
            ok += 1
            continue
        # fall back to the Special:FilePath redirect, which resolves the
        # original file regardless of its exact name on Commons
        fp = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(filename)
        if download_image(fp, dest, session):
            s.image_path = str(dest)
            ok += 1
    return ok


def print_summary(lat: float, lon: float, radius_m: float, sights: list[Sight]) -> None:
    gh = geohash_encode(lat, lon)
    print(f"\n=== Sightseeing report ===")
    print(f"Center:   {lat:.6f}, {lon:.6f}  (geohash {gh})")
    print(f"Radius:   {radius_m:.0f} m")
    print(f"Found:    {len(sights)} sights\n")
    if not sights:
        print("No sights found. Try a bigger radius.")
        return
    for i, s in enumerate(sights, 1):
        dist = f"{s.distance_m:7.0f} m" if s.distance_m is not None else "   ?    "
        coord = f"{s.lat:.5f},{s.lon:.5f}" if s.lat is not None else "?"
        img = "[img]" if s.image_path else "     "
        print(f"{i:3}. {dist}  {img} {s.label}  ({coord})  [{s.qid}]")
        if s.description:
            print(f"      {s.description}")
    print()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sightseeing report - {lat:.6f}, {lon:.6f}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem; background: #f7f7f9; color: #222; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #555; margin-bottom: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 1.25rem; }}
  .card {{ background: #fff; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.12); overflow: hidden; display: flex; flex-direction: column; }}
  .card img {{ width: 100%; height: 320px; object-fit: cover; background: #ddd; cursor: zoom-in; }}
  .card .noimg {{ height: 320px; display: flex; align-items: center; justify-content: center; background: #e8e8ec; color: #888; }}
  .card .body {{ padding: .8rem 1rem 1rem; }}
  .card h2 {{ font-size: 1.05rem; margin: 0 0 .3rem; }}
  .card .desc {{ font-size: .85rem; color: #555; margin: 0 0 .5rem; }}
  .card .facts {{ font-size: .8rem; color: #333; display: flex; flex-wrap: wrap; gap: .9rem; }}
  .card .facts span {{ background: #eef1f6; border-radius: 6px; padding: .15rem .5rem; }}
  a {{ color: #06c; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  #lightbox {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 10; align-items: center; justify-content: center; }}
  #lightbox img {{ max-width: 95vw; max-height: 92vh; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Sightseeing near {lat:.6f}, {lon:.6f}</h1>
<div class="meta">
  Geohash <b>{geohash}</b> &middot; radius <b>{radius_m:.0f} m</b> &middot; {count} sights &middot;
  data: <a href="https://www.wikidata.org">Wikidata</a> / <a href="https://commons.wikimedia.org">Wikimedia Commons</a>
</div>
<div class="grid">
{cards}
</div>
<div id="lightbox" onclick="this.style.display='none'"><img id="lightbox-img" alt=""></div>
<script>
document.querySelectorAll('.card img').forEach(function (img) {{
  img.addEventListener('click', function () {{
    var lb = document.getElementById('lightbox');
    document.getElementById('lightbox-img').src = img.src;
    lb.style.display = 'flex';
  }});
}});
</script>
</body>
</html>
"""


def sight_card(s: Sight, lat: float, lon: float, image_dir: Path, report_dir: Path) -> str:
    if s.image_path:
        src = os.path.relpath(s.image_path, report_dir)
        src = src.replace(os.sep, "/").replace("\\", "/")
        src = urllib.parse.quote(src, safe="/")
        media = f'<img src="{src}" alt="{html.escape(s.label)}" loading="lazy">'
    else:
        media = '<div class="noimg">no image</div>'
    facts = []
    if s.distance_m is not None:
        facts.append(f"<span>{s.distance_m:.0f} m from point</span>")
    if s.lat is not None and s.lon is not None:
        g = geohash_encode(s.lat, s.lon, 7)
        maps = f"https://www.openstreetmap.org/?mlat={s.lat}&mlon={s.lon}#map=17/{s.lat}/{s.lon}"
        facts.append(f'<span><a href="{maps}" target="_blank" rel="noopener">{s.lat:.5f}, {s.lon:.5f}</a> (geohash {g})</span>')
    facts.append(f'<span><a href="https://www.wikidata.org/wiki/{s.qid}" target="_blank" rel="noopener">{s.qid}</a></span>')
    if s.article:
        facts.append(f'<span><a href="{html.escape(s.article)}" target="_blank" rel="noopener">Wikipedia article</a></span>')
    desc = f'<p class="desc">{html.escape(s.description)}</p>' if s.description else ""
    return (
        '<div class="card">'
        f"{media}"
        '<div class="body">'
        f"<h2>{html.escape(s.label)}</h2>"
        f"{desc}"
        f'<div class="facts">{"".join(facts)}</div>'
        "</div></div>"
    )


def write_html_report(sights: list[Sight], lat: float, lon: float, radius_m: float,
                      out_path: Path, image_dir: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cards = "\n".join(
        sight_card(s, lat, lon, image_dir, out_path.parent) for s in sights
    )
    page = HTML_TEMPLATE.format(
        lat=lat,
        lon=lon,
        geohash=geohash_encode(lat, lon),
        radius_m=radius_m,
        count=len(sights),
        cards=cards,
    )
    out_path.write_text(page, encoding="utf-8")
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sightseeing",
        description="Find sightseeing targets (Wikidata) near a geohash point.",
    )
    p.add_argument("--lat", type=float, required=True, help="Latitude (WGS84, decimal degrees)")
    p.add_argument("--lon", type=float, required=True, help="Longitude (WGS84, decimal degrees)")
    p.add_argument("--radius", type=float, default=1000.0,
                   help="Search radius in meters (default: 1000)")
    p.add_argument("--limit", type=int, default=50,
                   help="Maximum number of results (default: 50)")
    p.add_argument("--out", default="sightseeing_report.html",
                   help="Output HTML report path (default: sightseeing_report.html)")
    p.add_argument("--images", default="sightseeing_images",
                   help="Directory for downloaded images (default: sightseeing_images)")
    p.add_argument("--no-images", action="store_true",
                   help="Skip downloading images (HTML will have no photos)")
    p.add_argument("--no-html", action="store_true",
                   help="Skip writing the HTML report (terminal summary only)")
    p.add_argument("--json", default=None,
                   help="Also write raw results to this JSON file")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not (-90 <= args.lat <= 90) or not (-180 <= args.lon <= 180):
        print("error: lat/lon out of range", file=sys.stderr)
        return 2
    session = requests.Session()
    print(f"Querying Wikidata for sights within {args.radius:.0f} m of "
          f"{args.lat:.6f}, {args.lon:.6f} ...")
    try:
        sights = fetch_sights(args.lat, args.lon, args.radius, args.limit, session)
    except requests.RequestException as exc:
        print(f"error: Wikidata query failed: {exc}", file=sys.stderr)
        return 1

    image_dir = Path(args.images)
    if not args.no_images and sights:
        print(f"Downloading images to {image_dir}/ ...")
        ok = download_images(sights, image_dir, session)
        print(f"  {ok}/{len(sights)} images downloaded")

    print_summary(args.lat, args.lon, args.radius, sights)

    if args.json:
        payload = [
            {
                "qid": s.qid,
                "label": s.label,
                "description": s.description,
                "lat": s.lat,
                "lon": s.lon,
                "distance_m": s.distance_m,
                "image": s.image_path,
                "article": s.article,
            }
            for s in sights
        ]
        Path(args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"JSON written to {args.json}")

    if not args.no_html:
        out = write_html_report(sights, args.lat, args.lon, args.radius,
                                Path(args.out), image_dir)
        print(f"HTML report written to {out.resolve()}")
        print(f"Open it in a browser to inspect the photos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
