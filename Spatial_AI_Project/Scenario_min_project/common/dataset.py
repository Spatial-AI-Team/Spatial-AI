"""PhysicalAI-AV-curated 데이터셋 접근 및 프레임 샘플링 헬퍼."""

import base64
from pathlib import Path

from config import (
    CAMERA,
    CLIP_INDEX,
    DATASET_ROOT,
    FRAME_MAX_SIDE,
    JPEG_QUALITY,
    SPLIT_FILE,
)
from thresholds import NUM_FRAMES


def load_clip_ids(split_file: str = SPLIT_FILE) -> list[str]:
    """split 파일에서 클립 ID 목록을 읽는다."""
    path = Path(DATASET_ROOT) / "curation" / split_file
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def all_clip_ids() -> list[str]:
    """metadata 인덱스에서 전체 클립 ID(1,966개)를 읽는다. (전체 확장용)"""
    import pyarrow.parquet as pq

    p = Path(DATASET_ROOT) / "metadata" / "clip_index_curated.parquet"
    ids = pq.read_table(p, columns=["clip_id"]).to_pydict()["clip_id"]
    return list(dict.fromkeys(ids))  # 원순서 유지 dedup


def video_path(clip_id: str, camera: str = CAMERA) -> Path:
    """클립 ID 에 대응하는 mp4 경로."""
    return Path(DATASET_ROOT) / "camera" / camera / f"{clip_id}.{camera}.mp4"


def get_clip(index: int = CLIP_INDEX, split_file: str = SPLIT_FILE):
    """split 내 index 번째 클립의 (clip_id, mp4 경로) 를 반환한다."""
    clip_ids = load_clip_ids(split_file)
    clip_id = clip_ids[index]
    return clip_id, video_path(clip_id)


