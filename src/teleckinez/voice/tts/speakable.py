"""Turn response text into something a TTS voice can read (voice.md §5).

The Ukrainian Piper voice is character-based: it reads letters only, so
"22 °C" or "40 %" would be skipped. Numbers become words agreeing with the
unit ("1 хвилина", "2 хвилини", "5 хвилин"; "дві хвилини", not "два").
English goes through espeak, which reads digits itself.
"""

import re

from teleckinez.dispatch.response import plural_uk

_ONES_M = ["нуль", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_TEENS = [
    "десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять",
    "п'ятнадцять", "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять",
]  # fmt: skip
_TENS = ["", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят",
         "вісімдесят", "дев'яносто"]  # fmt: skip
_HUNDREDS = ["", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот",
             "вісімсот", "дев'ятсот"]  # fmt: skip

# unit -> (feminine noun, forms for 1 / 2-4 / 5+, form after a decimal number)
_UNITS_UK: dict[str, tuple[bool, tuple[str, str, str], str]] = {
    "%": (False, ("відсоток", "відсотки", "відсотків"), "відсотка"),
    "°C": (False, ("градус", "градуси", "градусів"), "градуса"),
    "°": (False, ("градус", "градуси", "градусів"), "градуса"),
    "Вт": (False, ("ват", "вати", "ват"), "вата"),
    "хв": (True, ("хвилина", "хвилини", "хвилин"), "хвилини"),
    "год": (True, ("година", "години", "годин"), "години"),
    "с": (True, ("секунда", "секунди", "секунд"), "секунди"),
}
_NUMBER_UNIT = re.compile(r"(?<![\w,])(-?\d+(?:[.,]\d+)?)(?:\s*(%|°C|°|Вт\b|хв\b|год\b|с\b))?")
_EN_UNITS = {"°C": " degrees", "°": " degrees", "%": " percent"}


def number_uk(n: int, feminine: bool = False) -> str:
    """0..999 999 as Ukrainian words; 1 and 2 agree with a feminine noun."""
    if n < 0:
        return "мінус " + number_uk(-n, feminine)
    if n == 0:
        return _ONES_M[0]
    if n >= 1000:
        thousands, rest = divmod(n, 1000)
        word = plural_uk(thousands, "тисяча", "тисячі", "тисяч")
        head = number_uk(thousands, feminine=True)
        return f"{head} {word}" + (f" {number_uk(rest, feminine)}" if rest else "")
    words = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        tens, ones = divmod(rest, 10)
        if tens:
            words.append(_TENS[tens])
        if ones:
            if feminine and ones in (1, 2):
                words.append("одна" if ones == 1 else "дві")
            else:
                words.append(_ONES_M[ones])
    return " ".join(words)


def _say_uk(match: re.Match) -> str:
    raw, unit = match.group(1), match.group(2)
    feminine, forms, decimal_form = _UNITS_UK.get(unit, (False, None, None))
    whole, _, frac = raw.replace(".", ",").partition(",")
    n = int(whole)
    if frac:  # 21,5 -> двадцять одна кома п'ять (the whole part agrees with "ціла")
        text = f"{number_uk(n, feminine=True)} кома {number_uk(int(frac), feminine=True)}"
        return f"{text} {decimal_form}" if forms else text
    text = number_uk(n, feminine)
    return f"{text} {plural_uk(abs(n), *forms)}" if forms else text


def speakable(text: str, lang: str) -> str:
    if lang == "uk":
        return _NUMBER_UNIT.sub(_say_uk, text)
    for unit, words in _EN_UNITS.items():
        text = re.sub(rf"\s*{re.escape(unit)}", words, text)
    return text
