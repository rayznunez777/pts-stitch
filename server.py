"""
PTS Digitizer stitch service.

Wraps Ink/Stitch so the browser app can hand it an SVG plus a parameter set and
get back real stitches. Ink/Stitch reads its settings from attributes in its own
XML namespace on each element, so the work here is translating the app's
parameter names onto the SVG before running the engine headlessly.

    POST /digitize   {svg, params, objects, formats} -> stitches, colors, files
    GET  /health
"""

import base64
import json
import re
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from lxml import etree

INKSTITCH_DIR = Path(os.environ.get("INKSTITCH_DIR", "/opt/inkstitch"))
NS = "http://inkstitch.org/namespace"
SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("ALLOW_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return resp


@app.route("/digitize", methods=["OPTIONS"])
def digitize_preflight():
    return ("", 204)

# ---------------------------------------------------------------- parameters

# The app's own names on the left, Ink/Stitch's on the right. Anything not in
# this table is passed through unchanged, so a caller can set any Ink/Stitch
# parameter directly without waiting for the map to catch up.
FILL_MAP = {
    "fillAngle": "angle",
    "fillSpacing": "row_spacing_mm",
    "fillEndSpacing": "end_row_spacing_mm",
    "fillStitch": "max_stitch_length_mm",
    "fillStagger": "staggers",
    "fillSkipLast": "skip_last",
    "fillExpand": "expand_mm",
    "fillPullCompMM": "pull_compensation_mm",
    "fillPullCompPct": "pull_compensation_percent",
    "fillUnderpath": "underpath",
    "fillMethod": "fill_method",
    "meanderSize": "meander_scale_percent",
    "fuOn": "fill_underlay",
    "fuAngles": "fill_underlay_angle",
    "fuSpacing": "fill_underlay_row_spacing_mm",
    "fuInset": "fill_underlay_inset_mm",
    "fuStitch": "fill_underlay_max_stitch_length_mm",
    "fuSkipLast": "fill_underlay_skip_last",
    "fuUnderpath": "underlay_underpath",
}

SATIN_MAP = {
    "satinSpacing": "zigzag_spacing_mm",
    "satinMaxStitch": "max_stitch_length_mm",
    "satinSplitMethod": "split_method",
    "satinSplitStagger": "split_staggers",
    "shortStitchTrigger": "short_stitch_distance_mm",   # converted below
    "shortStitchInset": "short_stitch_inset",
    "pullCompMM": "pull_compensation_mm",
    "pullCompPct": "pull_compensation_percent",
    "pushComp": "push_compensation_mm",
    "suCenterOn": "center_walk_underlay",
    "suCenterRepeats": "center_walk_underlay_repeats",
    "suCenterPos": "center_walk_underlay_position",
    "suCenterLen": "center_walk_underlay_stitch_length_mm",
    "suContourOn": "contour_underlay",
    "suContourInsetFixed": "contour_underlay_inset_mm",
    "suContourInsetPct": "contour_underlay_inset_percent",
    "suContourLen": "contour_underlay_stitch_length_mm",
    "suZigzagOn": "zigzag_underlay",
    "suZigzagSpacing": "zigzag_underlay_spacing_mm",
    "suZigzagInsetFixed": "zigzag_underlay_inset_mm",
    "suZigzagInsetPct": "zigzag_underlay_inset_percent",
    "suZigzagMaxStitch": "zigzag_underlay_max_stitch_length_mm",
}

RUN_MAP = {
    "runStitch": "running_stitch_length_mm",
    "beanStitch": "bean_stitch_repeats",
}

ELEMENT_MAP = {
    "minStitchLength": "min_stitch_length_mm",
    "minJumpLength": "min_jump_stitch_length_mm",
    "forceLock": "force_lock_stitches",
    "trimDistance": "trim_after",       # boolean in Ink/Stitch; see below
}

FILL_METHODS = {
    "tatami": "auto_fill",
    "contour": "contour_fill",
    "guided": "guided_fill",
    "meander": "meander_fill",
    "circular": "circular_fill",
}


def fmt(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def translate(params, mapping, kind):
    """Turn one of the app's parameter dicts into Ink/Stitch attribute names."""
    out = {}
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        name = mapping.get(key)
        if name is None:
            # allow callers to set Ink/Stitch parameters directly
            if key.islower() and "_" in key:
                out[key] = fmt(value)
            continue
        if key == "fillMethod":
            out[name] = FILL_METHODS.get(str(value), "auto_fill")
            continue
        if key == "shortStitchTrigger":
            # the app expresses the trigger as a percentage of density;
            # Ink/Stitch wants it in millimetres
            density = float(params.get("satinSpacing", 0.4) or 0.4)
            out[name] = fmt(round(density * float(value) / 100.0, 3))
            continue
        if key == "beanStitch":
            out[name] = "2" if value else "0"
            continue
        if key == "meanderSize":
            out[name] = fmt(max(10, float(value) * 25))
            continue
        out[name] = fmt(value)
    return out


def apply_attrs(node, attrs):
    for name, value in attrs.items():
        node.set("{%s}%s" % (NS, name), value)


# ------------------------------------------------------------------ pipeline

INKSTITCH_SVG_VERSION = "4"


def set_svg_version(root):
    md = root.find("{%s}metadata" % SVG_NS)
    if md is None:
        md = etree.Element("{%s}metadata" % SVG_NS)
        root.insert(0, md)
    tag = "{%s}inkstitch_svg_version" % NS
    node = md.find(tag)
    if node is None:
        node = etree.SubElement(md, tag)
    node.text = INKSTITCH_SVG_VERSION


def prepare_svg(svg_text, params, objects):
    """Parse the SVG, wrap it in a layer if needed, and stamp parameters on
       every drawable element. Per-object settings override the document."""
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
    root = etree.fromstring(svg_text.encode("utf-8"), parser)

    doc_fill = translate(params, FILL_MAP, "fill")
    doc_satin = translate(params, SATIN_MAP, "satin")
    doc_run = translate(params, RUN_MAP, "run")
    doc_elem = translate(params, ELEMENT_MAP, "element")
    # trim_after is a boolean in Ink/Stitch; the app's value is a distance
    doc_elem.pop("trim_after", None)

    per_object = {o.get("id"): o for o in (objects or []) if o.get("id")}

    # Ink/Stitch upgrades documents that carry its parameters but no version
    # marker, and asks first via a dialog box. Headless, that dialog never
    # answers and the run hangs forever. Declaring the current version tells it
    # the document is already up to date.
    set_svg_version(root)

    # Ink/Stitch only looks at elements inside a layer
    layers = root.findall("{%s}g[@{%s}groupmode='layer']" % (SVG_NS, INKSCAPE_NS))
    if not layers:
        layer = etree.SubElement(root, "{%s}g" % SVG_NS)
        layer.set("{%s}groupmode" % INKSCAPE_NS, "layer")
        layer.set("{%s}label" % INKSCAPE_NS, "digitize")
        for child in list(root):
            if child is layer:
                continue
            root.remove(child)
            layer.append(child)

    count = 0
    for node in root.iter():
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag not in ("path", "rect", "circle", "ellipse", "polygon", "polyline", "line"):
            continue
        count += 1
        spec = per_object.get(node.get("id"), {})
        role = spec.get("role", "fill")

        attrs = dict(doc_elem)
        if role == "satin":
            # left as a fill here: the first pass converts it, and its settings
            # are applied afterwards once the satin column exists
            continue
        if role == "rung":
            continue
        if role == "run":
            attrs["stroke_method"] = "running_stitch"
            attrs.update(doc_run)
            attrs.update(translate(spec.get("params"), RUN_MAP, "run"))
        else:
            attrs.update(doc_fill)
            attrs.update(translate(spec.get("params"), FILL_MAP, "fill"))
        attrs.update(translate(spec.get("params"), ELEMENT_MAP, "element"))
        attrs.pop("trim_after", None)
        apply_attrs(node, attrs)

    layout = {"order": [], "bbox": {}}
    for node in root.iter():
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag != "path":
            continue
        nid = node.get("id")
        if not nid:
            continue
        layout["order"].append(nid)
        box = path_bbox(node.get("d", ""))
        if box:
            layout["bbox"][nid] = box

    return etree.tostring(root, xml_declaration=True, encoding="utf-8"), count, layout


_worker = None
_worker_lock = threading.Lock()
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "600"))


