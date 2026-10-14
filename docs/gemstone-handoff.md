# Gemstone material layers

`master_preset: gemstone` chooses the per-slot crystal pipeline even in an
otherwise `prop` asset. A delimited `Gemstone` material-name token provides a
fallback for older sidecars; explicit per-slot preset choices take precedence.

The shared contract resolves a dedicated layered Substrate master,
`ML_Gemstone_Crystal`, and unique MI/MYI folders under `/Game/Materials/Gemstone`.
The original `M_Gemstone_Crystal_Substrate` remains the crystal source graph.
These project assets must exist before importing the slot.

The producer sends no Painter/Blender appearance maps. Unreal owns all color and
detail controls. Initial import creates each material's MI and background MYI;
subsequent exports retain existing artist parameters and the nonempty layer.
This separates the staff inset and ornament gemstone controls.

Run `python -m unittest discover -s tests -p test_gemstone_handoff.py`.
