"""Charset label check, then a UTF-8 wrapper. The CSV scan never sees raw bytes.

Strict policy is UTF-8 decode without BOM or fail. Legacy policy is UTF-8 decode:
strip one leading EF BB BF, then replacement mode. An illegal sequence in
replacement mode emits U+FFFD and restores a following byte so an ASCII comma or
quote is still that character.
"""

from __future__ import annotations

from .model import DecodeFailure, Decoded

_ASCII_WS = "\t\n\f\r "
_UTF8 = "UTF-8"

# Encoding Standard label table. Names are the canonical encoding names.
_LABEL_GROUPS: dict[str, tuple[str, ...]] = {
    "UTF-8": (
        "unicode-1-1-utf-8",
        "utf-8",
        "utf8",
        "unicode11utf8",
        "unicode20utf8",
        "x-unicode20utf8",
    ),
    "IBM866": ("866", "cp866", "csibm866", "ibm866"),
    "ISO-8859-2": (
        "csisolatin2",
        "iso-8859-2",
        "iso-ir-101",
        "iso8859-2",
        "iso88592",
        "iso_8859-2",
        "iso_8859-2:1987",
        "l2",
        "latin2",
    ),
    "ISO-8859-3": (
        "csisolatin3",
        "iso-8859-3",
        "iso-ir-109",
        "iso8859-3",
        "iso88593",
        "iso_8859-3",
        "iso_8859-3:1988",
        "l3",
        "latin3",
    ),
    "ISO-8859-4": (
        "csisolatin4",
        "iso-8859-4",
        "iso-ir-110",
        "iso8859-4",
        "iso88594",
        "iso_8859-4",
        "iso_8859-4:1988",
        "l4",
        "latin4",
    ),
    "ISO-8859-5": (
        "csisolatincyrillic",
        "cyrillic",
        "iso-8859-5",
        "iso-ir-144",
        "iso8859-5",
        "iso88595",
        "iso_8859-5",
        "iso_8859-5:1988",
    ),
    "ISO-8859-6": (
        "arabic",
        "asmo-708",
        "csiso88596e",
        "csiso88596i",
        "csisolatinarabic",
        "ecma-114",
        "iso-8859-6",
        "iso-8859-6-e",
        "iso-8859-6-i",
        "iso-ir-127",
        "iso8859-6",
        "iso88596",
        "iso_8859-6",
        "iso_8859-6:1987",
    ),
    "ISO-8859-7": (
        "csisolatingreek",
        "ecma-118",
        "elot_928",
        "greek",
        "greek8",
        "iso-8859-7",
        "iso-ir-126",
        "iso8859-7",
        "iso88597",
        "iso_8859-7",
        "iso_8859-7:1987",
        "sun_eu_greek",
    ),
    "ISO-8859-8": (
        "csiso88598e",
        "csisolatinhebrew",
        "hebrew",
        "iso-8859-8",
        "iso-8859-8-e",
        "iso-ir-138",
        "iso8859-8",
        "iso88598",
        "iso_8859-8",
        "iso_8859-8:1988",
        "visual",
    ),
    "ISO-8859-8-I": ("csiso88598i", "iso-8859-8-i", "logical"),
    "ISO-8859-10": (
        "csisolatin6",
        "iso-8859-10",
        "iso-ir-157",
        "iso8859-10",
        "iso885910",
        "l6",
        "latin6",
    ),
    "ISO-8859-13": ("iso-8859-13", "iso8859-13", "iso885913"),
    "ISO-8859-14": ("iso-8859-14", "iso8859-14", "iso885914"),
    "ISO-8859-15": (
        "csisolatin9",
        "iso-8859-15",
        "iso8859-15",
        "iso885915",
        "iso_8859-15",
        "l9",
    ),
    "ISO-8859-16": ("iso-8859-16",),
    "KOI8-R": ("cskoi8r", "koi", "koi8", "koi8-r", "koi8_r"),
    "KOI8-U": ("koi8-ru", "koi8-u"),
    "macintosh": ("csmacintosh", "mac", "macintosh", "x-mac-roman"),
    "windows-874": (
        "dos-874",
        "iso-8859-11",
        "iso8859-11",
        "iso885911",
        "tis-620",
        "windows-874",
    ),
    "windows-1250": ("cp1250", "windows-1250", "x-cp1250"),
    "windows-1251": ("cp1251", "windows-1251", "x-cp1251"),
    "windows-1252": (
        "ansi_x3.4-1968",
        "ascii",
        "cp1252",
        "cp819",
        "csisolatin1",
        "ibm819",
        "iso-8859-1",
        "iso-ir-100",
        "iso8859-1",
        "iso88591",
        "iso_8859-1",
        "iso_8859-1:1987",
        "l1",
        "latin1",
        "us-ascii",
        "windows-1252",
        "x-cp1252",
    ),
    "windows-1253": ("cp1253", "windows-1253", "x-cp1253"),
    "windows-1254": (
        "cp1254",
        "csisolatin5",
        "iso-8859-9",
        "iso-ir-148",
        "iso8859-9",
        "iso88599",
        "iso_8859-9",
        "iso_8859-9:1989",
        "l5",
        "latin5",
        "windows-1254",
        "x-cp1254",
    ),
    "windows-1255": ("cp1255", "windows-1255", "x-cp1255"),
    "windows-1256": ("cp1256", "windows-1256", "x-cp1256"),
    "windows-1257": ("cp1257", "windows-1257", "x-cp1257"),
    "windows-1258": ("cp1258", "windows-1258", "x-cp1258"),
    "x-mac-cyrillic": ("x-mac-cyrillic", "x-mac-ukrainian"),
    "GBK": (
        "chinese",
        "csgb2312",
        "csiso58gb231280",
        "gb2312",
        "gb_2312",
        "gb_2312-80",
        "gbk",
        "iso-ir-58",
        "x-gbk",
    ),
    "gb18030": ("gb18030",),
    "Big5": ("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"),
    "EUC-JP": ("cseucpkdfmtjapanese", "euc-jp", "x-euc-jp"),
    "ISO-2022-JP": ("csiso2022jp", "iso-2022-jp"),
    "Shift_JIS": (
        "csshiftjis",
        "ms932",
        "ms_kanji",
        "shift-jis",
        "shift_jis",
        "sjis",
        "windows-31j",
        "x-sjis",
    ),
    "EUC-KR": (
        "cseuckr",
        "csksc56011987",
        "euc-kr",
        "iso-ir-149",
        "korean",
        "ks_c_5601-1987",
        "ks_c_5601-1989",
        "ksc5601",
        "ksc_5601",
        "windows-949",
    ),
    "replacement": (
        "csiso2022kr",
        "hz-gb-2312",
        "iso-2022-cn",
        "iso-2022-cn-ext",
        "iso-2022-kr",
        "replacement",
    ),
    "UTF-16BE": ("utf-16be",),
    "UTF-16LE": ("utf-16", "utf-16le"),
    "x-user-defined": ("x-user-defined",),
}

