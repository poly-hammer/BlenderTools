# Copyright Epic Games, Inc. All Rights Reserved.

import bpy
import os
import importlib
from . import operators, properties, constants
from .dependencies import remote_execution, unreal
from .ui import header_menu, addon_preferences, file_browser, dialog
from .core import formatting, validations, settings, utilities, export, ingest, extension, io

bl_info = {
    "name": "Send to Unreal",
    "author": "Epic Games Inc (now a community fork)",
    "version": (2, 6, 6),
    "blender": (3, 6, 0),
    "location": "Header > Pipeline > Send to Unreal",
    "description": "Sends an asset to the first open Unreal Editor instance on your machine.",
    "warning": "",
    "wiki_url": "https://poly-hammer.github.io/BlenderTools/send2ue",
    "category": "Pipeline",
}

modules = [
    export,
    ingest,
    settings,
    unreal,
    utilities,
    formatting,
    validations,
    dialog,
    file_browser,
    operators,
    properties,
    constants,
    remote_execution,
    addon_preferences,
    extension,
    io.fbx_b3,
    io.fbx_b4
]


def register():
    """
    Registers the addon classes when the addon is enabled.
    """
    # reload the submodules
    if os.environ.get('SEND2UE_DEV'):
        for module in modules:
            importlib.reload(module)

    try:
        # register the properties
        properties.register()

        # register the operators
        operators.register()

        # register the header menu
        header_menu.register()

        # register the addon preferences
        addon_preferences.register()

    except RuntimeError as error:
        print(error)

    # add an event handler that will run on new file loads
    bpy.app.handlers.load_post.append(bpy.app.handlers.persistent(utilities.setup_project))

    # add a function to the event timer that will fire after the addon is enabled
    bpy.app.timers.register(utilities.addon_enabled, first_interval=0.1)


def unregister():
    """
    Unregisters the addon classes when the addon is disabled.
    """
    # remove event handlers
    if utilities.setup_project in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(utilities.setup_project)

    try:
        # remove the pipeline menu
        header_menu.remove_parent_menu()

        # unregister the header menu
        header_menu.unregister()

        # register the addon preferences
        addon_preferences.unregister()

        # unregister the operators
        operators.unregister()

        # unregister the properties
        properties.unregister()

    except RuntimeError as error:
        print(error)

    # remove the temp data
    utilities.remove_temp_data()
