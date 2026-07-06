"""
Pure geometry <-> GML XML conversion helpers (the GML profile used by
LoST-Sync coverage mappings, NENA-INF-027 AMS provisioning files).

No GIS data access, no LoST-protocol business logic — just shapely <-> GML
serialization/parsing. Used directly by src/federation/coverage.py and
src/federation/sync.py.
"""

from __future__ import annotations

from lxml import etree

from src.lost.wire import lost_xml


def _gml_add_ring(ring_el: etree._Element, coords) -> None:
    for lon, lat in coords:
        pos = etree.SubElement(ring_el, f"{{{lost_xml._NS_GML}}}pos")
        pos.text = f"{lat} {lon}"


def _gml_polygon(polygon) -> etree._Element:
    poly_el = etree.Element(
        f"{{{lost_xml._NS_GML}}}Polygon",
        nsmap={"gml": lost_xml._NS_GML},
    )
    poly_el.set("srsName", "urn:ogc:def::crs:EPSG::4326")
    ext_el  = etree.SubElement(poly_el, f"{{{lost_xml._NS_GML}}}exterior")
    ring_el = etree.SubElement(ext_el,  f"{{{lost_xml._NS_GML}}}LinearRing")
    _gml_add_ring(ring_el, polygon.exterior.coords)
    for interior in polygon.interiors:
        int_el  = etree.SubElement(poly_el, f"{{{lost_xml._NS_GML}}}interior")
        iring   = etree.SubElement(int_el,  f"{{{lost_xml._NS_GML}}}LinearRing")
        _gml_add_ring(iring, interior.coords)
    return poly_el


def _shapely_to_gml(geom) -> etree._Element:
    if geom.geom_type == "MultiPolygon":
        mp = etree.Element(
            f"{{{lost_xml._NS_GML}}}MultiPolygon",
            nsmap={"gml": lost_xml._NS_GML},
        )
        mp.set("srsName", "urn:ogc:def::crs:EPSG::4326")
        for polygon in geom.geoms:
            pm = etree.SubElement(mp, f"{{{lost_xml._NS_GML}}}polygonMember")
            pm.append(_gml_polygon(polygon))
        return mp
    return _gml_polygon(geom)


def _gml_ring_coords(ring_el: etree._Element) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for pos in ring_el.findall(f"{{{lost_xml._NS_GML}}}pos"):
        parts = (pos.text or "").split()
        if len(parts) >= 2:
            lat, lon = float(parts[0]), float(parts[1])
            coords.append((lon, lat))
    return coords


def _gml_polygon_to_shapely(poly_el: etree._Element):
    from shapely.geometry import Polygon as _Polygon
    ext_ring = poly_el.find(f"{{{lost_xml._NS_GML}}}exterior/{{{lost_xml._NS_GML}}}LinearRing")
    exterior = _gml_ring_coords(ext_ring) if ext_ring is not None else []
    interiors = [
        _gml_ring_coords(ir)
        for int_el in poly_el.findall(f"{{{lost_xml._NS_GML}}}interior")
        for ir in [int_el.find(f"{{{lost_xml._NS_GML}}}LinearRing")]
        if ir is not None
    ]
    return _Polygon(exterior, interiors)


def _gml_sb_to_shapely(sb_el: etree._Element):
    from shapely.geometry import MultiPolygon as _MultiPolygon
    mp_el = sb_el.find(f".//{{{lost_xml._NS_GML}}}MultiPolygon")
    if mp_el is not None:
        polys = []
        for pm_el in mp_el.findall(f"{{{lost_xml._NS_GML}}}polygonMember"):
            poly_el = pm_el.find(f"{{{lost_xml._NS_GML}}}Polygon")
            if poly_el is not None:
                polys.append(_gml_polygon_to_shapely(poly_el))
        return _MultiPolygon(polys)
    poly_el = sb_el.find(f".//{{{lost_xml._NS_GML}}}Polygon")
    if poly_el is not None:
        return _gml_polygon_to_shapely(poly_el)
    return None
