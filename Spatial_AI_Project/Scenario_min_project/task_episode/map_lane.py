# -*- coding: utf-8 -*-
"""map-lane 모듈 (2차 metadata labeling용).

visionary map(v2.2.1.0, native v1 레이아웃)의 'divider_polylines' 필드는 실제로
**차선 경계선(line)** 이다(명칭이 아니라 뜻 자체가 그러함 — divider=노면표시 그 자체,
차선 중심선 아님). 경계선을 y로 정렬하고 y=0을 사이에 둔 인접쌍을 ego 차로로 본다.

2026-09-11: 구 v1.1.0 shared-column 레이아웃(centerlines/lane_ids/lane_type/area_points)
→ 신규 v2.2.1.0 native v1(divider_polylines/divider_ids/divider_classes/area_polygons)
컬럼명 전환. 이 모듈은 frames.parquet의 divider_polylines 만 읽는다(area_* 는 미사용,
2026-09-11 확인 — 교차로/횡단보도 판정에 area 를 쓰게 되면 area_polygons 로 읽을 것,
area_points 아님).

- map_valid(clip): ego 차로가 신뢰 검출되는 clip인지 게이트
    조건: 경계쌍 존재 · 폭 ∈ [2.5,4.5]m · 프레임 ≥60% 안정
- agent_lane_offset: 에피소드 대표프레임의 경계선으로 agent를 차로에 할당(0=ego,+좌,-우)
- classify_lane_change: obj3d 트랙의 차로 인덱스 변화 → cut_in/cut_out (회전·곡률 불변)

무효(map_valid=False) clip은 호출측에서 corridor fallback(Branch B)로 분기한다.
"""
import math
import statistics
import numpy as np
import pyarrow.parquet as pq

import paths as P
import events
from thresholds import VALID_FRAC

LANE_W_MIN, LANE_W_MAX = 2.5, 4.5   # ego 차로폭 유효 범위(m)
REF_X = 8.0                         # ego 차로폭 기준 전방거리(m)
FWD_NEAR = 35.0                     # cut_in/out 관심 전방(m)
SUSTAIN = 3                         # 상태 지속 최소 관측점

_cache = {}


def _lines(clip_id):
    """clip의 프레임별 경계선 목록 [(N,3) polyline, ...] 캐시."""
    if clip_id in _cache:
        return _cache[clip_id]
    mf = P.map_frames(clip_id)
    if not mf.exists():
        _cache[clip_id] = None
        return None
    d = pq.read_table(mf, columns=["divider_polylines"]).to_pydict()["divider_polylines"]
    frames = []
    for fr in d:
        lns = []
        for ln in fr:
            a = np.array(ln, dtype=float)
            if a.size and a.size % 3 == 0:
                lns.append(a.reshape(-1, 3))
        frames.append(lns)
    _cache[clip_id] = frames
    return frames


def _line_y(a, x):
    """경계선 a의 전방거리 x에서의 lateral y (범위 밖이면 None)."""
    fwd = a[(a[:, 0] > x - 6) & (a[:, 0] < x + 6)]
    if len(fwd) < 2:
        return None
    xs = fwd[:, 0]; order = np.argsort(xs)
    if x < xs.min() - 1.5 or x > xs.max() + 1.5:
        return None
    return float(np.interp(x, xs[order], fwd[order, 1]))


def _boundary_ys(lns, x):
    return [y for y in (_line_y(a, x) for a in lns) if y is not None]


def _ego_width(lns, x=REF_X):
    ys = _boundary_ys(lns, x)
    pos = [y for y in ys if 0 < y < 6]; neg = [y for y in ys if -6 < y < 0]
    if pos and neg:
        return min(pos) - max(neg)
    return None


def map_valid(clip_id):
    """ego 차로가 신뢰 검출되는 clip인지. 반환 (bool, stats)."""
    frames = _lines(clip_id)
    if not frames:
        return False, {"frac": 0.0, "med_w": None, "n": 0, "reason": "no_map"}
    ws = [w for w in (_ego_width(f) for f in frames) if w and LANE_W_MIN <= w <= LANE_W_MAX]
    frac = len(ws) / len(frames)
    return (frac >= VALID_FRAC), {"frac": round(frac, 2),
                                  "med_w": round(statistics.median(ws), 1) if ws else None,
                                  "n": len(frames)}


