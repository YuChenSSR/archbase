"""Static conversion of the R table chunks used by the archbase textbook.

Only ``render_table(body, root)`` is public.  The accepted R subset consists of
literal values, local assignments, c/rep, integer ranges, data.frame,
readr::read_csv/read_file and the book's flextable pipelines.  No R, eval, shell,
or Pandoc is invoked.  Unknown syntax/calls/options and malformed input raise
ValueError; paths must resolve inside root.

Output is an HTML fragment suitable for embedding in Markdown.  CSV fields are
kept as text (including leading zeros, empty fields and literal NA).  Merges are
expanded, with an explanatory note, so categories and bit positions stay
explicit.  colformat_md supports the inline code, subscript and superscript
notation used in the book; other text is escaped without being discarded.
Captions, numbering and bookmarks belong to the calling book converter.
"""

from __future__ import annotations

import copy
import csv
import html
import io
import pathlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = ["render_table"]
_MAX_VECTOR = 100_000
_MISSING = object()


@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    offset: int


@dataclass(frozen=True)
class _Symbol:
    name: str


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple[tuple[str | None, Any], ...]


@dataclass(frozen=True)
class _Range:
    first: Any
    last: Any


@dataclass(frozen=True)
class _Pipe:
    source: Any
    call: _Call


@dataclass(frozen=True)
class _Source:
    text: str
    language: str


@dataclass(frozen=True)
class _Metadata:
    kind: str


@dataclass
class _Table:
    keys: list[str]
    rows: list[list[Any]]
    headers: list[list[Any]]
    formatted: bool = False
    expanded: bool = False
    markdown: set[tuple[str, int, int]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def _lex(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
               "a": "\a", "v": "\v", "\\": "\\", "'": "'", '"': '"', "`": "`"}
    while pos < len(text):
        ch = text[pos]
        if ch in " \t\r":
            pos += 1
            continue
        if ch == "#":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end
            continue
        start = pos
        if ch in "\"'`":
            quote = ch
            pos += 1
            value: list[str] = []
            while pos < len(text) and text[pos] != quote:
                ch = text[pos]
                pos += 1
                if ch != "\\":
                    value.append(ch)
                    continue
                if pos >= len(text):
                    raise ValueError(f"Unterminated escape at offset {start}")
                esc = text[pos]
                pos += 1
                if esc in escapes:
                    value.append(escapes[esc])
                elif esc in "xuU":
                    size = {"x": 2, "u": 4, "U": 8}[esc]
                    digits = text[pos:pos + size]
                    if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise ValueError(f"Unsupported R escape at offset {pos - 2}")
                    try:
                        value.append(chr(int(digits, 16)))
                    except ValueError as exc:
                        raise ValueError(f"Invalid Unicode escape at offset {pos}") from exc
                    pos += size
                else:
                    raise ValueError(f"Unsupported R escape \\{esc} at offset {pos - 2}")
            if pos == len(text):
                raise ValueError(f"Unterminated string at offset {start}")
            pos += 1
            tokens.append(_Token("id" if quote == "`" else "string", "".join(value), start))
            continue
        op = next((op for op in ("%>%", "<-", "::") if text.startswith(op, pos)), None)
        if op:
            tokens.append(_Token(op, op, pos))
            pos += len(op)
            continue
        number = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?L?", text[pos:])
        if number:
            raw = number[0]
            value = Decimal(raw.removesuffix("L"))
            tokens.append(_Token("number", value, pos))
            pos += len(raw)
            continue
        name = re.match(r"(?:[^\W\d]|\.)[\w.]*", text[pos:])
        if name:
            tokens.append(_Token("id", name[0], pos))
            pos += len(name[0])
            continue
        if ch in "(),=:+-;\n":
            tokens.append(_Token(ch, ch, pos))
            pos += 1
            continue
        raise ValueError(f"Unsupported R syntax {ch!r} at offset {pos}")
    tokens.append(_Token("eof", "", pos))
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.tokens = _lex(text)
        self.pos = 0

    @property
    def token(self) -> _Token:
        return self.tokens[self.pos]

    def take(self, kind: str) -> _Token:
        token = self.token
        if token.kind != kind:
            raise ValueError(f"Expected {kind!r}, got {token.value!r} at offset {token.offset}")
        self.pos += 1
        return token

    def newlines(self) -> None:
        while self.token.kind == "\n":
            self.pos += 1

    def parse(self) -> list[tuple[str | None, Any]]:
        statements = []
        while self.token.kind != "eof":
            if self.token.kind in ("\n", ";"):
                self.pos += 1
                continue
            name = None
            if self.token.kind == "id" and self.tokens[self.pos + 1].kind in ("<-", "="):
                name = self.take("id").value
                self.pos += 1
                self.newlines()
            expression = self.expression()
            if self.token.kind not in ("\n", ";", "eof"):
                raise ValueError(f"Unexpected {self.token.value!r} at offset {self.token.offset}")
            statements.append((name, expression))
        return statements

    def expression(self, nested: bool = False) -> Any:
        value = self.atom()
        if nested:
            self.newlines()
        if self.token.kind == ":":
            self.pos += 1
            self.newlines()
            value = _Range(value, self.atom())
        if nested:
            self.newlines()
        while self.token.kind == "%>%":
            self.pos += 1
            self.newlines()
            call = self.atom()
            if not isinstance(call, _Call):
                raise ValueError("A pipeline step must be a supported function call")
            value = _Pipe(value, call)
            if nested:
                self.newlines()
        return value

    def atom(self) -> Any:
        token = self.token
        if token.kind in ("string", "number"):
            self.pos += 1
            return token.value
        if token.kind in ("+", "-"):
            self.pos += 1
            number = self.take("number").value
            return number if token.kind == "+" else -number
        if token.kind == "(":
            self.pos += 1
            self.newlines()
            value = self.expression(nested=True)
            self.take(")")
            return value
        name = self.take("id").value
        if self.token.kind == "::":
            self.pos += 1
            name += "::" + self.take("id").value
        if self.token.kind != "(":
            constants = {"TRUE": True, "FALSE": False, "NULL": None,
                         "NA": "NA", "NA_character_": "NA", "NA_integer_": "NA",
                         "NA_real_": "NA", "NaN": "NaN", "Inf": "Inf"}
            return constants[name] if name in constants else _Symbol(name)
        self.pos += 1
        self.newlines()
        args = []
        while self.token.kind != ")":
            key = None
            if self.token.kind in ("id", "string") and self.tokens[self.pos + 1].kind == "=":
                key = self.token.value
                self.pos += 2
                self.newlines()
            args.append((key, self.expression(nested=True)))
            if self.token.kind != ",":
                break
            self.pos += 1
            self.newlines()
            if self.token.kind == ")":
                raise ValueError("Trailing/missing R arguments are not supported")
        self.take(")")
        return _Call(name, tuple(args))


def _vector(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, bool, int, Decimal, _Source)):
        return [value]
    raise ValueError(f"Expected a literal vector, got {type(value).__name__}")


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value):
        raise ValueError(f"Expected an integer, got {value!r}")
    return int(value)


