#!/usr/bin/env python3
"""mp4-moov-check: find out why an MP4 or MOV file will not play.

The usual culprit after an interrupted recording is a missing index, the
"moov" box (ffmpeg: "moov atom not found"). This tool walks the file's
top-level boxes and, when there is one, the index, and names what it found:

  ok              index present and pointing inside the file
  ok_fragmented   fragmented MP4 with its header: plays as is
  missing_index   no moov box, the media data is there ("moov atom not found")
  empty_index     a moov box that lists no frames, the media data is there
  missing_init    fragments (moof) without their moov header
  index_past_end  index present, but the file was cut short after it
  index_damaged   the file ends inside the moov box
  no_media        neither an index nor media data
  zeros           the file is empty or all zeros
  not_mp4         no MP4/MOV structure at the start of the file

It reads box headers and the index tables only, never writes, and makes no
network connection. Python 3.8+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import textwrap
import unicodedata

__version__ = "1.0.0"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".lrv", ".insv", ".mqv"}
OK_VERDICTS = {"ok", "ok_fragmented"}

# Box types a file may start with. Anything else at offset 0 is not MP4/MOV.
FIRST_BOX = {"ftyp", "styp", "moov", "mdat", "free", "skip", "wide", "uuid",
             "pnot", "junk", "meta", "moof", "sidx", "pdin", "PICT"}

LABELS = {
    "ok": "OK",
    "ok_fragmented": "OK (FRAGMENTED)",
    "missing_index": "MISSING INDEX",
    "empty_index": "EMPTY INDEX",
    "missing_init": "MISSING HEADER",
    "index_past_end": "CUT SHORT",
    "index_damaged": "INDEX DAMAGED",
    "no_media": "NO MEDIA",
    "zeros": "EMPTY",
    "not_mp4": "NOT MP4/MOV",
}

MESSAGES = {
    "ok": "The index (moov) is present and points inside the file. If the video still "
          "won't play, the cause is something else: an unsupported codec, or damaged "
          "video data.",
    "ok_fragmented": "A fragmented MP4 with its header (moov + moof fragments). It should "
                     "play as is.",
    "missing_index": "moov atom not found: the video data is in the file, but the index "
                     "was never written. The recording stopped before it finished (flat "
                     "battery, crash, card pulled, storage full). The footage is usually "
                     "still there, and the index can be rebuilt from it.",
    "empty_index": "The index (moov) exists but lists no frames: the recorder wrote an "
                   "empty index up front and never filled it in. The footage after it is "
                   "usually still there, and the index can be rebuilt from it.",
    "missing_init": "Fragments (moof) without the moov header: a fragmented recording "
                    "that lost its start. The fragments themselves are still in the file.",
    "index_past_end": "The index is there but points past the end of the file: the file "
                      "was cut short (an incomplete copy, transfer or download). What comes "
                      "before the cut can often be recovered.",
    "index_damaged": "The file ends inside its index (moov): it was cut short while or "
                     "after the index was written.",
    "no_media": "No index, and no media data in the file: there is no video here to "
                "rebuild.",
    "zeros": "The file is empty or filled with zeros: the data was never written, or was "
             "wiped. Nothing to rebuild.",
    "not_mp4": "No MP4/MOV structure at the start of the file: it is another format, or "
               "its first bytes are damaged.",
}


class Box:
    """One box header: where it starts, how big it says it is."""

    __slots__ = ("type", "offset", "size", "header", "to_eof", "cut")

    def __init__(self, btype, offset, size, header, to_eof=False, cut=False):
        self.type = btype
        self.offset = offset
        self.size = size
        self.header = header
        self.to_eof = to_eof  # the size field said "until the end of the file"
        self.cut = cut        # the declared size runs past the end of the file

    @property
    def end(self):
        return self.offset + self.size

    def as_dict(self):
        d = {"type": self.type, "offset": self.offset, "size": self.size}
        if self.to_eof:
            d["to_end_of_file"] = True
        if self.cut:
            d["cut_short"] = True
        return d


def _printable(raw):
    return all(0x20 <= c <= 0x7E for c in raw)


def read_header(f, offset, limit):
    """The box starting at [offset], or None when no valid header is there.
    [limit] is where the enclosing space ends (the file size at top level)."""
    if offset + 8 > limit:
        return None
    f.seek(offset)
    head = f.read(8)
    if len(head) < 8:
        return None
    size, raw = struct.unpack(">I4s", head)
    if not _printable(raw):
        return None
    btype = raw.decode("latin-1")
    header, to_eof = 8, False
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size, header = struct.unpack(">Q", ext)[0], 16
        if size == 0:
            # A 64-bit size still at zero: the writer never came back to fill it in.
            size, to_eof = limit - offset, True
    elif size == 0:
        size, to_eof = limit - offset, True
    if size < header:
        return None
    return Box(btype, offset, size, header, to_eof, offset + size > limit)


MAX_BOXES = 200000  # a fragmented hour at one frame per fragment; more is not a real file


def walk(f, start, end):
    """Boxes laid end to end in [start, end), up to the first one that is
    unreadable or runs past [end], and never more than MAX_BOXES (a file of
    millions of empty boxes must not keep the check spinning). Returns
    (boxes, offset where it stopped)."""
    boxes, pos = [], start
    while pos + 8 <= end and len(boxes) < MAX_BOXES:
        box = read_header(f, pos, end)
        if box is None:
            break
        boxes.append(box)
        if box.cut:
            return boxes, end
        pos = box.end
    return boxes, pos


class _File:
    def __init__(self, f, size):
        self.f, self.size = f, size
        self._kids = {}

    def children(self, box):
        """A container's boxes, walked once however often they are asked for."""
        if box.offset not in self._kids:
            start = box.offset + box.header
            self._kids[box.offset] = walk(self.f, start, min(box.end, self.size))[0]
        return self._kids[box.offset]

    def child(self, box, *path):
        cur = box
        for name in path:
            cur = next((k for k in self.children(cur) if k.type == name), None)
            if cur is None:
                return None
        return cur

    def payload(self, box, limit=1 << 16):
        start = box.offset + box.header
        n = min(box.end, self.size) - start
        if n <= 0:
            return b""
        self.f.seek(start)
        return self.f.read(min(n, limit))


