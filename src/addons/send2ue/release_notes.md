## Bug Fixes
* Fixed combined mesh collision transforms not matching visual mesh positions
  * [188](https://github.com/poly-hammer/BlenderTools/pull/188)
* Fixed combined mesh visual mesh transforms when using object origin centering
* Fixed distortion of rotated instanced meshes when parent EMPTY has display transforms
* Added EMPTY type check to prevent non-EMPTY parents from entering combined mesh path
* Fixed crash when child objects exist outside the current View Layer
* Fixed non-export-collection children being included in combined mesh exports

## Patch Changes
* Lowered aggressiveness of socket names
  * [173](https://github.com/poly-hammer/BlenderTools/issues/173)

## Special Thanks
@Daerst

## Tests Passing On
* Blender `3.6`, `4.2`, `5.0` (installed from blender.org)
* Unreal `5.3`, `5.4`
