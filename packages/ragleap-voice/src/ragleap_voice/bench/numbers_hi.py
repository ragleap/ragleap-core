"""Conservative normalization of standalone Hindi cardinal numbers.

Supports Devanagari cardinal spellings and source-attested alternatives from
zero through 99 kharab (9,999,999,999,999). Compositions use descending
hundred, thousand, lakh, crore, arab, and kharab scales. Hindi cardinals are
indeclinable, but collective forms, ordinals, fractions, negatives, and spoken
decimals are outside scope and protected from partial conversion.

Spellings follow Unicode CLDR's Hindi cardinal RBNF, with alternatives
documented by BIS IS 16500:2012 and the Hindi cardinal grammar reference. The
sources differ on several lexical spellings; attested alternatives are accepted.
The parser is context-free, so number words used as ordinary prose (especially
"एक") can still be normalized. No transliterations or runtime dependencies
are used.
"""

_CARDINALS = {
    "शून्य": 0,
    "एक": 1,
    "दो": 2,
    "तीन": 3,
    "चार": 4,
    "पाँच": 5,
    "पांच": 5,
    "छह": 6,
    "छः": 6,
    "छै": 6,
    "सात": 7,
    "आठ": 8,
    "नौ": 9,
    "दस": 10,
    "ग्यारह": 11,
    "बारह": 12,
    "तेरह": 13,
    "चौदह": 14,
    "पन्द्रह": 15,
    "पंद्रह": 15,
    "सोलह": 16,
    "सत्रह": 17,
    "अठारह": 18,
    "उन्नीस": 19,
    "बीस": 20,
    "इक्कीस": 21,
    "बाईस": 22,
    "तेईस": 23,
    "चौबीस": 24,
    "पच्चीस": 25,
    "छब्बीस": 26,
    "सत्ताईस": 27,
    "अट्ठाईस": 28,
    "उनतीस": 29,
    "तीस": 30,
    "इकतीस": 31,
    "बत्तीस": 32,
    "तैंतीस": 33,
    "चौंतीस": 34,
    "पैंतीस": 35,
    "छत्तीस": 36,
    "सैंतीस": 37,
    "अड़तीस": 38,
    "उनतालीस": 39,
    "चालीस": 40,
    "इकतालीस": 41,
    "बयालीस": 42,
    "ब्यालीस": 42,
    "तैंतालीस": 43,
    "चौवालीस": 44,
    "चवालीस": 44,
    "पैंतालीस": 45,
    "छियालीस": 46,
    "सैंतालीस": 47,
    "अड़तालीस": 48,
    "उनचास": 49,
    "पचास": 50,
    "इक्यावन": 51,
    "बावन": 52,
    "तिरपन": 53,
    "तिरेपन": 53,
    "चौवन": 54,
    "पचपन": 55,
    "छप्पन": 56,
    "सत्तावन": 57,
    "अट्ठावन": 58,
    "उनसठ": 59,
    "साठ": 60,
    "इकसठ": 61,
    "बासठ": 62,
    "तिरेसठ": 63,
    "तिरसठ": 63,
    "चौंसठ": 64,
    "पैंसठ": 65,
    "छियासठ": 66,
    "सड़सठ": 67,
    "सरसठ": 67,
    "अड़सठ": 68,
    "उनहत्तर": 69,
    "सत्तर": 70,
    "इकहत्तर": 71,
    "बहत्तर": 72,
    "तिहत्तर": 73,
    "चौहत्तर": 74,
    "पचहत्तर": 75,
    "छिहत्तर": 76,
    "सतहत्तर": 77,
    "अठहत्तर": 78,
    "उनासी": 79,
    "उन्यासी": 79,
    "अस्सी": 80,
    "इक्यासी": 81,
    "बयासी": 82,
    "तिरासी": 83,
    "चौरासी": 84,
    "पचासी": 85,
    "छियासी": 86,
    "सत्तासी": 87,
    "अट्ठासी": 88,
    "अठासी": 88,
    "नवासी": 89,
    "नब्बे": 90,
    "इक्यानबे": 91,
    "इक्यानवे": 91,
    "बानबे": 92,
    "बानवे": 92,
    "तिरानबे": 93,
    "तिरानवे": 93,
    "चौरानबे": 94,
    "चौरानवे": 94,
    "पंचानबे": 95,
    "पचानवे": 95,
    "छियानबे": 96,
    "छियानवे": 96,
    "सत्तानबे": 97,
    "अट्ठानबे": 98,
    "अठानवे": 98,
    "निन्यानबे": 99,
}

