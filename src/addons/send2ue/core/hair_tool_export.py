# Copyright Epic Games, Inc. All Rights Reserved.

import json
import math
import sys

import bpy
from mathutils import Matrix
from . import armature_modifier_fix, utilities
from ..constants import BlenderTypes, ToolInfo


STATE_KEY = 'send2ue_hair_tool_export_state'
PREVIEW_RESTORE_KEY = 'send2ue_hair_tool_preview_restore_state'
MATERIAL_HEIGHT_PREVIEW_RESTORE_KEY = 'send2ue_unreal_material_height_preview_restore_state'
SOURCE_NAME_PROPERTY = '_send2ue_hair_tool_source_name'
TEMP_PROPERTY = '_send2ue_hair_tool_temp'
RFAOS_NAME = 'RFAOS'
SYSTEM_COLOR_UV_RG = 'HairTool_SystemColor_RG'
SYSTEM_COLOR_PRESENT_ATTRIBUTE = '_htue_system_color_present'
RFAOS_NANITE_UV_RG = 'HairTool_RFAOS_RG'
RFAOS_NANITE_UV_BA = 'HairTool_AO_SystemB'
SYSTEM_COLOR_UV_INDEX = 1
RFAOS_NANITE_UV_TAG = 6.0
RFAOS_NANITE_UV_START_INDEX = 2
RFAOS_PAYLOAD_VERSION = 5
RFAOS_MINIMUM_RANGE = 1.0 / 255.0
CHAOS_WEIGHT_ATTRIBUTE = 'ChaosWeight'
HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE = 'HairPixelDepthOffset'
LEGACY_CHAOS_DEPTH_MASK_ATTRIBUTE = 'ChaosDepthMask'
EXPORT_TARGET_PROPERTY = '_htue_export_target'
AO_MODIFIER_SETTING_IDS = {
    'samples': 'Input_3',
    'base_color_value': 'Input_13',
    'spread_angle': 'Input_4',
    'blur_steps': 'Input_8',
    'first_bounce_factor': 'Input_14',
    'second_bounce_factor': 'Input_15',
    'use_custom_normals': 'Socket_0',
}
COMBINED_MAX_RAY_DISTANCE_DEFAULT = 0.011


def _modifier_input_group(modifier, identifier):
    """Return one Geometry Nodes interface input on Blender 5.2+."""
    try:
        return modifier.properties.inputs[identifier]
    except (AttributeError, KeyError, TypeError):
        return None


def _modifier_input_get(modifier, identifier, fallback=None):
    """Read a Geometry Nodes input across Blender API generations."""
    input_group = _modifier_input_group(modifier, identifier)
    if input_group is not None:
        try:
            return input_group['value']
        except (KeyError, TypeError):
            return fallback
    try:
        return modifier.get(identifier, fallback)
    except (AttributeError, TypeError):
        return fallback


def _modifier_input_set(modifier, identifier, value):
    """Write a Geometry Nodes input across Blender API generations."""
    input_group = _modifier_input_group(modifier, identifier)
    if input_group is not None:
        input_group['value'] = value
        return
    modifier[identifier] = value


def _modifier_input_has(modifier, identifier):
    """Return whether an input exists without assuming modifier IDProperties."""
    if _modifier_input_group(modifier, identifier) is not None:
        return True
    try:
        return identifier in modifier
    except (AttributeError, TypeError):
        return False


def _modifier_input_delete(modifier, identifier):
    """Delete a legacy modifier input; 5.2 interface inputs are never ad hoc."""
    if _modifier_input_group(modifier, identifier) is not None:
        return
    try:
        del modifier[identifier]
    except (AttributeError, KeyError, TypeError):
        pass


def _modifier_input_values(modifier):
    """Yield Geometry Nodes input values across Blender API generations."""
    inputs = getattr(getattr(modifier, 'properties', None), 'inputs', None)
    if inputs is not None:
        for identifier in dir(inputs):
            if identifier.startswith('_'):
                continue
            try:
                input_group = inputs[identifier]
                if 'value' in input_group:
                    yield input_group['value']
            except (KeyError, TypeError):
                continue
        return
    try:
        for identifier in modifier.keys():
            yield modifier.get(identifier)
    except (AttributeError, TypeError):
        return


def _warn(message):
    print(f'[send2ue][hair_tool] WARNING: {message}')


def _set_neutral_ao(mesh):
    """Install a neutral AO fallback without stopping an automated export."""
    existing = mesh.attributes.get('AO')
    if existing:
        mesh.attributes.remove(existing)
    attribute = mesh.attributes.new(name='AO', type='FLOAT', domain='CORNER')
    for item in attribute.data:
        item.value = 1.0
    return attribute


def is_hair_tool_object(scene_object):
    """Return whether an object is a live Hair Tool geometry-nodes system."""
    if not scene_object or scene_object.type not in {'CURVES', 'MESH'}:
        return False

    if any(_is_edit_mesh_modifier(modifier) for modifier in scene_object.modifiers):
        return False

    node_group_names = {
        modifier.node_group.name
        for modifier in scene_object.modifiers
        if modifier.type == 'NODES' and modifier.node_group
    }
    return (
        any(name.startswith('Hair_System_Setup') for name in node_group_names)
        and any(name.startswith('Hair_System_Profile') for name in node_group_names)
    )


def _is_edit_mesh_modifier(modifier):
    modifier_name = str(getattr(modifier, 'name', '') or '').strip().replace(' ', '_').casefold()
    node_group = getattr(modifier, 'node_group', None)
    node_group_name = str(getattr(node_group, 'name', '') or '').strip().replace(' ', '_').casefold()
    return 'edit_mesh' in {modifier_name, node_group_name}


def is_prepared_source(scene_object):
    state = bpy.app.driver_namespace.get(STATE_KEY, {})
    return scene_object.name in state.get('source_names', set())


def _get_hair_tool_input_object(scene_object):
    """Return the upstream Hair Tool object referenced by Hair System Setup."""
    for modifier in scene_object.modifiers:
        if (
            modifier.type != 'NODES'
            or not modifier.node_group
            or not modifier.node_group.name.startswith('Hair_System_Setup')
        ):
            continue
        input_object = _modifier_input_get(modifier, 'Input_3')
        if isinstance(input_object, bpy.types.Object):
            return input_object
        # New 5.2 node groups receive Socket_* identifiers instead of the
        # legacy Input_* names. Hair System Setup has one upstream object
        # input, so use it when the preserved identifier is unavailable.
        for input_value in _modifier_input_values(modifier):
            if isinstance(input_value, bpy.types.Object):
                return input_value
    return None


def _get_armature(scene_object):
    for modifier in scene_object.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object and modifier.object.type == 'ARMATURE':
            return modifier.object

    exported_rig_objects = utilities.get_from_collection(BlenderTypes.SKELETON)
    return armature_modifier_fix.get_top_parent_rig_object(
        scene_object,
        exported_rig_objects,
    )