def _text(value: Any) -> str:
    if isinstance(value, _Source):
        return value.text
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, (str, int, Decimal)):
        return str(value)
    raise ValueError(f"Expected cell text, got {type(value).__name__}")


def _bind(name: str, args: list[tuple[str | None, Any]],
          parameters: tuple[str, ...], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    bound = {}
    for key, value in args:
        if key is not None:
            if key not in parameters or key in bound:
                raise ValueError(f"{name}: unknown or duplicate argument {key!r}")
            bound[key] = value
    positional = (key for key in parameters if key not in bound)
    for key, value in args:
        if key is None:
            target = next(positional, None)
            if target is None:
                raise ValueError(f"{name}: too many positional arguments")
            bound[target] = value
    for key in parameters:
        if key not in bound:
            if defaults is None or key not in defaults:
                raise ValueError(f"{name}: missing argument {key!r}")
            bound[key] = defaults[key]
    return bound


# Only presentation operations verified in the source book are discarded.
_STYLE_PARAMETERS = {
    "theme_box": (), "autofit": ("add_w", "add_h", "part"),
    "FitFlextableToPage": ("pgwidth",), "width": ("j", "width"),
    "valign": ("i", "j", "valign", "part"),
    "align": ("i", "j", "align", "part"),
    "fontsize": ("i", "j", "size", "part"),
    "bold": ("i", "j", "bold", "part"), "bg": ("i", "j", "bg", "part"),
    "border_remove": (), "border_outer": ("border", "part"),
    "border_inner": ("border", "part"),
    "hline": ("i", "j", "border", "part"),
    "hline_top": ("j", "border", "part"),
    "vline": ("i", "j", "border", "part"),
}
_TABLE_PARAMETERS = {
    "set_caption": ("caption", "autonum"),
    "delete_part": ("part",),
    "add_header_row": ("values", "colwidths", "top"),
    "merge_v": ("j", "part"), "merge_h": ("i", "part"),
    "colformat_md": ("i", "j", "part", "md_extensions"),
    "footnote": ("i", "j", "value", "ref_symbols", "part"),
}
_NAMESPACES = {
    "base": {"c", "rep", "data.frame"}, "readr": {"read_csv", "read_file"},
    "officer": {"run_autonum", "fp_border"}, "ftExtra": {"colformat_md"},
    "flextable": ({"flextable", "as_paragraph"} | set(_TABLE_PARAMETERS)
                  | (set(_STYLE_PARAMETERS) - {"FitFlextableToPage", "colformat_md"})),
}


def _function_name(name: str) -> str:
    if "::" not in name:
        return name
    package, function = name.split("::", 1)
    if function not in _NAMESPACES.get(package, set()):
        raise ValueError(f"Unsupported R function {name!r}")
    return function


class _Evaluator:
    def __init__(self, root: pathlib.Path):
        self.root = root.resolve()
        self.variables: dict[str, Any] = {}

    def evaluate(self, node: Any) -> Any:
        if isinstance(node, _Symbol):
            if node.name not in self.variables:
                raise ValueError(f"Unknown R variable {node.name!r}")
            return copy.deepcopy(self.variables[node.name])
        if isinstance(node, _Range):
            first, last = _integer(self.evaluate(node.first)), _integer(self.evaluate(node.last))
            if abs(last - first) + 1 > _MAX_VECTOR:
                raise ValueError("Integer range exceeds static conversion limit")
            step = 1 if last >= first else -1
            return list(range(first, last + step, step))
        if isinstance(node, _Pipe):
            source = self.evaluate(node.source)
            return self.call(node.call, [(None, source)])
        if isinstance(node, _Call):
            return self.call(node)
        return node

    def read(self, value: Any) -> tuple[pathlib.Path, str]:
        if not isinstance(value, str):
            raise ValueError("File paths must be literal strings")
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"File path escapes book root: {value!r}")
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return path, stream.read()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read {value!r}: {exc}") from exc

    def call(self, call: _Call, prefix: list[tuple[str | None, Any]] | None = None) -> Any:
        name = _function_name(call.name)
        args = list(prefix or []) + [(key, self.evaluate(value)) for key, value in call.args]
        if name == "c":
            if any(key is not None for key, _ in args):
                raise ValueError("Named c() vectors are outside the supported subset")
            values = [item for _, value in args for item in _vector(value)]
            if len(values) > _MAX_VECTOR:
                raise ValueError("Vector exceeds static conversion limit")
            return values
        if name == "rep":
            a = _bind(name, args, ("x", "times", "each"), {"times": 1, "each": 1})
            values = _vector(a["x"])
            each = _integer(a["each"])
            counts = [_integer(item) for item in _vector(a["times"])]
            if each < 0 or any(count < 0 for count in counts):
                raise ValueError("rep() counts must be nonnegative")
            if len(values) * each > _MAX_VECTOR:
                raise ValueError("rep() exceeds static conversion limit")
            values = [value for value in values for _ in range(each)]
            if len(counts) == 1:
                if len(values) * counts[0] > _MAX_VECTOR:
                    raise ValueError("rep() exceeds static conversion limit")
                return values * counts[0]
            if len(counts) != len(values) or sum(counts) > _MAX_VECTOR:
                raise ValueError("rep() count vector does not match values or exceeds limit")
            return [value for value, count in zip(values, counts) for _ in range(count)]
        if name == "data.frame":
            columns = []
            keys = []
            for key, value in args:
                if key in ("stringsAsFactors", "check.names"):
                    if not isinstance(value, bool):
                        raise ValueError(f"data.frame: {key} must be a boolean")
                    continue
                if key is None or key in keys or key == "row.names":
                    raise ValueError("data.frame requires distinct named columns, without row.names")
                keys.append(key)
                columns.append(_vector(value))
            if not keys:
                raise ValueError("data.frame must contain named columns")
            size = max(map(len, columns))
            if size > _MAX_VECTOR or any(not col or size % len(col) for col in columns):
                raise ValueError("data.frame columns cannot be recycled to a common length")
            rows = [[col[index % len(col)] for col in columns] for index in range(size)]
            return _Table(keys, rows, [keys.copy()])
        if name in ("read_csv", "read_file"):
            a = _bind(name, args, ("file",))
            path, text = self.read(a["file"])
            if name == "read_file":
                language = {".c": "c", ".h": "c", ".s": "asm", ".txt": "text"}.get(path.suffix.lower())
                if language is None:
                    raise ValueError(f"Unsupported source-file extension: {path.suffix}")
                return _Source(text, language)
            try:
                rows = [row for row in csv.reader(io.StringIO(text, newline=""), strict=True) if row]
            except csv.Error as exc:
                raise ValueError(f"Invalid CSV {a['file']!r}: {exc}") from exc
            if not rows or not rows[0]:
                raise ValueError(f"Empty CSV {a['file']!r}")
            if any(len(row) != len(rows[0]) for row in rows[1:]):
                raise ValueError(f"Inconsistent CSV row widths in {a['file']!r}")
            return _Table(rows[0], rows[1:], [rows[0].copy()])
        if name == "run_autonum":
            a = _bind(name, args, ("seq_id", "bkm", "bkm_all"),
                      {"seq_id": "tab", "bkm": None, "bkm_all": False})
            if a["seq_id"] != "tab":
                raise ValueError("Only table autonumber metadata is supported")
            return _Metadata(name)
        if name == "fp_border":
            _bind(name, args, ("color", "style", "width"),
                  {"color": None, "style": None, "width": None})
            return _Metadata(name)
        if name == "as_paragraph":
            if len(args) != 1 or args[0][0] is not None:
                raise ValueError("as_paragraph requires one literal vector")
            return _vector(args[0][1])
        if name == "flextable":
            a = _bind(name, args, ("data",))
            table = a["data"]
            if not isinstance(table, _Table) or table.formatted:
                raise ValueError("flextable requires a CSV or data.frame")
            table.formatted = True
            return table
        if name in _STYLE_PARAMETERS or name in _TABLE_PARAMETERS:
            parameters = (_STYLE_PARAMETERS | _TABLE_PARAMETERS)[name]
            a = _bind(name, args, ("x",) + parameters, {key: _MISSING for key in parameters})
            table = a.pop("x")
            if not isinstance(table, _Table) or not table.formatted:
                raise ValueError(f"{name} requires a flextable")
            if name in _STYLE_PARAMETERS:
                return table
            return self.transform(name, table, a)
        raise ValueError(f"Unsupported R function {call.name!r}")

    def transform(self, name: str, table: _Table, args: dict[str, Any]) -> _Table:
        def get(key: str, default: Any = None) -> Any:
            return default if args.get(key, _MISSING) is _MISSING else args[key]

        part = get("part", "body")
        if part not in ("body", "header", "all"):
            raise ValueError(f"{name}: unsupported table part {part!r}")
        if name == "set_caption":
            if not isinstance(get("caption"), str):
                raise ValueError("set_caption requires a literal caption")
        elif name == "delete_part":
            if part != "header":
                raise ValueError("Only delete_part(part='header') is supported")
            table.headers.clear()
            table.markdown = {cell for cell in table.markdown if cell[0] != "header"}
        elif name == "add_header_row":
            values = _vector(get("values"))
            widths = [_integer(n) for n in _vector(get("colwidths", [1] * len(values)))]
            if not values or len(values) != len(widths) or any(n <= 0 for n in widths):
                raise ValueError("Invalid add_header_row values/colwidths")
            if sum(widths) != len(table.keys):
                raise ValueError("add_header_row colwidths must cover every column exactly")
            row = [value for value, width in zip(values, widths) for _ in range(width)]
            top = get("top", True)
            if not isinstance(top, bool):
                raise ValueError("add_header_row top must be a boolean")
            if top:
                table.headers.insert(0, row)
                table.markdown = {(p, i + (p == "header"), j) for p, i, j in table.markdown}
            else:
                table.headers.append(row)
            table.expanded |= any(width > 1 for width in widths)
        elif name in ("merge_v", "merge_h", "colformat_md"):
            for section in ("header", "body") if part == "all" else (part,):
                rows = table.headers if section == "header" else table.rows
                ii = _indices(get("i"), len(rows))
                jj = _indices(get("j"), len(table.keys), table.keys)
                if name == "colformat_md":
                    extension = get("md_extensions")
                    if extension not in (None, "+hard_line_breaks"):
                        raise ValueError(f"Unsupported Markdown extensions: {extension!r}")
                    table.markdown.update((section, i, j) for i in ii for j in jj)
            if name.startswith("merge_"):
                table.expanded = True
        elif name == "footnote":
            if part == "all":
                raise ValueError("footnote part must be body or header")
            rows = table.headers if part == "header" else table.rows
            ii = _indices(get("i", 1), len(rows))
            jj = _indices(get("j", 1), len(table.keys), table.keys)
            notes = _vector(get("value"))
            if not notes:
                raise ValueError("footnote requires nonempty text")
            symbols = _vector(get("ref_symbols"))
            if symbols and len(symbols) != len(notes):
                raise ValueError("Footnote symbols must match the number of notes")
            location = ("表头" if part == "header" else "正文")
            location += "第" + "、".join(str(i + 1) for i in ii) + "行，"
            location += "列：" + "、".join(table.keys[j] for j in jj)
            for index, note in enumerate(notes):
                symbol = f"[{_text(symbols[index])}] " if symbols else ""
                table.notes.append(f"注（{location}）：{symbol}{_text(note)}")
        return table


