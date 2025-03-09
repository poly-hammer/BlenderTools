## Patch Changes
* Added legacy fbx importer validation to the project settings validation. This should help show others that they need to change their Unreal 5.5 project settings to use this Legacy FBX importer, instead of scene interchange. Their `DefaultEngine.ini` file should contain.
```ini
[ConsoleVariables]
Interchange.FeatureFlags.Import.FBX=False
```
  * [120](https://github.com/poly-hammer/BlenderTools/pull/120)


## Special Thanks
* @jack-yao91

## Tests Passing On
* Blender `3.6`, `4.2` (installed from blender.org)
* Unreal `5.3`, `5.4`