_SCALES = {
    "सौ": 10**2,
    "हज़ार": 10**3,
    "हजार": 10**3,
    "लाख": 10**5,
    "करोड़": 10**7,
    "अरब": 10**9,
    "खरब": 10**11,
}
_SCALE_WORDS = set(_SCALES)
_UNSUPPORTED_MARKERS = {
    # CLDR spellout markers for forms not normalized by this cardinal parser.
    "दशमलव", "ऋण", "वाँ", "वें", "वी", "वीं", "और",
    # Source-attested fractional numerals and documented collective forms.
    "बटा", "बटे", "तिहाई", "चौथाई", "पाव", "आध", "आधा", "आधी", "आधे", "पौन",
    "पौना", "सवा", "डेढ़", "पौने", "ढाई", "साढ़े", "प्रतिशत", "फीसदी",
    "दोनों", "तीनों", "चारों", "पाँचों", "छहों", "हज़ारों", "हजारों",
    "लाखों", "करोड़ों", "अरबों", "खरबों", "दर्जन", "दर्जनों", "सैकड़ा",
    "सैकड़े", "सैकड़ों",
    # Larger Indian scales are intentionally outside the supported range.
    "नील", "पद्म", "शंख",
}
# Hindi ordinals have irregular gender/case forms and regular suffix forms. Keep
# a full adjacent cardinal/ordinal phrase intact instead of
# converting its cardinal prefix. CLDR Hindi RBNF and the Hindi grammar source
# document these forms and suffixes.
_ORDINAL_MARKERS = {
    "पहला", "पहले", "पहली",
    "दूसरा", "दूसरे", "दूसरी",
    "तीसरा", "तीसरे", "तीसरी",
    "चौथा", "चौथे", "चौथी",
    "छटा", "छठा", "छठे", "छठी",
    "शून्यवाँ", "शून्यवें", "शून्यवी",
} | {
    word + suffix
    for word in (set(_CARDINALS) | _SCALE_WORDS)
    for suffix in ("वाँ", "वें", "वी", "वीं")
}
_UNSUPPORTED_MARKERS.update(_ORDINAL_MARKERS)
_NUMBER_WORDS = set(_CARDINALS) | _SCALE_WORDS | _UNSUPPORTED_MARKERS
_MAX_VALUE = 99 * 10**11 + 99 * 10**9 + 99 * 10**7 + 99 * 10**5 + 99 * 10**3 + 999


def _parse_under_100(words):
    if len(words) != 1:
        return None
    value = _CARDINALS.get(words[0])
    return value if value is not None and value < 100 else None


def _parse_hundreds(words):
    """Parse a cardinal group before a scale; Hindi tens are lexical words."""
    if not words:
        return None
    if len(words) == 1 and words[0] == "सौ":
        return 100
    if len(words) == 1:
        return _parse_under_100(words)
    if len(words) == 2 and words[1] == "सौ":
        coefficient = _parse_under_100(words[:1])
        return coefficient * 100 if coefficient and coefficient < 100 else None
    if len(words) == 3 and words[1] == "सौ":
        coefficient = _parse_under_100(words[:1])
        remainder = _parse_under_100(words[2:])
        if coefficient and remainder:
            return coefficient * 100 + remainder
    if len(words) == 2 and words[0] == "सौ":
        remainder = _parse_under_100(words[1:])
        return 100 + remainder if remainder else None
    return None


def _parse_run(run):
    if not run:
        return []
    if any(word in _UNSUPPORTED_MARKERS for word in run):
        return run

    # सौ is parsed inside each sub-hundred-thousand group (e.g. "एक सौ
    # हज़ार"); only the higher place-value words delimit groups.
    scales = [(index, _SCALES[word]) for index, word in enumerate(run)
              if word in _SCALES and _SCALES[word] >= 10**3]
    if not scales:
        value = _parse_hundreds(run)
        if value is not None:
            return [str(value)]
        # Adjacent standalone number words are separate spoken values.
        if len(run) > 1 and all(word in _CARDINALS for word in run):
            return [str(_CARDINALS[word]) for word in run]
        return run

    total = 0
    previous_scale = _MAX_VALUE + 1
    start = 0
    for index, scale in scales:
        if scale >= previous_scale:
            return run
        group = run[start:index]
        if group:
            coefficient = _parse_hundreds(group)
            if coefficient is None or coefficient == 0:
                return run
            max_coefficient = (999 if previous_scale == _MAX_VALUE + 1 and scale == 10**3
                               else (previous_scale // scale - 1
                                     if previous_scale != _MAX_VALUE + 1 else 99))
            if coefficient > max_coefficient:
                return run
        else:
            coefficient = 1
        total += coefficient * scale
        previous_scale = scale
        start = index + 1

    remainder_words = run[start:]
    if remainder_words:
        remainder = _parse_hundreds(remainder_words)
        if remainder is None or remainder == 0:
            return run
        # Each lower group must be smaller than the place represented by the
        # immediately preceding scale; this rejects e.g. लाख ... सौ groups.
        if remainder >= previous_scale:
            return run
        total += remainder
    if total > _MAX_VALUE:
        return run
    return [str(total)]


def numbers_to_digits(words):
    """Replace valid Hindi cardinal runs with decimal digit tokens."""
    out, run = [], []
    for word in words:
        if word in _NUMBER_WORDS:
            run.append(word)
        else:
            out.extend(_parse_run(run))
            run = []
            out.append(word)
    out.extend(_parse_run(run))
    return out
