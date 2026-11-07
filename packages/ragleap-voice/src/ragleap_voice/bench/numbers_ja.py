"""Japanese (kanji) cardinal number normalization for benchmark text.

Written Japanese does not put spaces between words, so a token that consists
entirely of number characters is read as one number run; any token that mixes
number characters with prose keeps the whole token unchanged, matching the
context-free limits documented for the other language modes.

Standard standalone kanji cardinals are supported from 零 through the
兆 (10**12) scale in the 万 (10**4) / 億 (10**8) / 兆 system, including
compositions such as 一億二千三百四十五万六千七百八十九. A token made only
of unit characters is read digit by digit, like the English and Turkish unit
runs ("一二三" -> "123", "二〇二六" -> "2026"). 零 is a positional zero
marker and 〇 is the digit zero; digit runs consist only of 〇 and unit digits, so a token
such as 二零二六 follows the positional-zero rules instead. Malformed or
ambiguous sequences such as
十十, 万万, 一億万, or 一百零十 are preserved intact. A multi-character
token starting with 零 (零一, 零零, 零一百) is malformed and kept whole; 〇,
not 零, is the leading digit zero.

Kana readings (にじゅうさん), kana/kanji mixtures inside one token, 大字
forms (壱, 弐, 参, 拾, 萬), Traditional characters, decimal/分数 readings,
dates, and currency forms are outside this module's scope. The parser is
context-free: cardinal characters inside a matched token are read as a number
even in ordinary prose. Written and checked by the contributor against the
references below; a fluent Japanese speaker has not reviewed it.

References checked: Unicode CLDR Japanese number system data
(https://github.com/unicode-org/cldr/blob/main/common/rbnf/ja.xml), CLDR
number-formatting specification
(https://unicode.org/reports/tr35/tr35-numbers.html), and the Japanese
numeral rules described at https://ja.wikipedia.org/wiki/漢数字.
"""
import re

_UNITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
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
_MULT_RANK = {"十": 1, "百": 2, "千": 3}
_SCALES = {"万": 10 ** 4, "億": 10 ** 8, "兆": 10 ** 12}
_NUMBER_TOKEN = re.compile("[零〇一二三四五六七八九十百千万億兆]+")


def numbers_to_digits(words):
    """Replace tokens made only of Japanese kanji number characters with digits."""
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
    # read digit by digit: 一二三 -> "123", 二〇二六 -> "2026", 一〇一 -> "101".
    if len(text) > 1 and all(ch in _UNITS and ch != _ZERO for ch in text):
        return ["".join(str(_UNITS[ch]) for ch in text)]

    total = section = 0
    last_unit = None   # value of the unit digit most recently added, if any
    last_kind = None   # "unit", "zero", "mult", or "scale"
    last_scale = None
    last_mult = None   # rank of the most recent 十/百/千 in this section

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
            rank = _MULT_RANK[ch]
            if last_mult is not None and rank >= last_mult:
                return None  # 二十三百 / 三千二千 / 十二百 are malformed
            multiplier = _MULTIPLIERS[ch]
            if last_unit:
                section += last_unit * (multiplier - 1)
                last_unit = None
            else:
                section += multiplier
            last_kind = "mult"
            last_mult = rank
        else:
            if last_kind == "zero":
                return None  # 一万零万-style misplaced scale
            scale = _SCALES[ch]
            if last_kind == "scale" and section == 0:
                return None  # 万億 / 億万 / 万万-style double scales
            if last_scale is not None and scale >= last_scale:
                return None
            total += (section or 1) * scale
            section = 0
            last_unit = None
            last_mult = None
            last_scale = scale
            last_kind = "scale"

    return [str(total + section)]
