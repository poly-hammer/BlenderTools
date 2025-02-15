# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
import os
from send2ue.core.extension import ExtensionBase
from send2ue.core import utilities
from send2ue.constants import ToolInfo, UnrealTypes


class UseCollectionsAsFoldersExtension(ExtensionBase):
    name = 'use_collections_as_folders'
    use_collections_as_folders: bpy.props.BoolProperty(
        name="Use collections as folders",
        default=False,
        description=(
            "This uses the collection hierarchy in your scene as sub folders from "
            "the specified mesh folder in your unreal project"
        )
    )
    
    def pre_animation_export(self, asset_data, properties):
        """
        Defines the pre animation export logic that uses blender collections as unreal folders
        
        :param dict asset_data: A mutable dictionary of asset data for the current asset.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        """
        if self.use_collections_as_folders:
            asset_type = asset_data.get('_asset_type')
            if asset_type and asset_type in [UnrealTypes.ANIM_SEQUENCE]:
                action_name = asset_data.get('_action_name')
                if action_name:
                    action = bpy.data.actions.get(action_name)
                    
                    if not action:
                        print(f"Couldnt find action '{action_name}'.")
                        return
                        
					# Grab armature object to then grab the collection from, as we can't grab it from the action itself
                    armature_object_name = asset_data['_armature_object_name']
                    armature_object = bpy.data.objects.get(armature_object_name)
                    if not armature_object:
                        print(f"Couldnt find armature '{armature_object_name}'.")
                        return
                    
                    asset_name = utilities.get_asset_name(action_name, properties)
                    
                    _, file_extension = os.path.splitext(asset_data.get('file_path'))
                    export_path = self.get_full_export_path(properties, UnrealTypes.ANIM_SEQUENCE, armature_object)
                    file_name_with_extension = f'{asset_name}{file_extension}'
                    
                    file_path = os.path.join(export_path, file_name_with_extension)
                    
                    self.update_asset_data({
                        'file_path': file_path
                    })

    def pre_mesh_export(self, asset_data, properties):
        """
        Defines the pre mesh export logic that uses blender collections as unreal folders

        :param dict asset_data: A mutable dictionary of asset data for the current asset.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        """
        if self.use_collections_as_folders:
            asset_type = asset_data.get('_asset_type')
            if asset_type and asset_type in [UnrealTypes.STATIC_MESH, UnrealTypes.SKELETAL_MESH]:
                object_name = asset_data.get('_mesh_object_name')
                if object_name:
                    scene_object = bpy.data.objects.get(object_name)

                    # if the combined assets option is on, then the name could be the empty
                    empty_object_name = asset_data.get('empty_object_name')
                    if empty_object_name:
                        object_name = empty_object_name

                    asset_name = utilities.get_asset_name(object_name, properties)
                    mesh_asset_type = utilities.get_mesh_unreal_type(scene_object)

                    _, file_extension = os.path.splitext(asset_data.get('file_path'))
                    export_path = self.get_full_export_path(properties, mesh_asset_type, scene_object)
                    file_name_with_extension = f'{asset_name}{file_extension}'
                    self.update_asset_data({
                        'file_path': os.path.join(export_path, file_name_with_extension)
                    })

    def pre_groom_export(self, asset_data, properties):
        """
        Defines the pre groom export logic that uses blender collections as unreal folders

        :param dict asset_data: A mutable dictionary of asset data for the current asset.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        """

        if self.use_collections_as_folders:
            asset_type = asset_data.get('_asset_type')
            if asset_type and asset_type in [UnrealTypes.GROOM]:
                object_name = asset_data.get('_object_name')
                if object_name:
                    scene_object = bpy.data.objects.get(object_name)
                    particle_object_name = asset_data.get('_particle_object_name')
                    if particle_object_name:
                        scene_object = utilities.get_mesh_object_for_groom_name(object_name)

                    asset_name = utilities.get_asset_name(object_name, properties)

                    _, file_extension = os.path.splitext(asset_data.get('file_path'))
                    export_path = self.get_full_export_path(properties, asset_type, scene_object)
                    file_name_with_extension = f'{asset_name}{file_extension}'
                    file_path = os.path.join(export_path, file_name_with_extension)
                    self.update_asset_data({
                        'file_path': file_path
                    })
        


    def get_full_export_path(self, properties, asset_type, scene_object):
        """
        Gets the unreal export path when use_collections_as_folders extension is active.

        :param object properties: The property group that contains variables that maintain the addon's correct state.
        :param str asset_type: The unreal type of asset.
        :param object scene_object: A object.
        :return str: The full export path for the given asset.
        """
        game_path = utilities.get_export_folder_path(properties, asset_type)
        sub_path = self.get_collections_as_path(scene_object, properties)
        export_path = os.path.join(game_path, sub_path)
        return export_path

    def pre_import(self, asset_data, properties):
        """
        Defines the pre import logic that uses blender collections as unreal folders

        :param dict asset_data: A mutable dictionary of asset data for the current asset.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        """
        if self.use_collections_as_folders:
            asset_type = asset_data.get('_asset_type')
            if asset_type and asset_type == UnrealTypes.ANIM_SEQUENCE:
                action_name = asset_data.get('_action_name')
                if action_name:
                    asset_name = utilities.get_asset_name(action_name, properties)
                    
                    armature_object_name = asset_data['_armature_object_name']
                    armature_object = bpy.data.objects.get(armature_object_name)
                
                    import_path = self.get_full_import_path(properties, UnrealTypes.ANIM_SEQUENCE, armature_object)

                    self.update_asset_data({
                        # update skeletal asset path now that it is under new collections path
                        'skeleton_asset_path': utilities.get_skeleton_asset_path(
                            armature_object,
                            properties,
                            self.get_full_import_path,
                            armature_object,
                        ),
                        # update asset folder and path to the new path based on the collections
                        'asset_folder': import_path,
                        'asset_path': f'{import_path}{asset_name}'
                    })
            elif asset_type and asset_type == UnrealTypes.GROOM:
                object_name = asset_data.get('_object_name')
                if object_name:
                    scene_object = bpy.data.objects.get(object_name)
                    particle_object_name = asset_data.get('_particle_object_name')
                    if particle_object_name:
                        scene_object = utilities.get_mesh_object_for_groom_name(object_name)

                    asset_name = utilities.get_asset_name(object_name, properties)
                    import_path = self.get_full_import_path(properties, asset_type, scene_object)
                    self.update_asset_data({
                        'asset_folder': import_path,
                        'asset_path': f'{import_path}{asset_name}'
                    })
            elif asset_type:
                object_name = asset_data.get('_mesh_object_name')
                if object_name:
                    scene_object = bpy.data.objects.get(object_name)
                    asset_name = utilities.get_asset_name(object_name, properties)
                    mesh_asset_type = utilities.get_mesh_unreal_type(scene_object)
                    # get import path when using blender collections as folders
                    import_path = self.get_full_import_path(properties, mesh_asset_type, scene_object)

                    if asset_type == UnrealTypes.GROOM:
                        # correct the target mesh path for groom asset data
                        self.update_asset_data({
                            'mesh_asset_path': f'{import_path}{asset_name}'
                        })
                    else:
                        self.update_asset_data({
                            'asset_folder': import_path,
                            'asset_path': f'{import_path}{asset_name}'
                        })

    def get_full_import_path(self, properties, asset_type, scene_object):
        """
        Gets the unreal import path when use_collections_as_folders extension is active.

        :param object properties: The property group that contains variables that maintain the addon's correct state.
        :param str asset_type: The unreal type of asset.
        :param object scene_object: A object.
        :return str: The full import path for the given asset.
        """
        game_path = utilities.get_import_path(properties, asset_type)
        sub_path = self.get_collections_as_path(scene_object, properties)
        if sub_path:
            import_path = f'{game_path}{sub_path}/'
        else:
            import_path = f'{game_path}'
        return import_path

    def get_collections_as_path(self, scene_object, properties):
        """
        Walks the collection hierarchy till it finds the given scene object.

        :param object scene_object: A object.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        :return str: The sub path to the given scene object.
        """
        parent_names = []
        if self.use_collections_as_folders and len(scene_object.users_collection) > 0:
            parent_collection = scene_object.users_collection[0]
            parent_collection_name = utilities.get_asset_name(parent_collection.name, properties)
            if parent_collection_name != "Export":
                parent_names.append(parent_collection_name)
            if len(parent_names) > 0:    
                self.set_parent_collection_names(parent_collection, parent_names, properties)
                parent_names.reverse()
                return '/'.join(parent_names).replace(f'{ToolInfo.EXPORT_COLLECTION.value}/', '')

        return ''

    def set_parent_collection_names(self, collection, parent_names, properties):
        """
        This function recursively adds the parent collection names to the given list until.

        :param object collection: A collection.
        :param list parent_names: A list of parent collection names.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        :return list: A list of parent collection names.
        """
        for parent_collection in bpy.data.collections:
            if collection.name in parent_collection.children.keys():
                parent_collection_name = utilities.get_asset_name(parent_collection.name, properties)
                if parent_collection_name != "Export":
                    parent_names.append(parent_collection_name)
                self.set_parent_collection_names(parent_collection, parent_names, properties)
                return None

    def draw_paths(self, dialog, layout, properties):
        """
        Draws an interface for the use_collections_as_folders option under the paths tab.

        :param Send2UnrealDialog dialog: The dialog class.
        :param bpy.types.UILayout layout: The extension layout area.
        :param Send2UeSceneProperties properties: The scene property group that contains all the addon properties.
        """
        dialog.draw_property(self, layout, 'use_collections_as_folders')