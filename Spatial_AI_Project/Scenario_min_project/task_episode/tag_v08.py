# vocab-literal-ok — _CAUSE_ENUM(구 4값 cause, v0.4 6값 전환 전까지 유지)·_OBJ_KW·_REL_KW는
# 사후 정규화용 키워드 매핑표라 어휘 값이 dict key로 재등장한다(값 자체는 다국어 동의어
# 목록이라 로더로 대체 불가). v0.4 cause 6값 전환은 별도 결정 사항(CLAUDE.md §4 전환 규칙) —
# 여기서 로더로 바꾸면 미승인 상태로 조용히 6값 스키마로 전환되는 부작용이 생겨 보류.
"""v0.8 프로토타입 — Scene Description + Critical Components (자유기술 + 사후 정규화).

패러다임 전환: 이산 maneuver enum 강제 대신,
  1) GT(egomotion arc + obstacle in-path)로 '무엇이 critical한가' 근거 제공
  2) 모델은 자유 기술(scene_description / critical_components / chain_of_causation) — 강점 활용
  3) 사후에 유사 토큰을 정규 어휘로 매핑(tags[]) — 고정어휘 강제 생성 아님
윈도우는 lead-in 포함(접근 구간부터)해 maneuver 아크 전체를 담는다.
"""

import re
import tempfile
from pathlib import Path

from config import IMAGE_SEQ_MAX_FRAMES, MODEL, SEND_FPS, TEMPERATURE, WINDOW_MAX_SIDE
from thresholds import LEAD_IN

V08_MAX_TOKENS = 4096  # 자유기술(scene/critical/coc)은 길어 넉넉히(다객체 구조화 truncate 방지)
from dataset import (
    clip_duration, frame_sequence_data_uris, is_frame_gallery, to_data_uri,
    write_subclip,
)
from client import visual_content
from events import detect_events, detect_obj3d_events
from classify073 import consolidate_episodes, _causal_agent

_ARC = {"stop": "stop", "decelerate": "decelerate", "accelerate": "accelerate",
        "turn_left": "turn left", "turn_right": "turn right", "evade": "evasive maneuver"}

# 사후 정규화: 자유기술 → 정규 어휘 (유사 토큰 매핑)
_OBJ_KW = {"pedestrian": ["보행자", "행인", "사람", "pedestrian", "person"],
           "vehicle": ["차량", "승용", "car", "vehicle", "sedan", "택시"],
           "large_vehicle": ["트럭", "버스", "대형", "truck", "bus", "폐기물"],
           "motorcycle": ["오토바이", "이륜", "motorcycle"],
           "bicycle_micromobility": ["자전거", "킥보드", "bicycle", "rider", "라이더"]}
_REL_KW = {"cut_in": ["끼어", "cut in", "cut-in", "진입", "cut_in"],
           "cross_ego_path": ["횡단", "가로질", "cross", "경로.*가로"],
           "block_lane": ["막", "차단", "block", "정차"],
           "oncoming_cross": ["대향", "마주", "oncoming"],
           "alongside_parallel": ["병렬", "나란", "parallel", "alongside"]}
_SIGNAL_KW = ["신호등", "traffic light", "적신호", "red light", "신호"]


def _norm_tags(text: str):
    """자유기술 텍스트 → 정규 어휘 태그 목록(유사 토큰 매핑)."""
    t = text.lower()
    tags = []
    for code, kws in _OBJ_KW.items():
        if any(re.search(k.lower(), t) for k in kws):
            tags.append(f"object_type:{code}"); break
    for code, kws in _REL_KW.items():
        if any(re.search(k.lower(), t) for k in kws):
            tags.append(f"relation:{code}"); break
    if any(k.lower() in t for k in _SIGNAL_KW):
        tags.append("cause:signal")
    return tags


def _gt_ref(o):
    return {"object_type": o["object_type"], "role": o.get("role"),
            "relation": o.get("relation"), "distance_m": o.get("min_dist"),
            "vru_state": o.get("vru_detail")}


def _gt_tags(o):
    t = [f"object_type:{o['object_type']}"]
    if o.get("relation"):
        t.append(f"relation:{o['relation']}")
    return t


def _gt_syn(o):
    """GT 근거로 합성한 서술(모델 미서술 시 주입용)."""
    return (f"{o['object_type']} in ego path ({o.get('role')}, "
            f"{o.get('relation') or ''} {o.get('min_dist')}m ahead)")


