"""Build the defence presentation: presentation/Teleckinez.pptx (+ PDF via LibreOffice).

    pip install python-pptx
    python presentation/make_slides.py           # .pptx + .pdf
    python presentation/make_slides.py --no-pdf

Numbers are read from benchmarks/results/*.json at build time, so the slides
always show the same values as SUMMARY.md and the report. Diagrams and
screenshots are taken from docs/diagrams/png and docs/report/img.
"""

import argparse
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "presentation" / "Teleckinez.pptx"
DIAGRAMS = ROOT / "docs" / "diagrams" / "png"
IMG = ROOT / "docs" / "report" / "img"
RESULTS = ROOT / "benchmarks" / "results"

FONT = "Arial"
NAVY = RGBColor(0x1F, 0x24, 0x33)
ACCENT = RGBColor(0x4C, 0x5F, 0xD5)
GREY = RGBColor(0x6B, 0x72, 0x80)
LIGHT = RGBColor(0xF1, 0xF3, 0xF9)
GOOD = RGBColor(0x2E, 0x7D, 0x32)
BAD = RGBColor(0xC6, 0x28, 0x28)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
W, H = Inches(13.333), Inches(7.5)


# ------------------------------------------------------------------ numbers


def latest(name: str) -> dict:
    files = sorted(RESULTS.glob(f"{name}_????-??-??.json"))
    if not files:
        raise SystemExit(f"no results for {name} in {RESULTS}")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def numbers() -> dict[str, str]:
    e1 = latest("e1_stt_synth")["results"]
    e2 = latest("e2_nlu")["results"]
    e3 = latest("e3_latency")["results"]
    e4 = latest("e4_faults")["results"]
    e6 = latest("e6_scale")["results"]
    big = max(e6, key=lambda r: r["devices"])
    return {
        "vosk_cmd": num(e1["vosk_grammar"]["all"]["command_acc"]),
        "vosk_ms": num(e1["vosk_grammar"]["all"]["elapsed_ms"]["p50"], 0),
        "whisper_cmd": num(e1["whisper_gpu"]["all"]["command_acc"]),
        "whisper_ms": num(e1["whisper_gpu"]["all"]["elapsed_ms"]["p50"], 0),
        "rule": num(e2["rule"]["full_accuracy"]),
        "llm": num(e2["llm"]["full_accuracy"]),
        "hybrid": num(e2["hybrid"]["full_accuracy"]),
        "llm_ms": num(e2["llm"]["latency_ms"]["p50"], 0),
        "e3_vosk": num(e3["voice:vosk"]["speech_end_to_tts"]["p50"] / 1000, 2),
        "e3_whisper": num(e3["voice:whisper"]["speech_end_to_tts"]["p50"] / 1000, 2),
        "e3_text": num(e3["text"]["transcript_to_tts"]["p50"], 0),
        "e4_ok": str(sum(r["correct"] for r in e4.values())),
        "e4_n": str(sum(r["n"] for r in e4.values())),
        "e4_offline": num(e4["offline"]["detect_ms"]["p50"], 0),
        "e4_freeze": num(e4["freeze"]["detect_ms"]["p50"] / 1000, 2),
        "e6_n": str(big["devices"]),
        "e6_ack": num(big["ack_p50_ms"], 0),
        "e6_group": num(big["group_s"], 2),
        "e6_rss": num(big["rss_assistant_mb"], 0),
    }


# ------------------------------------------------------------------ helpers


def text_box(slide, x, y, w, h, text, size=18, bold=False, color=NAVY, align=PP_ALIGN.LEFT,
             anchor=MSO_ANCHOR.TOP):  # fmt: skip
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.05)
    lines = text if isinstance(text, list) else [text]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        run.font.size, run.font.bold = Pt(size), bold
        run.font.color.rgb, run.font.name = color, FONT
    return box


