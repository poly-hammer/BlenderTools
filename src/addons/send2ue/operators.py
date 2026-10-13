# Copyright Epic Games, Inc. All Rights Reserved.

import os
import bpy
import queue
import threading
from .constants import ToolInfo, ExtensionTasks
from .core import (
    export,
    utilities,
    settings,
    validations,
    extension,
    hair_tool_export,
    armature_modifier_fix,
    preview_modifier_guard,
    grouppro_export,
)
from .ui import file_browser, dialog, addon_preferences
from .dependencies import unreal
from . import coordination
from .dependencies.rpc import blender_server
from .properties import register_scene_properties, unregister_scene_properties
from . import __package__ as base_package


class Send2Ue(bpy.types.Operator):
    """Push your assets to disk and/or an open unreal editor instance"""
    bl_idname = "wm.send2ue"
    bl_label = "Push Assets"
    workstation_phase_id: bpy.props.StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})
    workstation_deferred_handoff: bpy.props.StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timer = None
        self.escape = False
        self.done = False
        self.max_step = 0
        self.state = {}
        self._wq_operation = None

        # add execution queue
        execution_queue = bpy.app.driver_namespace.get(ToolInfo.EXECUTION_QUEUE.value)
        if not execution_queue:
            bpy.app.driver_namespace[ToolInfo.EXECUTION_QUEUE.value] = queue.Queue()
        self.execution_queue = bpy.app.driver_namespace[ToolInfo.EXECUTION_QUEUE.value]

    @staticmethod
    def draw_progress(self, context):
        self.layout.prop(context.window_manager.send2ue, 'progress')

    def modal(self, context, event):
        if not self.done and context.area is not None:
            context.area.tag_redraw()

        if self.execution_queue.empty():
            self.done = True

        if event.type == 'ESC':
            self.escape = True
            operation = getattr(self, '_wq_operation', None)
            if operation is not None:
                operation.fail('Native Send2UE was cancelled; confirm the editor stopped before releasing the work phase')
                self._wq_operation = None
            # clears the queue in a thread safe manner
            with self.execution_queue.mutex:
                self.execution_queue.queue.clear()

        if event.type == 'TIMER':
            operation = getattr(self, '_wq_operation', None)
            if operation is not None:
                try:
                    operation.check()
                    operation.heartbeat()
                except Exception as error:
                    self.escape_operation(context, error=error)
                    self.report({'ERROR'}, str(error))
                    return {'CANCELLED'}
            if not self.execution_queue.empty():
                try:
                    function, args, kwargs, message, asset_id, attribute = self.execution_queue.get()
                    step = self.max_step - self.execution_queue.qsize()
                    context.window_manager.send2ue.progress = min((step / self.max_step) * 100, 99)
                    utilities.refresh_all_areas()

                    # set the current asset id
                    context.window_manager.send2ue.asset_id = asset_id

                    # get the description
                    file_name = context.window_manager.send2ue.asset_data[asset_id].get(attribute)
                    description = message.format(
                        attribute=utilities.get_asset_name_from_file_name(file_name)
                    )
                    bpy.context.workspace.status_text_set_internal(description)

                    # run the function
                    function(*args, **kwargs)
                    if operation is not None and operation.deferred:
                        operation.exported_count += 1
                except Exception as error:
                    self.escape_operation(context, error=error)
                    if os.environ.get('SEND2UE_DEV'):
                        raise
                    self.report({'ERROR'}, str(error))
                    return {'CANCELLED'}

            if self.escape:
                bpy.types.STATUSBAR_HT_header.remove(self.draw_progress)
                context.window_manager.event_timer_remove(self.timer)
                bpy.context.workspace.status_text_set_internal(None)
                try:
                    # Cancellation cleanup must not launch more remote mutations.
                    operation = getattr(self, '_wq_operation', None)
                    if ((operation is None and getattr(self, 'workstation_phase_id', ''))
                            or (operation is not None and operation.deferred)):
                        with unreal.record_commands():
                            self.post_operation()
                    else:
                        self.post_operation()
                    self._complete_coordinated()
                except Exception as error:
                    self._fail_coordinated(error)
                    self.report({'ERROR'}, str(error))
                    return {'CANCELLED'}
                return {'FINISHED'}

            if self.done:
                context.window_manager.send2ue.progress = 100
                bpy.context.workspace.status_text_set_internal('Finished!')
                bpy.context.window_manager.progress_end()
                self.escape = True
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        try:
            self._begin_coordinated(context)
            connected = utilities.is_unreal_connected()
        except Exception as error:
            self._fail_coordinated(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        if connected:
            properties = bpy.context.scene.send2ue
            # run the full send to unreal operation which queues all the jobs
            try:
                self.pre_operation()
                self.execution_queue.queue.clear()
                context.window_manager.send2ue.progress = 0
                bpy.context.workspace.status_text_set_internal('Validating...')
                export.send2ue(properties)
                self._validate_coordinated_jobs(properties)
            # if validations fail
            except Exception as error:
                self.escape_operation(context, error=error)
                # if in dev mode raise the error instead of reporting it
                if os.environ.get('SEND2UE_DEV'):
                    raise error

                self.report({'ERROR'}, str(error))
                return {'CANCELLED'}

            self.max_step = self.execution_queue.qsize()

            # start a timer in the operators modal that processes the queued jobs
            context.window_manager.modal_handler_add(self)
            self.timer = context.window_manager.event_timer_add(0.01, window=context.window)
            bpy.types.STATUSBAR_HT_header.prepend(self.draw_progress)

            return {'RUNNING_MODAL'}
        else:
            self._fail_coordinated('Unreal connection was unavailable; native Send2UE did not complete')
            return {'CANCELLED'}

    def execute(self, context):
        try:
            self._begin_coordinated(context)
            connected = utilities.is_unreal_connected()
        except BaseException as error:
            self._fail_coordinated(error)
            raise
        if connected:
            properties = bpy.context.scene.send2ue
            try:
                self.pre_operation()
                self.execution_queue.queue.clear()
                export.send2ue(properties)
                self._validate_coordinated_jobs(properties)
                while not self.execution_queue.empty():
                    operation = getattr(self, '_wq_operation', None)
                    if operation is not None:
                        operation.check()
                        operation.heartbeat()
                    function, args, kwargs, message, asset_id, attribute = self.execution_queue.get()
                    context.window_manager.send2ue.asset_id = asset_id
                    function(*args, **kwargs)
                    if operation is not None and operation.deferred:
                        operation.exported_count += 1
                operation = getattr(self, '_wq_operation', None)
                if operation is not None and operation.deferred:
                    # Batch owns the later RPC import and durable dependencies.
                    # Run local cleanup/hooks without dispatching remote saves.
                    with unreal.record_commands():
                        self.post_operation()
                else:
                    self.post_operation()
                self._complete_coordinated()
            except BaseException as error:
                self.escape_operation(context, error=error)
                raise
        else:
            self._fail_coordinated('Unreal connection was unavailable; native Send2UE did not complete')
            return {'CANCELLED'}
        return {'FINISHED'}

    def _begin_coordinated(self, context):
        self._wq_operation = coordination.begin_operation(
            getattr(self, 'workstation_phase_id', ''), bpy.app.driver_namespace,
            str(bpy.data.filepath or ''),
            deferred_handoff=getattr(self, 'workstation_deferred_handoff', ''),
        )
        coordination.require_preparation_scopes(bpy.context.scene.send2ue)

    def _validate_coordinated_jobs(self, properties):
        if getattr(self, '_wq_operation', None) is not None:
            if self._wq_operation.deferred:
                with self.execution_queue.mutex:
                    jobs = list(self.execution_queue.queue)
                coordination.validate_export_jobs(self._wq_operation, jobs, properties.path_mode)
                return
            if properties.path_mode == 'send_to_disk' or self.execution_queue.empty():
                raise RuntimeError('The coordinated Send2UE request produced no native Unreal import jobs')

    def _complete_coordinated(self):
        operation = getattr(self, '_wq_operation', None)
        if operation is not None:
            if operation.deferred:
                operation.finish_export(bpy.context.window_manager.send2ue.asset_data)
                self._wq_operation = None
                return
            if not getattr(operation, 'imported_count', 0):
                raise RuntimeError('No native Unreal import result was verified for this coordinated request')
            operation.complete()
            self._wq_operation = None

    def _fail_coordinated(self, note):
        operation = getattr(self, '_wq_operation', None)
        if operation is not None:
            try:
                operation.fail(note)
            finally:
                self._wq_operation = None

    def escape_operation(self, context, error=None):
        self.escape = True
        self.done = True
        with self.execution_queue.mutex:
            self.execution_queue.queue.clear()
        if self.timer:
            bpy.types.STATUSBAR_HT_header.remove(self.draw_progress)
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        bpy.context.workspace.status_text_set_internal(None)
        context.window_manager.progress_end()
        if isinstance(error, (ConnectionError, TimeoutError)) or getattr(self, '_wq_operation', None) is not None:
            # Run local extension cleanup, but do not reconnect to a lost editor
            # for post-operation saves. Recorded commands are deliberately discarded.
            try:
                with unreal.record_commands():
                    self.post_operation()
            finally:
                self._fail_coordinated(error or 'Native Send2UE did not complete')
        else:
            self.post_operation()
        return {'FINISHED'}

    def pre_operation(self):
        hair_tool_export.restore_debug_view()
        # get the current state of the scene and its objects
        self.state['context'] = utilities.get_current_context()
        from .core import export_selection
        export_selection.prepare()

        # Expose the current contents of open GroupPro groups before extensions
        # inspect Export. The exact editable state is restored during cleanup.
        grouppro_export.prepare_edits(bpy.context.scene.send2ue)

        # unpack the textures for export if needed
        self.state['unpacked_files'] = utilities.unpack_textures()

        # sets the current frame to 0
        bpy.context.scene.frame_current = 0

        # run the pre export extensions
        extension.run_extension_tasks(ExtensionTasks.PRE_OPERATION.value)

        # Convert live Hair Tool systems to export-only mesh copies. These temporary
        # objects carry a single RFAOS color layer and optional head-bone skinning.
        hair_tool_export.prepare()

        # Give meshes that live inside an armature but lack an armature modifier a
        # temporary binding, so they export with valid skin weights instead of
        # crashing Unreal's importer.
        armature_modifier_fix.prepare()

        # Viewport-only Geometry Nodes previews (for example Unreal height
        # displacement indicators) must never be baked into the exported mesh.
        preview_modifier_guard.prepare()

        # GroupPro Empty geometry needs Mesh objects for validation, material
        # discovery and FBX selection. These native groups are temporary.
        grouppro_export.prepare(bpy.context.scene.send2ue)

    def post_operation(self):
        grouppro_export.cleanup()

        # Restore preview visibility before returning the user's scene state.
        preview_modifier_guard.cleanup()

        # Remove the temporary armature bindings added for un-skinned child meshes.
        armature_modifier_fix.cleanup()

        # Remove export-only Hair Tool meshes before restoring the user's context.
        hair_tool_export.cleanup()

        try:
            operation = getattr(self, '_wq_operation', None)
            if operation is not None and not unreal._COMMAND_RECORDING_STACK:
                operation.check()
                for asset_data in bpy.context.window_manager.send2ue.asset_data.values():
                    coordination.require_asset_scopes(asset_data, bpy.context.scene.send2ue)
            # This extension stage can save generated Skeleton dependencies.
            extension.run_extension_tasks(ExtensionTasks.POST_OPERATION.value)
        finally:
            # Restore local Blender state even if the final Unreal save failed.
            utilities.remove_unpacked_files(self.state.get('unpacked_files', {}))
            from .core import export_selection
            export_selection.cleanup()
            utilities.set_context(self.state.get('context', {}))
            hair_tool_export.restore_bridge_previews()


class SettingsDialog(bpy.types.Operator, dialog.Send2UnrealDialog):
    """Open the settings dialog to modify the tool properties"""
    bl_idname = "wm.settings_dialog"
    bl_label = "Settings Dialog"

    def execute(self, context):
        properties = bpy.context.scene.send2ue
        export.send2ue(properties)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_popup(self, width=600)


class ImportAsset(bpy.types.Operator, file_browser.ImportAsset):
    """Import a file that came from unreal"""
    bl_idname = "wm.import_asset"
    bl_label = "Import Asset"
    filename_ext = ".fbx"

    def execute(self, context):
        properties = bpy.context.scene.send2ue
        validation_manager = validations.ValidationManager(properties)
        validation_manager.validate_scene_scale()
        utilities.import_asset(self.filepath, bpy.context.window_manager.send2ue)
        return {'FINISHED'}


class CreatePredefinedCollections(bpy.types.Operator):
    """Creates the pre-defined collection 'Export' that is needed to collect asset data."""
    bl_idname = "send2ue.create_predefined_collections"
    bl_label = "Create Pre-defined Collections"

    def execute(self, context):
        utilities.create_collections()
        return {'FINISHED'}


class SaveTemplate(bpy.types.Operator, file_browser.ExportTemplate):
    """Saves the current state of the properties to the specified template file"""
    bl_idname = 'send2ue.save_template'
    bl_label = 'Save Template'
    filename_ext = '.json'

    def execute(self, context):
        settings.save_template(self.filepath)
        return {'FINISHED'}


class LoadTemplate(bpy.types.Operator, file_browser.ImportTemplate):
    """Loads the specified template file into the template folder location"""
    bl_idname = 'send2ue.load_template'
    bl_label = 'Load Template'
    filename_ext = '.json'

    def execute(self, context):
        settings.load_template(self.filepath)
        return {'FINISHED'}


class RemoveTemplate(bpy.types.Operator):
    """Remove the selected settings template"""
    bl_idname = "send2ue.remove_template"
    bl_label = "Delete this template?"

    def execute(self, context):
        properties = bpy.context.scene.send2ue
        settings.remove_template(properties)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_confirm(self, event)


class ReloadExtensions(bpy.types.Operator):
    """Reload the extensions files"""
    bl_idname = "send2ue.reload_extensions"
    bl_label = "Reload Extensions"

    def execute(self, context):
        addon = bpy.context.preferences.addons.get(base_package)
        if addon:
            for extension_folder in addon.preferences.extension_folder_list: # type: ignore
                if extension_folder.folder_path:
                    if not os.path.exists(extension_folder.folder_path) or not os.path.isdir(
                        extension_folder.folder_path
                    ):
                        self.report(f'"{extension_folder.folder_path}" is not a folder path on disk.')
                        return {'FINISHED'}

        extension_factory = extension.ExtensionFactory()

        # remove the extension operators
        extension_factory.remove_utility_operators()

        # remove the properties
        unregister_scene_properties()

        # re-create the utility operators
        extension_factory.create_utility_operators()

        # re-create the properties again
        register_scene_properties()

        return {'FINISHED'}


class StartRPCServers(bpy.types.Operator):
    """Bootstraps unreal and blender with rpc server threads, so that they are ready for remote calls."""
    bl_idname = 'send2ue.start_rpc_servers'
    bl_label = 'Start RPC Servers'

    def execute(self, context):
        # ensure the open unreal editor is bootstrapped with the rpc server
        utilities.is_unreal_connected()

        # start the blender rpc server if its not already running
        if 'BlenderRPCServer' not in [thread.name for thread in threading.enumerate()]:
            rpc_server = blender_server.RPCServer()
            rpc_server.start(threaded=True)

        return {'FINISHED'}


class NullOperator(bpy.types.Operator):
    """This is an operator that changes nothing, but it used to clear the undo stack"""
    bl_idname = 'send2ue.null_operator'
    bl_label = 'Null Operator'

    def execute(self, context):
        return {'FINISHED'}
    

class GenericUIListOperator:
    """Mix-in class containing functionality shared by operators
    that deal with managing Blender list entries."""
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    list_path: bpy.props.StringProperty() # type: ignore
    active_index_path: bpy.props.StringProperty() # type: ignore

    def get_list(self, context):
        return addon_preferences.get_context_attr(context, self.list_path)

    def get_active_index(self, context):
        return addon_preferences.get_context_attr(context, self.active_index_path)

    def set_active_index(self, context, index):
        addon_preferences.set_context_attr(context, self.active_index_path, index)
    

class UILIST_ADDON_PREFERENCES_OT_entry_remove(GenericUIListOperator, bpy.types.Operator):
    """Remove the selected entry from the list"""

    bl_idname = "uilist.addon_preferences_entry_remove"
    bl_label = "Remove Selected Entry"

    def execute(self, context):
        addon_preferences = context.preferences.addons[ToolInfo.NAME.value]
        my_list = self.get_list(addon_preferences)
        active_index = self.get_active_index(addon_preferences)

        my_list.remove(active_index)
        to_index = min(active_index, len(my_list) - 1)
        self.set_active_index(addon_preferences, to_index)

        return {'FINISHED'}


class UILIST_ADDON_PREFERENCES_OT_entry_add(GenericUIListOperator, bpy.types.Operator):
    """Add an entry to the list after the current active item"""

    bl_idname = "uilist.addon_preferences_entry_add"
    bl_label = "Add Entry"

    def execute(self, context):
        addon_preferences = context.preferences.addons[ToolInfo.NAME.value]
        my_list = self.get_list(addon_preferences)
        active_index = self.get_active_index(addon_preferences)

        to_index = min(len(my_list), active_index + 1)

        my_list.add()
        my_list.move(len(my_list) - 1, to_index)
        self.set_active_index(addon_preferences, to_index)

        return {'FINISHED'}


operator_classes = [
    Send2Ue,
    SettingsDialog,
    ImportAsset,
    CreatePredefinedCollections,
    RemoveTemplate,
    SaveTemplate,
    LoadTemplate,
    ReloadExtensions,
    StartRPCServers,
    NullOperator,
    UILIST_ADDON_PREFERENCES_OT_entry_remove,
    UILIST_ADDON_PREFERENCES_OT_entry_add,
]


def register():
    """
    Registers the operators.
    """
    for operator_class in operator_classes:
        if not utilities.get_operator_class_by_bl_idname(operator_class.bl_idname):
            bpy.utils.register_class(operator_class)

    # register the extension utility operators
    extension_factory = extension.ExtensionFactory()
    extension_factory.create_utility_operators()


def unregister():
    """
    Unregisters the operators.
    """
    # remove the extension operators
    extension_factory = extension.ExtensionFactory()
    extension_factory.remove_utility_operators()

    # unregister the classes
    for operator_class in operator_classes:
        if utilities.get_operator_class_by_bl_idname(operator_class.bl_idname):
            bpy.utils.unregister_class(operator_class)
