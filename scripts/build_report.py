"""Build the report: docs/report/NN_*.md -> build/Курсова_Teleckinez.odt (-> PDF).

Styles come from docs/report/reference.odt, which this script generates from
pandoc's default reference document with the formatting of the author's
previous course work (REPORT_PLAN §1): A4, margins 3/1/2/2 cm, Times New
Roman 14, line spacing 1.5, first-line indent 1.25 cm, justified; chapter
headings in capitals, centred, each on a new page; figure captions below the
figure, table captions above the table; listings in Consolas.

    python scripts/build_report.py                  # .odt + .pdf (LibreOffice)
    python scripts/build_report.py --no-pdf         # .odt only (fast; TOC without pages)
    python scripts/build_report.py --reference      # regenerate reference.odt only

Captions are written in the Markdown itself ("Рисунок 3.2 – …", "Table: Таблиця 2.1 – …"),
pandoc numbering is off. Page numbers are a header field (not shown on the
title page, which still counts as page 1). The table of contents is a real
LibreOffice index (Tools → Update → Update All refreshes it) placed where
02_toc.md has its placeholder; its page numbers are filled in by a second
pass: build -> PDF -> find the page of every heading -> build again.
"""

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "report"
REFERENCE = REPORT / "reference.odt"
BUILD = ROOT / "build"
OUTPUT = BUILD / "Курсова_Teleckinez.odt"

FONT = "Times New Roman"
BODY = (
    'fo:margin-top="0cm" fo:margin-bottom="0cm" fo:line-height="150%" '
    'fo:text-align="justify" fo:text-indent="1.25cm" fo:orphans="2" fo:widows="2"'
)
TEXT14 = f'style:font-name="{FONT}" fo:font-size="14pt" fo:language="uk" fo:country="UA"'


def _style(name: str, parent: str, para: str, text: str, display: str | None = None,
           next_style: str | None = None, outline: int | None = None) -> str:  # fmt: skip
    attrs = f'style:name="{name}" style:family="paragraph" style:parent-style-name="{parent}"'
    if display:
        attrs += f' style:display-name="{display}"'
    if next_style:
        attrs += f' style:next-style-name="{next_style}"'
    if outline:
        attrs += f' style:default-outline-level="{outline}"'
    return (
        f"<style:style {attrs}><style:paragraph-properties {para}/>"
        f"<style:text-properties {text}/></style:style>"
    )


STYLES = {
    "Standard": (
        '<style:style style:name="Standard" style:family="paragraph" style:class="text">'
        '<style:paragraph-properties fo:line-height="150%"/>'
        f"<style:text-properties {TEXT14}/></style:style>"
    ),
    "Text_20_body": _style("Text_20_body", "Standard", BODY, TEXT14, "Text body"),
    "First_20_paragraph": _style(
        "First_20_paragraph", "Text_20_body", BODY, TEXT14, "First paragraph", "Text_20_body"
    ),
    "Heading": _style(
        "Heading",
        "Standard",
        'fo:margin-top="0cm" fo:margin-bottom="0.42cm" fo:keep-with-next="always" '
        'fo:line-height="150%"',
        f'{TEXT14} fo:font-weight="bold"',
        next_style="Text_20_body",
    ),
    # РОЗДІЛ 1. … - capitals, centred, new page
    "Heading_20_1": _style(
        "Heading_20_1",
        "Heading",
        'fo:text-align="center" fo:text-indent="0cm" fo:break-before="page" '
        'fo:margin-bottom="0.42cm"',
        f'{TEXT14} fo:font-weight="bold" fo:text-transform="uppercase"',
        "Heading 1",
        "Text_20_body",
        1,
    ),
    "Heading_20_2": _style(
        "Heading_20_2",
        "Heading",
        'fo:text-align="justify" fo:text-indent="1.25cm" fo:margin-top="0.42cm"',
        f'{TEXT14} fo:font-weight="bold"',
        "Heading 2",
        "Text_20_body",
        2,
    ),
    "Heading_20_3": _style(
        "Heading_20_3",
        "Heading",
        'fo:text-align="justify" fo:text-indent="1.25cm" fo:margin-top="0.21cm"',
        f'{TEXT14} fo:font-weight="bold" fo:font-style="italic"',
        "Heading 3",
        "Text_20_body",
        3,
    ),
    "Caption": _style(
        "Caption",
        "Standard",
        'fo:text-align="center" fo:text-indent="0cm" fo:margin-top="0.1cm" '
        'fo:margin-bottom="0.21cm" fo:line-height="150%"',
        TEXT14,
    ),
    # Таблиця 2.1 – … above the table: left-aligned, keeps with the table
    "TableCaption": _style(
        "TableCaption",
        "Caption",
        'fo:text-align="justify" fo:text-indent="0cm" fo:margin-top="0.21cm" '
        'fo:margin-bottom="0.1cm" fo:keep-with-next="always"',
        TEXT14,
    ),
    "FigureCaption": _style(
        "FigureCaption",
        "Caption",
        'fo:text-align="center" fo:text-indent="0cm" fo:margin-bottom="0.32cm"',
        TEXT14,
    ),
    "Figure": _style(
        "Figure",
        "Standard",
        'fo:text-align="center" fo:text-indent="0cm" fo:margin-top="0.21cm" '
        'fo:line-height="100%" fo:keep-with-next="always"',
        TEXT14,
    ),
    "FigureWithCaption": _style(
        "FigureWithCaption",
        "Figure",
        'fo:text-align="center" fo:text-indent="0cm" fo:keep-with-next="always"',
        TEXT14,
    ),
    "Table_20_Contents": _style(
        "Table_20_Contents",
        "Standard",
        'fo:text-align="start" fo:text-indent="0cm" fo:line-height="100%" '
        'fo:margin-top="0.05cm" fo:margin-bottom="0.05cm"',
        f'style:font-name="{FONT}" fo:font-size="12pt"',
        "Table Contents",
    ),
    "Table_20_Heading": _style(
        "Table_20_Heading",
        "Table_20_Contents",
        'fo:text-align="center"',
        f'style:font-name="{FONT}" fo:font-size="12pt" fo:font-weight="bold"',
        "Table Heading",
    ),
    # listings: Consolas 10, single spacing
    "Preformatted_20_Text": _style(
        "Preformatted_20_Text",
        "Standard",
        'fo:text-align="start" fo:text-indent="0cm" fo:line-height="100%" '
        'fo:margin-top="0cm" fo:margin-bottom="0cm"',
        'style:font-name="Consolas" fo:font-size="10pt" fo:language="zxx" fo:country="none"',
        "Preformatted Text",
    ),
}