def frame_idx_at(clip_id, t, dur):
    """비디오 시각 t(초) → map frame index (선형 매핑)."""
    frames = _lines(clip_id)
    n = len(frames) if frames else 1
    return min(n - 1, max(0, int(round(t / max(dur, 1e-6) * (n - 1)))))


CURV_X_NEAR, CURV_X_FAR = 5.0, 30.0  # 도로 형상 탄젠트 각(근거리→원거리) 산출 구간(m)


def road_curvature_over(clip_id, t0, t1, dur):
    """[t0,t1] 구간 동안 도로 형상이 설명하는 자차 heading 변화량(deg) 근사.

    매 프레임 ego-relative 경계선에서 근거리(x=CURV_X_NEAR)→원거리(x=CURV_X_FAR) 벡터의
    각도를 "그 시점에 이 도로 형상을 따라가려면 필요한 heading"으로 보고, 구간 시작·끝
    프레임의 각도 차를 반환한다. 곡선 도로를 따라 순수 주행만 했다면 이 값이 실측
    heading 변화(raw)를 대부분 설명해야 한다(잔차 ≈ 0). 좌/우 경계 중 ego와 더 가까운
    쪽(|y| 최소)을 대표선으로 삼는다 — 폭 계산과 다른 소스라도 좌우 대칭이라 부호는 불변.

    1차 근사(v1): 엄밀한 호 길이 곡률 적분이 아니라 대표 탄젠트각의 시점간 차이.
    map_valid=False·경계선 부족 시 None(호출측이 curvature_correction_source=none으로 강등).
    """
    frames = _lines(clip_id)
    if not frames:
        return None
    fi0, fi1 = frame_idx_at(clip_id, t0, dur), frame_idx_at(clip_id, t1, dur)

    def _tangent_angle(fi):
        lns = frames[min(max(fi, 0), len(frames) - 1)]
        near = _boundary_ys(lns, CURV_X_NEAR)
        far = _boundary_ys(lns, CURV_X_FAR)
        if not near or not far:
            return None
        y_near = min(near, key=abs)
        y_far = min(far, key=abs)
        return math.degrees(math.atan2(y_far - y_near, CURV_X_FAR - CURV_X_NEAR))

    a0, a1 = _tangent_angle(fi0), _tangent_angle(fi1)
    if a0 is None or a1 is None:
        return None
    return float(a1 - a0)


def default_curvature_fn(clip_id, dur):
    """Stage2(task_episode) 기본 배선용 — map_valid clip만 실제 보정함수 반환, 아니면 None(무보정).
    호출부는 `events.detect_events(clip_id, curvature_fn=default_curvature_fn(clip_id, dur))`로 사용."""
    if not map_valid(clip_id)[0]:
        return None
    return lambda t0, t1: road_curvature_over(clip_id, t0, t1, dur)


def lane_crossing_count(clip_id, t0, t1, dur, x=REF_X):
    """[t0,t1] 구간 동안 ego가 지나친 차로 경계선 개수 — 계측 전용, 판정(turn/lane_change
    분류)에는 아직 미사용(2026-09-08, ego_action 2축 분리 작업 — 사람 리뷰용 보조 신호).

    프레임별 x지점 경계선 중 y<=0(ego 기준 우측) 개수를 ego의 "차로 순번" 근사로 쓴다
    (개별 경계선은 divider_ids가 프레임 로컬 인덱스라 프레임 간 ID 추적 불가 — 순번 값
    자체를 신호로 씀). 이 순번이 프레임 간 바뀐 횟수를 반환. map_valid 아니면 None
    (호출측이 lane_crossing_source=none으로 강등, curvature_fn과 동일 패턴).
    """
    if not map_valid(clip_id)[0]:
        return None
    frames = _lines(clip_id)
    fi0, fi1 = frame_idx_at(clip_id, t0, dur), frame_idx_at(clip_id, t1, dur)
    if fi1 <= fi0:
        return 0
    idx_seq = []
    for fi in range(fi0, fi1 + 1):
        lns = frames[min(max(fi, 0), len(frames) - 1)]
        ys = _boundary_ys(lns, x)
        if not ys:
            continue
        idx_seq.append(sum(1 for y in ys if y <= 0))
    return sum(1 for a, b in zip(idx_seq, idx_seq[1:]) if a != b)