def _bind_export_skin(export_object, armature_object):
    """Explicit Transfer Weights takes precedence over the rigid hair binding."""
    import math
    if getattr(export_object, 'ue_unique_transfer_weights', False):
        settings = getattr(export_object, 'vdt_object_props', None)
        if not settings or not settings.transfer_source:
            raise RuntimeError('Hair Tool weight transfer source is missing')
        active = bpy.context.view_layer.objects.active
        selected = list(bpy.context.selected_objects)
        try:
            bpy.ops.object.select_all(action='DESELECT')
            export_object.select_set(True)
            bpy.context.view_layer.objects.active = export_object
            outcome = bpy.ops.object.vdt_pointer_transfer_weights()
            if 'FINISHED' not in outcome:
                raise RuntimeError('Hair Tool source weight transfer did not finish')
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in selected:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = active
        deform = {bone.name for bone in armature_object.data.bones if bone.use_deform}
        indices = {group.index for group in export_object.vertex_groups if group.name in deform}
        for vertex in export_object.data.vertices:
            weights = [item.weight for item in vertex.groups if item.group in indices]
            if (not weights or any(not math.isfinite(w) or w < 0 for w in weights)
                    or sum(weights) <= 1e-8):
                raise RuntimeError('Hair Tool transferred skin has unweighted or invalid vertices')
    else:
        # Keep the existing head-only contract for ordinary card/guide hair.
        group = export_object.vertex_groups.new(name=_get_head_bone_name(armature_object))
        group.add(range(len(export_object.data.vertices)), 1.0, 'REPLACE')
    modifier = next((m for m in export_object.modifiers
                     if m.type == 'ARMATURE' and m.object == armature_object), None)
    if modifier is None:
        modifier = export_object.modifiers.new(name='Armature', type='ARMATURE')
    modifier.object = armature_object


def _get_head_bone_name(armature_object):
    deform_bones = [bone for bone in armature_object.data.bones if bone.use_deform]

    for bone in deform_bones:
        if bone.name.casefold() == 'head':
            return bone.name
    for bone in deform_bones:
        if bone.name.casefold().endswith('_head'):
            return bone.name
    for bone in deform_bones:
        if 'head' in bone.name.casefold():
            return bone.name
    if len(deform_bones) == 1:
        return deform_bones[0].name

    raise RuntimeError(
        f'Unable to find a head bone on armature "{armature_object.name}". '
        'Expected "head", a name ending in "_Head", or a single deform bone.'
    )


def _loop_to_polygon_indices(mesh):
    loop_to_polygon = [0] * len(mesh.loops)
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            loop_to_polygon[loop_index] = polygon.index
    return loop_to_polygon


def _attribute_component(
    mesh,
    attribute,
    loop_index,
    component_index=0,
    loop_to_polygon=None,
    default=0.0,
):
    if not attribute:
        return default

    loop = mesh.loops[loop_index]
    if attribute.domain == 'POINT':
        data_index = loop.vertex_index
    elif attribute.domain == 'CORNER':
        data_index = loop_index
    elif attribute.domain == 'FACE':
        data_index = loop_to_polygon[loop_index] if loop_to_polygon else 0
    else:
        return default

    item = attribute.data[data_index]
    if hasattr(item, 'value'):
        return float(item.value)
    if hasattr(item, 'color'):
        color = item.color
        if component_index < len(color):
            return float(color[component_index])
        return default
    if hasattr(item, 'vector'):
        vector = item.vector
        if component_index < len(vector):
            return float(vector[component_index])
        return default
    return default


def _ao_statistics(mesh):
    attribute = mesh.attributes.get('AO')
    if attribute is None or not mesh.loops:
        return None
    loop_to_polygon = _loop_to_polygon_indices(mesh)
    values = [
        _attribute_component(
            mesh,
            attribute,
            loop_index,
            loop_to_polygon=loop_to_polygon,
            default=1.0,
        )
        for loop_index in range(len(mesh.loops))
    ]
    if any(
        not math.isfinite(value) or value < -1.0e-5 or value > 1.00001
        for value in values
    ):
        return None
    ordered = sorted(values)

    def percentile(amount):
        return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * amount))]

    return {
        'minimum': ordered[0],
        'p05': percentile(0.05),
        'median': percentile(0.5),
        'mean': sum(values) / len(values),
        'p95': percentile(0.95),
        'maximum': ordered[-1],
    }


