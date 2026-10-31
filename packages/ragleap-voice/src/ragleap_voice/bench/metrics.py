"""WER and CER with one shared text normalizer for every system under test.

Normalization: Unicode NFKC, casefold, apostrophes dropped, other punctuation and
symbols become spaces, whitespace collapsed. Combining marks are KEPT, so Indic
scripts are not damaged. CER counts Unicode code points (not grapheme clusters)
of the normalized text.

By default digits and spelled-out numbers are NOT unified (normalizer version
"1", the one every published number so far was measured with). Pass
numbers=True (or numbers="en") for English cardinal-number normalization
(version "1+numbers-en.1"), or numbers="tr" for Turkish cardinal and ordinal
normalization (version "1+numbers-tr.2"), or numbers="es" for Spanish cardinal
normalization (version "1+numbers-es.1"), or numbers="de" for German cardinal
normalization (version "1+numbers-de.1"), or numbers="it" for Italian cardinal
normalization (version "1+numbers-it.2"), or numbers="id" for Indonesian
cardinal normalization (version "1+numbers-id.1"); `numbers="pt-BR"` selects Brazilian
Portuguese cardinals (version "1+numbers-pt-BR.1"), and `numbers="hi"` selects Hindi
cardinals (version "1+numbers-hi.2"). Record normalizer_version(numbers)
next to every result measured with any option. Decimal digits of any script
become ASCII digits with all number options. Other language strings raise ValueError;
values other than booleans or strings raise TypeError.

- English number words become digits: a run of single digits is read digit by
  digit ("four seven two nine" -> "4729"); other supported runs are cardinals
  ("one hundred twenty" -> "120").
- Turkish cardinal and ordinal number words become digits: "kırk iki" -> "42",
  "birinci" -> "1", and "iki bin yirmi altıncı" -> "2026". Standard numeric
  ordinal suffixes such as "15'inci" are dropped. A run consisting only of unit
  words is read digit by digit, as in the English mode.
- Spanish cardinal number words become digits: "treinta y dos" -> "32" and
  "un millón" -> "1000000". A run consisting only of unit words is read digit
  by digit. Standard apocopated "un" is recognized only before a scale noun;
  the common article "un" elsewhere is left unchanged.
- German cardinal number words become digits: "einundzwanzig" -> "21" and
  "zwei Millionen" -> "2000000". German decimal commas are retained as
  punctuation; malformed scale sequences are preserved.
- Italian cardinal number words become digits: "ventitré" -> "23" and
  "un milione e due" -> "1000002". Standard closed forms through 999,999 and
  scale nouns through biliardo are supported; Italian decimal commas are
  retained, correctly grouped periods are removed as thousands separators,
  and malformed scale sequences are preserved.
- Indonesian cardinal number words become digits: "dua puluh satu" -> "21"
  and "satu juta dua ratus ribu" -> "1200000". Standard forms through 999
  triliun are supported; malformed scale sequences and adjacent independent
  number phrases are kept separate. Indonesian decimal commas are retained,
  and complete period groups are treated as thousands separators.
- Brazilian Portuguese cardinal number words become digits: "vinte e três" ->
  "23" and "dois milhões e cem mil" -> "2100000". Standard forms through
  999,999,999,999,999,999 are supported, including both quadrilhão and
  quatrilhão scale spellings. Malformed scale sequences are preserved,
  adjacent standalone values remain separate, and decimal commas are retained;
  complete period groups are treated as thousands separators.
- Hindi cardinal number words become digits: "तेईस" -> "23" and
  "दो लाख तीन हज़ार" -> "203000". Standard Devanagari forms through 99
  खरब are supported; malformed scale sequences are preserved. Hindi periods
  are retained as decimal separators and complete Indian comma grouping is
  removed only when the entire numeric token follows the locale grouping.
- Malay cardinal number words become digits: "dua puluh satu" -> "21" and
  "satu juta dua ratus ribu" -> "1200000". Standard forms through
  999,999,999,999,999,999 use the Malaysian short scale (bilion = 10**9,
  trilion = 10**12). Commas are removed only from complete three-digit groups;
  periods between digits remain decimal points.
- English and Turkish drop a comma used as a thousands separator
  ("1,200" -> "1200"). Spanish, German, Italian, Indonesian, and Brazilian
  Portuguese retain it between digits so a decimal comma ("1,234") cannot
  silently become the integer 1234; decimals are not otherwise interpreted.

Not covered: spoken decimals ("three point five"), dates, currency-specific handling,
years read in pairs, dialect and unrelated inflected forms. The Turkish and
Spanish modes do not infer whether a number word is functioning as a number;
for example, Turkish "bir" and Spanish "uno" in ordinary prose are normalized
to digits wherever they occur. The Italian, Indonesian, and Brazilian
Portuguese modes likewise cannot determine whether a cardinal (notably "satu"
in Indonesian or "um" in Portuguese) is being used figuratively or as ordinary
prose.
"""
import re
import unicodedata