def _seconds(payload, at_v0, at_v1):
    """Duration in seconds from an mvhd/mdhd payload (version 0 or 1)."""
    if not payload:
        return None
    if payload[0] == 1:
        if len(payload) < at_v1 + 12:
            return None
        scale, dur = struct.unpack(">IQ", payload[at_v1:at_v1 + 12])
    else:
        if len(payload) < at_v0 + 8:
            return None
        scale, dur = struct.unpack(">II", payload[at_v0:at_v0 + 8])
    return round(dur / scale, 3) if scale else None


def _track(fx, trak):
    info = {}
    hdlr = fx.child(trak, "mdia", "hdlr")
    if hdlr:
        p = fx.payload(hdlr, 64)
        if len(p) >= 12:
            kind = p[8:12].decode("latin-1", "replace")
            info["kind"] = {"vide": "video", "soun": "audio"}.get(kind, kind)
    mdhd = fx.child(trak, "mdia", "mdhd")
    if mdhd:
        secs = _seconds(fx.payload(mdhd, 64), 12, 20)
        if secs is not None:
            info["seconds"] = secs
    stbl = fx.child(trak, "mdia", "minf", "stbl")
    if stbl is None:
        info["samples"] = 0
        return info
    kids = {}
    for k in fx.children(stbl):
        kids.setdefault(k.type, k)
    stsd = kids.get("stsd")
    if stsd:
        p = fx.payload(stsd, 64)
        if len(p) >= 16:
            info["codec"] = p[12:16].decode("latin-1", "replace")
    stsz = kids.get("stsz") or kids.get("stz2")
    info["samples"] = 0
    if stsz:
        p = fx.payload(stsz, 64)
        if len(p) >= 12:
            info["samples"] = struct.unpack(">I", p[8:12])[0]
    co = kids.get("stco") or kids.get("co64")
    if co:
        width = 8 if co.type == "co64" else 4
        start, stop = co.offset + co.header, min(co.end, fx.size)
        if stop - start >= 8:
            fx.f.seek(start)
            declared = struct.unpack(">I", fx.f.read(8)[4:8])[0]
            # Never trust the declared count past what the box really holds.
            count = min(declared, (stop - start - 8) // width)
            if count:
                info["chunks"] = count
                info["last_chunk_offset"] = _max_offset(fx.f, start + 8, count, width)
    return info


def _max_offset(f, at, count, width, batch=1 << 16):
    """The largest entry of a chunk-offset table, read in slices so a huge
    table never has to sit in memory whole."""
    best, fmt = 0, ">%d" + ("Q" if width == 8 else "I")
    f.seek(at)
    while count > 0:
        raw = f.read(min(count, batch) * width)
        n = len(raw) // width
        if n == 0:
            break
        best = max(best, max(struct.unpack(fmt % n, raw[:n * width])))
        count -= n
    return best


def _has_data(f, lo, hi, window=65536):
    """Whether [lo, hi) holds anything but zeros, judged from its start,
    middle and end (a file pre-allocated by a camera is zeros where it
    stopped writing)."""
    if hi <= lo:
        return False
    for at in sorted({lo, lo + (hi - lo) // 2, max(lo, hi - window)}):
        f.seek(at)
        if f.read(min(window, hi - at)).strip(b"\x00"):
            return True
    return False


def _all_zero(f, size):
    return not _has_data(f, 0, size)


def check(path):
    """Everything this tool knows about one file, as a plain dict."""
    size = os.path.getsize(path)
    res = {"file": path, "size": size}
    with open(path, "rb") as f:
        fx = _File(f, size)
        boxes, stop = walk(f, 0, size)
        if not boxes or boxes[0].type not in FIRST_BOX:
            res["boxes"] = []
            res["verdict"] = "zeros" if _all_zero(f, size) else "not_mp4"
            return _finish(res)
        res["boxes"] = [b.as_dict() for b in boxes[:1000]]
        if len(boxes) > 1000:
            res["boxes_total"] = len(boxes)
        if len(boxes) >= MAX_BOXES:
            res["walk_stopped_at"] = stop  # the rest was not looked at
        elif stop < size:
            # Bytes after the last readable box: media data an unfinished
            # header never accounted for, or damage.
            res["unreadable_from"] = stop
            res["unreadable_bytes"] = size - stop
        moov = next((b for b in boxes if b.type == "moov"), None)
        has_moof = any(b.type == "moof" for b in boxes)
        # Bytes that hold something: inside mdat boxes, plus what follows the
        # last readable header (an mdat whose size was never filled in leaves
        # its frames there). Pre-allocated space full of zeros does not count.
        media = 0
        for b in boxes:
            if b.type == "mdat":
                lo, hi = b.offset + b.header, min(b.end, size)
                if hi > lo and _has_data(f, lo, hi):
                    media += hi - lo
        if stop < size and _has_data(f, stop, size):
            media += size - stop
        res["media_bytes"] = media

        if moov is None:
            if has_moof:
                res["verdict"] = "missing_init"
            elif media > 0:
                res["verdict"] = "missing_index"
            else:
                res["verdict"] = "no_media"
            return _finish(res)

        res["moov_at"] = "start" if all(b.type != "mdat" or b.offset > moov.offset
                                        for b in boxes) else "end"
        if moov.cut:
            res["verdict"] = "index_damaged"
            return _finish(res)

        mvhd = fx.child(moov, "mvhd")
        if mvhd:
            secs = _seconds(fx.payload(mvhd, 64), 12, 20)
            if secs is not None:
                res["seconds"] = secs
        fragmented = fx.child(moov, "mvex") is not None
        tracks = [_track(fx, t) for t in fx.children(moov) if t.type == "trak"]
        res["tracks"] = tracks
        if fragmented:
            res["verdict"] = "ok_fragmented" if has_moof else "no_media"
        elif any(t.get("last_chunk_offset", 0) >= size for t in tracks) or any(
                b.cut for b in boxes if b.type == "mdat"):
            res["verdict"] = "index_past_end"
        elif sum(t.get("samples", 0) for t in tracks) == 0:
            res["verdict"] = "empty_index" if media > 0 else "no_media"
        else:
            res["verdict"] = "ok"
    return _finish(res)


def _finish(res):
    res["ok"] = res["verdict"] in OK_VERDICTS
    res["message"] = MESSAGES[res["verdict"]]
    return res


def _safe(text):
    """[text] with control and format characters replaced: a file name or a
    codec field from a damaged file must not be able to drive the terminal
    (escape sequences) or reorder what it shows (bidi overrides)."""
    return "".join("?" if unicodedata.category(c) in ("Cc", "Cf") else c for c in str(text))


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def human_time(secs):
    secs = int(round(secs))
    return "%d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def _box_line(boxes):
    if len(boxes) <= 8:
        parts = []
        for b in boxes:
            note = " (runs to end of file)" if b.get("to_end_of_file") else (
                " (cut short)" if b.get("cut_short") else "")
            parts.append("%s %s%s" % (b["type"], human_size(b["size"]), note))
        return " | ".join(parts)
    counts = {}
    for b in boxes:
        counts[b["type"]] = counts.get(b["type"], 0) + 1
    return " | ".join("%s x%d" % kv for kv in counts.items())


def format_result(res):
    lines = ["%s  (%s)" % (_safe(res["file"]), human_size(res["size"]))]
    if res.get("boxes"):
        lines.append("  boxes: " + _box_line(res["boxes"]))
    if res.get("unreadable_bytes"):
        lines.append("  after the last box: %s the box headers do not account for"
                     % human_size(res["unreadable_bytes"]))
    for t in res.get("tracks", []):
        bits = [_safe(t.get("kind", "?"))]
        if t.get("codec"):
            bits.append(_safe(t["codec"]))
        if t.get("seconds") is not None:
            bits.append(human_time(t["seconds"]))
        bits.append("%d samples" % t.get("samples", 0))
        lines.append("  track: " + ", ".join(bits))
    width = max(60, min(100, shutil.get_terminal_size((100, 20)).columns - 1))
    lines.append(textwrap.fill("[%s] %s" % (LABELS[res["verdict"]], res["message"]), width=width,
                               initial_indent="  ", subsequent_indent="  "))
    return "\n".join(lines)


def _files(paths, recursive, all_files):
    for p in paths:
        if os.path.isdir(p):
            if recursive:
                walker = os.walk(p)
            else:
                walker = [(p, [], sorted(os.listdir(p)))]
            for root, _dirs, names in walker:
                for name in sorted(names):
                    full = os.path.join(root, name)
                    if os.path.isfile(full) and (
                            all_files or os.path.splitext(name)[1].lower() in VIDEO_EXTS):
                        yield full
        else:
            yield p


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mp4-moov-check",
        description="Find out why an MP4 or MOV won't play: a missing moov atom "
                    "(\"moov atom not found\"), a cut-off file or a broken index. "
                    "Read-only.")
    ap.add_argument("paths", nargs="+", metavar="PATH", help="video files or folders")
    ap.add_argument("-r", "--recursive", action="store_true", help="look inside sub-folders too")
    ap.add_argument("--all-files", action="store_true",
                    help="in folders, check every file, not only video extensions")
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    results, errors = [], 0
    for path in _files(a.paths, a.recursive, a.all_files):
        try:
            results.append(check(path))
        except Exception as e:  # one unreadable file must not stop the others
            errors += 1
            results.append({"file": path, "error": "%s: %s" % (type(e).__name__, e)})
    if not results:
        print("No video files found.", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for i, res in enumerate(results):
            if i:
                print()
            print(("%s\n  [ERROR] %s" % (_safe(res["file"]), _safe(res["error"])))
                  if "error" in res else format_result(res))
    if errors:
        return 2
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