# --- 프레임 샘플링 ----------------------------------------------------------
def _read_uniform(path: Path | str, num_frames: int, fps_hint: float | None = None):
    """전체 클립에서 num_frames 개를 시간축 균등 샘플링해 (frame_index, bgr) 목록 반환."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"비디오 열기 실패: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = fps_hint or cap.get(cv2.CAP_PROP_FPS) or 0.0
    if total <= 0:
        cap.release()
        raise RuntimeError(f"프레임 수를 알 수 없음: {path}")

    n = min(num_frames, total)
    indices = np.linspace(0, total - 1, n).round().astype(int)

    out = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok:
            out.append((int(idx), frame))
    cap.release()
    if not out:
        raise RuntimeError(f"프레임 추출 실패: {path}")
    return out, fps


def sample_frames(
    path: Path | str,
    num_frames: int = NUM_FRAMES,
    max_side: int = FRAME_MAX_SIDE,
    jpeg_quality: int = JPEG_QUALITY,
) -> list[bytes]:
    """전 구간 균등 샘플링한 프레임을 개별 JPEG 바이트 목록으로 반환."""
    import cv2

    items, _ = _read_uniform(path, num_frames)
    frames: list[bytes] = []
    for _, frame in items:
        frame = _resize_max_side(frame, max_side)
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
        )
        if ok:
            frames.append(buf.tobytes())
    if not frames:
        raise RuntimeError(f"프레임 인코딩 실패: {path}")
    return frames


def build_montage(
    items: list[tuple[str, "object"]],
    cols: int = 0,
    cell_max_side: int = 640,
    jpeg_quality: int = JPEG_QUALITY,
) -> bytes:
    """(라벨, BGR프레임) 목록을 시간순 그리드 몽타주 JPEG 로 합친다.

    각 셀 좌상단에 라벨을 그린다. 서버의 '프롬프트당 이미지 최대 5장' 제약을
    지키면서 전 구간(또는 국소 구간) 커버리지를 하나의 이미지로 담기 위함.
    """
    import cv2
    import math

    if not items:
        raise RuntimeError("몽타주 프레임 없음")
    n = len(items)
    if cols <= 0:
        cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    h0, w0 = items[0][1].shape[:2]
    scale = cell_max_side / max(h0, w0)
    cw, ch = int(round(w0 * scale)), int(round(h0 * scale))

    canvas = _blank(rows * ch, cols * cw)
    for i, (label, frame) in enumerate(items):
        r, c = divmod(i, cols)
        cell = _letterbox(frame, cw, ch)
        _label(cell, label)
        canvas[r * ch : (r + 1) * ch, c * cw : (c + 1) * cw] = cell

    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise RuntimeError("몽타주 인코딩 실패")
    return buf.tobytes()


def sample_montage(
    path: Path | str,
    num_frames: int = NUM_FRAMES,
    cols: int = 0,
    cell_max_side: int = 640,
    jpeg_quality: int = JPEG_QUALITY,
) -> bytes:
    """전 구간 균등 샘플링한 num_frames 프레임을 시간순 그리드 몽타주 JPEG 로."""
    items, fps = _read_uniform(path, num_frames)
    labeled = [
        (f"#{i} f{fidx}" + (f" {fidx / fps:.1f}s" if fps else ""), frame)
        for i, (fidx, frame) in enumerate(items)
    ]
    return build_montage(labeled, cols, cell_max_side, jpeg_quality)


# --- 10fps work-frame (coarse-to-fine 용) -----------------------------------
def read_work_frames(path: Path | str, work_fps: int = 10):
    """30fps 소스를 work_fps 로 리샘플해 (work_idx, bgr) 목록을 반환.

    work_idx 는 0부터 시작하는 리샘플 프레임 번호. 원본 프레임 = work_idx * stride.
    반환: (items, src_fps, stride). t초 = work_idx / work_fps.
    """
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"비디오 열기 실패: {path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = max(1, int(round(src_fps / work_fps)))

    items = []
    src_idx = 0
    w = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if src_idx % stride == 0:
            items.append((w, frame))
            w += 1
        src_idx += 1
    cap.release()
    if not items:
        raise RuntimeError(f"work 프레임 추출 실패: {path}")
    return items, src_fps, stride


def _blank(h: int, w: int):
    import numpy as np

    return np.zeros((h, w, 3), dtype="uint8")


def _letterbox(frame, cw: int, ch: int):
    """비율 유지로 (cw, ch) 안에 맞추고 검은 여백으로 채운다."""
    import cv2

    h, w = frame.shape[:2]
    scale = min(cw / w, ch / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    cell = _blank(ch, cw)
    y0, x0 = (ch - nh) // 2, (cw - nw) // 2
    cell[y0 : y0 + nh, x0 : x0 + nw] = resized
    return cell


def _label(cell, text: str) -> None:
    """셀 좌상단에 배경 있는 텍스트 라벨을 그린다."""
    import cv2

    font, sc, th = cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
    (tw, tht), _ = cv2.getTextSize(text, font, sc, th)
    cv2.rectangle(cell, (0, 0), (tw + 8, tht + 8), (0, 0, 0), -1)
    cv2.putText(cell, text, (4, tht + 4), font, sc, (0, 255, 0), th, cv2.LINE_AA)


def _resize_max_side(frame, max_side: int):
    """긴 변이 max_side 를 넘으면 비율 유지로 축소."""
    import cv2

    h, w = frame.shape[:2]
    longest = max(h, w)
    if max_side <= 0 or longest <= max_side:
        return frame
    scale = max_side / longest
    new = (int(round(w * scale)), int(round(h * scale)))
    return cv2.resize(frame, new, interpolation=cv2.INTER_AREA)


def video_meta(path: Path | str) -> dict:
    """영상 기본 메타(프레임수·fps·해상도·길이).

    버그 수정(2026-09-11): 이전에는 `isOpened()`를 확인하지 않아 파일이 없거나 디코드
    불가일 때 `total=-1, fps=-1` → `duration_s = -1/-1 = 1.0`이라는 그럴듯한 가짜 값을
    에러 없이 반환했다(실측: run_pipeline.py 스모크에서 `s0.dur=1.0`). 이제 열기 실패·
    프레임수/fps 이상값은 `RuntimeError`로 명시 실패시킨다 — 이 파일의 다른 함수
    (`_read_uniform` 등)와 동일한 예외 기반 실패 관례를 따름. 대부분의 호출부가 이미
    try/except로 감싸 `ok=False`로 흡수하므로(tag_v08.py 등) 동작 방식 변경은 없다."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"비디오 열기 실패(파일 없음/디코드 불가): {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if total <= 0 or not fps:
        raise RuntimeError(f"비디오 메타 이상값(total={total}, fps={fps}): {path}")
    return {
        "frames": total,
        "fps": fps,
        "width": w,
        "height": h,
        "duration_s": total / fps,
    }


# --- 프레임 갤러리 (visionary-nvidia 신규 100 clip 전용, mp4 없음) -----------
# 2026-09-11 재구성: nvidia/<clip>/sensor/<cam>/<frame_id>.jpg 정지영상 갤러리 +
# index.parquet(frame_idx/frame_id/timestamp_us 실값/센서 파일명). 위 video_meta/
# sample_frames 등(cv2.VideoCapture 기반)은 이 레이아웃에 적용 불가 — 신규 함수로 대체.
# 실측(2026-09-11, vis100 6개 클립): 카메라 10.0fps 고정(±0.05%), 클립 길이 19.8~20.0s
# (200±1 프레임) — config.SEND_FPS=10과 이미 일치해 기본 호출은 매 프레임을 그대로 쓴다.


def _read_gallery_index(clip_id: str) -> list[dict]:
    """clip의 index.parquet을 frame_idx 순 정렬된 dict 리스트로 반환(모듈 내 캐시)."""
    cached = _INDEX_CACHE.get(clip_id)
    if cached is not None:
        return cached
    import pyarrow.parquet as pq
    import paths as P

    p = P.index_path(clip_id)
    if not p.exists():
        return []
    rows = pq.read_table(p).to_pylist()
    rows.sort(key=lambda r: r["frame_idx"])
    _INDEX_CACHE[clip_id] = rows
    return rows


_INDEX_CACHE: dict = {}


def is_frame_gallery(clip_id: str) -> bool:
    """clip_id 가 신규 프레임 갤러리 레이아웃(index.parquet 존재)인지 판별.

    이름 기반 추정이 아니라 디스크 실측(파일 존재)으로 판별 — mp4 데이터셋(gold50 등)과
    자동 분기하기 위함. paths.py(visionary-nvidia)만 이 레이아웃을 가지므로, 다른
    데이터셋(config.DATASET_ROOT)의 clip_id는 이 경로가 존재하지 않아 자동으로 False."""
    import paths as P

    return P.index_path(clip_id).exists()


def gallery_meta(clip_id: str, cam: str | None = None) -> dict:
    """index.parquet 프레임 갤러리 버전 video_meta(). 파일 없음/빈 테이블은 예외."""
    import paths as P

    rows = _read_gallery_index(clip_id)
    if not rows:
        raise RuntimeError(f"index.parquet 없음/빈 테이블: {P.index_path(clip_id)}")
    cam = cam or P.CAM
    ts = [r["timestamp_us"] for r in rows]
    n = len(rows)
    dur = (ts[-1] - ts[0]) / 1e6
    diffs = [ts[i + 1] - ts[i] for i in range(n - 1)]
    native_fps = (1e6 / (sum(diffs) / len(diffs))) if diffs else 0.0
    w, h = 0, 0
    fname = (rows[0].get("sensors") or {}).get(cam)
    if fname:
        jp = P.clip_dir(clip_id) / "sensor" / cam / fname
        if jp.exists():
            import cv2

            img = cv2.imread(str(jp))
            if img is not None:
                h, w = img.shape[:2]
    return {"frames": n, "fps": round(native_fps, 3), "width": w, "height": h,
            "duration_s": dur, "cam": cam}


def clip_duration(clip_id: str, path: Path | str | None = None) -> dict:
    """소스(mp4/프레임갤러리) 자동판별 후 대응 메타를 반환. 판정 로직 아님 — 입력 조립용
    duration_s 원료 통일 창구(run_pipeline.py/tag_v08.py가 공유)."""
    if is_frame_gallery(clip_id):
        return gallery_meta(clip_id)
    if path is None:
        raise ValueError(f"mp4 소스 clip은 path 인자가 필요함: {clip_id}")
    return video_meta(path)


def sample_frame_sequence(
    clip_id: str,
    t0: float,
    t1: float,
    cam: str | None = None,
    fps: float | None = None,
    max_frames: int | None = None,
    max_side: int | None = None,
    jpeg_quality: int | None = None,
) -> list[bytes]:
    """프레임 갤러리에서 [t0,t1]초 구간을 목표 fps로 균등 샘플링해 개별 JPEG 바이트
    목록으로 반환한다(`sample_frames()`의 정지영상 갤러리 버전 — video_url 아님).

    - t0,t1: 클립 첫 프레임(timestamp_us 최소값) 기준 상대 초. write_subclip(t0,t1)과 동일 관례.
    - fps 미지정시 config.SEND_FPS(=10, 실측 native fps와 일치 — 보간 없이 프레임 그대로 사용).
    - max_frames 지정 시 그 상한을 넘지 않도록 시간축 균등 재추출(토큰 예산 보호용, 미정 기본값
      — 호출측이 명시 지정하지 않으면 fps만으로 결정되는 개수 그대로 보낸다).
    - 보간 없음: 목표 시각에 가장 가까운 실제 프레임만 선택, 없는 프레임을 합성하지 않는다.
    """
    import cv2
    import numpy as np
    import paths as P
    from config import FRAME_MAX_SIDE, JPEG_QUALITY as _JQ, SEND_FPS

    fps = fps or SEND_FPS
    max_side = FRAME_MAX_SIDE if max_side is None else max_side
    jpeg_quality = _JQ if jpeg_quality is None else jpeg_quality
    cam = cam or P.CAM

    rows = _read_gallery_index(clip_id)
    if not rows:
        raise RuntimeError(f"index.parquet 없음/빈 테이블: {clip_id}")

    # index.parquet의 frame_idx/sensors 매핑은 클립 전체(예: lidar) 기준으로 채워지며,
    # 카메라별 실제 jpg 개수는 이보다 적을 수 있다(실측, 2026-09-11: 일부 clip에서 특정
    # 카메라만 마지막 1~2프레임 누락). 존재하지 않는 파일을 요청하지 않도록, 시간창으로
    # 자르기 전에 해당 cam의 파일이 실제로 존재하는 행만 후보로 남긴다.
    clip_dir = P.clip_dir(clip_id)

    def _cam_path(r: dict) -> Path | None:
        fname = (r.get("sensors") or {}).get(cam)
        return (clip_dir / "sensor" / cam / fname) if fname else None

    avail = [r for r in rows if (p := _cam_path(r)) is not None and p.exists()]
    if not avail:
        raise RuntimeError(f"카메라 {cam}의 실제 프레임 파일 없음: {clip_id}")

    ts0 = rows[0]["timestamp_us"]
    us0, us1 = ts0 + t0 * 1e6, ts0 + t1 * 1e6
    window = [r for r in avail if us0 - 1e3 <= r["timestamp_us"] <= us1 + 1e3]
    if not window:
        window = sorted(
            avail, key=lambda r: min(abs(r["timestamp_us"] - us0), abs(r["timestamp_us"] - us1))
        )[:1]

    n_target = max(1, round((t1 - t0) * fps))
    if max_frames:
        n_target = min(n_target, max_frames)
    n_target = min(n_target, len(window))

    if n_target >= len(window):
        picked = window
    else:
        step = (len(window) - 1) / (n_target - 1) if n_target > 1 else 0.0
        idxs = sorted({int(round(i * step)) for i in range(n_target)})
        picked = [window[i] for i in idxs]

    out: list[bytes] = []
    for r in picked:
        fname = (r.get("sensors") or {}).get(cam)
        if not fname:
            continue
        jp = P.clip_dir(clip_id) / "sensor" / cam / fname
        data = jp.read_bytes()
        if max_side and max_side > 0:
            img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                img = _resize_max_side(img, max_side)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                if ok:
                    data = buf.tobytes()
        out.append(data)
    if not out:
        raise RuntimeError(f"갤러리 프레임 추출 실패: {clip_id} [{t0},{t1}] cam={cam}")
    return out


def frame_sequence_data_uris(clip_id: str, t0: float, t1: float, **kw) -> list[str]:
    """sample_frame_sequence() 결과를 image data URI 목록으로(개별 image_url 전송용)."""
    return [jpeg_to_data_uri(b) for b in sample_frame_sequence(clip_id, t0, t1, **kw)]


# --- 윈도우 서브클립 -------------------------------------------------------
def write_subclip(
    path: Path | str,
    t0: float,
    t1: float,
    out_path: Path | str,
    max_side: int = 0,
    out_fps: float | None = None,
) -> dict:
    """[t0, t1]초 구간을 mp4 서브클립으로 저장.

    반환: {frame0, frame1(원본 인덱스), src_fps, n_written, path}.
    out_fps 미지정 시 원본 fps 유지(=실시간 길이 보존).
    """
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"비디오 열기 실패: {path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    f0 = max(0, int(round(t0 * src_fps)))
    f1 = min(total - 1, int(round(t1 * src_fps)))
    out_fps = out_fps or src_fps

    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    writer = None
    n = 0
    for fidx in range(f0, f1 + 1):
        ok, frame = cap.read()
        if not ok:
            break
        frame = _resize_max_side(frame, max_side)
        if writer is None:
            h, w = frame.shape[:2]
            writer = cv2.VideoWriter(
                str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h)
            )
        writer.write(frame)
        n += 1
    cap.release()
    if writer is not None:
        writer.release()
    if n == 0:
        raise RuntimeError(f"서브클립 프레임 없음: {path} [{t0},{t1}]")
    return {"frame0": f0, "frame1": f1, "src_fps": src_fps, "n_written": n,
            "path": str(out_path)}


# --- 인코딩 -----------------------------------------------------------------
def to_data_uri(path: Path) -> str:
    """mp4 파일을 base64 data URI 로 인코딩한다(비디오 직접 전송용)."""
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:video/mp4;base64,{b64}"


def jpeg_to_data_uri(jpg: bytes) -> str:
    """JPEG 바이트를 image data URI 로 인코딩한다."""
    b64 = base64.b64encode(jpg).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"