LABELS: dict[str, str] = {
    label: name for name, labels in _LABEL_GROUPS.items() for label in labels
}


def strip_ascii_whitespace(label: str) -> str:
    start = 0
    end = len(label)
    while start < end and label[start] in _ASCII_WS:
        start += 1
    while end > start and label[end - 1] in _ASCII_WS:
        end -= 1
    return label[start:end]


def lookup_label(label: str) -> str | None:
    """Return the canonical encoding name, or None if the label is not in the set."""

    key = strip_ascii_whitespace(label)
    if not key.isascii():
        # str.lower is Unicode-aware (KELVIN SIGN lowers to "k"). The standard
        # matches labels ASCII case-insensitively, so non-ASCII never matches.
        return None
    return LABELS.get(key.lower())


def decode_bytes(data: bytes, *, policy: str, charset: str, mutant: str | None = None) -> Decoded | DecodeFailure:
    """policy is 'fatal' or 'replacement'. mutant 'drop_ascii_restore' eats a restored ASCII byte."""

    if policy not in {"fatal", "replacement"}:
        raise ValueError("policy must be 'fatal' or 'replacement'")
    name = lookup_label(charset)
    if name is None:
        return DecodeFailure("E-charset", "D-charset", 0)
    if name != _UTF8:
        return DecodeFailure("E-not-utf8", "D-charset", 0)

    bom_stripped = False
    base = 0
    payload = data
    if policy == "replacement" and data.startswith(b"\xef\xbb\xbf"):
        bom_stripped = True
        base = 3
        payload = data[3:]

    fatal = policy == "fatal"
    decoded = _utf8_decode(payload, base, fatal=fatal, mutant=mutant)
    if isinstance(decoded, DecodeFailure):
        return decoded
    text, offsets, replacements = decoded
    return Decoded(
        text=text,
        offsets=tuple(offsets),
        end_offset=len(data),
        bom_stripped=bom_stripped,
        replacement_offsets=tuple(replacements),
    )