def _pack_rfaos(mesh):
    """Pack the material PDO mask and cloth Weight into vertex color."""
    if mesh.attributes.get(SYSTEM_COLOR_PRESENT_ATTRIBUTE) is None:
        _stamp_system_color_presence(mesh)
    random_attribute = mesh.attributes.get('Random')
    factor_attribute = mesh.attributes.get('Factor')
    system_color_attribute = mesh.attributes.get('SystemColor')
    ao_attribute = mesh.attributes.get('AO')
    depth_attribute = mesh.attributes.get('Depth')
    chaos_weight_attribute = mesh.attributes.get(CHAOS_WEIGHT_ATTRIBUTE)
    pixel_depth_offset_attribute = mesh.attributes.get(
        HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE
    )
    if pixel_depth_offset_attribute is None:
        pixel_depth_offset_attribute = mesh.attributes.get(
            LEGACY_CHAOS_DEPTH_MASK_ATTRIBUTE
        )
        if pixel_depth_offset_attribute is not None:
            _warn(
                f'Hair Tool mesh "{mesh.name}" uses legacy '
                f'"{LEGACY_CHAOS_DEPTH_MASK_ATTRIBUTE}". Rename it to '
                f'"{HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE}"; this export remains '
                'compatible.'
            )
    loop_to_polygon = _loop_to_polygon_indices(mesh)

    if factor_attribute is None:
        _warn(
            f'Hair Tool mesh "{mesh.name}" has no "Factor" attribute. '
            'Using a neutral 0.5 fallback; Root/Tip ranges will not vary.'
        )
        factor_values = [0.5] * len(mesh.loops)
    else:
        factor_values = [
            _attribute_component(
                mesh,
                factor_attribute,
                loop_index,
                loop_to_polygon=loop_to_polygon,
            )
            for loop_index in range(len(mesh.loops))
        ]
    if system_color_attribute is None:
        _warn(
            f'Hair Tool mesh "{mesh.name}" has no "SystemColor" attribute. '
            'Using neutral black RGB; the System Color stage will add no color.'
        )
    if chaos_weight_attribute is None:
        _warn(
            f'Hair Tool mesh "{mesh.name}" has no "{CHAOS_WEIGHT_ATTRIBUTE}" '
            'export mask. Unreal vertex color G will use the safe fixed fallback 0.'
        )
    if pixel_depth_offset_attribute is None:
        _warn(
            f'Hair Tool mesh "{mesh.name}" has no '
            f'"{HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE}" export mask. Unreal vertex '
            'color R will use the neutral fallback 1, leaving the material PDO '
            'calculation unchanged.'
        )
    if factor_values and max(factor_values) - min(factor_values) <= RFAOS_MINIMUM_RANGE:
        _warn(
            f'Hair Tool mesh "{mesh.name}" has a constant "Factor" attribute. '
            'Root/Tip ranges will not vary for this export.'
        )

    existing_rfaos = mesh.attributes.get(RFAOS_NAME)
    if existing_rfaos:
        mesh.attributes.remove(existing_rfaos)

    rfaos = mesh.color_attributes.new(
        name=RFAOS_NAME,
        type='BYTE_COLOR',
        domain='CORNER',
    )
    packed_values = []
    vertex_color_values = []
    invalid_channels = set()
    channel_names = (
        'Random',
        'Factor',
        'AO',
        'Depth',
        'SystemColor R',
        'SystemColor G',
        'SystemColor B',
    )
    for loop_index in range(len(mesh.loops)):
        packed = [
            _attribute_component(
                mesh, random_attribute, loop_index,
                loop_to_polygon=loop_to_polygon,
            ),
            factor_values[loop_index],
            _attribute_component(
                mesh, ao_attribute, loop_index,
                loop_to_polygon=loop_to_polygon,
                default=1.0,
            ),
            _attribute_component(
                mesh, depth_attribute, loop_index,
                loop_to_polygon=loop_to_polygon,
            ),
            _attribute_component(
                mesh, system_color_attribute, loop_index,
                component_index=0,
                loop_to_polygon=loop_to_polygon,
            ),
            _attribute_component(
                mesh, system_color_attribute, loop_index,
                component_index=1,
                loop_to_polygon=loop_to_polygon,
            ),
            _attribute_component(
                mesh, system_color_attribute, loop_index,
                component_index=2,
                loop_to_polygon=loop_to_polygon,
            ),
        ]
        fallback_values = (0.0, 0.5, 1.0, 0.0, 0.0, 0.0, 0.0)
        for channel_index, (channel_name, value) in enumerate(zip(channel_names, packed)):
            if not math.isfinite(value) or value < -1.0e-5 or value > 1.00001:
                invalid_channels.add(channel_name)
                packed[channel_index] = fallback_values[channel_index]
        packed = tuple(min(max(value, 0.0), 1.0) for value in packed)
        packed_values.append(packed)

        vertex_channels = [
            _attribute_component(
                mesh,
                pixel_depth_offset_attribute,
                loop_index,
                loop_to_polygon=loop_to_polygon,
                default=1.0,
            ),
            _attribute_component(
                mesh,
                chaos_weight_attribute,
                loop_index,
                loop_to_polygon=loop_to_polygon,
                default=0.0,
            ),
            packed[2],
            1.0,
        ]
        vertex_channel_names = (
            HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE,
            CHAOS_WEIGHT_ATTRIBUTE,
            'AO vertex fallback',
            'Reserved Alpha',
        )
        vertex_fallbacks = (1.0, 0.0, 1.0, 1.0)
        for channel_index, (channel_name, value) in enumerate(
            zip(vertex_channel_names, vertex_channels)
        ):
            if not math.isfinite(value) or value < -1.0e-5 or value > 1.00001:
                invalid_channels.add(channel_name)
                vertex_channels[channel_index] = vertex_fallbacks[channel_index]
        vertex_color_values.append(
            tuple(min(max(value, 0.0), 1.0) for value in vertex_channels)
        )

    if invalid_channels:
        _warn(
            f'Hair Tool mesh "{mesh.name}" had invalid values in '
            f'{", ".join(sorted(invalid_channels))}; safe fallbacks were used.'
        )

    for color_item, vertex_channels in zip(rfaos.data, vertex_color_values):
        # BYTE_COLOR exposes ``color`` in scene-linear space, while FBX writes
        # the underlying sRGB byte values. RFAOS contains data masks, not
        # display colors, so write the sRGB-facing property to keep the numeric
        # RGBA values unchanged when Unreal imports the FBX vertex colors.
        if hasattr(color_item, 'color_srgb'):
            color_item.color_srgb = vertex_channels
        else:
            color_item.color = vertex_channels

    for color_attribute in list(mesh.color_attributes):
        if color_attribute.name != RFAOS_NAME:
            mesh.color_attributes.remove(color_attribute)

    mesh.color_attributes.active_color = mesh.color_attributes[RFAOS_NAME]
    mesh.color_attributes.render_color_index = mesh.color_attributes.find(RFAOS_NAME)

    _pack_rfaos_nanite_uvs(mesh, packed_values)


def _ensure_payload_uv(mesh, name, index):
    uv_layers = mesh.uv_layers
    layer = uv_layers.get(name)
    if layer is not None:
        layer_index = next(
            (layer_index for layer_index, candidate in enumerate(uv_layers) if candidate == layer),
            -1,
        )
        if layer_index != index:
            uv_layers.remove(layer)
            layer = None

    while len(uv_layers) <= index:
        uv_layers.new(name=f'HairTool_Reserved_UV{len(uv_layers)}')
    if layer is None:
        layer = uv_layers[index]
        if layer.name != name:
            if not layer.name.startswith('HairTool_Reserved_UV'):
                _warn(
                    f'Hair Tool Nanite payload replaced "{layer.name}" at '
                    f'UV{index} on "{mesh.name}" with "{name}".'
                )
            layer.name = name

    return layer


def _pack_unorm8_pair(first, second):
    """Pack two UNORM8 values into one exactly decodable normalized float."""
    first_byte = round(min(max(first, 0.0), 1.0) * 255.0)
    second_byte = round(min(max(second, 0.0), 1.0) * 255.0)
    return ((first_byte * 256) + second_byte) / 65535.0


def _stamp_system_color_presence(mesh):
    """Preserve attribute existence on export-only geometry before joining."""
    if mesh.attributes.get(SYSTEM_COLOR_PRESENT_ATTRIBUTE) is not None:
        # Keep a per-component stamp made by Profile before its geometry join.
        # SystemColor alpha is deliberately irrelevant to color availability.
        return
    presence = mesh.attributes.new(
        name=SYSTEM_COLOR_PRESENT_ATTRIBUTE, type='FLOAT', domain='CORNER',
    )
    value = float(mesh.attributes.get('SystemColor') is not None)
    presence.data.foreach_set('value', [value] * len(mesh.loops))