def _covers(comp_text, obj_type):
    kws = _OBJ_KW.get(obj_type, [])
    t = comp_text.lower()
    return any(re.search(k.lower(), t) for k in kws)


def _gt_hint(ep, agents):
    arc = " → ".join(_ARC.get(k, k) for k in ep["kinds"])
    if agents:
        lst = "; ".join(f"{i+1}) {o['object_type']} ({o.get('role')}, "
                        f"{o.get('relation') or ''} {o.get('min_dist')}m ahead)"
                        for i, o in enumerate(agents))
        a = (f" GT-detected in-path objects (you MUST address each; do not omit): {lst}.")
    else:
        a = " No GT in-path objects."
    return f"GT: ego maneuver arc = [{arc}].{a}"


from vocab073 import EGO_ACTIONS, OBJECT_TYPES, RELATIONS
import disclosure
_CAUSE_ENUM = ["agent", "signal", "road_geometry", "other"]
_VRU_ENUM = ["crossing", "about_to_cross", "walking_along", "stationary", None]

# maxLength/maxItems 로 출력 길이 강제 제한 (nano 모델 장황→truncate 방지)
# 순방향(관찰 선행) 생성: guided decoding 은 스키마 property 순서로 토큰을 커밋하므로
# 관찰(scene→critical→coc)을 먼저, cause, 그리고 MA(ego_action)를 **마지막**에 둔다.
# ego_action(MA)은 여기서 모델이 맥락 후보로 내되, 최종은 rule/arc 앵커(_ground_ego_action)로 해소.
V08_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["scene_description", "critical_components", "chain_of_causation", "cause", "ego_action"],
    "properties": {
        "scene_description": {"type": "string", "maxLength": 400},
        "ego_intent": {"type": "string", "maxLength": 150},
        "critical_components": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["description", "why_critical"],
            "properties": {"description": {"type": "string", "maxLength": 160},
                           "why_critical": {"type": "string", "maxLength": 160},
                           # 모델이 enum 에서 직접 분류(guided decoding). 객체 아니면 null.
                           "object_type": {"type": ["string", "null"], "enum": OBJECT_TYPES + [None]},
                           "relation": {"type": ["string", "null"], "enum": RELATIONS + [None]}}}},
        "chain_of_causation": {"type": "string", "maxLength": 400},
        # cause·ego_action(MA) 은 관찰 뒤에 — 순방향(behavior-first 금지)
        "cause": {"type": "string", "enum": _CAUSE_ENUM},
        "ego_action": {"type": "string", "enum": EGO_ACTIONS}}}


# media: common.client.visual_content()가 만든 content part 리스트(video_url 1개 또는
# image_url 여러개) — 판정 로직 불변, 입력 조립(무엇을 어떤 파트 타입으로 보내는가)만 소스별로
# 분기(2026-09-11, 프레임 갤러리 clip 대응).
def _reason(client, media, hint):
    r = client.chat.completions.create(
        model=MODEL, temperature=TEMPERATURE, max_tokens=V08_MAX_TOKENS,
        messages=[{"role": "user", "content": [
            {"type": "text", "text":
             f"{hint}\nAnalyze this driving segment. Describe in English, freely: "
             f"(1) scene description (road, environment, ego intent), "
             f"(2) each critical component that influences the ego's behavior and why it is critical, "
             f"(3) the chain of causation for the ego's behavior. Use the GT arc/objects as grounding "
             f"but verify and describe from the video."},
            *media]}])
    return (r.choices[0].message.content or "").strip()


def _structure(client, media, think, hint=""):
    import json
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "Analyze this segment."},
                                     *media]},
        {"role": "assistant", "content": think},
        {"role": "user", "content": "Structure the above analysis into JSON (keep free-text in English; "
         "do not over-summarize). For ego_action, classify the ego's actual maneuver from its enum based on "
         f"your analysis (a stop-then-turn is a turn/unprotected turn, not just a stop). {hint}"}]
    for mt in (V08_MAX_TOKENS, 6144):   # truncate 시 토큰↑ 재시도
        r = client.chat.completions.create(
            model=MODEL, temperature=TEMPERATURE, max_tokens=mt,
            messages=msgs, extra_body={"guided_json": V08_SCHEMA})
        ch = r.choices[0]
        try:
            return json.loads(ch.message.content)
        except Exception:
            if ch.finish_reason != "length":
                return None   # 길이초과가 아니면 재시도 무의미
    return None