NORMALIZER_VERSION = "1"
NUMBERS_VERSION = "numbers-en.1"
NUMBERS_TR_VERSION = "numbers-tr.2"
NUMBERS_ES_VERSION = "numbers-es.1"
NUMBERS_DE_VERSION = "numbers-de.1"
NUMBERS_IT_VERSION = "numbers-it.2"
NUMBERS_ID_VERSION = "numbers-id.1"
NUMBERS_PT_BR_VERSION = "numbers-pt-BR.1"
NUMBERS_HI_VERSION = "numbers-hi.2"
NUMBERS_MS_VERSION = "numbers-ms.1"

_APOSTROPHES = {"'", "\u2019", "\u02bc"}

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine".split())}
_TEENS = {w: i + 10 for i, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: (i + 2) * 10 for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9}
_NUMBER_WORDS = set(_UNITS) | set(_TEENS) | set(_TENS) | set(_SCALES) | {"hundred"}
_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d)[,\u066c](?=\d{3}(?!\d))")
_ITALIAN_GROUPED_NUMBER = re.compile(
    r"(?<!\d)\d{1,3}(?:\.\d{3})+(?=,\d+|[^\d]|$)"
)
_INDONESIAN_NUMBER_SEQUENCE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)*(?!\d|[.,]\d)")
_INDONESIAN_GROUPED_NUMBER = re.compile(
    r"\d{1,3}(?:\.\d{3})+(?:,\d+)?"
)
_PT_BR_NUMBER_SEQUENCE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)*(?!\d|[.,]\d)")
_PT_BR_GROUPED_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?")
_HI_NUMBER_SEQUENCE = re.compile(
    r"(?<![\d.,])\d+(?:[,.]\d+)*(?:[.,](?![\d.,]))?(?![\d.,])"
)
_HI_GROUPED_NUMBER = re.compile(r"\d{1,2}(?:,\d{2})*,\d{3}(?:\.\d+)?")
_MALAY_NUMBER_SEQUENCE = re.compile(
    r"(?<![\d,.])\d+(?:[,.]\d+)*(?:[,.](?![\d.,]))?(?![\d.,])"
)
_MALAY_GROUPED_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[,.])?")


def _number_mode(numbers):
    if numbers is False:
        return None
    if numbers is True:
        return "en"
    if numbers in ("en", "tr", "es", "de", "it", "id", "pt-BR", "hi", "ms"):
        return numbers
    if isinstance(numbers, str):
        raise ValueError(f"unsupported number-normalization language: {numbers!r}")  # noqa: TRY004
    raise TypeError(
        "numbers must be False, True, 'en', 'tr', 'es', 'de', 'it', 'id', 'pt-BR', 'hi', or 'ms'"
    )