def _pack_rfaos_nanite_uvs(mesh, packed_values):
    """Write the v3 Nanite-safe scalar and SystemColor RGB payload.

    UV1 stores linear SystemColor RG.
    UV2 stores tagged packed Random+Depth in U and Factor in V.
    UV3 stores tagged AO in U and linear SystemColor B in V. Absent colors
    use raw B=-1 (Blender V=2); valid black with alpha=0 stays present.
    The FBX skeletal importer applies ``V = 1 - V``, so Unreal receives the
    original G, Factor, and B values in the V components.
    """
    system_rg_layer = _ensure_payload_uv(
        mesh,
        SYSTEM_COLOR_UV_RG,
        SYSTEM_COLOR_UV_INDEX,
    )
    rg_layer = _ensure_payload_uv(
        mesh,
        RFAOS_NANITE_UV_RG,
        RFAOS_NANITE_UV_START_INDEX,
    )
    ba_layer = _ensure_payload_uv(
        mesh,
        RFAOS_NANITE_UV_BA,
        RFAOS_NANITE_UV_START_INDEX + 1,
    )

    presence = mesh.attributes.get(SYSTEM_COLOR_PRESENT_ATTRIBUTE)
    has_system_color = mesh.attributes.get('SystemColor') is not None
    loop_to_polygon = _loop_to_polygon_indices(mesh)
    for index, packed in enumerate(packed_values):
        random_value, factor, ao, depth, system_r, system_g, system_b = packed
        present = _attribute_component(
            mesh, presence, index, loop_to_polygon=loop_to_polygon,
            default=float(has_system_color),
        ) > 0.5
        system_rg_layer.data[index].uv = (
            system_r,
            1.0 - system_g,
        )
        rg_layer.data[index].uv = (
            RFAOS_NANITE_UV_TAG + _pack_unorm8_pair(random_value, depth),
            1.0 - factor,
        )
        ba_layer.data[index].uv = (
            RFAOS_NANITE_UV_TAG + ao,
            1.0 - system_b if present else 2.0,
        )


def _remove_empty_material_slots(scene_object):
    mesh = scene_object.data
    materials = [slot.material for slot in scene_object.material_slots]
    if not materials or all(material is not None for material in materials):
        return

    valid_materials = []
    old_to_new_index = {}
    fallback_index = None
    for old_index, material in enumerate(materials):
        if material is None:
            continue
        if fallback_index is None:
            fallback_index = old_index
        old_to_new_index[old_index] = len(valid_materials)
        valid_materials.append(material)

    if fallback_index is None:
        return

    fallback_new_index = old_to_new_index[fallback_index]
    for old_index, material in enumerate(materials):
        if material is None:
            old_to_new_index[old_index] = fallback_new_index

    remapped_indices = [
        old_to_new_index.get(
            polygon.material_index,
            fallback_new_index,
        )
        for polygon in mesh.polygons
    ]

    mesh.materials.clear()
    for material in valid_materials:
        mesh.materials.append(material)
    # Clearing the material collection resets every polygon to slot zero.
    # Restore the remap only after the compacted slot list is in place.
    for polygon, material_index in zip(mesh.polygons, remapped_indices):
        polygon.material_index = material_index


def _apply_ao_modifier_settings(modifier, ao_settings):
    if not ao_settings:
        return
    for field, identifier in AO_MODIFIER_SETTING_IDS.items():
        if field in ao_settings:
            _modifier_input_set(modifier, identifier, ao_settings[field])


def _combined_ao_node_group(source_group, ao_settings=None):
    """Copy HT_Mesh_AO and set its unlinked ray-distance inputs safely."""
    maximum_distance = float(
        (ao_settings or {}).get(
            'combined_max_ray_distance',
            COMBINED_MAX_RAY_DISTANCE_DEFAULT,
        )
    )
    temporary_group = source_group.copy()
    temporary_group.name = '__S2U_HT_Mesh_AO'
    adjusted_nodes = 0
    for node in temporary_group.nodes:
        child_group = getattr(node, 'node_tree', None)
        if (
            node.type != 'GROUP'
            or child_group is None
            or not child_group.name.startswith('AO_With_Bounces')
        ):
            continue
        ray_distance = node.inputs.get('Max Ray Dist')
        if ray_distance is None or ray_distance.is_linked:
            continue
        ray_distance.default_value = maximum_distance
        adjusted_nodes += 1
    if adjusted_nodes == 0:
        _warn(
            'The temporary HT_Mesh_AO copy has no adjustable '
            'AO_With_Bounces Max Ray Dist inputs.'
        )
    return temporary_group