def _search_tags(rec) -> list:
    """큐레이션 검색용 flat 태그. role/dist 제외, relation 은 대표 1개(GT 우선).

    arc 유지(에피소드 거동 시퀀스), ego_action·cause·object_type·vru 포함.
    """
    t = set()
    ec = rec["ego_context"]
    t.add(f"ego_action:{ec['ego_action']}")
    for k in ec.get("arc", []):          # arc 유지
        t.add(f"arc:{k}")
    t.add(f"cause:{rec['cause']}")

    objs, vrus = set(), set()
    gt_rel, model_rel = None, None
    for c in rec.get("critical_components", []):
        ref = c.get("ref")
        if ref:                          # GT(obj3d) 근거 우선
            if ref.get("object_type"):
                objs.add(ref["object_type"])
            if ref.get("relation") and gt_rel is None:
                gt_rel = ref["relation"]
            if ref.get("vru_state"):
                vrus.add(ref["vru_state"])
        for tg in c.get("tags", []):
            if tg.startswith("object_type:"):
                objs.add(tg.split(":", 1)[1])
            elif tg.startswith("relation:") and model_rel is None:
                model_rel = tg.split(":", 1)[1]
        if c.get("source") == "gt_injected":
            t.add("flag:model_missed_gt")

    for o in objs:
        t.add(f"object_type:{o}")
    for v in vrus:
        t.add(f"vru:{v}")
    rel = gt_rel or model_rel            # 대표 relation 1개, GT 우선
    if rel:
        t.add(f"relation:{rel}")
    return sorted(t)


_LEFT = {"ego_turn_left", "ego_unprotected_left", "ego_u_turn"}


def _ground_ego_action(model_ea, arc):
    """[수정1] arc(egomotion 방향)로 ego_action 강제. 모델은 방향 내 의미변형만 선택.
    arc가 회전 방향의 진실 → 좌/우/무회전 일관성 강제(unprotected_left 남발 방지)."""
    has_l = "turn_left" in arc
    has_r = "turn_right" in arc
    if has_l and not has_r:
        return model_ea if model_ea in _LEFT else "ego_turn_left"
    if has_r and not has_l:
        return "ego_turn_right"                       # 우회전은 unprotected 아님
    if has_l and has_r:
        return model_ea if model_ea in (_LEFT | {"ego_turn_right"}) else "ego_turn_left"
    # arc에 회전 없음 → 회전 코드 무효
    if model_ea in (_LEFT | {"ego_turn_right"}):
        return "ego_stop" if "stop" in arc else "ego_lane_keep"
    return model_ea                                   # 비회전 의미라벨(stop/lane_keep/follow/yield/evade) 유지


