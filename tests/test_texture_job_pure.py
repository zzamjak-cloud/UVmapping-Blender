"""Blender 없이 실행하는 TextureJob 생성 회귀 테스트.

애드온이 UV를 만들지 않고 사용자가 펼쳐 둔 UV를 그대로 계약으로 옮기므로,
같은 UV에서 같은 해시가 나오는지와 베이크를 망칠 UV를 미리 막는지를 검사한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_job import (
    TEXTURE_JOB_PROPERTY,
    build_texture_jobs,
    ensure_texture_jobs,
)


_IDENTITY = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


@dataclass
class _Vertex:
    index: int
    co: tuple[float, float, float]


@dataclass
class _Edge:
    index: int
    vertices: tuple[int, int]
    use_seam: bool = False


@dataclass
class _Polygon:
    index: int
    vertices: tuple[int, ...]
    loop_indices: tuple[int, ...]
    material_index: int = 0


@dataclass
class _UVLoop:
    uv: tuple[float, float]


@dataclass
class _UVLayer:
    name: str
    data: list[_UVLoop]


class _UVLayers(list):
    @property
    def active(self):
        return self[0] if self else None

    def get(self, name: str):
        return next((layer for layer in self if layer.name == name), None)


@dataclass
class _Mesh:
    name: str
    vertices: list[_Vertex]
    edges: list[_Edge]
    polygons: list[_Polygon]
    uv_layers: _UVLayers

    @property
    def name_full(self) -> str:
        return self.name


@dataclass
class _Object:
    name: str
    data: _Mesh
    matrix_world: tuple = _IDENTITY
    custom: dict = field(default_factory=dict)

    @property
    def name_full(self) -> str:
        return self.name

    def as_pointer(self) -> int:
        return id(self)

    def __setitem__(self, key: str, value) -> None:
        self.custom[key] = value

    def get(self, key: str, default=None):
        return self.custom.get(key, default)


@dataclass
class _Settings:
    texture_resolution: str = "1024"
    padding_pixels: int = 16


def _quad_mesh(
    name: str,
    uvs: tuple[tuple[float, float], ...],
    *,
    with_uv_layer: bool = True,
) -> _Mesh:
    coordinates = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    face = (0, 1, 2, 3)
    edges = [
        _Edge(index=index, vertices=(first, second))
        for index, (first, second) in enumerate(
            ((0, 1), (1, 2), (2, 3), (0, 3))
        )
    ]
    layers = _UVLayers()
    if with_uv_layer:
        layers.append(_UVLayer(name="UVMap", data=[_UVLoop(uv) for uv in uvs]))
    return _Mesh(
        name=name,
        vertices=[_Vertex(index=index, co=co) for index, co in enumerate(coordinates)],
        edges=edges,
        polygons=[_Polygon(index=0, vertices=face, loop_indices=(0, 1, 2, 3))],
        uv_layers=layers,
    )


_LEFT_UVS = ((0.0, 0.0), (0.45, 0.0), (0.45, 0.45), (0.0, 0.45))
_RIGHT_UVS = ((0.55, 0.0), (1.0, 0.0), (1.0, 0.45), (0.55, 0.45))


def _expect_error(callable_, fragment: str) -> None:
    try:
        callable_()
    except ValueError as exc:
        assert fragment in str(exc), f"예상 문구 {fragment!r}가 없습니다: {exc}"
        return
    raise AssertionError(f"ValueError가 발생하지 않았습니다: {fragment!r}")


def test_separate_objects_share_one_atlas_contract() -> None:
    first = _Object("Left", _quad_mesh("LeftMesh", _LEFT_UVS))
    second = _Object("Right", _quad_mesh("RightMesh", _RIGHT_UVS))
    jobs = build_texture_jobs((first, second), resolution=1024, padding=16)

    left = jobs[first.as_pointer()]
    right = jobs[second.as_pointer()]
    assert left["atlas_id"] == right["atlas_id"]
    assert left["atlas_hash"] == right["atlas_hash"]
    assert left["atlas_member_id"] != right["atlas_member_id"]
    assert left["pack_shared_atlas"] is True
    assert left["uv_layer_name"] == "UVMap"
    assert left["target_resolution"] == (1024, 1024)
    assert left["requested_padding"] == 16
    assert left["packing_margin_uv"] == 16 / 1024
    assert left["settings"]["uv_source"] == "USER_AUTHORED"


def test_same_uv_produces_identical_hashes_on_recompute() -> None:
    obj = _Object("Panel", _quad_mesh("PanelMesh", _LEFT_UVS))
    first = build_texture_jobs((obj,), resolution=1024, padding=16)[obj.as_pointer()]
    second = build_texture_jobs((obj,), resolution=1024, padding=16)[obj.as_pointer()]

    for key in ("job_id", "atlas_id", "atlas_hash", "uv_hash", "mesh_hash"):
        assert first[key] == second[key]

    # UV가 바뀌면 베이크 단계가 감지할 수 있도록 해시도 바뀌어야 한다.
    obj.data.uv_layers[0].data[2].uv = (0.4, 0.4)
    changed = build_texture_jobs((obj,), resolution=1024, padding=16)[obj.as_pointer()]
    assert changed["uv_hash"] != first["uv_hash"]
    assert changed["mesh_hash"] != first["mesh_hash"]


def test_linked_duplicates_become_one_atlas_member() -> None:
    mesh = _quad_mesh("SharedMesh", _LEFT_UVS)
    first = _Object("Copy.001", mesh)
    second = _Object("Copy.002", mesh)
    jobs = build_texture_jobs((first, second), resolution=1024, padding=16)

    left = jobs[first.as_pointer()]
    right = jobs[second.as_pointer()]
    assert left["atlas_member_id"] == right["atlas_member_id"] == "SharedMesh"
    assert left["atlas_member_count"] == 1
    # 같은 Mesh를 공유하면 member가 하나이므로 shared Atlas가 아니다.
    assert left["pack_shared_atlas"] is False
    assert left["object_name"] == "Copy.001"
    assert right["object_name"] == "Copy.002"


def test_missing_uv_layer_is_rejected_with_guidance() -> None:
    obj = _Object("NoUV", _quad_mesh("NoUVMesh", _LEFT_UVS, with_uv_layer=False))
    _expect_error(
        lambda: build_texture_jobs((obj,), resolution=1024, padding=16),
        "UV 맵이 없습니다",
    )


def test_uv_outside_unit_square_is_rejected() -> None:
    outside = ((0.0, 0.0), (1.6, 0.0), (1.6, 0.9), (0.0, 0.9))
    obj = _Object("Overflow", _quad_mesh("OverflowMesh", outside))
    _expect_error(
        lambda: build_texture_jobs((obj,), resolution=1024, padding=16),
        "0-1 범위를 벗어났습니다",
    )


def test_overlapping_objects_are_rejected_but_single_object_is_allowed() -> None:
    first = _Object("A", _quad_mesh("AMesh", _LEFT_UVS))
    second = _Object("B", _quad_mesh("BMesh", _LEFT_UVS))
    _expect_error(
        lambda: build_texture_jobs((first, second), resolution=1024, padding=16),
        "UV가 서로 겹칩니다",
    )

    # 같은 UV라도 객체 하나만 구우면 겹침 판정 대상이 아니다.
    solo = build_texture_jobs((first,), resolution=1024, padding=16)
    assert solo[first.as_pointer()]["atlas_member_count"] == 1


def test_ensure_texture_jobs_writes_property_in_selection_order() -> None:
    first = _Object("Left", _quad_mesh("LeftMesh", _LEFT_UVS))
    second = _Object("Right", _quad_mesh("RightMesh", _RIGHT_UVS))
    ordered = ensure_texture_jobs((first, second), _Settings())

    assert len(ordered) == 2
    assert ordered[0]["object_name"] == "Left"
    assert ordered[1]["object_name"] == "Right"
    for obj, payload in ((first, ordered[0]), (second, ordered[1])):
        stored = obj.get(TEXTURE_JOB_PROPERTY)
        assert isinstance(stored, str) and payload["job_id"] in stored


def test_empty_selection_is_rejected() -> None:
    _expect_error(
        lambda: build_texture_jobs((), resolution=1024, padding=16),
        "Mesh 객체를 선택해 주세요",
    )


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 TextureJob 테스트 {len(tests)}/{len(tests)} 통과")
