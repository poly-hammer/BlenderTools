# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
from ..core import templates
from .. import __package__

class Ue2RigifyAddonPreferences(bpy.types.AddonPreferences):
    """
    This class subclasses the AddonPreferences class to create the addon preferences interface.
    """
    bl_idname = __package__

    custom_rig_template_path: bpy.props.StringProperty(
        name='Custom Templates folder',
        description='The location where your rig templates will be stored, including default templates. Defaults to Temp folder if empty',
        subtype='DIR_PATH',
        default='',
        update=lambda self, context: templates.copy_default_templates()
    ) # type: ignore

    def draw(self, context):
        """
        This function overrides the draw method in the AddonPreferences class. The draw method is the function
        that defines the user interface layout and gets updated routinely.

        :param object context: The addon preferences context.
        """
        layout = self.layout

        layout.prop(self, 'custom_rig_template_path')
        row = layout.row()
        row.operator('ue2rigify.import_rig_template', icon='IMPORT')
        row.operator('ue2rigify.export_rig_template', icon='EXPORT')


def register():
    """
    Registers the addon preferences when the addon is enabled.
    """
    bpy.utils.register_class(Ue2RigifyAddonPreferences)


def unregister():
    """
    Unregisters the addon preferences when the addon is disabled.
    """
    
    bpy.utils.unregister_class(Ue2RigifyAddonPreferences)