def normalizer_version(numbers=False):
    """The version string to record next to any number measured with these settings."""
    mode = _number_mode(numbers)
    if mode is None:
        return NORMALIZER_VERSION
    version = {"en": NUMBERS_VERSION, "tr": NUMBERS_TR_VERSION,
               "es": NUMBERS_ES_VERSION, "de": NUMBERS_DE_VERSION,
               "it": NUMBERS_IT_VERSION, "id": NUMBERS_ID_VERSION,
               "pt-BR": NUMBERS_PT_BR_VERSION, "hi": NUMBERS_HI_VERSION,
               "ms": NUMBERS_MS_VERSION}[mode]
    return NORMALIZER_VERSION + "+" + version


def normalize(text, numbers=False):
    mode = _number_mode(numbers)
    text = unicodedata.normalize("NFKC", text or "")
    if mode == "tr":
        # Turkish dotted/dotless I casing differs from Unicode's default casefold.
        text = text.replace("I", "ı").replace("İ", "i")
    text = text.casefold()
    if mode == "tr":
        from .numbers_tr import normalize_digit_ordinal_suffixes
        text = normalize_digit_ordinal_suffixes(text)
    if mode == "ms":
        def normalize_malay_group(match):
            value = match.group()
            if _MALAY_GROUPED_NUMBER.fullmatch(value):
                return value.replace(",", "")
            return value

        text = _MALAY_NUMBER_SEQUENCE.sub(normalize_malay_group, text)
    elif mode in ("it", "id", "pt-BR"):
        # Italian, Indonesian, and Brazilian Portuguese group thousands with
        # periods and decimals with commas.
        # Normalize complete groups even when CLDR would suppress grouping in
        # formatted output (for example, accept an explicitly written 1.234).
        if mode == "it":
            text = _ITALIAN_GROUPED_NUMBER.sub(
                lambda match: match.group().replace(".", ""), text
            )
        elif mode == "id":
            def normalize_indonesian_group(match):
                value = match.group()
                if _INDONESIAN_GROUPED_NUMBER.fullmatch(value):
                    return value.replace(".", "")
                return value

            text = _INDONESIAN_NUMBER_SEQUENCE.sub(normalize_indonesian_group, text)
        else:
            def normalize_pt_br_group(match):
                value = match.group()
                if _PT_BR_GROUPED_NUMBER.fullmatch(value):
                    return value.replace(".", "")
                return value

            text = _PT_BR_NUMBER_SEQUENCE.sub(normalize_pt_br_group, text)
    elif mode == "hi":
        def normalize_hindi_group(match):
            value = match.group()
            sentence_punctuation = value[-1] if value[-1] in ".," else ""
            number = value[:-1] if sentence_punctuation else value
            if _HI_GROUPED_NUMBER.fullmatch(number):
                return number.replace(",", "") + sentence_punctuation
            return value

        text = _HI_NUMBER_SEQUENCE.sub(normalize_hindi_group, text)
    if mode is not None and mode not in ("es", "de", "it", "id", "pt-BR", "hi", "ms"):
        text = _THOUSANDS_SEPARATOR.sub("", text)
    out = []
    for index, ch in enumerate(text):
        if ch in _APOSTROPHES:
            continue
        if (mode in ("de", "it", "id", "pt-BR") and ch == ","
                and index > 0 and index + 1 < len(text)
                and unicodedata.category(text[index - 1]) == "Nd"
                and unicodedata.category(text[index + 1]) == "Nd"):
            # These locales use a comma as the decimal separator. Keep it only
            # when it separates digits; all other punctuation follows the shared rule.
            out.append(ch)
            continue
        if (mode == "hi" and ch == "." and index > 0 and index + 1 < len(text)
                and unicodedata.category(text[index - 1]) == "Nd"
                and unicodedata.category(text[index + 1]) == "Nd"):
            # Hindi locale data uses a period for decimals.
            out.append(ch)
            continue
        if (mode == "ms" and ch == "." and index > 0 and index + 1 < len(text)
                and unicodedata.category(text[index - 1]) == "Nd"
                and unicodedata.category(text[index + 1]) == "Nd"):
            # Malay CLDR/DBP number formatting uses a point for decimals.
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S") or ch.isspace():
            out.append(" ")
        elif mode is not None and cat == "Nd":
            out.append(str(unicodedata.decimal(ch)))
        else:
            out.append(ch)
    words = "".join(out).split()
    if mode == "en":
        words = _numbers_to_digits(words)
    elif mode == "tr":
        from .numbers_tr import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "es":
        from .numbers_es import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "de":
        from .numbers_de import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "it":
        from .numbers_it import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "id":
        from .numbers_id import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "pt-BR":
        from .numbers_pt_br import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "hi":
        from .numbers_hi import numbers_to_digits
        words = numbers_to_digits(words)
    elif mode == "ms":
        from .numbers_ms import numbers_to_digits
        words = numbers_to_digits(words)
    return " ".join(words)


