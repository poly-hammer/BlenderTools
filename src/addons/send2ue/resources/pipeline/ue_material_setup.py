"""
UE Material Setup (JSON 기반 텍스처 직접 import)
================================================
send2ue post_import extension(send2ue_material_pipeline.py)이 import 직후
process_mesh(asset_path) 를 RPC 로 호출한다. 수동 실행도 가능.

블렌더 애드온이 남긴 JSON 사이드카(exports/<mesh_name>.json)를 읽어:
  1. 각 머티리얼의 텍스처를 디스크 파일에서 /Game/texture 로 직접 import
     → FBX 가 못 나르는 MetallicRoughness 포함 모든 맵이 확실히 들어온다.
  2. 텍스처 종류별로 sRGB / 노멀맵 압축을 설정.
  3. Create or load a preset material instance, then assign shared texture data.
  4. JSON 의 slot_index 로 메쉬 슬롯에 MI 할당.

가드: 머티리얼명에서 'M_' 를 뗀 것이 메쉬명과 정확히 같거나 '메쉬명_' 으로 시작할 때만 처리.
"""

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import unreal


UNREAL_INSTANCE_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PROTOTYPE_HANDOFF_KEY = "speedtree_prototype_handoff"
PROTOTYPE_METADATA_IDENTITY = "SpeedTree.PrototypeIdentity"
PROTOTYPE_METADATA_MEMBERS = "SpeedTree.PrototypeIdentityMembers"
PROTOTYPE_METADATA_OUTPUT = "SpeedTree.BlenderOutputContent"
PROTOTYPE_METADATA_SIDECAR = "SpeedTree.SidecarSHA256"


def _prototype_identity_api():
    module_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "prototype_identity.py",
    )
    spec = importlib.util.spec_from_file_location(
        "_send2ue_ue_prototype_identity", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "Could not load bundled prototype identity rules: " + module_path
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_prototype_handoff(data, required=False):
    value = data.get(PROTOTYPE_HANDOFF_KEY) if isinstance(data, dict) else None
    if value is None:
        if required:
            raise ValueError(
                "SpeedTree sidecar has no content-addressed prototype handoff"
            )
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "prototype_identity",
        "prototype_identity_members",
        "blender_geometry_content",
        "output_content",
    }:
        raise ValueError("SpeedTree prototype handoff schema is incomplete or unknown")
    if value.get("schema_version") != 2:
        raise ValueError("SpeedTree prototype handoff schema_version is unsupported")
    identity_api = _prototype_identity_api()
    identity = identity_api.validate_lineage(
        value.get("prototype_identity"),
        value.get("prototype_identity_members"),
    )
    try:
        blender_geometry = identity_api.validate_identity(
            value.get("blender_geometry_content"),
            expected_kind="speedtree_blender_export_geometry_content",
        )
    except ValueError as exc:
        raise ValueError(
            "SpeedTree Blender geometry content identity is invalid"
        ) from exc
    try:
        output = identity_api.validate_file_content_identity(
            value.get("output_content"),
            identity_api.BLENDER_FBX_CONTENT_KIND,
        )
    except ValueError as exc:
        raise ValueError(
            "SpeedTree Blender output content identity is invalid"
        ) from exc
    return {
        "schema_version": 2,
        "prototype_identity": identity,
        "prototype_identity_members": value[
            "prototype_identity_members"
        ],
        "blender_geometry_content": blender_geometry,
        "output_content": output,
    }


def _validate_current_prototype_export_payload(
    data,
    export_file_path,
    required=False,
):
    handoff = _validate_prototype_handoff(data, required=required)
    if handoff is None:
        return None
    path = str(export_file_path or "").strip()
    if not path:
        raise ValueError(
            "SpeedTree prototype handoff has no exact current FBX export path"
        )
    identity_api = _prototype_identity_api()
    try:
        current = identity_api.file_content_identity(
            path,
            identity_api.BLENDER_FBX_CONTENT_KIND,
        )
    except OSError as exc:
        raise ValueError(
            f"SpeedTree current FBX export payload is unreadable: {path}: {exc}"
        ) from exc
    if current != handoff["output_content"]:
        raise ValueError(
            "SpeedTree current FBX export payload does not match sidecar output content"
        )
    return handoff


def _candidate_contract_paths():
    override = os.environ.get("SUBSTANCE_TOOLS_PIPELINE_CONTRACT")
    if override:
        yield override

    here = os.path.abspath(__file__)
    current = os.path.dirname(here)
    while current and os.path.dirname(current) != current:
        yield os.path.join(current, "pipeline_contract.json")
        yield os.path.join(current, "substance-tools", "pipeline_contract.json")
        yield os.path.join(current, "substance_tools", "pipeline_contract.json")
        current = os.path.dirname(current)

    appdata = os.environ.get("APPDATA")
    if appdata:
        blender_root = os.path.join(appdata, "Blender Foundation", "Blender")
        if os.path.isdir(blender_root):
            for version in sorted(os.listdir(blender_root), reverse=True):
                yield os.path.join(
                    blender_root,
                    version,
                    "scripts",
                    "addons",
                    "substance_tools",
                    "pipeline_contract.json",
                )


def _pipeline_contract():
    for path in _candidate_contract_paths():
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
    return {}


def _contract_path_mapping():
    return _pipeline_contract().get("unreal_path_mapping", {}).get("current_default", {})


_SPEEDTREE_HANDOFF_API_UNSET = object()
_SPEEDTREE_HANDOFF_API = _SPEEDTREE_HANDOFF_API_UNSET


def _candidate_speedtree_handoff_api_paths():
    seen = set()
    for contract_path in _candidate_contract_paths():
        module_path = os.path.join(
            os.path.dirname(os.path.abspath(contract_path)),
            "speedtree_handoff_contract.py",
        )
        normalized = os.path.normcase(module_path)
        if normalized in seen:
            continue
        seen.add(normalized)
        yield module_path


def _speedtree_handoff_api():
    """Load the dependency-free shared rules without adding a repo to sys.path."""
    global _SPEEDTREE_HANDOFF_API
    if _SPEEDTREE_HANDOFF_API is not _SPEEDTREE_HANDOFF_API_UNSET:
        return _SPEEDTREE_HANDOFF_API

    for module_path in _candidate_speedtree_handoff_api_paths():
        if not os.path.isfile(module_path):
            continue
        module_name = "_send2ue_speedtree_handoff_contract"
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            _SPEEDTREE_HANDOFF_API = module
            return module
        except Exception as exc:
            _warn(
                "shared SpeedTree handoff API load failed: "
                f"{module_path} ({exc})"
            )
            sys.modules.pop(module_name, None)

    _SPEEDTREE_HANDOFF_API = None
    return None

# ─── 설정 ────────────────────────────────────────────────────────────────────
DEFAULT_MASTER_PRESET = "prop"
MASTER_PRESETS = {
    "gemstone": {
        "master": "/Game/Materials/Gemstone/M_Gemstone_Layered_Substrate",
        "mi_folder": "/Game/Materials/Gemstone/MI",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Materials/Gemstone/Layers/ML_Gemstone_Crystal",
        "layer_instance_folder": "/Game/Materials/Gemstone/MYI",
        "layer_texture_remap": {},
        "virtual_textures": False,
    },
    "prop": {
        "master": "/Game/Material/AssetSurface/Master/M_AssetSurface_Master",
        "mi_folder": "/Game/Material/AssetSurface/MI/Surface",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_Mesh_UV0",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/Surface",
        "layer_texture_remap": {
            "Albedo": "Albedo",
            "Extra": "Extra",
            "Normal": "Normal",
            "Height": "Height",
            "Opacity Map": "Opacity Map",
            "Subsurface": "Subsurface",
        },
        "virtual_textures": True,
    },
    "layer": {
        "master": "/Game/Material/AssetSurface/Master/M_LayerBlend",
        "mi_folder": "/Game/Material/AssetSurface/MI/LayerBlend",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_Mesh_UV0",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/LayerBlend",
        "layer_instance_strip_prefixes": ["LayerBlend_"],
        "layer_texture_remap": {
            "Albedo": "Albedo",
            "Extra": "Extra",
            "Normal": "Normal",
            "Height": "Height",
            "Opacity Map": "Opacity Map",
            "Subsurface": "Subsurface",
        },
        "virtual_textures": True,
    },
    "fabric_twosided": {
        "master": "/Game/Material/AssetSurface/Master/M_FabricTwoSided_Master",
        "mi_folder": "/Game/Material/AssetSurface/MI/FabricTwoSided",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_FabricTwoSided",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/FabricTwoSided",
        "layer_texture_remap": {
            "Albedo": "BaseColor", "Extra": "ORM", "Normal": "Normal",
            "Sheen Color": "Fuzz Color Map", "Sheen Opacity": "Fuzz Mask", "Sheen Roughness": "Fuzz Roughness Map",
            "Backface Albedo": "Backface BaseColor", "Backface Extra": "Backface ORM", "Backface Normal": "Backface Normal",
            "Backface Sheen Color": "Backface Fuzz Color Map", "Backface Sheen Opacity": "Backface Fuzz Mask", "Backface Sheen Roughness": "Backface Fuzz Roughness Map",
            "Opacity Map": "Opacity Map", "Backface Opacity Map": "Backface Opacity Map",
        },
        "virtual_textures": True,
    },
    "cloth": {
        "master": "/Game/Material/AssetSurface/Master/M_Coat_Fabric_Substrate_Master",
        "mi_folder": "/Game/Material/AssetSurface/MI/Cloth",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_Cloth",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/Cloth",
        "layer_texture_remap": {
            "Albedo": "BaseColor",
            "Extra": "ORM",
            "Normal": "Normal",
            "Sheen Color": "Fuzz Color Map",
            "Sheen Opacity": "Fuzz Mask",
            "Sheen Roughness": "Fuzz Roughness Map",
        },
        "virtual_textures": True,
    },
    "asset_surface": {
        "master": "/Game/Material/AssetSurface/Master/M_AssetSurface_Master",
        "mi_folder": "/Game/Material/AssetSurface/MI/Surface",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_Mesh_UV0",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/Surface",
        "layer_texture_remap": {
            "Albedo": "Albedo",
            "Extra": "Extra",
            "Normal": "Normal",
            "Height": "Height",
            "Opacity Map": "Opacity Map",
            "Subsurface": "Subsurface",
        },
        "virtual_textures": True,
    },
    "coat": {
        "master": "/Game/Material/AssetSurface/Master/M_Coat_Fabric_Substrate_Master",
        "mi_folder": "/Game/Material/AssetSurface/MI/Cloth",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/AssetSurface/Master/MaterialLayer/MY_Cloth",
        "layer_instance_folder": "/Game/Material/AssetSurface/MYI/Cloth",
        "layer_texture_remap": {
            "Albedo": "BaseColor",
            "Extra": "ORM",
            "Normal": "Normal",
            "Sheen Color": "Fuzz Color Map",
            "Sheen Opacity": "Fuzz Mask",
            "Sheen Roughness": "Fuzz Roughness Map",
        },
        "virtual_textures": True,
    },
    "hair": {
        "master": "/Game/Material/HairTool/Master/M_HT_HairCards",
        "mi_folder": "/Game/Material/HairTool/MI",
        "assignment": "none",
        "virtual_textures": None,
        "create_if_missing": True,
        "exclude_path_fragments": [],
    },
    "tree": {
        "master": "/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Master",
        "masters_by_shading": {
            "wood": "/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Master",
            "foliage": "/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Foliage_Master",
            "stem": "/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Stem_Master",
        },
        "mi_folder": "/Game/Material/Tree/AssetTree/MI",
        "assignment": "material_layer_instance",
        "layer_parent": "/Game/Material/Tree/AssetTree/Master/MaterialLayer/MY_Tree_Bark",
        "layer_instance_folder": "/Game/Material/Tree/AssetTree/MYI",
        "layer_parents_by_name": {
            "bark": "/Game/Material/Tree/AssetTree/Master/MaterialLayer/MY_Tree_Bark",
            "branch": "/Game/Material/Tree/AssetTree/Master/MaterialLayer/MY_Tree_Branch",
            "leaf": "/Game/Material/Tree/AssetTree/Master/MaterialLayer/MY_Tree_Leaf",
        },
        "layer_texture_remap": {
            "Albedo": "Albedo",
            "Extra": "Extra",
            "Normal": "Normal",
            "Height": "Height",
            "Subsurface": "Subsurface",
        },
        "virtual_textures": True,
    },
}
# 반투명(유리) 머티리얼은 전용 MI 를 만들지 않고 이 공유 글래스 MI 를 슬롯에 직접 할당한다.
# (Megascan 글래스를 프로젝트로 localize 한 인스턴스. 부모 M_MS_Glass_Material, TRANSLUCENT)
GLASS_MI_PATH        = "/Game/Material/AssetSurface/MI/MI_Prop_Glass_01"
TEXTURES_FOLDER      = "/Game/texture"
EXPORT_DIR           = r"C:/Users/PARK/Documents/UE_Blender_Pipeline/exports"
_PATH_MAPPING        = _contract_path_mapping()
_LOCAL_ANCHOR        = str(_PATH_MAPPING.get("local_anchor") or "Forestportfolio").strip("/\\")
JSON_SEARCH_ROOTS    = [
    os.path.join(r"C:/Users/PARK/OneDrive", _LOCAL_ANCHOR),
]
_D_DRIVE_ANCHOR      = os.path.join(r"D:/OneDrive", _LOCAL_ANCHOR)
if os.path.isdir(_D_DRIVE_ANCHOR) and _D_DRIVE_ANCHOR not in JSON_SEARCH_ROOTS:
    JSON_SEARCH_ROOTS.append(_D_DRIVE_ANCHOR)
# /Game/Meshes/<rel> 은 디스크의 JSON_SEARCH_ROOTS[0]/<rel> 에 1:1 대응(send2ue 자동경로 규칙).
# 이를 이용해 mesh_path 로부터 해당 프롭 폴더만 좁혀 JSON 을 찾는다 → 3만개 트리 전체 walk 회피.
GAME_MESHES_PREFIX   = f"{str(_PATH_MAPPING.get('unreal_anchor') or '/Game/Meshes').rstrip('/')}/"
DELETE_IMPORTED_SOURCE_MATERIALS = False
DELETE_IMPORTED_SOURCE_TEXTURES = False

# Shared surface-layer texture parameter names (JSON 의 param 과 동일해야 연결됨)
KNOWN_PARAMS = {
    "Backface Opacity Map",
    "Backface Albedo", "Backface Extra", "Backface Normal",
    "Backface Sheen Color", "Backface Sheen Opacity", "Backface Sheen Roughness",
    "Albedo",
    "Extra",
    "Normal",
    "Height",
    "Transmission",
    "Subsurface",
    "Emissive",
    "Sheen Color",
    "Sheen Opacity",
    "Sheen Roughness",
    "Moss Blend Mask",
    "Flow Map",
    "IRD Map",
    "ORM Map",
    "Opacity Map",
}
LAYER_PARAM_BY_LEGACY_PARAM = {
    "BaseColor": "Albedo",
    "MetallicRoughness": "Extra",
    "Roughness": "Extra",
    "Metallic": "Extra",
    "Occlusion": "Extra",
    "Normal": "Normal",
    "Height": "Height",
    "Alpha": "Transmission",
    "Emissive": "Emissive",
    "SheenColor": "Sheen Color",
    "SheenOpacity": "Sheen Opacity",
    "SheenRoughness": "Sheen Roughness",
    "Texture": "Albedo",
}

# Unreal-owned Hair Tool controls. Blender sidecars intentionally omit these,
# and existing instance overrides must survive every later re-export.
HAIR_INSTANCE_OWNED_SCALAR_PARAMETERS = {
    "System Color Influence",
    "System Mask Contrast",
    "System Mask Bias",
    "System Mask Invert",
    "Roughness Multiplier",
}
HAIR_INSTANCE_OWNED_VECTOR_PARAMETERS = {
    "System Color 01",
    "System Color 02",
}
FLAT_PARAM_BY_LAYER_PARAM = {
    "Albedo": "BaseColor",
    "Extra": "MetallicRoughness",
    "Normal": "Normal",
    "Emissive": "Emissive",
}
COAT_PARAM_BY_LAYER_PARAM = {
    "Albedo": "BaseColor",
    "Extra": "ORM",
    "Normal": "Normal",
}
DEFAULT_CLOTH_TEXTURE_PARAM_BY_LAYER_PARAM = {
    "Albedo": "BaseColor",
    "Extra": "ORM",
    "Normal": "Normal",
    "Sheen Color": "Fuzz Color Map",
    "Sheen Opacity": "Fuzz Mask",
    "Sheen Roughness": "Fuzz Roughness Map",
}


def _cloth_texture_param_by_layer_param():
    contract = _pipeline_contract()
    handoff_remap = (
        contract.get("unreal_handoff_sidecar", {})
        .get("cloth_master_param_remap", {})
    )
    material_layer_remap = handoff_remap.get("material_layer", {})
    legacy_remap = (
        contract.get("unreal_material_json", {})
        .get("cloth_master_param_remap", {})
    )
    mapping = (
        material_layer_remap.get("texture_remap")
        or handoff_remap.get("texture_remap")
        or legacy_remap.get("material_layer", {}).get("texture_remap")
        or legacy_remap.get("texture_remap")
    )
    if isinstance(mapping, dict) and mapping:
        merged = dict(DEFAULT_CLOTH_TEXTURE_PARAM_BY_LAYER_PARAM)
        merged.update({str(key): str(value) for key, value in mapping.items()})
        return merged
    return dict(DEFAULT_CLOTH_TEXTURE_PARAM_BY_LAYER_PARAM)


CLOTH_TEXTURE_PARAM_BY_LAYER_PARAM = _cloth_texture_param_by_layer_param()
ASSET_SURFACE_PARAM_BY_LAYER_PARAM = {
    "Albedo": "Albedo",
    "Extra": "Extra",
    "Normal": "Normal",
    "Height": "Height",
    "Transmission": "Transmission",
    "Moss Blend Mask": "Moss Blend Mask",
}

# Send to Unreal keeps the source texture stem in the asset name.  These
# suffixes are the source of truth for the tree texture set, even when an
# older sidecar uses a generic/legacy parameter name.
TEXTURE_PARAM_BY_NAME_SUFFIX = (
    ("_color", "Albedo"),
    ("_extra", "Extra"),
    ("_height", "Height"),
    ("_normal", "Normal"),
    ("_opacity", "Opacity"),
    ("_subsurface", "Subsurface"),
)

# Virtual Texture Streaming을 쓰므로 모든 텍스처의 인게임 최대 해상도는 제한하지 않는다.
DEFAULT_MAX_TEXTURE_SIZE = 0
ENABLE_VIRTUAL_TEXTURE_STREAMING = True
OPACITY_ALPHA_COVERAGE_THRESHOLD = 0.3333
# import 되는 StaticMesh 를 자동으로 Nanite 로 등록할지(반투명 머티리얼 메쉬는 자동 제외).
ENABLE_NANITE = True
ENABLE_SKELETAL_NANITE_VOXELIZE = True
DYNAMIC_WIND_JSON_SUFFIX = "_dynamic_wind_import_from_megaplant_groups.json"
# ─────────────────────────────────────────────────────────────────────────────


# 텍스처 해시 캐시. 기존 asset path -> mtime(float) 엔트리는 읽을 수 있지만,
# AssetImportData FileMD5와 실제 설정을 검증한 뒤에만 v2 dict로 지연 마이그레이션한다.
TEXTURE_IMPORT_CACHE = os.path.join(EXPORT_DIR, "_texture_import_cache.json")
TEXTURE_CACHE_ENTRY_VERSION = 2
_TEXTURE_ASSET_SEARCH_CACHE = {}
_TEXTURE_ASSET_REGISTRY_INDEX = None