def _utf8_decode(
    data: bytes,
    base: int,
    *,
    fatal: bool,
    mutant: str | None,
) -> tuple[str, list[int], list[int]] | DecodeFailure:
    """WHATWG UTF-8 decoder. A bad continuation restores the byte to the queue."""

    chars: list[str] = []
    offsets: list[int] = []
    replacements: list[int] = []
    i = 0
    n = len(data)
    bytes_needed = 0
    bytes_seen = 0
    code_point = 0
    lower = 0x80
    upper = 0xBF
    seq_start = 0

    def fail(at: int) -> DecodeFailure:
        return DecodeFailure("E-decode", "D-decode", at)

    def emit_replacement(at: int) -> None:
        chars.append("\ufffd")
        offsets.append(at)
        replacements.append(at)

    while True:
        if i >= n:
            if bytes_needed != 0:
                at = base + seq_start
                if fatal:
                    return fail(at)
                emit_replacement(at)
            break

        byte = data[i]
        if bytes_needed == 0:
            if byte <= 0x7F:
                chars.append(chr(byte))
                offsets.append(base + i)
                i += 1
                continue
            if 0xC2 <= byte <= 0xDF:
                bytes_needed = 1
                bytes_seen = 0
                code_point = byte & 0x1F
                lower = 0x80
                upper = 0xBF
                seq_start = i
                i += 1
                continue
            if 0xE0 <= byte <= 0xEF:
                lower = 0xA0 if byte == 0xE0 else 0x80
                upper = 0x9F if byte == 0xED else 0xBF
                bytes_needed = 2
                bytes_seen = 0
                code_point = byte & 0x0F
                seq_start = i
                i += 1
                continue
            if 0xF0 <= byte <= 0xF4:
                lower = 0x90 if byte == 0xF0 else 0x80
                upper = 0x8F if byte == 0xF4 else 0xBF
                bytes_needed = 3
                bytes_seen = 0
                code_point = byte & 0x07
                seq_start = i
                i += 1
                continue
            at = base + i
            if fatal:
                return fail(at)
            emit_replacement(at)
            i += 1
            continue

        if not (lower <= byte <= upper):
            at = base + seq_start
            bytes_needed = 0
            bytes_seen = 0
            code_point = 0
            lower = 0x80
            upper = 0xBF
            if fatal:
                return fail(at)
            emit_replacement(at)
            if mutant == "drop_ascii_restore" and byte <= 0x7F:
                # The following ASCII byte is consumed with the error instead of restored.
                i += 1
            continue

        lower = 0x80
        upper = 0xBF
        code_point = (code_point << 6) | (byte & 0x3F)
        bytes_seen += 1
        i += 1
        if bytes_seen != bytes_needed:
            continue
        chars.append(chr(code_point))
        offsets.append(base + seq_start)
        code_point = 0
        bytes_needed = 0
        bytes_seen = 0

    return "".join(chars), offsets, replacements
