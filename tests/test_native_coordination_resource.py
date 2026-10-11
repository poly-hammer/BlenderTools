"""Resource spelling follows queue identity without changing native metadata."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src/addons/send2ue/coordination.py"


class NativeCoordinationResourceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("_native_resource_coordination", SOURCE)
        self.coordination = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.coordination)
        self.bridge = mock.Mock()
        self.bridge.can_execute_handoff.return_value = True
        loader = mock.patch.object(self.coordination, "load_bridge", return_value=self.bridge)
        loader.start()
        self.addCleanup(loader.stop)
        self.namespace = {}
        self.metadata = dict(
            phase_id="same-phase", ticket_id="current-ticket", provider="Codex", session_id="same-owner",
            resource="unreal:myproject2", pipeline=self.coordination.BATCH_PIPELINE,
            request_id="same-native-request", target="d:/source/main.blend",
            project=self.coordination.RESOURCE, action=self.coordination.BATCH_ACTION,
        )

    def begin(self, metadata):
        return self.coordination.begin_operation(
            "same-phase", self.namespace, "D:/source/provider.blend",
            deferred_handoff=json.dumps(metadata),
        )

    def test_canonical_lowercase_resource_accepts_exact_binding_without_rewriting_metadata(self):
        before = dict(self.metadata)
        operation = self.begin(self.metadata)
        self.assertEqual(operation.metadata, before)
        self.assertEqual(self.metadata, before)
        self.bridge.can_execute_handoff.assert_called_once_with(before)
        self.bridge.require_active_phase.assert_called_once_with("same-phase", self.coordination.RESOURCE)
        self.bridge.require_scopes.assert_called_once_with("same-phase", ["source:D:/source/provider.blend"])
        self.bridge.bind_handoff.assert_not_called()
        self.bridge.complete_handoff.assert_not_called()
        self.assertIs(self.namespace[self.coordination.NAMESPACE_KEY], operation)

    def test_legacy_resource_spelling_remains_accepted(self):
        metadata = {**self.metadata, "resource": self.coordination.RESOURCE}
        self.assertEqual(self.begin(metadata).metadata, metadata)
        self.bridge.can_execute_handoff.assert_called_once_with(metadata)

    def test_wrong_or_malformed_resource_rejects_before_native_scope_or_execution(self):
        for value in ("unreal:OtherProject", "blender:myproject2", "unreal:myproject2-extra", " unreal:myproject2",
                      "", None, 1, True):
            with self.subTest(resource=value):
                with self.assertRaisesRegex(RuntimeError, "exact native request"):
                    self.begin({**self.metadata, "resource": value})
                self.assertEqual(self.namespace, {})
                self.bridge.require_active_phase.assert_not_called()
                self.bridge.require_scopes.assert_not_called()
                self.bridge.can_execute_handoff.assert_not_called()
                self.bridge.bind_handoff.assert_not_called()

    def test_resource_case_normalization_keeps_phase_pipeline_action_and_project_exact(self):
        for field, value in (("phase_id", "other-phase"), ("pipeline", "other-pipeline"),
                             ("action", "other-action"), ("project", "unreal:myproject2"),
                             ("owner_token", "forbidden")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, "exact native request"):
                    self.begin({**self.metadata, field: value})
                self.assertEqual(self.namespace, {})
                self.bridge.require_active_phase.assert_not_called()
                self.bridge.can_execute_handoff.assert_not_called()

    def test_inactive_exact_binding_cannot_start_native_export(self):
        self.bridge.can_execute_handoff.return_value = False
        with self.assertRaisesRegex(RuntimeError, "no executable exact binding"):
            self.begin(self.metadata)
        self.bridge.can_execute_handoff.assert_called_once_with(self.metadata)
        self.bridge.require_scopes.assert_not_called()
        self.assertEqual(self.namespace, {})


if __name__ == "__main__":
    unittest.main()
