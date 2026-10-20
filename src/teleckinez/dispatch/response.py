"""ResponseComposer: what the assistant says (dispatcher.md §6), uk/en templates.

Ukrainian phrasing uses impersonal past participles ("Світло на кухні
увімкнено", "Штори відкрито"), which need no gender agreement with the
device title; verbs that do agree with the subject use the `plural` flag
from devices.yaml ("штори не відповідають"). All wording lives in TEXTS.
"""

import time
from collections.abc import Callable, Sequence
from typing import Any

from teleckinez.core.models import Availability
from teleckinez.core.models import CommandStatus as S
from teleckinez.devices.registry import Device, DeviceRegistry
from teleckinez.devices.twin import TwinStore
from teleckinez.dispatch.tracker import CommandRecord

MAX_LISTED = 3  # failures / rooms named one by one before "і ще N"
FRESH_S = 30.0  # older data is reported with its age
IMPLY_ON = {"brightness", "color", "volume", "speed", "level", "mode", "target_temp"}

TEXTS: dict[str, dict[str, str]] = {
    "uk": {
        # results of commands; {title} is the device title, {v} a verb ending (plural)
        "completed": "{title} {what}",
        "completed_attr": "{title}: {what}",
        "already": "{title} вже {what}",
        "timeout_ack": "{title} не відповіда{v}",
        "timeout_state": "{title}: команду прийнято, але стан не змінився",
        "offline": "{title} зараз не в мережі",
        "rejected": "{title}: пристрій відхилив команду ({why})",
        "rejected_local": "{title}: {why}",
        "superseded": "{title}: команду замінено новішою",
        "broker": "немає зв'язку з MQTT-брокером",
        "group_ok": "{verb} {n} {devices}",
        "group_partial": "{verb} {ok} з {n} {devices_gen}",
        "group_more": "і ще {n} {devices} з проблемами",
        "progress": "{verb} {title}…",
        # NLU / resolver errors
        "not_understood": "Вибачте, я не зрозумів команду.",
        "low_confidence": "Не впевнений, що правильно почув. Повторіть, будь ласка.",
        "ambiguous_target": "Знайшов {what} {where} — назвіть кімнату.",
        "ambiguous_device": "{where} кілька пристроїв: {titles}. Уточніть, який саме.",
        "not_in_room": "{here} немає такого пристрою, а «{what}» є {there}.",
        "unsupported": "{title} не підтримує цю дію.",
        "unsupported_any": "Ця дія не підтримується.",
        "unknown_device": "Не знайшов такого пристрою.",
        "several": "кілька пристроїв",
        "in_rooms": "у {n} кімнатах",
        # queries
        "no_data": "{title}: немає даних",
        "state_offline": "{title} не в мережі, дані {age}. Останній відомий стан: {state}",
        "state_stale": "{text}. Увага: дані можуть бути застарілими ({age})",
        "state_old": "{text} (дані {age})",
        "and": " та ",
    },
    "en": {
        "completed": "{title} {be} {what}",
        "completed_attr": "{title}: {what}",
        "already": "{title} {be} already {what}",
        "timeout_ack": "{title} {be} not responding",
        "timeout_state": "{title}: the command was accepted, but the state did not change",
        "offline": "{title} {be} offline",
        "rejected": "{title} rejected the command ({why})",
        "rejected_local": "{title}: {why}",
        "superseded": "{title}: replaced by a newer command",
        "broker": "the MQTT broker is unreachable",
        "group_ok": "{verb} {n} {devices}",
        "group_partial": "{verb} {ok} of {n} {devices}",
        "group_more": "and {n} more {devices} failed",
        "progress": "{verb} the {title}…",
        "not_understood": "Sorry, I didn't understand the command.",
        "low_confidence": "I'm not sure I heard that right. Please repeat.",
        "ambiguous_target": "I found {what} {where}. Which room?",
        "ambiguous_device": "There are several devices {where_lc}: {titles}. Which one?",
        "not_in_room": "There is no {what} {here_lc}; there is one {there}.",
        "unsupported": "{title} does not support this action.",
        "unsupported_any": "This action is not supported.",
        "unknown_device": "I couldn't find that device.",
        "several": "several devices",
        "in_rooms": "in {n} rooms",
        "no_data": "{title}: no data yet",
        "state_offline": "{title} {be} offline, data from {age}. Last known state: {state}",
        "state_stale": "{text}. Warning: the data may be stale ({age})",
        "state_old": "{text} (as of {age})",
        "and": " and ",
    },
}