# Extra styles used with ::: {custom-style="..."} (title page, abstract, references).
EXTRA = [
    # the first paragraph of the title page switches to a master page without a page number
    '<style:style style:name="TitleFirst" style:family="paragraph" '
    'style:parent-style-name="Standard" style:master-page-name="First_20_Page">'
    '<style:paragraph-properties fo:text-align="center" fo:text-indent="0cm"/>'
    f"<style:text-properties {TEXT14}/></style:style>",
    _style("TitleCenter", "Standard", 'fo:text-align="center" fo:text-indent="0cm"',
           TEXT14, "Title Center"),
    _style("TitleCenterBold", "Standard", 'fo:text-align="center" fo:text-indent="0cm"',
           f'{TEXT14} fo:font-weight="bold"', "Title Center Bold"),
    _style("TitleRight", "Standard",
           'fo:text-align="start" fo:text-indent="0cm" fo:margin-left="7.5cm"', TEXT14,
           "Title Right"),
    _style("UnnumberedHeading", "Heading",
           'fo:text-align="center" fo:text-indent="0cm" fo:break-before="page" '
           'fo:margin-bottom="0.42cm"',
           f'{TEXT14} fo:font-weight="bold" fo:text-transform="uppercase"',
           "Unnumbered Heading", "Text_20_body"),
    # left-aligned: long URLs would stretch justified lines
    _style("Reference", "Text_20_body", BODY.replace("justify", "start"), TEXT14, "Reference"),
    # table of contents: right tab with a dotted leader at the text width (17 cm)
    '<style:style style:name="Contents_20_1" style:display-name="Contents 1" '
    'style:family="paragraph" style:parent-style-name="Standard" style:class="index">'
    '<style:paragraph-properties fo:text-align="start" fo:text-indent="0cm" '
    'fo:margin-left="0cm" fo:line-height="150%"><style:tab-stops><style:tab-stop '
    'style:position="17cm" style:type="right" style:leader-style="dotted" '
    'style:leader-text="."/></style:tab-stops></style:paragraph-properties>'
    f"<style:text-properties {TEXT14}/></style:style>",
    '<style:style style:name="Contents_20_2" style:display-name="Contents 2" '
    'style:family="paragraph" style:parent-style-name="Contents_20_1" style:class="index">'
    '<style:paragraph-properties fo:margin-left="0.5cm"><style:tab-stops><style:tab-stop '
    'style:position="16.5cm" style:type="right" style:leader-style="dotted" '
    'style:leader-text="."/></style:tab-stops></style:paragraph-properties></style:style>',
    _style("Listing", "Preformatted_20_Text",
           'fo:text-align="start" fo:text-indent="0cm" fo:line-height="100%"',
           'style:font-name="Consolas" fo:font-size="10pt"', "Listing"),
]  # fmt: skip

