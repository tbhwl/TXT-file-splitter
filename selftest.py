# -*- coding: utf-8 -*-
"""txt_splitter 自测脚本（无需 GUI，直接跑核心逻辑）。"""

import random
import shutil
import sys
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import txt_splitter as ts  # noqa: E402

BASE = HERE / "_selftest"
if BASE.exists():
    shutil.rmtree(BASE, ignore_errors=True)
BASE.mkdir(parents=True, exist_ok=True)

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def fresh(name):
    d = BASE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_text(path, text, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def read(path, encoding="utf-8"):
    return Path(path).read_bytes().decode(encoding)


def split(src, parts, dest, **kw):
    opts = ts.SplitOptions(parts=parts, **kw)
    d = fresh(dest)
    res = ts.split_one_file(src, opts, d, multi=False)
    return res, d


def test_01_even_split():
    lines = [f"line{i}\n" for i in range(100)]
    src = write_text(BASE / "t1.txt", "".join(lines))
    res, d = split(src, 3, "out1", smart=False)
    counts = [p.lines for p in res.parts]
    check("1.100行/3份 = 34/33/33", counts == [34, 33, 33], str(counts))
    joined = "".join(read(p.path) for p in res.parts)
    check("1.拼接 == 原文", joined == "".join(lines))
    check("1.校验通过", res.ok and res.verify_ok, res.message)


def test_02_small():
    src = write_text(BASE / "t2.txt", "".join(f"{i}\n" for i in range(10)))
    res, d = split(src, 3, "out2", smart=False)
    check("2.10行/3份 = 4/3/3", [p.lines for p in res.parts] == [4, 3, 3],
          str([p.lines for p in res.parts]))

    src = write_text(BASE / "t2b.txt", "a\nb\nc\nd\ne\n")
    res, d = split(src, 10, "out2b", smart=False)
    check("2.5行/10份 自动降为5份", len(res.parts) == 5, str(len(res.parts)))
    check("2.降份提示", "降为 5" in res.message, res.message)


def test_03_encoding_newline():
    content = "第一行\r\n第二行\r\n第三行"
    src = BASE / "gbk.txt"
    src.write_bytes(content.encode("gbk"))
    res, d = split(src, 2, "out3", smart=False)
    check("3.GBK 识别", res.encoding in ("gb18030", "gbk"), res.encoding)
    check("3.GBK 不乱码", read(res.parts[0].path, res.encoding).startswith("第一行"))
    check("3.CRLF 保留", b"\r\n" in Path(res.parts[0].path).read_bytes())
    check("3.末尾无换行保留",
          not Path(res.parts[-1].path).read_bytes().endswith(b"\n"))
    check("3.校验通过", res.verify_ok, res.verify_msg)


def test_04_smart_breakpoint():
    lines = [f"正文第{i}行\n" for i in range(1000)]
    lines[340] = "第二章 新的开始\n"
    src = write_text(BASE / "chapter.txt", "".join(lines))
    res, d = split(src, 3, "out4", smart=True)
    counts = [p.lines for p in res.parts]
    check("4.切点落在章节行前(340)", counts[0] == 340, str(counts))
    check("4.内容完整", "".join(read(p.path) for p in res.parts) == "".join(lines))


def test_05_smart_off():
    lines = [f"正文第{i}行\n" for i in range(1000)]
    lines[340] = "第二章 新的开始\n"
    src = write_text(BASE / "chapter2.txt", "".join(lines))
    res, d = split(src, 3, "out5", smart=False)
    counts = [p.lines for p in res.parts]
    check("5.关智能断点 = 精确均分", counts == [334, 333, 333], str(counts))


def test_06_blank_ops():
    content = "\n\n" + "".join(f"第{i}段\n" for i in range(12)) + "\n\n"
    src = write_text(BASE / "blank.txt", content)
    res, d = split(src, 3, "out6", smart=False, trim_blank=True, add_blank=True)
    parts = [read(p.path) for p in res.parts]
    clean = [p.strip("\r\n") for p in parts]
    check("6.去首尾空行", all(not p.startswith("\n") for p in clean))
    check("6.每份补空行", all(p.endswith("\n\n") for p in parts),
          repr([p[-4:] for p in parts]))
    check("6.宽松校验通过", res.verify_ok, res.verify_msg)
    check("6.空行计数", res.removed_blank > 0 and res.added_blank >= 3,
          f"del={res.removed_blank} add={res.added_blank}")


def test_07_tamper_detect():
    lines = [f"第{i}行内容\n" for i in range(60)]
    src = write_text(BASE / "tamper.txt", "".join(lines))
    res, d = split(src, 3, "out7", smart=False)
    target = Path(res.parts[1].path)
    data = target.read_bytes().decode("utf-8")
    target.write_bytes((data.replace("第", "壞", 1)).encode("utf-8"))
    entries = [(Path(p.path), p.start_line - 1, p.end_line) for p in res.parts]
    ok, msg = ts.verify_parts(lines, "".join(lines), entries, ts.SplitOptions(parts=3),
                              "utf-8", "\n")
    check("7.篡改被检出", not ok, msg)
    check("7.定位到第2份", "第 2/" in msg, msg)


def test_08_batch_failure():
    indir = fresh("batch_in")
    for i in range(10):
        write_text(indir / f"good{i}.txt", "".join(f"内容{j}\n" for j in range(20)))
    bad = indir / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00\xd8\x00\x00")
    out = fresh("batch_out")
    files = sorted(indir.glob("*.txt"))
    opts = ts.SplitOptions(parts=2, smart=False)
    summary = ts.run_split_job([str(f) for f in files], opts, out)
    check("8.批量 10 成功", summary["ok"] == 10, str(summary["ok"]))
    check("8.批量 1 失败", summary["fail"] == 1, str(summary["fail"]))
    check("8.失败不影响其他", summary["parts"] == 20, str(summary["parts"]))
    check("8.报告存在", Path(summary["dest"], ts.REPORT_NAME).exists())


def test_09_batch_custom_name():
    indir = fresh("multi_in")
    write_text(indir / "a.txt", "x\ny\nz\n")
    write_text(indir / "b.txt", "1\n2\n3\n")
    out = fresh("multi_out")
    opts = ts.SplitOptions(parts=2, naming="custom", custom_name="章{n}", smart=False)
    summary = ts.run_split_job([str(indir / "a.txt"), str(indir / "b.txt")], opts, out)
    names = sorted(p.name for r in summary["files"] for p in r.parts)
    check("9.批量自定义名前置原名", all(n.startswith(("a_", "b_")) for n in names), str(names))


def test_10_zip_only():
    src = write_text(BASE / "ziptest.txt", "".join(f"{i}\n" for i in range(30)))
    out = fresh("zip_out")
    opts = ts.SplitOptions(parts=3, smart=False, zip_mode="zip")
    summary = ts.run_split_job([str(src)], opts, out)
    entries = [p.name for p in out.iterdir()]
    zips = list(out.glob("*.zip"))
    check("10.仅zip 产出zip", len(zips) == 1, str(entries))
    check("10.仅zip 无散落分片", not list(out.glob("ziptest_*.txt")), str(entries))
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
    check("10.zip 内容=3份+报告", len(names) == 4 and ts.REPORT_NAME in names, str(names))
    check("10.zip 内分片可读", sorted(n for n in names if n.endswith(".txt")) ==
          ["ziptest_1.txt", "ziptest_2.txt", "ziptest_3.txt"], str(names))


def test_11_zip_chinese():
    src = write_text(BASE / "中文名称.txt", "".join(f"第{i}行\n" for i in range(9)))
    out = fresh("zip_cn")
    opts = ts.SplitOptions(parts=3, smart=False, zip_mode="zip")
    ts.run_split_job([str(src)], opts, out)
    zp = next(out.glob("*.zip"))
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
    check("11.zip 中文名正常", "中文名称_1.txt" in names, str(names))


def test_12_merge_encodings():
    write_text(BASE / "m1.txt", "甲\n乙\n", "utf-8")
    write_text(BASE / "m2.txt", "丙\n丁\n", "gbk")
    p3 = BASE / "m3.txt"
    p3.write_bytes("戊\n己\n".encode("utf-8-sig"))
    out = BASE / "merged.txt"
    res = ts.run_merge_job([BASE / "m1.txt", BASE / "m2.txt", p3],
                           ts.MergeOptions(separator="none", newline="keep",
                                           encoding="utf-8"), out)
    check("12.合并成功", res["ok"], res["message"])
    text = out.read_text(encoding="utf-8")
    check("12.内容全部在", all(ch in text for ch in "甲乙丙丁戊己"), text)
    check("12.行数=6", res["lines"] == 6, str(res["lines"]))


def test_13_merge_restore():
    lines = [f"第{i}行\n" for i in range(37)]
    src = write_text(BASE / "restore_src.txt", "".join(lines))
    res, d = split(src, 4, "restore_parts", smart=False)
    merged = BASE / "restore_out.txt"
    out = ts.run_merge_job([Path(p.path) for p in res.parts],
                           ts.MergeOptions(separator="none", newline="keep",
                                           encoding="utf-8"), merged)
    check("13.合并还原成功", out["ok"], out["message"])
    check("13.还原内容一致", read(merged) == "".join(lines))
    check("13.按分片序号排序", [p.name for p in
                                sorted(map(Path, [p.path for p in res.parts]),
                                       key=lambda p: ts.part_index_key(p.name))] ==
          [p.name for p in map(Path, [p.path for p in res.parts])])


def test_14_config():
    original = ts.config_candidates
    cfg_path = BASE / "cfg" / "config.json"
    ts.config_candidates = lambda: [cfg_path]
    try:
        ts.save_config({"parts": 7, "naming": "custom", "recent": ["D:/x"]})
        loaded = ts.load_config()
        check("15.配置保存/读取", loaded.get("parts") == 7 and
              loaded.get("naming") == "custom", str(loaded.get("parts")))
        check("15.配置缺省项补齐", loaded.get("smart") is True)
        cfg_path.write_text("{ bad json", encoding="utf-8")
        check("15.坏配置不崩溃", ts.load_config().get("parts") == 2)
    finally:
        ts.config_candidates = original


def test_16_naming():
    names = ts.build_part_names("小说", 12, "suffix")
    check("16.补零宽度", names[0] == "小说_01.txt" and names[-1] == "小说_12.txt", str(names[:2]))
    names = ts.build_part_names("小说", 9, "suffix")
    check("16.9份不补零", names[0] == "小说_1.txt", names[0])
    names = ts.build_part_names("x", 2, "custom", "a/b:c*?_{n}")
    check("16.非法字符过滤", all(not any(c in n for c in '\\/:*?"<>|') for n in names), str(names))
    names = ts.build_part_names("x", 3, "custom", "段落")
    check("16.自定义基础名加序号", names == ["段落_1.txt", "段落_2.txt", "段落_3.txt"], str(names))
    names = ts.build_part_names("x", 2, "custom", "{name}_{n}", multi=True)
    check("16.占位符替换", names == ["x_1.txt", "x_2.txt"], str(names))
    d = fresh("uniq")
    (d / "a.txt").write_text("x", encoding="utf-8")
    check("16.重名自动(1)", ts.unique_path(d / "a.txt").name == "a (1).txt")


def test_17_property():
    rnd = random.Random(7)
    ok = True
    detail = ""
    for _ in range(300):
        n = rnd.randint(1, 60)
        k = rnd.randint(1, 30)
        lines = [f"l{i}\n" for i in range(n)]
        cuts = ts.plan_cuts(lines, k, smart=rnd.choice([True, False]))
        if not cuts:
            ok, detail = False, f"n={n} k={k} 无切点"
            break
        if cuts[0][0] != 0 or cuts[-1][1] != n:
            ok, detail = False, f"n={n} k={k} 未覆盖全文 {cuts}"
            break
        if any(e - s < 1 for s, e in cuts):
            ok, detail = False, f"n={n} k={k} 出现空份 {cuts}"
            break
        if any(cuts[i][1] != cuts[i + 1][0] for i in range(len(cuts) - 1)):
            ok, detail = False, f"n={n} k={k} 切点不连续 {cuts}"
            break
        if len(cuts) != min(k, n):
            ok, detail = False, f"n={n} k={k} 份数不符 {len(cuts)}"
            break
    check("17.切点性质 300 组随机", ok, detail)


def test_18_empty_and_k1():
    empty = write_text(BASE / "empty.txt", "")
    res, d = split(empty, 3, "out18a", smart=False)
    check("18.空文件跳过", res.skipped and not res.ok, res.message)

    src = write_text(BASE / "k1.txt", "abc\ndef\n")
    res, d = split(src, 1, "out18b")
    check("18.份数=1 内容一致", len(res.parts) == 1 and read(res.parts[0].path) == "abc\ndef\n")


def test_19_zip_combo():
    src = write_text(BASE / "combo.txt", "".join(f"{i}\n" for i in range(30)))
    out = fresh("combo_out")
    (out / "无关文件.txt").write_text("x", encoding="utf-8")
    opts = ts.SplitOptions(parts=3, smart=False, zip_mode="folder+zip")
    summary = ts.run_split_job([str(src)], opts, out)
    zips = list(out.glob("combo_split.zip"))
    check("19.folder+zip 分片与zip都在",
          len(list(out.glob("combo_*.txt"))) == 3 and len(zips) == 1,
          str([p.name for p in out.iterdir()]))
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            names = zf.namelist()
        check("19.zip 不含无关文件", "无关文件.txt" not in names, str(names))
        check("19.zip 含报告", ts.REPORT_NAME in names, str(names))


def test_20_batch_zip():
    indir = fresh("bz_in")
    write_text(indir / "a.txt", "1\n2\n3\n4\n")
    write_text(indir / "b.txt", "5\n6\n7\n8\n")
    out = fresh("bz_out")
    opts = ts.SplitOptions(parts=2, smart=False, zip_mode="zip")
    summary = ts.run_split_job([str(indir / "a.txt"), str(indir / "b.txt")], opts, out)
    zips = list(out.glob("批量分割_*.zip"))
    check("20.批量仅zip 生成总包", len(zips) == 1, str([p.name for p in out.iterdir()]))
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            names = zf.namelist()
        check("20.zip 内按文件分子目录",
              "a_split/a_1.txt" in names and "b_split/b_2.txt" in names, str(names))


def main():
    tests = [
        test_01_even_split, test_02_small, test_03_encoding_newline,
        test_04_smart_breakpoint, test_05_smart_off, test_06_blank_ops,
        test_07_tamper_detect, test_08_batch_failure, test_09_batch_custom_name,
        test_10_zip_only, test_11_zip_chinese, test_12_merge_encodings,
        test_13_merge_restore, test_14_config, test_16_naming,
        test_17_property, test_18_empty_and_k1, test_19_zip_combo,
        test_20_batch_zip,
    ]
    for test in tests:
        print(f"\n== {test.__name__} ==")
        try:
            test()
        except Exception:
            traceback.print_exc()
            FAILED.append((test.__name__, "异常"))

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"  FAILED: {name}  {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
