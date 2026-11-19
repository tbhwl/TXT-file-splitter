# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真实创建窗口，跑一遍分割与合并，验证线程/队列/控件。"""

import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import txt_splitter as ts  # noqa: E402

BASE = HERE / "_guitest"
if BASE.exists():
    shutil.rmtree(BASE, ignore_errors=True)
BASE.mkdir(parents=True)

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def wait_idle(root, app, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if not app.busy:
            return True
        time.sleep(0.03)
    return False


def main():
    src = BASE / "gui_src.txt"
    src.write_text("".join(f"第{i}行\n" for i in range(60)), encoding="utf-8")
    src2 = BASE / "gui_src2.txt"
    src2.write_text("".join(f"另一份{i}\n" for i in range(20)), encoding="utf-8")

    root = ts.tk.Tk()
    root.withdraw()
    app = ts.TxtSplitterApp(root)
    root.update()
    check("GUI.窗口创建", app.split_tree is not None and app.merge_tree is not None)
    check("GUI.拖拽降级不报错", ts.HAS_DND in (True, False))

    app.add_split_paths([str(src)])
    root.update()
    check("GUI.添加文件进列表", len(app.split_items) == 1, str(len(app.split_items)))

    end = time.time() + 10
    rec = None
    while time.time() < end:
        root.update()
        rec = next(iter(app.split_items.values()))
        if rec["lines"] is not None:
            break
        time.sleep(0.03)
    check("GUI.行数/编码统计", rec and rec["lines"] == 60, str(rec.get("lines") if rec else None))

    app.parts_var.set("3")
    root.update()
    check("GUI.预计行数预览", "预计行数" in app.preview_var.get(), app.preview_var.get())
    check("GUI.文件名示例", "gui_src_1.txt" in app.example_var.get(), app.example_var.get())

    out = BASE / "split_out"
    app.outdir_var.set(str(out))
    app.start_split()
    check("GUI.任务跑完", wait_idle(root, app), "超时")
    pump(root, 0.5)
    made = sorted(p.name for p in out.glob("gui_src_*.txt"))
    check("GUI.产出3份", len(made) == 3, str(made))
    log = app.result_text.get("1.0", "end")
    check("GUI.结果面板显示校验通过", "校验通过" in log, log[-300:])
    check("GUI.进度条到100", app.progress["value"] == 100, str(app.progress["value"]))

    # 合并
    app.add_merge_paths([str(src), str(src2)])
    root.update()
    check("GUI.合并列表2个", len(app.merge_paths) == 2, str(len(app.merge_paths)))
    app.sort_merge_parts()
    root.update()
    mout = BASE / "merge_out"
    app.m_outdir_var.set(str(mout))
    app.m_outname_var.set("合并结果.txt")
    app.start_merge()
    check("GUI.合并跑完", wait_idle(root, app), "超时")
    pump(root, 0.5)
    merged = mout / "合并结果.txt"
    check("GUI.合并文件存在", merged.exists(), str(list(mout.glob('*'))))
    if merged.exists():
        text = merged.read_text(encoding="utf-8")
        # 默认「文件之间补一个空行」：60 + 20 + 1 = 81 行
        check("GUI.合并内容行数", len(text.splitlines()) == 81, str(len(text.splitlines())))
        check("GUI.合并补空行生效", "\n\n" in text)
    mlog = app.merge_text.get("1.0", "end")
    check("GUI.合并面板成功", "合并完成" in mlog, mlog[-300:])

    # 配置记忆
    app.parts_var.set("5")
    app.smart_var.set(False)
    cfg = app._collect_config()
    check("GUI.配置采集", cfg["parts"] == 5 and cfg["smart"] is False, str(cfg["parts"]))

    root.destroy()
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"  FAILED: {name}  {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
