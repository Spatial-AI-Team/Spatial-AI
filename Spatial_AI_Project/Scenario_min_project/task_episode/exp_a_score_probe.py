# -*- coding: utf-8 -*-
"""실험 A 원리 검증(사전 파일럿) — A3(생성) cause 값 vs `common/scoring.py` 점수화 1위 값 비교.

범위 엄격 한정(위임 지시 원문 그대로):
- vis100 3~5 클립만 사용. 100개/98개 전체 실행 금지(토큰별 다회 호출이라 A3보다 비용 큼)
- `run_pipeline.py`에 arm 스위치 배선 금지 — 이 스크립트는 독립 실행 전용
- 판정 로직(`tag_v08.py`) 불변 — A3 결과는 `tag_v08.tag_clip_v08()`을 그대로 호출해 재사용
- 8000(cosmos_dj) 무접촉. cosmos_dj(평가-frozen)도 피하고 replica(r2/r3/r4, 8002-8004)만 사용

각 클립의 각 에피소드 segment에 대해:
  1) A3 경로: tag_v08.tag_clip_v08()이 이미 산출한 rec["cause"](guided_json 생성값)
  2) 점수화 경로: rec["window"](A3와 동일 창)로 media를 다시 조립해 `common.scoring.
     score_candidates()`로 _CAUSE_ENUM 4값을 채점 → 1위 후보
  둘을 나란히 비교(완전 일치를 기대하지 않음 — 원리 검증: "그럴듯한 결과가 나오는가").

실행:
    ./run.sh task_episode/exp_a_score_probe.py [clip_id ...]
    (인자 없으면 아래 DEFAULT_CLIPS 4개 사용)
"""
import json
import sys
import time

from client import build_client_pool
from config import IMAGE_SEQ_MAX_FRAMES
from dataset import frame_sequence_data_uris
from openai import OpenAI
from scoring import score_candidates
from tag_v08 import _CAUSE_ENUM, tag_clip_v08

# 8000(cosmos_dj)·8001(평가-frozen) 무접촉 — replica만 명시적으로 고정.
_REPLICA_URLS = ["http://localhost:8002/v1", "http://localhost:8003/v1", "http://localhost:8004/v1"]

DEFAULT_CLIPS = [
    "002dec8e-3d95-4cc2-abbe-99b3a2e78618",  # 3 episode (stop/accel/decel)
    "01c46b6b-fe98-4754-98e1-7010d294bff4",  # 1 episode (lane_change_right)
    "0262ea23-9be7-430e-8c62-d251b4cd9b01",  # 1 episode (turn_right+decel)
    "022e5c9c-dceb-4026-a1a4-1bc4a4cccc0e",  # 3 episode (turn_left 포함)
]

_QUERY = (
    "Determine the primary cause of the ego vehicle's behavior in this clip. "
    "Answer with a JSON object of the form {\"cause\": \"<value>\"} using exactly one "
    "value from: agent, signal, road_geometry, other."
)


def _replica_pool():
    alive = []
    for u in _REPLICA_URLS:
        try:
            import urllib.request
            urllib.request.urlopen(u.rstrip("/") + "/models", timeout=2.0)
            alive.append(u)
        except Exception:
            pass
    if not alive:
        raise RuntimeError("replica(8002-8004) 전부 헬스체크 실패 — 8000/8001은 건드리지 않으므로 대체 불가")
    return [OpenAI(base_url=u, api_key="EMPTY") for u in alive]


def probe_clip(client, clip_id: str) -> dict:
    from paths import video_path
    path = video_path(clip_id)  # tag_v08 내부에서 gallery 자동판별(mp4 없어도 OK)

    n_calls = 0
    t0 = time.time()
    a3 = tag_clip_v08(client, path, clip_id)
    a3_elapsed = time.time() - t0
    if not a3.get("ok"):
        return {"clip_id": clip_id, "ok": False, "error": a3.get("error")}

    rows = []
    t0 = time.time()
    for rec in a3["records"]:
        w0, w1 = rec["window"]
        uris = frame_sequence_data_uris(clip_id, w0, w1, max_frames=IMAGE_SEQ_MAX_FRAMES)
        from client import visual_content
        media = visual_content("images", uris)
        scored = score_candidates(client, media, _QUERY, _CAUSE_ENUM, "cause")
        n_calls += sum(max(1, c["n_tokens"]) for c in scored["candidates"])
        rows.append({
            "segment_id": rec["segment_id"], "window": rec["window"],
            "a3_cause": rec["cause"],
            # tag_v08.py는 GT in-path 객체가 있으면 모델 출력과 무관하게 cause="agent"로
            # 강제 override한다(agent-우선 규칙, tag_v08.py:292-296). 이 override 이전의
            # 순수 모델 guided_json 출력값도 참고용으로 같이 남긴다(rec["_s3_raw"], S3 override
            # 이전 스냅샷 — 판정 로직에는 미사용, 관찰 전용).
            "a3_raw_model_cause": (rec.get("_s3_raw") or {}).get("cause"),
            "scored_top1": scored["top1"],
            "scored_entropy_nats": round(scored["entropy_nats"], 4),
            "scored_probs": {c["candidate"]: round(c["renormalized_prob"], 4) for c in scored["candidates"]},
            "match": rec["cause"] == scored["top1"],
        })
    score_elapsed = time.time() - t0

    return {
        "clip_id": clip_id, "ok": True, "rows": rows,
        "n_segments": len(rows), "n_scoring_calls": n_calls,
        "a3_elapsed_s": round(a3_elapsed, 1), "scoring_elapsed_s": round(score_elapsed, 1),
    }


def main():
    clip_ids = sys.argv[1:] or DEFAULT_CLIPS
    pool = _replica_pool()
    print(f"replica {len(pool)}개 사용: {[str(c.base_url) for c in pool]}", flush=True)

    results = []
    for i, cid in enumerate(clip_ids):
        client = pool[i % len(pool)]
        print(f"--- clip {cid} ---", flush=True)
        r = probe_clip(client, cid)
        results.append(r)
        if not r["ok"]:
            print(f"  실패: {r.get('error')}")
            continue
        for row in r["rows"]:
            mark = "일치" if row["match"] else "불일치"
            print(f"  seg{row['segment_id']} window={row['window']} "
                  f"A3(최종)={row['a3_cause']!r} A3(모델원값)={row['a3_raw_model_cause']!r} "
                  f"점수화1위={row['scored_top1']!r} ({mark}) "
                  f"probs={row['scored_probs']} entropy={row['scored_entropy_nats']}")
        print(f"  A3 elapsed={r['a3_elapsed_s']}s · 점수화 elapsed={r['scoring_elapsed_s']}s "
              f"· 점수화 API호출(토큰환산) {r['n_scoring_calls']}회분")

    n_ok = sum(1 for r in results if r["ok"])
    all_rows = [row for r in results if r["ok"] for row in r["rows"]]
    n_match = sum(1 for row in all_rows if row["match"])
    print(f"\n=== 요약: 클립 {n_ok}/{len(results)} 성공 · segment {len(all_rows)}개 중 "
          f"A3-점수화 일치 {n_match}개 ===")

    import os
    scratch = os.environ.get(
        "CLAUDE_JOB_DIR",
        "/tmp/claude-1003/-home-daejun-vla-tagging/91e744f9-e228-4df8-a16c-1db6dcd7c06d/scratchpad")
    out_path = os.path.join(scratch if scratch.endswith("scratchpad") else os.path.join(scratch, "tmp"),
                             "exp_a_score_probe_result.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"결과 저장: {out_path}")


if __name__ == "__main__":
    main()
