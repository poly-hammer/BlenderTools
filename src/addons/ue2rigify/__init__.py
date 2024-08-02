# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
import os
import importlib
from . import properties, operators, constants
from .settings import tool_tips, viewport_settings
from .core import scene, nodes, templates, utilities, validations
from .ui import view_3d, node_editor, addon_preferences, exporter


bl_info = {
    "name": "UE to Rigify",
    "author": "Epic Games Inc (now a community fork)",
    "description": "Allows you to drive a given rig and its animations with a Rigify rig.",
    "version": (1, 7, 1),
    "blender": (3, 6, 0),
    "location": "3D View > Tools > UE to Rigify",
    "wiki_url": "https://poly-hammer.github.io/BlenderTools/ue2rigify",
    "warning": "",
    "category": "Pipeline"
}


modules = [
    constants,
    scene,
    nodes,
    view_3d,
    exporter,
    tool_tips,
    utilities,
    templates,
    operators,
    properties,
    node_editor,
    addon_preferences,
    viewport_settings,
    validations
]


def register():
    """
    Registers the addon classes when the addon is enabled.
    """

    # reload the submodules
    if os.environ.get('UE2RIGIFY_DEV'):
        for module in modules:
            importlib.reload(module)

    properties.register()
    addon_preferences.register()
    view_3d.register()
    operators.register()
    nodes.register()

    templates.copy_default_templates()


def unregister():
    """
    Unregisters the addon classes when the addon is disabled.
    """
    try:
        nodes.remove_pie_menu_hot_keys()
        node_editor.unregister()
        nodes.unregister()
        operators.unregister()
        view_3d.unregister()
        addon_preferences.unregister()
        properties.unregister()
    except Exception as e:
        print(f"Error un-registering UE2Rigify: \n{e}")