def bullets(slide, x, y, w, h, items, size=20, gap=8):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        head, _, rest = item.partition("|")
        marker = p.add_run()
        marker.text = "■  "
        marker.font.size, marker.font.color.rgb, marker.font.name = Pt(size - 8), ACCENT, FONT
        run = p.add_run()
        run.text = head
        run.font.size, run.font.color.rgb, run.font.name = Pt(size), NAVY, FONT
        run.font.bold = bool(rest)
        if rest:
            r2 = p.add_run()
            r2.text = rest
            r2.font.size, r2.font.color.rgb, r2.font.name = Pt(size), NAVY, FONT
    return box


def card(slide, x, y, w, h, text, fill=LIGHT, color=NAVY, size=16, bold=False, line=None):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.adjustments[0] = 0.12
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line:
        shape.line.color.rgb = line
        shape.line.width = Pt(1.5)
    else:
        shape.line.fill.background()
    shape.shadow.inherit = False
    tf = shape.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Inches(0.08)
    for i, line_text in enumerate(text if isinstance(text, list) else [text]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = line_text
        run.font.size = Pt(size if i == 0 else size - 3)
        run.font.bold = bold and i == 0
        run.font.color.rgb, run.font.name = color, FONT
    return shape


def arrow(slide, x1, y1, x2, y2, color=GREY):
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    line.line.color.rgb = color
    line.line.width = Pt(2.25)
    end = line.line._get_or_add_ln()
    tail = end.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"})
    end.append(tail)
    return line


def picture(slide, path: Path, x, y, w, h, border=False):
    """Fit the image into the box, keep the aspect ratio, centre it."""
    iw, ih = Image.open(path).size
    scale = min(w / iw, h / ih)
    pw, ph = int(iw * scale), int(ih * scale)
    pic = slide.shapes.add_picture(str(path), x + (w - pw) // 2, y + (h - ph) // 2, pw, ph)
    if border:
        pic.line.color.rgb = RGBColor(0xD0, 0xD4, 0xE0)
        pic.line.width = Pt(1)
    return pic


def stat(slide, x, y, w, value, label, color=ACCENT):
    text_box(slide, x, y, w, Inches(0.8), value, size=36, bold=True, color=color)
    text_box(slide, x, y + Inches(0.75), w, Inches(0.7), label, size=14, color=GREY)


def base(prs, title: str, n: int):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.5), Inches(0.42), Inches(0.09),
                                 Inches(0.6))  # fmt: skip
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()
    text_box(slide, Inches(0.72), Inches(0.3), Inches(12), Inches(0.8), title, size=30, bold=True,
             anchor=MSO_ANCHOR.MIDDLE)  # fmt: skip
    text_box(slide, Inches(0.5), Inches(7.0), Inches(8), Inches(0.35),
             "Teleckinez · голосовий асистент з контролем виконання команд",
             size=11, color=GREY)  # fmt: skip
    text_box(slide, Inches(12.0), Inches(7.0), Inches(0.8), Inches(0.35), str(n), size=11,
             color=GREY, align=PP_ALIGN.RIGHT)  # fmt: skip
    return slide


def table(slide, x, y, w, rows, col_w, size=14, row_h=None):
    row_h = row_h or Inches(0.45)
    shape = slide.shapes.add_table(len(rows), len(rows[0]), x, y, w, row_h * len(rows))
    tbl = shape.table
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = cw
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            cell = tbl.cell(i, j)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            last = i == len(rows) - 1
            cell.fill.fore_color.rgb = NAVY if i == 0 else (RGBColor(0xE4, 0xE8, 0xFB) if last else
                                                           (WHITE if i % 2 else LIGHT))  # fmt: skip
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
            run = p.add_run()
            run.text = value
            run.font.size, run.font.name = Pt(size), FONT
            run.font.bold = i == 0 or last
            mark = value.split(" ")[0]
            run.font.color.rgb = (WHITE if i == 0 else GOOD if mark == "так" else
                                  BAD if mark == "ні" else NAVY)  # fmt: skip
    return shape


# ------------------------------------------------------------------ slides


