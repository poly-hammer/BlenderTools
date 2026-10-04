"""Run natively: blender --background --factory-startup --python this_file.py."""
import os
import sys
import unittest
from types import SimpleNamespace

try:
    import bpy
except ImportError:
    bpy = None

BLENDER_VERSION = getattr(getattr(bpy, 'app', None), 'version', ())
NATIVE_BLENDER_5 = (isinstance(BLENDER_VERSION, tuple) and len(BLENDER_VERSION) == 3
                    and all(isinstance(part, int) for part in BLENDER_VERSION)
                    and BLENDER_VERSION >= (5, 0, 0))

if NATIVE_BLENDER_5:
    sys.path.insert(0, os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', 'src', 'addons')))
    from send2ue.core import utilities


@unittest.skipUnless(NATIVE_BLENDER_5, 'Needs native Blender 5')
class TestActionSlots(unittest.TestCase):
    def setUp(self):
        self.objects = []
        self.actions = []
        self.armatures = []
        bpy.context.scene.render.fps = 30
        for name in ['ActiveOwner', 'NlaOwner']:
            armature = bpy.data.armatures.new(name + 'Bones')
            owner = bpy.data.objects.new(name, armature)
            bpy.context.scene.collection.objects.link(owner)
            owner.animation_data_create()
            self.objects.append(owner)
            self.armatures.append(armature)
        self.active, self.nla = self.objects
        self.active['Pulse'] = 2.
        self.active.keyframe_insert(data_path='["Pulse"]', frame=1.2)
        self.action = self.active.animation_data.action
        self.actions.append(self.action)
        self.nla.animation_data.action = self.action
        self.nla.animation_data.action_slot = self.action.slots.new('OBJECT', self.nla.name)
        self.nla['Pulse'] = 11.
        self.nla.keyframe_insert(data_path='["Pulse"]', frame=1.2)
        for owner, value in zip(self.objects, [1., 7.]):
            owner.location.x = value
            owner.keyframe_insert(data_path='location', index=0, frame=1.2)
            owner.keyframe_insert(data_path='scale', index=0, frame=1.2)
        slot = self.nla.animation_data.action_slot
        strip = self.nla.animation_data.nla_tracks.new().strips.new(
            self.action.name, 1, self.action)
        strip.action_slot = slot
        self.nla.animation_data.action = None

    def tearDown(self):
        for owner in self.objects:
            bpy.data.objects.remove(owner, do_unlink=True)
        for action in self.actions:
            bpy.data.actions.remove(action)
        for armature in self.armatures:
            bpy.data.armatures.remove(armature)

    def test_active_and_nla_slots_keep_custom_curves_separate(self):
        self.assertEqual(len(self.action.slots), 2)
        for owner, expected in zip(self.objects, [2., 11.]):
            data = utilities.get_custom_property_fcurve_data(self.action.name, owner)
            self.assertEqual(data['Pulse'][0][1], expected)

    def test_ambiguous_unbound_and_empty_actions_are_safe(self):
        unbound = bpy.data.objects.new('Unbound', None)
        self.objects.append(unbound)
        empty = bpy.data.actions.new('Empty')
        self.actions.append(empty)
        self.assertEqual(list(utilities.iter_action_fcurves(self.action, unbound)), [])
        self.assertEqual(list(utilities.iter_action_fcurves(empty)), [])
        self.assertEqual(list(utilities.iter_action_fcurves(None)), [])

    def test_root_scale_removal_preserves_bone_and_other_curves(self):
        bag = self.action.layers[0].strips[0].channelbag(
            self.active.animation_data.action_slot)
        bone_scale = bag.fcurves.new(data_path='pose.bones["Root"].scale', index=0)
        bone_scale.keyframe_points.insert(1., 1.)
        utilities.remove_object_scale_keyframes([self.action])
        paths = [curve.data_path for curve in utilities.iter_action_fcurves(self.action)]
        self.assertNotIn('scale', paths)
        self.assertIn('pose.bones["Root"].scale', paths)
        self.assertEqual(paths.count('location'), 2)
        self.assertEqual(paths.count('["Pulse"]'), 2)

    def test_rounding_visits_both_slots(self):
        utilities.round_keyframes([self.action])
        for curve in utilities.iter_action_fcurves(self.action):
            for point in curve.keyframe_points:
                self.assertEqual(point.co.x, 1.)

    def test_scaling_one_rig_preserves_the_other_slot(self):
        bpy.ops.object.select_all(action='DESELECT')
        self.active.select_set(True)
        bpy.context.view_layer.objects.active = self.active
        utilities.scale_object_actions([self.active], [self.action], 2.)
        values = [next(curve for curve in utilities.iter_action_fcurves(self.action, owner)
                       if curve.data_path == 'location').keyframe_points[0].co.y
                  for owner in self.objects]
        self.assertEqual(values, [200., 7.])

    def test_legacy_curve_collection_still_works(self):
        curves = [object(), object()]
        self.assertEqual(list(utilities.iter_action_fcurves(
            SimpleNamespace(fcurves=curves))), curves)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestActionSlots))
    if not result.wasSuccessful():
        raise SystemExit(1)
