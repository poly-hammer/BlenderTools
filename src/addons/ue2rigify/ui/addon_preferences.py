# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
from ..core import templates
from .. import __package__

class Ue2RigifyAddonPreferences(bpy.types.AddonPreferences):
    """
    This class subclasses the AddonPreferences class to create the addon preferences interface.
    """
    bl_idname = __package__
    
    def get_custom_location(self):
        # create key if doesn't exist then return
        try:
            self['custom_template_path']
        except:
            self['custom_template_path'] = ''
        return self['custom_template_path']

    def set_custom_location(self, value):
        self['custom_template_path'] = value
        # Create default templates at custom_rig_template_path
        templates.copy_default_templates()

    custom_rig_template_path: bpy.props.StringProperty(
        name='Custom Templates folder',
        description='The location where your rig templates will be stored, including default templates. Defaults to Temp folder if empty',
        subtype='DIR_PATH',
        default='',
        get=get_custom_location,
        set=set_custom_location
    )

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
