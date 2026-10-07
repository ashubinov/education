"""Безопасная обработка SVG из ответов модели + растеризация для Telegram."""
import re
import xml.etree.ElementTree as ET

ALLOWED_TAGS = {"svg", "g", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text", "tspan",
                "defs", "marker", "lineargradient", "radialgradient", "stop", "title", "desc"}
ALLOWED_ATTRS = {
    "viewbox", "xmlns", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points",
    "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "opacity",
    "fill-opacity", "stroke-opacity", "transform", "font-size", "font-family", "font-weight", "font-style",
    "text-anchor", "dominant-baseline", "id", "marker-end", "marker-start", "refx", "refy", "markerwidth",
    "markerheight", "orient", "offset", "stop-color", "stop-opacity", "gradientunits", "x-offset", "dx", "dy",
    "preserveaspectratio",
}
BAD_VALUE = re.compile(r"(javascript:|data:|expression|@import|url\(\s*['\"]?\s*(?!#))", re.I)
SVG_NS = "http://www.w3.org/2000/svg"


def sanitize(svg: str | None) -> str | None:
    if not svg or not isinstance(svg, str):
        return None
    s = svg.strip()
    m = re.search(r"<svg\b.*</svg>", s, flags=re.S | re.I)
    if not m:
        return None
    s = m.group(0)
    s = re.sub(r"<!(DOCTYPE|ENTITY)[^>]*>", "", s, flags=re.I)
    s = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", s)
    if len(s) > 12000:
        return None
    s = s.replace("xmlns:xlink", "data-x")
    try:
        root = ET.fromstring(s)
    except ET.ParseError:
        return None

    def clean(el) -> bool:
        tag = el.tag.split("}")[-1].lower() if isinstance(el.tag, str) else ""
        if tag not in ALLOWED_TAGS:
            return False
        for k in list(el.attrib):
            kl = k.split("}")[-1].lower()
            v = el.attrib[k]
            if kl not in ALLOWED_ATTRS or kl.startswith("on") or BAD_VALUE.search(v):
                del el.attrib[k]
        el.tag = tag
        for ch in list(el):
            if not clean(ch):
                el.remove(ch)
        return True

    if not clean(root):
        return None
    if "font-family" not in root.attrib:  # в <img> нет веб-шрифтов — задаём безопасный
        root.set("font-family", "Arial, Helvetica, sans-serif")
    # восстановить регистр camelCase-тегов/атрибутов для браузера
    out = ET.tostring(root, encoding="unicode")
    out = re.sub(r"\bxmlns:ns0=", "xmlns=", out)
    out = out.replace("ns0:", "")
    out = out.replace("lineargradient", "linearGradient").replace("radialgradient", "radialGradient")
    out = re.sub(r"\bviewbox=", "viewBox=", out)
    out = re.sub(r"\bmarkerwidth=", "markerWidth=", out)
    out = re.sub(r"\bmarkerheight=", "markerHeight=", out)
    out = re.sub(r"\brefx=", "refX=", out)
    out = re.sub(r"\brefy=", "refY=", out)
    out = re.sub(r"\bpreserveaspectratio=", "preserveAspectRatio=", out)
    out = re.sub(r"\bgradientunits=", "gradientUnits=", out)
    if "xmlns=" not in out.split(">", 1)[0]:
        out = out.replace("<svg", f"<svg xmlns='{SVG_NS}'", 1)
    if "viewBox" not in out.split(">", 1)[0]:
        out = out.replace("<svg", "<svg viewBox='0 0 400 240'", 1)
    return out


def to_png(svg: str) -> bytes | None:
    """SVG -> PNG через PyMuPDF (для Telegram). None, если не получилось."""
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None
