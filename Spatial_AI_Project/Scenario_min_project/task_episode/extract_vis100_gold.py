# -*- coding: utf-8 -*-
"""신규 100 clip(vis100, 프레임 갤러리 레이아웃) gold 라벨링 세트 구축.

배경: 팀원 재구성으로 기존 gold50/selected50 원본이 소실되어 신규 100 clip
(`gold_label/vis100.json`, `paths.py`의 `nvidia/<clip>/`)에 새로 gold 라벨링을 진행하기로
결정(2026-09-11). `extract_s0_validation.py`(s0_validation 50-clip)와 동일한 S0 배선
(`events.detect_events`+`classify073.consolidate_episodes`, tag_v08.py:222-223과 동일 조합)을
재사용하되, 신규 clip이 mp4가 아니라 `sensor/<cam>/<frame_id>.jpg` 프레임 갤러리라는 점만
다르다:
  - duration: `dataset.video_meta(path)` 대신 `dataset.clip_duration(clip_id, path)`
    (소스 자동판별 창구, run_pipeline.py와 동일 배선)
  - 재생용 mp4: jpg 시퀀스를 ffmpeg image2 데뮤서로 직접 합성(`-start_number 0 -i %06d.jpg`).
    프레임 개수는 clip마다 199~201로 다르므로(실측) 고정 개수 가정 없이 디렉토리 실제 파일
    수만큼 그대로 합성한다. 10fps 실측 확정값(dataset.py 주석)을 인코딩 fps로 사용.
    이 mp4는 사람이 브라우저에서 보는 리뷰용일 뿐 — VLM 전송 방식(품질)과 무관.

감사 필드(transition_filters_passed 등)는 extract_s0_validation.py와 동일 이유로 이 스크립트도
계산하지 않는다(무기록 fallback 금지 — 없는 것을 만들어내지 않고 그대로 드러낸다).

실행: ./run.sh task_episode/extract_vis100_gold.py [--workers N]
출력: gold_label/vis100/{sample_clips.json, episodes.json, s0_raw.json, vids/*.mp4}
"""
import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paths as P
from dataset import clip_duration
import events
import map_lane as M
from classify073 import consolidate_episodes
import taxonomy

ROOT = Path("/home/daejun/vla-tagging")
SRC = ROOT / "gold_label" / "vis100.json"
OUT = ROOT / "gold_label" / "vis100"


def process(clip_id):
    """clip 1개 -> (episodes.json용 레코드, s0_raw.json용 레코드, 오류메시지|None)."""
    path = P.video_path(clip_id)  # mp4 없는 clip은 존재하지 않는 경로 반환 — clip_duration이 gallery로 자동판별
    try:
        dur = clip_duration(clip_id, path)["duration_s"]
    except Exception as e:
        return None, None, f"clip_duration 실패: {e}"

    try:
        ev_raw = events.detect_events(clip_id)
    except Exception as e:
        return None, None, f"detect_events(raw) 예외: {e}"
    if not ev_raw.get("ok"):
        return None, None, f"detect_events 실패: {ev_raw.get('reason')}"

    curvature_fn = M.default_curvature_fn(clip_id, dur)          # tag_v08.py:223과 동일 배선
    lane_crossing_fn = M.default_lane_crossing_fn(clip_id, dur)  # 계측만, 판정 미반영
    try:
        ev_corr = events.detect_events(clip_id, curvature_fn=curvature_fn,
                                        lane_crossing_fn=lane_crossing_fn)
    except Exception as e:
        return None, None, f"detect_events(corrected) 예외: {e}"
    if not ev_corr.get("ok"):
        return None, None, f"detect_events(corrected) 실패: {ev_corr.get('reason')}"

    eps = consolidate_episodes(ev_corr["events"])
    ep_out = [{"t0": ep["t0"], "t1": ep["t1"], "arc": ep["kinds"],
               "ego_action": ep["ego_action"],
               "auto": taxonomy.auto_tags_from_arc(ep["kinds"])}
              for ep in eps]

    raw_kinds = sorted(e["kind"] for e in ev_raw["events"])
    corr_kinds = sorted(e["kind"] for e in ev_corr["events"])

    raw_dump = {
        "dur": dur,
        "map_valid": curvature_fn is not None,
        "events_raw": ev_raw["events"],
        "events_corrected": ev_corr["events"],
        "kind_diff": raw_kinds != corr_kinds,
    }
    return {"dur": dur, "episodes": ep_out}, raw_dump, None


