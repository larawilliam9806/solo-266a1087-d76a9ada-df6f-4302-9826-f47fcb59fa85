"""关键流程自测（不依赖 pytest，直接 python -I tests/selftest.py 运行）。

覆盖：
1. CSV 校验：中文表头、重复 (样本,项目)、负数/非数字/空值/缺列/缺批次号
2. 端到端：上传 → 预览(增/改/删/不变分类) → 发布 → 登记版本/行数 → 下游文件哈希一致
3. 并发：两个页面从同一版本发起，仅一个发布成功，另一个 409 须重新预览
4. 崩溃恢复：文件缺失、文件被改坏、遗留临时文件、游离文件、改名后未提交
5. 撤回语义：整批替换后旧行消失；会话取消/重复发布防护
6. 游离文件隔离：跨挂载复制、同秒同名独立留存、复制失败原件保留且
   下次启动继续处理、半成品清理、恢复日志可定位隔离件
"""
import errno
import hashlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wq_publish.app import create_app
from wq_publish.storage import LIVE_FILENAME
from wq_publish.validation import render_csv, validate_csv

PASS, FAIL = 0, 0
EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "examples")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def read_example(fname):
    with open(os.path.join(EXAMPLE_DIR, fname), "rb") as fh:
        return fh.read()


def upload(client, fname, batch_no="B1"):
    return client.post(
        "/preview",
        data={"batch_no": batch_no,
              "csv_file": (io.BytesIO(read_example(fname)), fname)},
        content_type="multipart/form-data",
    )


# ---------------- 1. 校验单元测试 ----------------
print("== 1. CSV 校验 ==")
rows, errs = validate_csv(read_example("batch_v1.csv"), "B1")
check("合法 CSV 通过并解析出 5 行", errs == [] and len(rows) == 5, str(errs))

rows, errs = validate_csv(read_example("batch_invalid.csv"), "B1")
check("重复(样本,项目)被拒", any("重复" in e for e in errs), str(errs))
check("负浓度被拒", any("负数" in e for e in errs), str(errs))
check("空浓度被拒", any("浓度为空" in e for e in errs), str(errs))
check("空项目被拒", any("检测项目为空" in e for e in errs), str(errs))

bad = "样本号,浓度\nS1,1.0\n".encode()
rows, errs = validate_csv(bad, "B1")
check("缺少检测项目列被拒", rows is None and any("表头" in e for e in errs), str(errs))

rows, errs = validate_csv(read_example("batch_v1.csv"), " ")
check("空批次号被拒", any("批次号" in e for e in errs), str(errs))

for text in ["S1,氨氮,nan", "S1,氨氮,inf", "S1,氨氮,abc"]:
    raw = f"样本号,检测项目,浓度\n{text}\n".encode()
    rows, errs = validate_csv(raw, "B1")
    check(f"非法浓度 {text!r} 被拒", rows is None and len(errs) > 0, str(errs))

raw_zero = "样本号,检测项目,浓度\nS1,氨氮,0\n".encode()
rows, errs = validate_csv(raw_zero, "B1")
check("零浓度（非负边界）通过", errs == [] and rows[0]["concentration"] == 0.0, str(errs))

# ---------------- 2-5. 端到端（隔离临时目录） ----------------
tmp = tempfile.mkdtemp(prefix="wq-selftest-")
data_dir = os.path.join(tmp, "data")
down_dir = os.path.join(tmp, "downstream")
app = create_app(data_dir=data_dir, downstream_dir=down_dir)
client = app.test_client()

print("== 2. 首次发布 v1 ==")
resp = upload(client, "batch_v1.csv", "BATCH-001")
check("预览页 200 且显示增删改", resp.status_code == 200
      and "新增".encode() in resp.data and "撤回".encode() in resp.data)
import re
m = re.search(rb'/publish/([0-9a-f]+)', resp.data)
check("预览页含发布令牌", m is not None)
token1 = m.group(1).decode()

resp = client.post(f"/publish/{token1}")
check("确认发布后 302 回首页", resp.status_code == 302)
reg = app.storage.current_registry()
check("登记为 v1、5 行", reg["current_version"] == 1 and reg["row_count"] == 5,
      f"got {dict(reg) if reg else None}")
live = os.path.join(down_dir, LIVE_FILENAME)
check("下游 CSV 已生成", os.path.exists(live))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("文件哈希与登记一致", file_hash == reg["content_hash"])
check("下游目录无临时文件残留",
      not [n for n in os.listdir(down_dir) if n.startswith(".tmp-")])
resp = client.get("/published.csv")
check("已发布 CSV 可下载", resp.status_code == 200 and b"S001" in resp.data)
resp = client.post(f"/publish/{token1}")
check("同一会话不可重复发布（返回 409 过期页）", resp.status_code == 409
      and app.storage.get_session(token1)["status"] == "published")

