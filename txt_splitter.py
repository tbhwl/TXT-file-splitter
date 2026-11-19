# -*- coding: utf-8 -*-
"""
TXT 分割 / 合并工具  v2.0

功能：
  - 按行平均分割，可选择「智能断点」（切点优先落在章节标题 / 分隔线 / 空行处）
  - 两种命名：原名+序号  或  自定义基础名+序号（支持 {n} {name} {total}）
  - 单文件 / 批量分割，自动校验（SHA256 + 行数，失败红字报警）
  - 每份去首尾空行 / 每份末尾补空行
  - 导出：仅文件夹 / 文件夹+zip / 仅zip
  - 多文件合并成一个 txt
  - 配置记忆（上次目录、份数、选项）
  - 可选拖拽（需 pip install tkinterdnd2，未安装自动降级）

仅使用标准库；tkinterdnd2 为可选依赖。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP_NAME = "TXT 分割 / 合并工具"
VERSION = "2.0"
CONFIG_NAME = "config.json"
REPORT_NAME = "split_report.csv"
TXT_SUFFIX = ".txt"
BIG_FILE_BYTES = 200 * 1024 * 1024      # 超过此大小在界面上提示确认
PREVIEW_MAX_BYTES = 20 * 1024 * 1024    # 超过此大小不做行级预览

ENCODING_CHOICES = [
    "utf-8", "utf-8-sig", "gb18030", "gbk",
    "utf-16", "utf-16-le", "utf-16-be", "big5", "cp936",
]

BOM_UTF8 = b"\xef\xbb\xbf"
BOM_UTF16_LE = b"\xff\xfe"
BOM_UTF16_BE = b"\xfe\xff"

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 章节标题：第1章 / 第十二节 / 第二卷 / 第三回 / 第一篇 / 第五幕 / 第十话；Markdown # / ##
CHAPTER_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千零两〇]+\s*[章节卷回篇部集幕话]"
    r"|#{1,6}\s"
    r"|(?i:chapter|part|section)\s+[0-9ivxlcdm]+)",
)
SEPARATOR_RE = re.compile(r"^\s*([=\-*~—_])\1{2,}\s*$")


# ============================================================================
# 通用小工具
# ============================================================================

def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def human_size(num: float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{int(num)}B" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}GB"


def natural_key(text: str):
    """自然排序：_2 排在 _10 前面。"""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


def part_index_key(text: str):
    """按分片序号还原：优先文件名末尾的数字。"""
    m = re.search(r"(\d+)(?=\D*$)", text)
    return (int(m.group(1)) if m else 10 ** 9, natural_key(text))


def sanitize_filename(name: str, fallback: str = "part") -> str:
    name = INVALID_FILENAME_CHARS.sub("_", name)
    name = name.strip().rstrip(" .")
    if not name:
        return fallback
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def unique_path(path: Path) -> Path:
    """已存在则追加 (1)(2)…，绝不覆盖。"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def strip_txt_suffix(name: str) -> str:
    if name.lower().endswith(TXT_SUFFIX):
        return name[: -len(TXT_SUFFIX)]
    return name


def list_txt_files(folder, recursive: bool = False) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    it = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(
        (p for p in it if p.is_file() and p.suffix.lower() == TXT_SUFFIX),
        key=lambda p: natural_key(p.name),
    )


# ============================================================================
# 编码识别 / 读写
# ============================================================================

class DetectError(Exception):
    def __init__(self, message: str, data: bytes = b""):
        super().__init__(message)
        self.data = data


