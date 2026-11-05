"""Mandarin Chinese cardinal number normalization for benchmark text.

Written Chinese does not put spaces between words, so a token that consists
entirely of number characters is read as one number run; any token that mixes
number characters with prose keeps the whole token unchanged
("我有一百二十三块钱" stays as it is), matching the context-free limits
documented for the other language modes.

Standard standalone simplified-character cardinals are supported from 零
through the 万亿 (10**12) scale in the standard short-scale 万 (10**4) /
亿 (10**8) system, including compositions such as
一亿二千三百四十五万六千七百八十九. A token made only of unit characters is
read digit by digit, like the English and Turkish unit runs ("一二三" ->
"123", "二〇二六" -> "2026"). 两 is accepted as 2 before 百/千/万/亿 and as a
standalone counting variant; 零 is a positional zero marker and 〇 is the
digit zero. Malformed or ambiguous sequences such as 十十, 万万, 一亿万,
一百零十, or 一百二三 are preserved intact. A multi-character token starting
with 零 (零一, 零零, 零一百) is malformed and kept whole; 〇, not 零, is the
leading digit zero. Colloquial truncations are read formally ("二百五" -> "205", "三百二" -> "302", "二万三" -> "20003").

Ordinals (第一...), dates, fractions, currency forms, spoken decimals,
Traditional characters (萬/億), the ambiguous 兆, and negative forms are
outside this module's scope. The parser is context-free: cardinal characters
inside a matched token are read as a number even in ordinary prose.

References checked: Unicode CLDR Mandarin Chinese number system data
(https://github.com/unicode-org/cldr/blob/main/common/rbnf/zh.xml), CLDR
number-formatting specification
(https://unicode.org/reports/tr35/tr35-numbers.html), and the Chinese numeral
rules described at https://zh.wikipedia.org/wiki/中文数字.
"""
import re

_UNITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_ZERO = "零"
_MULTIPLIERS = {"十": 10, "百": 100, "千": 1000}
_SCALES = {"万": 10 ** 4, "亿": 10 ** 8}
_NUMBER_TOKEN = re.compile("[零〇一二两三四五六七八九十百千万亿]+")


def numbers_to_digits(words):
    """Replace tokens made only of Chinese number characters with digits."""
    out = []
    for word in words:
        if (_NUMBER_TOKEN.fullmatch(word)
                and not (len(word) > 1 and word.startswith(_ZERO))):
            parsed = _parse_chars(word)
            out.extend(parsed if parsed is not None else [word])
        else:
            out.append(word)
    return out


def _parse_chars(text):
    """Return [digits] for one valid number token, or None to keep it whole."""
    # As in the English and Turkish steps, a run of unit characters alone is
    # read digit by digit: 一二三 -> "123", 二〇二六 -> "2026", 一零一 -> "101".
    if len(text) > 1 and all(ch in _UNITS for ch in text):
        return ["".join(str(_UNITS[ch]) for ch in text)]

    total = section = 0
    last_unit = None   # value of the unit digit most recently added, if any
    last_kind = None   # "unit", "zero", "mult", or "scale"
    last_scale = None

    for ch in text:
        if ch == _ZERO:
            if last_kind == "zero" or last_unit:
                return None  # repeated or misplaced 零 markers
            last_kind = "zero"
        elif ch in _UNITS:
            if last_kind == "unit" and last_unit:
                return None  # 一百二三-style adjacent digits are ambiguous
            section += _UNITS[ch]
            last_unit = _UNITS[ch]
            last_kind = "unit"
        elif ch in _MULTIPLIERS:
            if last_kind in ("zero", "mult") or last_unit == 0:
                return None  # 零十 / 十十 / 百十 and the like
            multiplier = _MULTIPLIERS[ch]
            if last_unit:
                section += last_unit * (multiplier - 1)
                last_unit = None
            else:
                section += multiplier
            last_kind = "mult"
        else:
            if last_kind == "zero":
                return None  # 一万零万-style misplaced scale
            scale = _SCALES[ch]
            if last_kind == "scale" and section == 0:
                # A scale directly following a smaller one compounds:
                # 万亿 -> 10**4 * 10**8 = 10**12. Equal or falling scales
                # (万万, 亿万) are malformed and kept whole.
                if scale > last_scale:
                    total *= scale
                    last_scale = scale
                    continue
                return None
            if last_scale is not None and scale >= last_scale:
                return None
            total += (section or 1) * scale
            section = 0
            last_unit = None
            last_scale = scale
            last_kind = "scale"

    return [str(total + section)]