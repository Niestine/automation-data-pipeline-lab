import helpers  # noqa: F401
import unittest

from incremental_crawl_lab.codec import compress_lzw, decompress_lzw, unwrap_encoding
from incremental_crawl_lab.errors import UnsupportedEncoding
from incremental_crawl_lab.fingerprint import sha256_hex
from incremental_crawl_lab.fixture import encode_wire


class CodecTest(unittest.TestCase):
    def test_compress_round_trip(self) -> None:
        payloads = [b"", b"a", b"hello", b"hello" * 23, bytes(range(256)) * 3]
        for payload in payloads:
            self.assertEqual(decompress_lzw(compress_lzw(payload)), payload)

    def test_encodings_hash_to_the_identity_bytes(self) -> None:
        payload = b"<html><body><p>harborline wool coat</p></body></html>"
        identity = sha256_hex(payload)
        for encoding in ("gzip", "deflate", "compress"):
            wire, coding = encode_wire(payload, encoding)
            self.assertEqual(sha256_hex(unwrap_encoding(wire, coding)), identity)
            self.assertNotEqual(wire, payload)

    def test_unknown_coding_and_block_mode_are_refused(self) -> None:
        with self.assertRaises(UnsupportedEncoding):
            unwrap_encoding(b"abc", "br")
        with self.assertRaises(UnsupportedEncoding):
            decompress_lzw(bytes([0x1F, 0x9D, 0x80 | 12, 0x00]))


if __name__ == "__main__":
    unittest.main()
