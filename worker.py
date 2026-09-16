"""
Long-lived Ink/Stitch worker.

Importing inkex, wxPython and shapely costs about eleven seconds, which is
most of the time a short design takes. Doing it once at start-up and then
looping over jobs removes that from every request.

Protocol, one job per line on stdin:

    <svg path>\t<format>\t<output path>

and one line back on stdout:

    OK\t<output path>      or      ERR\t<message>
"""

import os
import sys
import traceback

sys.path.insert(0, os.environ.get("INKSTITCH_DIR", "/opt/inkstitch"))
os.chdir(os.environ.get("INKSTITCH_DIR", "/opt/inkstitch"))

from lib import extensions  # noqa: E402


import io
import traceback as _tb


def _capture_stderr():
    """Extensions report their problems on stderr and then exit quietly, which
       leaves an empty output file and no explanation."""
    buf = io.StringIO()
    saved = sys.stderr
    sys.stderr = buf
    return buf, saved


def run_fill_to_satin(svg_path, ids, out_path):
    """Hand Ink/Stitch the filled shapes and their rungs and let it work out
       the rails. This is the geometry we deliberately do not do ourselves."""
    saved = os.dup(1)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    argv = sys.argv[:]
    # note: no --extension here. inkstitch.py strips that before handing the
    # rest to the extension, and this one's parser rejects anything unexpected.
    args = ["--id=%s" % i for i in ids] + \
           ["--keep=none", "--center=true", "--contour=true", str(svg_path)]
    try:
        os.dup2(fd, 1)
        sys.argv = [sys.argv[0]] + args
        extension = extensions.FillToSatin()
        try:
            extension.run(args=args)
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise RuntimeError("fill_to_satin exited with %s" % exc.code)
        except Exception as exc:                               # noqa: BLE001
            raise RuntimeError("fill_to_satin: %s" % exc)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(fd)
        sys.argv = argv


def run_stroke_to_satin(svg_path, ids, out_path):
    """Turn stroked centre lines into satin columns. Ink/Stitch builds the
       rails from the line and its stroke width, which is the whole point: the
       path says where the stitches run, and the width says how far."""
    saved = os.dup(1)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    argv = sys.argv[:]
    args = ["--id=%s" % i for i in ids] + [str(svg_path)]
    try:
        os.dup2(fd, 1)
        sys.argv = [sys.argv[0]] + args
        extension = extensions.StrokeToSatin()
        try:
            extension.run(args=args)
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise RuntimeError("stroke_to_satin exited with %s" % exc.code)
        except Exception as exc:                               # noqa: BLE001
            raise RuntimeError("stroke_to_satin: %s" % exc)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(fd)
        sys.argv = argv


def run_job(svg_path, fmt, out_path):
    """Run the output extension with stdout redirected into a file, which is
       where Ink/Stitch expects to write the embroidery data."""
    saved = os.dup(1)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    argv = sys.argv[:]
    try:
        os.dup2(fd, 1)
        sys.argv = [sys.argv[0], "--extension=output", "--format=%s" % fmt, svg_path]
        extension = extensions.Output()
        try:
            extension.run(args=["--extension=output", "--format=%s" % fmt, svg_path])
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise RuntimeError("engine exited with %s" % exc.code)
        except Exception as exc:                               # noqa: BLE001
            raise RuntimeError("stitch generation: %s" % exc)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(fd)
        sys.argv = argv


def main():
    # tell the parent we are ready only once the imports are done
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            parts = line.split("\t")
            kind = parts[0]
            if kind == "f2s":
                _, svg_path, ids, out_path = parts
                run_fill_to_satin(svg_path, [i for i in ids.split(",") if i], out_path)
            elif kind == "s2s":
                _, svg_path, ids, out_path = parts
                run_stroke_to_satin(svg_path, [i for i in ids.split(",") if i], out_path)
            else:
                _, svg_path, fmt, out_path = parts
                run_job(svg_path, fmt, out_path)
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                raise RuntimeError(
                    "the %s step produced an empty file. input %s (%d bytes), "
                    "selection %d items" % (
                        {"f2s": "rail", "s2s": "line to satin"}.get(kind, "stitch"),
                        os.path.basename(svg_path),
                        os.path.getsize(svg_path) if os.path.exists(svg_path) else -1,
                        len(parts[2].split(",")) if kind in ("f2s", "s2s") else 0))
            sys.stdout.write("OK\t%s\n" % out_path)
        except Exception as exc:                               # noqa: BLE001
            detail = traceback.format_exc()
            # the last real line of the traceback says far more than the type
            tail = [ln.strip() for ln in detail.strip().splitlines() if ln.strip()]
            msg = "%s: %s" % (type(exc).__name__, exc)
            if len(tail) > 1:
                msg += " || " + " <- ".join(tail[-3:])
            sys.stdout.write("ERR\t%s\n" % msg.replace("\t", " ").replace("\n", " ")[:900])
        sys.stdout.flush()


if __name__ == "__main__":
    main()
