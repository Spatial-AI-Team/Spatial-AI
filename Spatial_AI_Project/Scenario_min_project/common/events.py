# vocab-literal-ok — CLASS_MAP(:32)은 원시 데이터셋 라벨 문자열(automobile/heavy_truck 등,
# vocab 값 아님) -> taxonomy object_type 번역표라 리터럴이 불가피함(기존 코드, pre-commit
# 훅 신설 이전부터 존재).
"""egomotion 기반 동적 이벤트 검출 (when 을 GT 로 산출).

egomotion parquet 은 비디오(20s)보다 긴 전체 로그(~86-140s)를 담으므로,
반드시 카메라 timestamps 범위로 잘라 비디오 시각(초)에 매핑한 뒤 이벤트를 찾는다.
검출 종류: decelerate / accelerate / stop / turn_left / turn_right.
"""

import math
from pathlib import Path

from config import (
    CAMERA,
    DATASET_ROOT,
    EVENT_ACCEL_AX,
    EVENT_MERGE_SEC,
    EVENT_MIN_SEC,
    EVENT_STOP_SPEED,
    EVENT_TURN_YAWRATE,
    EVENT_TURN_HEADING,
    EVENT_UTURN_HEADING,
    EVENT_LC_HEADING_MAX,
    EVENT_LC_LAT_MIN,
    EVENT_LC_LAT_MAX,
    OBST_AHEAD_M,
    OBST_LANE_HALF,
    OBST_MIN_SEC,
)
from thresholds import EVENT_DECEL_AX, OBST_CUTIN_Y

# obstacle label_class → 어휘 object_type
CLASS_MAP = {
    "automobile": "vehicle", "car": "vehicle", "van": "vehicle",
    "heavy_truck": "large_vehicle", "truck": "large_vehicle",
    "trailer": "large_vehicle", "bus": "large_vehicle",
    "person": "pedestrian", "pedestrian": "pedestrian",
    "rider": "bicycle_micromobility", "cyclist": "bicycle_micromobility",
    "motorcycle": "motorcycle",
    "protruding_object": "other", "stroller": "other",
}


def _yaw(qw, qx, qy, qz):
    """쿼터니언 → yaw(rad)."""
    import numpy as np
    return np.arctan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def load_egomotion_clip(clip_id: str, camera: str = CAMERA) -> dict:
    """egomotion 을 비디오 범위로 잘라 (t_s, speed, ax, yaw_rate) 반환.

    반환: {t: np[초], speed, ax, yaw_rate, dur, ok}. 파일 없으면 ok=False.
    """
    import numpy as np
    import pyarrow.parquet as pq

    import paths as P
    ego_p = P.egomotion_path(clip_id)
    if not ego_p.exists():
        return {"ok": False, "reason": "egomotion/camera parquet 없음"}

    # 카메라 timestamp 소스: 구 mp4 데이터셋은 camera_ts()(timestamps.parquet,
    # 컬럼 "timestamp"), 신규 프레임 갤러리(nvidia/<clip>/)는 camera_ts() 파일 자체가
    # 없고 index.parquet(컬럼 "timestamp_us")가 후신이다. dataset.is_frame_gallery()와
    # 동일하게 디스크 실측(존재 여부)으로 분기 — clip_id 이름 추정 금지.
    cam_p = P.camera_ts(clip_id)
    if cam_p.exists():
        cam_col = "timestamp"
        cam_src = cam_p
    else:
        cam_col = "timestamp_us"
        cam_src = P.index_path(clip_id)
    if not cam_src.exists():
        return {"ok": False, "reason": "egomotion/camera parquet 없음"}

    cam = pq.read_table(cam_src, columns=[cam_col]).to_pydict()[cam_col]
    cam = np.array(cam)
    t0, t1 = int(cam.min()), int(cam.max())
    dur = (t1 - t0) / 1e6

    e = pq.read_table(ego_p).to_pydict()
    ego_col = "timestamp" if "timestamp" in e else "timestamp_us"
    ts = np.array(e[ego_col])
    m = (ts >= t0) & (ts <= t1)                      # ★ 비디오 범위로 필터
    if m.sum() < 3:
        return {"ok": False, "reason": "범위 내 egomotion 부족"}
    ts = ts[m]
    tsec = (ts - t0) / 1e6
    vx = np.array(e["vx"])[m]; vy = np.array(e["vy"])[m]
    speed = np.sqrt(vx * vx + vy * vy)
    ax = np.array(e["ax"])[m]
    yaw = _yaw(np.array(e["qw"])[m], np.array(e["qx"])[m],
              np.array(e["qy"])[m], np.array(e["qz"])[m])
    yaw_un = np.unwrap(yaw)
    dt = np.gradient(tsec)
    yaw_rate = np.gradient(yaw_un) / np.where(dt == 0, 1e-6, dt)
    return {"ok": True, "t": tsec, "speed": speed, "ax": ax,
            "yaw_rate": yaw_rate, "yaw": yaw_un - yaw_un[0], "dur": dur}


