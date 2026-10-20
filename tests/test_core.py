# tests/test_core.py
# Deterministic, wall-clock-free tests. Every file operation uses a real
# temporary file via tempfile.TemporaryDirectory, which is hermetic and needs
# no network. We cover: initial read, continuation, rotation (inode change),
# truncation (same inode, smaller size), missing file, stale max_bytes, and
# idempotency when there is nothing new.

import os
import shutil
import tempfile
import unittest

from file_growth_tailer import Tailer, ReadResult, ReopenError


class TestTailer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.path = os.path.join(self.dir, "growing.log")

    def tearDown(self):
        self._td.cleanup()

    def _write(self, data: bytes, mode="ab"):
        with open(self.path, mode) as f:
            f.write(data)

    def test_initial_read_returns_all_content(self):
        self._write(b"hello", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        r = t.read()
        self.assertEqual(r.data, b"hello")
        self.assertEqual(r.position, 5)
        self.assertIsNotNone(r.inode)

    def test_second_read_returns_only_new_content(self):
        self._write(b"abc", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        t.read()  # consumes b"abc"
        self._write(b"def")
        r = t.read()
        self.assertEqual(r.data, b"def")
        self.assertEqual(r.position, 6)

    def test_no_new_content_returns_empty(self):
        self._write(b"abc", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        t.read()
        r = t.read()
        self.assertEqual(r.data, b"")
        self.assertEqual(r.position, 3)

    def test_rotation_resets_position_to_zero(self):
        self._write(b"old-content", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        r1 = t.read()
        self.assertEqual(r1.data, b"old-content")
        old_inode = r1.inode

        # Rotate: move the old file aside and create a brand-new file.
        shutil.move(self.path, self.path + ".1")
        self._write(b"new", mode="wb")

        r2 = t.read()
        self.assertEqual(r2.data, b"new")
        self.assertEqual(r2.position, 3)
        self.assertNotEqual(r2.inode, old_inode)

    def test_truncation_same_inode_clamps_position(self):
        self._write(b"abcdefghij", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        t.read()  # position now 10

        # Truncate in place: same inode, smaller size.
        with open(self.path, "wb") as f:
            f.write(b"xy")  # file is now 2 bytes

        r = t.read()
        # We clamped to size (2) and read what's there from offset 2 -> empty.
        self.assertEqual(r.data, b"")
        self.assertEqual(r.position, 2)

    def test_truncation_then_growth_reads_new_tail(self):
        self._write(b"abcdefghij", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        t.read()
        with open(self.path, "wb") as f:
            f.write(b"xy")
        t.read()  # clamp to 2, read empty
        self._write(b"ZW")  # now 4 bytes total
        r = t.read()
        self.assertEqual(r.data, b"ZW")
        self.assertEqual(r.position, 4)

    def test_missing_file_raises_reopen_error(self):
        self._write(b"data", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        t.read()
        os.remove(self.path)
        with self.assertRaises(ReopenError):
            t.read()

    def test_resume_from_persisted_position(self):
        self._write(b"abcdef", mode="wb")
        # Simulate a restart: caller persisted position=3 and inode.
        t1 = Tailer(self.path, chunk_size=4096)
        r = t1.read()
        self.assertEqual(r.data, b"abcdef")
        inode = r.inode
        persisted_position = r.position

        t2 = Tailer(self.path, chunk_size=4096, position=persisted_position, inode=inode)
        self._write(b"ghi")
        r2 = t2.read()
        self.assertEqual(r2.data, b"ghi")
        self.assertEqual(r2.position, 9)

    def test_chunk_size_limits_single_read(self):
        self._write(b"0123456789", mode="wb")
        t = Tailer(self.path, chunk_size=4)
        r = t.read()
        self.assertEqual(r.data, b"0123")
        self.assertEqual(r.position, 4)
        r = t.read()
        self.assertEqual(r.data, b"4567")
        self.assertEqual(r.position, 8)

    def test_max_bytes_overrides_chunk_size(self):
        self._write(b"0123456789", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        r = t.read(max_bytes=3)
        self.assertEqual(r.data, b"012")
        self.assertEqual(r.position, 3)

    def test_invalid_chunk_size_raises(self):
        with self.assertRaises(ValueError):
            Tailer(self.path, chunk_size=0)
        with self.assertRaises(ValueError):
            Tailer(self.path, chunk_size=-1)

    def test_invalid_max_bytes_raises(self):
        self._write(b"abc", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        with self.assertRaises(ValueError):
            t.read(max_bytes=0)
        with self.assertRaises(ValueError):
            t.read(max_bytes=-5)

    def test_negative_initial_position_raises(self):
        with self.assertRaises(ValueError):
            Tailer(self.path, position=-1)

    def test_inode_property_reflects_last_read(self):
        self._write(b"abc", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        self.assertIsNone(t.inode)
        r = t.read()
        self.assertEqual(t.inode, r.inode)

    def test_position_property_reflects_last_read(self):
        self._write(b"abc", mode="wb")
        t = Tailer(self.path, chunk_size=4096)
        self.assertEqual(t.position, 0)
        t.read()
        self.assertEqual(t.position, 3)


if __name__ == "__main__":
    unittest.main()
