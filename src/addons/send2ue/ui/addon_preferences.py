# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
from pathlib import Path
from ..properties import Send2UeAddonProperties, ExtensionFolder
from ..constants import ToolInfo
from .. import __package__

def _get_context_attr(context, data_path):
    """Return the value of a context member based on its data path."""
    return context.path_resolve(data_path)

def _draw_add_remove_buttons(
    *,
    layout,
    list_path,
    active_index_path,
    list_length,
):
    """Draw the +/- buttons to add and remove list entries."""
    props = layout.operator("uilist.addon_preferences_entry_add", text="", icon='ADD')
    props.list_path = list_path
    props.active_index_path = active_index_path

    row = layout.row()
    row.enabled = list_length > 0
    props = row.operator("uilist.addon_preferences_entry_remove", text="", icon='REMOVE')
    props.list_path = list_path
    props.active_index_path = active_index_path

def draw_ui_list(
        layout,
        context,
        class_name="UI_UL_list",
        *,
        unique_id,
        list_path,
        active_index_path,
        insertion_operators=True,
        menu_class_name="",
        **kwargs,
):
    """
    This overrides the draw_ui_list function from the generic_ui_list module 
    so that we can draw the add and remove buttons for a list in the addon preferences.
    By default, the generic_ui_list module buttons link to ops that receive the scene
    context, which is not what we want in this case. So we had to create new ops that
    do this job.
    """

    row = layout.row()

    list_owner_path, list_prop_name = list_path.rsplit('.', 1)
    list_owner = _get_context_attr(context, list_owner_path)

    index_owner_path, index_prop_name = active_index_path.rsplit('.', 1)
    index_owner = _get_context_attr(context, index_owner_path)

    list_to_draw = _get_context_attr(context, list_path)

    row.template_list(
        class_name,
        unique_id,
        list_owner, list_prop_name,
        index_owner, index_prop_name,
        rows=4 if list_to_draw else 1,
        **kwargs,
    )

    col = row.column()

    if insertion_operators:
        _draw_add_remove_buttons(
            layout=col,
            list_path=list_path,
            active_index_path=active_index_path,
            list_length=len(list_to_draw),
        )
        layout.separator()

    if menu_class_name:
        col.menu(menu_class_name, icon='DOWNARROW_HLT', text="")
        col.separator()

    # Return the right-side column.
    return col


class FOLDER_UL_extension_path(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop_name):       
        row = layout.row()
        row.alert = False
        if item.folder_path and not Path(item.folder_path).exists():
            row.alert = True
        row.prop(item, "folder_path", text="", emboss=False)

class SendToUnrealPreferences(Send2UeAddonProperties, bpy.types.AddonPreferences):
    """
    This class creates the settings interface in the send to unreal addon.
    """
    bl_idname = __package__

    def draw(self, context):
        """
        This defines the draw method, which is in all Blender UI types that create interfaces.

        :param context: The context of this interface.
        """
        row = self.layout.row()
        row.prop(self, 'automatically_create_collections')
        row = self.layout.row()
        row.label(text='RPC Response Timeout')
        row.prop(self, 'rpc_response_timeout', text='')
        row = self.layout.row()

        row.label(text="Multicast TTL")
        row.prop(self, 'multicast_ttl', text='')
        row = self.layout.row()
        row.label(text="Multicast Group Endpoint")
        row.prop(self, 'multicast_group_endpoint', text='')
        row = self.layout.row()
        row.label(text="Command Endpoint")
        row.prop(self, 'command_endpoint', text='')
        row = self.layout.row()

        row.label(text='Extensions Repo Paths:')
        row = self.layout.row()
        draw_ui_list(
            row,
            context=bpy.context.preferences.addons[ToolInfo.NAME.value],
            class_name="FOLDER_UL_extension_path",
            list_path="preferences.extension_folder_list",
            active_index_path="preferences.extension_folder_list_active_index",
            unique_id="extension_folder_list_id",
            insertion_operators=True
        ) # type: ignore
        row = self.layout.row()
        row.operator('send2ue.reload_extensions', text='Reload All Extensions', icon='FILE_REFRESH')

def register():
    """
    Registers the addon preferences when the addon is enabled.
    """
    bpy.utils.register_class(ExtensionFolder)
    bpy.utils.register_class(FOLDER_UL_extension_path)
    bpy.utils.register_class(SendToUnrealPreferences)


def unregister():
    """
    Unregisters the addon preferences when the addon is disabled.
    """
    bpy.utils.unregister_class(SendToUnrealPreferences)
    bpy.utils.unregister_class(FOLDER_UL_extension_path)
    bpy.utils.unregister_class(ExtensionFolder)
