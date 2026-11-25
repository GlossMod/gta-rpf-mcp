"""RPF7 table-of-contents and binary entry cryptography.

Algorithms follow the MIT-licensed Cryma/gta-toolkit implementation. The
packaged data comes from the publicly tracked GlossMod/gmm-plug-in RPF module.
See THIRD-PARTY-NOTICES.txt for their licenses and revisions. The NG decode
resource concatenates gtav_hash_lut.dat, gtav_ng_key.dat and
gtav_ng_decrypt_tables.dat in that order. The encode resource packs word
tables for rounds 0, 1 and 16, followed by the byte lookup planes for rounds
2 through 15, from gtav_ng_encrypt_tables.dat and gtav_ng_encrypt_luts.dat.
"""

import ctypes
import lzma
import sys
from functools import cache
from importlib.resources import files
from struct import Struct

NONE = 0
OPEN = 0x4E45504F
AES = 0x0FFFFFF9
NG = 0x0FEFFFFF

_BLOCK_SIZE = 16
_KEY_COUNT = 101
_KEY_SIZE = 17 * _BLOCK_SIZE
_UINT32_MASK = 0xFFFFFFFF
_WORDS = Struct('<4I')
_ROUND = Struct('<4096I')
_LOOKUP_ROWS = 65536
_LOOKUP_SIZE = 3 * _LOOKUP_ROWS
_LAST_ROUND = 16
_DIRECT_ROUNDS = (0, 1, _LAST_ROUND)
_DIRECT_COLUMNS = ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10, 11), (12, 13, 14, 15))
_SHUFFLED_COLUMNS = ((0, 7, 10, 13), (1, 4, 11, 14), (2, 5, 8, 15), (3, 6, 9, 12))
_ENCRYPT_WORD = (0, 1, 2, 3, 1, 2, 3, 0, 2, 3, 0, 1, 3, 0, 1, 2)


class RpfCryptoError(ValueError):
    """An RPF cipher mode or its packaged data could not be used."""


def _read_data(name: str, expected_size: int) -> bytes:
    try:
        data = files(__package__).joinpath('data', name).read_bytes()
        if name.endswith('.xz'):
            data = lzma.decompress(data)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise RpfCryptoError(f'Cannot load bundled RPF7 format data {name}.') from exc
    if len(data) != expected_size:
        raise RpfCryptoError(f'Bundled RPF7 format data {name} has an invalid length.')
    return data


@cache
def _ng_decryption() -> tuple[bytes, bytes, tuple]:
    keys_end = 256 + _KEY_COUNT * _KEY_SIZE
    raw = _read_data('rpf7-ng-decrypt.xz', keys_end + 17 * _ROUND.size)
    return raw[:256], raw[256:keys_end], tuple(_ROUND.iter_unpack(raw[keys_end:]))


@cache
def _ng_encryption() -> tuple[dict, tuple]:
    tables_end = len(_DIRECT_ROUNDS) * _ROUND.size
    lookup_round_size = 16 * _LOOKUP_SIZE
    raw = _read_data('rpf7-ng-encrypt.xz', tables_end + 14 * lookup_round_size)
    tables = dict(zip(_DIRECT_ROUNDS, _ROUND.iter_unpack(raw[:tables_end]), strict=True))
    lookups = memoryview(raw)[tables_end:]
    return tables, tuple(
        lookups[offset:offset + lookup_round_size]
        for offset in range(0, len(lookups), lookup_round_size)
    )