def _evaluate_combined_ao(scene_object, state, ao_settings=None):
    """Evaluate Hair Tool AO once on the final, joined export geometry."""
    node_group = bpy.data.node_groups.get('HT_Mesh_AO')
    if not node_group:
        _warn(
            'Hair Tool node group "HT_Mesh_AO" was not found. '
            'Export continues with neutral AO=1.'
        )
        _set_neutral_ao(scene_object.data)
        state.setdefault('ao_stats', {})[scene_object.name] = {
            'minimum': 1.0,
            'maximum': 1.0,
            'source': 'combined_export_geometry',
            'fallback': True,
        }
        return

    original_mesh = scene_object.data
    original_world_matrix = scene_object.matrix_world.copy()
    world_mesh = None
    evaluated_mesh = None
    modifier = None
    temporary_node_group = None

    try:
        # The input is already one realized mesh containing every final Hair
        # Tool system in this export asset. Bake world space before AO so the
        # ray distances match the geometry that Unreal receives.
        world_mesh = original_mesh.copy()
        world_mesh.transform(original_world_matrix, shape_keys=True)
        existing_ao = world_mesh.attributes.get('AO')
        if existing_ao:
            world_mesh.attributes.remove(existing_ao)
        scene_object.data = world_mesh
        scene_object.matrix_world = Matrix.Identity(4)

        temporary_node_group = _combined_ao_node_group(node_group, ao_settings)
        modifier = scene_object.modifiers.new(name='__S2U_HAIR_AO', type='NODES')
        modifier.node_group = temporary_node_group
        _apply_ao_modifier_settings(modifier, ao_settings)
        if _modifier_input_has(modifier, 'Input_7'):
            _modifier_input_set(modifier, 'Input_7', 'AO')

        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph.update()
        evaluated_object = scene_object.evaluated_get(depsgraph)
        evaluated_mesh = bpy.data.meshes.new_from_object(evaluated_object)

        # Combined AO must not change topology. A mismatch means the proxy was
        # not the final realized export geometry and is unsafe to pack.
        if (
            len(evaluated_mesh.vertices) != len(world_mesh.vertices)
            or len(evaluated_mesh.polygons) != len(world_mesh.polygons)
        ):
            raise RuntimeError(
                'HT_Mesh_AO changed final joined topology: '
                f'{len(world_mesh.vertices)}->{len(evaluated_mesh.vertices)} vertices, '
                f'{len(world_mesh.polygons)}->{len(evaluated_mesh.polygons)} polygons'
            )

        ao_attribute = evaluated_mesh.attributes.get('AO')
        if not ao_attribute:
            _warn(
                f'Hair Tool AO evaluation produced no "AO" attribute for '
                f'"{scene_object.name}"; export continues with neutral AO=1.'
            )
            ao_attribute = _set_neutral_ao(evaluated_mesh)

        loop_to_polygon = _loop_to_polygon_indices(evaluated_mesh)
        ao_values = [
            _attribute_component(
                evaluated_mesh,
                ao_attribute,
                loop_index,
                loop_to_polygon=loop_to_polygon,
            )
            for loop_index in range(len(evaluated_mesh.loops))
        ]
        invalid_ao = any(
            not math.isfinite(value) or value < -1.0e-5 or value > 1.00001
            for value in ao_values
        )
        if not ao_values or invalid_ao:
            _warn(
                f'Hair Tool AO evaluation produced values outside finite 0-1 '
                f'for "{scene_object.name}"; export continues with neutral AO=1.'
            )
            _set_neutral_ao(evaluated_mesh)
            ao_values = [1.0] * len(evaluated_mesh.loops)

        ao_min = min(ao_values)
        ao_max = max(ao_values)
        ao_stats = _ao_statistics(evaluated_mesh) or {
            'minimum': ao_min,
            'maximum': ao_max,
            'mean': sum(ao_values) / len(ao_values),
        }
        if ao_max - ao_min <= RFAOS_MINIMUM_RANGE:
            _warn(
                f'Hair Tool AO evaluation produced a constant value '
                f'({ao_min:.6f}) for "{scene_object.name}". Export continues.'
            )

        evaluated_mesh.transform(original_world_matrix.inverted_safe())
        scene_object.modifiers.remove(modifier)
        modifier = None
        bpy.data.node_groups.remove(temporary_node_group)
        temporary_node_group = None
        if original_mesh.shape_keys:
            # AO is attribute-only. Replacing the mesh with new_from_object()
            # would discard the evaluated expression keys baked before joining.
            if any(
                tuple(a.vertices) != tuple(b.vertices)
                for a, b in zip(original_mesh.polygons, evaluated_mesh.polygons)
            ):
                raise RuntimeError('HT_Mesh_AO reordered shape-key topology')
            existing_ao = original_mesh.attributes.get('AO')
            if existing_ao:
                original_mesh.attributes.remove(existing_ao)
            preserved_ao = original_mesh.attributes.new('AO', 'FLOAT', 'CORNER')
            preserved_ao.data.foreach_set('value', ao_values)
            scene_object.data = original_mesh
            bpy.data.meshes.remove(evaluated_mesh)
            evaluated_mesh = None
        else:
            scene_object.data = evaluated_mesh
        scene_object.matrix_world = original_world_matrix
        state.setdefault('ao_stats', {})[scene_object.name] = {
            **ao_stats,
            'source': 'combined_export_geometry',
            'vertices': len(scene_object.data.vertices),
            'polygons': len(scene_object.data.polygons),
            'fallback': False,
        }
        state['temporary_mesh_names'].add(scene_object.data.name)
        evaluated_mesh = None

        if original_mesh.users == 0:
            bpy.data.meshes.remove(original_mesh)
        if world_mesh.users == 0:
            bpy.data.meshes.remove(world_mesh)
        world_mesh = None
    except Exception as error:
        if modifier and modifier.name in scene_object.modifiers:
            scene_object.modifiers.remove(modifier)
        if temporary_node_group and temporary_node_group.users == 0:
            bpy.data.node_groups.remove(temporary_node_group)
        scene_object.data = original_mesh
        scene_object.matrix_world = original_world_matrix
        for mesh in (evaluated_mesh, world_mesh):
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        _set_neutral_ao(original_mesh)
        state.setdefault('ao_stats', {})[scene_object.name] = {
            'minimum': 1.0,
            'maximum': 1.0,
            'source': 'combined_export_geometry',
            'fallback': True,
            'error': str(error),
        }
        _warn(
            f'Hair Tool AO evaluation failed for "{scene_object.name}": '
            f'{error}. Export continues with neutral AO=1.'
        )


def _preserve_per_system_ao(scene_object, state):
    """Validate the Hair Tool AO preserved before final geometry joining."""
    stats = _ao_statistics(scene_object.data)
    fallback = stats is None
    if fallback:
        _warn(
            f'Hair Tool systems for "{scene_object.name}" produced no valid AO; '
            'using neutral AO=1.'
        )
        _set_neutral_ao(scene_object.data)
        stats = _ao_statistics(scene_object.data)
    result = {
        **stats,
        'source': 'per_hair_tool_system',
        'vertices': len(scene_object.data.vertices),
        'polygons': len(scene_object.data.polygons),
        'fallback': fallback,
    }
    state.setdefault('ao_stats', {})[scene_object.name] = result
    return result


def _write_uv_layer(mesh, source_attribute_name, target_uv_name):
    source_attribute = mesh.attributes.get(source_attribute_name)
    if (
        not source_attribute
        or source_attribute.domain != 'CORNER'
        or source_attribute.data_type not in {'FLOAT_VECTOR', 'FLOAT2'}
    ):
        return False

    existing_target = mesh.attributes.get(target_uv_name)
    if existing_target and existing_target != source_attribute:
        mesh.attributes.remove(existing_target)

    uv_layer = mesh.uv_layers.get(target_uv_name)
    if not uv_layer:
        uv_layer = mesh.uv_layers.new(name=target_uv_name)

    for loop_index, uv_item in enumerate(uv_layer.data):
        source_item = source_attribute.data[loop_index]
        source_vector = source_item.vector
        uv_item.uv = (source_vector[0], source_vector[1])
    return True


def _write_hair_tool_uvs(mesh):
    if not _write_uv_layer(mesh, 'UVMapGN', 'UVMap'):
        _warn(
            f'Hair Tool mesh "{mesh.name}" has no usable UVMapGN corner attribute.'
            ' Existing UV0 will be used.'
        )
        if not mesh.uv_layers:
            mesh.uv_layers.new(name='UVMap')
    # Skeletal meshes expose only UV0..UV3. Contract v3 dedicates UV1 to
    # SystemColor.RG; the previous HairTool_UV helper had no Unreal consumer.
    render_uv = mesh.uv_layers.get('UVMap') or mesh.uv_layers[0]
    mesh.uv_layers.active = render_uv
    render_uv.active_render = True


