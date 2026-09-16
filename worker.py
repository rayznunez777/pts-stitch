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
            else:
                _, svg_path, fmt, out_path = parts
                run_job(svg_path, fmt, out_path)
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                raise RuntimeError("engine produced no output")
            sys.stdout.write("OK\t%s\n" % out_path)
        except Exception as exc:                               # noqa: BLE001
            msg = "%s: %s" % (type(exc).__name__, exc)
            print(traceback.format_exc()[-2000:], file=sys.stderr)
            sys.stdout.write("ERR\t%s\n" % msg.replace("\n", " ")[:900])
        sys.stdout.flush()


if __name__ == "__main__":
    main()
