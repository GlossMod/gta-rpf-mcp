import sys
import unittest

from gta_rpf_mcp import rpf_crypto as crypto


ENTRIES = bytes(range(32))
NAMES = b"\0entry-name.xml\0tail"
NG_ENTRIES = bytes.fromhex("efedc60ba932b7cf14ad5efcfc51fafb7ef62c9d563324343c7c72fccec7c5ec")
NG_NAMES = bytes.fromhex("a9612fb9ac310c9dcfc85d1afda4ce6e7461696c")
AES_ENTRIES = bytes.fromhex("e1921016e8c7eda99c418a5054dd3afe6ef5b16414cc678279e3e07b0ec7adc4")
AES_NAMES = bytes.fromhex("254659b1d4c4216c80d8ae17a3c9ed817461696c")


class RpfCryptoTests(unittest.TestCase):
    def test_ng_toc_matches_upstream_for_two_separate_tables(self):
        # These synthetic vectors were checked against Cryma/gta-toolkit's GTA5Crypto.
        decoded = crypto.decrypt_toc(crypto.NG, "entry-name.xml", 8452, NG_ENTRIES, NG_NAMES)
        self.assertEqual(decoded, (ENTRIES, NAMES))
        encoded = crypto.encrypt_toc(crypto.NG, "entry-name.xml", 8452, ENTRIES, NAMES)
        self.assertEqual(encoded, (NG_ENTRIES, NG_NAMES))
        self.assertEqual(encoded[1][-4:], b"tail")

    def test_ng_sector_growth_changes_key_and_reopens_with_final_size(self):
        new_size = 8452 + 512
        encoded = crypto.encrypt_toc(crypto.NG, "entry-name.xml", new_size, ENTRIES, NAMES)
        self.assertNotEqual(encoded, (NG_ENTRIES, NG_NAMES))
        self.assertNotEqual(crypto.decrypt_toc(crypto.NG, "entry-name.xml", 8452, *encoded), (ENTRIES, NAMES))
        self.assertEqual(crypto.decrypt_toc(crypto.NG, "entry-name.xml", new_size, *encoded), (ENTRIES, NAMES))

    def test_ng_key_selection_wraps_u32_and_uses_filename_lookup(self):
        size = (1 << 32) + 8452
        self.assertEqual(
            crypto.decrypt_toc(crypto.NG, "ENTRY-NAME.XML", size, NG_ENTRIES, NG_NAMES),
            (ENTRIES, NAMES),
        )

    def test_flagged_binary_entry_uses_its_name_and_uncompressed_size(self):
        for mode in (crypto.NG, crypto.OPEN, crypto.NONE):
            with self.subTest(mode=mode):
                self.assertEqual(crypto.decrypt_entry(mode, "entry-name.xml", 8452, NG_NAMES), NAMES)

    @unittest.skipUnless(sys.platform == "win32", "AES uses Windows CNG")
    def test_aes_ecb_matches_dotnet_for_full_blocks_and_leaves_tail(self):
        self.assertEqual(crypto.encrypt_toc(crypto.AES, "unused", 0, ENTRIES, NAMES), (AES_ENTRIES, AES_NAMES))
        self.assertEqual(crypto.decrypt_toc(crypto.AES, "unused", 0, AES_ENTRIES, AES_NAMES), (ENTRIES, NAMES))
        self.assertEqual(crypto.decrypt_entry(crypto.AES, "unused", 0, AES_NAMES), NAMES)

    def test_clear_toc_modes_preserve_bytes(self):
        for mode in (crypto.NONE, crypto.OPEN):
            with self.subTest(mode=mode):
                self.assertEqual(crypto.decrypt_toc(mode, "unused", 0, ENTRIES, NAMES), (ENTRIES, NAMES))
                self.assertEqual(crypto.encrypt_toc(mode, "unused", 0, ENTRIES, NAMES), (ENTRIES, NAMES))

    def test_unknown_mode_and_invalid_ng_basename_are_rejected(self):
        with self.assertRaisesRegex(crypto.RpfCryptoError, "0x12345678"):
            crypto.decrypt_toc(0x12345678, "entry-name.xml", 8452, ENTRIES, NAMES)
        with self.assertRaisesRegex(crypto.RpfCryptoError, "basename"):
            crypto.encrypt_toc(crypto.NG, "nested/entry-name.xml", 8452, ENTRIES, NAMES)