def _ng_keys(name: str, size: int) -> tuple:
    """Select the RPF7 key schedule from the original basename and uint32 size."""
    if not name or '/' in name or '\\' in name or '\x00' in name:
        raise RpfCryptoError('NG requires the original RPF or entry basename.')
    if size < 0:
        raise RpfCryptoError('NG requires a nonnegative archive or entry size.')
    lut, key_data, _ = _ng_decryption()
    try:
        name_bytes = name.encode('latin-1')
    except UnicodeEncodeError as exc:
        raise RpfCryptoError('The NG basename is outside the RPF7 byte character table.') from exc
    value = 0
    for byte in name_bytes:
        mixed = 1025 * (lut[byte] + value) & _UINT32_MASK
        value = mixed ^ (mixed >> 6)
    mixed = 9 * value & _UINT32_MASK
    digest = 32769 * (mixed ^ (mixed >> 11)) & _UINT32_MASK
    index = ((digest + size + 61) & _UINT32_MASK) % _KEY_COUNT
    key = key_data[index * _KEY_SIZE:(index + 1) * _KEY_SIZE]
    return tuple(_WORDS.iter_unpack(key))


def _ng_round(table: tuple, block: bytes, columns: tuple, words: tuple) -> bytes:
    result = []
    for key, (a, b, c, d) in zip(words, columns, strict=True):
        result.append(
            key ^ table[256 * a + block[a]] ^ table[256 * b + block[b]]
            ^ table[256 * c + block[c]] ^ table[256 * d + block[d]]
        )
    return _WORDS.pack(*result)


def _ng_decrypt(data: bytes, keys: tuple) -> bytes:
    _, _, tables = _ng_decryption()
    rounds = tuple(
        (table, _DIRECT_COLUMNS if round_index in _DIRECT_ROUNDS else _SHUFFLED_COLUMNS, keys[round_index])
        for round_index, table in enumerate(tables)
    )
    output = bytearray(data)
    for offset in range(0, len(data) - len(data) % _BLOCK_SIZE, _BLOCK_SIZE):
        block = data[offset:offset + _BLOCK_SIZE]
        for table, columns, words in rounds:
            block = _ng_round(table, block, columns, words)
        output[offset:offset + _BLOCK_SIZE] = block
    return bytes(output)


def _ng_encrypt(data: bytes, keys: tuple) -> bytes:
    tables, lookups = _ng_encryption()
    output = bytearray(data)
    for offset in range(0, len(data) - len(data) % _BLOCK_SIZE, _BLOCK_SIZE):
        block = data[offset:offset + _BLOCK_SIZE]
        for round_index in range(_LAST_ROUND, -1, -1):
            plain_words = _WORDS.unpack(block)
            mixed = tuple(word ^ key for word, key in zip(plain_words, keys[round_index], strict=True))
            if round_index in _DIRECT_ROUNDS:
                block = _ng_round(tables[round_index], _WORDS.pack(*mixed), _DIRECT_COLUMNS, (0, 0, 0, 0))
            else:
                lookup = lookups[round_index - 2]
                block = bytes(_ng_inverse_byte(lookup, i, mixed[word]) for i, word in enumerate(_ENCRYPT_WORD))
        output[offset:offset + _BLOCK_SIZE] = block
    return bytes(output)


def _ng_inverse_byte(table: memoryview, index: int, word: int) -> int:
    # Three flat 256-by-256 planes per byte, in upstream LUT0/LUT1/Indices order.
    start = index * _LOOKUP_SIZE
    high = table[start + 2 * _LOOKUP_ROWS + (word >> 16)]
    middle = table[start + 256 * high + _LOOKUP_ROWS + ((word >> 8) & 255)]
    return table[start + 256 * middle + (word & 255)]


def _toc(mode: int, name: str, size: int, entries: bytes, names: bytes, *, encrypt: bool) -> tuple[bytes, bytes]:
    if mode in (NONE, OPEN):
        return entries, names
    if mode == NG:
        keys = _ng_keys(name, size)
        transform = _ng_encrypt if encrypt else _ng_decrypt
        return transform(entries, keys), transform(names, keys)
    if mode == AES:
        return _aes_transform(entries, encrypt=encrypt), _aes_transform(names, encrypt=encrypt)
    raise RpfCryptoError(f'Unknown RPF7 TOC encryption marker 0x{mode:08X}.')