def _indices(value: Any, size: int, keys: list[str] | None = None) -> list[int]:
    if value is None:
        return list(range(size))
    result = []
    for item in _vector(value):
        if isinstance(item, str):
            if keys is None or keys.count(item) != 1:
                raise ValueError(f"Unknown or ambiguous column {item!r}")
            index = keys.index(item)
        else:
            index = _integer(item) - 1
        if not 0 <= index < size:
            raise ValueError(f"Table selector {item!r} is out of range")
        result.append(index)
    return result


def _escape(text: str) -> str:
    # Pandoc's markdown_in_html_blocks can otherwise reinterpret registers
    # ($a0), multiplication signs and backslashes as Markdown/TeX syntax.
    escaped = html.escape(text, quote=False)
    return re.sub(r"[\\`*_$~^\[\]{}|#!\t]", lambda match: f"&#{ord(match[0])};", escaped)


def _inline(text: str, markdown: bool) -> str:
    if not markdown:
        return _escape(text).replace("\r\n", "\n").replace("\n", "<br />")
    pattern = re.compile(r"(`+)([^`]*?)\1|(?<!~)~([^~\s]+)~(?!~)|(?<!\^)\^([^\^\s]+)\^(?!\^)")
    parts = []
    previous = 0
    for match in pattern.finditer(text):
        parts.append(_inline(text[previous:match.start()], False))
        if match[1]:
            parts.append("<code>" + html.escape(match[2], quote=False) + "</code>")
        elif match[3]:
            parts.append("<sub>" + html.escape(match[3], quote=False) + "</sub>")
        else:
            parts.append("<sup>" + html.escape(match[4], quote=False) + "</sup>")
        previous = match.end()
    parts.append(_inline(text[previous:], False))
    return "".join(parts)