def get_rfaos_payload_contract(mesh_object=None):
    """Return the JSON-safe contract consumed by the Unreal importer."""
    contract = {
        'version': RFAOS_PAYLOAD_VERSION,
        'encoding': 'HTUE_RGB_TAGGED_UV',
        'vertex_color_name': RFAOS_NAME,
        'vertex_color_channels': {
            'R': HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE,
            'G': CHAOS_WEIGHT_ATTRIBUTE,
            'B': 'AO',
            'A': 'ReservedOne',
        },
        'chaos_weight_attribute': CHAOS_WEIGHT_ATTRIBUTE,
        'chaos_weight_channel': 'G',
        'chaos_weight_fallback': 0.0,
        'pixel_depth_offset_attribute': HAIR_PIXEL_DEPTH_OFFSET_ATTRIBUTE,
        'pixel_depth_offset_channel': 'R',
        'pixel_depth_offset_fallback': 1.0,
        'pixel_depth_offset_legacy_attribute': LEGACY_CHAOS_DEPTH_MASK_ATTRIBUTE,
        'system_color_uv_index': SYSTEM_COLOR_UV_INDEX,
        'uv_rg_index': RFAOS_NANITE_UV_START_INDEX,
        'uv_ba_index': RFAOS_NANITE_UV_START_INDEX + 1,
        'uv_tag': RFAOS_NANITE_UV_TAG,
        'uv_rg_u_packing': 'UNORM8_PAIR_RANDOM_DEPTH',
        'system_color_encoding': 'LINEAR_RGB_UV1_RG_UV3_V',
        'system_color_alpha_used': False,
        'system_color_absent_raw_b': -1.0,
        'requires_full_precision_uvs': True,
        'depth_attribute': 'Depth',
        'depth_fallback': 0.0,
        'fbx_inverts_v': True,
        'nanite_max_uv_index': 3,
        'material_master': '/Game/Material/HairTool/Master/M_HT_HairCards',
        'material_texcoord_indices': [
            SYSTEM_COLOR_UV_INDEX,
            RFAOS_NANITE_UV_START_INDEX,
            RFAOS_NANITE_UV_START_INDEX + 1,
        ],
    }
    if mesh_object is not None and mesh_object.data.shape_keys:
        import numpy as np
        keys = mesh_object.data.shape_keys.key_blocks
        count = len(mesh_object.data.vertices) * 3
        matrix = np.array(mesh_object.matrix_world.to_3x3(), dtype=np.float64)
        maxima = {}
        for key in keys[1:]:
            base, shaped = np.empty(count, dtype=np.float32), np.empty(count, dtype=np.float32)
            key.relative_key.data.foreach_get('co', base)
            key.data.foreach_get('co', shaped)
            delta = (shaped-base).reshape(-1, 3) @ matrix.T * 100.0
            maxima[key.name] = float(np.linalg.norm(delta, axis=1).max())
        contract['evaluated_morph_max_delta_cm'] = maxima
    return contract


def _evaluated_mesh_objects(
    source_object,
    state,
    include_system_ao=False,
    ao_settings=None,
):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    created_objects = []

    ao_modifiers = [
        modifier
        for modifier in source_object.modifiers
        if (
            modifier.type == 'NODES'
            and modifier.node_group
            and modifier.node_group.name.startswith('HT_Mesh_AO')
        )
    ] if include_system_ao else []
    ao_viewport_states = [modifier.show_viewport for modifier in ao_modifiers]
    ao_setting_states = []
    for modifier in ao_modifiers:
        modifier_state = {}
        for identifier in AO_MODIFIER_SETTING_IDS.values():
            modifier_state[identifier] = (
                _modifier_input_has(modifier, identifier),
                _modifier_input_get(modifier, identifier),
            )
        ao_setting_states.append(modifier_state)
        _apply_ao_modifier_settings(modifier, ao_settings)
    for modifier in ao_modifiers:
        modifier.show_viewport = True
    if ao_modifiers:
        depsgraph.update()

    try:
        for instance in depsgraph.object_instances:
            parent = instance.parent
            original_object = instance.object.original
            original_parent = parent.original if parent else None
            if instance.object.type != 'MESH':
                continue
            # A direct evaluated result or an actual GN instance is valid. A
            # plain child mesh is construction data, not an AO occluder.
            if original_object != source_object and not (
                instance.is_instance and original_parent == source_object
            ):
                continue

            instance_matrix = instance.matrix_world.copy()
            mesh = bpy.data.meshes.new_from_object(instance.object)
            if not mesh.vertices or not mesh.polygons:
                bpy.data.meshes.remove(mesh)
                continue
            if include_system_ao and mesh.attributes.get('AO') is None:
                _set_neutral_ao(mesh)

            mesh_object = bpy.data.objects.new(
                f'__S2U_HAIR_PART_{source_object.name}',
                mesh,
            )
            bpy.context.scene.collection.objects.link(mesh_object)
            mesh_object.matrix_world = instance_matrix
            created_objects.append(mesh_object)
            state['temporary_object_names'].add(mesh_object.name)
    finally:
        for modifier, viewport_state, modifier_state in zip(
            ao_modifiers,
            ao_viewport_states,
            ao_setting_states,
        ):
            for identifier, (existed, value) in modifier_state.items():
                if existed:
                    _modifier_input_set(modifier, identifier, value)
                elif _modifier_input_has(modifier, identifier):
                    _modifier_input_delete(modifier, identifier)
            modifier.show_viewport = viewport_state
        if ao_modifiers:
            depsgraph.update()

    return created_objects


def _asset_ao_configuration(asset_parent):
    settings = getattr(asset_parent, 'htue_ao_settings', None)
    if settings is None:
        return {'evaluation_mode': 'PER_SYSTEM'}
    return {
        'evaluation_mode': str(settings.evaluation_mode),
        'combined_max_ray_distance': float(
            getattr(
                settings,
                'combined_max_ray_distance',
                COMBINED_MAX_RAY_DISTANCE_DEFAULT,
            )
        ),
        **{
            field: getattr(settings, field)
            for field in AO_MODIFIER_SETTING_IDS
        },
    }


def _join_objects(objects):
    """Join disposable evaluated parts in world space without touching sources."""
    if not objects:
        return None
    # Normalize only temporary copies. This removes active-object and
    # non-uniform source-transform dependence from the combined AO evaluation.
    for scene_object in objects:
        world_matrix = scene_object.matrix_world.copy()
        negative_handedness = world_matrix.to_3x3().determinant() < 0.0
        scene_object.data.transform(world_matrix, shape_keys=True)
        if negative_handedness:
            scene_object.data.flip_normals()
        scene_object.data.update()
        scene_object.matrix_world = Matrix.Identity(4)
    if len(objects) == 1:
        return objects[0]

    bpy.ops.object.select_all(action='DESELECT')
    for scene_object in objects:
        scene_object.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()
    return objects[0]


def _copy_transfer_settings(temporary_object, source_objects):
    transfer_source = None
    shape_keys = False
    weights = False

    for source_object in source_objects:
        shape_keys = shape_keys or bool(
            getattr(source_object, 'ue_unique_transfer_shape_keys', False)
        )
        weights = weights or bool(
            getattr(source_object, 'ue_unique_transfer_weights', False)
        )
        if transfer_source is None and hasattr(source_object, 'vdt_object_props'):
            transfer_source = source_object.vdt_object_props.transfer_source

    if hasattr(temporary_object, 'ue_unique_transfer_shape_keys'):
        temporary_object.ue_unique_transfer_shape_keys = shape_keys
    if hasattr(temporary_object, 'ue_unique_transfer_weights'):
        temporary_object.ue_unique_transfer_weights = weights
    if (
        transfer_source is not None
        and hasattr(temporary_object, 'vdt_object_props')
    ):
        temporary_object.vdt_object_props.transfer_source = transfer_source