print("== 3. 修订预览的增/改/删/不变分类 ==")
resp = upload(client, "batch_v2_revision.csv", "BATCH-002")
body = resp.data.decode()
# v2 相对 v1：S004氨氮新增；S001氨氮、S002COD 修改；S002氨氮 撤回；S001总磷/S003总磷 不变
check("新增 1 行", "+1" in body and "S004" in body)
check("修改 2 行", "~2" in body and "1.25" in body and "35.0" in body)
check("撤回 1 行（S002 氨氮）", "−1" in body and "S002" in body)
check("不变 2 行", "不变项 2" in body)
token2 = re.search(rb'/publish/([0-9a-f]+)', resp.data).group(1).decode()
client.post(f"/publish/{token2}")
reg = app.storage.current_registry()
check("发布后登记为 v2、5 行", reg["current_version"] == 2 and reg["row_count"] == 5)
current = app.storage.current_rows()
check("撤回生效：S002 氨氮已不存在",
      not any(r["sample_no"] == "S002" and r["analyte"] == "氨氮" for r in current))
check("修改生效：S001 氨氮=1.3",
      any(r["sample_no"] == "S001" and r["analyte"] == "氨氮"
          and r["concentration"] == 1.3 for r in current))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("v2 文件哈希与登记一致", file_hash == reg["content_hash"])

print("== 4. 同版本并发修订：只允许一个成功 ==")
r_a = upload(client, "batch_v1.csv", "CONCUR-A")
r_b = upload(client, "batch_v2_revision.csv", "CONCUR-B")
tok_a = re.search(rb'/publish/([0-9a-f]+)', r_a.data).group(1).decode()
tok_b = re.search(rb'/publish/([0-9a-f]+)', r_b.data).group(1).decode()
ok = client.post(f"/publish/{tok_a}")
check("A 发布成功 → v3", ok.status_code == 302
      and app.storage.current_registry()["current_version"] == 3)
conflict = client.post(f"/publish/{tok_b}")
check("B 发布被拒并返回 409 过期预览页", conflict.status_code == 409
      and "该预览已过期".encode() in conflict.data)
sess_b = app.storage.get_session(tok_b)
check("B 会话被标记 obsolete", sess_b["status"] == "obsolete")
reg_before = app.storage.current_registry()["current_version"]
retry = upload(client, "batch_v2_revision.csv", "CONCUR-B-RETRY")
tok_b2 = re.search(rb'/publish/([0-9a-f]+)', retry.data).group(1).decode()
client.post(f"/publish/{tok_b2}")
check("B 重新预览后可发布 → v4",
      app.storage.current_registry()["current_version"] == reg_before + 1)
check("A 的发布未被覆盖：v4 文件行数=5",
      app.storage.current_registry()["row_count"] == 5)

print("== 5. 崩溃后重启自动恢复 ==")
# 5a 下游文件被删除
os.unlink(live)
app2 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app2.storage.current_registry()
check("5a 文件缺失后重建且哈希与登记一致",
      os.path.exists(live)
      and hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5b 文件被外部改坏
with open(live, "ab") as fh:
    fh.write(b"CORRUPTED\n")
app3 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app3.storage.current_registry()
check("5b 损坏文件被按登记重写",
      hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5c 遗留半写临时文件
tmp_orphan = os.path.join(down_dir, ".tmp-publish-deadbeef.csv")
open(tmp_orphan, "wb").write(b"partial")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5c 遗留临时文件被清理", not os.path.exists(tmp_orphan))

# 5d 下游目录出现未登记游离文件
stranger = os.path.join(down_dir, "stranger.csv")
open(stranger, "wb").write(b"x")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5d 游离文件被移入 quarantine",
      not os.path.exists(stranger)
      and any(f.endswith("stranger.csv")
              for f in os.listdir(os.path.join(data_dir, "quarantine"))))

# 5e 模拟“文件已改名但事务未提交”：文件内容比登记新，重启应回滚到登记版本
with app3.storage._connect() as conn:
    old_csv = app3.storage._render_version(conn, reg["current_version"])
newer = render_csv([{"sample_no": "ZX", "analyte": "氨氮", "concentration": 9.9}])
app3.storage._atomic_write(newer)
check("预置：文件已被换成未登记的新版本",
      open(live, "rb").read().decode() != old_csv)
app4 = create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5e 改名后未提交 → 恢复为登记的完整旧版",
      open(live, "rb").read().decode() == old_csv)
events = app4.storage.recent_events(50)
check("恢复动作写入事件日志", any(e["kind"] == "recover" for e in events))

print("== 6. 会话取消 ==")
r = upload(client, "batch_v1.csv", "CANCEL-ME")
tok = re.search(rb'/publish/([0-9a-f]+)', r.data).group(1).decode()
v_before = app.storage.current_registry()["current_version"]
client.post(f"/cancel/{tok}")
check("取消后发布被拒绝（409 且会话 cancelled）",
      client.post(f"/publish/{tok}").status_code == 409
      and app.storage.get_session(tok)["status"] == "cancelled")