def default_lane_crossing_fn(clip_id, dur):
    """`default_curvature_fn`과 동일 opt-in 패턴 — map_valid clip만 실제 함수 반환.
    호출부는 `events.detect_events(clip_id, lane_crossing_fn=default_lane_crossing_fn(clip_id, dur))`."""
    if not map_valid(clip_id)[0]:
        return None
    return lambda t0, t1: lane_crossing_count(clip_id, t0, t1, dur)


def lane_offset(y, boundary_ys):
    """agent lateral y의 ego 대비 차로 오프셋: ego와 agent 사이 경계 수(+좌 -우, 0=동일차로)."""
    if y >= 0:
        return sum(1 for b in boundary_ys if 0 < b < y)
    return -sum(1 for b in boundary_ys if y < b < 0)


def agent_lane_offset(clip_id, frame_idx, x, y):
    """대표프레임 경계선으로 (x,y) agent의 차로 오프셋. 경계 없으면 None."""
    frames = _lines(clip_id)
    if not frames:
        return None
    lns = frames[min(frame_idx, len(frames) - 1)]
    ys = _boundary_ys(lns, x)
    return lane_offset(y, ys) if ys else None


CORRIDOR = 1.6      # ego 경로 반폭(m) — corridor 병용
CROSS_MARGIN = 0.4  # 경계선 밖 판정 여유(m)


def _ego_straight(clip_id, t0, t1):
    """구간 [t0,t1] 동안 ego가 직진(회전·차선변경 아님)인가 → 상대차 line 크로싱만 잡기 위함."""
    ev = events.detect_events(clip_id)
    if ev.get("ok"):
        for e in ev["events"]:
            if e["kind"] in ("turn_left", "turn_right", "lane_change_left", "lane_change_right") \
                    and not (e["t1"] < t0 or e["t0"] > t1):
                return False
    eg = events.load_egomotion_clip(clip_id)
    if not eg["ok"]:
        return True
    m = (eg["t"] >= t0) & (eg["t"] <= t1)
    if m.sum() < 2:
        return True
    yaw = np.degrees(eg["yaw"][m])
    return (yaw.max() - yaw.min()) < 8.0


def detect_cutin(clip_id, track, dur):
    """ego 기준 cut_in: 상대차가 ego-차로 경계선(map)을 넘어 corridor(obj3d)로 진입·머무름.
    ego 직진 구간에서만(ego 차선변경/회전 배제). track=[(t,x,y)] (동행·전방은 호출측 필터).
    반환 'L'|'R'(진입 방향) 또는 None.
    """
    frames = _lines(clip_id)
    if not frames:
        return None
    fwd = [(t, x, y) for (t, x, y) in track if 0 < x < 45]   # 발산 여부까지 보려 넓게
    if len(fwd) < 2 * SUSTAIN:
        return None
    if not _ego_straight(clip_id, fwd[0][0], fwd[-1][0]):
        return None                              # ego가 움직이면 상대차 크로싱 아님
    states = []
    for (t, x, y) in fwd:
        lns = frames[min(frame_idx_at(clip_id, t, dur), len(frames) - 1)]
        ys = _boundary_ys(lns, x)                # 매 프레임 경계선(프레임 정합)
        pos = [b for b in ys if 0 < b < 6]; neg = [b for b in ys if -6 < b < 0]
        if not pos or not neg:
            states.append("?"); continue
        yL, yR = min(pos), max(neg)
        if y > yL + CROSS_MARGIN:
            states.append("L")
        elif y < yR - CROSS_MARGIN:
            states.append("R")
        elif yR < y < yL and abs(y) < CORRIDOR:
            states.append("in")
        else:
            states.append("?")
    if states.count("in") < SUSTAIN:
        return None
    first_in = states.index("in")
    head = states[max(0, first_in - SUSTAIN):first_in]
    after = states[first_in:]
    if len(head) < SUSTAIN or len(set(head)) != 1 or head[0] not in ("L", "R"):
        return None                              # 진입 전 '일관된 인접차로' 아님
    opp = "R" if head[0] == "L" else "L"
    if opp in after:
        return None                              # 진입 후 반대편으로 빠짐 = 추월/발산(pass-through)
    if states[-1] != "in":
        return None                              # 마지막에 ego 차로에 머물러야(lead 됨)
    return head[0]


