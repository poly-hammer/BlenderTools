from pathlib import Path
import math
import sys
import tempfile

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "addons"))

from send2ue.core import hair_tool_export, ue_groom_adapter


def close(actual, expected, tolerance=1.0e-6):
    assert math.isclose(actual, expected, abs_tol=tolerance), (actual, expected)


# Removing an empty slot must preserve the material choice of every polygon.
slot_mesh = bpy.data.meshes.new("HTUE_MATERIAL_SLOT_SMOKE")
slot_mesh.from_pydata(
    [(0, 0, 0), (1, 0, 0), (0, 1, 0),
     (2, 0, 0), (3, 0, 0), (2, 1, 0),
     (4, 0, 0), (5, 0, 0), (4, 1, 0)],
    [],
    [(0, 1, 2), (3, 4, 5), (6, 7, 8)],
)
slot_a = bpy.data.materials.new("HTUE_SLOT_A")
slot_b = bpy.data.materials.new("HTUE_SLOT_B")
slot_mesh.materials.append(slot_a)
slot_mesh.materials.append(None)
slot_mesh.materials.append(slot_b)
for polygon, material_index in zip(slot_mesh.polygons, (0, 1, 2)):
    polygon.material_index = material_index
slot_object = bpy.data.objects.new("HTUE_MATERIAL_SLOT_SMOKE", slot_mesh)
bpy.context.scene.collection.objects.link(slot_object)
hair_tool_export._remove_empty_material_slots(slot_object)
assert [material.name for material in slot_mesh.materials] == [
    "HTUE_SLOT_A",
    "HTUE_SLOT_B",
]
assert [polygon.material_index for polygon in slot_mesh.polygons] == [0, 0, 1]
bpy.data.objects.remove(slot_object, do_unlink=True)
bpy.data.meshes.remove(slot_mesh)
bpy.data.materials.remove(slot_a)
bpy.data.materials.remove(slot_b)


# Combined mode changes only a disposable copy of the Hair Tool AO group.
ao_child = bpy.data.node_groups.new("AO_With_Bounces_SMOKE", "ShaderNodeTree")
ao_child.interface.new_socket(
    name="Max Ray Dist",
    in_out="INPUT",
    socket_type="NodeSocketFloat",
)
ao_parent = bpy.data.node_groups.new("HT_Mesh_AO_SMOKE", "ShaderNodeTree")
ao_node = ao_parent.nodes.new("ShaderNodeGroup")
ao_node.node_tree = ao_child
ao_node.inputs["Max Ray Dist"].default_value = 50.0
ao_copy = hair_tool_export._combined_ao_node_group(
    ao_parent,
    {"combined_max_ray_distance": 0.011},
)
close(ao_parent.nodes[0].inputs["Max Ray Dist"].default_value, 50.0)
close(ao_copy.nodes[0].inputs["Max Ray Dist"].default_value, 0.011)
bpy.data.node_groups.remove(ao_copy)
bpy.data.node_groups.remove(ao_parent)
bpy.data.node_groups.remove(ao_child)


mesh = bpy.data.meshes.new("HTUE_RGB_PAYLOAD_SMOKE")
mesh.from_pydata(
    [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
    [],
    [(0, 1, 2)],
)
mesh.update()

uv_source = mesh.attributes.new(name="UVMapGN", type="FLOAT_VECTOR", domain="CORNER")
for item, value in zip(uv_source.data, ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))):
    item.vector = value

scalar_values = {
    "Random": (0.125, 0.5, 0.875),
    "Factor": (0.25, 0.5, 1.0),
    "AO": (0.25, 0.75, 0.5),
    "Depth": (0.5, 0.0, 1.0),
    "ChaosWeight": (0.0, 0.25, 1.0),
    "HairPixelDepthOffset": (1.0, 0.75, 0.125),
}
for name, values in scalar_values.items():
    attribute = mesh.attributes.new(name=name, type="FLOAT", domain="POINT")
    for item, value in zip(attribute.data, values):
        item.value = value

