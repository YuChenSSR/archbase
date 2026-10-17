#!/usr/bin/env python3
"""Export this book's R Markdown sources without requiring an R installation."""

from __future__ import annotations

import argparse
import ast
import collections
import copy
import html
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit

sys.dont_write_bytecode = True
from convert_tables import render_table

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "markdown"
CHUNK = re.compile(r"^```\{r\s+([^\n}]*)\}\s*\n(.*?)^```[ \t]*$", re.M | re.S)
STRING = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
REF = re.compile(r"\\@ref(?:\(([^)]+)\)|\{([^}]+)\})")
READER = "markdown-smart-markdown_in_html_blocks"
WRITER = "gfm+tex_math_dollars"


def pandoc(text: str, *args: str) -> str:
    result = subprocess.run(
        ["pandoc", *args, "--wrap=none"], input=text, text=True,
        encoding="utf-8", capture_output=True, cwd=ROOT,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    if result.stderr.strip():
        raise RuntimeError(f"Pandoc reported a warning: {result.stderr.strip()}")
    return result.stdout


def literal(value: str) -> str:
    return ast.literal_eval(value)


def option(text: str, name: str) -> str | None:
    match = re.search(r"(?<![\w.])" + re.escape(name) + r"\s*=\s*(" + STRING + ")", text)
    return literal(match[1]) if match else None


def plain(node) -> str:
    if isinstance(node, list):
        return "".join(plain(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind, content = node.get("t"), node.get("c")
    if kind == "Str":
        return content
    if kind in {"Space", "SoftBreak", "LineBreak"}:
        return " "
    if kind in {"Code", "Math"}:
        return content[1]
    if kind in {"Link", "Image", "Span"}:
        return plain(content[1])
    return plain(content)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def anchor(identifier: str) -> dict:
    return {"t": "RawBlock", "c": ["html", f'<a id="{html.escape(identifier, quote=True)}"></a>']}


def inline_text(text: str) -> list:
    return [{"t": "Str", "c": text}]


def expand_source(source: Path, chapter: int | None, refs: dict, inventory: dict) -> str:
    text = source.read_text(encoding="utf-8")
    if source.name == "index.Rmd":
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    text = re.sub(r"^```\{=latex\}\s*\n.*?^```\s*$", "", text, flags=re.M | re.S)
    text = re.sub(r"^\\(?:newpage|mainmatter|frontmatter|backmatter)(?:\{\})?\s*$", "", text, flags=re.M)
    text = re.sub(r"^\\markboth\{[^}]*\}\{[^}]*\}\s*$", "", text, flags=re.M)
    counts = collections.Counter()
    filename = source.with_suffix(".md").name

    def chunk(match):
        options, body = match[1], match[2]
        label = options.split(",", 1)[0].strip()
        inventory["chunks"] += 1
        if label == "setup" and source.name == "index.Rmd":
            inventory["setup_chunks"] += 1
            return ""
        paths = re.findall(r"include_graphics\(\s*(" + STRING + r")\s*\)", body)
        if paths:
            kind = "fig"
            caption = option(options, "fig.cap")
            content = []
            for path_literal in paths:
                path = Path(literal(path_literal))
                if not (ROOT / path).is_file():
                    raise ValueError(f"Missing image: {path}")
                inventory["images"].append(path.as_posix())
                alt = (caption or path.stem).replace("[", r"\[").replace("]", r"\]")
                content.append(f"![{alt}]({quote('../' + path.as_posix(), safe='/')})")
            rendered = "\n\n".join(content)
        else:
            kind = "tab"
            caption = option(body, "caption") or option(options, "tab.cap")
            rendered = render_table(body, ROOT)
            # Simple tables remain editable as pipe tables; code layouts and
            # grouped headers retain HTML so their structure survives export.
            header = re.search(r"<thead>(.*?)</thead>", rendered, re.S)
            if header and header[1].count("<tr>") == 1 and "<pre>" not in rendered and "原表的合并单元格" not in rendered:
                rendered = pandoc(rendered, "-f", "html", "-t", WRITER).strip()
            inventory["tables"] += 1
        identifier = f"{kind}-{label}"
        if caption:
            counts[kind] += 1
            number = f"{chapter}.{counts[kind]}" if chapter else str(counts[kind])
            title = f"{'图' if kind == 'fig' else '表'} {number}　{caption}"
        else:
            number = label
            title = ""
        key = f"{kind}:{label}"
        if key in refs:
            raise ValueError(f"Duplicate reference: {key}")
        refs[key] = (filename, identifier, number)
        inventory["objects"].append({"source": source.name, "kind": kind, "label": label, "caption": caption})
        parts = [f'<a id="{identifier}"></a>']
        if kind == "tab" and title:
            parts.append(f"**{title}**")
        parts.append(rendered)
        if kind == "fig" and title:
            parts.append(f"**{title}**")
        return "\n\n".join(parts)

    text = CHUNK.sub(chunk, text)
    if re.search(r"^```\{r\b", text, re.M):
        raise ValueError(f"Unconverted R chunk in {source.name}")
    # Bookdown accepts parentheses; one existing source uses braces instead.
    text = REF.sub(lambda m: f"[引用](bookref:{quote(m[1] or m[2], safe=':')})", text)
    text = text.replace('<div id="refs"></div>', '::: {#refs}\n:::')
    return text


def prepare_chapter(source, chapter, refs, inventory, part_number):
    text = expand_source(source, chapter, refs, inventory)
    document = json.loads(pandoc(text, "-f", READER, "-t", "json"))
    inventory.setdefault("preservation", {})[source.with_suffix(".md").name] = {
        kind: collections.Counter(
            json.dumps(node["c"] if kind == "Math" else node["c"][1].rstrip("\n"), ensure_ascii=False)
            for node in nodes(document) if node.get("t") == kind
            and (kind != "CodeBlock" or node["c"][1].strip())
        ) for kind in ("Math", "CodeBlock", "Code")
    }
    filename = source.with_suffix(".md").name
    counts = [chapter or 0, 0, 0, 0, 0, 0]
    headings = []
    blocks = []
    title = ""
    part = None
    for block in document["blocks"]:
        if block["t"] != "Header":
            blocks.append(block)
            continue
        level, attrs, inlines = block["c"]
        name = plain(inlines)
        if name.startswith("(PART)"):
            part_number += 1
            part = f"第{part_number}部分　{name.removeprefix('(PART)').strip()}"
            blocks.append({"t": "Para", "c": [{"t": "Strong", "c": inline_text(part)}]})
            continue
        old_id = attrs[0]
        if level == 1:
            title = f"第{chapter}章　{name}" if chapter else name
            number = str(chapter) if chapter else name
            display = title
        else:
            counts[level - 1] += 1
            counts[level:] = [0] * (len(counts) - level)
            number = ".".join(map(str, counts[:level])) if chapter else name
            display = f"{number} {name}" if chapter else name
        identifier = old_id if old_id.startswith("sec-") or old_id == "references" else (
            "section-" + "-".join(map(str, counts[:level])) if chapter
            else f"{source.stem}-section-{len(headings) + 1}"
        )
        if old_id.startswith("sec-") or old_id == "references":
            if old_id in refs:
                raise ValueError(f"Duplicate section reference: {old_id}")
            refs[old_id] = (filename, identifier, number)
        headings.append((level, identifier, display))
        block["c"] = [level, [identifier, [], []], inline_text(display)]
        blocks.append(block)
    if not title:
        raise ValueError(f"Missing chapter title in {source.name}")
    document["blocks"] = blocks
    return {"source": source, "filename": filename, "title": title, "part": part,
            "headings": headings, "document": document}, part_number


def flatten(blocks: list) -> list:
    result = []
    for block in blocks:
        if block["t"] == "Header":
            result.append(anchor(block["c"][1][0]))
            block["c"][1] = ["", [], []]
            result.append(block)
        elif block["t"] == "Div":
            attrs, children = block["c"]
            identifier, classes, _ = attrs
            if identifier:
                result.append(anchor(identifier))
            if "col" in classes:
                if "width4" in classes:
                    if any(n.get("t") == "CodeBlock" and n["c"][1].strip() for n in nodes(children)):
                        raise ValueError("Unexpected content in a column spacer")
                    continue
                result.append({"t": "Para", "c": [{"t": "Strong", "c": inline_text("代码对照（按原文从左到右排列）")}]})
            result.extend(flatten(children))
        else:
            result.append(block)
    return result


def clean_inlines(value):
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict) and item.get("t") in {"Span", "Cite"}:
                content = item["c"][1]
                result.extend(clean_inlines(content))
            else:
                result.append(clean_inlines(item))
        return result
    if isinstance(value, dict):
        return {key: clean_inlines(item) for key, item in value.items()}
    return value


def validate(outputs: dict[str, str], inventory: dict):
    identifiers = {}
    parsed = {}
    for filename, text in outputs.items():
        ids = re.findall(r'<a id="([^"]+)"', text)
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate anchors in {filename}")
        identifiers[filename] = set(ids)
        parsed[filename] = json.loads(pandoc(text, "-f", WRITER, "-t", "json"))
        for kind, expected in inventory.get("preservation", {}).get(filename, {}).items():
            actual = collections.Counter(
                json.dumps(node["c"] if kind == "Math" else node["c"][1].rstrip("\n"), ensure_ascii=False)
                for node in nodes(parsed[filename]) if node.get("t") == kind
                and (kind != "CodeBlock" or node["c"][1].strip())
            )
            if expected != actual:
                raise ValueError(f"{kind} changed during conversion in {filename}: {expected - actual}")
        if re.search(r"bookref:|\\@ref|^```\{r|^:::\s*\{|\{#sec-", text, re.M):
            raise ValueError(f"Unconverted source syntax in {filename}")
    images = 0
    for filename, document in parsed.items():
        for node in nodes(document):
            if node.get("t") not in {"Link", "Image"}:
                continue
            target = node["c"][2][0]
            link = urlsplit(target)
            if link.scheme or link.netloc:
                continue
            path = unquote(link.path)
            fragment = unquote(link.fragment)
            if node["t"] == "Image":
                images += 1
            if not path:
                name = filename
            elif path in outputs:
                name = path
            else:
                if not (OUT / path).exists():
                    raise ValueError(f"Broken local link in {filename}: {target}")
                continue
            if fragment and fragment not in identifiers[name]:
                raise ValueError(f"Broken anchor in {filename}: {target}")
    if images != len(inventory["images"]):
        raise ValueError(f"Image count mismatch: {images} != {len(inventory['images'])}")
    return {"documents": len(outputs) - 1, "images": images, "tables": inventory["tables"],
            "r_chunks": inventory["chunks"], "bibliography_entries": len(identifiers["40-references.md"] - {"references", "refs"})}


def build() -> tuple[dict, dict]:
    sources = [ROOT / "index.Rmd", *sorted(ROOT.glob("[0-9]*.Rmd"))]
    refs = {}
    inventory = {"chunks": 0, "setup_chunks": 0, "tables": 0, "images": [], "objects": []}
    chapters = []
    part_number = 0
    for source in sources:
        prefix = source.stem.split("-", 1)[0]
        chapter = int(prefix) - 10 if prefix.isdigit() and 11 <= int(prefix) <= 22 else None
        item, part_number = prepare_chapter(source, chapter, refs, inventory, part_number)
        chapters.append(item)
    combined = copy.deepcopy(chapters[0]["document"])
    combined["meta"] = {
        "bibliography": {"t": "MetaString", "c": "book.bib"},
        "csl": {"t": "MetaString", "c": "chinese-gb7714-2005-numeric.csl"},
        "link-citations": {"t": "MetaBool", "c": True},
        "nocite": {"t": "MetaInlines", "c": [{"t": "Cite", "c": [[{
            "citationId": "*", "citationPrefix": [], "citationSuffix": [],
            "citationMode": {"t": "NormalCitation"}, "citationNoteNum": 0, "citationHash": 0,
        }], inline_text("@*")]}]},
    }
    combined["blocks"] = []
    for item in chapters:
        combined["blocks"].append({"t": "RawBlock", "c": ["html", f"<!-- chapter:{item['filename']} -->"]})
        combined["blocks"].extend(item["document"]["blocks"])
    resolved = json.loads(pandoc(json.dumps(combined, ensure_ascii=False), "-f", "json", "-t", "json", "--citeproc"))
    grouped = {}
    current = None
    for block in resolved["blocks"]:
        if block["t"] == "RawBlock" and block["c"][1].startswith("<!-- chapter:"):
            current = block["c"][1][13:-4].strip()
            grouped[current] = []
        else:
            if current is None:
                raise ValueError("Content before first chapter")
            grouped[current].append(block)
    outputs = {}
    for index, item in enumerate(chapters):
        filename = item["filename"]
        blocks = grouped[filename]
        for node in nodes(blocks):
            if node.get("t") == "CodeBlock" and not node["c"][0][1]:
                node["c"][0][1] = ["text"]
            if node.get("t") != "Link":
                continue
            target = node["c"][2][0]
            if target.startswith("bookref:"):
                key = unquote(target.removeprefix("bookref:"))
                # The source uses an underscore once for the NMOS-work figure.
                key = {"fig:NMOS_work": "fig:NMOS-work"}.get(key, key)
                if key not in refs:
                    raise ValueError(f"Unknown cross-reference in {filename}: {key}")
                destination, identifier, number = refs[key]
                node["c"][1] = inline_text(number)
                node["c"][2][0] = ("" if destination == filename else destination) + "#" + identifier
            elif target.startswith("#ref-"):
                node["c"][2][0] = "40-references.md" + target
        doc = {"pandoc-api-version": resolved["pandoc-api-version"], "meta": {},
               "blocks": clean_inlines(flatten(blocks))}
        body = pandoc(json.dumps(doc, ensure_ascii=False), "-f", "json", "-t", WRITER)
        toc = "\n".join("  " * (level - 2) + f"- [{title}](#{identifier})"
                        for level, identifier, title in item["headings"] if 2 <= level <= 3)
        if toc:
            body = re.sub(r"(^# .*$)", lambda m: m[0] + "\n\n**本章目录**\n\n" + toc, body, count=1, flags=re.M)
        nav = ["[全书目录](README.md)"]
        if index:
            nav.append(f"[上一章：{chapters[index - 1]['title']}]({chapters[index - 1]['filename']})")
        if index + 1 < len(chapters):
            nav.append(f"[下一章：{chapters[index + 1]['title']}]({chapters[index + 1]['filename']})")
        navigation = " · ".join(nav)
        outputs[filename] = f"{navigation}\n\n{body.rstrip()}\n\n---\n\n{navigation}\n"
    readme = ["# 计算机体系结构基础（第三版）· Markdown 版", "",
              "胡伟武、汪文祥、苏孟豪、张福新、王焕东、章隆兵、肖俊华、刘苏、陈新科、吴瑞阳、李晓钰、高燕萍著。", "",
              "本目录由当前仓库的 R Markdown 源文件转换，包含全部序言、12 章正文、章内习题、总结、参考文献及相关资源。", "",
              "## 阅读说明", "",
              "直接打开下方章节即可阅读，不需要运行 R 或 Bookdown。公式采用 LaTeX 数学语法，需使用支持数学公式的 Markdown 阅读器（例如 GitHub 或启用数学渲染的编辑器）。", "",
              "图片以相对路径复用仓库中的 `../images/` 和 `../materials/`，请保留仓库目录结构。表格数据和代码对照已展开；简单表格使用 Markdown 管道表格，复杂表头与代码对照使用内嵌 HTML，原表合并单元格以重复值展开以保留分组关系。打印版的列宽、颜色和分页不在 Markdown 中复现，双栏代码按原顺序排列。章节、图表编号按当前开源源码的顺序生成，可能与纸质版不同。", "",
              "## 全书目录", "", "### 书前内容", ""]
    for item in chapters:
        if item["part"]:
            readme.extend(["", f"### {item['part']}", ""])
        if item["source"].name == "30-conclusion.Rmd":
            readme.extend(["", "### 书后内容", ""])
        readme.append(f"- [{item['title']}]({item['filename']})")
    readme.extend(["", "## 更新 Markdown", "",
                   "需要 Python 3.10+ 与 Pandoc（含 citeproc；本次使用 Pandoc 2.17.0.1）。在仓库根目录运行：", "",
                   "```sh", "python3 markdown/convert.py", "```", "",
                   "只检查已生成文件是否与源码一致，并验证图片、文献及交叉引用：", "",
                   "```sh", "python3 markdown/convert.py --check", "```", "",
                   "正文以根目录 `.Rmd`、`book.bib` 和 `materials/` 数据为准；重新生成会覆盖本目录的章节 Markdown 与目录。脚本遇到未支持的 R 内容或失效引用会报错，避免静默遗漏。不同 Pandoc 版本可能产生排版差异。", "",
                   "## 来源与许可", "",
                   "来源：[仓库说明](../README.md) · [在线教材](https://foxsen.github.io/archbase/)。本次仅进行格式转换；教材内容采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)，详见仓库 [LICENSE](../LICENSE)。", ""])
    outputs["README.md"] = "\n".join(readme)
    stats = validate(outputs, inventory)
    return outputs, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate sources and compare with generated Markdown")
    args = parser.parse_args()
    outputs, stats = build()
    if args.check:
        stale = [name for name, text in outputs.items()
                 if not (OUT / name).exists() or (OUT / name).read_text(encoding="utf-8") != text]
        if stale:
            raise ValueError("Outdated Markdown: " + ", ".join(stale))
    else:
        OUT.mkdir(exist_ok=True)
        for filename, text in outputs.items():
            (OUT / filename).write_text(text, encoding="utf-8")
    print(json.dumps({"status": "checked" if args.check else "generated", **stats}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        sys.exit(str(error))
