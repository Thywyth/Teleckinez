"""RuleParser: rules + fuzzy matching (nlu.md §1, ADR-005).

1. The text is normalized into tokens (numbers as digits, units as % / °).
2. Tokens are scanned left to right; at each position the longest phrase
   from the index is taken. A phrase matches exactly (score 1.0), by word
   stem for Ukrainian case forms (0.9: "кухні" ~ "кухня"), or fuzzily with
   rapidfuzz for STT errors (score = ratio / 100, >= fuzzy_threshold).
3. Matched slots (room, device name/type, group, action, parameters) are
   assembled into an Intent; confidence is the mean score of the key slots.

Phrases come from devices.yaml (rooms, names, groups) and nlu/lexicon.py.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from rapidfuzz import fuzz, process

from teleckinez.core.models import Intent
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu import lexicon
from teleckinez.nlu.normalizer import detect_lang, tokenize

MAX_PHRASE = 4
FUZZY_MIN_LEN = 4  # shorter words are matched exactly only
STEM_SCORE = 0.9
KEY_SLOTS = ("room", "name", "type", "group", "action", "step", "attr", "query")


class IntentParser(Protocol):
    """Strategy interface: rule-based now, LLM-based later (stage 4)."""

    def parse(self, text: str) -> "ParseResult": ...


@dataclass(frozen=True)
class Entry:
    category: str  # room | name | group | type | action | step | attr | color | mode | query ...
    value: Any


@dataclass
class Match:
    start: int
    end: int
    phrase: str
    entries: list[Entry]
    score: float

    def get(self, category: str) -> Entry | None:
        return next((e for e in self.entries if e.category == category), None)


@dataclass
class ParseResult:
    intent: Intent | None
    reason: str | None = None  # not_understood | low_confidence
    matches: list[Match] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.intent is not None


class PhraseIndex:
    def __init__(self) -> None:
        self.exact: dict[tuple[str, ...], list[Entry]] = defaultdict(list)
        self.by_len: dict[int, list[str]] = defaultdict(list)  # fuzzy candidates
        self._fuzzy_ok: set[tuple[str, ...]] = set()

    def add(self, phrase: str, entry: Entry, fuzzy: bool = True) -> None:
        key = tuple(tokenize(phrase))
        if not key:
            return
        if entry not in self.exact[key]:
            self.exact[key].append(entry)
        if fuzzy and key not in self._fuzzy_ok and len(" ".join(key)) >= FUZZY_MIN_LEN:
            self._fuzzy_ok.add(key)
            self.by_len[len(key)].append(" ".join(key))

    def lookup(self, tokens: list[str], i: int, threshold: float) -> Match | None:
        # 1. exact, longest first
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            key = tuple(tokens[i : i + n])
            if key in self.exact:
                return Match(i, i + n, " ".join(key), self.exact[key], 1.0)
        # 2. stem (single words, Ukrainian case endings) and 3. fuzzy
        best: Match | None = None
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            text = " ".join(tokens[i : i + n])
            if len(text) < FUZZY_MIN_LEN or any(
                t.isdigit() or t in "%°" for t in tokens[i : i + n]
            ):
                continue
            if n == 1:
                stem = self._stem_match(text)
                if stem and (best is None or best.score < STEM_SCORE):
                    best = Match(i, i + 1, stem, self.exact[(stem,)], STEM_SCORE)
            found = process.extractOne(
                text, self.by_len.get(n, []), scorer=fuzz.ratio, score_cutoff=threshold
            )
            if found and (best is None or found[1] / 100 > best.score):
                key = tuple(found[0].split())
                best = Match(i, i + n, found[0], self.exact[key], found[1] / 100)
        return best

    def _stem_match(self, word: str) -> str | None:
        """'кухні' ~ 'кухня', 'спальню' ~ 'спальня': long common prefix, similar length."""
        best, best_prefix = None, 0
        for cand in self.by_len.get(1, []):
            if abs(len(cand) - len(word)) > 3:
                continue
            prefix = 0
            for a, b in zip(cand, word, strict=False):
                if a != b:
                    break
                prefix += 1
            needed = max(4, len(cand) - 2, len(word) - 3)
            if prefix >= needed and prefix > best_prefix:
                best, best_prefix = cand, prefix
        return best


def build_index(registry: DeviceRegistry) -> PhraseIndex:
    idx = PhraseIndex()
    for room_id, room in registry.rooms.items():
        for name in room.all():
            idx.add(name, Entry("room", room_id))
    for name in registry.names():
        idx.add(name, Entry("name", name))
    for group_id, names in registry.config.groups.items():
        for name in names.all():
            idx.add(name, Entry("group", group_id))
    for action, phrases in lexicon.ACTIONS.items():
        for p in phrases:
            idx.add(p, Entry("action", action), fuzzy=len(p) > 4)
    for p, step in lexicon.STEPS.items():
        idx.add(p, Entry("step", step))
    for attr, phrases in lexicon.ATTRIBUTES.items():
        for p in phrases:
            idx.add(p, Entry("attr", attr))
    for color, phrases in lexicon.COLORS.items():
        for p in phrases:
            idx.add(p, Entry("color", color))
    for mode, phrases in lexicon.AC_MODES.items():
        for p in phrases:
            idx.add(p, Entry("mode", mode))
    for dtype, phrases in lexicon.TYPE_WORDS.items():
        for p in phrases:
            idx.add(p, Entry("type", dtype))
    for p in lexicon.QUERY_WORDS:
        idx.add(p, Entry("query", True), fuzzy=False)
    for p in lexicon.ALL_WORDS:
        idx.add(p, Entry("all", True), fuzzy=False)
    for p in lexicon.STATE_ADJECTIVES:
        idx.add(p, Entry("state_adj", p))
    return idx


class RuleParser:
    name = "rule"

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        fuzzy_threshold: float = 85,
        min_confidence: float = 0.6,
    ) -> None:
        self.registry = registry
        self.index = build_index(registry)
        self.fuzzy_threshold = fuzzy_threshold
        self.min_confidence = min_confidence
        self._name_types = {
            name: {d.type.name for d in devs} for name, devs in registry.names().items()
        }

    def scan(self, tokens: list[str]) -> list[Match]:
        matches, i = [], 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.replace(".", "", 1).isdigit() or tok in ("%", "°", "+"):
                matches.append(Match(i, i + 1, tok, [Entry("number", tok)], 1.0))
                i += 1
                continue
            m = self.index.lookup(tokens, i, self.fuzzy_threshold)
            if m is None:
                i += 1  # unknown word or stopword
                continue
            matches.append(m)
            i = m.end
        return matches

    def parse(self, text: str) -> ParseResult:
        tokens = tokenize(text)
        lang = detect_lang(text)
        matches = self.scan(tokens)
        slots: dict[str, Match] = {}
        for m in matches:
            for entry in m.entries:
                slots.setdefault(entry.category, m)  # first occurrence wins

        def value(category: str) -> Any:
            m = slots.get(category)
            entry = m.get(category) if m else None
            return entry.value if entry else None

        room, group, action = value("room"), value("group"), value("action")
        device_name = value("name")
        device_type = value("type")
        if device_name and not device_type and len(self._name_types[device_name]) == 1:
            device_type = next(iter(self._name_types[device_name]))
        if value("all") and not group:
            group = "all"

        # Query: starts with a query word, or has a query word / state adjective and no verb.
        first = matches[0] if matches else None
        is_query = bool(first and first.get("query")) or (
            (value("query") or value("state_adj") or text.strip().endswith("?"))
            and not action
            and not value("step")
        )
        params = self._params(matches, tokens)
        if is_query:
            kind, action = "query", None
            if attr := value("attr"):
                params = {"attribute": attr}
        else:
            kind = "command"
            if value("step"):
                attr_hint, direction = value("step")
                attr_hint = value("attr") or attr_hint
                amount = next((v for k, v in params.items() if k in ("percent", "number")), None)
                params = {
                    "step": {"attribute": attr_hint, "direction": direction, "amount": amount}
                }
                action = "step"
            elif params and action in (None, "set", "turn_on"):
                action = "set"

        has_target = any((device_name, device_type, group, room))
        if not has_target or (kind == "command" and not (action or device_name or device_type)):
            return ParseResult(None, "not_understood", matches)
        if kind == "query" and not any((device_name, device_type, group, room, value("attr"))):
            return ParseResult(None, "not_understood", matches)

        scores = [m.score for c, m in slots.items() if c in KEY_SLOTS]
        confidence = round(sum(scores) / len(scores), 3) if scores else 0.0
        intent = Intent(
            kind=kind,
            action=action,
            device_type=device_type,
            device_name=device_name,
            room=room,
            group=group,
            params=params,
            lang=lang,
            confidence=confidence,
            raw_text=text,
            parser=self.name,
        )
        if confidence < self.min_confidence:
            return ParseResult(None, "low_confidence", matches)
        return ParseResult(intent, None, matches)

    @staticmethod
    def _params(matches: list[Match], tokens: list[str]) -> dict[str, Any]:
        """Numbers with units/attribute nouns, colors and AC modes."""
        params: dict[str, Any] = {}
        attr = next((m.get("attr").value for m in matches if m.get("attr")), None)
        for k, m in enumerate(matches):
            entry = m.entries[0]
            if entry.category == "color":
                params["color"] = entry.value
            elif entry.category == "mode":
                params["mode"] = entry.value
            elif entry.category == "number" and entry.value not in ("%", "°", "+"):
                number = float(entry.value)
                number = int(number) if number.is_integer() else number
                unit = matches[k + 1].phrase if k + 1 < len(matches) else None
                if unit == "°":
                    params["degrees"] = number
                elif unit == "%":
                    params["percent"] = number
                elif attr and attr not in ("humidity", "motion", "battery"):
                    params[attr] = number
                else:
                    params["number"] = number
        return params
