"""Independent safety checks for explicit source-local ribbon paint transfer."""

import copy
import importlib.util
import json
from collections import Counter
from pathlib import Path
import unittest
from unittest.mock import patch


PATH = Path(__file__).parents[1] / 'src/addons/send2ue/resources/pipeline/ue_hair_guide_cloth.py'
SPEC = importlib.util.spec_from_file_location('back_paint_independent_review', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def strip(samples, first_id=0):
    return {
        'sim_positions': [[x, t, 0.] for t in samples for x in (0., 1.)],
        'sim_triangles': [list(tri) for row in range(len(samples) - 1)
                          for tri in ((2*row, 2*row+1, 2*row+3),
                                      (2*row, 2*row+3, 2*row+2))],
        'kinematic_sim_indices': [0, 1],
        'sim_import_vertex_ids_2d': list(range(first_id, first_id + len(samples)*2)),
    }


def map_node(name, values):
    return {
        'name': name,
        'type': 'FChaosClothAssetWeightMapNode',
        'props': {'MeshTarget': 'Simulation', 'MapOverrideType': 'ReplaceAll',
                  'Snapshots': '(ActiveSnapshot=-1)', 'bIsFrozen': 'False',
                  'OutputName': '(StringValue="' + name + '")',
                  'VertexWeights': '(' + ','.join(map(str, values)) + ')'},
    }


class BackRibbonIndependentReviewTests(unittest.TestCase):
    def resample(self, old, new):
        return M._ribbon_paint_correspondence(
            old, new, list(range(len(old['sim_positions']))),
            list(range(len(new['sim_positions']))),
            old['sim_import_vertex_ids_2d'], new['sim_import_vertex_ids_2d'])

    def weight_case(self, policy=None, new_id_offset=0):
        old, new = strip([0., .5, 1.]), strip([0., 1.], new_id_offset)
        count = len(new['sim_positions'])
        key = json.dumps(['Guide', 0], separators=(',', ':'))
        rows = {1: {'source': 'Guide', 'spline': 0, 'key': key, 'weight': .125}}
        graph = {'nodes': [map_node('CodexParentForceKey', [.125]*6),
                           map_node('AuthoredClearance', [.1, .2, .3, .4, .8, .9])]}
        packet = {'parts': [{'gid': 1, 'source_guide': 'Guide',
                            'simulation_mesh': {'effective_mode': 'RIBBON'}}]}
        if policy is not None:
            packet['weight_map_transfer'] = policy
        owner = {'sim_source_indices': new['sim_import_vertex_ids_2d'],
                 'sim_source_identity_unique': [True]*count}
        return graph, {'built_mapping': old}, {'built_mapping': new}, packet, owner, rows

    def updates(self, fixture):
        graph, old, new, packet, owner, rows = fixture
        with patch.object(M, '_guide_identity_rows',
                          return_value=([.125]*len(owner['sim_source_indices']), rows)):
            return M._weight_map_updates(graph, old, new, packet, owner)

    def policy(self, keys=None):
        return {'version': 1, 'policy': 'rooted_ribbon_arclength',
                'guide_keys': [['Guide', 0]] if keys is None else keys}

    def test_root_and_tip_keep_each_authored_rail_value(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        correspondence = self.resample(old, new)
        paint = [.1, .2, .3, .4, .8, .9]
        actual = [paint[a]*(1.-t)+paint[b]*t
                  for a, b, t in (correspondence[i] for i in range(8))]
        self.assertEqual(actual[:2], paint[:2])
        self.assertEqual(actual[-2:], paint[-2:])
        self.assertAlmostEqual(actual[2], .18)
        self.assertAlmostEqual(actual[3], .28)

    def test_changed_root_is_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        for point in new['sim_positions'][:2]:
            point[2] += .001
        with self.assertRaisesRegex(ValueError, 'attachment changed'):
            self.resample(old, new)

    def test_reversed_root_rails_are_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        for index in range(0, len(new['sim_positions']), 2):
            new['sim_positions'][index], new['sim_positions'][index+1] = (
                new['sim_positions'][index+1], new['sim_positions'][index])
        with self.assertRaisesRegex(ValueError, 'rail orientation changed'):
            self.resample(old, new)

    def test_explicit_source_rails_preserve_asymmetric_paint_after_frame_rotation(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        # Rotate the new width axis around the same source centerline by 96deg.
        import math
        angle = math.radians(96.)
        for pair in range(len(new['sim_positions'])//2):
            for rail,offset in enumerate((-.5,.5)):
                new['sim_positions'][2*pair+rail][0] = .5+offset*math.cos(angle)
                new['sim_positions'][2*pair+rail][2] = offset*math.sin(angle)
        with self.assertRaisesRegex(ValueError, 'rail orientation changed'):
            self.resample(old,new)
        corr = M._ribbon_paint_correspondence(old,new,list(range(6)),list(range(8)),
                                             list(range(6)),list(range(8)),
                                             preserve_source_rails=True)
        paint=[.1,.2,.3,.4,.8,.9]
        actual=[paint[a]*(1-t)+paint[b]*t for a,b,t in (corr[i] for i in range(8))]
        self.assertEqual(actual[:2],paint[:2])
        self.assertEqual(actual[-2:],paint[-2:])
        self.assertAlmostEqual(actual[3]-actual[2],.1)

    def test_source_rail_permission_does_not_allow_a_different_tip(self):
        old,new=strip([0.,.5,1.]),strip([0.,1.1])
        with self.assertRaisesRegex(ValueError, 'endpoint changed'):
            M._ribbon_paint_correspondence(old,new,list(range(6)),list(range(4)),
                                         list(range(6)),list(range(4)),preserve_source_rails=True)

    def test_generated_parent_keys_do_not_require_authored_paint_resampling(self):
        fixture=list(self.weight_case(self.policy()))
        fixture[0]['nodes']=fixture[0]['nodes'][:1]
        fixture[1]['built_mapping']['kinematic_sim_indices']=[0,1,4,5]
        fixture[2]['built_mapping']['kinematic_sim_indices']=[2,3]
        actual=self.updates(fixture)
        self.assertEqual(actual['CodexParentForceKey'][2],[.125]*4)

    def test_foreign_guide_triangle_is_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        new['sim_positions'].append([20., 20., 20.])
        new['sim_triangles'].append([0, 1, 8])
        with self.assertRaisesRegex(ValueError, 'triangle support'):
            M._ribbon_paint_correspondence(old, new, list(range(6)), list(range(8)),
                                         list(range(6)), list(range(9)))

    def test_fixed_tip_or_discontinuous_fixed_region_is_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        for fixed in ([0, 1, 6, 7], [0, 1, 4, 5], [0, 1, 2]):
            with self.subTest(fixed=fixed):
                new['kinematic_sim_indices'] = fixed
                with self.assertRaisesRegex(ValueError, 'fixed root and dynamic tip'):
                    self.resample(old, new)

    def test_collapsed_cross_section_is_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        new['sim_positions'][3] = copy.deepcopy(new['sim_positions'][2])
        with self.assertRaisesRegex(ValueError, 'collapsed cross section'):
            self.resample(old, new)

    def test_degenerate_center_span_is_rejected(self):
        old, new = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        new['sim_positions'][2:4] = copy.deepcopy(new['sim_positions'][:2])
        with self.assertRaisesRegex(ValueError, 'degenerate center span'):
            self.resample(old, new)

    def test_reordered_native_vertices_use_source_identity(self):
        old, original = strip([0., .5, 1.]), strip([0., .2, .7, 1.])
        permutation = [7, 2, 5, 0, 6, 3, 4, 1]
        inverse = {previous: current for current, previous in enumerate(permutation)}
        reordered = copy.deepcopy(original)
        reordered['sim_positions'] = [original['sim_positions'][i] for i in permutation]
        reordered['sim_import_vertex_ids_2d'] = permutation
        reordered['sim_triangles'] = [[inverse[i] for i in tri]
                                      for tri in original['sim_triangles']]
        reordered['kinematic_sim_indices'] = [inverse[0], inverse[1]]
        expected = self.resample(old, original)
        actual = self.resample(old, reordered)
        self.assertEqual(actual, {inverse[i]: match for i, match in expected.items()})

    def test_changed_count_requires_policy_even_if_ids_keep_old_prefix(self):
        with self.assertRaisesRegex(ValueError, 'correspondence|geometry changed|triangle support changed'):
            self.updates(self.weight_case())

    def test_explicit_resample_preserves_tip_even_if_ids_keep_old_prefix(self):
        updates = self.updates(self.weight_case(self.policy()))
        self.assertEqual(updates['AuthoredClearance'][2], [.1, .2, .8, .9])

    def test_policy_is_required_after_source_ids_change(self):
        with self.assertRaisesRegex(ValueError, 'correspondence|geometry changed|triangle support changed'):
            self.updates(self.weight_case(new_id_offset=20))

    def test_policy_cannot_resample_a_different_spline(self):
        with self.assertRaisesRegex(ValueError, 'correspondence|geometry changed|triangle support changed'):
            self.updates(self.weight_case(self.policy([['Guide', 1]]), 20))

    def test_resample_with_changed_source_ids_retains_tip(self):
        updates = self.updates(self.weight_case(self.policy(), 20))
        self.assertEqual(updates['AuthoredClearance'][2], [.1, .2, .8, .9])

    def test_generated_dynamic_mask_follows_new_generator_weights(self):
        fixture = self.weight_case(self.policy(), 20)
        fixture[0]['nodes'].append(map_node('GeneratedDynamicMask', [0., 0., 1., 1., 1., 1.]))
        updates = self.updates(fixture)
        self.assertEqual(updates['GeneratedDynamicMask'][2], [0., 0., 1., 1.])

    def test_diagonal_flip_does_not_authorize_missing_or_extra_triangles(self):
        previous = Counter({(0, 1, 3): 1, (0, 2, 3): 1})
        self.assertTrue(M._same_painted_vertex_support(
            previous, Counter({(0, 1, 2): 1, (1, 2, 3): 1})))
        self.assertFalse(M._same_painted_vertex_support(
            previous, Counter({(0, 1, 2): 1})))
        self.assertFalse(M._same_painted_vertex_support(
            previous, Counter({(0, 1, 2): 2, (1, 2, 3): 1})))


if __name__ == '__main__':
    unittest.main()
