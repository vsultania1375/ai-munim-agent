"""Read CSV and XLSX uploads without heavy dependencies (stdlib zip/xml parsing)."""
from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class TablePreview:
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]


def load_table(path: Path, *, sample_rows: int = 10) -> TablePreview:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path, sample_rows=sample_rows)
    if suffix == ".xlsx":
        return _load_xlsx(path, sample_rows=sample_rows)
    raise ValueError("Only CSV and XLSX files are supported")


def _load_csv(path: Path, *, sample_rows: int) -> TablePreview:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = tuple(reader.fieldnames or ())
        rows = tuple({key: value or "" for key, value in row.items()} for row in list(reader)[:sample_rows])
    return TablePreview(headers=headers, rows=rows)


def _load_xlsx(path: Path, *, sample_rows: int) -> TablePreview:
    with zipfile.ZipFile(path) as workbook:
        shared_strings = _shared_strings(workbook)
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    table = _worksheet_rows(sheet, shared_strings)
    if not table:
        return TablePreview(headers=(), rows=())
    headers = tuple(table[0])
    rows = tuple(
        {
            header: values[index] if index < len(values) else ""
            for index, header in enumerate(headers)
        }
        for values in table[1 : sample_rows + 1]
    )
    return TablePreview(headers=headers, rows=rows)


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        payload = workbook.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(payload)
    namespace = _namespace(root.tag)
    return [
        "".join(text.text or "" for text in item.findall(f".//{namespace}t"))
        for item in root.findall(f"{namespace}si")
    ]


def _worksheet_rows(root: ElementTree.Element, shared_strings: list[str]) -> list[list[str]]:
    namespace = _namespace(root.tag)
    rows: list[list[str]] = []
    for row in root.findall(f".//{namespace}row"):
        values: list[str] = []
        for cell in row.findall(f"{namespace}c"):
            index = _column_index(cell.attrib.get("r", "A1"))
            while len(values) < index:
                values.append("")
            values.append(_cell_value(cell, shared_strings, namespace))
        rows.append(values)
    return rows


def _cell_value(cell: ElementTree.Element, shared_strings: list[str], namespace: str) -> str:
    value = cell.find(f"{namespace}v")
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        index = int(value.text)
        return shared_strings[index] if index < len(shared_strings) else ""
    return value.text


def _column_index(reference: str) -> int:
    letters = re.sub(r"[^A-Z]", "", reference.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[0] + "}"
    return ""