def _numbers_to_digits(words):
    """Replace every run of English number words with digits."""
    out, run = [], []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in _NUMBER_WORDS:
            run.append(w)
        elif (w == "and" and run and run[-1] in ("hundred", *_SCALES)
              and (nxt in _UNITS or nxt in _TEENS or nxt in _TENS)):
            continue  # "one hundred and twenty"
        else:
            out.extend(_parse_run(run))
            run = []
            out.append(w)
    out.extend(_parse_run(run))
    return out


def _parse_run(run):
    if not run:
        return []
    if len(run) > 1 and all(w in _UNITS for w in run):
        return ["".join(str(_UNITS[w]) for w in run)]  # read digit by digit
    numbers = []
    total = current = 0
    last = None          # kind of the previous word in the current number
    last_scale = None    # the scale most recently applied to the current number

    def flush():
        nonlocal total, current, last, last_scale
        if last is not None:
            numbers.append(str(total + current))
        total = current = 0
        last = last_scale = None

    for w in run:
        if w in _UNITS or w in _TEENS or w in _TENS:
            kind = "unit" if w in _UNITS else "teen" if w in _TEENS else "tens"
            value = _UNITS.get(w, _TEENS.get(w, _TENS.get(w)))
            if last in ("unit", "teen") or (last == "tens" and kind != "unit"):
                flush()
            current += value
        elif w == "hundred":
            if last == "hundred":
                flush()
            elif current >= 100:  # "one hundred one hundred": the last "one" starts the next number
                carry = current % 100
                current -= carry
                flush()
                current, last = carry, "unit"
            kind = "hundred"
            current = (current or 1) * 100
        else:
            scale = _SCALES[w]
            if last_scale is not None and scale >= last_scale:
                carry = current  # "one thousand one thousand": the last "one" starts the next number
                current = 0
                flush()
                current = carry
            kind = "scale"
            total += (current or 1) * scale
            current = 0
            last_scale = scale
        last = kind
    flush()
    return numbers


def edit_distance(ref, hyp):
    """Levenshtein distance between two sequences."""
    if not ref:
        return len(hyp)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if r == h else 1))
        prev = cur
    return prev[-1]


def word_errors(reference, hypothesis, numbers=False):
    """Return (errors, reference_word_count). Raises ValueError on an empty reference."""
    ref = normalize(reference, numbers).split()
    if not ref:
        raise ValueError("empty reference after normalization")
    return edit_distance(ref, normalize(hypothesis, numbers).split()), len(ref)


def char_errors(reference, hypothesis, numbers=False):
    """Return (errors, reference_char_count). Raises ValueError on an empty reference."""
    ref = normalize(reference, numbers)
    if not ref:
        raise ValueError("empty reference after normalization")
    return edit_distance(list(ref), list(normalize(hypothesis, numbers))), len(ref)


def wer(reference, hypothesis, numbers=False):
    e, n = word_errors(reference, hypothesis, numbers)
    return e / n


def cer(reference, hypothesis, numbers=False):
    e, n = char_errors(reference, hypothesis, numbers)
    return e / n
