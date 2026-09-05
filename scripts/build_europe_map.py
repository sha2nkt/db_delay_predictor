"""Build static/europe-map.js: one SVG path per country, projected and simplified,
for the country leaderboard's choropleth.

Source: Natural Earth 1:50m admin-0 countries (public domain). The geometry is
projected with a Lambert azimuthal equal-area centred on Europe (the ETRS89-LAEA
convention Eurostat uses: 10°E / 52°N), clipped to a lon/lat window, simplified
with Douglas-Peucker in pixel space and written as a plain JS object, so the page
needs no map library and no runtime download. Re-run only when the window or the
resolution changes; the output is committed.
"""

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
          "geojson/ne_50m_admin_0_countries.geojson")


def laea(lon: float, lat: float, lon0: float, lat0: float) -> tuple[float, float]:
    """Spherical Lambert azimuthal equal-area (Snyder 1987, eq. 24-2..24-4), unit sphere."""
    lam, phi = math.radians(lon - lon0), math.radians(lat)
    phi0 = math.radians(lat0)
    cos_c = math.sin(phi0) * math.sin(phi) + math.cos(phi0) * math.cos(phi) * math.cos(lam)
    k = math.sqrt(2 / (1 + cos_c))
    x = k * math.cos(phi) * math.sin(lam)
    y = k * (math.cos(phi0) * math.sin(phi) - math.sin(phi0) * math.cos(phi) * math.cos(lam))
    return x, y


def clip_ring(ring: list[tuple[float, float]], xmin, ymin, xmax, ymax) -> list[tuple[float, float]]:
    """Sutherland-Hodgman polygon clip against an axis-aligned rectangle."""
    def clip(points, inside, intersect):
        out = []
        if not points:
            return out
        prev = points[-1]
        for cur in points:
            if inside(cur):
                if not inside(prev):
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(intersect(prev, cur))
            prev = cur
        return out

    def x_at(p, q, x):
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))

    def y_at(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)

    ring = clip(ring, lambda p: p[0] >= xmin, lambda p, q: x_at(p, q, xmin))
    ring = clip(ring, lambda p: p[0] <= xmax, lambda p, q: x_at(p, q, xmax))
    ring = clip(ring, lambda p: p[1] >= ymin, lambda p, q: y_at(p, q, ymin))
    ring = clip(ring, lambda p: p[1] <= ymax, lambda p, q: y_at(p, q, ymax))
    return ring


def simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker on an open polyline (call with the ring closed)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if seg == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dx * (ay - py) - (ax - px) * dy) / seg
            if d > best:
                best, best_i = d, i
        if best > tolerance:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(points, keep) if k]


def ring_area(ring) -> float:
    """Signed shoelace area (SVG pixel space, y down)."""
    a = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        a += x0 * y1 - x1 * y0
    return a / 2


def ring_centroid(ring) -> tuple[float, float]:
    a = ring_area(ring)
    if abs(a) < 1e-9:
        return ring[0]
    cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return cx / (6 * a), cy / (6 * a)


def fmt(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--geojson", type=Path, default=None,
                        help="local Natural Earth 50m admin-0 GeoJSON (downloaded when omitted)")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "static" / "europe-map.js")
    parser.add_argument("--width", type=float, default=1000, help="viewBox width in px (default: 1000)")
    parser.add_argument("--bbox", type=float, nargs=4, default=(-11.0, 35.0, 31.0, 64.5),
                        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                        help="lon/lat window to keep (default: Portugal to the Baltics, Malta to mid-Scandinavia)")
    parser.add_argument("--center", type=float, nargs=2, default=(10.0, 52.0), metavar=("LON", "LAT"),
                        help="projection centre (default: ETRS89-LAEA, 10E 52N)")
    parser.add_argument("--tolerance", type=float, default=0.7, help="Douglas-Peucker tolerance in px (default: 0.7)")
    parser.add_argument("--min-area", type=float, default=2.5, help="drop rings smaller than this many px² (default: 2.5)")
    args = parser.parse_args()

    if args.geojson:
        data = json.loads(args.geojson.read_text(encoding="utf-8"))
    else:
        print(f"downloading {NE_URL}", file=sys.stderr)
        with urllib.request.urlopen(NE_URL, timeout=120) as resp:
            data = json.load(resp)

    lon0, lat0 = args.center
    lon_min, lat_min, lon_max, lat_max = args.bbox
    # the projected window is the bounding box of the lon/lat window's outline
    edge = []
    for i in range(201):
        t = i / 200
        edge.append((lon_min + t * (lon_max - lon_min), lat_min))
        edge.append((lon_min + t * (lon_max - lon_min), lat_max))
        edge.append((lon_min, lat_min + t * (lat_max - lat_min)))
        edge.append((lon_max, lat_min + t * (lat_max - lat_min)))
    proj_edge = [laea(lon, lat, lon0, lat0) for lon, lat in edge]
    xmin, xmax = min(p[0] for p in proj_edge), max(p[0] for p in proj_edge)
    ymin, ymax = min(p[1] for p in proj_edge), max(p[1] for p in proj_edge)
    scale = args.width / (xmax - xmin)
    height = (ymax - ymin) * scale

    def to_px(lon, lat):
        x, y = laea(lon, lat, lon0, lat0)
        return ((x - xmin) * scale, (ymax - y) * scale)

    countries = {}
    total_points = 0
    for feature in data["features"]:
        props = feature["properties"]
        code = props.get("ISO_A2_EH") or props.get("ISO_A2")
        if not code or code == "-99":
            continue
        geom = feature["geometry"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        paths = []
        largest = (0.0, None)
        for polygon in polygons:
            for ring in polygon:
                px = [to_px(lon, lat) for lon, lat, *_ in ring]
                px = clip_ring(px, 0, 0, args.width, height)
                if len(px) < 3:
                    continue
                area = abs(ring_area(px))
                if area < args.min_area:
                    continue
                if area > largest[0]:
                    largest = (area, px)
                simp = simplify(px + px[:1], args.tolerance)[:-1]
                if len(simp) < 3:
                    continue
                total_points += len(simp)
                paths.append("M" + "L".join(f"{fmt(x)} {fmt(y)}" for x, y in simp) + "Z")
        if not paths:
            continue
        cx, cy = ring_centroid(largest[1])
        countries[code] = {
            "name": props.get("NAME_EN") or props.get("NAME"),
            "d": "".join(paths),
            "c": [round(cx, 1), round(cy, 1)],
        }

    out = {
        "viewBox": f"0 0 {fmt(args.width)} {fmt(height)}",
        "countries": dict(sorted(countries.items())),
    }
    args.out.write_text(
        "/* Generated by scripts/build_europe_map.py from Natural Earth 1:50m admin-0"
        " countries (public domain). Do not edit by hand. */\n"
        "window.EUROPE_MAP = " + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    size = args.out.stat().st_size
    print(f"wrote {args.out}: {len(countries)} countries, {total_points} points, {size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