def _runs(mask, tsec, min_sec):
    """True 연속 구간 → [(i0,i1,t0,t1)] (길이 min_sec 이상)."""
    out = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if tsec[j - 1] - tsec[i] >= min_sec:
                out.append((i, j - 1, float(tsec[i]), float(tsec[j - 1])))
            i = j
        else:
            i += 1
    return out


def _merge_same(events, gap):
    """같은 kind 이고 간격이 gap 이하인 이벤트 병합."""
    events = sorted(events, key=lambda e: (e["kind"], e["t0"]))
    merged = []
    for e in events:
        if merged and merged[-1]["kind"] == e["kind"] and \
                e["t0"] - merged[-1]["t1"] <= gap:
            merged[-1]["t1"] = max(merged[-1]["t1"], e["t1"])
        else:
            merged.append(dict(e))
    return merged


def detect_events(clip_id: str, camera: str = CAMERA, curvature_fn=None, lane_crossing_fn=None) -> dict:
    """egomotion 이벤트 목록 반환. {ok, dur, events:[{t0,t1,center,kind,detail}]}.

    curvature_fn: 선택적 callable(t0, t1) -> 도로 형상이 설명하는 heading 변화량(deg) | None.
    turn/lane_change 판정 시 raw heading에서 이 값을 뺀 잔차를 쓴다(곡선로 오검출 방지).
    미지정(기본값, Stage1/task_selection이 쓰는 경로)이면 raw heading 그대로 — 이 함수는
    map 모듈을 import하지 않으며 map 의존이 전혀 생기지 않는다. Stage2/task_episode 호출부만
    map_lane.road_curvature_over를 감싼 클로저를 넘겨 잔차 보정을 켠다.

    lane_crossing_fn: 선택적 callable(t0, t1) -> 차로 경계선 교차 횟수(int) | None. turn/
    lane_change 이벤트에 `lane_crossings` 필드로 계측값만 기록한다(2026-09-08) — 판정
    로직(45도 임계값)에는 아직 반영하지 않음. 미지정이면 기존 동작과 완전히 동일.
    """
    import numpy as np

    eg = load_egomotion_clip(clip_id, camera)
    if not eg["ok"]:
        return {"ok": False, "reason": eg.get("reason"), "events": []}
    t, speed, ax, yr = eg["t"], eg["speed"], eg["ax"], eg["yaw_rate"]

    ev = []
    for i0, i1, a, b in _runs(ax < EVENT_DECEL_AX, t, EVENT_MIN_SEC):
        ev.append({"kind": "decelerate", "t0": a, "t1": b,
                   "detail": f"ax_min={ax[i0:i1+1].min():.2f} spd {speed[i0]:.1f}->{speed[i1]:.1f}"})
    for i0, i1, a, b in _runs(ax > EVENT_ACCEL_AX, t, EVENT_MIN_SEC):
        ev.append({"kind": "accelerate", "t0": a, "t1": b,
                   "detail": f"ax_max={ax[i0:i1+1].max():.2f} spd {speed[i0]:.1f}->{speed[i1]:.1f}"})
    for i0, i1, a, b in _runs(speed < EVENT_STOP_SPEED, t, EVENT_MIN_SEC):
        ev.append({"kind": "stop", "t0": a, "t1": b,
                   "detail": f"spd_min={speed[i0:i1+1].min():.1f}"})
    # 회전/U턴/차선변경: yaw_rate 크기가 아니라 net heading 변화량으로 분류.
    #   |Δheading| ≥ UTURN → u_turn,  ≥ TURN → turn_left/right,
    #   그 미만인데 측방변위가 차로폭 규모면 lane_change (net heading 복귀).
    yaw = eg["yaw"]                       # 0기준 unwrap heading(rad)
    heading_deg = np.degrees(yaw)
    # yaw 편위 후보 구간(양·음 합쳐 하나로): |yaw_rate|>thr 를 min_sec 이상
    for i0, i1, a, b in _runs(np.abs(yr) > EVENT_TURN_YAWRATE, t, EVENT_MIN_SEC):
        net_raw = heading_deg[i1] - heading_deg[i0]  # net 방향 전환(deg), 보정 전
        # 측방 변위: 초기 heading 기준 lateral = ∫ v·sin(θ-θ0) dt
        seg_t = t[i0:i1 + 1]
        rel = yaw[i0:i1 + 1] - yaw[i0]
        lat = float(np.trapezoid(speed[i0:i1 + 1] * np.sin(rel), seg_t)) \
            if hasattr(np, "trapezoid") else \
            float(np.trapz(speed[i0:i1 + 1] * np.sin(rel), seg_t))
        road_component = None
        if curvature_fn is not None:
            try:
                road_component = curvature_fn(a, b)
            except Exception:
                road_component = None
        net = net_raw - road_component if road_component is not None else net_raw
        curv_source = "map" if road_component is not None else "none"
        amag = abs(net)
        # u_turn은 맵 없이 신뢰 판별 불가 → 회전 방향(net 부호)으로 turn_left/right만.
        if amag >= EVENT_TURN_HEADING:
            kind = "turn_left" if net > 0 else "turn_right"
        elif amag <= EVENT_LC_HEADING_MAX and EVENT_LC_LAT_MIN <= abs(lat) <= EVENT_LC_LAT_MAX:
            kind = "lane_change_left" if lat > 0 else "lane_change_right"
        else:
            continue                                  # 완만한 커브·노이즈 → 태그 안 함
        lane_crossings = None
        if lane_crossing_fn is not None:
            try:
                lane_crossings = lane_crossing_fn(a, b)
            except Exception:
                lane_crossings = None
        ev.append({"kind": kind, "t0": a, "t1": b,
                   "curvature_correction_source": curv_source,
                   "lane_crossings": lane_crossings,
                   "detail": f"net={net:.0f}deg(raw={net_raw:.0f}) lat={lat:.1f}m"})

    ev = _merge_same(ev, EVENT_MERGE_SEC)
    for e in ev:
        e["center"] = round((e["t0"] + e["t1"]) / 2, 2)
        e["t0"] = round(e["t0"], 2)
        e["t1"] = round(e["t1"], 2)
    ev.sort(key=lambda e: e["t0"])
    return {"ok": True, "dur": eg["dur"], "events": ev,
            "mean_speed": float(np.mean(speed))}


