"""Every verdict, on small files built box by box (no sample videos needed)."""
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mp4_moov_check as m  # noqa: E402

MEDIA = bytes(range(1, 256)) * 40  # 10 KB of non-zero "frames"


def box(t, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), t.encode()) + payload


def full(t, payload=b"", version=0):
    return box(t, struct.pack(">I", version << 24) + payload)


def ftyp():
    return box("ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")


def mvhd(scale=1000, dur=3000):
    return full("mvhd", struct.pack(">IIII", 0, 0, scale, dur) + b"\x00" * 80)


def trak(samples, offsets, kind="vide", codec="avc1", wide=False):
    mdhd = full("mdhd", struct.pack(">IIII", 0, 0, 90000, 270000) + b"\x00" * 4)
    hdlr = full("hdlr", struct.pack(">I4s", 0, kind.encode()) + b"\x00" * 13)
    stsd = full("stsd", struct.pack(">I", 1) + box(codec, b"\x00" * 78))
    stsz = full("stsz", struct.pack(">II", 0, samples) + struct.pack(">%dI" % samples, *([100] * samples)))
    if wide:
        co = full("co64", struct.pack(">I", len(offsets)) + struct.pack(">%dQ" % len(offsets), *offsets))
    else:
        co = full("stco", struct.pack(">I", len(offsets)) + struct.pack(">%dI" % len(offsets), *offsets))
    stbl = box("stbl", stsd + stsz + co)
    return box("trak", box("tkhd", b"\x00" * 84) + box("mdia", mdhd + hdlr + box("minf", stbl)))


def moov_for(media_start, samples=10, wide=False):
    offsets = [media_start + i * 100 for i in range(samples)]
    return box("moov", mvhd() + trak(samples, offsets, wide=wide))


def healthy(wide=False):
    head = ftyp() + box("mdat", MEDIA)
    return head + moov_for(len(ftyp()) + 8, wide=wide)


def faststart():
    probe = moov_for(0)  # same size whatever the offsets
    start = len(ftyp()) + len(probe) + 8
    return ftyp() + moov_for(start) + box("mdat", MEDIA)


class Verdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def file(self, data, name="clip.mp4"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def verdict(self, data):
        return m.check(self.file(data))

    def test_healthy_moov_at_end(self):
        r = self.verdict(healthy())
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["moov_at"], "end")
        self.assertEqual(r["seconds"], 3.0)
        self.assertEqual(r["tracks"][0]["kind"], "video")
        self.assertEqual(r["tracks"][0]["codec"], "avc1")
        self.assertEqual(r["tracks"][0]["samples"], 10)
        self.assertTrue(r["ok"])

    def test_healthy_64_bit_chunk_offsets(self):
        self.assertEqual(self.verdict(healthy(wide=True))["verdict"], "ok")

    def test_healthy_fast_start(self):
        r = self.verdict(faststart())
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["moov_at"], "start")

    def test_missing_index_size_zero(self):
        r = self.verdict(ftyp() + struct.pack(">I4s", 0, b"mdat") + MEDIA)
        self.assertEqual(r["verdict"], "missing_index")
        self.assertTrue(r["boxes"][-1]["to_end_of_file"])
        self.assertFalse(r["ok"])
        self.assertIn("moov atom not found", r["message"])

    def test_missing_index_64_bit_size_never_filled(self):
        r = self.verdict(ftyp() + struct.pack(">I4sQ", 1, b"mdat", 0) + MEDIA)
        self.assertEqual(r["verdict"], "missing_index")

    def test_missing_index_placeholder_size(self):
        # mdat says 8 bytes (header only); the frames follow unaccounted for.
        r = self.verdict(ftyp() + box("mdat") + MEDIA)
        self.assertEqual(r["verdict"], "missing_index")
        self.assertEqual(r["unreadable_bytes"], len(MEDIA))

    def test_missing_index_declared_larger_than_file(self):
        r = self.verdict(ftyp() + struct.pack(">I4s", 8 + 10 * len(MEDIA), b"mdat") + MEDIA)
        self.assertEqual(r["verdict"], "missing_index")
        self.assertTrue(r["boxes"][-1]["cut_short"])

    def test_missing_index_after_reserved_free_space(self):
        data = ftyp() + box("free", b"\x00" * 4000) + struct.pack(">I4s", 0, b"mdat") + MEDIA
        self.assertEqual(self.verdict(data)["verdict"], "missing_index")

    def test_empty_index_written_up_front(self):
        data = ftyp() + box("moov", mvhd() + trak(0, [])) + box("mdat", MEDIA)
        self.assertEqual(self.verdict(data)["verdict"], "empty_index")

    def test_cut_short_after_a_fast_start_index(self):
        data = faststart()
        r = self.verdict(data[: len(data) - len(MEDIA) // 2])
        self.assertEqual(r["verdict"], "index_past_end")

    def test_cut_inside_the_index(self):
        data = healthy()
        r = self.verdict(data[:-40])
        self.assertEqual(r["verdict"], "index_damaged")

    def test_fragmented_with_header(self):
        moov = box("moov", mvhd() + box("mvex", full("trex", b"\x00" * 20)))
        frag = box("moof", full("mfhd", struct.pack(">I", 1))) + box("mdat", MEDIA)
        self.assertEqual(self.verdict(ftyp() + moov + frag + frag)["verdict"], "ok_fragmented")

    def test_fragments_without_header(self):
        frag = box("moof", full("mfhd", struct.pack(">I", 1))) + box("mdat", MEDIA)
        self.assertEqual(self.verdict(ftyp() + frag + frag)["verdict"], "missing_init")

    def test_preallocated_zeros_are_not_media(self):
        data = ftyp() + struct.pack(">I4s", 0, b"mdat") + b"\x00" * 200000
        self.assertEqual(self.verdict(data)["verdict"], "no_media")

    def test_all_zeros_and_empty(self):
        self.assertEqual(self.verdict(b"\x00" * 300000)["verdict"], "zeros")
        self.assertEqual(self.verdict(b"")["verdict"], "zeros")

    def test_chunk_table_count_is_not_trusted(self):
        # stco claims four billion entries but holds ten: read what is there.
        data = healthy()
        i = data.index(b"stco")
        data = data[:i + 8] + struct.pack(">I", 0xFFFFFFFF) + data[i + 12:]
        r = self.verdict(data)
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["tracks"][0]["chunks"], 10)

    def test_random_damage_never_crashes(self):
        import random
        rnd = random.Random(1234)
        for base in (healthy(), faststart(), ftyp() + struct.pack(">I4s", 0, b"mdat") + MEDIA):
            for _ in range(150):
                data = bytearray(base)
                for _ in range(rnd.randint(1, 12)):
                    data[rnd.randrange(len(data))] = rnd.randrange(256)
                if rnd.random() < 0.3:
                    data = data[:rnd.randrange(len(data))]
                self.assertIn(self.verdict(bytes(data))["verdict"], m.MESSAGES)

    def test_not_mp4(self):
        self.assertEqual(self.verdict(b"Hello, this is a text file, not a video. " * 50)["verdict"],
                         "not_mp4")
        self.assertEqual(self.verdict(b"\x47\x40\x11\x10" + b"\xff" * 2000)["verdict"], "not_mp4")


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.good = os.path.join(self.tmp.name, "good.mp4")
        self.bad = os.path.join(self.tmp.name, "sub", "bad.MOV")
        os.makedirs(os.path.dirname(self.bad))
        with open(self.good, "wb") as f:
            f.write(healthy())
        with open(self.bad, "wb") as f:
            f.write(ftyp() + struct.pack(">I4s", 0, b"mdat") + MEDIA)
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as f:
            f.write("not a video")

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = m.main(list(argv))
        return code, out.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main(self.good)[0], 0)
        code, text = self.run_main(self.bad)
        self.assertEqual(code, 1)
        self.assertIn("[MISSING INDEX]", text)

    def test_folder_scan_skips_other_files_unless_recursive(self):
        code, text = self.run_main("--json", self.tmp.name)
        names = [os.path.basename(r["file"]) for r in json.loads(text)]
        self.assertEqual(names, ["good.mp4"])
        self.assertEqual(code, 0)
        code, text = self.run_main("--json", "-r", self.tmp.name)
        names = sorted(os.path.basename(r["file"]) for r in json.loads(text))
        self.assertEqual(names, ["bad.MOV", "good.mp4"])
        self.assertEqual(code, 1)

    def test_control_characters_never_reach_the_terminal(self):
        res = m.check(self.good)
        res["file"] = "evil\x1b]0;title\x07‮.mp4"
        res["tracks"][0]["codec"] = "a\x1bc1"
        text = m.format_result(res)
        for bad in ("\x1b", "\x07", "‮"):
            self.assertNotIn(bad, text)

    def test_missing_file_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, text = self.run_main(os.path.join(self.tmp.name, "nope.mp4"))
        self.assertEqual(code, 2)
        self.assertIn("[ERROR]", text)


if __name__ == "__main__":
    unittest.main()
