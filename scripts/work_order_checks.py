"""设备校准工单测试：

管理员凭管理密钥登记唯一工单号/设备标识/委托人/带时区计划校准半开时段
（缺/错密钥 401、时段无效 422、工单号幂等：同号同内容 200 且逐字段等于
首次、同号异内容 409、并发同号同内容恰 1×201 其余 200）；取消仅 PENDING
可行（重复取消 200 返回首次取消时刻、已结清取消 409、已取消结清 409）；
结清只接受设备一致、校准时刻落在计划半开区间（半开边界 from 含/to 不含）、
当前 ACTIVE 且 PASS 的普通证书（基准证书/设备不一致/超时段/非 ACTIVE/
非 PASS 分别 422，证书/工单缺失 404），一张证书不得结清两个工单（预检与
并发双绑兜底均 409）；同工单同证书重试 200 返回原结果（settled_at 固定），
改绑其他证书 409；证书日后撤销不倒退工单状态、查询返回绑定证书当前状态；
并发结清×取消恰一个定稿、并发同证书双绑恰一个成功；未知工单 404；
既有签发/撤销接口契约与根索引端点清单不变。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/work_order_checks.py [base_url] [management_key]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
MK = sys.argv[2] if len(sys.argv) > 2 else "caltrace-delivery-admin"
results = []


def call(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Management-Key"] = key
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


UTC = timezone.utc
WIN_S = datetime(2026, 6, 1, tzinfo=UTC)
WIN_E = datetime(2026, 6, 20, tzinfo=UTC)


def iso(d):
    return d.isoformat()


def issue_ref(no, device, vfrom, vto):
    body = {"cert_no": no, "device_id": device, "valid_from": iso(vfrom),
            "valid_to": iso(vto), "calibrated_at": iso(vfrom)}
    return call("POST", "/api/v1/reference-certificates", body)


def issue_cal(no, device, parent, calibrated, conclusion="PASS"):
    body = {
        "cert_no": no, "device_id": device,
        "valid_from": iso(calibrated - timedelta(days=1)),
        "valid_to": iso(calibrated + timedelta(days=180)),
        "calibrated_at": iso(calibrated),
        "conclusion": conclusion, "parent_cert_no": parent,
    }
    return call("POST", "/api/v1/certificates", body)


def issue_cal_window(no, device, parent, calibrated, vfrom, vto, conclusion="PASS"):
    """显式指定有效期窗口（用于同设备多张证书构造首尾相接的不重叠窄窗口）。"""
    body = {
        "cert_no": no, "device_id": device,
        "valid_from": iso(vfrom), "valid_to": iso(vto),
        "calibrated_at": iso(calibrated),
        "conclusion": conclusion, "parent_cert_no": parent,
    }
    return call("POST", "/api/v1/certificates", body)


def register(wo, device, client, sfrom, sto, key=MK):
    return call("POST", "/api/v1/calibration-work-orders",
                {"work_order_no": wo, "device_id": device, "client": client,
                 "scheduled_from": iso(sfrom), "scheduled_to": iso(sto)}, key=key)


def settle(wo, cert, key=MK):
    return call("POST", f"/api/v1/calibration-work-orders/{wo}/settle",
                {"cert_no": cert}, key=key)


def cancel(wo, key=MK):
    return call("POST", f"/api/v1/calibration-work-orders/{wo}/cancel", None, key=key)


def get_wo(wo, key=MK):
    return call("GET", f"/api/v1/calibration-work-orders/{wo}", None, key=key)


# 三条相互独立的追溯链：
#   REF-WO-001 主链（全程 ACTIVE 用于正常/设备/时段/边界/FAIL 等校验，结尾才撤销）
#   REF-WO-002 用于“非 ACTIVE”：结清准备完成后撤销使其下游级联 INVALID
#   REF-WO-003 并发链（共享证书双绑、结清×取消竞态），结尾撤销验证不回退
print("== 准备：三条独立基准链与各场景普通证书 ==")
st, _ = issue_ref("REF-WO-001", "DEV-STD-WO",
                  datetime(2025, 1, 1, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC))
check("基准 REF-WO-001", st == 201, f"{st}")
st, _ = issue_ref("REF-WO-002", "DEV-STD-WO2",
                  datetime(2025, 1, 1, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC))
check("基准 REF-WO-002", st == 201, f"{st}")
st, _ = issue_ref("REF-WO-003", "DEV-STD-WO3",
                  datetime(2025, 1, 1, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC))
check("基准 REF-WO-003", st == 201, f"{st}")

st, _ = issue_cal("WO-CERT-OK", "DEV-WO-D1", "REF-WO-001", datetime(2026, 6, 10, 12, tzinfo=UTC))
check("WO-CERT-OK (D1,06-10 PASS)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-FAIL", "DEV-WO-D2", "REF-WO-001",
                  datetime(2026, 6, 11, tzinfo=UTC), conclusion="FAIL")
check("WO-CERT-FAIL (D2 FAIL)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-OTHERDEV", "DEV-WO-D3", "REF-WO-001", datetime(2026, 6, 11, tzinfo=UTC))
check("WO-CERT-OTHERDEV (D3)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-BFROM", "DEV-WO-D4", "REF-WO-001", datetime(2026, 6, 1, tzinfo=UTC))
check("WO-CERT-BFROM (D4, 校准==06-01T00:00)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-BTO", "DEV-WO-D5", "REF-WO-001", datetime(2026, 6, 5, tzinfo=UTC))
check("WO-CERT-BTO (D5, 校准==06-05T00:00)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-INV", "DEV-WO-D7", "REF-WO-002", datetime(2026, 6, 13, tzinfo=UTC))
check("WO-CERT-INV (D7, 待级联失效)", st == 201, f"{st}")
st, _ = issue_cal("WO-CERT-SHARE", "DEV-WO-D8", "REF-WO-003", datetime(2026, 6, 14, tzinfo=UTC))
check("WO-CERT-SHARE (D8, 一证书两工单)", st == 201, f"{st}")

# ---------------------------------------------------------------------------
# 1. 管理密钥
# ---------------------------------------------------------------------------
print("== 1. 管理密钥校验 ==")
st, b = register("WO-KEY-MISS", "DEV-WO-D1", "客户", WIN_S, WIN_E, key=None)
check("登记缺密钥 -> 401", st == 401 and err_code(b) == "invalid_management_key", f"{st}")
st, b = register("WO-KEY-BAD", "DEV-WO-D1", "客户", WIN_S, WIN_E, key="nope")
check("登记错密钥 -> 401", st == 401 and err_code(b) == "invalid_management_key", f"{st}")
st, b = settle("WO-KEY-BAD", "WO-CERT-OK", key="nope")
check("结清错密钥 -> 401", st == 401 and err_code(b) == "invalid_management_key", f"{st}")
st, b = cancel("WO-KEY-BAD", key="nope")
check("取消错密钥 -> 401", st == 401 and err_code(b) == "invalid_management_key", f"{st}")
st, b = get_wo("WO-NOT-EXIST", key="nope")
check("查询错密钥 -> 401（先验密钥再查存在性）",
      st == 401 and err_code(b) == "invalid_management_key", f"{st}")

# ---------------------------------------------------------------------------
# 2. 时段无效
# ---------------------------------------------------------------------------
print("== 2. 计划时段无效（半开区间，起点须早于终点） ==")
st, b = register("WO-BADWIN-1", "DEV-WO-D1", "客户",
                 datetime(2026, 6, 10, tzinfo=UTC), datetime(2026, 6, 10, tzinfo=UTC))
check("起点==终点 -> 422 invalid_schedule_window",
      st == 422 and err_code(b) == "invalid_schedule_window", f"{st} {b}")
st, b = register("WO-BADWIN-2", "DEV-WO-D1", "客户",
                 datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 10, tzinfo=UTC))
check("起点晚于终点 -> 422", st == 422 and err_code(b) == "invalid_schedule_window", f"{st}")

# ---------------------------------------------------------------------------
# 3. 登记成功与工单号幂等
# ---------------------------------------------------------------------------
print("== 3. 登记与工单号幂等 ==")
WO1 = "WO-REG-001"
st, b1 = register(WO1, "DEV-WO-D1", "客户甲", WIN_S, WIN_E)
check("首次登记 -> 201 PENDING 无定稿字段",
      st == 201 and b1["status"] == "PENDING" and b1["cert_no"] is None
      and b1["settled_at"] is None and b1["cancelled_at"] is None
      and b1["certificate"] is None and b1["created_at"], f"{st} {b1}")
st, b = register(WO1, "DEV-WO-D1", "客户甲", WIN_S, WIN_E)
check("同号同内容重试 -> 200 且响应逐字段等于首次", st == 200 and b == b1, f"{st}")
tz8 = timezone(timedelta(hours=8))
st, b = register(WO1, "DEV-WO-D1", "客户甲", WIN_S.astimezone(tz8), WIN_E.astimezone(tz8))
check("同时刻不同时区偏移表示 -> 视为同内容 200", st == 200 and b == b1, f"{st}")
st, b = register(WO1, "DEV-WO-D1", "客户甲", WIN_S, datetime(2026, 6, 21, tzinfo=UTC))
check("异时段 -> 409 work_order_conflict",
      st == 409 and err_code(b) == "work_order_conflict", f"{st}")
st, b = register(WO1, "DEV-WO-D1", "客户乙", WIN_S, WIN_E)
check("异委托人 -> 409", st == 409 and err_code(b) == "work_order_conflict", f"{st}")
st, b = register(WO1, "DEV-OTHER", "客户甲", WIN_S, WIN_E)
check("异设备 -> 409", st == 409 and err_code(b) == "work_order_conflict", f"{st}")
st, b = get_wo(WO1)
check("查询 PENDING 工单登记内容",
      st == 200 and b["status"] == "PENDING" and b["device_id"] == "DEV-WO-D1"
      and b["client"] == "客户甲" and b["scheduled_from"].startswith("2026-06-01T00:00:00")
      and b["scheduled_to"].startswith("2026-06-20T00:00:00"), f"{st} {b}")
st, b = get_wo("WO-NOT-EXIST-999")
check("查询未知工单 -> 404 work_order_not_found",
      st == 404 and err_code(b) == "work_order_not_found", f"{st}")

# 并发同号同内容：恰 1×201 其余 200，created_at 一致
WOC = "WO-CONC-REG"
out = []
out_lock = threading.Lock()
def do_register():
    stx, bx = register(WOC, "DEV-WO-D1", "并发客户", WIN_S, WIN_E)
    with out_lock:
        out.append((stx, bx.get("created_at"), bx.get("status")))
ts = [threading.Thread(target=do_register) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
codes = sorted(s for s, _, _ in out)
check("并发同号同内容：恰 1×201 + 7×200", codes == sorted([201] + [200] * 7), f"{codes}")
check("并发重放 created_at/status 全同",
      len({c for _, c, _ in out}) == 1 and {s for _, _, s in out} == {"PENDING"},
      f"{out}")

# ---------------------------------------------------------------------------
# 4. 结清成功 + 同工单同证书重试 + 改绑/取消冲突
# ---------------------------------------------------------------------------
print("== 4. 结清成功与幂等/冲突 ==")
WOS = "WO-SETTLE-001"
st, _ = register(WOS, "DEV-WO-D1", "客户甲", WIN_S, WIN_E)
st, b = settle(WOS, "WO-CERT-OK")
check("合格证书结清 -> 201 SETTLED，含绑定证书摘要",
      st == 201 and b["status"] == "SETTLED" and b["cert_no"] == "WO-CERT-OK"
      and b["settled_at"] and b["cancelled_at"] is None
      and b["certificate"]["status"] == "ACTIVE" and b["certificate"]["conclusion"] == "PASS"
      and b["certificate"]["kind"] == "CALIBRATION"
      and b["certificate"]["device_id"] == "DEV-WO-D1", f"{st} {b}")
first_settled = b["settled_at"]
st, b = settle(WOS, "WO-CERT-OK")
check("同工单同证书重试 -> 200，绑定/结清时刻固定",
      st == 200 and b["cert_no"] == "WO-CERT-OK" and b["settled_at"] == first_settled,
      f"{st}")
st, b = settle(WOS, "WO-CERT-OTHERDEV")
check("改绑其他证书 -> 409 work_order_rebind_conflict",
      st == 409 and err_code(b) == "work_order_rebind_conflict"
      and b["error"]["details"]["bound_cert_no"] == "WO-CERT-OK"
      and b["error"]["details"]["requested_cert_no"] == "WO-CERT-OTHERDEV", f"{st} {b}")
st, b = cancel(WOS)
check("取消已结清 -> 409 work_order_already_settled",
      st == 409 and err_code(b) == "work_order_already_settled", f"{st}")
st, b = settle("WO-NOT-EXIST-999", "WO-CERT-OK")
check("结清未知工单 -> 404 work_order_not_found",
      st == 404 and err_code(b) == "work_order_not_found", f"{st}")

# ---------------------------------------------------------------------------
# 5. 结清拒绝路径
# ---------------------------------------------------------------------------
print("== 5. 结清各类拒绝 ==")
WOR = "WO-REF-001"
register(WOR, "DEV-STD-WO3", "客户", WIN_S, WIN_E)
st, b = settle(WOR, "REF-WO-003")
check("基准证书结清 -> 422 settle_cert_not_calibration",
      st == 422 and err_code(b) == "settle_cert_not_calibration", f"{st} {b}")
st, g = get_wo(WOR)
check("被拒后工单仍 PENDING 未绑定", g["status"] == "PENDING" and g["cert_no"] is None, f"{g}")
st, b = settle(WOR, "WO-CERT-MISSING-X")
check("证书不存在 -> 404 cert_not_found",
      st == 404 and err_code(b) == "cert_not_found", f"{st} {b}")

WODM = "WO-DEVMISMATCH"
register(WODM, "DEV-WO-D1", "客户甲", WIN_S, WIN_E)
st, b = settle(WODM, "WO-CERT-OTHERDEV")
check("设备不一致 -> 422 settle_device_mismatch（details 双标识）",
      st == 422 and err_code(b) == "settle_device_mismatch"
      and b["error"]["details"]["work_order_device_id"] == "DEV-WO-D1"
      and b["error"]["details"]["cert_device_id"] == "DEV-WO-D3", f"{st} {b}")

WOOUT = "WO-OUTOFSCHED"
register(WOOUT, "DEV-WO-D1", "客户甲",
         datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 10, tzinfo=UTC))
st, b = settle(WOOUT, "WO-CERT-OK")
check("校准时刻不在计划时段 -> 422 settle_out_of_schedule",
      st == 422 and err_code(b) == "settle_out_of_schedule"
      and b["error"]["details"]["calibrated_at"].startswith("2026-06-10"), f"{st} {b}")
# 被各类 422 拒绝的工单都应仍 PENDING 且可用合格证书完成结清
st, b = settle(WOOUT, "WO-CERT-OK")  # 仍然超时段，保持 PENDING（设备一致 D1）
check("超时段工单重复尝试仍 422 且未结",
      st == 422 and err_code(b) == "settle_out_of_schedule", f"{st}")

# 半开边界
WOBF = "WO-BOUND-FROM"
register(WOBF, "DEV-WO-D4", "客户", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 5, tzinfo=UTC))
st, b = settle(WOBF, "WO-CERT-BFROM")
check("校准时刻==scheduled_from（起点含） -> 201",
      st == 201 and b["status"] == "SETTLED", f"{st} {b}")
WOBT = "WO-BOUND-TO"
register(WOBT, "DEV-WO-D5", "客户", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 5, tzinfo=UTC))
st, b = settle(WOBT, "WO-CERT-BTO")
check("校准时刻==scheduled_to（终点不含） -> 422",
      st == 422 and err_code(b) == "settle_out_of_schedule", f"{st} {b}")

# FAIL（证书当前 ACTIVE）
WOFAIL = "WO-FAIL"
register(WOFAIL, "DEV-WO-D2", "客户", WIN_S, WIN_E)
st, b = settle(WOFAIL, "WO-CERT-FAIL")
check("ACTIVE 但 FAIL -> 422 settle_cert_not_passed",
      st == 422 and err_code(b) == "settle_cert_not_passed"
      and b["error"]["details"]["conclusion"] == "FAIL", f"{st} {b}")
st, g = get_wo(WOFAIL)
check("FAIL 被拒后仍 PENDING", g["status"] == "PENDING", f"{g['status']}")

# 非 ACTIVE：撤销 REF-WO-002 使其下游 WO-CERT-INV 级联失效
st, _ = call("POST", "/api/v1/certificates/REF-WO-002/revoke",
             {"reason": "使 D7 证书级联失效", "operator": "测试"})
check("撤销 REF-WO-002", st == 200, f"{st}")
st, cinv = call("GET", "/api/v1/certificates/WO-CERT-INV")
check("WO-CERT-INV 已 INVALID", cinv["status"] == "INVALID", f"{cinv.get('status')}")
WOINA = "WO-NOTACTIVE"
register(WOINA, "DEV-WO-D7", "客户", WIN_S, WIN_E)
st, b = settle(WOINA, "WO-CERT-INV")
check("非 ACTIVE 证书结清 -> 422 settle_cert_not_active",
      st == 422 and err_code(b) == "settle_cert_not_active"
      and b["error"]["details"]["status"] == "INVALID", f"{st} {b}")

# 一张证书不得结清两个工单
WOSH1, WOSH2 = "WO-SHARE-1", "WO-SHARE-2"
register(WOSH1, "DEV-WO-D8", "客户一", WIN_S, WIN_E)
register(WOSH2, "DEV-WO-D8", "客户二", WIN_S, WIN_E)
st, b = settle(WOSH1, "WO-CERT-SHARE")
check("工单1 先结清成功", st == 201 and b["status"] == "SETTLED", f"{st} {b}")
st, b = settle(WOSH2, "WO-CERT-SHARE")
check("工单2 再用同证书 -> 409 cert_already_settled（点名首工单）",
      st == 409 and err_code(b) == "cert_already_settled"
      and b["error"]["details"]["settled_work_order_no"] == WOSH1, f"{st} {b}")
st, g = get_wo(WOSH2)
check("被拒工单2 仍 PENDING（不预占）", g["status"] == "PENDING" and g["cert_no"] is None, f"{g}")

# ---------------------------------------------------------------------------
# 6. 取消
# ---------------------------------------------------------------------------
print("== 6. 取消 ==")
WOC1 = "WO-CANCEL-001"
register(WOC1, "DEV-WO-D1", "客户甲", WIN_S, WIN_E)
st, b = cancel(WOC1)
check("首次取消 -> 201 CANCELLED",
      st == 201 and b["status"] == "CANCELLED" and b["cancelled_at"] and b["cert_no"] is None,
      f"{st} {b}")
cat = b["cancelled_at"]
st, b = cancel(WOC1)
check("重复取消 -> 200 返回首次取消时刻",
      st == 200 and b["status"] == "CANCELLED" and b["cancelled_at"] == cat, f"{st}")
st, b = settle(WOC1, "WO-CERT-SHARE")
check("已取消再结清 -> 409 work_order_cancelled",
      st == 409 and err_code(b) == "work_order_cancelled", f"{st} {b}")
st, b = cancel("WO-NOT-EXIST-999")
check("取消未知工单 -> 404", st == 404 and err_code(b) == "work_order_not_found", f"{st}")

# ---------------------------------------------------------------------------
# 7. 并发：同一证书结清两个不同工单
# ---------------------------------------------------------------------------
print("== 7. 并发一证书双绑 ==")
st, _ = issue_cal("WO-CERT-CONC1", "DEV-WO-D11", "REF-WO-003", datetime(2026, 6, 17, tzinfo=UTC))
check("签发并发共享证书", st == 201, f"{st}")
A, B2 = "WO-CONC-S1", "WO-CONC-S2"
register(A, "DEV-WO-D11", "客户", WIN_S, WIN_E)
register(B2, "DEV-WO-D11", "客户", WIN_S, WIN_E)
conc = []
clock = threading.Lock()
def do_settle(wo):
    sx, bx = settle(wo, "WO-CERT-CONC1")
    with clock:
        conc.append((wo, sx, err_code(bx) if isinstance(bx, dict) and "error" in bx else bx.get("status")))
ths = [threading.Thread(target=do_settle, args=(A,)), threading.Thread(target=do_settle, args=(B2,))]
[t.start() for t in ths]; [t.join() for t in ths]
succ = [r for r in conc if r[1] == 201]
cf = [r for r in conc if r[1] == 409 and r[2] == "cert_already_settled"]
check("并发双绑：恰 1×201 + 1×409 cert_already_settled",
      len(succ) == 1 and len(cf) == 1, f"{conc}")
winner = succ[0][0]; loser = B2 if winner == A else A
_, gw = get_wo(winner); _, gl = get_wo(loser)
check("胜方 SETTLED 绑定 / 负方 PENDING 未绑定",
      gw["status"] == "SETTLED" and gw["cert_no"] == "WO-CERT-CONC1" and gl["status"] == "PENDING",
      f"{winner}={gw['status']} {loser}={gl['status']}")
# 负方未被定稿：可正常取消（不预占证书、不影响其他工单/证书流转）
st, b = cancel(loser)
check("负方工单仍可取消（被 409 的结清不留任何定稿）",
      st == 201 and b["status"] == "CANCELLED", f"{st} {b}")
# 同时另一台设备的工单与证书照常流转（工单体系不预占、不阻断签发与结清）
st, _ = issue_cal("WO-CERT-CONC2", "DEV-WO-D11B", "REF-WO-003", datetime(2026, 6, 18, tzinfo=UTC))
check("另一台设备照常签发证书", st == 201, f"{st}")
C = "WO-CONC-S3"
register(C, "DEV-WO-D11B", "客户三", WIN_S, WIN_E)
st, b = settle(C, "WO-CERT-CONC2")
check("新设备工单用新证书结清成功（不受前次双绑冲突影响）",
      st == 201 and b["status"] == "SETTLED" and b["cert_no"] == "WO-CERT-CONC2", f"{st} {b}")

# 10 路同时抢一张证书：恰 1 个 SETTLED
st, _ = issue_cal("WO-CERT-RUSH", "DEV-WO-D13", "REF-WO-003", datetime(2026, 6, 17, 12, tzinfo=UTC))
check("签发 10 路争抢证书", st == 201, f"{st}")
rush = []
def rush_settle(wo):
    sx, bx = settle(wo, "WO-CERT-RUSH")
    with clock:
        rush.append(sx)
rts = []
for i in range(10):
    w = f"WO-RUSH-{i}"
    register(w, "DEV-WO-D13", "客户", WIN_S, WIN_E)
    rts.append(threading.Thread(target=rush_settle, args=(w,)))
[t.start() for t in rts]; [t.join() for t in rts]
check("10 路抢一张证书：恰 1×201，其余全部 409",
      sorted(rush).count(201) == 1 and all(x in (201, 409) for x in rush),
      f"{sorted(rush)}")

# ---------------------------------------------------------------------------
# 8. 并发：结清 × 取消 同一工单，多轮
# ---------------------------------------------------------------------------
print("== 8. 并发结清 × 取消（恰一个定稿） ==")
race_ok = True
race_detail = ""
n_settle_wins = 0
n_cancel_wins = 0
for i in range(12):
    w = f"WO-RACEX-{i}"
    # 每轮使用一张独立证书：已结清轮的证书被唯一索引占用，未结清轮互不影响。
    # 同设备 ACTIVE 有效期排他约束要求各证书有效期互不重叠——为每张证书构造
    # 首尾相接、各含自身校准时刻的窄窗口（半开相接不重叠）。工单计划时段
    # 覆盖全部竞态证书的校准时刻，使“结清”与“取消”在每轮都有资格胜出。
    base = datetime(2026, 8, 1, tzinfo=UTC)
    cfrom = base + timedelta(hours=i)
    calibrated = cfrom + timedelta(minutes=30)
    cto = base + timedelta(hours=i + 1)
    register(w, "DEV-WO-D12", "客户",
             datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    cert = f"WO-CERT-RACEX-{i}"
    st, _ = issue_cal_window(cert, "DEV-WO-D12", "REF-WO-003",
                             calibrated, cfrom, cto)
    if st != 201:
        race_ok = False
        race_detail = f"round {i}: 竞态证书签发失败 {st}"
        check("竞态证书签发", False, race_detail)
        break
    ro = []
    def rs(w=w, cert=cert):
        sx, bx = settle(w, cert)
        ro.append(("settle", sx, bx.get("status")))
    def rc(w=w):
        sx, bx = cancel(w)
        ro.append(("cancel", sx, bx.get("status")))
    t1 = threading.Thread(target=rs); t2 = threading.Thread(target=rc)
    t1.start(); t2.start(); t1.join(); t2.join()
    _, g = get_wo(w)
    s_ok = [r for r in ro if r[0] == "settle" and r[1] == 201]
    c_ok = [r for r in ro if r[0] == "cancel" and r[1] == 201]
    if s_ok:
        n_settle_wins += 1
    if c_ok:
        n_cancel_wins += 1
    # 结清与取消不能同时成功；最终态须与胜出操作一致
    if (s_ok and c_ok) or (s_ok and g["status"] != "SETTLED") or (c_ok and g["status"] != "CANCELLED") \
       or (not s_ok and not c_ok):
        race_ok = False
        race_detail = f"round {i}: {ro} final={g['status']}"
        check(f"第{i+1}轮 结清×取消 恰一定稿", False, race_detail)
        break
check(f"12 轮结清×取消全部恰一个定稿且最终态一致（结清胜 {n_settle_wins} 轮 / 取消胜 {n_cancel_wins} 轮）",
      race_ok, race_detail)

# ---------------------------------------------------------------------------
# 9. 撤销不回退已结清工单 + 契约
# ---------------------------------------------------------------------------
print("== 9. 撤销不倒退 / 根索引契约 ==")
# WOS 绑定 WO-CERT-OK（REF-WO-001 链）；撤销基准后工单仍 SETTLED、证书 INVALID
_, before = get_wo(WOS)
st, _ = call("POST", "/api/v1/certificates/REF-WO-001/revoke",
             {"reason": "主链收尾撤销，级联失效", "operator": "测试"})
check("撤销 REF-WO-001", st == 200, f"{st}")
_, after = get_wo(WOS)
check("撤销后 WOS 仍 SETTLED、绑定固定、证书当前 INVALID",
      after["status"] == "SETTLED" and after["cert_no"] == "WO-CERT-OK"
      and after["settled_at"] == before["settled_at"]
      and after["certificate"]["status"] == "INVALID",
      f"after status={after['certificate']['status']}")
# 已结清工单的同证书重放仍返回原结果（仅证书状态随当前）
st, b = settle(WOS, "WO-CERT-OK")
check("撤销后同工单同证书重放 -> 200 原结果（证书状态当前 INVALID）",
      st == 200 and b["settled_at"] == first_settled
      and b["certificate"]["status"] == "INVALID", f"{st}")

# REF-WO-003 链撤销后并发链上的已结清工单同样不回退
call("POST", "/api/v1/certificates/REF-WO-003/revoke", {"reason": "并发链收尾", "operator": "测试"})
_, gw3 = get_wo(WOSH1)
check("并发链撤销后已结清工单不回退",
      gw3["status"] == "SETTLED" and gw3["certificate"]["status"] == "INVALID",
      f"{gw3['status']}/{gw3['certificate']['status']}")

st, idx = call("GET", "/")
eps = idx.get("endpoints", [])
needed = [
    "POST /api/v1/calibration-work-orders",
    "POST /api/v1/calibration-work-orders/{work_order_no}/cancel",
    "POST /api/v1/calibration-work-orders/{work_order_no}/settle",
    "GET  /api/v1/calibration-work-orders/{work_order_no}",
]
check("根索引含四个工单端点", all(n in eps for n in needed),
      f"missing={[n for n in needed if n not in eps]}")
check("根索引保留既有端点",
      "POST /api/v1/certificates" in eps and "POST /api/v1/certificate-batches" in eps
      and "GET  /api/v1/revocations/{batch_id}/archive-impact" in eps, "既有端点缺失")

# 登记不预占：工单存在不影响证书签发/撤销（上面已大量签发；再核 PENDING 工单
# 在其计划设备上证书签发完全不受限——WO-CERT-* 都是在工单登记前后混排签发的）
st, b = call("GET", "/api/v1/certificates/WO-CERT-OK")
check("既有证书查询契约不变（历史内容可查，状态 INVALID）",
      st == 200 and b["cert_no"] == "WO-CERT-OK" and b["status"] == "INVALID", f"{st} {b.get('status')}")

fail = [r for r in results if not r[1]]
print()
print(f"结果: PASS={len(results)-len(fail)} FAIL={len(fail)}")
for n, _, d in fail:
    print("  FAIL:", n, "::", d)
sys.exit(1 if fail else 0)