def transcode_gallery(clip_ids, vids_dir, workers=6):
    """gold_tool.py 규약(vids/{clip_id[:8]}.mp4) — jpg 프레임 갤러리를 10fps mp4로 직접 합성.
    review._transcode(mp4->mp4 재인코딩)와 달리 여기는 소스가 jpg 시퀀스라 image2 데뮤서를 쓴다."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    vids_dir = Path(vids_dir)
    vids_dir.mkdir(parents=True, exist_ok=True)

    def _one(cid):
        out = vids_dir / f"{cid[:8]}.mp4"
        if out.exists() and out.stat().st_size > 0:
            return cid, True, None
        jpg_dir = P.clip_dir(cid) / "sensor" / P.CAM
        if not jpg_dir.exists():
            return cid, False, f"프레임 디렉토리 없음: {jpg_dir}"
        n = len(list(jpg_dir.glob("*.jpg")))
        if n == 0:
            return cid, False, f"jpg 프레임 0개: {jpg_dir}"
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error",
                 "-framerate", "10", "-start_number", "0",
                 "-i", str(jpg_dir / "%06d.jpg"),
                 "-vf", "scale=640:-2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                 "-crf", "28", "-movflags", "+faststart", str(out)],
                check=True, timeout=120, capture_output=True, text=True)
            return cid, True, None
        except subprocess.CalledProcessError as e:
            return cid, False, f"ffmpeg 실패(rc={e.returncode}): {e.stderr[-300:]}"
        except Exception as e:
            return cid, False, f"ffmpeg 예외: {e}"

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_one, clip_ids):
            results.append(r)
    ok = [cid for cid, ok_, _ in results if ok_]
    fail = [(cid, err) for cid, ok_, err in results if not ok_]
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    clip_ids = json.loads(SRC.read_text())
    print(f"대상 clip {len(clip_ids)}개 (source={SRC})", flush=True)

    episodes_out, raw_out, ok_ids, errors = {}, {}, [], []
    for i, cid in enumerate(clip_ids):
        ep_json, raw_json, err = process(cid)
        if err:
            errors.append({"clip_id": cid, "error": err})
            print(f"  [{i+1}/{len(clip_ids)}] {cid[:8]} S0 실패: {err}", flush=True)
            continue
        episodes_out[cid] = ep_json
        raw_out[cid] = raw_json
        ok_ids.append(cid)
        print(f"  [{i+1}/{len(clip_ids)}] {cid[:8]} S0 성공 · dur={ep_json['dur']:.1f} "
              f"· episodes={len(ep_json['episodes'])}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sample_clips.json").write_text(
        json.dumps(ok_ids, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "episodes.json").write_text(
        json.dumps(episodes_out, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "s0_raw.json").write_text(
        json.dumps(raw_out, ensure_ascii=False, indent=1), encoding="utf-8")

    n_map_valid = sum(1 for r in raw_out.values() if r["map_valid"])
    n_diff = sum(1 for r in raw_out.values() if r["kind_diff"])
    print(f"\nS0 추출 완료: {len(ok_ids)}/{len(clip_ids)} 성공, 실패 {len(errors)}건", flush=True)
    print(f"map_valid={n_map_valid}/{len(ok_ids)} · 곡률보정 유무로 kind 분류가 달라진 clip {n_diff}건",
          flush=True)
    if errors:
        print("S0 실패 목록:", json.dumps(errors, ensure_ascii=False, indent=1), flush=True)

    # mp4는 S0 실패 clip 포함 전체 100개에 대해 시도(라벨링 도구가 clip_id 자체는 노출해야
    # gold.json 병합 시 나중에 라벨을 추가할 수 있음 — 단, sample_clips.json/episodes.json은
    # S0 성공분만 포함하므로 index.html에는 S0 실패 clip이 노출되지 않는다. 이 스크립트는
    # ok_ids만 트랜스코드해 vids/·sample_clips.json·episodes.json 범위를 일치시킨다).
    vid_ok, vid_fail = transcode_gallery(ok_ids, OUT / "vids", workers=args.workers)
    print(f"\nmp4 합성 완료: {len(vid_ok)}/{len(ok_ids)} 성공, 실패 {len(vid_fail)}건", flush=True)
    if vid_fail:
        print("mp4 실패 목록:", json.dumps(
            [{"clip_id": c, "error": e} for c, e in vid_fail], ensure_ascii=False, indent=1),
            flush=True)


if __name__ == "__main__":
    main()