def decrypt_toc(mode: int, archive_name: str, archive_size: int, entries: bytes, names: bytes) -> tuple[bytes, bytes]:
    """Decode the existing TOC, using the archive's original byte size."""
    return _toc(mode, archive_name, archive_size, entries, names, encrypt=False)


def encrypt_toc(mode: int, archive_name: str, archive_size: int, entries: bytes, names: bytes) -> tuple[bytes, bytes]:
    """Encode a TOC in the selected mode, using the archive's final byte size."""
    return _toc(mode, archive_name, archive_size, entries, names, encrypt=True)


def decrypt_entry(mode: int, entry_name: str, uncompressed_size: int, data: bytes) -> bytes:
    """Decode a binary entry flagged encrypted, before optional inflation."""
    if mode == AES:
        return _aes_transform(data, encrypt=False)
    if mode in (NONE, OPEN, NG):
        # The TOC can be clear while a binary entry retains its original NG bytes.
        return _ng_decrypt(data, _ng_keys(entry_name, uncompressed_size))
    raise RpfCryptoError(f'Unknown RPF7 entry encryption marker 0x{mode:08X}.')


@cache
def _cng():
    if sys.platform != 'win32':
        raise RpfCryptoError('RPF7 AES archives require Windows CNG.')
    try:
        library = ctypes.WinDLL('bcrypt.dll', winmode=0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
    except OSError as exc:
        raise RpfCryptoError('Cannot load Windows CNG for RPF7 AES.') from exc
    handle = ctypes.c_void_p
    handle_ptr = ctypes.POINTER(handle)
    u32 = ctypes.c_uint32
    buffer = ctypes.c_void_p
    wide = ctypes.c_wchar_p
    transform = (handle, buffer, u32, buffer, buffer, u32, buffer, u32, ctypes.POINTER(u32), u32)
    bindings = {
        'BCryptOpenAlgorithmProvider': (handle_ptr, wide, wide, u32),
        'BCryptSetProperty': (handle, wide, buffer, u32, u32),
        'BCryptGenerateSymmetricKey': (handle, handle_ptr, buffer, u32, buffer, u32, u32),
        'BCryptEncrypt': transform,
        'BCryptDecrypt': transform,
        'BCryptDestroyKey': (handle,),
        'BCryptCloseAlgorithmProvider': (handle, u32),
    }
    for name, types in bindings.items():
        function = getattr(library, name)
        function.argtypes = types
        function.restype = ctypes.c_int32
    return library


def _check_cng(status: int) -> None:
    if status < 0:
        raise RpfCryptoError(f'Windows CNG rejected RPF7 AES (0x{status & _UINT32_MASK:08X}).')


@cache
def _aes_key() -> bytes:
    return _read_data('rpf7-aes.dat', 32)


def _aes_transform(data: bytes, *, encrypt: bool) -> bytes:
    aligned = len(data) - len(data) % _BLOCK_SIZE
    if not aligned:
        return data
    library = _cng()
    algorithm = ctypes.c_void_p()
    key = ctypes.c_void_p()
    _check_cng(library.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'AES', None, 0))
    try:
        mode = ctypes.create_unicode_buffer('ChainingModeECB')
        _check_cng(library.BCryptSetProperty(algorithm, 'ChainingMode', mode, ctypes.sizeof(mode), 0))
        secret = _aes_key()
        _check_cng(library.BCryptGenerateSymmetricKey(
            algorithm, ctypes.byref(key), None, 0, secret, len(secret), 0
        ))
        try:
            transform = library.BCryptEncrypt if encrypt else library.BCryptDecrypt
            output = ctypes.create_string_buffer(aligned)
            written = ctypes.c_uint32()
            _check_cng(transform(key, data, aligned, None, None, 0, output, aligned, ctypes.byref(written), 0))
            if written.value != aligned:
                raise RpfCryptoError('Windows CNG returned an unexpected RPF7 AES byte count.')
            return output.raw + data[aligned:]
        finally:
            library.BCryptDestroyKey(key)
    finally:
        library.BCryptCloseAlgorithmProvider(algorithm, 0)