CORR_ADJ = 3.0      # 인접차로 판정 |y|(m) — obj3d corridor 기준
CORR_IN = 1.5       # ego corridor 진입 |y|(m)
CORR_NET = 1.5      # 최소 횡방향 접근량(m)


def detect_cutin_corridor(clip_id, track):
    """obj3d corridor 기반 cut_in 후보 (map 불필요·전 clip·고recall).
    인접(|y|>ADJ)→ego corridor(|y|<IN) 진입 후 머무름, 반대편 미이탈(pass-through 배제),
    ego 직진 구간에서만. track=[(t,x,y)] 동행·전방은 호출측 필터. 반환 'L'|'R'|None.
    """
    fwd = [(t, x, y) for (t, x, y) in track if 0 < x < 40]
    if len(fwd) < 2 * SUSTAIN:
        return None
    if not _ego_straight(clip_id, fwd[0][0], fwd[-1][0]):
        return None
    ys = [y for (_, _, y) in fwd]
    y0 = sum(ys[:SUSTAIN]) / SUSTAIN; y1 = sum(ys[-SUSTAIN:]) / SUSTAIN
    if y0 > CORR_ADJ and abs(y1) < CORR_IN and min(ys) > -CORR_IN and (y0 - y1) > CORR_NET:
        return "L"                                # 좌 인접 → ego 진입, 우측으로 안 빠짐
    if y0 < -CORR_ADJ and abs(y1) < CORR_IN and max(ys) < CORR_IN and (y1 - y0) > CORR_NET:
        return "R"
    return None


def map_confirms_cutin(clip_id, track, dur):
    """map 유효 clip이면 경계선 크로싱까지 확인(정밀화). 반환 (map_valid, map_confirmed)."""
    if not map_valid(clip_id)[0]:
        return False, False
    return True, detect_cutin(clip_id, track, dur) is not None


def classify_lane_change(clip_id, ref_frame, track):
    """obj3d 트랙(list of (t,x,y))의 차로 인덱스 변화 → 'cut_in'|'cut_out'|None.

    대표프레임(ref_frame) 경계선을 각 agent x에 보간해 차로 오프셋 시퀀스를 구하고
    인접(±1)↔ego(0) 전이를 판정. 회전·곡률·경계지터에 불변.
    """
    offs = []
    for (t, x, y) in track:
        if x <= 0 or x > FWD_NEAR:
            continue
        o = agent_lane_offset(clip_id, ref_frame, x, y)
        if o is not None:
            offs.append(o)
    if len(offs) < 2 * SUSTAIN:                       # 전이 관측 위해 충분한 점 필요
        return None
    head = offs[:SUSTAIN]; tail = offs[-SUSTAIN:]
    span = max(abs(o) for o in offs)
    # cut_in: head가 '일관되게 인접(±1)' + tail '전부 ego(0)로 머무름' + overshoot 없음(통과 배제)
    if all(o == head[0] for o in head) and head[0] in (1, -1) \
            and all(o == 0 for o in tail) and span == 1:
        return "cut_in"
    # cut_out: 역 (ego에서 인접으로 나가 머무름)
    if all(o == 0 for o in head) and all(o == tail[0] for o in tail) \
            and tail[0] in (1, -1) and span == 1:
        return "cut_out"
    return None
