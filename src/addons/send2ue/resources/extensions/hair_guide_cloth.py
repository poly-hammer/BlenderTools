"""Optional generator-owned Chaos Cloth in the ordinary Send to Unreal flow."""
import json
from pathlib import Path
import sys

import bpy
from send2ue.core import hair_guide_cloth, cloth_receipt
from send2ue.core.extension import ExtensionBase
from send2ue.dependencies.unreal import run_commands

PIPELINE_FILE = (Path(__file__).resolve().parent.parent / 'pipeline' / 'ue_hair_guide_cloth.py').as_posix()
RECEIPT_PREFIX = 'SEND2UE_HAIR_GUIDE_CLOTH:'


def _run_cloth_commands(commands):
    from send2ue.dependencies import unreal as dependency, remote_execution
    if dependency._COMMAND_RECORDING_STACK:
        return run_commands(commands)
    # Compilation/baking may exceed the ordinary material command timeout.
    # Scope this to one call; never rewrite the user's global RPC preference.
    config = remote_execution.RemoteExecutionConfig()
    config.command_response_timeout = 900.0
    remote = remote_execution.RemoteExecution(config)
    try:
        remote.start()
        return dependency.run_unreal_python_commands(remote, commands)
    finally:
        remote.stop()


class HairGuideClothExtension(ExtensionBase):
    # Read final filenames after ordinary naming and grouping extensions.
    name = hair_guide_cloth.EXTENSION_NAME

    enabled: bpy.props.BoolProperty(
        name='Hair guides to Chaos Cloth', default=False,
        description='Export the actual generating guide mesh with hair cards and build a separate Chaos Cloth asset')
    simulation_mesh_mode: bpy.props.EnumProperty(
        name='Simulation guide shape',
        items=(
            ('RIBBON', 'Ribbon for circular guides',
             'Export supported circular Hair Tool guides as open ribbons, preserving their splines and paint; keep other guides unchanged'),
            ('ORIGINAL', 'Original guide',
             'Export the evaluated guide with its original profile')),
        default='RIBBON',
        description='Choose the exported simulation shape; authoring guides and render generation stay unchanged')
    cloth_template_asset_path: bpy.props.StringProperty(
        name='Cloth physics template', description='Existing Chaos Cloth asset supplying physical settings')
    body_mesh_asset_path: bpy.props.StringProperty(
        name='Body skeletal mesh', description='Existing body mesh using the exported hair armature skeleton')
    physics_asset_path: bpy.props.StringProperty(
        name='Physics asset', description='Existing collision physics asset for the hair cloth')

    def draw_export(self, dialog, layout, properties):
        box = layout.box()
        dialog.draw_property(self, box, 'enabled')
        if self.enabled:
            dialog.draw_property(self, box, 'simulation_mesh_mode')
            for key in ('cloth_template_asset_path', 'body_mesh_asset_path', 'physics_asset_path'):
                dialog.draw_property(self, box, key)
            box.label(text='Unpainted guides remain static. Artist geometry is preserved.')

    def post_mesh_export(self, asset_data, properties):
        if not self.enabled:
            return
        try:
            hair_guide_cloth.record_export(asset_data)
        except Exception as error:
            hair_guide_cloth.diagnostic(asset_data.get('asset_path', ''), str(error))
            raise RuntimeError('Hair Guide Cloth export failed: ' + str(error)) from error

    def post_import(self, asset_data, properties):
        if not self.enabled:
            return
        try:
            record = hair_guide_cloth.import_record(asset_data, properties)
            if record is None:
                return
            response = _run_cloth_commands([
                'import importlib.util, sys, json',
                '_hgc_spec = importlib.util.spec_from_file_location("send2ue_hair_guide_cloth_receiver", ' + repr(PIPELINE_FILE) + ')',
                '_hgc_runtime = importlib.util.module_from_spec(_hgc_spec)',
                'sys.modules[_hgc_spec.name] = _hgc_runtime',
                '_hgc_spec.loader.exec_module(_hgc_runtime)',
                '_hgc_receipt = _hgc_runtime.apply_hair_guide_cloth(' + repr(record) + ')',
                'print(' + repr(RECEIPT_PREFIX) + ' + json.dumps(_hgc_receipt, sort_keys=True))',
            ])
            if response:
                print('[send2ue][hair_guide_cloth] ' + str(response))
            cloth_receipt.validate_import_response(response, record)
        except Exception as error:
            # Imported meshes and the previous cloth remain recoverable, but a
            # requested cloth update must not finish as a successful operation.
            hair_guide_cloth.diagnostic(asset_data.get('asset_path', ''), str(error))
            raise RuntimeError('Hair Guide Cloth import failed: ' + str(error)) from error

    def post_operation(self, properties):
        # Keep compact diagnostics available to users after owned mesh cleanup.
        current = hair_guide_cloth.state()
        current['packages'].clear()
