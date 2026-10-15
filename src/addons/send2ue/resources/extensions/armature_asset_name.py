import os
import bpy
from send2ue.core.extension import ExtensionBase
from send2ue.constants import UnrealTypes

PROPERTY = 'send2ue_asset_name'


class ArmatureAssetNameExtension(ExtensionBase):
    """Opt-in skeletal asset name.

    With combined child meshes an armature asset is named after its first child mesh.
    An armature that carries the custom property ``send2ue_asset_name`` is exported
    under that name instead. Assets without the property are unchanged.
    """
    name = 'zzz_armature_asset_name'

    def pre_mesh_export(self, asset_data, properties):
        if asset_data.get('_asset_type') != UnrealTypes.SKELETAL_MESH:
            return
        mesh_object = bpy.data.objects.get(asset_data.get('_mesh_object_name', ''))
        rig = mesh_object.parent if mesh_object else None
        if not rig or rig.type != 'ARMATURE':
            return
        asset_name = str(rig.get(PROPERTY, '')).strip()
        if not asset_name:
            return
        path, ext = os.path.splitext(asset_data['file_path'])
        self.update_asset_data({
            'file_path': os.path.join(os.path.dirname(path), f'{asset_name}{ext}'),
            'asset_path': f"{asset_data['asset_folder']}{asset_name}",
        })
