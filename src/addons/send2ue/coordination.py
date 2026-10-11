"""Optional receipts for an owner-admitted native Unreal operation.

No phase means the existing manual pipeline. This module never admits a phase,
starts a chat, takes another owner's scope, or stores an owner token in Blender.
"""
import importlib.util
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sys
import time
import uuid

NAMESPACE_KEY = "send2ue_workstation_operation"
DEFERRED_RECEIPT_KEY = "send2ue_workstation_deferred_export_receipt"
RESOURCE = "unreal:MyProject2"
BATCH_PIPELINE = "speedtree_batch_send2ue"
BATCH_ACTION = "send2ue_manifest_export_rpc_ingest"
HANDOFF_FIELDS = ("phase_id", "ticket_id", "provider", "session_id", "resource",
                  "pipeline", "request_id", "target")
_BRIDGE = None


def load_bridge():
    global _BRIDGE
    root = Path(os.environ.get("WORKSTATION_QUEUE_REPO") or
                Path.home() / "Documents" / "GitHub" / "workstation-queue").resolve()
    path = root / "pipeline_bridge.py"
    if not path.is_file():
        raise RuntimeError("Workstation Queue pipeline bridge is unavailable: " + str(path))
    for name in ("queue_store", "work_phases", "wq_paths"):
        existing = sys.modules.get(name)
        if existing is not None and Path(getattr(existing, "__file__", "")).resolve() != root / (name + ".py"):
            raise RuntimeError("Workstation Queue module belongs to another installation: " + name)
    if _BRIDGE is not None and Path(_BRIDGE.__file__).resolve() == path:
        return _BRIDGE
    spec = importlib.util.spec_from_file_location("_send2ue_workstation_bridge", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(root))
    _BRIDGE = module
    return module


def current_operation(namespace=None):
    if namespace is None:
        import bpy
        namespace = bpy.app.driver_namespace
    return namespace.get(NAMESPACE_KEY)


class NativeOperation:
    def __init__(self, bridge, metadata, namespace, deferred=False, source=""):
        self.bridge = bridge
        self.metadata = metadata
        self.namespace = namespace
        self.last_heartbeat = time.monotonic()
        self.closed = False
        self.imported_count = 0
        self.deferred = deferred
        self.source = source
        self.exported_count = 0

    def check(self):
        if self.closed or not self.bridge.can_execute_handoff(self.metadata):
            raise RuntimeError("The coordinated Unreal operation must stop and reconcile its work phase")

    def require_scopes(self, scopes):
        self.check()
        self.bridge.require_scopes(self.metadata["phase_id"], scopes)

    def heartbeat(self):
        if time.monotonic() - self.last_heartbeat < 45:
            return
        if not self.bridge.heartbeat_handoff(self.metadata):
            raise RuntimeError("The coordinated Unreal operation no longer has its owner execution lease")
        self.last_heartbeat = time.monotonic()

    def detach(self):
        self.closed = True
        if self.namespace.get(NAMESPACE_KEY) is self:
            del self.namespace[NAMESPACE_KEY]

    def complete(self):
        # The caller invokes this only after every native job and final save.
        if self.deferred:
            raise RuntimeError("Batch disk export cannot complete the Unreal work phase")
        if not self.bridge.complete_handoff(self.metadata, self.metadata["request_id"], self.metadata["target"]):
            raise RuntimeError("Native completion could not be reconciled with the exact work phase request")
        self.detach()

    def fail(self, note):
        try:
            self.bridge.fail_handoff(self.metadata, str(note)[:2000])
        finally:
            self.detach()

    def finish_export(self, asset_data):
        """Attest this native disk export while retaining its exact phase binding."""
        self.check()
        if not self.deferred or not self.exported_count:
            raise RuntimeError("No native Batch export jobs completed")
        paths = set()
        for data in asset_data.values():
            if data.get("skip"):
                continue
            if not data.get("file_path"):
                raise RuntimeError("Native Batch export has no exact output path")
            paths.add(str(data["file_path"]))
            paths.update(str(path) for path in (data.get("lods") or {}).values())
            if data.get("fcurve_file_path"):
                paths.add(str(data["fcurve_file_path"]))
        if not paths:
            raise RuntimeError("Native Batch export produced no files")
        files = []
        for raw_path in sorted(paths):
            path = Path(raw_path).resolve()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            size = path.stat().st_size
            if not size:
                raise RuntimeError("Native Batch export produced an empty file: " + str(path))
            files.append(dict(path=str(path), bytes=size, sha256=digest.hexdigest()))
        receipt = dict(metadata=dict(self.metadata), source_blend=self.source,
                       exported_files=files, native_export_jobs=self.exported_count,
                       stage="exported_pending_unreal", phase_completed=False)
        self.namespace[DEFERRED_RECEIPT_KEY] = receipt
        self.detach()
        return receipt


