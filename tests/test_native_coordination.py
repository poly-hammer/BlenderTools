"""Native coordination contracts without importing/registering a live Blender."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "src/addons/send2ue"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


COORD = load_file("_native_coordination", ADDON / "coordination.py")
PACKAGE = types.ModuleType("_native_unreal_tests")
PACKAGE.__path__ = [str(ADDON / "dependencies")]
sys.modules[PACKAGE.__name__] = PACKAGE
REMOTE = load_file(PACKAGE.__name__ + ".remote_execution", ADDON / "dependencies/remote_execution.py")
UNREAL = load_file(PACKAGE.__name__ + ".unreal", ADDON / "dependencies/unreal.py")


def bridge_fixture():
    bridge = mock.Mock()
    bridge.can_execute_handoff.return_value = True
    bridge.heartbeat_handoff.return_value = True
    bridge.complete_handoff.return_value = True
    bridge.bind_handoff.side_effect = lambda phase, pipeline, request, target: dict(
        phase_id=phase, provider="Codex", session_id="fixture-owner", resource=COORD.RESOURCE,
        pipeline=pipeline, request_id=request, target=target,
    )
    return bridge


class NativeCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.bridge = bridge_fixture()
        self.namespace = {}
        self.loader = mock.patch.object(COORD, "load_bridge", return_value=self.bridge)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def begin(self):
        return COORD.begin_operation("phase", self.namespace, "C:/asset.blend")

    def test_manual_flow_never_loads_bridge(self):
        self.assertIsNone(COORD.begin_operation("", self.namespace, ""))
        COORD.load_bridge.assert_not_called()

    def test_exact_native_binding_and_only_its_completion_detaches(self):
        op = self.begin()
        self.bridge.require_active_phase.assert_called_once_with("phase", "unreal:MyProject2")
        self.assertNotIn("token", op.metadata)
        op.complete()
        self.bridge.complete_handoff.assert_called_once_with(op.metadata, op.metadata["request_id"], "C:/asset.blend")
        self.assertEqual(self.namespace, {})

    def test_inactive_phase_stops_before_binding(self):
        self.bridge.require_active_phase.side_effect = RuntimeError("waiting")
        with self.assertRaisesRegex(RuntimeError, "waiting"):
            self.begin()
        self.bridge.bind_handoff.assert_not_called()
        self.assertEqual(self.namespace, {})

    def test_second_manual_or_coordinated_operation_cannot_replace_running_binding(self):
        op = self.begin()
        for phase in ("phase2", ""):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                COORD.begin_operation(phase, self.namespace, "C:/other.blend")
        self.assertIs(COORD.current_operation(self.namespace), op)

    def test_failure_retains_reservation_through_bridge_and_detaches_local_context(self):
        op = self.begin()
        op.fail(TimeoutError("uncertain editor response"))
        self.bridge.fail_handoff.assert_called_once_with(op.metadata, "uncertain editor response")
        self.bridge.complete_handoff.assert_not_called()
        self.assertEqual(self.namespace, {})

    def test_quiet_heartbeat_is_throttled_and_rejected_owner_stops(self):
        with mock.patch.object(COORD.time, "monotonic", return_value=0):
            op = self.begin()
        with mock.patch.object(COORD.time, "monotonic", return_value=44):
            op.heartbeat()
        self.bridge.heartbeat_handoff.assert_not_called()
        with mock.patch.object(COORD.time, "monotonic", return_value=45):
            op.heartbeat()
        self.bridge.heartbeat_handoff.assert_called_once_with(op.metadata)
        self.bridge.heartbeat_handoff.return_value = False
        with mock.patch.object(COORD.time, "monotonic", return_value=90), self.assertRaisesRegex(RuntimeError, "lease"):
            op.heartbeat()

    def test_import_scope_gate_and_material_global_scope(self):
        self.begin()
        data = {"asset_path": "/Game/Meshes/Prop.Prop", "skeleton_asset_path": "/Game/Rigs/Rig.Rig"}
        COORD.require_asset_scopes(data, namespace=self.namespace)
        self.bridge.require_scopes.assert_called_with("phase", ["asset:/Game/Meshes/Prop", "asset:/Game/Rigs/Rig"])
        data["_material_pipeline_json_path"] = "C:/Prop.json"
        COORD.require_asset_scopes(data, namespace=self.namespace)
        self.bridge.require_scopes.assert_called_with("phase", ["editor"])

    def test_groom_preparation_requires_editor_before_native_hooks(self):
        self.begin()
        properties = types.SimpleNamespace(extensions=types.SimpleNamespace(
            ue_groom_adapter=types.SimpleNamespace(enabled=True, output_mode='BOTH')))
        COORD.require_preparation_scopes(properties, namespace=self.namespace)
        self.bridge.require_scopes.assert_called_once_with('phase', ['editor'])

    def test_empty_wrong_or_previous_import_path_never_counts_as_success(self):
        data = {"asset_path": "/Game/Meshes/Prop"}
        for result in (None, [], ["/Game/Meshes/Old.Old"], {}):
            with self.assertRaisesRegex(RuntimeError, "expected asset"):
                COORD.validate_import_result(data, result)
        COORD.validate_import_result(data, ["/Game/Meshes/Prop.Prop"])

    def test_loader_rejects_colliding_queue_store_before_import(self):
        self.loader.stop()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "pipeline_bridge.py").write_text("raise AssertionError('must not run')", encoding="utf-8")
            foreign = types.ModuleType("queue_store")
            foreign.__file__ = str(Path(directory, "foreign.py"))
            with mock.patch.dict(os.environ, {"WORKSTATION_QUEUE_REPO": directory}), mock.patch.dict(sys.modules, {"queue_store": foreign}), self.assertRaisesRegex(RuntimeError, "another installation"):
                COORD.load_bridge()

    def deferred_metadata(self):
        return dict(phase_id="phase", ticket_id="ticket", provider="Codex",
                    session_id="fixture-owner", resource=COORD.RESOURCE,
                    pipeline=COORD.BATCH_PIPELINE, request_id="same-native-request",
                    target="C:/root.blend", project=COORD.RESOURCE,
                    action=COORD.BATCH_ACTION)

    def test_deferred_provider_reuses_binding_and_export_never_completes_phase(self):
        metadata = self.deferred_metadata()
        op = COORD.begin_operation("phase", self.namespace, "C:/provider.blend",
                                   deferred_handoff=json.dumps(metadata))
        self.bridge.bind_handoff.assert_not_called()
        self.bridge.require_scopes.assert_called_once_with("phase", ["source:C:/provider.blend"])
        with self.assertRaisesRegex(RuntimeError, "cannot complete"):
            op.complete()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "Prop.fbx")
            path.write_bytes(b"native FBX bytes")
            op.exported_count = 1
            receipt = op.finish_export({"id": {"file_path": str(path)}})
        self.assertEqual(receipt["metadata"], metadata)
        self.assertEqual(receipt["source_blend"], "C:/provider.blend")
        self.assertEqual(receipt["stage"], "exported_pending_unreal")
        self.assertFalse(receipt["phase_completed"])
        self.assertEqual(receipt["exported_files"][0]["bytes"], 16)
        self.assertEqual(self.namespace, {COORD.DEFERRED_RECEIPT_KEY: receipt})
        self.bridge.complete_handoff.assert_not_called()

    def test_deferred_metadata_binding_or_outputs_must_be_exact(self):
        metadata = self.deferred_metadata()
        for override in ({"phase_id": "other"}, {"action": "manual"},
                         {"project": "unreal:Other"}, {"owner_token": "forbidden"}):
            with self.assertRaisesRegex(RuntimeError, "exact native"):
                COORD.begin_operation("phase", self.namespace, "C:/provider.blend",
                    deferred_handoff=json.dumps({**metadata, **override}))
        self.bridge.can_execute_handoff.return_value = False
        with self.assertRaisesRegex(RuntimeError, "exact binding"):
            COORD.begin_operation("phase", self.namespace, "C:/provider.blend",
                                  deferred_handoff=json.dumps(metadata))
        self.bridge.can_execute_handoff.return_value = True
        op = COORD.begin_operation("phase", self.namespace, "C:/provider.blend",
                                   deferred_handoff=json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeError, "No native Batch"):
            op.finish_export({})
        op.exported_count = 1
        with self.assertRaises(FileNotFoundError):
            op.finish_export({"id": {"file_path": "does_not_exist.fbx"}})
        self.assertIs(self.namespace[COORD.NAMESPACE_KEY], op)
        self.assertNotIn(COORD.DEFERRED_RECEIPT_KEY, self.namespace)
        self.bridge.complete_handoff.assert_not_called()


class NativeRemoteReceiptTests(unittest.TestCase):
    def execute_commands(self, remote, commands):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec("\n".join(commands), {})
        return output.getvalue()

    def test_coordinated_remote_exception_is_structured_failure_not_printed_success(self):
        operation = mock.Mock()
        bpy = types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={COORD.NAMESPACE_KEY: operation}))
        with mock.patch.dict(sys.modules, {"bpy": bpy}), mock.patch.object(REMOTE, "RemoteExecution"), mock.patch.object(UNREAL, "run_unreal_python_commands", side_effect=self.execute_commands):
            with self.assertRaisesRegex(RuntimeError, "ValueError: native material save failed"):
                UNREAL.run_commands(["raise ValueError('native material save failed')"])
        operation.check.assert_called_once()

    def test_success_requires_exact_marker_and_manual_output_is_preserved(self):
        with mock.patch.object(REMOTE, "RemoteExecution"), mock.patch.object(UNREAL, "run_unreal_python_commands", side_effect=self.execute_commands):
            self.assertIn('"success": true', UNREAL.run_commands(["print('saved')"], strict=True))
            self.assertEqual(UNREAL.run_commands(["print('manual')"]), "manual\n")
        with mock.patch.object(REMOTE, "RemoteExecution"), mock.patch.object(UNREAL, "run_unreal_python_commands", return_value='__WQ_NATIVE_RESULT__{"request_id":"old","success":true}'):
            with self.assertRaisesRegex(RuntimeError, "no matching"):
                UNREAL.run_commands(["pass"], strict=True)

    def test_recorded_cleanup_does_not_dispatch_or_mint_success_receipt(self):
        with mock.patch.object(REMOTE, "RemoteExecution") as factory:
            with UNREAL.record_commands() as recorded:
                self.assertEqual(UNREAL.run_commands(["save_asset()"], strict=True), "")
        self.assertEqual(recorded, [["save_asset()"]])
        factory.assert_not_called()


class NativeOperatorTests(unittest.TestCase):
    def setUp(self):
        self.namespace = {}
        self.bridge = bridge_fixture()
        self.bpy = types.SimpleNamespace(
            types=types.SimpleNamespace(Operator=object, STATUSBAR_HT_header=mock.Mock()),
            props=types.SimpleNamespace(StringProperty=lambda **kwargs: None),
            app=types.SimpleNamespace(driver_namespace=self.namespace), data=types.SimpleNamespace(filepath="C:/asset.blend"),
            context=mock.Mock(),
        )
        self.bpy.context.scene.send2ue.path_mode = "send_to_project"
        self.utilities = mock.Mock()
        self.utilities.is_unreal_connected.return_value = True
        self.export = mock.Mock()
        fixture_package = types.ModuleType('_native_operator_fixture')
        fixture_package.__path__ = []
        fixture_core = types.ModuleType(fixture_package.__name__ + '.core')
        self.export_selection = mock.Mock()
        fixture_core.export_selection = self.export_selection
        tree = ast.parse((ADDON / "operators.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Send2Ue")
        env = dict(__name__=fixture_package.__name__ + '.operators',
                   __package__=fixture_package.__name__,
                   bpy=self.bpy, queue=queue, os=os, unreal=UNREAL, coordination=COORD,
                   utilities=self.utilities, export=self.export,
                   ToolInfo=types.SimpleNamespace(EXECUTION_QUEUE=types.SimpleNamespace(value="jobs")),
                   grouppro_export=mock.Mock(), preview_modifier_guard=mock.Mock(),
                   armature_modifier_fix=mock.Mock(), hair_tool_export=mock.Mock(),
                   extension=mock.Mock(), ExtensionTasks=types.SimpleNamespace(POST_OPERATION=types.SimpleNamespace(value="post")))
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "operators.py", "exec"), env)
        self.op = env["Send2Ue"]()
        self.real_post_operation = self.op.post_operation
        self.extension = env['extension']
        self.op.workstation_phase_id = "phase"
        self.op.report = mock.Mock()
        self.op.pre_operation = mock.Mock()
        self.op.post_operation = mock.Mock()
        self.context = mock.Mock()
        self.patches = [mock.patch.object(COORD, "load_bridge", return_value=self.bridge),
                        mock.patch.dict(sys.modules, {
                            "bpy": self.bpy,
                            fixture_package.__name__: fixture_package,
                            fixture_core.__name__: fixture_core,
                        })]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def enqueue(self, function):
        self.export.send2ue.side_effect = lambda properties: self.op.execution_queue.put((function, (), {}, "{attribute}", "asset", "file_path"))
        self.context.window_manager.send2ue.asset_data = {"asset": {"file_path": "Prop.fbx"}}

    def verified_import(self):
        COORD.current_operation(self.namespace).imported_count += 1

    def test_synchronous_complete_is_after_native_job_and_post_operation(self):
        events = []
        self.enqueue(lambda: (events.append("import"), self.verified_import()))
        self.op.post_operation.side_effect = lambda: events.append("saved dependencies")
        self.bridge.complete_handoff.side_effect = lambda *args: events.append("complete") or True
        self.assertEqual(self.op.execute(self.context), {"FINISHED"})
        self.assertEqual(events, ["import", "saved dependencies", "complete"])

    def test_validation_no_jobs_never_completes(self):
        with self.assertRaisesRegex(RuntimeError, "no native"):
            self.op.execute(self.context)
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()

    def test_queue_without_verified_import_never_completes(self):
        self.enqueue(lambda: None)
        with self.assertRaisesRegex(RuntimeError, "No native Unreal import"):
            self.op.execute(self.context)
        self.bridge.complete_handoff.assert_not_called()

    def test_final_dependency_save_failure_retains_recovery(self):
        self.enqueue(self.verified_import)
        self.op.post_operation.side_effect = RuntimeError("save failed")
        with self.assertRaisesRegex(RuntimeError, "save failed"):
            self.op.execute(self.context)
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()

    def test_inactive_phase_does_not_touch_export_or_connection(self):
        self.bridge.require_active_phase.side_effect = RuntimeError("waiting")
        with self.assertRaisesRegex(RuntimeError, "waiting"):
            self.op.execute(self.context)
        self.op.pre_operation.assert_not_called()
        self.utilities.is_unreal_connected.assert_not_called()

    def test_material_preparation_scope_is_checked_before_any_export_or_connection(self):
        self.bpy.context.scene.send2ue.extensions.material_pipeline.enabled = True
        self.bridge.require_scopes.side_effect = RuntimeError('editor scope missing')
        with self.assertRaisesRegex(RuntimeError, 'editor scope missing'):
            self.op.execute(self.context)
        self.op.pre_operation.assert_not_called()
        self.utilities.is_unreal_connected.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()

    def test_actual_post_operation_restores_blender_context_on_save_failure(self):
        self.extension.run_extension_tasks.side_effect = RuntimeError('dependency save failed')
        with self.assertRaisesRegex(RuntimeError, 'dependency save failed'):
            self.real_post_operation()
        self.utilities.remove_unpacked_files.assert_called_once()
        self.export_selection.cleanup.assert_called_once()
        self.utilities.set_context.assert_called_once()

    def test_modal_cancel_never_completes_after_clearing_queue(self):
        self.enqueue(self.verified_import)
        self.assertEqual(self.op.invoke(self.context, mock.Mock()), {"RUNNING_MODAL"})
        self.op.modal(self.context, types.SimpleNamespace(type="ESC"))
        self.assertEqual(self.op.modal(self.context, types.SimpleNamespace(type="TIMER")), {"FINISHED"})
        self.bridge.fail_handoff.assert_called_once()
        self.bridge.complete_handoff.assert_not_called()

    def test_modal_success_waits_for_post_operation(self):
        self.enqueue(self.verified_import)
        self.op.invoke(self.context, mock.Mock())
        self.op.modal(self.context, types.SimpleNamespace(type="TIMER"))
        self.bridge.complete_handoff.assert_not_called()
        self.op.modal(self.context, types.SimpleNamespace(type="TIMER"))
        self.bridge.complete_handoff.assert_not_called()
        self.assertEqual(self.op.modal(self.context, types.SimpleNamespace(type="TIMER")), {"FINISHED"})
        self.op.post_operation.assert_called_once()
        self.bridge.complete_handoff.assert_called_once()

    def configure_deferred(self):
        metadata = NativeCoordinationTests.deferred_metadata(self)
        self.op.workstation_deferred_handoff = json.dumps(metadata)
        self.bpy.context.scene.send2ue.path_mode = "send_to_disk"
        return metadata

    def test_deferred_native_export_and_cleanup_receipt_wait_for_rpc_ingest(self):
        metadata = self.configure_deferred()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "Prop.fbx")
            def export_mesh():
                path.write_bytes(b"native FBX")
            export_mesh.__module__ = "send2ue.core.export"
            self.enqueue(export_mesh)
            self.bpy.context.window_manager.send2ue.asset_data = {"asset": {"file_path": str(path)}}
            self.assertEqual(self.op.execute(self.context), {"FINISHED"})
        receipt = self.namespace[COORD.DEFERRED_RECEIPT_KEY]
        self.assertEqual(receipt["metadata"], metadata)
        self.assertEqual(receipt["native_export_jobs"], 1)
        self.assertNotIn(COORD.NAMESPACE_KEY, self.namespace)
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.bind_handoff.assert_not_called()

    def test_deferred_export_refuses_queued_imports_before_execution(self):
        self.configure_deferred()
        native_import = mock.Mock()
        self.enqueue(native_import)
        with self.assertRaisesRegex(RuntimeError, "cannot run native Unreal import"):
            self.op.execute(self.context)
        native_import.assert_not_called()
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()

    def test_deferred_file_failure_leaves_recovery_reservation(self):
        self.configure_deferred()
        def export_mesh():
            raise RuntimeError("native FBX write failed")
        export_mesh.__module__ = "send2ue.core.export"
        self.enqueue(export_mesh)
        with self.assertRaisesRegex(RuntimeError, "native FBX write failed"):
            self.op.execute(self.context)
        self.assertNotIn(COORD.DEFERRED_RECEIPT_KEY, self.namespace)
        self.bridge.complete_handoff.assert_not_called()
        self.bridge.fail_handoff.assert_called_once()


class NativeIngestTests(unittest.TestCase):
    def test_ordinary_static_mesh_empty_result_is_rejected_even_without_phase(self):
        data = {'_asset_type': 'StaticMesh', 'asset_path': '/Game/Prop', 'file_path': 'C:/Prop.fbx'}
        bpy = types.SimpleNamespace(
            app=types.SimpleNamespace(driver_namespace={}),
            context=types.SimpleNamespace(window_manager=types.SimpleNamespace(send2ue=types.SimpleNamespace(asset_data={'id': data})), scene=types.SimpleNamespace(send2ue=None)))
        tree = ast.parse((ADDON / 'core/ingest.py').read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'import_asset')
        node.decorator_list = []
        calls = mock.Mock()
        calls.import_asset.return_value = []
        extension = mock.Mock()
        env = dict(bpy=bpy, coordination=COORD, extension=extension, UnrealRemoteCalls=calls,
                   UnrealTypes=types.SimpleNamespace(STATIC_MESH='StaticMesh', SKELETAL_MESH='SkeletalMesh'),
                   ExtensionTasks=types.SimpleNamespace(PRE_IMPORT=types.SimpleNamespace(value='pre'), POST_IMPORT=types.SimpleNamespace(value='post')),
                   _property_data_for_asset=lambda data, props: props)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'ingest.py', 'exec'), env)
        with mock.patch.dict(sys.modules, {'bpy': bpy}), self.assertRaisesRegex(RuntimeError, 'expected asset'):
            env['import_asset']('id', {})
        extension.run_extension_tasks.assert_called_once_with('pre')


if __name__ == "__main__":
    unittest.main()