def detect_obj3d_events(clip_id: str, dur: float, camera: str = CAMERA) -> list:
    """visionary obj3d(3DOD)로 ego 진행로 내 agent 이벤트 검출.

    obj3d lidar frame = x전방·y좌우(obstacle rig와 동일 관례, 실측 확인). 더 정확·풍부.
    frame_idx→video 시각은 map/frames timestamp_us + 카메라 t0 로 정렬.
    반환 형태는 detect_obstacle_events 와 동일(파이프라인 호환).
    """
    import numpy as np
    import pyarrow.parquet as pq
    import paths as P

    of = P.obj3d_frames(clip_id)
    if not of.exists():
        return []
    fr = pq.read_table(of, columns=["frame_idx", "obj_ids", "classes", "boxes_3d"]).to_pydict()
    nfr = len(fr["frame_idx"])
    # map timestamp_us 가 0으로 미기록 → frame_idx 선형 매핑으로 video 시각 근사(≈10Hz).
    maxfi = max(fr["frame_idx"]) or 1
    tracks = {}
    for i in range(nfr):
        fi = fr["frame_idx"][i]
        t = (fi / maxfi) * dur
        for oid, cl, b in zip(fr["obj_ids"][i], fr["classes"][i], fr["boxes_3d"][i]):
            tk = tracks.setdefault(oid, {"cls": cl, "pts": []})
            tk["pts"].append((t, float(b[0]), float(b[1])))  # x=전방, y=좌우

    out = []
    for oid, tk in tracks.items():
        obj = P.OBJ3D_CLASS_MAP.get(tk["cls"], "other")
        pts = sorted(tk["pts"])
        t = np.array([p[0] for p in pts]); x = np.array([p[1] for p in pts]); y = np.array([p[2] for p in pts])
        inc = (np.abs(y) < OBST_LANE_HALF) & (x > 0) & (x < OBST_AHEAD_M)
        if inc.sum() < 3:
            continue
        t0i, t1i = float(t[inc].min()), float(t[inc].max())
        if t1i - t0i < OBST_MIN_SEC:
            continue
        seg = {"subject": "agent", "object_type": obj, "role": None, "relation": None,
               "longitudinal_action": None, "vru_detail": None,
               "t0": round(max(0.0, t0i), 2), "t1": round(min(dur, t1i), 2),
               "kind": f"obj3d:{tk['cls']}", "min_dist": round(float(x[inc].min()), 1)}
        if obj in ("pedestrian", "bicycle_micromobility"):
            seg["role"] = "crossing"; seg["relation"] = "cross_ego_path"
            if obj == "pedestrian":
                seg["vru_detail"] = "crossing"
        else:
            entered = np.abs(y[0]) > OBST_CUTIN_Y and np.abs(y[inc]).min() < 1.5
            if entered:
                seg["role"] = "adjust"; seg["relation"] = "cut_in"
            else:
                seg["role"] = "preceding_vehicle"; seg["longitudinal_action"] = "maintain_speed"
        out.append(seg)

    out.sort(key=lambda s: (s["object_type"], s["role"], s["min_dist"]))
    kept = []
    for s in out:
        dup = any(s["object_type"] == q["object_type"] and s["role"] == q["role"]
                  and not (s["t1"] < q["t0"] or s["t0"] > q["t1"]) for q in kept)
        if not dup:
            kept.append(s)
    kept.sort(key=lambda s: s["t0"])
    return kept