def _bake_group_shape_keys(source_parts):
    """Preserve opted-in live deformations before combining card systems.

    Once a group contains an evaluated transfer, fulfill every requested shape
    transfer per system. A later proximity pass over the joined mesh would erase
    the evaluated keys and can bind lower lashes to the upper eyelid again.
    """
    requested = [
        (source, parts) for source, parts in source_parts
        if parts and getattr(source, 'ue_unique_transfer_shape_keys', False)
    ]
    if not any(source.get('vdt_evaluated_shape_keys', False) for source, _ in requested):
        return False
    from vertex_data_tools.evaluated_shape_keys import bake_evaluated_shape_keys
    from vertex_data_tools import run_pointer_shape_key_transfer

    evaluated = {}
    proximity = []
    for source, parts in requested:
        settings = getattr(source, 'vdt_object_props', None)
        character = settings.transfer_source if settings else None
        if character is None:
            raise RuntimeError(f'{source.name}: shape-key transfer source is missing')
        if source.get('vdt_evaluated_shape_keys', False):
            evaluated.setdefault(character, []).append((source, parts))
        else:
            proximity.extend((character, part) for part in parts)
    for character, entries in evaluated.items():
        bake_evaluated_shape_keys(character, entries)
    for character, part in proximity:
        part.vdt_object_props.transfer_source = character
        run_pointer_shape_key_transfer(bpy.context, character, part)
    return True


def _link_to_export_collection(scene_object, export_collection):
    for collection in list(scene_object.users_collection):
        collection.objects.unlink(scene_object)
    export_collection.objects.link(scene_object)


def _asset_group_key(scene_object, export_collection=None):
    """Group a Hair Tool system by its explicit or inherited Export Empty."""
    export_collection = export_collection or bpy.data.collections.get(
        ToolInfo.EXPORT_COLLECTION.value
    )
    exported_objects = set(export_collection.all_objects) if export_collection else set()

    if EXPORT_TARGET_PROPERTY in scene_object:
        explicit_target = scene_object.get(EXPORT_TARGET_PROPERTY)
        if (
            isinstance(explicit_target, bpy.types.Object)
            and explicit_target.type == 'EMPTY'
            and export_collection is not None
            and export_collection in explicit_target.users_collection
        ):
            return explicit_target
        raise RuntimeError(
            f'Invalid Export Empty target on "{scene_object.name}". '
            'Relink it from HT Unreal > Export Collection Link.'
        )

    parent = scene_object.parent
    while parent:
        if parent.type == 'EMPTY' and parent in exported_objects:
            return parent
        parent = parent.parent

    return scene_object.parent or scene_object


def _export_source_candidates(export_collection):
    """Return direct, visible, render-enabled Hair Tool Export links only."""
    from . import export_selection
    return [
        scene_object
        for scene_object in export_collection.all_objects
        if (
            export_collection in scene_object.users_collection
            and export_selection.includes(scene_object)
            and scene_object.visible_get()
            and not scene_object.hide_render
            and is_hair_tool_object(scene_object)
        )
    ]


def _final_export_sources(export_collection):
    """Return final Hair Tool outputs after global upstream suppression."""
    source_candidates = _export_source_candidates(export_collection)
    upstream_sources = {
        input_object
        for input_object in (
            _get_hair_tool_input_object(scene_object)
            for scene_object in source_candidates
        )
        if input_object in source_candidates
    }
    return [
        scene_object
        for scene_object in source_candidates
        if scene_object not in upstream_sources
    ]


def restore_debug_view():
    """Restore real source visibility before context capture or export discovery."""
    debug_view = sys.modules.get('debug_render_pass_cycle')
    restore_debug = getattr(debug_view, 'prepare_for_export', None)
    if callable(restore_debug):
        restore_debug()