def _load_texture_cache() -> dict:
    try:
        with open(TEXTURE_IMPORT_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_texture_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(TEXTURE_IMPORT_CACHE), exist_ok=True)
        with open(TEXTURE_IMPORT_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        _warn(f"텍스처 캐시 저장 실패(무시): {e}")


def _log(msg):
    unreal.log(f"[Blender Pipeline] {msg}")


def _warn(msg):
    unreal.log_warning(f"[Blender Pipeline] {msg}")


def _mesh_path_to_disk_folder(mesh_path: str):
    """import 된 메쉬의 /Game/Meshes 경로를 디스크 프롭 폴더로 역산. 매핑 밖이면 None.
    예: /Game/Meshes/00_common/prop/Prop_X → <root>/00_common/prop"""
    if not mesh_path or not mesh_path.startswith(GAME_MESHES_PREFIX) or not JSON_SEARCH_ROOTS:
        return None
    rel = mesh_path[len(GAME_MESHES_PREFIX):]
    rel_folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return os.path.join(JSON_SEARCH_ROOTS[0], rel_folder.replace("/", os.sep))


def _walk_for_json(roots, filename):
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            if filename in filenames:
                found.append(os.path.join(dirpath, filename))
    return found


def _find_json_path(mesh_name: str, mesh_path: str = None):
    # 빠른 경로: mesh_path 로 프롭 폴더를 역산해 그 작은 서브트리만 walk(3만개 OneDrive 트리 전체 walk 회피).
    # 못 찾으면 기존처럼 JSON_SEARCH_ROOTS 전체로 폴백한다.
    filename = f"{mesh_name}.json"
    candidates = []

    disk_folder = _mesh_path_to_disk_folder(mesh_path)
    if disk_folder and os.path.isdir(disk_folder):
        candidates = _walk_for_json([disk_folder], filename)

    if not candidates:
        candidates = _walk_for_json([r for r in JSON_SEARCH_ROOTS if os.path.isdir(r)], filename)

    legacy_path = os.path.join(EXPORT_DIR, filename)
    if os.path.exists(legacy_path):
        candidates.append(legacy_path)

    if not candidates:
        return None
    unique_candidates = {
        os.path.normcase(os.path.abspath(path)): path
        for path in candidates
    }
    if len(unique_candidates) != 1:
        raise RuntimeError(
            "ambiguous JSON sidecar fallback for "
            f"{mesh_name}: "
            + "; ".join(sorted(unique_candidates.values()))
        )
    return next(iter(unique_candidates.values()))


def _load_json(
    mesh_name: str,
    explicit_path: str = None,
    mesh_path: str = None,
    expected_sha256: str = "",
):
    # extension 이 정확한 JSON 경로를 넘겨주면 walk 를 건너뛴다. 없으면 mesh_path 로 폴더를 좁힌다.
    if explicit_path:
        if not os.path.isfile(explicit_path):
            raise RuntimeError(f"explicit JSON sidecar is missing: {explicit_path}")
        path = explicit_path
    else:
        path = _find_json_path(mesh_name, mesh_path)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            payload = f.read()
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if expected_sha256 and actual_sha256 != str(expected_sha256).casefold():
            raise RuntimeError(
                "JSON sidecar content changed after Blender export: "
                f"{path}"
            )
        data = json.loads(payload.decode("utf-8"))
        _log(f"JSON sidecar: {path}")
        return data
    except Exception as e:
        if explicit_path:
            raise RuntimeError(
                f"explicit JSON sidecar could not be read: {path} ({e})"
            ) from e
        _warn(f"JSON 읽기 실패 ({mesh_name}.json): {e}")
        return None


def _speedtree_sidecar_descriptor(data: dict):
    if not isinstance(data, dict):
        return None
    value = data.get("speedtree_handoff_contract")
    return value if isinstance(value, dict) else None


def _validate_speedtree_handoff_contract(
    data: dict,
    expected_mesh_name: str,
    mesh_path: str = "",
    export_file_path: str = "",
):
    """Validate new contract-authored sidecars before any Unreal mutation.

    Descriptor-free non-tree sidecars retain legacy compatibility. Any tree
    marker requires the current descriptor and material intent contract.
    """
    materials = data.get("materials", []) if isinstance(data, dict) else []
    for entry in materials:
        if not isinstance(entry, dict) or not entry.get("speedtree_intent"):
            continue
        if entry.get("tree_shading") != "foliage":
            continue
        layers = _entry_layers(entry, {"key": "tree"})
        if not any(
            texture.get("param") == "Albedo" and texture.get("file")
            for layer in layers for texture in layer.get("textures", [])
        ):
            raise RuntimeError(
                "SpeedTree foliage has no Albedo payload; import blocked before mutation: "
                + str(entry.get("name") or "<unnamed>")
            )
    has_intent = any(
        isinstance(entry, dict) and "speedtree_intent" in entry
        for entry in materials
    )
    has_tree_entry = any(
        isinstance(entry, dict)
        and str(entry.get("master_preset") or "").strip().casefold() == "tree"
        for entry in materials
    )
    has_tree_root = str(
        (
            data.get("material_master")
            or data.get("master_material")
            or data.get("master_preset")
            or ""
        )
        if isinstance(data, dict)
        else ""
    ).strip().casefold() == "tree"
    descriptor = _speedtree_sidecar_descriptor(data)
    requires_speedtree_contract = bool(
        descriptor is not None
        or has_intent
        or has_tree_entry
        or has_tree_root
        or _is_tree_asset_path(mesh_path)
    )
    try:
        _validate_current_prototype_export_payload(
            data,
            export_file_path,
            # Prototype identity is opt-in, independent of material preset.
            # Unmarked existing SpeedTree and prop sidecars keep their contract.
            required=False,
        )
    except ValueError as exc:
        raise RuntimeError(
            "SpeedTree handoff contract preflight blocked before mutation: "
            + str(exc)
        ) from exc

    if not requires_speedtree_contract:
        return None

    contract_api = _speedtree_handoff_api()
    if contract_api is None:
        raise RuntimeError(
            "SpeedTree handoff contract preflight blocked before mutation: "
            "shared speedtree_handoff_contract.py is unavailable"
        )
    if descriptor is None:
        raise RuntimeError(
            "SpeedTree handoff contract preflight blocked before mutation: "
            "tree sidecar has no speedtree_handoff_contract"
        )

    errors = []
    try:
        descriptor = contract_api.validate_sidecar_descriptor(
            descriptor,
            expected_mesh_name=expected_mesh_name,
        )
    except Exception as exc:
        errors.append(str(exc))

    json_mesh_name = str(data.get("mesh_name") or "").strip()
    if not json_mesh_name:
        errors.append("contract sidecar has no mesh_name")
    elif json_mesh_name.casefold() != str(expected_mesh_name or "").strip().casefold():
        errors.append(
            f"sidecar mesh_name mismatch: {json_mesh_name!r} != "
            f"{expected_mesh_name!r}"
        )

    for entry_index, entry in enumerate(materials):
        if not isinstance(entry, dict):
            errors.append(f"materials[{entry_index}] is not an object")
            continue
        is_tree = str(entry.get("master_preset") or "").strip().casefold() == "tree"
        intent = entry.get("speedtree_intent")
        if is_tree and intent is None:
            errors.append(
                f"{entry.get('name', '<unnamed>')}: tree entry has no speedtree_intent"
            )
            continue
        if intent is None:
            continue
        if not is_tree:
            errors.append(
                f"{entry.get('name', '<unnamed>')}: speedtree_intent requires master_preset 'tree'"
            )
            continue
        try:
            validated = contract_api.validate_material_intent_for_name(
                intent,
                str(entry.get("name") or ""),
            )
            expected = contract_api.build_material_intent(
                str(entry.get("name") or ""),
                explicit_tree_part=str(entry.get("tree_part") or ""),
                explicit_tree_shading=str(entry.get("tree_shading") or ""),
                instance_profile=str(entry.get("instance_profile") or ""),
            )
            for key, expected_value in expected.items():
                if validated.get(key) != expected_value:
                    raise ValueError(
                        f"speedtree_intent {key} mismatch: "
                        f"{validated.get(key)!r} != {expected_value!r}"
                    )
            if expected.get("instance_profile"):
                entry_mode = str(
                    entry.get("material_instance_mode") or ""
                ).strip().casefold()
                if entry_mode != expected.get("material_instance_mode"):
                    raise ValueError(
                        f"material_instance_mode mismatch: {entry_mode!r} != "
                        f"{expected.get('material_instance_mode')!r}"
                    )
        except Exception as exc:
            errors.append(f"{entry.get('name', '<unnamed>')}: {exc}")

    if errors:
        raise RuntimeError(
            "SpeedTree handoff contract preflight blocked before mutation: "
            + " | ".join(errors)
        )
    return descriptor


def _vector4_components(value):
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(float(component) for component in value)
    components = []
    for property_name in ("x", "y", "z", "w"):
        try:
            component = getattr(value, property_name)
        except Exception:
            try:
                component = value.get_editor_property(property_name)
            except Exception:
                return None
        components.append(float(component))
    return tuple(components)


def _editor_values_match(current, expected) -> bool:
    current_vector = _vector4_components(current)
    expected_vector = _vector4_components(expected)
    if current_vector is not None and expected_vector is not None:
        return all(
            abs(actual - wanted) <= 1.0e-6
            for actual, wanted in zip(current_vector, expected_vector)
        )
    return current == expected


def _set_texture_property_if_changed(tex, property_name: str, value) -> bool:
    try:
        current = tex.get_editor_property(property_name)
    except Exception:
        return False
    if _editor_values_match(current, value):
        return False
    tex.set_editor_property(property_name, value)
    return True


def _persist_prototype_metadata(
    mesh,
    data,
    sidecar_sha256: str,
    export_file_path: str = "",
) -> bool:
    handoff = _validate_current_prototype_export_payload(
        data,
        export_file_path,
        required=False,
    )
    if handoff is None:
        return False
    sidecar_digest = str(sidecar_sha256 or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", sidecar_digest) is None:
        raise RuntimeError(
            "Prototype metadata requires the exact Blender-selected sidecar sha256"
        )
    values = {
        PROTOTYPE_METADATA_IDENTITY: json.dumps(
            handoff["prototype_identity"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        PROTOTYPE_METADATA_MEMBERS: json.dumps(
            handoff["prototype_identity_members"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        PROTOTYPE_METADATA_OUTPUT: json.dumps(
            handoff["output_content"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        PROTOTYPE_METADATA_SIDECAR: sidecar_digest,
    }
    library = unreal.EditorAssetLibrary
    if not all(
        hasattr(library, name)
        for name in ("get_metadata_tag", "set_metadata_tag")
    ):
        raise RuntimeError("Unreal metadata tag API is unavailable")
    changed = False
    for key, value in values.items():
        if str(library.get_metadata_tag(mesh, key) or "") == value:
            continue
        if library.set_metadata_tag(mesh, key, value) is False:
            raise RuntimeError(f"Could not set Unreal prototype metadata tag {key}")
        changed = True
    return changed


def _texture_param_from_name(file_path=None, asset_name=None):
    """Return the canonical import role for a known texture-name suffix."""
    for value in (file_path, asset_name):
        if not value:
            continue
        stem = os.path.splitext(os.path.basename(str(value).replace("\\", "/")))[0].lower()
        for suffix, role in TEXTURE_PARAM_BY_NAME_SUFFIX:
            if stem.endswith(suffix):
                return role
    return None


def _effective_texture_param(param: str, file_path=None, asset_name=None) -> str:
    # A recognized filename suffix wins over a legacy JSON role. This keeps
    # existing MYI/MI JSON contracts working while fixing tree map imports.
    role = _texture_param_from_name(file_path, asset_name) or str(param or "")
    return role.removeprefix("Backface ")


def _file_md5(file_path: str) -> str:
    digest = hashlib.md5()
    with open(file_path, "rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _texture_source_fingerprint(file_path: str, cached_entry=None):
    # Cache metadata is diagnostic only. A strict content gate must hash the
    # current source even when mtime/size match a previously verified entry.
    del cached_entry
    stat_result = os.stat(file_path)
    mtime_ns = int(
        getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))
    )
    size = int(stat_result.st_size)
    source_md5 = _file_md5(file_path)
    return source_md5, {
        "version": TEXTURE_CACHE_ENTRY_VERSION,
        "source_path": os.path.normcase(os.path.abspath(file_path)),
        "mtime_ns": mtime_ns,
        "size": size,
        "md5": source_md5,
    }


def _asset_data_tag_value(asset_data, tag_name: str) -> str:
    if asset_data is None:
        return ""
    try:
        value = asset_data.get_tag_value(tag_name)
    except Exception:
        try:
            value = unreal.AssetRegistryHelpers.get_tag_value(asset_data, tag_name)
        except Exception:
            value = ""
    if isinstance(value, tuple):
        value = next((item for item in reversed(value) if isinstance(item, str)), "")
    if value:
        return str(value)
    try:
        tags = asset_data.tags_and_values
        return str(tags.get(tag_name, ""))
    except Exception:
        return ""


def _asset_import_file_md5(asset_path: str):
    try:
        asset_data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    except Exception:
        return None
    raw_value = _asset_data_tag_value(asset_data, "AssetImportData")
    if not raw_value:
        return None

    try:
        source_files = json.loads(raw_value)
    except (TypeError, ValueError):
        source_files = []
    if isinstance(source_files, dict):
        source_files = [source_files]
    for source_file in source_files if isinstance(source_files, list) else []:
        if not isinstance(source_file, dict):
            continue
        file_md5 = str(source_file.get("FileMD5") or "").lower()
        if re.fullmatch(r"[0-9a-f]{32}", file_md5) and file_md5 != "0" * 32:
            return file_md5

    match = re.search(r'\bFileMD5["\']?\s*[:=]\s*["\']?([0-9a-fA-F]{32})', raw_value)
    if match and match.group(1) != "0" * 32:
        return match.group(1).lower()
    return None


def _desired_texture_settings(
    param: str,
    virtual_texture_streaming=None,
    file_path=None,
    asset_name=None,
) -> dict:
    del virtual_texture_streaming  # Project policy: every imported texture uses VT streaming.
    param = _effective_texture_param(param, file_path, asset_name)
    if param == "Normal":
        srgb = False
        compression = unreal.TextureCompressionSettings.TC_NORMALMAP
    elif param in {
        "Extra",
        "MetallicRoughness",
        "Roughness",
        "Metallic",
        "Occlusion",
        "Sheen Opacity",
        "Sheen Roughness",
        "Flow Map",
        "IRD Map",
        "ORM Map",
    }:
        srgb = False
        compression = unreal.TextureCompressionSettings.TC_MASKS
    elif param in {"Height", "Opacity", "Opacity Map", "Alpha", "Transmission"}:
        srgb = False
        compression = unreal.TextureCompressionSettings.TC_GRAYSCALE
    else:
        srgb = True
        compression = unreal.TextureCompressionSettings.TC_DEFAULT

    settings = {
        "srgb": srgb,
        "compression_settings": compression,
        "max_texture_size": DEFAULT_MAX_TEXTURE_SIZE,
        # OpacityMask is sampled by a non-VT linear grayscale parameter in the
        # canonical tree foliage layer.  Re-enabling VT here makes the instance
        # override incompatible with that graph and restores solid white cards.
        "virtual_texture_streaming": (
            False
            if param in {"Opacity", "Opacity Map", "Alpha"}
            else bool(ENABLE_VIRTUAL_TEXTURE_STREAMING)
        ),
    }
    if param in {"Opacity", "Opacity Map", "Alpha"}:
        vector_type = getattr(unreal, "Vector4", None)
        if vector_type is not None:
            alpha_thresholds = vector_type(
                OPACITY_ALPHA_COVERAGE_THRESHOLD,
                0.0,
                0.0,
                0.0,
            )
        else:
            # Unit-test and older Python-wrapper fallback. UE 5.8 exposes Vector4.
            alpha_thresholds = (
                OPACITY_ALPHA_COVERAGE_THRESHOLD,
                0.0,
                0.0,
                0.0,
            )
        settings.update(
            {
                "do_scale_mips_for_alpha_coverage": True,
                "alpha_coverage_thresholds": alpha_thresholds,
            }
        )
    return settings


def _texture_settings_match(tex, settings: dict) -> bool:
    if tex is None:
        return False
    for property_name, expected in settings.items():
        try:
            current = tex.get_editor_property(property_name)
        except Exception:
            return False
        if property_name in {"srgb", "virtual_texture_streaming"}:
            current = bool(current)
        if not _editor_values_match(current, expected):
            return False
    return True


def _configure_imported_texture(
    tex,
    param: str,
    virtual_texture_streaming=None,
    file_path=None,
    asset_name=None,
) -> bool:
    changed = False
    settings = _desired_texture_settings(
        param,
        virtual_texture_streaming,
        file_path,
        asset_name,
    )
    changed |= _set_texture_property_if_changed(
        tex,
        "compression_settings",
        settings["compression_settings"],
    )
    # Import auto-detection can classify blue albedo as a normal map. Unreal
    # refuses sRGB while TC_NORMALMAP is active, so correct compression first.
    changed |= _set_texture_property_if_changed(tex, "srgb", settings["srgb"])
    changed |= _set_texture_property_if_changed(
        tex,
        "max_texture_size",
        settings["max_texture_size"],
    )

    try:
        before = bool(tex.get_editor_property("virtual_texture_streaming"))
    except Exception:
        before = None
    virtual_texture_streaming = settings["virtual_texture_streaming"]
    if before is not None and before != virtual_texture_streaming:
        if hasattr(tex, "set_virtual_texture_streaming"):
            tex.set_virtual_texture_streaming(virtual_texture_streaming)
        else:
            tex.set_editor_property("virtual_texture_streaming", virtual_texture_streaming)
        changed = True

    for property_name in (
        "do_scale_mips_for_alpha_coverage",
        "alpha_coverage_thresholds",
    ):
        if property_name in settings:
            changed |= _set_texture_property_if_changed(
                tex,
                property_name,
                settings[property_name],
            )

    return changed


def _source_control_flag(state, property_name: str) -> bool:
    try:
        return bool(getattr(state, property_name))
    except Exception:
        try:
            return bool(state.get_editor_property(property_name))
        except Exception:
            return False


def _source_control_error(source_control) -> str:
    try:
        return str(source_control.last_error_msg())
    except Exception:
        return "unknown source-control error"


def _checkout_texture_for_update(asset_path: str) -> bool:
    source_control = getattr(unreal, "SourceControl", None)
    if source_control is None:
        raise RuntimeError("Unreal SourceControl helper is unavailable")
    try:
        state = source_control.query_file_state(asset_path, True, False)
    except TypeError:
        state = source_control.query_file_state(asset_path)

    if _source_control_flag(state, "is_checked_out_other"):
        raise RuntimeError(f"texture is checked out by another user: {asset_path}")
    if (
        _source_control_flag(state, "is_checked_out")
        or _source_control_flag(state, "is_added")
    ):
        return False

    if not source_control.check_out_file(asset_path, True):
        raise RuntimeError(
            f"texture source-control checkout failed: {asset_path} "
            f"({_source_control_error(source_control)})"
        )
    _log(f"  texture source-control checkout: {asset_path}")
    return _source_control_flag(state, "is_valid")


def _revert_owned_texture_checkout(asset_path: str):
    source_control = getattr(unreal, "SourceControl", None)
    if source_control is None:
        return
    source_control.revert_unchanged_file(asset_path, True)


def _mark_texture_for_add(asset_path: str):
    source_control = getattr(unreal, "SourceControl", None)
    if source_control is None:
        raise RuntimeError("Unreal SourceControl helper is unavailable")
    if not source_control.mark_file_for_add(asset_path, True):
        raise RuntimeError(
            f"texture source-control add failed: {asset_path} "
            f"({_source_control_error(source_control)})"
        )
    _log(f"  texture source-control add: {asset_path}")


def _run_texture_import(file_path: str, asset_name: str, replace_existing: bool):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", file_path)
    task.set_editor_property("destination_path", TEXTURES_FOLDER)
    task.set_editor_property("destination_name", asset_name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", bool(replace_existing))
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return task


def _texture_import_task_succeeded(task, full_path: str) -> bool:
    try:
        if any(obj is not None for obj in task.get_objects()):
            return True
    except Exception:
        pass
    try:
        imported_paths = task.get_editor_property("imported_object_paths") or []
    except Exception:
        try:
            imported_paths = task.imported_object_paths or []
        except Exception:
            imported_paths = []
    expected_path = full_path.split(".")[0]
    return any(str(path).split(".")[0] == expected_path for path in imported_paths)


def _save_texture_asset(full_path: str) -> bool:
    if unreal.EditorAssetLibrary.save_asset(full_path, only_if_is_dirty=False):
        return True
    _warn(f"  texture save failed: {full_path}")
    return False


def _cache_verified_texture(
    tex_cache: dict,
    full_path: str,
    source_md5: str,
    fingerprint: dict,
    tex,
    desired_settings: dict,
) -> bool:
    if _asset_import_file_md5(full_path) != source_md5:
        _warn(f"  texture AssetImportData FileMD5 verification failed: {full_path}")
        return False
    if not _texture_settings_match(tex, desired_settings):
        _warn(f"  texture role-setting verification failed: {full_path}")
        return False
    if tex_cache is not None:
        tex_cache[full_path] = dict(fingerprint)
    return True


def _is_texture2d(asset) -> bool:
    if asset is None:
        return False
    texture_class = getattr(unreal, "Texture2D", None)
    if texture_class is not None:
        try:
            if isinstance(asset, texture_class):
                return True
        except TypeError:
            pass
    try:
        return asset.get_class().get_name() == "Texture2D"
    except Exception:
        return False


def _load_texture2d(asset_path: str):
    try:
        asset = unreal.load_asset(asset_path)
    except Exception as exc:
        _warn(f"  texture asset load failed; candidate omitted: {asset_path} ({exc})")
        return None
    if asset is not None and not _is_texture2d(asset):
        _log(f"  non-Texture2D candidate omitted: {asset_path}")
        return None
    return asset


def _texture_asset_paths_named(asset_name: str) -> list:
    """Build one Texture2D name index per Unreal Python process."""
    global _TEXTURE_ASSET_REGISTRY_INDEX
    if _TEXTURE_ASSET_REGISTRY_INDEX is None:
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        try:
            assets = registry.get_assets_by_path("/Game", recursive=True)
        except TypeError:
            assets = registry.get_assets_by_path("/Game", True)
        index = {}
        for asset_data in assets:
            if _asset_data_class_name(asset_data).casefold() != "texture2d":
                continue
            name = str(asset_data.asset_name).strip().casefold()
            path = str(asset_data.package_name).split(".")[0]
            if name and path:
                index.setdefault(name, []).append(path)
        _TEXTURE_ASSET_REGISTRY_INDEX = {
            name: sorted(set(paths), key=str.casefold)
            for name, paths in index.items()
        }
    return list(
        (_TEXTURE_ASSET_REGISTRY_INDEX or {}).get(
            str(asset_name or "").strip().casefold(),
            [],
        )
    )


def _existing_texture_asset_path(asset_name: str, preferred_path: str = ""):
    """Resolve one exact existing Texture2D without guessing between matches."""
    preferred_path = str(preferred_path or "").split(".")[0]
    if preferred_path:
        try:
            preferred_exists = unreal.EditorAssetLibrary.does_asset_exist(preferred_path)
        except Exception as exc:
            _warn(
                "  texture asset existence lookup failed; candidate omitted: "
                f"{preferred_path} ({exc})"
            )
            preferred_exists = False
        if preferred_exists and _load_texture2d(preferred_path) is not None:
            return preferred_path

    cache_key = str(asset_name or "").strip().casefold()
    if not cache_key:
        return None
    if cache_key in _TEXTURE_ASSET_SEARCH_CACHE:
        return _TEXTURE_ASSET_SEARCH_CACHE[cache_key]

    try:
        candidates = _texture_asset_paths_named(asset_name)
    except Exception as exc:
        _log(f"  texture registry lookup unavailable: {asset_name} ({exc})")
        candidates = []

    candidates = list(
        dict.fromkeys(str(path).split(".")[0] for path in candidates if path)
    )
    if len(candidates) == 1 and _load_texture2d(candidates[0]) is not None:
        resolved = candidates[0]
        _log(f"  existing texture resolved by exact name: {asset_name} -> {resolved}")
    else:
        resolved = None
        if len(candidates) > 1:
            _log(
                "  texture exact-name lookup ambiguous; parameter left empty: "
                f"{asset_name} ({', '.join(candidates)})"
            )
    _TEXTURE_ASSET_SEARCH_CACHE[cache_key] = resolved
    return resolved


def _unique_source_matching_texture(
    asset_name: str,
    preferred_path: str,
    source_md5: str,
):
    """Return one exact-name Texture2D whose import source matches the local file."""
    try:
        candidates = _texture_asset_paths_named(asset_name)
    except Exception as exc:
        _log(f"  texture registry lookup unavailable: {asset_name} ({exc})")
        return None, None
    candidates = list(
        dict.fromkeys(str(path).split(".")[0] for path in candidates if path)
    )
    candidates = [path for path in candidates if path != preferred_path]
    matching = []
    for candidate in candidates:
        texture = _load_texture2d(candidate)
        if texture is None:
            continue
        try:
            if _asset_import_file_md5(candidate) == source_md5:
                matching.append((candidate, texture))
        except Exception as exc:
            _warn(
                "  existing texture source verification failed; candidate omitted: "
                f"{candidate} ({exc})"
            )
    if len(matching) == 1:
        return matching[0]
    if len(matching) > 1:
        _log(
            "  source-matching texture lookup ambiguous; existing assets omitted: "
            f"{asset_name} ({', '.join(path for path, _texture in matching)})"
        )
    return None, None


def _import_texture_impl(
    file_path: str,
    asset_name: str,
    param: str,
    tex_cache: dict = None,
    force_reimport: bool = False,
    virtual_texture_streaming=None,
):
    """디스크 텍스처를 TEXTURES_FOLDER 로 직접 import 하고 종류별 설정 적용. asset path 반환.

    AssetImportData FileMD5와 역할 설정이 모두 같으면 force mode에서도 mutation 없이 건너뛴다.
    legacy mtime cache entry는 검증 성공 뒤에만 v2 fingerprint dict로 교체된다.
    """
    if not asset_name:
        _log(f"  texture has no asset_name; parameter left empty ({file_path})")
        return None

    full_path = f"{TEXTURES_FOLDER}/{asset_name}"
    if not file_path or not os.path.isfile(file_path):
        existing_path = _existing_texture_asset_path(asset_name, full_path)
        if existing_path:
            _log(
                "  declared texture source unavailable; existing asset reused: "
                f"{asset_name} -> {existing_path}"
            )
            return existing_path
        _log(
            "  texture unresolved; parameter left empty: "
            f"{asset_name} ({file_path})"
        )
        return None
    try:
        source_md5, fingerprint = _texture_source_fingerprint(
            file_path,
            tex_cache.get(full_path) if tex_cache is not None else None,
        )
    except OSError as exc:
        _warn(f"  텍스처 MD5 읽기 실패: {asset_name} ({exc})")
        return None

    desired_settings = _desired_texture_settings(
        param,
        virtual_texture_streaming,
        file_path,
        asset_name,
    )
    asset_exists = unreal.EditorAssetLibrary.does_asset_exist(full_path)
    if not asset_exists:
        existing_path, existing_texture = _unique_source_matching_texture(
            asset_name,
            full_path,
            source_md5,
        )
        if existing_path and existing_texture is not None:
            settings_match = _texture_settings_match(
                existing_texture,
                desired_settings,
            )
            checkout_owned = False
            try:
                if not settings_match:
                    checkout_owned = _checkout_texture_for_update(existing_path)
                    _configure_imported_texture(
                        existing_texture,
                        param,
                        virtual_texture_streaming,
                        file_path,
                        asset_name,
                    )
                    if not _save_texture_asset(existing_path):
                        return None
                if not _cache_verified_texture(
                    tex_cache,
                    existing_path,
                    source_md5,
                    fingerprint,
                    existing_texture,
                    desired_settings,
                ):
                    return None
                _log(
                    "  verified existing texture reused before import: "
                    f"{asset_name} -> {existing_path}"
                )
                return existing_path
            finally:
                if checkout_owned:
                    _revert_owned_texture_checkout(existing_path)
        task = _run_texture_import(file_path, asset_name, replace_existing=False)
        if not _texture_import_task_succeeded(task, full_path):
            _warn(f"  텍스처 import task 실패: {asset_name}")
            return None
        tex = unreal.load_asset(full_path)
        if not _is_texture2d(tex):
            _warn(f"  텍스처 import 실패: {asset_name}")
            return None
        _configure_imported_texture(
            tex,
            param,
            virtual_texture_streaming,
            file_path,
            asset_name,
        )
        if not _save_texture_asset(full_path):
            return None
        _mark_texture_for_add(full_path)
        if not _cache_verified_texture(
            tex_cache,
            full_path,
            source_md5,
            fingerprint,
            tex,
            desired_settings,
        ):
            return None
        _log(f"  텍스처 import: {asset_name} ({param})")
        return full_path

    tex = unreal.load_asset(full_path)
    if not _is_texture2d(tex):
        _log(f"  non-Texture2D destination omitted: {full_path}")
        return None
    imported_md5 = _asset_import_file_md5(full_path)
    source_matches = imported_md5 == source_md5
    settings_match = _texture_settings_match(tex, desired_settings)
    if source_matches and settings_match:
        if not _cache_verified_texture(
            tex_cache,
            full_path,
            source_md5,
            fingerprint,
            tex,
            desired_settings,
        ):
            return None
        force_note = " (force ignored: verified unchanged)" if force_reimport else ""
        _log(f"  texture verified, import skipped: {asset_name} ({param}){force_note}")
        return full_path

    checkout_owned = _checkout_texture_for_update(full_path)
    try:
        if not source_matches:
            task = _run_texture_import(file_path, asset_name, replace_existing=True)
            if not _texture_import_task_succeeded(task, full_path):
                _warn(f"  텍스처 reimport task 실패: {asset_name}")
                return None
            tex = unreal.load_asset(full_path)
            if not _is_texture2d(tex):
                _warn(f"  텍스처 reimport 실패: {asset_name}")
                return None
            _configure_imported_texture(
                tex,
                param,
                virtual_texture_streaming,
                file_path,
                asset_name,
            )
            if not _save_texture_asset(full_path):
                return None
            _log(f"  텍스처 reimport: {asset_name} ({param})")
        else:
            _configure_imported_texture(
                tex,
                param,
                virtual_texture_streaming,
                file_path,
                asset_name,
            )
            if not _save_texture_asset(full_path):
                return None
            _log(f"  texture role settings updated: {asset_name} ({param})")

        if not _cache_verified_texture(
            tex_cache,
            full_path,
            source_md5,
            fingerprint,
            tex,
            desired_settings,
        ):
            return None
        return full_path
    finally:
        if checkout_owned:
            _revert_owned_texture_checkout(full_path)


def _verified_texture_fallback_after_failure(
    file_path: str,
    asset_name: str,
    param: str,
    virtual_texture_streaming,
    preferred_verified_before: bool,
):
    preferred_path = f"{TEXTURES_FOLDER}/{asset_name}"
    if preferred_verified_before:
        return preferred_path
    candidates = []
    try:
        candidates.extend(_texture_asset_paths_named(asset_name))
    except Exception as exc:
        _log(f"  texture registry fallback unavailable: {asset_name} ({exc})")
    candidates = list(dict.fromkeys(str(path).split(".")[0] for path in candidates))
    candidates = [path for path in candidates if path != preferred_path]

    try:
        source_md5 = _file_md5(file_path)
        desired_settings = _desired_texture_settings(
            param,
            virtual_texture_streaming,
            file_path,
            asset_name,
        )
    except Exception as exc:
        _warn(f"  texture fallback verification unavailable: {asset_name} ({exc})")
        return None

    verified = []
    for candidate in candidates:
        texture = _load_texture2d(candidate)
        if texture is None:
            continue
        try:
            candidate_matches = (
                _asset_import_file_md5(candidate) == source_md5
                and _texture_settings_match(texture, desired_settings)
            )
        except Exception as exc:
            _warn(
                "  existing texture verification failed; candidate omitted: "
                f"{candidate} ({exc})"
            )
            continue
        if candidate_matches:
            verified.append(candidate)
    if len(verified) == 1:
        return verified[0]
    if len(verified) > 1:
        _log(
            "  verified texture fallback ambiguous; parameter left empty: "
            f"{asset_name} ({', '.join(verified)})"
        )
    return None


def _import_texture(
    file_path: str,
    asset_name: str,
    param: str,
    tex_cache: dict = None,
    force_reimport: bool = False,
    virtual_texture_streaming=None,
):
    """Best-effort texture handoff; texture failures never gate the mesh/MI flow."""
    preferred_verified_before = False
    source_is_local = bool(asset_name and file_path and os.path.isfile(file_path))
    if source_is_local:
        preferred_path = f"{TEXTURES_FOLDER}/{asset_name}"
        try:
            preferred_exists = unreal.EditorAssetLibrary.does_asset_exist(preferred_path)
            if preferred_exists:
                preferred_texture = _load_texture2d(preferred_path)
                preferred_verified_before = bool(
                    preferred_texture is not None
                    and _asset_import_file_md5(preferred_path) == _file_md5(file_path)
                    and _texture_settings_match(
                        preferred_texture,
                        _desired_texture_settings(
                            param,
                            virtual_texture_streaming,
                            file_path,
                            asset_name,
                        ),
                    )
                )
        except Exception as exc:
            _warn(f"  preferred texture verification failed: {asset_name} ({exc})")
    try:
        resolved = _import_texture_impl(
            file_path,
            asset_name,
            param,
            tex_cache,
            force_reimport=force_reimport,
            virtual_texture_streaming=virtual_texture_streaming,
        )
    except Exception as exc:
        _warn(
            "  local texture handoff failed; fallback evaluated: "
            f"{asset_name or '<unnamed>'} ({exc})"
        )
        resolved = None
    if resolved:
        return resolved
    verified_fallback = None
    if source_is_local:
        verified_fallback = _verified_texture_fallback_after_failure(
            file_path,
            asset_name,
            param,
            virtual_texture_streaming,
            preferred_verified_before,
        )
    if verified_fallback:
        _log(
            "  verified existing texture reused after local handoff failure: "
            f"{asset_name} -> {verified_fallback}"
        )
        return verified_fallback
    return None


def _nanite_shape_preservation_voxelize():
    for enum_name in ("NaniteShapePreservation", "ENaniteShapePreservation"):
        enum_type = getattr(unreal, enum_name, None)
        if enum_type is None:
            continue
        for value_name in ("VOXELIZE", "Voxelize", "voxelize"):
            value = getattr(enum_type, value_name, None)
            if value is not None:
                return value
        for value_name in dir(enum_type):
            if "voxel" in value_name.lower():
                value = getattr(enum_type, value_name, None)
                if value is not None:
                    return value
    return None


def _set_nanite_shape_preservation(nanite, value) -> bool:
    if value is None:
        return False
    try:
        if nanite.get_editor_property("shape_preservation") == value:
            return False
    except Exception:
        pass
    try:
        nanite.set_editor_property("shape_preservation", value)
        return True
    except Exception as exc:
        _warn(f"  Nanite Shape Preservation Voxelize set failed: {exc}")
        return False


def _notify_nanite_settings_changed(mesh):
    for method_name in ("notify_nanite_settings_changed", "post_edit_change"):
        method = getattr(mesh, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass


def _set_nanite_bool(nanite, property_name: str, value) -> bool:
    if value is None:
        return False
    value = bool(value)
    try:
        if bool(nanite.get_editor_property(property_name)) == value:
            return False
    except Exception:
        pass
    try:
        nanite.set_editor_property(property_name, value)
        return True
    except Exception as exc:
        _warn(f"  Nanite {property_name} set failed: {exc}")
        return False


def _set_nanite(
    mesh,
    enabled: bool,
    shape_preservation=None,
    voxel_ndf=None,
    voxel_opacity=None,
) -> bool:
    """Set mesh Nanite settings. Returns True when any value changed."""
    # The material post-import pass must not undo the importer's morph guard.
    # UE 5.8 skeletal Nanite currently suppresses these deformations.
    if _is_skeletal_mesh(mesh) and mesh.get_editor_property("morph_targets"):
        enabled = False
        _log("  Skeletal morph targets detected: using classic skinned rendering")
    nanite = mesh.get_editor_property("nanite_settings")
    changed = False
    if bool(nanite.get_editor_property("enabled")) != enabled:
        nanite.set_editor_property("enabled", enabled)
        changed = True
    if enabled:
        changed = _set_nanite_shape_preservation(nanite, shape_preservation) or changed
        changed = _set_nanite_bool(nanite, "voxel_ndf", voxel_ndf) or changed
        changed = _set_nanite_bool(nanite, "voxel_opacity", voxel_opacity) or changed
    if not changed:
        return False
    mesh.set_editor_property("nanite_settings", nanite)
    _notify_nanite_settings_changed(mesh)
    if enabled and shape_preservation is not None:
        shape_label = str(shape_preservation)
        if "." in shape_label:
            shape_label = shape_label.rsplit(".", 1)[-1]
        shape_label = shape_label.strip("<> ").split(":", 1)[0]
        detail = f"  Nanite enabled + Shape Preservation {shape_label}"
        if voxel_ndf:
            detail += " + Voxel NDF"
        if voxel_opacity:
            detail += " + Voxel Opacity"
        _log(detail)
    else:
        _log("  Nanite enabled" if enabled else "  Nanite disabled")
    return True


def _sync_browser_to_mesh(mesh_path: str):
    """Content Browser 를 import 된 메쉬로 이동/선택시킨다.
    (텍스처 import 가 마지막이라 브라우저가 /Game/texture 로 튀는 것을 되돌림)"""
    try:
        command_line = unreal.SystemLibrary.get_command_line().casefold()
        if "-unattended" in command_line or "-run=" in command_line:
            return
        unreal.EditorAssetLibrary.sync_browser_to_objects([mesh_path])
    except Exception as e:
        _warn(f"  브라우저 동기화 실패(무시): {e}")


def _is_translucent(data) -> bool:
    """JSON 머티리얼 중 하나라도 반투명이면 True. (블렌더 애드온이 'translucent' 플래그를 씀)"""
    if not data:
        return False
    return any(entry.get("translucent") for entry in data.get("materials", []))


def _same_asset(a, b) -> bool:
    return a is not None and b is not None and a.get_path_name() == b.get_path_name()


def _entry_name_blob(entry: dict) -> str:
    values = []
    for key in (
        "name",
        "slot_name",
        "material_slot_name",
        "imported_slot_name",
        "imported_material_slot_name",
        "original_material_name",
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    return " ".join(values).casefold()


def _tree_part_key(entry: dict):
    aliases = {
        "bark": "bark",
        "trunk": "bark",
        "stump": "bark",
        "branch": "branch",
        "branches": "branch",
        "twig": "branch",
        "twigs": "branch",
        "stem": "branch",
        "stems": "branch",
        "leaf": "leaf",
        "leaves": "leaf",
        "foliage": "leaf",
        "cluster": "leaf",
    }
    sources = (
        entry,
        entry.get("material_layer")
        if isinstance(entry.get("material_layer"), dict)
        else {},
    )
    explicit_value = ""
    for source in sources:
        explicit = str(
            source.get("tree_part")
            or source.get("unreal_tree_part")
            or ""
        ).strip().casefold()
        if explicit:
            explicit_value = explicit
            break

    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        return contract_api.classify_tree_part(
            _entry_name_blob(entry),
            explicit=explicit_value,
        )

    for source in sources:
        explicit = str(
            source.get("tree_part")
            or source.get("unreal_tree_part")
            or ""
        ).strip().casefold()
        normalized = aliases.get(explicit)
        if normalized:
            return normalized
    blob = _entry_name_blob(entry)
    name_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", blob)
        if token
    }
    # Leaf-atlas scope wins over a subgroup label.  M_leaf_*_stem/twig uses the
    # leaf UV/translucency contract even when the source collection says stem.
    if name_tokens.intersection({"leaf", "leaves", "foliage", "cluster"}):
        return "leaf"
    if (
        name_tokens.intersection({"branch", "branches", "twig", "twigs", "stem", "stems"})
        or any(token.endswith(("branch", "twig")) for token in name_tokens)
    ):
        return "branch"
    if name_tokens.intersection({"bark", "trunk", "stump"}):
        return "bark"
    return None


def _tree_shading_key(entry: dict, tree_part: str = None) -> str:
    aliases = {
        "wood": "wood",
        "opaque": "wood",
        "foliage": "foliage",
        "subsurface": "foliage",
        "sss": "foliage",
        "stem": "stem",
        "wrap": "stem",
    }
    sources = (
        entry,
        entry.get("material_layer")
        if isinstance(entry.get("material_layer"), dict)
        else {},
    )
    explicit_value = ""
    for source in sources:
        explicit = str(
            source.get("tree_shading")
            or source.get("unreal_tree_shading")
            or source.get("tree_master_variant")
            or ""
        ).strip().casefold()
        if explicit:
            explicit_value = explicit
            break

    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        return contract_api.classify_tree_shading(
            _entry_name_blob(entry),
            explicit=explicit_value,
            tree_part=tree_part,
        )

    for source in sources:
        explicit = str(
            source.get("tree_shading")
            or source.get("unreal_tree_shading")
            or source.get("tree_master_variant")
            or ""
        ).strip().casefold()
        normalized = aliases.get(explicit)
        if normalized:
            return normalized
    tree_part = tree_part or _tree_part_key(entry)
    if tree_part == "leaf":
        return "foliage"
    name_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", _entry_name_blob(entry))
        if token
    }
    if name_tokens.intersection({"stem", "stems"}):
        return "stem"
    return "wood"


def _is_tree_asset_path(mesh_path: str) -> bool:
    normalized = str(mesh_path or "").replace("\\", "/").casefold()
    return "/tree/" in normalized or "/trees/" in normalized


def _is_speedtree_asset(data: dict, mesh_path: str) -> bool:
    descriptor = _speedtree_sidecar_descriptor(data)
    if descriptor is not None:
        return str(descriptor.get("asset_kind") or "").casefold() == "speedtree"
    return _is_tree_asset_path(mesh_path)


def _tree_preset_contract_overlay() -> dict:
    contract_api = _speedtree_handoff_api()
    if contract_api is None or not hasattr(contract_api, "tree_unreal_preset"):
        return {}
    value = contract_api.tree_unreal_preset()
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in (
        "mi_folder",
        "layer_instance_folder",
        "layer_texture_remap",
        "virtual_textures",
        "masters_by_shading",
    ):
        if key in value:
            result[key] = value[key]
    if isinstance(value.get("layer_parents_by_part"), dict):
        result["layer_parents_by_name"] = value["layer_parents_by_part"]
    masters = value.get("masters_by_shading")
    if isinstance(masters, dict) and masters.get("wood"):
        result["master"] = masters["wood"]
    return result


def _master_preset(data: dict, entry: dict = None, mesh_path: str = "") -> dict:
    entry = entry or {}
    tree_part = _tree_part_key(entry)
    tree_shading = _tree_shading_key(entry, tree_part)
    key = (
        entry.get("material_master")
        or entry.get("master_material")
        or entry.get("master_preset")
    )
    if not key:
        material_name = re.sub(r"[^0-9A-Za-z_]+", "_", str(entry.get("name") or "")).lower()
        if re.search(r"(?:^|_)gemstone(?:_|$)", material_name):
            key = "gemstone"
        elif tree_part or _is_tree_asset_path(mesh_path):
            key = "tree"
        else:
            key = (
                data.get("material_master")
                or data.get("master_material")
                or data.get("master_preset")
                or DEFAULT_MASTER_PRESET
            )
    key = str(key or DEFAULT_MASTER_PRESET).strip().lower()
    preset = MASTER_PRESETS.get(key)
    if preset is None:
        _warn(f"unknown material master preset '{key}', using '{DEFAULT_MASTER_PRESET}'")
        key = DEFAULT_MASTER_PRESET
        preset = MASTER_PRESETS[key]
    result = dict(preset)
    if key == "gemstone":
        contract = _pipeline_contract().get("unreal_handoff_sidecar", {}).get("gemstone_material_layer", {})
        for target, source in (("master", "master"), ("mi_folder", "mi_folder"), ("layer_parent", "parent"), ("layer_instance_folder", "instance_folder")):
            if contract.get(source):
                result[target] = str(contract[source])
    if key == "tree":
        result.update(_tree_preset_contract_overlay())
    scope = data.get("codex_test_asset_scope")
    if isinstance(scope, dict) and _is_codex_test_asset_path(mesh_path):
        scope_root = str(scope.get("root") or "").rstrip("/")
        if _is_codex_test_asset_path(scope_root):
            result["mi_folder"] = scope_root + "/MI"
            result["layer_instance_folder"] = scope_root + "/MYI"
    result["key"] = key
    if tree_part:
        result["tree_part"] = tree_part
    if key == "tree":
        result["tree_shading"] = tree_shading
        masters_by_shading = result.get("masters_by_shading")
        if isinstance(masters_by_shading, dict):
            shading_master = str(masters_by_shading.get(tree_shading) or "").strip()
            if shading_master:
                result["master"] = shading_master
    return result


def _uses_tree_material_preset(data: dict, mesh_path: str) -> bool:
    if _is_speedtree_asset(data, mesh_path):
        return True
    if not data:
        return False
    return any(
        _master_preset(data, entry, mesh_path).get("key") == "tree"
        for entry in data.get("materials", [])
        if isinstance(entry, dict)
    )


def _uses_verified_hair_uv_payload(data: dict, mesh_path: str) -> bool:
    """True only for tagged Hair Tool v3 data whose UV payload was authored."""
    if not isinstance(data, dict):
        return False
    mesh_name = str(mesh_path or "").rsplit("/", 1)[-1].casefold()
    if "eyelash" in mesh_name or mesh_name.endswith("_lash"):
        return False
    for entry in data.get("materials", []):
        if not isinstance(entry, dict):
            continue
        preset = _master_preset(data, entry, mesh_path)
        if preset.get("key") != "hair":
            continue
        hair_tool = entry.get("hair_tool")
        if not isinstance(hair_tool, dict):
            continue
        payload = hair_tool.get("vertex_uv_payload")
        if not isinstance(payload, dict):
            continue
        if (
            int(payload.get("version") or 0) >= 3
            and str(payload.get("encoding") or "").strip().upper()
            == "HTUE_RGB_TAGGED_UV"
        ):
            return True
    return False


def _set_skeletal_material_nanite(mesh, data, mesh_path):
    """Material assignment must preserve artist-authored hair Nanite settings."""
    uses_hair = any(
        _master_preset(data, entry, mesh_path).get("key") == "hair"
        for entry in (data or {}).get("materials", [])
        if isinstance(entry, dict)
    )
    enabled = not _is_translucent(data)
    if uses_hair:
        enabled = enabled and bool(
            mesh.get_editor_property("nanite_settings").get_editor_property("enabled")
        )
        # UV payload tags describe vertex data, not permission to voxelize it.
        # Keep None/PreserveArea/Voxelize, NDF, opacity and artist-disabled Nanite.
        return _set_nanite(mesh, enabled)
    voxelize = (
        _nanite_shape_preservation_voxelize()
        if ENABLE_SKELETAL_NANITE_VOXELIZE
        and _uses_tree_material_preset(data, mesh_path)
        else None
    )
    return _set_nanite(mesh, enabled, voxelize)


def _load_master_material(preset: dict):
    master_path = preset["master"]
    master_mat = unreal.load_asset(master_path)
    if master_mat is None:
        _log(f"  master material unavailable; fallback remains empty: {master_path}")
    return master_mat


def _create_or_load_mi(
    asset_tools,
    master_mat,
    mat_base: str,
    mi_folder: str,
    manage_existing: bool = False,
    create_if_missing: bool = True,
):
    mi_name = f"MI_{mat_base}"
    mi_path = f"{mi_folder}/{mi_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(mi_path):
        mi = unreal.load_asset(mi_path)
        parent_changed = False
        try:
            current_parent = mi.get_editor_property("parent")
        except Exception:
            current_parent = None
        if (
            manage_existing
            and master_mat is not None
            and not _same_asset(current_parent, master_mat)
        ):
            unreal.MaterialEditingLibrary.set_material_instance_parent(mi, master_mat)
            _log(f"  MI parent 변경: {mi_name} -> {master_mat.get_path_name()}")
            parent_changed = True
        return mi, mi_path, False, parent_changed, "existing"

    if not create_if_missing:
        _log(f"  target material unavailable; slot left unchanged: {mi_path}")
        return None, mi_path, False, False, "missing"

    copy_from_path = _derive_number_suffix_copy_source(mi_path)
    if copy_from_path:
        mi_folder_path, _mi_name = mi_path.rsplit("/", 1)
        source = unreal.load_asset(copy_from_path)
        copied = None
        try:
            copied = unreal.EditorAssetLibrary.duplicate_asset(copy_from_path, mi_path)
        except Exception as e:
            _warn(f"  MI suffix copy failed, trying AssetTools: {copy_from_path} -> {mi_path} ({e})")
        if copied is None and source is not None:
            try:
                copied = asset_tools.duplicate_asset(mi_name, mi_folder_path, source)
            except Exception as e:
                _warn(f"  MI suffix AssetTools copy failed: {copy_from_path} -> {mi_path} ({e})")
        if copied is not None:
            _log(f"  MI copy: {copy_from_path} -> {mi_path}")
            return copied, mi_path, True, False, "copy"

    if master_mat is None:
        _log(f"  MI and usable master unavailable; slot left unchanged: {mi_path}")
        return None, mi_path, False, False, "missing"

    unreal.EditorAssetLibrary.make_directory(mi_folder)
    factory = unreal.MaterialInstanceConstantFactoryNew()
    mi = asset_tools.create_asset(
        mi_name, mi_folder, unreal.MaterialInstanceConstant, factory
    )
    if mi is None:
        _warn(f"MI 생성 실패: {mi_name}")
        return None, mi_path, False, False, "missing"
    unreal.MaterialEditingLibrary.set_material_instance_parent(mi, master_mat)
    _log(f"  MI 생성: {mi_name}")
    return mi, mi_path, True, False, "new"


def _entry_target_material_path(entry: dict):
    for key in (
        "target_material_path",
        "material_instance_path",
        "unreal_material_path",
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            return value.split(".")[0]
    return None


def _entry_copy_source_material_path(entry: dict):
    for key in (
        "copy_from_material_path",
        "source_material_path",
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            return value.split(".")[0]
    return None


def _entry_create_if_missing(entry: dict, preset: dict) -> bool:
    value = entry.get("create_if_missing", preset.get("create_if_missing", True))
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


def _entry_manages_existing_material_instance(entry: dict) -> bool:
    """Existing MIs are assignment-only unless the contract explicitly opts in."""
    manage_existing = entry.get("manage_existing_material_instance", False)
    if isinstance(manage_existing, str):
        manage_existing = manage_existing.strip().casefold() not in {
            "0",
            "false",
            "no",
            "off",
        }
    ownership = str(
        entry.get("material_instance_ownership")
        or entry.get("material_ownership")
        or ""
    ).strip().casefold()
    return bool(manage_existing) or ownership in {"pipeline", "managed"}


def _entry_reuses_material_instance_unchanged(entry: dict, preset: dict) -> bool:
    """Return whether an existing MI is assignment-only."""
    if _entry_manages_existing_material_instance(entry):
        return False
    # These fields still document ownership intent, but default MI/MYI reuse is
    # broad. Texture2D content is synchronized separately from material mutation.
    return True


def _material_instance_has_empty_background_layer(mi, entry: dict, preset: dict) -> bool:
    """Return True only for a provably uninitialized material-layer MI.

    Existing material instances remain assignment-only by default.  A generated
    material-layer MI whose background layer is empty is the narrow exception:
    preserving it unchanged skips the required MYI creation/assignment.  The
    MYI is structural and must be restored even when every texture is absent;
    its parameters then remain empty by design.  Non-empty artist layers are
    never replaced by this implicit repair.
    """
    if mi is None or preset.get("assignment") != "material_layer_instance":
        return False
    mat_base = _material_instance_base_name(str(entry.get("name") or ""))
    material_layer = entry.get("material_layer")
    has_explicit_layer_contract = isinstance(material_layer, dict)
    material_layer = material_layer if has_explicit_layer_contract else {}
    generated_mi_path = (
        f"{str(preset.get('mi_folder') or '').rstrip('/')}/MI_{mat_base}"
        if mat_base and preset.get("mi_folder")
        else ""
    )
    actual_mi_path = str(mi.get_path_name() or "").split(".", 1)[0]
    if not has_explicit_layer_contract and actual_mi_path != generated_mi_path:
        return False
    desired_layer = str(
        material_layer.get("instance_path")
        or _layer_instance_path(
            mat_base,
            preset,
            entry,
        )
        or ""
    ).split(".")[0]
    if not desired_layer:
        return False
    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    if helper is None or not hasattr(helper, "dump_material_layers"):
        return False
    try:
        result = helper.dump_material_layers(mi.get_path_name())
    except Exception:
        return False
    report_text = ""
    ok = False
    if isinstance(result, tuple):
        if result and isinstance(result[0], bool):
            ok = bool(result[0])
            report_text = str(result[1] if len(result) > 1 else "")
        elif result and isinstance(result[0], str):
            report_text = str(result[0])
            ok = True
    elif isinstance(result, str):
        report_text = result
        ok = True
    try:
        report = json.loads(report_text) if report_text else {}
    except Exception:
        return False
    if not bool(report.get("ok", ok)):
        return False
    layers = list(report.get("layers") or [])
    if not report.get("has_layers") or not layers:
        return True
    background = next(
        (row for row in layers if int(row.get("index", -1)) == 0),
        layers[0],
    )
    return not str(background.get("path") or "").strip()


def _entry_instance_profile(entry: dict) -> str:
    profile = str(entry.get("instance_profile") or "").strip()
    if not profile:
        return ""
    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        try:
            profile = contract_api.normalize_instance_profile(profile)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid SpeedTree instance_profile {profile!r}: {exc}"
            ) from exc
    else:
        if not UNREAL_INSTANCE_PROFILE_RE.fullmatch(profile):
            raise RuntimeError(
                f"invalid SpeedTree instance_profile {profile!r}; use one key made "
                "of letters, numbers, '_' or '-'"
            )
        profile = profile.casefold()
    mode = str(
        entry.get("material_instance_mode") or "create_or_reuse"
    ).strip().casefold()
    if mode not in {"create_or_reuse", "assign_existing"}:
        raise RuntimeError(
            f"unsupported material_instance_mode {mode!r} for profile {profile!r}"
        )
    return profile


def _instance_profile_material_paths(entry: dict, preset: dict):
    profile = _entry_instance_profile(entry)
    if not profile:
        return None
    if preset.get("key") != "tree":
        raise RuntimeError(
            f"instance_profile is only supported for SpeedTree materials: "
            f"{entry.get('name', '<unnamed>')}"
        )
    if _entry_target_material_path(entry):
        raise RuntimeError(
            "instance_profile cannot be combined with target_material_path"
        )
    mat_base = _material_instance_base_name(str(entry.get("name") or ""))
    mi_folder = str(preset.get("mi_folder") or "").rstrip("/")
    if not mat_base or not mi_folder:
        raise RuntimeError(
            f"could not resolve base MI path for instance_profile {profile!r}"
        )
    base_path = f"{mi_folder}/MI_{mat_base}"
    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        target_name = contract_api.profile_target_name(
            str(entry.get("name") or ""),
            profile,
        )
    else:
        target_name = f"MI_{mat_base}_{profile}"
    return {
        "profile": profile,
        "mode": str(
            entry.get("material_instance_mode") or "create_or_reuse"
        ).strip().casefold(),
        "base_path": base_path,
        "target_path": f"{mi_folder}/{target_name}",
    }


def _is_material_instance_constant(asset) -> bool:
    if asset is None:
        return False
    instance_class = getattr(unreal, "MaterialInstanceConstant", None)
    if instance_class is not None:
        try:
            if isinstance(asset, instance_class):
                return True
        except TypeError:
            pass
    try:
        return asset.get_class().get_name() == "MaterialInstanceConstant"
    except Exception:
        return False


def _validate_instance_profile_targets(data: dict, mesh_path: str = "") -> dict:
    """Resolve every user-owned child MI before any pipeline mutation starts."""
    targets = {}
    plans_by_target_path = {}
    errors = []
    for entry_index, entry in enumerate(data.get("materials", [])):
        if not isinstance(entry, dict):
            continue
        try:
            profile = _entry_instance_profile(entry)
            if not profile:
                continue
            if entry.get("translucent"):
                raise RuntimeError("translucent entries cannot use instance_profile")
            preset = _master_preset(data, entry, mesh_path)
            paths = _instance_profile_material_paths(entry, preset)
            base_path = paths["base_path"]
            target_path = paths["target_path"]
            target_exists = unreal.EditorAssetLibrary.does_asset_exist(target_path)
            target_asset = unreal.load_asset(target_path) if target_exists else None
            if target_exists and not _is_material_instance_constant(target_asset):
                raise RuntimeError(
                    f"target asset is not a MaterialInstanceConstant: {target_path}"
                )
            if target_exists:
                intent = ("existing_target", target_path)
                plan = plans_by_target_path.get(target_path)
                if plan is not None and plan["intent"] != intent:
                    raise RuntimeError(
                        f"conflicting profile target intent: {target_path}"
                    )
                if plan is None:
                    plan = {
                        **paths,
                        "intent": intent,
                        "preset": preset,
                        "master_path": "",
                        "master_asset": None,
                        "base_asset": None,
                        "asset": target_asset,
                        "target_existed": True,
                        "create_base": False,
                        "create_target": False,
                        "entry_indices": [],
                    }
                    plans_by_target_path[target_path] = plan
                plan["entry_indices"].append(entry_index)
                targets[entry_index] = plan
                continue
            if not target_exists and paths["mode"] == "assign_existing":
                _log(
                    f"  user-managed instance unavailable; base fallback remains: "
                    f"{target_path}"
                )
                continue
            base_exists = unreal.EditorAssetLibrary.does_asset_exist(base_path)
            base_asset = unreal.load_asset(base_path) if base_exists else None
            if base_exists and not _is_material_instance_constant(base_asset):
                raise RuntimeError(
                    f"base asset is not a MaterialInstanceConstant: {base_path}"
                )
            master_path = str(preset.get("master") or "").split(".")[0]
            master_asset = unreal.load_asset(master_path) if master_path else None
            if not base_exists and master_asset is None and not target_exists:
                _log(
                    f"  profile base and master unavailable; material remains "
                    f"unassigned: {base_path}"
                )
                continue
            intent = (base_path, master_path)
            plan = plans_by_target_path.get(target_path)
            if plan is not None and plan["intent"] != intent:
                raise RuntimeError(
                    f"conflicting profile target intent: {target_path}"
                )
            if plan is None:
                plan = {
                    **paths,
                    "intent": intent,
                    "preset": preset,
                    "master_path": master_path,
                    "master_asset": master_asset,
                    "base_asset": base_asset,
                    "asset": target_asset,
                    "target_existed": False,
                    "create_base": not base_exists and not target_exists,
                    "create_target": not target_exists,
                    "entry_indices": [],
                }
                plans_by_target_path[target_path] = plan
            plan["entry_indices"].append(entry_index)
            targets[entry_index] = plan
        except Exception as exc:
            errors.append(f"{entry.get('name', '<unnamed>')}: {exc}")
    if errors:
        raise RuntimeError(
            "SpeedTree instance profile preflight blocked before mutation: "
            + " | ".join(errors)
        )
    return targets


def _mark_new_asset_for_add(asset_path: str, label: str = "asset"):
    source_control = getattr(unreal, "SourceControl", None)
    if source_control is None:
        return
    try:
        state = source_control.query_file_state(asset_path, True, False)
    except TypeError:
        state = source_control.query_file_state(asset_path)
    if _source_control_flag(state, "is_checked_out_other"):
        raise RuntimeError(
            f"{label} is checked out by another user: {asset_path}"
        )
    if _source_control_flag(state, "is_added"):
        return
    if not source_control.mark_file_for_add(asset_path, True):
        raise RuntimeError(
            f"{label} source-control add failed: {asset_path} "
            f"({_source_control_error(source_control)})"
        )
    _log(f"  {label} source-control add: {asset_path}")


def _mark_material_instance_for_add(asset_path: str):
    _mark_new_asset_for_add(asset_path, "material instance")


def _save_and_mark_new_material_asset(asset_path: str, label: str = "material instance"):
    try:
        if not unreal.EditorAssetLibrary.save_asset(
            asset_path,
            only_if_is_dirty=False,
        ):
            raise RuntimeError(f"{label} save failed: {asset_path}")
        _mark_new_asset_for_add(asset_path, label)
    except Exception:
        try:
            unreal.EditorAssetLibrary.delete_asset(asset_path)
        except Exception as rollback_exc:
            _warn(f"  {label} rollback failed: {asset_path} ({rollback_exc})")
        raise


def _save_material_texture_update(asset_path: str) -> bool:
    """Persist best-effort parameter changes without making textures an admission gate."""
    try:
        saved = unreal.EditorAssetLibrary.save_asset(
            asset_path,
            only_if_is_dirty=False,
        )
    except Exception as exc:
        _warn(
            f"  material texture update save failed: {asset_path} ({exc}); "
            "handoff continues"
        )
        return False
    if not saved:
        _warn(
            f"  material texture update save failed: {asset_path}; "
            "handoff continues"
        )
        return False
    return True


def _load_exact_material_instance(asset_path: str):
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return None
    asset = unreal.load_asset(asset_path)
    if not _is_material_instance_constant(asset):
        raise RuntimeError(
            f"asset is not a MaterialInstanceConstant: {asset_path}"
        )
    return asset


def _create_profile_mi_asset(asset_tools, asset_path: str, parent):
    existing = _load_exact_material_instance(asset_path)
    if existing is not None:
        return existing, False

    folder, name = asset_path.rsplit("/", 1)
    unreal.EditorAssetLibrary.make_directory(folder)
    factory = unreal.MaterialInstanceConstantFactoryNew()
    create_error = None
    try:
        asset = asset_tools.create_asset(
            name,
            folder,
            unreal.MaterialInstanceConstant,
            factory,
        )
    except Exception as exc:
        asset = None
        create_error = exc
    if asset is None:
        raced_asset = _load_exact_material_instance(asset_path)
        if raced_asset is not None:
            return raced_asset, False
        detail = f" ({create_error})" if create_error else ""
        raise RuntimeError(
            f"material instance creation failed: {asset_path}{detail}"
        )
    try:
        unreal.MaterialEditingLibrary.set_material_instance_parent(asset, parent)
        if not unreal.EditorAssetLibrary.save_asset(
            asset_path, only_if_is_dirty=False
        ):
            raise RuntimeError(f"material instance save failed: {asset_path}")
        _mark_material_instance_for_add(asset_path)
    except Exception:
        try:
            unreal.EditorAssetLibrary.delete_asset(asset_path)
        except Exception as rollback_exc:
            _warn(
                f"  failed material instance rollback: {asset_path} "
                f"({rollback_exc})"
            )
        raise
    return asset, True


def _ensure_instance_profile_targets(asset_tools, targets: dict) -> dict:
    """Create missing Base/Target MIs before mesh or texture mutation."""
    unique_plans = []
    seen = set()
    for plan in targets.values():
        target_path = plan["target_path"]
        if target_path not in seen:
            seen.add(target_path)
            unique_plans.append(plan)
    unique_plans.sort(key=lambda plan: plan["target_path"].casefold())

    created_paths = []
    try:
        for plan in unique_plans:
            if plan["create_base"]:
                plan["base_asset"], base_created = _create_profile_mi_asset(
                    asset_tools,
                    plan["base_path"],
                    plan["master_asset"],
                )
                plan["create_base"] = False
                if base_created:
                    created_paths.append(plan["base_path"])
            if plan["create_target"]:
                plan["asset"], target_created = _create_profile_mi_asset(
                    asset_tools,
                    plan["target_path"],
                    plan["base_asset"],
                )
                plan["create_target"] = False
                if target_created:
                    created_paths.append(plan["target_path"])
                    _log(
                        f"  user-managed profile '{plan['profile']}' created once: "
                        f"{plan['target_path']}"
                    )
                else:
                    _log(
                        f"  user-managed profile '{plan['profile']}' race-reused unchanged: "
                        f"{plan['target_path']}"
                    )
            else:
                _log(
                    f"  user-managed profile '{plan['profile']}' reused unchanged: "
                    f"{plan['target_path']}"
                )
    except Exception:
        for asset_path in reversed(created_paths):
            try:
                unreal.EditorAssetLibrary.delete_asset(asset_path)
            except Exception as rollback_exc:
                _warn(
                    f"  profile MI rollback failed: {asset_path} ({rollback_exc})"
                )
        raise
    return targets


def _derive_number_suffix_copy_source(target_path: str):
    if not target_path or "/" not in target_path:
        return None
    target_folder, target_name = target_path.rsplit("/", 1)
    match = re.match(r"(.+)_\d+$", target_name)
    if not match:
        return None
    source_path = f"{target_folder}/{match.group(1)}"
    if unreal.EditorAssetLibrary.does_asset_exist(source_path):
        return source_path
    return None


def _load_or_copy_target_material(
    asset_tools,
    target_path: str,
    copy_from_path: str = None,
    master_mat=None,
    create_if_missing: bool = True,
):
    existing = _load_exact_material_instance(target_path)
    if existing is not None:
        return existing, target_path, False, "existing"

    # Duplication also creates an asset. Respect assignment-only intent before
    # deriving a suffix copy source or using an explicit copy source.
    if not create_if_missing:
        _log(f"  target material unavailable; slot left unchanged: {target_path}")
        return None, target_path, False, "missing"

    if not copy_from_path:
        copy_from_path = _derive_number_suffix_copy_source(target_path)

    if not copy_from_path:
        if master_mat is None:
            _log(
                "  target material and master unavailable; slot left unchanged: "
                + target_path
            )
            return None, target_path, False, "missing"
        target_folder, target_name = target_path.rsplit("/", 1)
        unreal.EditorAssetLibrary.make_directory(target_folder)
        factory = unreal.MaterialInstanceConstantFactoryNew()
        mi = asset_tools.create_asset(
            target_name,
            target_folder,
            unreal.MaterialInstanceConstant,
            factory,
        )
        if mi is None:
            raced = _load_exact_material_instance(target_path)
            if raced is not None:
                return raced, target_path, False, "existing"
            _warn(f"  target material create failed: {target_path}")
            return None, target_path, False, "missing"
        unreal.MaterialEditingLibrary.set_material_instance_parent(mi, master_mat)
        _save_and_mark_new_material_asset(target_path)
        _log(f"  MI create: {target_path}")
        return mi, target_path, True, "new"
    if not unreal.EditorAssetLibrary.does_asset_exist(copy_from_path):
        _log(f"  copy source material unavailable: {copy_from_path} -> {target_path}")
        return None, target_path, False, "missing"

    target_folder, target_name = target_path.rsplit("/", 1)
    unreal.EditorAssetLibrary.make_directory(target_folder)

    copied = None
    try:
        copied = unreal.EditorAssetLibrary.duplicate_asset(copy_from_path, target_path)
    except Exception as e:
        _warn(f"  duplicate_asset failed, trying AssetTools: {copy_from_path} -> {target_path} ({e})")

    if copied is None:
        source = unreal.load_asset(copy_from_path)
        try:
            copied = asset_tools.duplicate_asset(target_name, target_folder, source)
        except Exception as e:
            _warn(f"  AssetTools duplicate failed: {copy_from_path} -> {target_path} ({e})")
            copied = None

    if copied is None:
        _warn(f"  target material copy failed: {copy_from_path} -> {target_path}")
        return None, target_path, False, "missing"

    if not _is_material_instance_constant(copied):
        try:
            unreal.EditorAssetLibrary.delete_asset(target_path)
        finally:
            raise RuntimeError(
                f"copied target is not a MaterialInstanceConstant: {target_path}"
            )
    _save_and_mark_new_material_asset(target_path)
    _log(f"  MI copy: {copy_from_path} -> {target_path}")
    return copied, target_path, True, "copy"


def _supported_mesh_classes():
    classes = []
    for class_name in ("StaticMesh", "SkeletalMesh"):
        mesh_class = getattr(unreal, class_name, None)
        if mesh_class is not None:
            classes.append(mesh_class)
    return tuple(classes)


def _mesh_material_entries(mesh):
    for property_name in ("static_materials", "materials"):
        try:
            entries = mesh.get_editor_property(property_name)
        except Exception:
            continue
        return property_name, entries
    return None, []


def _static_material_name_values(static_material):
    names = []
    for prop in ("material_slot_name", "imported_material_slot_name"):
        try:
            value = static_material.get_editor_property(prop)
        except Exception:
            value = None
        if value:
            names.append(str(value))
    try:
        material = static_material.get_editor_property("material_interface")
    except Exception:
        material = None
    if material is not None:
        try:
            names.append(material.get_name())
        except Exception:
            pass
    return names


def _entry_slot_match_names(entry: dict, mat_name: str):
    names = []
    for value in (
        mat_name,
        entry.get("slot_name"),
        entry.get("material_slot_name"),
        entry.get("imported_slot_name"),
        entry.get("imported_material_slot_name"),
        entry.get("original_material_name"),
    ):
        value = str(value or "").strip()
        if value and value not in names:
            names.append(value)
    return names


def _entry_slot_display_name(entry: dict, mat_name: str):
    for value in (
        entry.get("slot_name"),
        entry.get("material_slot_name"),
        entry.get("imported_material_slot_name"),
        mat_name,
    ):
        value = str(value or "").strip()
        if value:
            return value
    return mat_name


def _slot_index_for_entry(mesh, entry: dict, mat_name: str):
    candidates = _entry_slot_match_names(entry, mat_name)
    if not candidates:
        return None
    candidate_set = set(candidates)
    candidate_folded = {name.casefold() for name in candidates}
    _property_name, material_entries = _mesh_material_entries(mesh)
    for i, material_entry in enumerate(material_entries):
        slot_names = _static_material_name_values(material_entry)
        if any(name in candidate_set for name in slot_names):
            return i
    for i, material_entry in enumerate(material_entries):
        slot_names = _static_material_name_values(material_entry)
        if any(str(name).casefold() in candidate_folded for name in slot_names):
            return i
    return None


def _sync_fbx_duplicate_material_slots(mesh, data):
    """Assign FBX's duplicate-name aliases from the exact authored slot."""
    property_name, slots = _mesh_material_entries(mesh)
    if property_name != "static_materials":
        return False
    entries = {str(entry.get("name") or ""): entry for entry in (data or {}).get("materials", [])}
    changed = False
    for index, slot in enumerate(slots):
        try:
            imported = str(slot.get_editor_property("imported_material_slot_name"))
        except Exception:
            continue
        if imported in entries:
            continue
        match = re.fullmatch(r"(.+)_ncl\d+_\d+", imported)
        if not match or match.group(1) not in entries:
            continue
        name = match.group(1)
        source_index = _slot_index_for_entry(mesh, entries[name], name)
        if source_index is None or source_index == index:
            continue
        material = slots[source_index].get_editor_property("material_interface")
        if material is not None and _set_material_interface(mesh, index, material):
            changed = True
    return changed


def _slot_index_for_material_name(mesh, mat_name: str):
    """메쉬의 실제 슬롯 중 import 된 머티리얼명이 mat_name 인 슬롯 인덱스. 없으면 None.
    Empty 결합(combine child meshes) export 처럼 JSON 의 slot_index 가 실제 결합 슬롯 순서와
    다를 수 있으므로, 슬롯을 이름으로 매칭해 정확한 인덱스를 찾는다."""
    return _slot_index_for_entry(mesh, {"name": mat_name}, mat_name)


def _rename_material_slot(mesh, slot_index: int, slot_name: str) -> bool:
    if not slot_name:
        return False
    property_name, material_entries = _mesh_material_entries(mesh)
    if not property_name or slot_index < 0 or slot_index >= len(material_entries):
        return False

    material_entry = material_entries[slot_index]
    changed = False
    for prop in ("material_slot_name", "imported_material_slot_name"):
        try:
            current = str(material_entry.get_editor_property(prop))
        except Exception:
            continue
        if current == slot_name:
            continue
        try:
            material_entry.set_editor_property(prop, slot_name)
        except Exception as exc:
            _warn(f"  slot name update failed: {prop} -> {slot_name} ({exc})")
            continue
        changed = True

    if not changed:
        return False
    try:
        mesh.set_editor_property(property_name, material_entries)
    except Exception as exc:
        _warn(f"  material slot array update failed: {property_name} ({exc})")
        return False
    return True


def _is_skeletal_mesh(mesh) -> bool:
    skeletal_mesh_class = getattr(unreal, "SkeletalMesh", None)
    return skeletal_mesh_class is not None and isinstance(mesh, skeletal_mesh_class)


def _mesh_relative_disk_folder_candidates(mesh_path: str):
    if not mesh_path or not mesh_path.startswith(GAME_MESHES_PREFIX):
        return []
    rel = mesh_path[len(GAME_MESHES_PREFIX):]
    rel_folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    candidates = []
    for root in JSON_SEARCH_ROOTS:
        if root and os.path.isdir(root):
            candidates.append(os.path.join(root, rel_folder.replace("/", os.sep)))
    return candidates


def _dynamic_wind_contract_rules() -> dict:
    contract_api = _speedtree_handoff_api()
    if contract_api is None or not hasattr(contract_api, "dynamic_wind_rules"):
        return {}
    value = contract_api.dynamic_wind_rules()
    return value if isinstance(value, dict) else {}


def _dynamic_wind_json_from_data(data: dict):
    data = data or {}
    rules = _dynamic_wind_contract_rules()
    fields = rules.get("sidecar_fields") or (
        "dynamic_wind_json",
        "dynamic_wind_json_path",
        "wind_json",
        "wind_json_path",
    )
    for key in fields:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    wind = data.get("wind")
    if isinstance(wind, dict):
        for key in ("json", "json_path", "path"):
            value = str(wind.get(key) or "").strip()
            if value:
                return value
    return None


def _dynamic_wind_candidate_dirs(mesh_path: str, json_path: str = None):
    dirs = []
    if json_path:
        current = os.path.dirname(os.path.abspath(json_path))
        for _ in range(6):
            if not current or current in dirs:
                break
            dirs.extend([
                current,
                os.path.join(current, "JSON"),
                os.path.join(current, "fbx", "JSON"),
            ])
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    for folder in _mesh_relative_disk_folder_candidates(mesh_path):
        dirs.extend([
            folder,
            os.path.join(folder, "JSON"),
            os.path.join(folder, "fbx"),
            os.path.join(folder, "fbx", "JSON"),
        ])

    seen = set()
    result = []
    for folder in dirs:
        normalized = os.path.normcase(os.path.abspath(folder))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(folder)
    return result


def _find_dynamic_wind_json(data: dict, mesh_path: str, mesh_name: str, json_path: str = None):
    explicit = _dynamic_wind_json_from_data(data)
    dirs = _dynamic_wind_candidate_dirs(mesh_path, json_path)
    if explicit:
        explicit = os.path.expandvars(os.path.expanduser(explicit))
        if os.path.isabs(explicit) and os.path.isfile(explicit):
            return explicit
        for folder in dirs:
            candidate = os.path.join(folder, explicit)
            if os.path.isfile(candidate):
                return candidate

    suffix = str(
        _dynamic_wind_contract_rules().get("filename_suffix")
        or DYNAMIC_WIND_JSON_SUFFIX
    )
    filename = f"{mesh_name}{suffix}"
    for folder in dirs:
        candidate = os.path.join(folder, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def _import_dynamic_wind_if_available(mesh, mesh_path: str, mesh_name: str, data: dict, json_path: str = None) -> bool:
    if not _is_skeletal_mesh(mesh) or not _is_speedtree_asset(data, mesh_path):
        return False
    helper = getattr(unreal, "CodexDynamicWindImportLibrary", None)
    if not helper or not hasattr(helper, "import_dynamic_wind_json_to_skeletal_mesh"):
        _warn("  DynamicWind import helper missing; tree wind data skipped")
        return False
    wind_json = _find_dynamic_wind_json(data, mesh_path, mesh_name, json_path)
    if not wind_json:
        _warn(f"  DynamicWind JSON not found for tree skeletal mesh: {mesh_name}")
        return False
    try:
        result = helper.import_dynamic_wind_json_to_skeletal_mesh(mesh, wind_json)
    except Exception as exc:
        _warn(f"  DynamicWind import failed: {wind_json} ({exc})")
        return False
    _log(f"  DynamicWind <- {wind_json} ({result})")
    return True


def _new_skeletal_material_entry(slot_name: str, material, old_entry=None):
    entry = unreal.SkeletalMaterial()
    entry.set_editor_property("material_interface", material)
    entry.set_editor_property("material_slot_name", slot_name)
    try:
        entry.set_editor_property("imported_material_slot_name", slot_name)
    except Exception:
        pass
    if old_entry is not None:
        for prop in ("uv_channel_data", "overlay_material_interface"):
            try:
                entry.set_editor_property(prop, old_entry.get_editor_property(prop))
            except Exception:
                pass
    return entry


def _remap_skeletal_material_sections(mesh, ordered) -> bool:
    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    method = getattr(helper, "remap_skeletal_mesh_material_sections", None)
    if not callable(method):
        raise RuntimeError(
            "CodexMaterialTools skeletal material-section remap helper is missing"
        )
    old_indices = [int(old_index) for old_index, _new_index in ordered]
    new_indices = [int(new_index) for _old_index, new_index in ordered]
    result = method(mesh, old_indices, new_indices, True)
    values = result if isinstance(result, tuple) else (result,)
    explicit_success = next(
        (value for value in values if isinstance(value, bool)),
        None,
    )
    payload_text = next(
        (
            value
            for value in values
            if isinstance(value, str) and value.lstrip().startswith("{")
        ),
        "{}",
    )
    errors = next((value for value in values if isinstance(value, list)), [])
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        payload = {}
    # Depending on the live UE Python wrapper, a BlueprintCallable function
    # with output references can surface only OutJson (or OutJson/OutErrors)
    # and omit its native bool return.  The helper writes the same result to
    # the required JSON ``ok`` field, so use that authoritative value when no
    # explicit bool was exposed.
    success = (
        explicit_success
        if explicit_success is not None
        else bool(payload.get("ok"))
    )
    if not success:
        raise RuntimeError(
            "skeletal material-section remap failed: "
            + "; ".join(str(value) for value in errors)
            + (" | " + payload_text if payload_text else "")
        )
    return bool(payload.get("changed"))


def _complete_skeletal_section_remap(material_entries, ordered):
    """Map duplicate imported slots onto the canonical sidecar slot domain."""
    direct = {
        int(old_index): new_index
        for new_index, (old_index, _slot_name, _material) in enumerate(ordered)
    }
    desired_names = []
    for _old_index, slot_name, material in ordered:
        names = {str(slot_name or "").strip().casefold()}
        if material is not None:
            for getter in ("get_name", "get_path_name"):
                try:
                    value = str(getattr(material, getter)() or "").strip()
                except Exception:
                    value = ""
                if value:
                    names.add(value.casefold())
                    names.add(value.rsplit("/", 1)[-1].split(".", 1)[0].casefold())
        names.discard("")
        desired_names.append(names)
    desired_tree_parts = [
        _tree_part_key({"name": " ".join(sorted(names))})
        for names in desired_names
    ]

    remap = []
    for old_index, entry in enumerate(material_entries):
        new_index = direct.get(old_index)
        if new_index is None:
            old_names = {
                str(value or "").strip().casefold()
                for value in _static_material_name_values(entry)
                if str(value or "").strip()
            }
            matches = [
                index
                for index, names in enumerate(desired_names)
                if old_names & names
            ]
            if len(matches) == 1:
                new_index = matches[0]
            if new_index is None:
                old_tree_part = _tree_part_key(
                    {"name": " ".join(sorted(old_names))}
                )
                tree_part_matches = [
                    index
                    for index, tree_part in enumerate(desired_tree_parts)
                    if old_tree_part and tree_part == old_tree_part
                ]
                if len(tree_part_matches) == 1:
                    new_index = tree_part_matches[0]
        if new_index is not None:
            remap.append((old_index, new_index))
    return remap


def _assign_skeletal_slot(mesh, slot_index: int, material, slot_name: str = None) -> bool:
    property_name, material_entries = _mesh_material_entries(mesh)
    if property_name != "materials" or slot_index < 0 or slot_index >= len(material_entries):
        return False

    slot_name = str(slot_name or "").strip()
    if not slot_name:
        slot_name = str(material_entries[slot_index].get_editor_property("material_slot_name"))

    current_material = material_entries[slot_index].get_editor_property("material_interface")
    current_slot = str(material_entries[slot_index].get_editor_property("material_slot_name"))
    if (
        current_slot == slot_name
        and current_material is not None
        and material is not None
        and current_material.get_path_name() == material.get_path_name()
    ):
        return False

    new_entries = list(material_entries)
    new_entries[slot_index] = _new_skeletal_material_entry(
        slot_name,
        material,
        material_entries[slot_index],
    )
    mesh.set_editor_property("materials", new_entries)
    return True


def _normalize_skeletal_material_slots(mesh, assignments: dict) -> bool:
    if not _is_skeletal_mesh(mesh) or not assignments:
        return False

    property_name, material_entries = _mesh_material_entries(mesh)
    if property_name != "materials":
        return False

    ordered = [
        (slot_index, slot_name, material)
        for slot_index, (slot_name, material) in sorted(assignments.items())
    ]

    unchanged = len(material_entries) == len(ordered)
    if unchanged:
        for new_index, (_old_index, slot_name, material) in enumerate(ordered):
            current_entry = material_entries[new_index]
            try:
                current_slot = str(current_entry.get_editor_property("material_slot_name"))
                current_material = current_entry.get_editor_property("material_interface")
            except Exception:
                unchanged = False
                break
            if current_slot != slot_name:
                unchanged = False
                break
            if bool(current_material) != bool(material):
                unchanged = False
                break
            if (
                current_material is not None
                and material is not None
                and current_material.get_path_name() != material.get_path_name()
            ):
                unchanged = False
                break
    materials_changed = not unchanged
    section_remap = _complete_skeletal_section_remap(material_entries, ordered)
    # The sidecar is the canonical slot domain.  Reimport can append duplicate
    # FBX slots, and retaining those indices makes every later Assembly build
    # treat the drift as authored data.  Compact first, then remap every proven
    # imported section through the editor helper so repeated reimports are
    # idempotent instead of accumulating aliases.
    if materials_changed:
        new_entries = []
        for old_index, slot_name, material in ordered:
            old_entry = material_entries[old_index] if 0 <= old_index < len(material_entries) else None
            new_entries.append(_new_skeletal_material_entry(slot_name, material, old_entry))
        mesh.set_editor_property("materials", new_entries)

    try:
        sections_changed = _remap_skeletal_material_sections(mesh, section_remap)
    except Exception:
        if materials_changed:
            mesh.set_editor_property("materials", material_entries)
        raise
    return materials_changed or sections_changed


def _set_material_interface(mesh, slot_index: int, material) -> bool:
    property_name, material_entries = _mesh_material_entries(mesh)
    if not property_name or slot_index < 0 or slot_index >= len(material_entries):
        return False

    current = material_entries[slot_index].get_editor_property("material_interface")
    if (
        current is not None
        and material is not None
        and current.get_path_name() == material.get_path_name()
    ):
        return False

    if hasattr(mesh, "set_material"):
        mesh.set_material(slot_index, material)
        return True

    material_entries[slot_index].set_editor_property("material_interface", material)
    mesh.set_editor_property(property_name, material_entries)
    return True


def _assign_slot(mesh, slot_index: int, material, slot_name: str = None) -> bool:
    """메쉬 슬롯에 머티리얼 할당. FBX import 결과 슬롯 수가 JSON 과 달라도 안전하게 가드."""
    _property_name, material_entries = _mesh_material_entries(mesh)
    if _is_skeletal_mesh(mesh):
        return _assign_skeletal_slot(mesh, slot_index, material, slot_name)
    if slot_index < 0 or slot_index >= len(material_entries):
        _warn(f"  슬롯 인덱스 {slot_index} 가 메쉬 슬롯 수({len(material_entries)})를 벗어남 — 할당 skip")
        return False
    material_changed = _set_material_interface(mesh, slot_index, material)
    slot_renamed = _rename_material_slot(mesh, slot_index, slot_name)
    return material_changed or slot_renamed


def _surface_layer_param(param: str) -> str:
    return LAYER_PARAM_BY_LEGACY_PARAM.get(str(param or ""), str(param or ""))


def _assign_layer_zero_textures(
    mi,
    param_tex_map: dict,
    clear_missing_managed: bool = False,
) -> bool:
    """Fallback for stale editors: Python can address only material layer index 0."""
    changed = False
    accepted_names = set()
    association = unreal.MaterialParameterAssociation.LAYER_PARAMETER
    for param, tex_path in param_tex_map.items():
        try:
            tex = unreal.load_asset(tex_path)
            if not _is_texture2d(tex):
                continue
            current = unreal.MaterialEditingLibrary.get_material_instance_texture_parameter_value(
                mi,
                param,
                association,
            )
            if current is not None and current.get_path_name() == tex.get_path_name():
                accepted_names.add(str(param))
                continue
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(
                mi,
                param,
                tex,
                association,
            )
        except Exception as exc:
            _warn(
                f"  layer[0] texture role left empty: {param} ({exc}); "
                "handoff continues"
            )
            continue
        accepted_names.add(str(param))
        _log(f"  layer[0] {param} -> {tex_path.split('/')[-1]} (python fallback)")
        changed = True
    if clear_missing_managed:
        changed |= _prune_managed_texture_parameter_overrides(
            mi,
            KNOWN_PARAMS,
            set(param_tex_map),
            managed_bindings={
                (name, "LAYER_PARAMETER", 0) for name in KNOWN_PARAMS
            },
            keep_bindings={
                (name, "LAYER_PARAMETER", 0) for name in accepted_names
            },
        )
    return changed


def _tree_texture_param_allowed(param: str, preset: dict = None) -> bool:
    if not preset or preset.get("key") != "tree":
        return True
    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        return contract_api.tree_texture_param_allowed(
            param,
            preset.get("tree_shading"),
        )
    param = str(param or "").strip().casefold()
    if param == "transmission":
        return False
    if (
        preset.get("tree_shading") == "wood"
        and param in {"alpha", "opacity", "opacity map", "subsurface"}
    ):
        return False
    return True


def _entry_layers(entry: dict, preset: dict = None):
    layers = entry.get("layers") or []
    normalized = []
    for layer_index, layer in enumerate(layers):
        textures = []
        for texture in layer.get("textures", []):
            item = dict(texture)
            item["param"] = _surface_layer_param(item.get("param"))
            if _tree_texture_param_allowed(item["param"], preset):
                textures.append(item)
        normalized.append(
            {
                "name": str(layer.get("name") or f"Layer_{layer_index + 1:02d}"),
                "index": int(layer.get("index", layer_index)),
                "textures": textures,
            }
        )
    if normalized:
        return normalized

    textures = []
    for texture in entry.get("textures", []):
        item = dict(texture)
        item["param"] = _surface_layer_param(item.get("param"))
        if _tree_texture_param_allowed(item["param"], preset):
            textures.append(item)
    return [{"name": "Base", "index": 0, "textures": textures}] if textures else []


def _import_layer_textures(
    layers,
    tex_cache: dict,
    force_reimport: bool = False,
    virtual_texture_streaming=None,
):
    layer_maps = []
    for layer in layers:
        param_tex_map = {}
        for texture in layer.get("textures", []):
            param = texture.get("param")
            tex_path = _import_texture(
                texture.get("file"),
                texture.get("asset_name"),
                param,
                tex_cache,
                force_reimport=force_reimport,
                virtual_texture_streaming=texture.get(
                    "virtual_texture_streaming",
                    virtual_texture_streaming,
                ),
            )
            if tex_path and param in KNOWN_PARAMS:
                param_tex_map[param] = tex_path
        layer_maps.append(
            {
                "name": layer.get("name", "Base"),
                "index": int(layer.get("index", len(layer_maps))),
                "textures": param_tex_map,
            }
        )
    return layer_maps


def _sync_entry_texture_assets(entry: dict, preset: dict, tex_cache: dict):
    """Import or refresh sidecar textures without taking ownership of an MI/MYI.

    Existing material instances can remain assignment-only while the Texture2D
    assets they already reference still follow the latest Blender handoff.  The
    content-hash gate in :func:`_import_texture` keeps unchanged textures a no-op.
    """
    return _import_layer_textures(
        _entry_layers(entry, preset),
        tex_cache,
        virtual_texture_streaming=preset.get("virtual_textures"),
    )


def reimport_textures_from_json(json_path: str) -> int:
    json_path = os.path.abspath(str(json_path or ""))
    if not os.path.isfile(json_path):
        _warn(f"texture reimport JSON missing: {json_path}")
        return 0
    try:
        with open(json_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        _warn(f"texture reimport JSON read failed: {json_path} ({exc})")
        return 0

    tex_cache = _load_texture_cache()
    imported = 0
    seen = set()
    for entry in data.get("materials", []):
        preset = _master_preset(data, entry)
        for layer in _entry_layers(entry, preset):
            for texture in layer.get("textures", []):
                asset_name = texture.get("asset_name")
                param = texture.get("param")
                key = (asset_name, param)
                if key in seen:
                    continue
                seen.add(key)
                tex_path = _import_texture(
                    texture.get("file"),
                    asset_name,
                    param,
                    tex_cache,
                    force_reimport=True,
                )
                if tex_path:
                    imported += 1
    _save_texture_cache(tex_cache)
    _log(f"texture reimport complete: {imported} texture(s)")
    return imported


def _assign_surface_layer_textures(
    mi,
    layer_maps,
    clear_missing_managed: bool = False,
) -> bool:
    """Assign imported textures to a material instance using shared layer params.

    The Unreal Python API can address layer parameters by association but not by
    layer index. If the Codex C++ helper is available it handles indexed layer
    parameters; otherwise this falls back to the first layer only.
    """
    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    if helper and hasattr(helper, "set_material_instance_layer_texture_parameter_value"):
        changed = False
        keep_bindings = set()
        layer_indices = {
            int(layer.get("index", 0)) for layer in layer_maps
        }
        for layer in layer_maps:
            for param, tex_path in layer.get("textures", {}).items():
                layer_index = int(layer.get("index", 0))
                try:
                    tex = unreal.load_asset(tex_path)
                    if not _is_texture2d(tex):
                        continue
                    role_changed = helper.set_material_instance_layer_texture_parameter_value(
                        mi,
                        str(param),
                        tex,
                        layer_index,
                    )
                except Exception as exc:
                    _warn(
                        f"  layer[{layer_index}] texture role left empty: "
                        f"{param} ({exc}); handoff continues"
                    )
                    continue
                keep_bindings.add((str(param), "LAYER_PARAMETER", layer_index))
                if role_changed:
                    _log(
                        f"  layer[{layer_index}] {param} -> "
                        f"{tex_path.split('/')[-1]}"
                    )
                    changed = True
        if clear_missing_managed:
            changed |= _prune_managed_texture_parameter_overrides(
                mi,
                KNOWN_PARAMS,
                {name for name, _association, _index in keep_bindings},
                managed_bindings={
                    (name, "LAYER_PARAMETER", layer_index)
                    for name in KNOWN_PARAMS
                    for layer_index in layer_indices
                },
                keep_bindings=keep_bindings,
            )
        return changed

    if len(layer_maps) > 1 or any(int(layer.get("index", 0)) != 0 for layer in layer_maps):
        _warn("  indexed layer helper missing; assigning only layer[0] textures")
    first_layer = next(
        (layer.get("textures", {}) for layer in layer_maps if int(layer.get("index", 0)) == 0),
        {},
    )
    return _assign_layer_zero_textures(
        mi,
        first_layer,
        clear_missing_managed=clear_missing_managed,
    )


def _first_layer_textures(layer_maps) -> dict:
    return next(
        (layer.get("textures", {}) for layer in layer_maps if int(layer.get("index", 0)) == 0),
        {},
    )


def _assign_flat_textures(
    mi,
    layer_maps,
    param_map: dict,
    label: str,
    clear_missing_managed: bool = False,
) -> bool:
    changed = False
    keep_names = set()
    for layer_param, tex_path in _first_layer_textures(layer_maps).items():
        flat_param = param_map.get(layer_param)
        if not flat_param:
            continue
        try:
            tex = unreal.load_asset(tex_path)
            if not _is_texture2d(tex):
                continue
            current = unreal.MaterialEditingLibrary.get_material_instance_texture_parameter_value(
                mi, flat_param
            )
            if current is not None and current.get_path_name() == tex.get_path_name():
                keep_names.add(flat_param)
                continue
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(
                mi, flat_param, tex
            )
        except Exception as exc:
            _warn(
                f"  {label} texture role left empty: {flat_param} ({exc}); "
                "handoff continues"
            )
            continue
        keep_names.add(flat_param)
        _log(f"  {label} {flat_param} <- {tex_path.split('/')[-1]}")
        changed = True
    if clear_missing_managed:
        changed |= _prune_managed_texture_parameter_overrides(
            mi,
            set(param_map.values()),
            keep_names,
            managed_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in param_map.values()
            },
            keep_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in keep_names
            },
        )
    return changed


def _texture_parameter_name(parameter_value):
    try:
        info = parameter_value.get_editor_property("parameter_info")
        return str(info.get_editor_property("name"))
    except Exception:
        try:
            return str(parameter_value.get_editor_property("parameter_name"))
        except Exception:
            return ""


def _parameter_association_key(value) -> str:
    text = str(value or "GLOBAL_PARAMETER")
    # Unreal 5.8 renders enums as '<MaterialParameterAssociation.GLOBAL_PARAMETER: 2>'.
    # A bare string and the live enum must identify the same owned binding.
    return text.rsplit(".", 1)[-1].split(":", 1)[0].strip(" <>\t\r\n").upper()


def _texture_parameter_binding(parameter_value):
    name = _texture_parameter_name(parameter_value)
    try:
        info = parameter_value.get_editor_property("parameter_info")
    except Exception:
        info = None
    association = "GLOBAL_PARAMETER"
    index = -1
    if info is not None:
        try:
            association = _parameter_association_key(
                info.get_editor_property("association")
            )
        except Exception:
            pass
        try:
            index = int(info.get_editor_property("index"))
        except Exception:
            pass
    return name, association, index


def _prune_managed_texture_parameter_overrides(
    asset,
    managed_names: set,
    keep_names: set,
    update: bool = True,
    managed_bindings: set = None,
    keep_bindings: set = None,
) -> bool:
    """Clear stale pipeline-owned texture roles without touching artist roles."""
    managed_names = {str(name) for name in managed_names if str(name or "")}
    keep_names = {str(name) for name in keep_names if str(name or "")}
    if not managed_names:
        return False
    try:
        values = list(asset.get_editor_property("texture_parameter_values"))
    except Exception:
        return False

    if managed_bindings is not None:
        managed_bindings = {
            (str(name), _parameter_association_key(association), int(index))
            for name, association, index in managed_bindings
        }
        keep_bindings = {
            (str(name), _parameter_association_key(association), int(index))
            for name, association, index in (keep_bindings or set())
        }
        removed = [
            value
            for value in values
            if _texture_parameter_binding(value) in managed_bindings
            and _texture_parameter_binding(value) not in keep_bindings
        ]
    else:
        removed = [
            value
            for value in values
            if _texture_parameter_name(value) in managed_names
            and _texture_parameter_name(value) not in keep_names
        ]
    if not removed:
        return False
    removed_ids = {id(value) for value in removed}
    try:
        asset.set_editor_property(
            "texture_parameter_values",
            [value for value in values if id(value) not in removed_ids],
        )
    except Exception as exc:
        _warn(f"  managed texture override clear unavailable: {exc}")
        return False
    if update:
        try:
            unreal.MaterialEditingLibrary.update_material_instance(asset)
        except Exception:
            pass
    _log(
        "  stale managed texture overrides cleared: "
        + ", ".join(_texture_parameter_name(value) for value in removed)
    )
    return True


def _layer_instance_base_name(mat_base: str, preset: dict, entry: dict) -> str:
    material_layer = entry.get("material_layer") if isinstance(entry.get("material_layer"), dict) else {}
    for value in (
        material_layer.get("instance_name"),
        entry.get("material_layer_instance_name"),
        entry.get("layer_instance_name"),
    ):
        value = str(value or "").strip()
        if value:
            return value[4:] if value.startswith("MYI_") else value

    base = str(mat_base or "").strip()
    for prefix in preset.get("layer_instance_strip_prefixes", []):
        prefix = str(prefix or "")
        if prefix and base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base


def _layer_instance_path(mat_base: str, preset: dict, entry: dict):
    material_layer = entry.get("material_layer") if isinstance(entry.get("material_layer"), dict) else {}
    for key in ("instance_path", "path"):
        value = str(material_layer.get(key) or "").strip()
        if value:
            return value.split(".")[0]
    for key in (
        "material_layer_instance_path",
        "layer_instance_path",
        "target_layer_instance_path",
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            return value.split(".")[0]

    folder = str(entry.get("layer_instance_folder") or preset.get("layer_instance_folder") or "").rstrip("/")
    if not folder:
        return None
    base = _layer_instance_base_name(mat_base, preset, entry)
    if not base:
        return None
    return f"{folder}/MYI_{base}"


def _validate_codex_test_material_scope(data: dict, mesh_path: str) -> bool:
    if not _is_codex_test_asset_path(mesh_path):
        return False
    scope = data.get("codex_test_asset_scope")
    if not isinstance(scope, dict):
        raise RuntimeError(
            "Codex test material sidecar has no isolated asset scope contract"
        )
    root = str(scope.get("root") or "").rstrip("/")
    if not _is_codex_test_asset_path(root):
        raise RuntimeError("Codex test material scope root is outside /Game/Codex/Tests")
    for entry in data.get("materials", []):
        target_path = _entry_target_material_path(entry)
        if not target_path or not target_path.startswith(root + "/MI/"):
            raise RuntimeError(
                "Codex test material target is outside the isolated MI scope"
            )
        material_layer = entry.get("material_layer")
        if not isinstance(material_layer, dict):
            raise RuntimeError("Codex test material has no isolated layer target")
        layer_path = str(
            material_layer.get("instance_path")
            or material_layer.get("path")
            or ""
        ).split(".", 1)[0]
        if not layer_path.startswith(root + "/MYI/"):
            raise RuntimeError(
                "Codex test layer target is outside the isolated MYI scope"
            )
    return True


def _layer_parent_path(preset: dict, entry: dict):
    material_layer = entry.get("material_layer") if isinstance(entry.get("material_layer"), dict) else {}
    for key in ("parent", "parent_layer"):
        value = str(material_layer.get(key) or "").strip()
        if value:
            return value.split(".")[0]
    for key in ("material_layer_parent", "layer_parent", "parent_layer"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value.split(".")[0]
    tree_part = str(preset.get("tree_part") or "").casefold()
    layer_parents = preset.get("layer_parents_by_name")
    if tree_part and isinstance(layer_parents, dict):
        value = str(layer_parents.get(tree_part) or "").strip()
        if value:
            return value.split(".")[0]
    value = str(preset.get("layer_parent") or "").strip()
    return value.split(".")[0] if value else None


def _as_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _linear_color(value):
    if isinstance(value, dict):
        channels = [_as_float(value.get(key)) for key in ("r", "g", "b", "a")]
        if channels[3] is None:
            channels[3] = 1.0
    elif isinstance(value, (list, tuple)):
        channels = [_as_float(item) for item in value[:4]]
        if len(channels) == 3:
            channels.append(1.0)
    else:
        return None
    if len(channels) != 4 or any(channel is None for channel in channels):
        return None
    return unreal.LinearColor(*channels)


def _parameter_source_dicts(entry: dict):
    entry = entry or {}
    material_layer = entry.get("material_layer") if isinstance(entry.get("material_layer"), dict) else {}
    for source in (material_layer, entry):
        yield source
        wind = source.get("wind")
        if isinstance(wind, dict):
            yield wind


def _layer_scalar_params(entry: dict) -> dict:
    params = {}
    for source in _parameter_source_dicts(entry):
        for key in ("scalar_parameters", "scalars", "scalar_params", "wind_scalars"):
            values = source.get(key)
            if isinstance(values, dict):
                for name, value in values.items():
                    parsed = _as_float(value)
                    if parsed is not None:
                        params[str(name)] = parsed
        for name, value in source.items():
            if "wind" not in str(name).casefold():
                continue
            parsed = _as_float(value)
            if parsed is not None:
                params[str(name)] = parsed
    return params


def _layer_vector_params(entry: dict) -> dict:
    params = {}
    for source in _parameter_source_dicts(entry):
        for key in ("vector_parameters", "vectors", "vector_params", "wind_vectors"):
            values = source.get(key)
            if isinstance(values, dict):
                for name, value in values.items():
                    parsed = _linear_color(value)
                    if parsed is not None:
                        params[str(name)] = parsed
        for name, value in source.items():
            if "wind" not in str(name).casefold():
                continue
            parsed = _linear_color(value)
            if parsed is not None:
                params[str(name)] = parsed
    return params


def _layer_texture_remap(preset: dict, entry: dict) -> dict:
    material_layer = entry.get("material_layer") if isinstance(entry.get("material_layer"), dict) else {}
    mapping = (
        material_layer.get("texture_remap")
        or entry.get("layer_texture_remap")
        or entry.get("material_layer_texture_remap")
    )
    if not isinstance(mapping, dict):
        mapping = preset.get("layer_texture_remap", {})
    result = {str(key): str(value) for key, value in dict(mapping).items() if key and value}
    if preset.get("key") == "tree":
        result.pop("Transmission", None)
        result["Alpha"] = "Opacity Map"
        result["Opacity"] = "Opacity Map"
        result["Opacity Map"] = "Opacity Map"
        # MY_Tree_Branch has no opacity parameter. Stem exports may still
        # contain an opaque SBS output; do not send it to the layer helper.
        if preset.get("tree_shading") == "stem":
            for source, target in list(result.items()):
                if target == "Opacity Map":
                    result.pop(source)
        if preset.get("tree_shading") != "wood":
            result["Subsurface"] = "Subsurface"
        else:
            result.pop("Subsurface", None)
    return result


def _unreal_helper_result_parts(result):
    """Extract a return bool, JSON report, and errors regardless of UFUNCTION order."""
    returned_ok = None
    report_text = ""
    errors = []
    items = result if isinstance(result, tuple) else (result,)
    for item in items:
        if isinstance(item, bool) and returned_ok is None:
            returned_ok = item
        elif isinstance(item, str):
            value = item.strip()
            if value.startswith(("{", "[")) and not report_text:
                report_text = item
            elif value:
                errors.append(item)
        elif isinstance(item, (list, tuple, set)):
            errors.extend(str(value) for value in item if value)
        elif item is not None and hasattr(item, "__iter__"):
            try:
                errors.extend(str(value) for value in item if value)
            except TypeError:
                pass
    return returned_ok, report_text, errors


def _call_create_or_update_layer_instance(helper, parent_layer, layer_path, texture_params, scalar_params=None, vector_params=None):
    result = helper.create_or_update_material_layer_instance(
        parent_layer,
        layer_path,
        texture_params,
        scalar_params or {},
        vector_params or {},
        False,
        True,
    )
    returned_ok, report_text, errors = _unreal_helper_result_parts(result)
    try:
        report = json.loads(report_text) if report_text else {}
    except Exception:
        report = {}
    ok = bool(
        report.get(
            "ok",
            returned_ok
            if returned_ok is not None
            else bool(report.get("layer_instance")) and not errors,
        )
    )
    errors.extend(str(item) for item in (report.get("errors") or []))
    return ok, errors, report


def _is_codex_test_asset_path(asset_path: str) -> bool:
    package_path = str(asset_path or "").split(".", 1)[0].replace("\\", "/")
    return package_path.casefold().startswith("/game/codex/tests/")


def _normalize_material_layer_asset(
    helper,
    method_name: str,
    asset_path: str,
    label: str,
    mutation_scope_path: str = "",
):
    if (
        _is_codex_test_asset_path(mutation_scope_path)
        and not _is_codex_test_asset_path(asset_path)
    ):
        _log(f"  {label} kept read-only for isolated test: {asset_path}")
        return
    method = getattr(helper, method_name, None)
    if method is None:
        raise RuntimeError(f"CodexMaterialTools {label} normalization helper missing")

    result = method(asset_path)
    returned_ok, report_text, errors = _unreal_helper_result_parts(result)

    try:
        report = json.loads(report_text) if report_text else {}
    except Exception:
        report = {}
    report_ok = bool(report.get("ok", returned_ok))
    if not report_ok or errors:
        detail = " | ".join(errors) or report_text or "unknown normalization failure"
        raise RuntimeError(f"{label} normalization failed: {asset_path} ({detail})")

    changed = (
        report.get("removed_placeholder_count", 0)
        or report.get("removed_set_declaration_count", 0)
        or report.get("removed_get_declaration_count", 0)
        or report.get("restored_tree_input_count", 0)
    )
    if changed:
        _log(f"  {label} normalized: {asset_path}")


def _normalize_material_layer_dependencies(
    helper,
    preset: dict,
    parent_layer: str,
    mutation_scope_path: str = "",
):
    master_path = str(preset.get("master") or "")
    if master_path:
        _normalize_material_layer_asset(
            helper,
            "normalize_material_layer_placeholders",
            master_path,
            "material master",
            mutation_scope_path=mutation_scope_path,
        )
    # NormalizeMaterialFunctionAttributeNodes repairs the MF_TreeMaterialBase
    # inputs.  Generic layer/cloth parents use MF_MaterialBase instead and must
    # never be sent through that destructive, tree-specific precondition.
    if preset.get("key") == "tree" and parent_layer:
        _normalize_material_layer_asset(
            helper,
            "normalize_material_function_attribute_nodes",
            parent_layer,
            "material layer function",
            mutation_scope_path=mutation_scope_path,
        )


def _material_instance_background_matches(helper, mi, layer_asset):
    """Return True/False when DumpMaterialLayers can verify the desired MYI."""
    dump = getattr(helper, "dump_material_layers", None)
    if dump is None:
        return None
    try:
        result = dump(mi.get_path_name())
    except Exception:
        return None
    returned_ok, report_text, _errors = _unreal_helper_result_parts(result)
    try:
        report = json.loads(report_text) if report_text else {}
    except Exception:
        return None
    if not bool(report.get("ok", returned_ok)):
        return None
    layers = list(report.get("layers") or [])
    if not report.get("has_layers") or not layers:
        return False
    background = next(
        (row for row in layers if int(row.get("index", -1)) == 0),
        layers[0],
    )
    actual = str(background.get("path") or "").split(".", 1)[0]
    desired = str(layer_asset.get_path_name() or "").split(".", 1)[0]
    return bool(actual and actual == desired)


def _call_set_material_instance_background_layer(helper, mi, layer_asset):
    if hasattr(helper, "set_material_instance_background_layer_report"):
        result = helper.set_material_instance_background_layer_report(mi, layer_asset)
        returned_ok, report_json, errors = _unreal_helper_result_parts(result)
        if report_json:
            try:
                report = json.loads(report_json)
                errors.extend(str(item) for item in (report.get("errors") or []))
                ok = bool(
                    report.get("ok", returned_ok)
                    or report.get("desired_is_set")
                )
            except Exception as exc:
                return False, [f"background layer report parse failed: {exc}"]
            return ok, errors
        verified = _material_instance_background_matches(helper, mi, layer_asset)
        if verified is not None:
            return verified, errors
        if errors:
            return False, errors
    if hasattr(helper, "set_material_instance_background_layer_with_errors"):
        result = helper.set_material_instance_background_layer_with_errors(mi, layer_asset)
        returned_ok, _report_text, errors = _unreal_helper_result_parts(result)
        changed = bool(returned_ok)
    else:
        changed = bool(helper.set_material_instance_background_layer(mi, layer_asset))
        errors = []
    verified = _material_instance_background_matches(helper, mi, layer_asset)
    return (changed if verified is None else verified), errors


def _assign_material_layer_instance(
    mi,
    mat_base: str,
    layer_maps,
    preset: dict,
    entry: dict,
    clear_missing_managed: bool = False,
) -> bool:
    # Missing SpeedTree textures must not erase an existing MYI's overrides.
    if (entry or {}).get("speedtree_intent") and entry.get("tree_shading") == "foliage":
        imported = _first_layer_textures(layer_maps)
        declared = {
            texture.get("param")
            for layer in _entry_layers(entry, preset)
            for texture in layer.get("textures", [])
        }
        missing = (declared | {"Albedo"}) - set(imported)
        if missing:
            raise RuntimeError(
                "SpeedTree foliage texture handoff is incomplete; existing MYI preserved: "
                + str(entry.get("name") or mat_base)
                + "; missing=" + ",".join(sorted(missing))
            )
    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    if not helper or not hasattr(helper, "create_or_update_material_layer_instance"):
        _warn("  CodexMaterialTools layer instance helper missing; MYI assignment skipped")
        return False
    if not (
        hasattr(helper, "set_material_instance_background_layer_report")
        or hasattr(helper, "set_material_instance_background_layer_with_errors")
        or hasattr(helper, "set_material_instance_background_layer")
    ):
        _warn("  CodexMaterialTools background layer helper missing; MYI assignment skipped")
        return False

    parent_layer = _layer_parent_path(preset, entry)
    layer_path = _layer_instance_path(mat_base, preset, entry)
    if not parent_layer or not layer_path:
        _warn("  material layer instance path is incomplete; MYI assignment skipped")
        return False

    _normalize_material_layer_dependencies(
        helper,
        preset,
        parent_layer,
        mutation_scope_path=layer_path,
    )

    remap = _layer_texture_remap(preset, entry)
    texture_params = {}
    for layer_param, tex_path in _first_layer_textures(layer_maps).items():
        target_param = remap.get(layer_param)
        if target_param and tex_path:
            texture_params[target_param] = tex_path
    scalar_params = _layer_scalar_params(entry)
    vector_params = _layer_vector_params(entry)
    if scalar_params or vector_params:
        _log(
            "  material layer parameters: "
            f"{len(scalar_params)} scalar(s), {len(vector_params)} vector(s)"
        )

    ok, errors, layer_report = _call_create_or_update_layer_instance(
        helper,
        parent_layer,
        layer_path,
        texture_params,
        scalar_params,
        vector_params,
    )
    if not ok:
        _warn(f"  MYI create/update failed: {layer_path}")
        for error in errors or []:
            _warn(f"    {error}")
        return False

    layer_asset = unreal.load_asset(layer_path)
    if layer_asset is None:
        _warn(f"  MYI load failed after create/update: {layer_path}")
        return False
    if layer_report.get("created"):
        try:
            _mark_new_asset_for_add(layer_path, "material layer instance")
        except Exception:
            try:
                unreal.EditorAssetLibrary.delete_asset(layer_path)
            except Exception as rollback_exc:
                _warn(
                    f"  material layer instance rollback failed: "
                    f"{layer_path} ({rollback_exc})"
                )
            raise

    layer_overrides_pruned = False
    if clear_missing_managed:
        layer_overrides_pruned = _prune_managed_texture_parameter_overrides(
            layer_asset,
            set(remap.values()),
            set(texture_params),
            update=False,
            managed_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in remap.values()
            },
            keep_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in texture_params
            },
        )
        if layer_overrides_pruned:
            for method_name in ("update_parameter_set", "post_edit_change"):
                method = getattr(layer_asset, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass
            try:
                layer_saved = unreal.EditorAssetLibrary.save_asset(
                    layer_path,
                    only_if_is_dirty=False,
                )
            except Exception as exc:
                _warn(
                    "material layer instance save failed after stale-role clear: "
                    f"{layer_path} ({exc}); handoff continues"
                )
            else:
                if not layer_saved:
                    _warn(
                        "material layer instance save failed after stale-role clear: "
                        f"{layer_path}; handoff continues"
                    )

    # Remove stale flat/layer overrides before the C++ helper persists the MI.
    # Do not trigger a live material preview update here; UE 5.8 can assert
    # while compiling a newly-created Material Layer Instance thumbnail.
    managed_mi_names = (
        set(KNOWN_PARAMS)
        | set(FLAT_PARAM_BY_LAYER_PARAM.values())
        | set(COAT_PARAM_BY_LAYER_PARAM.values())
        | set(ASSET_SURFACE_PARAM_BY_LAYER_PARAM.values())
        | set(remap.values())
    )
    overrides_pruned = _prune_managed_texture_parameter_overrides(
        mi,
        managed_mi_names,
        set(),
        update=False,
    )
    changed, background_errors = _call_set_material_instance_background_layer(
        helper, mi, layer_asset
    )
    if background_errors:
        _warn(f"  background MYI assignment failed: {layer_path}")
        for error in background_errors:
            _warn(f"    {error}")
        return False
    if not changed:
        _warn(f"  background MYI assignment not verified: {layer_path}")
        return False
    changed = layer_overrides_pruned or overrides_pruned or changed
    # SetMaterialLayers can be a no-op when the MYI path stays the same. Its
    # changed parameter values still require rebuilding the owning MI's cached
    # render parameters/permutation; otherwise the viewport keeps parent defaults.
    # Save directly to avoid the newly-created MYI thumbnail path on UE 5.8.
    safe_save = getattr(helper, "save_asset_package_without_thumbnail", None)
    if callable(safe_save):
        # Refresh the function instance first. Updating only the owning MI leaves
        # stale compiled MYI texture/sampler state (including VT aspect ratios).
        unreal.MaterialEditingLibrary.update_material_function(layer_asset)
        if not safe_save(layer_asset):
            raise RuntimeError(f"material layer refresh save failed: {layer_path}")
        unreal.MaterialEditingLibrary.update_material_instance(mi)
        if not safe_save(mi):
            raise RuntimeError(f"layer-backed material refresh save failed: {mi.get_path_name()}")
        changed = True
        _log(f"  layer-backed MI render parameters refreshed: {mi.get_path_name()}")
    _log(f"  background MYI <- {layer_path}")
    return changed


def _assign_master_textures_impl(
    mi,
    layer_maps,
    assignment: str,
    preset: dict = None,
    entry: dict = None,
    mat_base: str = "",
    clear_missing_managed: bool = False,
) -> bool:
    preset = preset or {}
    entry = entry or {}
    if assignment == "none":
        return False
    if assignment == "layer":
        return _assign_surface_layer_textures(
            mi,
            layer_maps,
            clear_missing_managed=clear_missing_managed,
        )
    if assignment == "material_layer_instance":
        return _assign_material_layer_instance(
            mi,
            mat_base,
            layer_maps,
            preset,
            entry,
            clear_missing_managed=clear_missing_managed,
        )
    if assignment == "asset_surface_flat":
        return _assign_flat_textures(
            mi,
            layer_maps,
            ASSET_SURFACE_PARAM_BY_LAYER_PARAM,
            "asset_surface",
            clear_missing_managed=clear_missing_managed,
        )
    if assignment == "coat_flat":
        return _assign_flat_textures(
            mi,
            layer_maps,
            COAT_PARAM_BY_LAYER_PARAM,
            "coat",
            clear_missing_managed=clear_missing_managed,
        )
    return _assign_flat_textures(
        mi,
        layer_maps,
        FLAT_PARAM_BY_LAYER_PARAM,
        "prop",
        clear_missing_managed=clear_missing_managed,
    )


def _assign_master_textures(
    mi,
    layer_maps,
    assignment: str,
    preset: dict = None,
    entry: dict = None,
    mat_base: str = "",
    clear_missing_managed: bool = False,
) -> bool:
    """Apply texture parameters, gating structural material-layer failures."""
    try:
        changed = _assign_master_textures_impl(
            mi,
            layer_maps,
            assignment,
            preset=preset,
            entry=entry,
            mat_base=mat_base,
            clear_missing_managed=clear_missing_managed,
        )
    except Exception as exc:
        if assignment == "material_layer_instance":
            raise RuntimeError(
                f"material layer instance handoff failed: {exc}"
            ) from exc
        _warn(f"  texture parameter handoff incomplete; continuing: {exc}")
        # A role may already have been applied before the failure. Let callers
        # persist that safe subset instead of discarding the whole MI update.
        return True
    if assignment == "material_layer_instance" and not changed:
        raise RuntimeError(
            "material layer instance handoff was not created or verified"
        )
    return changed


def _material_instance_base_name(mat_name: str) -> str:
    contract_api = _speedtree_handoff_api()
    if contract_api is not None:
        return contract_api.material_instance_base_name(mat_name)
    if mat_name.startswith("M_"):
        return mat_name[2:]
    if mat_name.startswith("MI_"):
        return mat_name[3:]
    return mat_name


def _hair_target_material_name(mat_name: str, entry: dict) -> str:
    for key in ("material_instance_name", "target_material_name", "unreal_material_name"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return f"MI_{_material_instance_base_name(str(mat_name or ''))}"


def _hair_target_material_path(mat_name: str, entry: dict, preset: dict):
    folder = str(preset.get("mi_folder") or "").rstrip("/")
    target_name = _hair_target_material_name(mat_name, entry)
    if not folder or not target_name:
        return None
    target_path = _entry_target_material_path(entry)
    if target_path:
        normalized_target = str(target_path).replace("\\", "/").casefold()
        normalized_folder = f"{folder}/".replace("\\", "/").casefold()
        if normalized_target.startswith(normalized_folder):
            return target_path
        _warn(
            f"  ignoring non-hair target material path for hair '{mat_name}': "
            f"{target_path}"
        )
    return f"{folder}/{target_name}"


def _asset_path_excluded(path: str, fragments) -> bool:
    normalized = str(path or "").replace("\\", "/").casefold()
    return any(str(fragment or "").casefold() in normalized for fragment in fragments or [])


def _asset_data_class_name(asset_data):
    for property_name in ("asset_class_path", "asset_class"):
        try:
            value = getattr(asset_data, property_name)
        except Exception:
            continue
        if value:
            if property_name == "asset_class_path":
                try:
                    class_asset_name = getattr(value, "asset_name")
                except Exception:
                    try:
                        class_asset_name = value.get_editor_property("asset_name")
                    except Exception:
                        class_asset_name = None
                if class_asset_name:
                    return str(class_asset_name)
            text = str(value)
            return text.rsplit("/", 1)[-1].rsplit(".", 1)[-1].strip("'\"")
    return ""


def _asset_paths_named(asset_names, excluded_fragments=None, allowed_classes=None):
    wanted = {str(name or "").casefold() for name in asset_names if str(name or "").strip()}
    if not wanted:
        return []
    excluded_fragments = excluded_fragments or []
    allowed_classes = {str(name).casefold() for name in allowed_classes or []}
    asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()
    results = []
    try:
        assets = asset_registry.get_assets_by_path("/Game", recursive=True)
    except TypeError:
        assets = asset_registry.get_assets_by_path("/Game", True)
    for asset_data in assets:
        name = str(asset_data.asset_name)
        if name.casefold() not in wanted:
            continue
        if allowed_classes and _asset_data_class_name(asset_data).casefold() not in allowed_classes:
            continue
        path = str(asset_data.package_name)
        if _asset_path_excluded(path, excluded_fragments):
            continue
        results.append(path)
    return sorted(results, key=lambda path: ("/Materials/" not in path, path.casefold()))


def _hair_source_material_paths(mat_name: str, entry: dict, preset: dict):
    base_name = _material_instance_base_name(str(mat_name or ""))
    source_names = [
        base_name,
        f"{base_name}_Inst",
        f"{base_name}_LWHQ_Inst",
    ]
    return _asset_paths_named(
        source_names,
        preset.get("exclude_path_fragments"),
        allowed_classes={"MaterialInstanceConstant"},
    )


def _asset_base_material_path(asset):
    if asset is None or not hasattr(asset, "get_base_material"):
        return ""
    try:
        base_material = asset.get_base_material()
    except Exception:
        return ""
    try:
        return str(base_material.get_path_name())
    except Exception:
        return str(base_material or "")


def _wrong_generated_hair_material_paths(mat_name: str, entry: dict, preset: dict, target_path: str):
    target_name = _hair_target_material_name(mat_name, entry)
    candidates = set()

    entry_target_path = _entry_target_material_path(entry)
    if entry_target_path and entry_target_path != target_path:
        candidates.add(entry_target_path)

    for path in _asset_paths_named(
        [target_name],
        allowed_classes={"MaterialInstanceConstant"},
    ):
        if path == target_path:
            continue
        if _asset_path_excluded(path, preset.get("exclude_path_fragments")):
            candidates.add(path)

    wrong_paths = []
    for path in sorted(candidates):
        asset = unreal.load_asset(path)
        base_path = _asset_base_material_path(asset).replace("\\", "/").casefold()
        if "/game/material/assetsurface/" in base_path:
            wrong_paths.append(path)
    return wrong_paths


def _delete_wrong_generated_hair_materials(mat_name: str, entry: dict, preset: dict, target_path: str):
    deleted = []
    for wrong_path in _wrong_generated_hair_material_paths(mat_name, entry, preset, target_path):
        if not unreal.EditorAssetLibrary.does_asset_exist(wrong_path):
            continue
        if unreal.EditorAssetLibrary.delete_asset(wrong_path):
            deleted.append(wrong_path)
            _log(f"  deleted wrong generated hair MI: {wrong_path}")
        else:
            _warn(f"  wrong generated hair MI delete failed: {wrong_path}")
    return deleted


def _entry_reparents_existing_hair_material(entry: dict) -> bool:
    """Parent choice is separate from ownership of synchronized hair controls."""
    value = entry.get("reparent_existing_material_instance", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return value is True


def _load_or_create_hair_material(
    asset_tools,
    mat_name: str,
    entry: dict,
    preset: dict,
    preserve_existing: bool = False,
):
    target_path = _hair_target_material_path(mat_name, entry, preset)
    if not target_path:
        _warn(f"  hair material target path incomplete for '{mat_name}'")
        return None, None, False

    existing = _load_exact_material_instance(target_path)
    if existing is not None and preserve_existing:
        _log(f"  user-owned hair MI reused unchanged: {target_path}")
        return existing, target_path, False
    if existing is not None and not _entry_reparents_existing_hair_material(entry):
        # Hair and short-fur instances can share the export contract while using
        # different BSDF masters. Parameter synchronization never implies reparent.
        _log(f"  existing hair MI parent preserved: {target_path}")
        return existing, target_path, False
    if existing is None and not _entry_create_if_missing(entry, preset):
        _log(f"  requested hair MI is unavailable; slot left unchanged: {target_path}")
        return None, target_path, False

    master = _load_master_material(preset)
    if existing is not None and master is None:
        _log(f"  existing hair MI reused without parent mutation: {target_path}")
        return existing, target_path, False
    if master is None:
        return None, target_path, False
    mi, _path, created, _source = _load_or_copy_target_material(
        asset_tools,
        target_path,
        master_mat=master,
        create_if_missing=_entry_create_if_missing(entry, preset),
    )
    if mi is None:
        return None, target_path, False
    if not created and not _entry_reparents_existing_hair_material(entry):
        # A concurrent creator can resolve an MI after the initial lookup.
        return mi, target_path, False
    try:
        current_parent = mi.get_editor_property("parent")
    except Exception:
        current_parent = None
    if not _same_asset(current_parent, master):
        unreal.MaterialEditingLibrary.set_material_instance_parent(mi, master)
        unreal.MaterialEditingLibrary.update_material_instance(mi)
        _save_material_texture_update(target_path)
        _log(f"  hair MI parent update: {target_path} -> {master.get_path_name()}")
    return mi, target_path, bool(created)


def _assign_hair_tool_parameters(
    mi,
    entry: dict,
    layer_maps,
    initialize_instance_owned_parameters: bool = True,
    clear_missing_managed: bool = False,
) -> bool:
    changed = False
    keep_texture_names = set()
    for param, tex_path in _first_layer_textures(layer_maps).items():
        try:
            texture = unreal.load_asset(tex_path)
            if not _is_texture2d(texture):
                continue
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(
                mi, param, texture
            )
        except Exception as exc:
            _warn(
                f"  hair texture role left empty: {param} ({exc}); "
                "handoff continues"
            )
            continue
        keep_texture_names.add(str(param))
        _log(f"  hair texture {param} <- {tex_path.split('/')[-1]}")
        changed = True

    if clear_missing_managed:
        changed |= _prune_managed_texture_parameter_overrides(
            mi,
            KNOWN_PARAMS,
            keep_texture_names,
            managed_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in KNOWN_PARAMS
            },
            keep_bindings={
                (name, "GLOBAL_PARAMETER", -1) for name in keep_texture_names
            },
        )

    hair_tool = entry.get("hair_tool") or {}
    synced_parameters = {
        str(name) for name in (hair_tool.get("sync_parameters") or [])
    }
    # Existing instances own every control outside this explicit source list.
    # New instances can still initialize a legacy payload without a sync list.
    for name, value in (hair_tool.get("scalar_parameters") or {}).items():
        name = str(name)
        if (
            not initialize_instance_owned_parameters
            and name not in synced_parameters
        ):
            _log(f"  preserve existing hair MI scalar: {name}")
            continue
        unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
            mi, name, float(value)
        )
        changed = True
    for name, value in (hair_tool.get("vector_parameters") or {}).items():
        name = str(name)
        if (
            not initialize_instance_owned_parameters
            and name not in synced_parameters
        ):
            _log(f"  preserve existing hair MI vector: {name}")
            continue
        components = list(value or [])
        while len(components) < 4:
            components.append(1.0)
        color = unreal.LinearColor(
            float(components[0]),
            float(components[1]),
            float(components[2]),
            float(components[3]),
        )
        unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
            mi, name, color
        )
        changed = True
    if changed:
        unreal.MaterialEditingLibrary.update_material_instance(mi)
    return changed


def _ensure_hair_master_skeletal_mesh_usage(mi) -> bool:
    try:
        master = mi.get_base_material()
    except Exception:
        master = None
    if master is None:
        _warn("  hair MI base material missing; skeletal usage could not be checked")
        return False
    try:
        changed = False
        if not bool(master.get_editor_property("used_with_skeletal_mesh")):
            master.set_editor_property("used_with_skeletal_mesh", True)
            changed = True
        try:
            if not bool(master.get_editor_property("used_with_nanite")):
                master.set_editor_property("used_with_nanite", True)
                changed = True
        except Exception:
            pass
        if not changed:
            return False
        compile_errors = unreal.MaterialEditingLibrary.recompile_material(master) or []
        for error in compile_errors:
            _warn(f"  hair master skeletal shader compile: {error}")
        master_path = master.get_path_name().split(".")[0]
        unreal.EditorAssetLibrary.save_asset(master_path, only_if_is_dirty=False)
        _log(f"  enabled hair master skeletal/Nanite usage: {master_path}")
        return True
    except Exception as exc:
        _warn(f"  failed to enable hair master skeletal mesh usage: {exc}")
        return False


def _source_asset_names(data: dict):
    cleanup = data.get("cleanup")
    if isinstance(cleanup, dict):
        material_names = _unique_string_list(cleanup.get("source_material_names", []))
        texture_names = _unique_string_list(cleanup.get("source_texture_names", []))
        if material_names or texture_names:
            return material_names, texture_names

    material_names = []
    texture_names = []
    seen_material_names = set()
    seen_texture_names = set()

    def append_unique(target: list, seen: set, name: str):
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            target.append(name)

    def add_material_names(name: str):
        append_unique(material_names, seen_material_names, name)
        base_name = _material_instance_base_name(name)
        append_unique(material_names, seen_material_names, base_name)
        for prefix in ("LayerBlend_", "Prop_", "Coat_"):
            if base_name.startswith(prefix):
                append_unique(material_names, seen_material_names, base_name[len(prefix):])

    def add_texture_names(tex: dict):
        file_path = str(tex.get("file", ""))
        if file_path:
            source_name = os.path.splitext(os.path.basename(file_path))[0]
            append_unique(texture_names, seen_texture_names, source_name)
        else:
            asset_name = str(tex.get("asset_name", ""))
            append_unique(texture_names, seen_texture_names, asset_name)

    for entry in data.get("materials", []):
        add_material_names(str(entry.get("name", "")))
        add_material_names(str(entry.get("slot_name", "")))
        for tex in entry.get("textures", []):
            add_texture_names(tex)
        for layer in entry.get("layers", []):
            for tex in layer.get("textures", []):
                add_texture_names(tex)
    return material_names, texture_names


def _unique_string_list(values) -> list:
    unique = []
    seen = set()
    if not isinstance(values, list):
        return unique
    for value in values:
        name = str(value or "")
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def _asset_paths_by_name(folder_path: str) -> dict:
    assets = unreal.EditorAssetLibrary.list_assets(folder_path, recursive=False, include_folder=False)
    paths_by_name = {}
    for asset_path in (str(path) for path in assets):
        package_path = asset_path.split(".", 1)[0]
        asset_name = package_path.rsplit("/", 1)[-1]
        paths_by_name[asset_name.casefold()] = package_path
    return paths_by_name


def _delete_asset_if_type(asset_path: str, allowed_class_names: set) -> bool:
    if not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        return False
    asset = unreal.load_asset(asset_path)
    if asset is None:
        return False
    class_name = asset.get_class().get_name()
    if class_name not in allowed_class_names:
        return False
    if unreal.EditorAssetLibrary.delete_asset(asset_path):
        return True
    _warn(f"  cleanup delete failed: {asset_path}")
    return False


def _cleanup_imported_source_assets(mesh_path: str, data: dict):
    started = time.perf_counter()
    mesh_folder = mesh_path.rsplit("/", 1)[0]
    material_names, texture_names = _source_asset_names(data)
    existing_asset_paths = _asset_paths_by_name(mesh_folder)

    deleted = 0
    skipped_materials = 0
    skipped_textures = 0
    if DELETE_IMPORTED_SOURCE_MATERIALS:
        for name in material_names:
            asset_path = existing_asset_paths.get(name.casefold())
            if not asset_path or asset_path == mesh_path:
                continue
            if _delete_asset_if_type(asset_path, {"Material", "MaterialInstanceConstant"}):
                deleted += 1
    else:
        skipped_materials = sum(
            1
            for name in material_names
            if (asset_path := existing_asset_paths.get(name.casefold())) and asset_path != mesh_path
        )

    if DELETE_IMPORTED_SOURCE_TEXTURES:
        for name in texture_names:
            asset_path = existing_asset_paths.get(name.casefold())
            if not asset_path or asset_path == mesh_path:
                continue
            if _delete_asset_if_type(asset_path, {"Texture2D"}):
                deleted += 1
    else:
        skipped_textures = sum(
            1
            for name in texture_names
            if (asset_path := existing_asset_paths.get(name.casefold())) and asset_path != mesh_path
        )

    elapsed = time.perf_counter() - started
    if deleted or skipped_materials or skipped_textures:
        # Source asset deletes trigger package deletion, source-control work, and shader churn.
        # Keep them available for manual maintenance, but do not run them during export by default.
        material_note = (
            f"; {skipped_materials} source material asset(s) left in place"
            if skipped_materials
            else ""
        )
        texture_note = (
            f"; {skipped_textures} source texture asset(s) left in place"
            if skipped_textures
            else ""
        )
        _log(
            f"  cleanup complete: {deleted} source asset(s) removed from {mesh_folder}"
            f"{material_note}{texture_note} ({elapsed:.2f}s)"
        )
    else:
        _log(f"  cleanup: no source material/texture assets found in {mesh_folder} ({elapsed:.2f}s)")


_CHECKED_OUT_MATERIAL_PIPELINE_ASSETS = set()


def _material_asset_needs_checkout(asset_path: str) -> bool:
    source_control = getattr(unreal, "SourceControl", None)
    if source_control is None:
        return True
    try:
        state = source_control.query_file_state(asset_path, True, False)
    except TypeError:
        state = source_control.query_file_state(asset_path)
    if _source_control_flag(state, "is_checked_out_other"):
        raise RuntimeError(
            f"material pipeline asset is checked out by another user: {asset_path}"
        )
    return not (
        _source_control_flag(state, "is_checked_out")
        or _source_control_flag(state, "is_added")
    )


def _material_pipeline_mutation_paths(mesh_path: str, data: dict) -> list:
    """Resolve existing packages the current material contract may mutate."""
    paths = [mesh_path]
    for entry in data.get("materials", []):
        mat_name = str(entry.get("name", ""))
        if entry.get("translucent"):
            continue
        preset = _master_preset(data, entry, mesh_path)
        profile_paths = _instance_profile_material_paths(entry, preset)
        if (
            profile_paths
            and unreal.EditorAssetLibrary.does_asset_exist(
                profile_paths["target_path"]
            )
        ):
            continue
        target_path = _entry_target_material_path(entry)
        if preset.get("key") == "hair":
            target_path = target_path or _hair_target_material_path(
                mat_name,
                entry,
                preset,
            )
        if not target_path:
            mat_base = _material_instance_base_name(mat_name)
            mi_folder = str(preset.get("mi_folder") or "").rstrip("/")
            if mat_base and mi_folder:
                target_path = f"{mi_folder}/MI_{mat_base}"
        if target_path and _entry_reuses_material_instance_unchanged(entry, preset):
            target_exists = unreal.EditorAssetLibrary.does_asset_exist(target_path)
            target_asset = unreal.load_asset(target_path) if target_exists else None
            needs_empty_layer_repair = _material_instance_has_empty_background_layer(
                target_asset,
                entry,
                preset,
            )
            if not needs_empty_layer_repair and (
                target_exists or not _entry_create_if_missing(entry, preset)
            ):
                continue

        master_path = str(preset.get("master") or "").split(".")[0]
        if (
            preset.get("key") == "hair"
            and target_path
            and not _entry_reparents_existing_hair_material(entry)
        ):
            existing_hair = _load_exact_material_instance(target_path)
            if existing_hair is not None:
                # Skeletal/Nanite usage repair acts on the resolved base master,
                # which may be the independently authored short-fur material.
                master_path = _asset_base_material_path(existing_hair).split(".")[0]
        if master_path:
            paths.append(master_path)
        parent_layer = _layer_parent_path(preset, entry)
        if parent_layer:
            paths.append(parent_layer)
        if target_path:
            paths.append(target_path)

        if preset.get("assignment") == "material_layer_instance":
            layer_path = _layer_instance_path(
                _material_instance_base_name(mat_name),
                preset,
                entry,
            )
            if layer_path:
                paths.append(layer_path)

    paths = list(dict.fromkeys(path.split(".")[0] for path in paths if path))
    if _is_codex_test_asset_path(mesh_path):
        paths = [path for path in paths if _is_codex_test_asset_path(path)]
    return paths


def _checkout_material_pipeline_assets(mesh_path: str, data: dict) -> list:
    existing = []
    for path in _material_pipeline_mutation_paths(mesh_path, data):
        if (
            path in _CHECKED_OUT_MATERIAL_PIPELINE_ASSETS
            or not unreal.EditorAssetLibrary.does_asset_exist(path)
        ):
            continue
        if _material_asset_needs_checkout(path):
            existing.append(path)
    if not existing:
        return []
    subsystem = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
    failed = [path for path in existing if not subsystem.checkout_asset(path)]
    if failed:
        raise RuntimeError(
            "material pipeline source-control checkout failed: " + ", ".join(failed)
        )
    _CHECKED_OUT_MATERIAL_PIPELINE_ASSETS.update(existing)
    _log(f"  source-control checkout: {len(existing)} material pipeline asset(s)")
    return existing


def preflight_mesh_materials(
    mesh_path: str,
    json_path: str = None,
    expected_mesh_name: str = "",
    sidecar_sha256: str = "",
    export_file_path: str = "",
) -> bool:
    """Normalize shared material-layer assets before Unreal touches an existing mesh.

    A skeletal-mesh reimport recompiles its currently assigned material instances
    during ImportAssetTasks.  Legacy SpeedTree layers therefore have to be repaired
    before the FBX import, not from post_import after it.
    """
    mesh_path = mesh_path.split(".")[0]
    mesh_name = str(expected_mesh_name or mesh_path.rsplit("/", 1)[-1]).strip()
    data = _load_json(
        mesh_name,
        json_path,
        mesh_path,
        expected_sha256=sidecar_sha256,
    )
    if not data:
        return False
    _validate_speedtree_handoff_contract(
        data,
        mesh_name,
        mesh_path,
        export_file_path=export_file_path,
    )
    _validate_codex_test_material_scope(data, mesh_path)

    instance_profile_targets = _validate_instance_profile_targets(
        data, mesh_path
    )
    _checkout_material_pipeline_assets(mesh_path, data)
    _ensure_instance_profile_targets(
        unreal.AssetToolsHelpers.get_asset_tools(),
        instance_profile_targets,
    )

    mutable_layer_entries = []
    for entry in data.get("materials", []):
        preset = _master_preset(data, entry, mesh_path)
        if preset.get("assignment") != "material_layer_instance":
            continue
        mat_name = str(entry.get("name", ""))
        profile_paths = _instance_profile_material_paths(entry, preset)
        if (
            profile_paths
            and unreal.EditorAssetLibrary.does_asset_exist(
                profile_paths["target_path"]
            )
        ):
            continue
        target_path = _entry_target_material_path(entry)
        if preset.get("key") == "hair":
            target_path = target_path or _hair_target_material_path(
                mat_name,
                entry,
                preset,
            )
        if not target_path:
            mat_base = _material_instance_base_name(mat_name)
            mi_folder = str(preset.get("mi_folder") or "").rstrip("/")
            if mat_base and mi_folder:
                target_path = f"{mi_folder}/MI_{mat_base}"
        target_exists = bool(
            target_path
            and unreal.EditorAssetLibrary.does_asset_exist(target_path)
        )
        target_asset = unreal.load_asset(target_path) if target_exists else None
        needs_empty_layer_repair = _material_instance_has_empty_background_layer(
            target_asset,
            entry,
            preset,
        )
        if (
            target_path
            and _entry_reuses_material_instance_unchanged(entry, preset)
            and not needs_empty_layer_repair
            and (
                target_exists
                or not _entry_create_if_missing(entry, preset)
            )
        ):
            continue
        mutable_layer_entries.append((entry, preset))

    if not mutable_layer_entries:
        return False

    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    if helper is None:
        raise RuntimeError("CodexMaterialTools material preflight helper missing")

    normalized = False
    for entry, preset in mutable_layer_entries:
        parent_layer = _layer_parent_path(preset, entry)
        _normalize_material_layer_dependencies(
            helper,
            preset,
            parent_layer,
            mutation_scope_path=mesh_path,
        )
        normalized = True
    return normalized


def _project_asset_package_file_exists(asset_path: str) -> bool:
    """Return whether a /Game asset package has reached the project Content folder."""
    package_path = str(asset_path or "").split(".")[0]
    if not package_path.startswith("/Game/"):
        return False
    try:
        content_dir = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_content_dir()
        )
    except Exception:
        return False
    relative_path = package_path[len("/Game/") :].replace("/", os.sep)
    return os.path.isfile(os.path.join(content_dir, relative_path + ".uasset"))


def _save_generated_skeleton_dependency(mesh, mesh_path: str, helper) -> bool:
    """Persist the default Skeleton created by a Send2UE skeletal-mesh import.

    ImportAssetTasks can leave the generated ``<mesh>_Skeleton`` package only in
    memory.  Saving the mesh alone then serializes a dependency that disappears
    after an editor restart.  Existing or explicitly shared Skeleton assets are
    intentionally left untouched.
    """
    try:
        skeleton = mesh.get_editor_property("skeleton")
    except Exception:
        skeleton = None
    if skeleton is None:
        return False

    get_path_name = getattr(skeleton, "get_path_name", None)
    skeleton_path = (
        str(get_path_name()).split(".")[0] if callable(get_path_name) else ""
    )
    expected_path = f"{str(mesh_path).split('.')[0]}_Skeleton"
    if skeleton_path != expected_path:
        return False
    if _project_asset_package_file_exists(skeleton_path):
        return False
    if not helper.save_asset_package_without_thumbnail(skeleton):
        raise RuntimeError(f"generated Skeleton save failed: {skeleton_path}")
    _log(f"  saved generated Skeleton dependency: {skeleton_path}")
    return True


def process_mesh(
    mesh_path: str,
    master_mat=None,
    json_path: str = None,
    expected_mesh_name: str = "",
    sidecar_sha256: str = "",
    export_file_path: str = "",
) -> bool:
    """단일 StaticMesh/SkeletalMesh 를 JSON 기반으로 처리. 변경이 있었으면 True.

    json_path: send2ue extension 이 넘겨주는 JSON 절대경로(있으면 OneDrive walk 생략).
    """
    mesh_path = mesh_path.split(".")[0]
    mesh = unreal.load_asset(mesh_path)
    if not isinstance(mesh, _supported_mesh_classes()):
        return False

    mesh_name = str(expected_mesh_name or mesh_path.rsplit("/", 1)[-1]).strip()
    data = _load_json(
        mesh_name,
        json_path,
        mesh_path,
        expected_sha256=sidecar_sha256,
    )
    asset_tools = None
    if data:
        _validate_speedtree_handoff_contract(
            data,
            mesh_name,
            mesh_path,
            export_file_path=export_file_path,
        )
        _validate_codex_test_material_scope(data, mesh_path)
        instance_profile_targets = _validate_instance_profile_targets(
            data, mesh_path
        )
        _checkout_material_pipeline_assets(mesh_path, data)
        asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
        _ensure_instance_profile_targets(
            asset_tools,
            instance_profile_targets,
        )
    else:
        instance_profile_targets = {}

    def save_mesh_asset():
        if _is_skeletal_mesh(mesh):
            helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
            if not helper or not hasattr(helper, "save_asset_package_without_thumbnail"):
                raise RuntimeError(
                    "CodexMaterialTools safe skeletal-mesh save helper is missing"
                )
            _save_generated_skeleton_dependency(mesh, mesh_path, helper)
            if not helper.save_asset_package_without_thumbnail(mesh):
                raise RuntimeError(f"safe skeletal-mesh save failed: {mesh_path}")
            return
        if not unreal.EditorAssetLibrary.save_asset(mesh_path):
            raise RuntimeError(f"static-mesh save failed: {mesh_path}")

    # Nanite: import 되는 StaticMesh 에 켜되, 반투명 머티리얼 메쉬는 끈다.
    # (JSON 이 없으면 불투명으로 가정 → 켬. 반투명으로 판정되면 이미 켜져 있어도 끈다.)
    if ENABLE_NANITE:
        nanite_enabled = not _is_translucent(data)
        if isinstance(mesh, unreal.StaticMesh) and _set_nanite(mesh, nanite_enabled):
            save_mesh_asset()
        elif _is_skeletal_mesh(mesh):
            if _set_skeletal_material_nanite(mesh, data, mesh_path):
                save_mesh_asset()

    if data is None:
        _warn(f"JSON 사이드카 없음: {mesh_name}.json — skip (블렌더에서 Rename 버튼을 눌렀나요?)")
        return False

    changed = False
    if _persist_prototype_metadata(
        mesh,
        data,
        sidecar_sha256,
        export_file_path=export_file_path,
    ):
        changed = True
    json_mesh_name = str(data.get("mesh_name", ""))
    if json_mesh_name and json_mesh_name != mesh_name:
        _warn(f"JSON mesh_name mismatch: asset={mesh_name}, json={json_mesh_name}; using JSON data")
    if _import_dynamic_wind_if_available(mesh, mesh_path, mesh_name, data, json_path):
        save_mesh_asset()
        changed = True

    # 검증된 텍스처 fingerprint 캐시(메쉬마다 reload하고, 검증된 entry만 끝에 저장).
    tex_cache = _load_texture_cache()
    tex_cache_before = dict(tex_cache)
    skeletal_slot_assignments = {}

    for entry_index, entry in enumerate(data.get("materials", [])):
        mat_name = str(entry.get("name", ""))
        slot_name = _entry_slot_display_name(entry, mat_name)
        target_material_path = _entry_target_material_path(entry)
        legacy_generated_mi_flow = mat_name.startswith("M_")
        if not target_material_path and not legacy_generated_mi_flow:
            continue

        # 실제 슬롯을 머티리얼 이름으로 매칭(Empty 결합 export 대응). 없으면 JSON slot_index 로 폴백.
        slot_index = _slot_index_for_entry(mesh, entry, mat_name)
        if slot_index is None:
            if entry.get("slot_match_required"):
                _warn(f"  slot not found for target-only material '{mat_name}' -> skip")
                continue
            slot_index = int(entry.get("slot_index", 0))

        # Explicit artist-owned assignments can reference a base Material as
        # well as an MI. Never reparent, initialize or synchronize either.
        if (target_material_path and
                entry.get('material_instance_ownership') == 'external' and
                entry.get('manage_existing_material_instance') is False and
                entry.get('create_if_missing') is False):
            assigned = unreal.load_asset(target_material_path)
            if assigned is None or not isinstance(assigned, unreal.MaterialInterface):
                raise RuntimeError('Existing material interface missing: ' + target_material_path)
            if _is_skeletal_mesh(mesh):
                skeletal_slot_assignments[slot_index] = (slot_name, assigned)
            if _assign_slot(mesh, slot_index, assigned, slot_name):
                changed = True
            _log(f'  existing material retained in slot[{slot_index}]: {target_material_path}')
            continue

        preset = _master_preset(data, entry, mesh_path)
        profile_target = instance_profile_targets.get(entry_index)
        if (
            profile_target
            and profile_target.get("target_existed")
            and profile_target.get("asset") is not None
        ):
            _sync_entry_texture_assets(entry, preset, tex_cache)
            assigned_mi = profile_target["asset"]
            _log(
                f"  existing profile '{profile_target['profile']}' -> "
                f"{profile_target['target_path']} "
                "(texture assets synchronized; MI reused unchanged)"
            )
            if _is_skeletal_mesh(mesh):
                skeletal_slot_assignments[slot_index] = (slot_name, assigned_mi)
            if _assign_slot(mesh, slot_index, assigned_mi, slot_name):
                changed = True
            continue

        if preset.get("key") == "hair":
            reuse_unchanged = _entry_reuses_material_instance_unchanged(
                entry,
                preset,
            )
            mi, mi_path, mi_created = _load_or_create_hair_material(
                asset_tools,
                mat_name,
                entry,
                preset,
                preserve_existing=reuse_unchanged,
            )
            if mi is None:
                continue
            reuse_unchanged = bool(reuse_unchanged and not mi_created)
            if _is_skeletal_mesh(mesh) and not reuse_unchanged:
                _ensure_hair_master_skeletal_mesh_usage(mi)
            layer_maps = _sync_entry_texture_assets(entry, preset, tex_cache)
            if not reuse_unchanged:
                if _assign_hair_tool_parameters(
                    mi,
                    entry,
                    layer_maps,
                    initialize_instance_owned_parameters=mi_created,
                    clear_missing_managed=mi_created,
                ):
                    _save_material_texture_update(mi_path)
            _log(f"  hair slot[{slot_index}] '{mat_name}' -> {mi_path}")
            if _is_skeletal_mesh(mesh):
                skeletal_slot_assignments[slot_index] = (slot_name, mi)
            if _assign_slot(mesh, slot_index, mi, slot_name):
                changed = True
            continue

        # 반투명(유리): 전용 MI/텍스처 없이 공유 글래스 MI 를 슬롯에 바로 할당.
        if entry.get("translucent"):
            glass_mi = unreal.load_asset(GLASS_MI_PATH)
            if glass_mi is None:
                _warn(f"  글래스 MI 없음: {GLASS_MI_PATH} — 슬롯[{slot_index}] skip")
                continue
            if _is_skeletal_mesh(mesh):
                skeletal_slot_assignments[slot_index] = (slot_name, glass_mi)
            if _assign_slot(mesh, slot_index, glass_mi, slot_name):
                _log(f"  슬롯[{slot_index}] '{mat_name}' → 글래스 MI 할당(공유)")
                changed = True
            continue

        if target_material_path:
            copy_from_path = _entry_copy_source_material_path(entry)
            existing_mi = _load_exact_material_instance(target_material_path)
            initialize_empty_layer = _material_instance_has_empty_background_layer(
                existing_mi,
                entry,
                preset,
            )
            reuse_unchanged = bool(
                existing_mi is not None
                and _entry_reuses_material_instance_unchanged(entry, preset)
                and not initialize_empty_layer
            )
            if reuse_unchanged:
                mi = existing_mi
                mi_path = target_material_path
                mi_created = False
                mi_source = "existing_assignment_only"
                selected_master = None
                _log(f"  existing target MI reused unchanged: {mi_path}")
            else:
                selected_master = master_mat or _load_master_material(preset)
                if (
                    existing_mi is None
                    and selected_master is None
                    and not copy_from_path
                ):
                    continue
                mi, mi_path, mi_created, mi_source = _load_or_copy_target_material(
                    asset_tools,
                    target_material_path,
                    copy_from_path,
                    selected_master,
                    create_if_missing=_entry_create_if_missing(entry, preset),
                )
            if mi is None:
                continue
            layer_maps = _sync_entry_texture_assets(entry, preset, tex_cache)
            parent_changed = False
            if selected_master is not None and not reuse_unchanged:
                try:
                    current_parent = mi.get_editor_property("parent")
                except Exception:
                    current_parent = None
                if not _same_asset(current_parent, selected_master):
                    unreal.MaterialEditingLibrary.set_material_instance_parent(mi, selected_master)
                    parent_changed = True
                    _log(f"  MI parent update: {mi_path} -> {selected_master.get_path_name()}")
            _log(
                f"  slot[{slot_index}] '{mat_name}' -> {mi_path} "
                f"(master: {preset['key']})"
            )
            params_changed = False
            if not reuse_unchanged:
                mat_base = _material_instance_base_name(mat_name)
                params_changed = _assign_master_textures(
                    mi,
                    layer_maps,
                    preset["assignment"],
                    preset=preset,
                    entry=entry,
                    mat_base=mat_base,
                    clear_missing_managed=True,
                )
            if (mi_created or parent_changed or params_changed) and preset["assignment"] != "material_layer_instance":
                _save_material_texture_update(mi_path)
                changed = True
            elif mi_created or parent_changed or params_changed:
                changed = True
            if _is_skeletal_mesh(mesh):
                skeletal_slot_assignments[slot_index] = (slot_name, mi)
            if _assign_slot(mesh, slot_index, mi, slot_name):
                changed = True
            continue

        mat_base = _material_instance_base_name(mat_name)
        manage_existing = _entry_manages_existing_material_instance(entry)
        generated_mi_path = (
            f"{str(preset['mi_folder']).rstrip('/')}/MI_{mat_base}"
        )
        existing_unchanged = None
        if not manage_existing:
            existing_unchanged = _load_exact_material_instance(generated_mi_path)
        initialize_empty_layer = _material_instance_has_empty_background_layer(
            existing_unchanged,
            entry,
            preset,
        )
        if initialize_empty_layer:
            _log(
                f"  existing MI has an empty background layer; initializing: "
                f"{generated_mi_path}"
            )
        selected_master = (
            None
            if existing_unchanged is not None and not initialize_empty_layer
            else master_mat or _load_master_material(preset)
        )

        _log(
            f"  슬롯[{slot_index}] '{mat_name}' 처리 "
            f"(base: {mat_base}, master: {preset['key']})"
        )

        # 1. MI 생성/로드
        if existing_unchanged is not None and not initialize_empty_layer:
            mi = existing_unchanged
            mi_path = generated_mi_path
            mi_created = False
            parent_changed = False
            mi_source = "existing"
        else:
            mi, mi_path, mi_created, parent_changed, mi_source = _create_or_load_mi(
                asset_tools,
                selected_master,
                mat_base,
                preset["mi_folder"],
                manage_existing=(manage_existing or initialize_empty_layer),
                create_if_missing=_entry_create_if_missing(entry, preset),
            )
        if mi is None:
            profile_target = instance_profile_targets.get(entry_index)
            if profile_target and profile_target.get("asset") is not None:
                assigned_mi = profile_target["asset"]
                _log(
                    f"  user-managed profile '{profile_target['profile']}' -> "
                    f"{profile_target['target_path']} (base unavailable; reused unchanged)"
                )
                if _is_skeletal_mesh(mesh):
                    skeletal_slot_assignments[slot_index] = (slot_name, assigned_mi)
                if _assign_slot(mesh, slot_index, assigned_mi, slot_name):
                    changed = True
            continue

        # 2. Texture2D content always follows the sidecar, even when an exact MI
        # remains assignment-only. Parameter/parent mutation still requires a
        # newly created/copied MI or explicit pipeline ownership.
        layer_maps = _sync_entry_texture_assets(entry, preset, tex_cache)
        reuse_unchanged = bool(
            mi_source == "existing"
            and not manage_existing
            and not initialize_empty_layer
        )
        params_changed = False
        if reuse_unchanged:
            _log(
                f"  existing MI reused unchanged after texture asset sync: {mi_path}"
            )
        else:
            params_changed = _assign_master_textures(
                mi,
                layer_maps,
                preset["assignment"],
                preset=preset,
                entry=entry,
                mat_base=mat_base,
                clear_missing_managed=True,
            )
        if mi_created:
            _save_and_mark_new_material_asset(mi_path)
            changed = True
        elif (parent_changed or params_changed) and preset["assignment"] != "material_layer_instance":
            _save_material_texture_update(mi_path)
            changed = True
        elif parent_changed or params_changed:
            changed = True

        # 3. Keep the base MI/MYI pipeline-managed. A profile target is
        # user-owned and assignment-only: do not save, reparent, or edit it.
        assigned_mi = mi
        profile_target = instance_profile_targets.get(entry_index)
        if profile_target:
            assigned_mi = profile_target["asset"]
            _log(
                f"  user-managed profile '{profile_target['profile']}' -> "
                f"{profile_target['target_path']} (reused without mutation)"
            )
        if _is_skeletal_mesh(mesh):
            skeletal_slot_assignments[slot_index] = (slot_name, assigned_mi)
        if _assign_slot(mesh, slot_index, assigned_mi, slot_name):
            changed = True

    if _sync_fbx_duplicate_material_slots(mesh, data):
        changed = True

    # 이번 처리에서 새로 import 된 텍스처가 있으면 캐시 갱신
    if _normalize_skeletal_material_slots(mesh, skeletal_slot_assignments):
        _log(f"  skeletal material slots normalized to JSON: {len(skeletal_slot_assignments)} slot(s)")
        changed = True

    if tex_cache != tex_cache_before:
        _save_texture_cache(tex_cache)

    _cleanup_imported_source_assets(mesh_path, data)

    if changed:
        save_mesh_asset()
        _log(f"메쉬 '{mesh_name}' 완료")

    # 마지막으로 브라우저를 메쉬로 돌려 선택 상태로 끝낸다(텍스처 폴더로 튀는 것 방지).
    if not changed and _is_skeletal_mesh(mesh):
        # A no-op reimport can still create the default Skeleton package only in
        # memory. Persist that dependency even when materials and Nanite settings
        # already match, then resave the mesh so the reference survives restart.
        helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
        if not helper or not hasattr(helper, "save_asset_package_without_thumbnail"):
            raise RuntimeError(
                "CodexMaterialTools safe skeletal-mesh save helper is missing"
            )
        if _save_generated_skeleton_dependency(mesh, mesh_path, helper):
            if not helper.save_asset_package_without_thumbnail(mesh):
                raise RuntimeError(f"safe skeletal-mesh save failed: {mesh_path}")
            _log(f"  persisted generated Skeleton reference for: {mesh_name}")

    _sync_browser_to_mesh(mesh_path)
    return changed


def persist_generated_skeleton_dependencies(mesh_paths) -> int:
    """Persist generated Skeleton packages after every Send2UE import completes."""
    helper = getattr(unreal, "CodexMaterialToolsLibrary", None)
    if not helper or not hasattr(helper, "save_asset_package_without_thumbnail"):
        raise RuntimeError(
            "CodexMaterialTools safe skeletal-mesh save helper is missing"
        )

    saved_count = 0
    for raw_path in dict.fromkeys(mesh_paths or []):
        mesh_path = str(raw_path or "").split(".")[0]
        mesh = unreal.load_asset(mesh_path)
        if not _is_skeletal_mesh(mesh):
            continue
        if not _save_generated_skeleton_dependency(mesh, mesh_path, helper):
            continue
        if not helper.save_asset_package_without_thumbnail(mesh):
            raise RuntimeError(f"safe skeletal-mesh save failed: {mesh_path}")
        _log(f"  persisted generated Skeleton reference for: {mesh_path}")
        saved_count += 1
    return saved_count


def process_meshes(mesh_paths):
    count = 0
    for p in mesh_paths:
        if process_mesh(p):
            count += 1
    _log(f"일괄 처리 완료: {count} 메쉬 변경")


def run(import_folder: str = None):
    """수동 폴백: 폴더(재귀) 안 모든 StaticMesh/SkeletalMesh 를 처리."""
    import_folder = import_folder or f"{GAME_MESHES_PREFIX.rstrip('/')}/00_common"
    asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()
    mesh_filter = unreal.ARFilter(
        class_names=["StaticMesh", "SkeletalMesh"],
        package_paths=[import_folder],
        recursive_paths=True,
    )
    meshes = asset_registry.get_assets(mesh_filter)
    if not meshes:
        _warn(f"'{import_folder}' 에서 StaticMesh/SkeletalMesh 없음")
        return
    process_meshes([str(m.package_name) for m in meshes])