def detect_obstacle_events(clip_id: str, dur: float, camera: str = CAMERA) -> list:
    """obstacle.offline 트랙으로 ego 진행로 내 agent 이벤트를 검출(egomotion 사각지대 보완).

    obstacle.offline 은 이미 클립 정합(0~dur)·rig 좌표계. GT 클래스/위치에서
    object_type·role·relation 을 결정론적으로 산출. 반환: agent 세그먼트 dict 목록
    (subject=agent, t0/t1, object_type/role/relation/longitudinal_action/vru_detail, kind).
    """
    import numpy as np
    import pyarrow.parquet as pq

    p = Path(DATASET_ROOT) / "labels" / "obstacle.offline" / f"{clip_id}.obstacle.offline.parquet"
    if not p.exists():
        return []
    t = pq.read_table(
        p, columns=["timestamp_us", "track_id", "center_x", "center_y", "label_class"]
    ).to_pydict()
    ts = np.array(t["timestamp_us"], dtype="float64")
    tsec = (ts - ts.min()) / 1e6
    tid = np.array(t["track_id"]); cx = np.array(t["center_x"]); cy = np.array(t["center_y"])
    cls = np.array(t["label_class"])

    out = []
    for k in set(tid.tolist()):
        m = tid == k
        st = tsec[m]; order = np.argsort(st)
        st = st[order]; x = cx[m][order]; y = cy[m][order]
        c = cls[m][0]
        incorr = (np.abs(y) < OBST_LANE_HALF) & (x > 0) & (x < OBST_AHEAD_M)
        if incorr.sum() < 3:
            continue
        t0, t1 = float(st[incorr].min()), float(st[incorr].max())
        if t1 - t0 < OBST_MIN_SEC:
            continue
        obj = CLASS_MAP.get(str(c), "other")
        seg = {"subject": "agent", "object_type": obj, "role": None,
               "relation": None, "longitudinal_action": None, "vru_detail": None,
               "t0": round(max(0.0, t0), 2), "t1": round(min(dur, t1), 2),
               "kind": f"obstacle:{c}", "min_dist": round(float(x[incorr].min()), 1)}

        if obj in ("pedestrian", "bicycle_micromobility"):
            # 진행로 내 보행자/라이더 → 횡단으로 간주
            seg["role"] = "crossing"
            seg["relation"] = "cross_ego_path"
            if obj == "pedestrian":
                seg["vru_detail"] = "crossing"
        else:
            # 차량: 차로 밖에서 진입 → cut_in(adjust), 아니면 선행차
            entered = np.abs(y[0]) > OBST_CUTIN_Y and np.abs(y[incorr]).min() < 1.5
            if entered:
                seg["role"] = "adjust"; seg["relation"] = "cut_in"
            else:
                seg["role"] = "preceding_vehicle"
                seg["longitudinal_action"] = "maintain_speed"  # 코스한 기본값
        out.append(seg)

    # 같은 (object_type, role) 이 시간 겹치면 가장 가까운 것만 (혼잡 씬 폭주 방지)
    out.sort(key=lambda s: (s["object_type"], s["role"], s["min_dist"]))
    kept = []
    for s in out:
        dup = any(s["object_type"] == q["object_type"] and s["role"] == q["role"]
                  and not (s["t1"] < q["t0"] or s["t0"] > q["t1"]) for q in kept)
        if not dup:
            kept.append(s)
    kept.sort(key=lambda s: s["t0"])
    return kept