def _code(text: str, language: str) -> str:
    # CommonMark ends a raw HTML block at a blank line, even inside <pre>.
    escaped = (html.escape(text, quote=False).replace("\r", "&#13;")
               .replace("\n", "&#10;").replace("\t", "&#9;"))
    return f'<pre><code class="language-{language}">' + escaped + "</code></pre>"


def _cell(value: Any, markdown: bool) -> str:
    if isinstance(value, _Source):
        return _code(value.text, value.language)
    text = _text(value)
    if "\n" in text and not markdown:
        return _code(text, "text")
    return _inline(text, markdown)


def _render(table: _Table) -> str:
    lines = ["<table>"]
    for part, rows, section, tag in (("header", table.headers, "thead", "th"),
                                     ("body", table.rows, "tbody", "td")):
        if not rows:
            continue
        lines.append(f"<{section}>")
        for i, row in enumerate(rows):
            cells = [_cell(value, (part, i, j) in table.markdown) for j, value in enumerate(row)]
            lines.append("<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>")
        lines.append(f"</{section}>")
    lines.append("</table>")
    if table.expanded:
        lines.append("<p>注：原表的合并单元格展开显示；重复值保留分类、分组或位域范围。</p>")
    lines.extend("<p>" + _inline(note, False) + "</p>" for note in table.notes)
    return "\n".join(lines)


def render_table(body: str, root: pathlib.Path) -> str:
    """Render one R chunk body as caption-free HTML, without executing R.

    ``root`` is the book root used to resolve CSV/source paths.  ``body`` must
    omit chunk fences/options and contain only the documented static subset.
    Data frames use exact-length R recycling; generated vectors are limited to
    100,000 elements.  All input statements are checked, including assignments
    and arguments to ignored presentation calls.  Unsupported input raises
    ValueError instead of falling back to displaying R implementation code.
    """
    if not isinstance(body, str) or not isinstance(root, pathlib.Path):
        raise ValueError("Expected a chunk body string and a pathlib.Path book root")
    evaluator = _Evaluator(root)
    rendered = []
    try:
        for name, node in _Parser(body).parse():
            value = evaluator.evaluate(node)
            if name is not None:
                evaluator.variables[name] = value
            elif isinstance(value, _Table) and value.formatted:
                rendered.append(_render(value))
            else:
                raise ValueError("A visible chunk expression must produce a flextable")
    except RecursionError as exc:
        raise ValueError("R chunk nesting exceeds the static parser limit") from exc
    if not rendered:
        raise ValueError("R chunk contains no rendered table")
    return "\n\n".join(rendered)