PAGE = (
    '<style:page-layout style:name="Mpm1"><style:page-layout-properties fo:page-width="21cm" '
    'fo:page-height="29.7cm" style:num-format="1" style:print-orientation="portrait" '
    'fo:margin-top="2cm" fo:margin-bottom="2cm" fo:margin-left="3cm" fo:margin-right="1cm" '
    'style:writing-mode="lr-tb"/><style:header-style><style:header-footer-properties '
    'fo:min-height="0cm" fo:margin-bottom="0.3cm"/></style:header-style>'
    "<style:footer-style/></style:page-layout>"
)
MASTER = (
    '<office:master-styles><style:master-page style:name="Standard" '
    'style:page-layout-name="Mpm1"><style:header><text:p text:style-name="MP1">'
    '<text:page-number text:select-page="current">1</text:page-number></text:p>'
    "</style:header></style:master-page>"
    '<style:master-page style:name="First_20_Page" style:display-name="First Page" '
    'style:page-layout-name="Mpm1" style:next-style-name="Standard"/>'
    "</office:master-styles>"
)


def make_reference(target: Path = REFERENCE) -> Path:
    """pandoc's default reference.odt with the course work styles."""
    import pypandoc

    raw = subprocess.run(
        [pypandoc.get_pandoc_path(), "--print-default-data-file", "reference.odt"],
        capture_output=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "default.odt"
        src.write_bytes(raw)
        with zipfile.ZipFile(src) as z:
            files = {n: z.read(n) for n in z.namelist()}
    styles = files["styles.xml"].decode("utf-8")
    for name, xml in STYLES.items():
        pattern = re.compile(
            rf'<style:style style:name="{name}"[^>]*?(/>|>.*?</style:style>)', re.S
        )
        styles, n = pattern.subn(lambda _m, x=xml: x, styles, count=1)
        if n == 0:
            styles = styles.replace("</office:styles>", xml + "</office:styles>")
    styles = styles.replace("</office:styles>", "".join(EXTRA) + "</office:styles>")
    styles = re.sub(r"<style:page-layout .*?</style:page-layout>", PAGE, styles, flags=re.S)
    styles = re.sub(r"<office:master-styles>.*?</office:master-styles>", MASTER, styles, flags=re.S)
    styles = re.sub(
        r'(<style:style style:name="MP1"[^>]*>).*?</style:style>',
        r'\1<style:paragraph-properties fo:text-align="end" fo:text-indent="0cm"/>'
        rf'<style:text-properties style:font-name="{FONT}" fo:font-size="14pt"/></style:style>',
        styles,
        flags=re.S,
    )
    font = '<style:font-face style:name="Consolas" svg:font-family="Consolas" ' \
           'style:font-family-generic="modern" style:font-pitch="fixed"/>'  # fmt: skip
    styles = styles.replace("</office:font-face-decls>", font + "</office:font-face-decls>", 1)
    # default language for spell checking and hyphenation
    styles = styles.replace('fo:language="en" fo:country="US"', 'fo:language="uk" fo:country="UA"')
    files["styles.xml"] = styles.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_odt(target, files)
    return target


def _write_odt(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", files.pop("mimetype", b"application/vnd.oasis.opendocument.text"),
                   compress_type=zipfile.ZIP_STORED)  # fmt: skip
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


CELL = (
    'fo:border="0.5pt solid #000000" fo:padding-left="0.1cm" fo:padding-right="0.1cm" '
    'fo:padding-top="0.03cm" fo:padding-bottom="0.03cm"'
)


TOC_PLACEHOLDER = "TKZ-TOC-PLACEHOLDER"
HEADING = re.compile(
    r'<text:h text:style-name="Heading_20_[12]" text:outline-level="([12])">(.*?)</text:h>', re.S
)


def heading_text(xml: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def toc_xml(content: str, pages: dict[str, int] | None) -> str:
    """A LibreOffice table-of-contents index, pre-filled with the headings after it."""
    start = content.find(TOC_PLACEHOLDER)
    entries = []
    for m in HEADING.finditer(content, start):
        title = heading_text(m.group(2))
        page = str((pages or {}).get(title, "00"))
        style = f"Contents_20_{m.group(1)}"
        entries.append(
            f'<text:p text:style-name="{style}">{html.escape(title)}<text:tab/>{page}</text:p>'
        )
    template = "".join(
        f'<text:table-of-content-entry-template text:outline-level="{lvl}" '
        f'text:style-name="Contents_20_{lvl}"><text:index-entry-text/>'
        '<text:index-entry-tab-stop style:type="right" style:leader-char="."/>'
        "<text:index-entry-page-number/></text:table-of-content-entry-template>"
        for lvl in (1, 2)
    )
    return (
        '<text:table-of-content text:protected="true" text:name="Зміст">'
        '<text:table-of-content-source text:outline-level="2" text:use-index-marks="false">'
        '<text:index-title-template text:style-name="UnnumberedHeading">ЗМІСТ'
        f"</text:index-title-template>{template}</text:table-of-content-source>"
        '<text:index-body><text:index-title text:name="Зміст_Head">'
        '<text:p text:style-name="UnnumberedHeading">ЗМІСТ</text:p></text:index-title>'
        + "".join(entries)
        + "</text:index-body></text:table-of-content>"
    )


def toc_titles(odt: Path) -> list[str]:
    with zipfile.ZipFile(odt) as z:
        content = z.read("content.xml").decode("utf-8")
    body = content.split('text:name="Зміст"', 1)[-1].split("</text:table-of-content>", 1)[-1]
    return [heading_text(m.group(2)) for m in HEADING.finditer(body)]


def find_pages(pdf: Path, titles: list[str]) -> dict[str, int]:
    """Page (1-based, the title page counts) where each heading appears, in order."""
    import pymupdf

    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().upper()

    doc = pymupdf.open(pdf)
    texts = [norm(page.get_text()) for page in doc]
    # the page number is printed above the heading, so "ЗМІСТ" is not at the very start
    toc_end = next((i for i, t in enumerate(texts) if "ЗМІСТ" in t[:20]), 0)
    while toc_end + 1 < len(texts) and "....." in texts[toc_end + 1]:  # TOC continues
        toc_end += 1
    pages, current = {}, toc_end + 1
    for title in titles:
        needle = norm(title)[:40]
        for i in range(current, len(texts)):
            if needle in texts[i]:
                pages[title] = i + 1
                current = i
                break
    return pages


def postprocess(path: Path, pages: dict[str, int] | None = None) -> None:
    """Table borders, full-width tables (pandoc writes borderless cells), the TOC."""
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    content = files["content.xml"].decode("utf-8")
    content = re.sub(
        rf"<text:p [^>]*>{TOC_PLACEHOLDER}</text:p>",
        lambda _m: toc_xml(content, pages),
        content,
        count=1,
    )
    content = re.sub(
        r'(<style:style style:name="Table(?:Header)?RowCell" style:family="table-cell">\s*)'
        r'<style:table-cell-properties fo:border="none" />',
        rf"\1<style:table-cell-properties {CELL} />",
        content,
    )
    content = re.sub(
        r'<style:table-properties table:align="center" />',
        '<style:table-properties style:width="17cm" table:align="margins" '
        'fo:margin-top="0cm" fo:margin-bottom="0.21cm" />',
        content,
    )
    files["content.xml"] = content.encode("utf-8")
    _write_odt(path, files)


def sources() -> list[Path]:
    return sorted(p for p in REPORT.glob("[0-9][0-9]_*.md"))


def build(output: Path = OUTPUT, pages: dict[str, int] | None = None) -> Path:
    import pypandoc

    if not REFERENCE.exists():
        make_reference()
    files = sources()
    if not files:
        raise SystemExit("no docs/report/NN_*.md files")
    BUILD.mkdir(parents=True, exist_ok=True)
    text = "\n\n".join(p.read_text(encoding="utf-8") for p in files)
    combined = BUILD / "report_combined.md"
    combined.write_text(text, encoding="utf-8")
    resource = [REPORT, ROOT / "docs" / "diagrams" / "png", ROOT / "benchmarks" / "results", ROOT]
    pypandoc.convert_file(
        str(combined),
        "odt",
        format="markdown-implicit_figures+implicit_figures",
        outputfile=str(output),
        extra_args=[
            f"--reference-doc={REFERENCE}",
            f"--resource-path={';'.join(map(str, resource))}",
            "--metadata=lang:uk-UA",
            "--dpi=200",
        ],
    )
    postprocess(output, pages)
    return output


def soffice() -> str:
    for candidate in (
        shutil.which("soffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("LibreOffice (soffice) not found")


def to_pdf(odt: Path) -> Path:
    subprocess.run(
        [soffice(), "--headless", "--convert-to", "pdf", "--outdir", str(odt.parent), str(odt)],
        check=True,
        capture_output=True,
    )
    return odt.with_suffix(".pdf")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reference", action="store_true", help="regenerate reference.odt only")
    ap.add_argument("--no-pdf", action="store_true", help="only .odt, TOC pages left as 00")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.reference:
        print(make_reference())
        return
    make_reference()
    odt = build()
    if not args.no_pdf:
        # pass 2: the same layout (same TOC lines), now with the real page numbers
        pages = find_pages(to_pdf(odt), toc_titles(odt))
        odt = build(pages=pages)
        pdf = to_pdf(odt)
        missing = [t for t in toc_titles(odt) if t not in pages]
        if missing:
            print("TOC: page not found for", missing)
        print(pdf)
    print(odt)


if __name__ == "__main__":
    main()