GROUP_VERBS = {
    "uk": {"turn_on": "Увімкнено", "turn_off": "Вимкнено", "open": "Відкрито", "close": "Закрито",
           "lock": "Зачинено", "unlock": "Відчинено", "*": "Виконано для"},
    "en": {"turn_on": "Turned on", "turn_off": "Turned off", "open": "Opened", "close": "Closed",
           "lock": "Locked", "unlock": "Unlocked", "*": "Done for"},
}  # fmt: skip
PROGRESS_VERBS = {
    "uk": {"open": "Відкриваю", "close": "Закриваю", "start": "Запускаю", "*": "Виконую:"},
    "en": {"open": "Opening", "close": "Closing", "start": "Starting", "*": "Working on"},
}
# Phrases that describe a state and read as "<title> <phrase>" without a colon.
STATE_WORDS = {
    "uk": ("увімкнено", "вимкнено", "відкрито", "закрито", "зачинено", "відчинено",
           "прибирає", "на базі", "повертається на базу"),
    "en": ("on", "off", "open", "closed", "locked", "unlocked", "cleaning", "docked",
           "returning to the dock"),
}  # fmt: skip
ERRORS = {
    "uk": {
        "value_out_of_range": "таке значення не підтримується",
        "unsupported_action": "ця дія не підтримується",
        "busy": "пристрій зайнятий",
        "device_error": "внутрішня помилка пристрою",
    },
    "en": {
        "value_out_of_range": "the value is out of range",
        "unsupported_action": "this action is not supported",
        "busy": "the device is busy",
        "device_error": "internal device error",
    },
}
COLOR_NAMES = {
    "uk": {"white": "білий", "warm": "теплий білий", "red": "червоний", "orange": "помаранчевий",
           "yellow": "жовтий", "green": "зелений", "cyan": "блакитний", "blue": "синій",
           "purple": "фіолетовий", "pink": "рожевий"},
    "en": {"warm": "warm white"},
}  # fmt: skip
MODE_NAMES = {
    "uk": {"cool": "охолодження", "heat": "обігрів", "fan": "вентиляція", "dry": "осушення",
           "auto": "автоматичний"},
    "en": {"cool": "cooling", "heat": "heating", "fan": "fan", "dry": "dry", "auto": "auto"},
}  # fmt: skip
VACUUM_STATUS = {
    "uk": {"docked": "на базі", "cleaning": "прибирає", "returning": "повертається на базу"},
    "en": {"docked": "docked", "cleaning": "cleaning", "returning": "returning to the dock"},
}
# (uk, en) templates for attribute values; {v} is the formatted value
ATTR_TEXT = {
    "brightness": ("яскравість {v} %", "brightness {v} %"),
    "color": ("колір {v}", "color {v}"),
    "target_temp": ("встановлено {v} °C", "set to {v} °C"),
    "current_temp": ("температура {v} °C", "temperature {v} °C"),
    "temperature": ("температура {v} °C", "temperature {v} °C"),
    "humidity": ("вологість {v} %", "humidity {v} %"),
    "mode": ("режим {v}", "mode {v}"),
    "speed": ("швидкість {v}", "speed {v}"),
    "level": ("рівень {v}", "level {v}"),
    "volume": ("гучність {v}", "volume {v}"),
    "water_temp": ("вода {v} °C", "water {v} °C"),
    "power_w": ("споживає {v} Вт", "using {v} W"),
    "battery": ("заряд {v} %", "battery {v} %"),
}
BOOL_TEXT = {  # attribute: ((uk true, uk false), (en true, en false))
    "on": (("увімкнено", "вимкнено"), ("on", "off")),
    "locked": (("зачинено", "відчинено"), ("locked", "unlocked")),
    "motion": (("є рух", "руху немає"), ("motion", "no motion")),
    "open": (("відчинено", "зачинено"), ("open", "closed")),
    "heating": (("обігрів працює", ""), ("heating", "")),
}


# ----------------------------------------------------------------------- helpers


def plural_uk(n: int, one: str, few: str, many: str) -> str:
    """1 пристрій, 2 пристрої, 5 пристроїв, 21 пристрій, 11 пристроїв."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def devices_word(n: int, lang: str, genitive: bool = False) -> str:
    if lang == "en":
        return "device" if n == 1 else "devices"
    if genitive:  # "з 6 пристроїв", "з 21 пристрою"
        return "пристрою" if n % 10 == 1 and n % 100 != 11 else "пристроїв"
    return plural_uk(n, "пристрій", "пристрої", "пристроїв")


def num(value: Any, lang: str) -> str:
    """21.5 -> '21,5' (uk) / '21.5' (en); 22.0 -> '22'."""
    if isinstance(value, float):
        value = int(value) if value.is_integer() else round(value, 1)
    text = str(value)
    return text.replace(".", ",") if lang == "uk" else text


def age_text(seconds: float, lang: str) -> str:
    s = int(seconds)
    unit = ("с", "хв", "год") if lang == "uk" else ("s", "min", "h")
    value, idx = (s, 0) if s < 60 else (s // 60, 1) if s < 3600 else (s // 3600, 2)
    return f"{value} {unit[idx]} тому" if lang == "uk" else f"{value} {unit[idx]} ago"


def join(items: Sequence[str], lang: str) -> str:
    """'a, b та c' / 'a, b and c'."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + TEXTS[lang]["and"] + items[-1]


def sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?…" else text + "."


def lower_first(text: str) -> str:
    """'Штори у вітальні' -> 'штори у вітальні'; keeps abbreviations like 'TV'."""
    if len(text) > 1 and text[:2].isupper():
        return text
    return text[:1].lower() + text[1:]


def is_state_phrase(what: str, lang: str) -> bool:
    return what.endswith("% open") or any(
        what == w or what.startswith((w + " ", w + ",")) for w in STATE_WORDS[lang]
    )


# ---------------------------------------------------------------------- composer


class ResponseComposer:
    def __init__(
        self, registry: DeviceRegistry, twins: TwinStore, clock: Callable[[], float] = time.time
    ) -> None:
        self.registry = registry
        self.twins = twins
        self.clock = clock

    @staticmethod
    def t(lang: str, key: str, **kw: Any) -> str:
        return TEXTS[lang][key].format(**kw)

    # ---------------------------------------------------------------- commands

    def command_result(self, records: Sequence[CommandRecord], lang: str = "uk") -> str:
        records = list(records)
        if len(records) == 1:
            return sentence(self._single(records[0], lang))
        ok = [r for r in records if r.status is S.COMPLETED]
        failed = [r for r in records if r.status is not S.COMPLETED]
        verbs = GROUP_VERBS[lang]
        verb = verbs.get(records[0].action, verbs["*"])
        n = len(records)
        if not failed:
            return sentence(self.t(lang, "group_ok", verb=verb, n=n, devices=devices_word(n, lang)))
        parts = []
        if ok:
            parts.append(
                self.t(
                    lang,
                    "group_partial",
                    verb=verb,
                    ok=len(ok),
                    n=n,
                    devices=devices_word(n, lang),
                    devices_gen=devices_word(n, lang, genitive=True),
                )
            )
        reasons = [self._single(r, lang) for r in failed[:MAX_LISTED]]
        if len(failed) > MAX_LISTED:
            rest = len(failed) - MAX_LISTED
            reasons.append(self.t(lang, "group_more", n=rest, devices=devices_word(rest, lang)))
        parts.append("; ".join(reasons))
        return " ".join(sentence(p) for p in parts)

    def progress(self, record: CommandRecord, lang: str = "uk") -> str:
        """Interim message for long actions right after ACCEPTED: "Відкриваю штори…"."""
        kind = record.action
        pos = record.desired.get("position")
        if kind not in ("open", "close", "start") and isinstance(pos, int):
            kind = "open" if pos >= 50 else "close"
        verbs = PROGRESS_VERBS[lang]
        title = lower_first(self.registry.get(record.device_id).title(lang))
        return self.t(lang, "progress", verb=verbs.get(kind, verbs["*"]), title=title)

    def _single(self, r: CommandRecord, lang: str) -> str:
        device = self.registry.get(r.device_id)
        kw = {
            "title": device.title(lang),
            "be": "are" if device.plural else "is",
            "v": "ють" if device.plural else "є",
        }
        if r.status is S.COMPLETED:
            what = self._describe_desired(r.desired, lang)
            if r.already:
                return self.t(lang, "already", what=what, **kw)
            key = "completed" if is_state_phrase(what, lang) else "completed_attr"
            return self.t(lang, key, what=what, **kw)
        if r.error == "broker_unavailable":
            return self.t(lang, "broker")
        why = ERRORS[lang].get(r.error or "", r.error or "")
        key = {
            S.TIMEOUT_ACK: "timeout_ack",
            S.TIMEOUT_STATE: "timeout_state",
            S.DEVICE_OFFLINE: "offline",
            S.REJECTED: "rejected",
            S.REJECTED_LOCAL: "rejected_local",
            S.SUPERSEDED: "superseded",
        }.get(r.status)
        return self.t(lang, key, why=why, **kw) if key else f"{kw['title']}: {r.status.value}"

    def _describe_desired(self, desired: dict[str, Any], lang: str) -> str:
        """Phrase for a reached state: 'увімкнено', 'відкрито на 50 %', 'яскравість 40 %'."""
        if desired.get("on") is True and set(desired) & IMPLY_ON:
            desired = {k: v for k, v in desired.items() if k != "on"}  # "on" is implied
        text = ", ".join(p for k, v in desired.items() if (p := self.attr_phrase(k, v, lang)))
        return text or ("готово" if lang == "uk" else "done")

    # ------------------------------------------------------------------ queries

    def query_result(
        self, devices: Sequence[Device], lang: str = "uk", attribute: str | None = None
    ) -> str:
        now = self.clock()
        return " ".join(sentence(self._describe_device(d, now, lang, attribute)) for d in devices)

    def _describe_device(self, device: Device, now: float, lang: str, attribute: str | None) -> str:
        twin = self.twins.get(device.id)
        title = device.title(lang)
        if not twin.reported:
            return self.t(lang, "no_data", title=title)
        state = twin.reported
        keys = [k for k in self._query_keys(device, attribute) if k in state]
        described = ", ".join(
            p for k in keys if (p := self.attr_phrase(k, state[k], lang, state=state))
        )
        age = twin.age(now) or 0.0
        if twin.availability is Availability.OFFLINE:
            return self.t(
                lang,
                "state_offline",
                title=title,
                be="are" if device.plural else "is",
                age=age_text(age, lang),
                state=described,
            )
        text = f"{title}: {described}"
        if twin.availability is Availability.STALE:
            return self.t(lang, "state_stale", text=text, age=age_text(age, lang))
        if age > FRESH_S:
            return self.t(lang, "state_old", text=text, age=age_text(age, lang))
        return text

    @staticmethod
    def _query_keys(device: Device, attribute: str | None) -> list[str]:
        names = [c.name for c in device.type.capabilities]
        if attribute == "target_temp":  # "яка температура": the measured value first
            order = ("temperature", "current_temp", "water_temp", "target_temp")
            family = [a for a in order if a in names]
            return family[:2] if "current_temp" in family else family[:1]
        if attribute and attribute in names:
            return [attribute]
        return names

    def attr_phrase(self, key: str, value: Any, lang: str, *, state: dict | None = None) -> str:
        """One attribute as words: ('brightness', 40) -> 'яскравість 40 %'."""
        uk = lang == "uk"
        i = 0 if uk else 1
        state = state or {}
        if state.get("on") is False and key in IMPLY_ON | {"power_w"}:
            return ""  # "вимкнено" says it all
        if key in BOOL_TEXT:
            return BOOL_TEXT[key][i][0 if value else 1]
        if key == "position":
            if state.get("moving"):
                return f"рухаються, зараз {value} %" if uk else f"moving, now at {value} %"
            if value >= 100:
                return "відкрито" if uk else "open"
            if value <= 0:
                return "закрито" if uk else "closed"
            return f"відкрито на {value} %" if uk else f"{value} % open"
        if key == "status":
            return VACUUM_STATUS[lang].get(value, value)
        if key == "moving" or (key == "power_w" and not value):
            return ""
        if key == "color":
            value = COLOR_NAMES[lang].get(value, value)
        elif key == "mode":
            value = MODE_NAMES[lang].get(value, value)
        if key in ATTR_TEXT:
            return ATTR_TEXT[key][i].format(v=num(value, lang))
        return f"{key} {value}"

    # ------------------------------------------------------------- failures

    def failure(
        self,
        reason: str,
        details: dict[str, Any] | None = None,
        lang: str = "uk",
        name: str | None = None,
    ) -> str:
        """Text for NLU / resolver errors (nothing was sent to devices)."""
        d = details or {}
        reg = self.registry
        if reason == "ambiguous_target":
            rooms = d.get("rooms", [])
            if len(rooms) > MAX_LISTED + 1:
                where = self.t(lang, "in_rooms", n=len(rooms))
            else:
                where = join([reg.room_loc(r, lang) for r in rooms], lang)
            return self.t(lang, reason, what=name or self.t(lang, "several"), where=where)
        if reason == "ambiguous_device":
            titles = [lower_first(reg.get(i).title(lang)) for i in d.get("devices", [])]
            where = reg.room_loc(d["room"], lang) if d.get("room") else ""
            return self.t(
                lang,
                reason,
                where=sentence(where)[:-1],
                where_lc=where,
                titles=join(titles[: MAX_LISTED + 1], lang),
            )
        if reason == "not_in_room":
            here = reg.room_loc(d["room"], lang) if d.get("room") else ""
            return self.t(
                lang,
                reason,
                here=sentence(here)[:-1],
                here_lc=here,
                what=name or d.get("name") or "",
                there=join([reg.room_loc(r, lang) for r in d.get("rooms", [])], lang),
            )
        if reason == "unsupported_action":
            devices = d.get("devices") or []
            if devices:
                return self.t(lang, "unsupported", title=reg.get(devices[0]).title(lang))
            return self.t(lang, "unsupported_any")
        if reason in ("unknown_device", "low_confidence"):
            return self.t(lang, reason)
        return self.t(lang, "not_understood")