check("取消不影响当前版本",
      app.storage.current_registry()["current_version"] == v_before)

print("== 7. 游离文件隔离：跨挂载、同秒同名、失败续处理 ==")
qdir = os.path.join(data_dir, "quarantine")

# 7a 数据目录与下游目录位于不同挂载：跨目录 rename 一律抛 EXDEV
real_replace = os.replace


def _exdev_replace(src, dst):
    if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(dst)):
        raise OSError(errno.EXDEV, "Invalid cross-device link", src)
    return real_replace(src, dst)


stray_cross = os.path.join(down_dir, "stray_cross.csv")
with open(stray_cross, "wb") as fh:
    fh.write(b"cross-device-content")
os.replace = _exdev_replace
try:
    create_app(data_dir=data_dir, downstream_dir=down_dir)  # 不应因 EXDEV 崩溃
finally:
    os.replace = real_replace
copies = [f for f in os.listdir(qdir) if f.endswith("stray_cross.csv")]
check("7a 跨挂载时游离文件仍可隔离（复制而非跨设备改名）",
      not os.path.exists(stray_cross) and len(copies) == 1, str(copies))
check("7a 隔离件内容完整",
      len(copies) == 1
      and open(os.path.join(qdir, copies[0]), "rb").read() == b"cross-device-content")

# 7b 同一秒隔离同名文件：每份原件独立留存，互不覆盖
for i in (1, 2):
    with open(os.path.join(down_dir, "dup.csv"), "wb") as fh:
        fh.write(f"dup-content-{i}".encode())
    create_app(data_dir=data_dir, downstream_dir=down_dir)
dups = sorted(f for f in os.listdir(qdir) if f.endswith("dup.csv"))
check("7b 同秒同名游离文件各自独立留存", len(dups) == 2, str(dups))
contents = sorted(open(os.path.join(qdir, f), "rb").read() for f in dups)
check("7b 两份隔离件内容完整、未互相覆盖",
      contents == [b"dup-content-1", b"dup-content-2"], str(contents))

# 7c 隔离复制失败：原件保留、启动不崩、日志如实、不留半成品、下次启动继续处理
stray_fragile = os.path.join(down_dir, "fragile.csv")
with open(stray_fragile, "wb") as fh:
    fh.write(b"keep-me")
real_copy = shutil.copyfileobj


def _boom_copy(*args, **kwargs):
    raise OSError("No space left on device (simulated)")


shutil.copyfileobj = _boom_copy
startup_ok = True
try:
    app_fragile = create_app(data_dir=data_dir, downstream_dir=down_dir)
except Exception:
    startup_ok = False
finally:
    shutil.copyfileobj = real_copy
check("7c 隔离复制失败不阻碍网页启动", startup_ok)
check("7c 原件未完整留存前不被删除",
      os.path.exists(stray_fragile)
      and open(stray_fragile, "rb").read() == b"keep-me")
check("7c 半成品不作为完整隔离件残留",
      not [f for f in os.listdir(qdir) if f.startswith(".tmp-quarantine-")])
frag_events = ([e for e in app_fragile.storage.recent_events(50)
                if "fragile.csv" in e["detail"]] if startup_ok else [])
check("7c 失败如实记录、不谎报已隔离",
      frag_events and "隔离失败" in frag_events[0]["detail"]
      and "已完整隔离" not in frag_events[0]["detail"],
      frag_events[0]["detail"] if frag_events else "无日志")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("7c 故障恢复后再次启动继续处理并成功隔离",
      not os.path.exists(stray_fragile)
      and any(f.endswith("fragile.csv") for f in os.listdir(qdir)))

# 7d 上次中断留下的隔离半成品：启动时清理，且不被当成完整隔离件
half = os.path.join(qdir, ".tmp-quarantine-half")
with open(half, "wb") as fh:
    fh.write(b"partial")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("7d 未完成的隔离半成品被清理", not os.path.exists(half))

# 7e 恢复日志可定位到对应隔离件
stray_log = os.path.join(down_dir, "logme.csv")
with open(stray_log, "wb") as fh:
    fh.write(b"log-me")
app_log = create_app(data_dir=data_dir, downstream_dir=down_dir)
log_events = [e for e in app_log.storage.recent_events(50)
              if e["kind"] == "recover" and "logme.csv" in e["detail"]
              and "已完整隔离" in e["detail"]]
m = re.search(r"quarantine/(\S+)", log_events[0]["detail"]) if log_events else None
check("7e 恢复日志记录隔离件名且文件确实存在",
      m is not None and os.path.exists(os.path.join(qdir, m.group(1))),
      log_events[0]["detail"] if log_events else "无日志")

# 7f 正式 CSV 仍按版本登记恢复并可供下游读取（上述隔离操作不影响正式文件）
reg = app_log.storage.current_registry()
check("7f 正式 CSV 与登记版本一致、可正常读取",
      open(live, "rb").read()
      and hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