def _guess_utf16(data: bytes):
    """无 BOM 的 UTF-16 猜测：ASCII 文本会产生大量隔位 0x00。"""
    sample = data[:8192]
    if len(sample) < 8:
        return None
    even = sample[0::2].count(0)
    odd = sample[1::2].count(0)
    half = max(1, len(sample) // 2)
    if odd / half > 0.35 and even / half < 0.10:
        return "utf-16-le"
    if even / half > 0.35 and odd / half < 0.10:
        return "utf-16-be"
    return None


def detect_encoding(data: bytes) -> str:
    if not data:
        return "utf-8"
    if data.startswith(BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(BOM_UTF16_LE) or data.startswith(BOM_UTF16_BE):
        return "utf-16"
    u16 = _guess_utf16(data)
    if u16:
        try:
            data.decode(u16)
            return u16
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    raise DetectError("无法自动识别文件编码，请手动选择", data)


def decode_bytes(data: bytes, encoding: str) -> str:
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DetectError(f"按 {encoding} 解码失败：{exc}", data) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def read_document(path, encoding: str | None = None):
    """返回 (text, encoding)。encoding 为 None 时自动识别。"""
    data = Path(path).read_bytes()
    enc = encoding or detect_encoding(data)
    return decode_bytes(data, enc), enc


def encode_text(text: str, encoding: str) -> bytes:
    return text.encode(encoding)


# ============================================================================
# 行处理
# ============================================================================

def split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def is_blank(line: str) -> bool:
    return line.strip() == ""


def detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def trim_boundary_blank_lines(lines: list[str]) -> list[str]:
    """去掉开头和结尾的纯空白行，中间不动。"""
    start, end = 0, len(lines)
    while start < end and is_blank(lines[start]):
        start += 1
    while end > start and is_blank(lines[end - 1]):
        end -= 1
    return lines[start:end]


def ensure_blank_end(text: str, newline: str):
    """保证文本以空行结束，返回 (新文本, 新增换行数)。"""
    added = 0
    if not text:
        return newline, 1
    if not text.endswith(("\n", "\r")):
        text += newline
        added += 1
    guard = 0
    while not text.endswith(newline + newline) and guard < 4:
        text += newline
        added += 1
        guard += 1
    return text, added


def unify_newline(text: str, mode: str) -> str:
    if mode == "keep":
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "crlf":
        t = t.replace("\n", "\r\n")
    return t


def first_diff_line(expected: str, actual: str) -> int:
    """返回从 1 开始的第一处差异行号；无差异返回 0。"""
    if expected == actual:
        return 0
    e = expected.splitlines(keepends=True)
    a = actual.splitlines(keepends=True)
    for i in range(min(len(e), len(a))):
        if e[i] != a[i]:
            return i + 1
    return min(len(e), len(a)) + 1


# ============================================================================
# 智能断点
# ============================================================================

def breakpoint_weight(lines: list[str], cut: int):
    """cut 表示「第 cut 行起归下一份」。返回权重，None 表示不是候选。"""
    if cut <= 0 or cut >= len(lines):
        return None
    if CHAPTER_RE.match(lines[cut]):
        return 100
    if SEPARATOR_RE.match(lines[cut]):
        return 60
    if is_blank(lines[cut - 1]):
        return 20
    return None


def _window(n: int, k: int) -> int:
    chunk = n / k
    if chunk < 3:
        return 0
    w = int(min(200, max(3, int(chunk / 10))))
    if 2 * w >= chunk:
        w = max(1, int(chunk / 2) - 1)
    return w


def plan_cuts(lines: list[str], k: int, smart: bool = True) -> list[tuple[int, int]]:
    """返回 [(start, end), ...]，0 基、end 不含。"""
    n = len(lines)
    if n == 0:
        return []
    k = max(1, min(int(k), n))
    if k == 1:
        return [(0, n)]

    w = _window(n, k) if smart else 0
    cuts: list[tuple[int, int]] = []
    prev = 0
    for i in range(1, k):
        # 向上取整：保证余数落在最前面的几份（100 行分 3 份 → 34/33/33）
        target = -(-i * n // k)
        cut = target
        if w:
            lo = max(prev + 1, target - w)
            hi = min(n - (k - i), target + w)
            best = None
            best_key = None
            for cand in range(lo, hi + 1):
                weight = breakpoint_weight(lines, cand)
                if weight is None:
                    continue
                score = weight - abs(cand - target)
                key = (score, -abs(cand - target), -cand)
                if best_key is None or key > best_key:
                    best_key = key
                    best = cand
            if best is not None:
                cut = best
        cut = max(cut, prev + 1)
        cut = min(cut, n - (k - i))
        cuts.append((prev, cut))
        prev = cut
    cuts.append((prev, n))
    return cuts


def plan_distribution(lines: list[str], k: int, smart: bool = True) -> list[int]:
    return [end - start for start, end in plan_cuts(lines, k, smart)]


# ============================================================================
# 命名
# ============================================================================

def pad_width(k: int) -> int:
    return max(1, len(str(k)))


def build_part_names(stem: str, k: int, mode: str = "suffix",
                    custom: str = "", multi: bool = False) -> list[str]:
    """生成 k 个输出文件名（不含目录）。"""
    width = pad_width(k)
    stem = stem or "txt"
    names: list[str] = []
    for i in range(1, k + 1):
        num = f"{i:0{width}d}"
        if mode == "custom" and custom.strip():
            base = custom.strip()
            if multi and "{name}" not in base:
                base = f"{stem}_{base}"
            if "{n}" in base or "{name}" in base or "{total}" in base:
                text = (base.replace("{n}", num)
                            .replace("{name}", stem)
                            .replace("{total}", str(k)))
            else:
                text = f"{base}_{num}"
            text = strip_txt_suffix(text)
            names.append(sanitize_filename(text, "part") + TXT_SUFFIX)
        else:
            names.append(sanitize_filename(f"{stem}_{num}", "part") + TXT_SUFFIX)
    return names


# ============================================================================
# 分割
# ============================================================================

@dataclass
class SplitOptions:
    parts: int = 2
    naming: str = "suffix"          # suffix | custom
    custom_name: str = ""
    smart: bool = True
    verify: bool = True
    trim_blank: bool = False
    add_blank: bool = False
    zip_mode: str = "folder"        # folder | folder+zip | zip
    subfolder: bool = False


@dataclass
class PartResult:
    index: int
    name: str
    path: str
    start_line: int
    end_line: int
    lines: int
    chars: int
    sha: str
    ok: bool = True
    note: str = ""


@dataclass
class FileResult:
    source: str
    ok: bool = True
    skipped: bool = False
    message: str = ""
    encoding: str = ""
    parts: list[PartResult] = field(default_factory=list)
    verify_ok: bool = True
    verify_msg: str = ""
    removed_blank: int = 0
    added_blank: int = 0
    elapsed: float = 0.0


def format_part_text(part_lines: list[str], trim_blank: bool,
                     add_blank: bool, newline: str):
    """返回 (输出文本, 删除空行数, 新增空行数)。写盘和校验共用，保证一致。"""
    removed = 0
    if trim_blank:
        trimmed = trim_boundary_blank_lines(part_lines)
        removed = len(part_lines) - len(trimmed)
        part_lines = trimmed
    text = "".join(part_lines)
    added = 0
    if add_blank:
        text, added = ensure_blank_end(text, newline)
    return text, removed, added


def verify_parts(lines: list[str], text: str, entries, opts: SplitOptions,
                 encoding: str, newline: str):
    """读回磁盘内容做独立比对。返回 (ok, message)。"""
    disk_texts: list[str] = []
    bad_part = 0
    bad_line = 0
    for idx, (path, start, end) in enumerate(entries, start=1):
        try:
            disk_text, _ = read_document(path, encoding)
        except Exception:
            return False, f"第 {idx}/{len(entries)} 份读回失败"
        expected, _, _ = format_part_text(lines[start:end], opts.trim_blank,
                                          opts.add_blank, newline)
        if disk_text != expected:
            if not bad_part:
                bad_part = idx
                bad_line = first_diff_line(expected, disk_text)
        disk_texts.append(disk_text)

    if bad_part:
        return False, (f"第 {bad_part}/{len(entries)} 份内容不一致"
                       f"（起始于该份第 {bad_line} 行）")

    joined_source = "".join(lines)
    if opts.trim_blank or opts.add_blank:
        norm_source = "".join(trim_boundary_blank_lines(lines))
        norm_disk = "".join(
            "".join(trim_boundary_blank_lines(split_lines(t))) for t in disk_texts
        )
        if norm_source != norm_disk:
            return False, "拼接后的内容与原文不一致（已忽略首尾空行差异）"
    else:
        joined_disk = "".join(disk_texts)
        if joined_disk != joined_source:
            return False, "拼接后的内容与原文不一致"
    return True, ""


def split_one_file(src, opts: SplitOptions, out_dir, multi: bool = False,
                   emit=None, cancel=None) -> FileResult:
    """分割单个文件，分片写入 out_dir。"""
    started = time.time()
    src = Path(src)
    res = FileResult(source=str(src))
    try:
        text, encoding = read_document(src)
    except DetectError as exc:
        res.ok = False
        res.message = str(exc)
        return res
    except OSError as exc:
        res.ok = False
        res.message = f"读取失败：{exc}"
        return res

    res.encoding = encoding
    lines = split_lines(text)
    n = len(lines)
    if n == 0:
        res.ok = False
        res.skipped = True
        res.message = "文件为空，已跳过"
        return res

    want = max(1, int(opts.parts))
    k = min(want, n)
    if k < want:
        res.message = f"行数不足，份数由 {want} 降为 {k}"
    cuts = plan_cuts(lines, k, smart=opts.smart and k > 1)
    names = build_part_names(src.stem, k, opts.naming, opts.custom_name, multi=multi)
    newline = detect_newline(text)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries: list[tuple[Path, int, int]] = []
    for idx, (start, end) in enumerate(cuts, start=1):
        if cancel is not None and cancel.is_set():
            res.ok = False
            res.message = "已取消"
            return res
        part_lines = lines[start:end]
        out_text, removed, added = format_part_text(
            part_lines, opts.trim_blank, opts.add_blank, newline)
        res.removed_blank += removed
        res.added_blank += added
        target = unique_path(out_dir / names[idx - 1])
        try:
            target.write_bytes(encode_text(out_text, encoding))
        except OSError as exc:
            res.ok = False
            res.message = f"写入失败：{target.name}（{exc}）"
            return res
        entries.append((target, start, end))
        res.parts.append(PartResult(
            index=idx,
            name=target.name,
            path=str(target),
            start_line=start + 1,
            end_line=end,
            lines=len(part_lines),
            chars=len(out_text),
            sha=hashlib.sha256(out_text.encode("utf-8")).hexdigest()[:16],
        ))
        if emit:
            emit(("part", idx, k, target.name))

    if opts.verify:
        ok, msg = verify_parts(lines, text, entries, opts, encoding, newline)
        res.verify_ok = ok
        res.verify_msg = msg
        if not ok:
            res.ok = False
            res.message = "自动校验未通过"

    for part in res.parts:
        part.ok = res.verify_ok
    res.elapsed = time.time() - started
    return res


# ============================================================================
# 报告 / 批量任务 / zip
# ============================================================================

def write_report(results: list[FileResult], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = unique_path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["源文件", "分片序号", "分片文件名", "起始行", "结束行",
                         "行数", "字符数", "SHA256前16位", "校验"])
        for res in results:
            if not res.parts:
                writer.writerow([res.source, "", "", "", "", "", "", "",
                                 res.message or "未产出"])
                continue
            for part in res.parts:
                writer.writerow([
                    res.source, part.index, part.name, part.start_line,
                    part.end_line, part.lines, part.chars, part.sha,
                    "通过" if res.verify_ok else "失败",
                ])
    return path


def _build_zip(zip_path: Path, entries: list[tuple[Path, str]]) -> Path:
    zip_path = unique_path(Path(zip_path))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in entries:
            zf.write(src, arcname)
    return zip_path


def run_split_job(files: list[str], opts: SplitOptions, dest_root,
                  emit=None, cancel=None) -> dict:
    """执行一批分割任务，返回汇总。emit 接收 ('file'|'log'|'part'|'file_done', ...)。"""
    started = time.time()
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    files = [Path(f) for f in files]
    multi = len(files) > 1
    zip_only = opts.zip_mode == "zip"
    staging = None
    if zip_only:
        staging = dest_root / f".txtsplit_tmp_{os.getpid()}_{now_stamp()}"
        staging.mkdir(parents=True, exist_ok=True)
    write_root = staging if zip_only else dest_root

    results: list[FileResult] = []
    zip_entries: list[tuple[Path, str]] = []

    try:
        for pos, src in enumerate(files, start=1):
            if cancel is not None and cancel.is_set():
                break
            if emit:
                emit(("file", pos, len(files), src.name))
            use_sub = multi or opts.subfolder
            out_dir = write_root / f"{src.stem}_split" if use_sub else write_root
            try:
                res = split_one_file(src, opts, out_dir, multi=multi,
                                     emit=emit, cancel=cancel)
            except Exception as exc:  # 单文件失败不影响后续
                res = FileResult(source=str(src), ok=False,
                                 message=f"异常：{exc}")
            results.append(res)
            if emit:
                emit(("file_done", res))
            prefix = f"{src.stem}_split/" if multi else ""
            for part in res.parts:
                zip_entries.append((Path(part.path), prefix + Path(part.path).name))

        report_path = write_report(results, dest_root / REPORT_NAME)
        if emit:
            emit(("log", "info", f"报告已写入：{report_path.name}"))

        zip_path = None
        if opts.zip_mode != "folder" and zip_entries:
            try:
                if multi:
                    zip_name = f"批量分割_{now_stamp()}.zip"
                else:
                    zip_name = f"{files[0].stem}_split.zip" if files else "split.zip"
                zip_entries.append((report_path, REPORT_NAME))
                zip_path = _build_zip(dest_root / zip_name, zip_entries)
                if emit:
                    emit(("log", "info", f"zip 已生成：{zip_path.name}"))
            except Exception as exc:
                if emit:
                    emit(("log", "bad", f"zip 生成失败：{exc}"))
                zip_path = None
                if zip_only and staging is not None and staging.exists():
                    try:
                        backup = unique_path(dest_root / f"未打包分片_{now_stamp()}")
                        shutil.move(str(staging), str(backup))
                        if emit:
                            emit(("log", "warn", f"分片已保留到：{backup}"))
                    except OSError:
                        pass
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    ok_files = sum(1 for r in results if r.ok)
    fail_files = len(results) - ok_files
    total_parts = sum(len(r.parts) for r in results)
    return {
        "files": results,
        "ok": ok_files,
        "fail": fail_files,
        "parts": total_parts,
        "dest": str(dest_root),
        "zip": str(zip_path) if zip_path else "",
        "elapsed": time.time() - started,
        "cancelled": bool(cancel is not None and cancel.is_set()),
    }


# ============================================================================
# 合并
# ============================================================================

@dataclass
class MergeOptions:
    separator: str = "blank"     # none | blank | banner
    newline: str = "keep"        # keep | crlf | lf
    encoding: str = "utf-8"      # utf-8 | utf-8-sig | gb18030
    trim_blank: bool = False
    verify: bool = True


def build_merged_text(files, opts: MergeOptions):
    """返回 (文本, 说明列表)。files 为 Path 列表。"""
    docs = []
    for path in files:
        text, enc = read_document(path)
        docs.append((Path(path), text, enc))
    pieces: list[str] = []
    infos: list[str] = []
    for i, (path, text, enc) in enumerate(docs):
        if opts.trim_blank:
            text = "".join(trim_boundary_blank_lines(split_lines(text)))
        if opts.newline != "keep":
            text = unify_newline(text, opts.newline)
            nl = "\r\n" if opts.newline == "crlf" else "\n"
        else:
            nl = detect_newline(text)
        if text and not text.endswith(("\n", "\r")):
            text += nl
        if i > 0:
            prev_nl = nl
            if opts.separator == "blank":
                pieces.append(prev_nl)
            elif opts.separator == "banner":
                pieces.append(prev_nl)
                pieces.append(f"--- 来源: {path.name} ---{prev_nl}")
        pieces.append(text)
        infos.append(f"{path.name}（{enc}，{len(text.splitlines())} 行）")
    return "".join(pieces), infos


def run_merge_job(files, opts: MergeOptions, out_path, emit=None, cancel=None) -> dict:
    started = time.time()
    out_path = Path(out_path)
    result = {
        "ok": True, "message": "", "out": str(out_path),
        "lines": 0, "chars": 0, "sha": "", "sources": [],
        "elapsed": 0.0, "encode_error": "",
    }
    try:
        if emit:
            emit(("log", "info", f"合并 {len(files)} 个文件…"))
        text, infos = build_merged_text(files, opts)
        if cancel is not None and cancel.is_set():
            result.update(ok=False, message="已取消")
            return result
        result["sources"] = infos
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(text.encode(opts.encoding))
        if emit:
            emit(("log", "info", f"已写入 {out_path.name}"))
        if opts.verify:
            disk_text, _ = read_document(out_path, opts.encoding)
            if disk_text != text:
                result.update(ok=False, message="校验失败：写出内容与预期不一致")
            else:
                result.update(ok=True)
        result["lines"] = len(text.splitlines())
        result["chars"] = len(text)
        result["sha"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    except DetectError as exc:
        result.update(ok=False, message=str(exc))
    except OSError as exc:
        result.update(ok=False, message=f"读写失败：{exc}")
    result["elapsed"] = time.time() - started
    return result


# ============================================================================
# 配置
# ============================================================================

DEFAULT_CONFIG = {
    "parts": 2,
    "naming": "suffix",
    "custom_name": "",
    "smart": True,
    "verify": True,
    "trim_blank": False,
    "add_blank": False,
    "zip_mode": "folder",
    "subfolder": False,
    "include_sub": False,
    "last_dir": "",
    "out_dir": "",
    "window": "980x720+60+40",
    "merge": {
        "separator": "blank",
        "newline": "keep",
        "encoding": "utf-8",
        "trim_blank": False,
        "verify": True,
        "out_dir": "",
        "out_name": "",
    },
}


def config_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return [here / CONFIG_NAME, Path(appdata) / "TxtSplitter" / CONFIG_NAME]


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in config_candidates():
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
                break
        except (OSError, ValueError):
            continue
    return cfg


def save_config(cfg: dict) -> str:
    last_error = ""
    for path in config_candidates():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
            return str(path)
        except OSError as exc:
            last_error = str(exc)
    return f"保存配置失败：{last_error}"


# ============================================================================
# GUI
# ============================================================================

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _TK_OK = True
    _TK_ERROR = ""
except Exception as _exc:  # pragma: no cover - 无 tkinter 的环境
    _TK_OK = False
    _TK_ERROR = str(_exc)
    tk = None
    ttk = filedialog = messagebox = None

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:  # 可选依赖，未安装则自动降级
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

UI_FONT = "Microsoft YaHei UI"
COLOR_OK = "#1b7f3b"
COLOR_BAD = "#c0392b"
COLOR_WARN = "#b9770e"
COLOR_INFO = "#444444"
COLOR_MUTED = "#777777"

M_SEP_DISPLAY = {"无": "none", "补一个空行": "blank", "插入来源标记": "banner"}
M_SEP_REVERSE = {v: k for k, v in M_SEP_DISPLAY.items()}
M_NL_DISPLAY = {"保持原样": "keep", "统一 CRLF": "crlf", "统一 LF": "lf"}
M_NL_REVERSE = {v: k for k, v in M_NL_DISPLAY.items()}
M_ENC_DISPLAY = {"UTF-8": "utf-8", "UTF-8 BOM": "utf-8-sig", "GB18030": "gb18030"}
M_ENC_REVERSE = {v: k for k, v in M_ENC_DISPLAY.items()}


def _enable_dpi():
    """Windows 高分屏下让界面不发虚。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class TxtSplitterApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self._queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.active_mode = "split"
        self.last_dest = ""
        self.split_items: dict[str, dict] = {}
        self.split_by_path: dict[str, str] = {}
        self.merge_paths: list[Path] = []
        self._seq = 0

        self._init_vars()
        self._build_ui()
        self._apply_config()
        self._bind_traces()
        self._poll_queue()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.root.title(f"{APP_NAME}  v{VERSION}")
        try:
            theme = ttk.Style(self.root)
            if sys.platform == "win32":
                theme.theme_use("vista")
            theme.configure(".", font=(UI_FONT, 10))
            theme.configure("Treeview", rowheight=26)
            theme.configure("TButton", padding=(10, 5))
            theme.configure("TNotebook.Tab", padding=(18, 7))
        except Exception:
            pass

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)

        self.tab_split = ttk.Frame(self.notebook, padding=10)
        self.tab_merge = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_split, text=" 分割 ")
        self.notebook.add(self.tab_merge, text=" 合并 ")

        self._build_split_tab(self.tab_split)
        self._build_merge_tab(self.tab_merge)

        status = ttk.Frame(main)
        status.pack(fill="x", pady=(6, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status, textvariable=self.status_var,
                  foreground=COLOR_MUTED).pack(side="left")
        dnd_text = "拖拽可用" if HAS_DND else "拖拽未启用"
        ttk.Label(status, text=dnd_text,
                  foreground=COLOR_MUTED).pack(side="right")

    def _build_split_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=3)
        parent.rowconfigure(5, weight=2)

        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="源文件").pack(side="left")
        ttk.Button(bar, text="添加文件", command=self.pick_split_files).pack(side="left", padx=(8, 4))
        ttk.Button(bar, text="添加文件夹", command=self.pick_split_folder).pack(side="left", padx=4)
        recent = ttk.Menubutton(bar, text="最近")
        self.recent_menu = tk.Menu(recent, tearoff=0)
        recent.configure(menu=self.recent_menu)
        recent.pack(side="left", padx=4)
        ttk.Button(bar, text="移除", command=self.remove_split_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="清空", command=self.clear_split).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="含子目录",
                        variable=self.include_sub_var).pack(side="left", padx=(14, 4))

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        cols = ("sel", "name", "size", "lines", "enc", "status")
        self.split_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                       height=5, selectmode="extended")
        headers = {"sel": "选", "name": "文件名", "size": "大小",
                   "lines": "行数", "enc": "编码", "status": "状态"}
        widths = {"sel": 40, "name": 360, "size": 90, "lines": 90,
                  "enc": 90, "status": 110}
        for col in cols:
            self.split_tree.heading(col, text=headers[col])
            self.split_tree.column(col, width=widths[col],
                                   anchor="center" if col != "name" else "w",
                                   stretch=(col == "name"))
        self.split_tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.split_tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        self.split_tree.configure(yscrollcommand=ysb.set)
        self.split_tree.bind("<Button-1>", self._on_split_click)
        self.split_tree.bind("<<TreeviewSelect>>", self._on_split_select)

        self.path_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.path_var, foreground=COLOR_MUTED,
                  anchor="w").grid(row=2, column=0, sticky="ew", pady=(2, 4))

        settings = ttk.LabelFrame(parent, text="分割设置", padding=8)
        settings.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="分成几份").pack(side="left")
        ttk.Spinbox(row, from_=1, to=9999, width=6,
                    textvariable=self.parts_var).pack(side="left", padx=(6, 2))
        ttk.Label(row, text="份").pack(side="left")
        ttk.Label(row, text="    命名").pack(side="left")
        ttk.Radiobutton(row, text="原名+序号", value="suffix",
                        variable=self.naming_var).pack(side="left", padx=(6, 2))
        ttk.Radiobutton(row, text="自定义", value="custom",
                        variable=self.naming_var).pack(side="left", padx=(10, 2))
        self.custom_entry = ttk.Entry(row, width=26, textvariable=self.custom_var)
        self.custom_entry.pack(side="left")
        ttk.Label(row, text="（可用 {n} {name} {total}）",
                  foreground=COLOR_MUTED).pack(side="left", padx=(4, 0))

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="保存到  ").pack(side="left")
        ttk.Entry(row, textvariable=self.outdir_var).pack(
            side="left", fill="x", expand=True, padx=(4, 4))
        ttk.Button(row, text="选择…", command=self.browse_out_dir).pack(side="left")
        ttk.Checkbutton(row, text="放进子文件夹",
                        variable=self.subfolder_var).pack(side="left", padx=(10, 0))

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="选项  ").pack(side="left")
        ttk.Checkbutton(row, text="智能断点", variable=self.smart_var).pack(side="left", padx=(4, 8))
        ttk.Checkbutton(row, text="自动校验", variable=self.verify_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(row, text="去首尾空行", variable=self.trim_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(row, text="每份补空行", variable=self.addblank_var).pack(side="left")
        ttk.Label(row, text="输出").pack(side="left", padx=(16, 4))
        for text, val in (("仅文件夹", "folder"), ("文件夹+zip", "folder+zip"), ("仅zip", "zip")):
            ttk.Radiobutton(row, text=text, value=val,
                            variable=self.zipmode_var).pack(side="left", padx=(0, 6))

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        self.preview_var = tk.StringVar(value="")
        self.example_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.preview_var,
                  foreground="#20663f").pack(side="left")
        ttk.Label(row, textvariable=self.example_var,
                  foreground=COLOR_MUTED).pack(side="right")

        actions = ttk.Frame(parent)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 4))
        self.start_btn = ttk.Button(actions, text="开始分割", command=self.start_split)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text="取消", command=self.cancel_job,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="打开输出文件夹",
                   command=self.open_out_dir).pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="determinate", length=240)
        self.progress.pack(side="left", padx=(14, 8))
        self.progress_lbl = ttk.Label(actions, text="就绪", foreground=COLOR_MUTED)
        self.progress_lbl.pack(side="left")

        result_box = ttk.LabelFrame(parent, text="结果", padding=4)
        result_box.grid(row=5, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        self.result_text = tk.Text(result_box, height=6, wrap="word",
                                   state="disabled", relief="flat",
                                   font=(UI_FONT, 9))
        self.result_text.grid(row=0, column=0, sticky="nsew")
        rsb = ttk.Scrollbar(result_box, orient="vertical",
                            command=self.result_text.yview)
        rsb.grid(row=0, column=1, sticky="ns")
        self.result_text.configure(yscrollcommand=rsb.set)
        self._configure_tags(self.result_text)

        self._setup_dnd(self.split_tree, self._on_drop_split)
        self._setup_dnd(self.result_text, self._on_drop_split)

    def _build_merge_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=3)
        parent.rowconfigure(5, weight=2)

        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="待合并文件（从上到下顺序拼接）").pack(side="left")
        ttk.Button(bar, text="添加文件", command=self.pick_merge_files).pack(side="left", padx=(8, 4))
        ttk.Button(bar, text="添加文件夹", command=self.pick_merge_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="移除", command=self.remove_merge_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="清空", command=self.clear_merge).pack(side="left", padx=4)

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        cols = ("idx", "name", "size")
        self.merge_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                       height=5, selectmode="extended")
        headers = {"idx": "序号", "name": "文件名", "size": "大小"}
        widths = {"idx": 60, "name": 520, "size": 100}
        for col in cols:
            self.merge_tree.heading(col, text=headers[col])
            self.merge_tree.column(col, width=widths[col],
                                   anchor="w" if col == "name" else "center",
                                   stretch=(col == "name"))
        self.merge_tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.merge_tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        self.merge_tree.configure(yscrollcommand=ysb.set)
        self.merge_tree.bind("<<TreeviewSelect>>", self._on_merge_select)

        order_bar = ttk.Frame(parent)
        order_bar.grid(row=2, column=0, sticky="ew", pady=4)
        ttk.Button(order_bar, text="上移", command=lambda: self.move_merge(-1)).pack(side="left")
        ttk.Button(order_bar, text="下移", command=lambda: self.move_merge(1)).pack(side="left", padx=4)
        ttk.Button(order_bar, text="按文件名自然排序",
                   command=self.sort_merge_natural).pack(side="left", padx=(14, 4))
        ttk.Button(order_bar, text="按分片序号还原",
                   command=self.sort_merge_parts).pack(side="left", padx=4)
        self.merge_path_var = tk.StringVar(value="")
        ttk.Label(order_bar, textvariable=self.merge_path_var,
                  foreground=COLOR_MUTED).pack(side="right")

        settings = ttk.LabelFrame(parent, text="合并设置", padding=8)
        settings.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="文件之间").pack(side="left")
        ttk.Combobox(row, width=12, state="readonly",
                     values=list(M_SEP_DISPLAY),
                     textvariable=self.m_sep_var).pack(side="left", padx=(6, 14))
        ttk.Label(row, text="换行符").pack(side="left")
        ttk.Combobox(row, width=10, state="readonly",
                     values=list(M_NL_DISPLAY),
                     textvariable=self.m_nl_var).pack(side="left", padx=(6, 14))
        ttk.Label(row, text="输出编码").pack(side="left")
        ttk.Combobox(row, width=9, state="readonly",
                     values=list(M_ENC_DISPLAY),
                     textvariable=self.m_enc_var).pack(side="left", padx=(6, 14))
        ttk.Checkbutton(row, text="去每份首尾空行",
                        variable=self.m_trim_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(row, text="合并后校验",
                        variable=self.m_verify_var).pack(side="left")

        row = ttk.Frame(settings)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="文件名    ").pack(side="left")
        ttk.Entry(row, width=30, textvariable=self.m_outname_var).pack(side="left")
        ttk.Label(row, text="  保存到  ").pack(side="left")
        ttk.Entry(row, textvariable=self.m_outdir_var).pack(
            side="left", fill="x", expand=True, padx=(2, 4))
        ttk.Button(row, text="选择…", command=self.browse_merge_outdir).pack(side="left")

        actions = ttk.Frame(parent)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 4))
        self.merge_start_btn = ttk.Button(actions, text="开始合并",
                                          command=self.start_merge)
        self.merge_start_btn.pack(side="left")
        ttk.Button(actions, text="打开所在文件夹",
                   command=self.open_merge_dir).pack(side="left", padx=6)
        self.m_progress = ttk.Progressbar(actions, mode="determinate", length=240)
        self.m_progress.pack(side="left", padx=(14, 8))
        self.m_progress_lbl = ttk.Label(actions, text="就绪", foreground=COLOR_MUTED)
        self.m_progress_lbl.pack(side="left")

        result_box = ttk.LabelFrame(parent, text="结果", padding=4)
        result_box.grid(row=5, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        self.merge_text = tk.Text(result_box, height=6, wrap="word",
                                  state="disabled", relief="flat",
                                  font=(UI_FONT, 9))
        self.merge_text.grid(row=0, column=0, sticky="nsew")
        rsb = ttk.Scrollbar(result_box, orient="vertical",
                            command=self.merge_text.yview)
        rsb.grid(row=0, column=1, sticky="ns")
        self.merge_text.configure(yscrollcommand=rsb.set)
        self._configure_tags(self.merge_text)

        self._setup_dnd(self.merge_tree, self._on_drop_merge)

    def _configure_tags(self, widget):
        widget.tag_configure("ok", foreground=COLOR_OK)
        widget.tag_configure("bad", foreground=COLOR_BAD)
        widget.tag_configure("warn", foreground=COLOR_WARN)
        widget.tag_configure("info", foreground=COLOR_INFO)

    # -------------------------------------------------------------- 变量
    def _init_vars(self):
        self.parts_var = tk.StringVar(value="2")
        self.naming_var = tk.StringVar(value="suffix")
        self.custom_var = tk.StringVar(value="")
        self.outdir_var = tk.StringVar(value="")
        self.subfolder_var = tk.BooleanVar(value=False)
        self.smart_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=True)
        self.trim_var = tk.BooleanVar(value=False)
        self.addblank_var = tk.BooleanVar(value=False)
        self.zipmode_var = tk.StringVar(value="folder")
        self.include_sub_var = tk.BooleanVar(value=False)

        self.m_sep_var = tk.StringVar(value="补一个空行")
        self.m_nl_var = tk.StringVar(value="保持原样")
        self.m_enc_var = tk.StringVar(value="UTF-8")
        self.m_trim_var = tk.BooleanVar(value=False)
        self.m_verify_var = tk.BooleanVar(value=True)
        self.m_outname_var = tk.StringVar(value="")
        self.m_outdir_var = tk.StringVar(value="")

    def _bind_traces(self):
        for var in (self.parts_var, self.naming_var, self.custom_var,
                    self.smart_var):
            var.trace_add("write", lambda *_: self.update_preview())

    # -------------------------------------------------------------- 配置
    def _apply_config(self):
        c = self.cfg
        self.parts_var.set(str(c.get("parts", 2)))
        self.naming_var.set(c.get("naming", "suffix"))
        self.custom_var.set(c.get("custom_name", ""))
        self.outdir_var.set(c.get("out_dir", "") or "")
        self.subfolder_var.set(bool(c.get("subfolder", False)))
        self.smart_var.set(bool(c.get("smart", True)))
        self.verify_var.set(bool(c.get("verify", True)))
        self.trim_var.set(bool(c.get("trim_blank", False)))
        self.addblank_var.set(bool(c.get("add_blank", False)))
        self.zipmode_var.set(c.get("zip_mode", "folder"))
        self.include_sub_var.set(bool(c.get("include_sub", False)))
        self.last_dest = c.get("out_dir", "") or ""

        m = c.get("merge") or {}
        self.m_sep_var.set(M_SEP_REVERSE.get(m.get("separator", "blank"), "补一个空行"))
        self.m_nl_var.set(M_NL_REVERSE.get(m.get("newline", "keep"), "保持原样"))
        self.m_enc_var.set(M_ENC_REVERSE.get(m.get("encoding", "utf-8"), "UTF-8"))
        self.m_trim_var.set(bool(m.get("trim_blank", False)))
        self.m_verify_var.set(bool(m.get("verify", True)))
        self.m_outdir_var.set(m.get("out_dir", "") or c.get("out_dir", "") or "")
        self.m_outname_var.set(m.get("out_name", "") or "")

        geom = c.get("window")
        w, h, x, y = 1040, 760, 60, 40
        if geom:
            m = re.match(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$", str(geom))
            if m:
                w, h, x, y = (int(g) for g in m.groups())
        self.root.update_idletasks()
        # 不能小于控件实际需要的高度，否则底部状态栏会被裁掉
        w = max(w, self.root.winfo_reqwidth())
        h = max(h, self.root.winfo_reqheight())
        w, h, x, y = self._clamp_geometry(w, h, x, y)
        self.root.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self._refresh_recent()
        self.update_preview()

    def _clamp_geometry(self, w, h, x, y):
        """按当前屏幕把窗口限制在可见区域内（含任务栏余量）。"""
        try:
            scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            sw = int(self.root.winfo_screenwidth() / scale)
            sh = int(self.root.winfo_screenheight() / scale)
        except Exception:
            return w, h, x, y
        w = max(640, min(w, sw - 30))
        h = max(560, min(h, sh - 80))
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h - 50))
        return w, h, x, y

    def _collect_config(self) -> dict:
        try:
            geom = self.root.geometry()
            m = re.match(r"^\d+x\d+", geom)
            if not m or int(geom.split("x")[0]) < 640:
                geom = self.cfg.get("window", "")
        except Exception:
            geom = self.cfg.get("window", "")
        cfg = dict(self.cfg)
        cfg.update({
            "parts": self._read_parts() or self.cfg.get("parts", 2),
            "naming": self.naming_var.get(),
            "custom_name": self.custom_var.get(),
            "smart": bool(self.smart_var.get()),
            "verify": bool(self.verify_var.get()),
            "trim_blank": bool(self.trim_var.get()),
            "add_blank": bool(self.addblank_var.get()),
            "zip_mode": self.zipmode_var.get(),
            "subfolder": bool(self.subfolder_var.get()),
            "include_sub": bool(self.include_sub_var.get()),
            "out_dir": self.outdir_var.get().strip(),
            "window": geom,
            "merge": {
                "separator": M_SEP_DISPLAY.get(self.m_sep_var.get(), "blank"),
                "newline": M_NL_DISPLAY.get(self.m_nl_var.get(), "keep"),
                "encoding": M_ENC_DISPLAY.get(self.m_enc_var.get(), "utf-8"),
                "trim_blank": bool(self.m_trim_var.get()),
                "verify": bool(self.m_verify_var.get()),
                "out_dir": self.m_outdir_var.get().strip(),
                "out_name": self.m_outname_var.get().strip(),
            },
        })
        return cfg

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("确认", "任务还在进行中，确定退出吗？"):
                return
            self.cancel_event.set()
        save_config(self._collect_config())
        try:
            self.root.destroy()
        except Exception:
            pass

    # -------------------------------------------------------------- 日志
    def log(self, level: str, message: str, pane: str | None = None):
        if pane == "merge":
            use_merge = True
        elif pane == "split":
            use_merge = False
        elif self.busy:
            use_merge = self.active_mode == "merge"
        else:
            try:
                use_merge = self.notebook.index("current") == 1
            except Exception:
                use_merge = False
        widget = self.merge_text if use_merge else self.result_text
        if widget is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        widget.configure(state="normal")
        widget.insert("end", f"[{stamp}] {message}\n", level)
        widget.see("end")
        widget.configure(state="disabled")

    def clear_log(self):
        for widget in (self.result_text, self.merge_text):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")

    # -------------------------------------------------------------- 拖拽
    def _setup_dnd(self, widget, handler):
        if not HAS_DND:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)
        except Exception:
            pass

    def _drop_paths(self, event):
        try:
            return list(self.root.tk.splitlist(event.data))
        except Exception:
            return str(event.data).split()

    def _on_drop_split(self, event):
        self.add_split_paths(self._drop_paths(event))
        return getattr(event, "action", "copy")

    def _on_drop_merge(self, event):
        self.add_merge_paths(self._drop_paths(event))
        return getattr(event, "action", "copy")

    # -------------------------------------------------------------- 工具栏
    def _dlg_dir(self) -> str:
        for cand in (self.cfg.get("last_dir"), self.outdir_var.get(),
                     self.cfg.get("out_dir")):
            if cand and Path(cand).is_dir():
                return cand
        return str(Path.home())

    def pick_split_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 txt 文件", initialdir=self._dlg_dir(),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if paths:
            self.add_split_paths(list(paths))

    def pick_split_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹",
                                         initialdir=self._dlg_dir())
        if folder:
            self.add_split_paths([folder])

    def browse_out_dir(self):
        folder = filedialog.askdirectory(title="选择保存位置",
                                         initialdir=self._dlg_dir())
        if folder:
            self.outdir_var.set(folder)
            self.last_dest = folder

    def _refresh_recent(self):
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, "end")
        recent = [p for p in self.cfg.get("recent", []) if p]
        if not recent:
            self.recent_menu.add_command(label="（暂无）", state="disabled")
            return
        for path in recent[:5]:
            self.recent_menu.add_command(
                label=path, command=lambda p=path: self.add_split_paths([p]))

    def _push_recent(self, path: Path):
        path = str(path)
        recent = [p for p in self.cfg.get("recent", []) if p != path]
        recent.insert(0, path)
        self.cfg["recent"] = recent[:5]
        self._refresh_recent()

    # -------------------------------------------------------------- 分割列表
    def add_split_paths(self, paths):
        files: list[Path] = []
        ignored = 0
        for raw in paths:
            p = Path(raw)
            try:
                if p.is_dir():
                    found = list_txt_files(p, recursive=self.include_sub_var.get())
                    if not found:
                        self.log("warn", f"⚠ 文件夹里没有 txt：{p}", pane="split")
                    files.extend(found)
                elif p.is_file() and p.suffix.lower() == ".txt":
                    files.append(p)
                else:
                    ignored += 1
            except OSError as exc:
                self.log("bad", f"❌ 无法访问：{p}（{exc}）", pane="split")
        added = 0
        first_new = None
        for p in files:
            key = str(p.resolve())
            if key in self.split_by_path:
                continue
            try:
                size = p.stat().st_size
            except OSError as exc:
                self.log("bad", f"❌ 无法读取：{p.name}（{exc}）", pane="split")
                continue
            iid = f"f{self._seq}"
            self._seq += 1
            self.split_items[iid] = {
                "path": p, "size": size, "lines": None, "enc": "",
                "sel": True, "status": "待处理", "cache": None, "loading": False,
            }
            self.split_by_path[key] = iid
            self.split_tree.insert("", "end", iid=iid, values=(
                "☑", p.name, human_size(size), "—", "—", "待处理"))
            added += 1
            if first_new is None:
                first_new = iid
        if added:
            self.log("info", f"已添加 {added} 个文件", pane="split")
            if not self.outdir_var.get().strip():
                self.outdir_var.set(str(files[0].parent))
            if first_new:
                self.split_tree.selection_set(first_new)
                self.split_tree.focus(first_new)
                self._on_split_select()
            else:
                self._on_split_select()
            self.cfg["last_dir"] = str(files[0].parent)
            self._push_recent(files[0].parent)
        if ignored:
            self.log("warn", f"⚠ 忽略了 {ignored} 个非 txt 文件/文件夹", pane="split")

    def remove_split_selected(self):
        for iid in list(self.split_tree.selection()):
            rec = self.split_items.pop(iid, None)
            if rec:
                self.split_by_path.pop(str(rec["path"].resolve()), None)
            try:
                self.split_tree.delete(iid)
            except Exception:
                pass
        self.update_preview()

    def clear_split(self):
        self.split_items.clear()
        self.split_by_path.clear()
        for iid in self.split_tree.get_children():
            self.split_tree.delete(iid)
        self.update_preview()

    def _on_split_click(self, event):
        if self.split_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.split_tree.identify_column(event.x) != "#1":
            return
        iid = self.split_tree.identify_row(event.y)
        if not iid or iid not in self.split_items:
            return
        rec = self.split_items[iid]
        rec["sel"] = not rec["sel"]
        self.split_tree.set(iid, "sel", "☑" if rec["sel"] else "☐")
        self.update_preview()

    def _on_split_select(self, _event=None):
        iids = self.split_tree.selection()
        if iids:
            rec = self.split_items.get(iids[0])
            self.path_var.set(str(rec["path"]) if rec else "")
            pending = [i for i in iids
                       if self.split_items.get(i, {}).get("lines") is None
                       and not self.split_items.get(i, {}).get("loading")]
            if pending:
                self._start_stats(pending)
        else:
            self.path_var.set("")
        self.update_preview()

    def _start_stats(self, iids):
        todo = []
        for iid in iids:
            rec = self.split_items.get(iid)
            if not rec or rec.get("loading"):
                continue
            rec["loading"] = True
            todo.append((iid, rec["path"]))
        if not todo:
            return
        threading.Thread(target=self._stats_worker, args=(todo,), daemon=True).start()

    def _stats_worker(self, todo):
        for iid, path in todo:
            try:
                size = path.stat().st_size
                text, enc = read_document(path)
                lines = split_lines(text)
                cache = lines if size <= PREVIEW_MAX_BYTES else None
                self._queue.put(("stats", iid, len(lines), enc, cache, ""))
            except Exception as exc:
                self._queue.put(("stats", iid, None, "", None, str(exc)))

    def _handle_stats(self, iid, lines, enc, cache, error):
        rec = self.split_items.get(iid)
        if not rec:
            return
        rec["loading"] = False
        if error or lines is None:
            rec["status"] = "读取失败"
            self.split_tree.set(iid, "status", "读取失败")
            self.log("bad", f"❌ {rec['path'].name}：{error}", pane="split")
            return
        rec["lines"] = lines
        rec["enc"] = enc
        rec["cache"] = cache
        self.split_tree.set(iid, "lines", f"{lines:,}")
        self.split_tree.set(iid, "enc", enc)
        self.update_preview()

    # -------------------------------------------------------------- 预览
    def _read_parts(self):
        try:
            k = int(float(self.parts_var.get().strip()))
        except (TypeError, ValueError):
            return None
        return k if k >= 1 else None

    def update_preview(self, *_):
        if not hasattr(self, "preview_var"):
            return
        iids = self.split_tree.selection()
        if not iids:
            self.preview_var.set("")
            self.example_var.set("")
            return
        rec = self.split_items.get(iids[0])
        if not rec:
            return
        k = self._read_parts()
        n = rec.get("lines")
        if k is None:
            self.preview_var.set("份数无效")
        elif n is None:
            self.preview_var.set("统计行数中…")
        elif n == 0:
            self.preview_var.set("文件为空")
        else:
            kk = min(k, n)
            cache = rec.get("cache")
            if cache is not None:
                dist = plan_distribution(cache, kk, self.smart_var.get() and kk > 1)
                head = " / ".join(f"{d:,}" for d in dist[:6])
                more = " …" if len(dist) > 6 else ""
                extra = "" if kk == k else f"（行数不足，实际 {kk} 份）"
                self.preview_var.set(f"预计行数：{head}{more} 行 {extra}")
            else:
                self.preview_var.set(f"预计 {kk} 份（文件较大，完成后显示实际行数）")

        total_sel = sum(1 for r in self.split_items.values() if r["sel"])
        n_eff = n if n else 1
        names = build_part_names(Path(rec["path"]).stem,
                                 min(k or 1, max(n_eff, 1)),
                                 self.naming_var.get(), self.custom_var.get(),
                                 multi=total_sel > 1)
        if names:
            shown = names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"
            self.example_var.set(f"示例：{shown}")

    # -------------------------------------------------------------- 分割执行
    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.start_btn.configure(state=state)
        self.merge_start_btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def start_split(self):
        if self.busy:
            return
        files = [rec["path"] for rec in self.split_items.values() if rec["sel"]]
        if not files:
            messagebox.showwarning("提示", "请先添加并勾选要分割的 txt 文件")
            return
        dest = self.outdir_var.get().strip()
        if not dest:
            messagebox.showwarning("提示", "请先选择保存位置")
            return
        k = self._read_parts()
        if k is None:
            messagebox.showwarning("提示", "份数必须是大于 0 的整数")
            return
        naming = self.naming_var.get()
        custom = self.custom_var.get().strip()
        if naming == "custom" and not custom:
            messagebox.showwarning("提示", "自定义命名需要填写基础名")
            self.custom_entry.focus_set()
            return
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("错误", f"保存位置不可用：{exc}")
            return

        big = []
        for p in files:
            try:
                if p.stat().st_size > BIG_FILE_BYTES:
                    big.append(p)
            except OSError:
                continue
        if big and not messagebox.askyesno(
                "文件较大",
                f"{len(big)} 个文件超过 200MB，切分时会占用较多内存。\n继续吗？"):
            return

        opts = SplitOptions(
            parts=k, naming=naming, custom_name=custom,
            smart=bool(self.smart_var.get()),
            verify=bool(self.verify_var.get()),
            trim_blank=bool(self.trim_var.get()),
            add_blank=bool(self.addblank_var.get()),
            zip_mode=self.zipmode_var.get(),
            subfolder=bool(self.subfolder_var.get()),
        )
        self.active_mode = "split"
        self.clear_log()
        self.last_dest = dest
        self.outdir_var.set(dest)
        self._set_busy(True)
        self.cancel_event.clear()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.progress_lbl.configure(text="准备中…")
        self.log("info", f"开始分割 {len(files)} 个文件 → {dest}")
        save_config(self._collect_config())
        self.worker = threading.Thread(target=self._split_worker,
                                       args=(files, opts, dest), daemon=True)
        self.worker.start()

    def _split_worker(self, files, opts, dest):
        try:
            summary = run_split_job(files, opts, dest,
                                    emit=lambda ev: self._queue.put(ev),
                                    cancel=self.cancel_event)
            self._queue.put(("done", summary))
        except Exception:
            self._queue.put(("fatal", traceback.format_exc()))

    def _handle_file_done(self, res: FileResult):
        name = Path(res.source).name
        iid = self.split_by_path.get(str(Path(res.source).resolve()))
        if iid in self.split_items:
            status = "跳过" if res.skipped else ("完成" if res.ok else "失败")
            self.split_items[iid]["status"] = status
            try:
                self.split_tree.set(iid, "status", status)
            except Exception:
                pass
        if res.skipped:
            self.log("warn", f"⚠ {name}：{res.message}")
            return
        if not res.ok:
            detail = res.verify_msg or res.message
            self.log("bad", f"❌ {name}：{detail}")
            return
        counts = " / ".join(f"{p.lines:,}" for p in res.parts)
        tail = "校验通过" if res.verify_ok else "校验失败"
        extra = []
        if res.removed_blank:
            extra.append(f"删空行 {res.removed_blank}")
        if res.added_blank:
            extra.append(f"补空行 {res.added_blank}")
        suffix = ("，" + "，".join(extra)) if extra else ""
        note = f"（{res.message}）" if res.message else ""
        level = "ok" if res.verify_ok else "bad"
        self.log(level, f"✅ {name} → {len(res.parts)} 份：{counts} 行 · {tail}{suffix}{note}")
        if not res.verify_ok:
            self.log("bad", f"   校验详情：{res.verify_msg}")

    def _handle_done(self, summary):
        self._set_busy(False)
        self.progress.configure(value=100)
        tag = "ok" if summary["fail"] == 0 and not summary.get("cancelled") else "warn"
        state = "已取消" if summary.get("cancelled") else "完成"
        self.log(tag, f"— {state}：成功 {summary['ok']} / 失败 {summary['fail']}，"
                      f"共 {summary['parts']} 个分片，耗时 {summary['elapsed']:.1f}s",
                 pane="split")
        if summary.get("zip"):
            self.log("info", f"zip：{summary['zip']}", pane="split")
        if summary["fail"]:
            self.progress_lbl.configure(text="完成，但有失败项")
            self.status_var.set("完成，但有失败项")
        else:
            self.progress_lbl.configure(text="完成")
            self.status_var.set(f"完成：{summary['parts']} 个分片 / {summary['ok']} 个文件")
        save_config(self._collect_config())

    # -------------------------------------------------------------- 合并
    def add_merge_paths(self, paths):
        added = 0
        for raw in paths:
            p = Path(raw)
            try:
                if p.is_dir():
                    for f in list_txt_files(p, recursive=False):
                        if self._append_merge(f):
                            added += 1
                elif p.is_file() and p.suffix.lower() == ".txt":
                    if self._append_merge(p):
                        added += 1
            except OSError as exc:
                self.log("bad", f"❌ 无法访问：{p}（{exc}）", pane="merge")
        if added:
            self._refresh_merge_tree()
            self.log("info", f"已添加 {added} 个文件，共 {len(self.merge_paths)} 个待合并",
                     pane="merge")
            self.cfg["last_dir"] = str(Path(paths[0]).parent if Path(paths[0]).is_file()
                                       else paths[0])
            self._push_recent(Path(paths[0]))
        if not self.m_outdir_var.get() and self.merge_paths:
            self.m_outdir_var.set(str(self.merge_paths[0].parent))
        if not self.m_outname_var.get():
            self.m_outname_var.set(f"合并_{now_stamp()}.txt")

    def _append_merge(self, path: Path) -> bool:
        resolved = path.resolve()
        if any(p.resolve() == resolved for p in self.merge_paths):
            return False
        self.merge_paths.append(path)
        return True

    def _refresh_merge_tree(self):
        selected = set(self.merge_tree.selection())
        for iid in self.merge_tree.get_children():
            self.merge_tree.delete(iid)
        for i, p in enumerate(self.merge_paths, start=1):
            try:
                size = human_size(p.stat().st_size)
            except OSError:
                size = "?"
            iid = f"m{i}"
            self.merge_tree.insert("", "end", iid=iid,
                                   values=(i, p.name, size))
            if iid in selected:
                self.merge_tree.selection_add(iid)

    def _on_merge_select(self, _event=None):
        iids = self.merge_tree.selection()
        if iids:
            idx = int(iids[0][1:]) - 1
            if 0 <= idx < len(self.merge_paths):
                self.merge_path_var.set(str(self.merge_paths[idx]))
        else:
            self.merge_path_var.set("")

    def remove_merge_selected(self):
        idxs = sorted((int(i[1:]) - 1 for i in self.merge_tree.selection()),
                      reverse=True)
        for idx in idxs:
            if 0 <= idx < len(self.merge_paths):
                self.merge_paths.pop(idx)
        self._refresh_merge_tree()

    def clear_merge(self):
        self.merge_paths.clear()
        self._refresh_merge_tree()

    def move_merge(self, delta):
        iids = self.merge_tree.selection()
        if not iids:
            return
        idx = int(iids[0][1:]) - 1
        new = idx + delta
        if not (0 <= new < len(self.merge_paths)):
            return
        self.merge_paths[idx], self.merge_paths[new] = (
            self.merge_paths[new], self.merge_paths[idx])
        self._refresh_merge_tree()
        self.merge_tree.selection_set(f"m{new + 1}")

    def sort_merge_natural(self):
        self.merge_paths.sort(key=lambda p: natural_key(p.name))
        self._refresh_merge_tree()

    def sort_merge_parts(self):
        self.merge_paths.sort(key=lambda p: part_index_key(p.name))
        self._refresh_merge_tree()
        self.log("info", "已按分片序号排序（适合把 _01.._NN 还原）", pane="merge")

    def pick_merge_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要合并的 txt", initialdir=self._dlg_dir(),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if paths:
            self.add_merge_paths(list(paths))

    def pick_merge_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹",
                                         initialdir=self._dlg_dir())
        if folder:
            self.add_merge_paths([folder])

    def browse_merge_outdir(self):
        folder = filedialog.askdirectory(title="选择保存位置",
                                         initialdir=self._dlg_dir())
        if folder:
            self.m_outdir_var.set(folder)

    def start_merge(self):
        if self.busy:
            return
        if not self.merge_paths:
            messagebox.showwarning("提示", "请先添加要合并的 txt 文件")
            return
        outdir = self.m_outdir_var.get().strip()
        if not outdir:
            messagebox.showwarning("提示", "请先选择合并结果的保存位置")
            return
        name = self.m_outname_var.get().strip() or f"合并_{now_stamp()}.txt"
        if not name.lower().endswith(".txt"):
            name += ".txt"
        out_path = Path(outdir) / sanitize_filename(name, "合并")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("错误", f"保存位置不可用：{exc}")
            return
        out_path = unique_path(out_path)
        for p in self.merge_paths:
            if p.resolve() == out_path.resolve():
                messagebox.showerror("错误", "输出文件与某个输入文件相同，请改名")
                return
        opts = MergeOptions(
            separator=M_SEP_DISPLAY.get(self.m_sep_var.get(), "blank"),
            newline=M_NL_DISPLAY.get(self.m_nl_var.get(), "keep"),
            encoding=M_ENC_DISPLAY.get(self.m_enc_var.get(), "utf-8"),
            trim_blank=bool(self.m_trim_var.get()),
            verify=bool(self.m_verify_var.get()),
        )
        self.active_mode = "merge"
        self.clear_log()
        self._set_busy(True)
        self.cancel_event.clear()
        self.m_progress.configure(maximum=100, value=15)
        self.m_progress_lbl.configure(text="合并中…")
        self.log("info", f"开始合并 {len(self.merge_paths)} 个文件 → {out_path}")
        save_config(self._collect_config())
        self.worker = threading.Thread(target=self._merge_worker,
                                       args=(list(self.merge_paths), opts, out_path),
                                       daemon=True)
        self.worker.start()

    def _merge_worker(self, paths, opts, out_path):
        try:
            result = run_merge_job(paths, opts, out_path,
                                   emit=lambda ev: self._queue.put(ev),
                                   cancel=self.cancel_event)
            self._queue.put(("merge_done", result))
        except Exception:
            self._queue.put(("fatal", traceback.format_exc()))

    def _handle_merge_done(self, result):
        self._set_busy(False)
        self.m_progress.configure(value=100 if result.get("ok") else 0)
        if result.get("ok"):
            self.m_progress_lbl.configure(text="完成")
            self.log("ok", f"✅ 合并完成 → {Path(result['out']).name} · "
                          f"{result['lines']:,} 行 · {result['chars']:,} 字符 · "
                          f"SHA {result['sha']}", pane="merge")
            for info in result.get("sources", [])[:12]:
                self.log("info", f"   · {info}", pane="merge")
            self.status_var.set(f"合并完成：{Path(result['out']).name}")
        else:
            self.m_progress_lbl.configure(text="失败")
            self.log("bad", f"❌ 合并失败：{result.get('message')}", pane="merge")
            self.status_var.set("合并失败")
        save_config(self._collect_config())

    # -------------------------------------------------------------- 打开目录
    def open_out_dir(self):
        target = self.last_dest or self.outdir_var.get().strip()
        self._open_folder(target)

    def open_merge_dir(self):
        target = self.m_outdir_var.get().strip()
        self._open_folder(target)

    def _open_folder(self, target: str):
        if not target or not Path(target).is_dir():
            messagebox.showwarning("提示", "输出文件夹还不存在")
            return
        try:
            os.startfile(target)
        except Exception as exc:
            messagebox.showerror("错误", f"无法打开文件夹：{exc}")

    # -------------------------------------------------------------- 队列
    def cancel_job(self):
        if self.busy:
            self.cancel_event.set()
            self.log("warn", "正在取消…")
            self.progress_lbl.configure(text="取消中…")

    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        try:
            self.root.after(100, self._poll_queue)
        except Exception:
            pass

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "stats":
            self._handle_stats(*msg[1:])
        elif kind == "file":
            _, pos, total, name = msg
            self.progress.configure(maximum=max(total, 1), value=pos - 1)
            self.progress_lbl.configure(text=f"处理中 {pos}/{total}：{name}")
            self.log("info", f"[{pos}/{total}] {name}")
        elif kind == "part":
            _, idx, total, name = msg
            self.progress_lbl.configure(text=f"  └ 写入 {idx}/{total}：{name}")
        elif kind == "log":
            _, level, text = msg
            self.log(level, text)
        elif kind == "file_done":
            self._handle_file_done(msg[1])
        elif kind == "done":
            self._handle_done(msg[1])
        elif kind == "merge_done":
            self._handle_merge_done(msg[1])
        elif kind == "fatal":
            self._set_busy(False)
            try:
                self.progress.stop()
                self.m_progress.stop()
            except Exception:
                pass
            self.log("bad", "❌ 发生未预期的错误：")
            self.log("bad", msg[1])
            messagebox.showerror("错误", "程序遇到未预期的错误，详情见结果面板")


def main() -> int:
    if not _TK_OK:
        print(f"tkinter 不可用：{_TK_ERROR}")
        return 1
    _enable_dpi()
    try:
        root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    except Exception:
        root = tk.Tk()
    TxtSplitterApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
