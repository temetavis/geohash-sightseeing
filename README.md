# geohash-sightseeing

A small tool to help earn the **Sightseeing** achievement in
[geohashing](https://en.wikipedia.org/wiki/Geohashing): it takes a lat/lon
coordinate pair (WGS84) and a radius in meters, queries
[Wikidata](https://www.wikidata.org) for notable sights within that radius,
downloads their photos from [Wikimedia Commons](https://commons.wikimedia.org),
and produces a terminal summary plus an HTML report with large,
click-to-zoom images that are easy to inspect.

For every sight you get:

- name and short description (in your preferred languages, English first),
- distance from the target coordinate,
- the sight's own coordinates and geohash (so you can check graticule/30m-zone
  rules yourself),
- a link to the Wikidata item and, when available, the English Wikipedia article,
- a downloaded photo, shown large in the HTML report.

## Install

```bash
pip install .
# or just run the single-file script:
pip install requests
python sightseeing.py --help
```

## Usage

```bash
python sightseeing.py --lat 47.6062 --lon -122.3321 --radius 1000
```

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--lat` / `--lon` | (required) | Center point, WGS84 decimal degrees |
| `--radius` | `1000` | Search radius in meters |
| `--limit` | `50` | Maximum number of results |
| `--out` | `sightseeing_report.html` | HTML report path |
| `--images` | `sightseeing_images` | Directory for downloaded photos |
| `--no-images` | off | Skip photo downloads |
| `--no-html` | off | Terminal summary only |
| `--json PATH` | off | Also dump raw results as JSON |

Example:

```bash
python sightseeing.py --lat 48.8584 --lon 2.2945 --radius 500 \
    --limit 30 --out report.html --images photos --json results.json
```

Then open `report.html` in a browser: photos are shown as large cards and
clicking one opens a full-screen lightbox.

## How it works

1. A SPARQL query is sent to `https://query.wikidata.org/sparql` using the
   `wikibase:around` geo service, restricted to items that are instances of
   (subclasses of) sights: tourist attractions, museums, monuments, castles,
   churches, parks, towers, bridges, archaeological sites, etc.
2. Results are sorted by distance; each item's `P18` image (Commons file) is
   downloaded as a 1280 px thumbnail into the images directory.
3. A self-contained HTML page (images referenced relatively) plus a terminal
   summary are written out.

## Development

```bash
pip install -e ".[test]"
pytest
```

## License

MIT