def begin_operation(phase_id, namespace, target, pipeline="send2ue", deferred_handoff=""):
    if namespace.get(NAMESPACE_KEY) is not None:
        raise RuntimeError("Another coordinated Unreal operation is already running in this Blender session")
    if not phase_id and not deferred_handoff:
        return None
    bridge = load_bridge()
    if deferred_handoff:
        if not phase_id:
            raise RuntimeError("Deferred Batch handoff requires its admitted phase ID")
        try:
            metadata = json.loads(deferred_handoff)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Deferred Batch handoff metadata is invalid") from error
        if (not isinstance(metadata, dict)
                or any(not isinstance(metadata.get(key), str) or not metadata[key]
                       for key in HANDOFF_FIELDS)
                or metadata["phase_id"] != phase_id
                or metadata["resource"].casefold() != RESOURCE.casefold()
                or metadata["pipeline"] != BATCH_PIPELINE
                or metadata.get("project") != RESOURCE
                or metadata.get("action") != BATCH_ACTION
                or any("token" in key.casefold() for key in metadata)):
            raise RuntimeError("Deferred Batch handoff does not match its exact native request")
        if not target:
            raise RuntimeError("Deferred Batch handoff requires an existing Blender source path")
        # The final target can own multiple provider files. Their actual source
        # scope is checked individually without replacing the existing binding.
        bridge.require_active_phase(phase_id, RESOURCE)
        if not bridge.can_execute_handoff(metadata):
            raise RuntimeError("Deferred Batch handoff has no executable exact binding")
        source = ntpath.normpath(target).replace("\\", "/")
        bridge.require_scopes(phase_id, ["source:" + source])
        namespace.pop(DEFERRED_RECEIPT_KEY, None)
        operation = NativeOperation(bridge, metadata, namespace, deferred=True, source=source)
        namespace[NAMESPACE_KEY] = operation
        return operation
    bridge.require_active_phase(phase_id, RESOURCE)
    metadata = bridge.bind_handoff(phase_id, pipeline, str(uuid.uuid4()), target)
    operation = NativeOperation(bridge, metadata, namespace)
    namespace[NAMESPACE_KEY] = operation
    try:
        operation.check()
    except BaseException as error:
        operation.fail(error)
        raise
    return operation


def validate_export_jobs(operation, jobs, path_mode):
    if not operation.deferred or path_mode != "send_to_disk" or not jobs:
        raise RuntimeError("Deferred Batch handoff requires native disk export jobs")
    export_module = (__package__ or "send2ue") + ".core.export"
    for function, *_ in jobs:
        if (getattr(function, "__module__", "") != export_module
                or getattr(function, "__name__", "") not in
                {"export_mesh", "export_animation", "export_hair"}):
            raise RuntimeError("Deferred Batch handoff cannot run native Unreal import jobs")


def require_asset_scopes(asset_data, properties=None, namespace=None):
    operation = current_operation(namespace)
    if operation is None or asset_data.get("skip"):
        return
    asset_path = str(asset_data.get("asset_path") or "").split(".", 1)[0]
    if not asset_path.startswith("/Game/"):
        raise RuntimeError("The coordinated import has no exact Unreal asset path")
    scopes = ["asset:" + asset_path]
    if asset_data.get('_asset_type') == 'SkeletalMesh':
        # FBX can create a Skeleton/PhysicsAsset beside the mesh. The native
        # importer determines their names; reserve the actual target folder.
        scopes.append("asset:" + asset_path.rsplit('/', 1)[0])
    for field in ("skeleton_asset_path", "physics_asset_path"):
        value = str(asset_data.get(field) or getattr(properties, 'unreal_' + field, '') or "").split(".", 1)[0]
        if value.startswith("/Game/"):
            scopes.append("asset:" + value)
    # Auto materials can modify shared masters and source-matching textures far
    # outside the mesh folder. Do not pretend its sidecar lists every mutation.
    extensions = getattr(properties, "extensions", None)
    materials = getattr(extensions, "material_pipeline", None)
    if (asset_data.get('_ue_groom_adapter') or asset_data.get("_material_pipeline_json_path") or
            (materials is not None and getattr(materials, "enabled", False))):
        scopes = ["editor"]
    operation.require_scopes(scopes)


def require_preparation_scopes(properties, namespace=None):
    operation = current_operation(namespace)
    if operation is None:
        return
    extensions = getattr(properties, "extensions", None)
    materials = getattr(extensions, "material_pipeline", None)
    groom = getattr(extensions, "ue_groom_adapter", None)
    groom_output = (groom is not None and getattr(groom, "enabled", False)
                    and getattr(groom, "output_mode", "CARDS") in {"GROOM", "BOTH"})
    if (materials is not None and getattr(materials, "enabled", False)) or groom_output:
        operation.require_scopes(["editor"])


def validate_import_result(asset_data, result):
    if asset_data.get("skip"):
        return
    expected = str(asset_data.get("asset_path") or "").split(".", 1)[0].casefold()
    paths = result.get("imported_object_paths", []) if isinstance(result, dict) else result
    paths = paths if isinstance(paths, (list, tuple)) else []
    if not expected or expected not in {str(path).split(".", 1)[0].casefold() for path in paths}:
        raise RuntimeError("Unreal import did not produce the expected asset: " + (expected or "<missing path>"))