# A bridge-assigned Export Empty groups a directly linked Hair Tool output
# without reparenting it. Render-disabled links are intentionally skipped.
export_collection = bpy.data.collections.new("Export")
bpy.context.scene.collection.children.link(export_collection)
inherited_target = bpy.data.objects.new("Hair_Parent_Target_SMOKE", None)
export_target = bpy.data.objects.new("Hair_Export_Target_SMOKE", None)
export_collection.objects.link(inherited_target)
export_collection.objects.link(export_target)
export_source = bpy.data.objects.new("Hair_Export_Source_SMOKE", mesh)
export_collection.objects.link(export_source)
export_source.parent = inherited_target
upstream_source = bpy.data.objects.new("Hair_Upstream_Source_SMOKE", mesh)
export_collection.objects.link(upstream_source)
upstream_source.parent = inherited_target
setup_group = bpy.data.node_groups.new("Hair_System_Setup_EXPORT_SMOKE", "GeometryNodeTree")
setup_input = setup_group.interface.new_socket(
    name="Upstream Hair System",
    in_out="INPUT",
    socket_type="NodeSocketObject",
)
profile_group = bpy.data.node_groups.new("Hair_System_Profile_EXPORT_SMOKE", "GeometryNodeTree")
setup_modifier = export_source.modifiers.new("Hair_System_Setup", "NODES")
setup_modifier.node_group = setup_group
profile_modifier = export_source.modifiers.new("Profile", "NODES")
profile_modifier.node_group = profile_group
upstream_setup = upstream_source.modifiers.new("Hair_System_Setup", "NODES")
upstream_setup.node_group = setup_group
upstream_profile = upstream_source.modifiers.new("Profile", "NODES")
upstream_profile.node_group = profile_group
hair_tool_export._modifier_input_set(setup_modifier, setup_input.identifier, upstream_source)
assert hair_tool_export._modifier_input_has(setup_modifier, setup_input.identifier)
assert (
    hair_tool_export._modifier_input_get(setup_modifier, setup_input.identifier)
    is upstream_source
)
assert upstream_source in hair_tool_export._modifier_input_values(setup_modifier)
assert (
    ue_groom_adapter._modifier_input(setup_modifier, "Upstream Hair System")
    is upstream_source
)
export_source[hair_tool_export.EXPORT_TARGET_PROPERTY] = export_target
upstream_source[hair_tool_export.EXPORT_TARGET_PROPERTY] = inherited_target
original_parent = export_source.parent
original_matrix = export_source.matrix_world.copy()
assert hair_tool_export._asset_group_key(export_source) == export_target
export_target.name = "Hair_Export_Target_Renamed_SMOKE"
assert hair_tool_export._asset_group_key(export_source) == export_target
assert export_source.parent == original_parent
assert export_source.matrix_world == original_matrix
assert export_source in hair_tool_export._export_source_candidates(export_collection)
final_sources = hair_tool_export._final_export_sources(export_collection)
assert export_source in final_sources
assert upstream_source not in final_sources
export_source.hide_render = True
assert export_source not in hair_tool_export._export_source_candidates(export_collection)
export_source.hide_render = False
export_collection.objects.unlink(export_target)
try:
    hair_tool_export._asset_group_key(export_source, export_collection)
except RuntimeError as error:
    assert "Relink" in str(error)
else:
    raise AssertionError("A stale explicit Export assignment must not silently fall back")
del export_source[hair_tool_export.EXPORT_TARGET_PROPERTY]
assert hair_tool_export._asset_group_key(export_source, export_collection) == inherited_target
bpy.data.objects.remove(export_source, do_unlink=True)
bpy.data.objects.remove(upstream_source, do_unlink=True)
bpy.data.objects.remove(export_target, do_unlink=True)
bpy.data.objects.remove(inherited_target, do_unlink=True)
bpy.data.node_groups.remove(setup_group)
bpy.data.node_groups.remove(profile_group)
bpy.data.collections.remove(export_collection)
system_colors = (
    (0.125, 0.25, 0.375, 0.0),
    (0.5, 0.625, 0.75, 1.0),
    (0.875, 1.0, 0.0, 0.5),
)
system_color = mesh.attributes.new(name="SystemColor", type="FLOAT_COLOR", domain="POINT")
for item, value in zip(system_color.data, system_colors):
    item.color = value

# Per-System mode preserves Hair Tool's AO while evaluated card geometry is
# joined. Validation must not replace valid values.
selector_object = bpy.data.objects.new("HTUE_AO_POLICY_SMOKE", mesh)
selection_state = {"ao_stats": {}}
selected = hair_tool_export._preserve_per_system_ao(
    selector_object,
    selection_state,
)
close(selected["mean"], 0.5)
assert selected["source"] == "per_hair_tool_system"
assert selected["fallback"] is False
bpy.data.objects.remove(selector_object, do_unlink=True)

hair_tool_export._write_hair_tool_uvs(mesh)
hair_tool_export._pack_rfaos(mesh)

assert [layer.name for layer in mesh.uv_layers] == [
    "UVMap",
    "HairTool_SystemColor_RG",
    "HairTool_RFAOS_RG",
    "HairTool_AO_SystemB",
]

