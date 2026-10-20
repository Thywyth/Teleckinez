"""Vocabulary that is not in devices.yaml: verbs, query words, parameters (nlu.md §1).

Rooms, device names and group names come from devices.yaml, so the same
source feeds NLU and (later) the Vosk grammar. Phrases are written in the
normalized form produced by `normalizer.tokenize` (lowercase, apostrophe ').
"""

# Canonical action -> phrases (imperatives sg/pl, infinitives, common surzhyk)
ACTIONS: dict[str, list[str]] = {
    "turn_on": [
        "увімкни", "ввімкни", "увімкніть", "ввімкніть", "увімкнути", "ввімкнути", "включи",
        "включіть", "включити", "запали", "запаліть", "засвіти", "закип'яти", "нагрій",
        "turn on", "switch on", "power on", "enable", "boil", "on",
    ],
    "turn_off": [
        "вимкни", "вимкніть", "вимкнути", "виключи", "виключіть", "виключити", "погаси",
        "погасіть", "загаси", "turn off", "switch off", "power off", "disable", "off",
    ],
    "toggle": ["перемкни", "перемкніть", "toggle"],
    "open": [
        "відкрий", "відкрийте", "відкрити", "відчини", "відчиніть", "відчинити", "підніми",
        "підійміть", "розсунь", "open", "raise",
    ],
    "close": [
        "закрий", "закрийте", "закрити", "зачини", "зачиніть", "зачинити", "опусти",
        "опустіть", "засунь", "close", "shut",
    ],
    "lock": ["замкни", "замкніть", "заблокуй", "lock"],
    "unlock": ["розблокуй", "відімкни", "відімкніть", "unlock"],
    "set": [
        "встанови", "встановіть", "встановити", "постав", "поставте", "зроби", "зробіть",
        "set", "make", "change", "put",
    ],
    "start": ["запусти", "запустіть", "почни", "прибери", "start", "run", "clean"],
    "dock": ["на базу", "додому", "повернись", "dock", "go home", "return"],
    "stop": ["зупини", "зупиніть", "стоп", "stop"],
}  # fmt: skip

# Relative changes: phrase -> (attribute, direction). The attribute is a hint;
# the resolver picks the one the target device actually has.
STEPS: dict[str, tuple[str, int]] = {
    "гучніше": ("volume", 1), "голосніше": ("volume", 1), "louder": ("volume", 1),
    "тихіше": ("volume", -1), "quieter": ("volume", -1),
    "яскравіше": ("brightness", 1), "світліше": ("brightness", 1), "brighter": ("brightness", 1),
    "темніше": ("brightness", -1), "тьмяніше": ("brightness", -1), "dimmer": ("brightness", -1),
    "darker": ("brightness", -1),
    "тепліше": ("target_temp", 1), "warmer": ("target_temp", 1),
    "холодніше": ("target_temp", -1), "прохолодніше": ("target_temp", -1),
    "cooler": ("target_temp", -1), "colder": ("target_temp", -1),
    "швидше": ("speed", 1), "сильніше": ("speed", 1), "faster": ("speed", 1),
    "повільніше": ("speed", -1), "слабше": ("speed", -1), "slower": ("speed", -1),
    "збільш": ("", 1), "підвищ": ("", 1), "додай": ("", 1), "increase": ("", 1),
    "turn up": ("", 1),
    "зменш": ("", -1), "знизь": ("", -1), "убав": ("", -1), "decrease": ("", -1),
    "turn down": ("", -1),
}  # fmt: skip

# Attribute nouns: "яскравість 40", "гучність на 30", "температуру 22"
ATTRIBUTES: dict[str, list[str]] = {
    "brightness": ["яскравість", "яскравості", "brightness"],
    "volume": ["гучність", "гучності", "звук", "volume", "sound"],
    "target_temp": ["температура", "температуру", "температури", "temperature"],
    "position": ["положення", "position"],
    "speed": ["швидкість", "швидкості", "speed"],
    "level": ["рівень", "рівня", "інтенсивність", "level"],
    "humidity": ["вологість", "вологості", "humidity"],
    "motion": ["рух", "руху", "motion", "movement"],
    "battery": ["заряд", "батарея", "батареї", "battery", "charge"],
    "power_w": ["потужність", "споживання", "power", "consumption"],
    "water_temp": ["вода", "воду", "води", "water"],
}

COLORS: dict[str, list[str]] = {
    "white": ["білий", "біле", "білим", "білого", "white"],
    "warm": ["теплий", "тепле", "теплим", "warm"],
    "red": ["червоний", "червоне", "червоним", "червоного", "red"],
    "orange": ["помаранчевий", "помаранчеве", "оранжевий", "orange"],
    "yellow": ["жовтий", "жовте", "жовтим", "yellow"],
    "green": ["зелений", "зелене", "зеленим", "green"],
    "cyan": ["блакитний", "блакитне", "блакитним", "cyan", "light blue"],
    "blue": ["синій", "синє", "синім", "синього", "blue"],
    "purple": ["фіолетовий", "фіолетове", "фіолетовим", "бузковий", "purple", "violet"],
    "pink": ["рожевий", "рожеве", "рожевим", "pink"],
}

AC_MODES: dict[str, list[str]] = {
    "cool": ["охолодження", "охолоджування", "холод", "cooling", "cool"],
    "heat": ["обігрів", "нагрів", "обігрівання", "heating", "heat"],
    "fan": ["вентиляція", "провітрювання", "fan mode", "ventilation"],
    "dry": ["осушення", "осушування", "dry", "dehumidify"],
    "auto": ["авто", "автоматичний", "auto", "automatic"],
}

# Generic type words, for phrases that do not use a configured name.
TYPE_WORDS: dict[str, list[str]] = {
    "light": ["світло", "світла", "світильник", "світильники", "лампи", "лампочку", "освітлення",
              "light", "lights"],
    "curtains": ["штори", "штору", "фіранки", "жалюзі", "curtains", "blinds"],
    "door_lock": ["замок", "замки", "двері", "lock", "door"],
    "climate_sensor": ["датчик", "датчики", "сенсор", "sensor", "sensors"],
}  # fmt: skip

QUERY_WORDS = [
    "яка", "який", "яке", "які", "чи", "скільки", "як там", "що з", "що там", "стан", "статус",
    "what", "what's", "whats", "is", "are", "how", "how much", "status", "state",
]  # fmt: skip

# "Все світло", "всі штори", "вимкни все у вітальні"
ALL_WORDS = [
    "все",
    "усе",
    "всі",
    "усі",
    "всю",
    "усю",
    "весь",
    "увесь",
    "all",
    "every",
    "everything",
]

# Adjectives that ask about a state: "чи зачинені двері?" -> query locked
STATE_ADJECTIVES = [
    "зачинені", "зачинено", "зачинений", "відчинені", "відчинено", "закриті", "закрито",
    "відкриті", "відкрито", "увімкнене", "увімкнено", "увімкнений", "вимкнене", "вимкнено",
    "locked", "unlocked", "open", "closed",
]  # fmt: skip

# Ignored when nothing else matched (prepositions, articles, politeness).
STOPWORDS = {
    "у", "в", "на", "до", "з", "із", "зі", "і", "й", "та", "будь", "ласка", "будьласка",
    "мені", "нам", "там", "тут", "будь-ласка", "the", "a", "an", "in", "at", "to", "of",
    "please", "my", "on the",
}  # fmt: skip