def build(n: dict[str, str]) -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # 1. title (department requirements: department, topic, student, supervisor)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    band = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, Inches(1.3))
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()
    text_box(s, Inches(0.6), Inches(0.2), Inches(12.1), Inches(0.9),
             ["Львівський національний університет імені Івана Франка",
              "Факультет електроніки та комп'ютерних технологій · "
              "Кафедра радіоелектронних і комп'ютерних систем"],
             size=16, color=WHITE, align=PP_ALIGN.CENTER)  # fmt: skip
    text_box(s, Inches(0.6), Inches(1.75), Inches(12.1), Inches(0.5), "Курсова робота", size=20,
             color=GREY, align=PP_ALIGN.CENTER)  # fmt: skip
    text_box(s, Inches(1.0), Inches(2.3), Inches(11.3), Inches(1.9),
             "Розробка архітектури голосового асистента для диспетчеризації "
             "та контролю станів виконавчих пристроїв IoT",
             size=34, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)  # fmt: skip
    text_box(s, Inches(0.6), Inches(4.25), Inches(12.1), Inches(0.5), "Teleckinez", size=22,
             bold=True, color=ACCENT, align=PP_ALIGN.CENTER)  # fmt: skip
    text_box(s, Inches(7.4), Inches(5.0), Inches(5.4), Inches(1.9),
             ["Виконав: студент групи ФеІ-35",
              "спеціальності 122 – Комп'ютерні науки",
              "Ціздин Р. Р.",
              "Науковий керівник: [уточнити: доц./ас.] Бойко Я. В."],
             size=16)  # fmt: skip
    text_box(s, Inches(0.6), Inches(6.85), Inches(12.1), Inches(0.4), "Львів – 2026", size=14,
             color=GREY, align=PP_ALIGN.CENTER)  # fmt: skip

    # 2. problem and goal
    s = base(prs, "Проблема: «відправив і забув»", 2)
    text_box(s, Inches(0.6), Inches(1.35), Inches(6), Inches(0.4), "Звичайний асистент", size=18,
             bold=True, color=BAD)  # fmt: skip
    xs = [Inches(0.6), Inches(3.3), Inches(6.0)]
    for x, t in zip(xs, ["«Увімкни світло»", "команда в мережу", "«Готово!»"], strict=True):
        card(s, x, Inches(1.85), Inches(2.3), Inches(0.8), t, size=17)
    for a, b in itertools.pairwise(xs):
        arrow(s, a + Inches(2.3), Inches(2.25), b, Inches(2.25))
    text_box(s, Inches(8.6), Inches(1.75), Inches(4.3), Inches(1.1),
             "а пристрій міг бути офлайн, «зависнути», загубити команду або не виконати її",
             size=15, color=BAD)  # fmt: skip
    text_box(s, Inches(0.6), Inches(3.05), Inches(6), Inches(0.4), "Teleckinez", size=18,
             bold=True, color=GOOD)  # fmt: skip
    xs = [Inches(0.6), Inches(2.75), Inches(4.9), Inches(7.05)]
    steps = ["команда", "пристрій: прийняв (ack)", "стан = бажаний?", "«Світло на кухні ввімкнено»"]
    for x, t in zip(xs, steps, strict=True):
        card(s, x, Inches(3.55), Inches(1.85), Inches(0.95), t, size=15,
             line=GOOD if t.startswith("стан") else None)  # fmt: skip
    for a, b in itertools.pairwise(xs):
        arrow(s, a + Inches(1.85), Inches(4.02), b, Inches(4.02))
    fails = ["«…не відповідає»", "«…зараз не в мережі»", "«…прийнято, але стан не змінився»",
             "«…пристрій відхилив команду»"]  # fmt: skip
    for i, t in enumerate(fails):
        card(s, Inches(9.35), Inches(3.05) + Inches(0.5) * i, Inches(3.5), Inches(0.42), t,
             fill=RGBColor(0xFD, 0xEC, 0xEC), color=BAD, size=13)  # fmt: skip
    card(s, Inches(0.6), Inches(5.15), Inches(12.25), Inches(1.5),
         ["Мета: архітектура локального голосового асистента, який диспетчеризує команди "
          "до IoT-пристроїв і контролює їх виконання",
          "команда вважається виконаною лише тоді, коли звітований стан пристрою збігся з бажаним"],
         fill=RGBColor(0xE4, 0xE8, 0xFB), size=19, bold=True)  # fmt: skip

    # 3. analogues
    s = base(prs, "Аналоги", 3)
    rows = [
        ["Рішення", "Локально", "Українська", "Відкритий код", "Перевіряє виконання"],
        ["Amazon Alexa", "ні", "ні", "ні", "через інтеграцію"],
        ["Google Assistant", "ні", "обмежено", "ні", "через інтеграцію"],
        ["Apple Siri / HomeKit", "частково", "так", "ні", "стан аксесуара"],
        ["Home Assistant Assist", "так / хмара", "частково", "так", "ні: «готово» одразу"],
        ["Rhasspy", "так", "сторонні моделі", "так", "ні: лише намір"],
        ["OpenVoiceOS", "так / сервер", "сторонні моделі", "так", "залежить від навички"],
        ["Teleckinez", "так", "так", "так", "так: ack + збіг стану + LWT"],
    ]
    table(s, Inches(0.6), Inches(1.4), Inches(12.1), rows,
          [Inches(3.0), Inches(1.9), Inches(2.1), Inches(2.0), Inches(3.1)], size=16,
          row_h=Inches(0.55))  # fmt: skip
    text_box(s, Inches(0.6), Inches(6.15), Inches(12.1), Inches(0.6),
             "Відкриті локальні рішення закінчуються на розпізнаному намірі; "
             "про фактичний стан пристрою асистент не дізнається.",
             size=16, color=GREY)  # fmt: skip

    # 4. architecture
    s = base(prs, "Архітектура: подієво-орієнтований модульний моноліт", 4)
    picture(s, DIAGRAMS / "02_components.png", Inches(0.5), Inches(1.25), Inches(8.6), Inches(5.6))
    bullets(s, Inches(9.3), Inches(1.5), Inches(3.7), Inches(5.3), [
        "Внутрішня шина подій:| модулі не викликають один одного",
        "Зовнішня шина — MQTT:| QoS 1, retain, LWT",
        "Змінні рушії:| STT, TTS, NLU, тригер, адаптер прошивки — вибір у settings.yaml",
        "Пристрої:| симулятор, Tasmota, Zigbee2MQTT — без змін ядра",
    ], size=17)  # fmt: skip

    # 5. voice pipeline
    s = base(prs, "Голосовий конвеєр — повністю локально", 5)
    stages = [
        ("Ключове слово", "«Телекінез»\nVosk / openWakeWord"),
        ("VAD", "WebRTC\nкінець = 0,7 с тиші"),
        ("STT", f"Vosk + граматика\n{n['vosk_ms']} мс"),
        ("NLU", "правила + fuzzy\n< 1 мс"),
        ("Диспетчер", "ack ≈ 60 мс\n+ збіг стану"),
        ("TTS", "Piper\n≈ 50 мс"),
    ]
    x0, cw, gap = Inches(0.5), Inches(1.9), Inches(0.21)
    for i, (head, sub) in enumerate(stages):
        x = x0 + (cw + gap) * i
        card(s, x, Inches(1.7), cw, Inches(1.4), [head, *sub.split("\n")], size=19, bold=True,
             fill=RGBColor(0xE4, 0xE8, 0xFB) if head == "Диспетчер" else LIGHT)  # fmt: skip
        if i:
            arrow(s, x - gap, Inches(2.4), x, Inches(2.4))
    stat(s, Inches(0.7), Inches(3.6), Inches(4), f"{n['e3_vosk']} с",
         "від кінця фрази до звуку відповіді (Vosk, медіана, синт.)")  # fmt: skip
    stat(s, Inches(4.9), Inches(3.6), Inches(4), f"{n['e3_text']} мс",
         "текстова команда → звук відповіді")  # fmt: skip
    stat(s, Inches(9.1), Inches(3.6), Inches(3.8), "0 запитів", "в інтернет під час роботи")
    bullets(s, Inches(0.7), Inches(5.25), Inches(12), Inches(1.5), [
        "Українська та англійська;| звертання і команда — однією фразою або з паузою",
        "Без мікрофона:| текстовий режим і поле в веб-панелі (той самий шлях, що й голос)",
    ], size=17)  # fmt: skip

    # 6. command lifecycle
    s = base(prs, "Життєвий цикл команди — скінченний автомат", 6)
    picture(s, DIAGRAMS / "06_command_states.png", Inches(0.4), Inches(1.25), Inches(7.6),
            Inches(5.6))  # fmt: skip
    rows = [
        ["Кінцевий стан", "Відповідь"],
        ["COMPLETED", "«Штори у вітальні відкрито»"],
        ["TIMEOUT_ACK", "«…не відповідає»"],
        ["TIMEOUT_STATE", "«…прийнято, але стан не змінився»"],
        ["DEVICE_OFFLINE", "«…зараз не в мережі»"],
        ["REJECTED", "«…пристрій відхилив команду»"],
        ["REJECTED_LOCAL", "«…не підтримує цю дію»"],
        ["SUPERSEDED", "«…замінено новішою»"],
    ]
    table(s, Inches(8.2), Inches(1.4), Inches(4.8), rows, [Inches(1.85), Inches(2.95)], size=12,
          row_h=Inches(0.5))  # fmt: skip
    text_box(s, Inches(8.2), Inches(5.6), Inches(4.8), Inches(1.2),
             "3 спроби з тим самим cmd_id (ack_timeout 2 с); недопустимий перехід = виняток",
             size=14, color=GREY)  # fmt: skip

    # 7. simulator and faults
    s = base(prs, "Симулятор пристроїв і модель збоїв", 7)
    bullets(s, Inches(0.6), Inches(1.4), Inches(4.6), Inches(5.4), [
        "31 пристрій,| 16 типів, 8 кімнат",
        "Окреме MQTT-з'єднання| на кожен пристрій — справжній LWT",
        "Фізика:| штори і ворота рухаються, чайник гріє, затримка 20–80 мс",
        "7 збоїв:| offline, freeze, drop, stuck, slow, reject, drift",
        "Режими прошивок:| native, Tasmota, Zigbee2MQTT",
    ], size=17)  # fmt: skip
    picture(s, IMG / "fault.png", Inches(5.4), Inches(1.35), Inches(7.5), Inches(5.5), border=True)

    # 8. web panel
    s = base(prs, "Веб-панель", 8)
    picture(s, IMG / "dashboard.png", Inches(0.5), Inches(1.3), Inches(9.0), Inches(5.6),
            border=True)  # fmt: skip
    picture(s, IMG / "voice.png", Inches(9.75), Inches(1.3), Inches(3.1), Inches(4.3), border=True)
    text_box(s, Inches(9.7), Inches(5.7), Inches(3.3), Inches(1.2),
             "Стан наживо через WebSocket; журнал команд із часом кожного етапу; збої; "
             "налаштування без перезапуску", size=13, color=GREY)  # fmt: skip

    # 9. results: recognition and parsing
    s = base(prs, "Результати: розпізнавання і розбір команд", 9)
    picture(s, RESULTS / sorted(p.name for p in RESULTS.glob("e1_stt_synth_accuracy_*.png"))[-1],
            Inches(0.4), Inches(1.3), Inches(6.4), Inches(3.4))  # fmt: skip
    picture(s, RESULTS / sorted(p.name for p in RESULTS.glob("e2_nlu_accuracy_*.png"))[-1],
            Inches(6.9), Inches(1.3), Inches(6.0), Inches(3.4))  # fmt: skip
    stat(s, Inches(0.6), Inches(4.9), Inches(3.1), f"{n['vosk_cmd']} %",
         f"Vosk + граматика, {n['vosk_ms']} мс (Whisper: {n['whisper_cmd']} %)")  # fmt: skip
    stat(s, Inches(3.8), Inches(4.9), Inches(3.0), f"{n['rule']} %", "розбір правилами, 0,04 мс")
    stat(s, Inches(6.9), Inches(4.9), Inches(3.0), f"{n['llm']} %",
         f"локальна LLM qwen2.5:3b, {n['llm_ms']} мс", color=BAD)  # fmt: skip
    stat(s, Inches(10.0), Inches(4.9), Inches(3.0), f"{n['hybrid']} %", "гібрид правила + LLM")
    text_box(s, Inches(0.6), Inches(6.45), Inches(12), Inches(0.45),
             "Голосові виміри — на мовленні, синтезованому Piper (696 кліпів); "
             "живі записи — наступний крок", size=13, color=GREY)  # fmt: skip

    # 10. results: reliability, latency, scale
    s = base(prs, "Результати: надійність і масштаб", 10)
    picture(s, RESULTS / sorted(p.name for p in RESULTS.glob("e4_faults_????-??-??.png"))[-1],
            Inches(0.4), Inches(1.25), Inches(7.0), Inches(5.6))  # fmt: skip
    stat(s, Inches(7.7), Inches(1.4), Inches(2.6), f"{n['e4_ok']}/{n['e4_n']}",
         "правильних реакцій на змодельовані збої", color=GOOD)  # fmt: skip
    stat(s, Inches(10.4), Inches(1.4), Inches(2.6), f"{n['e4_offline']} мс",
         "виявлення відключення (LWT)")  # fmt: skip
    stat(s, Inches(7.7), Inches(3.15), Inches(2.6), f"{n['e4_freeze']} с",
         "«зависання»: 3 спроби × 2 с + паузи")  # fmt: skip
    stat(s, Inches(10.4), Inches(3.15), Inches(2.6), "0", "хибних тривог")
    stat(s, Inches(7.7), Inches(4.9), Inches(2.6), f"{n['e6_n']} пристроїв",
         f"ack p50 {n['e6_ack']} мс — як і на 10")  # fmt: skip
    stat(s, Inches(10.4), Inches(4.9), Inches(2.6), f"{n['e6_group']} с",
         f"«вимкни все» на {n['e6_n']} пристроїв")  # fmt: skip

    # 11. conclusions
    s = base(prs, "Висновки та перспективи", 11)
    bullets(s, Inches(0.6), Inches(1.4), Inches(7.3), Inches(5.4), [
        "Архітектура| з контролем виконання: 7 кінцевих станів команди замість «готово»",
        "Прототип:| голос uk/en, симулятор зі збоями, веб-панель — без інтернету",
        f"Правила > LLM| для команд будинку: {n['rule']} % проти {n['llm']} % і в тисячі разів "
        "швидше",
        f"Збої:| {n['e4_ok']} з {n['e4_n']} розпізнано правильно; масштаб до {n['e6_n']} "
        "пристроїв без деградації",
        "Обмеження:| пристрої змодельовано; голос — синтезований; Zigbee2MQTT без ack",
    ], size=18)  # fmt: skip
    text_box(s, Inches(8.3), Inches(1.4), Inches(4.6), Inches(0.5), "Далі", size=20, bold=True,
             color=ACCENT)  # fmt: skip
    for i, t in enumerate(
        [
            "ESP32 + ESPHome / Tasmota, Zigbee",
            "розпізнавання мовця для замка",
            "сценарії («на ніч») з контролем кожного кроку",
            "стандарт Matter",
        ]
    ):
        card(s, Inches(8.3), Inches(2.05) + Inches(0.95) * i, Inches(4.6), Inches(0.8), t, size=16)

    # 12. thanks
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = NAVY
    bg.line.fill.background()
    text_box(s, Inches(0.6), Inches(2.6), Inches(12.1), Inches(1.2), "Дякую за увагу!", size=48,
             bold=True, color=WHITE, align=PP_ALIGN.CENTER)  # fmt: skip
    text_box(s, Inches(0.6), Inches(3.9), Inches(12.1), Inches(0.6),
             "github.com/Thywyth/Teleckinez",
             size=20, color=RGBColor(0xC9, 0xCF, 0xDD), align=PP_ALIGN.CENTER)  # fmt: skip
    return prs


def to_pdf(pptx: Path) -> Path:
    soffice = shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe"
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(pptx.parent),
                    str(pptx)], check=True, capture_output=True, timeout=180)  # fmt: skip
    return pptx.with_suffix(".pdf")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prs = build(numbers())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUT)
    print(OUT)
    if not args.no_pdf:
        print(to_pdf(OUT))


if __name__ == "__main__":
    main()