for loop_index, loop in enumerate(mesh.loops):
    vertex_index = loop.vertex_index
    random_value = scalar_values["Random"][vertex_index]
    factor = scalar_values["Factor"][vertex_index]
    ao = scalar_values["AO"][vertex_index]
    depth = scalar_values["Depth"][vertex_index]
    chaos_weight = scalar_values["ChaosWeight"][vertex_index]
    pixel_depth_offset = scalar_values["HairPixelDepthOffset"][vertex_index]
    red, green, blue, _alpha = system_colors[vertex_index]

    uv1 = mesh.uv_layers[1].data[loop_index].uv
    uv2 = mesh.uv_layers[2].data[loop_index].uv
    uv3 = mesh.uv_layers[3].data[loop_index].uv
    close(uv1[0], red)
    close(uv1[1], 1.0 - green)
    close(
        uv2[0],
        hair_tool_export.RFAOS_NANITE_UV_TAG
        + hair_tool_export._pack_unorm8_pair(random_value, depth),
    )
    close(uv2[1], 1.0 - factor)
    close(uv3[0], hair_tool_export.RFAOS_NANITE_UV_TAG + ao)
    close(uv3[1], 1.0 - blue)

    color_item = mesh.color_attributes["RFAOS"].data[loop_index]
    color = color_item.color_srgb if hasattr(color_item, "color_srgb") else color_item.color
    close(color[0], pixel_depth_offset, 1.0 / 255.0 + 1.0e-6)
    close(color[1], chaos_weight, 1.0 / 255.0 + 1.0e-6)
    close(color[2], ao, 1.0 / 255.0 + 1.0e-6)
    close(color[3], 1.0, 1.0 / 255.0 + 1.0e-6)

contract = hair_tool_export.get_rfaos_payload_contract()
assert contract["version"] == 5
assert contract["encoding"] == "HTUE_RGB_TAGGED_UV"
assert contract["system_color_alpha_used"] is False
assert contract["material_texcoord_indices"] == [1, 2, 3]
assert contract["chaos_weight_attribute"] == "ChaosWeight"
assert contract["chaos_weight_channel"] == "G"
assert contract["pixel_depth_offset_attribute"] == "HairPixelDepthOffset"
assert contract["pixel_depth_offset_channel"] == "R"
assert contract["pixel_depth_offset_fallback"] == 1.0
assert contract["system_color_absent_raw_b"] == -1.0

# Joining an absent-color part with valid black must keep their distinction.
# Alpha=0 is still a real RGB attribute, and all Factor values must survive.
mesh.attributes.remove(mesh.attributes[hair_tool_export.SYSTEM_COLOR_PRESENT_ATTRIBUTE])
black_color = mesh.attributes.new(name='SystemColor', type='FLOAT_COLOR', domain='POINT')
for item in black_color.data:
    item.color = (0.0, 0.0, 0.0, 0.0)
absent_mesh = mesh.copy()
absent_mesh.attributes.remove(absent_mesh.attributes['SystemColor'])
parts = []
for source_mesh in (mesh, absent_mesh):
    hair_tool_export._stamp_system_color_presence(source_mesh)
    part = bpy.data.objects.new('HTUE_PRESENCE_JOIN', source_mesh)
    bpy.context.scene.collection.objects.link(part)
    parts.append(part)
joined = hair_tool_export._join_objects(parts)
hair_tool_export._pack_rfaos(joined.data)
present_count = absent_count = 0
for loop_index in range(len(joined.data.loops)):
    marker = joined.data.attributes[hair_tool_export.SYSTEM_COLOR_PRESENT_ATTRIBUTE].data[loop_index].value
    uv3 = joined.data.uv_layers[3].data[loop_index].uv
    close(uv3[1], 1.0 if marker > 0.5 else 2.0)
    factor = hair_tool_export._attribute_component(joined.data, joined.data.attributes['Factor'], loop_index)
    close(joined.data.uv_layers[2].data[loop_index].uv[1], 1.0 - factor)
    if marker > 0.5:
        present_count += 1
    else:
        absent_count += 1
assert present_count == absent_count == 3

# The out-of-range sentinel must survive real FBX serialization as V=2.
bpy.ops.object.select_all(action='DESELECT')
joined.select_set(True)
bpy.context.view_layer.objects.active = joined
expected_v = sorted(item.uv[1] for item in joined.data.uv_layers[3].data)
with tempfile.TemporaryDirectory(prefix='htue_presence_fbx_') as temporary_directory:
    fbx_path = str(Path(temporary_directory) / 'presence.fbx')
    assert bpy.ops.export_scene.fbx(filepath=fbx_path, use_selection=True, bake_anim=False) == {'FINISHED'}
    bpy.data.objects.remove(joined, do_unlink=True)
    assert bpy.ops.import_scene.fbx(filepath=fbx_path) == {'FINISHED'}
    imported = next(obj for obj in bpy.context.selected_objects if obj.type == 'MESH')
    actual_v = sorted(item.uv[1] for item in imported.data.uv_layers[3].data)
    assert len(actual_v) == len(expected_v)
    for actual, expected in zip(actual_v, expected_v):
        close(actual, expected)

print("SEND2UE_HAIR_TOOL_RGB_PAYLOAD_SMOKE_OK")