def read_line(stream, seconds, what):
    """A blocking read with no timeout turns any hiccup into a permanent hang,
       which from the outside looks exactly like the app being broken."""
    result = {}

    def pull():
        try:
            result["line"] = stream.readline()
        except Exception as exc:                                # noqa: BLE001
            result["error"] = str(exc)

    thread = threading.Thread(target=pull, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError("timed out after %ds waiting for %s" % (seconds, what))
    if "error" in result:
        raise RuntimeError(result["error"])
    return (result.get("line") or "").strip()


def worker_process():
    """Start the engine once and keep it warm. Importing inkex, wx and shapely
       is most of the cost of a short design, so it must not happen per call."""
    global _worker
    if _worker is not None and _worker.poll() is None:
        return _worker
    env = dict(os.environ)
    env["INKSTITCH_DIR"] = str(INKSTITCH_DIR)
    cmd = [sys.executable, "-u", str(Path(__file__).parent / "worker.py")]
    if os.environ.get("USE_XVFB", "1") == "1":
        # wxPython demands a display even on the headless path
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24"] + cmd
    _worker = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
    try:
        ready = read_line(_worker.stdout, int(os.environ.get("START_TIMEOUT", "120")),
                          "the engine to start")
    except TimeoutError:
        err = drain(_worker.stderr)
        _worker.kill()
        _worker = None
        raise RuntimeError("the engine did not start. " + (err or "no output from it"))
    if ready != "READY":
        err = drain(_worker.stderr)
        _worker = None
        raise RuntimeError("the engine failed to start. " + (err or ready or "no output"))
    return _worker


def drain(stream):
    """Whatever the engine managed to say before it gave up."""
    import select
    out = []
    try:
        while select.select([stream], [], [], 0.2)[0]:
            line = stream.readline()
            if not line:
                break
            out.append(line.rstrip())
    except Exception:                                           # noqa: BLE001
        pass
    return " | ".join(out[-6:])[:600]


def run_fill_to_satin(svg_bytes, ids):
    """First pass: turn filled shapes plus rungs into real satin columns."""
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "in.svg"
    dst = Path(tmp) / "out.svg"
    src.write_bytes(svg_bytes)
    with _worker_lock:
        proc = worker_process()
        proc.stdin.write("f2s\t%s\t%s\t%s\n" % (src, ",".join(ids), dst))
        proc.stdin.flush()
        reply = read_line(proc.stdout, JOB_TIMEOUT, "the rails to be worked out")
    if reply.startswith("ERR"):
        raise RuntimeError(reply.split("\t", 1)[-1])
    data = dst.read_bytes()
    if not data:
        raise RuntimeError("fill_to_satin produced nothing")
    return data


NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")


def path_bbox(d):
    nums = [float(v) for v in NUM.findall(d or "")]
    if len(nums) < 4:
        return None
    xs = nums[0::2]
    ys = nums[1::2]
    n = min(len(xs), len(ys))
    if not n:
        return None
    return (min(xs[:n]), min(ys[:n]), max(xs[:n]), max(ys[:n]))


def overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def convert_satins_in_place(svg_bytes, objects):
    """Convert one shape at a time. fill_to_satin gathers everything it makes
       into a single block, and its output carries its own transform, so
       matching columns back to their shapes afterwards is guesswork. Doing one
       shape per pass means ownership is known, and each column can be put back
       where its shape was — which matters because document order is sew order.
    """
    owners = {}
    for o in objects:
        if o.get("role") == "rung" and o.get("owner"):
            owners.setdefault(o["owner"], []).append(o["id"])
    shapes = [o["id"] for o in objects if o.get("role") == "satin" and owners.get(o["id"])]

    made = 0
    marker = "{%s}pts_done" % NS
    for shape_id in shapes:
        root = etree.fromstring(svg_bytes)
        layer = find_layer(root)
        if layer is None:
            break
        position, colour = None, None
        for i, child in enumerate(layer):
            if child.get("id") == shape_id:
                position = i
                colour = child.get("fill") or style_value(child.get("style"), "fill")
                break
        if position is None:
            continue

        svg_bytes = run_fill_to_satin(svg_bytes, [shape_id] + owners[shape_id])

        root = etree.fromstring(svg_bytes)
        layer = find_layer(root)
        # fill_to_satin wraps its output in a group when it splits a shape, so
        # the new columns are not direct children of the layer
        fresh = [n for n in layer.iter()
                 if isinstance(n.tag, str)
                 and etree.QName(n).localname == "path"
                 and n.get("{%s}satin_column" % NS) and not n.get(marker)]
        for node in fresh:
            node.set(marker, "1")
            # a satin column is a stroke, and fill_to_satin does not carry the
            # source colour across, so the thread has to be set here
            if colour and colour != "none":
                node.set("stroke", colour)
                node.set("fill", "none")
                style = node.get("style")
                if style:
                    node.set("style", set_style(style, {"stroke": colour, "fill": "none"}))
        # move whatever sits directly under the layer — the column itself, or
        # the group it was wrapped in — so the sew order stays put
        movers, seen = [], set()
        for node in fresh:
            top = node
            while top.getparent() is not None and top.getparent() is not layer:
                top = top.getparent()
            if top.getparent() is layer and id(top) not in seen:
                seen.add(id(top))
                movers.append(top)
        for offset, node in enumerate(movers):
            layer.remove(node)
            layer.insert(min(position + offset, len(layer)), node)
        made += len(fresh)
        svg_bytes = etree.tostring(root, xml_declaration=True, encoding="utf-8")

    return svg_bytes, made


def style_value(style, key):
    for part in (style or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() == key:
                return v.strip()
    return None


def set_style(style, updates):
    parts = {}
    for part in (style or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            parts[k.strip()] = v.strip()
    parts.update(updates)
    return ";".join("%s:%s" % (k, v) for k, v in parts.items())


def colour_order(svg_bytes):
    """DST carries no thread colours, so the block order has to come from the
       document: the colour of each stitchable element, in sew order, with
       runs of the same colour collapsed."""
    root = etree.fromstring(svg_bytes)
    layer = find_layer(root)
    if layer is None:
        return []
    out = []
    for node in layer.iter():
        if not isinstance(node.tag, str):
            continue
        if etree.QName(node).localname not in ("path", "rect", "circle", "ellipse", "polygon", "polyline", "line"):
            continue
        colour = node.get("stroke")
        if not colour or colour == "none":
            colour = node.get("fill")
        if not colour or colour == "none":
            colour = style_value(node.get("style"), "stroke") or style_value(node.get("style"), "fill")
        if not colour or colour == "none":
            continue
        colour = colour.strip().lower()
        if not out or out[-1] != colour:
            out.append(colour)
    return out


def find_layer(root):
    for node in root.iter():
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag == "g" and node.get("{%s}groupmode" % INKSCAPE_NS) == "layer":
            return node
    return None


def reorder_satins(svg_bytes, layout, satin_ids):
    """fill_to_satin gathers everything it converts into one block, which
       rewrites the sew order — and in embroidery the order is the design.
       Put each new column back where the shape it came from used to sit."""
    root = etree.fromstring(svg_bytes)
    layer = None
    for node in root.iter():
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag == "g" and node.get("{%s}groupmode" % INKSCAPE_NS) == "layer":
            layer = node
            break
    if layer is None:
        return svg_bytes, 0

    index_of = {nid: i for i, nid in enumerate(layout["order"])}
    sources = [(nid, layout["bbox"][nid]) for nid in satin_ids if nid in layout["bbox"]]

    children = [c for c in layer]
    keyed, satins = [], 0
    for pos, node in enumerate(children):
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        nid = node.get("id")
        if tag == "path" and nid in index_of:
            keyed.append((index_of[nid], pos, node))
            continue
        if tag == "path" and node.get("{%s}satin_column" % NS):
            satins += 1
            box = path_bbox(node.get("d", ""))
            best, best_score = None, 0.0
            if box:
                for sid, sbox in sources:
                    score = overlap_area(box, sbox)
                    if score > best_score:
                        best, best_score = sid, score
            keyed.append((index_of.get(best, len(index_of)), pos, node))
            continue
        keyed.append((len(index_of) + 1, pos, node))

    keyed.sort(key=lambda t: (t[0], t[1]))
    for _, _, node in keyed:
        layer.append(node)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8"), satins


def apply_satin_params(svg_bytes, params, per_object):
    """The satin paths only exist after the first pass, so their settings go on
       here, once Ink/Stitch has named them."""
    root = etree.fromstring(svg_bytes)
    attrs = translate(params, SATIN_MAP, "satin")
    attrs.update(translate(params, ELEMENT_MAP, "element"))
    attrs.pop("trim_after", None)
    tag = "{%s}satin_column" % NS
    n = 0
    for node in root.iter():
        if node.get(tag) in ("true", "True", "1"):
            apply_attrs(node, attrs)
            n += 1
    return etree.tostring(root, xml_declaration=True, encoding="utf-8"), n


def run_inkstitch(svg_bytes, fmt_name, timeout=600):
    tmp = tempfile.mkdtemp()
    src = Path(tmp) / "in.svg"
    dst = Path(tmp) / ("out." + fmt_name)
    src.write_bytes(svg_bytes)
    with _worker_lock:
        proc = worker_process()
        proc.stdin.write("out\t%s\t%s\t%s\n" % (src, fmt_name, dst))
        proc.stdin.flush()
        reply = read_line(proc.stdout, JOB_TIMEOUT, "the stitches")
    if reply.startswith("ERR"):
        raise RuntimeError(reply.split("\t", 1)[-1])
    if not reply.startswith("OK"):
        global _worker
        _worker = None
        raise RuntimeError("engine stopped responding")
    data = dst.read_bytes()
    try:
        src.unlink(); dst.unlink(); os.rmdir(tmp)
    except OSError:
        pass
    return data


def convert(dst_bytes, formats):
    """Ink/Stitch is the slow part, so run it once for DST and let pystitch
       write the other formats from the same stitch data."""
    import pystitch
    out = {"dst": dst_bytes}
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.dst"
        src.write_bytes(dst_bytes)
        pattern = pystitch.read(str(src))
        for name in formats:
            if name == "dst":
                continue
            target = Path(tmp) / ("a." + name)
            try:
                pystitch.write(pattern, str(target))
                out[name] = target.read_bytes()
            except Exception:                                  # noqa: BLE001
                continue
    return out


def stitch_list(dst_bytes):
    """Parse the DST back into the flat arrays the browser preview wants."""
    import pystitch
    with tempfile.NamedTemporaryFile(suffix=".dst", delete=False) as fh:
        fh.write(dst_bytes)
        path = fh.name
    try:
        pattern = pystitch.read(path)
        xs, ys, fs = [], [], []
        colors = []
        for x, y, cmd in pattern.stitches:
            if cmd == pystitch.STITCH:
                flag = 0
            elif cmd in (pystitch.JUMP, pystitch.TRIM):
                flag = 1
            elif cmd in (pystitch.COLOR_CHANGE, pystitch.COLOR_BREAK):
                flag = 2
            elif cmd == pystitch.END:
                flag = 4
            else:
                continue
            xs.append(round(x / 10.0, 3))
            ys.append(round(y / 10.0, 3))
            fs.append(flag)
        for thread in pattern.threadlist:
            colors.append("#%06X" % (thread.color & 0xFFFFFF))
        return {"x": xs, "y": ys, "f": fs}, colors
    finally:
        os.unlink(path)


def statistics(stitches):
    import math
    xs = [x for x, f in zip(stitches["x"], stitches["f"]) if f == 0]
    ys = [y for y, f in zip(stitches["y"], stitches["f"]) if f == 0]
    px = py = None
    longest = 0.0
    shortest = float("inf")
    thread = 0.0
    n = trims = colors = 0
    for x, y, f in zip(stitches["x"], stitches["y"], stitches["f"]):
        if f == 2:
            colors += 1
            continue
        if f == 4:
            continue
        if f == 1:
            trims += 1
            px, py = x, y
            continue
        if px is not None:
            d = math.hypot(x - px, y - py)
            thread += d
            longest = max(longest, d)
            if d > 0:
                shortest = min(shortest, d)
        px, py = x, y
        n += 1
    return {
        "stitches": n,
        "trims": trims,
        "colorChanges": colors,
        "thread": round(thread, 1),
        "bobbin": round(thread * 0.39, 1),
        "maxStitch": round(longest, 2),
        "minStitch": round(0 if shortest == float("inf") else shortest, 2),
        "width": round(max(xs) - min(xs), 2) if xs else 0,
        "height": round(max(ys) - min(ys), 2) if ys else 0,
    }


# ------------------------------------------------------------------- routes

@app.get("/health")
def health():
    return jsonify({"ok": True, "engine": "inkstitch", "dir": str(INKSTITCH_DIR)})


@app.get("/selftest")
def selftest():
    """Push a tiny satin column through the whole pipeline. If this answers,
       the engine is alive and the app should work."""
    svg = ('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
           'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
           'width="48mm" height="12mm" viewBox="-4 -4 48 12">'
           '<g inkscape:groupmode="layer" inkscape:label="l">'
           '<path id="s1" d="M0,0 L40,0 M0,4 L40,4" fill="none" stroke="#1b3a6b" '
           'stroke-width="0.3"/></g></svg>')
    started = time.time()
    try:
        prepared, count, layout = prepare_svg(svg, {"satinSpacing": 0.4},
                                              [{"id": "s1", "role": "run"}])
        root = etree.fromstring(prepared)
        for node in root.iter():
            if node.get("id") == "s1":
                apply_attrs(node, {"satin_column": "true", "zigzag_spacing_mm": "0.4"})
        prepared = etree.tostring(root, xml_declaration=True, encoding="utf-8")
        dst = run_inkstitch(prepared, "dst")
        stitches, _ = stitch_list(dst)
        return jsonify({"ok": True, "stitches": len(stitches["x"]),
                        "ms": int((time.time() - started) * 1000)})
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc),
                        "ms": int((time.time() - started) * 1000)}), 500