def tag_clip_v08(client, path, clip_id: str) -> dict:
    result = {"clip_id": clip_id, "ok": False, "mode": "v08"}
    try:
        # 소스 자동판별(2026-09-11): 신규 visionary-nvidia 100 clip은 mp4가 없다(index.parquet
        # 프레임 갤러리). `path`(P.video_path(clip_id))는 이 경우 존재하지 않는 경로를 가리키므로
        # duration_s는 clip_duration()으로 소스에 맞게 얻고, 영상 파트는 아래 루프에서
        # video_url(mp4 subclip)/image_url 시퀀스(갤러리)로 분기해 조립한다. 판정 로직 불변 —
        # 입력 조립(무엇을 어떤 파트 타입으로 보내는가)만 바뀐다.
        gallery = is_frame_gallery(clip_id)
        dur = clip_duration(clip_id, path)["duration_s"]
        import map_lane as _M
        det = detect_events(clip_id, curvature_fn=_M.default_curvature_fn(clip_id, dur))
        if not det["ok"]:
            result["error"] = det.get("reason"); return result
        obst = detect_obj3d_events(clip_id, dur)
        episodes = consolidate_episodes(det["events"])
        recs = []
        tmp = Path(tempfile.mkdtemp(prefix="v08_"))
        for i, ep in enumerate(episodes, 1):
            w0 = max(0.0, ep["onset"] - LEAD_IN)   # lead-in 포함 (접근 구간)
            w1 = min(dur, ep["t1"] + 1.0)
            if gallery:
                uris = frame_sequence_data_uris(clip_id, w0, w1, max_frames=IMAGE_SEQ_MAX_FRAMES)
                media = visual_content("images", uris)
            else:
                sub = tmp / f"e{i}.mp4"
                write_subclip(path, w0, w1, sub, WINDOW_MAX_SIDE, SEND_FPS)
                uri = to_data_uri(sub); sub.unlink(missing_ok=True)
                media = visual_content("video", uri)
            _, overlapping = _causal_agent(obst, ep)
            in_path = [o for o in overlapping
                       if o.get("role") in ("crossing", "preceding_vehicle")
                       and o.get("min_dist", 999) < 30]
            hint = _gt_hint(ep, in_path)              # ② GT 객체 열거+커버리지 지시
            think = _reason(client, media, hint)       # S2 raw(자유 서술)
            v = _structure(client, media, think, hint) or {}
            import copy
            s3_raw = copy.deepcopy(v)                  # S3 raw(GT override 이전 스냅샷) — 관찰용, 판정에 미사용
            comps = v.get("critical_components", []) or []
            # [수정2] object_type/relation 은 GT(obj3d)만 채택. 모델 enum 은 매칭용으로만 보관
            # (모델 과채움 door_open 차단). 매칭 안 된 모델 컴포넌트는 서술만(enum null).
            for c in comps:
                c["source"] = "model"; c["ref"] = None
                c["_mot"] = c.get("object_type")
                c["object_type"] = None; c["relation"] = None; c["tags"] = []

            consistency = []
            for o in in_path:
                hit = next((c for c in comps if c.get("_mot") == o["object_type"]), None)
                if hit:                              # 모델이 언급 + GT 근거 부여
                    hit["source"] = "gt"; hit["ref"] = _gt_ref(o)
                    hit["object_type"] = o["object_type"]; hit["relation"] = o.get("relation")
                    hit["tags"] = _gt_tags(o)
                    consistency.append({"gt": _gt_ref(o), "covered_by_model": True})
                else:                                # 모델 누락 → GT 주입(floor)
                    comps.append({"source": "gt_injected", "ref": _gt_ref(o),
                                  "object_type": o["object_type"], "relation": o.get("relation"),
                                  "description": _gt_syn(o),
                                  "why_critical": "GT in-path object (missed by model free-text; GT-injected)",
                                  "tags": _gt_tags(o), "grounded": False})
                    consistency.append({"gt": _gt_ref(o), "covered_by_model": False})

            # ego_action: 모델 추출(의미) → arc(egomotion)로 방향·회전유무 강제 그라운딩
            ego_action = _ground_ego_action(v.get("ego_action") or ep["ego_action"], ep["kinds"])
            # cause: GT agent 우선(신뢰) → 없으면 모델 추출 cause → signal 은 grounding 확인
            if in_path:
                cause = "agent"
            else:
                cause = v.get("cause") or "other"
                if cause == "signal":
                    from classify073 import _signal_present, _frame_uri
                    if not _signal_present(client, _frame_uri(path, ep["onset"])):
                        cause = "other"

            # 순방향(관찰→cause→MA) 순서로 기록: SD → critical → coc → cause → ego_action(앵커 해소)
            rec = {
                "segment_id": i, "window": [round(w0, 2), round(w1, 2)],
                "key_frame_t": round(ep["onset"], 2),
                "scene_description": v.get("scene_description"),
                "ego_intent": v.get("ego_intent"),
                "critical_components": comps,
                "chain_of_causation": v.get("chain_of_causation"),
                "cause": cause,
                "ego_context": {"ego_action": ego_action, "arc": ep["kinds"],
                                "arc_rule": ep["ego_action"]},  # MA: rule/arc 앵커로 해소, arc-rule 라벨 참고 보관
                "consistency": consistency, "think": think}
            rec["search_tags"] = _search_tags(rec)
            rec["flags"] = disclosure.stamp()
            # 2026-09-09: rule/vlm 경계 관찰용 — 판정 로직 불변, 캡처만 추가(pipeline-integrator).
            rec["_s2_raw"] = think
            rec["_s3_raw"] = s3_raw
            recs.append(rec)
        result["records"] = recs
        result["ok"] = True
    except Exception as e:
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"; result["trace"] = traceback.format_exc()[-400:]
    return result
