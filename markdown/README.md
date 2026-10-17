# 计算机体系结构基础（第三版）· Markdown 版

胡伟武、汪文祥、苏孟豪、张福新、王焕东、章隆兵、肖俊华、刘苏、陈新科、吴瑞阳、李晓钰、高燕萍著。

本目录由当前仓库的 R Markdown 源文件转换，包含全部序言、12 章正文、章内习题、总结、参考文献及相关资源。

## 阅读说明

直接打开下方章节即可阅读，不需要运行 R 或 Bookdown。公式采用 LaTeX 数学语法，需使用支持数学公式的 Markdown 阅读器（例如 GitHub 或启用数学渲染的编辑器）。

图片以相对路径复用仓库中的 `../images/` 和 `../materials/`，请保留仓库目录结构。表格数据和代码对照已展开；简单表格使用 Markdown 管道表格，复杂表头与代码对照使用内嵌 HTML，原表合并单元格以重复值展开以保留分组关系。打印版的列宽、颜色和分页不在 Markdown 中复现，双栏代码按原顺序排列。章节、图表编号按当前开源源码的顺序生成，可能与纸质版不同。

## 全书目录

### 书前内容

- [丛书序言](index.md)
- [推荐序](01-foreword-recommend.md)
- [自序](02-foreword-author.md)
- [第三版序](03-foreword-3rd.md)
- [前言](04-preface.md)
- [关于本书的在线版本](05-online-version.md)

### 第1部分　引言

- [第1章　引言](11-introduction.md)

### 第2部分　指令系统结构

- [第2章　指令系统](12-isa.md)
- [第3章　特权指令系统](13-privileged-isa.md)
- [第4章　软硬件协同](14-software-hardware-coop.md)

### 第3部分　计算机硬件结构

- [第5章　计算机组成原理和结构](15-organization.md)
- [第6章　计算机总线接口技术](16-bus.md)
- [第7章　计算机启动过程分析](17-boot.md)

### 第4部分　CPU的微结构

- [第8章　运算器设计](18-microarch.md)
- [第9章　指令流水线](19-pipeline.md)

### 第5部分　并行处理结构

- [第10章　并行编程基础](20-parallel-programming.md)
- [第11章　多核处理结构](21-multicore.md)

### 第6部分　系统性能评价与分析

- [第12章　计算机系统性能评价与性能分析](22-perf-evaluation.md)

### 书后内容

- [总结](30-conclusion.md)
- [参考文献](40-references.md)
- [相关资源](50-resources.md)

## 更新 Markdown

需要 Python 3.10+ 与 Pandoc（含 citeproc；本次使用 Pandoc 2.17.0.1）。在仓库根目录运行：

```sh
python3 markdown/convert.py
```

只检查已生成文件是否与源码一致，并验证图片、文献及交叉引用：

```sh
python3 markdown/convert.py --check
```

正文以根目录 `.Rmd`、`book.bib` 和 `materials/` 数据为准；重新生成会覆盖本目录的章节 Markdown 与目录。脚本遇到未支持的 R 内容或失效引用会报错，避免静默遗漏。不同 Pandoc 版本可能产生排版差异。

## 来源与许可

来源：[仓库说明](../README.md) · [在线教材](https://foxsen.github.io/archbase/)。本次仅进行格式转换；教材内容采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)，详见仓库 [LICENSE](../LICENSE)。