@app.post("/digitize")
def digitize():
    body = request.get_json(force=True, silent=True) or {}
    svg = body.get("svg")
    if not svg:
        return jsonify({"error": "no svg supplied"}), 400
    formats = [f.lower() for f in (body.get("formats") or ["dst"])]
    if "dst" not in formats:
        formats.insert(0, "dst")

    started = time.time()
    try:
        prepared, count, layout = prepare_svg(svg, body.get("params") or {}, body.get("objects") or [])
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"error": "could not read that SVG: %s" % exc}), 400
    if not count:
        return jsonify({"error": "no drawable elements found in the SVG"}), 400

    try:
        satin_ids = [o["id"] for o in (body.get("objects") or [])
                     if o.get("role") in ("satin", "rung")]
        satins = 0
        dbg = os.environ.get("DEBUG_DIR")
        if dbg:
            Path(dbg, "1-prepared.svg").write_bytes(prepared)
        if satin_ids:
            prepared, made = convert_satins_in_place(prepared, body.get("objects") or [])
            if dbg:
                Path(dbg, "2-after-fill-to-satin.svg").write_bytes(prepared)
            prepared, satins = apply_satin_params(prepared, body.get("params") or {},
                                                  body.get("objects") or [])
            if dbg:
                Path(dbg, "3-final.svg").write_bytes(prepared)
        dst = run_inkstitch(prepared, "dst", timeout=int(body.get("timeout") or 600))
        files = convert(dst, formats)
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        return jsonify({"error": "the engine timed out on this design: %s" % exc}), 504
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    stitches, colors = stitch_list(files["dst"])
    from_doc = colour_order(prepared)
    if from_doc:
        colors = from_doc
    return jsonify({
        "stitches": stitches,
        "colors": colors,
        "stats": statistics(stitches),
        "elements": count,
        "satinColumns": satins,
        "ms": int((time.time() - started) * 1000),
        "files": {k: base64.b64encode(v).decode("ascii") for k, v in files.items()},
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
