# Errors

### Could not find an open Unreal Editor instance!
This happens when remote execution is not working with your project. To fix this go through all the steps
in the quickstart again. If that doesn't work the issue is most likely due to a
networking issue. Check your systems firewall for maya and python. Also check the port `6766` on your computer to see
if it is blocked by another application.

### You do not have a collection "Export" in your outliner. Please create it.
You will receive this error if you do not have an "Export" set in your outliner. To fix this go to
`Pipeline > Utilities > Create Pre-Defined Collections`.

### NoOptionError: No option 'r.skincache.compileshaders'
The option "Support Compute Skin Cache" is required for groom imports. If you have this project setting
on but consistently receive this error, consider turning off the "Check project settings" validation in
send to unreal validations settings.

### Blender 5: `Action` has no attribute `fcurves`

Blender 5 stores animation curves in action slot channelbags. Send to Unreal
reads the rig's active action or NLA slot so that a shared action cannot mix
different objects' custom property or placement curves. Legacy action curves
remain supported. Turning off custom property export skips the custom curve
read entirely.

### Python 3.13: RPC `SyntaxError` inside docstring text

Newer Python versions normalize compiled docstrings. Remote callable source
generation removes the docstring syntax node instead of matching `__doc__`
against the original source. String values in the function body are preserved.

### Running the compatibility regression checks

From the repository root, the RPC checks run without Blender or Unreal:

```shell
python -m unittest discover -s tests -p test_rpc_factory.py -v
```

The action ownership checks run inside Blender 5:

```shell
blender --background --factory-startup --python tests/test_send2ue_action_slots.py
```

These checks cover the patched functions. They complement the existing
Blender/Unreal integration suite; they do not replace the supported-version
test matrix. Unreal 5.5 and later must use the legacy FBX importer for this
add-on (`Interchange.FeatureFlags.Import.FBX False`).
