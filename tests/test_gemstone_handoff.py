"""Crystal routing and artist-owned controls across repeated material handoffs."""
import unittest
from test_ue_material_setup import FakeRuntime, _load_module

class GemstoneHandoffTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module(FakeRuntime())

    def test_per_slot_override_in_mixed_prop_asset(self):
        preset = self.module._master_preset({'material_master':'prop'}, {'name':'M_Cane_Gemstone_01', 'master_preset':'gemstone'})
        self.assertEqual(preset['key'], 'gemstone')
        self.assertTrue(preset['master'].endswith('/M_Gemstone_Layered_Substrate'))
        self.assertEqual(preset['assignment'], 'material_layer_instance')
        self.assertTrue(preset['layer_parent'].endswith('/ML_Gemstone_Crystal'))

    def test_name_fallback_with_legacy_prop_sidecar(self):
        preset = self.module._master_preset({'material_master':'prop'}, {'name':'M_Cane_Ornament_GEMSTONE.001'})
        self.assertEqual(preset['key'], 'gemstone')

    def test_token_boundary_and_explicit_override(self):
        for name in ('M_Ornament_GemstoneDust', 'M_StoneFloor'):
            self.assertEqual(self.module._master_preset({}, {'name':name})['key'], 'prop')
        self.assertEqual(self.module._master_preset({}, {'name':'M_Cane_Gemstone', 'master_preset':'prop'})['key'], 'prop')

    def test_unreal_appearance_is_retained_on_reexport(self):
        preset = self.module._master_preset({}, {'master_preset':'gemstone'})
        entry = {'name':'M_Cane_Gemstone', 'manage_existing_material_instance':False, 'material_instance_ownership':'artist'}
        self.assertTrue(self.module._entry_reuses_material_instance_unchanged(entry, preset))
        self.assertEqual(self.module._layer_texture_remap(preset, entry), {})
        self.assertFalse(preset['virtual_textures'])

if __name__ == '__main__':
    unittest.main()
