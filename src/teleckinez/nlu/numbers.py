"""Spoken numbers -> digits for uk/en: "двадцять два" -> 22, "twenty-two" -> 22.

STT engines often return numbers as words (Vosk always does), so the parser
must understand both "40" and "сорок". Covers 0..100 plus ordinals 1..3 used
for speeds/levels ("на третю швидкість") - enough for smart-home commands.
"""

UNITS: dict[str, int] = {}
TENS: dict[str, int] = {}
SPECIAL: dict[str, int] = {}


def _add(target: dict[str, int], value: int, *forms: str) -> None:
    for form in forms:
        target[form] = value


# --- Ukrainian (with frequent case forms)
_add(UNITS, 0, "нуль", "нуля")
_add(UNITS, 1, "один", "одна", "одну", "одне", "одного", "одній")
_add(UNITS, 2, "два", "дві", "двох")
_add(UNITS, 3, "три", "трьох")
_add(UNITS, 4, "чотири", "чотирьох")
_add(UNITS, 5, "п'ять", "п'яти")
_add(UNITS, 6, "шість", "шести")
_add(UNITS, 7, "сім", "семи")
_add(UNITS, 8, "вісім", "восьми")
_add(UNITS, 9, "дев'ять", "дев'яти")
_add(SPECIAL, 10, "десять", "десяти")
_add(SPECIAL, 11, "одинадцять", "одинадцяти")
_add(SPECIAL, 12, "дванадцять", "дванадцяти")
_add(SPECIAL, 13, "тринадцять", "тринадцяти")
_add(SPECIAL, 14, "чотирнадцять", "чотирнадцяти")
_add(SPECIAL, 15, "п'ятнадцять", "п'ятнадцяти")
_add(SPECIAL, 16, "шістнадцять", "шістнадцяти")
_add(SPECIAL, 17, "сімнадцять", "сімнадцяти")
_add(SPECIAL, 18, "вісімнадцять", "вісімнадцяти")
_add(SPECIAL, 19, "дев'ятнадцять", "дев'ятнадцяти")
_add(TENS, 20, "двадцять", "двадцяти")
_add(TENS, 30, "тридцять", "тридцяти")
_add(TENS, 40, "сорок", "сорока")
_add(TENS, 50, "п'ятдесят", "п'ятдесяти")
_add(TENS, 60, "шістдесят", "шістдесяти")
_add(TENS, 70, "сімдесят", "сімдесяти")
_add(TENS, 80, "вісімдесят", "вісімдесяти")
_add(TENS, 90, "дев'яносто")
_add(SPECIAL, 100, "сто", "ста")

# --- English
_EN_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_EN_TEENS = [
    "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
]  # fmt: skip
_EN_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
UNITS.update({w: i for i, w in enumerate(_EN_UNITS)})
SPECIAL.update({w: i for i, w in enumerate(_EN_TEENS, start=10)})
TENS.update({w: i * 10 for i, w in enumerate(_EN_TENS, start=2)})
SPECIAL["hundred"] = 100
SPECIAL["a hundred"] = 100

# Ordinals: speed / level ("третю швидкість", "second speed")
ORDINALS: dict[str, int] = {}
_add(ORDINALS, 1, "перша", "першу", "перший", "першій", "first")
_add(ORDINALS, 2, "друга", "другу", "другий", "другій", "second")
_add(ORDINALS, 3, "третя", "третю", "третій", "third")

# "half" -> 50 %
HALF = {"половину", "наполовину", "половина", "навпіл", "half", "halfway"}


def words_to_numbers(tokens: list[str]) -> list[str]:
    """Replace number words with digit tokens; "двадцять два" -> ["22"]."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in TENS:
            value = TENS[tok]
            if i + 1 < len(tokens) and tokens[i + 1] in UNITS and UNITS[tokens[i + 1]] > 0:
                value += UNITS[tokens[i + 1]]
                i += 1
            out.append(str(value))
        elif tok in SPECIAL:
            out.append(str(SPECIAL[tok]))
        elif tok in UNITS:
            out.append(str(UNITS[tok]))
        elif tok in ORDINALS:
            out.append(str(ORDINALS[tok]))
        elif tok in HALF:
            out.extend(["50", "%"])
        else:
            out.append(tok)
        i += 1
    return out
