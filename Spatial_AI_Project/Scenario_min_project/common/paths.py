"""visionary-nvidia 데이터셋 경로 (clip 단위 중첩 레이아웃, 2026-09 재구성 반영).

2026-09-11 재구성: 신규 100 scene 이 `nvidia/<clip>/`(구 `unified_dataset/nvidia/<clip>/`
아님)에 있다. `unified_dataset/nvidia/`는 이 재구성으로 비워졌다(구 clip 전량 디스크에서
사라짐 — clip_ids() 의 curation split 은 신규 100 clip 과 교집합 0, 별도 확인 필요).

레이아웃: nvidia/<clip>/{sensor,processed,anno,index.parquet}
  sensor/<cam>/<frame_id>.jpg (+ lidar_top_360fov/<frame_id>.npy) — **mp4 없음**(구조와
    무관하게 파일 자체가 없음). 프레임별 정지영상 갤러리로 대체됨. video_path()/camera_ts()
    (mp4·timestamps.parquet 기대)는 이 신규 clip 에 대해 존재하지 않는 경로를 반환한다 —
    호출측(dataset.video_meta 등, cv2 기반)이 실패한다. 이 파일 재구성 범위 밖(영상 수집
    경로 전면 재설계 필요, pipeline-integrator/vlm-engineer 소관)이라 손대지 않는다.
  index.parquet: scene 전체 프레임 인덱스(frame_idx, frame_id, timestamp_us 실값 — map/obj3d
    와 달리 0 아님, sensors 컬럼에 camera/lidar 파일명 매핑)
  processed/pose/v0.0.2/poses.parquet   egomotion 대응(구 labels/egomotion/<clip>.egomotion.parquet
    후신). frame_id·timestamp_us·x/y/z·qx/qy/qz/qw·vx/vy/vz·ax/ay/az·yaw_rate. index.parquet과
    frame 단위 1:1 정합(timestamp_us 동일값 실측 확인, 2 clip). v0.0.1 도 병존하지만 map
    v2.2.1.0·obj3d v1.15.0.0 의 version.json lineage.depends_on.pose 가 전부 "v0.0.2"를
    가리켜 이 버전을 고름(임의 선택 아님, 계보 실측 확인).
  anno/obj3d/v1.15.0.0/{det_frames,frames,tracks}.parquet   (3DOD pseudo-label, lidar frame
    x=전방 y=좌우, 11-DOF boxes_3d). 100개 중 2건 없음(0c1b6cbd..., 0b4ddc9a...).
  anno/map/v2.2.1.0/{frames,scene}.parquet   (native v1 레이아웃 — divider_polylines 등,
    구 centerlines/lane_ids/lane_type/area_points 로부터 컬럼명 변경. PREDICTION_SCHEMA.md 참고)

anno/map, anno/obj3d 둘 다 `source`="pseudo"·`reviewed`=false 전량(사람 GT 아님, Visionary
모델 pseudo-label — 사용자가 GT로 채택하기로 확정). 코드가 `source` 컬럼을 읽지 않으므로
이 provenance 사실은 코드 경로에 남지 않는다(2026-09-11 조사, disclosure 슬롯 부재 — 아래
참고).
"""

from pathlib import Path

ROOT = "/katech/datasets/visionary"
BASE = f"{ROOT}/unified_dataset/nvidia"
CAM = "camera_front_wide_120fov"
OBJ3D_VER = "v1.15.0.0"
MAP_VER = "v2.2.1.0"
POSE_VER = "v0.0.2"  # map/obj3d version.json lineage.depends_on.pose 실측 일치(2026-09-11)


def clip_dir(cid: str) -> Path:
    return Path(BASE) / cid


def video_path(cid: str, cam: str = CAM) -> Path:
    return clip_dir(cid) / "sensor" / cam / f"{cid}.{cam}.mp4"


def camera_ts(cid: str, cam: str = CAM) -> Path:
    return clip_dir(cid) / "sensor" / cam / f"{cid}.{cam}.timestamps.parquet"


def index_path(cid: str) -> Path:
    """scene 전체 프레임 인덱스(frame_idx/frame_id/timestamp_us 실값). mp4 없이 clip 길이·
    프레임-시각 매핑이 필요하면 이 파일이 신규 레이아웃의 대응 소스다(camera_ts() 후신 후보
    — 아직 events.py 배선은 안 함, 호출측 결정 전까지 값만 제공)."""
    return clip_dir(cid) / "index.parquet"


def egomotion_path(cid: str) -> Path:
    return clip_dir(cid) / "processed" / "pose" / POSE_VER / "poses.parquet"


def obj3d_frames(cid: str) -> Path:
    return clip_dir(cid) / "anno" / "obj3d" / OBJ3D_VER / "frames.parquet"


def map_frames(cid: str) -> Path:
    return clip_dir(cid) / "anno" / "map" / MAP_VER / "frames.parquet"


def clip_ids(split: str = "diverse_set-test") -> list[str]:
    split = split.replace(".txt", "")
    f = Path(ROOT) / "curation" / f"{split}.txt"
    return [x.strip() for x in f.read_text().splitlines() if x.strip()]


# obj3d class::subclass → 어휘 object_type
OBJ3D_CLASS_MAP = {
    "vehicle::car": "vehicle", "vehicle::truck": "large_vehicle",
    "vehicle::bus": "large_vehicle", "vehicle::trailer": "large_vehicle",
    "vehicle::construction_vehicle": "large_vehicle",
    "pedestrian::pedestrian": "pedestrian",
    "twowheeler::bicycle": "bicycle_micromobility",
    "twowheeler::twowheeler": "bicycle_micromobility",
    "twowheeler::motorcycle": "motorcycle",
    "object::traffic_cone": "other", "object::bollard": "other",
}
