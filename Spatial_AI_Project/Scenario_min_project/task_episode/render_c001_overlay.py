# -*- coding: utf-8 -*-
"""C001(gold50) 산출물 정성 확인용 burn-in 오버레이 렌더러.

`outputs/labels/<exp>/<clip>/final.json`(pipeline-integrator 계약 경로 ②③④ 중 ④,
2026-09-11 exp 네임스페이스 분리 이후 경로)의 `track1[]`을 읽어
`common/overlay.render_overlay`가 기대하는 세그먼트 dict
(`t_start`/`t_end`/`cause`/`ego_action`/`segment_id`)로 변환해 mp4를 찍는다.

판정 로직은 전혀 건드리지 않는다 — final.json을 그대로 읽어 시각화만 한다.
`common/overlay.py`도 수정하지 않는다(어댑팅은 이 스크립트 안에서만).

실행: ./run.sh task_episode/render_c001_overlay.py --exp C001 --clip <id1> <id2> ...
      ./run.sh task_episode/render_c001_overlay.py --exp C001 --n 3
"""
import argparse
import json
from pathlib import Path

import paths as P
from overlay import render_overlay

ROOT = Path("/home/daejun/vla-tagging")
OUT_DIR = ROOT / "outputs" / "review_overlay"


def _load_final(labels_dir: Path, clip_id: str) -> dict:
    p = labels_dir / clip_id / "final.json"
    if not p.exists():
        raise FileNotFoundError(f"final.json 없음: {p}")
    return json.loads(p.read_text())


def _to_segment(rec: dict) -> dict:
    """track1 레코드 -> overlay.render_overlay 세그먼트 dict.

    overlay._is_v073()는 "cause" 키 존재 여부로 라벨 스타일을 고르고,
    overlay._seg_label()의 v073 분기는 segment_id + ego_action + cause만
    사용한다(object_type/role/relation 등은 cause=="agent"일 때만 참조,
    C001 스키마에는 없으므로 생략 — overlay.py는 없으면 None으로 표시할 뿐
    KeyError는 나지 않는다).
    scene_description/key_frame_t는 overlay.py의 어떤 함수도 읽지 않으므로
    (검증: _seg_label·_is_active·_draw_timeline 모두 t_start/t_end/cause/
    ego_action/segment_id/object_type/role/... 만 조회) 라벨에는 반영되지
    않는다 — 세그먼트 dict에는 참고용으로 실어 두되(무해), overlay.py를
    고치지 않는 한 화면에는 나타나지 않는다는 점을 아래 보고에 남긴다.
    """
    window = rec.get("window") or [None, None]
    t_start, t_end = (window + [None, None])[:2]
    seg = {
        "segment_id": rec.get("segment_id"),
        "t_start": t_start,
        "t_end": t_end,
        "cause": rec.get("cause"),
        "ego_action": (rec.get("ego_context") or {}).get("ego_action"),
        # overlay.py가 읽지 않는 참고 필드(화면에는 안 뜸, 명시적으로 남겨둠)
        "key_frame_t": rec.get("key_frame_t"),
        "scene_description": rec.get("scene_description"),
    }
    return seg


def render_clip(labels_dir: Path, clip_id: str) -> dict:
    data = _load_final(labels_dir, clip_id)
    track1 = data.get("track1") or []
    segments = [_to_segment(r) for r in track1]
    valid = [s for s in segments if isinstance(s.get("t_start"), (int, float))
             and isinstance(s.get("t_end"), (int, float))]
    print(f"[{clip_id}] track1 레코드 {len(track1)}개 -> 유효 세그먼트 {len(valid)}개 "
          f"(cause={[s.get('cause') for s in valid]}, ego_action={[s.get('ego_action') for s in valid]})")
    if not valid:
        print(f"[{clip_id}] 경고: 유효 세그먼트 0개 — 빈 오버레이가 만들어진다")

    clip_path = P.video_path(clip_id)
    if not clip_path.exists():
        raise FileNotFoundError(f"영상 없음: {clip_path}")

    out_path = OUT_DIR / f"{clip_id}.mp4"
    render_overlay(clip_path, segments, out_path, clip_id=clip_id)
    size = out_path.stat().st_size
    print(f"[{clip_id}] 렌더 완료: {out_path} ({size} bytes)")
    return {"clip_id": clip_id, "n_segments": len(valid), "out_path": str(out_path), "size": size}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="outputs/labels/<exp>/ 네임스페이스(2026-09-11 exp 분리)")
    ap.add_argument("--clip", nargs="+", default=None, help="렌더할 clip_id 목록")
    ap.add_argument("--n", type=int, default=3, help="--clip 미지정 시 처리할 clip 개수")
    args = ap.parse_args()

    labels_dir = ROOT / "outputs" / "labels" / args.exp

    if args.clip:
        clip_ids = args.clip
    else:
        all_clips = sorted(p.name for p in labels_dir.iterdir() if (p / "final.json").exists())
        clip_ids = all_clips[: args.n]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for cid in clip_ids:
        try:
            results.append(render_clip(labels_dir, cid))
        except Exception as e:
            print(f"[{cid}] 실패: {e}")
            results.append({"clip_id": cid, "error": str(e)})

    print(json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