def prepare():
    """Create export-only mesh copies for Hair Tool systems in the Export collection."""
    restore_debug_view()
    cleanup()
    from . import hair_guide_cloth
    guide_cloth_enabled = hair_guide_cloth.enabled()
    hair_guide_cloth.begin()

    # M_LayerBlend height in Blender is a clearance/silhouette preview only.
    # Send to Unreal must export the authored base mesh so Unreal applies Nanite
    # displacement exactly once. Restore any state left by an interrupted run,
    # then suspend every Bridge-owned height preview for this operation.
    try:
        from hair_tool_unreal_bridge import layerblend_preview

        stale_states = bpy.app.driver_namespace.pop(
            MATERIAL_HEIGHT_PREVIEW_RESTORE_KEY, None
        )
        if stale_states is not None:
            layerblend_preview.restore_height_previews(stale_states)
        height_preview_states = layerblend_preview.suspend_height_previews()
        bpy.app.driver_namespace[
            MATERIAL_HEIGHT_PREVIEW_RESTORE_KEY
        ] = height_preview_states
    except ImportError:
        pass

    # A Bridge AO preview is a disposable display cache, not an export source.
    # Restore the untouched live Hair Tool systems before discovering outputs.
    try:
        from hair_tool_unreal_bridge import deformer_sync

        bpy.app.driver_namespace.pop(PREVIEW_RESTORE_KEY, None)
        preview_objects = [
            scene_object
            for scene_object in bpy.data.objects
            if scene_object.get(deformer_sync.COMBINED_PREVIEW_PROPERTY)
        ]
        preview_restore_states = []
        for preview_object in preview_objects:
            try:
                source_states = json.loads(
                    str(
                        preview_object.get(
                            deformer_sync.COMBINED_PREVIEW_SOURCES_PROPERTY,
                            "[]",
                        )
                    )
                )
            except (TypeError, ValueError):
                source_states = []
            preview_restore_states.append(
                {
                    'root_name': str(
                        preview_object.get(
                            deformer_sync.COMBINED_PREVIEW_ROOT_PROPERTY,
                            '',
                        )
                    ),
                    'source_states': source_states,
                }
            )
            deformer_sync.remove_combined_ao_preview(preview_object)
        if preview_restore_states:
            bpy.app.driver_namespace[PREVIEW_RESTORE_KEY] = preview_restore_states
    except ImportError:
        pass

    # The UE Groom path reads evaluated Curves directly. Only the Cards and
    # Cards + Groom modes need temporary evaluated mesh copies.
    from . import ue_groom_adapter
    if not ue_groom_adapter.wants_cards(bpy.context.scene.send2ue):
        return

    export_collection = bpy.data.collections.get(ToolInfo.EXPORT_COLLECTION.value)
    if not export_collection:
        return

    # Upstream suppression is global, before grouping. The Bridge AO preview
    # calls the same helper so different target assignments cannot diverge.
    source_objects = _final_export_sources(export_collection)
    state = {
        'source_names': {scene_object.name for scene_object in source_objects},
        'temporary_object_names': set(),
        'temporary_mesh_names': set(),
    }
    bpy.app.driver_namespace[STATE_KEY] = state

    try:
        grouped_sources = {}
        for source_object in source_objects:
            grouped_sources.setdefault(
                _asset_group_key(source_object, export_collection),
                [],
            ).append(source_object)

        for asset_parent, asset_sources in grouped_sources.items():
            ao_configuration = _asset_ao_configuration(asset_parent)
            parts = []
            shape_source_parts = []
            guide_captures = []
            guide_capture_complete = guide_cloth_enabled
            for source_object in asset_sources:
                if guide_cloth_enabled:
                    captured = hair_guide_cloth.capture_source(
                        source_object, state,
                        include_system_ao=(ao_configuration['evaluation_mode'] == 'PER_SYSTEM'),
                        ao_settings=ao_configuration,
                    )
                    if captured is not None:
                        source_parts, guide_capture = captured
                        parts.extend(source_parts)
                        shape_source_parts.append((source_object, source_parts))
                        guide_captures.append(guide_capture)
                        continue
                    guide_capture_complete = False
                source_parts = _evaluated_mesh_objects(
                    source_object,
                    state,
                    include_system_ao=(
                        ao_configuration['evaluation_mode'] == 'PER_SYSTEM'
                    ),
                    ao_settings=ao_configuration,
                )
                parts.extend(source_parts)
                shape_source_parts.append((source_object, source_parts))

            if not parts:
                continue

            shapes_baked = _bake_group_shape_keys(shape_source_parts)

            # Stamp disposable parts before joining, which zero-fills missing
            # SystemColor and otherwise loses the distinction from authored black.
            for part in parts:
                _stamp_system_color_presence(part.data)

            # Only actually generated card meshes are joined. AO evaluation
            # order is the explicit per-asset mode stored on the export Empty.
            temporary_object = _join_objects(parts)
            asset_name = asset_parent.name if asset_parent != asset_sources[0] else asset_sources[0].name
            temporary_object.name = f'{asset_name}__S2U_HAIR'
            temporary_object.data.name = f'{asset_name}__S2U_HAIR'
            temporary_object[SOURCE_NAME_PROPERTY] = asset_name
            temporary_object[TEMP_PROPERTY] = True
            _copy_transfer_settings(temporary_object, asset_sources)
            if shapes_baked:
                temporary_object.ue_unique_transfer_shape_keys = False
                temporary_object['_vdt_shape_transfer_completed'] = True
            _link_to_export_collection(temporary_object, export_collection)
            state['temporary_object_names'].add(temporary_object.name)

            # Keep the export copy in the same hierarchy as the source asset.
            if asset_parent != asset_sources[0]:
                world_matrix = temporary_object.matrix_world.copy()
                temporary_object.parent = asset_parent
                temporary_object.parent_type = 'OBJECT'
                temporary_object.matrix_parent_inverse = (
                    asset_parent.matrix_world.inverted()
                )
                temporary_object.matrix_world = world_matrix

            if ao_configuration['evaluation_mode'] == 'COMBINED':
                _evaluate_combined_ao(
                    temporary_object,
                    state,
                    ao_settings=ao_configuration,
                )
            else:
                _preserve_per_system_ao(temporary_object, state)
            temporary_object['_htue_ao_evaluation_mode'] = ao_configuration[
                'evaluation_mode'
            ]
            _write_hair_tool_uvs(temporary_object.data)
            _pack_rfaos(temporary_object.data)
            _remove_empty_material_slots(temporary_object)

            armatures = {
                armature
                for armature in (
                    (hair_guide_cloth.resolve_export_armature(source) if guide_cloth_enabled else _get_armature(source))
                    for source in asset_sources
                )
                if armature
            }
            if len(armatures) > 1:
                raise RuntimeError(
                    f'Hair Tool asset "{asset_name}" is bound to multiple armatures.'
                )
            armature_object = next(iter(armatures), None)
            if armature_object:
                _bind_export_skin(temporary_object, armature_object)

            if guide_capture_complete:
                try:
                    hair_guide_cloth.finish_asset(
                        temporary_object, guide_captures, asset_name,
                        armature_object, export_collection, state,
                    )
                except Exception as error:
                    # Optional cloth preparation never replaces ordinary export.
                    hair_guide_cloth.diagnostic(asset_name, str(error))

    except Exception:
        cleanup()
        raise


def cleanup():
    """Remove temporary Hair Tool export meshes without touching source systems."""
    state = bpy.app.driver_namespace.pop(STATE_KEY, None)
    if not state:
        return

    for object_name in state.get('temporary_object_names', set()):
        scene_object = bpy.data.objects.get(object_name)
        if not scene_object:
            continue
        mesh = scene_object.data if scene_object.type == 'MESH' else None
        bpy.data.objects.remove(scene_object, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    for mesh_name in state.get('temporary_mesh_names', set()):
        mesh = bpy.data.meshes.get(mesh_name)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def restore_bridge_previews():
    """Restore display-only previews after Send2UE restores its captured context."""
    restored = []
    height_preview_states = bpy.app.driver_namespace.pop(
        MATERIAL_HEIGHT_PREVIEW_RESTORE_KEY, None
    )
    if height_preview_states is not None:
        try:
            from hair_tool_unreal_bridge import layerblend_preview

            restored.extend(
                layerblend_preview.restore_height_previews(height_preview_states)
            )
        except ImportError:
            pass

    preview_states = bpy.app.driver_namespace.pop(PREVIEW_RESTORE_KEY, None)
    if not preview_states:
        return restored
    try:
        from hair_tool_unreal_bridge import deformer_sync
    except ImportError:
        return restored

    for preview_state in preview_states:
        root = bpy.data.objects.get(preview_state.get('root_name', ''))
        if root is None:
            continue
        for source_state in preview_state.get('source_states', []):
            if isinstance(source_state, str):
                source_state = {'name': source_state}
            source = bpy.data.objects.get(str(source_state.get('name', '')))
            if source is None:
                continue
            source.hide_set(bool(source_state.get('hidden', False)))
            source.hide_render = bool(source_state.get('hide_render', False))
        bpy.context.view_layer.update()
        try:
            result = deformer_sync.build_combined_ao_preview(None, root)
            restored.append(result['preview'].name)
        except Exception as error:
            _warn(
                f'Could not restore the AO preview for "{root.name}": {error}. '
                'The live Hair Tool sources remain available.'
            )
    return restored


def get_asset_source_name(mesh_object):
    return mesh_object.get(SOURCE_NAME_PROPERTY, mesh_object.name)
