"""Session-only native RNA registration QA; no phase or source operations."""

import json
import sys

import addon_utils
import bpy

if "--factory-startup" in sys.argv:
    raise RuntimeError("Use a normal Blender with an isolated temporary config")
if bpy.data.filepath:
    raise RuntimeError("Registration QA must not open a production file")
wm = bpy.ops.wm
bpy.ops.wm = wm


def forbid_preferences(*args, **kwargs):
    raise RuntimeError("Registration QA cannot save user preferences")


wm.save_userpref = forbid_preferences
baseline = set(bpy.context.preferences.addons.keys())
module = addon_utils.enable("send2ue", default_set=False, persistent=False)
if module is None:
    raise RuntimeError("Native Send2UE session registration failed")
try:
    rna = bpy.ops.wm.send2ue.get_rna_type()
    fields = ("workstation_phase_id", "workstation_deferred_handoff")
    for name in fields:
        prop = rna.properties[name]
        if prop.type != "STRING" or prop.default != "" or not prop.is_skip_save:
            raise RuntimeError("Native coordination property contract differs: " + name)
    if set(bpy.context.preferences.addons.keys()) != baseline:
        raise RuntimeError("Session addon registration changed persistent enabled addons")
    print("NATIVE_DEFERRED_RNA_QA=" + json.dumps(dict(
        fields=list(fields), default_set=False, preferences_saved=False,
        production_source_opened=False, native_phase_started=False), sort_keys=True))
finally:
    addon_utils.disable("send2ue", default_set=False)
